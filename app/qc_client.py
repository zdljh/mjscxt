"""
AI 质检模块（图片 / 视频）—— 可开关、可配置、不达标自动重生成

职责：
1) 质检配置持久化（qc_config.json）：总开关 / 图片·视频独立开关 / 质检独立接口（base_url / api_key / model，
   不复用文本分析 LLM 接口）/
   判定标准（合格线分数 + 自定义质检提示词）/ 最大重试次数 / 视频抽帧策略
2) 图片质检：把生成图交给「OpenAI 兼容 + 支持视觉输入」的多模态模型，返回
   {passed, score, reason, issues}
3) 视频质检：先用 ffmpeg 抽帧（首/中/尾等），再把多帧一并交给多模态模型判定
4) 质检与重试历史：output/qc/<项目>/<类型>_<镜头>.json（每次尝试一条记录，持续追加）

设计约束（重要）：
- 任何质检失败（未开启 / 未配置 / 网络异常 / 返回非法 JSON / ffmpeg 缺失）都不得抛异常打断生成流程，
  统一通过返回值里的 ok / skipped / error 字段表达。
- api_key 永不回显明文，只返回脱敏值。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

# 音频客观层（ffmpeg 指标 + 频谱/波形渲染）。⚠️ audio_qc **不反向依赖本模块**，
# 因此这里 import 不会形成循环；硬阈值与渲染逻辑都由它持有（谁消费谁定义）。
import audio_qc

logger = logging.getLogger(__name__)

# 项目根目录（定位加密密钥库 output/secrets.enc 与主密钥 .secret_key）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# ===================== 默认配置 =====================

# 质检判定口径：水印 / 角标 / 字幕 / 「AI 生成」标识一律不计为质量缺陷（B 项⑥）
# 该口径会追加到「默认提示词」与「用户自定义提示词」之后，保证历史配置同样生效。
WATERMARK_EXEMPT_NOTE = (
    "\n【判定口径·重要】画面中若仅存在水印、角标、logo、平台标识、字幕或「AI 生成」标识，"
    "一律不得视为质量缺陷，不得因此扣分，也不得据此判定不通过；"
    "请只针对画面本身的崩坏 / 畸变 / 糊化 / 闪烁 / 撕裂 / 一致性问题做判定。"
)

# 景别（镜头类型）判定容差：图像生成模型对取景范围的控制力有限，实测 43% 的分镜图
# 因「景别不符」被判不通过 —— 其中绝大多数只差一档（如目标中景、实际给了近景或全景），
# 反复重试仍难命中，白白烧 GPU。这里明确容差口径，避免把「差一档」当成硬缺陷。
# 与 SHOT_CAMERA_SPECS（comfyui_client）配套：判定标准由镜头信息里的「判定标准：…」给出。
FRAMING_TOLERANCE_NOTE = (
    "\n【景别判定口径·重要】判定景别时**必须以上方「镜头信息」里给出的「判定标准」为准**，"
    "不要用你自己的习惯理解。容差口径：\n"
    "  · 与判定标准相符，或仅相差一档（如目标中景，实际偏近景、或略偏全景）→ "
    "至多在 issues 里记一条轻微偏差，**不得据此判不通过**，score 只做小幅扣分（不超过 8 分）；\n"
    "  · 相差两档及以上（如目标特写却给全景/中景，或目标全景却给特写）→ 才算景别不符，"
    "写明「景别不符」并在 score 上明显扣分；\n"
    "  · 若「判定标准」写明**本镜未指定景别**（camera 只给了机位/运镜，如「俯拍缓推」）："
    "**不得判「景别不符」**，请只按画面内容描述里的取景要求判定；\n"
    "  · 机位与景别正交：镜头信息里给了「机位：俯拍 / 仰拍 / 平视 / 环绕 …」时，画面必须与之一致，"
    "机位明显不符（如要求俯拍却给了平视）按**两档级**偏差处理，写明「机位不符」并明显扣分；\n"
    "  · 取景范围本身不影响主体与动作的清晰表达时，宁可放过也不要误杀。"
)

# 关键缺陷硬规则（P0：收紧放行）：模型偶尔给有明显崩坏的图打高分/放行，
# 因此在提示词里下达硬性规则，并在代码侧再加一道不可绕过的闸门（见 _finalize_verdict）。
CRITICAL_RULE_NOTE = (
    "\n【硬性判定规则·最高优先级】若画面存在下列任一关键缺陷：人物畸变/崩坏/五官错位、"
    "多手多脚/缺肢/手指异常、肢体错位或断裂、画面撕裂/闪烁/重影、严重糊化或大色块、"
    "黑屏/白屏、主体缺失或变形、与镜头描述明显不符 —— 必须输出 pass=false 且 score ≤ 50，"
    "并在 issues 中逐条写明。**禁止在存在上述缺陷时给出通过结论或高分**；"
    "宁可严格也不得放过有明显缺陷的画面。"
)

# 风格一致性硬规则：追加到所有图片/视频质检提示词末尾，保证用户自定义的旧配置
# 也同样具备风格检测能力（与 WATERMARK_EXEMPT_NOTE / CRITICAL_RULE_NOTE 同一机制）。
# {style} 由 check_image / check_video 在运行时替换为目标风格串；无风格时整段不追加。
STYLE_CHECK_NOTE = (
    "\n【风格一致性判定·重要】若上方「目标风格」非空，请务必额外检查画面整体画风/"
    "渲染方式/笔触/配色是否与「{style}」一致；若明显偏离（例如目标为国漫偏写实，"
    "却出现写实照片、真人摄影、3D 写实渲染、卡通赛璐璐等），必须在 issues 中写明"
    "「风格不符/画风不符」并输出 style_match=false。"
)

# 设定一致性核对（分镜质检带「本镜出现的角色 / 物品 / 场景」的设定图）：
# 分镜图是**按参考图生成**的，但旧实现只把成品图单独送检 —— 模型手里没有锚点，
# 「这个角色是不是长成了设定里的样子」只能靠它自己想象，实测「角色不像设定 /
# 道具走形」这类问题要么被放过、要么被误判。这里把生成时用的参考图一并送检，
# 让判定有据可依。与 WATERMARK_EXEMPT_NOTE / CRITICAL_RULE_NOTE 同一「运行时追加」
# 机制：用户自定义的旧 image_prompt 也自动获得该能力。
MAX_REF_IMAGES = 4     # 质检最多附带几张设定参考图（去重后）
_REF_LABEL_PREFIX_RE = re.compile(r"^参考图\s*\d+\s*是")


def build_ref_consistency_note(refs: list) -> str:
    """生成「设定一致性核对」段落。

    refs 为 [(label, path), ...]，label 形如「角色「方源」的外貌、服装与发型」。
    ⚠️ 第 1 张图固定是待检成品图，因此设定图从「第 2 张」开始编号
    （生成侧 label 里的「参考图N」是相对参考图自身的编号，必须剥掉，否则模型会数错图）。
    """
    items = []
    for i, (label, _p) in enumerate(refs or [], start=2):
        lab = _REF_LABEL_PREFIX_RE.sub("", str(label or "").strip()).strip()
        items.append("  第 %d 张 = 设定图：%s" % (i, lab or "（未标注）"))
    if not items:
        return ""
    return (
        "\n【设定一致性核对·重要】本次按顺序传入 %d 张图：\n"
        "  第 1 张 = 待检的分镜图（AI 生成结果）；\n%s\n"
        "请**逐张**核对第 1 张里出现的角色 / 物品 / 场景是否与对应设定图一致：\n"
        "  · 角色：脸型 / 五官 / 发型 / 发色 / 服装款式与配色 / 配饰 必须与设定一致；"
        "出现「换脸 / 换装 / 发色不符 / 配饰丢失 / 年龄或气质明显不同」都算设定不符；\n"
        "  · 物品：形状 / 材质 / 配色 / 纹样 必须与设定一致；"
        "出现「形状走样 / 配色错误 / 纹样改变 / 材质感突变」都算设定不符；\n"
        "  · 场景：主要结构与氛围一致即可，**机位与构图允许不同**，不得据此扣分；\n"
        "  · 设定图是「三视图 / 多视图」时，分镜图与其中**任一视图**一致即算一致；\n"
        "  · 参考图对应的角色 / 物品**没有出现在画面里**时（例如本镜只有场景），"
        "不得因「画面里找不到」而扣分。\n"
        "⚠️ 只要存在角色或物品**明显变形 / 走样 / 与设定不符**，必须在 issues 里逐条写明"
        "（以「角色变形：…」或「物品变形：…」开头），并按上方硬性规则输出 pass=false 且 score ≤ 50。"
        % (len(items) + 1, "\n".join(items))
    )


# 代码侧关键缺陷词表（命中即阻断，与模型分数无关）
# 注意：不含「水印/字幕/logo/文字」——这些按 WATERMARK_EXEMPT_NOTE 不计缺陷。
CRITICAL_ISSUE_KEYWORDS = (
    "畸变", "扭曲", "崩坏", "崩了", "脸部崩", "面部崩", "五官错位", "五官畸变", "五官错误",
    "多手", "多脚", "多头", "缺手", "缺脚", "断肢", "肢体错位", "肢体断裂", "手指异常", "六指",
    "融合", "粘连", "穿模", "黑屏", "白屏", "纯色块", "大色块", "严重糊", "糊化", "失焦",
    "撕裂", "闪烁", "重影", "鬼影", "变形", "比例失调", "主体缺失", "主体不完整", "残影",
    "拼接痕迹", "画面崩", "错位",
    # P0 独立复核补充（_qc_gate 侧会独立比对，命中即强制阻断，与模型 accepted 无关）：
    # 覆盖「画面崩坏 / 拼接 / 人物重复」三类高频漏检表述（更宽的表达形态）。
    "拼接", "画面异常", "人物重复", "重复人物", "人物多出", "多出人物", "多余人物",
    "人物多余", "人物重叠", "人物数量异常", "多个人物", "分身",
)

# 音频关键缺陷词表（供 check_audio 的 AI 层结论做代码侧硬闸）。
# ⚠️ 不能复用上面那张图片词表：音频缺陷与画面缺陷没有交集，拿「畸变/多手」去匹配
#    音频结论等于永远不阻断；反过来把「杂音」加进图片词表又会让图片质检误杀。
AUDIO_CRITICAL_KEYWORDS = (
    "无声", "静音", "没声音", "没有声音", "听不到", "空白", "空音",
    "爆音", "爆裂", "削波", "削顶", "失真", "过载", "破音",
    "杂音", "噪声过大", "电流声", "底噪", "嘶嘶",
    "断续", "断句异常", "跳音", "卡顿", "丢帧", "损坏", "无法播放",
    "忽大忽小", "音量异常", "忽高忽低",
    "漏配", "漏句", "缺少人声", "人声缺失", "不是人声", "非人声",
)


#: 否定语境词（出现在关键词前若干字符内，说明该条是在**说明不存在缺陷**）
_NEGATION_WORDS = ("无", "没有", "未", "不存在", "非", "不", "缺少", "未发现", "已消除")


def _has_negative_context(text: str, idx: int, window: int = 6) -> bool:
    """判断 ``text[idx]`` 处的关键词是否处于否定语境（如「无明显畸变」）

    只看关键词**前面** window 个字符，避免把「风格不符」这类真缺陷误当否定。
    """
    ctx = str(text or "")[max(0, idx - window):idx]
    return any(neg in ctx for neg in _NEGATION_WORDS)


def find_critical_issues(issues, keywords=None) -> list:
    """从 issues 文本中筛出命中关键缺陷词的条目

    带否定语境过滤：命中词前 6 字内出现「无/没有/未/不存在/非/不」的（如"无明显畸变"）
    不视为关键缺陷，避免把「说明没有缺陷」的条目误判成阻断项。

    ``keywords`` 可换成音频词表（``AUDIO_CRITICAL_KEYWORDS``）；默认走图片/视频词表。
    ⚠️ 必须可换：音频结论里根本不会出现「畸变/多手」，共用一张词表等于音频永不阻断。
    """
    table = tuple(keywords) if keywords else CRITICAL_ISSUE_KEYWORDS
    hits: list = []
    for it in (issues or []):
        s = str(it)
        for k in table:
            found = False
            start = 0
            while True:
                idx = s.find(k, start)
                if idx == -1:
                    break
                if not _has_negative_context(s, idx):
                    found = True
                    break
                start = idx + len(k)
            if found:
                hits.append(s[:200])
                break
    return hits


# 风格达标检测：用户与总控敲定的风格串（如「国漫风格，偏写实」）在成图/成片上
# 是否被落实。此前质检只查「畸变/糊化/一致性」，完全不管风格，导致风格跑偏也不会
# 触发「不达标 → 改提示词重生成」。这里把风格也纳入质检判定维度。
# 注意：风格不达标与「画面崩坏」是两类缺陷，前者只强制 passed=False（触发重试），
# 后者（find_critical_issues）额外打 blocked=True（关键缺陷阻断）。

# 「风格/画风」后接的否定词，覆盖「画风与目标不符」「风格不一致」等变体表述。
_STYLE_NEG_WORDS = ("不符", "不一致", "不匹配", "不对", "偏离", "跑偏", "错误", "缺失", "不统一")

# 独立的「风格错误」信号词（不需「风格/画风」前缀，直接命中；
# 多为「目标风格被替换成别的风格」的表述）。
STYLE_ISSUE_KEYWORDS = (
    "写实照片", "真人摄影", "3D写实", "写实渲染", "卡通渲染", "赛璐璐",
    "不是国漫", "不像国漫", "非国漫", "国漫风格缺失", "缺少国漫",
)


def find_style_issues(issues, style=None) -> list:
    """从 issues 文本中筛出「风格/画风不达标」类条目。

    匹配两路（任一命中即判定风格缺陷）：
    1) 独立风格错误词（如「写实照片」「3D写实」，说明目标风格被替换）；
    2) 「风格/画风」+ 否定词的组合（覆盖「画风与目标不符」「风格不一致」等变体）。

    ⚠️ 第 2 路必须带**否定语境过滤**：否则「风格统一，无缺失」「画风稳定，没有偏差」
    这类**肯定表述**会因为命中 _STYLE_NEG_WORDS 里的「缺失 / 偏离」而被误判成风格缺陷，
    进而强制 passed=False 触发无意义的重生成（实测踩过）。
    """
    hits: list = []
    for it in (issues or []):
        s = str(it)
        if any(k in s for k in STYLE_ISSUE_KEYWORDS):
            hits.append(s[:200])
            continue
        if "风格" in s or "画风" in s:
            bad = False
            for neg in _STYLE_NEG_WORDS:
                start = 0
                while True:
                    idx = s.find(neg, start)
                    if idx == -1:
                        break
                    if not _has_negative_context(s, idx):
                        bad = True
                        break
                    start = idx + len(neg)
                if bad:
                    break
            if bad:
                hits.append(s[:200])
    return hits


def _apply_style_gate(verdict: dict, style) -> dict:
    """在质检结论上叠加「风格达标」判定（幂等，永不抛异常）。

    判定来源（任一命中即判定风格不达标，强制 passed=False）：
    1) 模型显式返回 style_match=false（prompt 里要求输出的字段）；
    2) issues 命中 STYLE_ISSUE_KEYWORDS（代码侧兜底，不依赖模型自觉）。

    结果写入 verdict["style_mismatch"] / verdict["style_issues"]，供入库闸门与
    教训库识别「风格缺陷」以触发改写提示词重生成。
    """
    verdict = verdict or {}
    if not style or not verdict.get("ok"):
        return verdict
    issues = list(verdict.get("issues") or [])
    sm = verdict.get("style_match")
    style_hits = find_style_issues(issues, style)
    mismatch = bool(sm is False) or bool(style_hits)
    verdict["style_mismatch"] = mismatch
    verdict["style_issues"] = style_hits
    if mismatch:
        verdict["passed"] = False
        if style_hits and not verdict.get("reason"):
            verdict["reason"] = "画面风格与目标风格不符：" + "；".join(style_hits[:2])
    return verdict


def _finalize_verdict(verdict: dict, pass_score: int) -> dict:
    """代码侧硬闸：单向收紧质检结论（只会把「通过」改成「不通过」，绝不反向放行）

    1) issues 命中关键缺陷 → passed=False、blocked=True、score 压到 ≤50；
    2) score < pass_score 时，即使模型给 pass=true 也强制 False；
    3) 补 accepted（= passed，供入库判断）与 blocked（关键缺陷阻断）字段。
    """
    issues = verdict.get("issues") or []
    hits = find_critical_issues(issues)
    score = verdict.get("score")
    blocked = False
    if hits:
        blocked = True
        verdict["passed"] = False
        if isinstance(score, int):
            verdict["score"] = min(score, 50)
    elif isinstance(score, int) and score < int(pass_score):
        verdict["passed"] = False
    verdict["critical_issues"] = hits
    verdict["blocked"] = blocked
    verdict["accepted"] = bool(verdict.get("passed"))
    if blocked:
        logger.warning(f"质检关键缺陷阻断（不通过）: {hits}")
    return verdict

DEFAULT_IMAGE_PROMPT = (
    "你是漫剧分镜图片质检员。请检查这张 AI 生成的分镜图是否达到可直接使用的标准：\n"
    "1) 人物：脸型/发型/服装/配饰与设定一致，无五官畸变、多手多脚、肢体错位；\n"
    "2) 画面：无严重糊化、噪点、色块、扭曲；\n"
    "3) 构图：主体完整清晰，场景与镜头描述相符；\n"
    "4) 景别：取景范围是否落在「镜头信息」给出的判定标准内（判定口径见下方容差说明，"
    "差一档不算缺陷）；\n"
    "5) 风格：画面整体画风/渲染方式/笔触/配色必须与目标风格一致，不得偏离；\n"
    "6) 水印/角标/logo/字幕即使存在，也不计入缺陷、不扣分。\n"
    "目标风格：{style}\n"
    "镜头信息：{shot_desc}\n"
    "请只输出一个 JSON 对象，不要任何解释文字，格式：\n"
    '{"score": 0-100 的整数, "pass": true 或 false, "style_match": true 或 false, '
    '"reason": "一句话结论", "issues": ["具体问题1", "具体问题2"]}'
)

DEFAULT_AUDIO_PROMPT = (
    "你是漫剧配音质检员。下面两张图不是画面，而是同一段配音音频的**频谱图**与**波形图**：\n"
    "  第 1 张：频谱图（横轴时间，纵轴频率，颜色亮度=能量强弱）\n"
    "  第 2 张：波形图（横轴时间，纵轴振幅）\n"
    "请据此判断这段配音是否达到可直接使用的标准：\n"
    "1) 人声能量分布是否正常（人声主要能量集中在数百 Hz 至数千 Hz 的中频段）；\n"
    "2) 波形是否存在长时间「平坦零线」（说明整段无声、漏配音或合成失败）；\n"
    "3) 波形上下是否被削成平直横线（说明增益过大导致爆音失真）；\n"
    "4) 是否存在异常高频噪声、周期性爆破或明显断续（说明音频损坏或拼接异常）。\n"
    "配音文本参考：{line_text}\n"
    "请只输出一个 JSON 对象，不要任何解释文字，格式：\n"
    '{"score": 0-100 的整数, "pass": true 或 false, "reason": "一句话结论", "issues": ["具体问题1", "具体问题2"]}'
)

DEFAULT_VIDEO_PROMPT = (
    "你是漫剧视频质检员。下面按顺序给出同一镜头视频的若干抽帧图片（首帧/中间帧/尾帧）。\n"
    "请判断该视频片段是否达到可直接使用的标准：\n"
    "1) 画面：无严重闪烁、撕裂、糊化、崩坏或色块；\n"
    "2) 一致性：人物外观与场景在整段视频中保持稳定，无明显畸变；\n"
    "3) 内容：与镜头描述相符，主体清晰；\n"
    "4) 风格：画面整体画风/渲染方式/笔触/配色必须与目标风格一致，不得偏离；\n"
    "5) 水印/角标/logo/字幕即使存在，也不计入缺陷、不扣分。\n"
    "目标风格：{style}\n"
    "镜头信息：{shot_desc}\n"
    "请只输出一个 JSON 对象，不要任何解释文字，格式：\n"
    '{"score": 0-100 的整数, "pass": true 或 false, "style_match": true 或 false, '
    '"reason": "一句话结论", "issues": ["具体问题1", "具体问题2"]}'
)

# ===================== 剧本质检提示词 =====================
# ⚠️ 字段名必须与 novel_to_script 的真实产物一致，否则模型会报「字段缺失」假问题。
# 历史缺陷：本提示词检查 `visual_description`（镜头级）与角色级 `description` ——
# **这两个字段都不存在**（真实字段是 shot.description / character.appearance），
# 且完全没有检查 items / scenes 的结构，模型据此报出一堆不存在的缺陷。
# 真实 schema（见 output/scripts/<项目>/第N集.json）：
#   script   : title / episode_no / episode_title / theme / style / characters /
#              items / scenes / shots / production_notes / metadata
#   shot     : shot_id / duration / camera / location / description / visual_detail /
#              dialogue[{speaker,text}] / emotion / audio_cues / characters_in_shot /
#              items_in_shot / prompt_h3 / style
#              （narration 是 2026-09-19 之前的旧字段，本系统已不产出旁白）
#   character: name / age / identity / appearance / current_outfit / personality /
#              voice_style / reference_prompt_zh / reference_prompt_en
#   item     : name / category / appearance / owner / importance /
#              reference_prompt_zh / reference_prompt_en
#   scene    : name / location / appearance / reference_prompt_zh / reference_prompt_en
DEFAULT_SCRIPT_PROMPT = (
    "你是漫剧剧本质检员。请检查这个 JSON 剧本是否达到可直接进入生产的标准。\n"
    "**只按下面列出的真实字段名判定，不要凭空要求其它字段名**（例如本剧本文档里"
    "镜头画面描述就叫 description，不叫 visual_description）。\n\n"
    "【结构完整性】\n"
    "1. 顶层必须包含：title, style, characters, items, scenes, shots\n"
    "2. 每个角色必须有：name, appearance, personality"
    "（current_outfit / voice_style / identity / age 为可选增强项）\n"
    "3. 每个物品必须有：name, category, appearance（owner / importance 可选）\n"
    "4. 每个场景必须有：name, location, appearance\n"
    "5. 每个镜头必须有：shot_id, duration, camera, location, description, "
    "characters_in_shot, items_in_shot\n\n"
    "【逻辑一致性】\n"
    "6. characters_in_shot 中的角色必须在 characters 列表中有定义\n"
    "7. items_in_shot 中的物品必须在 items 列表中有定义\n"
    "8. 镜头的 location 应能在 scenes 列表中找到对应场景\n"
    "9. 镜头顺序应有清晰的叙事逻辑，shot_id 连续\n\n"
    "【风格一致性】\n"
    "10. 画面描述与整体气质必须符合指定创作风格（{style}）\n"
    "11. 角色外观描述应与该风格匹配\n\n"
    "【提示词质量】\n"
    "12. description 应足够具体（建议 50 字以上），包含人物动作、环境光线与构图要素；\n"
    "13. camera 应为「景别+运镜」写法（如 中景跟拍 / 特写推入）\n\n"
    "【可执行性评估】\n"
    "14. 总时长应接近目标时长（{target_duration} 秒）\n"
    "15. 每个镜头时长应在 3-12 秒范围内\n"
    "16. 本系统不产出旁白：镜头没有台词是**允许**的（纯画面镜/空镜），"
    "只要该镜的 audio_cues 写了音效或配乐提示即算合格；"
    "但如果某镜既没有台词、又没写 audio_cues，成片到该镜会既无人声也无音效，判为问题。\n"
    "17. 单个镜头的台词合计不宜超过 30 字（约 6.7 秒配音）：台词过多会溢出到后面几镜，"
    "成片尾部被截断，应拆成更多镜头\n\n"
    "剧本数据：\n{script_data}\n\n"
    "请只输出一个JSON对象，格式：\n"
    '{\"score\": 0-100, \"pass\": true/false, \"reason\": \"一句话结论\", '
    '\"issues\": [\"问题1\", \"问题2\"], \"suggestions\": [\"建议1\", \"建议2\"], '
    '\"categories\": {\"structure\": 0-100, \"logic\": 0-100, \"style\": 0-100, '
    '\"prompt_quality\": 0-100, \"feasibility\": 0-100}}'
)

# ===================== 音频质检 =====================
# 音频无法像图片那样直接交给视觉模型「听」，因此采用两层判定：
#   客观层（`audio_qc.py`，ffmpeg 指标，零模型依赖，**始终执行**）负责硬闸：
#     整段无声 / 近乎无声 / 空文件 / 不含音频流；
#   AI 层（`check_audio`：把音频渲染成频谱图+波形图再送多模态模型）负责内容层判读。
#
# ⚠️ 2026-09-19 之前的状况：`audio_enabled` / `audio_prompt` / `audio_min_*` 等配置键、
#    `DEFAULT_AUDIO_PROMPT`、以及六个 AUDIO_* 阈值常量**全部零消费** —— 配置写好了、
#    提示词写好了、阈值写好了，但没有任何代码读它们，`audio_qc_ready` 也不存在，
#    前端「音频质检」页写着「功能正在开发中」。用户打开开关、调阈值什么都不会发生。
#    硬阈值现已**迁到实际消费它们的 `audio_qc.py`**（单一事实源：谁用谁定义），
#    可调阈值仍留在本配置文件里，由用户按音色与语速整体调档。

CONFIG_KEYS = (
    "enabled", "image_enabled", "video_enabled", "audio_enabled", "script_enabled",
    "base_url", "api_key", "model",
    "endpoint_override",   # 被其它模块（如分镜链路）自动写入的接口，记录以便「恢复为 AI 设置」
    "image_prompt", "video_prompt", "audio_prompt", "script_prompt",
    "pass_score", "max_retries", "video_frame_count",
    "image_max_side", "timeout", "api_retries", "api_backoff", "updated_at",
    "script_categories",  # 剧本质检各维度权重和合格线
    # 推理模型控制（2026-09-17 新增，确保配置能正确落盘）
    "disable_thinking",
    "min_tokens_when_thinking",
    # 音频阈值（此前未在白名单，导致配置丢失）
    "audio_min_speech_ratio", "audio_min_mean_db", "audio_max_drift",
    # 尾帧质检开关
    "keyframe_qc_enabled",
    # 图片质检是否附带「本镜出现的角色/物品/场景」设定图做一致性核对（2026-09-20 新增）
    "image_ref_compare",
    # 提示词预检（生成前质检，见 prompt_qc.py）。⚠️ 它不依赖质检接口，默认开启
    "prompt_enabled", "prompt_mode",
)


def _empty_config() -> dict:
    return {
        "enabled": False,            # 质检总开关
        "image_enabled": True,       # 图片质检开关
        "video_enabled": True,       # 视频质检开关
        "audio_enabled": True,       # 音频质检开关（客观层零模型依赖；AI 层复用质检接口）
        "script_enabled": True,      # 剧本质检开关
        # 质检必须使用自己独立配置的 base_url / api_key / model（不再复用文本分析 LLM 接口）
        "base_url": "",
        "api_key": "",
        "model": "",
        "endpoint_override": {"base_url": "", "api_key": "", "model": ""},
        "image_prompt": DEFAULT_IMAGE_PROMPT,
        "video_prompt": DEFAULT_VIDEO_PROMPT,
        "audio_prompt": DEFAULT_AUDIO_PROMPT,
        "script_prompt": DEFAULT_SCRIPT_PROMPT,
        # 音频客观层阈值（可按音色/语速调档）
        "audio_min_speech_ratio": 0.50,   # 有声占比下限（低于此值扣分）
        "audio_min_mean_db": -45.0,       # 平均电平下限（低于此值扣分）
        "audio_max_drift": 0.50,          # 与预期时长偏差上限（比例，超限扣分）
        "pass_score": 70,            # 合格线（0-100），score >= pass_score 且 pass != false 视为达标
        "max_retries": 2,            # 不达标最大重试次数
        "video_frame_count": 3,      # 视频抽帧数量（1-6）
        "image_max_side": 1024,      # 送检前压缩的最长边（控制 token 与耗时）
        # 图片质检是否附带「本镜出现的角色/物品/场景」的设定图：
        # 分镜图是按参考图生成的，只送成品图的话模型没有锚点，「角色不像设定/道具变形」
        # 这类问题只能靠猜。开启后按 shot.characters_in_shot / items_in_shot 顺序附带
        # 最多 MAX_REF_IMAGES 张设定图，并要求逐张核对是否变形、与设定是否一致。
        "image_ref_compare": True,
        "timeout": 180,              # 单次质检请求读超时（秒）
        "api_retries": API_RETRY_ATTEMPTS,   # 网络层额外重试次数（瞬时故障时退避重试，与 max_retries 重画无关）
        "api_backoff": API_RETRY_BACKOFF,    # 网络重试退避基数（秒），按 2 的幂增长、单次上限见 API_RETRY_MAX_SLEEP
        # ⚠️ 推理型模型（如 agnes-2.5-flash、R1 系）会把 token 花在 reasoning_content 上，
        # 额度给小时正文 content 直接为 ""，质检就永远「返回内容为空」。
        # 2026-09-17 起**默认允许思考**（关思考会让质检退化成直觉判断、漏掉明显问题），
        # 改用「token 下限 + 空正文自动加码重试」兜底。确需关掉的模块把这里设为 true。
        "disable_thinking": False,
        # 允许思考时质检请求的最小 max_tokens（思考本身就要吃几百 token）
        "min_tokens_when_thinking": 1024,
        # 剧本质检各维度权重和合格线
        "script_categories": {
            "structure": {"weight": 0.2, "pass_threshold": 80},
            "logic": {"weight": 0.3, "pass_threshold": 70},
            "style": {"weight": 0.2, "pass_threshold": 70},
            "prompt_quality": {"weight": 0.15, "pass_threshold": 60},
            "feasibility": {"weight": 0.15, "pass_threshold": 70}
        },
        # 提示词预检（生成前质检，实现见 prompt_qc.py）
        # ⚠️ 与图片/视频质检不同：它**不依赖质检接口**（纯确定性检查、零成本、零模型依赖），
        # 因此即使没配质检接口也默认开启 —— 提示词是出图/出片的输入，输入错了后面白跑。
        "prompt_enabled": True,
        # warn=只记录 / repair=确定性自愈后放行（默认）/ block=有问题就拦
        "prompt_mode": "repair",
        "updated_at": None,
    }


def _normalize_override(raw) -> dict:
    if not isinstance(raw, dict):
        return {"base_url": "", "api_key": "", "model": ""}
    return {"base_url": (raw.get("base_url") or "").strip(),
            "api_key": (raw.get("api_key") or "").strip(),
            "model": (raw.get("model") or "").strip()}


# ===================== 配置读写 =====================

def load_config(config_path: str) -> dict:
    cfg = _empty_config()
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            for k in CONFIG_KEYS:
                if k == "endpoint_override":
                    continue
                if k in data and data[k] is not None:
                    cfg[k] = data[k]
            cfg["endpoint_override"] = _normalize_override(data.get("endpoint_override"))
            # P0-3：明文密钥自动迁移到加密库并清空 json 字段（含 endpoint_override 嵌套结构）
            if data.get("api_key") or (isinstance(data.get("endpoint_override"), dict)
                                       and data["endpoint_override"].get("api_key")):
                try:
                    import secret_store
                    if secret_store.scrub_plaintext_key(config_path, "qc", _PROJECT_ROOT):
                        logger.info("质检配置中的明文密钥已迁移至加密库")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"质检密钥迁移失败（暂不阻断）：{e}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"质检配置读取失败（按默认值处理）：{e}")
    # 密钥取值：环境变量 > 加密库 > json（迁移后应为空）
    try:
        import secret_store
        secure = secret_store.get_store(_PROJECT_ROOT).get_api_key("qc")
        if secure:
            cfg["api_key"] = secure
            # endpoint_override 若声明了 base_url/model 但密钥为空，用加密库的密钥补齐，
            # 否则 resolve_endpoint 会因 override 缺 key 而落到未配置分支
            ov = cfg.get("endpoint_override")
            if isinstance(ov, dict) and ov and not (ov.get("api_key") or "").strip():
                ov["api_key"] = secure
        env_base = secret_store.SecretStore.env_base_url("qc")
        env_model = secret_store.SecretStore.env_model("qc")
        if env_base:
            cfg["base_url"] = env_base
        if env_model:
            cfg["model"] = env_model
    except Exception as e:  # noqa: BLE001
        logger.warning(f"质检密钥读取异常（回退 json）：{e}")
    # 类型兜底：统一交给 `_normalize`（save_config / load_config_dict 用的是同一套规则）
    # ⚠️ 审计 G2：这里原本手写了一份「简化版」归一化，且布尔项用的是裸 `bool()` ——
    #    字符串 "false"/"0"/"no"/"off" 都是非空字符串 → 一律判 True（用户在页面或第三方
    #    脚本里写 "false"，读回来反而是「开」）；而且它只覆盖 3 个开关，
    #    audio_enabled / script_enabled / keyframe_qc_enabled / prompt_enabled 完全没归一化，
    #    音频/剧本/尾帧质检的开关因此形同虚设。两份口径并存必然漂移，现收敛为单一实现。
    return _normalize(cfg)


def save_config(config_path: str, patch: dict, keep_key_if_blank: bool = True) -> dict:
    cfg = load_config(config_path)
    for k, v in (patch or {}).items():
        if k not in CONFIG_KEYS or k == "updated_at":
            continue
        if k == "endpoint_override":
            cfg["endpoint_override"] = _normalize_override(v)
            continue
        if k == "api_key":
            if v is None:
                continue
            v = str(v).strip()
            if not v and keep_key_if_blank:
                continue          # 留空 = 不改动已保存密钥
            if v and set(v) == {"*"}:
                continue          # 误提交脱敏值，按不改动处理
            if v and "*" in v:
                continue          # 脱敏回显值（如 sk-a******wxyz），按不改动处理
            # P0-3：有效新密钥写入加密库，json 中不落明文
            try:
                import secret_store
                if not secret_store.get_store(_PROJECT_ROOT).set_api_key("qc", v):
                    raise RuntimeError("密钥加密存储不可用")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"质检密钥加密保存失败：{e}")
                raise ValueError(
                    "质检密钥加密存储不可用（缺少 cryptography 或主密钥），已拒绝明文落盘。"
                    f"请安装 cryptography 后重试，或改用环境变量 "
                    f"{secret_store.ENV_KEY_MAP.get('qc', 'MJSCXT_API_KEY_QC')} 配置密钥。")
            cfg["api_key"] = ""
            continue
        if k in ("enabled", "image_enabled", "video_enabled", "image_ref_compare"):
            # ⚠️ 审计 G2：这里原本是 `bool(v)` —— 字符串 "false"/"0"/"no"/"off"/"none"
            #    都是**非空字符串**，`bool()` 一律判 True。用户在页面或第三方脚本里把开关
            #    存成 "false"，读回来反而是「开」，开关形同虚设。
            #    同文件 `_as_bool` 的 docstring 恰好记录了这条坑，只有 save_config 自己漏改。
            #    非法值沿用当前（已归一化的）取值，绝不静默翻转开关。
            cfg[k] = _as_bool(v, bool(cfg.get(k, False)))
        elif k in ("pass_score", "max_retries", "video_frame_count", "image_max_side",
                   "timeout", "api_retries"):
            try:
                cfg[k] = int(v)
            except Exception:  # noqa: BLE001
                continue
        elif k == "api_backoff":
            try:
                cfg[k] = float(v)
            except Exception:  # noqa: BLE001
                continue
        else:
            cfg[k] = str(v or "")
    cfg = load_config_dict(cfg)
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    # P0-3：落盘前确保密钥字段不含明文（含 endpoint_override 嵌套结构）
    # 明文密钥只存加密库；endpoint_override 的密钥由 load_config 从加密库补齐。
    cfg["api_key"] = ""
    ov = cfg.get("endpoint_override")
    if isinstance(ov, dict):
        ov["api_key"] = ""
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    # ⚠️ 必须重新 load_config 再返回，**不能返回上面那个已清空 api_key 的 cfg**：
    # 明文密钥只存加密库，上面刚把 cfg["api_key"] 置空是为了防明文落盘；
    # 若直接返回它，调用方拿到的就是「无密钥」的配置 → public_view 算出
    # ready=False / image_qc_active=False → 前端每次保存都会弹
    # 「质检开关已开启，但质检接口信息不完整，生成流程将跳过质检」的**误导性警告**
    # （实测：仅提交 {"image_enabled": true} 后 video_qc_active 从 true 掉成 false）。
    # 重新读取一次即可拿到 load_config 注入的密钥，且返回的正是应用真正会用的配置。
    return load_config(config_path)


def load_config_dict(raw: dict) -> dict:
    """把内存字典按 load_config 的同一套规则规范化（供 save / 测试复用）"""
    base = _empty_config()
    base.update({k: v for k, v in (raw or {}).items()
                 if k in CONFIG_KEYS and k not in ("endpoint_override",)})
    base["endpoint_override"] = _normalize_override((raw or {}).get("endpoint_override"))
    return _normalize(base)


def _as_bool(v, default: bool = False) -> bool:
    """宽容布尔解析。

    ⚠️ 不能用 ``bool(v)``：字符串 ``"false"`` / ``"0"`` / ``"no"`` / ``"off"`` 都是
    非空字符串，``bool()`` 一律判 True —— 于是用户在页面或第三方脚本里把开关存成
    ``"false"``，读回来反而是「开」，开关形同虚设（本轮审计发现的正是这类空壳开关）。
    """
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("false", "0", "no", "off", "none", "null", ""):
            return False
        if s in ("true", "1", "yes", "on"):
            return True
        return default
    if v is None:
        return default
    return bool(v)


def _normalize(cfg: dict) -> dict:
    cfg["enabled"] = _as_bool(cfg.get("enabled"), False)
    cfg["image_enabled"] = _as_bool(cfg.get("image_enabled"), True)
    cfg["video_enabled"] = _as_bool(cfg.get("video_enabled"), True)
    # ⚠️ 这三个开关此前只出现在 CONFIG_KEYS / _empty_config，_normalize 里没有归一化：
    #    用户在页面上把 audio_enabled 存成字符串 "false" 或 0，读回来就是真值，
    #    开关形同虚设。补齐布尔归一化（与 image_enabled / video_enabled 同口径）。
    cfg["audio_enabled"] = _as_bool(cfg.get("audio_enabled"), True)
    cfg["script_enabled"] = _as_bool(cfg.get("script_enabled"), True)
    cfg["keyframe_qc_enabled"] = _as_bool(cfg.get("keyframe_qc_enabled"), True)
    # 提示词预检：默认开启；模式非法时回落到 repair（与 prompt_qc.prompt_qc_mode 同语义）
    cfg["prompt_enabled"] = _as_bool(cfg.get("prompt_enabled"), True)
    # 图片质检是否附带设定图（save_config 的布尔组里也有它，读取侧必须同口径归一化）
    cfg["image_ref_compare"] = _as_bool(cfg.get("image_ref_compare"), True)
    _pmode = str(cfg.get("prompt_mode") or "repair").strip().lower()
    cfg["prompt_mode"] = _pmode if _pmode in ("warn", "repair", "block") else "repair"
    cfg["endpoint_override"] = _normalize_override(cfg.get("endpoint_override"))
    for key, default, lo, hi in (("pass_score", 70, 0, 100), ("max_retries", 2, 0, 10),
                                 ("video_frame_count", 3, 1, 6), ("image_max_side", 1024, 256, 2048),
                                 ("timeout", 180, 10, 900),
                                 ("api_retries", API_RETRY_ATTEMPTS, 0, 5)):
        try:
            cfg[key] = max(lo, min(hi, int(cfg.get(key, default))))
        except Exception:  # noqa: BLE001
            cfg[key] = default
    try:
        cfg["api_backoff"] = max(0.0, min(30.0, float(cfg.get("api_backoff", API_RETRY_BACKOFF))))
    except Exception:  # noqa: BLE001
        cfg["api_backoff"] = API_RETRY_BACKOFF

    # 音频客观层可调阈值。⚠️ 此前这三个键只在 _empty_config 里写死，_normalize 不碰它们，
    # 于是任何越界值（负数占比、正数 dB、>1 的偏差上限）都会原样生效，把判定卡死或放空。
    for key, default, lo, hi in (("audio_min_speech_ratio", 0.50, 0.0, 1.0),
                                 ("audio_min_mean_db", -45.0, -100.0, 0.0),
                                 ("audio_max_drift", 0.50, 0.0, 5.0)):
        try:
            cfg[key] = max(lo, min(hi, float(cfg.get(key, default))))
        except Exception:  # noqa: BLE001
            cfg[key] = default
    return cfg


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def resolve_endpoint(cfg: dict, override: dict = None) -> dict:
    """生效的质检接口：只认质检自己的独立配置（base_url / api_key / model）。

    - 质检不再复用文本分析 LLM 接口（LLM 可能只配了纯文本模型，无法做视觉质检）。
    - override：可选的临时覆盖（仅用于页面「测试连通性」传暂存参数，不落盘）。
    - 若其它链路曾自动写入接口（endpoint_override 有值且与当前一致），标记为「自动写入」。
    """
    ov = _normalize_override(override) if override else {"base_url": "", "api_key": "", "model": ""}
    saved = _normalize_override(cfg.get("endpoint_override"))
    if ov["base_url"] and ov["api_key"] and ov["model"]:
        return {**ov, "source": "test_override",
                "auto_synced": bool(saved["base_url"]) and saved["base_url"] == ov["base_url"]}
    ep = {"base_url": (cfg.get("base_url") or "").strip(),
          "api_key": (cfg.get("api_key") or "").strip(),
          "model": (cfg.get("model") or "").strip(),
          # ⚠️ 审计 G2 同型：不能裸 `bool()` —— "false"/"0"/"off" 都是非空字符串，一律判 True，
          #    于是「关闭思考」的开关在字符串写法下永远关不掉。
          "disable_thinking": _as_bool(cfg.get("disable_thinking"), DISABLE_THINKING_DEFAULT),
          "min_tokens_when_thinking": int(cfg.get("min_tokens_when_thinking")
                                          or MIN_TOKENS_WHEN_THINKING)}
    auto = bool(saved["base_url"] and saved["base_url"] == ep["base_url"]
                and saved["api_key"] and saved["api_key"] == ep["api_key"]
                and saved["model"] and saved["model"] == ep["model"])
    return {**ep, "source": "qc_config", "auto_synced": auto}


def qc_endpoint_ready(cfg: dict, override: dict = None) -> bool:
    ep = resolve_endpoint(cfg, override)
    return bool(ep["base_url"] and ep["api_key"] and ep["model"])


def set_endpoint(config_path: str, base_url: str, api_key: str, model: str) -> dict:
    """被其它链路（如分镜加速自动写入）调用的质检接口同步：落盘并记录 endpoint_override

    P0-3：密钥写入加密库，json 只保留 base_url/model（不含明文密钥）。
    """
    cfg = load_config(config_path)
    ep = {"base_url": (base_url or "").strip(), "api_key": (api_key or "").strip(),
          "model": (model or "").strip()}
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return cfg
    try:
        import secret_store
        secret_store.get_store(_PROJECT_ROOT).set_api_key("qc", ep["api_key"])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"质检接口密钥加密保存失败（将不落盘明文）：{e}")
    cfg["base_url"], cfg["api_key"], cfg["model"] = ep["base_url"], "", ep["model"]
    # endpoint_override 只记录 base_url/model，密钥由加密库提供
    cfg["endpoint_override"] = {"base_url": ep["base_url"], "api_key": "", "model": ep["model"]}
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return cfg


def reset_endpoint(config_path: str) -> dict:
    """「恢复为 AI 设置」：清空自动写入的接口，交还给用户在 AI 设置里独立配置"""
    cfg = load_config(config_path)
    try:
        import secret_store
        secret_store.get_store(_PROJECT_ROOT).clear_api_key("qc")
    except Exception:  # noqa: BLE001
        pass
    cfg["base_url"], cfg["api_key"], cfg["model"] = "", "", ""
    cfg["endpoint_override"] = {"base_url": "", "api_key": "", "model": ""}
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return cfg


def public_view(cfg: dict) -> dict:
    ep = resolve_endpoint(cfg)
    ready = bool(cfg.get("enabled") and ep["base_url"] and ep["api_key"] and ep["model"])
    view = {k: v for k, v in cfg.items() if k != "api_key"}
    view.update({
        "has_api_key": bool(cfg.get("api_key")),
        "api_key_masked": mask_key(cfg.get("api_key") or ""),
        "effective_base_url": ep["base_url"],
        "effective_model": ep["model"],
        "endpoint_source": ep["source"],
        "endpoint_auto_synced": ep.get("auto_synced", False),
        "ready": ready,
        "image_qc_active": bool(cfg.get("enabled") and cfg.get("image_enabled") and ep["base_url"] and ep["api_key"] and ep["model"]),
        "video_qc_active": bool(cfg.get("enabled") and cfg.get("video_enabled") and ep["base_url"] and ep["api_key"] and ep["model"]),
        # 音频分两档：客观层只要开关打开就能跑（零模型依赖），AI 层还要接口就绪。
        # 分开暴露是为了让前端能如实告诉用户「客观层在跑但 AI 层没配置」，
        # 而不是笼统显示一个「未启用」让人误以为整条音频质检都没生效。
        "audio_qc_active": bool(cfg.get("enabled") and cfg.get("audio_enabled")),
        "audio_ai_active": bool(cfg.get("enabled") and cfg.get("audio_enabled")
                                and ep["base_url"] and ep["api_key"] and ep["model"]),
        # S1：剧本质检开关（与 image/video 同口径，需 enabled + script_enabled + 端点就绪）。
        # 前端 /api/qc/config 据此如实回显「剧本质检是否真的在跑」，不再假装已质检。
        "script_qc_active": bool(cfg.get("enabled") and cfg.get("script_enabled")
                                and ep["base_url"] and ep["api_key"] and ep["model"]),
        "default_image_prompt": DEFAULT_IMAGE_PROMPT,
        "default_video_prompt": DEFAULT_VIDEO_PROMPT,
        "default_audio_prompt": DEFAULT_AUDIO_PROMPT,
    })
    return view


def clear_config(config_path: str) -> dict:
    """清空配置：同时重置开关与独立接口（不含 prompt / 阈值以外的残留）"""
    cfg = _empty_config()
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ===================== 开关判定（生成流程调用） =====================

def image_qc_ready(cfg: dict, override: dict = None) -> bool:
    return bool(cfg.get("enabled") and cfg.get("image_enabled")
                and qc_endpoint_ready(cfg, override))


def video_qc_ready(cfg: dict, override: dict = None) -> bool:
    return bool(cfg.get("enabled") and cfg.get("video_enabled")
                and qc_endpoint_ready(cfg, override))


def audio_qc_ready(cfg: dict, override: dict = None) -> bool:
    """音频质检**客观层**是否可执行。

    ⚠️ 与 image_qc_ready / video_qc_ready 不同，这里**不要求质检接口就绪**：
    客观层是纯 ffmpeg 指标判定（零模型依赖、零成本、毫秒级），没配多模态接口的项目
    同样应该享受「整段无声 / 时长失控 / 削波」这些硬闸保护。AI 层是否可跑另见
    ``audio_ai_ready``，``check_audio`` 内部会自行判断。
    """
    return bool(_as_bool(cfg.get("enabled"), False)
                and _as_bool(cfg.get("audio_enabled"), True))


def audio_ai_ready(cfg: dict, override: dict = None) -> bool:
    """音频质检**AI 层**（频谱图 + 波形图送多模态模型）是否可执行。"""
    return bool(audio_qc_ready(cfg) and qc_endpoint_ready(cfg, override))


# ===================== 网络层重试与退避（仅针对瞬时故障） =====================
# 说明：这里处理的是「同一次质检请求」内部的网络重试，与「不达标重生成」
# （qc_cfg.max_retries，换 seed 重画）是两件事。
# 目的：外部质检服务偶发 read timeout / HTTP 520 / 空返回时，先在网络层做有限次
#       重试 + 指数退避，避免一次瞬时抖动就让资产被判为「质检调用异常」而阻断入库。
# 语义不变：重试全部失败后仍按原逻辑阻断（不静默放行、不跳过质检）。

API_RETRY_ATTEMPTS = 2      # 额外重试次数（总尝试 = 1 + 2），可被 qc_config.json 的 api_retries 覆盖
API_RETRY_BACKOFF = 1.5     # 退避基数（秒）：第 n 次重试等待 base * 2^(n-1)，可被 api_backoff 覆盖
API_RETRY_MAX_SLEEP = 8.0   # 单次退避等待上限（秒）
API_CONNECT_TIMEOUT = 10    # 连接超时（秒）；读超时沿用配置里的 timeout

# 可重试的 HTTP 状态码：限流 / 网关与上游瞬时故障（含 Cloudflare 系列 5xx）
RETRYABLE_HTTP_STATUS = (408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)
# 明确不可重试的状态码：请求本身 / 鉴权 / 配置类错误，重试无意义
FATAL_HTTP_STATUS = (400, 401, 403, 404, 405, 413, 415, 422)


class QcApiError(RuntimeError):
    """质检接口调用错误。

    retryable=True 表示属瞬时故障（超时 / 可重试 5xx / 空返回 / 非 JSON），值得重试；
    retryable=False 表示请求或配置本身有问题，重试无意义。
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int = None,
                 retry_after: float = None, kind: str = ""):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status = status
        self.retry_after = retry_after
        # kind 供 _post_chat 决定「下一轮怎么改请求重试」：
        #   reasoning_only    = 模型只吐了思考内容、正文为空
        #   think_opt_rejected= 服务端不接受 chat_template_kwargs 字段
        self.kind = kind or ""
        self.attempts = 1
        self.retry_errors: list = []

    def with_attempts(self, attempts: int, retry_errors: list) -> "QcApiError":
        """补上重试统计后的最终错误（供上层留痕与展示：共尝试几次、失败原因）"""
        self.attempts = int(attempts)
        self.retry_errors = list(retry_errors or [])
        if self.attempts > 1:
            self.args = (f"{self.args[0]}（共尝试 {self.attempts} 次，"
                         f"含重试 {self.attempts - 1} 次）",)
        return self


