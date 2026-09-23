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

import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime

from llm_client import LLMError, LLMTruncatedError, LLMGatewayUnavailable
from dialogue_utils import dialogue_text as _dlg_text, normalize_lines as _dlg_lines
import fs_atomic
import style_kit
import h3_prompt_kit

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

#: **已废弃分镜字段登记表**（结构化，替代注释里的君子协定）。
#: 键 = 字段名；值中的 ``deprecated=True`` 供代码/回归脚本做机器可判定的守卫。
#: 口径：这些字段**不得再写入剧本**，``_norm_shots`` 的字段白名单在标准化时会显式丢弃
#: 模型越界输出的对应字段并留痕；读取侧（coverage / h3_prompt_kit / tts_client）仅为兼容
#: 旧剧本做 legacy 记账，是唯一的合法消费方。
DEPRECATED_SHOT_FIELDS = {
    "narration": {
        "deprecated": True,
        "since": "2026-09-19",
        "reason": (
            "旁白通道已关闭（2026-09-19 产品决策）：剧本阶段不写、配音链路不念、成片不产出旁白。"
            "历史缺陷：narration 曾被当成「心理活动 + 背景补叙 + 环境描写」的公共出口，"
            "实测 ep04 旁白 2231 字 ≈ 496 秒铺在 100 秒画面上（4.93x 溢出），尾部被成片 -shortest "
            "静默截断。现在 _norm_shots 不再透传模型越界输出的 narration，"
            "保证「成片无旁白」是硬不变量；旧剧本残留字段由读取侧按需兼容。"
        ),
    },
}

SYSTEM_BIBLE = ("你是资深漫剧编剧与 AI 绘画提示词工程师，精通把长篇小说改编成可拍摄的漫剧分镜脚本，"
                "并输出严格合法的 JSON。严禁在 content 中输出任何思考过程、英文推理、分析或解释文字，"
                "只允许输出一个可被 json.loads 直接解析的 JSON 对象。")

# ===================== 全量覆盖策略常量（改编 ≠ 缩写，严禁删减原文） =====================

CHARS_PER_SHOT = 120          # 每个镜头承载的原文字数基准（镜头数随内容体量自动扩展，收紧以承载细节）
SHOTS_PER_CHUNK_MIN = 6       # 单块分镜数下限（再短的块也至少这么多镜）
# ---- 分镜阶段的 token 预算（必须给「思考」留预留量）----
# ⚠️ always-on reasoning 模型（agnes-3.0-flash / GLM 系）在写分镜前会先输出一大段思考，
# 实测该任务的思考量 ≈16K token。若 max_tokens 低于思考量，模型会「只吐思考、正文为空」，
# 表现为整集卡死（2026-09-23 端到端复测的核心阻塞）。此前按 shots_target*300+1200 给
# （12 镜 → 4800）远低于水位，故改为「固定思考预留 + 每镜正文额度」。
SHOTS_THINKING_RESERVE = 16384   # 思考预留（与 llm_client.REASONING_ONLY_TOKEN_FLOOR 对齐）
SHOTS_TOKENS_PER_SHOT = 300      # 单镜正文额度（description+visual_detail+dialogue+audio_cues 实测够用）
COVERAGE_THRESHOLD = 0.95     # 原文覆盖率阈值：低于该值自动补生成缺失片段

# ===================== 提炼 / 分镜阶段的断点缓存（对抗网关偶发挂起） =====================
# 背景（2026-09-24 实测）：网关上游偶发挂起 —— 连 max_tokens=3600 的小请求也会挂满
# 1200s read timeout（而同一时刻 16384 额度的大请求 258s 就返回了），说明**与请求体量无关**。
# 而本阶段单个请求体量本来就大（思考预留 16384 + 每镜 300），一集完整生成要 20+ 分钟；
# pipeline 的重试又是**整个 script 步骤重来**（`_run_step_with_retry`）——没有缓存时
# 每次重试都要把 提炼 + 设定 + 全部分镜 重新跑一遍，网关一抖就永远跑不完。
#
# 这里按「prompt 内容指纹」做**内容寻址**落盘，重跑时命中即跳过模型调用：
#   - 任意输入（原文 / 镜头额度 / 风格 / 提示词模板本身）变化 → 指纹随之变化 → 自动失效，
#     绝不会用旧结果冒充新结果（把 prompt 整体入指纹，是防止「改了提示词却命中旧缓存」的关键）；
#   - 网关抖动的净效果从「永远跑不完」变成「多跑几次总能跑完」。
# 缓存只写不读回主链路语义 —— 未命中时行为与加缓存前**完全一致**。
# 用 MJSCXT_SHOTS_CACHE=0 可关闭（离线测试 / 需要强制重新生成时）。
SHOTS_CACHE_ENV = "MJSCXT_SHOTS_CACHE"


