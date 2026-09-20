"""
prompt_memory.py — AI 提示词记忆库

作用：把「质检说出的具体缺陷 + 当时用的提示词」沉淀成一条条经验，
下次构造同类提示词时自动把经验补进去，避免重复犯同样的错。

数据落盘：output/lessons/lessons.jsonl（追加式，逐条记录）
语义：以「缺陷关键词」为依据做匹配，命中则把对应的修正建议追加进
生成的提示词。

⚠️ 唯一实例契约（T01）：
    本模块**有且只有一个**内存列表与**唯一一个**写入者。
    历史缺陷：`PromptMemory`（基础）与 `PromptMemoryEnhanced`（增强）各持一份
    内存列表，却把 `_path` 定死为同一个 `lessons.jsonl`，而 `_flush()` 用
    `open(path, "w")` 整文件覆盖 → 两份列表抢写同一文件、last-writer-wins，
    增强侧刚写的 `kind="script"` 会被基础侧的下一次 flush 整体抹掉。
    现收敛为「唯一实现 `PromptMemory`」：增强能力（分类/优先级/上下文/衰减）
    整体折入，`PromptMemoryEnhanced` 退化为空壳别名；`get_enhanced_memory`
    与 `get_memory` 返回**同一对象**；`_flush()` 改为原子写（唯一临时名 +
    fsync + `os.replace` 退避重试），彻底消除双单例互抹与半截文件。

用法：
    mem = PromptMemory(root_dir)
    mem.record(proj, kind, prompt, issues, reason)     # 质检不达届时调用，记教训
    mem.suggestions(kind, prompt) -> [str]             # 生成提示词前调用，取修正建议
    mem.learned_prompt(kind, prompt, text_block)       # 拼装成最后的大提示词
    mem.hash(prompt) -> str                            # 提示词指纹
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

# 修正建议侧的「规则关键词种子」：质检 defect 文案经过这些词归一化后，
# 才能稳定命中同名教训。规则按优先级排列，命中即停。
#
# 注意：material 类缺陷（发色/服饰/冠饰等具体道具）无法用统一规则表达，
# 但质检的 issue 里通常会带上原文专有名词，我们把这些专有名词也作为
# 匹配键的一部分（通过 extract_core_terms），因此「发冠样式错误」这类
# 长期会通过「专有名词 + 类别」的组合命中和复发。
_MATCH_RULES: List[str] = [
    "构图", "遮挡", "面部", "发色", "发冠", "发型", "服饰", "配色", "衣服",
    "背景", "场景", "光影", "对比", "畸变", "糊", "模糊", "噪",
    "灯笼", "水印", "字幕", "肢体", "手", "脸", "眼睛", "比例", "拥挤",
    "人物数量", "多余人", "面板", "文字", "logo", "重复", "武器", "剑",
    "风格", "画风",
]

# 修正建议为空时，按类别给一个兜底建议
_CLASS_HINTS: dict = {
    "构图": "调整构图，突出主体，避免主体被边缘裁切或过小。",
    "遮挡": "避免前景杂物遮挡主体面部与关键动作。",
    "面部": "进一步刻画面部细节，避免五官模糊或扭曲。",
    "发色": "严格保持参考图中的发色与发质，不得自由改变。",
    "发冠": "严格还原参考图中的发冠/发饰样式与位置。",
    "发型": "严格还原参考图中的发型，不得改变扎法或发髻。",
    "服饰": "严格保持参考图中的服装款式、颜色与材质。",
    "配色": "全图色调统一，避免局部色彩跳变或过饱和。",
    "背景": "丰富背景层次，避免大面积空白或单调墙面。",
    "场景": "增强场景通透感与空间透视，避免扁平。",
    "光影": "强化主光源方向与体积光影，避免平光或死黑。",
    "对比": "增强主体与背景的明暗对比，拉开层次。",
    "畸变": "修复透视与形体畸变，人物比例自然。",
    "糊": "提升清晰度，消除噪点、涂抹与模糊。",
    "模糊": "提升清晰度，消除模糊与运动拖影。",
    "噪": "降低噪点，提升画面纯净度。",
    "手": "修正手部结构，手指数量与关节自然。",
    "肢体": "修正肢体比例与关节衔接，避免僵硬。",
    "脸": "优化五官结构，避免脸型畸变。",
    "眼睛": "优化眼部细节，眼神聚焦。",
    "比例": "修正人物与环境比例关系。",
    "拥挤": "精简画面元素，避免过度拥挤。",
    "人物数量": "严格按分镜要求的人物数量绘制，不增减人。",
    "多余人": "移除画面中多余的人物或物体。",
    "武器": "严格还原参考图中的武器造型与细节。",
    "剑": "严格还原参考图中的剑/武器造型，不得变形或换款。",
    "风格": "严格采用目标风格的画风、渲染方式与配色，不得偏离为其他风格。",
    "画风": "严格采用目标风格的画风、渲染方式与配色，不得偏离为其他风格。",
}

# 每条经验最多存活条数（防止无限膨胀）
_MAX_LESSONS = 2000
# 单条经验最长保留天数（避免过时教训长期生效）
_MAX_AGE_DAYS = 120


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(text: str) -> str:
    """归一化用于匹配：去空白、转小写，保留中英文与数字。"""
    return re.sub(r"[\s]+", "", (text or "")).lower()


# ===================== 提示词指纹与素材提取 =====================

def prompt_hash(prompt: str) -> str:
    """提示词指纹：去掉空白后的 sha1 前 12 位，用于发现「同提示词反复失败」。"""
    p = _norm(prompt)
    if not p:
        return ""
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:12]


def extract_core_terms(text: str) -> List[str]:
    """从缺陷描述 / 提示词里尽量抽出「原文专有名词」作为匹配键补充。

    例如质检说「发冠样式与设定不符，应为白玉莲花冠」→ 抽出 [白玉莲花冠]。
    这些专有名词能显著提高「同素材复发」时的命中率。
    """
    terms: List[str] = []
    # 中文连续词（去标点）2~6 字的词组
    for seg in re.findall(r"[\u4e00-\u9fff]{2,6}", text or ""):
        if len(seg) >= 2:
            terms.append(seg)
    # 双引号/书名号内的专有名词优先
    for seg in re.findall(r"[《“\"「『]([^》”\"」』]{1,10})[》”\"」』]", text or ""):
        if seg.strip():
            terms.append(seg.strip())
    # 去重保序
    seen: set = set()
    out: List[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:60]


# ===================== 数据契约：分类 / 优先级 / 主键 / 惰性补齐 =====================

# 问题分类定义
ISSUE_CATEGORIES = {
    "structure": "结构问题",
    "logic": "逻辑问题",
    "style": "风格问题",
    "prompt_quality": "提示词质量问题",
    "feasibility": "可执行性问题",
    "visual": "视觉问题",
    "consistency": "一致性问题",
}

# 优先级定义
PRIORITY_LEVELS = {
    "critical": {"weight": 1.0, "decay_days": 180},  # 严重问题，长期保留
    "high": {"weight": 0.8, "decay_days": 120},      # 高优先级
    "medium": {"weight": 0.5, "decay_days": 60},     # 中等优先级
    "low": {"weight": 0.2, "decay_days": 30},        # 低优先级
}

# 问题分类关键词映射
_CATEGORY_KEYWORDS = {
    "structure": ["缺少字段", "格式错误", "JSON", "结构", "必填", "字段"],
    "logic": ["逻辑", "矛盾", "不一致", "引用", "未定义", "角色", "物品"],
    "style": ["风格", "不符", "不匹配", "不一致"],
    "prompt_quality": ["描述过短", "描述不详细", "缺少描述", "提示词"],
    "feasibility": ["时长", "过长", "过短", "镜头数量", "偏差"],
    "visual": ["畸变", "崩坏", "模糊", "糊化", "噪点", "色块", "撕裂"],
    "consistency": ["不一致", "变化", "差异", "不同"],
}


def categorize_issue(issue: str) -> str:
    """自动分类问题类型"""
    issue_lower = (issue or "").lower()

    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in issue_lower:
                return category

    return "visual"  # 默认分类


def assess_priority(issues: List[str], score: int = 100) -> str:
    """评估问题优先级"""
    if not issues:
        return "low"

    # 计算严重程度
    critical_count = 0
    high_count = 0

    critical_keywords = ["结构", "逻辑矛盾", "角色缺失", "物品缺失", "严重畸变", "崩坏"]
    high_keywords = ["风格不符", "时长偏差", "描述过短"]

    for issue in issues:
        issue_lower = issue.lower()
        if any(kw in issue_lower for kw in critical_keywords):
            critical_count += 1
        elif any(kw in issue_lower for kw in high_keywords):
            high_count += 1

    # 根据分数和问题严重程度判断优先级
    if critical_count > 0 or score < 50:
        return "critical"
    elif high_count > 0 or score < 70:
        return "high"
    elif len(issues) > 3 or score < 80:
        return "medium"
    else:
        return "low"


def _calculate_decay_weight(lesson: dict) -> float:
    """计算衰减权重"""
    ts_str = lesson.get("ts", "")
    if not ts_str:
        return 1.0

    try:
        ts = datetime.fromisoformat(ts_str)
        days_elapsed = (datetime.now() - ts).days

        priority = lesson.get("priority", "medium")
        decay_days = PRIORITY_LEVELS.get(priority, {}).get("decay_days", 60)

        # 线性衰减
        if days_elapsed >= decay_days:
            return 0.1  # 最低权重
        return 1.0 - (days_elapsed / decay_days) * 0.9
    except (ValueError, TypeError):
        return 1.0


def derive_lesson_id(kind: str, phash: str, ts: str) -> str:
    """确定性派生教训主键：``"L" + sha1(f"{kind}\\x1f{phash}\\x1f{ts}")[:16]``。

    为什么必须确定性：存量教训（如 71 条）**没有** `lesson_id` 字段，若用随机
    主键则无法在「不迁移文件」的前提下被删除 / 计数回写。用
    (kind, phash, ts) 三元组稳定派生后，加载时即可为旧记录补齐同一 id。
    唯一性依据：同 `kind+phash` 的旧记录本就被 `record()` 的合并逻辑
    （保留最新一条）收敛，故 `ts` 足以区分；「同秒同 kind 同 phash」会被去重
    合并成一行，不产生冲突。
    """
    raw = f"{kind or ''}\x1f{phash or ''}\x1f{ts or ''}"
    return "L" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_lesson(d: dict) -> dict:
    """纯函数：为一条教训补齐数据契约字段（惰性迁移，不写文件、不改入参）。

    - `lesson_id`：缺失时按 (kind, phash, ts) 确定性派生；
    - `use_count`：缺失或 None 时**一律补 0**（⚠️ 不按 `ts` 估算；
      `use_count` 的产品价值正是识别「记了却从未被召回的死教训」，伪造初值
      会摧毁该判据）；
    - `last_used`：缺失时补 None。

    已携带这些字段的记录原样保留（返回浅拷贝），因此不会覆盖新记录写入的
    真实 `use_count` / `last_used`。
    """
    if not isinstance(d, dict):
        return {}
    out = dict(d)
    if not out.get("lesson_id"):
        out["lesson_id"] = derive_lesson_id(
            out.get("kind") or "", out.get("phash") or "", out.get("ts") or "")
    if out.get("use_count") is None:
        out["use_count"] = 0
    if "last_used" not in out:
        out["last_used"] = None
    return out


# ===================== 原子落盘 =====================

def _atomic_replace(tmp: str, path: str) -> None:
    """`os.replace` + Windows 共享冲突退避重试。

    ⚠️ Windows 上若目标文件正被另一个线程/进程打开读取（例如刚读完、
    句柄尚未释放），`os.replace` 会抛 `PermissionError: [WinError 5] 拒绝访问`。
    这属于「暂时拿不到」而不是「文件坏了」：直接失败会让正常落盘偶发失败
    （实测并发读写时必现）。退避重试即可。与 `ai_chat._atomic_replace` /
    `project_store._atomic_replace` 同型（本模块内联实现，避免跨模块硬依赖）。
    """
    last = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:        # WinError 5 / 32：目标被占用
            last = e
            time.sleep(0.02 * (i + 1))
    raise last


# ===================== 记忆库主体 =====================

class PromptMemory:
    """AI 提示词记忆库（唯一实现）。

    增强能力（分类 / 优先级 / 上下文 / 衰减 / 学习曲线）已由原
    `PromptMemoryEnhanced` **整体折入**：全模块只有一个内存列表、一个 `_path`、
    一个写入者。
    """

    #: 风格类建议里的占位符：召回时用**当前项目**的风格替换
    #:
    #: 为什么用占位符而不是记录时就把风格名写死：教训是跨项目复用的，
    #: 而「严格采用 X 风格」里的 X 是**项目专属**的。若记录时就写死，
    #: A 项目（中国古风玄幻）的风格教训被 B 项目（国漫偏写实）召回时，
    #: 会把 A 的风格强行扣到 B 上 —— 这不是没帮助，是主动伤害。
    STYLE_PLACEHOLDER = "{style}"

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self._dir = os.path.join(root_dir, "lessons")
        # ★ 全模块唯一：所有实例共享同一落盘路径语义（此处每个 root_dir 一个实例，
        #   由 get_memory 保证同 root_dir 复用同一对象）。
        self._path = os.path.join(self._dir, "lessons.jsonl")
        self._lock = threading.RLock()
        os.makedirs(self._dir, exist_ok=True)
        self._lessons: list = self._load()

    # ---------- 落盘 ----------
    def _load(self) -> list:
        """读入全部教训，逐条经 `_normalize_lesson` 补齐数据契约字段。

        对 Windows 的**瞬时共享冲突**（`os.replace` 换目录项瞬间 `open()` 拿不到
        句柄）做退避重试；其余读取异常按既有容错语义只告警、读到的有效条数照旧。
        """
        out: list = []
        if not os.path.isfile(self._path):
            return out
        handle = None
        for attempt in range(6):
            try:
                handle = open(self._path, "r", encoding="utf-8")
                break
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"提示词记忆读取失败：{e}")
                return out
        if handle is None:
            logger.warning("提示词记忆读取反复被占用（已重试 6 次），跳过本次加载")
            return out
        try:
            with handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(obj, dict):
                        out.append(_normalize_lesson(obj))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"提示词记忆读取失败：{e}")
        return out

    def _flush(self):
        """原子覆盖写：唯一临时名 + flush + fsync + `os.replace` 退避重试。

        ⚠️ 必须原子：旧实现直接以写模式 open 目标路径（先截断目标文件再写），
        并发写者会互相截断，进程崩溃/断电会留下半截文件；固定 `.tmp` 名也会被
        并发写者互相截断。临时名每次唯一（pid + tid + 随机段）+ fsync 后 replace，
        保证读者要么看到旧全文、要么看到新全文。
        失败时清理临时文件并保持既有「记录失败只 warning 不抛」的容错语义。
        """
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"提示词记忆写入失败：{e}")
            return
        tmp = f"{self._path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for item in self._lessons:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())        # 先落盘再 replace，避免断电后只剩空文件
            _atomic_replace(tmp, self._path)
        except Exception as e:  # noqa: BLE001
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)          # 失败不留垃圾临时文件
            except OSError:
                pass
            logger.warning(f"提示词记忆写入失败：{e}")

    # ---------- 内部：追加一条并去重/裁剪/落盘 ----------
    def _commit_lesson(self, lesson: dict) -> dict:
        """把一条教训追加进内存列表，做合并/去重/裁剪后原子落盘。

        `record()` 与 `record_with_context()` 共用此路径，保证两条写入路径
        「产出同形记录、行为等价」。
        """
        ph = lesson.get("phash") or ""
        kind = lesson.get("kind") or ""
        with self._lock:
            self._lessons.append(lesson)
            # 同提示词同缺陷的旧记录合并（保留最新一条），避免重复条目膨胀
            self._lessons = [x for x in self._lessons
                             if not (x.get("phash") == ph and x.get("kind") == kind
                                     and (x.get("ts") or "") < (lesson.get("ts") or ""))]
            # 去重：真实语义上已被保留的最新条目
            seen = set()
            dedup = []
            for x in reversed(self._lessons):
                key = (x.get("phash") or "", x.get("kind") or "", _norm(" ".join(x.get("issues") or [])))
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(x)
            self._lessons = list(reversed(dedup))
            # 上限裁剪
            if len(self._lessons) > _MAX_LESSONS:
                self._lessons = self._lessons[-_MAX_LESSONS:]
            self._flush()
        return lesson

    # ---------- 记录一条教训 ----------
    def record(self, project: str, kind: str, prompt: str,
               issues: List[str], reason: str = "",
               score: Optional[int] = None) -> dict:
        """质检不达标时调用：把「提示词 + 缺陷」沉淀为一条经验，供后续召回。

        `category` / `priority` 由 `categorize_issue` / `assess_priority` **自动推断**，
        与 `record_with_context()` 产出**同形记录**（都带 category/priority/context/
        decay_weight/use_count/last_used）。
        """
        ph = prompt_hash(prompt)
        if not ph:
            return {}
        defects = [str(x) for x in (issues or []) if str(x).strip()]
        if not defects and reason:
            defects = [reason]
        ts = _now()
        lesson = {
            "lesson_id": derive_lesson_id(kind, ph, ts),
            "ts": ts,
            "project": project or "",
            "kind": kind,
            "category": categorize_issue(defects[0] if defects else (reason or "")),
            "priority": assess_priority(defects, score if isinstance(score, int) else 100),
            "context": {},
            "phash": ph,
            "prompt": (prompt or "")[:3000],
            "issues": defects[:20],
            "reason": (reason or "")[:500],
            "score": score,
            "terms": extract_core_terms((reason or "") + " " + " ".join(defects))[:40],
            "decay_weight": 1.0,
            "use_count": 0,
            "last_used": None,
        }
        return self._commit_lesson(lesson)

    # ---------- 记录一条带上下文的教训（原增强版能力，折入） ----------
    def record_with_context(self, project: str, kind: str, category: str,
                            priority: str, context: dict, prompt: str,
                            issues: List[str], reason: str = "",
                            score: Optional[int] = None) -> dict:
        """带上下文和分类的记录。

        与基础 `record()` 产出同形记录，区别仅在于 `category/priority/context`
        由调用方显式传入（而非自动推断）。
        """
        ph = prompt_hash(prompt)
        if not ph:
            return {}

        defects = [str(x) for x in (issues or []) if str(x).strip()]
        if not defects and reason:
            defects = [reason]

        ts = _now()
        lesson = {
            "lesson_id": derive_lesson_id(kind, ph, ts),
            "ts": ts,
            "project": project or "",
            "kind": kind,
            "category": category,
            "priority": priority,
            "context": context or {},
            "phash": ph,
            "prompt": (prompt or "")[:3000],
            "issues": defects[:20],
            "reason": (reason or "")[:500],
            "score": score,
            "terms": extract_core_terms((reason or "") + " " + " ".join(defects))[:40],
            "decay_weight": 1.0,
            "use_count": 0,
            "last_used": None,
        }
        return self._commit_lesson(lesson)

    # ---------- 召回修正建议 ----------
    def suggestions(self, kind: str, prompt: str,
                    project: str = "", max_hints: int = 3,
                    style: str = "") -> List[str]:
        """生成提示词前调用：按 kind + 提示词相似度 + 项目归属，召回修正建议。

        打分构成：
        - ``sim``：提示词指纹完全命中 1.0；否则按文本相似度近似；
        - ``kw_hit``：该教训的缺陷关键词是否出现在**当前查询提示词**里
          （⚠️ 历史缺陷：原先拿教训自己的 prompt 去匹配自己的 terms，恒等命中，
          贡献了一个与相关性无关的常数分，等于把召回变成了随机取样）；
        - ``proj_bonus``：同项目教训加权，跨项目教训降权（跨项目经验仍有参考价值，
          但同类名/同场景的教训显然更对症）。
        """
        ph = prompt_hash(prompt)
        qp = _norm(prompt or "")
        scored: List[tuple] = []
        for l in self._lessons:
            if l.get("kind") and l.get("kind") != kind:
                continue
            # 空教训（无缺陷也无结论）不参与召回 —— 历史脏数据里有整批这种记录
            if not (l.get("issues") or l.get("reason")):
                continue
            # 1) 提示词指纹命中（同句提示词曾经失败）——最强信号
            sim = 0.0
            if ph and l.get("phash") == ph:
                sim = 1.0
            # 2) 提示词文本相似（编辑距离近似）
            elif l.get("prompt"):
                sim = self._similar(l.get("prompt"), prompt)
            # 3) 缺陷关键词命中（当前提示词里有「曾经出问题的词」）
            kw_hit = 0.0
            for t in (l.get("terms") or []):
                if t and qp and t in qp:
                    kw_hit += 1.0
            # 4) 项目归属加权/降权
            lp = str(l.get("project") or "").strip()
            if lp and project:
                proj_bonus = 0.15 if lp == project else -0.10
            else:
                proj_bonus = 0.0
            total = sim + min(kw_hit, 3.0) * 0.25 + proj_bonus
            if total > 0.2:
                scored.append((total, l))
        scored.sort(key=lambda x: x[0], reverse=True)
        hints, seen = [], set()
        for _, l in scored:
            for iss in (l.get("issues") or []):
                text = self._render_hint(iss, style)
                if not text:
                    continue          # 需要风格但本次没给风格 → 丢弃这条，不要误导
                key = _norm(text)
                if key and key not in seen:
                    seen.add(key)
                    hints.append(text)
                if len(hints) >= max_hints:
                    break
            if len(hints) >= max_hints:
                break
        return hints

    # ---------- 带优先级和上下文的召回（原增强版能力，折入） ----------
    def suggestions_with_priority(self, kind: str, prompt: str,
                                  context: dict = None, max_hints: int = 5) -> List[dict]:
        """带优先级和上下文的召回"""
        ph = prompt_hash(prompt)
        scored: List[tuple] = []

        for l in self._lessons:
            if l.get("kind") and l.get("kind") != kind:
                continue

            # 计算衰减权重
            decay_weight = _calculate_decay_weight(l)
            if decay_weight < 0.1:
                continue  # 跳过严重衰减的记忆

            # 1) 提示词指纹命中
            sim = 0.0
            if ph and l.get("phash") == ph:
                sim = 1.0
            # 2) 提示词文本相似
            elif l.get("prompt"):
                sim = self._similar(l.get("prompt"), prompt)

            # 3) 缺陷关键词命中（必须匹配**当前查询提示词**）
            #    ⚠️ 历史缺陷：这里曾拿教训自己的 prompt 去匹配自己的 terms，恒等命中，
            #    给每条教训都加了一个与相关性无关的常数分 —— 召回退化成随机取样。
            kw_hit = 0.0
            qp = _norm(prompt or "")
            for t in (l.get("terms") or []):
                if t and qp and t in qp:
                    kw_hit += 1.0

            # 4) 上下文匹配
            context_match = 0.0
            if context and l.get("context"):
                for key, value in context.items():
                    if l["context"].get(key) == value:
                        context_match += 0.2

            # 5) 优先级权重
            priority_weight = PRIORITY_LEVELS.get(l.get("priority", "medium"), {}).get("weight", 0.5)

            total = (sim + min(kw_hit, 3.0) * 0.25 + context_match) * decay_weight * priority_weight

            if total > 0.1:
                scored.append((total, l))

        scored.sort(key=lambda x: x[0], reverse=True)

        hints = []
        seen = set()
        for _, l in scored:
            for iss in (l.get("issues") or []):
                key = _norm(iss)
                if key and key not in seen:
                    seen.add(key)
                    hints.append({
                        "issue": iss,
                        "category": l.get("category", "visual"),
                        "priority": l.get("priority", "medium"),
                        "project": l.get("project", ""),
                        "score": l.get("score"),
                    })
                if len(hints) >= max_hints:
                    break
            if len(hints) >= max_hints:
                break

        return hints

    @classmethod
    def _render_hint(cls, issue, style: str = "") -> str:
        """把一条建议渲染成可用文本：替换 ``{style}`` 占位符

        - 无占位符 → 原样返回；
        - 有占位符且本次给了 style → 替换成当前项目的风格；
        - 有占位符但没给 style → 返回空串（丢弃该建议）。宁可少给一条，
          也不能把别的项目的风格名安到当前项目上。
        """
        text = str(issue or "").strip()
        if not text:
            return ""
        if cls.STYLE_PLACEHOLDER not in text:
            return text
        s = str(style or "").strip()
        if not s:
            return ""
        return text.replace(cls.STYLE_PLACEHOLDER, s)

    @staticmethod
    def _similar(a: str, b: str) -> float:
        """字符级相似度（不分词），阈值内近似。"""
        a, b = _norm(a), _norm(b)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        # 用较长串做滑动窗口子串命中近似
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        if len(short) < 4:
            return 1.0 if short in long_ else 0.0
        hit = 0
        for i in range(0, len(short) - 3, 1):
            if short[i:i + 4] in long_:
                hit += 1
        return min(hit / 8.0, 0.95)

    # ---------- 拼装带经验的提示词 ----------
    def learned_prompt(self, kind: str, prompt: str,
                       project: str = "", max_hints: int = 3,
                       style: str = "") -> str:
        """在原始提示词后追加历史修正建议；无建议则原样返回。

        style：本次生成的目标风格。仅用于替换建议里的 ``{style}`` 占位符，
        保证「严格采用 X 风格」里的 X 永远是**当前项目**的风格。
        """
        hints = self.suggestions(kind, prompt, project, max_hints, style=style)
        if not hints:
            return prompt
        safe = [(h or "").strip() for h in hints if (h or "").strip()]
        if not safe:
            return prompt
        suffix = "\n【历史质检修正建议（请务必遵守）】\n" + "\n".join(f"  {i+1}. {h}" for i, h in enumerate(safe))
        return prompt + suffix

    # ---------- 衰减过时记忆（原增强版能力，折入） ----------
    def decay_old_lessons(self, days_threshold: int = 30) -> int:
        """衰减过时记忆"""
        decayed_count = 0

        with self._lock:
            for lesson in self._lessons:
                old_weight = lesson.get("decay_weight", 1.0)
                new_weight = _calculate_decay_weight(lesson)

                if new_weight < old_weight:
                    lesson["decay_weight"] = new_weight
                    decayed_count += 1

            if decayed_count > 0:
                self._flush()

        return decayed_count

    # ---------- 学习曲线统计（原增强版能力，折入） ----------
    def get_learning_curve(self, project: str = None) -> dict:
        """获取学习曲线统计"""
        with self._lock:
            filtered = self._lessons
            if project:
                filtered = [l for l in filtered if l.get("project") == project]

            if not filtered:
                return {"total": 0, "by_category": {}, "by_priority": {}, "trend": []}

            # 按类别统计
            by_category = {}
            for l in filtered:
                cat = l.get("category", "unknown")
                by_category[cat] = by_category.get(cat, 0) + 1

            # 按优先级统计
            by_priority = {}
            for l in filtered:
                pri = l.get("priority", "medium")
                by_priority[pri] = by_priority.get(pri, 0) + 1

            # 按时间趋势（最近30天）
            trend = []
            now = datetime.now()
            for i in range(30):
                date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                count = sum(1 for l in filtered if l.get("ts", "").startswith(date))
                trend.append({"date": date, "count": count})

            trend.reverse()

            return {
                "total": len(filtered),
                "by_category": by_category,
                "by_priority": by_priority,
                "trend": trend,
                "avg_score": sum(l.get("score", 0) or 0 for l in filtered) / max(len(filtered), 1),
            }

    # ---------- 统计与检索 ----------
    def stats(self) -> dict:
        with self._lock:
            by_kind: dict = {}
            for l in self._lessons:
                k = l.get("kind") or "?"
                by_kind[k] = by_kind.get(k, 0) + 1
            return {
                "total": len(self._lessons),
                "by_kind": by_kind,
                "path": self._path,
            }

    def list(self, kind: str = "", limit: int = 50) -> list:
        with self._lock:
            items = [dict(l) for l in self._lessons
                     if (not kind or l.get("kind") == kind)]
            return items[-limit:][::-1]         # 最新在前

    # ---------- 带过滤条件的列表（原增强版能力，折入） ----------
    def list_with_context(self, kind: str = "", category: str = "",
                          priority: str = "", limit: int = 50) -> list:
        """带过滤条件的列表"""
        with self._lock:
            items = self._lessons

            if kind:
                items = [l for l in items if l.get("kind") == kind]
            if category:
                items = [l for l in items if l.get("category") == category]
            if priority:
                items = [l for l in items if l.get("priority") == priority]

            return items[-limit:][::-1]  # 最新在前

    def clear(self, kind: str = "") -> int:
        with self._lock:
            before = len(self._lessons)
            if kind:
                self._lessons = [l for l in self._lessons if l.get("kind") != kind]
            else:
                self._lessons = []
            self._flush()
            return before - len(self._lessons)

    def prune_empty(self) -> int:
        """清掉「无缺陷也无结论」的空教训，返回清理条数。

        为什么需要：历史缺陷导致某段时间写入的教训全是
        ``issues=[] / reason="" / score=0`` 的空壳（写入端曾误读 ``rec["verdict"]``）。
        这些记录既不能提供建议，又会在 stats 里冒充"学到了 36 条经验"，
        还会拖慢召回。写入端已加守卫不再产生新的空记录，这里负责清历史遗留。
        """
        with self._lock:
            before = len(self._lessons)
            self._lessons = [l for l in self._lessons
                             if (l.get("issues") or l.get("reason"))]
            removed = before - len(self._lessons)
            if removed:
                self._flush()
            return removed

    # ---------- 数据契约派生：主键 / 惰性补齐（类级入口，供 instance 调用） ----------
    #: 确定性派生教训主键（与模块级 `derive_lesson_id` 同一实现）
    lesson_id = staticmethod(derive_lesson_id)
    #: 惰性补齐数据契约字段（与模块级 `_normalize_lesson` 同一实现）
    _normalize_lesson = staticmethod(_normalize_lesson)


# ===================== 已废弃：空壳别名 =====================

class PromptMemoryEnhanced(PromptMemory):
    """已废弃：统一为 PromptMemory，仅为 isinstance 兼容保留。

    不再定义 `__init__`、不再持有独立 `_path`，也不再有独立的模块级单例；
    增强能力（record_with_context / suggestions_with_priority /
    decay_old_lessons / get_learning_curve / list_with_context）已整体折入
    `PromptMemory`。
    """
    pass


# ===================== 模块级单例 =====================

_INSTANCE: Optional[PromptMemory] = None
_INST_LOCK = threading.Lock()


def get_memory(root_dir: str) -> PromptMemory:
    """返回 `root_dir` 对应的唯一 `PromptMemory` 实例（全模块唯一写入者）。"""
    global _INSTANCE
    with _INST_LOCK:
        if _INSTANCE is None or os.path.abspath(_INSTANCE.root_dir) != os.path.abspath(root_dir):
            _INSTANCE = PromptMemory(root_dir)
        return _INSTANCE


def get_enhanced_memory(root_dir: str) -> PromptMemory:
    """**同一对象**：增强版单例已删除，直接返回 `get_memory(root_dir)`。

    保留该函数名只为向后兼容既有调用点；返回值与 `get_memory` 完全一致
    （`get_memory(root) is get_enhanced_memory(root)` 恒成立）。
    """
    return get_memory(root_dir)


def record(project: str, kind: str, prompt: str, issues: List[str],
           reason: str = "", score: Optional[int] = None, root_dir: str = "") -> dict:
    """关键便捷入口：由生成链路内部直接调用，记录一条经验。"""
    if not root_dir:
        return {}
    return get_memory(root_dir).record(project, kind, prompt, issues, reason, score)


def record_with_context(project: str, kind: str, category: str,
                        priority: str, context: dict, prompt: str,
                        issues: List[str], reason: str = "",
                        score: Optional[int] = None, root_dir: str = "") -> dict:
    """增强版记录入口（统一后落到底层唯一实例）。"""
    if not root_dir:
        return {}
    return get_enhanced_memory(root_dir).record_with_context(
        project, kind, category, priority, context, prompt, issues, reason, score
    )


def suggest(kind: str, prompt: str, project: str = "", root_dir: str = "",
            style: str = "") -> List[str]:
    if not root_dir:
        return []
    return get_memory(root_dir).suggestions(kind, prompt, project, style=style)


def suggest_with_priority(kind: str, prompt: str, context: dict = None,
                          max_hints: int = 5, root_dir: str = "") -> List[dict]:
    """增强版召回入口（统一后落到底层唯一实例）。"""
    if not root_dir:
        return []
    return get_enhanced_memory(root_dir).suggestions_with_priority(
        kind, prompt, context, max_hints
    )


def learned_prompt(kind: str, prompt: str, project: str = "",
                   root_dir: str = "", max_hints: int = 3,
                   style: str = "") -> str:
    """关键便捷入口：返回叠加了「历史质检修正建议」的提示词；无建议则原样返回。

    ⚠️ 必须放在模块级：生成链路是以 `prompt_memory.learned_prompt(...)` 调用的。
    此前只有类方法、没有模块级函数，调用处抛 AttributeError 又被 except 静默吞掉，
    导致「质检不通过 → 改提示词重生成」这条链路**从来没有真正生效**过。
    """
    if not root_dir:
        return prompt
    return get_memory(root_dir).learned_prompt(kind, prompt, project, max_hints, style=style)


def get_learning_curve(project: str = None, root_dir: str = "") -> dict:
    """获取学习曲线（统一后落到底层唯一实例）。"""
    if not root_dir:
        return {"total": 0, "by_category": {}, "by_priority": {}, "trend": []}
    return get_enhanced_memory(root_dir).get_learning_curve(project)


def decay_lessons(root_dir: str = "") -> int:
    """衰减过时记忆（统一后落到底层唯一实例）。"""
    if not root_dir:
        return 0
    return get_enhanced_memory(root_dir).decay_old_lessons()
