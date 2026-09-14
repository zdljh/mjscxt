"""
小说 → A 版剧本转换器

核心思路（长篇小说必须分块，但**不得删减原小说内容**，只做小说→剧本的体裁改写）：
1. 分块：优先按章节把全文聚合成约 CHUNK_CHARS 字的块；无章节则按段落聚合
2. 全量覆盖：分块只用于控制单次 prompt 体量，**所有块都必须产出结果并合并**，
   块与块连续无缝、不跳段、不抽样（旧的「首尾保留 + 中间均匀抽样」逻辑已取消）
3. 三段式生成：
   ① 逐块提炼（每块一次调用）→ 剧情摘要 / 人物 / 物品 / 场景 / 情节要点
   ② 汇总设定（一次调用）→ title / theme / style / characters[] / items[] / scenes[]（含参考图提示词）
   ③ 逐块写分镜（每块一次调用）→ shots[]（A 版字段，含 prompt_h3 草稿）
4. 组装为系统 A 版剧本 Schema 并落盘 output/scripts
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime

from llm_client import LLMError, LLMTruncatedError
from dialogue_utils import dialogue_text as _dlg_text, normalize_lines as _dlg_lines

logger = logging.getLogger(__name__)

CHUNK_CHARS = 3000
MAX_CHUNKS = 8
MIN_CHUNK_CHARS = 300
MAX_CHARS_PER_CHUNK_PROMPT = 3400
COVERAGE_MAX_ROUNDS = 3          # 原文覆盖率不足时自动补生成轮次上限（与 continuity.COVERAGE_MAX_ROUNDS 对齐）
COVERAGE_THRESHOLD = 0.95        # 原文覆盖率阈值（coverage.py 读取，缺省同值）

SHOT_FIELDS_DEFAULT = {
    "episode": 1,
    "duration": 5,
    "camera": "中景",
    "location": "",
    "description": "",
    "dialogue": [],
    "dialogue_text": "",
    "emotion": "平静",
    "audio_cues": "",
    "prompt_h3": "",
    "characters_in_shot": [],
    "items_in_shot": [],
}

SYSTEM_BIBLE = ("你是资深漫剧编剧与 AI 绘画提示词工程师，精通把长篇小说改编成可拍摄的漫剧分镜脚本，"
                "并输出严格合法的 JSON。严禁在 content 中输出任何思考过程、英文推理、分析或解释文字，"
                "只允许输出一个可被 json.loads 直接解析的 JSON 对象。")

# ===================== 全量覆盖策略常量（改编 ≠ 缩写，严禁删减原文） =====================

CHARS_PER_SHOT = 120          # 每个镜头承载的原文字数基准（镜头数随内容体量自动扩展，收紧以承载细节）
SHOTS_PER_CHUNK_MIN = 6       # 单块分镜数下限（再短的块也至少这么多镜）
COVERAGE_THRESHOLD = 0.95     # 原文覆盖率阈值：低于该值自动补生成缺失片段
REWRITE_RULES = (
    "【改写规则（这是改编，不是缩写：严禁删减原文内容）】\n"
    "1) 原文的叙述、心理描写、场景描写、对话、人物动作必须全部落到镜头里，"
    "分别体现为画面描述（description）、台词（dialogue）、旁白/画外音（audio_cues 标注「旁白」）、"
    "动作与情绪（emotion）；\n"
    "2) 允许体裁形式改写：心理活动可改为旁白或自语台词，叙述可改为画面动作描述，"
    "环境描写可改为画面与音效，但不得改变情节、不得删减人物；\n"
    "3) 严禁删除情节、删除人物、跳过段落、合并概括、只挑重点写；"
    "原文内容越多，镜头就要越多（约每 {chars_per_shot} 字 1 个镜头，情节密集处更多）；\n"
    "4) 原文对话尽量原样写进对应角色的 dialogue.text，禁止改写成概括式引述；\n"
    "5) 镜头按原文时间顺序排列，块首镜头自然衔接上一块结尾，不得跳段、不得重复；\n"
    "6) 【细节零删减·最高优先级】原文单句内的修饰细节（外貌、衣着、神态、动作过程、心理活动、"
    "环境与光线、器物声响）都必须落到镜头里：外貌/器物/环境写进 description（画面描述），"
    "心理活动与背景补叙写进 narration（旁白，原句措辞优先），动作过程写进 description，"
    "对话写进 dialogue；\n"
    "7) 【措辞尽量原样】承载细节时优先沿用原文措辞（如原文「双亲已经亡故，是方之一脉仅剩下的双孤之一」"
    "就照此措辞写进旁白或画面描述），只做体裁转换与必要的镜头化补白，严禁改写成笼统概括；\n"
    "8) 自检：写完一块后逐句回看原文，确认每一句（含背景补叙、过渡句、环境句）都能在某条镜头的 "
    "description / picture / dialogue / audio_cues 中找到对应承载，不允许出现「没写到」的句子。"
)


# ===================== 分块与全量覆盖 =====================

def build_chunks(text: str, chapters: list, chunk_chars: int = CHUNK_CHARS) -> list:
    """把全文切成若干块（优先按章节边界聚合）"""
    text = text or ""
    if not text.strip():
        return []

    chunks = []
    if chapters:
        buf_text, buf_start, buf_chars, from_ch, to_ch = [], None, 0, None, None
        for ch in chapters:
            seg = text[ch["start"]:ch["end"]]
            if buf_start is None:
                buf_start, from_ch = ch["start"], ch["index"]
            buf_text.append(seg)
            buf_chars += len(seg)
            to_ch = ch["index"]
            if buf_chars >= chunk_chars:
                chunks.append({
                    "text": "".join(buf_text).strip(),
                    "char_count": buf_chars,
                    "from_chapter": from_ch,
                    "to_chapter": to_ch,
                })
                buf_text, buf_start, buf_chars = [], None, 0
        if buf_text and buf_chars > 0:
            chunks.append({
                "text": "".join(buf_text).strip(),
                "char_count": buf_chars,
                "from_chapter": from_ch,
                "to_chapter": to_ch,
            })
    else:
        paras = [p for p in re.split(r"\n{1,}", text)]
        buf, buf_chars = [], 0
        for p in paras:
            buf.append(p)
            buf_chars += len(p) + 1
            if buf_chars >= chunk_chars:
                chunks.append({"text": "\n".join(buf).strip(), "char_count": buf_chars,
                               "from_chapter": None, "to_chapter": None})
                buf, buf_chars = [], 0
        if buf:
            chunks.append({"text": "\n".join(buf).strip(), "char_count": buf_chars,
                           "from_chapter": None, "to_chapter": None})

    # 零丢弃：过短的块并入相邻块（原实现用过滤丢弃，会把整章/整段正文静默舍弃）
    if len(chunks) >= 2:
        merged = []
        for c in chunks:
            if merged and len(c["text"]) < MIN_CHUNK_CHARS:
                prev = merged[-1]
                prev["text"] = (prev["text"] + "\n" + c["text"]).strip()
                prev["char_count"] += c["char_count"]
                prev["to_chapter"] = c.get("to_chapter") or prev.get("to_chapter")
            else:
                merged.append(c)
        if len(merged) >= 2 and len(merged[0]["text"]) < MIN_CHUNK_CHARS:
            # 首块过短：并入后一块，保证正文开头不被丢弃
            merged[1]["text"] = (merged[0]["text"] + "\n" + merged[1]["text"]).strip()
            merged[1]["char_count"] += merged[0]["char_count"]
            merged[1]["from_chapter"] = (merged[0].get("from_chapter")
                                         or merged[1].get("from_chapter"))
            merged.pop(0)
        chunks = merged
    for i, c in enumerate(chunks):
        c["index"] = i + 1
        c["total"] = len(chunks)
        c["title"] = _chunk_title(c)
    return chunks


def _chunk_title(c: dict) -> str:
    first = (c.get("text") or "").split("\n")[0].strip()
    if c.get("from_chapter"):
        if c["from_chapter"] == c.get("to_chapter"):
            return f"第{c['from_chapter']}章"
        return f"第{c['from_chapter']}-{c['to_chapter']}章"
    return first[:24] or f"第{c['index']}块"


def sample_chunks(chunks: list, max_chunks: int = MAX_CHUNKS):
    """均匀抽样（必含首块与末块），返回 (抽样块列表, 抽样下标 1-based)"""
    n = len(chunks)
    if n <= max_chunks:
        return list(chunks), [c["index"] for c in chunks]
    step = (n - 1) / (max_chunks - 1)
    idxs = sorted({int(round(i * step)) for i in range(max_chunks)})
    idxs[0], idxs[-1] = 0, n - 1
    if len(idxs) > max_chunks:  # 极端情况去重后再补
        idxs = idxs[:max_chunks - 1] + [n - 1]
    return [chunks[i] for i in idxs], [chunks[i]["index"] for i in idxs]


# ===================== 三段式生成 =====================

def _as_dict(data) -> dict:
    """模型返回容错：兼容部分模型把 JSON 对象包在数组里返回（[{"...": ...}]）的情况。"""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                return it
    return {}


def _bare_list_to_bible(rows) -> dict:
    """模型只返回了某个数组（未包成完整对象）时的兜底归位。

    按首元素的字段特征判断该数组属于 characters / items / scenes 中的哪一类，
    避免整链路因为“少了一层对象”而中断。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {}
    sample = rows[0]
    if any(k in sample for k in ("category", "owner")):
        return {"items": rows}
    if "location" in sample and "appearance" in sample:
        return {"scenes": rows}
    return {"characters": rows}


