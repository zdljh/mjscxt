"""MiniMax H3 提示词构建 —— 单一事实源

背景（为什么要单独抽一个模块）
--------------------------------------------------------------------------
历史实现里 H3 视频提示词有**两个互相打架的来源**：

1. ``novel_to_script`` 让 LLM 直接写 ``shot["prompt_h3"]``，字段说明只是
   「英文画面描述（60 词以内）」。模型于是输出一句裸英文，例如::

       Aerial shot rising through thin mist onto a five-story pagoda courtyard,
       golden sunrise rays, symmetrical composition, cold blue sky with warm gold contrast

   它既没有 H3 规范要求的六段结构，也**完全不知道自己配了几张参考图**，
   更没有 <Picture N> 标签 —— 而 H3 走的是 Ref2VA（参考图转视频），
   没有标签的提示词等于让模型盲猜参考图用途，实测出片与设定严重不符。

2. ``comfyui_client._build_h3_prompt`` 才是懂参考图语义的构建器，但只在
   ``prompt_h3`` 为空时才被调用；而 (1) 几乎总会填上值，于是**结构化构建器
   形同虚设**。实测全项目 10 份剧本、200+ 镜头里结构化提示词数量为 **0**。

本模块把「H3 提示词长什么样」收敛成唯一实现，供以下三处复用：

- ``comfyui_client._build_h3_prompt``（生成期兜底/权威构建）
- ``app._resolve_h3_prompt``（生成期择优：合规即用，不合规则并入细节）
- ``script_prompt_analyzer``（LLM 侧的规格说明与结构校验）

规范依据：H3 官方提示词指南
- Ref2VA（有参考图）固定六段，顺序不可变：
  ``subject_definitions`` / ``summary`` / ``retention_analysis`` /
  ``detailed_description`` / ``overall_soundscape`` / ``non_diegetic_music``
- base 模式（T2VA / I2VA / FL2VA / L2VA）固定三段：
  ``integrated_multimodal_description`` / ``overall_soundscape`` / ``non_diegetic_music``
- 每个镜头行以 ``[Shot N] MM:SS.mmm`` 时间码开头（毫秒三位）
- 台词必须带语言标记，如 ``[Chinese] 台词原文``，说话人以 ``(S1)`` 标注
- 与画面无关的抽象词（cinematic / beautiful）尽量少用，改用具体视觉与听觉细节
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Ref2VA 六段（顺序即输出顺序，不可变更）
REF_SECTIONS: Tuple[str, ...] = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)

#: base 模式三段
BASE_SECTIONS: Tuple[str, ...] = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)

#: 单镜最长时长（超过则拆成多个时间码节拍，让时间轴与目标时长对齐）
BEAT_MAX_SEC = 6.0

#: 无风格时的兜底（保持历史行为；有 style 时一律以 style 为准）
_DEFAULT_STYLE = "国漫3D渲染"

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_PAREN_NOTE_RE = re.compile(r"[（(]\s*(旁白|画外音|音效|配乐|BGM|VO|OS)[^）)]*[）)]", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def fmt_ts(sec: Any) -> str:
    """秒 → H3 时间码 ``MM:SS.mmm``（毫秒三位，与官方示例一致）"""
    try:
        s = float(sec or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s < 0:
        s = 0.0
    total_ms = int(round(s * 1000))
    minutes, rem_ms = divmod(total_ms, 60_000)
    seconds, ms = divmod(rem_ms, 1000)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


def lang_tag(text: str) -> str:
    """判断文本主语言，返回 ``Chinese`` / ``English`` / ``""``"""
    t = str(text or "")
    if not t.strip():
        return ""
    cjk = len(_CJK_RE.findall(t))
    latin = len(re.findall(r"[A-Za-z]", t))
    if cjk and cjk >= max(1, latin // 4):
        return "Chinese"
    if latin:
        return "English"
    return ""


def dialogue_lines(raw) -> List[Dict[str, str]]:
    """把任意形态的台词归一成 ``[{"speaker": 名, "text": 台词}]``（保持出现顺序）"""
    out: List[Dict[str, str]] = []
    if raw is None:
        return out
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            out.append({"speaker": "", "text": text})
        return out
    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("line") or raw.get("dialogue")
                   or raw.get("content") or "").strip()
        speaker = str(raw.get("speaker") or raw.get("character") or raw.get("role")
                      or raw.get("name") or "").strip()
        if text:
            out.append({"speaker": speaker, "text": text})
        return out
    if isinstance(raw, (list, tuple)):
        for item in raw:
            out.extend(dialogue_lines(item))
    return out


def speaker_slots(lines: Sequence[Dict[str, str]]) -> Dict[str, str]:
    """按首次出场顺序给说话人分配 ``S1`` / ``S2`` …（同名复用同一槽位）"""
    slots: Dict[str, str] = {}
    for ln in lines:
        name = str(ln.get("speaker") or "").strip()
        if name and name not in slots:
            slots[name] = f"S{len(slots) + 1}"
    return slots


def _first_name(shot: dict) -> str:
    for k in ("characters_in_shot", "characters"):
        vals = shot.get(k) or []
        if isinstance(vals, str) and vals.strip():
            return vals.strip()
        if isinstance(vals, (list, tuple)):
            for v in vals:
                if isinstance(v, dict) and v.get("name"):
                    return str(v["name"]).strip()
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def _clean_sfx(text: str) -> str:
    """去掉音效串里的旁白/配乐标注，只留环境与动作音"""
    t = _PAREN_NOTE_RE.sub("", str(text or ""))
    t = re.sub(r"^(音效|配乐|BGM|环境音)\s*[:：]\s*", "", t.strip(), flags=re.IGNORECASE)
    for prefix in ("旁白", "画外音"):
        if t.startswith(prefix):
            t = t[len(prefix):].lstrip("：: ")
    return t.strip(" 。；;，,")


# --------------------------------------------------------------------------- #
# 声音 / 音乐段（H3 要求独立成段，不能并进画面描述）
# --------------------------------------------------------------------------- #

_DEFAULT_MUSIC_BY_EMOTION = {
    "紧张": "低频弦乐持续压迫，节奏渐紧",
    "期待": "弦乐缓慢上行，留白等待爆发",
    "庄重": "低沉鼓点与号角质感的铜管长音",
    "威严": "缓慢低音铜管铺底，重音落点明确",
    "悲伤": "单线条钢琴稀疏音符，尾音自然衰减",
    "温柔": "钢琴与弦乐弱奏，旋律舒缓下行",
    "愤怒": "短促打击乐与低音弦乐切分推进",
    "恐惧": "高频弦乐颤音与不谐和音程，渐强后骤停",
    "平静": "稀疏钢琴单音配持续低音垫，安静收尾",
}


def build_soundscape(shot: dict, scene_hint: str = "") -> str:
    """``overall_soundscape``：环境音 + 动作音 + 非语言人声（画面内可闻）"""
    loc = str(shot.get("location") or scene_hint or "").strip()
    sfx = _clean_sfx(shot.get("audio_cues") or "")
    emotion = str(shot.get("emotion") or "").strip()
    parts: List[str] = []
    if sfx:
        parts.append(sfx)
    if loc:
        parts.append(f"{loc}的环境底噪")
    if emotion:
        parts.append(f"与「{emotion}」情绪相符的呼吸与衣料摩擦声")
    if not parts:
        parts.append("安静环境底噪，人物呼吸与衣料摩擦清晰可闻")
    body = "，".join(parts).rstrip("。；;，,")
    return f"{body}。全程无解说、无旁白念白，声音随镜头推进自然增强或减弱。"


def build_music(shot: dict, style: str = "") -> str:
    """``non_diegetic_music``：画面外配乐（角色听不到）"""
    emotion = str(shot.get("emotion") or "").strip()
    for key, desc in _DEFAULT_MUSIC_BY_EMOTION.items():
        if key and key in emotion:
            return f"{desc}，始终保持在画面之外，不出现人声演唱。"
    return "持续低音铺底，配器克制，随画面节奏缓慢起伏，始终保持在画面之外，不出现人声演唱。"


# --------------------------------------------------------------------------- #
# 画面时间轴
# --------------------------------------------------------------------------- #

def _beats(shot: dict, duration: float) -> List[Tuple[float, float, str]]:
    """把单镜拆成时间轴节拍 ``[(起始秒, 时长, 描述)]``

    时长 ≤ ``BEAT_MAX_SEC`` 时只有一个节拍；更长时按 2~3 个节拍铺满，
    避免「描述只有一瞬、视频却要演 12 秒」的空转。
    """
    desc = str(shot.get("description") or "").strip()
    # narration 是**旧剧本遗留字段**（本系统自 2026-09-19 起剧本阶段不再产出旁白）。
    # 保留读取只为兼容改造前生成的项目、让它们重出视频时不至于丢掉画面里的情绪衔接；
    # 新剧本这里恒为空串，下方「画外音延续」分支不会触发。
    narration = str(shot.get("narration") or "").strip()
    detail = str(shot.get("visual_detail") or "").strip()
    # ⚠️ 模型可能把同一条细节同时写进 description 与 visual_detail（两个字段本就允许重叠），
    # 直接 join 会让同一句细节在视频提示词里出现两遍 → 重复的那份丢掉。
    if detail and detail in desc:
        detail = ""
    body = "，".join(x for x in (desc, detail) if x)
    if not body:
        body = str(shot.get("prompt_h3") or "").strip() or "画面延续上一镜的构图与光影"

    dur = max(1.0, float(duration or 5.0))
    if dur <= BEAT_MAX_SEC:
        return [(0.0, dur, body)]

    n = 2 if dur <= BEAT_MAX_SEC * 2 else 3
    span = dur / n
    beats: List[Tuple[float, float, str]] = []
    for i in range(n):
        start = i * span
        if i == 0:
            text = body
        elif i == n - 1:
            text = "动作收束，画面在情绪落点上短暂停留，为下一镜留出衔接"
            if narration:
                text += f"；画外音延续：「{narration[:30]}」"
        else:
            text = f"动作持续推进，保持人物外观、服装与场景光照与前一刻完全一致"
        beats.append((start, span, text))
    return beats


def _strip_end(text: str) -> str:
    """去掉句末标点，便于重新拼接（避免出现「。。」）"""
    return str(text or "").strip().rstrip("。．.；;，,！!？?")


def _spoken_clause(lines: Sequence[Dict[str, str]], slots: Dict[str, str]) -> str:
    """把台词渲染成 H3 要求的「(S1) 说：[Chinese] 台词」形式"""
    spoken: List[str] = []
    for ln in lines:
        text = _strip_end(ln.get("text"))
        if not text:
            continue
        tag = lang_tag(text) or "Chinese"
        slot = slots.get(ln.get("speaker") or "", "")
        who = f"({slot}) " if slot else ""
        spoken.append(f"{who}说：[{tag}] {text}")
    if not spoken:
        return ""
    return "；".join(spoken) + "。对白的口型、语气与情绪必须与台词语义一致。"


def build_detailed_description(shot: dict, duration: float, style: str = "",
                               picture_refs: Optional[Dict[str, str]] = None) -> str:
    """``detailed_description``：按 ``[Shot N] MM:SS.mmm`` 逐节拍写画面"""
    camera = str(shot.get("camera") or "中景").strip()
    lines = dialogue_lines(shot.get("dialogue"))
    slots = speaker_slots(lines)
    picture_refs = picture_refs or {}

    # 构图基准声明只挂在首节拍，后续节拍不必重复
    first_ref = "<Picture 1>" if "<Picture 1>" in picture_refs else ""

    beats = _beats(shot, duration)
    spoken = _spoken_clause(lines, slots)

    out: List[str] = []
    for idx, (start, _span, text) in enumerate(beats, start=1):
        clause = _strip_end(text)
        if idx == 1 and first_ref:
            clause += f"，构图、景别与人物位置以 {first_ref} 为基准"
        line = f"[Shot {idx}] {fmt_ts(start)} {camera}：{clause}。"
        # 台词落在最后一个节拍，符合「动作推进→开口说话」的时序直觉
        if idx == len(beats) and spoken:
            line += " " + spoken
        out.append(line)

    if style:
        out.append(f"全片画面风格统一为「{style}」，光影细腻，构图稳定。")
    out.append("画面中严禁出现任何文字、字幕、台词文本、水印、logo 或标识。")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 主体定义 / 保留分析
# --------------------------------------------------------------------------- #

def _subject_definitions(picture_defs: Sequence[Tuple[str, str]],
                         subjects: Sequence[Dict[str, str]]) -> str:
    lines: List[str] = []
    for label, desc in picture_defs:
        lines.append(f"{label} - {desc}")
    for i, sub in enumerate(subjects, start=1):
        name = str(sub.get("name") or f"主体{i}").strip()
        appearance = str(sub.get("appearance") or "").strip()
        lines.append(f"<Subject {i}> - {name}：{appearance}（本镜头外观、服装必须与该参考图一致）")
    return "\n".join(lines) if lines else "<Picture 1> - 本镜头的画面参考图"


def _retention_analysis(picture_defs: Sequence[Tuple[str, str]],
                        subjects: Sequence[Dict[str, str]], style: str = "") -> str:
    lines: List[str] = []
    for label, desc in picture_defs:
        lines.append(f"- 必须保留 {label} 中的：{desc}")
    for sub in subjects:
        name = str(sub.get("name") or "").strip()
        if name:
            lines.append(f"- {name} 的五官、发型、发色、服装与配饰逐项保持与参考图一致，"
                         f"不得替换人物、不得改变服装款式与颜色")
    lines.append("- 保留参考图的光照方向与整体色调，只推进动作与时间，不改变场景结构")
    if style:
        lines.append(f"- 画面风格严格锁定为「{style}」，不得被参考图之外的画风带偏")
    lines.append("- 不得叠加滤镜式风格转换、不得添加文字/字幕/水印/logo；不得改变画幅比例")
    return "\n".join(lines)


def build_summary(shot: dict, duration: float, subjects: Sequence[Dict[str, str]] = ()) -> str:
    """``summary``：2~4 句目标视频概述"""
    desc = str(shot.get("description") or "").strip()
    emotion = str(shot.get("emotion") or "").strip()
    loc = str(shot.get("location") or "").strip()
    names = "、".join(str(s.get("name") or "").strip() for s in subjects if s.get("name"))
    bits: List[str] = []
    bits.append(f"一段约 {float(duration or 5):.0f} 秒的漫剧镜头" + (f"，场景为{loc}" if loc else "") + "。")
    if names:
        bits.append(f"画面主体为{names}。")
    if desc:
        bits.append(f"主要内容：{_strip_end(desc)}。")
    if emotion:
        bits.append(f"整体情绪基调为「{emotion}」。")
    if len(bits) < 2:
        bits.append("镜头保持单一连续动作，起幅与落幅明确。")
    return "".join(bits)


# --------------------------------------------------------------------------- #
# 对外构建入口
# --------------------------------------------------------------------------- #

def build_ref2va(shot: dict, picture_defs: Sequence[Tuple[str, str]],
                 subjects: Sequence[Dict[str, str]] = (), duration: Any = None,
                 style: str = "") -> str:
    """构建 Ref2VA 六段式提示词（有参考图时使用）"""
    shot = shot or {}
    dur = duration if duration is not None else (shot.get("duration") or 5)
    try:
        dur_f = float(dur)
    except (TypeError, ValueError):
        dur_f = 5.0
    pic_map = {label: desc for label, desc in picture_defs}

    sections = [
        ("subject_definitions", _subject_definitions(list(picture_defs), list(subjects))),
        ("summary", build_summary(shot, dur_f, subjects)),
        ("retention_analysis", _retention_analysis(list(picture_defs), list(subjects), style)),
        ("detailed_description", build_detailed_description(shot, dur_f, style, pic_map)),
        ("overall_soundscape", build_soundscape(shot)),
        ("non_diegetic_music", build_music(shot, style)),
    ]
    return "\n\n".join(f"{name}:\n{body}" for name, body in sections)


def build_base(shot: dict, mode: str = "T2VA", duration: Any = None, style: str = "") -> str:
    """构建 base 模式三段式提示词（无参考图时使用）"""
    shot = shot or {}
    dur = duration if duration is not None else (shot.get("duration") or 5)
    try:
        dur_f = float(dur)
    except (TypeError, ValueError):
        dur_f = 5.0
    mode = str(mode or "T2VA").upper()
    body = build_detailed_description(shot, dur_f, style)
    head = f"[{mode}] " if mode else ""
    sections = [
        ("integrated_multimodal_description", head + body),
        ("overall_soundscape", build_soundscape(shot)),
        ("non_diegetic_music", build_music(shot, style)),
    ]
    return "\n\n".join(f"{name}:\n{text}" for name, text in sections)


# --------------------------------------------------------------------------- #
# 校验 / 合并
# --------------------------------------------------------------------------- #

def _present_sections(text: str) -> List[str]:
    found = []
    low = str(text or "").lower()
    for name in REF_SECTIONS:
        if f"{name}:" in low or f"{name}：" in low:
            found.append(name)
    return found


def validate(prompt: str) -> Dict[str, Any]:
    """校验提示词是否符合 H3 规范

    返回 ``{"valid", "mode", "missing", "found"}``。
    ``mode`` 为 ``"ref"``（六段齐全）/ ``"base"``（三段齐全）/ ``"invalid"``。
    """
    text = str(prompt or "").strip()
    if not text:
        return {"valid": False, "mode": "invalid", "missing": list(REF_SECTIONS), "found": []}

    found = _present_sections(text)
    missing_ref = [s for s in REF_SECTIONS if s not in found]
    low = text.lower()
    found_base = [s for s in BASE_SECTIONS if f"{s}:" in low or f"{s}：" in low]
    missing_base = [s for s in BASE_SECTIONS if s not in found_base]

    if not missing_ref:
        return {"valid": True, "mode": "ref", "missing": [], "found": found}
    if not missing_base:
        return {"valid": True, "mode": "base", "missing": [], "found": found_base}
    # 认为更接近 ref 语义（含参考图标签）时按 ref 报缺
    if "<picture" in low or "subject_definitions" in found:
        return {"valid": False, "mode": "ref", "missing": missing_ref, "found": found}
    return {"valid": False, "mode": "base", "missing": missing_base, "found": found_base}


def merge_detail(prompt: str, detail: str) -> str:
    """把「历史薄描述 / 额外画面细节」并进 ``detailed_description`` 末尾

    用于兼容旧剧本：老提示词往往只是一句裸英文，直接整段采用会让 H3
    失去参考图语义；丢弃又浪费了模型写出的画面信息。折中做法是把它作为
    补充细节粘到详细描述之后，既保住结构化语义，又不丢信息。
    """
    detail = str(detail or "").strip()
    if not detail:
        return prompt
    lines = str(prompt or "").split("\n")
    # 找到 detailed_description 段落的结束位置（下一个顶层段落名之前）
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.strip().lower().startswith("detailed_description"))
    except StopIteration:
        return f"{prompt}\n\n补充画面细节：{detail}"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().lower().rstrip(":：") in REF_SECTIONS + BASE_SECTIONS:
            end = i
            break
    extra = f"补充画面细节：{detail.rstrip('。')}。"
    merged = lines[:end] + [extra] + lines[end:]
    return "\n".join(merged)


def resolve(shot: dict, picture_defs: Sequence[Tuple[str, str]] = (),
            subjects: Sequence[Dict[str, str]] = (), duration: Any = None,
            style: str = "") -> str:
    """生成期择优：合规的既有 ``prompt_h3`` 直接用，否则用构建器重建

    这是修「薄英文提示词把结构化构建器整个顶掉」的落点：
    - 既有提示词通过 :func:`validate`（六段/三段齐全）→ 尊重它（LLM 写的散文往往更生动）
    - 不合规（历史裸英文句、缺段）→ 用构建器产出规范提示词，并把旧文本并入细节
    """
    shot = shot or {}
    style = str(style or shot.get("style") or "").strip()
    existing = str(shot.get("prompt_h3") or "").strip()

    if picture_defs:
        built = build_ref2va(shot, picture_defs, subjects, duration=duration, style=style)
    else:
        built = build_base(shot, "T2VA", duration=duration, style=style)

    if not existing:
        return built
    verdict = validate(existing)
    if verdict["valid"]:
        return existing
    logger.info("prompt_h3 结构不合规（缺 %s），改用规范构建器并并入原描述",
                ",".join(verdict["missing"]) or "未知")
    return merge_detail(built, existing)


def style_of(shot: dict, fallback: str = "") -> str:
    """取镜头风格，缺省回退到调用方传入的风格 / 内置默认值"""
    return str(shot.get("style") or fallback or _DEFAULT_STYLE).strip()


__all__ = [
    "REF_SECTIONS", "BASE_SECTIONS",
    "fmt_ts", "lang_tag", "dialogue_lines", "speaker_slots",
    "build_soundscape", "build_music", "build_summary",
    "build_detailed_description", "build_ref2va", "build_base",
    "validate", "merge_detail", "resolve", "style_of",
]
