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

正文语言（2026-09-24 二次对齐）
--------------------------------------------------------------------------
本地手跑模板 ``H3信号10段测试001.json`` 的**六段正文全部是英文散文**，
只有台词本体用 ``<d>[Chinese] 原文</d>`` 包裹。本模块此前把六段写成中文，
与模板不一致；实测 H3 对英文长句的理解与权重分配更稳，故正文统一改为英文产出。

中文输入（``description`` / ``emotion`` / ``audio_cues`` 等）**原样嵌进英文句子**
（模板同样把角色名、台词、符文名等中文原样保留，不做罗马字转写）；
只有「风格」走 :func:`style_kit.style_suffix_en` 给出确定性的英文风格短语。
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

#: 提示词长度闸门（A-5）：H3 服务端对超长提示词**静默截断**，被砍掉的正是末尾的
#: ``overall_soundscape`` / ``non_diegetic_music`` 与后段动作节拍 —— 且日志里没有任何痕迹
#: （"关键词丢失"的典型形态）。这里在客户端先截断并告警，让丢失可见、可控。
MAX_PROMPT_CHARS = 6000
#: 单段"补充细节"（旧裸英文提示词 / merged detail）的长度闸门
MAX_DETAIL_CHARS = 800

#: 截断标记（计入闸门额度，保证输出严格不超过 limit）
_CLAMP_MARK = "…[截断]"

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_PAREN_NOTE_RE = re.compile(r"[（(]\s*(旁白|画外音|音效|配乐|BGM|VO|OS)[^）)]*[）)]", re.IGNORECASE)


def _clamp(text: str, limit: int, label: str) -> str:
    """把 ``text`` 截到 ``limit`` 字符以内；超长时告警并以 ``…[截断]`` 收尾

    ⚠️ 标记本身也占额度（``text[:limit - len(mark)] + mark``），保证返回值**严格 ≤ limit**。
    若照字面写成 ``text[:limit] + mark``，6000 的闸门会漏出 6005 字符 —— 服务端照样再砍一刀，
    闸门就白设了。
    """
    if len(text) <= limit:
        return text
    logger.warning("提示词超长截断：%s %d→%d 字符（末尾以 …[截断] 标记）", label, len(text), limit)
    if limit <= len(_CLAMP_MARK):
        return text[:limit]
    return text[:limit - len(_CLAMP_MARK)] + _CLAMP_MARK


def clamp_prompt(text: str, label: str = "prompt_h3") -> str:
    """对外统一入口：把最终提示词截到 :data:`MAX_PROMPT_CHARS`

    任何产出最终 H3 提示词的路径都应过一道这里，避免绕过 :func:`resolve` 的裸返回
    （例如 ``comfyui_client.resolve_h3_prompt`` 里直接放行既有 ``prompt_h3`` 的分支）。

    ``label`` **只用于日志标签**（默认 ``prompt_h3``），不影响截断行为与返回值；
    所有 kind 共用本函数时传入各自标签（如 ``prompt_qc.audio``），避免超长日志里
    统一被误标成 H3（P-4，2026-09-22）。
    """
    return _clamp(str(text or ""), MAX_PROMPT_CHARS, label or "prompt_h3")


_SECTION_RE_CACHE: Optional[re.Pattern] = None


def _SECTION_RE() -> re.Pattern:
    """段名标签行正则（模块级缓存）：``^name[:：][ \\t]*$``。

    段名标签**独占一行**（``name:`` 后到行尾只有空白），这是 build_ref2va /
    build_base 的固定格式；用整行匹配避免 body 里的普通文本被误认成段名。
    """
    global _SECTION_RE_CACHE
    if _SECTION_RE_CACHE is None:
        names = "|".join(re.escape(n) for n in REF_SECTIONS + BASE_SECTIONS)
        _SECTION_RE_CACHE = re.compile(rf"(?m)^({names})[:：][ \t]*$")
    return _SECTION_RE_CACHE


def _section_spans(text: str):
    """把 H3 文本切成 ``[(段名, 标签(含换行), body起点, 下段起点), ...]``。

    识别不出 ≥2 个段标签（非结构化文本 / 结构残缺）时返回 ``None``，调用方退化。
    """
    ms = list(_SECTION_RE().finditer(text))
    if len(ms) < 2:
        return None
    spans = []
    for i, m in enumerate(ms):
        body_start = m.end() + 1 if (m.end() < len(text) and text[m.end()] == "\n") else m.end()
        next_start = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        spans.append((m.group(1), text[m.start():body_start], body_start, next_start))
    return spans