def _normalize_bible(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return _bare_list_to_bible(raw)
    return {}


# ===================== 截断自适应（A 项③） =====================
# 单块送模型时若输出被截断（finish_reason=length），除了自动提高 max_tokens 重试外，
# 仍失败则把该块再二分（最多 CHAPTER_SPLIT_MAX_DEPTH 层）后分别生成再合并，保证不静默失败。
CHAPTER_SPLIT_MAX_DEPTH = 3
CHAPTER_SPLIT_MIN_CHARS = 400     # 低于该字数的子块不再继续二分（避免无意义碎片）


def _robust_json(client, prompt: str, system: str, temperature: float, max_tokens: int,
                 events: list = None, label: str = "",
                 max_attempts: int = 3, token_ladder=None) -> dict:
    """统一 JSON 调用入口：走 chat_json_robust（截断自动提额重试）并记录重试事件

    max_attempts / token_ladder 可按阶段覆盖默认重试策略（如 bible 汇总阶段输出更长，
    需要更高的提额上限与更多尝试次数）。
    """
    robust = getattr(client, "chat_json_robust", None)
    if robust is None:                      # 兼容旧客户端
        return client.chat_json(prompt, system=system, temperature=temperature,
                                max_tokens=max_tokens)
    kw = {}
    if token_ladder:
        kw["token_ladder"] = tuple(token_ladder)
    if max_attempts:
        kw["max_attempts"] = int(max_attempts)

    def _on_event(h):
        if events is not None and int(h.get("attempt") or 1) > 1:
            events.append({"label": label, "attempt": h.get("attempt"),
                           "max_tokens": h.get("max_tokens"),
                           "finish_reason": h.get("finish_reason"),
                           "truncated": bool(h.get("truncated"))})

    try:
        return robust(prompt, system=system, temperature=temperature,
                      max_tokens=max_tokens, on_event=_on_event, **kw)
    finally:
        meta = getattr(client, "last_json_meta", None)
        if events is not None and isinstance(meta, dict) and int(meta.get("attempts") or 0) > 1:
            seen = {(e.get("label"), e.get("attempt")) for e in events}
            if (label, meta.get("attempts")) not in seen:
                events.append({"label": label, "attempt": meta.get("attempts"),
                               "max_tokens": meta.get("max_tokens"),
                               "finish_reason": meta.get("finish_reason"),
                               "truncated": bool(meta.get("truncated"))})


def _split_chunk_in_half(chunk: dict) -> list:
    """把子块按中点附近的句末标点一分为二（保证不丢字），用于截断后的进一步切分"""
    text = str(chunk.get("text") or "")
    if len(text) < CHAPTER_SPLIT_MIN_CHARS * 2:
        return []
    mid = len(text) // 2
    cut = mid
    for m in re.finditer(r"[。！？；!?;\n]", text):
        if m.end() >= mid:
            cut = m.end()
            break
    parts = [text[:cut].strip(), text[cut:].strip()]
    out = []
    for i, p in enumerate(parts):
        if len(p) < CHAPTER_SPLIT_MIN_CHARS // 2:
            continue
        out.append({**chunk, "text": p, "char_count": len(p),
                    "title": f"{chunk.get('title') or '块'}·{i + 1}",
                    "_split_from": chunk.get("title")})
    return out if len(out) == 2 else []


def _merge_outlines(outlines: list, chunk: dict) -> dict:
    """把同一原始子块的多个子-提炼结果合并为一个 outline（人物/物品/场景按名去重）"""
    merged = {"summary": "", "characters": [], "items": [], "scenes": [], "key_beats": []}
    summaries = []
    seen = {k: set() for k in ("characters", "items", "scenes")}
    for o in outlines:
        if not isinstance(o, dict):
            continue
        if o.get("summary"):
            summaries.append(str(o["summary"]))
        for key in ("characters", "items", "scenes"):
            for row in (o.get(key) or []):
                if not isinstance(row, dict):
                    continue
                nm = str(row.get("name") or "").strip()
                if not nm or nm in seen[key]:
                    continue
                seen[key].add(nm)
                merged[key].append(row)
        for b in (o.get("key_beats") or []):
            if str(b).strip():
                merged["key_beats"].append(str(b))
    merged["summary"] = "；".join(summaries)[:200]
    # 全量覆盖：情节要点不再压到 5 条（避免把长块内容压缩丢失），容纳整段全部节点
    merged["key_beats"] = merged["key_beats"][:14]
    merged["_chunk"] = {"index": chunk.get("index"), "title": chunk.get("title"),
                        "char_count": len(str(chunk.get("text") or "")), "sub_split": True}
    return merged


def extract_chunk_outline(client, chunk: dict, novel_title: str,
                          events: list = None, depth: int = 0) -> dict:
    """① 单块提炼（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）"""
    body = chunk["text"][:MAX_CHARS_PER_CHUNK_PROMPT]
    prompt = f"""【任务】下面是长篇小说《{novel_title}》的第 {chunk['index']}/{chunk['total']} 段原文（{chunk.get('title')}，约 {len(body)} 字），请提炼改编漫剧所需的辅助信息（人物 / 物品 / 场景 / 剧情摘要 / 关键情节节点）。本提炼只作分镜阶段的辅助索引：分镜阶段会拿到本段完整原文，因此这里**不需要逐句复述原文**，但也不得删改原意。
【原文开始】
{body}
【原文结束】
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字，结构如下：
{{
  "summary": "本段剧情摘要，120 字以内",
  "characters": [{{"name": "人物名", "role": "主角/配角/反派", "appearance": "外貌与服装，35 字以内", "personality": "性格，20 字以内"}}],
  "items": [{{"name": "物品名", "category": "武器/法宝/道具/服饰", "appearance": "外观，30 字以内"}}],
  "scenes": [{{"name": "场景名", "location": "地点类型", "appearance": "环境特征，30 字以内"}}],
  "key_beats": ["按原文顺序列出本段关键情节节点，每条 30 字以内，最多 12 条（分镜阶段会读取完整原文，这里只做索引，不要逐句复述、不要写成英文）"]
}}"""
    label = f"outline#{chunk.get('index')}"
    try:
        data = _as_dict(_robust_json(client, prompt, system=SYSTEM_BIBLE, temperature=0.35,
                                     max_tokens=3000, events=events, label=label,
                                     max_attempts=3, token_ladder=(4096, 8192, 16384)))
    except LLMTruncatedError:
        subs = _split_chunk_in_half(chunk) if depth < CHAPTER_SPLIT_MAX_DEPTH else []
        if not subs:
            raise LLMTruncatedError(
                f"{label}（{chunk.get('title')}，{len(chunk.get('text') or '')} 字）"
                f"在自动提高 max_tokens 后仍被截断，且已无法继续二分（depth={depth}）") from None
        logger.warning(f"{label} 提炼输出被截断，自动二分为 {len(subs)} 个子块重试（depth={depth}）")
        if events is not None:
            events.append({"label": label, "event": "sub_split", "depth": depth,
                           "parts": [len(s.get("text") or "") for s in subs]})
        return _merge_outlines(
            [extract_chunk_outline(client, s, novel_title, events, depth + 1) for s in subs], chunk)
    data["_chunk"] = {
        "index": chunk["index"],
        "title": chunk.get("title"),
        "char_count": len(body),
    }
    return data


def _ctx_block(ctx, key: str) -> str:
    """取跨集连贯性上下文块（A/B/C 方案的 prompt 注入片段），无则返回空串"""
    if not isinstance(ctx, dict):
        return ""
    val = ctx.get(key)
    return str(val).strip() if val else ""


def _ctx_line(ctx, key: str) -> str:
    v = _ctx_block(ctx, key)
    return (v + "\n") if v else ""


def build_bible(client, outlines: list, novel_title: str, style: str, episodes: int,
                target_shots: int, events: list = None, continuity_ctx: dict = None) -> dict:
    """② 汇总全剧设定，产出 A 版 characters/items/scenes

    continuity_ctx 非空时（跨集连贯性方案 A①②③）：注入项目级设定库（角色外观锁定）、
    上集摘要卡与衔接契约，使本集设定与既有跨集设定保持一致。
    """
    digest = []
    for o in outlines:
        if not isinstance(o, dict):
            continue
        digest.append({
            "段": o.get("_chunk", {}).get("index"),
            "摘要": o.get("summary", ""),
            "人物": [{"name": c.get("name"), "role": c.get("role"), "appearance": c.get("appearance")}
                     for c in (o.get("characters") or [])[:6] if isinstance(c, dict)],
            "物品": [{"name": i.get("name"), "category": i.get("category"), "appearance": i.get("appearance")}
                     for i in (o.get("items") or [])[:6] if isinstance(i, dict)],
            "场景": [{"name": s.get("name"), "appearance": s.get("appearance")}
                     for s in (o.get("scenes") or [])[:6] if isinstance(s, dict)],
            "情节要点": (o.get("key_beats") or [])[:5],
        })
    prompt = f"""【任务】以下是长篇小说《{novel_title}》各段落的提炼结果（JSON）。请把它们整合成一份可直接用于漫剧生产的「全剧设定集」。
【风格要求】{style}
【集数】{episodes} 集  【预计总镜头数】{target_shots}
【分段提炼结果】
{json.dumps(digest, ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{{
  "title": "剧名（4-12 字）",
  "theme": "一句话主题/卖点（30 字以内）",
  "style": "{style}",
  "characters": [{{"name": "姓名", "age": "年龄", "identity": "身份/阵营（15 字以内）", "appearance": "外貌（含发色/瞳色/标志特征，60 字以内；若上方设定库已锁定则该字段必须与锁定值逐字一致）", "outfit": "本集服装状态（20 字以内，与上集结尾一致；若本集确有换装必须体现原因）", "personality": "性格（30 字以内）", "voice_style": "配音风格（15 字以内）", "reference_prompt_zh": "中文参考图提示词：角色三视图设定图描述，60 字以内", "reference_prompt_en": "English prompt for character reference sheet, under 40 words"}}],
  "items": [{{"name": "物品名", "category": "武器/法宝/道具/服饰", "appearance": "外观（50 字以内）", "owner": "持有人", "reference_prompt_zh": "中文参考图提示词，50 字以内", "reference_prompt_en": "English prompt, under 35 words"}}],
  "scenes": [{{"name": "场景名", "location": "地点类型", "appearance": "环境与氛围（60 字以内）", "reference_prompt_zh": "中文参考图提示词，50 字以内", "reference_prompt_en": "English prompt, under 35 words"}}],
  "production_notes": {{"style_guide": "画面与叙事风格说明（60 字以内）"}}
}}
【硬性约束】characters 最多 6 个（只保留主要角色，按戏份排序）；items 最多 5 个；scenes 最多 6 个；不要输出示例里的占位文字。若上方提供了「项目级设定库」，则已登记角色的 name / appearance / personality 必须与该库完全一致（禁止改名、禁止改外观），只允许更新 outfit（当前服装状态）。
【格式红线】直接以 {{ 作为输出的第一个字符；严禁输出任何推理过程、思考草稿、英文说明、markdown 代码块标记或前后缀解释文字；整个 JSON 输出控制在 1200 字以内（字段描述能短则短）。"""
    bible_retry_kw = {"max_attempts": 4, "token_ladder": (6000, 8192, 16384, 24576)}
    data = {}
    last_raw = None
    for tag, p in (("bible", prompt),
                   ("bible-repair", prompt + "\n\n【重要·格式修复】上一次调用未产出完整合规 JSON。"
                    "请重新输出**一个完整、紧凑的 JSON 对象**，必须同时包含 characters、items、scenes、"
                    "production_notes 四个键，不要只输出其中某个数组，不要输出任何解释文字。"),
                   ("bible-slim", prompt + "\n\n【重要·精简模式】模型输出连续被截断。请只保留最核心信息："
                    "characters 最多 4 个（name / identity / appearance / outfit 四个字段，每个不超过 20 字），"
                    "items 最多 3 个（name / category / appearance），scenes 最多 3 个（name / appearance），"
                    "其余字段全部省略。直接输出 JSON，不要解释。")):
        try:
            raw = _robust_json(client, p, system=SYSTEM_BIBLE, temperature=0.3, max_tokens=6000,
                               events=events, label=tag, **bible_retry_kw)
        except LLMError as e:
            logger.warning(f"bible 阶段 {tag} 调用失败：{e}")
            continue
        last_raw = raw
        data = _normalize_bible(raw)
        if data.get("characters"):
            return data
        logger.warning(f"bible {tag} 返回缺少 characters（原类型 {type(raw).__name__}）：{str(raw)[:200]}")

    # 兜底：模型连续失败时用分块提炼结果做确定性聚合，保证整集生成不中断
    logger.warning("bible 汇总失败，降级为分块设定确定性聚合（未使用模型汇总）")
    data = _fallback_bible(outlines, novel_title, style)
    if events is not None:
        events.append({"label": "bible-fallback", "attempt": 0, "max_tokens": 0,
                       "finish_reason": "n/a", "truncated": False,
                       "note": "模型汇总连续失败，已降级为分块设定聚合"})
    return data


def _fallback_bible(outlines: list, novel_title: str, style: str) -> dict:
    """bible 兜底：从各分块提炼结果确定性聚合角色 / 物品 / 场景（不调用模型）"""
    chars, items, scenes = {}, {}, {}

    def _pick(src: dict, key: str, fields: list) -> None:
        name = str(src.get("name") or "").strip()
        if not name or name in key:
            return
        key[name] = {"name": name, **{f: str(src.get(f) or "") for f in fields}}

    for o in outlines or []:
        if not isinstance(o, dict):
            continue
        for c in (o.get("characters") or [])[:6]:
            if isinstance(c, dict):
                _pick(c, chars, ["identity", "appearance", "outfit", "personality", "voice_style"])
        for i in (o.get("items") or [])[:6]:
            if isinstance(i, dict):
                _pick(i, items, ["category", "appearance", "owner"])
        for s in (o.get("scenes") or [])[:6]:
            if isinstance(s, dict):
                _pick(s, scenes, ["location", "appearance"])
    return {
        "title": (novel_title or "")[:20],
        "theme": "",
        "style": style,
        "characters": list(chars.values())[:6],
        "items": list(items.values())[:5],
        "scenes": list(scenes.values())[:6],
        "production_notes": {"style_guide": style},
        "_degraded": True,
    }


def build_shots_for_chunk(client, bible: dict, outline: dict, chunk: dict, shots_target: int,
                          events: list = None, depth: int = 0,
                          continuity_ctx: dict = None) -> list:
    """③ 单块写分镜（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）

    continuity_ctx 非空时（跨集连贯性方案 A②③ / C⑦⑧）：注入上集摘要卡、衔接契约、
    项目级风格指南、人物口吻词典、金句保留清单与运镜术语表。
    """
    char_brief = [
        {"name": c.get("name"), "appearance": (c.get("appearance") or "")[:40]}
        for c in (bible.get("characters") or [])[:6] if isinstance(c, dict)
    ]
    item_brief = [{"name": i.get("name"), "appearance": (i.get("appearance") or "")[:30]}
                  for i in (bible.get("items") or [])[:5] if isinstance(i, dict)]
    scene_brief = [{"name": s.get("name"), "appearance": (s.get("appearance") or "")[:40]}
                   for s in (bible.get("scenes") or [])[:6] if isinstance(s, dict)]
    shots_cap = max(int(shots_target), min(120, int(shots_target) * 3 + 6))
    prompt = f"""【任务】为漫剧《{bible.get('title') or ''}》的「{chunk.get('title')}」（第 {chunk['index']}/{chunk['total']} 段）编写分镜：至少 {shots_target} 个、上限 {shots_cap} 个，必须完整承载下方原文的全部情节。
{REWRITE_RULES.format(chars_per_shot=CHARS_PER_SHOT)}
【全剧风格】{bible.get('style') or ''}　【画面风格指南】{_ctx_block(continuity_ctx, 'style_guide_text') or (bible.get('production_notes') or {}).get('style_guide') or ''}
{_ctx_line(continuity_ctx, 'prev_block')}{_ctx_line(continuity_ctx, 'bible_block')}{_ctx_line(continuity_ctx, 'contract_block')}{_ctx_line(continuity_ctx, 'style_block')}{_ctx_line(continuity_ctx, 'camera_block')}【可用角色】{json.dumps(char_brief, ensure_ascii=False)}
【可用物品】{json.dumps(item_brief, ensure_ascii=False)}
【可用场景】{json.dumps(scene_brief, ensure_ascii=False)}
【本段原文（必须逐句改写成镜头/台词/旁白/画面描述，严禁删减或概括压缩）】
{chunk.get('text') or ''}
【本段剧情摘要】{outline.get('summary', '')}
【本段情节要点】{json.dumps(outline.get('key_beats') or [], ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{{"shots": [{{"camera": "景别+运镜（必须取自上方运镜术语表，如 中景跟拍/特写推入，10 字以内）", "location": "所属场景名（必须来自可用场景）", "description": "画面内容描述（80 字以内，写清人物动作、外貌衣着、环境光线与画面构图，尽量沿用原文措辞）", "narration": "旁白文本（承载原文的心理活动/背景补叙/环境描写，尽量照原文措辞，60 字以内；无则空字符串）", "dialogue": [{{"speaker": "说话角色名（必须与可用角色完全一致）", "text": "该角色台词（≤60 字，原文对话尽量原样保留）"}}], "emotion": "情绪（8 字以内）", "audio_cues": "音效/配乐提示（60 字以内；旁白请以「旁白:」开头）", "characters_in_shot": ["出场角色名"], "items_in_shot": ["出场物品名"], "prompt_h3": "英文画面描述（60 词以内，描述主体、动作、环境、光线、运镜）"}}]}}
【台词要求】dialogue 必须是数组，数组元素为 {{"speaker": 角色名, "text": 台词}}；speaker 必须精确等于「可用角色」中的名字，禁止写“旁白/众人”等未登记角色；无台词的镜头 dialogue 写 []（空数组），禁止写成字符串或 null。
【硬性约束】shots 数组元素个数必须在 {shots_target} ~ {shots_cap} 之间：上方原文的全部情节都要落到镜头里，不得删减情节、不得跳过段落、不得合并概括（内容多时用更多镜头承载，而不是少写镜头）；name 字段必须与上面「可用角色/物品/场景」中的名字完全一致，不要新造名字。若上方给出「本集必须出现的原文金句」，必须把每句**原样**写进对应角色的 dialogue.text（不得改写、不得拆分、不得省略）。上一集已发生的事件禁止在本集重演。
【逐句归属自检（细节零删减）】逐句回看原文，确保每一句（含背景补叙、过渡句、环境句）都落在某条镜头的 description / narration / dialogue / audio_cues 里；短句可合并到相邻镜头，但不得整句丢弃。心理活动与背景补叙优先用 narration 承载，并尽量保留原句措辞。"""
    label = f"shots#{chunk.get('index')}"
    try:
        data = _robust_json(client, prompt, system=SYSTEM_BIBLE, temperature=0.6,
                            max_tokens=max(4096, min(16000, int(shots_target) * 300 + 1200)),
                            events=events, label=label,
                            max_attempts=4,
                            token_ladder=(8192, 12288, 16384, 24576, 32768))
    except LLMTruncatedError:
        subs = _split_chunk_in_half(chunk) if depth < CHAPTER_SPLIT_MAX_DEPTH else []
        if not subs or int(shots_target) <= 1:
            raise LLMTruncatedError(
                f"{label}（{chunk.get('title')}，{len(chunk.get('text') or '')} 字，目标 {shots_target} 镜）"
                f"在自动提高 max_tokens 后仍被截断，且已无法继续二分（depth={depth}）") from None
        logger.warning(f"{label} 分镜输出被截断，自动二分为 {len(subs)} 个子块重试（depth={depth}）")
        if events is not None:
            events.append({"label": label, "event": "sub_split", "depth": depth,
                           "parts": [len(s.get("text") or "") for s in subs]})
        per = max(1, int(round(int(shots_target) / float(len(subs)))))
        beats = list(outline.get("key_beats") or [])
        merged = []
        for i, s in enumerate(subs):
            sub_outline = dict(outline)
            if beats:
                n = len(beats)
                a = i * n // len(subs)
                b = max(a + 1, (i + 1) * n // len(subs))
                sub_outline["key_beats"] = beats[a:b]
            merged.extend(build_shots_for_chunk(client, bible, sub_outline, s, per,
                                                events=events, depth=depth + 1,
                                                continuity_ctx=continuity_ctx))
        return merged
    if isinstance(data, dict):
        shots = data.get("shots")
    elif isinstance(data, list):
        # 兜底：模型直接把 shots 数组作为顶层返回
        shots = [x for x in data if isinstance(x, dict)
                 and any(k in x for k in ("description", "camera", "prompt_h3"))] or None
    else:
        shots = None
    if not isinstance(shots, list):
        return []
    return [s for s in shots if isinstance(s, dict)]


def _fallback_shots_for_chunk(chunk: dict, shots_target: int = 1, bible: dict = None) -> list:
    """分镜阶段模型失败时的兜底：按原文逐句生成「原文承载镜头」，保证该段内容不丢。

    与覆盖率补生成同源（只增不删）：description / narration 直接沿用原文措辞，
    标记 fallback=True 供前端提示需人工润色。
    """
    text = str((chunk or {}).get("text") or "")
    if not text.strip():
        return []
    bible = bible or {}
    chars = [c.get("name") for c in (bible.get("characters") or [])
             if isinstance(c, dict) and c.get("name")]
    scenes = [s.get("name") for s in (bible.get("scenes") or [])
              if isinstance(s, dict) and s.get("name")]
    total = int(chunk.get("char_count") or len(text))
    per = max(CHARS_PER_SHOT, total // max(1, int(shots_target)))
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?…；;])\s*|\n+", text) if s.strip()]
    if not sentences:
        sentences = [text.strip()]
    groups, buf = [], ""
    for s in sentences:
        if buf and len(buf) + len(s) > per:
            groups.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        groups.append(buf)
    out = []
    for g in groups:
        out.append({
            "camera": "中景", "location": (scenes[0] if scenes else ""),
            "description": g[:200], "narration": g[:200],
            "dialogue": [], "emotion": "平静", "audio_cues": "",
            "characters_in_shot": chars[:1], "items_in_shot": [], "prompt_h3": "",
            "fallback": True,
            "fallback_reason": f"第 {chunk.get('index')} 段模型分镜失败，按原文逐句承载",
        })
    return out


# ===================== 组装 =====================

def _norm_list(items, limit, keys):
    out = []
    for it in (items or [])[:limit]:
        if not isinstance(it, dict):
            continue
        row = {}
        for k in keys:
            v = it.get(k)
            row[k] = "" if v is None else (v if isinstance(v, (int, float)) else str(v))
        if any(str(row.get(k, "")).strip() for k in keys):
            out.append(row)
    return out


# ===================== 剧本 Schema：镜头数 / 每集时长（自动判定） =====================
# AI 转剧本阶段自动判定并写入，后续分镜 / 视频 / 配音链路直接引用，无需人工配置。
SHOT_DURATION_MIN = 3.0          # 单镜头最短秒数
SHOT_DURATION_MAX = 12.0         # 单镜头最长秒数
SHOT_DURATION_SILENT = 3.0       # 无台词的纯画面镜头基准秒数
CHARS_PER_SECOND = 4.5           # 中文配音语速基准（字/秒），用于按台词长度推算镜头时长


def estimate_shot_duration(shot: dict) -> float:
    """按画面 + 台词长度自动推算单镜头时长（秒），保证同一剧本多次运行结果稳定。

    台词兼容两种写法：结构化 [{"speaker","text"}] / 旧字符串（含 "角色名：台词" 前缀）。
    """
    dialogue = _dlg_text(shot.get("dialogue"))
    dialogue = re.sub(r"^[^：:]{1,12}[：:]", "", dialogue)          # 去掉“角色名：”前缀
    speak_sec = len(dialogue) / CHARS_PER_SECOND if dialogue else 0.0
    desc_sec = min(2.0, len(str(shot.get("description") or "")) / 60.0)
    raw = SHOT_DURATION_SILENT + speak_sec + desc_sec
    value = max(SHOT_DURATION_MIN, min(SHOT_DURATION_MAX, raw))
    return round(value * 2) / 2.0                                    # 取整到 0.5 秒


def build_episode_stats(shots: list) -> dict:
    """由分镜列表生成「镜头数 / 每集时长（秒）」统计（含分集明细 episode_plan）。"""
    per_ep: dict = {}
    for sh in shots or []:
        if not isinstance(sh, dict):
            continue
        ep = int(sh.get("episode") or 1)
        per_ep.setdefault(ep, []).append(sh)

    plan = []
    for ep in sorted(per_ep):
        rows = per_ep[ep]
        total = round(sum(float(s.get("duration") or SHOT_DURATION_SILENT) for s in rows), 2)
        plan.append({
            "episode_no": ep,
            "shot_count": len(rows),
            "duration_sec": total,
            "duration_per_shot_sec": round(total / len(rows), 2) if rows else 0.0,
            "shot_ids": [s.get("shot_id") for s in rows],
        })

    total_shots = sum(r["shot_count"] for r in plan)
    total_sec = round(sum(r["duration_sec"] for r in plan), 2)
    primary = plan[0] if len(plan) == 1 else None
    return {
        "shot_count": primary["shot_count"] if primary else total_shots,
        "duration_sec": primary["duration_sec"] if primary else total_sec,
        "duration_per_shot_sec": (primary["duration_per_shot_sec"] if primary
                                  else (round(total_sec / total_shots, 2) if total_shots else 0.0)),
        "episode_count": len(plan),
        "total_shot_count": total_shots,
        "total_duration_sec": total_sec,
        "episode_plan": plan,
    }


def apply_episode_schema(script: dict, duration_per_shot: float = None) -> dict:
    """把自动判定的镜头数 / 每集时长写入剧本 Schema（顶层 + production_notes + metadata）。

    - 顶层：shot_count / episode_duration_sec / duration_per_shot_sec / episode_plan
    - production_notes：total_shots / estimated_duration（兼容旧字段）+ 新字段
    - metadata：episode_stats（供前端与下游链路直接读取）
    """
    shots = script.get("shots") or []
    if duration_per_shot:
        for sh in shots:
            if isinstance(sh, dict):
                sh["duration"] = round(float(duration_per_shot) * 2) / 2.0
    stats = build_episode_stats(shots)
    script["shot_count"] = stats["shot_count"]
    script["episode_duration_sec"] = stats["duration_sec"]
    script["duration_per_shot_sec"] = stats["duration_per_shot_sec"]
    script["episode_plan"] = stats["episode_plan"]

    notes = script.setdefault("production_notes", {})
    notes["shot_count"] = stats["shot_count"]
    notes["episode_duration_sec"] = stats["duration_sec"]
    notes["total_shots"] = stats["total_shot_count"]
    notes["estimated_duration"] = stats["total_duration_sec"]
    notes["episode_plan"] = stats["episode_plan"]

    meta = script.setdefault("metadata", {})
    meta["episode_stats"] = stats
    meta["shot_count"] = stats["shot_count"]
    meta["episode_duration_sec"] = stats["duration_sec"]
    return script


def build_chapter_coverage_meta(shots: list, chars_per_shot: int = CHARS_PER_SHOT) -> dict:
    """按「镜头数 × 每镜承载字数」估算内容承载量（覆盖率校验前的预估值）

    仅用于覆盖率校验未执行时给出「预计承载字数 / 是否可能遗漏」的提示；
    真实覆盖率以 coverage.py 的逐句校验结果为准。
    """
    n = len([s for s in (shots or []) if isinstance(s, dict)])
    capacity = n * int(chars_per_shot or CHARS_PER_SHOT)
    return {"shots": n, "chars_per_shot": int(chars_per_shot or CHARS_PER_SHOT),
            "capacity_chars": capacity, "verified": False}


def _norm_shots(raw_shots: list, bible: dict, episodes: int, start_id: int = 1) -> list:
    scenes = [s.get("name") for s in (bible.get("scenes") or []) if isinstance(s, dict)]
    chars = [c.get("name") for c in (bible.get("characters") or []) if isinstance(c, dict)]
    items = [i.get("name") for i in (bible.get("items") or []) if isinstance(i, dict)]
    shots = []
    sid = start_id
    for s in raw_shots:
        if not isinstance(s, dict):
            continue
        loc = str(s.get("location") or "").strip()
        if scenes and loc and loc not in scenes:
            loc = next((n for n in scenes if n and n in loc), scenes[0])
        if not loc and scenes:
            loc = scenes[0]
        row = {
            "shot_id": sid,
            "duration": 5,
            "camera": str(s.get("camera") or "中景").strip()[:20] or "中景",
            "location": loc,
            "description": str(s.get("description") or "").strip()[:200],
            # 旁白：承载原文心理/背景补叙/环境描写的原句措辞（只做体裁改写，不删减）
            "narration": str(s.get("narration") or "").strip()[:200],
            # 台词：结构化 [{"speaker","text"}]（分镜阶段直接写明说话人，配音链路直接读取）
            "dialogue": _dlg_lines(s.get("dialogue"), chars, chars),
            "emotion": str(s.get("emotion") or "平静").strip()[:20],
            "audio_cues": str(s.get("audio_cues") or "").strip()[:60],
            "prompt_h3": str(s.get("prompt_h3") or "").strip(),
            "characters_in_shot": [c for c in (s.get("characters_in_shot") or []) if c in chars] or chars[:1],
            "items_in_shot": [i for i in (s.get("items_in_shot") or []) if i in items],
        }
        # 覆盖率补生成镜头：保留其承载的原文单元编号，便于覆盖率校验与前端回溯
        src_ids = s.get("source_unit_ids")
        if isinstance(src_ids, (list, tuple)) and src_ids:
            row["source_unit_ids"] = [int(x) for x in src_ids
                                      if isinstance(x, (int, float))][:80]
        if s.get("supplement"):
            row["supplement"] = True
        # 模型失败后的原文兜底镜头：保留标记，前端可提示需人工润色
        if s.get("fallback"):
            row["fallback"] = True
            row["fallback_reason"] = str(s.get("fallback_reason") or "")[:120]
        # 兼容展示字段：台词纯文本（有说话人时 "角色：台词"），前端/提示词按需读取
        row["dialogue_text"] = " ".join(
            (f"{d['speaker']}：{d['text']}" if d.get("speaker") else d.get("text") or "")
            for d in row["dialogue"]
        ).strip()
        # 单镜头时长：模型显式给出且合法则采用，否则按台词长度自动判定
        model_dur = None
        try:
            model_dur = float(s.get("duration"))
        except (TypeError, ValueError):
            model_dur = None
        if model_dur and SHOT_DURATION_MIN <= model_dur <= SHOT_DURATION_MAX:
            row["duration"] = round(model_dur * 2) / 2.0
        else:
            row["duration"] = estimate_shot_duration(row)
        shots.append(row)
        sid += 1
    # 分配集数
    n = len(shots)
    if n:
        per = max(1, math.ceil(n / max(1, episodes)))
        for i, sh in enumerate(shots):
            sh["episode"] = min(episodes, i // per + 1)
    return shots


def _run_full_coverage_check(client, novel_text: str, script: dict, reports=None,
                             warnings: list = None, continuity_dir: str = None,
                             project_key: str = None, max_rounds: int = COVERAGE_MAX_ROUNDS,
                             total_steps: int = 0) -> dict:
    """整本剧本的原文覆盖率校验：逐句核对原文是否被镜头承载 → 遗漏补生成 → 复检。

    只增不删：补生成只向 shots 追加镜头，不改写、不替换既有镜头。
    校验失败时降级（记 warning 并沿用原覆盖率摘要），不阻断剧本落盘。
    """
    warnings = warnings if warnings is not None else []
    try:
        import coverage as coverage_mod
        if reports:
            reports("coverage", total_steps, total_steps,
                    f"原文覆盖率校验（逐句核对 {len(novel_text or '')} 字是否被镜头承载）…", 98)
        rep = coverage_mod.run_coverage_check(
            client, novel_text, script, episode_no=1, threshold=None,
            max_rounds=max_rounds, events=None,
            continuity_dir=continuity_dir, project_key=project_key, save=True)
        if rep.get("supplement_shots"):
            apply_episode_schema(script)   # 补生成镜头后刷新镜头数 / 时长
        if reports:
            reports("coverage", total_steps, total_steps,
                    f"原文覆盖率：情节级 {rep.get('plot_coverage_percent')}%、"
                    f"细节级 {rep.get('detail_coverage_percent')}%，遗漏 "
                    f"{rep.get('missing_count')} 条、补生成 {rep.get('supplement_shots')} 镜", 99)
        return coverage_mod.summary_for_meta(rep, rep.get("report_path")) \
            or (script.get("metadata") or {}).get("coverage") or {}
    except Exception as e:  # noqa: BLE001
        note = (f"原文覆盖率校验失败（已跳过，剧本仍按全量分块生成）："
                f"{type(e).__name__}: {str(e)[:200]}")
        warnings.append(note)
        logger.warning(note)
        return (script.get("metadata") or {}).get("coverage") or {}


def convert_novel_to_script(client, novel_meta: dict, novel_text: str, style: str = "3D动漫渲染",
                            episodes: int = 1, target_shots: int = 12, progress_cb=None,
                            check_coverage: bool = True, continuity_dir: str = None,
                            project_key: str = None,
                            coverage_max_rounds: int = COVERAGE_MAX_ROUNDS) -> dict:
    """完整转换流程，返回 A 版剧本 dict（含 metadata）

    check_coverage=True（默认）时，整本路径同样执行「原文覆盖率校验 → 遗漏自动补生成 →
    复检 → 报告落盘」，保证长篇小说既不删减原文，也不因分块而丢内容。
    """
    def report(phase, current, total, message, percent):
        if progress_cb:
            try:
                progress_cb(phase, current, total, message, percent)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"进度回调异常：{e}")

    t0 = time.time()
    novel_title = novel_meta.get("title") or novel_meta.get("name") or "未命名小说"
    chapters = novel_meta.get("chapters") or []
    chunks = build_chunks(novel_text, chapters)
    if not chunks:
        raise LLMError("小说正文为空，无法转换")
    # 全量覆盖：不再抽样，所有块都送模型（分块仅用于控制单次 prompt 体量）
    sampled = list(chunks)
    sampled_idx = [c["index"] for c in chunks]

    total_steps = len(sampled) + 2 + len(sampled)
    report("prepare", 0, total_steps,
           f"全文 {novel_meta.get('char_count', 0)} 字，切分为 {len(chunks)} 块，"
           f"全量覆盖送模型 {len(sampled)} 块（不抽样、不丢内容）",
           3)

    # ① 逐块提炼
    outlines, warnings = [None] * len(sampled), []
    for i, chunk in enumerate(sampled):
        report("outline", i + 1, total_steps, f"提炼第 {chunk['index']} 块（{chunk.get('title')}）…",
               int(5 + (i + 1) / total_steps * 55))
        try:
            outlines[i] = extract_chunk_outline(client, chunk, novel_title)
        except LLMError as e:
            # 大纲仅用于辅助提示，失败不丢原文（分镜阶段仍全量送该块正文）
            warnings.append(f"第 {chunk['index']} 块大纲提炼失败（改用空大纲，原文仍全量送模型）：{e}")
            logger.warning(f"第 {chunk['index']} 块提炼失败：{e}")
    if not any(outlines):
        raise LLMError("所有分块提炼均失败：" + ("；".join(warnings) or "未知错误"))

    # ② 汇总设定
    report("bible", len(sampled) + 1, total_steps, "汇总全剧人物 / 物品 / 场景设定…", 65)
    bible = _as_dict(build_bible(client, outlines, novel_title, style, episodes, target_shots))

    characters = _norm_list(bible.get("characters"), 8,
                            ["name", "age", "appearance", "personality", "voice_style",
                             "reference_prompt_zh", "reference_prompt_en"])
    items = _norm_list(bible.get("items"), 6,
                       ["name", "category", "appearance", "owner",
                        "reference_prompt_zh", "reference_prompt_en"])
    scenes = _norm_list(bible.get("scenes"), 8,
                        ["name", "location", "appearance", "reference_prompt_zh", "reference_prompt_en"])
    if not characters:
        raise LLMError("模型未返回有效角色设定，转换中止")
    bible = {"title": str(bible.get("title") or novel_title)[:40],
             "theme": str(bible.get("theme") or "")[:200],
             "style": str(bible.get("style") or style)[:60],
             "characters": characters, "items": items, "scenes": scenes,
             "production_notes": bible.get("production_notes") or {}}

    # ③ 逐块写分镜（镜头数按每块原文体量自动扩展，不再按 target_shots 摊薄砍内容）
    all_shots = []
    for i, chunk in enumerate(sampled):
        per_chunk = estimate_shots_for_chars(chunk.get("char_count") or len(chunk.get("text") or ""))
        report("shots", len(sampled) + 2 + i, total_steps,
               f"编写第 {chunk['index']} 块分镜（{chunk.get('title')}，{per_chunk} 镜）…",
               int(70 + (i + 1) / total_steps * 25))
        try:
            all_shots.extend(build_shots_for_chunk(client, bible, outlines[i] or {"summary": "", "key_beats": []},
                                                   chunk, per_chunk))
        except LLMError as e:
            # 兜底：按原文逐句生成承载镜头，绝不静默丢弃该段原文
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 块分镜失败，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 块分镜失败，已兜底 {len(fb)} 镜：{e}")

    shots = _norm_shots(all_shots, bible, episodes)
    if not shots:
        raise LLMError("模型未返回有效分镜：" + ("；".join(warnings) or "未知错误"))

    script = {
        "title": bible["title"],
        "theme": bible["theme"],
        "style": bible["style"],
        "characters": characters,
        "items": items,
        "scenes": scenes,
        "shots": shots,
        "production_notes": {
            "total_shots": len(shots),
            "estimated_duration": len(shots) * 5,
            "style_guide": str((bible.get("production_notes") or {}).get("style_guide") or "")[:300],
        },
        "metadata": {
            "source": "novel_to_script",
            "source_novel": {
                "novel_id": novel_meta.get("novel_id"),
                "name": novel_meta.get("name"),
                "char_count": novel_meta.get("char_count"),
                "chapter_count": novel_meta.get("chapter_count"),
            },
            "chunks_total": len(chunks),
            "chunks_used": len(sampled),
            # 全量覆盖：sampled_chunks 语义 = 全部块下标（保留旧字段名兼容前端）
            "sampled_chunks": sampled_idx,
            "coverage_mode": "full",
            "chars_per_shot": CHARS_PER_SHOT,
            "estimated_shots": sum(estimate_shots_for_chars(c.get("char_count") or 0) for c in chunks),
            "shots_per_chunk": [estimate_shots_for_chars(c.get("char_count") or 0) for c in chunks],
            "chunk_chars": CHUNK_CHARS,
            "style": style,
            "episodes": episodes,
            "target_shots": target_shots,
            "model": client.model,
            "base_url": client.base_url,
            "elapsed_sec": round(time.time() - t0, 1),
            "warnings": warnings,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    # ⑤ AI 转剧本阶段自动判定「每集镜头数 / 每集时长（秒）」并写入 Schema（下游链路直接引用）
    apply_episode_schema(script)
    script["metadata"]["coverage"] = build_chapter_coverage_meta(shots)
    # ⑥ 原文覆盖率校验：逐句核对原文是否被镜头承载，有遗漏即补生成镜头并复检（只增不删）
    if check_coverage:
        script["metadata"]["coverage"] = _run_full_coverage_check(
            client, novel_text, script, reports=report, warnings=warnings,
            continuity_dir=continuity_dir,
            project_key=project_key or (novel_meta or {}).get("name"),
            max_rounds=coverage_max_rounds, total_steps=total_steps)
    report("done", total_steps, total_steps,
           f"转换完成：{len(characters)} 角色 / {len(items)} 物品 / {len(scenes)} 场景 / "
           f"{len(script.get('shots') or shots)} 镜头 / 本集约 {script['episode_duration_sec']}s", 100)
    return script


# ===================== 按章节分集生成（每章一集） =====================

CHAPTER_MIN_CHARS = 300          # 过短章节阈值（低于此值仅提示，不做合并）
CHAPTER_CHUNK_CHARS = 3000       # 单章二次分块粒度（字符）
CHAPTER_MAX_SUBCHUNKS = 12       # [兼容保留] 旧「单章最多子块数」上限；全量覆盖后不再抽样，仅前端旧字段展示
CHAPTER_MIN_SUBCHUNK_CHARS = 200 # 章内子块最小字数（过短的尾块并入前一块，保证不丢正文）


class EpisodeNotFoundError(Exception):
    """指定剧集尚未生成"""


def _split_oversized(unit: str, limit: int) -> list:
    """把超长子单元按句末标点/换行再切，仍超限则按固定长度硬切（保证不丢字）"""
    parts, buf = [], ""
    for piece in re.split(r"(?<=[。！？；!?;])|\n", unit or ""):
        if not piece:
            continue
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        if buf:
            parts.append(buf)
            buf = ""
        while len(piece) > limit:
            parts.append(piece[:limit])
            piece = piece[limit:]
        buf = piece
    if buf:
        parts.append(buf)
    return [p for p in parts if p.strip()] or [unit]


def build_chapter_chunks(chapter_text: str, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                         max_subchunks: int = CHAPTER_MAX_SUBCHUNKS):
    """单章过长时做二次分块（按段落聚合 → 超长段落按句硬切 → 尾部并块 → 全量覆盖）。

    与整本分块（build_chunks）不同：本函数不做 MIN_CHUNK_CHARS 过滤，
    保证章节正文首尾不被丢弃。

    **全量覆盖**：分块只用于控制单次 prompt 体量，块与块连续无缝，
    所有子块都会送模型并合并，不做首尾保留式抽样（max_subchunks 仅作兼容保留）。

    返回 (全部子块, 送模型的子块, 送模型子块下标 1-based)：后两者在语义上等于全部子块。
    """
    text = (chapter_text or "").strip()
    if not text:
        return [], [], []

    units = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        units.extend([para] if len(para) <= chunk_chars else _split_oversized(para, chunk_chars))
    if not units:
        units = _split_oversized(text, chunk_chars)

    chunks, buf = [], ""
    for u in units:
        if buf and len(buf) + len(u) + 1 > chunk_chars:
            chunks.append(buf)
            buf = u
        else:
            buf = f"{buf}\n{u}" if buf else u
    if buf:
        chunks.append(buf)
    # 尾部过短则并入前一块：避免产生无意义碎片，同时不丢正文
    if len(chunks) >= 2 and len(chunks[-1]) < max(CHAPTER_MIN_SUBCHUNK_CHARS, chunk_chars // 3):
        chunks[-2] = chunks[-2] + "\n" + chunks[-1]
        chunks.pop()

    out = [{"text": c.strip(), "char_count": len(c.strip()),
            "from_chapter": None, "to_chapter": None} for c in chunks if c.strip()]
    for i, c in enumerate(out):
        c["index"] = i + 1
        c["total"] = len(out)
        c["title"] = f"第{i + 1}子块"
    # 全量覆盖：所有子块连续无缝地送模型并合并，不做任何抽样（旧 sample_chunks 调用已移除）
    all_idx = [c["index"] for c in out]
    return out, list(out), all_idx


def estimate_subchunks(char_count, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                       max_subchunks: int = CHAPTER_MAX_SUBCHUNKS) -> int:
    """不读正文即可估算的单章二次分块数量（与 build_chapter_chunks 的尾部并块行为对齐）

    全量覆盖改造后不再设子块数上限：返回值即实际需要送模型的子块数。
    """
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    est = int(math.ceil(n / float(chunk_chars)))
    if est >= 2 and (n - (est - 1) * chunk_chars) < max(CHAPTER_MIN_SUBCHUNK_CHARS, chunk_chars // 3):
        est -= 1
    return max(1, est)


def estimate_shots_for_chars(char_count) -> int:
    """按内容体量估算镜头数（全量覆盖：约每 CHARS_PER_SHOT 字 1 个镜头，不为凑时长砍内容）"""
    n = max(0, int(char_count or 0))
    if not n:
        return 0
    return max(SHOTS_PER_CHUNK_MIN, int(math.ceil(n / float(CHARS_PER_SHOT))))


def chapter_advice(char_count: int, subchunk_count: int = 0) -> dict:
    """给单章的规模提示（过短只提示不合并；全量覆盖：内容体量决定镜头数）"""
    n = int(char_count or 0)
    too_short = n < CHAPTER_MIN_CHARS
    too_long = n > CHAPTER_CHUNK_CHARS
    est_shots = estimate_shots_for_chars(n)
    if too_short:
        msg = f"本章仅 {n} 字，内容偏短，成片镜头约 {est_shots} 个（按需求不做自动合并）"
    elif too_long and subchunk_count >= 2:
        msg = (f"本章 {n} 字，超过 {CHAPTER_CHUNK_CHARS} 字，将自动二次分块为 "
               f"{subchunk_count} 个子块全量覆盖送模型（不抽样、不丢内容），预计约 {est_shots} 个镜头")
    elif too_long:
        msg = (f"本章 {n} 字，略超 {CHAPTER_CHUNK_CHARS} 字，按单块全量处理，"
               f"预计约 {est_shots} 个镜头")
    else:
        msg = (f"本章 {n} 字，单块即可完成，预计约 {est_shots} 个镜头"
               f"（镜头数随内容体量自动扩展）")
    return {"char_count": n, "too_short": too_short, "too_long": too_long,
            "subchunks": subchunk_count, "estimated_shots": est_shots, "message": msg}


def episode_project_name(novel_name: str, episode_no) -> str:
    """每集独立项目名：后续资产/分镜/视频按「某一集」继续走通，互不覆盖"""
    return f"{safe_project_name(novel_name)}_第{int(episode_no)}集"


def convert_chapter_to_script(client, novel_meta: dict, novel_text: str, chapter: dict,
                              style: str = "3D动漫渲染", target_shots: int = 12,
                              episode_no: int = 1, chunk_chars: int = CHAPTER_CHUNK_CHARS,
                              max_subchunks: int = CHAPTER_MAX_SUBCHUNKS,
                              progress_cb=None, continuity_ctx: dict = None) -> dict:
    """把「一章」转成一集 A 版剧本（每章一集，独立落盘）

    continuity_ctx（跨集连贯性方案 A/B/C）：由 continuity.build_continuity_context 组装，
    包含项目级设定库、上集摘要卡、衔接契约、项目级风格指南、金句清单、口吻词典与运镜术语表；
    传入后本集生成将带着跨集上下文，且 style_guide 改用项目级唯一配置。
    """
    def report(phase, current, total, message, percent):
        if progress_cb:
            try:
                progress_cb(phase, current, total, message, percent)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"进度回调异常：{e}")

    t0 = time.time()
    novel_title = novel_meta.get("title") or novel_meta.get("name") or "未命名小说"
    chapter_title = (chapter.get("title") or f"第{chapter.get('index')}章").strip()
    seg = (novel_text or "")[int(chapter.get("start") or 0):int(chapter.get("end") or 0)]
    if not seg.strip():
        raise LLMError(f"章节正文为空：{chapter_title}")
    if len(seg) < CHAPTER_MIN_CHARS:
        logger.warning(f"章节偏短（{len(seg)} 字）：{chapter_title}")

    all_chunks, sampled, sampled_idx = build_chapter_chunks(seg, chunk_chars, max_subchunks)
    if not sampled:
        raise LLMError(f"章节无法分块：{chapter_title}")
    advice = chapter_advice(len(seg), len(all_chunks))

    warnings = []
    trunc_events = []          # 截断自动提额 / 二次二分事件（写入 metadata，便于排查）
    if advice["too_short"]:
        warnings.append(advice["message"])
    # 全量覆盖：镜头数随内容体量自动扩展（约每 CHARS_PER_SHOT 字 1 镜），不再为凑固定时长砍内容
    est_shots_total = sum(estimate_shots_for_chars(c.get("char_count") or 0) for c in all_chunks)

    total_steps = len(sampled) + 2 + len(sampled)
    report("prepare", 0, total_steps,
           f"第{episode_no}集《{chapter_title}》：{len(seg)} 字，二次分块 {len(all_chunks)} 块，"
           f"全量送模型 {len(sampled)} 块（不抽样），预计 {est_shots_total} 镜", 3)

    # ① 逐块提炼（按 chunk.index 建立映射，避免与 sampled 错位）
    outlines, outline_by_chunk = [], {}
    for i, chunk in enumerate(sampled):
        report("outline", i + 1, total_steps,
               f"第{episode_no}集 提炼子块 {chunk['index']}/{chunk['total']}…",
               int(5 + (i + 1) / total_steps * 55))
        try:
            ol = extract_chunk_outline(client, chunk, novel_title, events=trunc_events)
        except LLMTruncatedError as e:
            warnings.append(f"第 {chunk['index']} 子块提炼被截断失败（已自动提额并尝试二次切分）：{e}")
            logger.warning(f"第 {chunk['index']} 子块提炼被截断失败：{e}")
            continue
        except LLMError as e:
            warnings.append(f"第 {chunk['index']} 子块提炼失败：{e}")
            logger.warning(f"第 {chunk['index']} 子块提炼失败：{e}")
            continue
        outlines.append(ol)
        outline_by_chunk[chunk["index"]] = ol
    if not outlines:
        raise LLMError("本章所有子块提炼均失败（每块均已自动提高 max_tokens，必要时二次切分）："
                       + ("；".join(warnings) or "未知错误"))

    # ② 汇总本集设定
    report("bible", len(sampled) + 1, total_steps, f"第{episode_no}集 汇总人物 / 物品 / 场景设定…", 65)
    bible = _as_dict(build_bible(client, outlines, f"{novel_title}·{chapter_title}", style, 1,
                                 target_shots, events=trunc_events, continuity_ctx=continuity_ctx))

    characters = _norm_list(bible.get("characters"), 8,
                            ["name", "age", "identity", "appearance", "outfit", "personality",
                             "voice_style", "reference_prompt_zh", "reference_prompt_en"])
    items = _norm_list(bible.get("items"), 6,
                       ["name", "category", "appearance", "owner",
                        "reference_prompt_zh", "reference_prompt_en"])
    scenes = _norm_list(bible.get("scenes"), 8,
                        ["name", "location", "appearance", "reference_prompt_zh", "reference_prompt_en"])
    if not characters:
        raise LLMError("模型未返回有效角色设定，转换中止")
    bible = {"title": str(bible.get("title") or novel_title)[:40],
             "theme": str(bible.get("theme") or "")[:200],
             "style": str(bible.get("style") or style)[:60],
             "characters": characters, "items": items, "scenes": scenes,
             "production_notes": bible.get("production_notes") or {}}

    # ③ 逐块写分镜（每块镜头数按该块原文体量自动扩展，不再按 target_shots 摊薄砍内容）
    all_shots = []
    for i, chunk in enumerate(sampled):
        chunk_chars_i = chunk.get("char_count") or len(chunk.get("text") or "")
        per_chunk = estimate_shots_for_chars(chunk_chars_i)
        report("shots", len(sampled) + 2 + i, total_steps,
               f"第{episode_no}集 编写分镜（子块 {chunk['index']}/{chunk['total']}，"
               f"{chunk_chars_i} 字 → {per_chunk} 镜）…",
               int(70 + (i + 1) / total_steps * 25))
        ol = outline_by_chunk.get(chunk["index"])
        if not ol:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块缺少提炼结果，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）")
            logger.warning(f"第 {chunk['index']} 子块缺少提炼结果，已兜底 {len(fb)} 镜")
            continue
        try:
            all_shots.extend(build_shots_for_chunk(client, bible, ol, chunk, per_chunk,
                                                   events=trunc_events,
                                                   continuity_ctx=continuity_ctx))
        except LLMTruncatedError as e:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块分镜被截断失败（已自动提额并尝试二次切分），"
                            f"已按原文兜底生成 {len(fb)} 镜（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 子块分镜被截断失败，已兜底 {len(fb)} 镜：{e}")
        except LLMError as e:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块分镜失败，已按原文兜底生成 {len(fb)} 镜"
                            f"（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 子块分镜失败，已兜底 {len(fb)} 镜：{e}")

    shots = _norm_shots(all_shots, bible, 1)
    for sh in shots:
        sh["episode"] = int(episode_no)
    if not shots:
        raise LLMError("模型未返回有效分镜（每块均已自动提高 max_tokens，必要时二次切分）："
                       + ("；".join(warnings) or "未知错误"))

    project_name = episode_project_name(novel_meta.get("name") or novel_title, episode_no)
    script = {
        "title": bible["title"],
        "episode_no": int(episode_no),
        "episode_title": chapter_title,
        "theme": bible["theme"],
        "style": bible["style"],
        "characters": characters,
        "items": items,
        "scenes": scenes,
        "shots": shots,
        "production_notes": {
            "total_shots": len(shots),
            "estimated_duration": len(shots) * 5,
            # C⑥：style_guide 提升为项目级唯一配置（continuity 提供时优先，不再每集各写一套）
            "style_guide": str(_ctx_block(continuity_ctx, "style_guide_text")
                               or (bible.get("production_notes") or {}).get("style_guide") or "")[:300],
            "style_guide_source": "project" if _ctx_block(continuity_ctx, "style_guide_text") else "episode",
            "bible_locked": bool(continuity_ctx),
            "continuity_version": str((continuity_ctx or {}).get("version") or "") or None,
        },
        "metadata": {
            "source": "novel_chapter_to_script",
            "source_novel": {
                "novel_id": novel_meta.get("novel_id"),
                "name": novel_meta.get("name"),
                "title": novel_meta.get("title"),
                "char_count": novel_meta.get("char_count"),
                "chapter_count": novel_meta.get("chapter_count"),
            },
            "episode_no": int(episode_no),
            "episode_title": chapter_title,
            "chapter_index": chapter.get("index"),
            "chapter_title": chapter_title,
            "chapter_char_count": len(seg),
            "chunks_total": len(all_chunks),
            "chunks_used": len(sampled),
            # 全量覆盖：sampled_chunks 语义 = 全部子块下标（保留旧字段名兼容前端）
            "sampled_chunks": sampled_idx,
            "coverage_mode": "full",
            "chars_per_shot": CHARS_PER_SHOT,
            "estimated_shots": est_shots_total,
            "shots_per_chunk": [estimate_shots_for_chars(c.get("char_count") or 0) for c in all_chunks],
            "subchunk_chars": chunk_chars,
            "style": style,
            "target_shots": target_shots,
            "project_name": project_name,
            "model": client.model,
            "base_url": client.base_url,
            "elapsed_sec": round(time.time() - t0, 1),
            "warnings": warnings,
            "truncation_events": trunc_events,
            "truncation_retries": len([e for e in trunc_events if e.get("attempt")]),
            "auto_sub_splits": len([e for e in trunc_events if e.get("event") == "sub_split"]),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    # ⑤ AI 转剧本阶段自动判定「本集镜头数 / 本集时长（秒）」并写入 Schema（下游链路直接引用）
    apply_episode_schema(script)
    report("done", total_steps, total_steps,
           f"第{episode_no}集完成：{len(characters)} 角色 / {len(items)} 物品 / "
           f"{len(scenes)} 场景 / {script['shot_count']} 镜头 / 本集约 {script['episode_duration_sec']}s", 100)
    return script


def episode_script_path(script_dir: str, novel_name: str, episode_no, project_key: str = None) -> str:
    """按集组织落盘路径：output/scripts/<项目键>/第N集.json

    project_key 由项目注册表（project_store）统一分配，保证「一部小说一个独立目录」；
    未提供时退回按小说名安全化，兼容旧行为。
    """
    folder = project_key or safe_project_name(novel_name)
    return os.path.abspath(os.path.join(
        script_dir, folder, f"第{int(episode_no)}集.json"))


def save_episode_script(script: dict, script_dir: str, novel_name: str, episode_no,
                        project_key: str = None) -> str:
    """按集落盘（覆盖同名集号），返回绝对路径"""
    path = episode_script_path(script_dir, novel_name, episode_no, project_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = script.setdefault("metadata", {})
    meta["script_path"] = path
    meta["project_name"] = meta.get("project_name") or episode_project_name(novel_name, episode_no)
    meta["project_key"] = project_key or safe_project_name(novel_name)
    meta["episode_no"] = int(episode_no)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    logger.info(f"第{episode_no}集剧本已落盘：{path}")
    return path


def list_episodes(script_dir: str, novel_name: str, project_key: str = None) -> list:
    """列出某小说已生成的剧集（按集号排序）"""
    folder = os.path.join(script_dir, project_key or safe_project_name(novel_name))
    if not os.path.isdir(folder):
        return []
    out = []
    for fn in os.listdir(folder):
        m = re.match(r"^第(\d+)集\.json$", fn)
        if not m:
            continue
        path = os.path.abspath(os.path.join(folder, fn))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"剧集读取失败 {path}：{e}")
            continue
        meta = data.get("metadata") or {}
        shots = data.get("shots") or []
        stats = meta.get("episode_stats") or build_episode_stats(shots)
        cov = meta.get("coverage") or {}
        out.append({
            "episode_no": int(m.group(1)),
            "file": fn,
            "path": path,
            "title": data.get("title"),
            "episode_title": data.get("episode_title") or meta.get("chapter_title"),
            "chapter_index": meta.get("chapter_index"),
            "chapter_char_count": meta.get("chapter_char_count"),
            "characters": len(data.get("characters") or []),
            "items": len(data.get("items") or []),
            "scenes": len(data.get("scenes") or []),
            "shots": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
            "shot_count": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
            "episode_duration_sec": float(data.get("episode_duration_sec") or stats.get("duration_sec") or 0),
            "duration_per_shot_sec": float(data.get("duration_per_shot_sec")
                                           or stats.get("duration_per_shot_sec") or 0),
            "episode_stats": stats,
            "prompt_ready": sum(1 for s in shots if isinstance(s, dict) and s.get("prompt_h3")),
            "project_name": meta.get("project_name") or episode_project_name(novel_name, m.group(1)),
            "generated_at": meta.get("generated_at"),
            "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
            "warnings": meta.get("warnings") or [],
            # ④⑤ 原文覆盖率（落盘可查；未跑过新流程的旧剧集为 None）
            "coverage_percent": cov.get("coverage_percent"),
            "coverage_plot_percent": cov.get("plot_coverage_percent", cov.get("coverage_percent")),
            "coverage_detail_percent": cov.get("detail_coverage_percent", cov.get("char_coverage_percent")),
            "coverage_detail_passed": cov.get("detail_passed"),
            "coverage_zero_omission": cov.get("zero_omission"),
            "coverage_passed": cov.get("passed"),
            "coverage_threshold_percent": cov.get("threshold_percent"),
            "coverage_missing_count": cov.get("missing_count"),
            "coverage_supplement_shots": cov.get("supplement_shots"),
            "coverage_supplement_rounds": cov.get("supplement_rounds"),
            "coverage_report_path": meta.get("coverage_report_path"),
            "coverage_verified": bool(cov.get("checked_at")),
        })
    out.sort(key=lambda r: r["episode_no"])
    return out


def load_episode_script(script_dir: str, novel_name: str, episode_no, project_key: str = None) -> dict:
    """读取已落盘的单集剧本（供前端切集预览 / 载入后续步骤）"""
    path = episode_script_path(script_dir, novel_name, episode_no, project_key)
    if not os.path.isfile(path):
        raise EpisodeNotFoundError(f"第{int(episode_no)}集尚未生成：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_project_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip())
    return name[:40] or "novel_project"


def save_generated_script(script: dict, script_dir: str, project_name: str = None,
                          project_key: str = None) -> str:
    """落盘剧本 JSON。

    - project_key 为空：沿用旧行为，落在 output/scripts/<项目名>_<时间戳>.json
    - project_key 非空：落在 output/scripts/<项目键>/整本_<时间戳>.json，
      与项目隔离目录结构一致（项目键由 project_store 统一分配）
    """
    meta = script.setdefault("metadata", {})
    if project_key:
        pname = str(project_key)
        folder = os.path.join(script_dir, pname)
        os.makedirs(folder, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.abspath(os.path.join(folder, f"整本_{ts}.json"))
    else:
        os.makedirs(script_dir, exist_ok=True)
        pname = safe_project_name(project_name or script.get("title") or "novel_project")
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.abspath(os.path.join(script_dir, f"{pname}_{ts}.json"))
    meta["script_path"] = path
    meta["project_name"] = pname
    meta["project_key"] = pname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    logger.info(f"剧本已落盘：{path}")
    return path