def _shots_cache_enabled() -> bool:
    """缓存开关：默认开启，环境变量置 0/false/no/off 时关闭。"""
    return str(os.environ.get(SHOTS_CACHE_ENV, "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _cache_file(cache_dir: str, kind: str, prompt: str) -> str:
    """内容寻址缓存路径：文件名 = sha1(kind + prompt) 前 20 位。"""
    digest = hashlib.sha1(f"{kind}\x00{prompt}".encode("utf-8")).hexdigest()[:20]
    return os.path.join(cache_dir, f"{kind}_{digest}.json")


def _cache_read(path: str):
    """读缓存：不存在 / 损坏 / 非 dict 一律当作**未命中**。

    缓存是「只读加速视图」，不是主链路状态 —— 因此损坏时按未命中重新生成，
    而不是 fail-loud 中断整集（与 `fs_atomic` 对主链路文件的 fail-loud 口径区分开）。
    """
    try:
        data = fs_atomic.read_json_strict(path, None)
    except Exception as e:  # noqa: BLE001 —— 缓存损坏仅降级为未命中
        logger.warning(f"提炼/分镜缓存不可用，按未命中重新生成：{os.path.basename(path)}：{e}")
        return None
    return data if isinstance(data, dict) else None


def _cache_write(path: str, payload: dict) -> None:
    """写缓存：失败只告警，绝不影响本次产出（缓存是加速手段，不是必需产物）。"""
    try:
        fs_atomic.atomic_write_json(path, payload)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"写提炼/分镜缓存失败（忽略，不影响本次产出）：{e}")


def _cache_get(cache_dir: str, kind: str, prompt: str, events: list, label: str):
    """命中则返回缓存 dict，否则 None（未命中不产生任何副作用）。"""
    if not cache_dir or not _shots_cache_enabled():
        return None
    path = _cache_file(cache_dir, kind, prompt)
    hit = _cache_read(path)
    if hit is None:
        return None
    logger.info(f"{label} 命中断点缓存，跳过模型调用：{os.path.basename(path)}")
    if events is not None:
        events.append({"label": label, "event": "cache_hit", "kind": kind})
    return hit


def _cache_put(cache_dir: str, kind: str, prompt: str, payload: dict) -> None:
    if not cache_dir or not _shots_cache_enabled():
        return
    _cache_write(_cache_file(cache_dir, kind, prompt), payload)


REWRITE_RULES = (
    "【改写规则（这是改编，不是缩写：严禁删减原文内容）】\n"
    "1) 原文的叙述、心理描写、场景描写、对话、人物动作必须全部落到镜头里，"
    "分别体现为画面描述（description）、台词（dialogue，含角色自语/心声）、"
    "动作与情绪（emotion）、音效与配乐（audio_cues）；\n"
    "2) 允许体裁形式改写：心理活动改写成该角色本人的自语台词（speaker 写角色名）"
    "或可拍的表情/动作，叙述改写成画面动作描述，环境描写改写成画面与音效，"
    "但不得改变情节、不得删减人物；\n"
    "3) 严禁删除情节、删除人物、跳过段落、合并概括、只挑重点写；"
    "原文内容越多，镜头就要越多（约每 {chars_per_shot} 字 1 个镜头，情节密集处更多）；\n"
    "4) 原文对话尽量原样写进对应角色的 dialogue.text，禁止改写成概括式引述；\n"
    "5) 镜头按原文时间顺序排列，块首镜头自然衔接上一块结尾，不得跳段、不得重复；\n"
    "6) 【细节零删减·最高优先级】原文单句内的修饰细节（外貌、衣着、神态、动作过程、心理活动、"
    "环境与光线、器物声响）都必须落到镜头里：外貌/器物/环境/神态写进 description（画面描述），"
    "心理活动转成可拍的表情动作或角色自语台词，动作过程写进 description，"
    "对话与自语写进 dialogue，器物声响写进 audio_cues；\n"
    "7) 【措辞尽量原样】承载细节时优先沿用原文措辞，只做体裁转换与必要的镜头化补白，"
    "严禁改写成笼统概括；\n"
    "8) 【本系统不产出旁白】成片没有画外音解说：原文里的背景补叙、环境描写一律靠画面呈现，"
    "心理活动靠角色神态动作或自语台词呈现，**禁止**用任何「旁白/画外音」形式复述原文；\n"
    "9) 自检：写完一块后逐句回看原文，确认每一句（含背景补叙、过渡句、环境句）都能在某条镜头的 "
    "description / visual_detail / dialogue / audio_cues 中找到对应承载，不允许出现「没写到」的句子。"
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


def _gateway_down_fallback(e, chunk: dict, per_chunk: int, bible: dict,
                           produced: list) -> list:
    """「LLM 网关不可用」时的统一处置（不是普通的单块失败）

    实测教训：网关上游没算力时，每一块都会失败 → 每块都按原文兜底 →
    整集 6/6 兜底，脚本却仍记成「生成成功」。这种剧本没有分镜设计、没有台词，
    配音链路读不到 dialogue 就只出 1 句，用户根本无从判断哪一镜有问题。
    所以分两种情况：
    - **还没产出任何真实镜头** → 直接失败。产出兜底剧本比报错更糟。
    - **已有真实产出** → 剩余块兜底，保住已完成部分，并在 warnings 里如实标注降级。
    """
    if not produced:
        hint = getattr(e, "hint", "") or "请到「AI 设置」检查 base_url / model 是否可用"
        raise LLMError(
            "LLM 网关不可用，已中止生成：继续下去只会得到一份「无分镜、无台词」的"
            f"原文兜底剧本（该剧本配音只能出 0 句）。原因：{e}　处置建议：{hint}"
        ) from e
    return _fallback_shots_for_chunk(chunk, per_chunk, bible)


def _gateway_down_warning(chunk: dict, n_fb: int) -> str:
    return (f"⚠ LLM 网关不可用：第 {chunk['index']} 块已按原文兜底生成 {n_fb} 镜。"
            "本集为**降级产出**（该块无分镜设计、无台词），网关恢复后建议重跑本集。")


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
                          events: list = None, depth: int = 0,
                          cache_dir: str = "") -> dict:
    """① 单块提炼（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）

    cache_dir 非空时启用断点缓存：命中即跳过模型调用（见文件上方缓存说明）。
    """
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
    hit = _cache_get(cache_dir, "outline", prompt, events, label)
    if hit is not None and hit.get("_chunk"):
        return hit
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
            [extract_chunk_outline(client, s, novel_title, events, depth + 1, cache_dir)
             for s in subs], chunk)
    data["_chunk"] = {
        "index": chunk["index"],
        "title": chunk.get("title"),
        "char_count": len(body),
    }
    _cache_put(cache_dir, "outline", prompt, data)
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
                target_shots: int, events: list = None, continuity_ctx: dict = None,
                cache_dir: str = "") -> dict:
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
  "characters": [{{"name": "姓名", "age": "年龄", "identity": "身份/阵营（15 字以内）", "appearance": "外貌（含发色/瞳色/标志特征，60 字以内；若上方设定库已锁定则该字段必须与锁定值逐字一致）", "outfit": "本集服装状态（20 字以内，与上集结尾一致；若本集确有换装必须体现原因）", "personality": "性格（30 字以内）", "voice_style": "配音风格（15 字以内）", "reference_prompt_zh": "中文参考图提示词：角色三视图设定图，60 字以内，只写画面可见的具体特征——发色发型、瞳色、脸型、服装款式与材质配色、标志配饰、三视图版式（**必须写明「正面、侧面、背面三张全身视图横排，从头到脚完整入画、同一角色身高比例一致」**，不要写成半身/胸像）；**严禁写任何风格词/画风词/质量词**（如「国漫」「3D渲染」「电影级」「高清」「精致」）", "reference_prompt_en": "English prompt for a character reference sheet with three full-body views (front, side, back laid out horizontally, head-to-toe, consistent body proportions), under 45 words, comma-separated CONCRETE visual keywords (hair color and style, eye color, face shape, outfit material and colors, signature accessories, view layout). It MUST be an accurate translation of reference_prompt_zh. Never romanize Chinese concepts into invented pinyin (「国漫」 must become 'Chinese animated style', NOT 'xuanxuan'); never write style or quality words — the program appends them"}}],
  "items": [{{"name": "物品名", "category": "武器/法宝/道具/服饰", "appearance": "外观（50 字以内）", "owner": "持有人", "importance": "重要/临时（重要=后续章节会重复出现或推动剧情，临时=仅本集使用），只输出重要道具", "reference_prompt_zh": "中文参考图提示词，50 字以内，只写形制、材质、颜色、纹样与磨损状态；**严禁写风格词/画风词/质量词**", "reference_prompt_en": "English prompt for an item prop sheet, under 40 words, comma-separated concrete visual keywords (shape, material, color, pattern, wear). Accurate translation of reference_prompt_zh; no invented pinyin, no style or quality words"}}],
  "scenes": [{{"name": "场景名", "location": "地点类型", "appearance": "环境与氛围（60 字以内）", "reference_prompt_zh": "中文参考图提示词，50 字以内，只写空间结构、建筑形制、时间天气、光源方向与色调；**严禁写风格词/画风词/质量词**（且不要出现人物）", "reference_prompt_en": "English prompt for an environment concept art sheet, under 40 words, comma-separated concrete visual keywords (spatial layout, architecture, time of day and weather, light direction, color palette, no people). Accurate translation of reference_prompt_zh; no invented pinyin, no style or quality words"}}],
  "production_notes": {{"style_guide": "画面与叙事风格说明（60 字以内）"}}
}}
【硬性约束】characters 最多 6 个（只保留主要角色，按戏份排序）；items 最多 5 个；scenes 最多 6 个；不要输出示例里的占位文字。若上方提供了「项目级设定库」，则已登记角色的 name / appearance / personality 必须与该库完全一致（禁止改名、禁止改外观），只允许更新 outfit（当前服装状态）。
【风格红线·重要变更】风格词由**程序在生成前统一追加**（幂等，不会重复），不再由你写。因此 characters / items / scenes 三个数组里每一条 reference_prompt_zh 与 reference_prompt_en **都不得自行写风格词、画风词或质量词**——自己写了会导致风格在提示词里出现两遍（实测就是「中国古风玄幻漫剧风格。风格：中国古风玄幻漫剧，画面精致…」这种重复），属于不合格输出。你只需专注描述画面里看得见的具体特征，把风格判断交给程序。
【格式红线】直接以 {{ 作为输出的第一个字符；严禁输出任何推理过程、思考草稿、英文说明、markdown 代码块标记或前后缀解释文字；整个 JSON 输出控制在 1200 字以内（字段描述能短则短）。"""
    # 断点缓存：命中则跳过模型汇总（未命中时行为与加缓存前完全一致）。
    # 只缓存**模型成功产出**的结果；下面的确定性兜底不缓存，好让下次仍有机会走模型。
    hit = _cache_get(cache_dir, "bible", prompt, events, "bible")
    if hit is not None and hit.get("characters"):
        return hit
    bible_retry_kw = {"max_attempts": 4, "token_ladder": (6000, 8192, 16384, 24576)}
    data = {}
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
        data = _normalize_bible(raw)
        if data.get("characters"):
            _cache_put(cache_dir, "bible", prompt, data)
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
    out_chars, out_items, out_scenes = (list(chars.values())[:6], list(items.values())[:5],
                                        list(scenes.values())[:6])
    # 兜底路径同样要带风格：否则一旦 bible 汇总失败，资产提示词又回到「零风格词」老样子
    eff = style_kit.normalize_style(style)
    if eff:
        for group in (out_chars, out_items, out_scenes):
            style_kit.apply_asset_style_all(group, eff)
    return {
        "title": (novel_title or "")[:20],
        "theme": "",
        "style": eff,
        "characters": out_chars,
        "items": out_items,
        "scenes": out_scenes,
        "production_notes": {"style_guide": eff},
        "_degraded": True,
    }


def build_shots_for_chunk(client, bible: dict, outline: dict, chunk: dict, shots_target: int,
                          events: list = None, depth: int = 0,
                          continuity_ctx: dict = None, cache_dir: str = "") -> list:
    """③ 单块写分镜（截断时自动提高 max_tokens；仍截断则把该块再二分后合并）

    continuity_ctx 非空时（跨集连贯性方案 A②③ / C⑦⑧）：注入上集摘要卡、衔接契约、
    项目级风格指南、人物口吻词典、金句保留清单与运镜术语表。
    cache_dir 非空时启用断点缓存：命中即跳过模型调用（见文件上方缓存说明）。
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
    speech_budget = SHOT_SPEECH_BUDGET_CHARS
    prompt = f"""【任务】为漫剧《{bible.get('title') or ''}》的「{chunk.get('title')}」（第 {chunk['index']}/{chunk['total']} 段）编写分镜：至少 {shots_target} 个、上限 {shots_cap} 个，必须完整承载下方原文的全部情节。
{REWRITE_RULES.format(chars_per_shot=CHARS_PER_SHOT)}
【全剧风格】{bible.get('style') or ''}　【画面风格指南】{_ctx_block(continuity_ctx, 'style_guide_text') or (bible.get('production_notes') or {}).get('style_guide') or ''}
{_ctx_line(continuity_ctx, 'prev_block')}{_ctx_line(continuity_ctx, 'bible_block')}{_ctx_line(continuity_ctx, 'contract_block')}{_ctx_line(continuity_ctx, 'style_block')}{_ctx_line(continuity_ctx, 'camera_block')}【可用角色】{json.dumps(char_brief, ensure_ascii=False)}
【可用物品】{json.dumps(item_brief, ensure_ascii=False)}
【可用场景】{json.dumps(scene_brief, ensure_ascii=False)}
【本段原文（必须逐句改写成镜头/台词/画面描述，严禁删减或概括压缩）】
{chunk.get('text') or ''}
【本段剧情摘要】{outline.get('summary', '')}
【本段情节要点】{json.dumps(outline.get('key_beats') or [], ensure_ascii=False)}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字，结构如下：
{{"shots": [{{"camera": "景别+运镜（必须取自上方运镜术语表，如 中景跟拍/特写推入，10 字以内）", "location": "所属场景名（必须来自可用场景）", "description": "画面内容描述（80 字以内，写清人物动作、表情与关键构图；四个要素缺一不可：①人物动作过程（谁做了什么、怎么做的）②外貌衣着细节（发型/瞳色/服装材质/配饰）③环境与光线（时间、天气、光源方向、色调）④构图与景别（人物在画面中的位置、前中后景关系）；尽量沿用原文措辞）", "visual_detail": "画面补充细节（可选；当 description 之外还有更细的时间/天气/光源方向/动作过程/环境细节时写在这里，80 字以内；没有多余细节时写空字符串）", "dialogue": [{{"speaker": "说话角色名（必须与可用角色完全一致）", "text": "该角色台词（≤30 字；原文对话尽量原样保留；角色的自语/心声写成该角色本人的台词）"}}], "emotion": "情绪（8 字以内）", "audio_cues": "音效/配乐提示（60 字以内，只写环境音/音效/配乐，不写人声）", "characters_in_shot": ["出场角色名"], "items_in_shot": ["出场物品名"]}}]}}
【禁止输出 prompt_h3 字段】视频提示词由程序在生成阶段按 H3 规范自动构建（它会结合当次实际传入的参考图，生成 subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music 六段）。你在剧本阶段并不知道最终配几张参考图，写出来的英文提示词缺少 <Picture N> 标签，反而会覆盖规范提示词导致出片偏离设定。因此**不要写 prompt_h3、不要写英文提示词**；把画面信息全部写进 description 即可。
【台词要求】dialogue 必须是数组，数组元素为 {{"speaker": 角色名, "text": 台词}}；speaker 必须精确等于「可用角色」中的名字，禁止写“旁白/众人”等未登记角色；无台词的镜头 dialogue 写 []（空数组），禁止写成字符串或 null。角色的心理活动改写成该角色**本人**的自语台词时，speaker 仍写角色名（不要写成「旁白」，本系统没有旁白角色）。
【台词预算（防成片截断）】单个镜头的 dialogue **合计不超过 {speech_budget} 字**（≈6.7 秒配音）。台词再多就**拆成更多镜头**，不要塞进同一个镜头——配音是按镜头时间轴铺的，单镜台词超出镜头时长会被成片尾部静默截掉。
【音轨说明（本系统不产出旁白）】成片没有画外音解说，配音链路**只读 dialogue**：audio_cues 里写「雨声」「风声」这类音效**不会产生人声**。因此：① 有对话或自语的镜头必须写 dialogue，禁止把台词塞进 description / visual_detail / audio_cues；② 纯画面/纯动作镜头允许没有台词（该镜成片留白，由音效与配乐铺底），但**必须**在 audio_cues 写明音效/配乐提示；③ **严禁**凭空编造原文里没有的台词来「凑人声」——宁可留白，也不要无中生有。
【硬性约束】shots 数组元素个数必须在 {shots_target} ~ {shots_cap} 之间：上方原文的全部情节都要落到镜头里，不得删减情节、不得跳过段落、不得合并概括（内容多时用更多镜头承载，而不是少写镜头）；name 字段必须与上面「可用角色/物品/场景」中的名字完全一致，不要新造名字。若上方给出「本集必须出现的原文金句」，必须把每句**原样**写进对应角色的 dialogue.text（不得改写、不得拆分、不得省略）。上一集已发生的事件禁止在本集重演。
【逐句归属自检（细节零删减）】逐句回看原文，确保每一句（含背景补叙、过渡句、环境句）都落在某条镜头的 description / visual_detail / dialogue / audio_cues 里；短句可合并到相邻镜头，但不得整句丢弃。记住：本系统没有旁白，背景补叙与环境描写靠画面承载，心理活动靠神态动作或角色自语承载。"""
    label = f"shots#{chunk.get('index')}"
    hit = _cache_get(cache_dir, "shots", prompt, events, label)
    if hit is not None and isinstance(hit.get("shots"), list) and hit["shots"]:
        return [s for s in hit["shots"] if isinstance(s, dict)]
    try:
        # ⚠️ 起始额度必须已包含「思考水位」：agnes-3.0-flash 这类 always-on reasoning 模型
        # 在本任务的思考量实测 ≈16K token（见 llm_client.REASONING_ONLY_TOKEN_FLOOR 注释）。
        # 此前按 shots_target*300+1200 给（12 镜 → 4800），低于思考水位 → 正文恒为空，
        # 表现为「模型只吐思考内容」并把整集卡死。这里按「思考预留 + 每镜正文」给足。
        _budget = SHOTS_THINKING_RESERVE + int(shots_target) * SHOTS_TOKENS_PER_SHOT
        data = _robust_json(client, prompt, system=SYSTEM_BIBLE, temperature=0.6,
                            max_tokens=max(8192, min(24000, _budget)),
                            events=events, label=label,
                            max_attempts=4,
                            token_ladder=(16384, 24576, 32768))
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
                                                continuity_ctx=continuity_ctx,
                                                cache_dir=cache_dir))
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
    out = [s for s in shots if isinstance(s, dict)]
    if out:
        _cache_put(cache_dir, "shots", prompt, {"shots": out})
    return out