def _compress_structured(text: str, limit: int, label: str):
    """按段压缩：从 body 最长的段开始逐段压到恰好 ≤ limit，保段名与末尾段。

    只压缩各段**正文**（``body``），段名标签行原样保留 —— 这样 :func:`validate`
    仍能识别出全部段名（含末尾的 ``overall_soundscape`` / ``non_diegetic_music``），
    不会因截断把一条结构完整的提示词判成「缺段」。压缩后仍超限则返回 ``None``
    由调用方退化到旧的末尾截断。
    """
    spans = _section_spans(text)
    if not spans:
        return None
    segs = []
    for (name, lab, body_start, next_start) in spans:
        raw = text[body_start:next_start]
        body = raw.rstrip("\n")
        sep = raw[len(body):]
        segs.append([name, lab, body, sep])
    overflow = sum(len(l) + len(b) + len(s) for _, l, b, s in segs) - limit
    if overflow <= 0:
        return None
    order = sorted(range(len(segs)), key=lambda i: len(segs[i][2]), reverse=True)
    for i in order:
        if overflow <= 0:
            break
        name, lab, body, sep = segs[i]
        if not body:
            continue
        target = max(0, len(body) - overflow)
        if target == 0:
            new_b = ""
        else:
            new_b = _clamp(body, target, f"{label}.{name}")
        overflow -= len(body) - len(new_b)
        segs[i][2] = new_b
    if overflow > 0:
        return None
    return "".join(l + b + s for _, l, b, s in segs)


def clamp_h3_prompt(text: str, limit: int = MAX_PROMPT_CHARS, label: str = "h3") -> str:
    """结构感知截断（P-2，2026-09-22）

    普通 :func:`clamp_prompt` 从**末尾**截断；而 H3 规范把 ``overall_soundscape`` /
    ``non_diegetic_music`` 放在**末尾** —— 一旦超长，末尾截断会连带砍掉这两段，
    :func:`validate` 立刻报「结构不合规（缺段）」，把一条本来完整的提示词判废。

    这里改为：超长时优先压缩**贡献超长的段**（通常是塞了超长 description 的
    ``summary`` / ``detailed_description`` 等中段），**完整保留每个段的段名标签与
    末尾两段**；识别不出结构、或压缩到极致仍超限时，退化到旧的末尾截断（保底，不更糟）。
    """
    text = str(text or "")
    if len(text) <= limit:
        return text
    out = _compress_structured(text, limit, label)
    if out is not None and len(out) <= limit:
        return out
    return _clamp(text, limit, label)


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
    """``overall_soundscape``：环境音 + 动作音 + 台词混响（画面内可闻）

    本地模板的写法是**把画面内所有可闻声音按时间顺序铺开**（英文散文），并在有台词时
    顺带描述「台词的音色与混响」（例如 ``Her spoken words reverberate cleanly
    against the obsidian platform, the echo of "娶我" lingering for a full second``）。
    这既补齐了配音的空间感，也**再次向模型确认「人物确实在说话」**——对驱动口型
    有正面作用。

    ⚠️ 历史实现结尾固定写「全程无解说、无旁白念白」，本意是防旁白，但副作用是
    在同一段里同时出现「台词」与「无念白」两种相反信号，模型可能因此把台词
    降级成画外音（口不动）。现改为只在**确实没有台词**时才声明无旁白。
    """
    loc = str(shot.get("location") or scene_hint or "").strip()
    sfx = _clean_sfx(shot.get("audio_cues") or "")
    emotion = str(shot.get("emotion") or "").strip()
    lines = dialogue_lines(shot.get("dialogue"))
    parts: List[str] = []
    if sfx:
        parts.append(sfx)
    if loc:
        parts.append(f"the ambient room tone of {loc}")
    elif emotion:
        parts.append("a quiet ambient room tone")
    if emotion:
        parts.append(f"breathing and fabric rustle matched to the 「{emotion}」 mood")
    if not parts:
        parts.append("a quiet ambient room tone, with breathing and fabric rustle clearly audible")
    body = ", ".join(parts).rstrip("。；;，,")
    if lines:
        said = _strip_end(lines[-1].get("text"))[:12]
        where = loc or "the scene"
        if said:
            tail = (f'The echo of "{said}" lingers for a moment before dissolving into the '
                    f'ambient tone; the spoken voice spreads naturally through {where}, '
                    f'synced to the lip movement.')
        else:
            tail = (f"The spoken voice spreads naturally through {where}, synced to the "
                    f"lip movement, the echo dissolving into the ambient tone.")
        return f"{body}. {tail}"
    return (f"{body}. No narration or voice-over throughout; the sound rises and falls "
            f"naturally with the camera movement.")


