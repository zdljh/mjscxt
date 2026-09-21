# -*- coding: utf-8 -*-
"""自动生产流水线（Pipeline）——「一章小说 → 一集可交付成片」的无人值守编排

为什么需要
----------
改造前所有环节都是**手动触发**：前端点一次「生成剧本」、再点「生成资产」、
再点「生成分镜」…… 中途任何一步断了都要人工补。这无法支撑「电脑 24 小时
自动生产、人只看最终成品」的需求。

本模块把整条链路编排成一条**流水线**，由 autopilot 守护进程驱动：

    script → assets* → storyboard → keyframe? → video → final → tts → mix
                                                              └→ deliverable

（assets 为项目级资产，只在首集前跑一次；keyframe 为可选模式）

三个关键设计
------------
1. **幂等 + 断点续跑**
   每步先探测产物是否已存在且有效，存在即跳过。崩溃 / 重启 / 重跑都不会重复
   烧 GPU —— 这是 24 小时无人值守的基础。

2. **质检门禁 + 自动重试**
   复用既有质检链路（脚本原文覆盖率、图片/视频 AI 质检、跨镜角色一致性）。
   不达标**自动重试**（重试时换 seed 提高多样性），超过上限才升级为
   「需人工介入」。**绝不静默放行不达标产物**。

3. **失败隔离**
   单步失败只影响该集：该集标记失败并进入异常队列，其它集与后续集不受影响。

与 app.py 的关系
----------------
本模块不复制任何业务逻辑，全部通过**延迟取宿主模块**复用 app.py 里既有的
worker（`_storyboard_worker` / `_video_generate_worker` / `_dub_worker` /
`_mix_worker` …）。延迟访问（而非顶层 import）是为了避开循环导入：
app.py 在模块加载期 import 本模块，此时 app 尚未完成初始化。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import traceback

import cancellation

logger = logging.getLogger(__name__)

# ===================== 步骤定义 =====================

#: 步骤顺序（键即步骤 id）
#: upscale 放在最末：超分针对「已配音混音」的成片做画质增强，放前面会被后续混音覆盖
STEP_SEQUENCE = ("script", "assets", "storyboard", "keyframe", "video", "final", "tts", "mix",
                 "upscale")

STEP_LABELS = {
    "script": "剧本生成",
    "assets": "资产（角色/物品/场景）",
    "storyboard": "分镜图",
    "keyframe": "尾帧（关键帧驱动）",
    "video": "视频生成",
    "final": "成片合成",
    "tts": "配音合成",
    "mix": "音画对齐混音",
    "upscale": "超分（FlashVSR）",
}

#: 需要「质检门禁」的步骤（不达标必须重试，不允许静默通过）
GATED_STEPS = ("script", "storyboard", "video")

#: 单次占用的 GPU 重任务步骤（守护进程据此做资源互斥，避免抢显存）
GPU_STEPS = ("storyboard", "keyframe", "video", "upscale")

DEFAULT_CONFIG = {
    # ---- 输入 ----
    "novel_id": "",                # 小说 id（必填）
    "project_key": "",             # 项目键（缺省由 novel_id 推导）
    "style": "",                   # 总控 AI 敲定的风格描述
    "target_shots": 12,            # 每集目标镜头数
    # ---- 范围 ----
    "episodes": "all",             # "all" 或 [1,2,3]
    "overwrite_script": False,     # 是否覆盖已存在的剧本
    # ---- 各环节开关 ----
    "enable_assets": True,
    "enable_keyframe": False,
    "enable_video": True,
    "enable_final": True,
    "enable_tts": True,
    "enable_mix": True,
    # 超分（FlashVSR）：默认开启。⚠️ 这一步是画质增强而非出片必需环节，
    # step_upscale 全程 fail-open——环境不可用或执行失败一律记 skipped，
    # 绝不把已经跑通的成片拖成失败。
    "enable_upscale": True,
    "upscale_scale": 2,            # 超分倍率，FlashVSR 支持 2 / 3 / 4
    "video_mode": "episode",       # episode（整集一次生成，连续无缝）/ per_shot（逐镜独立）/ keyframe
    # 关键帧「跨镜链式」：auto=同场景才串 / always=无条件串 / off=关闭。
    # 上一镜尾帧作为下一镜首帧参考，镜与镜首尾相接，避免每镜各画各的。
    "keyframe_chain_mode": "auto",
    # ---- 质量阈值 ----
    "coverage_min_percent": 95.0,      # 原文覆盖率下限
    "consistency_min_score": 80,       # 跨镜一致性分数下限
    "require_consistency": True,
    "step_max_retries": 2,             # 单步失败重试次数上限
    # ---- 运行时 ----
    "auto_repair": True,               # 失败自动补救（换 seed / 重生成）
    "seed": None,
}


def normalize_config(raw: dict, default_project_key: str = "") -> dict:
    """把外部传入的配置补齐为完整可用配置（并做类型收敛）"""
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (raw or {}).items():
        if k in cfg or k in ("novel_id", "project_key"):
            cfg[k] = v
    # 类型收敛（配置可能来自前端 JSON，类型不可信）
    for k in ("target_shots", "consistency_min_score", "step_max_retries"):
        try:
            cfg[k] = int(cfg.get(k))
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CONFIG[k]
    # 超分倍率：非法值一律回落 2（FlashVSR 只支持 2/3/4，传错会让该步直接跳过）
    try:
        cfg["upscale_scale"] = int(cfg.get("upscale_scale"))
    except (TypeError, ValueError):
        cfg["upscale_scale"] = DEFAULT_CONFIG["upscale_scale"]
    if cfg["upscale_scale"] not in (2, 3, 4):
        cfg["upscale_scale"] = DEFAULT_CONFIG["upscale_scale"]
    try:
        cfg["coverage_min_percent"] = float(cfg.get("coverage_min_percent"))
    except (TypeError, ValueError):
        cfg["coverage_min_percent"] = DEFAULT_CONFIG["coverage_min_percent"]
    for k in ("enable_assets", "enable_keyframe", "enable_video", "enable_final",
              "enable_tts", "enable_mix", "enable_upscale", "require_consistency",
              "auto_repair", "overwrite_script"):
        cfg[k] = bool(cfg.get(k))
    if cfg.get("video_mode") not in ("per_shot", "episode", "keyframe"):
        cfg["video_mode"] = "per_shot"
    if cfg["video_mode"] == "keyframe":
        cfg["enable_keyframe"] = True      # 关键帧模式必须先生成尾帧
    try:
        import keyframe as _kf
        cfg["keyframe_chain_mode"] = _kf.norm_chain_mode(cfg.get("keyframe_chain_mode"))
    except Exception:  # noqa: BLE001
        cfg["keyframe_chain_mode"] = "auto"
    if not cfg.get("project_key"):
        cfg["project_key"] = default_project_key or cfg.get("novel_id") or ""
    return cfg


# ===================== 异常 =====================


class PipelineError(RuntimeError):
    """流水线一般性错误（会触发重试）"""


class NeedsHumanError(PipelineError):
    """重试已耗尽 / 环境性故障，需要人工介入（不再重试）"""


# ===================== 宿主访问 =====================


def _A():
    """取宿主模块 app（延迟访问，避开循环导入）"""
    import sys as _sys
    mod = _sys.modules.get("app")
    if mod is None:
        raise PipelineError("宿主模块 app 尚未加载，无法运行流水线")
    return mod


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ===================== 通用工具 =====================


def _run_task_worker(worker, args, registry_name: str, lock_name: str,
                     init: dict = None, prefix: str = "pipe") -> dict:
    """在线程内直接执行一个「写任务字典」的既有 worker，返回其最终状态

    既有 worker（`_storyboard_worker` / `_dub_worker` / `_mix_worker` …）都以
    `task_id` 作为第一个参数，并把进度与结果写进各自的全局任务字典。流水线
    为每次调用生成一个内部 task_id，预置初始状态，调用后读取最终状态并把该
    条目清掉（避免污染前端可见的任务列表）。
    """
    A = _A()
    registry = getattr(A, registry_name)
    lock = getattr(A, lock_name)
    tid = f"{prefix}_{int(time.time() * 1000)}_{os.getpid()}"
    state = {"status": "running", "progress": 0, "phase": "准备中",
             "results": [], "pipeline": True, "created_at": _now()}
    if init:
        state.update(init)
    with lock:
        registry[tid] = state
    err = ""
    try:
        worker(tid, *args)
    except Exception as e:  # noqa: BLE001  单步异常向上抛，由重试层决定
        err = f"{type(e).__name__}: {e}"
        logger.error("流水线 worker 异常（%s）：%s\n%s", prefix, err, traceback.format_exc())
    with lock:
        final = dict(registry.get(tid) or {})
        registry.pop(tid, None)      # 内部任务不留在 UI 列表
    if err and not final.get("error"):
        final["error"] = err
        final.setdefault("status", "failed")
    return final


def _outcome_from_task(final: dict, what: str) -> dict:
    """把既有 worker 的任务状态归一化成流水线可判定的结果"""
    status = (final or {}).get("status") or "failed"
    results = (final or {}).get("results") or []
    ok_cnt = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
    # 质检阻断计数：资产/分镜/视频 worker 都会打 qc_blocked
    blocked = sum(1 for r in results if isinstance(r, dict) and r.get("qc_blocked"))
    # 「全部被合法跳过」不是失败。
    # 最典型的场景：物品卡里只有临时道具 —— _generate_asset_task 对
    # importance=临时 的物体**有意不生成参考图**，results 里只留 status=skipped，
    # 于是 ok_cnt=0。历史实现按「results 非空且无成功」直接判失败，导致整集在
    # assets 步骤反复失败并挂起等人工介入（实测：雨夜归人 第2集只有「深色长柄伞」
    # 一个临时道具，连续 2 次失败被标记需人工处理）。
    skipped_cnt = sum(1 for r in results if isinstance(r, dict)
                      and (str(r.get("status") or "") == "skipped" or r.get("skipped")))
    all_skipped = bool(results) and skipped_cnt == len(results)
    if status == "failed" or (results and not ok_cnt and not all_skipped):
        return {"ok": False, "blocked": bool(blocked), "count": ok_cnt,
                "error": (final or {}).get("error") or f"{what}失败",
                "detail": final}
    return {"ok": True, "blocked": bool(blocked), "count": ok_cnt, "error": "", "detail": final}


# ===================== 产物探测（幂等 / 断点续跑判据） =====================


def _nonempty(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _playable(path: str) -> bool:
    """G7：视频产物「可播放性」判据（取代「文件存在 + size>0」）。

    一次超时/被杀留下的半截 mp4 往往远超 1KB，旧判据 `_nonempty` 会把半截文件
    当「已完成」→ 后续步骤永久跳过、坏成片进验收。现在要求 ffprobe 能读出
    **视频流且 duration>0** 才算就绪。

    降级策略：ffprobe 二进制缺失时退回「存在 + 非空」（不能让工具链不齐导致
    整条流水线误判全部未就绪而重烧全部镜头）；文件存在但 ffprobe 解析失败
    / 无视频流 / 时长为 0 → 判未就绪（会重跑）。
    仅用于 .mp4 等视频产物；图片类产物（分镜/尾帧 .png）仍用 `_nonempty`。
    """
    if not _nonempty(path):
        return False
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("ffprobe"):
        return True  # ffprobe 不可用：降级为「存在 + 非空」，避免误伤全流水线
    try:
        r = _sp.run(["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_type:format=duration", "-of", "json", path],
                    capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False
        d = json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001 - 解析失败 = 半截/损坏，判未就绪
        return False
    streams = d.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    try:
        dur = float((d.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return bool(has_video and dur > 0)


def probe_script(ctx) -> bool:
    A = _A()
    p = A.novel_to_script.episode_script_path(A.SCRIPT_DIR, ctx["project_key"], ctx["episode_no"])
    return _nonempty(p)


def probe_assets(ctx) -> dict:
    """资产就绪判据：剧本里的每个角色/物品/场景都有非空基础图"""
    A = _A()
    script = ctx.get("script") or {}
    out = {}
    for kind, dir_key, script_key in (
        ("character", "CHARACTERS_DIR", "characters"),
        ("item", "ITEMS_DIR", "items"),
        ("scene", "SCENES_DIR", "scenes"),
    ):
        base = os.path.join(getattr(A, dir_key), ctx["project_name"])
        names = [str(a.get("name") or "").strip()
                 for a in (script.get(script_key) or []) if isinstance(a, dict)]
        names = [n for n in names if n]
        ready, missing = 0, []
        for n in names:
            d = os.path.join(base, n)
            hit = None
            for cand in ("front.png", "base.png"):
                if _nonempty(os.path.join(d, cand)):
                    hit = cand
                    break
            if not hit:
                # 目录扫描兜底：任何一张非空图都算就绪
                if os.path.isdir(d):
                    for fn in os.listdir(d):
                        if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) \
                                and _nonempty(os.path.join(d, fn)):
                            hit = fn
                            break
            if hit:
                ready += 1
            else:
                missing.append(n)
        out[kind] = {"total": len(names), "ready": ready, "missing": missing,
                     "done": bool(names) and not missing}
    # 顶层 done：三类资产都齐全才算就绪（step_assets 据此整步跳过，避免每天重跑都
    # 全量重生成资产 —— 这是「断点续跑」在资产步骤上的落点）
    out["done"] = all(bool(v.get("done")) for v in out.values() if isinstance(v, dict))
    out["kinds_ready"] = sum(1 for v in out.values()
                             if isinstance(v, dict) and v.get("done"))
    out["kinds_total"] = 3
    return out


def probe_storyboard(ctx) -> dict:
    A = _A()
    d = A._ep_dir(os.path.join(A.STORYBOARDS_DIR, ctx["project_name"]), ctx["episode_no"])
    shots = (ctx.get("script") or {}).get("shots") or []
    missing = []
    for i, s in enumerate(shots):
        seq = A._shot_seq(s.get("shot_id", i + 1), i + 1)
        if not _nonempty(os.path.join(d, f"shot_{seq:02d}.png")):
            missing.append(s.get("shot_id", i + 1))
    return {"total": len(shots), "ready": len(shots) - len(missing),
            "missing": missing, "done": bool(shots) and not missing, "dir": d}


def probe_keyframe(ctx) -> dict:
    A = _A()
    d = A._ep_dir(os.path.join(A.KEYFRAMES_DIR, ctx["project_name"]), ctx["episode_no"])
    shots = (ctx.get("script") or {}).get("shots") or []
    missing = []
    for i, s in enumerate(shots):
        seq = A._shot_seq(s.get("shot_id", i + 1), i + 1)
        if not _nonempty(os.path.join(d, f"shot_{seq:02d}_end.png")):
            missing.append(s.get("shot_id", i + 1))
    return {"total": len(shots), "ready": len(shots) - len(missing),
            "missing": missing, "done": bool(shots) and not missing, "dir": d}


def probe_video(ctx) -> dict:
    A = _A()
    d = A._ep_dir(os.path.join(A.VIDEOS_DIR, ctx["project_name"]), ctx["episode_no"])
    mode = ctx["config"].get("video_mode") or "per_shot"
    if mode == "episode":
        tag = ctx.get("episode_tag") or f"ep{ctx['episode_no']:02d}"
        hit = None
        for cand in (f"{tag}_full.mp4", "episode_full.mp4"):
            if _playable(os.path.join(d, cand)):
                hit = cand
                break
        return {"total": 1, "ready": 1 if hit else 0, "missing": [] if hit else ["整集"],
                "done": bool(hit), "dir": d, "file": os.path.join(d, hit) if hit else ""}
    shots = (ctx.get("script") or {}).get("shots") or []
    missing = []
    for i, s in enumerate(shots):
        seq = A._shot_seq(s.get("shot_id", i + 1), i + 1)
        if not _playable(os.path.join(d, f"shot_{seq:02d}.mp4")):
            missing.append(s.get("shot_id", i + 1))
    return {"total": len(shots), "ready": len(shots) - len(missing),
            "missing": missing, "done": bool(shots) and not missing, "dir": d}


def final_path(ctx) -> str:
    A = _A()
    return os.path.join(A.FINAL_DIR, ctx["project_name"], f"ep{ctx['episode_no']:02d}_final.mp4")


def probe_final(ctx) -> dict:
    p = final_path(ctx)
    return {"total": 1, "ready": 1 if _playable(p) else 0, "done": _playable(p), "file": p}


def dub_manifest_path(ctx) -> str:
    A = _A()
    return os.path.join(A.DUB_DIR, ctx["project_name"],
                        f"ep{ctx['episode_no']:02d}_dub_manifest.json")


def probe_tts(ctx) -> dict:
    p = dub_manifest_path(ctx)
    if not _nonempty(p):
        return {"total": 0, "ready": 0, "done": False, "file": p}
    try:
        with open(p, "r", encoding="utf-8") as f:
            mf = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {"total": 0, "ready": 0, "done": False, "file": p}
    lines = [l for l in (mf.get("lines") or []) if isinstance(l, dict)]
    ok = [l for l in lines if l.get("ok") and _nonempty(l.get("out_path") or "")]
    return {"total": len(lines), "ready": len(ok),
            "done": bool(lines) and len(ok) == len(lines), "file": p,
            "missing": [l.get("line_id") for l in lines if l not in ok]}


def mix_output_path(ctx) -> str:
    """混音产物路径（流水线固定命名，便于幂等探测）"""
    A = _A()
    return os.path.join(A.mix_out_dir(ctx["project_name"]),
                        f"ep{ctx['episode_no']:02d}_dubbed.mp4")


def probe_mix(ctx) -> dict:
    p = mix_output_path(ctx)
    return {"total": 1, "ready": 1 if _playable(p) else 0, "done": _playable(p), "file": p}


def upscale_path(ctx) -> str:
    """超分产物路径（流水线固定命名，便于幂等探测与「成品验收」直接引用）

    VideoUpscaler 自身产出的文件名带时间戳（不可幂等），因此流水线会把结果
    归档到该确定性路径，重跑时 probe 命中即跳过。
    """
    A = _A()
    return os.path.join(A.UPSCALE_DIR, ctx["project_name"],
                        f"ep{ctx['episode_no']:02d}_upscaled.mp4")


def probe_upscale(ctx) -> dict:
    p = upscale_path(ctx)
    return {"total": 1, "ready": 1 if _playable(p) else 0, "done": _playable(p), "file": p}


PROBES = {
    "script": probe_script,
    "assets": probe_assets,
    "storyboard": probe_storyboard,
    "keyframe": probe_keyframe,
    "video": probe_video,
    "final": probe_final,
    "tts": probe_tts,
    "mix": probe_mix,
    "upscale": probe_upscale,
}


# ===================== 步骤实现 =====================
# 每个步骤函数返回 dict：{"ok": bool, "skipped": bool, "blocked": bool,
#                          "error": str, "detail": {...}, "artifact": str}


def step_script(ctx) -> dict:
    """章节正文 → 结构化剧本（含原文覆盖率守门 + 跨集连贯性编排）"""
    A = _A()
    if probe_script(ctx) and not ctx["config"].get("overwrite_script"):
        return {"ok": True, "skipped": True, "artifact": _script_path(ctx),
                "detail": {"note": "剧本已存在，跳过（断点续跑）"}}

    client = A._current_llm_client()
    if not client or not getattr(client, "configured", False):
        raise NeedsHumanError("尚未配置文本模型接口（AI 设置 → 文本分析），无法生成剧本")

    text = A.read_novel_text(A.NOVELS_DIR, ctx["novel_meta"]["novel_id"])
    if not text:
        raise NeedsHumanError("小说正文为空，无法生成剧本")

    chapter = ctx["chapter"]
    cfg = ctx["config"]

    def _cb(phase, cur, tot, msg, pct):
        ctx["progress"](f"第{ctx['episode_no']}集剧本：{msg}", 4 + int((pct or 0) * 0.14),
                        phase=phase)

    conv = A.continuity.convert_chapter_with_continuity(
        client, ctx["novel_meta"], text, chapter, ctx["project_key"], A.CONTINUITY_DIR,
        style=cfg.get("style") or "", target_shots=int(cfg.get("target_shots") or 12),
        episode_no=ctx["episode_no"], save_dir=A.SCRIPT_DIR, progress_cb=_cb,
    )
    script = conv.get("script") or {}
    cov = conv.get("coverage") or {}
    val = conv.get("validation") or {}
    path = (conv.get("script_path")
            or A.novel_to_script.save_episode_script(
                script, A.SCRIPT_DIR, ctx["project_key"], ctx["episode_no"], ctx["project_key"]))

    detail = {
        "script_path": path,
        "shots": len(script.get("shots") or []),
        "characters": len(script.get("characters") or []),
        "items": len(script.get("items") or []),
        "scenes": len(script.get("scenes") or []),
        "coverage_percent": cov.get("coverage_percent"),
        "coverage_passed": cov.get("passed"),
        "continuity_score": (script.get("metadata") or {}).get("continuity", {}).get("validation_score"),
        "continuity_issues": len(val.get("issues") or []),
        "continuity_rewrite": bool((conv.get("rewrite") or {}).get("triggered")),
    }
    # 覆盖率门禁：不达标即判失败，交由重试层处理（绝不静默放行）
    floor = float(cfg.get("coverage_min_percent") or 0)
    pct = cov.get("coverage_percent")
    if floor > 0 and isinstance(pct, (int, float)) and pct < floor:
        return {"ok": False, "artifact": path, "detail": detail,
                "error": f"原文覆盖率 {pct}% 低于阈值 {floor}%（{len(cov.get('missing') or [])} 处遗漏）"}
    ctx["script"] = script
    return {"ok": True, "artifact": path, "detail": detail}


def _script_path(ctx) -> str:
    A = _A()
    return A.novel_to_script.episode_script_path(A.SCRIPT_DIR, ctx["project_key"], ctx["episode_no"])


def step_assets(ctx) -> dict:
    """角色 / 物品 / 场景资产（项目级；三种类型各自带 AI 质检与重生成）"""
    A = _A()
    script = ctx.get("script") or {}
    pd = probe_assets(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd},
                "artifact": os.path.join(A.CHARACTERS_DIR, ctx["project_name"])}

    results, errs = {}, []
    for kind, script_key in (("character", "characters"), ("item", "items"), ("scene", "scenes")):
        if pd.get(kind, {}).get("done"):
            results[kind] = {"skipped": True, "total": pd[kind]["total"]}
            continue
        assets = [a for a in (script.get(script_key) or []) if isinstance(a, dict)]
        if not assets:
            results[kind] = {"skipped": True, "total": 0, "note": "剧本中无此类资产"}
            continue
        # S13 断点续跑：probe_assets 已算出本类 missing（缺 base 图）名单；只把缺失项传给
        # worker，已就绪资产不重烧 —— 30 个角色缺 1 个时不再重跑 30 次基础图 + 多视图 + 质检
        missing_names = {n for n in (pd.get(kind, {}).get("missing") or [])}
        todo = [a for a in assets
                if str((a.get("name") or "").strip()) in missing_names]
        if not todo:
            # 本类顶层未 done（顶层 done 需三类全齐）但本类无缺失 → 视为已就绪
            results[kind] = {"skipped": True, "total": len(assets), "note": "本类资产已就绪"}
            continue
        ctx["progress"](f"生成{kind}资产（缺 {len(todo)}/{len(assets)} 个）", 20,
                        phase=f"assets:{kind}")
        final = _run_task_worker(
            A._generate_asset_task, (todo, kind, ctx["project_name"], ctx["config"].get("style") or ""),
            "generation_state", "lock",
            init={"total": len(todo), "asset_type": kind, "phase": f"{kind} 资产"},
            prefix=f"pipe_asset_{kind}")
        out = _outcome_from_task(final, f"{kind} 资产生成")
        results[kind] = {"total": len(assets), "generated": len(todo),
                         "ok": out["ok"], "count": out["count"],
                         "blocked": out["blocked"], "error": out["error"]}
        if not out["ok"]:
            errs.append(f"{kind}: {out['error']}")
    if errs:
        return {"ok": False, "detail": {"results": results}, "error": "；".join(errs)}
    return {"ok": True, "detail": {"results": results}}


def step_storyboard(ctx) -> dict:
    """分镜图（逐镜生成，图片 AI 质检不达标自动换 seed 重生成并可阻断入库）"""
    A = _A()
    pd = probe_storyboard(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["dir"]}
    script = ctx.get("script") or {}
    shots = script.get("shots") or []
    if not shots:
        raise NeedsHumanError("剧本没有镜头数据，无法生成分镜图")

    char_idx = A._build_asset_index(script.get("characters") or [], ctx["project_name"], "character")
    item_idx = A._build_asset_index(script.get("items") or [], ctx["project_name"], "item")
    scene_idx = A._build_asset_index(script.get("scenes") or [], ctx["project_name"], "scene")
    ctx["progress"](f"生成分镜图（{len(shots)} 镜）", 34, phase="storyboard")
    final = _run_task_worker(
        A._storyboard_worker, (ctx["project_name"], shots, char_idx, item_idx, scene_idx,
                               ctx["episode_no"], ctx["config"].get("style") or ""),
        "generation_state", "lock",
        init={"total": len(shots), "phase": "分镜图生成"},
        prefix="pipe_sb")
    out = _outcome_from_task(final, "分镜图生成")
    detail = {"count": out["count"], "total": len(shots), "blocked": out["blocked"],
              "qc_blocked": (final or {}).get("qc_blocked_count")}
    if not out["ok"]:
        return {"ok": False, "blocked": out["blocked"], "detail": detail, "error": out["error"]}
    # 复核产物：worker 报成功但文件缺失 / 为空 → 仍然判失败（不能凭状态字段放行）
    recheck = probe_storyboard(ctx)
    detail["probe"] = recheck
    if not recheck.get("done"):
        return {"ok": False, "detail": detail,
                "error": f"分镜图缺失 {len(recheck.get('missing') or [])} 镜："
                         f"{recheck.get('missing')[:8]}"}
    return {"ok": True, "detail": detail, "artifact": recheck["dir"]}


def step_keyframe(ctx) -> dict:
    """尾帧生成（关键帧驱动模式的前置；走 keyframe 模块，支持断点续跑）"""
    A = _A()
    pd = probe_keyframe(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["dir"]}
    script = ctx.get("script") or {}
    shots = script.get("shots") or []
    if not shots:
        raise NeedsHumanError("剧本没有镜头数据，无法生成尾帧")
    kf_dir = A._keyframes_dir(ctx["project_name"], ctx["episode_no"])
    sb_map = A._keyframe_sb_map(ctx["project_name"], script, episode_no=ctx["episode_no"])
    ctx["progress"](f"生成尾帧（{len(shots)} 镜）", 42, phase="keyframe")

    def _cb(done, total, item):
        pct = 42 + int((done / max(total, 1)) * 40)
        ctx["progress"](f"尾帧 {done}/{total}", min(pct, 82), phase="keyframe")

    _kf_verify, _kf_vretries = A._keyframe_qc_verifier(ctx["project_name"], script=ctx.get("script"))
    # 尾帧提示词预检（生成前质检）：与手动链路保持同一覆盖（能自愈先自愈，成批不阻断）
    _kf_pre, _kf_pre_on = A._keyframe_prompt_preflight(ctx["project_name"])
    report = A.keyframe.generate_keyframes(
        shots, sb_map, kf_dir, seed=ctx["config"].get("seed"),
        timeout=int(ctx.get("timeout_per_segment") or 900),
        only_missing=True, progress_cb=_cb,
        chain_mode=ctx["config"].get("keyframe_chain_mode") or "auto",
        verify_cb=_kf_verify, max_verify_retries=_kf_vretries,
        preflight_cb=_kf_pre)
    recheck = probe_keyframe(ctx)
    if not recheck.get("done"):
        return {"ok": False, "detail": {"report": report, "probe": recheck},
                "error": f"尾帧缺失 {len(recheck.get('missing') or [])} 镜："
                         f"{recheck.get('missing')[:8]}"}
    return {"ok": True, "detail": {"report": report, "probe": recheck,
                                   "prompt_qc_enabled": _kf_pre_on}, "artifact": kf_dir}


def step_video(ctx) -> dict:
    """逐镜视频生成（视频 AI 质检门禁：抽帧送检，不达标自动重生成，阻断不入库）"""
    A = _A()
    pd = probe_video(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["dir"]}
    script = ctx.get("script") or {}
    shots = script.get("shots") or []
    if not shots:
        raise NeedsHumanError("剧本没有镜头数据，无法生成视频")
    cfg = ctx["config"]
    mode = cfg.get("video_mode") or "per_shot"
    if mode == "keyframe":
        kf = probe_keyframe(ctx)
        if not kf.get("done"):
            raise NeedsHumanError("关键帧模式要求尾帧齐全，请先完成尾帧生成")

    # 参考图：优先剧本自带，缺失时由 worker 内部从磁盘资产兜底
    char_refs = script.get("characters") or []
    scene_refs = script.get("scenes") or []
    ctx["progress"](f"生成视频（{len(shots)} 镜 · {mode}）", 50, phase="video")
    final = _run_task_worker(
        A._video_generate_worker,
        (ctx["project_name"], shots, char_refs, scene_refs, {}, True, mode,
         int(ctx.get("timeout_per_segment") or 900),
         ctx.get("episode_tag") or f"ep{ctx['episode_no']:02d}",
         ctx["episode_no"],
         cfg.get("keyframe_chain_mode") or "auto",
         cfg.get("style") or ""),
        "generation_state", "lock",
        init={"total": len(shots), "phase": "视频生成", "qc": A._qc_brief("video")},
        prefix="pipe_video")
    out = _outcome_from_task(final, "视频生成")
    recheck = probe_video(ctx)
    detail = {"mode": mode, "count": out["count"], "total": len(shots),
              "blocked": out["blocked"],
              "qc_blocked": (final or {}).get("qc_blocked_count"), "probe": recheck,
              "success_count": (final or {}).get("success_count")}
    if not recheck.get("done"):
        return {"ok": False, "blocked": out["blocked"], "detail": detail,
                "error": f"视频缺失 {len(recheck.get('missing') or [])} 镜："
                         f"{recheck.get('missing')[:8]}"}
    return {"ok": True, "detail": detail, "artifact": recheck["dir"]}


def step_final(ctx) -> dict:
    """成片合成：按剧本镜头顺序拼接该集所有片段，可选叠加字幕"""
    A = _A()
    pd = probe_final(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}
    script = ctx.get("script") or {}
    shots = script.get("shots") or []
    vd = probe_video(ctx)
    if not vd.get("done"):
        raise PipelineError("视频未就绪，无法合成成片（请先完成视频生成）")

    out = final_path(ctx)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    # 整集模式（video_mode=episode）：step_video 由 H3 一次生成「整集视频」，
    # 磁盘上根本没有逐镜 shot_XX.mp4。此时不能按逐镜拼接，直接把整集视频
    # 采用为成片，否则会误报「该集没有可拼接的镜头视频」而整集卡死。
    mode = (ctx["config"].get("video_mode") or "per_shot")
    if mode == "episode":
        src = vd.get("file") or ""
        if not _nonempty(src):
            raise PipelineError("整集模式未找到整集视频文件，无法合成成片")
        ctx["progress"]("整集模式：采用整集视频作为成片", 70, phase="final")
        files = [src]
        tmp = out + ".episode.mp4"
        if os.path.exists(tmp):
            os.remove(tmp)
        shutil.copy2(src, tmp)
    else:
        # 按剧本镜头顺序（而非文件名字典序）拼接，保证叙事顺序正确
        files = []
        for i, s in enumerate(shots):
            seq = A._shot_seq(s.get("shot_id", i + 1), i + 1)
            p = os.path.join(vd["dir"], f"shot_{seq:02d}.mp4")
            if _nonempty(p):
                files.append(p)
        if not files:
            raise PipelineError("该集没有可拼接的镜头视频")

        ctx["progress"](f"合成成片（{len(files)} 段）", 70, phase="final")
        tmp = out + ".concat.mp4"
        if os.path.exists(tmp):
            os.remove(tmp)
        A.video_processor.concat_videos(files, tmp)
    if not _nonempty(tmp):
        raise PipelineError("片段拼接失败（未产出有效文件）")

    # 字幕（复用既有 add_subtitles；无台词时跳过）
    subbed = ""
    try:
        from dialogue_utils import dialogue_text
        subs, cur = [], 0.0
        for s in shots:
            dur = float(s.get("duration") or 5)
            text = dialogue_text(s.get("dialogue"))
            if text:
                subs.append({"start": cur, "end": cur + dur, "text": text})
            cur += dur
        if subs:
            subbed = A.video_processor.add_subtitles(tmp, subs, out)
    except Exception as e:  # noqa: BLE001  字幕失败不阻断成片
        logger.warning("成片字幕生成跳过（不影响成片）：%s", e)

    produced = subbed if _nonempty(subbed) else ""
    if not produced:
        os.replace(tmp, out)
        produced = out
    elif os.path.abspath(produced) != os.path.abspath(out):
        os.replace(produced, out)
        produced = out
    if _nonempty(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not _nonempty(out):
        raise PipelineError("成片合成失败（目标文件为空）")
    return {"ok": True, "artifact": out,
            "detail": {"segments": len(files), "subtitles": bool(subbed), "size": os.path.getsize(out)}}


def step_tts(ctx) -> dict:
    """配音：逐句合成 → 合并该集音轨 → 落配音清单"""
    A = _A()
    pd = probe_tts(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}
    script = ctx.get("script") or {}
    project = ctx["project_name"]
    out_dir = A._dub_project_dir(project)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = A.load_voice_map(vm_path) or A.default_voice_map(
        script.get("characters") or [], project, ctx["episode_no"])
    plan = A.build_dub_plan(script, voice_map, project, ctx["episode_no"],
                            shot_ids=None, only_missing=False,
                            out_dir_wav=os.path.join(out_dir, "lines"))
    if not plan.get("lines"):
        return {"ok": True, "skipped": True,
                "detail": {"note": "该集没有可朗读台词，跳过配音"}}
    try:
        A.save_voice_map(plan["voice_map"], vm_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("音色映射保存失败（不影响本次合成）：%s", e)

    ctx["progress"](f"配音合成（{plan['line_count']} 句）", 82, phase="tts")
    final = _run_task_worker(
        A._dub_worker, (project, plan, out_dir, "wav", ctx["episode_no"]),
        "dub_tasks", "dub_lock",
        init={"total": plan["line_count"], "phase": "配音合成",
              "project_name": project, "out_dir": out_dir},
        prefix="pipe_dub")
    recheck = probe_tts(ctx)
    if not recheck.get("done"):
        return {"ok": False, "detail": {"task": final, "probe": recheck},
                "error": (final or {}).get("error") or "配音未完成（存在未成功台词）"}
    return {"ok": True, "detail": {"probe": recheck, "lines": plan["line_count"]},
            "artifact": recheck["file"]}


def step_mix(ctx) -> dict:
    """音画对齐混音：把该集配音按时间轴铺到成片上"""
    A = _A()
    pd = probe_mix(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}
    tts = probe_tts(ctx)
    if not tts.get("done"):
        return {"ok": True, "skipped": True,
                "detail": {"note": "该集无有效配音，跳过混音（成片保持无配音版本）"}}
    fv = final_path(ctx)
    if not _nonempty(fv):
        raise PipelineError("成片未就绪，无法混音")

    env = A.mix_ffmpeg_check()
    if not env.get("available"):
        raise NeedsHumanError("ffmpeg/ffprobe 不可用：" + "；".join(env.get("reasons") or []))

    out_name = os.path.basename(mix_output_path(ctx))
    prepared = A._mix_prepare({"project_name": ctx["project_name"],
                               "episode": ctx["episode_no"],
                               "video_path": fv})
    ctx["progress"](f"音画对齐混音（{len(prepared['entries'])} 句）", 92, phase="mix")
    final = _run_task_worker(
        A._mix_worker, (prepared, out_name),
        "mix_tasks", "mix_lock",
        init={"phase": "音画对齐", "project_name": ctx["project_name"],
              "video_path": fv, "out_name": out_name,
              "line_count": len(prepared["entries"])},
        prefix="pipe_mix")
    recheck = probe_mix(ctx)
    if not recheck.get("done"):
        return {"ok": False, "detail": {"task": final, "probe": recheck},
                "error": (final or {}).get("error") or "混音未产出有效文件"}
    return {"ok": True, "detail": {"probe": recheck, "lines": len(prepared["entries"])},
            "artifact": recheck["file"]}


def step_upscale(ctx) -> dict:
    """超分（FlashVSR）：对混音成片做超分，并归档到确定性路径

    设计要点（重要）
    ----------------
    这一步是**画质增强**，不是出片的必要环节，因此全程 fail-open：
    环境不可用（ComfyUI 离线 / FlashVSR 模型缺失 / 节点未安装）或执行失败时，
    一律返回 ``skipped`` 而非 ``failed``。

    原因：本步骤默认开启，若按普通步骤「失败即 raise」，会把一整集已经跑完的
    成片拖成失败态，用户既拿不到交付、又要为「锦上添花」的环节买单重跑。
    """
    A = _A()
    pd = probe_upscale(ctx)
    if pd.get("done"):
        return {"ok": True, "skipped": True, "detail": {"probe": pd}, "artifact": pd["file"]}

    # 优先超分混音成品（带配音）；没有则退回无配音成片
    src = mix_output_path(ctx)
    if not _nonempty(src):
        src = final_path(ctx)
    if not _nonempty(src):
        return {"ok": True, "skipped": True,
                "detail": {"note": "该集无成片可超分，跳过"}}

    try:
        import upscale_client
    except Exception as e:  # noqa: BLE001
        logger.warning("第%s集超分跳过（模块不可用）：%s", ctx["episode_no"], e)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分模块不可用，已跳过：{e}"}}

    # 环境自检：不可用直接跳过，不进入重试循环
    try:
        env = A.upscale_env_check()
    except Exception as e:  # noqa: BLE001
        logger.warning("第%s集超分跳过（环境自检失败）：%s", ctx["episode_no"], e)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分环境自检失败，已跳过：{e}"}}
    if not env.get("available"):
        reason = "；".join(env.get("reasons") or []) or "超分环境不可用"
        logger.warning("第%s集超分跳过（环境不可用）：%s", ctx["episode_no"], reason)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分环境不可用，已跳过：{reason}",
                           "env": {"comfy_online": env.get("comfy_online"),
                                   "model_ready": env.get("model_ready"),
                                   "te_ready": env.get("te_ready"),
                                   "reasons": env.get("reasons") or []}}}

    scale = int(ctx["config"].get("upscale_scale") or 2)
    if scale not in (2, 3, 4):      # 兜底：配置未经 normalize_config 直连时
        scale = 2
    ctx["progress"](f"超分（FlashVSR {scale}x）…", 96, phase="upscale")
    try:
        res = upscale_client.VideoUpscaler().upscale(
            src, project_name=ctx["project_name"], scale=scale,
            # ⚠️ 成片是带 TTS 配音的，而 TE-Speed 链路默认 attach_audio=False ——
            # 不显式开启会把音轨丢掉，超分产物变成无声视频。
            attach_audio=True,
            progress_cb=lambda msg, pct=None: ctx["progress"](
                f"超分：{msg}", 96, phase="upscale"),
        )
    except Exception as e:  # noqa: BLE001  超分失败不阻断出片
        logger.warning("第%s集超分失败（已跳过，不影响成片交付）：%s",
                       ctx["episode_no"], e, exc_info=True)
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分执行失败，已跳过：{type(e).__name__}: {e}",
                           "source": src}}

    produced = (res or {}).get("output_path") or ""
    if not _nonempty(produced):
        return {"ok": True, "skipped": True,
                "detail": {"note": "超分未产出有效文件，已跳过", "source": src}}

    # 归档到确定性路径（带时间戳的原文件名无法用于幂等探测）
    out = upscale_path(ctx)
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.abspath(produced) != os.path.abspath(out):
            shutil.copy2(produced, out)
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "skipped": True,
                "detail": {"note": f"超分产物归档失败，已跳过：{e}", "produced": produced}}
    if not _nonempty(out):
        return {"ok": True, "skipped": True,
                "detail": {"note": "超分产物归档后为空，已跳过", "produced": produced}}

    return {"ok": True, "artifact": out,
            "detail": {"source": src, "engine": (res or {}).get("engine"),
                       "scale": scale, "before": (res or {}).get("before"),
                       "after": (res or {}).get("after"),
                       "elapsed_sec": (res or {}).get("elapsed_sec"),
                       "size": os.path.getsize(out)}}


STEP_RUNNERS = {
    "script": step_script,
    "assets": step_assets,
    "storyboard": step_storyboard,
    "keyframe": step_keyframe,
    "video": step_video,
    "final": step_final,
    "tts": step_tts,
    "mix": step_mix,
    "upscale": step_upscale,
}


def step_enabled(step: str, ctx) -> bool:
    """步骤是否对该集启用（由配置与集号决定）"""
    cfg = ctx["config"]
    if step == "script":
        return True
    if step == "assets":
        return bool(cfg.get("enable_assets"))
    if step == "storyboard":
        return True                      # 分镜图是视频的必要输入，恒开
    if step == "keyframe":
        return bool(cfg.get("enable_keyframe"))
    if step == "video":
        return bool(cfg.get("enable_video"))
    if step == "final":
        return bool(cfg.get("enable_final"))
    if step == "tts":
        return bool(cfg.get("enable_tts"))
    if step == "mix":
        return bool(cfg.get("enable_mix"))
    if step == "upscale":
        return bool(cfg.get("enable_upscale"))
    return False


# ===================== 一致性复检（跨镜角色） =====================


def check_consistency(ctx) -> dict:
    """成片前的跨镜一致性复检（不达标即判失败，交由重试层处理）"""
    A = _A()
    cfg = ctx["config"]
    if not cfg.get("require_consistency"):
        return {"ok": True, "skipped": True, "note": "未开启一致性门禁"}
    try:
        collect = A._consistency_collect(ctx["project_name"], ctx["episode_no"])
        if not collect.get("shot_images"):
            return {"ok": True, "skipped": True, "note": "尚无分镜图可比对"}
        report = A.consistency.run(
            ctx["project_name"],
            character_refs=collect["character_refs"],
            shot_images=collect["shot_images"],
            asset_dirs=collect["asset_dirs"],
            cfg=A._qc_load_cfg(),
            include_assets=False, include_shots=True)
    except Exception as e:  # noqa: BLE001  一致性校验失败不阻断生产（仅记录）
        logger.warning("一致性复检异常（不阻断）：%s", e)
        return {"ok": True, "skipped": True, "note": f"复检异常：{e}"}
    summary = report.get("summary") or {}
    floor = float(cfg.get("consistency_min_score") or 0)
    mins = summary.get("min_score")
    out = {"summary": summary, "report_path": report.get("report_path")}
    if floor > 0 and isinstance(mins, (int, float)) and mins < floor:
        return {"ok": False, "detail": out,
                "error": f"跨镜一致性最低分 {mins} 低于阈值 {floor}"
                         f"（{summary.get('failed')} 处不达标）"}
    return {"ok": True, "detail": out}


# ===================== 单集流水线 =====================


def _load_script_into_ctx(ctx) -> dict:
    A = _A()
    p = _script_path(ctx)
    if not _nonempty(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("剧本读取失败 %s：%s", p, e)
        return {}


def _deliverable_of(ctx, steps: dict) -> str:
    """该集的最终交付物：优先超分成品，其次混音成品，最后成片"""
    for key in ("upscale", "mix", "final"):
        art = (steps.get(key) or {}).get("artifact") or ""
        if _nonempty(art):
            return art
    return ""


def run_episode(config: dict, project_name: str, episode_no: int, novel_meta: dict,
                chapter: dict, progress_cb=None, should_stop=None) -> dict:
    """跑完一集的完整流水线，返回可直接落库的结果字典

    参数
    ----
    config        : normalize_config() 后的配置
    project_name  : 项目名（产物目录键）
    episode_no    : 集号
    novel_meta    : novel_parser.get_novel() 的元信息
    chapter       : 该集对应章节 {"index","title","text"...}
    progress_cb   : fn(message, percent, phase=None)
    should_stop   : fn() -> bool，返回 True 时中止本集（用于暂停 / 停止托管）。
                    检查点有两类：① 步骤边界；② 步骤内部「发起 LLM 调用前 /
                    重试退避前」—— 后者让暂停**秒级生效**，不必等重试链跑完。
                    两类检查点都落在**尚未产出文件**的位置，因此不会留下半成品；
                    已完成的步骤全部保留，续跑时会被逐步骤探测跳过。
    """
    A = _A()
    started = time.time()
    # 进度单调化：各步骤内部回报的百分比（如剧本步骤的 4%~18%）与整体锚点百分比
    # 来源不同，直接透传会让进度条回退（实测出现 18% → 16% → 17%）。无人值守界面里
    # 「进度倒退」非常误导，这里统一钳住只增不减。
    _cb_user = progress_cb or (lambda *a, **k: None)
    _pct_seen = {"v": 0}

    def _progress(message, percent=None, phase=None):
        try:
            p = int(percent or 0)
        except (TypeError, ValueError):
            p = 0
        if p < _pct_seen["v"]:
            p = _pct_seen["v"]
        else:
            _pct_seen["v"] = p
        _cb_user(message, p, phase=phase)

    ctx = {
        "config": config, "project_name": project_name,
        "project_key": config.get("project_key") or project_name,
        "episode_no": int(episode_no), "episode_tag": f"ep{int(episode_no):02d}",
        "novel_meta": novel_meta or {}, "chapter": chapter or {},
        "timeout_per_segment": int(config.get("timeout_per_segment") or 900),
        "script": {}, "logs": [], "steps": {},
        "progress": _progress,
    }
    dead = _is_dead_letter(config, project_name, int(episode_no))
    if dead:
        return {"ok": False, "status": "needs_human", "episode_no": int(episode_no),
                "project": project_name, "deliverable": "", "steps": {},
                "error": f"该集此前已判定需人工介入（{dead.get('reason') or '未知原因'}）",
                "note": "请先在「需人工介入」中处理或标记忽略后才会重新尝试"}

    result = {
        "ok": False, "status": "failed", "episode_no": int(episode_no),
        "project": project_name, "project_key": ctx["project_key"],
        "chapter_title": (chapter or {}).get("title") or f"第{int(episode_no)}集",
        "started_at": _now(), "deliverable": "", "steps": {}, "error": "",
    }

    # 把「是否该停」注册进当前执行上下文（contextvars）：本集内部所有 LLM 调用与
    # 重试退避都能感知到它，从而实现「暂停秒级生效」。作用域仅限本调用链 ——
    # 用户手动触发的生产（should_stop=None）不受任何影响。
    _cancel_token = cancellation.push(should_stop)
    try:
        for step in STEP_SEQUENCE:
            if should_stop and should_stop():
                result.update({"status": "cancelled",
                               "error": "托管已暂停，在步骤边界安全中止（已完成步骤保留，可续跑）"})
                return result
            if not step_enabled(step, ctx):
                result["steps"][step] = {"status": "disabled"}
                continue

            # 已存在的剧本要先读进来，后续步骤（资产/分镜/视频）都依赖它
            if step != "script" and not ctx.get("script"):
                ctx["script"] = _load_script_into_ctx(ctx)

            ctx["progress"](f"{STEP_LABELS[step]}…", result_pct(result, step), phase=step)
            out, attempts = _run_step_with_retry(step, ctx)
            result["steps"][step] = {
                "status": "done" if out.get("ok") else "failed",
                "skipped": bool(out.get("skipped")),
                "blocked": bool(out.get("blocked")),
                "attempts": attempts,
                "error": out.get("error") or "",
                "artifact": out.get("artifact") or "",
                "detail": out.get("detail") or {},
                "finished_at": _now(),
            }
            ctx["steps"][step] = out
            if not out.get("ok"):
                result["steps_status"] = "blocked"
                raise PipelineError(f"{STEP_LABELS[step]}未通过：{out.get('error')}"
                                    f"（已尝试 {attempts} 次）")
            # 每完成一步立即记录产物，便于崩溃后从日志判断进度
            ctx["logs"].append(f"[{_now()}] {STEP_LABELS[step]} 完成"
                               f"{'（跳过）' if out.get('skipped') else ''}"
                               f"{' · 重试 %d 次' % (attempts - 1) if attempts > 1 else ''}")

        result["deliverable"] = _deliverable_of(ctx, ctx["steps"])
        result["ok"] = True
        result["status"] = "done"
        if not result["deliverable"]:
            result["ok"] = False
            result["status"] = "failed"
            result["error"] = "流水线跑完但未产出可交付文件（请检查各环节开关）"
    except NeedsHumanError as e:
        result["status"] = "needs_human"
        result["error"] = str(e)
    except cancellation.Cancelled as e:
        # 协作式中止（托管暂停 / 用户停止）：停在**尚未产出文件**的安全点，
        # 已完成步骤全部保留，续跑时会被逐步骤探测跳过。
        result["status"] = "cancelled"
        result["error"] = f"收到中止信号，已在安全点停下（已完成步骤保留，可续跑）：{e}"
        logger.info("第%s集因中止信号停止：%s", episode_no, e)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        logger.error("第%s集流水线失败：%s\n%s", episode_no, result["error"], traceback.format_exc())
    finally:
        cancellation.reset(_cancel_token)

    result["elapsed_sec"] = round(time.time() - started, 1)
    result["finished_at"] = _now()
    # 记账（时长 + 成功与否），供成本看板统计
    try:
        A.analytics.record_event("pipeline", project_name,
                                 f"第{episode_no}集自动生产",
                                 int(result["elapsed_sec"]),
                                 units=len(result["steps"]),
                                 success=bool(result["ok"]))
    except Exception:  # noqa: BLE001
        pass
    return result


#: 步骤在整体进度里的百分比锚点
_STEP_PCT = {"script": 2, "assets": 18, "storyboard": 32, "keyframe": 44,
             "video": 48, "final": 70, "tts": 80, "mix": 90, "upscale": 96}


def result_pct(result: dict, step: str) -> int:
    """该步骤开始时的整体进度（用步骤锚点，避免各 worker 自己写的百分比互相打架）"""
    return int(_STEP_PCT.get(step, 5))


def _run_step_with_retry(step: str, ctx: dict) -> tuple:
    """执行一个步骤，失败按配置自动重试；返回 (outcome, 尝试次数)"""
    cfg = ctx["config"]
    cfg_max = int(cfg.get("step_max_retries") or 0) if cfg.get("auto_repair") else 0
    # 质检门禁类步骤失败通常因为生成质量，多给一次机会
    max_tries = max(cfg_max, 1 if step in GATED_STEPS else cfg_max) + 1
    last = {}
    for attempt in range(1, max_tries + 1):
        # ⚠️ 中止信号优先于重试：托管暂停时立刻停，不再空转剩余重试次数。
        # 这个检查点在「还没产出任何文件」的位置，因此不会留下半成品。
        cancellation.check(f"{STEP_LABELS[step]} 重试前收到中止信号")
        if attempt > 1:
            ctx["progress"](f"{STEP_LABELS[step]} 第 {attempt}/{max_tries} 次重试…",
                            result_pct(ctx_step_ctx(ctx), step), phase=f"{step}:retry")
            logger.warning("第%s集 %s 第 %d 次重试（上次：%s）",
                           ctx["episode_no"], step, attempt, last.get("error"))
            # 可被打断的退避：避免长退避期间「暂停」长时间无响应
            cancellation.sleep(min(3 * attempt, 10))
        try:
            out = STEP_RUNNERS[step](ctx)
        except NeedsHumanError:
            raise
        except cancellation.Cancelled:
            raise                      # 中止信号必须穿透，不能被当成一次「步骤失败」
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            logger.error("第%s集 %s 异常：%s\n%s", ctx["episode_no"], step,
                         out["error"], traceback.format_exc())
        last = out
        if out.get("ok"):
            return out, attempt
        # 明确的环境性故障不做无意义重试
        if out.get("fatal") or out.get("needs_human"):
            break
    return last, max_tries


def ctx_step_ctx(ctx):
    return ctx


# ===================== 死信（需人工介入）记录 =====================


def dead_letter_path(project_name: str) -> str:
    A = _A()
    return os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "dead_letter.json")


# P1-11（A-13）：死信文件损坏防护 —— 「.bak 快照 + 唯一临时名 + 损坏先恢复」
# 背景：`_read_dead_letters` 曾「解析失败 → 静默 {}」，死信记录因此丢失，
# 该集会被无限重烧并反复重新标记。对齐 project_store / secret_store 口径：
# 每次发布前把「当前可解析好版本」快照到 .bak，损坏时先从 .bak 恢复；
# 恢复不了才降级为空（并 error 日志留痕，绝不静默）。


def _dead_letter_bak(path: str) -> str:
    """死信文件「上一份可解析好版本」备份路径：``path + '.bak'``."""
    return path + ".bak"


def _dead_letter_snapshot_bak(path: str) -> None:
    """发布前快照：若活文件当前可解析，复制到 .bak（保留最后一次好版本）."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError):
        return  # 活文件已不可解析 → 不覆盖既有 .bak（它仍是最后一份好版本）
    try:
        shutil.copy2(path, _dead_letter_bak(path))
    except OSError:
        pass