# ===================== HTTP（OpenAI 兼容视觉输入） =====================

def _chat_url(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    path = urlparse(u).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return u
    if path in ("", "/"):
        return f"{u}/v1/chat/completions"
    return f"{u}/chat/completions"


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or host.endswith(".local")


DISABLE_THINKING_DEFAULT = False
MIN_TOKENS_WHEN_THINKING = 1024


def _with_thinking_off(payload: dict) -> dict:
    """注入「关闭思考」参数（OpenAI 兼容层事实标准：chat_template_kwargs）"""
    p = dict(payload or {})
    ctk = p.get("chat_template_kwargs")
    ctk = dict(ctk) if isinstance(ctk, dict) else {}
    ctk["enable_thinking"] = False
    p["chat_template_kwargs"] = ctk
    return p


def _without_thinking_opt(payload: dict) -> dict:
    return {k: v for k, v in (payload or {}).items() if k != "chat_template_kwargs"}


def _bump_tokens(payload: dict, factor: int = 4) -> dict:
    """放大 max_tokens：思考型模型把额度吃光时的兜底"""
    p = dict(payload or {})
    try:
        cur = int(p.get("max_tokens") or 0)
    except (TypeError, ValueError):
        cur = 0
    p["max_tokens"] = max(512, cur * factor)
    return p


def encode_image_data_url(path: str, max_side: int = 1024) -> str:
    """读取本地图片 → 压缩 → data:image/jpeg;base64,...（控制 token 与带宽）"""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, float(max_side) / max(w, h)) if max(w, h) else 1.0
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        raw = buf.getvalue()
        mime = "image/jpeg"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图片压缩失败，按原图送检：{e}")
        ext = os.path.splitext(path)[1].lower()
        mime = {"png": "image/png", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _post_chat_once(ep: dict, payload: dict, timeout: int) -> dict:
    """单次请求（不做重试）。失败时抛 QcApiError，并标注该错误是否值得重试。"""
    url = _chat_url(ep["base_url"])
    session = requests.Session()
    if _is_local(url):
        session.trust_env = False
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {ep['api_key']}"}
    t0 = time.time()
    try:
        resp = session.post(url, headers=headers, json=payload,
                            timeout=(min(API_CONNECT_TIMEOUT, timeout), timeout))
    except requests.exceptions.Timeout as e:
        raise QcApiError(f"质检接口请求超时（连接 {min(API_CONNECT_TIMEOUT, timeout)}s / "
                         f"读取 {timeout}s）：{e}", retryable=True) from e
    except requests.exceptions.RequestException as e:
        # 连接被重置、TLS 抖动、分块传输中断等，均属瞬时故障
        raise QcApiError(f"质检接口网络异常：{e}", retryable=True) from e
    latency = int((time.time() - t0) * 1000)
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        # 少数服务端不认 chat_template_kwargs，直接 400。这里识别出来，
        # 让 _post_chat 去掉该字段再试一次，而不是把「模型没配好」甩给用户。
        if resp.status_code == 400 and any(
                k in (body or "").lower()
                for k in ("chat_template_kwargs", "enable_thinking", "unknown", "unsupported")):
            raise QcApiError(f"质检接口不接受 chat_template_kwargs 字段：{body}",
                             retryable=True, status=400, kind="think_opt_rejected")
        if resp.status_code in RETRYABLE_HTTP_STATUS:
            retry_after = None
            try:
                ra = (resp.headers.get("Retry-After") or "").strip()
                if ra:
                    retry_after = float(ra)
            except Exception:  # noqa: BLE001
                retry_after = None
            raise QcApiError(f"质检接口 HTTP {resp.status_code}：{body}", retryable=True,
                             status=resp.status_code, retry_after=retry_after)
        raise QcApiError(f"质检接口 HTTP {resp.status_code}：{body}",
                         retryable=resp.status_code not in FATAL_HTTP_STATUS,
                         status=resp.status_code)
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise QcApiError(f"质检接口返回非 JSON：{(resp.text or '')[:200]}",
                         retryable=True) from e
    choices = data.get("choices") or []
    if not choices:
        raise QcApiError(f"质检接口返回缺少 choices：{json.dumps(data, ensure_ascii=False)[:200]}",
                         retryable=True)
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content:
        content = choices[0].get("text") or ""
    if not content:
        reasoning = str(msg.get("reasoning_content") or "")
        if reasoning:
            # 推理型模型的典型症状：token 全被思考吃掉，正文为 ""。
            # 不能拿思考内容当结论（它不是 JSON 判定），只能换打法重试。
            raise QcApiError(
                "质检模型只返回了思考内容（reasoning_content）而没有正文，"
                "通常是推理型模型把 max_tokens 全花在思考上。系统会自动关闭思考模式重试",
                retryable=True, kind="reasoning_only")
        raise QcApiError("质检接口返回内容为空", retryable=True)
    return {"content": str(content), "latency_ms": latency, "url": url}


def _post_chat(ep: dict, payload: dict, timeout: int, retries: int = None,
               backoff: float = None) -> dict:
    """带重试与指数退避的请求：仅对瞬时故障重试，不可重试错误立即抛出。

    返回体额外带 attempts（实际尝试次数）与 retry_errors（此前失败原因），供留痕。
    """
    total = 1 + max(0, int(API_RETRY_ATTEMPTS if retries is None else retries))
    base = API_RETRY_BACKOFF if backoff is None else max(0.0, float(backoff))
    errors: list = []
    cur = dict(payload)
    if ep.get("disable_thinking", DISABLE_THINKING_DEFAULT):
        cur = _with_thinking_off(cur)          # 显式关思考（默认不关）
    else:
        # 允许思考 → 保证额度下限，否则思考吃光 token 只剩空正文
        try:
            mt = int(cur.get("max_tokens") or 0)
        except (TypeError, ValueError):
            mt = 0
        floor = int(ep.get("min_tokens_when_thinking") or MIN_TOKENS_WHEN_THINKING)
        if mt < floor:
            cur["max_tokens"] = floor
    for i in range(total):
        try:
            r = _post_chat_once(ep, cur, timeout)
            r["attempts"] = i + 1
            r["retry_errors"] = errors
            if errors:
                logger.warning(f"质检接口重试成功：共尝试 {i + 1} 次，此前失败：{errors[:2]}")
            return r
        except QcApiError as e:
            last = (i >= total - 1)
            kind = getattr(e, "kind", "") or ""
            # 关思考也不管用（服务端不认该字段 / 已关仍空）→ 换打法再试，而不是干等
            if not last and kind in ("reasoning_only", "think_opt_rejected"):
                if kind == "think_opt_rejected" or "chat_template_kwargs" in cur:
                    cur = _without_thinking_opt(cur)
                cur = _bump_tokens(cur)
                errors.append(str(e)[:200])
                logger.warning(f"质检接口 {kind}（第 {i + 1}/{total} 次）："
                               f"去掉思考抑制参数并放大 max_tokens={cur.get('max_tokens')} 后重试")
                continue
            if not e.retryable or last:
                raise e.with_attempts(i + 1, errors)
            wait = e.retry_after if (e.retry_after and e.retry_after > 0) else base * (2 ** i)
            wait = min(float(wait), API_RETRY_MAX_SLEEP)
            errors.append(str(e)[:200])
            logger.warning(f"质检接口瞬时故障（第 {i + 1}/{total} 次尝试失败）：{e}；"
                           f"{wait:.1f}s 后重试")
            if wait > 0:
                time.sleep(wait)
    raise RuntimeError("质检接口重试流程异常结束")  # 理论不可达


# 1x1 PNG（视觉连通性探测用，避免依赖本地文件）
_PROBE_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AGtDQAAAABJRU5ErkJggg==")


def test_vision(ep: dict, timeout: int = 60) -> dict:
    """视觉连通性探测：用一张极小图片请求一次，确认该接口 / 模型支持图像输入。

    永不抛异常：失败时返回 success=False + error。
    """
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return {"success": False, "vision": False,
                "error": "接口未配置（base_url / api_key / model 均为必填）"}
    content = [
        {"type": "text", "text": "这是一张测试图片，请只回复：OK"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _PROBE_PNG_B64}},
    ]
    # max_tokens 不能给小：混合推理模型（允许思考时）会先吐几百 token 的思考，
    # 给 16 会让正文永远为空，用户看到「reply 为空」会误判成模型不支持视觉。
    payload = {"model": ep["model"],
               "messages": [{"role": "user", "content": content}],
               "temperature": 0, "max_tokens": 256, "stream": False}
    try:
        r = _post_chat(ep, payload, timeout, retries=1, backoff=1.0)
    except Exception as e:  # noqa: BLE001
        # 只返回了思考内容 ⇒ 接口其实是通的、模型也响应了，只是额度被思考吃掉。
        # 这时不能报「不支持图像」（那会把用户引去换模型），只能说「未确认」。
        if getattr(e, "kind", "") == "reasoning_only":
            return {"success": True, "vision": None, "uncertain": True,
                    "verdict": "reachable_but_no_content",
                    "error": str(e), "max_tokens": payload["max_tokens"],
                    "hint": ("接口可达、模型有响应，但额度被思考占用、没有返回正文，"
                             "因此无法确认是否支持图像输入。请提高质检模块的 max_tokens "
                             "（建议 ≥1024）后重测。"),
                    "attempts": getattr(e, "attempts", 1)}
        return {"success": False, "vision": False, "error": str(e),
                "attempts": getattr(e, "attempts", 1)}
    reply = (r.get("content") or "").strip()
    out = {"success": True, "latency_ms": r["latency_ms"], "url": r["url"],
           "reply": reply[:100], "attempts": int(r.get("attempts", 1)),
           "retries_used": max(0, int(r.get("attempts", 1)) - 1),
           "max_tokens": payload["max_tokens"]}
    if reply:
        out["vision"] = True
        out["verdict"] = "ok"
    else:
        # 接口可达，但这次没吐正文（额度被思考占用）→ 视觉能力「未确认」，
        # 不要谎报 vision=True（否则用户以为质检模型已就绪，实际跑起来全是空判断）
        out["vision"] = None
        out["uncertain"] = True
        out["verdict"] = "reachable_but_no_content"
        out["hint"] = ("接口可达，但本次没有返回正文（可能额度被思考占用）。"
                       "无法确认该模型是否支持图像输入；若质检时报「空判断/正文为空」，"
                       "请提高质检模块的 max_tokens 或改用心智更轻的视觉模型。")
    return out


