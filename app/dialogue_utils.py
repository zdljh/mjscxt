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
