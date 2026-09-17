# -*- coding: utf-8 -*-
"""关键帧驱动视频模式（P1-2）

传统「参考图模式」把分镜图 + 主角锚点图丢给 H3，模型只能猜运动轨迹，
构图与位置可控性差、容易跑偏（人物漂移、出画、动作与剧本不符）。

关键帧驱动改为：
    1) 先用分镜图（=首帧）经 Qwen Edit 生成该镜动作结束时的「尾帧」
    2) H3 的段参考图槽位改为注入 [首帧, 尾帧]
       —— 让模型在两端之间插值运动，首尾构图都被锚死

跨镜链式（chain_mode，2026-09-17 新增）：
    参考工作流的做法是「上一镜的尾帧 = 下一镜的首帧」，镜与镜之间首尾相接，
    整集才连得起来。此前每镜都拿自己的分镜图当首帧，导致相邻两镜的画面
    各画各的（用户反馈：每个分镜的首尾帧都不一样）。
    链式模式下 shot N 的首帧 = shot N-1 的尾帧（存在时），
    尾帧仍由该首帧经 Qwen Edit 推进得到，H3 参考槽位 = [上一镜尾帧, 本镜尾帧]。
    - auto（默认）：仅在相邻两镜同场景（location/scene 一致）时串帧，跨场景不串；
    - always：无条件串帧；
    - off：保持旧行为（每镜用自己的分镜图当首帧）。

零行为变更原则：本模块只用既有 comfyui_client 接口，不新增任何模型依赖。
若 H3 工作流只有 1 个参考图槽位，则自动退化为「只锚首帧」，并在报告中明确标注。
"""
from __future__ import annotations

import logging
import os
import random
import shutil
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 链式模式：auto=同场景才串 / always=无条件串 / off=关闭（旧行为）
CHAIN_MODES = ("auto", "always", "off")
DEFAULT_CHAIN_MODE = "auto"


def norm_chain_mode(mode) -> str:
    """归一化链式模式参数，非法值回落默认（永不抛异常）"""
    m = str(mode or "").strip().lower()
    return m if m in CHAIN_MODES else DEFAULT_CHAIN_MODE

# 按镜头类型给出的「运动收尾」提示，避免尾帧与首帧几乎无差别（无运动可插值）
_MOTION_BY_CAMERA = {
    "特写": "动作收尾：人物完成一个细微的表情/眼神变化（如抬眼、垂眸、嘴角微动），"
            "肩部与头部位置随动作小幅移动，取景仍保持肩部以上特写。",
    "近景": "动作收尾：人物上半身完成一次小幅位移或转身，手臂自然落位，"
            "取景仍保持胸部以上近景。",
    "中景": "动作收尾：人物完成该镜头描述的主要动作（起身/转身/抬手/迈步），"
            "身体重心发生明确位移，取景仍保持中景不变。",
    "全景": "动作收尾：人物在场景中完成一次位移或环境变化（雾散/涟漪扩散/光影移动），"
            "人物在画面中的位置相比首帧有明显变化，取景仍保持全景。",
    "远景": "动作收尾：人物或主体在广阔场景中完成位移，环境光影随时间推进，"
            "主体在画面中的位置相比首帧有明显变化。",
    "俯拍": "动作收尾：自上方俯视的视角下，人物完成该镜头的主要动作，"
            "地面投影与相对位置发生可见变化，仍保持俯拍构图。",
    "仰拍": "动作收尾：自下方仰视的视角下，人物完成该镜头的主要动作，"
            "身体姿态发生可见变化，仍保持仰拍构图。",
    "空镜": "动作收尾：环境本身发生变化（雾气流散、水波扩散、光线移动），"
            "构图与镜头位置保持不变。",
}

_DEFAULT_MOTION = ("动作收尾：人物完成该镜头描述的主要动作，身体姿态与画面内位置"
                   "相比首帧有明确变化，机位与焦段保持不变。")


def motion_hint(camera: str) -> str:
    """按镜头类型取运动收尾提示（未知类型回退通用描述）"""
    cam = (camera or "").strip()
    if not cam:
        return _DEFAULT_MOTION
    for key, val in _MOTION_BY_CAMERA.items():
        if key in cam:
            return val
    return _DEFAULT_MOTION