def _repair_json_quotes(text: str) -> str:
    """修复「字符串值内嵌未转义双引号」——多模态模型最常见的 JSON 破坏方式。

    实测案例：模型在 reason 里用引号强调剧本原文，返回
        "reason": "场景与镜头描述不符：灯笼仍亮着而非"同时熄灭"，与核心叙事冲突。"
    JSON 规范里字符串内的双引号必须转义，这种输出 json.loads 必然失败。
    而质检解析失败会走「阻断入库」分支，导致图片永远不落盘、流水线无限重跑 ——
    所以这里必须把它修回来，而不是让模型的一次措辞不当毁掉整条生产链。

    做法：逐字符扫描并跟踪「是否在字符串内」。只有在字符串内遇到双引号、
    且其后第一个非空白字符**不是**合法结构字符（: , } ] 或结束）时，
    才判定为内嵌引号并补上转义；其余情况按正常结束引号处理。
    """
    out: list = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not in_str:
            if ch == '"':
                in_str = True
            out.append(ch)
            i += 1
            continue
        # —— 字符串内部 ——
        if ch == "\\":
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            nxt = text[j] if j < n else ""
            if nxt in (":", ",", "}", "]", ""):
                in_str = False           # 合法的字符串结束
                out.append(ch)
            else:
                out.append('\\"')        # 内嵌引号 → 转义
            i += 1
            continue
        # 字符串内裸换行/制表符同样非法，一并转义
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _balance_brackets(text: str) -> str:
    """括号配平：丢掉多余的右括号 / 补齐缺失的右括号（模型另一种常见破坏方式）。

    实测案例（2026-09-19 音频 AI 层）：模型返回的 issues 数组后面多了一个 ``]``
        {"score": 5, ..., "issues": ["…", "…", "…"]]}
    这种输出 json.loads 必然失败，而质检解析失败会走「AI 层调用失败」，让一次
    措辞/括号失误把整条音频 AI 质检降级成「只有客观层结论」。

    ⚠️ 必须**跳过字符串内部**的括号（reason 里出现「」【】都是正常内容），
    只对结构括号 `{}` `[]` 做栈式配平；字符串内的括号原样保留。
    """
    out: list = []
    stack: list = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch in "{[":
            stack.append(ch)
            out.append(ch)
            i += 1
            continue
        if ch in "}]":
            want = "{" if ch == "}" else "["
            if stack and stack[-1] == want:
                stack.pop()
                out.append(ch)
            # 多余的右括号：直接丢弃（这是要修的那种情况）
            i += 1
            continue
        out.append(ch)
        i += 1
    closers = {"{": "}", "[": "]"}
    out.extend(closers[c] for c in reversed(stack))
    return "".join(out)


