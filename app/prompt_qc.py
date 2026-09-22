"""提示词预检（生成前质检）

## 为什么要有这一层

提示词是一切出图/出片的**输入**。输入错了，后面的图片质检、视频质检都只是在为错误的
结果打分 —— 用户看到的是「质检通过了，但画面不是我要的」。把质检前移到提示词，才能在
**消耗 GPU 之前**把可确定的缺陷挡掉。

## 设计取舍：预检 + 自愈，而不是一律拦截

提示词缺陷里绝大多数是**确定性可判可修**的（缺风格、缺景别、台词被写进画面、结构缺段），
所以默认策略是：

1. **确定性检查**（零模型依赖、零成本、毫秒级）—— 这里是主力，覆盖真实缺陷的大头；
2. **确定性自愈** —— 能修的当场修，生成用修好的提示词，不打断流程；
3. **只对「必然废图」的缺陷拦截**（画面内容为空、H3 结构缺段且无法修复）——
   拦截会阻塞整集生产，用它换来的收益必须足够大。

因此默认模式是 ``repair``：自愈后放行，记 issue 不阻断；``block`` 为严格模式（有任何
issue 就拦），``warn`` 为诊断模式（只记录、连自愈都不做）。

**为什么这一层不做 LLM 审阅**：预检的价值在于「免费、稳定、毫秒级、零模型依赖」——
它要能跑在每一集每一个镜头上。加一次 LLM 调用就把成本、延迟、不确定性都引进来了，
而真实缺陷的大头（缺风格、缺景别、结构缺段、台词泄漏、禁词）恰好都是确定性可判的。
语义层面的判断留给生成后的图片/视频质检（那时有真图可看，判断也更准）。

## 判据来源

全部对齐生成端的同一个单一事实源，避免「生成端一个标准、质检端另一个标准」的历史坑
（景别判定曾因两端标准不同，把 43% 的镜头误判为不合格）：

- 分镜图骨架 → ``comfyui_client.build_storyboard_prompt`` 写死的标记
- H3 结构 → ``h3_prompt_kit.REF_SECTIONS`` / ``h3_prompt_kit.validate``
- 风格 → ``style_kit.style_suffix`` / ``style_kit.with_style``
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import h3_prompt_kit
import style_kit

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 可检的提示词类型
# --------------------------------------------------------------------------- #

PROMPT_KINDS: Tuple[str, ...] = ("storyboard", "h3", "asset", "keyframe", "audio")

KIND_LABELS = {
    "storyboard": "分镜图提示词",
    "h3": "视频提示词(H3)",
    "asset": "资产参考图提示词",
    "keyframe": "尾帧提示词",
    "audio": "配音台词",
}

#: 质检模式：warn=只记录 / repair=自愈后放行 / block=有 issue 即拦
PROMPT_QC_MODES: Tuple[str, ...] = ("warn", "repair", "block")
DEFAULT_MODE = "repair"


# --------------------------------------------------------------------------- #
# 分镜图提示词：骨架标记（与 build_storyboard_prompt 一一对应）
# --------------------------------------------------------------------------- #
SB_MARK_FRAMING = "景别（必须严格遵守）"
SB_MARK_CONTENT = "动作与画面内容"
SB_MARK_REF_USAGE = "参考图用途"
SB_MARK_STYLE = "画面风格"
SB_MARK_NO_TEXT = "不得出现任何文字、字幕、台词文本、水印"

#: H3 提示词里的无文字声明（build_detailed_description 结尾固定句）
H3_NO_TEXT = "严禁出现任何文字、字幕、台词文本、水印"
H3_NO_TEXT_FULL = "画面中严禁出现任何文字、字幕、台词文本、水印、logo 或标识。"

# --------------------------------------------------------------------------- #
# 尾帧提示词：骨架标记（与 keyframe.build_end_frame_prompt 一一对应）
# --------------------------------------------------------------------------- #
#: 「锚定参考图」语义 —— 尾帧是「以首帧为参考图的图生图」，不写这句模型就会换脸换服装
KF_MARK_ANCHOR = "保持参考图的角色外观"
#: 取景连贯 —— 尾帧必须与首帧同机位同景别，否则两帧无法插值出连续动作
KF_MARK_FRAMING = "与首帧保持连贯"
#: 链式承接 —— 首帧取自上一镜尾帧时，必须写明「承接」，否则模型当成同镜两个时间点而拒绝推进动作
KF_MARK_CHAIN = "承接该画面继续推进"

#: 「字幕类指令」—— 把这些写进提示词，模型会把文字直接画进画面
_INSTRUCTION_RE = re.compile(
    r"(?:字幕|台词文本|文字|标题|水印|logo|LOGO)\s*[:：][^。；，,;！!？?]*[。；，,;！!？?]?"
)

#: 质量类空词：对图像模型无信息量，还会挤占风格/构图语义
_QUALITY_FLUFF: Tuple[str, ...] = (
    "masterpiece", "best quality", "ultra detailed", "highly detailed",
    "award winning", "8k", "4k", "杰作", "最佳画质", "超高清", "高清晰度",
)

#: 台词的可见长度下限：太短的（「嗯」「好」）在画面描述里可能是巧合，不判泄漏
_MIN_LEAK_LEN = 4

#: 资产提示词最短长度（中英混排按字符计）
_ASSET_MIN_LEN = 12

#: 时间码：与 h3_prompt_kit.fmt_ts / build_detailed_description 的写法一致
_TS_RE = re.compile(r"\[Shot\s+(\d+)\]\s+(\d{1,2}):(\d{2})\.(\d{3})")
#: 台词标记：(S1) 说：[Chinese] 台词
_SPOKEN_RE = re.compile(r"\(S\d+\)\s*[^：:]{0,6}[：:]\s*\[[A-Za-z\-]+\]")
#: 连续标点（含全角/半角混排）
_PUNCT_RUN = re.compile(r"[，、,;。；！？!?]{2,}")

# --------------------------------------------------------------------------- #
# 配音台词：缺陷标记（与 tts_client.clean_line_text / build_dub_plan 的产物对应）
# --------------------------------------------------------------------------- #
# 配音的「提示词」就是那句台词本身 —— 它会被 TTS 逐字念出来，所以判断标准与图片提示词
# 完全不同：图片提示词里「信息越多越好」，台词里**多一个字符都是噪音**。
#
# 历史缺陷（本 kind 存在的原因）：`build_dub_plan` 只做 `clean_line_text` 清洗，
# 之后台词就直奔 TTS。剧本里的结构化残留（`(S1) 说：[Chinese] …`、`[Shot 3] 00:01.200`）
# 与舞台指示（`（转身冷笑）`）会被原样念出来，成片里就能听到「左括号转身冷笑右括号」；
# 空台词或纯标点句（`……`）则会合成出一段静音，而它在流程里显示为「合成成功」——
# 要等到成片验收才发现整集缺了一句。

#: 说话人/时间码等结构化残留：会被逐字念出来的「格式化垃圾」
#: ⚠️ 顺序即优先级：「说话人前缀」必须排在「说话人标记」之前。
#:    `(S1) 说：[Chinese] 台词` 若只删 `(S1)`，会剩下 `说： 台词` ——
#:    TTS 会把这句「说：」也念出来（实测过）。必须整段前缀一起剥掉。
_AUDIO_LEAK_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("说话人前缀（如 (S1) 说：）", re.compile(r"\(\s*(?:S|SPK|N)\s*\d*\s*\)"
                                              r"\s*[^：:\n]{0,6}[：:]\s*")),
    ("语言标签（如 [Chinese]）", re.compile(r"\[(?:Chinese|English|Japanese|Korean|"
                                            r"French|German|Spanish|Portuguese|Russian|"
                                            r"Italian|Auto)\]")),
    ("镜头标签（如 [Shot 3]）", re.compile(r"\[\s*Shot\s*\d+\s*\]")),
    ("时间码（如 00:01.200）", re.compile(r"\b\d{1,2}:\d{2}\.\d{3}\b")),
    ("说话人标记（如 (S1)）", re.compile(r"\(\s*(?:S|SPK|N)\s*\d*\s*\)")),
)
#: 舞台指示关键词：括号里出现这些词说明是「给演员的提示」，不是要念的台词
_AUDIO_STAGE_HINTS: Tuple[str, ...] = (
    "转身", "回头", "叹气", "冷笑", "皱眉", "抬头", "低头", "挥手", "点头", "摇头",
    "沉默", "停", "走近", "后退", "拔剑", "落泪", "颤抖", "咬牙", "呼吸", "旁白",
    "镜头", "画面", "动作", "表情", "画外音", "内心", "os", "OS",
)
_AUDIO_BRACKET_RE = re.compile(r"[（(]([^（()）]{1,24})[)）]")
#: 零宽字符与方向控制符（肉眼看不见，但会被算进字符数、可能触发 TTS 异常）
_AUDIO_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
#: emoji / 变体选择符：TTS 念不出，还会让模型把符号读成名称
_AUDIO_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27bf\ufe0f]")
#: 有效字符（去掉空白、标点、符号后剩下的才是「会发声的内容」）
_AUDIO_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]", re.UNICODE)
#: 单句台词的舒适上限：超过后 TTS 容易在中间断句、也更容易触发超时
_AUDIO_MAX_CHARS = 60
#: 台词最短有效字数：1 个字（「嗯」）能出声但信息量过低，只提示不阻断
_AUDIO_MIN_WORDS = 2
#: 中性情绪词：这些情绪不值得切换 VoiceDesign 模式（切了反而增加音色漂移风险）
_AUDIO_NEUTRAL_EMOTIONS: Tuple[str, ...] = (
    "", "平静", "无", "常态", "自然", "如常", "normal", "neutral", "calm",
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _txt(v) -> str:
    return str(v or "").strip()


def _style_clause(style) -> str:
    """生成端写进提示词的那半句风格声明（与 build_storyboard_prompt 同源）

    返回 ``""`` 表示「这个风格没有可声明的正文」（例如只有画幅 token），
    此时任何「未声明风格」的判定都应当跳过，否则会误报。
    """
    try:
        return style_kit.style_suffix(style, head="画面风格", with_tail=False, with_aspect=False)
    except Exception:  # noqa: BLE001
        return ""


def style_declared(text: str, style) -> bool:
    """提示词里是否已承载目标风格。

    按**词**比对而不是整句比对：模型/历史数据常写「…，中国古风玄幻漫剧风格。」
    这种漏冒号的写法，整句比对会把已有的风格判成缺失，进而重复追加（双重风格）。
    """
    clause = _style_clause(style)
    if not clause:
        return True
    body = clause.split("：", 1)[-1]
    toks = [t.strip() for t in body.split("，") if t.strip()]
    if not toks:
        return True
    hay = text.lower()
    return any(t.lower() in hay for t in toks)


def _dialogue_texts(ctx) -> List[str]:
    """取镜头台词原文（用于检测「台词被写进画面提示词」）"""
    out: List[str] = []
    if not isinstance(ctx, dict):
        return out
    for d in (ctx.get("dialogue") or []):
        t = _txt(d.get("text") if isinstance(d, dict) else d)
        if len(t) >= _MIN_LEAK_LEN:
            out.append(t)
    return out


def _tidy(text: str) -> str:
    """清掉自愈后留下的重复/混排标点与多余空白

    ⚠️ 必须把**混排标点**也归一：删掉一个词后常留下「，,。」这种全角+半角混排，
    只按「同一字符重复」的规则去重是抓不到的（`([。；，])\\1+` 匹配不到「，,。」）。
    """
    s = re.sub(r"[ \t]{2,}", " ", _txt(text))
    for _ in range(3):
        # 任意 ≥2 个标点组成的串 → 归一到「其中最重的那个终止符」
        s = _PUNCT_RUN.sub(lambda m: "。" if any(c in m.group(0) for c in "。！？!?；") else "，", s)
    s = re.sub(r"[，、,;]\s*([。；！？!?])", r"\1", s)
    s = re.sub(r"^\s*[，、,;。；]+", "", s)
    s = re.sub(r"[，、,;\s]+$", "", s)
    return s.strip()


def _has_shot_content(ctx) -> bool:
    """镜头是否自带画面内容（三选一有值即算）"""
    if not isinstance(ctx, dict):
        return True   # 没有镜头上下文（例如手工传提示词来预检）→ 不据此判致命
    return bool(_txt(ctx.get("description")) or _txt(ctx.get("visual_detail"))
                or _txt(ctx.get("storyboard_prompt_zh")))


# --------------------------------------------------------------------------- #
# 分镜图提示词检查
# --------------------------------------------------------------------------- #

def _check_storyboard(prompt: str, ctx, style, ref_count: Optional[int] = None,
                      **_kw) -> Dict[str, Any]:
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空，无任何可用信息")
        return {"issues": issues, "fatal": fatal}

    if not _has_shot_content(ctx):
        fatal.append("镜头缺少画面描述（description / visual_detail / storyboard_prompt_zh 均为空）")

    if SB_MARK_CONTENT not in prompt:
        issues.append("缺少「动作与画面内容」段：模型只能自行想象画面")
    if SB_MARK_FRAMING not in prompt:
        issues.append("缺少「景别（必须严格遵守）」段：景别不受控，容易跑偏成中景")
    if ref_count and SB_MARK_REF_USAGE not in prompt:
        issues.append("给了参考图但未声明「参考图用途」：模型不知道每张图该参考什么")
    if SB_MARK_NO_TEXT not in prompt:
        issues.append("缺少「不得出现文字/字幕/水印」约束：生成图易带字幕或水印")
    if not style_declared(prompt, style):
        issues.append("未声明画面风格：画风会漂移到参考图或模型默认风格")

    # 台词被写进画面提示词 —— 模型会把台词当字幕画出来（硬缺陷，可自愈）
    for t in _dialogue_texts(ctx):
        if t in prompt:
            issues.append(f"台词文本被写进画面提示词（会被渲染成字幕）：{t[:20]}")
    if _INSTRUCTION_RE.search(prompt):
        issues.append("含「字幕/文字/水印」类指令：会被模型当作要绘制的画面内容")
    hits = [w for w in _QUALITY_FLUFF if w.lower() in prompt.lower()]
    if hits:
        issues.append("含质量类空词（无信息量且挤占语义）：" + "、".join(hits[:3]))
    return {"issues": issues, "fatal": fatal}


def _repair_storyboard(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    repairs: List[str] = []
    out = prompt

    for t in _dialogue_texts(ctx):
        if t in out:
            out = out.replace(t, "")
            repairs.append("移除画面提示词里的台词文本（避免被渲染成字幕）")
    if _INSTRUCTION_RE.search(out):
        out = _INSTRUCTION_RE.sub("", out)
        repairs.append("移除「字幕/文字/水印」类指令")
    for w in _QUALITY_FLUFF:
        if w.lower() in out.lower():
            out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
            repairs.append("移除质量类空词")
    out = _tidy(out)

    if not style_declared(out, style):
        new = style_kit.with_style(out, style)
        if new and new != out:
            out = new
            repairs.append("补写画面风格声明")

    if SB_MARK_NO_TEXT not in out:
        out = out.rstrip()
        sep = "" if out.endswith(("。", "！", "？", "；")) else "。"
        out = f"{out}{sep}画面中不得出现任何文字、字幕、台词文本、水印、logo 或标识。"
        repairs.append("补写「不得出现文字/字幕/水印」约束")
    return out, repairs


# --------------------------------------------------------------------------- #
# H3 视频提示词检查
# --------------------------------------------------------------------------- #

def _check_h3(prompt: str, ctx, style, expect_refs: Optional[bool] = None,
              **_kw) -> Dict[str, Any]:
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空，H3 无法生成")
        return {"issues": issues, "fatal": fatal}

    v = h3_prompt_kit.validate(prompt)
    if not v.get("valid"):
        miss = "、".join(v.get("missing") or []) or "未知"
        # 结构缺段是出片跑偏的根因（历史缺陷：裸英文顶掉结构化构建器，211 镜 0 合规）
        fatal.append(f"H3 结构不合规（{v.get('mode')} 模式缺段：{miss}）")

    marks = [(int(m.group(1)), int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0)
             for m in _TS_RE.finditer(prompt)]
    if not marks:
        issues.append("缺少 [Shot N] MM:SS.mmm 时间码：节拍无时间轴，长镜头会空转")
    else:
        nums = [n for n, _ in marks]
        if nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
            issues.append(f"时间码编号不连续（{[n for n in nums][:8]}）：节拍顺序被打乱")
        times = [t for _, t in marks]
        if times[0] > 0.001:
            issues.append("首个时间码不是 00:00.000：视频开头会缺画面")
        if any(b < a for a, b in zip(times, times[1:])):
            issues.append("时间码非递增：节拍时间轴倒流")

    if _dialogue_texts(ctx) and not _SPOKEN_RE.search(prompt):
        issues.append("镜头有台词但缺少「(S1) 说：[Chinese] 台词」标记：口型与配音可能对不上")

    if expect_refs and "<Picture 1>" not in prompt:
        issues.append("期望使用参考图但缺少 <Picture 1> 标签：H3 拿不到参考图语义")

    if not style_declared(prompt, style):
        issues.append("未声明画面风格：全片画风可能被参考图带偏")
    if H3_NO_TEXT not in prompt:
        issues.append("缺少「严禁出现文字/字幕/水印」约束")
    return {"issues": issues, "fatal": fatal}


def _repair_h3(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    """H3 提示词只做「安全追加」类自愈。

    结构缺段（六段不全）**不在这里重建** —— 重建需要参考图语义（每张图的用途），
    只有生成端 ``comfyui_client.resolve_h3_prompt`` 掌握。这里只补无文字约束这种
    与上下文无关的固定句，其余交给生成端重建（见 verdict.rebuild_hint）。
    """
    repairs: List[str] = []
    out = prompt
    if out and H3_NO_TEXT not in out:
        out = out.rstrip() + "\n" + H3_NO_TEXT_FULL
        repairs.append("补写「严禁出现文字/字幕/水印」约束")
    return out, repairs


# --------------------------------------------------------------------------- #
# 资产参考图提示词检查
# --------------------------------------------------------------------------- #

def _check_asset(prompt: str, ctx, style, **_kw) -> Dict[str, Any]:
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空：该资产会退化为无参考信息的随机外观")
        return {"issues": issues, "fatal": fatal}

    if len(prompt) < _ASSET_MIN_LEN:
        issues.append(f"提示词过短（{len(prompt)} 字符）：不足以描述主体特征")

    if not style_declared(prompt, style):
        issues.append("未声明风格：资产画风会与全片其它资产不一致")

    # 风格双写：与尾帧检查共用同一份判定（见 _style_dup_token）
    dup = _style_dup_token(prompt, style)
    if dup:
        issues.append(f"风格被重复声明（「{dup}」出现 {prompt.count(dup)} 次）：会放大该风格权重")

    hits = _fluff_hits(prompt)
    if hits:
        issues.append("含质量类空词（无信息量且挤占风格语义）：" + "、".join(hits[:3]))
    return {"issues": issues, "fatal": fatal}


def _repair_asset(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    repairs: List[str] = []
    out = prompt
    for w in _QUALITY_FLUFF:
        if w.lower() in out.lower():
            out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
            repairs.append("移除质量类空词")
    out = _tidy(out)
    # with_style 内部先 _collapse_style（自愈双写）再按标记判幂等 → 一次同时修「缺失」与「双写」
    new = style_kit.with_style(out, style)
    if new and new != out:
        out = new
        repairs.append("规范化风格声明（补齐缺失 / 合并重复）")
    return out, repairs


# --------------------------------------------------------------------------- #
# 尾帧提示词检查
# --------------------------------------------------------------------------- #

def _fluff_hits(prompt: str) -> List[str]:
    """命中质量类空词（中英混排，大小写不敏感）"""
    low = (prompt or "").lower()
    return [w for w in _QUALITY_FLUFF if w.lower() in low]


def _style_dup_token(prompt: str, style) -> str:
    """返回「被重复声明 ≥2 次」的风格词，没有则返回 ""

    典型症状是「模型自写风格词 + 程序追加风格句」叠加，同一风格写两遍会放大其权重。
    """
    clause = _style_clause(style)
    if not clause:
        return ""
    for tok in clause.split("：", 1)[-1].split("，"):
        tok = tok.strip()
        if tok and prompt.count(tok) >= 2:
            return tok
    return ""


def _check_keyframe(prompt: str, ctx, style, **_kw) -> Dict[str, Any]:
    """尾帧提示词检查

    尾帧是「以本镜分镜图为首帧参考的图生图」，链式模式下还会直接成为**下一镜的首帧**
    —— 一张坏图会顺着链污染后面所有镜头，所以这一层是源头把关。
    """
    issues: List[str] = []
    fatal: List[str] = []

    if not prompt:
        fatal.append("提示词为空：尾帧会退化为随机画面，并顺着链污染后续镜头的首帧")
        return {"issues": issues, "fatal": fatal}

    if len(prompt) < _ASSET_MIN_LEN:
        issues.append(f"提示词过短（{len(prompt)} 字符）：不足以约束动作推进的终点")

    # ① 锚定参考图：不写这句，模型会换脸、换服装、换场景
    if KF_MARK_ANCHOR not in prompt:
        issues.append("未声明「保持参考图外观/服装/场景/画风不变」：尾帧极易换脸换服装")

    # ② 取景连贯：机位/景别一变，首尾帧之间就插不出连续动作
    if KF_MARK_FRAMING not in prompt:
        issues.append("未要求机位/焦段/构图与首帧连贯：尾帧可能切换视角")

    # ③ 链式承接：首帧取自上一镜尾帧时缺这句，模型会当成同镜的两个时间点而拒绝推进动作
    if isinstance(ctx, dict) and ctx.get("chained") and KF_MARK_CHAIN not in prompt:
        issues.append("链式尾帧未写明「承接上一镜」：模型会当成同镜两个时间点，动作推不动")

    if not style_declared(prompt, style):
        issues.append("未声明风格：尾帧画风会与分镜图跑偏")

    dup = _style_dup_token(prompt, style)
    if dup:
        issues.append(f"风格被重复声明（「{dup}」出现 {prompt.count(dup)} 次）：会放大该风格权重")

    if H3_NO_TEXT not in prompt:
        issues.append("未禁止画面出现文字：台词/字幕会被渲染进画面")

    hits = _fluff_hits(prompt)
    if hits:
        issues.append("含质量类空词（无信息量且挤占风格语义）：" + "、".join(hits[:3]))
    return {"issues": issues, "fatal": fatal}


def _repair_keyframe(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    """尾帧提示词自愈：只修文本层可确定的项

    ⚠️ 「锚定参考图」「取景连贯」这类**语义**缺失不在这里补：随手补一句通用措辞会掩盖
    真正的问题（该镜的动作推进根本没被描述清楚），这时应当重建而不是打补丁 —— 见
    ``_rebuild_hint`` 给出的重建器。
    """
    repairs: List[str] = []
    out = prompt
    for w in _QUALITY_FLUFF:
        if w.lower() in out.lower():
            out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
            repairs.append("移除质量类空词")
    out = _tidy(out)

    if not style_declared(out, style):
        new = style_kit.with_style(out, style)
        if new and new != out:
            out = new
            repairs.append("补写画面风格声明")

    if out and H3_NO_TEXT not in out:
        out = out.rstrip()
        sep = "" if out.endswith(("。", "！", "？", "；")) else "。"
        out = f"{out}{sep}{H3_NO_TEXT_FULL}"
        repairs.append("补写「严禁出现文字/字幕/水印」约束")
    return out, repairs


# --------------------------------------------------------------------------- #
# 配音台词检查
# --------------------------------------------------------------------------- #

def _audio_word_count(text: str) -> int:
    """有效字数（去掉空白、标点、符号后剩下「会发声」的字符数）"""
    return len(_AUDIO_WORD_RE.findall(str(text or "")))


def _audio_leak_hits(text: str) -> List[str]:
    """命中的结构化残留（返回人类可读的标签）"""
    return [label for label, rx in _AUDIO_LEAK_PATTERNS if rx.search(text or "")]


def _audio_stage_brackets(text: str) -> List[str]:
    """括号里的舞台指示片段（不是要念的台词）"""
    hits: List[str] = []
    for m in _AUDIO_BRACKET_RE.finditer(text or ""):
        inner = (m.group(1) or "").strip()
        low = inner.lower()
        if not inner:
            continue
        if any(h.lower() in low for h in _AUDIO_STAGE_HINTS):
            hits.append(inner)
    return hits


def _check_audio(prompt: str, ctx, style, **_kw) -> Dict[str, Any]:
    """配音台词检查

    ⚠️ 这一层判的不是「提示词写得好不好」，而是「这句台词拿去念会不会出问题」——
    台词会被 TTS 逐字念出来，所以多一个字符、少一个字都是实质缺陷。
    """
    issues: List[str] = []
    fatal: List[str] = []

    text = _txt(prompt)
    if not text:
        fatal.append("台词为空：该句会合成出静音片段（或直接被跳过），"
                     "成片对应镜头整段无声")
        return {"issues": issues, "fatal": fatal}

    words = _audio_word_count(text)
    if words == 0:
        fatal.append("台词只有标点/符号（无任何可发声字符）：TTS 会产出空音频或直接失败")
        return {"issues": issues, "fatal": fatal}

    if words < _AUDIO_MIN_WORDS:
        issues.append(f"有效字数仅 {words} 个：信息量过低，多数情况下是脏数据残留")

    leaks = _audio_leak_hits(text)
    if leaks:
        issues.append("含结构化残留（会被 TTS 逐字念出来）：" + "、".join(leaks))

    stage = _audio_stage_brackets(text)
    if stage:
        issues.append("含舞台指示/表演提示（会被念出来）：「" + "」「".join(stage[:3]) + "」")

    if _AUDIO_INVISIBLE_RE.search(text):
        issues.append("含零宽字符或方向控制符：肉眼不可见，但会污染文本与时长统计")

    if _AUDIO_EMOJI_RE.search(text):
        issues.append("含 emoji / 特殊符号：TTS 无法发声，可能被读成符号名称")

    if words > _AUDIO_MAX_CHARS:
        issues.append(f"单句过长（{words} 个有效字 > {_AUDIO_MAX_CHARS}）："
                      f"易被断句或触发合成超时，建议按语气拆句")

    # 情绪：剧本每镜都有 emotion，但只有 VoiceDesign 模式才真的按 instruct 控语气；
    # preset（CustomVoice）会把 instruct 当备注忽略 → 「情绪化配音」静默失效。
    if isinstance(ctx, dict):
        emotion = _txt(ctx.get("emotion"))
        mode = _txt(ctx.get("voice_mode")).lower()
        if emotion and emotion.lower() not in [e.lower() for e in _AUDIO_NEUTRAL_EMOTIONS] \
                and mode and mode != "design":
            issues.append(f"该镜情绪为「{emotion}」但音色模式是 {mode}："
                          f"instruct 会被忽略，配音听不出情绪")

        # 说话人兜底：剧本没写 speaker 时 build_dub_plan 会落到「旁白」，
        # 于是角色台词被旁白音色念出来 —— 音色错配，且前端看起来「有配音」。
        if ctx.get("speaker_fallback"):
            issues.append("该句未指定说话人，将使用「旁白」音色兜底："
                          "角色台词会被旁白念，音色错配")

    if not style_declared(text, style) and isinstance(ctx, dict) and ctx.get("require_style"):
        issues.append("未声明配音风格：多角色音色可能不统一")

    return {"issues": issues, "fatal": fatal}


def _repair_audio(prompt: str, ctx, style) -> Tuple[str, List[str]]:
    """配音台词自愈：只剥「确定不是台词内容」的噪音，绝不改写台词本身。

    ⚠️ 语义层缺陷（说话人缺失、情绪模式不对）**不在文本层修** —— 那需要改音色配置，
    属于调用方的职责；随手补一句会掩盖真正的问题（见 ``_rebuild_hint``）。
    """
    repairs: List[str] = []
    out = str(prompt or "")

    if _AUDIO_INVISIBLE_RE.search(out):
        out = _AUDIO_INVISIBLE_RE.sub("", out)
        repairs.append("移除零宽字符/方向控制符")

    if _AUDIO_EMOJI_RE.search(out):
        out = _AUDIO_EMOJI_RE.sub("", out)
        repairs.append("移除 emoji/特殊符号")

    if _audio_leak_hits(out):
        for _label, rx in _AUDIO_LEAK_PATTERNS:
            out = rx.sub("", out)
        repairs.append("剥离结构化残留（语言标签/镜头标签/时间码/说话人标记）")

    # 舞台指示：只剥「括号内含表演提示词」的括号，正常括号内容（如「（笑）」之外的
    # 专有名词）保持原样，避免误删台词本身
    stage = _audio_stage_brackets(out)
    if stage:
        out = _AUDIO_BRACKET_RE.sub(
            lambda m: "" if any(h.lower() in (m.group(1) or "").lower()
                                for h in _AUDIO_STAGE_HINTS) else m.group(0), out)
        repairs.append("剥离舞台指示/表演提示")

    out = _PUNCT_RUN.sub(lambda m: m.group(0)[0], out)
    out = _tidy(out)
    return out, repairs


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

_CHECKS = {
    "storyboard": _check_storyboard,
    "h3": _check_h3,
    "asset": _check_asset,
    "keyframe": _check_keyframe,
    "audio": _check_audio,
}
_REPAIRS = {
    "storyboard": _repair_storyboard,
    "h3": _repair_h3,
    "asset": _repair_asset,
    "keyframe": _repair_keyframe,
    "audio": _repair_audio,
}


def _score(issues: Sequence[str], fatal: Sequence[str]) -> int:
    s = 100 - 8 * len(issues) - 40 * len(fatal)
    return max(0, min(100, s))


def prompt_qc_ready(cfg) -> bool:
    """预检是否启用。

    ⚠️ 与图片/视频质检不同，这里**不依赖质检接口**：确定性检查零成本、零模型依赖，
    没配质检接口的项目也应该享受这一层保护（默认开启）。
    """
    return bool((cfg or {}).get("prompt_enabled", True))


def prompt_qc_mode(cfg) -> str:
    mode = str((cfg or {}).get("prompt_mode") or DEFAULT_MODE).strip().lower()
    return mode if mode in PROMPT_QC_MODES else DEFAULT_MODE


def check_prompt(kind: str, prompt: str, ctx: dict = None, style: str = "",
                 cfg: dict = None, ref_count: Optional[int] = None,
                 expect_refs: Optional[bool] = None) -> dict:
    """对一条提示词做确定性预检（不做自愈，不改动 prompt）。

    返回 verdict 与图片/视频质检**同构**：``ok / kind / prompt_kind / passed /
    accepted / blocked / score / issues / critical_issues / reason`` —— 便于复用
    已有的闸门、历史落盘与前端展示。
    """
    kind = str(kind or "").strip().lower()
    if kind not in _CHECKS:
        return {"ok": False, "kind": "prompt", "prompt_kind": kind,
                "passed": False, "accepted": False, "blocked": True, "score": 0,
                "issues": [f"未知的提示词类型：{kind}（支持 {'/'.join(PROMPT_KINDS)}）"],
                "critical_issues": [], "reason": "提示词类型不受支持", "skipped": False}

    text = _txt(prompt)
    style = _txt(style) or _txt((ctx or {}).get("style") if isinstance(ctx, dict) else "")
    res = _CHECKS[kind](text, ctx, style, ref_count=ref_count, expect_refs=expect_refs)
    fatal = list(res.get("fatal") or [])
    issues = list(res.get("issues") or [])
    passed = not fatal
    reason = ""
    if fatal:
        reason = "致命缺陷：" + "；".join(fatal[:2])
    elif issues:
        reason = "存在可优化项：" + "；".join(issues[:2])
    out = {
        "ok": True,
        "skipped": False,
        "kind": "prompt",
        "prompt_kind": kind,
        "passed": passed,
        "accepted": passed,
        "blocked": bool(fatal),
        "score": _score(issues, fatal),
        "issues": issues,
        # critical_issues 供 app.py 的 `_qc_gate` 复用（它会把这里的条目强制视为阻断项）
        "critical_issues": fatal,
        "reason": reason,
        "chars": len(text),
        "style": style,
    }
    out["rebuild_hint"] = _rebuild_hint(kind, out, text)
    return out


def repair_prompt(kind: str, prompt: str, ctx: dict = None, style: str = "") -> Tuple[str, List[str]]:
    """确定性自愈（幂等：对同一条提示词重复调用，第二次不再产生修改）。

    返回 ``(自愈后的提示词, 修复项说明)``。
    """
    kind = str(kind or "").strip().lower()
    fn = _REPAIRS.get(kind)
    if fn is None:
        return _txt(prompt), []
    style = _txt(style) or _txt((ctx or {}).get("style") if isinstance(ctx, dict) else "")
    try:
        return fn(_txt(prompt), ctx, style)
    except Exception as e:  # noqa: BLE001
        # 自愈失败不能让生成链断掉：原样返回 + 记录
        logger.warning("提示词自愈失败（%s）：%s", kind, e)
        return _txt(prompt), []


def _rebuild_hint(kind: str, verdict: dict, text: str = "") -> str:
    """结构性缺陷无法在文本层自愈时，告诉调用方该用哪个构建器重建。

    这类缺陷（缺景别骨架、缺参考图语义、H3 缺段、尾帧缺锚定语义）只能由掌握镜头上下文
    与参考图信息的生成端重建 —— 预检不越权去猜，只负责指出「该重建了」。

    ``text`` 是待判定的提示词正文（可选）。按**标记**判而不是按 issue 文案判，
    避免「文案一改、判定就悄悄失效」。
    """
    joined = " ".join([str(x) for x in (verdict.get("issues") or [])]
                      + [str(x) for x in (verdict.get("critical_issues") or [])])
    # 空提示词只能重建（文本层没有任何可修补的东西）
    if not verdict.get("chars"):
        if kind == "storyboard":
            return "comfyui_client.build_storyboard_prompt（用镜头上下文重建分镜图提示词）"
        if kind == "h3":
            return "comfyui_client.resolve_h3_prompt（用参考图语义重建 H3 提示词）"
        if kind == "keyframe":
            return "keyframe.build_end_frame_prompt（用镜头上下文重建尾帧提示词）"
        if kind == "audio":
            return ("从剧本该镜头重新取台词（dialogue_utils.normalize_lines → "
                    "shot.dialogue；本系统不产出旁白，台词是唯一人声来源）")
        return ""
    if kind == "storyboard":
        if any(m in joined for m in (SB_MARK_CONTENT, SB_MARK_FRAMING, SB_MARK_REF_USAGE)):
            return "comfyui_client.build_storyboard_prompt（用镜头上下文重建分镜图提示词）"
    if kind == "h3":
        if verdict.get("critical_issues") or "时间码" in joined:
            return "comfyui_client.resolve_h3_prompt（用参考图语义重建 H3 提示词）"
    if kind == "keyframe" and text:
        # 锚定参考图 / 取景连贯缺失 → 文本层补不出「这一镜的动作推进是什么」，只能重建
        if KF_MARK_ANCHOR not in text or KF_MARK_FRAMING not in text:
            return "keyframe.build_end_frame_prompt（用镜头上下文重建尾帧提示词）"
    if kind == "audio":
        # 空台词 / 只剩标点：文本层没有任何可修补的内容，只能从剧本重新取台词
        if not text or _audio_word_count(text) == 0:
            return ("从剧本该镜头重新取台词（dialogue_utils.normalize_lines → "
                    "shot.dialogue；本系统不产出旁白，台词是唯一人声来源）")
    return ""


def _crit_kind(msg) -> str:
    """致命缺陷的**类型键**：取到第一个中/英文左括号之前的部分。

    为什么不能按整串比：钳制只会让提示词**变短**，被剪掉的是末尾段落，于是同一条致命
    缺陷的**描述细节会变**（H3 的缺段列表由「non_diegetic_music」变成
    「overall_soundscape、non_diegetic_music」），整串相等就匹配不上，会把「原文本来就
    存在的致命缺陷」误判成「本次钳制新增」而降级掉 —— 真致命被洗白（2026-09-22 QA 复验
    P-1 的反例实测）。按类型键比对才能既降级「钳制新增的致命类型」，又保留「原文本就
    存在的致命类型」。
    """
    s = str(msg or "")
    for ch in ("（", "("):
        i = s.find(ch)
        if i >= 0:
            s = s[:i]
    return s.strip()


def preflight(kind: str, prompt: str, ctx: dict = None, style: str = "",
              cfg: dict = None, ref_count: Optional[int] = None,
              expect_refs: Optional[bool] = None) -> dict:
    """生成前预检 + 自愈（**生成链路的推荐入口**）。

    流程：检查 → （模式允许时）自愈 → 复检 → 按模式给放行结论。

    返回::

        {
          "prompt":   <应交给生成端的提示词，可能是自愈后的>,
          "before":   <自愈前的 verdict>,
          "verdict":  <自愈后的 verdict，语义以它为准>,
          "repairs":  [...],
          "accept":   bool,      # 是否放行生成
          "blocked":  bool,      # 是否命中致命缺陷
          "skipped":  bool,      # 预检未启用
          "label":    str,       # 便于日志/前端展示的一句话结论
          "reason":   str,
        }
    """
    cfg = cfg or {}
    text = _txt(prompt)
    mode = prompt_qc_mode(cfg)

    if not prompt_qc_ready(cfg):
        verdict = check_prompt(kind, text, ctx=ctx, style=style, cfg=cfg,
                               ref_count=ref_count, expect_refs=expect_refs)
        verdict["skipped"] = True
        return {"prompt": text, "before": verdict, "verdict": verdict, "repairs": [],
                "accept": True, "blocked": False, "skipped": True,
                "label": "提示词预检未启用（跳过）",
                "reason": "提示词预检未启用（跳过）"}

    before = check_prompt(kind, text, ctx=ctx, style=style, cfg=cfg,
                          ref_count=ref_count, expect_refs=expect_refs)
    repairs: List[str] = []
    final = text
    # ⚠️ 空提示词**绝不自愈**：补风格 / 补无文字约束会把空串变成一个「只有风格句、
    #    没有任何画面内容」的提示词，于是「提示词为空」这条致命缺陷在下一次检查里
    #    消失了 —— 等于用自愈把致命缺陷洗白，然后拿一条废提示词去出图。
    #    空就是空，交给调用方重建（见 rebuild_hint）。
    if mode in ("repair", "block") and text:
        final, repairs = repair_prompt(kind, text, ctx=ctx, style=style)
    # N1（2026-09-22 复验）：自愈后的提示词可能重新超过服务端上限，使 D-06 的「≤6000」
    # 不绝对成立。6000 是**服务端硬上限**，属安全闸门而非自愈策略，故在**启用预检**的
    # 路径上无条件钳制（warn 模式的语义是「不因质检缺陷阻断」，不是「允许超出上限」）；
    # 预检关闭时上面已 early-return 原样透传，保持「关闭时不改写」契约不变。
    _clamped = h3_prompt_kit.clamp_prompt(final)
    _prompt_clamped = _clamped != final
    if _prompt_clamped:
        repairs.append(f"提示词超长已截断（{len(final)} > "
                       f"{h3_prompt_kit.MAX_PROMPT_CHARS} 字符）")
        final = _clamped
    repairs = list(dict.fromkeys(repairs))   # 同类修复（如多次去空词）只报一次
    verdict = check_prompt(kind, final, ctx=ctx, style=style, cfg=cfg,
                           ref_count=ref_count, expect_refs=expect_refs)
    # 自愈的成效写进 verdict，便于前端/报告展示「修了什么」
    verdict["repairs"] = repairs
    # N1：暴露「本次是否因超长被钳制」，便于观测与测试（复检看到的是钳制后的文本）
    verdict["prompt_clamped"] = _prompt_clamped
    # N1 修正（2026-09-22 QA 复验 P-1）：截断是**客户端安全闸门**，绝不能自己造出一条
    # 原来不存在的致命缺陷。H3 的 validate 按顺序查段名，从末尾剪掉超长内容会连带剪掉
    # overall_soundscape / non_diegetic_music → 报「结构不合规（缺段）」→ blocked=True，
    # 把一条**结构本来完整**的提示词判废（服务端本来只是静默截断、照样出片）。
    # 判据：只降级「本次钳制**新增**」的致命缺陷；`before`（原文本）已有的致命缺陷照旧阻断。
    # ⚠️ 必须按**类型键**（见 _crit_kind）而不是整串比对 —— 钳制会改变同一条致命缺陷的描述
    #    （缺段列表变长），整串比对会把「原文本来就缺段」误判成「钳制新增」而洗白真致命。
    # 降级不等于隐藏：同时进 issues（可见）与 clamp_induced_critical（可分型统计）。
    # ⚠️ 注意 `verdict["score"]` / `verdict["passed"]` 仍是含致命惩罚的旧值：`block` 严格模式
    #    依旧会拦（这符合严格模式语义），`repair`（默认）模式不再误阻断。
    if _prompt_clamped:
        _pre_kinds = {_crit_kind(c) for c in (before.get("critical_issues") or [])}
        _after_crit = list(verdict.get("critical_issues") or [])
        _induced = [c for c in _after_crit if _crit_kind(c) not in _pre_kinds]
        _kept = [c for c in _after_crit if _crit_kind(c) in _pre_kinds]
        if _induced:
            verdict["clamp_induced_critical"] = _induced
            verdict["critical_issues"] = _kept
            verdict["issues"] = list(verdict.get("issues") or []) + [
                f"（截断致）{c}" for c in _induced]
            logger.warning("提示词预检[%s] 钳制新增致命缺陷已降级为非阻断（%d 项）：%s",
                           kind, len(_induced), "；".join(_induced)[:200])
    verdict["before_issues"] = list(before.get("issues") or [])
    verdict["before_score"] = before.get("score")
    verdict["mode"] = mode
    verdict["rebuild_hint"] = _rebuild_hint(kind, verdict, final)

    blocked = bool(verdict.get("critical_issues"))
    if mode == "warn":
        accept = True
    elif mode == "block":
        accept = bool(verdict.get("passed")) and not (verdict.get("issues") or [])
    else:  # repair
        accept = not blocked

    if blocked:
        label = "提示词致命缺陷"
    elif accept:
        label = "提示词达标" + (f"（自愈 {len(repairs)} 项）" if repairs else "")
    else:
        label = "提示词不达标"
    if mode == "warn" and (blocked or verdict.get("issues")):
        label = "提示词有问题（warn 模式不阻断）"
    # reason 必须与 accept 结论一致，不能出现「已放行」却只列缺陷的割裂表述
    if blocked:
        reason = "致命缺陷：" + "；".join(verdict.get("critical_issues") or [])[:200]
    elif accept and repairs:
        reason = f"已自愈 {len(repairs)} 项：" + "；".join(repairs[:2])
        if verdict.get("issues"):
            reason += f"；另有 {len(verdict['issues'])} 项非致命提示（不阻断）"
    elif accept and verdict.get("issues"):
        rest = "；".join(verdict["issues"][:2])
        reason = f"按当前模式放行，但仍有 {len(verdict['issues'])} 项提示：{rest}"
    else:
        reason = verdict.get("reason") or "无问题"
    return {"prompt": final, "before": before, "verdict": verdict, "repairs": repairs,
            "accept": accept, "blocked": blocked, "skipped": False,
            "label": label, "reason": reason,
            "rebuild_hint": verdict.get("rebuild_hint") or ""}


def prompt_qc_gate(pf: dict, cfg: dict = None) -> dict:
    """把 preflight 结果转成统一闸门结构 ``{accept, blocked, skipped, label, reason}``

    与 ``app._qc_gate`` 字段一致，便于两条链路共用同一套编排代码。
    """
    pf = pf or {}
    if pf.get("skipped"):
        return {"accept": True, "blocked": False, "skipped": True,
                "label": pf.get("label") or "提示词预检未启用（跳过）",
                "reason": pf.get("reason") or "", "critical_issues": []}
    v = pf.get("verdict") or {}
    return {"accept": bool(pf.get("accept")), "blocked": bool(pf.get("blocked")),
            "skipped": False, "label": pf.get("label") or "",
            "reason": pf.get("reason") or "",
            "critical_issues": list(v.get("critical_issues") or []),
            "repairs": list(pf.get("repairs") or []),
            "mode": v.get("mode") or prompt_qc_mode(cfg or {})}
