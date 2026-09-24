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
import threading
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

# P2-T3（审计 T-3）：qc_config 落盘此前三处（save_config / set_endpoint /
# reset_endpoint）都用**固定 `.tmp`** 且模块无锁 —— 两个线程同时 save_config
# 时固定临时名会互相截断（一边写到一半被另一边 os.replace 换走 → json 残半），
# 损坏的配置文件会被 load_config 标 _config_corrupt 回退默认值（不阻断但配置丢失）。
# 手法同 P1-1（project_store._write_json）：唯一临时名 + fsync + Windows 占用退避
# replace + 模块级锁串行化整个「读改写」段（锁加在各写函数外层，见下方调用点）。
_QC_WRITE_LOCK = threading.Lock()


def _atomic_write_json(config_path: str, cfg: dict) -> None:
    """原子写 JSON（唯一临时名 + fsync + 占用退避 replace），不截断、不留垃圾。"""
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    # 临时名每次唯一（pid+threadid+随机），并发写者各写各的，互不截断
    tmp = f"{config_path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    last = None
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())          # 先落盘再 replace，避免断电后只剩空文件
        for i in range(8):
            try:
                os.replace(tmp, config_path)   # 同分区原子换名
                return
            except PermissionError as e:       # Windows 目标被并发读取占用（WinError 5/32）
                last = e
                time.sleep(0.02 * (i + 1))
        raise last
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)               # 失败不留垃圾临时文件
        except OSError as e:
            logger.debug("清理临时文件失败（忽略）：%s", e)
        raise

# ===================== 默认配置 =====================