def _loads_lenient(text: str):
    """多级容错解析模型返回的 JSON（都失败时返回 None，由调用方决定如何处置）

    顺序：原文 → 截取花括号区间 → 去尾逗号 → 修复内嵌未转义引号 → 括号配平。
    逐级收紧，能救回就救回，绝不因一次措辞/括号失误就丢掉一份有效质检结论。
    """
    s, e = text.find("{"), text.rfind("}")
    inner = text[s:e + 1] if (s != -1 and e > s) else ""
    candidates = [text]
    if inner:
        candidates += [inner, re.sub(r",\s*([}\]])", r"\1", inner)]
    for c in candidates:
        for cand in (c, _repair_json_quotes(c),
                     _balance_brackets(re.sub(r",\s*([}\]])", r"\1", c))):
            try:
                obj = json.loads(cand)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(obj, dict):
                return obj
    return None


_FENCE_OPEN_RE = re.compile(r"^\s*```[a-zA-Z0-9_+\-]*[ \t]*\r?\n?")
_FENCE_CLOSE_RE = re.compile(r"\r?\n?[ \t]*```\s*$")


def _strip_code_fence(text: str) -> str:
    """去掉 markdown 代码块围栏，露出里面的 JSON。

    多模态模型经常把结论包成 ```json ... ```，而且**可能只有开头没有结尾**
    （输出被 max_tokens 截断），此时旧的配对正则 ```` ```(?:json)?\\s*(.+?)``` ````
    会整体失配，导致围栏原样进入解析、最终报「无法解析为 JSON」（缺陷 D7）。
    这里做三级退化：配对围栏 → 只剥开头 → 只剥结尾。
    """
    if not text:
        return text
    raw = text.strip()
    if "```" not in raw:
        return raw

    # 1) 标准配对围栏（允许围栏后有换行、允许 json 之外的语言标记）
    m = re.search(r"```[a-zA-Z0-9_+\-]*[ \t]*\r?\n?(.*?)```", raw, re.S)
    if m:
        inner = m.group(1).strip()
        if inner:
            return inner

    # 2) 只有开头围栏（结尾被截断）—— 先剥开头
    stripped = _FENCE_OPEN_RE.sub("", raw)
    # 3) 再尝试剥掉可能存在的结尾围栏
    stripped = _FENCE_CLOSE_RE.sub("", stripped)
    return stripped.strip()