def same_scene(prev: dict, cur: dict) -> bool:
    """相邻两镜是否同场景（链式串帧的判据）

    依次比较 location → scene_id → scene/scene_name；任一层两边都有值时以该层为准。
    全都缺失时返回 True（宁可串起来保持连贯，也不要每镜各画各的）。
    """
    if not isinstance(prev, dict) or not isinstance(cur, dict):
        return False
    for k in ("location", "scene_id", "scene", "scene_name"):
        a = str(prev.get(k) or "").strip()
        b = str(cur.get(k) or "").strip()
        if a and b:
            return a == b
    return True


def build_end_frame_prompt(shot: dict, char_refs: Optional[List[dict]] = None,
                           scene_refs: Optional[List[dict]] = None,
                           chained: bool = False) -> str:
    """构造「尾帧」生成提示词（中文，供 Qwen Edit 以首帧为参考图做编辑）

    要点：
    - 明确「仅改变动作/时间点，外观与场景必须与参考图完全一致」，避免模型改脸改服装
    - 显式禁止画面内出现文字（与 H3 链路同一约束，防止台词被渲染成字幕）

    chained=True 表示本镜首帧沿用了上一镜的尾帧（跨镜链式），
    参考图已经不是本镜自己的分镜图，因此首句措辞要改成「承接上一镜」，
    否则模型会以为是同一镜的两个时间点而拒绝推进动作。
    """
    shot = shot or {}
    camera = (shot.get("camera") or "中景").strip()
    desc = (shot.get("description") or "").strip()
    loc = (shot.get("location") or "").strip()
    emotion = (shot.get("emotion") or "").strip()
    chars = [c for c in (shot.get("characters_in_shot") or []) if c]
    items = [i for i in (shot.get("items_in_shot") or []) if i]

    parts: List[str] = []
    if chained:
        parts.append(f"参考图是**上一镜结束时的画面**。本镜（「{camera}」）承接该画面继续推进："
                     f"保持参考图的角色外观、服装、发型、配色、场景环境、光照与整体画风完全不变，"
                     f"只把时间与动作推进到本镜结束的瞬间。")
    else:
        parts.append(f"这是同一镜头「{camera}」的尾帧画面：保持参考图的角色外观、服装、发型、"
                     f"配色、场景环境、光照与整体画风完全不变，只把画面推进到该镜头动作结束的瞬间。")
    if loc:
        parts.append(f"场景：{loc}。")
    if desc:
        parts.append(f"镜头内容：{desc}")
    parts.append(motion_hint(camera))
    if emotion:
        parts.append(f"人物情绪：{emotion}。")
    if chars:
        parts.append(f"出场人物：{'、'.join(chars)}（外貌与服装必须与参考图一致，不得替换人物）。")
    if items:
        parts.append(f"关键道具：{'、'.join(items)}（形制与材质与参考图一致）。")
    parts.append(f"取景要求：仍为{camera}，机位、焦段与构图框架与首帧保持连贯，"
                 f"不得切换视角或大幅改变景别。")
    parts.append("画面中严禁出现任何文字、字幕、台词文本、水印或标识。")
    return "".join(parts)


def end_frame_path(keyframes_dir: str, shot_id) -> str:
    """尾帧落盘路径：output/keyframes/<项目>/shot_<NN>_end.png（与分镜图命名对齐）"""
    seq = _seq(shot_id)
    return os.path.join(keyframes_dir, f"shot_{seq:02d}_end.png")


def start_frame_link_path(keyframes_dir: str, shot_id) -> str:
    """首帧在本目录的镜像路径（便于画布/导出统一按目录取图，不依赖分镜图目录）"""
    seq = _seq(shot_id)
    return os.path.join(keyframes_dir, f"shot_{seq:02d}_start.png")


def _seq(shot_id) -> int:
    """把 shot_id（可能是 1 / "1" / "shot_01"）归一为整数序号"""
    if isinstance(shot_id, int):
        return shot_id
    s = str(shot_id or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def generate_end_frame(start_frame: str, prompt: str, out_path: str,
                       seed: Optional[int] = None, timeout: int = 900) -> dict:
    """以首帧为参考图，生成该镜头尾帧（Qwen Edit 2511 图像编辑链路）"""
    try:
        import comfyui_client
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"comfyui_client 不可用：{e}"}
    client = comfyui_client.ComfyUIClient()
    prefix = os.path.splitext(os.path.basename(out_path))[0]
    try:
        result = client.generate_storyboard(
            prompt_zh=prompt, ref_images=[start_frame],
            filename_prefix=f"comic_drama_kf/{prefix}", seed=seed, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"尾帧生成失败：{e}"}
    files = (result or {}).get("files") or []
    if not files:
        return {"ok": False, "error": "ComfyUI 未返回尾帧图片"}
    try:
        import shutil
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        if os.path.abspath(files[0]) != os.path.abspath(out_path):
            shutil.copy2(files[0], out_path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"尾帧落盘失败：{e}"}
    return {"ok": True, "path": out_path, "raw": files[0],
            "prompt_id": (result or {}).get("prompt_id")}