# 质检判定口径：水印 / 角标 / 字幕 / 「AI 生成」标识一律不计为质量缺陷（B 项⑥）
# 该口径会追加到「默认提示词」与「用户自定义提示词」之后，保证历史配置同样生效。
WATERMARK_EXEMPT_NOTE = (
    "\n【判定口径·重要】画面中若仅存在水印、角标、logo、平台标识、字幕或「AI 生成」标识，"
    "一律不得视为质量缺陷，不得因此扣分，也不得据此判定不通过；"
    "请只针对画面本身的崩坏 / 畸变 / 糊化 / 闪烁 / 撕裂 / 一致性问题做判定。"
    # ⚠️ 2026-09-24 补：只写「水印/字幕豁免」不够 —— 质检模型对「画面里有文字」自带一条
    # **先验规则**（「无文字规范」），实测把「卷轴上的 7 列古篆」判成违规（score=35），
    # 而同一件道具在「图里没文字」时又依据**设定**判「缺少古篆文字」——同一件道具
    # 两个方向都能判出缺陷，形成无解循环（出图侧还有去水印负向词压字）。
    # 这里把口径写死，让质检在「内容文字」上有唯一立场。
    "\n【文字口径·重要】「不得出现文字」类约束**只针对叠加物**（字幕条、台词字幕、标题条、"
    "水印、logo、生成标识）。**画面内容本身需要的文字不算违规，一律不得因此扣分或判不通过** —— "
    "器物铭文、卷轴古篆、牌匾刻字、系统面板数值等属于画面内容，设定要求时应正常呈现："
    "既不得因「画面里有文字」判缺陷，也不得因「画面里没有文字」判缺陷（那属于设定问题，"
    "不是出图质量问题）；文字是否清晰可读、是否有乱码也**不计入缺陷** —— 中文文字属于"
    "图像模型能力边界，乱码/错字不影响主体判定，请只针对画面本身的崩坏 / 畸变 / 糊化 / "
    "闪烁 / 撕裂 / 一致性 / 主体形态 / 风格做判定。"
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
    # task#7 词表补漏：五官 + 「不 + 形容词」类缺陷（旧词表只有 五官错位/畸变/错误，
    # 「五官不自然」这种最常见表述反而漏判）。这些「不X」是**缺陷描述**而非否定 —— 与
    # _has_negative_context 的收紧口径配套：句首真否定（「不是五官不自然」）仍判为空。
    "五官不自然", "五官不协调", "五官不对称",
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


#: **强否定词**：语义明确表示「不存在 / 缺乏该缺陷」，在关键词前、同子句内出现即判否定。
#: 这些词极少与缺陷词构成「描述缺陷」的复合词（不会有「无规则畸变」这类），故可直接生效。
#: 口径 = 原 `_NEGATION_WORDS` 去掉歧义的「不 / 非」（见下），补一个同义「并无」。
_NEGATION_STRONG = (
    "无", "没有", "并无", "未", "未发现", "不存在", "缺少", "已消除",
)

#: **歧义前缀**「不 / 非」：既可能是否定（不是 / 不属 / 非为），也可能只是**缺陷描述词
#: 的一部分**（不规则 / 不对称 / 非对称 / 不自然 / 非常）。因此仅当其后**紧跟**一个
#: 「判定 / 系动词」字时才认定为否定，否则视为「修饰缺陷的形容词」，**不否定**关键词。
#: 历史缺陷（P0-3 补强 / task#7）：「不规则畸变」「不对称扭曲」「五官不自然」里的「不」
#: 被旧实现当作否定词 → 真缺陷被整条吞掉（`find_critical_issues → []` → 崩坏图入库）。
_NEG_PREFIX_CHARS = ("不", "非")
#: 允许出现在「不 / 非」之后的**判定 / 系动词**首字（即 不是 / 不属 / 非为 / 不构成 / 不等于…）。
#: 维护约定：新增时只允许「不/非 + 该字」能表达『并非如此』这类**判断**的字；
#: ⚠️ 严禁把「自 / 规 / 对 / 常 / 正 / 协 / 清 / 均 / 明 / 稳 / 统」等**构词**字加入 ——
#: 它们恰恰是缺陷词（不自然 / 不规则 / 不对称 / 不正常 / 不协调 / 不清晰…）的前缀。
_NEG_PREFIX_JUDGE = ("是", "属", "为", "算", "构", "存", "等", "符", "代", "像")


#: 子句分隔符：否定词作用域以**子句**为界（跨子句的「不/无」不影响本子句的缺陷描述）。
#: P0-3：『五官不自然，面部畸变』里的「不」属于前一个子句（修饰「自然」），
#: 不得据此判定后一子句的「畸变」被否定——旧实现只看关键词前固定 window 个字符，
#: 会跨子句误吞整条真缺陷（崩坏图 blocked=False 直接入库）。
_CLAUSE_SEP_RE = re.compile(r"[，。；！？,.;!?、\n\r]+")


def _has_negative_context(text: str, idx: int, window: int = 6) -> bool:
    """判断 ``text[idx]`` 处的关键词是否处于否定语境（如「无明显畸变」）

    作用域规则（P0-3 修复）：
      · 只在关键词**所在子句内**、且位于关键词**前面** window 个字符里找否定词；
      · 跨子句的否定词不生效（『五官不自然，面部畸变』→「畸变」不被前句的「不」否定）；
      · 子句起点即关键词时前缀为空 → 视为非否定（正常命中）；
      · 同子句内的「无/没有/未」仍正常生效（『无明显畸变』不误杀）。

    修饰判定（task#7 补强）：否定词必须**真正否定该缺陷词**，而非「子句内出现过否定字就算」：
      · 强否定词（``_NEGATION_STRONG``）直接生效；
      · 「不 / 非」是歧义前缀 —— 只有「不/非 + 判定字」（不是 / 不属 / 非为…）才是否定，
        「不规则畸变 / 不对称扭曲 / 五官不自然」里的「不」是构词前缀，**不构成否定**。
    """
    text = str(text or "")
    # 关键词所在子句的起点 = 该位置之前最后一个子句分隔符之后
    clause_start = 0
    for _m in _CLAUSE_SEP_RE.finditer(text, 0, idx):
        clause_start = _m.end()
    ctx = text[max(clause_start, idx - window):idx]
    if any(neg in ctx for neg in _NEGATION_STRONG):
        return True
    # 歧义前缀：仅「不/非 + 判定字」才算否定（避开「不规则/不对称/五官不自然」的构词前缀）
    for _i, _ch in enumerate(ctx):
        if _ch in _NEG_PREFIX_CHARS and ctx[_i + 1:_i + 2] in _NEG_PREFIX_JUDGE:
            return True
    return False


def find_critical_issues(issues, keywords=None) -> list:
    """从 issues 文本中筛出命中关键缺陷词的条目

    带否定语境过滤：只有**真正否定该缺陷词**的表述（如「无明显畸变」「不是五官不自然」）
    才不算关键缺陷——强否定词（无/没有/未/不存在/缺少/已消除…）直接生效，「不 / 非」需
    后接判定字（不是 / 不属 / 非为…）才算否定；「不规则畸变 / 不对称扭曲 / 五官不自然」
    里的「不」是构词前缀，**照常命中**（否则真缺陷被误吞，崩坏图入库）。

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


# G1 重试止损（共享模块）：本文件是叶子模块（只 import requests/audio_qc，
# 不 import app / comfyui_client），因此把「连续重试缺陷特征提取 + 止损判定」
# 放这里，供 app / comfyui_client / keyframe 以注入回调（qc_stop_cb）方式复用，
# 规避 app ↔ comfyui_client 的循环依赖。

def qc_retry_features(rec: dict) -> set:
    """从一条质检记录里提取「缺陷特征集」，用于止损比对。

    特征 = 该记录 critical_issues ∪ issues 的归一化文本集合。
    同一缺陷在多轮重试里逐字复现 → 特征集相等 → 换 seed/换提示词无收益。
    """
    rec = rec or {}
    feats: set = set()
    for k in ("critical_issues", "issues"):
        for it in (rec.get(k) or []):
            s = str(it).strip()
            if s:
                feats.add(s[:200])
    return feats


def qc_retry_hopeless(attempts: list, streak: int = 2) -> tuple:
    """连续 N 次重试的缺陷特征集完全相同 → 判定重试无收益（止损）。

    返回 ``(stop: bool, detail: str)``：
      - 最后 ``streak`` 条记录特征集相等且非空 → ``(True, "缺陷摘要")``；
      - 特征集为空（模型没报具体缺陷）或不足 streak 条 → ``(False, "")``。

    ⚠️ 只读特征比对，不做风格/关键缺陷二次判定 —— 后者由调用方（_qc_gate）负责。
    """
    if not attempts or len(attempts) < streak:
        return False, ""
    tail = list(attempts[-streak:])
    feats = [qc_retry_features(r) for r in tail]
    if not feats[0]:
        return False, ""
    if any(f != feats[0] for f in feats[1:]):
        return False, ""
    return True, "；".join(list(feats[0]))[:200]


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
    elif not isinstance(score, int):
        # P1-17：模型未回可用分数（score=None / 非整数）→ 无法判定是否达线 → **fail-closed**。
        # 旧实现把 score is None 当作「满足分数」（`score is None or score >= pass_score`），
        # 于是 {"score":null,"pass":true} 被判 accept、漏回分数即放行。此处不再放行。
        verdict["passed"] = False
        verdict["score_missing"] = True
        logger.warning("质检结论缺少可用分数（score=%r）→ fail-closed 判为不通过", score)
    elif score < int(pass_score):
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
    "6) 水印/角标/logo/字幕即使存在，也不计入缺陷、不扣分；「不得出现文字」类约束只针对"
    "上述**叠加物**，画面内容本身需要的文字（器物铭文、牌匾、面板数值等）属于画面内容，"
    "不得因画面有/无此类文字判缺陷，文字乱码/错字也不计缺陷（中文文字属模型能力边界）。\n"
    "目标风格：{style}\n"
    "镜头信息：{shot_desc}\n"
    "判定线：score >= {pass_score} 且无关键缺陷 → pass=true，否则 pass=false"
    "（请严格按合格线给 pass，不要凭直觉打分后由系统改判，避免模型判过、代码判不过的假失败）。\n"
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
    "判定线：score >= {pass_score} 且无关键缺陷 → pass=true，否则 pass=false"
    "（请严格按合格线给 pass，不要凭直觉打分后由系统改判，避免模型判过、代码判不过的假失败）。\n"
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
    "判定线：score >= {pass_score} 且无关键缺陷 → pass=true，否则 pass=false"
    "（请严格按合格线给 pass，不要凭直觉打分后由系统改判，避免模型判过、代码判不过的假失败）。\n"
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
    # G9/O1 图片/视频客观层阈值（2026-09-20 新增）
    "image_pixel_std_min", "video_max_drift",
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
        # G9/O1 客观层确定性闸门（图片黑图 stddev 下限 / 视频时长偏差上限）
        "image_pixel_std_min": 8.0,  # 像素 stddev < 8 → 黑图/纯色图 fatal
        "video_max_drift": 0.30,      # 视频 |实测-期望|/期望 > 30% → fatal
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

# G13（P1）说明：质检配置「逐镜反复 load_config + Fernet 解密」的开销，**不在
# load_config 里加全局 mtime 缓存**解决 —— 那样会破坏 load_config 的「纯重读」
# 契约（verify_qc_bool_parse F5 断言 load_config 以 `return _normalize(cfg)` 收尾），
# 且 Windows mtime 分辨率粗、写→读快循环会读脏。正确做法是「worker 进循环前读一次
# 并复用」（资产 worker 已如此），本批已把分镜/视频 worker 的逐镜 load 上提。
# load_config 本身保持纯重读，任何直接写文件 + 立即读的场景都拿到最新值。


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
            # G14（P1）：文件**存在但解析失败** ≠ 文件不存在。
            # 文件不存在是正常的（默认关闭）；文件损坏则必须 fail-loud——
            # 否则静默回落 enabled=False 会让用户以为「我明明开了质检」却整条被跳过。
            logger.error(f"质检配置文件损坏，解析失败（fail-loud，本次按默认关闭）：{e}")
            cfg["_config_corrupt"] = True
            cfg["_config_error"] = str(e)
    # ⭐ 单一事实源：AI 凭证统一走 tasks.db 的 ai_credentials 表（qc 模块）。
    # get_credentials 内部已按 env > DB 解析密钥（env 仍最高，运维部署可用 env 覆盖），
    # 故这里直接信任其结果；DB 有非空字段就覆盖 json 值。
    # 回落：DB 读不到（模块异常）才走旧的 secrets.enc「qc 槽 + env base/model」。
    db_key = ""
    try:
        import ai_credentials_db
        db_ep = ai_credentials_db.get_credentials("qc")
        if db_ep.get("base_url"):
            cfg["base_url"] = db_ep["base_url"]
        if db_ep.get("model"):
            cfg["model"] = db_ep["model"]
        if db_ep.get("api_key"):
            db_key = db_ep["api_key"]
            cfg["api_key"] = db_key
            # endpoint_override 若声明了 base_url/model 但密钥为空，用 DB 的密钥补齐，
            # 否则 resolve_endpoint 会因 override 缺 key 而落到未配置分支
            ov = cfg.get("endpoint_override")
            if isinstance(ov, dict) and ov and not (ov.get("api_key") or "").strip():
                ov["api_key"] = db_key
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AI 凭证 DB（qc）读取失败，回落加密库/env：{e}")
    # ⚠️「DB 没有该模块的密钥」≠「没配过密钥」。本模块的密钥写入点（save_config /
    #    set_endpoint）写的是**旧加密库**的裸槽 `qc`；AI 设置页（ai_config.save_module）
    #    写的是 `ai.qc`。DB 只在这两种之外（启动迁移 / UI 显式写库）才落行。
    #    原实现只在 DB **抛异常**时才回落旧口径 →「DB 无行 + 加密库有钥」被判成无钥
    #    → public_view.ready=False / video_qc_active=False → 质检被静默跳过，
    #    前端还弹「质检接口信息不完整」的误导警告（verify_project_audit A2 守护的正是它）。
    #    现改为：DB 未给出密钥就回落旧口径。槽位顺序沿用
    #    ai_credentials_db.migrate_from_legacy 的 slot_map["qc"]（ai.qc → qc）。
    if not db_key:
        try:
            import secret_store
            store = secret_store.get_store(_PROJECT_ROOT)
            for _slot in ("ai.qc", "qc"):
                secure = (store.get_api_key(_slot) or "").strip()
                if secure:
                    cfg["api_key"] = secure
                    ov = cfg.get("endpoint_override")
                    if isinstance(ov, dict) and ov and not (ov.get("api_key") or "").strip():
                        ov["api_key"] = secure
                    break
            env_base = secret_store.SecretStore.env_base_url("qc")
            env_model = secret_store.SecretStore.env_model("qc")
            if env_base:
                cfg["base_url"] = env_base
            if env_model:
                cfg["model"] = env_model
        except Exception as e2:  # noqa: BLE001
            logger.warning(f"质检密钥读取异常（回退 json）：{e2}")
    # 类型兜底：统一交给 `_normalize`（save_config / load_config_dict 用的是同一套规则）
    # ⚠️ 审计 G2：这里原本手写了一份「简化版」归一化，且布尔项用的是裸 `bool()` ——
    #    字符串 "false"/"0"/"no"/"off" 都是非空字符串 → 一律判 True（用户在页面或第三方
    #    脚本里写 "false"，读回来反而是「开」）；而且它只覆盖 3 个开关，
    #    audio_enabled / script_enabled / keyframe_qc_enabled / prompt_enabled 完全没归一化，
    #    音频/剧本/尾帧质检的开关因此形同虚设。两份口径并存必然漂移，现收敛为单一实现。
    # G14：若文件「存在但解析失败」，cfg 上带 _config_corrupt/_config_error，_normalize
    #    只赋值已知键、不删除额外键，故损坏标记会随返回值带出，供 public_view 高亮。
    return _normalize(cfg)


def _sync_credentials_db(cfg: dict = None, api_key: str = None, source: str = "qc_config",
                         clear: bool = False) -> str:
    """A2（2026-09-23 收口）：把质检端点同步进「AI 凭证单一事实源」（tasks.db 的 qc 模块）。

    为什么要做：读侧（`load_config` / `ai_config.get_module`）自 P0-5 起**优先读 tasks.db**，
    但质检端点此前只有旧加密库（裸槽 ``qc``）一个写点 —— 实际能工作全靠「DB 无该模块行时
    回落旧库」这条读侧兜底，DB 并未真正成为单点（`/api/qc/config` 与 `/api/ai/config` 不对称）。

    - `clear=True`：清空 DB 的 qc 模块（「恢复为 AI 设置」必须同步清，否则读侧仍读 DB 旧端点，
      用户会看到「点了恢复却还是旧接口」）。
    - 否则按**生效端点**（`resolve_endpoint`，会从加密库补齐密钥）写入；`api_key=None` 表示
      本次未改密钥 → 库内原值不动。

    返回 "" 表示成功；否则返回错误描述 —— 调用方需**响亮降级**（绝不阻断已完成的落盘）。
    """
    try:
        import ai_credentials_db
        if clear:
            ai_credentials_db.clear_credentials("qc")
            return ""
        ep = resolve_endpoint(cfg)
        # 安全阀：端点全空且本次未改密钥 → **不写 DB**。
        # 否则「只改一个阈值再保存」这种调用方（cfg 里 base_url/model 为空）会把 DB 里
        # 已有的端点覆盖成空串 —— 清空必须走显式的 clear（clear_config / reset_endpoint）。
        if not (ep.get("base_url") or ep.get("model") or api_key):
            return ""
        ai_credentials_db.set_credentials(
            "qc", base_url=ep.get("base_url") or "", model=ep.get("model") or "",
            api_key=api_key, source=source)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        logger.error("质检端点同步到 AI 凭证库失败（读侧将回落旧加密库，可能出现"
                     "「保存成功但任务仍用旧值」的漂移，请检查 tasks.db）：%s", msg)
        return msg
    return ""


def save_config(config_path: str, patch: dict, keep_key_if_blank: bool = True) -> dict:
    # P2-T3：整段「读 load_config → 改 → 原子写」持锁串行化，
    # 防两个线程同时 save 时读改写互相丢更新（唯一临时名只防文件截断，不防逻辑丢更新）。
    with _QC_WRITE_LOCK:
        return _save_config_impl(config_path, patch, keep_key_if_blank)


def _save_config_impl(config_path: str, patch: dict, keep_key_if_blank: bool = True) -> dict:
    cfg = load_config(config_path)
    # A2（2026-09-23）：记录本次是否写入了**新**密钥。None = 未改密钥，
    # 同步 DB 时保持库内原值（与 /api/ai/config 的 keep 语义一致）。
    new_key = None
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
            new_key = v
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
        elif k in ("image_pixel_std_min", "video_max_drift", "api_backoff"):
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
    _atomic_write_json(config_path, cfg)
    # ⚠️ 必须重新 load_config 再返回，**不能返回上面那个已清空 api_key 的 cfg**：
    # 明文密钥只存加密库，上面刚把 cfg["api_key"] 置空是为了防明文落盘；
    # 若直接返回它，调用方拿到的就是「无密钥」的配置 → public_view 算出
    # ready=False / image_qc_active=False → 前端每次保存都会弹
    # 「质检开关已开启，但质检接口信息不完整，生成流程将跳过质检」的**误导性警告**
    # （实测：仅提交 {"image_enabled": true} 后 video_qc_active 从 true 掉成 false）。
    # 重新读取一次即可拿到 load_config 注入的密钥，且返回的正是应用真正会用的配置。
    final = load_config(config_path)
    # A2（2026-09-23 收口）：写侧与 /api/ai/config 对称地落「AI 凭证单一事实源」（tasks.db）。
    # 此前质检端点只有旧加密库（裸槽 `qc`）一个写点，而读侧自 P0-5 起 DB 优先 ——
    # 实际只能靠「DB 无该模块行时回落旧库」兜住，DB 并未真正成为单点。
    # 这里按**生效端点**同步（resolve_endpoint 会从加密库补齐密钥）；
    # api_key=None → 库内原密钥不动；失败只响亮降级，绝不阻断已完成的保存。
    _sync_credentials_db(final, api_key=new_key)
    return final


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
    # G9/O1 图片客观层阈值：纯色/黑图 stddev 下限（float，默认 8.0，上限 50.0）
    try:
        cfg["image_pixel_std_min"] = max(0.0, min(50.0, float(cfg.get("image_pixel_std_min", 8.0))))
    except Exception:  # noqa: BLE001
        cfg["image_pixel_std_min"] = 8.0
    # G9/O1 视频客观层阈值：时长偏差上限（float，默认 0.30，上限 5.0）
    try:
        cfg["video_max_drift"] = max(0.0, min(5.0, float(cfg.get("video_max_drift", 0.30))))
    except Exception:  # noqa: BLE001
        cfg["video_max_drift"] = 0.30
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
    with _QC_WRITE_LOCK:
        return _set_endpoint_impl(config_path, base_url, api_key, model)


def _set_endpoint_impl(config_path: str, base_url: str, api_key: str, model: str) -> dict:
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
    _atomic_write_json(config_path, cfg)
    # A2：自动写入的端点同样落 DB 单一事实源（读侧 DB 优先，漏这里会出现
    # 「自动同步了旧加密库、但任务从 DB 读到旧端点」的分裂）
    _sync_credentials_db(cfg, api_key=ep["api_key"], source="qc_endpoint_override")
    return cfg


def reset_endpoint(config_path: str) -> dict:
    """「恢复为 AI 设置」：清空自动写入的接口，交还给用户在 AI 设置里独立配置"""
    with _QC_WRITE_LOCK:
        return _reset_endpoint_impl(config_path)


def _reset_endpoint_impl(config_path: str) -> dict:
    cfg = load_config(config_path)
    try:
        import secret_store
        secret_store.get_store(_PROJECT_ROOT).clear_api_key("qc")
    except Exception as e:  # noqa: BLE001
        logger.error("清理加密库中的质检密钥失败，加密库可能残留旧密钥：%s", e)
    cfg["base_url"], cfg["api_key"], cfg["model"] = "", "", ""
    cfg["endpoint_override"] = {"base_url": "", "api_key": "", "model": ""}
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write_json(config_path, cfg)
    # A2：必须**同步清空** DB 的 qc 模块 —— 否则读侧（DB 优先）仍会读到旧端点，
    # 用户点了「恢复为 AI 设置」却看不到任何变化（同一根因的镜像面）。
    _sync_credentials_db(clear=True)
    return cfg


def public_view(cfg: dict) -> dict:
    ep = resolve_endpoint(cfg)
    ready = bool(cfg.get("enabled") and ep["base_url"] and ep["api_key"] and ep["model"])
    view = {k: v for k, v in cfg.items()
            if k not in ("api_key", "_config_corrupt", "_config_error")}
    view.update({
        # G14：配置文件「存在但损坏」的 fail-loud 标记，让 UI 如实提示而非静默关闭质检
        "config_corrupt": bool(cfg.get("_config_corrupt")),
        "config_error": str(cfg.get("_config_error") or ""),
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
    # D-03 同族收口：此处原为裸 `open(w)+json.dump`（非原子写，崩溃/并发会留半截 json），
    # 改为与文件内其它落盘一致的原子写。
    _atomic_write_json(config_path, cfg)
    # A2：端点被一并重置 → 必须同步清空 DB 的 qc 模块，否则读侧（DB 优先）仍读旧端点。
    _sync_credentials_db(clear=True)
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
        # G11 修复：补全中文/英文/大小写变体白名单；识别不了 → None（未知），
        # 而不是 False —— 否则"字段值写法差异"会变成 100% 假失败（合法结论被判不通过）。
        # 归一化：去空白/小写/剥尾标点（。.!！），再比对白名单。
        _s = _norm_bool_str(passed)
        if _s in ("true", "yes", "1", "pass", "ok", "passed", "通过", "达标", "合格", "符合", "好"):
            passed = True
        elif _s in ("false", "no", "0", "fail", "failed", "不通过", "未通过", "不合格", "不达标", "差"):
            passed = False
        else:
            passed = None
    if passed is None:
        # 模型没给布尔结论（或无法识别）→ 退用分数兜底：score>=pass_score 视为通过。
        passed = (score is not None and score >= pass_score)
    issues = obj.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    # P0：代码侧硬闸（关键缺陷阻断 + 分数不达标强制不通过），只收紧不放宽
    sm = obj.get("style_match")
    if isinstance(sm, str):
        # G11 修复：style_match 更脆弱——任何非白名单字符串原来都被折成 False，
        # 而 False 在 _apply_style_gate 里被当作"明确的风格不符证据"强制失败。
        # 现补全白名单；识别不了 → None（未知），交给 _apply_style_gate 的 issues
        # 关键词兜底，不再凭"写法差异"误杀。
        _sm = _norm_bool_str(sm)
        if _sm in ("true", "yes", "1", "pass", "ok", "match", "一致", "符合", "相同", "匹配"):
            sm = True
        elif _sm in ("false", "no", "0", "fail", "failed", "不一致", "不符", "不匹配", "偏离", "跑偏"):
            sm = False
        else:
            sm = None
    return _finalize_verdict({
        # P1-17：分数缺失（score is None）不再视为「满足分数」——要求分数存在且达线。
        "passed": bool(passed) and (score is not None and score >= pass_score),
        "score": score,
        "reason": str(obj.get("reason") or obj.get("comment") or "")[:500],
        "issues": [str(x)[:200] for x in issues][:6],
        "raw": text[:1000],
        "style_match": sm if isinstance(sm, bool) else None,
    }, pass_score)


def _norm_bool_str(v: str) -> str:
    """归一化布尔型字符串：去首尾空白 → 小写 → 剥尾标点（。.!！?？~～）。

    G11 辅助：让白名单比对对"通过。" / "Yes" / "ok!" 这类带尾标点/大小写的写法鲁棒。
    """
    s = str(v or "").strip().lower()
    while s and s[-1] in "。.!！?？~～,，;；:：":
        s = s[:-1]
    return s.strip()


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
    except Exception as e:  # noqa: BLE001
        logger.debug("JSON 候选解析失败（试下一个候选）：%s", e)
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception as e:  # noqa: BLE001
            logger.debug("JSON 候选解析失败（试下一个候选）：%s", e)
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
        # P0-2：显式标注「接口级故障」——调用根本没拿到模型判定（鉴权 401/403/404、
        # 超时、网络抖动等），**不是**「图片内容不合格」。上层闸门据此 fail-open
        # （放行已出图资产 + 响亮告警），而不是把 ComfyUI 已出好的图按内容不合格丢弃。
        return {"ok": False, "skipped": False, "error": str(e),
                "api_attempts": getattr(e, "attempts", 1),
                "retryable": getattr(e, "retryable", None),
                "interface_fault": True}
    verdict = _apply_style_gate(verdict, style_norm)
    # 把「带了几张设定图」透出来，便于前端/体检确认该能力真的生效（而不是静默没带）
    verdict["ref_images_used"] = len(ref_list)
    if ref_list:
        verdict["ref_labels"] = [str(l)[:60] for l, _p in ref_list]
    # G9/O1 图片客观层（零模型依赖，与视频/音频客观层同构）：
    # 全黑/全白/纯色（像素 stddev < 阈值）→ fatal 计入 critical_issues、blocked=True；
    # 长宽比异常 → 非 fatal 计入 issues。AI 层失败也会透出（不因 AI 不可用漏掉黑图）。
    obj = image_objective(image_path, cfg)
    obj_fatal = list(obj.get("critical_issues") or [])
    obj_issues = list(obj.get("issues") or [])
    verdict["objective"] = obj
    verdict["objective_fatal"] = obj_fatal
    verdict["objective_issues"] = obj_issues
    if obj_issues:
        verdict["issues"] = list(verdict.get("issues") or []) + obj_issues
    if obj_fatal:
        verdict["critical_issues"] = list(verdict.get("critical_issues") or []) + obj_fatal
        verdict["blocked"] = True
        verdict["passed"] = False
        verdict["score"] = min(int(verdict.get("score") or 0), 50)
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
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
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
                   prefix: str = "frame", fallback_meta: dict = None,
                   frame_ratio: list = None) -> dict:
    """全片均匀抽帧（片头/中段/片尾），并校验每帧实际时间戳。

    返回 {ok, frames, frame_meta, timestamps, duration, meta, error}
      frames      : 抽帧图片路径列表（喂给多模态模型）
      frame_meta  : [{path, requested_ts, actual_ts, verified}]，actual_ts 由 ffmpeg showinfo 解析

    D-05（P1）新增 ``frame_ratio``：按**占比**指定抽帧时间点（0~1 的小数，相对全片时长）。
      整集模式（一次产出 20~44 段）必须用它按「每段中点」抽帧——只按 ``count`` 全片均布
      3 帧时，中段几十段完全没有采样，模型拿 3 张无关帧去判整集是否崩坏 → 形同虚设。
      传入且能读到视频时长时**不受 count 的 1~6 上限约束**（该上限只约束 count 路径，
      保持既有配置语义不变）；时长读不到时告警并退回 count 路径（不静默）。
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

    # D-05：优先按占比抽帧（整集路径 = 每段中点），一次覆盖全片每段。
    ratios = []
    for r in (frame_ratio or []):
        try:
            rf = float(r)
        except (TypeError, ValueError):
            continue
        ratios.append(min(1.0, max(0.0, rf)))
    if ratios and dur > 0:
        # 去重 + 保序：相邻段中点理论上互不相同，去重只为防御重复入参
        times = sorted({round(dur * r, 3) for r in ratios})
        result["meta"]["frame_mode"] = "ratio"
        result["meta"]["frame_ratio_count"] = len(times)
    else:
        if ratios:  # 时长不可读 → 明确告警后退化，不静默丢帧
            logger.warning(
                "extract_frames 收到 frame_ratio(%d 个) 但视频时长不可读（%s）→ "
                "退化按 count=%d 全片均布抽帧", len(ratios), video_path, count)
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
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
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


def _has_audio_stream(video_path: str) -> bool:
    """探测视频是否含音频流（G9 确定性闸门：无音轨 → 计入 issues）。

    ffprobe 不存在或探测失败 → 返回 False（与现状一致，仅不报缺音）；
    能跑通 ffprobe 时，只要存在 audio codec 即返回 True。
    """
    fp = _ffprobe_exe()
    if not fp or not os.path.isfile(video_path):
        return False
    try:
        p = subprocess.run(
            [fp, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1",
             video_path],
            capture_output=True, timeout=30)
        out = _decode_io(p.stdout).strip()
        return "audio" in out.lower()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ffprobe 音频流探测失败：{e}")
        return False


def image_objective(image_path: str, cfg: dict = None) -> dict:
    """图片质检客观层（G9/O1 确定性闸门，与 check_audio.quick_check 同构）。

    零模型依赖，**永远执行**：
      1) 文件存在 + 非空；
      2) 像素 stddev 检查 —— 全黑 / 全白 / 纯色（std < 阈值）→ 致命缺陷；
      3) 长宽比异常（aspect < 1/10 或 > 10）→ 告警。

    永不抛异常：Pillow 不可用 / 读图失败 → 仅记录 error，不阻断后续。
    """
    cfg = cfg or _empty_config()
    base = {"ok": True, "skipped": False, "blocked": False, "passed": False,
            "score": 0, "issues": [], "critical_issues": [], "fatal": [],
            "error": "", "metrics": {}}
    if not image_path or not os.path.isfile(image_path):
        base["fatal"].append(f"图片文件不存在：{image_path}")
        base["blocked"] = True
        base["critical_issues"] = list(base["fatal"])
        base["reason"] = base["fatal"][0]
        return base
    try:
        import PIL.Image, PIL.ImageStat  # noqa: N811
    except Exception as e:  # noqa: BLE001
        base["skipped"] = True
        base["ai_skip_reason"] = f"Pillow 不可用（{e}），客观层跳过"
        return base
    try:
        with PIL.Image.open(image_path) as im:
            im_d = im.convert("RGB")
            w, h = im_d.size
            stat = PIL.ImageStat.Stat(im_d)
            # stat.stddev[i] 是第 i 通道的标准差。纯色图三通道 std 均 < 2；正常分镜图 std > 30。
            # 取三通道中最大的 stddev，等价于灰度 stddev，鲁棒。
            if stat.stddev:
                stddev = max(float(s) for s in stat.stddev)
            else:
                stddev = 0.0
            aspect = float(w) / max(1, h)
    except Exception as e:  # noqa: BLE001
        base["skipped"] = True
        base["ai_skip_reason"] = f"图片读取失败（{e}），客观层跳过"
        base["error"] = str(e)
        return base

    # 像素 stddev 阈值：纯色/黑图 < 8；正常分镜图通常 > 30
    std_th = float(cfg.get("image_pixel_std_min", 8.0))
    if stddev < std_th:
        base["fatal"].append(
            f"画面疑似黑图/纯色图（像素 stddev={stddev:.1f} < {std_th:.0f}）")
        base["blocked"] = True
    # 长宽比异常（极瘦/极宽）：aspect < 1/10 或 > 10 → 非 fatal 告警
    if aspect < 0.1 or aspect > 10:
        base["issues"].append(
            f"图片长宽比 {aspect:.2f} 异常（正常 0.5~2.0），可能是拼接/裁剪错误")
    base["metrics"] = {"width": w, "height": h, "stddev": round(stddev, 2),
                       "aspect": round(aspect, 4)}
    base["critical_issues"] = list(base["fatal"])
    base["score"] = max(0, 100 - (len(base["issues"]) * 5 + len(base["fatal"]) * 50))
    base["passed"] = not base["fatal"]
    return base


# A-19（P2-8）：视频客观层缺陷的**结构化判定码** + 致命性由「检测逻辑」决定，
# **不再靠中文字文案包含**。旧实现在 check_video 里写
#   obj_fatal = [s for s in obj_issues if ("无音轨" in s or "时长" in s
#                                           or "帧率" in s or "未通过校验" in s)]
# 一旦上游把文案「无音轨」改成「音轨缺失」、把「时长」措辞微调，致命集合立刻
# 变空 → objective_fatal=[] → blocked=False（**静默降级**，无音轨视频放行进成品）。
# 这里给每条客观缺陷带稳定的 ``code`` + ``fatal`` 布尔：``fatal`` 在「检测出该缺陷」
# 时由逻辑直接置位，与文案措辞彻底解耦。文案（msg）可随意改，code/fatal 不变，
# 从而保证「改文案后 objective_fatal 集合不变」。
#
# 四类致命码（与旧子串筛选 4 类一一对应，口径不变）：
#   · no_audio          ← 旧 "无音轨"
#   · duration_drift    ← 旧 "时长"
#   · frames_unverified ← 旧 "未通过校验"（全帧 / 部分帧都算）
#   · fps_abnormal      ← 旧 "帧率"
_VIDEO_OBJ_FATAL_CODES = ("no_audio", "duration_drift",
                          "frames_unverified", "fps_abnormal")


def _video_objective_issues(video_path: str, expected_duration: float,
                           cfg: dict, fr: dict) -> list:
    """视频质检确定性闸门（G9/O1）：不依赖模型的硬指标检测。

    A-19：返回**结构化**项列表，每项为 ``{"code", "fatal", "msg"}``：
      · ``code``  稳定判定码（见 ``_VIDEO_OBJ_FATAL_CODES``）；
      · ``fatal`` 该缺陷是否致命（由检测逻辑直接决定，与文案无关）；
      · ``msg``   人类可读文案（可自由改措辞，不影响致命性）。
    ``check_video`` 据此把 ``fatal=True`` 项的 msg 塞进 critical_issues / objective_fatal。

    检测项（口径与旧实现一致，仅判定依据从「中文字串包含」换成「结构化 fatal 字段」）：
      1) 时长偏差：``|fr.duration - expected| / expected > video_max_drift`` → fatal；
         expected_duration 为空 / 0 → 跳过该项（调用方没传就不断言）。
      2) 音轨：``_has_audio_stream(video_path) == False`` → fatal。
      3) 抽帧时间戳 verified：``frame_meta[i].verified == False`` → fatal（全帧/部分帧）。
      4) fps 异常（< 8 或 > 60）→ fatal。
    """
    items: list = []
    meta = fr.get("meta") or {}
    fr_duration = float(meta.get("duration") or fr.get("duration") or 0.0)
    fps = float(meta.get("fps") or 0.0)

    def _add(code: str, msg: str, fatal: bool = True) -> None:
        items.append({"code": code, "fatal": bool(fatal), "msg": msg})

    # 1) 时长偏差
    if expected_duration and expected_duration > 0 and fr_duration > 0:
        drift_th = float(cfg.get("video_max_drift", 0.30))
        drift = abs(fr_duration - float(expected_duration)) / float(expected_duration)
        if drift > drift_th:
            _add("duration_drift",
                 f"视频实测时长 {fr_duration:.2f}s 与期望 {float(expected_duration):.2f}s "
                 f"偏差 {drift*100:.0f}% 超阈值 {drift_th*100:.0f}%（H3 可能截断/补白）")

    # 2) 音轨探测
    if not _has_audio_stream(video_path):
        _add("no_audio", "视频无音轨（H3 输出无声 / 未合轨），需检查 ComfyUI 音轨")

    # 3) frame_meta.verified=False 计入 fatal
    unverified = sum(1 for fm in (fr.get("frame_meta") or [])
                     if fm and not fm.get("verified"))
    total_frames = len(fr.get("frame_meta") or [])
    if total_frames and unverified == total_frames:
        _add("frames_unverified",
             f"全部 {total_frames} 帧时间戳均未通过校验（抽帧可能失败/丢帧）")
    elif unverified:
        _add("frames_unverified",
             f"{unverified}/{total_frames} 帧时间戳未通过校验（可能存在丢帧）")

    # 4) fps 异常
    if fps and (fps < 8.0 or fps > 60.0):
        _add("fps_abnormal", f"视频帧率 {fps:.1f} fps 异常（正常 24-30 fps）")

    return items


def _video_obj_texts(items: list) -> list:
    """结构化客观缺陷项 → 人类可读文案列表（供 ``issues`` / meta 落盘）。"""
    return [str(it.get("msg") or "") for it in (items or []) if it.get("msg")]


def _video_obj_fatal(items: list) -> list:
    """结构化客观缺陷项 → **致命**项的文案列表（A-19：读 ``fatal`` 字段，非中文字串包含）。"""
    return [str(it.get("msg") or "") for it in (items or [])
            if it.get("fatal") and it.get("msg")]


def check_video(video_path: str, shot_desc: str = "", cfg: dict = None,
                override: dict = None, frames_dir: str = None,
                fallback_meta: dict = None, style: str = "",
                expected_duration: float = None,
                frame_ratio: list = None) -> dict:
    """视频质检：ffmpeg 抽帧 → 多模态判定。永不抛异常。
    override 仅用于「测试连通性」临时传参，不落盘。
    style：目标风格串，用于「风格达标」判定；为空则不做风格检测。
    expected_duration：期望时长（秒）—— G9/O1 确定性闸门：实测时长与期望偏差
        超 video_max_drift（默认 30%）计入 issues；为空 / 0 时跳过该项判定。
    frame_ratio：D-05（P1）—— 按占比指定抽帧时间点，整集模式传「每段中点」，
        使抽帧真正覆盖每一段（不再受 video_frame_count 默认 3 / 上限 6 约束）。
    """
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
                        fallback_meta=fallback_meta, frame_ratio=frame_ratio)
    if not fr["ok"]:
        return {"ok": False, "skipped": False, "error": fr["error"], "duration": fr["duration"],
                "frame_meta": fr.get("frame_meta") or [], "video_meta": fr.get("meta") or {}}

    style_norm = str(style or "").strip()
    prompt = (cfg.get("video_prompt") or DEFAULT_VIDEO_PROMPT).replace(
        "{shot_desc}", shot_desc or "（无）").replace(
        "{frame_count}", str(len(fr["frames"]))).replace(
        "{pass_score}", str(cfg.get("pass_score", 70))).replace(
        "{style}", style_norm or "（未指定）")
    ts_brief = "、".join(f"第{i + 1}帧 {fm['actual_ts']}s"
                        for i, fm in enumerate(fr.get("frame_meta") or []))
    prompt = f"共 {len(fr['frames'])} 张抽帧图片（按时间顺序；实际时间戳：{ts_brief}）。\n" + prompt
    prompt = prompt + WATERMARK_EXEMPT_NOTE + CRITICAL_RULE_NOTE + FRAMING_TOLERANCE_NOTE
    if style_norm:
        prompt = prompt + STYLE_CHECK_NOTE.replace("{style}", style_norm)
    # G9/O1 确定性闸门（视频客观层，零模型依赖）：命中致命缺陷（无音轨/时长超差/全帧未校验/fps异常）
    # → blocked=True、passed=False，AI 层仍跑但不短路，避免误杀。
    # A-19：``_video_objective_issues`` 现返回结构化项 {code, fatal, msg}，致命性由
    # ``fatal`` 字段（检测逻辑直接置位）决定，**不再靠中文字文案包含** —— 上游改措辞
    # （如「无音轨」→「音轨缺失」）不再让 objective_fatal 静默变空、漏掉致命缺陷。
    obj_items = _video_objective_issues(video_path, expected_duration, cfg, fr)
    obj_issues = _video_obj_texts(obj_items)      # 全部客观缺陷文案（并入 issues / 落盘）
    obj_fatal = _video_obj_fatal(obj_items)       # 仅致命项文案（读 fatal 字段，非中文字串）
    (fr.get("meta") or {}).update({"objective_issues": obj_issues,
                                  "objective_fatal": obj_fatal})
    try:
        verdict = _run_vision(ep, prompt, fr["frames"], cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"视频质检调用失败：{e}")
        # AI 层失败也要透出客观层致命缺陷（不能让视频质检因 AI 不可用就漏掉无音轨/时长问题）
        # P0-2：interface_fault=True 表示接口级故障（鉴权/超时/网络），非「视频内容不合格」。
        # 客观层缺陷仍照常透出（fatal 会阻断），但 AI 层故障本身不丢弃已渲染的成片。
        return {"ok": False, "skipped": False, "error": str(e),
                "api_attempts": getattr(e, "attempts", 1),
                "retryable": getattr(e, "retryable", None),
                "interface_fault": True,
                "frames": fr["frames"], "duration": fr["duration"],
                "frame_meta": fr.get("frame_meta") or [],
                "timestamps": fr.get("timestamps") or [],
                "video_meta": fr.get("meta") or {},
                "issues": list(obj_issues),
                "critical_issues": list(obj_fatal),
                "blocked": bool(obj_fatal),
                "objective_only": True}
    verdict.update({"frames": fr["frames"], "duration": fr["duration"],
                    "frame_count": len(fr["frames"]),
                    "frame_meta": fr.get("frame_meta") or [],
                    "timestamps": fr.get("timestamps") or [],
                    "duration_source": (fr.get("meta") or {}).get("source"),
                    "video_meta": fr.get("meta") or {}})
    # G9：把客观层 issues 并入 AI 层 verdict（AI 不报的客观缺陷仍保留；
    # 客观 fatal 项命中关键缺陷词表的会被 find_critical_issues 进一步识别）
    if obj_issues:
        verdict["issues"] = list(verdict.get("issues") or []) + list(obj_issues)
    if obj_fatal:
        verdict["critical_issues"] = list(verdict.get("critical_issues") or []) + list(obj_fatal)
        verdict["blocked"] = True
        verdict["passed"] = False
        verdict["objective_fatal"] = True
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
