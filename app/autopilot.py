# -*- coding: utf-8 -*-
"""无人值守托管（Autopilot）——让电脑 24 小时自己生产漫剧

目标形态
--------
用户只做两件事：① 上传小说、建项目；② 用总控 AI 对话敲定风格与要求。
之后交给本守护进程：它按章节顺序把每一集从头跑到尾（剧本→资产→分镜→尾帧→
视频→成片→配音→混音），中途遇到质检不达标自动重试，只有超过阈值才挂起等人工。
用户最终只需要在「成品验收」里点通过 / 打回。

六个设计要点
------------
1. **单线程串行 + 资源互斥**
   只有一块 GPU，多个重任务并行只会互相抢显存导致全部变慢甚至 OOM。因此托管
   固定「同一时刻只跑一集的流水线」，用轮转（round-robin）在多个项目间公平推进。

2. **失败隔离**
   一集失败只影响这一集：记入死信并继续处理下一集 / 下一个项目。绝不因为某集
   剧本质量差就停掉整条生产线。

3. **幂等 + 可续跑**
   每集开始前由 pipeline 逐步骤探测产物，已完成的步骤直接跳过；服务重启后
   （`resume_on_start`）自动接着干，不会重复烧 GPU。

4. **死信上限**
   同一集连续失败达 `max_episode_attempts` 次 → 标记「需人工介入」并跳过，
   直到用户在处理里点「重试」或「忽略」。

5. **可观测**
   实时状态（当前项目 / 集号 / 步骤 / 进度 / 已跑时长 / 累计重试数 / 异常清单）
   全部暴露给前端；另有 24 小时生产曲线（每集完成时间与耗时）。

6. **可暂停**
   pause 只会在**步骤边界**生效（不会打断正在跑的 GPU 任务），保证不产生半成品。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback

logger = logging.getLogger(__name__)

# ===================== 状态与持久化 =====================

_LOCK = threading.RLock()          # 保护 _STATE（可重入：内部函数会互相调用）
_THREAD = None                     # 守护线程
_WAKE = threading.Event()          # 立即唤醒（新任务 / 启停时）
_STOP = threading.Event()          # 进程退出信号
_RUNTIME_RESTORED = False          # 运行时状态（暂停标记）是否已从磁盘恢复

#: 托管运行态（内存；供前端高频读取）
_STATE = {
    "running": False,              # 守护线程是否在跑
    "paused": False,               # 是否已暂停
    "current": None,               # {"project","episode","step","message","percent","started_at"}
    "last_error": "",
    "cycle": 0,                    # 已完成多少轮轮转
    "started_at": "",
    "checked_at": "",
    "totals": {"episodes_done": 0, "episodes_failed": 0, "retries": 0},
}

#: 每个项目连续失败计数（项目名 → {集号: 次数}）
_ATTEMPTS: dict = {}

#: 上一轮实际生产的 (项目, 集号)，以及同一集连续被生产的次数
#: 用于兜住「状态判定异常导致无限重跑同一集」这类问题（实测出现过 1 秒 121 次），
#: 对 24/7 进程这是必须的保命机制。
_LAST_RUN: dict = {"key": None, "repeat": 0, "at": 0.0}

#: 同一集两次生产之间的最小间隔（秒）：被打回/需重做时避免紧凑空转
MIN_RERUN_INTERVAL = 45

#: 托管轮询间隔（秒）：没有可做的活时休眠多久再扫一次
IDLE_SLEEP = 20

#: 默认托管计划
PLAN_DEFAULTS = {
    "enabled": False,              # 是否纳入托管
    "episodes": "all",             # "all" 或 [1,2,3]
    "priority": 0,                 # 数字越大越先生产
    "max_episode_attempts": 2,     # 同一集连续失败上限，超过即挂起等人工
    # ---- 传给 pipeline 的生产配置 ----
    "style": "",
    "target_shots": 12,
    "video_mode": "episode",
    "enable_assets": True,
    "enable_keyframe": False,
    "enable_video": True,
    "enable_final": True,
    "enable_tts": True,
    "enable_mix": True,
    # 超分（FlashVSR）：默认开启。必须列进 PLAN_DEFAULTS ——
    # api_autopilot_plan_set 会按 `k in PLAN_DEFAULTS` 过滤入参，
    # 不在此处的字段无法通过接口关闭，等于没有关掉的入口。
    "enable_upscale": True,
    "upscale_scale": 2,
    "coverage_min_percent": 95.0,
    "consistency_min_score": 80,
    "require_consistency": True,
    "step_max_retries": 2,
    "auto_repair": True,
    "overwrite_script": False,
    # ---- 无人值守（24h 托管）相关 ----
    # 挂起等人工的集，超过 N 小时自动复活重试。0 = 永不复活、必须人工处理。
    # 没有这个开关时，24h 跑一夜会攒下一堆「等人工」的集，第二天全堵在那儿。
    "auto_revive_hours": 6.0,
    # 成片产出后自动验收（不再堆在「待验收」里等人点）。
    # 注意：待验收**并不阻塞**后续集生产，这个开关只是消除人工动作、让看板干净。
    "auto_accept": False,
}


def _A():
    """延迟取宿主模块 app（避开循环导入：app 在加载期就会 import 本模块）"""
    import sys as _sys
    mod = _sys.modules.get("app")
    if mod is None:
        raise RuntimeError("宿主模块 app 尚未加载")
    return mod


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _autopilot_dir(project: str = "") -> str:
    root = os.path.join(_A().PROJECT_OUTPUT_DIR, "autopilot")
    return os.path.join(root, project) if project else root


def _nonempty(p: str) -> bool:
    try:
        return bool(p) and os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: str, default):
    if not _nonempty(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or default
    except Exception:  # noqa: BLE001
        return default


# ===================== 运行时状态持久化（暂停状态跨重启保持） =====================
# 为什么需要：托管会在服务启动时自动恢复生产（24/7 的核心诉求）。但如果用户是
# 主动「暂停」的（例如要检修 ComfyUI、腾出显存做别的事），重启后若无条件恢复，
# 就会在用户没预期的情况下立刻启动重任务。因此把「是否暂停」落盘，启动时尊重它。

def runtime_path() -> str:
    return os.path.join(_autopilot_dir(), "runtime.json")


def _persist_runtime() -> None:
    with _LOCK:
        data = {"paused": bool(_STATE.get("paused")),
                "pause_reason": _STATE.get("pause_reason") or "",
                "updated_at": _now()}
    try:
        _write_json(runtime_path(), data)
    except Exception as e:  # noqa: BLE001  落盘失败不得影响主流程
        logger.warning("托管运行时状态落盘失败（不影响本次运行）：%s", e)


def _restore_runtime() -> dict:
    """把上次的暂停状态读回内存；无记录则保持默认（未暂停）"""
    global _RUNTIME_RESTORED
    data = _read_json(runtime_path(), {}) or {}
    paused = bool(data.get("paused"))
    with _LOCK:
        _STATE["paused"] = paused
        _STATE["pause_reason"] = str(data.get("pause_reason") or "") if paused else ""
    _RUNTIME_RESTORED = True
    if paused:
        logger.info("托管：恢复上次的暂停状态（%s）", _STATE.get("pause_reason") or "无原因")
    return {"paused": paused, "pause_reason": _STATE.get("pause_reason") or ""}


def _restore_once() -> None:
    """懒加载式恢复（只做一次）：让 status()/is_paused() 在重启后立即反映上次的暂停状态"""
    if _RUNTIME_RESTORED:
        return
    try:
        _restore_runtime()
    except Exception as e:  # noqa: BLE001  读取失败不得影响服务
        logger.debug("托管运行时状态恢复失败（按未暂停处理）：%s", e)
        globals()["_RUNTIME_RESTORED"] = True


# ===================== 托管计划（每个项目一份） =====================


def plan_path(project_name: str) -> str:
    return os.path.join(_autopilot_dir(project_name), "plan.json")


def get_plan(project_name: str) -> dict:
    plan = dict(PLAN_DEFAULTS)
    plan.update(_read_json(plan_path(project_name), {}) or {})
    return plan


def set_plan(project_name: str, patch: dict, novel_id: str = "") -> dict:
    """写入托管计划（只接受已知字段，避免脏数据污染）"""
    plan = get_plan(project_name)
    for k, v in (patch or {}).items():
        if k in PLAN_DEFAULTS:
            plan[k] = v
    if novel_id:
        plan["novel_id"] = novel_id
    plan["updated_at"] = _now()
    _write_json(plan_path(project_name), plan)
    wake()
    return plan


def list_plans() -> list:
    """列出所有项目的托管计划（含未启用的，供前端展示全貌）"""
    A = _A()
    out = []
    seen = set()
    root = _autopilot_dir()
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if not os.path.isdir(os.path.join(root, name)):
                continue
            seen.add(name)
            out.append({"project": name, **get_plan(name)})
    # 项目注册表里存在但还没有托管计划的，也一并列出（默认为未启用）
    try:
        for rec in A.project_store.list_projects(with_stats=False):
            key = rec.get("dir_key") or rec.get("name")
            if key and key not in seen:
                out.append({"project": key, "name": rec.get("name"),
                            **get_plan(key)})
                seen.add(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("项目注册表读取失败（仅展示已有托管计划）：%s", e)
    out.sort(key=lambda x: (-int(x.get("priority") or 0), x.get("project") or ""))
    return out


def enable(project_name: str, patch: dict = None) -> dict:
    p = set_plan(project_name, {**(patch or {}), "enabled": True})
    _ensure_thread()
    return p


def disable(project_name: str) -> dict:
    return set_plan(project_name, {"enabled": False})


def enabled_projects() -> list:
    return [p for p in list_plans() if p.get("enabled")]


# ===================== 集列表与进度 =====================


def _novel_meta(project_name: str, plan: dict) -> dict:
    """取该项目关联的小说元信息（优先计划里记录的 novel_id）"""
    A = _A()
    novel_id = (plan.get("novel_id") or "").strip()
    if not novel_id:
        try:
            rec = A.project_store.get_project(project_name) or {}
            novel_id = (rec.get("novel_id") or "").strip()
        except Exception:  # noqa: BLE001
            novel_id = ""
    if not novel_id:
        return {}
    try:
        return A.get_novel(A.NOVELS_DIR, novel_id) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("小说元信息读取失败（%s）：%s", novel_id, e)
        return {}


def chapters_of(novel_meta: dict) -> list:
    """把小说切成章节列表（含正文切片），供逐集生产"""
    A = _A()
    if not novel_meta:
        return []
    text = ""
    try:
        text = A.read_novel_text(A.NOVELS_DIR, novel_meta.get("novel_id")) or ""
    except Exception as e:  # noqa: BLE001
        logger.warning("小说正文读取失败：%s", e)
    if not text:
        return []
    items = []
    for c in A.split_chapters(text):
        items.append({
            "index": int(c.get("index") or len(items) + 1),
            "title": c.get("title") or f"第{c.get('index')}章",
            "start": c.get("start") or 0,
            "end": c.get("end") or 0,
            "char_count": c.get("char_count") or 0,
        })
    return items


def target_episodes(chapters: list, plan: dict) -> list:
    """按计划里的 episodes 配置挑出要生产的集号"""
    all_no = [c["index"] for c in chapters]
    sel = plan.get("episodes", "all")
    if sel == "all" or sel is None:
        return all_no
    if isinstance(sel, (int, float)):
        return [int(sel)] if int(sel) in all_no else []
    if isinstance(sel, str):
        parts = [p.strip() for p in sel.replace("，", ",").split(",") if p.strip()]
        try:
            want = {int(p) for p in parts}
        except ValueError:
            return all_no
        return [n for n in all_no if n in want]
    if isinstance(sel, (list, tuple)):
        want = {int(x) for x in sel}
        return [n for n in all_no if n in want]
    return all_no


def deliverables_map(project_name: str) -> dict:
    """该项目的验收索引 {集号: item}

    注意：必须走 pipeline.list_deliverables（它会实时补 `exists` 字段），
    不能直接读 JSON —— 落盘的条目**没有** exists 键，直接读会把所有已产出
    的集误判为「未完成」，进而让守护进程无限重跑同一集（实测踩过：1 秒内
    重复生产 121 次）。
    """
    import pipeline
    out = {}
    for v in pipeline.list_deliverables(project_name):
        try:
            out[int(v.get("episode_no"))] = v
        except (TypeError, ValueError):
            continue
    return out


def _episode_state(project_name: str, episode_no: int, plan: dict, chapters: list) -> dict:
    """判断某集当前状态：done / rejected / pending_human / todo"""
    A = _A()
    try:
        import pipeline
    except ImportError:
        return {"state": "todo", "note": "流水线模块不可用"}

    dl = deliverables_map(project_name).get(int(episode_no))
    if dl:
        if dl.get("review") == "rejected":
            return {"state": "rejected", "note": "成片被打回，待重做"}
        if dl.get("exists"):
            return {"state": "done", "note": "已产出成片待验收",
                    "deliverable": dl.get("path"), "review": dl.get("review")}
        return {"state": "todo", "note": "成片文件已丢失，需重做"}
    dead = (pipeline.list_dead_letters(project_name) or [])
    for d in dead:
        if int(d.get("episode_no") or 0) == int(episode_no) and not d.get("resolved"):
            return {"state": "pending_human", "note": d.get("reason") or "需人工介入"}
    return {"state": "todo", "note": ""}


def project_progress(project_name: str, plan: dict = None) -> dict:
    """单个项目的逐集进度（供前端「分集进度」视图）"""
    plan = plan or get_plan(project_name)
    meta = _novel_meta(project_name, plan)
    chapters = chapters_of(meta)
    want = target_episodes(chapters, plan)
    rows = []
    done = 0
    for no in want:
        ch = next((c for c in chapters if c["index"] == no), {})
        st = _episode_state(project_name, no, plan, chapters)
        if st["state"] == "done":
            done += 1
        rows.append({"episode_no": no, "title": ch.get("title") or f"第{no}章",
                     "char_count": ch.get("char_count") or 0, **st})
    return {
        "project": project_name,
        "novel_id": meta.get("novel_id") or "",
        "novel_title": meta.get("title") or "",
        "enabled": bool(plan.get("enabled")),
        "total": len(want),
        "done": done,
        "percent": round(done / len(want) * 100, 1) if want else 0.0,
        "chapters_found": len(chapters),
        "episodes": rows,
    }


def all_progress() -> list:
    out = []
    for plan in list_plans():
        try:
            out.append(project_progress(plan["project"], plan))
        except Exception as e:  # noqa: BLE001
            logger.warning("项目 %s 进度计算失败：%s", plan.get("project"), e)
    return out


# ===================== 生产历史（24 小时曲线） =====================


def _history_path(project_name: str) -> str:
    return os.path.join(_autopilot_dir(project_name), "history.jsonl")


def append_history(project_name: str, episode_no: int, result: dict) -> None:
    p = _history_path(project_name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    row = {
        "project": project_name, "episode_no": int(episode_no),
        "ok": bool(result.get("ok")), "status": result.get("status"),
        "elapsed_sec": result.get("elapsed_sec"),
        "steps": {k: (v or {}).get("status") for k, v in (result.get("steps") or {}).items()},
        "error": (result.get("error") or "")[:300],
        "at": _now(), "ts": time.time(),
    }
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("生产历史写入失败：%s", e)


def read_history(project_name: str = "", hours: int = 24) -> list:
    A = _A()
    root = _autopilot_dir()
    projects = [project_name] if project_name else (
        sorted(os.listdir(root)) if os.path.isdir(root) else [])
    cutoff = time.time() - hours * 3600
    rows = []
    for pj in projects:
        p = _history_path(pj)
        if not _nonempty(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (row.get("ts") or 0) >= cutoff:
                        rows.append(row)
        except OSError:
            continue
    rows.sort(key=lambda x: x.get("ts") or 0)
    return rows


def production_curve(hours: int = 24, buckets: int = 24, project: str = "") -> dict:
    """把生产历史聚合成「每小时完成集数」曲线，并给出吞吐与平均耗时

    传入 project 时只统计该项目的生产历史 —— 否则一个从没跑过的新项目
    也会显示别的项目的产量，让人误以为「我的项目已经出片了」。
    """
    rows = read_history(project, hours=hours)
    if not rows:
        return {"hours": hours, "buckets": [], "episodes_done": 0,
                "episodes_failed": 0, "avg_elapsed_sec": 0, "throughput_per_hour": 0.0}
    now = time.time()
    span = max(hours * 3600 / max(buckets, 1), 1)
    series = [{"label": "", "done": 0, "failed": 0} for _ in range(buckets)]
    for i in range(buckets):
        start = now - (buckets - i) * span
        series[i]["label"] = time.strftime("%H:%M", time.localtime(start + span / 2))
    for row in rows:
        idx = int((buckets - 1) - ((now - (row.get("ts") or now)) // span))
        idx = max(0, min(buckets - 1, idx))
        series[idx]["done" if row.get("ok") else "failed"] += 1
    done = sum(1 for r in rows if r.get("ok"))
    failed = len(rows) - done
    secs = [r.get("elapsed_sec") or 0 for r in rows if r.get("ok")]
    avg = round(sum(secs) / len(secs), 1) if secs else 0
    return {
        "hours": hours, "buckets": series,
        "episodes_done": done, "episodes_failed": failed,
        "avg_elapsed_sec": avg,
        "throughput_per_hour": round(done / max(hours, 1), 2),
        "last_at": rows[-1].get("at"),
    }


# ===================== 托管主循环 =====================


def pause(reason: str = "") -> dict:
    with _LOCK:
        _STATE["paused"] = True
        _STATE["pause_reason"] = reason
    _persist_runtime()
    logger.info("托管已暂停：%s", reason or "手动暂停")
    return status()


def resume() -> dict:
    if not enabled_projects() and not any(p.get("enabled") for p in list_plans()):
        # 允许 resume 作为「启动」用；真正是否有活由循环判断
        pass
    with _LOCK:
        _STATE["paused"] = False
        _STATE["pause_reason"] = ""
    _persist_runtime()
    _ensure_thread()
    wake()
    return status()


def is_paused() -> bool:
    _restore_once()
    with _LOCK:
        return bool(_STATE["paused"])


def wake() -> None:
    """唤醒守护线程立即扫一轮（新任务入队 / 配置变更时调用）"""
    _WAKE.set()


def _ensure_thread() -> None:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="autopilot", daemon=True)
        _THREAD.start()
        _STATE["running"] = True
        _STATE["started_at"] = _STATE.get("started_at") or _now()
        logger.info("托管守护线程已启动")


def stop(timeout: float = 5.0) -> None:
    """进程退出时优雅停止（步骤边界生效）"""
    _STOP.set()
    wake()
    t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    with _LOCK:
        _STATE["running"] = False


def _set_current(**kw) -> None:
    with _LOCK:
        cur = dict(_STATE.get("current") or {})
        cur.update(kw)
        _STATE["current"] = cur


def _clear_current() -> None:
    with _LOCK:
        _STATE["current"] = None


def _loop() -> None:
    """守护主循环：轮转推进各项目的下一待生产集"""
    logger.info("托管循环开始")
    while not _STOP.is_set():
        try:
            did = _one_round()
        except Exception as e:  # noqa: BLE001  循环绝不能因单次异常退出
            logger.error("托管轮转异常：%s\n%s", e, traceback.format_exc())
            with _LOCK:
                _STATE["last_error"] = f"{type(e).__name__}: {e}"
            did = False
        with _LOCK:
            _STATE["checked_at"] = _now()
            _STATE["cycle"] = int(_STATE.get("cycle") or 0) + 1
            _STATE["running"] = True
        if did:
            continue                      # 有活干 → 立刻找下一件
        _WAKE.wait(timeout=IDLE_SLEEP)
        _WAKE.clear()
    with _LOCK:
        _STATE["running"] = False
    logger.info("托管循环已退出")


def _one_round() -> bool:
    """扫描一轮：找到第一个可生产的（项目, 集）并跑完它。返回是否有活可做"""
    if is_paused():
        return False
    for plan in enabled_projects():
        project = plan["project"]
        try:
            _auto_revive(project, plan)      # 先解挂超期的失败集，再挑下一集
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 自动复活扫描失败：%s", project, e)
        try:
            pick = _pick_episode(project, plan)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 集列表计算失败，跳过：%s", project, e)
            continue
        if not pick:
            continue
        _produce(project, plan, pick)
        return True
    return False


def _auto_revive(project: str, plan: dict) -> int:
    """自动复活：把「挂起等人工」超过 auto_revive_hours 的集重新放回队列。

    24h 无人值守下，失败集如果只能靠人工点掉，一夜下来就全堵死了。
    这里按时间自动解挂（默认 6 小时），并留痕说明是系统自动复活的。
    返回复活的集数。
    """
    hours = plan.get("auto_revive_hours")
    try:
        hours = float(hours if hours is not None else PLAN_DEFAULTS["auto_revive_hours"])
    except (TypeError, ValueError):
        hours = float(PLAN_DEFAULTS["auto_revive_hours"])
    if hours <= 0:
        return 0
    try:
        import pipeline
    except ImportError:
        return 0

    revived = 0
    for d in (pipeline.list_dead_letters(project) or []):
        if d.get("resolved"):
            continue
        marked = str(d.get("marked_at") or "").strip()
        try:
            t = time.mktime(time.strptime(marked, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            continue                       # 时间格式异常 → 不动它，交给人工
        if (time.time() - t) < hours * 3600:
            continue
        try:
            ep = int(d.get("episode_no"))
            pipeline.resolve_dead_letter(project, ep, note=f"自动复活（挂起已超过 {hours:g} 小时）")
            # 复活后要把尝试计数清零，否则下一次失败又会立刻被挂起
            _ATTEMPTS.setdefault(project, {})[ep] = 0
            revived += 1
            logger.info("%s 第%s集自动复活（挂起于 %s，已超过 %g 小时）",
                        project, ep, marked, hours)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 自动复活失败：%s", project, e)
    return revived


def _pick_episode(project: str, plan: dict):
    """挑出该项目下一个要生产的集：按顺序找第一个「未完成且不需人工」的集"""
    meta = _novel_meta(project, plan)
    if not meta:
        logger.debug("%s 未关联小说，跳过", project)
        return None
    chapters = chapters_of(meta)
    if not chapters:
        logger.debug("%s 小说正文为空或无章节，跳过", project)
        return None

    # 项目级资产：第 1 集之前必须先有资产（脚本里才有角色/场景可引用）
    want = target_episodes(chapters, plan)
    if not want:
        return None

    # 先看是否已有产出但被打回的集（优先重做，用户明确要求了）
    rows = {r["episode_no"]: r for r in
            (project_progress(project, plan).get("episodes") or [])}
    for no in want:
        r = rows.get(no) or {}
        if r.get("state") == "rejected" and _rerun_allowed(project, no):
            ch = next((c for c in chapters if c["index"] == no), {})
            return {"episode_no": no, "chapter": ch, "reason": "成片被打回，重新生产"}

    # 再按顺序推进第一个未完成的集
    for no in want:
        r = rows.get(no) or {}
        st = r.get("state")
        if st == "done":
            continue
        if st == "pending_human":
            continue
        chapter = next((c for c in chapters if c["index"] == no), {})
        if not chapter:
            continue
        # 连续失败超限 → 挂起等人工
        tries = _ATTEMPTS.get(project, {}).get(no, 0)
        if tries >= int(plan.get("max_episode_attempts") or 2):
            _mark_dead(project, no,
                       f"连续 {tries} 次生产失败，已挂起等人工处理")
            _ATTEMPTS.setdefault(project, {})[no] = 0
            continue
        # 防紧凑空转：同一集刚跑过就等一个间隔（打回重做也一样，不必贴着重跑）
        if not _rerun_allowed(project, no):
            continue
        return {"episode_no": no, "chapter": chapter, "reason": "按章节顺序推进"}
    return None


def _rerun_allowed(project: str, episode_no: int) -> bool:
    """该集是否已过最小重跑间隔（防状态异常导致的无限重跑）"""
    with _LOCK:
        if _LAST_RUN.get("key") != f"{project}#{episode_no}":
            return True
        return (time.time() - float(_LAST_RUN.get("at") or 0)) >= MIN_RERUN_INTERVAL


def _note_run(project: str, episode_no: int, ok: bool) -> None:
    """记录本次生产；同一集连续成功但状态未转为 done → 判定状态同步异常并挂起"""
    key = f"{project}#{episode_no}"
    with _LOCK:
        if _LAST_RUN.get("key") == key:
            _LAST_RUN["repeat"] = int(_LAST_RUN.get("repeat") or 0) + 1
        else:
            _LAST_RUN.update({"key": key, "repeat": 1})
        _LAST_RUN["at"] = time.time()
        repeat = _LAST_RUN["repeat"]
    if ok and repeat >= 3:
        # 产出物登记成功、却仍被判定为「未完成」→ 状态同步出了问题，别再烧 GPU
        _mark_dead(project, episode_no,
                   "已连续生成成功但进度仍未更新，疑似状态同步异常；"
                   "请检查成片文件是否存在、交付物索引是否可写",
                   {"repeat": repeat})
        with _LOCK:
            _LAST_RUN["repeat"] = 0
        return
    if ok:
        with _LOCK:
            _LAST_RUN["repeat"] = 0


def _mark_dead(project: str, episode_no: int, reason: str, detail: dict = None) -> None:
    try:
        import pipeline
        pipeline.mark_dead_letter(project, episode_no, reason, detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("死信标记失败：%s", e)


def _produce(project: str, plan: dict, pick: dict) -> None:
    """跑完一集的流水线，并把结果登记到交付物 / 历史 / 死信"""
    import pipeline

    episode_no = pick["episode_no"]
    chapter = pick["chapter"]
    meta = _novel_meta(project, plan)
    A = _A()
    t0 = time.time()

    _set_current(project=project, episode=episode_no,
                 title=chapter.get("title") or f"第{episode_no}章",
                 step="script", message=f"开始生产：{pick.get('reason')}",
                 percent=0, started_at=_now(), retries=0, phase="start",
                 steps_done=[])

    # 步骤链用于前端「跑到哪一步」可视化：pipeline 每进入一个新步骤就回调一次，
    # 这里把"上一个步骤"记为已完成，从而得到实时进度链。
    _seen: list = []
    _retries = [0]

    def _cb(message, percent, phase=None):
        import pipeline as _pl
        base = str(phase or "").split(":")[0]
        if ":retry" in str(phase or ""):
            _retries[0] += 1
        if base in _pl.STEP_SEQUENCE and base not in _seen:
            idx = _pl.STEP_SEQUENCE.index(base)
            _seen.extend(_pl.STEP_SEQUENCE[:idx])   # 此前的步骤都已完成
            _seen.append(base)
        _set_current(step=base or "running", message=message, percent=int(percent or 0),
                     steps_done=list(dict.fromkeys(_seen)), retries=_retries[0])

    try:
        config = pipeline.normalize_config({**plan, "novel_id": meta.get("novel_id") or ""},
                                           default_project_key=project)
        result = pipeline.run_episode(
            config, project, episode_no, meta, chapter,
            progress_cb=_cb, should_stop=is_paused)
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "status": "failed", "episode_no": episode_no,
                  "project": project, "error": f"{type(e).__name__}: {e}",
                  "elapsed_sec": round(time.time() - t0, 1), "steps": {}}
        logger.error("第%s集托管执行异常：%s\n%s", episode_no, e, traceback.format_exc())

    # ---- 结果登记 ----
    retries = sum(max(0, int((s or {}).get("attempts") or 1) - 1)
                  for s in (result.get("steps") or {}).values())
    with _LOCK:
        _STATE["totals"]["retries"] = int(_STATE["totals"].get("retries") or 0) + retries
    append_history(project, episode_no, result)

    produced_ok = bool(result.get("ok") and result.get("deliverable"))
    _note_run(project, episode_no, produced_ok)

    if produced_ok:
        try:
            pipeline.record_deliverable(project, episode_no, result["deliverable"], meta={
                "title": chapter.get("title") or "",
                "chapter_index": chapter.get("index"),
                "elapsed_sec": result.get("elapsed_sec"),
                "retries": retries,
                "steps": {k: (v or {}).get("status")
                          for k, v in (result.get("steps") or {}).items()},
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("交付物登记失败：%s", e)
        # 无人值守：产出即自动验收（待验收不阻塞生产，这里只是替人点掉那一下）
        if plan.get("auto_accept"):
            try:
                pipeline.set_deliverable_review(project, episode_no, "accepted",
                                                note="自动验收（托管 auto_accept）")
            except Exception as e:  # noqa: BLE001
                logger.warning("自动验收失败：%s", e)
        with _LOCK:
            _STATE["totals"]["episodes_done"] = int(_STATE["totals"].get("episodes_done") or 0) + 1
        _ATTEMPTS.setdefault(project, {})[episode_no] = 0
        logger.info("第%s集生产完成：%s", episode_no, result.get("deliverable"))
    elif result.get("status") == "cancelled":
        logger.info("第%s集因托管暂停中止（已完成步骤已保留，可续跑）", episode_no)
    else:
        with _LOCK:
            _STATE["totals"]["episodes_failed"] = int(_STATE["totals"].get("episodes_failed") or 0) + 1
            _STATE["last_error"] = result.get("error") or "未知错误"
        if result.get("status") == "needs_human":
            # 环境性问题（模型未配置 / ffmpeg 缺失等）：直接挂起等人工，不浪费尝试次数
            _mark_dead(project, episode_no, result.get("error") or "需人工介入",
                       {"status": result.get("status")})
        else:
            _ATTEMPTS.setdefault(project, {})[episode_no] = \
                _ATTEMPTS.get(project, {}).get(episode_no, 0) + 1
            logger.warning("第%s集生产失败（第 %d 次）：%s", episode_no,
                           _ATTEMPTS[project][episode_no], result.get("error"))

    _clear_current()


# ===================== 对外状态 =====================


def status(project: str = "") -> dict:
    """托管总览（前端主视图据此渲染）
    若传入 project，则只返回该项目状态；否则聚合所有项目。
    """
    _restore_once()
    with _LOCK:
        st = json.loads(json.dumps(_STATE, ensure_ascii=False, default=str))
    plans = list_plans()
    enabled = [p for p in plans if p.get("enabled")]
    deliveries = []
    exceptions = []
    target_projects = [project] if project else [p["project"] for p in plans]
    for p in plans:
        if project and p["project"] != project:
            continue
        try:
            deliveries.extend(pipeline_list_deliverables(p["project"]))
            exceptions.extend(pipeline_list_dead(p["project"]))
        except Exception as e:  # noqa: BLE001
            logger.warning("状态聚合失败（%s）：%s", p.get("project"), e)
    # 传入 project 时，开关/计划数也要收敛到该项目 ——
    # 否则前端按项目查询时，会看到别的项目开启托管而误判自己已开启。
    if project:
        mine = [p for p in plans if p.get("project") == project]
        enabled_count = sum(1 for p in mine if p.get("enabled"))
        plan_count = len(mine)
    else:
        enabled_count = len(enabled)
        plan_count = len(plans)
    st.update({
        # 回显作用域：调用方（含 AI 总控）必须能一眼看出这份数字属于哪个项目，
        # 否则模型会拿历史对话里的项目名去「对号入座」，把 A 的数据说成 B 的。
        "project": project,
        "scoped": bool(project),
        "enabled_count": enabled_count,
        "plan_count": plan_count,
        "pending_review": sum(1 for d in deliveries if d.get("review") == "pending"
                              and d.get("exists")),
        "delivered_total": len(deliveries),
        "exceptions": len([e for e in exceptions if not e.get("resolved")]),
        "curve": production_curve(24, project=project),
    })
    return st


def pipeline_list_deliverables(project: str) -> list:
    import pipeline
    return pipeline.list_deliverables(project)


def pipeline_list_dead(project: str) -> list:
    import pipeline
    return pipeline.list_dead_letters(project)


def ready(plan: dict = None) -> dict:
    """托管可行性自检（告诉用户还差什么才能真正无人值守）"""
    A = _A()
    checks = []
    try:
        client = A._current_llm_client()
        ok = bool(client and getattr(client, "configured", False))
    except Exception:  # noqa: BLE001
        ok = False
    checks.append({"key": "text_model", "label": "文本模型（剧本生成）", "ok": ok,
                   "hint": "" if ok else "请到「AI 设置 → 文本分析」配置接口"})
    try:
        cfg = A._qc_load_cfg()
        qc_img = A.qc_client.image_qc_ready(cfg)
        qc_vid = A.qc_client.video_qc_ready(cfg)
    except Exception:  # noqa: BLE001
        qc_img = qc_vid = False
    checks.append({"key": "qc", "label": "AI 质检（图片/视频）",
                   "ok": bool(qc_img and qc_vid),
                   "hint": "" if (qc_img and qc_vid) else
                           "质检未开启时产物将不做 AI 判定，建议在「AI 设置 → 质检」开启"})
    try:
        env = A.mix_ffmpeg_check()
        ff = bool(env.get("available"))
        ff_hint = "" if ff else "；".join(env.get("reasons") or [])
    except Exception as e:  # noqa: BLE001
        ff, ff_hint = False, str(e)
    checks.append({"key": "ffmpeg", "label": "FFmpeg（成片/混音）", "ok": ff, "hint": ff_hint})
    try:
        st = A.comfyui_client.get_status()
        cok = st.get("status") == "online"
    except Exception:  # noqa: BLE001
        cok = False
    checks.append({"key": "comfyui", "label": "ComfyUI 生成引擎", "ok": cok,
                   "hint": "" if cok else "ComfyUI 未启动或不可达；无人值守期间需保持运行"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