def build_music(shot: dict, style: str = "") -> str:
    """``non_diegetic_music``：画面外配乐（角色听不到）

    本地模板 10 段里 **9 段直接写 ``N/A``**（只有 1 段写了说明性 N/A）—— 即
    默认**不生成**画外配乐，配乐由后期另行处理。故本函数默认也返回 ``N/A``：

    - 显式关闭（``music: false`` / ``"false"`` / ``"无"`` 等）→ ``N/A``
    - 调用方给了**具体配乐描述** → 尊重它，并包成英文句
    - 其余情况（含情绪已知）→ 跟随模板写 ``N/A``

    ⚠️ 历史实现会按情绪**凭空生成**一段配乐描述，与模板「默认 N/A」不一致。
    ``N/A`` 段名仍在（:func:`validate` 靠段名判合规），六段结构不受影响。
    """
    raw_flag = shot.get("non_diegetic_music")
    if raw_flag is None:
        raw_flag = shot.get("music")
    # 显式关闭配乐：布尔 False / 字符串 "false"/"none"/"n/a"/"无" 一律视为 N/A
    if raw_flag is not None and not isinstance(raw_flag, str):
        if raw_flag is False:
            return "N/A"
    if isinstance(raw_flag, str) and raw_flag.strip().lower() in (
            "false", "none", "n/a", "na", "no", "off", "无", "不要", "不需要"):
        return "N/A"
    if isinstance(raw_flag, str) and raw_flag.strip() and raw_flag.strip() not in ("true", "yes", "on"):
        # 调用方直接给了配乐描述 → 尊重它（包成英文句，与模板语言一致）
        return (f"Non-diegetic score: {_strip_end(raw_flag)}; it stays strictly outside "
                f"the frame, with no sung vocals.")
    # 默认跟随模板：本段不生成音乐，配乐交由后期处理
    return "N/A"


# --------------------------------------------------------------------------- #
# 画面时间轴
# --------------------------------------------------------------------------- #