def _own_start(sb_map: Dict[str, str], key: str, seq: int) -> str:
    """取本镜自己的分镜图作为「原生首帧」

    只按「镜头键」精确取图：数字键 / shot_NN 键。
    绝不按数组下标兜底——那会在 manifest 缺项时把别的镜头的分镜图当成首帧，
    生成出与剧本不符的尾帧（真实缺陷，已在新版修正）。
    """
    return (sb_map.get(key)
            or sb_map.get(f"shot_{seq:02d}")
            or sb_map.get(str(seq))
            or "")


def plan_keyframes(shots: List[dict], sb_map: Dict[str, str],
                   keyframes_dir: str, only_missing: bool = True,
                   chain_mode: str = DEFAULT_CHAIN_MODE) -> List[dict]:
    """规划尾帧生成清单（不调用模型，供前端预检与断点续跑）

    链式（chain_mode != off）时，第 N 镜的首帧优先取第 N-1 镜的尾帧：
      - 已落盘的 `shot_NN_start.png` 镜像（上一轮生成时写下的）；
      - 否则直接取上一镜尾帧 `shot_(N-1)_end.png`；
      - 再否则，若上一镜本次会生成，则登记为「首帧待上一镜产出」。

    返回每项：{shot_id, seq, start_frame, own_start, chain_from, chain_src,
              chained, has_start, has_end, need_gen, reason}
    """
    cm = norm_chain_mode(chain_mode)
    plan: List[dict] = []
    prev_shot: Optional[dict] = None
    prev_seq = 0
    prev_sid = None
    prev_end_ready = False

    for i, shot in enumerate(shots or []):
        sid = shot.get("shot_id", i + 1)
        seq = _seq(sid) or (i + 1)
        key = str(sid).strip()
        own = _own_start(sb_map, key, seq)
        own_ok = bool(own) and os.path.isfile(own)

        # ---- 链式首帧：上一镜尾帧 ----
        chain_src, chain_from = "", None
        if cm != "off" and prev_shot is not None and prev_seq:
            if cm == "always" or same_scene(prev_shot, shot):
                chain_src = end_frame_path(keyframes_dir, prev_seq)
                chain_from = prev_sid

        start, chained = own, False
        if chain_src:
            mirror = start_frame_link_path(keyframes_dir, seq)
            if os.path.isfile(mirror):
                start, chained = mirror, True
            elif os.path.isfile(chain_src):
                start, chained = chain_src, True
            elif prev_end_ready:
                start, chained = chain_src, True      # 上一镜本次会产出，届时可用

        has_start = bool(start) and os.path.isfile(start)
        start_pending = bool(chained and not has_start and prev_end_ready)
        end_p = end_frame_path(keyframes_dir, seq)
        has_end = os.path.isfile(end_p) and os.path.getsize(end_p) > 0

        available = has_start or start_pending or own_ok
        if has_end and only_missing:
            need, reason = False, "尾帧已存在，跳过"
        elif not available:
            need = False
            reason = ("缺少首帧：本镜分镜图与上一镜尾帧均不可用"
                      "（请先在步骤5生成分镜图片）")
        else:
            need = True
            reason = "待生成尾帧" + ("（首帧接续上一镜尾帧）" if chained else "")

        plan.append({"shot_id": sid, "seq": seq, "start_frame": start,
                     "own_start": own, "chain_from": chain_from,
                     "chain_src": chain_src, "chained": bool(chained),
                     "start_pending": start_pending,
                     "end_path": end_p, "has_start": has_start,
                     "has_end": has_end, "need_gen": bool(need), "reason": reason})

        prev_shot, prev_seq, prev_sid = shot, seq, sid
        prev_end_ready = bool(has_end or need)
    return plan