def _fallback_shots_for_chunk(chunk: dict, shots_target: int = 1, bible: dict = None) -> list:
    """分镜阶段模型失败时的兜底：按原文逐句生成「原文承载镜头」，保证该段内容不丢。

    与覆盖率补生成同源（只增不删）：原文措辞写进 description（超长部分由 _norm_shots
    拆进 visual_detail，故正文不会因为 200 字截断而丢失），标记 fallback=True 供前端提示需人工润色。
    这类镜头没有台词（本系统不产出旁白），成片该段留白 —— dialogue_utils.audit_script 会显式告警。
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
            "description": g[:200], "dialogue": [], "emotion": "平静", "audio_cues": "",
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
#: 单镜台词合计字数建议上限（≈6.7 秒配音）。这是「剧本阶段」的软预算：写超了应当拆成更多镜头。
#: 「生成期」另有一道硬兜底 —— required_shot_duration() 超 SHOT_DURATION_MAX 的镜头会被 _norm_shots 自动拆镜，
#: 所以即使模型没遵守预算，也不会让配音溢出到下一镜（溢出的尾部会被成片 -shortest 静默截掉）。
SHOT_SPEECH_BUDGET_CHARS = 30

# 动作镜识别词：description 命中任意一个即给画面停留时间加成（动作戏观感不仓促）
_ACTION_MARKERS = (
    "冲", "扑", "挥", "劈", "斩", "刺", "砍", "击", "踢", "打", "斗", "抓", "抛", "掷",
    "跳", "跃", "翻", "滚", "追", "逃", "跑", "奔", "飞", "坠", "落", "闪", "避", "挡",
    "拔", "抽", "掷", "掐", "捏", "撕", "扯", "推", "撞", "擒", "锁", "绞", "轰", "炸",
    "施法", "结印", "御剑", "掐诀", "催动", "爆发", "猛冲", "疾驰",
)


def required_shot_duration(shot: dict) -> float:
    """该镜头「装得下内容」所需的时长（秒），**不封顶**。

    与 estimate_shot_duration 同源，只是不做 [MIN, MAX] 夹取：返回值 > SHOT_DURATION_MAX
    就说明这个镜头的台词/画面塞不进一个镜头，配音铺到时间轴上会溢出到下一镜、尾部被
    成片 `-shortest` 静默截掉（历史缺陷：ep04 旁白 496 秒铺在 100 秒画面上）。

    生成期用它做对账：超限即拆镜（见 _norm_shots），而不是事后告警。
    """
    # 台词合计时长：_dlg_text 会把同镜多条台词拼起来，正是配音链路的实际喂入量。
    spoken = _dlg_text(shot.get("dialogue"))
    spoken = re.sub(r"^[^：:]{1,12}[：:]", "", spoken)               # 去掉“角色名：”前缀
    if not spoken:
        spoken = str(shot.get("dialogue_text") or "").strip()
    speak_sec = len(spoken) / CHARS_PER_SECOND if spoken else 0.0

    desc = " ".join(x for x in (
        str(shot.get("description") or ""),
        str(shot.get("visual_detail") or ""),
    ) if x).strip()
    desc_sec = min(2.0, len(desc) / 60.0)
    # 动作复杂度加成：description 里动作过程词/动词越多，画面越需要停留时间。
    # 历史缺陷：动作镜与静景镜一律 3 秒基准，动作戏（打斗/追逐/施法）观感仓促。
    action_sec = 0.0
    if desc:
        action_hits = sum(1 for kw in _ACTION_MARKERS if kw in desc)
        if action_hits:
            action_sec = min(1.5, 0.4 + action_hits * 0.15)
    return SHOT_DURATION_SILENT + speak_sec + desc_sec + action_sec


def estimate_shot_duration(shot: dict) -> float:
    """按画面 + 台词长度自动推算单镜头时长（秒），保证同一剧本多次运行结果稳定。

    台词兼容两种写法：结构化 [{"speaker","text"}] / 旧字符串（含 "角色名：台词" 前缀）。

    2026-09-19 优化：纯动作/无台词镜头不再一律给 3 秒 ——
    - description 里动作要素越多（动词/动作过程词），画面停留越久（动作镜需要呈现完整过程）；
    - 动作描写长的镜头（描述超 60 字）给更高基准，避免「动作才做一半就切走」。

    ⚠️ 值被夹在 [SHOT_DURATION_MIN, SHOT_DURATION_MAX]：返回值**无法**表达「装不下」。
    需要判断是否溢出请用 required_shot_duration()。
    """
    return round(max(SHOT_DURATION_MIN,
                     min(SHOT_DURATION_MAX, required_shot_duration(shot))) * 2) / 2.0


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


def _keep_valid_h3(raw) -> str:
    """只保留结构合规的 H3 提示词，其余丢弃

    合规 = 六段式（Ref2VA）或三段式（base）齐全，见 :mod:`h3_prompt_kit`。
    剧本阶段模型写的裸英文描述必然不合规，会被丢弃；提示词分析器产出的
    规范文本会被保留，用户的「重新生成提示词」成果不会被下一次 script
    归一化抹掉。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return text if h3_prompt_kit.validate(text)["valid"] else ""
    except Exception:  # noqa: BLE001 —— 校验失败不应影响剧本生成主流程
        return ""


