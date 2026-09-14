# -*- coding: utf-8 -*-
"""关键帧驱动视频模式（P1-2）

传统「参考图模式」把分镜图 + 主角锚点图丢给 H3，模型只能猜运动轨迹，
构图与位置可控性差、容易跑偏（人物漂移、出画、动作与剧本不符）。

关键帧驱动改为：
    1) 先用分镜图（=首帧）经 Qwen Edit 生成该镜动作结束时的「尾帧」
    2) H3 的段参考图槽位改为注入 [首帧, 尾帧]
       —— 让模型在两端之间插值运动，首尾构图都被锚死

零行为变更原则：本模块只用既有 comfyui_client 接口，不新增任何模型依赖。
若 H3 工作流只有 1 个参考图槽位，则自动退化为「只锚首帧」，并在报告中明确标注。
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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


def build_end_frame_prompt(shot: dict, char_refs: Optional[List[dict]] = None,
                           scene_refs: Optional[List[dict]] = None) -> str:
    """构造「尾帧」生成提示词（中文，供 Qwen Edit 以首帧为参考图做编辑）

    要点：
    - 明确「仅改变动作/时间点，外观与场景必须与参考图完全一致」，避免模型改脸改服装
    - 显式禁止画面内出现文字（与 H3 链路同一约束，防止台词被渲染成字幕）
    """
    shot = shot or {}
    camera = (shot.get("camera") or "中景").strip()
    desc = (shot.get("description") or "").strip()
    loc = (shot.get("location") or "").strip()
    emotion = (shot.get("emotion") or "").strip()
    chars = [c for c in (shot.get("characters_in_shot") or []) if c]
    items = [i for i in (shot.get("items_in_shot") or []) if i]

    parts: List[str] = []
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


def plan_keyframes(shots: List[dict], sb_map: Dict[str, str],
                   keyframes_dir: str, only_missing: bool = True) -> List[dict]:
    """规划尾帧生成清单（不调用模型，供前端预检与断点续跑）

    返回每项：{shot_id, seq, start_frame, end_path, has_start, has_end, need_gen, reason}
    """
    plan: List[dict] = []
    for i, shot in enumerate(shots or []):
        sid = shot.get("shot_id", i + 1)
        seq = _seq(sid) or (i + 1)
        key = str(sid).strip()
        # 只按「镜头键」精确取图：数字键 / shot_NN 键。
        # 绝不按数组下标兜底——那会在 manifest 缺项时把别的镜头的分镜图当成首帧，
        # 生成出与剧本不符的尾帧（真实缺陷，已在新版修正）。
        start = (sb_map.get(key)
                 or sb_map.get(f"shot_{seq:02d}")
                 or sb_map.get(str(seq))
                 or "")
        end_p = end_frame_path(keyframes_dir, seq)
        has_start = bool(start) and os.path.isfile(start)
        has_end = os.path.isfile(end_p) and os.path.getsize(end_p) > 0
        need = has_start and (not has_end)
        if not has_start:
            need = False
            reason = "缺少分镜图（请先在步骤5生成分镜图片）"
        elif has_end and only_missing:
            need = False
            reason = "尾帧已存在，跳过"
        else:
            need = True
            reason = "待生成尾帧"
        plan.append({"shot_id": sid, "seq": seq, "start_frame": start,
                     "end_path": end_p, "has_start": has_start,
                     "has_end": has_end, "need_gen": bool(need), "reason": reason})
    return plan


def generate_keyframes(shots: List[dict], sb_map: Dict[str, str], keyframes_dir: str,
                       seed: Optional[int] = None, timeout: int = 900,
                       only_missing: bool = True,
                       progress_cb: Optional[Callable[[int, int, dict], None]] = None
                       ) -> dict:
    """批量生成尾帧（串行；单镜失败不影响其它镜）

    progress_cb(done, total, item) —— 每个镜头完成后回调一次
    """
    plan = plan_keyframes(shots, sb_map, keyframes_dir, only_missing=only_missing)
    todo = [p for p in plan if p["need_gen"]]
    total = len(todo)
    results: List[dict] = []
    # 已存在的直接登记为已完成（断点续跑语义）
    for p in plan:
        if p["has_end"] and only_missing:
            results.append({"shot_id": p["shot_id"], "seq": p["seq"], "ok": True,
                            "path": p["end_path"], "skipped": True,
                            "reason": "尾帧已存在（断点续跑跳过）"})
    ok_count = len(results)

    by_shot = {str(s.get("shot_id", i + 1)): s for i, s in enumerate(shots or [])}
    for n, item in enumerate(todo, start=1):
        shot = by_shot.get(str(item["shot_id"])) or {}
        prompt = build_end_frame_prompt(shot)
        r = generate_end_frame(item["start_frame"], prompt, item["end_path"],
                               seed=seed, timeout=timeout)
        r.update({"shot_id": item["shot_id"], "seq": item["seq"]})
        results.append(r)
        if r.get("ok"):
            ok_count += 1
        logger.info(f"尾帧 {n}/{total} shot {item['shot_id']} -> "
                    f"{'OK' if r.get('ok') else 'FAIL ' + str(r.get('error'))}")
        if progress_cb:
            try:
                progress_cb(n, total, r)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"关键帧进度回调异常（忽略）：{e}")

    return {"ok": ok_count > 0 or not todo, "total": len(plan), "generated": total,
            "succeeded": ok_count, "failed": max(0, len(results) - ok_count),
            "results": results}


def build_keyframe_segments(shots: List[dict], sb_map: Dict[str, str],
                            end_map: Dict[str, str], prompt_builder: Callable
                            ) -> Tuple[List[dict], List[dict]]:
    """把镜头转成 H3 段，参考图槽位 = [首帧, 尾帧]

    prompt_builder(shot, seq) -> (prompt_str, start_frame)
    （由 app.py 注入，复用既有 H3 提示词构造，避免提示词逻辑分叉）

    返回 (segments, meta)，meta 记录每段用了首尾帧还是退化单帧。
    """
    segs: List[dict] = []
    meta: List[dict] = []
    for i, shot in enumerate(shots or []):
        sid = shot.get("shot_id", i + 1)
        seq = _seq(sid) or (i + 1)
        prompt, start = prompt_builder(shot, seq)
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