def _dead_letter_try_restore_bak(path: str):
    """活文件损坏时，尝试从 .bak（最后一份好版本）恢复并返回其 dict；无备份返回 None."""
    bak = _dead_letter_bak(path)
    if not os.path.isfile(bak):
        return None
    try:
        with open(bak, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("episodes", {})
    tmp = f"{path}.restore.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return None
    return data


def _read_dead_letters(project_name: str) -> dict:
    p = dead_letter_path(project_name)
    if not _nonempty(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        # P1-11（A-13）：解析失败 = 文件损坏。先尝试从 .bak 恢复，
        # 恢复不了才降级为空（留 error 日志，绝不静默吞掉）。
        logger.error("死信文件 %s 解析失败（文件损坏：%s），尝试从 .bak 恢复", p, e)
        restored = _dead_letter_try_restore_bak(p)
        if restored is not None:
            logger.warning("死信文件 %s 已从 .bak 恢复（保留既有死信记录）", p)
            return restored
        logger.error("死信文件 %s 损坏且无可用 .bak，按无死信处理（既有记录已丢失）", p)
        return {}
    if not isinstance(data, dict):
        data = {}
    eps = data.get("episodes")
    if eps is not None and not isinstance(eps, dict):
        logger.warning("死信文件 %s 的 episodes 结构异常，按空结构处理", p)
        data["episodes"] = {}
    elif eps is None:
        data["episodes"] = {}
    return data


def _is_dead_letter(config: dict, project_name: str, episode_no: int) -> dict:
    """该集是否已被判定「需人工介入」且尚未处理"""
    d = _read_dead_letters(project_name)
    item = (d.get("episodes") or {}).get(str(episode_no))
    return item if isinstance(item, dict) and not item.get("resolved") else {}


def mark_dead_letter(project_name: str, episode_no: int, reason: str,
                     detail: dict = None) -> dict:
    """把某集标记为「需人工介入」（用户处理或忽略后才会重新尝试）"""
    p = dead_letter_path(project_name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = _read_dead_letters(project_name)
    eps = data.setdefault("episodes", {})
    eps[str(int(episode_no))] = {
        "episode_no": int(episode_no), "reason": reason,
        "detail": detail or {}, "marked_at": _now(), "resolved": False,
    }
    _dead_letter_snapshot_bak(p)  # P1-11（A-13）：发布前快照最后一份好版本
    tmp = f"{p}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    logger.warning("第%s集已标记需人工介入：%s", episode_no, reason)
    return eps[str(int(episode_no))]


def resolve_dead_letter(project_name: str, episode_no: int, note: str = "") -> dict:
    """人工处理完成 / 标记忽略（之后流水线会重新尝试该集）"""
    p = dead_letter_path(project_name)
    data = _read_dead_letters(project_name)
    item = (data.get("episodes") or {}).get(str(int(episode_no)))
    if not item:
        return {}
    item.update({"resolved": True, "resolved_at": _now(), "resolve_note": note})
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _dead_letter_snapshot_bak(p)  # P1-11（A-13）：发布前快照最后一份好版本
    tmp = f"{p}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return item


def list_dead_letters(project_name: str) -> list:
    d = _read_dead_letters(project_name)
    items = [v for v in (d.get("episodes") or {}).values() if isinstance(v, dict)]
    items.sort(key=lambda x: int(x.get("episode_no") or 0))
    return items


# ===================== 交付物登记 =====================


def record_deliverable(project_name: str, episode_no: int, path: str,
                       meta: dict = None) -> dict:
    """把该集成片登记进「待验收」队列（用户只需看这里）"""
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    os.makedirs(os.path.dirname(idx_path), exist_ok=True)
    data = {}
    if _nonempty(idx_path):
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:  # noqa: BLE001
            data = {}
    items = data.setdefault("items", {})
    key = str(int(episode_no))
    prev = items.get(key) or {}
    items[key] = {
        "episode_no": int(episode_no),
        "project": project_name,
        "path": path,
        "filename": os.path.basename(path),
        "size": os.path.getsize(path) if _nonempty(path) else 0,
        "meta": meta or {},
        "created_at": prev.get("created_at") or _now(),
        "updated_at": _now(),
        # 重新生产会重置验收状态（内容已变，旧结论失效）
        "review": "pending" if prev.get("path") != path else (prev.get("review") or "pending"),
    }
    tmp = idx_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, idx_path)
    return items[key]


def list_deliverables(project_name: str = "") -> list:
    A = _A()
    root = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot")
    projects = [project_name] if project_name else (
        sorted(os.listdir(root)) if os.path.isdir(root) else [])
    out = []
    for pj in projects:
        p = os.path.join(root, pj, "deliverables.json")
        if not _nonempty(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:  # noqa: BLE001
            continue
        for v in (data.get("items") or {}).values():
            if isinstance(v, dict):
                v = dict(v)
                v["exists"] = _nonempty(v.get("path") or "")
                v["url"] = f"/api/autopilot/deliverable/file/{pj}/{v.get('filename')}"
                out.append(v)
    out.sort(key=lambda x: (x.get("project") or "", int(x.get("episode_no") or 0)))
    return out


def set_deliverable_review(project_name: str, episode_no: int, review: str,
                           note: str = "") -> dict:
    """验收 / 打回（打回会在下次托管轮转时重跑该集）"""
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    data = {}
    if _nonempty(idx_path):
        with open(idx_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    items = data.setdefault("items", {})
    item = items.get(str(int(episode_no)))
    if not item:
        return {}
    item.update({"review": review, "review_note": note, "reviewed_at": _now()})
    tmp = idx_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, idx_path)
    if review == "rejected":
        # 打回 = 该集需要重做：清掉死信状态让流水线重新尝试
        resolve_dead_letter(project_name, episode_no, note="成片被打回，重新生产")
    return item


def mark_deliverable_stale(project_name: str, episode_no: int, reason: str,
                           detail: dict = None) -> dict:
    """把该集已登记的成片标记为「已过期」（镜头被重做，成片需要重新合成）

    用户闭环里很关键的一步：对某镜不满意 → 重做该镜 → 但成片还是旧的。
    这里给交付物打标，验收页就能提示「镜头有更新，请重新合成后再验收」，
    而不是让用户对着过期成片点验收。
    """
    A = _A()
    idx_path = os.path.join(A.PROJECT_OUTPUT_DIR, "autopilot", project_name, "deliverables.json")
    if not _nonempty(idx_path):
        return {}
    try:
        with open(idx_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}
    item = (data.get("items") or {}).get(str(int(episode_no)))
    if not isinstance(item, dict):
        return {}
    meta = item.setdefault("meta", {})
    stale = meta.setdefault("stale", {})
    stale.update({"reason": reason, "detail": detail or {}, "marked_at": _now()})
    item["updated_at"] = _now()
    tmp = idx_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, idx_path)
    return item


def deliverable_path(project_name: str, filename: str) -> str:
    """按「交付物索引」解析真实文件路径（不假设成片被拷贝到 autopilot 目录）

    成片可能来自 output/dub_mix/（混音）或 output/final/（无配音），因此必须以
    索引里登记的路径为准；仅允许索引内的文件被访问，避免目录穿越。
    """
    idx_path = os.path.join(_A().PROJECT_OUTPUT_DIR, "autopilot", project_name,
                            "deliverables.json")
    if not _nonempty(idx_path):
        return ""
    try:
        with open(idx_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return ""
    want = os.path.basename(filename)
    for v in (data.get("items") or {}).values():
        if not isinstance(v, dict):
            continue
        if os.path.basename(v.get("path") or "") == want and _nonempty(v.get("path")):
            return v["path"]
    return ""