def _overflow_detail(raw, limit: int) -> str:
    """把超长描述的「超出部分」拆出来（不丢画面细节，供 visual_detail 使用）。

    - 空串 / 未超长 → 返回空串（visual_detail 不重复 description）；
    - 超长 → 从第 limit 个字符开始截取，去头尾空白，限 400 字。
    """
    s = str(raw or "").strip()
    if len(s) <= int(limit):
        return ""
    return s[int(limit):].strip()[:400]


def _match_known_names(raw_names, known: list, field: str) -> list:
    """把镜头声明的角色/物品名收敛到 bible 名单内（**禁止静默 take-first**）。

    P1-16 修复：旧实现用 `[...] if c in chars] or chars[:1]` —— 名字与 bible 对不上时
    静默填入首个角色（通常是主角），使整集以**错误角色**为外观锚点（日志/界面看不出来），
    并把下游 S6「禁止静默 take-first」的 `_no_reference` 分支彻底架空（列表恒非空）。
    现在只保留**精确命中 bible** 的名字（去重保序）；匹配不到即返回空列表，交由下游
    `_allocate_storyboard_refs` 的 no_reference 分支显式告警/跳过，并在「有输入但全未
    命中」时记 warning，保证排障可见。
    """
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    names = [n for n in (raw_names or []) if n]
    if not names:
        return []
    known_set = set(known or [])
    seen, out = set(), []
    for n in names:
        if n in known_set and n not in seen:
            seen.add(n)
            out.append(n)
    if not out:
        logger.warning(
            "镜头 %s 声明的名字 %s 均未命中 bible（可用：%s）→ 保留空列表，"
            "交由下游 no_reference 显式告警/跳过（禁止静默兜底取错角色）",
            field, "、".join(names)[:80],
            "、".join(x for x in (known or []) if x)[:120])
    return out