def parse_verdict(content: str, pass_score: int) -> dict:
    text = _strip_code_fence(content or "")
    obj = _loads_lenient(text)
    if not isinstance(obj, dict):
        # 多级容错仍失败：把完整原文落到日志，便于定位畸形输出的具体形态
        logger.warning("质检结论无法解析为 JSON（清洗后 %d 字符）：%s", len(text), text)
        raise RuntimeError(f"质检结论无法解析为 JSON：{text[:200]}")
    score = obj.get("score")
    try:
        score = int(round(float(score)))
    except Exception:  # noqa: BLE001
        score = None
    passed = obj.get("pass")
    if passed is None:
        passed = obj.get("passed")
    if isinstance(passed, str):
        passed = passed.strip().lower() in ("true", "yes", "1", "pass", "达标", "合格")
    if passed is None:
        passed = (score is not None and score >= pass_score)
    issues = obj.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    # P0：代码侧硬闸（关键缺陷阻断 + 分数不达标强制不通过），只收紧不放宽
    sm = obj.get("style_match")
    if isinstance(sm, str):
        sm = sm.strip().lower() in ("true", "yes", "1", "pass", "一致", "符合")
    return _finalize_verdict({
        "passed": bool(passed) and (score is None or score >= pass_score),
        "score": score,
        "reason": str(obj.get("reason") or obj.get("comment") or "")[:500],
        "issues": [str(x)[:200] for x in issues][:6],
        "raw": text[:1000],
        "style_match": sm if isinstance(sm, bool) else None,
    }, pass_score)


