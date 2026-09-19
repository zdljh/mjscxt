# -*- coding: utf-8 -*-
"""台词结构统一工具：结构化 {speaker, text} 台词 + 旧剧本字符串兼容。

背景：
- 分镜/剧本生成阶段直接把台词写成结构化对象 ``dialogue = {"speaker": 角色名, "text": 台词}``；
- 配音（tts_client）、字幕（video_postprocess）、H3 提示词（comfyui_client）、
  质检描述（app/script_prompt_analyzer）等下游统一调用本模块读取，
  不再各自对字符串做正则切分；
- 旧剧本（dialogue 为纯字符串，或 "角色名：台词" 形式）保持兼容：
  说话人按「前缀命中角色表 → 文本包含角色名 → 自称句式 → 出场角色首位」逐级回退推断。

本模块不依赖项目内其他模块，可被任意链路安全导入。
"""
import re
from typing import Iterable, List, Optional

# 自称句式标记（用于没有显式说话人时的名字片段匹配）
SELF_REF_MARKERS = (
    "吾乃", "吾是", "老夫", "在下", "本座", "余乃", "某乃", "本人",
    "我叫", "我是", "妾身", "小女子", "晚辈", "师兄", "师姐", "为师",
)

# "角色名：台词" 前缀（1~12 字，且不含标点/换行，避免误吃正文）
_PREFIX_RE = re.compile(r"^\s*([^：:，。！？、\n]{1,12})\s*[：:]\s*")