# 实质台词文本判据（P0-2 补修 / task#9）：
# 一条台词只有当它含「实质字符」（CJK / 字母 / 数字）才算"有台词内容"。
# 纯标点（"。！？"）、空 text（[{"text":""}]）、结构化空壳（[{"text":""}]）
# 一律视为**无实质台词**——与下游 prompt_qc「缺少画面描述」的硬不变量对齐。
_DLGM_SUBSTANTIVE_RE = re.compile(r"[\u4e00-\u9fff\w]")


def _has_meaningful_dlg(raw, chars: list) -> bool:
    """归一化后是否含**实质**台词文本（单一判据，供「丢弃闸门」与「画面补齐」同源使用）。

    task#9 修复的根因：旧实现里丢弃闸门用**原始值真值**（``bool([{"text":""}])`` /
    ``bool("。！？".strip())`` 都为 True），而画面补齐条件用**归一化后真值**（这两类
    归一化后 dialogue 为 []）→ 两处口径不一致 → 「结构化空壳 / 纯标点」镜头**既不被丢弃
    也不被补齐** → 空壳镜在分镜步永久卡死。本函数以「归一化后是否含实质字符」为唯一
    判据，让两处共用，消除分歧。
    """
    for line in _dlg_lines(raw, chars, chars):
        if _DLGM_SUBSTANTIVE_RE.search(str(line.get("text") or "")):
            return True
    return False