def _run_vision(ep: dict, prompt: str, image_paths: list, cfg: dict) -> dict:
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url",
                        "image_url": {"url": encode_image_data_url(p, cfg.get("image_max_side", 1024))}})
    payload = {
        "model": ep["model"],
        "messages": [
            {"role": "system", "content": "你是严格、客观的漫剧内容质检员，只输出 JSON。"},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
    }
    t0 = time.time()
    resp = _post_chat(ep, payload, cfg.get("timeout", 180),
                      retries=cfg.get("api_retries", API_RETRY_ATTEMPTS),
                      backoff=cfg.get("api_backoff", API_RETRY_BACKOFF))
    verdict = parse_verdict(resp["content"], cfg.get("pass_score", 70))
    verdict.update({"ok": True, "skipped": False, "latency_ms": resp["latency_ms"],
                    "api_total_ms": int((time.time() - t0) * 1000),
                    "api_attempts": int(resp.get("attempts", 1)),
                    "retries_used": max(0, int(resp.get("attempts", 1)) - 1),
                    "call_url": resp["url"], "model": ep["model"]})
    return verdict


# ===================== 通用多模态调用（供一致性校验等复用） =====================

def run_custom_vision(prompt: str, image_paths: list, cfg: dict = None,
                      override: dict = None, max_tokens: int = 900,
                      system: str = None, temperature: float = 0) -> dict:
    """通用多模态调用：给定提示词 + 多张图，返回模型原始文本与耗时。

    与质检的区别：不做「合格/不合格」判定，只把判定权交给调用方（如一致性校验）。
    复用质检的 endpoint 配置（base_url / api_key / model）与图片编码逻辑。
    永不抛异常：失败返回 {"ok": False, "error": ...}。
    """
    cfg = cfg or _empty_config()
    ep = resolve_endpoint(cfg, override)
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return {"ok": False, "error": "多模态接口未配置（base_url/api_key/model）"}
    if not image_paths:
        return {"ok": False, "error": "未提供图片"}
    missing = [p for p in image_paths if not p or not os.path.isfile(p)]
    if missing:
        return {"ok": False, "error": f"图片不存在：{missing[0]}"}

    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url",
                        "image_url": {"url": encode_image_data_url(
                            p, cfg.get("image_max_side", 1024))}})
    payload = {
        "model": ep["model"],
        "messages": [
            {"role": "system", "content": system or "你是严格、客观的漫剧视觉审校员，只输出 JSON。"},
            {"role": "user", "content": content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    t0 = time.time()
    try:
        resp = _post_chat(ep, payload, cfg.get("timeout", 180),
                          retries=cfg.get("api_retries", API_RETRY_ATTEMPTS),
                          backoff=cfg.get("api_backoff", API_RETRY_BACKOFF))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"通用多模态调用失败：{e}")
        return {"ok": False, "error": str(e), "model": ep.get("model")}
    return {"ok": True, "content": resp.get("content") or "",
            "latency_ms": resp.get("latency_ms"),
            "api_total_ms": int((time.time() - t0) * 1000),
            "model": ep.get("model"), "url": resp.get("url")}


def parse_json_loose(content: str) -> dict:
    """宽松解析模型返回的 JSON（容忍 markdown 代码块 / 前后废话）。失败返回 {}"""
    if not content:
        return {}
    text = _strip_code_fence(content)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    # 最后再走一次多级容错（处理内嵌未转义引号等畸形输出）
    obj = _loads_lenient(text)
    return obj if isinstance(obj, dict) else {}


# ===================== 图片质检 =====================

def check_image(image_path: str, shot_desc: str = "", cfg: dict = None,
                override: dict = None, style: str = "",
                ref_images: list = None) -> dict:
    """单张图片质检。永不抛异常：失败时返回 ok=False 并带 error。
    override 仅用于「测试连通性」临时传参，不落盘。
    style：目标风格串（用户与总控敲定），用于「风格达标」判定；为空则不做风格检测。
    ref_images：设定参考图 [(label, path), ...]（也可以是 {"label","path"} dict）——
      生成侧用的那几张角色 / 物品 / 场景设定图。给了就一并送检，让模型能**逐个核对
      「画面里的角色 / 物品是否与设定一致、有没有变形」**，而不是凭想象判。
      受 cfg["image_ref_compare"]（默认 True）控制；不存在的文件与重复图（同一张被多个
      槽位复用）自动跳过。"""
    cfg = cfg or _empty_config()
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "质检总开关未开启"}
    if not cfg.get("image_enabled"):
        return {"ok": False, "skipped": True, "reason": "图片质检开关未开启"}
    ep = resolve_endpoint(cfg, override)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {"ok": False, "skipped": True, "reason": "质检接口未配置（base_url/api_key/model）"}
    if not image_path or not os.path.isfile(image_path):
        return {"ok": False, "skipped": False, "error": f"图片不存在：{image_path}"}
    style_norm = str(style or "").strip()
    prompt = (cfg.get("image_prompt") or DEFAULT_IMAGE_PROMPT).replace(
        "{shot_desc}", shot_desc or "（无）").replace(
        "{pass_score}", str(cfg.get("pass_score", 70))).replace(
        "{style}", style_norm or "（未指定）")
    prompt = prompt + WATERMARK_EXEMPT_NOTE + CRITICAL_RULE_NOTE + FRAMING_TOLERANCE_NOTE
    if style_norm:
        prompt = prompt + STYLE_CHECK_NOTE.replace("{style}", style_norm)
    # ---- 设定一致性核对：把生成时用的参考图一并送检 ----
    ref_list = []
    if ref_images and cfg.get("image_ref_compare", True):
        seen = {os.path.abspath(image_path)}
        for item in ref_images:
            if isinstance(item, dict):
                label = item.get("label") or item.get("name") or ""
                path = item.get("path") or item.get("file") or item.get("image")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                label, path = item[0], item[1]
            else:
                continue
            if not path or not os.path.isfile(path):
                continue
            ap = os.path.abspath(path)
            if ap in seen:      # 同一张图被多个槽位复用（如主角色=次要角色）不重复送
                continue
            seen.add(ap)
            ref_list.append((label, path))
        ref_list = ref_list[:MAX_REF_IMAGES]
    if ref_list:
        prompt = prompt + build_ref_consistency_note(ref_list)
    image_paths = [image_path] + [p for _l, p in ref_list]
    try:
        verdict = _run_vision(ep, prompt, image_paths, cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图片质检调用失败：{e}")
        return {"ok": False, "skipped": False, "error": str(e),
                "api_attempts": getattr(e, "attempts", 1),
                "retryable": getattr(e, "retryable", None)}
    verdict = _apply_style_gate(verdict, style_norm)
    # 把「带了几张设定图」透出来，便于前端/体检确认该能力真的生效（而不是静默没带）
    verdict["ref_images_used"] = len(ref_list)
    if ref_list:
        verdict["ref_labels"] = [str(l)[:60] for l, _p in ref_list]
    return verdict


# ===================== 视频质检（ffmpeg 抽帧） =====================

def _decode_io(raw) -> str:
    """subprocess 原始字节 → 文本。

    Windows 下 text=True 会按 locale(cp936) 解码 ffmpeg/ffprobe 的 UTF-8 输出，
    遇到非 GBK 字节会抛 UnicodeDecodeError 被外层 except 吞掉，导致帧率/时长静默解析为 0。
    因此统一走「UTF-8 + replace」解码。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")


def ffmpeg_exe() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe_exe() -> str:
    return shutil.which("ffprobe") or ""


def _ffmpeg_stream_info(video_path: str) -> dict:
    """解析 ffmpeg -i 输出里的 时长 / 帧率（不依赖 ffprobe）"""
    info = {"fps": 0.0, "duration": 0.0}
    try:
        p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", video_path],
                           capture_output=True, timeout=60)
        txt = _decode_io(p.stderr) + _decode_io(p.stdout)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
        if m:
            info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m2 = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt) or re.search(r"(\d+(?:\.\d+)?)\s*tbr", txt)
        if m2:
            info["fps"] = float(m2.group(1))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"解析视频流信息失败：{e}")
    return info


def _count_video_frames(video_path: str) -> int:
    """统计视频总帧数：优先 ffprobe -count_frames，其次 ffmpeg 解码计数"""
    fp = _ffprobe_exe()
    if fp:
        try:
            p = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=nb_read_frames",
                                "-of", "default=nw=1:nk=1", video_path],
                               capture_output=True, timeout=300)
            first = _decode_io(p.stdout).strip().splitlines()
            if first and first[0].strip().isdigit():
                return int(first[0].strip())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ffprobe 统计帧数失败：{e}")
    try:
        p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", video_path,
                            "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                           capture_output=True, timeout=600)
        txt = _decode_io(p.stderr) + _decode_io(p.stdout)
        hits = re.findall(r"frame=\s*(\d+)", txt)
        if hits:
            return int(hits[-1])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ffmpeg 统计帧数失败：{e}")
    return 0


def _fill_video_meta(meta: dict, video_path: str) -> dict:
    """补全 fps / frame_count（已有时不重复探测）"""
    if not meta.get("fps"):
        meta["fps"] = _ffmpeg_stream_info(video_path).get("fps") or 0.0
    if not meta.get("frame_count") and meta.get("fps") and meta.get("duration"):
        meta["frame_count"] = int(round(float(meta["duration"]) * float(meta["fps"])))
    return meta


def video_meta(video_path: str, fallback: dict = None) -> dict:
    """视频元信息 {duration, fps, frame_count, source, degraded}（B 项⑤）

    时长三级回退：
      ① ffprobe format=duration（最准）
      ② ffmpeg -i 的 Duration 行
      ③ 帧数 ÷ 帧率 推算（帧数 = ffprobe -count_frames / ffmpeg 解码计数 / 调用方传入；
         帧率 = ffmpeg -i 解析值 / 调用方传入，如 ComfyUI 返回值）
    """
    fb = dict(fallback or {})
    meta = {"duration": 0.0,
            "fps": float(fb.get("fps") or 0.0),
            "frame_count": int(fb.get("frame_count") or 0),
            "source": "unknown", "degraded": False}
    if not video_path or not os.path.isfile(video_path):
        meta["degraded"] = True
        return meta

    fp = _ffprobe_exe()
    if fp:
        try:
            p = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", video_path],
                               capture_output=True, text=True, timeout=60)
            s = (p.stdout or "").strip()
            if s and s.upper() != "N/A":
                d = float(s)
                if d > 0:
                    meta["duration"] = d
                    meta["source"] = "ffprobe-format"
                    return _fill_video_meta(meta, video_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ffprobe 读取时长失败：{e}")

    info = _ffmpeg_stream_info(video_path)
    if info.get("fps") and not meta["fps"]:
        meta["fps"] = info["fps"]
    if info.get("duration", 0) > 0:
        meta["duration"] = info["duration"]
        meta["source"] = "ffmpeg-duration"
        return _fill_video_meta(meta, video_path)

    if not meta["frame_count"]:
        meta["frame_count"] = _count_video_frames(video_path)
    if meta["frame_count"] and meta["fps"]:
        meta["duration"] = float(meta["frame_count"]) / float(meta["fps"])
        meta["source"] = "frame_count/fps"
    else:
        meta["degraded"] = True          # 时长仍未知：抽帧将退化为固定时间点
    return meta


def video_duration(video_path: str, fallback: dict = None) -> float:
    """视频时长（秒）。读不到时按「帧数 ÷ 帧率」或调用方（ComfyUI）返回值推算"""
    return float(video_meta(video_path, fallback).get("duration") or 0.0)


def extract_frames(video_path: str, out_dir: str, count: int = 3,
                   prefix: str = "frame", fallback_meta: dict = None) -> dict:
    """全片均匀抽帧（片头/中段/片尾），并校验每帧实际时间戳。

    返回 {ok, frames, frame_meta, timestamps, duration, meta, error}
      frames      : 抽帧图片路径列表（喂给多模态模型）
      frame_meta  : [{path, requested_ts, actual_ts, verified}]，actual_ts 由 ffmpeg showinfo 解析
    """
    result = {"ok": False, "frames": [], "frame_meta": [], "timestamps": [],
              "duration": 0.0, "meta": {}, "error": ""}
    if not video_path or not os.path.isfile(video_path):
        result["error"] = f"视频不存在：{video_path}"
        return result
    if not shutil.which("ffmpeg"):
        result["error"] = "未找到 ffmpeg（请安装并加入 PATH）"
        return result
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"抽帧目录创建失败：{e}"
        return result

    meta = video_meta(video_path, fallback_meta)
    dur = float(meta.get("duration") or 0.0)
    result["duration"] = round(dur, 3)
    result["meta"] = meta

    count = max(1, min(6, int(count or 3)))
    if dur > 0:
        # 全片均匀覆盖：5% ~ 95%（避免首尾黑场/异常帧），count=1 时取正中间
        if count == 1:
            times = [round(dur * 0.5, 3)]
        else:
            lo, hi = dur * 0.05, dur * 0.95
            times = [round(lo + (hi - lo) * i / (count - 1), 3) for i in range(count)]
    else:
        # 时长确实无法推算：退化按固定时间点抽帧（0s/1s/2s…），并标记 degraded
        times = [float(i) for i in range(count)]
        result["meta"]["degraded"] = True

    frames, frame_meta = [], []
    exe = ffmpeg_exe()
    for i, ts in enumerate(times):
        out = os.path.join(out_dir, f"{prefix}_{i + 1}_{int(ts * 1000)}ms.jpg")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "info", "-copyts",
               "-ss", f"{ts}", "-i", video_path, "-frames:v", "1",
               "-vf", "showinfo", "-q:v", "2", out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            actual = None
            m = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", p.stderr or "")
            if m:
                actual = round(float(m.group(1)), 3)
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                frames.append(out)
                if actual is None:
                    actual = round(ts, 3)      # ffmpeg 未回吐 pts 时，以请求时间戳兜底
                frame_meta.append({
                    "path": out,
                    "requested_ts": round(ts, 3),
                    "actual_ts": actual,
                    "verified": abs(actual - ts) <= max(1.0, dur * 0.05) if dur > 0 else False,
                })
            else:
                logger.warning(f"抽帧失败 ts={ts}s: {(p.stderr or '')[:200]}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"抽帧异常 ts={ts}s: {e}")
    result["ok"] = bool(frames)
    result["frames"] = frames
    result["frame_meta"] = frame_meta
    result["timestamps"] = [fm["actual_ts"] for fm in frame_meta]
    if not frames:
        result["error"] = "ffmpeg 抽帧未产出任何图片"
    return result


def check_video(video_path: str, shot_desc: str = "", cfg: dict = None,
                override: dict = None, frames_dir: str = None,
                fallback_meta: dict = None, style: str = "") -> dict:
    """视频质检：ffmpeg 抽帧 → 多模态判定。永不抛异常。
    override 仅用于「测试连通性」临时传参，不落盘。
    style：目标风格串，用于「风格达标」判定；为空则不做风格检测。"""
    cfg = cfg or _empty_config()
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "质检总开关未开启"}
    if not cfg.get("video_enabled"):
        return {"ok": False, "skipped": True, "reason": "视频质检开关未开启"}
    ep = resolve_endpoint(cfg, override)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {"ok": False, "skipped": True, "reason": "质检接口未配置（base_url/api_key/model）"}

    if not frames_dir:
        frames_dir = os.path.join(os.path.dirname(os.path.abspath(video_path)),
                                  "_qc_frames", os.path.splitext(os.path.basename(video_path))[0])
    fr = extract_frames(video_path, frames_dir, cfg.get("video_frame_count", 3),
                        fallback_meta=fallback_meta)
    if not fr["ok"]:
        return {"ok": False, "skipped": False, "error": fr["error"], "duration": fr["duration"],
                "frame_meta": fr.get("frame_meta") or [], "video_meta": fr.get("meta") or {}}

    style_norm = str(style or "").strip()
    prompt = (cfg.get("video_prompt") or DEFAULT_VIDEO_PROMPT).replace(
        "{shot_desc}", shot_desc or "（无）").replace(
        "{frame_count}", str(len(fr["frames"]))).replace(
        "{style}", style_norm or "（未指定）")
    ts_brief = "、".join(f"第{i + 1}帧 {fm['actual_ts']}s"
                        for i, fm in enumerate(fr.get("frame_meta") or []))
    prompt = f"共 {len(fr['frames'])} 张抽帧图片（按时间顺序；实际时间戳：{ts_brief}）。\n" + prompt
    prompt = prompt + WATERMARK_EXEMPT_NOTE + CRITICAL_RULE_NOTE + FRAMING_TOLERANCE_NOTE
    if style_norm:
        prompt = prompt + STYLE_CHECK_NOTE.replace("{style}", style_norm)
    try:
        verdict = _run_vision(ep, prompt, fr["frames"], cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"视频质检调用失败：{e}")
        return {"ok": False, "skipped": False, "error": str(e),
                "api_attempts": getattr(e, "attempts", 1),
                "retryable": getattr(e, "retryable", None),
                "frames": fr["frames"], "duration": fr["duration"],
                "frame_meta": fr.get("frame_meta") or [],
                "timestamps": fr.get("timestamps") or [],
                "video_meta": fr.get("meta") or {}}
    verdict.update({"frames": fr["frames"], "duration": fr["duration"],
                    "frame_count": len(fr["frames"]),
                    "frame_meta": fr.get("frame_meta") or [],
                    "timestamps": fr.get("timestamps") or [],
                    "duration_source": (fr.get("meta") or {}).get("source"),
                    "video_meta": fr.get("meta") or {}})
    return _apply_style_gate(verdict, style_norm)


# ===================== 音频质检（客观层 + AI 层） =====================

def _merge_audio_verdict(objective: dict, ai: dict) -> dict:
    """把客观层与 AI 层结论合成一份 verdict。

    合成规则是**单向收紧**（与 ``_finalize_verdict`` 同一取向，绝不反向放行）：

    * ``blocked`` = 客观层致命 OR AI 命中音频关键缺陷词；
    * ``passed``  = 两层都通过（任何一层说不通过就是不通过）；
    * ``score``   = 两层取**较小值**（避免「客观层 20 分、AI 层 90 分 → 平均 55 分」
      这种把硬缺陷摊薄的算法）；
    * ``issues``  = 两层合并去重，并把客观层的硬缺陷放在最前面。
    """
    obj = objective or {}
    aiv = ai or {}
    obj_issues = list(obj.get("issues") or [])
    obj_fatal = list(obj.get("critical_issues") or [])
    ai_issues = [str(x) for x in (aiv.get("issues") or [])]
    ai_fatal = find_critical_issues(ai_issues, AUDIO_CRITICAL_KEYWORDS) if aiv.get("ok") else []

    issues = []
    for it in list(obj_fatal) + list(ai_fatal) + obj_issues + ai_issues:
        s = str(it)
        if s and s not in issues:
            issues.append(s)

    scores = [s for s in (obj.get("score"), aiv.get("score"))
              if isinstance(s, int)]
    score = min(scores) if scores else None
    passed = bool(obj.get("passed")) and bool(aiv.get("passed")) if aiv.get("ok") \
        else bool(obj.get("passed"))
    blocked = bool(obj_fatal) or bool(ai_fatal)

    fatal_all = list(obj_fatal)
    for it in ai_fatal:
        if it not in fatal_all:
            fatal_all.append(it)
    if blocked:
        reason = "关键缺陷：" + "；".join([str(x) for x in fatal_all][:2])
    elif not passed:
        reason = "存在可优化项：" + "；".join([str(x) for x in issues][:2])
    elif issues:
        reason = "存在可优化项（不阻断）：" + "；".join([str(x) for x in issues][:2])
    else:
        reason = obj.get("reason") or aiv.get("reason") or "音频达标"

    out = dict(obj)
    out.update({
        "ok": True,
        "skipped": False,
        "objective_only": False,
        "ai_used": bool(aiv.get("ok")),
        "passed": passed,
        "accepted": not blocked,
        "blocked": blocked,
        "issues": [str(x)[:300] for x in issues][:8],
        "critical_issues": [str(x)[:200] for x in fatal_all][:6],
        "reason": str(reason)[:500],
        "objective": {"passed": obj.get("passed"), "score": obj.get("score"),
                      "issues": obj_issues, "critical_issues": obj_fatal},
        "ai": {k: v for k, v in aiv.items() if k != "raw"} if aiv.get("ok") else None,
    })
    if isinstance(score, int):
        out["score"] = score
    # 音频没有「画面风格」维度，AI 层返回的 style_match 字段在这里没有意义，删掉以免误导
    out.pop("style_match", None)
    return out


def check_audio(audio_path: str, expect_sec: float = None, line_text: str = "",
                cfg: dict = None, override: dict = None, visuals_dir: str = None,
                check_speech_ratio: bool = True) -> dict:
    """音频质检：客观层（ffmpeg 指标）→ AI 层（频谱图 + 波形图送多模态）。永不抛异常。

    参数
    ----
    ``expect_sec``  期望时长（单句传台词推算时长，整轨传视频时长）。只用于「时长偏差」判定。
    ``line_text``   该句台词，注入 ``audio_prompt`` 的 ``{line_text}`` 占位符供模型比对。
    ``check_speech_ratio``
        是否启用「有声占比下限」判定。⚠️ 整集/成片音轨必须传 ``False``：这类音轨本来就有
        大量刻意留白（无台词镜头），拿单句的 50% 标准去卡它必然误判成「漏句」。

    执行策略（两层都不阻断生成，只给结论）
    ------------------------------------
    1. 客观层永远执行；**客观层已判致命时直接返回，不再花一次模型调用** ——
       整段无声的音频没必要再让模型看频谱图。
    2. AI 层仅在 ``audio_ai_ready`` 为真时执行；未配置接口时如实标注
       ``ai_skip_reason``，客观层结论单独生效（不假装做过 AI 质检）。
    """
    cfg = cfg or _empty_config()
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "质检总开关未开启"}
    if not cfg.get("audio_enabled"):
        return {"ok": False, "skipped": True, "reason": "音频质检开关未开启"}
    if not audio_path or not os.path.isfile(audio_path):
        return {"ok": False, "skipped": False, "blocked": True, "passed": False,
                "score": 0, "issues": [], "critical_issues": [f"音频文件不存在：{audio_path}"],
                "reason": f"音频文件不存在：{audio_path}", "metrics": {},
                "objective_only": True}

    # ---- ① 客观层（零模型依赖，始终执行）----
    objective = audio_qc.quick_check(
        audio_path, expect_sec=expect_sec,
        min_speech_ratio=(cfg.get("audio_min_speech_ratio", 0.50)
                          if check_speech_ratio else None),
        min_mean_db=cfg.get("audio_min_mean_db", -45.0),
        max_drift=cfg.get("audio_max_drift", 0.50))

    if objective.get("blocked"):
        objective["ai_skipped"] = True
        objective["ai_skip_reason"] = "客观层已判定致命缺陷（整段无声/无音轨/空文件），跳过 AI 层"
        return objective

    # ---- ② AI 层（需要质检接口）----
    if not audio_ai_ready(cfg, override):
        objective["ai_skipped"] = True
        objective["ai_skip_reason"] = "质检接口未配置（base_url/api_key/model），仅客观层结论生效"
        return objective

    if not visuals_dir:
        visuals_dir = os.path.join(os.path.dirname(os.path.abspath(audio_path)),
                                   "_qc_audio",
                                   os.path.splitext(os.path.basename(audio_path))[0])
    vis = audio_qc.render_visuals(
        audio_path, visuals_dir,
        prefix=os.path.splitext(os.path.basename(audio_path))[0])
    if not vis.get("ok"):
        objective["ai_skipped"] = True
        objective["ai_skip_reason"] = f"频谱/波形图渲染失败：{vis.get('error')}"
        return objective

    m = objective.get("metrics") or {}
    facts = [f"实测时长 {float(m.get('duration') or 0):.2f} 秒"]
    if expect_sec:
        facts.append(f"期望时长 {float(expect_sec):.2f} 秒")
    if isinstance(m.get("speech_ratio"), float):
        facts.append(f"有声占比 {float(m['speech_ratio']) * 100:.1f}%")
    if isinstance(m.get("mean_db"), float):
        facts.append(f"平均电平 {float(m['mean_db']):.1f} dB")
    if isinstance(m.get("max_db"), float):
        facts.append(f"峰值电平 {float(m['max_db']):.1f} dB")
    prompt = (cfg.get("audio_prompt") or DEFAULT_AUDIO_PROMPT)
    prompt = prompt.replace("{line_text}", str(line_text or "（未提供）")[:200])
    prompt = prompt.replace("{pass_score}", str(cfg.get("pass_score", 70)))
    prompt = ("以下是该段配音的 ffmpeg 客观指标（可信的实测值，请结合图片一并判断）："
              + "，".join(facts) + "。\n") + prompt

    try:
        ep = resolve_endpoint(cfg, override)
        ai = _run_vision(ep, prompt, vis["images"], cfg)
    except Exception as e:  # noqa: BLE001 - 质检失败不能影响主流程
        logger.warning(f"音频 AI 层质检调用失败：{e}")
        objective["ai_skipped"] = True
        objective["ai_skip_reason"] = f"AI 层调用失败：{e}"
        objective["visuals"] = vis["images"]
        return objective

    merged = _merge_audio_verdict(objective, ai)
    merged["visuals"] = vis["images"]
    return merged


# ===================== 质检历史持久化 =====================

# 只替换文件系统非法字符（\ / : * ? " < > | 与控制字符），保留中文等 Unicode 字符。
# 旧规则「非 ASCII 一律折叠成下划线」会让「青玉短笛_top」与「雨后石桥_top」都变成
# 「______top」而命中同一历史文件、互相覆盖 shot_id（物品 top 视角记录被写成了场景名），
# 这里改为保真命名以根治该命名复用问题。
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_token(raw, fallback: str = "0") -> str:
    s = str(raw if raw not in (None, "") else fallback).strip()
    safe = _ILLEGAL_NAME_CHARS.sub("_", s).strip(" .")
    return safe or fallback


def history_path(qc_root: str, project: str, kind: str, shot_key) -> str:
    safe_kind = safe_token(kind, "asset")
    safe_shot = safe_token(shot_key, "0")
    return os.path.join(qc_root, project, f"{safe_kind}_{safe_shot}.json")


def append_history(qc_root: str, project: str, kind: str, shot_key,
                   entry: dict) -> str:
    """追加一条质检/重试记录，返回历史文件绝对路径"""
    path = history_path(qc_root, project, kind, shot_key)
    data = {"project": project, "kind": kind, "shot_id": shot_key,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "records": []}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f) or {}
            data["records"] = old.get("records") or []
            data["created_at"] = old.get("created_at")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"质检历史读取失败（将重建）：{e}")
    data.setdefault("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    entry = dict(entry or {})
    entry.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    data["records"].append(entry)
    data["total_attempts"] = len(data["records"])
    data["last_passed"] = bool(entry.get("passed"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)


def read_history(qc_root: str, project: str, kind: str, shot_key) -> dict:
    path = history_path(qc_root, project, kind, shot_key)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    return {}


# ===================== 剧本质检 =====================

def _validate_script_structure(script: dict) -> list:
    """验证剧本JSON结构完整性

    ⚠️ 字段名必须与 novel_to_script 的真实产物一致：
    历史缺陷：这里要求角色/物品有 ``description``、镜头有 ``visual_description``
    —— 这三个字段**都不存在**（真实字段是 character.appearance / item.appearance /
    shot.description）。于是每一份剧本都会被报出一堆「缺少字段」，而 check_script
    把 structure_issues 当关键问题**直接判失败并跳过 AI 质检**，
    导致剧本质检 100% 假失败、AI 层从未真正运行过。
    """
    issues = []

    # 检查顶层字段
    required_top = ["title", "characters", "items", "scenes", "shots"]
    for field in required_top:
        if field not in script:
            issues.append(f"缺少必要字段: {field}")

    # 检查角色结构（真实字段：name / appearance / personality）
    if "characters" in script:
        for i, char in enumerate(script["characters"]):
            if not isinstance(char, dict):
                issues.append(f"角色 {i} 不是有效对象")
                continue
            for field in ["name", "appearance", "personality"]:
                if field not in char:
                    issues.append(f"角色 {char.get('name', f'[{i}]')} 缺少字段: {field}")

    # 检查物品结构（真实字段：name / category / appearance）
    if "items" in script:
        for i, item in enumerate(script["items"]):
            if not isinstance(item, dict):
                issues.append(f"物品 {i} 不是有效对象")
                continue
            for field in ["name", "category", "appearance"]:
                if field not in item:
                    issues.append(f"物品 {item.get('name', f'[{i}]')} 缺少字段: {field}")

    # 检查场景结构（真实字段：name / location / appearance）
    if "scenes" in script:
        for i, scene in enumerate(script["scenes"]):
            if not isinstance(scene, dict):
                issues.append(f"场景 {i} 不是有效对象")
                continue
            if not scene.get("name"):
                issues.append(f"场景 {i} 缺少名称")

    # 检查镜头结构（真实字段：description，不是 visual_description）
    if "shots" in script:
        for i, shot in enumerate(script["shots"]):
            if not isinstance(shot, dict):
                issues.append(f"镜头 {i} 不是有效对象")
                continue
            required_shot = ["shot_id", "duration", "camera", "location", "description"]
            for field in required_shot:
                if field not in shot:
                    issues.append(f"镜头 {shot.get('shot_id', f'[{i}]')} 缺少字段: {field}")

    return issues


def _matches_defined(name, defined: set) -> bool:
    """引用名是否指向某个已定义名字（支持别名的包含匹配）

    项目其它环节（``dialogue_utils.match_prefix_speaker``、``novel_to_script._norm_shots``）
    对角色/物品名都用「相等或互相包含」的宽松匹配。剧本里常出现全名/简称混用
    （实测：角色表登记「方源」，镜头里写「古月方源」），若按精确相等判定，
    会刷出一堆「引用了未定义的角色」假错误，把真正的缺陷埋掉。
    """
    s = str(name or "").strip()
    if not s:
        return False
    if s in defined:
        return True
    return any(s in d or d in s for d in defined if d)


def _validate_script_logic(script: dict) -> list:
    """验证剧本逻辑一致性（角色/物品引用用宽松别名匹配，见 _matches_defined）"""
    issues = []

    # 提取定义的角色名和物品名
    defined_chars = {c.get("name") for c in script.get("characters", []) if isinstance(c, dict)}
    defined_items = {i.get("name") for i in script.get("items", []) if isinstance(i, dict)}

    # 检查镜头中的引用
    for i, shot in enumerate(script.get("shots", [])):
        if not isinstance(shot, dict):
            continue

        shot_id = shot.get("shot_id", f"[{i}]")

        # 检查角色引用（宽松匹配：别名/全名简称不报错）
        for char in shot.get("characters_in_shot", []):
            if not _matches_defined(char, defined_chars):
                issues.append(f"镜头 {shot_id} 引用了未定义的角色: {char}")

        # 检查物品引用（宽松匹配）
        for item in shot.get("items_in_shot", []):
            if not _matches_defined(item, defined_items):
                issues.append(f"镜头 {shot_id} 引用了未定义的物品: {item}")
    
    # 检查镜头顺序逻辑（简单检查shot_id是否连续）
    shot_ids = [s.get("shot_id") for s in script.get("shots", []) if isinstance(s, dict)]
    if shot_ids:
        # 尝试将shot_id转换为数字并检查连续性
        try:
            numeric_ids = []
            for sid in shot_ids:
                if isinstance(sid, (int, float)):
                    numeric_ids.append(int(sid))
                elif isinstance(sid, str) and sid.isdigit():
                    numeric_ids.append(int(sid))
            
            if numeric_ids:
                sorted_ids = sorted(numeric_ids)
                for i in range(1, len(sorted_ids)):
                    if sorted_ids[i] - sorted_ids[i-1] > 1:
                        issues.append(f"镜头ID不连续: {sorted_ids[i-1]} -> {sorted_ids[i]}")
        except (ValueError, TypeError):
            pass  # 非数字ID，跳过连续性检查
    
    return issues


def _style_tokens(text: str) -> set:
    """把风格串拆成可比较的词元

    风格串常常是一整串没有分隔符的中文（如「中国古风玄幻漫剧」），按分隔符切只得一个词元，
    与「国漫古风」完全无法比较 → 同义风格被误判为冲突。因此同时加入**字符二元组**：
    「中国古风玄幻漫剧」与「国漫古风」共享二元组「古风」，即视为一致。
    """
    s = str(text or "").strip()
    if not s:
        return set()
    toks = {t.strip() for t in re.split(r"[,，、;；/|\s]+", s) if len(t.strip()) >= 2}
    compact = re.sub(r"[,，、;；/|\s]+", "", s)
    toks |= {compact[i:i + 2] for i in range(len(compact) - 1)}
    return toks


def _validate_script_style(script: dict, style: str) -> list:
    """验证风格一致性

    ⚠️ 原实现用「整串互相包含」判定（``script_style.lower() not in style.lower()``），
    只要用户风格与剧本风格措辞不同就报不一致 —— 例如剧本写「中国古风玄幻漫剧」、
    指定风格「国漫古风」，两者明明同义却被判冲突。
    改为**词元重叠**判定：有任一共同词元即视为一致，完全不重叠才提示。
    """
    issues = []

    script_style = str(script.get("style") or "").strip()
    if script_style and style:
        a, b = _style_tokens(script_style), _style_tokens(style)
        # 词元有交集，或一方是另一方的子串，都算一致
        if not (a & b) and script_style not in style and style not in script_style:
            issues.append(f"剧本风格 '{script_style}' 与指定风格 '{style}' 可能不一致（无共同风格词）")

    return issues


def _validate_script_prompts(script: dict) -> list:
    """验证提示词质量

    ⚠️ 真实字段是 shot.description / character.appearance。
    历史缺陷：这里读的是 ``shot.visual_description``（不存在）→ 每个镜头都被报
    「缺少视觉描述」，把提示词质量校验变成了纯噪声。
    """
    issues = []

    # 检查镜头画面描述质量（真实字段：description）
    for i, shot in enumerate(script.get("shots", [])):
        if not isinstance(shot, dict):
            continue

        shot_id = shot.get("shot_id", f"[{i}]")
        visual_desc = str(shot.get("description") or "")

        if not visual_desc:
            issues.append(f"镜头 {shot_id} 缺少画面描述")
        elif len(visual_desc) < 30:
            issues.append(f"镜头 {shot_id} 画面描述过短（{len(visual_desc)}字），建议50字以上")

    # 检查角色外貌描述（真实字段：appearance）
    for i, char in enumerate(script.get("characters", [])):
        if not isinstance(char, dict):
            continue

        char_name = char.get("name", f"[{i}]")
        appearance = str(char.get("appearance") or "")
        if not appearance:
            issues.append(f"角色 {char_name} 缺少外貌描述")
        elif len(appearance) < 20:
            issues.append(f"角色 {char_name} 外貌描述过短（{len(appearance)}字）")

    return issues


#: 单镜时长下限/上限，与 novel_to_script.SHOT_DURATION_MIN/MAX 对齐（3~12 秒）。
#: 历史缺陷：这里写 1~15 秒且「镜头数 > 30 就告警」，而剧本生成端的约束是 3~12 秒、
#: 真实剧集单集可达 56 镜 —— 约束互相打架，正常剧本反被判不可执行。
SHOT_DURATION_MIN_OK = 3.0
SHOT_DURATION_MAX_OK = 12.0
SHOT_DURATION_TOLERANCE = 2.0     # 超出边界的容差（模型四舍五入 / 台词长度微调）


def _validate_script_feasibility(script: dict, target_duration: int = 60) -> list:
    """验证可执行性（阈值与剧本生成端保持一致，见上方常量说明）"""
    issues = []

    shots = script.get("shots", [])
    if not shots:
        issues.append("剧本没有镜头，无法执行")
        return issues

    # 计算总时长
    total_duration = 0
    shot_durations = []
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        duration = shot.get("duration", 0)
        if not isinstance(duration, (int, float)):
            issues.append(f"镜头 {shot.get('shot_id', f'[{i}]')} 时长无效: {duration}")
            continue

        shot_durations.append(duration)
        total_duration += duration

        # 检查单个镜头时长（留 2 秒容差，避免浮点/取整误报）
        if duration < SHOT_DURATION_MIN_OK - SHOT_DURATION_TOLERANCE:
            issues.append(f"镜头 {shot.get('shot_id', f'[{i}]')} 时长过短: {duration}秒"
                          f"（建议 {SHOT_DURATION_MIN_OK:g}~{SHOT_DURATION_MAX_OK:g} 秒）")
        elif duration > SHOT_DURATION_MAX_OK + SHOT_DURATION_TOLERANCE:
            issues.append(f"镜头 {shot.get('shot_id', f'[{i}]')} 时长过长: {duration}秒"
                          f"（建议 {SHOT_DURATION_MIN_OK:g}~{SHOT_DURATION_MAX_OK:g} 秒）")

    # 检查总时长偏差
    if target_duration > 0 and shot_durations:
        duration_diff = abs(total_duration - target_duration) / target_duration
        if duration_diff > 0.3:  # 偏差超过30%
            issues.append(f"总时长 {total_duration}秒 与目标 {target_duration}秒 偏差过大"
                          f"（{duration_diff*100:.0f}%）")

    # 镜头数量：只卡「少到无法叙事」的下限。不设上限 —— 原文越长镜头越多是设计目标
    #（novel_to_script 按约 120 字/镜承载原文，实测单集可达 56 镜）。
    if len(shots) < 3:
        issues.append(f"镜头数量过少（{len(shots)}个），建议至少 5 个镜头")

    return issues


def check_script(script_path: str = None, script_data: dict = None,
                 style: str = "国漫古风", target_duration: int = 60,
                 cfg: dict = None, override: dict = None) -> dict:
    """剧本质检：结构+逻辑+风格+提示词质量+可执行性
    
    参数:
        script_path: 剧本文件路径（与script_data二选一）
        script_data: 剧本数据字典
        style: 指定创作风格
        target_duration: 目标总时长（秒）
        cfg: 质检配置
        override: 临时覆盖配置
    
    返回:
        dict: {
            "ok": bool,           # 质检是否成功执行
            "passed": bool,       # 是否通过质检
            "score": int,         # 综合得分
            "reason": str,        # 一句话结论
            "issues": list,       # 问题列表
            "suggestions": list,  # 改进建议
            "categories": dict,   # 各维度得分
            "skipped": bool,      # 是否跳过
            "error": str,         # 错误信息
        }
    """
    cfg = cfg or _empty_config()
    
    # 检查开关
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "质检总开关未开启"}
    if not cfg.get("script_enabled"):
        return {"ok": False, "skipped": True, "reason": "剧本质检开关未开启"}
    
    # 检查接口配置
    ep = resolve_endpoint(cfg, override)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {"ok": False, "skipped": True, "reason": "质检接口未配置（base_url/api_key/model）"}
    
    # 加载剧本数据
    if script_data is None:
        if script_path and os.path.isfile(script_path):
            try:
                with open(script_path, "r", encoding="utf-8") as f:
                    script_data = json.load(f)
            except Exception as e:
                return {"ok": False, "skipped": False, "error": f"剧本文件读取失败: {e}"}
        else:
            return {"ok": False, "skipped": False, "error": "未提供有效的剧本数据或文件"}
    
    # 执行客观验证（不依赖AI）
    structure_issues = _validate_script_structure(script_data)
    logic_issues = _validate_script_logic(script_data)
    style_issues = _validate_script_style(script_data, style)
    prompt_issues = _validate_script_prompts(script_data)
    feasibility_issues = _validate_script_feasibility(script_data, target_duration)
    
    all_objective_issues = (
        structure_issues + logic_issues + style_issues + 
        prompt_issues + feasibility_issues
    )
    
    # 计算客观验证得分（每个问题扣5分，最低0分）
    objective_score = max(0, 100 - len(all_objective_issues) * 5)
    
    # 如果客观验证有严重问题（结构或逻辑错误），直接返回失败
    critical_issues = structure_issues + logic_issues
    if critical_issues:
        return {
            "ok": True,
            "passed": False,
            "score": min(objective_score, 50),
            "reason": f"客观验证发现{len(critical_issues)}个关键问题",
            "issues": all_objective_issues,
            "suggestions": ["修复结构和逻辑问题后重试"],
            "categories": {
                "structure": max(0, 100 - len(structure_issues) * 10),
                "logic": max(0, 100 - len(logic_issues) * 10),
                "style": max(0, 100 - len(style_issues) * 5),
                "prompt_quality": max(0, 100 - len(prompt_issues) * 5),
                "feasibility": max(0, 100 - len(feasibility_issues) * 5)
            },
            "skipped": False,
            "objective_only": True,
        }
    
    # 调用AI进行深度质检
    prompt_template = cfg.get("script_prompt") or DEFAULT_SCRIPT_PROMPT
    script_json = json.dumps(script_data, ensure_ascii=False, indent=2)
    
    prompt = prompt_template.replace(
        "{style}", style or "国漫古风"
    ).replace(
        "{target_duration}", str(target_duration)
    ).replace(
        "{script_data}", script_json[:8000]  # 限制长度，避免超出上下文
    )
    
    # 调用多模态模型（虽然剧本质检不需要图像，但复用现有接口）
    content = [{"type": "text", "text": prompt}]
    payload = {
        "model": ep["model"],
        "messages": [
            {"role": "system", "content": "你是严格、客观的漫剧剧本质检员，只输出JSON。"},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 1200,
        "stream": False,
    }
    
    t0 = time.time()
    try:
        resp = _post_chat(ep, payload, cfg.get("timeout", 180),
                          retries=cfg.get("api_retries", API_RETRY_ATTEMPTS),
                          backoff=cfg.get("api_backoff", API_RETRY_BACKOFF))
    except Exception as e:
        # AI调用失败，回退到客观验证结果
        logger.warning(f"剧本质检AI调用失败，使用客观验证结果：{e}")
        return {
            "ok": True,
            "passed": objective_score >= cfg.get("pass_score", 70),
            "score": objective_score,
            "reason": f"AI质检调用失败，基于客观验证（{len(all_objective_issues)}个问题）",
            "issues": all_objective_issues,
            "suggestions": [],
            "categories": {
                "structure": max(0, 100 - len(structure_issues) * 10),
                "logic": max(0, 100 - len(logic_issues) * 10),
                "style": max(0, 100 - len(style_issues) * 5),
                "prompt_quality": max(0, 100 - len(prompt_issues) * 5),
                "feasibility": max(0, 100 - len(feasibility_issues) * 5)
            },
            "skipped": False,
            "ai_failed": True,
            "ai_error": str(e),
        }
    
    # 解析AI返回结果
    verdict = parse_verdict(resp["content"], cfg.get("pass_score", 70))
    
    # 合并客观验证和AI质检结果
    ai_issues = verdict.get("issues", [])
    ai_suggestions = verdict.get("suggestions", [])
    all_issues = all_objective_issues + ai_issues
    
    # 合并各维度得分
    categories = verdict.get("categories", {})
    objective_categories = {
        "structure": max(0, 100 - len(structure_issues) * 10),
        "logic": max(0, 100 - len(logic_issues) * 10),
        "style": max(0, 100 - len(style_issues) * 5),
        "prompt_quality": max(0, 100 - len(prompt_issues) * 5),
        "feasibility": max(0, 100 - len(feasibility_issues) * 5)
    }
    
    # 取客观验证和AI质检的较低分
    for cat in ["structure", "logic", "style", "prompt_quality", "feasibility"]:
        obj_score = objective_categories.get(cat, 100)
        ai_score = categories.get(cat, 100)
        categories[cat] = min(obj_score, ai_score)
    
    # 计算加权综合分
    script_cats = cfg.get("script_categories", {})
    weighted_score = 0
    total_weight = 0
    for cat, score in categories.items():
        cat_config = script_cats.get(cat, {"weight": 0.2})
        weight = cat_config.get("weight", 0.2)
        weighted_score += score * weight
        total_weight += weight
    
    if total_weight > 0:
        final_score = int(weighted_score / total_weight)
    else:
        final_score = verdict.get("score", objective_score)
    
    # 合并关键缺陷
    critical_hits = find_critical_issues(all_issues)
    
    return {
        "ok": True,
        "passed": verdict.get("passed", False) and not critical_hits and final_score >= cfg.get("pass_score", 70),
        "score": final_score,
        "reason": verdict.get("reason", ""),
        "issues": all_issues[:20],  # 限制数量
        "suggestions": ai_suggestions[:10],
        "categories": categories,
        "critical_issues": critical_hits,
        "skipped": False,
        "latency_ms": resp.get("latency_ms"),
        "api_total_ms": int((time.time() - t0) * 1000),
        "objective_issues": {
            "structure": structure_issues,
            "logic": logic_issues,
            "style": style_issues,
            "prompt_quality": prompt_issues,
            "feasibility": feasibility_issues
        }
    }


def script_qc_ready(cfg: dict, override: dict = None) -> bool:
    """检查剧本质检是否就绪"""
    return bool(cfg.get("enabled") and cfg.get("script_enabled")
                and qc_endpoint_ready(cfg, override))