def _pick(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def iter_names(characters: Optional[Iterable]) -> List[str]:
    """从角色表（[{"name":...}] 或 ["林风"]）中取出全部非空角色名"""
    out: List[str] = []
    for ch in characters or []:
        name = ""
        if isinstance(ch, dict):
            name = _pick(ch.get("name"), ch.get("character"), ch.get("role"))
        elif isinstance(ch, str):
            name = ch.strip()
        if name and name not in out:
            out.append(name)
    return out


# ============================ 读 ============================

def dialogue_text(raw) -> str:
    """取台词纯文本（兼容 str / dict / list / None）"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return _pick(raw.get("text"), raw.get("line"), raw.get("dialogue"), raw.get("content"))
    if isinstance(raw, (list, tuple)):
        return " ".join(t for t in (dialogue_text(i) for i in raw) if t).strip()
    return str(raw).strip()


def dialogue_speaker(raw) -> str:
    """取显式说话人（兼容 str / dict / list / None），无则返回空串"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return ""
    if isinstance(raw, dict):
        return _pick(raw.get("speaker"), raw.get("character"), raw.get("role"), raw.get("name"))
    if isinstance(raw, (list, tuple)):
        for item in raw:
            sp = dialogue_speaker(item)
            if sp:
                return sp
        return ""
    return ""


def has_dialogue(raw) -> bool:
    """是否存在可朗读台词"""
    return bool(dialogue_text(raw))


# ============================ 推断（兼容旧剧本） ============================

def match_prefix_speaker(text: str, characters: Optional[Iterable]) -> str:
    """从 "角色名：台词" 前缀识别说话人（必须与角色表命中，否则视为正文冒号）"""
    m = _PREFIX_RE.match(str(text or ""))
    if not m:
        return ""
    cand = m.group(1).strip()
    for name in iter_names(characters):
        if cand == name or cand in name or name in cand:
            return name
    return ""


def infer_speaker(text: str, characters: Optional[Iterable],
                  cast: Optional[Iterable] = None) -> str:
    """无显式说话人时按文本推断角色名（全名命中 > 名字片段+自称句式 > 出场角色首位）"""
    text = str(text or "")
    if not text:
        return _pick(*(iter_names(cast)[:1]))
    best, best_score, best_len = "", 0, 0
    for name in iter_names(characters):
        score = 0
        if name in text:
            score = 3
        else:
            tokens = [t for t in name.replace("·", " ").replace("・", " ").split() if t]
            for t in tokens:
                if len(t) >= 2 and t in text:
                    score = max(score, 2)
                elif len(t) == 1 and t in text and any(mk in text for mk in SELF_REF_MARKERS):
                    score = max(score, 2)
        if score > best_score or (score == best_score and score and len(name) > best_len):
            best, best_score, best_len = name, score, len(name)
    return best or _pick(*(iter_names(cast)[:1]))


# ============================ 写 / 规范 ============================

def normalize_dialogue(raw, characters: Optional[Iterable] = None,
                       cast: Optional[Iterable] = None, max_text: int = 200) -> dict:
    """把任意写法的台词规范为 ``{"speaker": str, "text": str}``（空台词返回双空串）。

    - 已是结构化的：保留 speaker / text；
    - 旧字符串且带 "角色名：" 前缀：解析出说话人；
    - 完全无线索：按角色表推断（推断不出则取本镜头出场角色首位）。
    """
    text = re.sub(r"\s+", " ", dialogue_text(raw)).strip()
    if not text:
        return {"speaker": "", "text": ""}
    speaker = dialogue_speaker(raw)
    if not speaker:
        speaker = match_prefix_speaker(text, characters)
    if not speaker:
        speaker = infer_speaker(text, characters, cast)
    return {"speaker": speaker[:40], "text": text[:max_text]}


def normalize_lines(raw, characters: Optional[Iterable] = None,
                    cast: Optional[Iterable] = None, max_text: int = 200) -> List[dict]:
    """把任意写法的台词规范为 ``[{"speaker": str, "text": str}, ...]``（无台词返回空列表）。

    - 已结构化（dict / list[dict]）：逐条保留 speaker / text；
    - 旧字符串：按 "角色名：" 前缀 → 角色表推断 逐级补全说话人；
    - 空台词条目直接丢弃。
    """
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: List[dict] = []
    for it in items:
        line = normalize_dialogue(it, characters, cast, max_text=max_text)
        if line["text"]:
            out.append(line)
    return out


def format_line(raw, sep: str = "：", with_speaker: bool = True) -> str:
    """把台词格式化为单行文本（字幕 / 提示词用）：有说话人则 "角色：台词"，否则纯台词"""
    text = dialogue_text(raw)
    if not text:
        return ""
    speaker = dialogue_speaker(raw) if with_speaker else ""
    return f"{speaker}{sep}{text}" if speaker else text


# ===================== 剧本「可配音 / 可出片」体检 =====================


def _shot_label(shot: dict, idx: int) -> str:
    sid = shot.get("shot_id")
    seq = shot.get("seq") or shot.get("shot_no")
    return f"#{seq if seq else (sid if sid is not None else idx + 1)}"


def audit_script(script: Optional[dict]) -> dict:
    """体检剧本在进入「配音 / 出片」前是否存在内容缺口。

    为什么需要它：LLM 失败时 novel_to_script 会按原文兜底生成镜头
    （``fallback=True``），这类镜头是 **dialogue=[] 且 prompt_h3=""** ——
    原文照搬、没有任何台词。脚本生成阶段看起来「成功了」（镜头数正常），
    但到配音环节就会一句都合不出来，或者合出整集无声；用户完全不知道
    为什么「配音生成成功却没有声音」。

    这里把这类缺口显式暴露出来，返回：
      - silent_shots：台词、旁白、音效提示**三者全空**（该镜成片既无人声也无音效）
      - no_voice_shots：无台词但写了音效提示的镜头（**正常留白**，不算缺陷）
      - narration_line_count：无台词但残留旁白的镜头数（**旧「旁白时代」剧本的遗留**；
        本系统自 2026-09-19 起剧本阶段不再产出旁白，见 novel_to_script.REWRITE_RULES 第 8 条）
      - fallback_shots：兜底生成的镜头（内容照搬原文，需人工润色）
      - blank_visual_shots：description 与 prompt_h3 均为空（出片没有画面提示）
      - unknown_speaker_shots：出场角色不在角色表里（音色会退回「旁白」）
      - overlong_speech_shots：台词量超出单镜时长上限（配音会溢出到后面几镜）
      - warnings：可直接展示给用户的中文提示

    纯函数、无副作用、不依赖项目内其它模块。
    """
    script = script if isinstance(script, dict) else {}
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    # 角色表名集合（用于判断说话人/出场角色是否可识别）
    known = {n for n in iter_names(script.get("characters") or [])}

    silent, fallback, blank_visual, unknown_speaker = [], [], [], []
    no_voice, overlong, legacy_narration = [], [], []
    speakable_lines = 0        # 有台词的镜头数
    narration_lines = 0        # 无台词但残留旁白的镜头数（旧剧本遗留）
    for i, s in enumerate(shots):
        label = _shot_label(s, i)
        if s.get("fallback"):
            fallback.append(label)
        dlg_text = dialogue_text(s.get("dialogue"))
        if not dlg_text and str(s.get("dialogue_text") or "").strip():
            dlg_text = str(s.get("dialogue_text")).strip()
        narration = str(s.get("narration") or "").strip()
        cues = str(s.get("audio_cues") or "").strip()
        if dlg_text:
            speakable_lines += 1
        elif narration:
            narration_lines += 1
            legacy_narration.append(label)
        elif cues:
            # 无台词但交代了音效/配乐 → 正常留白（成片由音效铺底），不是缺陷
            no_voice.append(label)
        else:
            silent.append(label)
        # 单镜台词量超出时长上限：配音会沿时间轴溢出到后面几镜，尾部被成片截掉。
        # 该字段由 novel_to_script._norm_shots 在标准化时写入（生成期对账），不是此处凭空计算。
        try:
            overflow = float(s.get("duration_overflow_sec") or 0)
        except (TypeError, ValueError):
            overflow = 0.0
        if overflow > 0:
            overlong.append(f"{label}（超出 {overflow:g}s）")
        desc = str(s.get("description") or "").strip()
        if not desc and not str(s.get("prompt_h3") or "").strip():
            blank_visual.append(label)
        cast = s.get("characters_in_shot") or s.get("characters") or []
        names = []
        for c in cast:
            nm = c.get("name") if isinstance(c, dict) else str(c or "")
            if str(nm or "").strip():
                names.append(str(nm).strip())
        if names and known and any(n not in known for n in names):
            unknown_speaker.append(label)

    warnings: List[str] = []
    # 整集兜底＝剧本实际上没被模型加工过（实测 E2E 项目「第1集」6/6 都是兜底），
    # 这种情况必须升级提示，否则用户会以为「剧本生成成功了」。
    all_fallback = bool(shots) and len(fallback) == len(shots)
    if all_fallback:
        warnings.append(
            f"该集全部 {len(shots)} 个镜头都是「模型失败后按原文兜底」生成的，"
            f"剧本实际上未经模型加工（无分镜设计、无台词）。建议重新生成剧本，"
            f"或人工分镜后再进入配音出片。")
    elif fallback:
        warnings.append(
            f"有 {len(fallback)} 个镜头是「模型失败后按原文兜底」生成的（{', '.join(fallback[:8])}"
            f"{' 等' if len(fallback) > 8 else ''}）：内容照搬原文、没有台词，"
            f"建议人工润色后再配音出片。")
    if silent:
        warnings.append(
            f"有 {len(silent)} 个镜头没有台词，也没写音效提示（{', '.join(silent[:8])}"
            f"{' 等' if len(silent) > 8 else ''}）：成片到该镜头既无人声也无音效，"
            f"请补写 audio_cues 音效提示，或把原文的心理活动改写成该角色的自语台词。")
    if no_voice:
        warnings.append(
            f"有 {len(no_voice)} 个镜头是无台词的纯画面镜（{', '.join(no_voice[:8])}"
            f"{' 等' if len(no_voice) > 8 else ''}）：已写明音效提示，成片由音效与配乐铺底，属正常留白。")
    if narration_lines:
        warnings.append(
            f"有 {narration_lines} 个镜头残留了「旁白」文本（{', '.join(legacy_narration[:8])}"
            f"{' 等' if narration_lines > 8 else ''}）：本系统已不再产出旁白，"
            f"这批剧本是改造前生成的，成片会带画外音解说、且旁白时长常远超画面。建议重新生成剧本。")
    if overlong:
        warnings.append(
            f"有 {len(overlong)} 个镜头的台词量超出单镜时长上限（{', '.join(overlong[:8])}"
            f"{' 等' if len(overlong) > 8 else ''}）：配音沿时间轴溢出会挤掉后面镜头的台词，"
            f"成片尾部会被静默截断。建议把这些镜头的台词拆成更多镜头。")
    if blank_visual:
        warnings.append(
            f"有 {len(blank_visual)} 个镜头既无画面描述也无 H3 提示词（{', '.join(blank_visual[:8])}"
            f"{' 等' if len(blank_visual) > 8 else ''}）：出片会缺少画面指引。")
    if unknown_speaker:
        warnings.append(
            f"有 {len(unknown_speaker)} 个镜头的出场角色不在角色表里（{', '.join(unknown_speaker[:8])}"
            f"{' 等' if len(unknown_speaker) > 8 else ''}）：音色会退回默认「旁白」。")

    stats = {
        "shot_count": len(shots),
        "speakable_lines": speakable_lines,
        "narration_line_count": narration_lines,
        "speakable_line_count_total": speakable_lines + narration_lines,
        "silent_shot_count": len(silent),
        "no_voice_shot_count": len(no_voice),
        "overlong_speech_shot_count": len(overlong),
        "fallback_shot_count": len(fallback),
        "all_fallback": all_fallback,
        "blank_visual_shot_count": len(blank_visual),
        "unknown_speaker_shot_count": len(unknown_speaker),
    }
    if shots and speakable_lines == 0:
        warnings.insert(0, f"整集 {len(shots)} 个镜头里没有任何台词，配音合成会产出 0 句音频："
                           f"本系统靠 dialogue 出声（旁白已取消），请检查剧本是否漏写台词"
                           f"（原文的心理活动应当改写成该角色的自语台词）。")
    # ok = 「可以直接进配音出片」。三种情况不放行：
    #   有「既无台词也无音效提示」的空响镜头 / 有空洞画面 /
    #   整集无任何**台词**（配音链路只读 dialogue，成片会完全无人声；旁白已取消、不再算数）/
    #   整集全是兜底镜头（剧本未经模型加工）。
    #   有残留旁白的旧剧本同样不放行：旁白通道已关闭，成片不会念它。
    # ⚠️ 「无台词但写了音效提示」的纯画面镜**不算缺陷** —— 新口径允许留白（旧口径曾把它算作
    #    silent，会把合法的动作镜/空镜整集判失败）。
    return {
        "ok": (bool(shots) and not (silent or blank_visual)
               and speakable_lines > 0 and not all_fallback),
        "stats": stats,
        "warnings": warnings,
        "problem_shots": {
            "silent": silent,
            "no_voice": no_voice,
            "overlong_speech": overlong,
            "fallback": fallback,
            "blank_visual": blank_visual,
            "unknown_speaker": unknown_speaker,
        },
    }