def _beats(shot: dict, duration: float) -> List[Tuple[float, float, str]]:
    """把单镜拆成时间轴节拍 ``[(起始秒, 时长, 描述)]``

    时长 ≤ ``BEAT_MAX_SEC`` 时只有一个节拍；更长时按 2~3 个节拍铺满，
    避免「描述只有一瞬、视频却要演 12 秒」的空转。

    ⚠️ 节拍描述以**英文**产出（对齐本地模板）。``description`` / ``visual_detail``
    里的中文原文按模板惯例**原样保留**（模板同样把中文台词、角色名嵌在英文句里），
    只把「衔接语」写成英文。
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
    # ⚠️ 每段各自去掉句末标点再 join：LLM 常在 description 末尾带「。」，
    # 直接拼会产出「。，」连排（视频模型会把它当断句，浪费提示词预算）。
    body = ", ".join(x for x in (_strip_end(desc), _strip_end(detail)) if x)
    if not body:
        body = _strip_end(str(shot.get("prompt_h3") or "")) or \
            "the framing continues from the previous shot with the same composition and lighting"

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
            # 本地模板的收尾写法是「动作推进 → 说话 → 收束」，最后一拍必须
            # **为台词留出明确的开口时机**（「and then speaks」），否则 H3 会把
            # 台词当成背景旁白、人物嘴唇不动。
            text = ("the movement settles and the posture stabilises, with the framing "
                    "cutting cleanly from the previous moment")
            if narration:
                text += f'; the voice-over continues: "{narration[:30]}"'
        else:
            text = ("the action continues to advance, keeping the characters' appearance, "
                    "wardrobe and scene lighting exactly consistent with the previous moment")
        beats.append((start, span, text))
    return beats


def _strip_end(text: str) -> str:
    """去掉句末标点，便于重新拼接（避免出现「。。」）"""
    return str(text or "").strip().rstrip("。．.；;，,！!？?")


#: 说话动作的前导语（按性别/未知各一句）。这段**必须显式描述「开口说话」的
#: 物理动作**，否则 H3 会把台词只当成音轨去配音、画面里人物嘴唇不动（实测现象）。
#: 本地手跑模板（H3信号10段测试001.json）里每句台词前都有
#: ``speaks — a clear, resonant female voice with ... at a measured declarative rate``，
#: 那正是模型据此驱动口型的依据，不能省。
_SPEAK_LEAD = {
    "male": "turns his head and speaks — a clear male voice at a measured spoken rate",
    "female": "turns her head and speaks — a clear female voice at a measured spoken rate",
    "": "speaks — a clear voice at a measured spoken rate",
}


def _guess_voice(form: str) -> str:
    """从说话人署名猜音色性别键（女帝/娘… → female；男/爷… → male；判不出返回 ``""``）"""
    t = str(form or "")
    if re.search(r"女|娘|妃|后|母|妹|姐|婆|妈", t):
        return "female"
    if re.search(r"男|爷|父|兄|弟|帝|王|公|叔|伯", t):
        return "male"
    return ""


def _spoken_clause(lines: Sequence[Dict[str, str]], slots: Dict[str, str],
                   speak_lead: str = "") -> str:
    """把台词渲染成**本地模板同格式**的 H3 台词句

    本地手跑模板（``H3信号10段测试001.json``）的写法是::

        She holds still, breathing slowly, and then speaks — a clear, resonant
        female voice with sharp regal timbre and unwavering pitch, at a measured
        declarative rate (S1): <d>[Chinese] 我，穆兰圣女……</d> She closes her lips
        after the final word.

    历史实现写成 ``(S1) 说：[Chinese] 台词。对白的口型、语气与情绪必须与台词语义一致。``，
    与本地格式有本质差别，会导致两个问题：

    1. **人物不开口** —— 旧写法只声明「说」，没有「开口/口型开合」的动作描述，
       H3 倾向只出配音、不做唇部动画（实测现象）。
    2. 中文指令「说：」夹在英文描述里，与模板的英文语境不一致。

    现按模板改为：先写英语「开口说话」动作 + 音色语速，再用 ``(SN): <d>[Chinese] 台词</d>``
    包住台词，最后补一句「说完合上嘴唇」收口。
    """
    spoken: List[str] = []
    for ln in lines:
        text = str(ln.get("text") or "").strip()
        if not text:
            continue
        tag = lang_tag(text) or "Chinese"
        speaker = str(ln.get("speaker") or "").strip()
        slot = slots.get(speaker, "")
        who = f"({slot}): " if slot else ""
        lead = speak_lead or _SPEAK_LEAD.get(_guess_voice(speaker), "") or _SPEAK_LEAD[""]
        spoken.append(f"{lead} {who}<d>[{tag}] {text}</d>")
    if not spoken:
        return ""
    return " ".join(spoken) + " After the final word the lips close and the mouth returns to a still, closed position."


#: 景别中文 → 英文（对齐模板：``A full shot`` / ``a medium close-up`` …）。
#: 模板把景别写成**英文名词短语**，历史实现直接嵌中文「中景：」，与模板不一致。
_CAMERA_EN = {
    "大远景": "A wide establishing shot",
    "远景": "A wide shot",
    "全景": "A full shot",
    "大全景": "A very wide shot",
    "中景": "A medium shot",
    "中近景": "A medium close-up",
    "近景": "A close-up",
    "特写": "A close-up",
    "大特写": "An extreme close-up",
    "微距": "A macro close-up",
}


def _camera_en(camera: str) -> str:
    """景别 → 英文短语；认不出时返回空串（交由调用方退化为无景别写法，不瞎猜）"""
    t = str(camera or "").strip()
    if not t:
        return ""
    if t in _CAMERA_EN:
        return _CAMERA_EN[t]
    # 子串兜底：「中近景仰拍」这类带修饰的写法也能命中
    for zh in sorted(_CAMERA_EN, key=len, reverse=True):
        if zh in t:
            return _CAMERA_EN[zh]
    # 已经是英文（调用方直接给了英文景别）→ 原样用
    if re.match(r"^[A-Za-z]", t):
        return t
    return ""


def _mid_sentence(clause: str) -> str:
    """把句首大写压成小写（用于「At 00:06.000, the camera cuts to a medium shot」这种句中位置）

    ⚠️ 不处理 ``An extreme close-up`` 这类**首字母即元音 A** 的写法：直接小写会得到
    「a extreme」，需要用 ``an``。这里一并纠正冠词。
    """
    s = str(clause or "").strip()
    if not s:
        return ""
    if s.startswith("An "):
        return "an " + s[3:]
    if s.startswith("A "):
        return "a " + s[2:]
    return s[0].lower() + s[1:] if s[:1].isupper() else s


def _style_opening(style: str) -> str:
    """``detailed_description`` 的首句风格声明（对齐模板 ``The target video uses …``）

    模板首句是 ``The target video uses a Chinese xianxia cultivation drama style with
    cool moonlit silver-and-teal palette, soft frontal moonlight and a shallow depth of
    field …``。这里用 :func:`style_kit.style_suffix_en` 把中文风格串翻成确定性的
    英文短语，再套上模板句式。

    ⚠️ 风格已在此处（段**首**）声明，故 :func:`build_detailed_description` 不再于
    段尾追加中文「全片画面风格统一为…」—— 同一风格声明两遍会放大其权重。
    """
    from style_kit import style_suffix_en  # 延迟导入：避免模块级循环依赖

    suffix = style_suffix_en(style, with_tail=False) if style else ""
    body = suffix[len("Style: "):].strip() if suffix.startswith("Style: ") else suffix
    if not body:
        return ("The target video keeps a consistent visual style across the whole "
                "segment, with delicate lighting and stable composition.")
    return (f"The target video uses a {body} style, with a shallow depth of field that "
            f"keeps the speaking faces as the sharp focal plane while the background "
            f"falls into soft bokeh.")


def build_detailed_description(shot: dict, duration: float, style: str = "",
                               picture_refs: Optional[Dict[str, str]] = None) -> str:
    """``detailed_description``：按 ``[Shot N]`` 逐节拍写画面（本地模板同格式）

    与本地手跑模板（``H3信号10段测试001.json``）对齐的要点：

    - **首句写全片风格**（``The target video uses … style … bokeh.``），景别用英文短语。
    - 首镜用 ``[Shot 1]``，**后续镜用 ``At MM:SS.mmm, the camera cuts to ...``** 起头，
      时间码内嵌在句子里（不再是「[Shot N] 00:03.500 中景：」）。
    - 无台词的镜头必须**显式写 ``No dialogue``** —— 留空会让模型自行「补台词」，
      进而把台词画成字幕；模板里每个无台词节拍都明确标注。
    - 结尾**不再追加**「画面中严禁出现任何文字、字幕…」这类中文禁令：模板里没有，
      而且它本身就在提示词里引入了「字幕/文字」这两个词，反而更容易诱发字幕。
    """
    camera = str(shot.get("camera") or "中景").strip()
    camera_en = _camera_en(camera)
    lines = dialogue_lines(shot.get("dialogue"))
    slots = speaker_slots(lines)
    picture_refs = picture_refs or {}

    # 构图基准声明只挂在首节拍，后续节拍不必重复
    first_ref = "<Picture 1>" if "<Picture 1>" in picture_refs else ""

    beats = _beats(shot, duration)
    spoken = _spoken_clause(lines, slots)

    out: List[str] = [_style_opening(style)]
    for idx, (start, _span, text) in enumerate(beats, start=1):
        clause = _strip_end(text)
        # A-5：单节拍的画面细节（description + visual_detail）也设闸门，
        # 否则历史超长描述会把 detailed_description 整段撑爆。
        clause = _clamp(clause, MAX_DETAIL_CHARS, "build_detailed_description.detail")
        if idx == 1 and first_ref:
            clause += f"; the composition, framing and character placement follow {first_ref}"
        # 统一补句号收口（clause 已 strip 掉原句末标点，不会出现「。。」）
        clause += "."
        # 模板格式：首镜 [Shot 1]，后续镜「At 时间码, the camera cuts to」。
        # 景别只在首镜点明，后续由画面内容承接（模板同样不逐镜重复景别词）。
        if idx == 1:
            head = f"[Shot 1] {camera_en}: " if camera_en else "[Shot 1] "
        else:
            # ⚠️ 句中位置必须压小写：``cuts to A medium shot`` 是错的。
            cam_mid = _mid_sentence(camera_en)
            head = (f"At {fmt_ts(start)}, the camera cuts to {cam_mid}: " if cam_mid
                    else f"At {fmt_ts(start)}, the camera cuts to a new framing: ")
        # 台词落在最后一个节拍，符合「动作推进→开口说话」的时序直觉
        if idx == len(beats) and spoken:
            line = head + clause + " " + spoken
        else:
            # 无台词节拍显式标注，避免模型自补台词 → 画成字幕
            line = head + clause + " No dialogue."
        out.append(line)

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 主体定义 / 保留分析
# --------------------------------------------------------------------------- #

def _subject_definitions(picture_defs: Sequence[Tuple[str, str]],
                         subjects: Sequence[Dict[str, str]],
                         style: str = "") -> str:
    """``subject_definitions``：逐张参考图声明用途 + 逐主体描述外观（英文句式）

    模板写法::

        <Picture 1> is the reference image defining the appearance, costume and style
        of the Male Lead 韩立, and serves as the composition anchor for his on-screen shots.
        <Subject 1> is 韩立 in <Picture 1> — a young Chinese male with ...

    ⚠️ 中文外观描述按模板惯例**原样嵌进英文句子**（模板同样保留中文人名/术语）。
    """
    lines: List[str] = []
    # 主体 → 其参考图标签（用于 ``<Subject N> ... in <Picture M>`` 的归属声明）
    subj_pics: Dict[str, str] = {}
    for i, sub in enumerate(subjects, start=1):
        name = str(sub.get("name") or "").strip()
        pic = str(sub.get("picture") or "").strip()
        if not pic:
            pic = f"<Picture {min(i, max(1, len(picture_defs)))}>"
        if name:
            subj_pics[name] = pic

    for label, desc in picture_defs:
        # 找出该参考图对应的主体名（有则写进句子，便于模型建立图-人绑定）
        owner = ""
        for name, pic in subj_pics.items():
            if pic == label:
                owner = name
                break
        if owner:
            lines.append(
                f"{label} is the reference image defining the appearance, costume and "
                f"style of {owner}, and serves as the composition anchor for their "
                f"on-screen shots: {desc}.")
        else:
            lines.append(
                f"{label} is the reference image defining the environment, materials and "
                f"lighting mood of the scene: {desc}.")
    for i, sub in enumerate(subjects, start=1):
        name = str(sub.get("name") or f"Subject {i}").strip()
        appearance = str(sub.get("appearance") or "").strip()
        pic = subj_pics.get(name, f"<Picture {min(i, max(1, len(picture_defs)))}>")
        lines.append(
            f"<Subject {i}> is {name} in {pic} — {appearance}; the on-screen appearance "
            f"and costume must stay consistent with this reference image.")
    return "\n".join(lines) if lines else \
        "<Picture 1> is the reference image defining the appearance and composition of this shot."


def _retention_analysis(picture_defs: Sequence[Tuple[str, str]],
                        subjects: Sequence[Dict[str, str]], style: str = "",
                        shots: str = "") -> str:
    """``retention_analysis``：逐主体声明必须保留的外观项（英文句式）

    模板写法::

        <Subject 2> (appears in [Shot 1], [Shot 2]): fully_preserved - her long black
        hair, pale teal-blue silk veil, ... are all retained without change.
        <Picture 2> ([Shot 2] composition anchor): fully_preserved - ...

    历史实现写成中文「必须保留 …」bullet 列表，与模板的
    ``fully_preserved`` 句式不一致（P-3 对齐项）。
    """
    lines: List[str] = []
    appear = f" (appears in {shots})" if shots else ""
    for i, sub in enumerate(subjects, start=1):
        name = str(sub.get("name") or f"Subject {i}").strip()
        appearance = str(sub.get("appearance") or "").strip()
        item = appearance or "their facial features, hairstyle and costume"
        lines.append(f"<Subject {i}> {name}{appear}: fully_preserved - {item}, all "
                     f"retained without change.")
    # ⚠️ 参考图要按**用途**分开写保留项：主体参考图管「costume / palette / hairstyle」，
    # 场景参考图管「scene structure / materials / lighting」。若不分流，场景图也会被
    # 写成「the costume … follow the reference image exactly」——语义错位的假声明。
    subj_pics = {str(s.get("picture") or "").strip() for s in subjects}
    subj_pics.discard("")
    for label, desc in picture_defs:
        is_env = label not in subj_pics if subj_pics else False
        if is_env:
            lines.append(f"{label}{appear}: fully_preserved - {desc}; the scene "
                         f"structure, materials and lighting follow the reference image exactly.")
        else:
            lines.append(f"{label}{appear}: fully_preserved - {desc}; the costume, palette "
                         f"and hairstyle follow the reference image exactly.")
    lines.append("Lighting direction and overall colour grading are retained from the "
                 "reference images; only the action and time advance, with no change to "
                 "the scene structure.")
    if style:
        lines.append(f"The visual style stays locked to {style} and is not pulled off "
                     f"course by anything outside the reference images.")
    # ⚠️ 本地模板的 retention_analysis **不含任何「禁止文字/字幕」的否定指令**，
    # 只描述「哪些内容必须保留」。历史实现在这里写「不得添加文字/字幕/水印/logo」，
    # 副作用是：提示词里凭空出现「字幕」「文字」两个词，H3 反而更容易把它们画进画面
    # （实测视频生成出了字幕）。故删掉该行，改用「必须保留」的正向表述。
    lines.append("Composition, aspect ratio, character appearance and scene structure "
                 "stay consistent from shot to shot, keeping the sequence continuous.")
    return "\n".join(lines)


def build_summary(shot: dict, duration: float, subjects: Sequence[Dict[str, str]] = ()) -> str:
    """``summary``：2~4 句目标视频概述

    模板 10 段**无一例外**都以 ``[reference generation]`` 开头 —— 这是 Ref2VA
    模式的显式声明，告诉模型「本段以参考图为基础生成」（P-1 对齐项）。
    历史实现缺此前缀，与模板不一致。
    """
    desc = str(shot.get("description") or "").strip()
    emotion = str(shot.get("emotion") or "").strip()
    loc = str(shot.get("location") or "").strip()
    names = ", ".join(str(s.get("name") or "").strip() for s in subjects if s.get("name"))
    bits: List[str] = []
    bits.append(f"The target video is a comic-drama shot of about "
                f"{float(duration or 5):.0f} seconds" + (f", set in {loc}" if loc else "") + ".")
    if names:
        bits.append(f"The on-screen subjects are {names}.")
    if desc:
        bits.append(f"Main content: {_strip_end(desc)}.")
    if emotion:
        bits.append(f"The overall emotional tone is 「{emotion}」.")
    if len(bits) < 2:
        bits.append("The shot keeps a single continuous action with a clear start and end.")
    # ⚠️ 句间必须补空格：bits 各自已带句末「.」，直接 "".join 会产出
    # 「…12 seconds, set in X.The on-screen subjects…」这种粘连句（模型会当断句错误）。
    return "[reference generation] " + " ".join(bits)


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

    # retention_analysis 里声明「本段出现在哪些镜头」，与模板 ``(appears in [Shot 1]…)``
    # 同构。单次调用只知道这一个 shot，故按节拍数折算（单节拍即 [Shot 1]）。
    n_beats = len(_beats(shot, dur_f))
    shots = ", ".join(f"[Shot {i}]" for i in range(1, n_beats + 1))

    sections = [
        ("subject_definitions", _subject_definitions(list(picture_defs), list(subjects), style)),
        ("summary", build_summary(shot, dur_f, subjects)),
        ("retention_analysis", _retention_analysis(list(picture_defs), list(subjects),
                                                    style, shots)),
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
    detail = _clamp(str(detail or "").strip(), MAX_DETAIL_CHARS, "merge_detail.detail")
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
        return clamp_h3_prompt(built)
    verdict = validate(existing)
    if verdict["valid"]:
        return clamp_h3_prompt(existing)
    logger.info("prompt_h3 结构不合规（缺 %s），改用规范构建器并并入原描述",
                ",".join(verdict["missing"]) or "未知")
    return clamp_h3_prompt(merge_detail(built, existing))


def style_of(shot: dict, fallback: str = "") -> str:
    """取镜头风格，缺省回退到调用方传入的风格 / 内置默认值"""
    return str(shot.get("style") or fallback or _DEFAULT_STYLE).strip()


__all__ = [
    "REF_SECTIONS", "BASE_SECTIONS",
    "MAX_PROMPT_CHARS", "MAX_DETAIL_CHARS", "clamp_prompt", "clamp_h3_prompt",
    "fmt_ts", "lang_tag", "dialogue_lines", "speaker_slots",
    "build_soundscape", "build_music", "build_summary",
    "build_detailed_description", "build_ref2va", "build_base",
    "validate", "merge_detail", "resolve", "style_of",
]