def resolve_start_map(shots: List[dict], sb_map: Dict[str, str], keyframes_dir: str,
                      chain_mode: str = DEFAULT_CHAIN_MODE) -> Dict[str, str]:
    """每镜「实际首帧」映射（供视频生成取 H3 参考图槽位 1）

    键同时注册数字键与 shot_NN 键。链式优先；链式来源不存在时回退本镜分镜图。
    """
    out: Dict[str, str] = {}
    for p in plan_keyframes(shots, sb_map, keyframes_dir,
                            only_missing=True, chain_mode=chain_mode):
        cur = p.get("start_frame") or ""
        if not (cur and os.path.isfile(cur)):
            cur = p.get("own_start") or ""
        if cur and os.path.isfile(cur):
            out[str(p["shot_id"])] = cur
            out[f"shot_{p['seq']:02d}"] = cur
    return out


def generate_keyframes(shots: List[dict], sb_map: Dict[str, str], keyframes_dir: str,
                       seed: Optional[int] = None, timeout: int = 900,
                       only_missing: bool = True,
                       progress_cb: Optional[Callable[[int, int, dict], None]] = None,
                       chain_mode: str = DEFAULT_CHAIN_MODE,
                       verify_cb: Optional[Callable[[str, dict, dict], Tuple[bool, str]]] = None,
                       max_verify_retries: int = 0
                       ) -> dict:
    """批量生成尾帧（串行；单镜失败不影响其它镜）

    chain_mode：跨镜链式（上一镜尾帧 = 下一镜首帧），见模块顶部说明。
    verify_cb(path, shot, item) -> (ok, reason)：可选的尾帧质检回调（由 app.py 注入
        QC 实现）。返回 False 时会换 seed 重画，最多 max_verify_retries 次；
        仍不通过则本镜判失败（链式会让后续镜自动回退到自己的分镜图，不会连环污染）。
    progress_cb(done, total, item) —— 每个镜头完成后回调一次
    """
    cm = norm_chain_mode(chain_mode)
    plan = plan_keyframes(shots, sb_map, keyframes_dir,
                          only_missing=only_missing, chain_mode=cm)
    todo = [p for p in plan if p["need_gen"]]
    total = len(todo)
    results: List[dict] = []
    # 已存在的直接登记为已完成（断点续跑语义）
    for p in plan:
        if p["has_end"] and only_missing:
            results.append({"shot_id": p["shot_id"], "seq": p["seq"], "ok": True,
                            "path": p["end_path"], "skipped": True,
                            "chained": p.get("chained", False),
                            "reason": "尾帧已存在（断点续跑跳过）"})
    ok_count = len(results)

    by_shot = {str(s.get("shot_id", i + 1)): s for i, s in enumerate(shots or [])}
    plan_by_seq = {p["seq"]: p for p in plan}
    os.makedirs(keyframes_dir, exist_ok=True)

    def _mirror(src: str, dst: str):
        """把实际使用的首帧镜像到 shot_NN_start.png（画布/导出/续跑统一取图）"""
        try:
            if src and os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"首帧镜像落盘失败（忽略）：{e}")

    for n, item in enumerate(todo, start=1):
        sid, seq = item["shot_id"], item["seq"]
        shot = by_shot.get(str(sid)) or {}
        # 链式首帧可能在本轮才由上一镜产出 → 到生成时才解析实际路径
        start = item.get("start_frame") or ""
        chained = bool(item.get("chained"))
        if not (start and os.path.isfile(start)):
            own = item.get("own_start") or ""
            if own and os.path.isfile(own):
                start, chained = own, False
                item["chained"] = False
        if not (start and os.path.isfile(start)):
            r = {"ok": False, "shot_id": sid, "seq": seq,
                 "error": "缺少首帧：本镜分镜图与上一镜尾帧均不可用"}
            results.append(r)
            logger.warning(f"尾帧 {n}/{total} shot {sid} -> 缺少首帧，跳过")
            if progress_cb:
                try:
                    progress_cb(n, total, r)
                except Exception:  # noqa: BLE001
                    pass
            continue

        _mirror(start, start_frame_link_path(keyframes_dir, seq))
        prompt = build_end_frame_prompt(shot, chained=chained)

        attempts = max(0, int(max_verify_retries or 0)) if verify_cb else 0
        r: dict = {}
        for attempt in range(attempts + 1):
            _seed = seed if attempt == 0 else random.randint(1, 2 ** 31 - 1)
            r = generate_end_frame(start, prompt, item["end_path"],
                                   seed=_seed, timeout=timeout)
            if not r.get("ok") or verify_cb is None:
                break
            try:
                v_ok, v_reason = verify_cb(item["end_path"], shot, item)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"尾帧质检回调异常（按通过处理）：{e}")
                v_ok, v_reason = True, ""
            r["qc"] = {"ok": bool(v_ok), "reason": v_reason or "",
                       "attempt": attempt + 1}
            if v_ok:
                break
            if attempt >= attempts:
                r.update({"ok": False, "error": f"尾帧未通过质检：{v_reason or '不符合要求'}"})
            else:
                logger.info(f"尾帧 shot {sid} 质检不通过（{v_reason}），换 seed 重画")

        r.update({"shot_id": sid, "seq": seq, "chained": chained,
                  "chain_from": item.get("chain_from"),
                  "start_frame": start})
        results.append(r)
        if r.get("ok"):
            ok_count += 1
            # 把本镜尾帧写成下一镜的首帧镜像，让链式关系在磁盘上可见、可续跑
            nxt = plan_by_seq.get(seq + 1)
            if nxt and nxt.get("chained") and nxt.get("chain_src"):
                if os.path.abspath(nxt["chain_src"]) == os.path.abspath(item["end_path"]):
                    _mirror(item["end_path"], start_frame_link_path(keyframes_dir, nxt["seq"]))
        logger.info(f"尾帧 {n}/{total} shot {sid} -> "
                    f"{'OK' if r.get('ok') else 'FAIL ' + str(r.get('error'))}")
        if progress_cb:
            try:
                progress_cb(n, total, r)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"关键帧进度回调异常（忽略）：{e}")

    return {"ok": ok_count > 0 or not todo, "total": len(plan), "generated": total,
            "succeeded": ok_count, "failed": max(0, len(results) - ok_count),
            "chain_mode": cm,
            "chained_count": sum(1 for p in plan if p.get("chained")),
            "results": results}


