"""
ComfyUI API 客户端（v2 / 2026-09 修复版）
- 角色：Qwen 2512 生成基础图 → Qwen Edit 2511 生成多视图（正面/左侧/右侧/背面）
- 物品：Qwen 2512 生成基础图 → Qwen Edit 2511 生成 3D 多视角（正面/左45°/右45°/俯视）
- 场景：Qwen 2512 生成基础图 → Qwen Edit 2511 生成 3D 多视角（正面/左45°/右45°/俯视）
- 视频：MiniMax H3（Ref2VA）10 段无缝生成

v2 关键修复：
1. UI(node-graph) → API 转换改为「节点自带 widgets_values_named 优先」，
   仅在缺失时按 object_info 声明的控件顺序做位置兜底（并剔除 control_after_generate
   等前端伪控件），彻底解决 KSampler 参数错位（steps='randomize'）。
2. 展开 ComfyUI 新版 subgraph（UUID 型 class_type 实例）：把子图内部真实节点
   抽取为 API 节点，并把 SetNode/GetNode/Reroute 走通为「值来源重定向」。
3. 过滤 MarkdownNote / Note / Label / Reroute / PrimitiveNode 等虚拟节点。
4. 多视角：3 个 LoadImageOutput 参考图全部替换（原实现只改第一个）。
5. 视频：前端 HTTP 资源路径解析为本地绝对路径；H3 的 10 段 Text Multiline
   全部替换（原实现只改第一段），并按 clip 序号一一对应。
"""
import os
import re
import json
import time
import copy
import shutil
import random
import logging
import threading
import requests
import cancellation  # S9：远端任务取消（中止信号贯穿 ComfyUI 轮询，与 pipeline/llm_client 同一套）
from typing import Dict, List, Optional, Any, Tuple, Sequence
# ⚠️ Sequence 曾被漏导入：类级注解 `_LIGHT_KEYWORDS: Sequence[...]` 在类创建时**不求值**，
# 所以模块照常导入、py_compile 也通过，但一旦有工具读取
# `ComfyUIClient.__annotations__` 或调用 `typing.get_type_hints()` 就会抛
# NameError: name 'Sequence' is not defined（实测）。别删这个导入。

from config import (
    COMFYUI_URL, COMFYUI_WORKFLOWS_DIR, COMFYUI_OUTPUT_DIR,
    PROJECT_OUTPUT_DIR, WORKFLOW_TEMPLATE, MULTIVIEW_CONFIG,
    H3_EMIT_AUDIO,
    CONFLICT_NEGATIVE_TOKENS,
)
from dialogue_utils import (dialogue_text as _dlg_text, format_line as _dlg_line,
                            dialogue_speaker as _dlg_speaker)
from h3_episode_builder import H3EpisodeBuilder
import style_kit
import h3_prompt_kit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ===================== 调用统计（P2-3 成本看板数据源） =====================
# 只做进程内累计计数，不落盘、不影响业务；analytics 模块按需读取。
_CALL_STATS = {
    "prompt_submitted": 0,      # 提交到 /prompt 的次数
    "prompt_failed": 0,         # 提交失败次数
    "completed": 0,             # 等待完成的次数（成功）
    "waited_seconds": 0.0,      # 累计等待时长（GPU 在跑的时间近似值）
    "segments_generated": 0,    # 累计生成段数（视频）
}
_CALL_STATS_LOCK = threading.Lock()


def get_call_stats() -> dict:
    """读取调用统计快照"""
    with _CALL_STATS_LOCK:
        return dict(_CALL_STATS)


def reset_call_stats() -> dict:
    """重置调用统计"""
    with _CALL_STATS_LOCK:
        for k in list(_CALL_STATS.keys()):
            _CALL_STATS[k] = 0 if not isinstance(_CALL_STATS[k], float) else 0.0
        return dict(_CALL_STATS)


def _bump(key: str, delta=1) -> None:
    """安全累加统计项（统计失败绝不影响业务）
    B-19 H1：读改写包锁，避免多线程并发竞争导致计数丢失。
    """
    try:
        with _CALL_STATS_LOCK:
            _CALL_STATS[key] = _CALL_STATS.get(key, 0) + delta
    except Exception as e:  # noqa: BLE001
        logger.warning("调用统计累加失败（计数可能失真）：%s", e)

# 前端伪控件 / 虚拟节点（不应提交给后端）
PSEUDO_WIDGETS = {
    "upload", "refresh", "Constant",
    "Auto-refresh after generation", "control_after_generate_widget",
}
# 位置兜底时需要剔除的「执行后动作」取值（跟随在 seed 之后）
PSEUDO_VALUES = {"fixed", "increment", "decrement", "randomize"}
# 需要跳过的虚拟节点类型
VIRTUAL_NODE_TYPES = {
    "MarkdownNote", "Note", "Label (rgthree)", "Reroute", "PrimitiveNode", "Bookmark",
}
# 正向提示词判定：出现这些词视为负向提示词
NEGATIVE_HINTS = ("模糊", "水印", "blurry", "watermark", "low quality", "worst quality", "低质量", "噪点")

# ⚠️ 「关键词命中」不足以判定负向槽位：正向提示词**主动**写「画面中不得出现任何文字、
#    字幕、水印、logo」这类否定式约束是常态（见 build_storyboard_prompt 的收尾段），
#    其中的「水印」二字是正向语义。若按裸关键词把它判成负向槽位，后果是
#    clean_conflict_negative_tokens 会在**正向**提示词里删掉 CONFLICT_NEGATIVE_TOKENS，
#    甚至把「水印/logo/文字/AI生成」当负向词追加到正向提示词（历史去水印强化踩过的坑）。
#    判据：命中 NEGATIVE_HINTS，**且不是**否定式正向前缀。
_NEG_CONSTRAINT_RE = re.compile(
    r"(不得|严禁|禁止|不要|避免|杜绝|切勿|不含|不包含|没有|无)"
    r"[^，。；;、]{0,16}"
    r"(水印|文字|字幕|logo|标识|签名|日期戳|模糊|噪点|low quality|blurry|watermark)",
    re.IGNORECASE,
)


def _is_negative_slot(value: str) -> bool:
    """该提示词槽位是否为「负向槽位」（本模块 4 处提示词节点判定共用同一判据）。

    ⚠️ 不要退回成裸关键词命中 —— 那会把正向的「不得出现…水印」判成负向槽位
    （实测导致 77% 的正向提示词被追加裸负面词）。
    """
    if not value:
        return False
    if _NEG_CONSTRAINT_RE.search(value):
        return False        # 「不得出现 X」= 正向约束，不是负向槽位
    low = value.lower()
    return any(h.lower() in low for h in NEGATIVE_HINTS)


# ===================== 提示词槽位极性（正负同体节点） =====================
#
# QwenImage2.1 起，``TextEncodeQwenImage21`` 把正向(prompt)与负向(negative_prompt)
# **装在同一个节点**里，并输出 positive / negative / latent 三路。这打破了本模块此前
# 「一个提示词节点一个极性，靠 NEGATIVE_HINTS 内容启发式判极性」的隐含前提：
#
#   把该节点的所有字符串输入拼起来判极性 → 必然命中「模糊/水印」等负向词
#   → 整个节点被判成负向槽位。后果有两个方向，且**都不会报错**：
#     ① ``_generate_base_image`` 落正向提示词时按「非 text 字段就把所有字符串输入都写成
#        正向词」的老逻辑走 → 把 negative_prompt 覆盖成正向提示词，负向词彻底失效；
#     ② ``clean_conflict_negative_tokens`` 会因为字段名 (``negative_prompt``)
#        不在 PROMPT_TEXT_FIELDS 里而**完全跳过负向槽位**。
#
# 所以极性判定改为「节点类型优先、内容启发式兜底」：
#   - 正负同体节点 → 按**字段名**定极性，不做任何内容启发式；
#   - 老模板（CLIPTextEncode / TextEncodeQwenImageEditPlus）→ 沿用 _is_negative_slot。
#: 「正负同体」提示词节点类型
COMBINED_PROMPT_NODE_TYPES = ("TextEncodeQwenImage21",)
#: 正负同体节点里承载正向 / 负向文本的字段名
COMBINED_POSITIVE_FIELD = "prompt"
COMBINED_NEGATIVE_FIELD = "negative_prompt"
#: 图片链路的提示词编码节点白名单（集中一处，避免 4 处手抄漂移）
PROMPT_NODE_TYPES = ("CLIPTextEncode", "TextEncodeQwenImageEditPlus",
                     "TextEncodeQwenImage21")


def _node_sort_key(nid) -> int:
    """节点 id 排序键（数字 id 按数值，非数字 id 视作 0 —— 与历史行为一致）"""
    return int(nid) if str(nid).isdigit() else 0


#: 「动态展开」输入类型：object_info 里只登记**父名**，子项在提交时以点号键出现
#:   COMFY_AUTOGROW_V3     → images.image_1 …
#:   COMFY_DYNAMICCOMBO_V3 → format.bit_depth / format.input_color_space …
DYNAMIC_INPUT_TYPES = ("COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3")


def _is_dynamic_child(declared: dict, key: str) -> bool:
    """``key`` 是否是某个动态展开输入（autogrow / dynamic-combo）的子项？

    服务端 `_expand_schema_for_dynamic` / `DynamicCombo` 就是这样生成子项名的，
    校验器若只比对父名，会把合法的 `images.image_1`、`format.bit_depth` 全报成
    「未知输入」——纯误报，会把真异常淹掉。
    """
    if "." not in str(key):
        return False
    parent = str(key).rsplit(".", 1)[0]
    spec = declared.get(parent)
    return bool(spec) and isinstance(spec, (list, tuple)) and bool(spec) \
        and spec[0] in DYNAMIC_INPUT_TYPES


def _prompt_slot_polarity(class_type: str, field: str, value) -> str:
    """提示词槽位极性：``"pos"`` / ``"neg"`` / ``""``（不是提示词槽位）。

    - 正负同体节点：按**字段名**判（prompt=正向、negative_prompt=负向），
      空串也算（负向槽位默认就是空的，必须能被加固写入）；
    - 老模板：空槽位返回 ""（无可判内容，跳过），否则走 `_is_negative_slot` 启发式。
    """
    if field not in PROMPT_TEXT_FIELDS or not isinstance(value, str):
        return ""
    if class_type in COMBINED_PROMPT_NODE_TYPES:
        if field == COMBINED_POSITIVE_FIELD:
            return "pos"
        if field == COMBINED_NEGATIVE_FIELD:
            return "neg"
        return ""
    if not value.strip():
        return ""
    return "neg" if _is_negative_slot(value) else "pos"


def _iter_prompt_slots(api_prompt: dict, class_types=PROMPT_NODE_TYPES):
    """遍历提示词槽位，产出 ``(节点id, 字段名, 取值, 极性)``。

    判断单一来源：极性一律经 `_prompt_slot_polarity`，调用方不再各写一套启发式。
    """
    for nid, node in (api_prompt or {}).items():
        if not isinstance(node, dict):
            continue
        ctype = str(node.get("class_type") or "")
        if ctype not in class_types:
            continue
        inputs = node.get("inputs") or {}
        for field in PROMPT_TEXT_FIELDS:
            value = inputs.get(field)
            polarity = _prompt_slot_polarity(ctype, field, value)
            if polarity:
                yield nid, field, value, polarity


# ===================== 参考图槽位键名 =====================
# 两代编辑节点两种键名，必须都认（只认老键名 → 新模板参考图静默不注入）：
#   Qwen-Edit 2511 / TextEncodeQwenImageEditPlus :  image1 / image2 / image3
#   QwenImage2.1 / TextEncodeQwenImage21         :  images.image_1 … (autogrow 点号键)
_IMAGE_SLOT_RE = re.compile(r"^(?:images\.)?image_?(\d*)$")


def _slot_index(key) -> int:
    """参考图槽位序号（``images.image_3`` / ``image3`` → 3；无语尾数字 → 0）"""
    m = _IMAGE_SLOT_RE.match(str(key))
    return int(m.group(1)) if (m and m.group(1)) else 0


# 分镜图景别强约束：仅写「特写」二字时模型容易退化为中景/近景，这里给出显式构图规范
SHOT_CAMERA_SPECS = {
    "特写": ("特写镜头（close-up）：镜头极贴近主体，人物面部（或手部、道具局部）占据画面 70% 以上面积，"
             "背景明显虚化，只呈现局部，严禁退为近景、中景或全景"),
    "近景": ("近景镜头（medium close-up）：取景自人物胸部以上至头顶，面部细节清晰，"
             "严禁退为中景或全景"),
    "中景": ("中景镜头（medium shot）：取景自人物腰部或膝部以上至头顶，人物占画面一半左右，"
             "可带入部分环境；**严禁退为全景/远景**（不得出现膝盖以下部位、脚部或大片地面）"),
    "全景": "全景镜头（wide shot）：完整呈现人物全身及其所处环境，人物占画面高度的大半；**不得退为远景色块**",
    "远景": "远景镜头（long shot）：人物在画面中较小、环境为主体，强调空间感与氛围；**不得推成中景/近景**",
}
#: 景别关键字的解析顺序（**具体优先**）：剧本里 camera 字段常是「景别+运镜」的复合写法
#: （如「特写推入」「全景升降」「中景跟拍」），必须按关键字解析，不能只做精确匹配。
_CAMERA_KEY_ORDER = ("特写", "远景", "全景", "近景", "中景")

#: 机位/视角关键字。与景别**正交**：剧本 camera 字段里既有景别（中景/特写）也有机位（俯拍/仰拍）。
#: ⚠️ 历史缺陷：机位以前完全没人解析，等于白写在剧本里 —— 生成端不知道要俯拍，
#: 质检端也没有依据判机位，于是「要求俯拍却给了平视」既没被约束也没被检出。
_CAMERA_ANGLE_SPECS = {
    "俯拍": "俯拍（高角度）：镜头高于主体自上向下俯视，画面能看到主体顶部/脚前的地面",
    "仰拍": "仰拍（低角度）：镜头低于主体自下向上仰视，主体显得高大压迫",
    "平视": "平视：镜头与主体视线同高",
    "环绕": "环绕：镜头绕主体转动（在静帧里体现为明显的侧向机位）",
    "过肩": "过肩：越过前景人物肩部拍向主体",
    "斜侧": "斜侧机位：镜头相对主体明显偏斜（非正面）",
}
_CAMERA_ANGLE_ORDER = ("俯拍", "仰拍", "平视", "环绕", "过肩", "斜侧")

