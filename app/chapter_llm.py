# -*- coding: utf-8 -*-
"""LLM 归纳「章节标题规律」→ 出正则 → 供 split_chapters 扫全文定位章节。

背景
----
每本小说的章节标记格式都可能不同（``第N章`` / ``Chapter N`` / ``一、`` /
``【场景三】`` / 空行分节 / 无标题……），``novel_parser.split_chapters`` 的纯正则
只认得几种固定格式，遇到其它就漏检、最后退化成按字数硬切。本模块让**前端「文本分析
模型」（OpenAI 兼容，任意厂商）** 看一眼小说样本，归纳出「这本书的章节标题长什么样」，
返回若干条正则；这些正则被并入 ``split_chapters`` 的标记扫描，从而对格式任意的小
说也能正确分章。

设计要点
--------
- **LLM 只看样本、出规则、再扫全文**（一次 LLM 调用，快、可复用于整本）：
  ``derive_patterns`` 取文本前段样本 → 让模型产出正则 → 校验/清洗 → 返回。
- **零第三方依赖、可离线单测**：``make_sample`` / ``build_prompt`` / ``parse_patterns``
  均为纯函数（不碰 LLMClient），只有 ``derive_patterns`` 才调用 client。
- **失败一律降级、绝不阻断上传**：任何异常（LLM 未配置 / 调用失败 / 解析不出）都
  返回 ``[]``，让调用方退回纯正则结果，行为与改造前完全一致。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

__all__ = [
    "make_sample", "build_prompt", "parse_patterns", "derive_patterns",
    "MAX_PATTERNS", "MAX_PATTERN_LEN", "SAMPLE_CHARS",
]

# LLM 最多返回几条章节标题正则（防止模型失控生成一长串）
MAX_PATTERNS = 8
# 单条正则长度上限（过长的「正则」通常是模型把整段标题原样塞进来，无泛化价值）
MAX_PATTERN_LEN = 120
# 喂给 LLM 的样本长度（取小说前段，足以观察其章节标记风格）
SAMPLE_CHARS = 6000

_SYSTEM_PROMPT = (
    "你是「小说章节切分」规则归纳器。我会给你一段小说正文样本，"
    "请你判断这本书的**章节标题行**长什么样，并给出若干条能匹配这些标题行"
    "的**Python 正则表达式**（按行匹配，用于 re.MULTILINE 下的 finditer）。"
)


def make_sample(text: str, sample_chars: int = SAMPLE_CHARS) -> str:
    """取正文前段样本（去多余空行、控制长度），供 LLM 观察章节标记风格。"""
    text = (text or "").strip()
    if not text:
        return ""
    # 规整空白：把 3 个及以上连续换行压成 2 个，避免样本被大段空行稀释
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[: int(sample_chars or SAMPLE_CHARS)]


def build_prompt(sample: str, hint: str = "") -> str:
    """构造让 LLM 产出章节标题正则的提示词（要求严格 JSON）。"""
    sample_block = (sample or "").strip() or "（样本为空）"
    hint_block = f"\n补充说明：{hint.strip()}" if (hint or "").strip() else ""
    return (
        "请观察下面这段小说正文样本，判断其中「章节/小节标题行」的格式"
        "（例如 第N章 / 第N回 / Chapter N / 一、 / 【场景】 / 纯空行分节 等），"
        "归纳出能匹配这些标题行的 Python 正则表达式。"
        + hint_block
        + "\n\n要求：\n"
        "1. 每条正则都按**整行**匹配（内部已含行首行尾锚定），供 re.MULTILINE 使用；"
        "2. 优先给出**能泛化到全书**的少数几条，而不是逐条枚举看到的标题原文；"
        f"3. 最多 {MAX_PATTERNS} 条；若无明显章节标记（如纯空行分节），可返回空数组；"
        "4. 只输出如下 JSON，不要任何解释或 markdown 代码块：\n"
        '{"patterns": ["正则1", "正则2"], "note": "一句话说明你观察到的章节格式"}\n'
        "\n=== 正文样本 ===\n"
        f"{sample_block}"
    )


def _sanitize_one(pat: str) -> str:
    """清洗单条正则：去空白首尾、限长；无法编译则返回空串。"""
    p = (pat or "").strip()
    if not p or len(p) > MAX_PATTERN_LEN:
        return ""
    # 统一加上多行语义，避免模型漏写 re.M 时 finditer 只匹配首行
    body = p if p.startswith("(?m)") else "(?m)" + p
    try:
        re.compile(body)
    except re.error:
        return ""
    return p


def parse_patterns(resp, max_patterns: int = MAX_PATTERNS) -> list:
    """从 LLM 的 JSON 响应里提取并校验正则列表（纯函数，可离线测）。

    返回清洗后、可编译的**去重**正则字符串列表；任何不合法项都被丢弃。
    ``resp`` 可以是 LLMClient.chat_json_robust 解析出的 dict，或含 JSON 的原始字符串。
    """
    data = resp
    if isinstance(resp, str):
        try:
            data = json.loads(resp)
        except (ValueError, TypeError):
            data = {}
    if not isinstance(data, dict):
        return []
    raw = data.get("patterns")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, str):
            continue
        p = _sanitize_one(item)
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= int(max_patterns or MAX_PATTERNS):
            break
    return out


def derive_patterns(client, text: str, hint: str = "") -> list:
    """调用 LLM 从小说文本归纳章节标题正则（失败返回 []，绝不抛给调用方）。

    client 为 llm_client.LLMClient（由 app._current_llm_client() 构造，密钥取自
    前端「文本分析模型」）。LLM 未配置 / 调用 / 解析任何失败 → 记日志并返回空列表，
    调用方据此退回纯正则切分，行为与改造前一致。
    """
    if client is None:
        return []
    sample = make_sample(text)
    if not sample:
        return []
    prompt = build_prompt(sample, hint)
    chat = getattr(client, "chat_json_robust", None)
    if not callable(chat):
        logger.warning("chapter_llm：客户端无 chat_json_robust，退回纯正则切分")
        return []
    try:
        resp = chat(prompt, system=_SYSTEM_PROMPT, max_tokens=1500)
    except Exception as e:  # noqa: BLE001  任何 LLM 侧失败都降级，不阻断上传
        logger.warning(f"chapter_llm：LLM 归纳章节规则失败，退回纯正则切分：{e}")
        return []
    pats = parse_patterns(resp)
    if pats:
        logger.info(f"chapter_llm：LLM 归纳出 {len(pats)} 条章节标题正则")
    else:
        logger.info("chapter_llm：LLM 未给出有效正则（或样本无章节标记），用纯正则切分")
    return pats