def build_keyframe_segments(shots: List[dict], sb_map: Dict[str, str],
                            end_map: Dict[str, str], prompt_builder: Callable,
                            chain_mode: str = DEFAULT_CHAIN_MODE,
                            keyframes_dir: str = ""
                            ) -> Tuple[List[dict], List[dict]]:
    """把镜头转成 H3 段，参考图槽位 = [首帧, 尾帧]

    prompt_builder(shot, seq) -> (prompt_str, start_frame)
    （由 app.py 注入，复用既有 H3 提示词构造，避免提示词逻辑分叉）

    chain_mode != off 且给出 keyframes_dir 时，首帧优先取「上一镜尾帧」（跨镜链式）。

    返回 (segments, meta)，meta 记录每段用了首尾帧还是退化单帧。
    """
    cm = norm_chain_mode(chain_mode)
    start_override: Dict[str, str] = {}
    if cm != "off" and keyframes_dir:
        start_override = resolve_start_map(shots, sb_map, keyframes_dir, chain_mode=cm)
    segs: List[dict] = []
    meta: List[dict] = []
    for i, shot in enumerate(shots or []):
        sid = shot.get("shot_id", i + 1)
        seq = _seq(sid) or (i + 1)
        prompt, start = prompt_builder(shot, seq)
        if start_override:
            ov = start_override.get(str(sid)) or start_override.get(f"shot_{seq:02d}")
            if ov and os.path.isfile(ov):
                start = ov
        end = end_map.get(str(sid)) or end_map.get(f"shot_{seq:02d}") or ""
        refs: List[str] = []
        if start and os.path.isfile(start):
            refs.append(start)
        if end and os.path.isfile(end):
            refs.append(end)
        if not refs:
            # 首尾帧都缺 → 交给上游兜底（保持与参考图模式一致的失败语义）
            refs = []
        try:
            dur = float(shot.get("duration") or 5)
        except (TypeError, ValueError):
            dur = 5.0
        segs.append({"prompt": prompt, "duration": dur,
                     "reference_images": refs, "name": f"shot_{seq:02d}"})
        meta.append({"shot_id": sid, "seq": seq, "duration": dur,
                     "start_frame": start if refs else "",
                     "end_frame": end if (refs and len(refs) > 1) else "",
                     "ref_count": len(refs),
                     "degraded": len(refs) < 2})
    return segs, meta