def _norm_shots(raw_shots: list, bible: dict, episodes: int, start_id: int = 1) -> list:
    scenes = [s.get("name") for s in (bible.get("scenes") or []) if isinstance(s, dict)]
    chars = [c.get("name") for c in (bible.get("characters") or []) if isinstance(c, dict)]
    items = [i.get("name") for i in (bible.get("items") or []) if isinstance(i, dict)]
    # 风格：镜头级落一次 style，下游（分镜图 / 视频提示词）才有值可用。
    # 历史缺陷：这里不写 style，导致 comfyui_client 里 shot.get("style", "3D动漫渲染")
    # 永远回落硬编码默认值 —— 用户与总控敲定的风格一个镜头都传不到。
    shot_style = style_kit.normalize_style(bible.get("style"))
    shots = []
    sid = start_id
    dropped_empty = []
    dropped_deprecated = []   # 已废弃字段（DEPRECATED_SHOT_FIELDS）被模型越界输出的次数
    for s in raw_shots:
        if not isinstance(s, dict):
            continue
        # 已废弃字段可见化（见 DEPRECATED_SHOT_FIELDS）：模型若仍吐出 narration 等已废弃字段，
        # 这里显式记账并在本函数末尾打一条 warning —— 让「重新写旁白」被**可见地拒绝/告警**，
        # 而不是靠注释里的君子协定蒙混过关。字段本身仍照旧丢弃，不改变任何业务行为。
        for _dep_field in DEPRECATED_SHOT_FIELDS:
            if str(s.get(_dep_field) or "").strip():
                dropped_deprecated.append(_dep_field)
        # 空壳镜头（画面描述 / 补充细节 / 台词 三者皆空）**必须在这里丢掉**。
        # 历史缺陷：模型偶尔会吐出一条只有 camera/location/emotion 的幽灵镜头
        #（实测《蛊真人》ep02 shot_02：description/visual_detail/dialogue/audio_cues 全空），
        # 本函数原样收下 → 生成期 prompt_qc 判「镜头缺少画面描述」**致命缺陷且不可自愈**
        # → 该镜永远出不了图 → probe_storyboard 永远缺 1 镜 → 整集在分镜步永久卡死，
        # 且用户在界面上拿不到任何可操作的补救入口。
        # 这类镜头不承载任何原文内容（原文覆盖率校验不会因此丢句），丢掉是零损失；
        # 若确实有原文没被承载，后续 coverage 补生成会按原文补回一条**有内容**的镜头。
        # 注意：audio_cues 不参与判定 —— 只有音效没有画面的镜头同样出不了图。
        _has_vis = bool(str(s.get("description") or "").strip()
                        or str(s.get("visual_detail") or "").strip()
                        or str(s.get("storyboard_prompt_zh") or "").strip())
        # task#9 补修：台词判据与下方「画面补齐」同源（归一化后是否含实质文本），
        # 不再用原始值真值——否则 `[{"text":""}]` / `"。！？"` 既骗过闸门又不被补齐。
        _has_dlg = _has_meaningful_dlg(s.get("dialogue"), chars)
        if not (_has_vis or _has_dlg):
            dropped_empty.append(s.get("camera") or s.get("location") or "?")
            continue
        loc = str(s.get("location") or "").strip()
        if scenes and loc and loc not in scenes:
            # P1-16 修复（场景侧）：匹配不到时**保留原 loc** 并记 warning，绝不静默回落
            # `scenes[0]`。旧行为会把「破败的大殿」这类不在 bible 里的场景名换成「后山」
            # 这类首个场景 —— 环境锚点整集级错位，且日志/界面看不出来。
            _hit = next((n for n in scenes if n and n in loc), None)
            if _hit:
                loc = _hit
            else:
                logger.warning(
                    "镜头场景名未命中 scenery bible：%r（可用：%s）→ 保留原文，"
                    "不静默回落首个场景", loc, "、".join(x for x in scenes if x)[:120])
        if not loc and scenes:
            loc = scenes[0]
        row = {
            "shot_id": sid,
            "duration": 5,
            "camera": str(s.get("camera") or "中景").strip()[:20] or "中景",
            "location": loc,
            # description：限长 200 字（前端展示与 prompt 体量控制用）。
            # 但画面细节不丢：原始描述若超长，把超出的部分拆进 visual_detail（分镜图/视频
            # 提示词会把它并回画面主体）。历史缺陷：description 截断 200 字后剩余细节
            # 直接丢失，导致「动作完整、光影明确」的要求只能靠模型猜。
            "description": str(s.get("description") or "").strip()[:200],
            # visual_detail：优先用模型直接输出的字段（分镜 schema 已要求模型把超出
            # description 的更细画面细节写这里）；模型没给时用 _overflow_detail 兜底
            #（description 截断 200 字后的剩余部分）。供 build_storyboard_prompt /
            # h3_prompt_kit 等下游取用。
            "visual_detail": (str(s.get("visual_detail") or "").strip()[:400]
                              or _overflow_detail(s.get("description"), 200)),
            # narration：**已废弃字段（登记于 DEPRECATED_SHOT_FIELDS），此处显式丢弃**；
            # 模型若越界输出会在本函数末尾打一条 warning（可见地拒绝，不是注释君子协定）。
            # 历史缺陷：narration 被当成「心理活动 + 背景补叙 + 环境描写」的公共出口，
            # 再叠加当时的「每镜必须有人声」约束，导致原著所有叙述性文字都变成画外音解说
            #（实测 ep04 旁白 2231 字 ≈ 496 秒，铺在 100 秒画面上 → 4.93x 溢出，尾部被
            # 成片 -shortest 静默截断）。现在剧本阶段不再产出旁白，这里也不再透传模型的
            # 越界输出，保证「成片无旁白」是硬不变量而不是提示词君子协定。
            # 旧剧本文件里残留的 narration 由读取侧（coverage / h3_prompt_kit / tts_client）
            # 按需兼容，这是 DEPRECATED_SHOT_FIELDS 里 narration 唯一的合法消费方。
            # 台词：结构化 [{"speaker","text"}]（分镜阶段直接写明说话人，配音链路直接读取）
            "dialogue": _dlg_lines(s.get("dialogue"), chars, chars),
            "emotion": str(s.get("emotion") or "平静").strip()[:20],
            "audio_cues": str(s.get("audio_cues") or "").strip()[:60],
            # 视频提示词：**剧本阶段不再信任模型自写的文本**。
            # 历史缺陷：这里原样保留模型写的「英文画面描述（60 词以内）」，一句无
            # <Picture N> 标签的裸英文，会在生成期把结构化 H3 构建器整个顶掉
            #（app.py 原写法 `shot.get('prompt_h3') or _build_h3_prompt(...)`），
            # 实测全项目 200+ 镜头的结构化提示词数量为 0。
            # 现在只保留「本身已合规」的提示词（例如提示词分析器产出的六段式），
            # 其余一律丢弃，交由生成期 h3_prompt_kit 按当次参考图规范重建。
            "prompt_h3": _keep_valid_h3(s.get("prompt_h3")),
            "style": shot_style,          # ← 风格注入：分镜图/视频提示词的风格来源
            # P1-16 修复（角色侧）：**去掉 `or chars[:1]` 兜底**。旧行为在「镜头角色名与
            # bible 对不上」时静默填入首个角色（通常是主角），使整集以错误角色为外观锚点，
            # 且日志/界面看不出来 —— 同时把下游 S6「禁止静默 take-first」的 `_no_reference`
            # 分支彻底架空（列表恒非空）。现在只保留**精确命中 bible** 的角色，匹配不到即留空，
            # 由下游 `_allocate_storyboard_refs` 的 no_reference 分支显式告警/跳过。
            "characters_in_shot": _match_known_names(
                s.get("characters_in_shot"), chars, "characters_in_shot"),
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
        # P0-2 修复（上游补齐）：只有台词、没有画面描述的镜头，在生成期提示词预检里会命中
        #   「镜头缺少画面描述（description / visual_detail / storyboard_prompt_zh 均为空）」
        # 这条**致命且不可自愈**的缺陷（prompt_qc._check_storyboard → fatal）→ 该镜永远
        # 出不了图 → probe_storyboard 永远缺 1 镜 → **整集在分镜步永久卡死**。
        # 这里在上游用**台词上下文**补齐一条画面描述，使该镜带「画面内容」进入生成。
        # ⚠️ 刻意**不写入台词原文**：台词进画面提示词会被模型渲染成字幕（prompt_qc 的硬原则），
        #    且会被 description 复用方（H3/尾帧/图片质检）当成「镜头内容」——那是以台词冒充
        #    画面，属于新缺陷。故只描述「说话人物的表演」，不含任何台词文本。
        # 真·四字段全空（含 task#9 的「结构化空壳 / 纯标点」形状）的幽灵镜头已在上方丢弃。
        # 补齐判据与上方丢弃闸门**同源**（都走 _has_meaningful_dlg），避免两处口径再次漂移：
        # 只有「归一化后确有实质台词文本」才补画面描述；纯标点/空壳既已被丢弃，此处恒假。
        if not (row["description"] or row["visual_detail"]) \
                and _has_meaningful_dlg(s.get("dialogue"), chars):
            _spk = []
            for _d in row["dialogue"]:
                _nm = (_d.get("speaker") or "").strip() if isinstance(_d, dict) else ""
                if _nm and _nm not in _spk:
                    _spk.append(_nm)
            _who = "、".join(_spk) or "人物"
            row["description"] = (
                f"{_who}开口说话（本镜以人物台词表演为主，画面聚焦说话人物的口型与神情）"
            )[:200]
        # 单镜头时长：取「模型给的时长」与「内容实际需要的时长」的**较大值**。
        # 历史缺陷：原实现只要模型给了合法值就直接采用（4~5 秒），完全不看这镜有多少台词
        #   → 长台词硬贴在短画面上，配音沿时间轴溢出到后面几镜，成片尾部被 `-shortest` 静默截掉。
        #   （实测《蛊真人》ep04：21/21 镜都直接采用模型值，与 estimate_shot_duration 的返回值全部不一致）
        auto_dur = estimate_shot_duration(row)
        model_dur = None
        try:
            model_dur = float(s.get("duration"))
        except (TypeError, ValueError):
            model_dur = None
        if model_dur and SHOT_DURATION_MIN <= model_dur <= SHOT_DURATION_MAX:
            row["duration"] = round(max(model_dur, auto_dur) * 2) / 2.0
        else:
            row["duration"] = auto_dur
        # 生成期对账：内容确实塞不进单镜上限时**显式记账**，不静默吞咽。
        # 不在这里私自抬高 SHOT_DURATION_MAX —— QC 侧的 SHOT_DURATION_MAX_OK 是同一个口径，
        # 单方面拉高会让成片被剧本质检判「时长过长」。溢出部分由 dub_mix 的 max_line_sec 变速兜底，
        # 该字段供 dialogue_utils.audit_script 提示用户「这一镜台词写多了，建议拆镜」。
        need = required_shot_duration(row)
        if need > SHOT_DURATION_MAX:
            row["duration_overflow_sec"] = round(need - SHOT_DURATION_MAX, 2)
        shots.append(row)
        sid += 1
    if dropped_empty:
        logger.warning("已丢弃 %d 条空壳镜头（无画面描述/细节/台词，出不了图且会卡死整集）：%s",
                       len(dropped_empty), dropped_empty[:12])
    if dropped_deprecated:
        logger.warning(
            "分镜标准化丢弃了模型越界输出的已废弃字段 %s（共 %d 处，见 DEPRECATED_SHOT_FIELDS）："
            "旁白通道已关闭，剧本阶段不再产出 narration；如有叙述性内容，请改写为角色自语台词"
            "（dialogue）或画面描述（description）。",
            "、".join(sorted(set(dropped_deprecated))), len(dropped_deprecated))
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
        # ---- P0-3 剧本↔原著一致性：整本单集路径（1 集 = 整本小说）无单章比较基准，
        #      锚定与要素覆盖显式跳过，只做元信息泄漏扫描 + 命中镜头定向重写。
        try:
            import script_consistency as sc_mod
            sc_rep = sc_mod.run_script_consistency_check(
                client, script, novel_meta=None, chapter_text="", chapter=None,
                episode_no=1, auto_fix=True, events=None,
                continuity_dir=continuity_dir, project_key=project_key, save=True,
                skip_anchor=True, skip_elements=True)
            if reports:
                reports("consistency", total_steps, total_steps,
                        f"剧本一致性：元信息泄漏 "
                        f"{(sc_rep.get('leak') or {}).get('hit_count')} 处，"
                        f"定向修复 {sc_rep.get('fix_rounds')} 轮"
                        f"（{'已修复' if sc_rep.get('fixed') else '留告警'}）", 99.5)
        except Exception as e:  # noqa: BLE001
            note = (f"剧本一致性校验失败（已跳过，剧本仍按原结果落盘）："
                    f"{type(e).__name__}: {str(e)[:200]}")
            warnings.append(note)
            logger.warning(note)
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
                       ["name", "category", "appearance", "owner", "importance",
                        "reference_prompt_zh", "reference_prompt_en"])
    scenes = _norm_list(bible.get("scenes"), 8,
                        ["name", "location", "appearance", "reference_prompt_zh", "reference_prompt_en"])
    if not characters:
        raise LLMError("模型未返回有效角色设定，转换中止")
    # 风格：以调用方传入的 style 为准 + 确定性补写（模型不得自写风格，统一由此收尾）
    eff_style = style_kit.normalize_style(style) or style_kit.normalize_style(bible.get("style"))
    style_filled = style_kit.apply_asset_style_all(characters, eff_style) \
        + style_kit.apply_asset_style_all(items, eff_style) \
        + style_kit.apply_asset_style_all(scenes, eff_style)
    if eff_style and style_filled:
        logger.info("全剧设定：已为 %d 条资产参考提示词补写风格「%s」", style_filled, eff_style)
    bible = {"title": str(bible.get("title") or novel_title)[:40],
             "theme": str(bible.get("theme") or "")[:200],
             "style": eff_style[:60],
             "characters": characters, "items": items, "scenes": scenes,
             "production_notes": bible.get("production_notes") or {}}

    # ③ 逐块写分镜（镜头数按每块原文体量自动扩展，不再按 target_shots 摊薄砍内容）

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
        except LLMGatewayUnavailable as e:
            # 网关问题不是「这一块运气不好」，重试无用 —— 见 _gateway_down_fallback 说明
            fb = _gateway_down_fallback(e, chunk, per_chunk, bible, all_shots)
            all_shots.extend(fb)
            warnings.append(_gateway_down_warning(chunk, len(fb)))
            logger.error(f"第 {chunk['index']} 块因网关不可用降级兜底 {len(fb)} 镜：{e}")
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
                              progress_cb=None, continuity_ctx: dict = None,
                              cache_dir: str = "") -> dict:
    """把「一章」转成一集 A 版剧本（每章一集，独立落盘）

    continuity_ctx（跨集连贯性方案 A/B/C）：由 continuity.build_continuity_context 组装，
    包含项目级设定库、上集摘要卡、衔接契约、项目级风格指南、金句清单、口吻词典与运镜术语表；
    传入后本集生成将带着跨集上下文，且 style_guide 改用项目级唯一配置。

    cache_dir 非空时，提炼 / 分镜两步启用断点缓存：重跑时命中已完成的块即跳过模型调用，
    使「网关偶发挂起 → pipeline 整步重试」不再每次都从头烧 20+ 分钟（见文件上方缓存说明）。
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
            ol = extract_chunk_outline(client, chunk, novel_title, events=trunc_events,
                                       cache_dir=cache_dir)
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
                                 target_shots, events=trunc_events,
                                 continuity_ctx=continuity_ctx, cache_dir=cache_dir))

    characters = _norm_list(bible.get("characters"), 8,
                            ["name", "age", "identity", "appearance", "outfit", "personality",
                             "voice_style", "reference_prompt_zh", "reference_prompt_en"])
    items = _norm_list(bible.get("items"), 6,
                       ["name", "category", "appearance", "owner", "importance",
                        "reference_prompt_zh", "reference_prompt_en"])
    scenes = _norm_list(bible.get("scenes"), 8,
                        ["name", "location", "appearance", "reference_prompt_zh", "reference_prompt_en"])
    if not characters:
        raise LLMError("模型未返回有效角色设定，转换中止")
    # 风格：以调用方传入的 style 为准（模型的返回值可能是自我发挥，用户意图优先）
    eff_style = style_kit.normalize_style(style) or style_kit.normalize_style(bible.get("style"))
    # 确定性补写：模型不得自写风格词（写了会重复），统一在这里收尾，中英双语都补
    style_filled = style_kit.apply_asset_style_all(characters, eff_style) \
        + style_kit.apply_asset_style_all(items, eff_style) \
        + style_kit.apply_asset_style_all(scenes, eff_style)
    if eff_style and style_filled:
        logger.info("第%s集：已为 %d 条资产参考提示词补写风格「%s」", episode_no, style_filled, eff_style)
    bible = {"title": str(bible.get("title") or novel_title)[:40],
             "theme": str(bible.get("theme") or "")[:200],
             "style": eff_style[:60],
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
                                                   continuity_ctx=continuity_ctx,
                                                   cache_dir=cache_dir))
        except LLMTruncatedError as e:
            fb = _fallback_shots_for_chunk(chunk, per_chunk, bible)
            all_shots.extend(fb)
            warnings.append(f"第 {chunk['index']} 子块分镜被截断失败（已自动提额并尝试二次切分），"
                            f"已按原文兜底生成 {len(fb)} 镜（内容未丢，建议人工润色）：{e}")
            logger.warning(f"第 {chunk['index']} 子块分镜被截断失败，已兜底 {len(fb)} 镜：{e}")
        except LLMGatewayUnavailable as e:
            fb = _gateway_down_fallback(e, chunk, per_chunk, bible, all_shots)
            all_shots.extend(fb)
            warnings.append(_gateway_down_warning(chunk, len(fb)))
            logger.error(f"第 {chunk['index']} 子块因网关不可用降级兜底 {len(fb)} 镜：{e}")
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