#: 机位**同义词**：剧本写法很自由，只认「俯拍」不认「俯视」等于漏掉一半机位标注
#: （漏掉的后果与「机位没人解析」一样：生成端不约束、质检端不判定）。
#: 解析顺序：先用 _CAMERA_ANGLE_ORDER 的正式词（具体优先），再回落到本表。
_CAMERA_ANGLE_ALIASES = {"俯视": "俯拍", "高角度": "俯拍", "高机位": "俯拍",
                         "仰视": "仰拍", "低角度": "仰拍", "低机位": "仰拍",
                         "平角": "平视", "水平视角": "平视",
                         "环摇": "环绕", "绕拍": "环绕"}

#: 景别未指定时的判定标准（camera 只给了机位/运镜）。
#: 这段文字会被同时注入**生成端**与**质检端**，因此措辞必须两边都说得通。
CAMERA_UNSPECIFIED_SPEC = (
    "本镜未指定景别（camera 字段只给了机位/运镜，如「俯拍缓推」「环绕慢摇」）："
    "取景范围以「动作与画面内容」的描述为准，**不要按某个固定景别去套**，"
    "也**不得据此判定景别不符**；只判「机位/构图/主体清晰度/是否崩坏」"
)


def camera_key(camera) -> str:
    """从复合写法里解析出景别关键字（``特写推入`` → ``特写``；``全景升降`` → ``全景``）

    ⚠️ 这修的是一个**双向错位**的根因：
    剧本的 camera 字段是「景别+运镜」（特写推入 / 中景跟拍 / 全景升降），而
    ``SHOT_CAMERA_SPECS`` 只有精确键。原实现 ``SPECS.get(camera) or SPECS["中景"]``
    对任何复合写法都回落到**中景规格** —— 于是生成端给「特写推入」的镜头写的是
    「中景：腰部以上至头顶」，而质检端读的是字面「特写」，两端同时错位，
    实测分镜图质检通过率仅 57%、失败原因几乎全是「景别不符」。

    ⚠️ **2026-09-20 再修：只给机位/运镜、没有景别词时不再猜「中景」，改返回空串（未指定）。**
    实测《蛊真人》ep02 shot_13：``camera = "俯拍缓推"``，description 是
    「镜头自方源脚面俯拍：灰白山石上积了一大滩血水…他清瘦的靴底半浸其中」——
    本质是**脚部俯拍特写**。猜成「中景」会同时污染两端：
      · 生成端 → 注入「中景：取景自腰部或膝部以上」与 description 的脚部俯拍
        **直接互斥**，模型在两条矛盾指令间摇摆，6 次重试出的全是「全景 + 平视」；
      · 质检端 → 拿「中景（腰部或膝部以上）」去判一张脚部俯拍图，必然判「景别不符」，
        该镜**永远不可能通过**，白烧 6 次 GPU（max_retries=5）。
    返回空串后：生成端不注入景别硬约束、质检端不做景别判定 ——
    「宁可不说，也不要拿一个猜错的标准去判」。
    """
    s = str(camera or "").strip()
    if not s:
        return "中景"
    if s in SHOT_CAMERA_SPECS:
        return s
    for k in _CAMERA_KEY_ORDER:
        if k in s:
            return k
    # 只有机位/运镜词（俯拍缓推 / 环绕慢摇 / 拉远）→ **不猜**，交回上层按「未指定」处理
    return ""


def camera_angle(camera) -> str:
    """解析机位/视角（``俯拍缓推`` → ``俯拍``）；没有机位词时返回空串。

    与 :func:`camera_key`（景别）正交：两者都要各自注入生成端与质检端，
    否则「要求俯拍却给了平视」这类偏差既没人约束也没人检出。
    """
    s = str(camera or "").strip()
    for k in _CAMERA_ANGLE_ORDER:
        if k in s:
            return k
    for alias, key in _CAMERA_ANGLE_ALIASES.items():
        if alias in s:
            return key
    return ""


def camera_spec(camera) -> str:
    """取景别（镜头类型）的**权威判定标准**（生成端与质检端共用同一份）

    见 :func:`camera_key` 说明：必须能解析复合写法，否则两端标准会错位；
    景别确实未给时返回 :data:`CAMERA_UNSPECIFIED_SPEC`（而不是编一个中景）。
    """
    k = camera_key(camera)
    return SHOT_CAMERA_SPECS[k] if k else CAMERA_UNSPECIFIED_SPEC


SHOT_ACTION_SUFFIX = ("；上述动作必须完整、明确地表现出来（动作结果一眼可辨，如道具已收起、已离开手部），"
                      "不得省略、弱化或只做出起始姿态")

# 提示词所在字段：
#   CLIPTextEncode.text / TextEncodeQwenImageEditPlus.prompt
#   TextEncodeQwenImage21.prompt(正向) + TextEncodeQwenImage21.negative_prompt(负向)
# ⚠️ negative_prompt 必须在内：漏掉它会让「冲突负向词清理」在
#    QwenImage2.1 上**整段静默跳过**（字段名对不上 → 一句都没改，也不报错）。
PROMPT_TEXT_FIELDS = ("text", "prompt", "negative_prompt")
# 需要剥离的 LoadImage* 前端显示后缀
SUFFIX_RE = re.compile(r"\s*\[(output|input|temp)\]\s*$")

# ===================== P0 修复：正负提示词冲突 / 场景资产带人 / 画幅统一 =====================

# 1) 正负提示词冲突：按长度降序匹配，保证「3D渲染」先于「3D」被命中
CONFLICT_NEGATIVE_SORTED = tuple(sorted(CONFLICT_NEGATIVE_TOKENS, key=len, reverse=True))

# 2) 场景资产禁止出现人物：
#    场景定义里常带「村民三两结伴」这类人物描述，会直接渲染出人（实测"山间小径"资产图 4 人）。
#    正向追加空场景声明 + 生成前清洗掉人物描述短语。
SCENE_NO_CHARACTER_SUFFIX = (
    "。空场景：画面中不得出现任何人物、人影、人群、士兵或生物，"
    "只呈现环境本身（建筑、地形、植被、道具与光影）"
)
# 人物类词汇（两字及以上，避免误伤"人间仙境/人迹罕至"等场景词）
CHARACTER_WORDS = (
    "人物", "角色", "主角", "村民", "人群", "人们", "众人", "行人", "路人", "群众", "游客", "游人",
    "士兵", "侍卫", "侍从", "仆人", "仆役", "孩童", "孩子", "小孩", "老者", "老人", "青年", "少女",
    "少年", "男子", "女子", "人影", "身影", "侠客", "武者", "修士", "商贩", "摊贩", "渔夫", "农夫",
    "僧人", "道士", "骑士", "守卫", "弟子", "随从", "观众", "看客", "陌生人", "男女老少",
    "人山人海", "三三两两", "结伴", "成群结队", "熙熙攘攘", "往来穿梭", "人头攒动",
)
# 数量+人（如"4人""四个人""几人群"）
CHARACTER_QTY_RE = re.compile(
    r"[0-9０-９一二三四五六七八九十两几数多]+\s*(?:个|名|位|群|队|对)?\s*(?:人|人物|人影|身影)"
)
# 提示词分句符（按句清洗，人物句整句丢弃）
_PROMPT_SPLIT_RE = re.compile(r"[，,；;。\n]")

# 目录标注（P0-5 修复）：
#   LoadImageOutput / LoadAudioOutput / LoadVideoOutput 等「Output 系列」读取 ComfyUI
#   output 目录，写入的 image/audio/video 值必须带 " [output]" 标注，否则 ComfyUI 的
#   folder_paths.exists_annotated_filepath() 会回退到 input 目录查找并报
#   400 "Invalid image file"。
#   LoadImage / LoadAudio / LoadVideo 等读取 input 目录，禁止带标注。
ANNOTATED_DIR = "output"
OUTPUT_LOAD_CLASSES = {"LoadImageOutput", "LoadAudioOutput", "LoadVideoOutput",
                       "LoadLatentOutput", "LoadImageMaskOutput"}
INPUT_LOAD_CLASSES = {"LoadImage", "LoadAudio", "LoadVideo", "LoadImageMask",
                      "LoadAnimatedImage", "LoadLatent"}
# 文件名型（媒体）输入键：转换时需按节点类型规范化目录标注
MEDIA_INPUT_KEYS = {"image", "images", "audio", "video", "file", "filename", "path",
                    "image_path", "audio_path", "video_path"}


class ComfyUIClient:
    """ComfyUI API 客户端"""

    def __init__(self, base_url: str = COMFYUI_URL):
        self.base_url = base_url.rstrip("/")
        self.client_id = self._generate_client_id()
        self._object_info = None
        self._object_info_ts = 0.0
        self.last_convert_meta: Dict[str, Any] = {}

    def _generate_client_id(self) -> str:
        import uuid
        return str(uuid.uuid4())

    # ===================== 基础 HTTP =====================

    def _get(self, path: str, timeout: int = 30) -> Any:
        resp = requests.get(f"{self.base_url}{path}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict = None) -> Any:
        resp = requests.post(f"{self.base_url}{path}", json=data or {}, timeout=300)
        resp.raise_for_status()
        return resp.json()

    def get_status(self, timeout: int = 3) -> dict:
        """探测 ComfyUI 在线状态。

        注意：这是高频调用的状态接口（前端每次刷新都会打），
        因此**必须用短超时**——默认 3 秒。用 requests 的 (connect, read) 元组形式，
        保证「主机不可达时快速失败」而不是干等 30 秒（Windows 上防火墙丢包会更久）。
        """
        try:
            resp = requests.get(f"{self.base_url}/system_stats",
                                timeout=(min(2, timeout), timeout))
            resp.raise_for_status()
            return {"status": "online", "stats": resp.json()}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def get_object_info(self, force: bool = False, ttl: int = 300) -> dict:
        """获取 /object_info（带缓存，离线时返回空 dict，不影响主流程）"""
        now = time.time()
        if not force and self._object_info is not None and (now - self._object_info_ts) < ttl:
            return self._object_info
        try:
            self._object_info = self._get("/object_info", timeout=60)
            self._object_info_ts = now
        except Exception as e:
            logger.warning(f"获取 object_info 失败（将跳过严格校验）: {e}")
            if self._object_info is None:
                self._object_info = {}
        return self._object_info

    def declared_inputs(self, class_type: str) -> List[str]:
        """按 object_info 声明顺序返回节点可声明的输入名（required 在前，optional 在后）"""
        oi = self.get_object_info().get(class_type) or {}
        spec = oi.get("input") or {}
        names = list((spec.get("required") or {}).keys()) + list((spec.get("optional") or {}).keys())
        return names

    # ===================== 工作流加载与转换 =====================

    def load_workflow(self, workflow_file: str, return_meta: bool = False):
        """加载工作流文件并转换为 API prompt 格式"""
        workflow_path = os.path.join(COMFYUI_WORKFLOWS_DIR, workflow_file)
        with open(workflow_path, "r", encoding="utf-8-sig") as f:   # 兼容 UTF-8 BOM
            wf = json.load(f)
        return self.to_api(wf, return_meta=return_meta)

    @staticmethod
    def _is_api_format(wf: dict) -> bool:
        if not isinstance(wf, dict) or not wf:
            return False
        if "nodes" in wf or "links" in wf:
            return False
        return all(isinstance(v, dict) and "class_type" in v for v in wf.values())

    def to_api(self, wf: dict, return_meta: bool = False):
        """UI(node-graph) → API prompt"""
        if self._is_api_format(wf):
            meta = {"already_api": True, "node_count": len(wf)}
            return (wf, meta) if return_meta else wf
        api, meta = self._ui_to_api(wf)
        self.last_convert_meta = meta
        return (api, meta) if return_meta else api

    # ---------- 核心转换 ----------

    @staticmethod
    def _link_specs(links, prefix: str) -> Dict[Any, Tuple[str, int]]:
        """link 列表 → {link_id: ('node', 扁平节点id, 输出槽位)}"""
        out: Dict[Any, Tuple[str, int]] = {}
        for l in links or []:
            if isinstance(l, dict):
                lid, oid, oslot = l.get("id"), l.get("origin_id"), l.get("origin_slot", 0)
            elif isinstance(l, (list, tuple)) and len(l) >= 3:
                lid, oid, oslot = l[0], l[1], l[2]
            else:
                continue
            if lid is None or oid is None:
                continue
            out[lid] = ("node", f"{prefix}{oid}", oslot)
        return out

    def _ui_to_api(self, ui_workflow: dict) -> Tuple[dict, dict]:
        subgraphs: Dict[str, dict] = {}
        for sg in ((ui_workflow.get("definitions") or {}).get("subgraphs") or []):
            if sg.get("id"):
                subgraphs[sg["id"]] = sg

        flat: Dict[str, dict] = {}            # 扁平真实节点: id -> {type, named, seq}
        pending: List[tuple] = []             # (节点id, 输入名, 取值 spec)
        redirect: Dict[tuple, tuple] = {}      # (节点id, 槽位) -> spec（含 GetNode/Reroute/子图输出）
        set_map: Dict[str, tuple] = {}         # SetNode 名 -> spec
        inst_records: List[tuple] = []         # (顺序, clip号, 实例id, prompt 的 spec)
        warnings: List[str] = []
        seq = [0]

        def walk(nodes, links, prefix, depth, in_bindings, in_link_names, in_slot_names,
                 inode, onode):
            link_spec = self._link_specs(links, prefix)
            out_alias: Dict[int, tuple] = {}

            for raw in nodes or []:
                nid = raw.get("id")
                ntype = raw.get("type") or ""
                fid = f"{prefix}{nid}"
                named = dict(raw.get("widgets_values_named") or {})

                # 1) 子图实例（UUID 型 class_type）→ 递归展开
                sg = subgraphs.get(ntype)
                if sg and depth < 6:
                    inst_in: Dict[str, tuple] = {}
                    for inp in raw.get("inputs") or []:
                        nm, lid = inp.get("name"), inp.get("link")
                        if lid is not None and lid in link_spec:
                            inst_in[nm] = link_spec[lid]
                        elif nm in named:
                            inst_in[nm] = ("lit", named[nm])
                    pin_names: Dict[Any, str] = {}
                    slot_names: List[str] = []
                    for pin in sg.get("inputs") or []:
                        slot_names.append(pin.get("name"))
                        for lid in (pin.get("linkIds") or []):
                            pin_names[lid] = pin.get("name")
                    iid = (sg.get("inputNode") or {}).get("id")
                    oid = (sg.get("outputNode") or {}).get("id")
                    inner_alias = walk(sg.get("nodes"), sg.get("links") or [], fid + ":", depth + 1,
                                       inst_in, pin_names, slot_names, iid, oid)
                    for slot, spec in inner_alias.items():
                        redirect[(fid, slot)] = spec
                    # 记录该 clip 的提示词节点（供 10 段提示词精确替换）
                    if "prompt" in inst_in:
                        clip_no = None
                        m = re.search(r"(\d+)", str(named.get("prompt", "")))
                        if m:
                            clip_no = int(m.group(1))
                        inst_records.append((len(inst_records), clip_no, fid, inst_in["prompt"]))
                    continue

                # 2) 子图内部虚拟输入/输出节点
                if (inode is not None and nid == inode) or (onode is not None and nid == onode):
                    continue

                # 3) Set / Get / Reroute / PrimitiveNode → 重定向
                if ntype == "SetNode":
                    nm = named.get("Constant")
                    src = None
                    for inp in raw.get("inputs") or []:
                        if inp.get("link") in link_spec:
                            src = link_spec[inp["link"]]
                            break
                    if nm is not None and src is not None:
                        set_map[nm] = src
                    continue
                if ntype == "GetNode":
                    nm = named.get("Constant")
                    if nm in set_map:
                        redirect[(fid, 0)] = set_map[nm]
                    else:
                        warnings.append(f"GetNode {fid} 引用了未定义变量 {nm!r}")
                        redirect[(fid, 0)] = ("lit", None)
                    continue
                if ntype == "Reroute":
                    for inp in raw.get("inputs") or []:
                        if inp.get("link") in link_spec:
                            redirect[(fid, 0)] = link_spec[inp["link"]]
                            break
                    continue
                if ntype == "PrimitiveNode":
                    vals = [v for v in (raw.get("widgets_values") or []) if v is not None]
                    redirect[(fid, 0)] = ("lit", vals[0] if vals else None)
                    continue
                if ntype in VIRTUAL_NODE_TYPES:
                    continue

                # 4) 真实节点
                flat[fid] = {"type": ntype, "named": named, "seq": seq[0]}
                seq[0] += 1
                for inp in raw.get("inputs") or []:
                    nm, lid = inp.get("name"), inp.get("link")
                    if nm is None or lid is None or lid not in link_spec:
                        continue
                    spec = link_spec[lid]
                    # 子图内部：该输入来自子图外部绑定
                    if inode is not None and spec[1] == f"{prefix}{inode}":
                        bind_name = in_link_names.get(lid)
                        if bind_name is None and in_slot_names and spec[2] < len(in_slot_names):
                            bind_name = in_slot_names[spec[2]]
                        spec = (in_bindings or {}).get(bind_name, ("lit", None))
                    pending.append((fid, nm, spec))

            # 子图输出槽位 → 本层真实来源
            if onode is not None:
                for l in links or []:
                    if isinstance(l, dict) and l.get("target_id") == onode:
                        out_alias[l.get("target_slot", 0)] = (
                            "node", f"{prefix}{l.get('origin_id')}", l.get("origin_slot", 0))
                    elif isinstance(l, (list, tuple)) and len(l) >= 5 and l[3] == onode:
                        out_alias[l[4]] = ("node", f"{prefix}{l[1]}", l[2])
            return out_alias

        walk(ui_workflow.get("nodes") or [], ui_workflow.get("links") or [], "", 0,
             {}, {}, [], None, None)

        # ---------- 取值解析（跟随重定向链） ----------

        def resolve(spec, depth=0):
            if spec is None or depth > 16:
                return None
            if spec[0] == "lit":
                return spec
            if (spec[1], spec[2]) in redirect:
                return resolve(redirect[(spec[1], spec[2])], depth + 1)
            if spec[1] not in flat:
                return None
            return spec

        # 位置兜底所需的控件名（object_info 不可用时退化为 inputs 里的 widget 项）
        def fallback_widgets(raw):
            named = raw.get("widgets_values_named") or {}
            if named:
                return dict(named)
            widget_names = [i.get("name") for i in (raw.get("inputs") or []) if "widget" in i]
            values = list(raw.get("widgets_values") or [])
            if len(values) > len(widget_names):
                # 剔除伪控件取值（如 seed 后的 'randomize'）
                values = [v for idx, v in enumerate(values)
                          if not (idx > 0 and isinstance(v, str) and v in PSEUDO_VALUES)]
            while len(values) > len(widget_names):
                values.pop()
            return dict(zip(widget_names, values))

        # 重新遍历原始节点树取得「未使用 named 的节点」的兜底控件值
        raw_nodes_by_id: Dict[str, dict] = {}

        def collect(nodes, prefix):
            for raw in nodes or []:
                fid = f"{prefix}{raw.get('id')}"
                raw_nodes_by_id[fid] = raw
                sg = subgraphs.get(raw.get("type") or "")
                if sg:
                    collect(sg.get("nodes"), fid + ":")

        collect(ui_workflow.get("nodes") or [], "")

        # ---------- 组装 API prompt ----------

        pending_by_node: Dict[str, List[tuple]] = {}
        for pid, nm, spec in pending:
            pending_by_node.setdefault(pid, []).append((nm, spec))

        api: Dict[str, dict] = {}
        unresolved: List[str] = []
        for fid, info in sorted(flat.items(), key=lambda kv: kv[1]["seq"]):
            named = info["named"]
            if not named and fid in raw_nodes_by_id:
                named = fallback_widgets(raw_nodes_by_id[fid])
            inputs: Dict[str, Any] = {}
            ctype = info["type"]
            for k, v in named.items():
                if k in PSEUDO_WIDGETS:
                    continue
                if isinstance(v, str) and ctype.startswith("Load") and k in MEDIA_INPUT_KEYS:
                    # P0-5：按节点「读取目录」规范化标注——Load*Output 必须带 " [output]"，
                    # LoadImage 等 input 目录节点写裸文件名（不得带标注）
                    v = self.annotate_file_ref(v, ctype)
                inputs[k] = v
            for nm, spec in pending_by_node.get(fid, []):
                r = resolve(spec)
                if r is None:
                    unresolved.append(f"{fid}.{nm}")
                    continue
                if r[0] == "lit":
                    inputs[nm] = r[1]
                else:
                    inputs[nm] = [r[1], r[2]]
            api[fid] = {"class_type": info["type"], "inputs": inputs}

        # clip → 提示词节点映射（解析后回填）
        clip_prompt_nodes: List[tuple] = []
        for order, clip_no, fid, spec in inst_records:
            r = resolve(spec)
            if r and r[0] == "node":
                clip_prompt_nodes.append((clip_no if clip_no is not None else order + 1, r[1]))
        clip_prompt_nodes.sort(key=lambda x: x[0])

        meta = {
            "flat_nodes": len(api),
            "subgraph_count": len(subgraphs),
            "clip_prompt_nodes": clip_prompt_nodes,
            "unresolved_inputs": unresolved,
            "warnings": warnings,
        }
        if unresolved:
            logger.warning(f"UI→API 转换存在未解析输入 {len(unresolved)} 个: {unresolved[:5]}")
        logger.info(f"UI→API 完成: 真实节点 {len(api)} 个, 子图 {len(subgraphs)} 个, "
                    f"clip→提示词节点 {clip_prompt_nodes}")
        return api, meta

    # ---------- 转换结果自检 ----------

    def validate_api_prompt(self, api_prompt: dict, verbose: bool = False) -> dict:
        """用 object_info 校验转换结果：未知节点类型 / 缺失必填输入 / 悬空连线"""
        oi = self.get_object_info()
        ids = set(api_prompt.keys())
        unknown_types, missing_required, dangling, unexpected = [], [], [], []
        for nid, node in api_prompt.items():
            ct = node.get("class_type")
            spec = (oi.get(ct) or {}).get("input") if oi else None
            if oi and ct not in oi:
                unknown_types.append(f"{nid}:{ct}")
            for k, v in (node.get("inputs") or {}).items():
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                    if v[0] not in ids:
                        dangling.append(f"{nid}.{k} -> {v[0]}")
                elif spec is not None:
                    declared = (spec.get("required") or {}) | (spec.get("optional") or {})
                    if k not in declared and not _is_dynamic_child(declared, k):
                        unexpected.append(f"{nid}.{k}")
            if spec:
                for req in (spec.get("required") or {}).keys():
                    if req in (node.get("inputs") or {}):
                        continue
                    # autogrow 输入（COMFY_AUTOGROW_V3，如 TextEncodeQwenImage21.images）：
                    # object_info 里它挂在 required 下，但服务端 `_expand_schema_for_dynamic`
                    # 会把模板名展开成**点号键**（images.image_1 …）并全部登记为 optional，
                    # `template.min=0` 时一个都不传也完全合法（execute 收到 {}）。
                    # 所以：min=0 → 永不算缺失；min>0 → 有任一 `req.xxx` 键即满足。
                    req_spec = (spec.get("required") or {}).get(req) or []
                    if req_spec and req_spec[0] == "COMFY_AUTOGROW_V3":
                        extra = req_spec[1] if len(req_spec) > 1 and isinstance(req_spec[1], dict) else {}
                        if (extra.get("template") or {}).get("min", 0) == 0:
                            continue
                        if any(str(k).startswith(f"{req}.") for k in (node.get("inputs") or {})):
                            continue
                    missing_required.append(f"{nid}({ct}).{req}")
        report = {
            "node_count": len(api_prompt),
            "unknown_types": unknown_types,
            "missing_required": missing_required,
            "dangling_links": dangling,
            "unexpected_inputs": unexpected,
        }
        if verbose or unknown_types or missing_required or dangling:
            logger.info(f"转换自检: {json.dumps(report, ensure_ascii=False)[:600]}")
        return report

    # ===================== 资源与队列 =====================

    def upload_image(self, image_path: str, name: str = None,
                     subfolder: str = "", image_type: str = "input") -> str:
        """上传图片到 ComfyUI（input / output / temp 目录）

        ⚠️ ComfyUI 的 /upload/image 只接受**裸文件名**：
        `name` 里带 "/" 而 subfolder 为空时，服务端会拿这个相对路径去 join 一个
        不存在的目录 → 直接 **500 "Server got itself in trouble"**（实测复现）。
        而调用方习惯把「项目/类型/资产」目录一起塞进 name（见 generate_multiview
        的 filename_prefix），于是「资产基础图上传」100% 500，多视角链路整条断掉。
        这里统一做归一化：name 里的目录部分挪到 subfolder 参数，name 只留文件名。
        返回格式不变（"子目录/文件名"），下游 LoadImageOutput 的 [output] 标注照旧。
        """
        url = f"{self.base_url}/upload/image"
        if name is None:
            name = os.path.basename(image_path)
        # 归一化：反斜杠统一；按 "/" 切段（丢弃空段，兼容首/尾/重复斜杠）；
        # 末段是文件名，前面所有段是目录 → 目录进 subfolder，name 只留裸文件名。
        # 末尾带 "/" 视为「只给了目录」→ 文件名回落本地图片的 basename
        # （否则 "d/" 会被当成名为 "d" 的无扩展名文件静默传上去）。
        raw = str(name).replace("\\", "/")
        parts = [p for p in raw.split("/") if p]
        if parts and raw.endswith("/"):
            head, tail = "/".join(parts), os.path.basename(image_path)
        elif len(parts) > 1:
            head, tail = "/".join(parts[:-1]), parts[-1]
        elif parts:
            head, tail = "", parts[0]
        else:
            head, tail = "", os.path.basename(image_path)
        name = tail
        if head:
            subfolder = "/".join(p for p in (str(subfolder).strip("/"), head) if p)
        with open(image_path, "rb") as f:
            files = {"image": (name, f, "image/png")}
            data = {"overwrite": "true", "type": image_type}
            if subfolder:
                data["subfolder"] = subfolder
            resp = requests.post(url, files=files, data=data, timeout=120)
            resp.raise_for_status()
            result = resp.json()
            uploaded = result.get("name", name)
            sub = result.get("subfolder") or ""
            return f"{sub}/{uploaded}" if sub else uploaded

    def queue_prompt(self, api_prompt: dict) -> str:
        payload = {"prompt": api_prompt, "client_id": self.client_id}
        try:
            result = self._post("/prompt", payload)
        except Exception:
            _bump("prompt_failed")
            raise
        if "error" in result:
            _bump("prompt_failed")
            raise RuntimeError(f"ComfyUI 队列错误: {result['error']}")
        _bump("prompt_submitted")
        return result.get("prompt_id", "")

    def get_history(self, prompt_id: str) -> dict:
        return self._get(f"/history/{prompt_id}")

    def interrupt(self, prompt_id: str = None) -> None:
        """S9：向 ComfyUI 发 /interrupt，打断当前正在出队的任务。

        - `prompt_id` 为 None → 打断队列中**正在执行**的那个（ComfyUI 官方语义）；
        - 为具体 prompt_id → 仅当它仍在队列/执行中时才有效（配合 `delete_queued` 精准清理）。
        失败静默（网络抖了也不应让取消路径本身抛错拖垮上层）。
        """
        try:
            if prompt_id is None:
                self._post("/interrupt")
            else:
                self._post("/interrupt")
                self.delete_queued(prompt_id)
            logger.info(f"已请求 ComfyUI 打断远端任务: {prompt_id or '(当前出队)'}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI /interrupt 失败（不影响取消流程）: {e}")

    def delete_queued(self, prompt_id: str) -> None:
        """S9：把指定 prompt 从队列中删除（ComfyUI `POST /queue {"delete":[id]}`）。"""
        try:
            self._post("/queue", {"delete": [prompt_id]})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI 删除队列项失败（不影响取消流程）: {prompt_id}: {e}")

    def wait_for_completion(self, prompt_id: str, timeout: int = 1800) -> dict:
        """轮询远端任务直到完成。

        S9 增强（不改变返回契约——超时仍返回 `{}`，避免 ripple 到 6 处调用方）：
          ① 轮询内检查 `cancellation.should_stop()`（contextvar，无注册时恒 False，
             不影响普通 API 调用路径）；一旦收到「暂停/停止」→ 立即 `interrupt` +
             抛 `cancellation.Cancelled`（穿透到 pipeline 归一为 cancelled），不再白烧 GPU；
          ② 轮询用 `cancellation.sleep`（可被打断的短休眠），点了暂停最多 0.25s 就有反应，
             而不是等满 3s；
          ③ 超时（非中止）后也 `interrupt` 一次，避免「本地判超时、远端继续跑」的双重浪费。
        ⚠️ 注意（审计 S9 备注）：cancellation 检查点**刻意不放进 ComfyUI 渲染循环**
        （会留半成品）——这里加的是「超时/取消后的远端清理」，两者不冲突。
        B-21 P1-13：三态分离（completed / error / timeout）+ interrupt 定向到指定 prompt_id。
        超时和 error 的 interrupt 都传 prompt_id（不再打断队列中正在执行的其他任务）。
        """
        start = time.time()
        while time.time() - start < timeout:
            # ① 中止信号：点「暂停」后立刻打断远端并抛出，让上层转 cancelled 而非干等
            if cancellation.should_stop():
                logger.warning(f"等待期间收到中止信号，打断远端任务 {prompt_id}")
                self.interrupt(prompt_id)
                raise cancellation.Cancelled(f"ComfyUI 远端等待期间收到中止信号：{prompt_id}")
            try:
                history = self.get_history(prompt_id)
                if prompt_id in history:
                    entry = history[prompt_id]
                    status = entry.get("status", {}) or {}
                    if status.get("completed") or status.get("status_str") == "success":
                        _bump("completed")
                        _bump("waited_seconds", round(time.time() - start, 2))
                        return entry
                    if status.get("status_str") == "error":
                        logger.error(f"生成出错: {status}")
                        _bump("waited_seconds", round(time.time() - start, 2))
                        # B-21 P1-13：error 态也定向 interrupt（清理本 prompt 的残留队列项）
                        self.interrupt(prompt_id)
                        return entry
            except cancellation.Cancelled:
                raise  # 中止信号必须穿透，不能被轮询的通用 except 吞掉
            except Exception as e:
                logger.debug(f"轮询历史失败: {e}")
            # ② 可被打断的短休眠（3s 轮询间隔），暂停时最多 0.25s 即有反应。
            # ⚠️ 关键：`cancellation.sleep` 在收到中止信号时**直接抛 Cancelled**，
            # 会绕过循环顶部的 `should_stop()` 分支 —— 也就是说「点暂停」时
            # **不会执行 self.interrupt()，ComfyUI 上的任务会继续白跑**
            # （用户反馈「暂停要同步停止 comfyui 的任务」的直接原因）。
            # 因此这里自己捕获并补上远端中断，再原样抛出。
            try:
                cancellation.sleep(3)
            except cancellation.Cancelled:
                logger.warning(f"退避期间收到中止信号，主动打断远端任务 {prompt_id}")
                self.interrupt(prompt_id)
                raise
        # ③ 超时（非中止）：仍清理远端，避免本地判超时而远端白跑
        # B-21 P1-13：超时 interrupt 定向到指定 prompt_id（不再误伤队列中其他任务）
        logger.warning(f"等待超时: {prompt_id}（清理远端队列）")
        self.interrupt(prompt_id)
        _bump("waited_seconds", round(time.time() - start, 2))
        return {}

    def get_output_files(self, history: dict, file_ext: str = "") -> List[str]:
        files = []
        for _node_id, node_output in (history.get("outputs") or {}).items():
            for _key, value in (node_output or {}).items():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, dict) and "filename" in item:
                        filename = item["filename"]
                        subfolder = item.get("subfolder", "")
                        base = os.path.join(COMFYUI_OUTPUT_DIR, subfolder) if subfolder else COMFYUI_OUTPUT_DIR
                        full = os.path.join(base, filename)
                        if not file_ext or filename.endswith(file_ext):
                            files.append(full)
        return files

    # ---------- 路径工具（P0-4：HTTP 资源路径 → 本地绝对路径） ----------

    @staticmethod
    def resolve_local_path(path_or_url: str) -> Optional[str]:
        """把前端传来的 HTTP 资源路径（/api/assets/...、/api/videos/...）解析为本地绝对路径"""
        if not path_or_url or not isinstance(path_or_url, str):
            return None
        p = path_or_url.strip()
        if p.startswith("http://") or p.startswith("https://"):
            # 去掉协议与主机，仅保留路径部分
            rest = p.split("//", 1)[-1]
            p = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        p = p.replace("\\", "/")
        if p.startswith("/api/assets/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "assets", p[len("/api/assets/"):]))
        if p.startswith("/api/videos/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "videos", p[len("/api/videos/"):]))
        if p.startswith("/api/final/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "final", p[len("/api/final/"):]))
        if p.startswith("/api/storyboards/file/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "storyboards",
                                                 p[len("/api/storyboards/file/"):]))
        if os.path.exists(p):
            return os.path.normpath(p)
        return None

    # ---------- 目录标注工具（P0-5：按节点类型区分 [output] / 无标注） ----------

    @staticmethod
    def annotate_file_ref(filename: str, class_type: str, default_dir: str = ANNOTATED_DIR) -> str:
        """按「读取目录」语义为文件名型资源输入补/去目录标注。

        - Load*Output 系列（LoadImageOutput / LoadAudioOutput / ...）读 ComfyUI output 目录
          → 返回 "名字 [output]"
        - LoadImage / LoadAudio / LoadVideo 等读 input 目录 → 返回 "名字"（不带标注）
        - 其它类型 → 原样返回，不做任何加工（避免引入新错误）
        """
        if not filename or not isinstance(filename, str):
            return filename
        name = SUFFIX_RE.sub("", filename).strip()   # 先剥离可能已存在的标注，保证幂等
        if class_type in OUTPUT_LOAD_CLASSES or \
                (class_type.startswith("Load") and class_type.endswith("Output")):
            return f"{name} [{default_dir}]"
        if class_type in INPUT_LOAD_CLASSES or class_type.startswith("Load"):
            return name
        return filename

    # ===================== 第一阶段：基础图生成 =====================

    # ---------- P0 修复辅助：场景去人 / 冲突负向词清理 ----------

    @classmethod
    def clean_conflict_negative_tokens(cls, api_prompt: dict) -> dict:
        """从负向提示词槽位剔除与正向 3D 风格冲突的词（P0：风格冲突）

        只处理**极性为负向**的槽位，不改正向提示词。极性经 `_prompt_slot_polarity`
        判定（正负同体节点按字段名，老模板走内容启发式），因此 QwenImage2.1 的
        ``TextEncodeQwenImage21.negative_prompt`` 也能被清理（此前字段名不在白名单 → 整段跳过）。
        返回 {节点id.字段: 变更说明} 便于审计。
        """
        changed: dict = {}
        for nid, field, value, polarity in _iter_prompt_slots(api_prompt):
            if polarity != "neg" or not (value or "").strip():
                continue
            new = value
            removed: List[str] = []
            for tok in CONFLICT_NEGATIVE_SORTED:
                if tok in new:
                    new = new.replace(tok, "")
                    removed.append(tok)
            if not removed:
                continue
            new = re.sub(r"[，,、]\s*(?=[，,、])", "", new)
            new = re.sub(r"^\s*[，,、]+\s*", "", new)
            new = re.sub(r"[，,、\s]+$", "", new)
            (api_prompt[nid]["inputs"])[field] = new
            changed[f"{nid}.{field}"] = f"移除冲突负向词 {removed}"
        if changed:
            logger.info(f"[风格冲突清理] 负向提示词已修正 {len(changed)} 处: {changed}")
        return changed

    @classmethod
    def sanitize_scene_prompt(cls, prompt_zh: str) -> str:
        """场景提示词去人物（P0：场景资产带人）

        场景定义常写「村民三两结伴」→ 资产图实测渲染出 4 人。这里按分句粒度丢弃含人物
        描述的短句，并追加空场景声明；若清洗后为空则回退原文（避免把场景描述清空）。
        """
        text = (prompt_zh or "").strip()
        if not text:
            return text
        parts = [p.strip() for p in _PROMPT_SPLIT_RE.split(text)]
        kept: List[str] = []
        dropped: List[str] = []
        for p in parts:
            if not p:
                continue
            hit = any(w in p for w in CHARACTER_WORDS) or bool(CHARACTER_QTY_RE.search(p))
            if hit:
                dropped.append(p)
            else:
                kept.append(p)
        cleaned = "，".join(kept).strip()
        if not cleaned:
            cleaned = text      # 全句含人物时不至于清空，仅靠正向声明压制
        if dropped:
            logger.info(f"[场景去人] 丢弃人物描述句 {dropped}；清洗后场景描述: {cleaned[:80]}")
        return cleaned.rstrip("。;； ") + SCENE_NO_CHARACTER_SUFFIX

    def _find_positive_text_node(self, api_prompt: dict,
                                 class_types=PROMPT_NODE_TYPES) -> Optional[str]:
        """定位**承载正向文本的节点**（返回节点 id）。

        极性来源唯一：`_prompt_slot_polarity`。正负同体节点（TextEncodeQwenImage21）
        永远有正向字段，因此必然命中；老模板靠内容启发式区分正/负节点。
        """
        pos = [nid for nid, _f, _v, pol in _iter_prompt_slots(api_prompt, class_types)
               if pol == "pos"]
        if pos:
            return sorted(pos, key=_node_sort_key)[-1]
        # 退化：模板里只有负向槽位（极端情况），取 id 最大的提示词节点，避免直接失败
        pool = [nid for nid, n in (api_prompt or {}).items()
                if isinstance(n, dict) and n.get("class_type") in class_types]
        return sorted(pool, key=_node_sort_key)[-1] if pool else None

    def _positive_field(self, api_prompt: dict, node_id: str) -> Optional[str]:
        """返回该正向节点真正承载正向文本的**字段名**。

        必须定位字段而不是「把所有字符串输入都写成正向词」：
        ``TextEncodeQwenImage21`` 同节点里还有 ``negative_prompt``，后者被覆盖会
        让负向词整段失效（且不报错）。
        """
        node = (api_prompt or {}).get(node_id) or {}
        ctype = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        for field in PROMPT_TEXT_FIELDS:
            if _prompt_slot_polarity(ctype, field, inputs.get(field)) == "pos":
                return field
        return None

    def _find_negative_slot(self, api_prompt: dict,
                            class_types=PROMPT_NODE_TYPES) -> Optional[Tuple[str, str]]:
        """定位负向槽位，返回 ``(节点id, 字段名)``；找不到返回 None。

        返回字段名是必须的：正负同体节点的负向文本在 ``negative_prompt`` 上，
        老模板在 ``text`` / ``prompt`` 上——只回节点 id 会让调用方写错字段。
        """
        neg = [(nid, f) for nid, f, _v, pol in _iter_prompt_slots(api_prompt, class_types)
               if pol == "neg"]
        if not neg:
            return None
        return sorted(neg, key=lambda t: _node_sort_key(t[0]))[-1]

    def _find_negative_text_node(self, api_prompt: dict,
                                 class_types=PROMPT_NODE_TYPES) -> Optional[str]:
        """定位负向节点 id（`_find_negative_slot` 的兼容包装，仅回节点 id）"""
        hit = self._find_negative_slot(api_prompt, class_types)
        return hit[0] if hit else None

    def _generate_base_image(self, workflow_file: str, prompt_zh: str,
                             asset_type: str = None, seed: int = None,
                             style: str = "", size=None,
                             filename_prefix: str = None) -> List[str]:
        """通用基础图生成：更新正向提示词节点

        P0 修复（同轮补充）：
        - asset_type="scene" → 清洗人物描述并追加"空场景"声明（场景资产不得带人）；
        - clean_conflict_negative_tokens 剔除与 3D 正向风格冲突的负向词；
        - seed 用于质检不达标时的重生成（保证与上一版结果不同）。

        风格落地（2026-09-18 修复）：
        - style 非空 → 把风格后缀拼进正向提示词（此前完全没有这一步，用户敲定的风格
          一个资产都没落到提示词里）；
        - size 非空 → 覆写尺寸节点，让「竖屏 9:16」真正体现在画布上（此前尺寸来自
          模板硬编码的 1664×928 横向）。

        G8（资源清理）：filename_prefix 非空 → 覆写 SaveImageAdvanced/SaveImage 的
        filename_prefix，让资产基础图落在 `comic_drama/<项目>_asset_<类型>` 这种项目专属
        子目录，而非全部堆在 ComfyUI output 默认目录（此前删项目/滚动清理都够不着）。
        """
        api_prompt, meta = self.load_workflow(workflow_file, return_meta=True)
        node_id = self._find_positive_text_node(api_prompt)
        if node_id is None:
            logger.error(f"{workflow_file} 中未找到正向提示词节点，转换元信息: {meta}")
            return []
        # 风格注入：必须在场景去人之前拼好，保证风格词不被 sanitize 丢掉
        prompt_zh = style_kit.with_style(prompt_zh, style) if style else prompt_zh
        # 场景资产去人
        if asset_type == "scene":
            prompt_zh = self.sanitize_scene_prompt(prompt_zh)
        # 只写「承载正向文本的那一个字段」：
        #   老模板 CLIPTextEncode → text ；TextEncodeQwenImageEditPlus → prompt ；
        #   QwenImage2.1 TextEncodeQwenImage21 → prompt（同节点另有 negative_prompt）。
        # ⚠️ 旧实现是「非 text 字段就把 node 里所有字符串输入都写成正向提示词」——
        #    遇到正负同体的 TextEncodeQwenImage21 会把 negative_prompt 覆盖掉，负向词静默失效。
        pos_field = self._positive_field(api_prompt, node_id)
        if pos_field is None:
            logger.error(f"{workflow_file} 正向节点 {node_id} 未找到可写入的正向字段，"
                         f"输入字段={sorted((api_prompt[node_id].get('inputs') or {}).keys())}")
            return []
        api_prompt[node_id]["inputs"][pos_field] = prompt_zh
        logger.info(f"[{workflow_file}] 正向提示词节点 {node_id}.{pos_field} 已更新"
                    f"（共 {len(api_prompt)} 节点）")
        self.clean_conflict_negative_tokens(api_prompt)                 # P0：清理风格冲突负向词
        # 负向词：按风格再压一批「画风打架」的词（如国漫风不该出现写实照片）
        if style:
            self._append_style_negative(api_prompt, style)
        # 画幅落地：尺寸节点覆写
        if size:
            hit = style_kit.apply_latent_size(api_prompt, size)
            if hit:
                logger.info("[%s] 画幅已按风格覆写为 %s×%s：%s",
                            workflow_file, size[0], size[1], ",".join(hit))
            else:
                logger.warning("[%s] 未找到尺寸节点，画幅 %s×%s 未能落地（沿用模板尺寸）",
                               workflow_file, size[0], size[1])
        if seed is not None:
            logger.info(f"资产采样种子已注入: {self._inject_seed(api_prompt, seed)}")

        # G8：覆写输出文件名前缀（项目专属子目录），避免资产基础图堆在 ComfyUI output 默认目录
        if filename_prefix:
            for _nid, _n in api_prompt.items():
                if _n.get("class_type") in ("SaveImageAdvanced", "SaveImage") \
                        and "filename_prefix" in _n["inputs"]:
                    _n["inputs"]["filename_prefix"] = filename_prefix

        prompt_id = self.queue_prompt(api_prompt)
        history = self.wait_for_completion(prompt_id)
        return self.get_output_files(history, ".png")

    def _append_style_negative(self, api_prompt: dict, style: str) -> None:
        """把「与目标风格冲突」的词追加到负向提示词**槽位**（找不到负向槽位则跳过）

        写入字段由 `_find_negative_slot` 给出：正负同体节点是 ``negative_prompt``，
        老模板是 ``text`` / ``prompt``——按字段写，不能靠猜。
        """
        negs = style_kit.negative_for_style(style)
        if not negs:
            return
        try:
            hit = self._find_negative_slot(api_prompt)
        except Exception:  # noqa: BLE001
            hit = None
        if not hit:
            return
        node_id, field = hit
        node = api_prompt[node_id]
        cur = node.get("inputs", {}).get(field)
        if not isinstance(cur, str):
            return
        node["inputs"][field] = (cur.rstrip("，,。") + "，" + "，".join(negs)) if cur.strip() else "，".join(negs)

    def generate_character_base(self, prompt_zh: str, seed: int = None,
                                style: str = "", size=None,
                                filename_prefix: str = None) -> List[str]:
        logger.info(f"生成角色基础图: {prompt_zh[:50]}...")
        # 角色基础图 = 设定集（三视图横排）。必须确定性注入「全身 + 横排三视图」版式硬约束：
        # 剧本层提示词只写外貌特征、不含构图约束（风格词也由程序追加，版式同属构图维度），
        # 不注入则模型默认半身/胸像构图且三格版式不可控，多视角与分镜一致性都会崩坏
        # （2026-09-19 实测：三视图出成半身，且同图内人物身高比例不一致）。
        prompt_zh = self._ensure_fullbody_prompt(prompt_zh, style)
        return self._generate_base_image(WORKFLOW_TEMPLATE["character_gen"], prompt_zh,
                                         asset_type="character", seed=seed, style=style, size=size,
                                         filename_prefix=filename_prefix)

    @staticmethod
    def _ensure_fullbody_prompt(prompt_zh: str, style: str = "") -> str:
        """给角色参考图提示词确定性地补「全身三视图」版式约束（幂等）

        2026-09-23 补强：除「三人同比例」外，再显式要求**间距均匀互不遮挡**、
        **脚底落在同一条水平线**、**纯白背景**。前两项直接对应实测里最容易
        跑偏的两个量（三人横向粘连 / 脚底不共线），后一项避免把设定图渲染成
        「三人合影」的写实场景（带透视景深 → 三人远近大小不一）。
        2026-09-23（白底需求）：角色/物品参考图**不需要背景**，从「简洁纯色背景」
        收紧为明确的「纯白背景」，避免模型自由发挥出渐变/场景/贴图。
        画幅已同步改为 1:1（见 style_kit.ASSET_BASE_RATIO 的角色项），
        「三人横排」版式与「竖幅画幅」的冲突已解除。
        """
        text = str(prompt_zh or "").strip()
        # 幂等判断必须在 with_style 之前做（同 style_kit._style_suffix 的坑）
        marker = "全身三视图"
        base = text if marker in text else style_kit.with_style(text, style) if style else text
        if marker in base:
            return base
        suffix = ("，全身三视图设定图：正面、左侧面、背面三张全身视图从左到右横排、"
                  "间距均匀互不遮挡，同一角色同一比例，人物身高占比一致，"
                  "三人脚底落在同一条水平线上，"
                  "画面完整呈现从头到脚的全身，头顶上方与脚部下方留少量边距，"
                  "纯白背景，不要场景、道具、投影与任何背景纹理")
        # 剧本层提示词常以「三视图。」收尾，直接拼会得到「三视图。，全身三视图…」的脏标点
        base = base.rstrip("。，,.;； ")
        return (base + suffix) if base else suffix.lstrip("，")

    @staticmethod
    def _ensure_item_white_bg(prompt_zh: str, style: str = "") -> str:
        """给物品参考图提示词确定性地补「纯白背景」约束（幂等）。

        物品/道具参考图与角色设定图同理：后续要拿来做参考图编辑（多视角/分镜），
        背景越干净越利于一致性；带场景/贴图的物品图会把背景一起带进分镜。
        """
        text = str(prompt_zh or "").strip()
        marker = "纯白背景"
        base = text if marker in text else style_kit.with_style(text, style) if style else text
        if marker in base:
            return base
        suffix = ("，纯白背景，无任何场景、地面、桌面、阴影与背景纹理，"
                  "物品完整居中、边缘清晰")
        base = base.rstrip("。，,.;； ")
        return (base + suffix) if base else suffix.lstrip("，")

    def generate_item_base(self, prompt_zh: str, seed: int = None,
                           style: str = "", size=None,
                           filename_prefix: str = None) -> List[str]:
        logger.info(f"生成物品基础图: {prompt_zh[:50]}...")
        prompt_zh = self._ensure_item_white_bg(prompt_zh, style)
        return self._generate_base_image(WORKFLOW_TEMPLATE["item_gen"], prompt_zh,
                                         asset_type="item", seed=seed, style=style, size=size,
                                         filename_prefix=filename_prefix)

    def generate_scene_base(self, prompt_zh: str, seed: int = None,
                            style: str = "", size=None,
                            filename_prefix: str = None) -> List[str]:
        logger.info(f"生成场景基础图: {prompt_zh[:50]}...")
        return self._generate_base_image(WORKFLOW_TEMPLATE["scene_gen"], prompt_zh,
                                         asset_type="scene", seed=seed, style=style, size=size,
                                         filename_prefix=filename_prefix)

    # ===================== 第二阶段：多视角生成 =====================

    def generate_multiview(self, base_image_path: str, asset_type: str,
                           asset_name: str, base_prompt_zh: str,
                           seed: int = None, style: str = "", size=None,
                           filename_prefix: str = None) -> Dict[str, str]:
        """基于基础图生成多视角图（Qwen Edit 2511）

        character → 4 视图（正/左/右/背）；item / scene → 4 视角（正/左45/右45/俯视）

        风格落地（2026-09-18 修复）：原 docstring 声称"统一补竖屏 9:16 画幅声明"，
        但代码里并没有这一步 —— 风格与画幅都是模板自带的，用户的设定到不了多视角图。
        现在 style / size 由调用方传入并真正生效。
        seed 供 app 层质检不达标时重生成。

        G8：filename_prefix 非空 → 多视角产物落项目专属子目录（每视角再加 key 后缀
        避免同前缀互相覆盖）。
        """
        views = MULTIVIEW_CONFIG["character_views"] if asset_type == "character" \
            else MULTIVIEW_CONFIG["item_scene_views"]

        # B-13 P1-14：基础图上传到 ComfyUI output 根目录时文件名带项目名，避免跨项目同名资产互相覆盖
        base_name = f"comic_drama_{asset_type}_{asset_name}_base.png"
        if filename_prefix:
            # filename_prefix 形如 comic_drama/<项目>_asset_<类型>_epNN，
            # 基础图沿用同一前缀子目录，与多视角产物同目录，不产生孤儿
            base_name = f"{filename_prefix.rstrip('/')}/{base_name}"
        try:
            output_name = self.upload_image(base_image_path, base_name, image_type="output")
        except Exception as e:
            # 不做「回退到 input 目录」：LoadImageOutput 只认 output 目录，回退必然 400，
            # 与其提交注定失败的 prompt，不如快速失败并给出明确原因（P0-5）
            raise RuntimeError(
                f"基础图上传到 ComfyUI output 目录失败，LoadImageOutput 无法引用: {e}"
            ) from e
        logger.info(f"基础图已上传到 ComfyUI output 目录: {output_name}")

        # 风格后缀：拼在 base_desc 之后，保证每个视角都带风格
        styled_desc = style_kit.with_style(base_prompt_zh, style, with_tail=False) if style \
            else base_prompt_zh
        # P0：场景多视角同样必须去人（基础图与多视角一致，避免视角转换时"带出"人物）
        if asset_type == "scene":
            styled_desc = self.sanitize_scene_prompt(styled_desc)

        results = {}
        for view in views:
            logger.info(f"生成 {asset_name} {view['label']}...")
            view_prompt = self._build_multiview_prompt(asset_type, styled_desc, view)
            if asset_type == "scene" and SCENE_NO_CHARACTER_SUFFIX not in view_prompt:
                view_prompt = view_prompt.rstrip("。;； ") + SCENE_NO_CHARACTER_SUFFIX
            # G8：每视角在公共前缀后加 key，避免同前缀下不同视角产物互相覆盖
            view_prefix = f"{filename_prefix}_{view['key']}" if filename_prefix else None
            img_path = self._run_multiview_workflow(output_name, view_prompt, seed=seed,
                                                     size=size, filename_prefix=view_prefix)
            if img_path:
                results[view["key"]] = img_path
            else:
                logger.warning(f"{asset_name} {view['label']} 生成失败")
        return results

    def _build_multiview_prompt(self, asset_type: str, base_desc: str, view: dict) -> str:
        """构造多视角编辑提示词：**机位前置**，中英同口径，一致性要求降为从属。

        ⚠️ 关于「多视角图机位不变」（2026-09-23 实测，勿重复踩坑）：
            本函数**改不动模型输出的机位**。参考图编辑只复刻参考图里已可见的角度，
            不会凭空补全没见过的面 —— 用正面基础图 + 明确「背面/俯视」指令，
            输出仍是正面。已用 8 组对照实验排除措辞因素（换语言/语序/命令式 vs 编辑式、
            断开 vae、覆写 cfg、跨对象对照），结论与机制见 config.MULTIVIEW_CONFIG 注释。
            真要拿到多视角，得在**基础图**阶段带入角度。
            本函数的作用是把机位说清楚、不再自相矛盾、不让「保持一致」压过机位要求，
            对更强的模型保留正确口径。

        角色多视图每张都要求「全身」构图（2026-09-19 实测：基础图出成半身/胸像，
        视角图继承半身构图导致"三视图不是全身"。distance=full-body shot 不够，
        必须显式写"从头到脚完整入画"，并排除半身/胸像/大头）。
        """
        # 中文机位：优先用配置里的 zh；缺省回落到 label + 英文方位（旧调用方可继续工作）
        angle_zh = (view.get("zh") or "").strip() or \
            f"本图是{view['label']}，相机方位：{view['azimuth']}，{view['elevation']}"
        camera_terms = f"{view['azimuth']}, {view['elevation']}, {view['distance']}"
        if asset_type == "character":
            return (
                f"【机位】{angle_zh}。"
                f"画面完整呈现人物从头到脚，头顶与脚部不留裁切，"
                f"不要半身像、不要胸像、不要大头特写、不要截断脚部。"
                f"【一致性】只改变观察角度：脸型、发型、服装、配饰与参考图保持一致，"
                f"人物身高比例不变。{camera_terms}。{base_desc}"
            )
        asset_word = "物品" if asset_type == "item" else "场景"
        return (
            f"【机位】{angle_zh}。"
            f"相机绕{asset_word}变换位置后重新取景，{asset_word}在画面中的朝向随视角改变。"
            f"【一致性】{asset_word}的形状、材质、颜色、花纹与参考图一致。"
            f"{camera_terms}。{base_desc}"
        )

    def _run_multiview_workflow(self, uploaded_image_name: str, prompt_zh: str,
                                image_dir: str = ANNOTATED_DIR,
                                seed: int = None, size=None,
                                filename_prefix: str = None) -> Optional[str]:
        """运行多视角编辑工作流（分镜生成_Qwen21.json：QwenImage2.1 参考图编辑）"""
        wf_name = WORKFLOW_TEMPLATE["multiview_gen"]
        api_prompt, meta = self.load_workflow(wf_name, return_meta=True)
        if seed is not None:
            logger.info(f"多视角采样种子已注入: {self._inject_seed(api_prompt, seed)}")

        # 1) 正向提示词：只写正向字段（正负同体节点是 prompt，老模板是 prompt/text）
        node_id = self._find_positive_text_node(api_prompt)
        pos_field = self._positive_field(api_prompt, node_id) if node_id else None
        if node_id is not None and pos_field:
            api_prompt[node_id]["inputs"][pos_field] = prompt_zh
        else:
            raise RuntimeError(f"{wf_name} 未定位到正向提示词字段（节点={node_id}）")
        self.clean_conflict_negative_tokens(api_prompt)   # P0：清理风格冲突负向词
        # 画幅落地（与基础图一致，否则多视角会把竖屏 base 图改成模板的横屏尺寸）
        if size:
            hit = style_kit.apply_latent_size(api_prompt, size)
            if hit:
                logger.info("多视角画幅已覆写为 %s×%s：%s", size[0], size[1], ",".join(hit))
            else:
                logger.info("多视角工作流无尺寸节点，画幅继承参考图（基础图 %s×%s）",
                            size[0], size[1])

        # 2) 参考图：3 个 LoadImageOutput 全部替换（P0-3），且按节点类型补目录标注（P0-5）
        #    LoadImageOutput 读 output 目录 → 必须写 "名字 [output]"
        #    LoadImage（若模板中出现）读 input 目录 → 必须写裸文件名，不能带标注
        ref_nodes = [(nid, n.get("class_type")) for nid, n in api_prompt.items()
                     if n.get("class_type") in ("LoadImageOutput", "LoadImage")]
        ref_values = []
        for nid, ctype in ref_nodes:
            value = self.annotate_file_ref(uploaded_image_name, ctype, default_dir=image_dir)
            api_prompt[nid]["inputs"]["image"] = value
            ref_values.append(f"{nid}({ctype})={value}")
        if not ref_nodes:
            logger.warning(f"{wf_name} 中未找到参考图节点（LoadImageOutput/LoadImage）")
        else:
            logger.info(f"多视角参考图已替换 {len(ref_nodes)} 个节点: {ref_values}")

        # G8：覆写输出文件名前缀（项目专属子目录），避免多视角图堆在 ComfyUI output 默认目录
        if filename_prefix:
            for _nid, _n in api_prompt.items():
                if _n.get("class_type") in ("SaveImageAdvanced", "SaveImage") \
                        and "filename_prefix" in _n["inputs"]:
                    _n["inputs"]["filename_prefix"] = filename_prefix

        prompt_id = self.queue_prompt(api_prompt)
        history = self.wait_for_completion(prompt_id, timeout=900)
        files = self.get_output_files(history, ".png")
        return files[0] if files else None

    # ===================== 第三阶段：分镜图生成（Qwen Edit 2511） =====================

    @staticmethod
    def _find_image_slots(api_prompt: dict, prompt_node_id: str) -> List[Tuple[str, Optional[str]]]:
        """解析正向编辑节点的参考图槽位 → 真正持有文件名的 LoadImage* 节点 id

        两种模板两种键名，**必须都认**：
        - 老模板（Qwen-Edit 2511 / TextEncodeQwenImageEditPlus）：``image1/image2/image3``；
        - QwenImage2.1（TextEncodeQwenImage21）：autogrow 点号键 ``images.image_1`` …
          （服务端 `_expand_schema_for_dynamic` 用 finalize_prefix 生成）。
        只认前者会让参考图**静默不注入**——分镜失去角色/场景一致性，且不报错。

        链路可能是「LoadImageOutput → imageN」直连，也可能中间隔一层
        （老的 ``FluxKontextImageScale`` / 新的 ``ImageScaleToTotalPixels``），
        因此向上游最多回溯两层找 LoadImage*。
        """
        result: List[Tuple[str, Optional[str]]] = []
        node = api_prompt.get(prompt_node_id) or {}
        keys = sorted((k for k in (node.get("inputs") or {}) if _IMAGE_SLOT_RE.match(str(k))),
                      key=lambda k: _slot_index(k))
        for key in keys:
            value = node["inputs"][key]
            result.append((key, ComfyUIClient._trace_load_image(api_prompt, value, depth=2)))
        return result

    @staticmethod
    def _trace_load_image(api_prompt: dict, value, depth: int) -> Optional[str]:
        """沿连线向上游回溯，找到持有文件名的 LoadImage* 节点 id（找不到返回 None）"""
        if depth <= 0 or not (isinstance(value, list) and len(value) == 2):
            return None
        src_id = str(value[0])
        src = api_prompt.get(src_id) or {}
        if str(src.get("class_type", "")).startswith("LoadImage"):
            return src_id
        for v in (src.get("inputs") or {}).values():
            found = ComfyUIClient._trace_load_image(api_prompt, v, depth - 1)
            if found:
                return found
        return None

    # 画面光源/时间要素 → 分镜图光影引导（从镜头描述/风格里抽取）
    # 历史缺陷：描述里的「黄昏/阴雨/烛光/月光」等光影要素没有独立成句，模型容易忽略，
    # 导致分镜图与视频在光源上不一致（视频有日落、分镜图却是正午平光）。
    _LIGHT_KEYWORDS: Sequence[Tuple[str, str]] = (
        ("黄昏", "黄昏暖调逆光，长影拉长，天空带橙色到紫色的渐变"),
        ("傍晚", "傍晚蓝调过渡光，暖色点光源，整体氛围静谧"),
        ("清晨", "清晨低角度柔光，冷色空气透视，露珠微光"),
        ("黎明", "黎明前冷蓝色调，地平线微光，薄雾弥漫"),
        ("正午", "正午顶光，高对比度，阴影短而清晰"),
        ("午夜", "午夜深蓝冷调，月光为主光源，高反差明暗"),
        ("夜晚", "夜晚冷蓝月光，点光源（灯笼/烛火/灯光）与大面积暗部对比"),
        ("深夜", "深夜冷色调，微弱月光，暗部深沉"),
        ("雨夜", "雨夜冷蓝调，湿润反光，光源被雨幕柔化"),
        ("雨天", "阴雨天漫射光，低对比度，天空阴沉，地面反光"),
        ("阴天", "阴天漫射柔光，低对比度，色调偏冷"),
        ("雪天", "雪天高亮漫射光，蓝白冷调，雪地反光强烈"),
        ("雪夜", "雪夜冷蓝调，雪面反光与暗部高反差"),
        ("雾天", "雾天漫射光，空间透视感强，远景模糊"),
        ("云雾", "云雾缭绕的漫射光，空气透视，光柱穿透"),
        ("烛光", "烛火暖光为主光源，暖黄光晕，暗部偏冷形成对比"),
        ("烛火", "烛火暖光为主光源，暖黄光晕，暗部偏冷形成对比"),
        ("火光", "火光照明的暖橙色调，明暗对比强烈，火苗跳动"),
        ("灯笼", "灯笼暖光点缀，暖红与冷夜蓝形成色彩对比"),
        ("月光", "清冷月光为主光源，银蓝色调，轮廓光清晰"),
        ("油灯", "油灯暖光，低照度，柔和暖黄与深褐暗部"),
        ("晨光", "晨光低角度暖调，长影与空气透视"),
        ("夕照", "夕照暖红金调，逆光剪影，天空燃烧感"),
        ("窗光", "窗户透入的定向侧光，光斑与明暗分割线清晰"),
        ("闪电", "闪电冷白瞬间光，高反差，明暗交替"),
    )
    # 镜头描述里没出现光源词时，按情绪给一个中性光影基线（避免模型自由发挥）
    _LIGHT_EMOTION_FALLBACK: Sequence[Tuple[str, str]] = (
        ("阴郁", "冷调低饱和光，阴影浓重"),
        ("悲伤", "冷灰漫射光，低反差，情绪压抑"),
        ("恐惧", "冷蓝硬光，明暗撕裂，光源方向明确"),
        ("紧张", "高对比硬光，光源方向清晰"),
        ("温暖", "暖色柔光，低反差，光线柔和"),
        ("平静", "自然柔光，明暗过渡自然，光线均匀"),
    )

    @staticmethod
    def _merge_visual_detail(desc: str, detail: str) -> str:
        """把 visual_detail 并入画面主体，**已出现在 desc 里的分句不再重复追加**。

        不能简单 `f"{desc}。{detail}"`：提示词分析器写 storyboard_prompt_zh 时也会消费
        visual_detail（见 script_prompt_analyzer.build_shot_prompt），两条来源叠加会把同一句
        细节写两遍 —— 分镜图提示词里重复描述会放大该要素、干扰构图。
        """
        desc = str(desc or "").strip()
        detail = str(detail or "").strip()
        if not detail:
            return desc
        if not desc:
            return detail
        if detail in desc:
            return desc
        segs = [s.strip() for s in detail.replace("。", "，").split("，")]
        add = [s for s in segs if s and s not in desc]
        if not add:
            return desc
        # desc 已以句末标点收尾时不再补「。」，否则会出现「。。」
        sep = "" if desc.endswith(("。", "！", "？", "…")) else "。"
        return f"{desc}{sep}{'，'.join(add)}"

    @staticmethod
    def _light_hint_covered(hint: str, text: str) -> bool:
        """判断光影提示语是否已被文本覆盖（幂等，避免同一束光写两遍）。

        只比对**完整提示语是不是子串**是不够的：剧本阶段写进 visual_detail 的往往是
        「黄昏暖调逆光」这种**首段短语**，而不是整条「黄昏暖调逆光，长影拉长，天空带
        橙色到紫色的渐变」。此时整条比对不命中 → 又追加一次 → 分镜图提示词里同一束光
        出现两遍（实测 shot2：黄昏暖调逆光 出现 2 次）。
        因此这里同时比对首段（第一个「，」之前），任一命中即视为已覆盖。
        """
        if not hint or not text:
            return False
        if hint in text:
            return True
        head = hint.split("，", 1)[0].strip()
        return bool(head) and head in text

    @staticmethod
    def _extract_light_hint(shot: dict) -> str:
        """从镜头描述/情绪里抽取光影引导；无命中返回空串（不强行加光影）。

        幂等：若命中的光影提示语（或它的首段短语）已经出现在文本里，说明画面细节
        已承载光源，不再重复追加。
        """
        text = " ".join([
            str(shot.get("description") or ""),
            # storyboard_prompt_zh 会顶替 description 成为画面主体（见 build_storyboard_prompt），
            # 必须一起扫描：否则分析器已写明「黄昏逆光」时识别不到 → 再叠一条光影氛围句。
            str(shot.get("storyboard_prompt_zh") or ""),
            str(shot.get("visual_detail") or ""),
            str(shot.get("audio_cues") or ""),
            str(shot.get("emotion") or ""),
        ])
        if not text.strip():
            return ""
        for kw, hint in ComfyUIClient._LIGHT_KEYWORDS:
            if kw in text:
                return "" if ComfyUIClient._light_hint_covered(hint, text) else hint
        emotion = str(shot.get("emotion") or "").strip()
        for kw, hint in ComfyUIClient._LIGHT_EMOTION_FALLBACK:
            if kw in emotion:
                return "" if ComfyUIClient._light_hint_covered(hint, text) else hint
        return ""

    @staticmethod
    def build_storyboard_prompt(shot: dict, ref_labels: List[str] = None) -> str:
        """按镜头剧情描述构建分镜图（Qwen Edit 多参考图）中文提示词。

        ⚠️ **结构化分节规范（2026-09-24，目标「从源头减少重跑」）**：
        提示词按固定顺序输出为独立小节，每节用「【节名】」开头、句末用「。」收尾，
        使模型能逐节定位约束、不再把硬约束淹没在一大段扁平文字里。
        顺序（见 :ref:`docs/prompt-spec.md`，守卫 verify_storyboard_prompt_spec.py 锁定）：
          ① 【取景】景别/机位硬约束（最靠前，必须严格遵守）
          ② 【参考图】各参考图绑定什么（角色/物品/场景，逐条对应）
          ③ 【画面内容】动作与画面内容 + 说话状态 + 情绪氛围
          ④ 【光影】光影氛围
          ⑤ 【风格】画面风格声明
          ⑥ 【禁令】一致性 + 无文字/水印硬禁令
        历史教训：分镜图是质检重跑重灾区（教训库 68/73 条），其中「景别」占 45 条——
        扁平长提示词里景别约束被画面内容稀释。景别/机位是**取景级硬约束**，
        必须最靠前、独立成段、加粗强调，绝不能混在画面内容之后。
        """
        camera = str(shot.get("camera") or "中景").strip()
        cam_key = camera_key(camera)        # "" = 本镜没给景别（camera 只有机位/运镜）
        cam_angle = camera_angle(camera)    # 俯拍 / 仰拍 / 平视 / 环绕 …（与景别正交）
        cam_spec = camera_spec(camera)

        sections = []

        # ①【取景】景别/机位硬约束（最靠前）。**没给景别时绝不能编一个**硬塞进去。
        # 实测《蛊真人》ep02 shot_13 camera="俯拍缓推"（description 是脚部俯拍），
        # 旧实现猜成「中景：取景自腰部或膝部以上」→ 与画面描述的脚部俯拍**互斥**，
        # 模型在两条矛盾指令间摇摆，6 次重试出的全是「全景 + 平视」，而质检端又按
        # 中景判它「景别不符」→ 该镜永远过不了。改为把取景交还给画面描述。
        framing_lines = []
        if cam_key:
            framing_lines.append(f"景别（必须严格遵守）：{cam_key}——{cam_spec}。")
        else:
            framing_lines.append(
                f"取景（本镜未指定景别）：严格以【画面内容】里的取景描述为准"
                f"（camera 原值「{camera}」只给了机位/运镜，"
                f"不要擅自套用中景/全景等固定景别，也不要把它撑成全景）。"
            )
        if cam_angle:
            framing_lines.append(f"机位（必须严格遵守）：{_CAMERA_ANGLE_SPECS[cam_angle]}。")
        sections.append("【取景】" + "".join(framing_lines))

        # ②【参考图】参考图绑定：把每类参考图该锁什么写具体（角色/物品/场景分别对应）。
        # 历史教训：「角色」「背景」「一致」合计 27 条重跑，根因是参考图用途一句话带过，
        # 模型分不清「这张图该参考哪个角色的哪部分」。这里改成逐类逐条。
        if ref_labels:
            sections.append("【参考图】参考图用途：" + "；".join(ref_labels) + "。")
        else:
            sections.append("【参考图】本镜无参考图，画面主体与风格仅依据下方【画面内容】与【风格】生成。")

        # ③【画面内容】动作与画面内容 + 说话状态 + 情绪氛围。
        # 画面内容优先级：
        # 1) storyboard_prompt_zh —— 提示词分析器**专门为该镜分镜图**写的中文提示词。
        #    历史缺陷：这个字段只写不读，用户花了 token 生成却从未生效（白花钱）。
        # 2) description —— 剧本自带的画面描述（默认路径）
        # 3) visual_detail —— 剧本阶段保留的扩展画面细节（时间/天气/光源方向/动作过程补全）
        desc = str(shot.get("storyboard_prompt_zh") or "").strip() \
            or (shot.get("description") or "").strip()
        detail = str(shot.get("visual_detail") or "").strip()
        if detail and detail != desc:
            # visual_detail 是描述被截断后的剩余细节，合并成完整画面主体。
            # ⚠️ 用类名调用本类 staticmethod（裸名会去模块作用域找 → NameError）。
            desc = ComfyUIClient._merge_visual_detail(desc, detail)
        location = shot.get("location", "")
        content_lines = []
        content_lines.append(f"镜头{shot.get('shot_id', 1)}")
        if location:
            content_lines.append(f"场景：{location}")
        if desc:
            content_lines.append(f"动作与画面内容：{desc}{SHOT_ACTION_SUFFIX}")
        if _dlg_text(shot.get("dialogue")):
            # 只给说话状态与口型提示，严禁把台词文本写进提示词（模型会把台词当画面字幕画出来）
            content_lines.append(
                f"说话状态：{_dlg_speaker(shot.get('dialogue')) or '人物'}正在低声说一句短句，"
                f"只表现为自然的口型开合与细微表情变化"
            )
        if shot.get("emotion"):
            content_lines.append(f"情绪氛围：{shot['emotion']}")
        if content_lines:
            sections.append("【画面内容】" + "；".join(content_lines) + "。")

        # ④【光影】时间/天气/光源引导。
        # ⚠️ `_extract_light_hint` 是本类的 @staticmethod，在另一个 staticmethod 里
        # **必须用类名调用**；写成裸名 `_extract_light_hint(shot)` 会去模块作用域找，
        # 直接 NameError → 整集分镜图 100% 生成失败（实测雨夜归人 ep2 连续失败 2 次）。
        light_hint = ComfyUIClient._extract_light_hint(shot)
        if light_hint:
            sections.append(f"【光影】光影氛围：{light_hint}。")

        # ⑤【风格】风格与画幅：一律以镜头自带 style 为准（不硬编码国漫）。
        shot_style = style_kit.normalize_style(shot.get("style"))
        if shot_style:
            clause = style_kit.style_suffix(shot_style, head="画面风格", with_tail=False)
            style_clause = clause if clause else ""
        else:
            style_clause = "画面风格以参考图为准，不得自行改变画风"
            logger.warning("镜头 %s 缺少 style（分镜图提示词将不声明风格，建议补齐剧本 style）",
                           shot.get("shot_id"))
        sections.append(f"【风格】{style_clause}。")

        # ⑥【禁令】一致性 + 无文字/水印硬禁令（最后一段兜底，措辞最强硬）。
        sections.append(
            "【禁令】画面中人物的脸型、发型、服装、配饰与角色参考图完全一致；"
            "物品的形状、材质、颜色与物品参考图一致；环境氛围与场景参考图一致；"
            "光影细腻，构图清晰；镜头景别、机位必须与【取景】规定严格一致；"
            "画面中不得出现任何文字、字幕、台词文本、水印、logo 或标识"
            "（尤其不得在右下角出现「AI生成」等生成标识）。"
        )

        return "".join(sections)

    # ===================== H3 音轨控制（生成阶段不出声，配音统一交给 QwenTTS） =====================

    # 末端封装/保存节点：只有这些节点决定最终落盘文件是否带音轨。
    # 说明：H3 工作流内部还有 H3ContinuousStitchOutputV14 等带 audio 输入的中间节点（audio 为
    # optional），它们承担 10 段之间的音频接力；这里**不切断中间节点**，避免破坏连续段链路，
    # 也避免产出"静音但仍有音频流"的伪无声文件。真正不产生音轨靠切断末端 mux 节点实现。
    H3_AUDIO_MUX_TYPES = ("CreateVideo", "CreateVideoWithAudio", "SaveVideo",
                          "SaveWEBM", "VHS_VideoCombine", "SaveAudio", "CreateAudio")
    # 兼容旧命名（早期版本使用的属性名）
    H3_AUDIO_SINK_TYPES = H3_AUDIO_MUX_TYPES

    @classmethod
    def strip_h3_audio_inputs(cls, api_prompt: dict) -> List[str]:
        """断开 H3 工作流末端封装节点的音频输入，使输出 mp4 不含音轨

        仅改动本次提交的 API prompt（内存对象），不修改磁盘上的工作流文件。
        返回被改写的节点描述列表，便于日志核对。
        """
        changed: List[str] = []
        for nid, node in (api_prompt or {}).items():
            if not isinstance(node, dict):
                continue
            ctype = str(node.get("class_type") or "")
            if ctype not in cls.H3_AUDIO_SINK_TYPES:
                continue
            inputs = node.get("inputs") or {}
            for key in ("audio", "audio1", "audio2", "audio_path", "audio_input"):
                if key in inputs and inputs.get(key) is not None:
                    old = inputs[key]
                    inputs[key] = None
                    changed.append(f"{nid}({ctype}).{key}=None(原={old})")
        return changed

    # ===================== 随机种子注入（质检重试时用于产出不同结果） =====================

    @staticmethod
    def _inject_seed(api_prompt: dict, seed) -> List[str]:
        """把 seed 写入工作流中所有采样类节点的 seed / noise_seed 输入

        返回被改写的节点描述列表（便于日志核对）。未传 seed 或未找到采样节点时不做任何改动。
        """
        if seed is None:
            return []
        changed: List[str] = []
        for nid, node in (api_prompt or {}).items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") or {}
            ctype = str(node.get("class_type") or "")
            for key in ("seed", "noise_seed"):
                if key in inputs and not isinstance(inputs.get(key), list):
                    inputs[key] = int(seed)
                    changed.append(f"{nid}({ctype}).{key}={int(seed)}")
                    break
        return changed

    def generate_storyboard(self, prompt_zh: str, ref_images: List[str],
                            filename_prefix: str = "comic_drama_sb/shot",
                            seed: int = None, timeout: int = 900,
                            size=None) -> dict:
        """使用 分镜生成_Qwen21.json（QwenImage2.1 参考图编辑）生成单张分镜图

        ref_images: 参考图列表（本地绝对路径或 /api/... HTTP 资源路径），最多 3 张，
                    按顺序对应正向编辑节点的参考图槽位
                    （QwenImage2.1 是 images.image_1..3；老模板是 image1..3）。
        seed:       可选随机种子（质检不达标重生成时传入，保证产出与上一次不同）。
        size:       可选 (宽, 高)，按用户敲定的画幅覆写尺寸节点（竖屏 9:16 落地）。
        """
        wf_name = WORKFLOW_TEMPLATE["storyboard_gen"]
        api_prompt, _meta = self.load_workflow(wf_name, return_meta=True)
        seed_changed = self._inject_seed(api_prompt, seed)
        if seed_changed:
            logger.info(f"分镜采样种子已注入: {seed_changed}")

        # 1) 正向提示词：只写正向字段（正负同体节点的 negative_prompt 必须原样保留）
        node_id = self._find_positive_text_node(api_prompt)
        pos_field = self._positive_field(api_prompt, node_id) if node_id else None
        if node_id is None or not pos_field:
            raise RuntimeError(f"{wf_name} 未定位到正向提示词字段（节点={node_id}）")
        api_prompt[node_id]["inputs"][pos_field] = prompt_zh
        self.clean_conflict_negative_tokens(api_prompt)                 # P0：清理风格冲突负向词
        if size:
            hit = style_kit.apply_latent_size(api_prompt, size)
            if hit:
                logger.info("分镜图幅已覆写为 %s×%s：%s", size[0], size[1], ",".join(hit))
            else:
                # 「参考图编辑」型模板没有横向尺寸节点：
                #   老模板 FluxKontextImageScale 与 QwenImage2.1 的 TextEncodeQwenImage21.latent
                #   都按参考图尺寸出 latent → 输出画幅继承第一张参考图，
                #   由基础资产图的尺寸决定，这里空转属正常。
                logger.info("分镜工作流无尺寸节点，画幅继承参考图（%s×%s）"
                            "—— 由基础资产图尺寸决定", size[0], size[1])

        # 2) 参考图上传到 ComfyUI output 目录（LoadImageOutput 只认 output 目录 + [output] 标注）
        uploaded: List[str] = []
        for idx, p in enumerate((ref_images or [])[:3]):
            local = self.resolve_local_path(p) if isinstance(p, str) else None
            if not local or not os.path.exists(local):
                logger.warning(f"分镜参考图不可用，已跳过: {p}")
                continue
            name = f"sb_ref_{os.path.splitext(os.path.basename(filename_prefix))[0]}_{idx + 1}.png"
            uploaded.append(self.upload_image(local, name, image_type="output"))
        if not uploaded:
            raise RuntimeError("分镜参考图全部不可用（请先完成步骤2/3/4的资产生成）")

        # 3) 逐个槽位替换参考图（不替换会残留模板里的他人图片名 → 400 Invalid image file）
        #    P0 修复（原 comfyui_client.py:870）：原实现 `uploaded[min(idx, len(uploaded) - 1)]`
        #    在参考图少于槽位时会把**最后一张**复制到所有多余槽位 —— ref_count=2（主角+场景）
        #    时 image3 变成第二张场景图，等于告诉模型"第三主体是场景"，造成主体错位/画面错乱。
        #    现改为：参考图与槽位按序一一对应；槽位多于参考图时，多余槽位复用**第一张
        #    （主角锚点）**，并在 slot_duplicates 中显式登记 + 日志告警，杜绝静默重复场景图。
        slots = self._find_image_slots(api_prompt, node_id)
        ref_values = []
        slot_duplicates: List[str] = []
        for idx, (key, load_id) in enumerate(slots):
            if load_id is None:
                continue
            if idx < len(uploaded):
                src = uploaded[idx]
            else:
                src = uploaded[0]
                slot_duplicates.append(f"{key}(slot{idx + 1})={src}")
                logger.warning(
                    f"分镜参考图槽位多于参考图（{len(slots)} 槽 / {len(uploaded)} 张）："
                    f"{key} 复用主角锚点图 {src}（已登记 slot_duplicates）")
            ctype = api_prompt[load_id].get("class_type")
            value = self.annotate_file_ref(src, ctype)
            api_prompt[load_id]["inputs"]["image"] = value
            ref_values.append(f"{key}->{load_id}({ctype})={value}")
        if slot_duplicates:
            logger.warning(f"分镜参考图存在槽位复用 {len(slot_duplicates)} 处: {slot_duplicates}")
        logger.info(f"分镜参考图已注入 {len(ref_values)} 个槽位: {ref_values}")

        # 4) 输出文件名前缀
        for _nid, n in api_prompt.items():
            if n.get("class_type") in ("SaveImageAdvanced", "SaveImage") and "filename_prefix" in n["inputs"]:
                n["inputs"]["filename_prefix"] = filename_prefix

        report = self.validate_api_prompt(api_prompt)
        if report["unknown_types"] or report["missing_required"] or report["dangling_links"]:
            logger.warning(f"分镜提交前自检异常: {json.dumps(report, ensure_ascii=False)[:400]}")

        prompt_id = self.queue_prompt(api_prompt)
        history = self.wait_for_completion(prompt_id, timeout=timeout)
        files = self.get_output_files(history, ".png")
        return {"prompt_id": prompt_id, "files": files, "history": history,
                "refs_used": uploaded, "prompt_node": node_id, "seed": seed,
                "slot_duplicates": slot_duplicates}

    # ===================== 视频生成（MiniMax H3） =====================
    # D-08（P2）：原先此处有一个 [LEGACY · 已停用] 的固定「10 段模板单次生成」方法，
    # 函数名却在语义上像是「生成一个视频」——新同学按名字调用会让每个分镜都跑 10 段。
    # 经全仓（含 frontend/）核实**零调用者**，已删除；H3 视频生成统一走下方
    # generate_h3_sequence / generate_h3_sequence_sequential（段数 = 传入分镜数）。
    # 防回潮守卫见 verify_legacy_generate_video.py。


    def generate_h3_sequence_sequential(
        self,
        segments: List[dict],
        filename_prefix: str = "comic_drama/episode",
        seed: int = None,
        timeout_per_segment: int = 900,
        template_file: str = None,
        size=None,
        qc_fn=None,
        qc_cfg: dict = None,
        qc_style: str = "",
        max_retries: int = 2,
        qc_stop_cb=None,
    ) -> dict:
        """H3 整集视频生成（N 段一个工作流，原生 H3ContinuousSeamlessJoinV14 衔接）
        + 整片 QC 门控。

        设计说明：H3 多段无缝衔接依赖同一工作流内 previous_latent / handover
        连线（段实例间张量传递），无法拆成多次独立 ComfyUI 任务再跨任务喂隐变量。
        因此「逐段提交」在 H3 层级不成立；本方法保持 N 段一次提交，产出 **单个
        连续整集视频**，生成后对整片做抽帧质检，不通过则整片重试（换随机种子）。

        - segments: 同 generate_h3_sequence
        - qc_fn: 可选；qc_fn(video_path, shot_desc, qc_cfg, style) 返回
          {"passed": bool, "verdict": {...}, ...}；通过才保留成片，不通过则整片重试
        - qc_stop_cb: 可选；G1 止损回调 qc_stop_cb(qc_results) -> (stop, detail)。
          连续两次缺陷完全相同时提前停止整片重试，避免白烧 GPU（最贵的一处）。
        - max_retries: 整片 QC 不通过时最多重试次数（换随机种子）
        返回 dict：
          {"files": [video_path], "segments": [...], "qc_results": [...],
           "failed": bool, "attempts_used": int, "prompt_id": str}
        """
        segs = [dict(s or {}) for s in (segments or [])]
        if not segs:
            raise ValueError("generate_h3_sequence_sequential: segments 不能为空")
        n = len(segs)
        logger.info(f"[H3-episode] 整集生成：{n} 段一次提交，qc_fn={bool(qc_fn)}, "
                    f"max_retries={max_retries}")

        qc_results: List[dict] = []
        attempts_used = 0
        cur_seed = seed
        best_file = None       # 最后一次生成成功的文件（供 QC 全不通过时兜底）
        last_prompt_id = ""
        passed = False
        last_seg_report: List[dict] = []
        last_error: str = ""

        for attempt in range(max_retries + 1):
            attempts_used = attempt + 1
            if attempt > 0:
                # ⚠️ 必须是真随机数，不能写 None：_inject_seed 对 None 直接 `return []`
                #    （= 不注入），于是沿用工作流 JSON 里的**字面量 seed**；而前端的
                #    `control_after_generate: randomize` 属于 widgets_values，API 模式不提交。
                #    实测（2026-09-20）：这让三次"重试"提交完全相同的 prompt+参考图+seed，
                #    产物逐字节相同 —— 22~44 段 H3 一次跑几十分钟，属纯白烧 GPU。
                cur_seed = random.randint(1, 2 ** 31 - 1)
                logger.info(f"[H3-episode] 第 {attempt + 1} 次整片重试（换种子 {cur_seed}）")

            # ---------- 生成（N 段一个工作流 → 单个连续成片） ----------
            result = None
            try:
                result = self.generate_h3_sequence(
                    segments=segs,
                    filename_prefix=filename_prefix,
                    seed=cur_seed,
                    timeout_per_segment=timeout_per_segment,
                    template_file=template_file,
                    size=size,
                )
            except RuntimeError as e:
                # S12：确定性输入错误（如某段无可用参考图被拒绝提交）——
                # 重试必然同败，立即停止并保留原因，不再空转换种子。
                last_error = str(e)
                logger.warning(f"[H3-episode] 生成被拒绝（不再重试）: {e}")
                break
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次生成异常: {e}")
                continue

            files = result.get("files") or []
            last_prompt_id = result.get("prompt_id") or last_prompt_id
            last_seg_report = result.get("segments") or []
            if not files:
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次：ComfyUI 未返回视频文件")
                continue
            if not os.path.isfile(files[0]):
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次：文件不存在 {files[0]}")
                continue

            best_file = files[0]

            # ---------- 整片 QC 门控 ----------
            if qc_fn:
                # 用所有段提示词拼接代表整片
                shot_desc = "\n".join((s.get("prompt") or "") for s in segs)
                try:
                    qc_result = qc_fn(best_file, shot_desc, qc_cfg, qc_style)
                except Exception as qc_err:
                    # A-1：QC 回调抛异常 = **不可判定**（接口/ffmpeg 不可用，与内容无关），
                    # 不是"内容不达标"。旧实现 `continue` 会换种子重跑整集，且绕过 qc_stop_cb
                    # 止损 —— 每次整片 20~44 段 H3、几十分钟 GPU，纯白烧。这里 break：
                    # best_file 已就位，成片照常返回给调用方人工复核。
                    logger.warning(
                        f"[H3-episode] 第 {attempt + 1} 次 QC 调用异常"
                        f"（不可判定，不重试）: {qc_err}")
                    qc_results.append({"attempt": attempt + 1, "passed": None,
                                       "unavailable": True,
                                       "reason": f"QC 异常: {qc_err}"})
                    break
                qc_passed = qc_result.get("passed", False)
                _v = qc_result.get("verdict") or {}
                qc_results.append({
                    "attempt": attempt + 1, "passed": qc_passed,
                    "reason": _v.get("reason") or "",
                    "critical_issues": _v.get("critical_issues") or [],
                    "issues": _v.get("issues") or [],
                    "file": best_file,
                })
                if qc_passed:
                    logger.info(f"[H3-episode] 第 {attempt + 1} 次整片 QC 通过")
                    passed = True
                    break
                # G1 止损铺开：整片重试最贵，连续两次缺陷一字不差 → 提前停（换 seed 只是换骰子）
                if qc_stop_cb is not None:
                    _ep_stop, _ep_detail = qc_stop_cb(qc_results)
                    if _ep_stop:
                        logger.warning(
                            f"[H3-episode] 整片重试止损：连续 {len(qc_results)} 次缺陷完全相同"
                            f"（{_ep_detail}），提前停止；建议改段提示词/剧本后单独重跑")
                        qc_results[-1]["retry_stopped"] = True
                        qc_results[-1]["retry_stopped_features"] = _ep_detail
                        break
                logger.warning(f"[H3-episode] 第 {attempt + 1} 次整片 QC 不通过"
                               f"（reason={(_v.get('reason') or '-')})，"
                               f"{'重试' if attempt < max_retries else '放弃'}")
            else:
                logger.info("[H3-episode] 整片生成成功（无 QC 门控）")
                passed = True
                break

        if not best_file:
            logger.error(f"[H3-episode] {n} 段整集生成失败（无成片）{('：' + last_error) if last_error else ''}")
            return {
                "files": [], "segments": last_seg_report,
                "qc_results": qc_results, "failed": True,
                "error": last_error,
                "attempts_used": attempts_used, "prompt_id": last_prompt_id,
                "segment_count": n,
            }

        logger.info(f"[H3-episode] 完成：产物 {best_file}，通过={passed}，"
                    f"attempts={attempts_used}")
        return {
            "files": [best_file],
            "segments": last_seg_report,
            "qc_results": qc_results,
            "failed": not passed,
            "attempts_used": attempts_used,
            "prompt_id": last_prompt_id,
            "segment_count": n,
        }

    def generate_h3_sequence(self, segments: List[dict],
                             filename_prefix: str = "comic_drama/episode",
                             emit_audio: bool = None,
                             seed: int = None,
                             timeout: int = None,
                             timeout_per_segment: int = 900,
                             template_file: str = None,
                             save_build_to: str = None,
                             size=None) -> dict:
        """H3 多段一次生成：**工作流段数 = len(segments)**，一个分镜对应一段。

        与 generate_video 的差异（修复"每个分镜跑了 10 段"）：
        - 旧实现固定加载「10 段无缝拼接」模板，并把 10 段 Text Multiline 全部写成同一提示词，
          于是一个分镜产出的是 10 段同内容拼接的长视频（约 72s），既费时又不符合单镜时长。
        - 本方法按调用方给定的段数**动态重建工作流**（第 4 集 22 段、第 5 集 44 段），
          逐段独立注入 提示词 / 时长 / 参考图，段间仍由 H3ContinuousSeamlessJoinV14 保持无缝。

        segments: [{"prompt": str, "duration": float, "reference_images": [本地绝对路径, ...],
                    "name": str}]  —— 元素顺序 = 工作流段顺序
        timeout:  总超时（秒）；None 时按 1200 + timeout_per_segment × 段数 估算
        timeout_per_segment: 单段预估耗时（默认 900s，用于总超时兜底）
        save_build_to: 可选，把重建后的 UI 工作流落盘（便于复现/排障）
        size: 可选 (宽, 高)，按用户敲定的画幅覆写模板分辨率（竖屏 9:16 → (544, 960)）
        """
        segs = [dict(s or {}) for s in (segments or [])]
        if not segs:
            raise ValueError("generate_h3_sequence: segments 不能为空")
        n = len(segs)
        tpl_name = template_file or WORKFLOW_TEMPLATE["h3_video"]
        tpl_path = os.path.join(COMFYUI_WORKFLOWS_DIR, tpl_name)
        builder = H3EpisodeBuilder(tpl_path)
        default_duration = float((segs[0] or {}).get("duration") or 5.0)
        wf, layout = builder.build(n, duration=default_duration,
                                   resolution_override=tuple(size) if size else None)
        if save_build_to:
            os.makedirs(os.path.dirname(save_build_to), exist_ok=True)
            with open(save_build_to, "w", encoding="utf-8") as f:
                json.dump(wf, f, ensure_ascii=False)
            layout["build_path"] = save_build_to
        logger.info(f"H3 动态工作流已就绪：{n} 段（模板 {tpl_name}），"
                    f"UI 节点 {layout['node_total']} / 连线 {layout['link_total']}")

        api_prompt, meta = self.to_api(wf, return_meta=True)
        seed_changed = self._inject_seed(api_prompt, seed)
        if seed_changed:
            logger.info(f"视频采样种子已注入: {seed_changed}")

        # ---------------- 逐段注入（提示词 / 时长 / 参考图） ----------------
        seg_report: List[dict] = []
        for i, seg in enumerate(segs):
            lay = layout["segments"][i]
            prompt_text = seg.get("prompt") or ""
            api_prompt[str(lay["prompt_node"])]["inputs"]["text"] = prompt_text
            dur = float(seg.get("duration") or default_duration)
            api_prompt[str(lay["duration_node"])]["inputs"]["value"] = dur

            local_refs: List[str] = []
            for img in (seg.get("reference_images") or []):
                local = self.resolve_local_path(img)
                if local and os.path.exists(local):
                    local_refs.append(local)
                elif img:
                    logger.warning(f"段{i + 1} 参考图不可用，已跳过: {img}")
            # S12（P0）：参考图为空时不再静默沿用模板自带的 LoadImage 示例图——
            # 示例图里的人物会污染角色外观，成片出现与剧本无关的人物。
            # fail-fast：直接抛错让整次 H3 提交失败，上层 worker 感知并重试/跳过，
            # 而不是烧完 GPU 才拿到一个含无关人物的成片。
            if not local_refs:
                raise RuntimeError(
                    f"段{i + 1}（{seg.get('name') or f'seg_{i + 1}'}）无任何可用参考图，"
                    f"已拒绝提交（S12：模板示例图会污染角色外观）。请补齐该段 "
                    f"reference_images 后重试。"
                )
            loaded: List[dict] = []
            slots = lay["ref_nodes"]
            # 参考图不足时用本段第一张参考图补齐空槽（纯单参考即重复同一张）。
            fill_src = local_refs[0]
            up_cache: dict = {}
            for j in range(len(slots)):
                is_fill = j >= len(local_refs)
                rp = local_refs[j] if not is_fill else fill_src
                try:
                    if rp not in up_cache:
                        # 参考图上传到 ComfyUI input 目录（段实例用 LoadImage 读取）
                        up_cache[rp] = self.annotate_file_ref(
                            self.upload_image(rp, image_type="input"), "LoadImage")
                    val = up_cache[rp]
                    api_prompt[str(slots[j])]["inputs"]["image"] = val
                    loaded.append({"slot": slots[j], "src": rp, "value": val})
                except Exception as e:
                    logger.warning(f"段{i + 1} 参考图上传失败 {rp}: {e}")
            seg_report.append({"index": i, "name": seg.get("name") or f"seg_{i + 1}",
                               "inst": lay["inst"], "prompt_len": len(prompt_text),
                               "prompt_head": prompt_text[:60], "duration": dur,
                               "refs": loaded})
            logger.info(f"段{i + 1}/{n} 注入完成：时长 {dur}s，参考图 {len(loaded)} 张")

        # ---------------- 保存文件名前缀 ----------------
        for nid, node in api_prompt.items():
            if node.get("class_type") in ("SaveVideo", "SaveImage", "SaveImageAdvanced", "VHS_VideoCombine"):
                if "filename_prefix" in node["inputs"]:
                    node["inputs"]["filename_prefix"] = filename_prefix

        # ---------------- 音轨策略（与 generate_video 一致：默认静音，后续统一配音） ----------------
        emit_audio = H3_EMIT_AUDIO if emit_audio is None else bool(emit_audio)
        audio_changed: List[str] = []
        if not emit_audio:
            audio_changed = self.strip_h3_audio_inputs(api_prompt)
            if audio_changed:
                logger.info(f"H3 音轨已断开（生成阶段不出声）: {audio_changed}")
            else:
                logger.warning("H3 音轨断开未命中任何节点，请检查工作流是否变更（成片可能仍带音轨）")

        self.clean_conflict_negative_tokens(api_prompt)   # P0：清理与 3D 正向风格冲突的负向词

        report = self.validate_api_prompt(api_prompt)
        if report["unknown_types"] or report["missing_required"] or report["dangling_links"]:
            logger.warning(f"H3 提交前自检异常: {json.dumps(report, ensure_ascii=False)[:400]}")

        timeout = timeout or int(1200 + timeout_per_segment * n)
        logger.info(f"H3 提交：{n} 段，总超时 {timeout}s（单段预估 {timeout_per_segment}s）")
        prompt_id = self.queue_prompt(api_prompt)
        history = self.wait_for_completion(prompt_id, timeout=timeout)
        files = self.get_output_files(history, ".mp4")

        audio_check = []
        if files:
            from video_postprocess import probe_media
            for f in files:
                m = probe_media(f)
                audio_check.append({"file": f, "has_audio": m.get("has_audio"),
                                    "audio_streams": m.get("audio_streams"),
                                    "video_streams": m.get("video_streams"),
                                    "error": m.get("error")})

        return {"prompt_id": prompt_id, "files": files, "history": history,
                "segment_count": n, "segments": seg_report, "layout": layout,
                "meta": meta, "seed": seed, "emit_audio": bool(emit_audio),
                "audio_disconnected": audio_changed, "audio_check": audio_check,
                "timeout": timeout, "template": tpl_name,
                "validate_report": report}

    @staticmethod
    def _h3_picture_defs(char_refs: List[dict], scene_refs: List[dict],
                         storyboard_ref: dict = None):
        """把参考图列表映射成 H3 的 ``(<Picture N>, 用途说明)`` 与 ``<Subject N>`` 定义

        语义约定：
            storyboard_ref 非空 → <Picture 1> = 分镜图（构图/景别/机位/人物姿态基准）
                                  <Picture 2> = 主角外观锚点
            否则                 → <Picture 1..n> = 角色外观锚点，其后为场景环境参考
        """
        picture_defs: List[tuple] = []
        subjects: List[Dict[str, str]] = []

        def _appearance(ref: dict) -> str:
            return str(ref.get("appearance") or ref.get("description")
                       or ref.get("reference_prompt_zh") or "").strip()[:120]

        if storyboard_ref:
            sb_name = storyboard_ref.get("name") or "本镜头分镜图"
            picture_defs.append((
                "<Picture 1>",
                f"该镜头的分镜图（{sb_name}），定义本镜的构图、景别、机位、环境与人物姿态"))
            main = (char_refs or [{}])[0]
            if main.get("name"):
                picture_defs.append((
                    "<Picture 2>",
                    f"{main.get('name')} 的外观参考，定义其五官、发型、服装与画风，"
                    f"必须与 <Picture 1> 保持同一人物"))
            for ref in (char_refs or [])[:1]:
                subjects.append({"name": ref.get("name", "主角"),
                                 "appearance": _appearance(ref)})
            return picture_defs, subjects

        for ref in (char_refs or [])[:2]:
            name = ref.get("name", f"角色{len(picture_defs) + 1}")
            picture_defs.append((
                f"<Picture {len(picture_defs) + 1}>",
                f"{name} 的外观参考，定义其五官、发型、服装与画风，"
                f"并作为其出场镜头的构图锚点"))
            subjects.append({"name": name, "appearance": _appearance(ref)})
        for ref in (scene_refs or [])[:1]:
            name = ref.get("name", f"场景{len(picture_defs) + 1}")
            picture_defs.append((
                f"<Picture {len(picture_defs) + 1}>",
                f"{name} 的环境参考，定义场景结构、材质氛围与光照基调"))
        return picture_defs, subjects

    def resolve_h3_prompt(self, shot: dict, char_refs: List[dict],
                          scene_refs: List[dict], storyboard_ref: dict = None) -> str:
        """生成期**权威**的 H3 提示词入口（修「薄英文顶掉结构化构建器」）

        择优规则：
        - 剧本里已有 ``prompt_h3`` 且通过 :func:`h3_prompt_kit.validate`
          （六段/三段齐全）→ 直接采用（LLM 写的散文通常更生动）
        - 不合规（历史裸英文句、缺段）→ 用规范构建器重建，并把旧文本并入
          ``detailed_description`` 作补充细节，信息不丢

        为什么不能让旧的 ``prompt_h3`` 直接生效：H3 走 Ref2VA，提示词必须带
        ``<Picture N>`` 标签告诉模型每张参考图的用途；而剧本阶段的 LLM 根本
        不知道最终配了几张图，只能写出一句无标签的裸英文 —— 实测全项目 200+
        镜头的结构化提示词数量为 0，出片与设定严重不符。
        """
        shot = shot or {}
        picture_defs, subjects = self._h3_picture_defs(char_refs, scene_refs, storyboard_ref)
        style = h3_prompt_kit.style_of(shot)
        if not picture_defs:
            built = h3_prompt_kit.build_base(shot, "T2VA", style=style)
            existing = str(shot.get("prompt_h3") or "").strip()
            if not existing:
                # A-5：无参考图分支不经过 resolve()，必须自己过一道长度闸门
                #（build_base 在超长 description 下同样可能越界）
                return h3_prompt_kit.clamp_h3_prompt(built)
            verdict = h3_prompt_kit.validate(existing)
            # A-5：这条路径**完全绕过 resolve()**（既有 prompt_h3 直接生效），
            # 能把裸 >6000 字符提示词原样送进 H3 —— 必须显式截断。
            return h3_prompt_kit.clamp_h3_prompt(
                existing if verdict["valid"] else h3_prompt_kit.merge_detail(built, existing))
        return h3_prompt_kit.clamp_h3_prompt(
            h3_prompt_kit.resolve(shot, picture_defs, subjects, style=style))

    def _build_h3_prompt(self, shot: dict, char_refs: List[dict], scene_refs: List[dict],
                         storyboard_ref: dict = None) -> str:
        """构建规范 H3 Ref2VA 提示词（无条件重建，忽略剧本里的既有 prompt_h3）

        需要一个「干净重建」的调用点时用它（例如风格纠偏重试）；日常生成请用
        :meth:`resolve_h3_prompt`，后者会优先尊重已合规的既有提示词。
        """
        shot = shot or {}
        picture_defs, subjects = self._h3_picture_defs(char_refs, scene_refs, storyboard_ref)
        style = h3_prompt_kit.style_of(shot)
        if not picture_defs:
            # A-5 加固：本方法同样不经过 resolve()，且被 app.py 4 处直接调用
            #（2057/2068/3715/3733），同一类"超长提示词被服务端静默截断"的口子。
            return h3_prompt_kit.clamp_h3_prompt(
                h3_prompt_kit.build_base(shot, "T2VA", style=style))
        return h3_prompt_kit.clamp_h3_prompt(
            h3_prompt_kit.build_ref2va(shot, picture_defs, subjects, style=style))
