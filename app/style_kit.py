"""项目风格统一落地工具（风格注入链路的唯一入口）

背景（2026-09-18 修复）
----------------------
用户与 AI 总控敲定的风格串（如「国漫风格/偏写实/竖屏9:16」）此前只在剧本 bible 的提示词里
"提了一句"，随后在所有真正的生成环节全部丢失：

1. ``novel_to_script._norm_shots`` 组装的镜头行里**没有 style 字段** → 24/24 镜 ``shot["style"]``
   为 None → 视频提示词 ``shot.get("style", "3D动漫渲染")`` 永远回落到硬编码默认值；
2. ``app._generate_asset_task`` 调 ``generate_character_base/item_base/scene_base`` 时**只传
   reference_prompt_zh**，风格既没进 bible 的参考图提示词，也没在生成前追加；
3. ``comfyui_client`` 分镜提示词把「国漫3D渲染风格，竖屏 9:16 构图」**写死**在要求段里；
4. 工作流尺寸节点（``EmptySD3LatentImage``）与 H3 模板的 ``ResolutionSelector`` 都是
   模板自带的 16:9，**没有任何代码按用户设定覆写** → 成片恒为 960×544 横屏。

结论：风格设定对**所有**资产类型都是失效的（不只物品），只是物品最容易一眼看出。

本模块提供唯一入口，避免又一次"每个调用点各写一遍、又漏一处"：

- :func:`normalize_style`        —— 归一化风格串（分隔符统一为「，」、去重保序）
- :func:`style_suffix`           —— 生成可直接拼进图像提示词的风格后缀
- :func:`with_style`             —— 幂等地把风格后缀拼到任意提示词末尾
- :func:`aspect_ratio`           —— 从风格串解析画幅（"竖屏9:16" → (9, 16)）
- :func:`aspect_size`            —— 画幅 → 工作流用宽高（按 multiple 对齐，复刻 ComfyUI 公式）
- :func:`apply_latent_size`      —— 覆写 API prompt 中所有尺寸节点的宽高
- :func:`apply_resolution_widgets` —— 覆写 UI 工作流里分辨率节点的 widget 值
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 风格串归一化
# --------------------------------------------------------------------------- #

#: 用户 / 模型可能使用的各种分隔符
_SEP_RE = re.compile(r"[，,、/／|｜·・；;\s]+")

#: 视为「无风格」的占位串
_EMPTY_TOKENS = {"", "none", "null", "无", "默认", "未设置", "无风格", "custom", "自定义"}


def normalize_style(style) -> str:
    """归一化风格串：保留原词序与用词，仅统一分隔符并去重。

    ``"国漫风格/偏写实/竖屏9:16"`` → ``"国漫风格，偏写实，竖屏9:16"``
    """
    raw = str(style or "").strip()
    if not raw or raw.lower() in _EMPTY_TOKENS:
        return ""
    seen, parts = set(), []
    for p in _SEP_RE.split(raw):
        p = p.strip()
        if not p or p.lower() in _EMPTY_TOKENS or p in seen:
            continue
        seen.add(p)
        parts.append(p)
    return "，".join(parts)


def style_tokens(style) -> List[str]:
    """拆出风格词列表（供画幅解析、关键词匹配使用）"""
    norm = normalize_style(style)
    return [p for p in norm.split("，") if p] if norm else []


# --------------------------------------------------------------------------- #
# 画幅解析
# --------------------------------------------------------------------------- #

_RATIO_RE = re.compile(r"(?P<a>\d{1,2})\s*[:：]\s*(?P<b>\d{1,2})")

#: 中文/英文关键词 → 画幅（关键词比裸数字更明确地表达用户意图，优先命中）
_KEYWORD_RATIO: Sequence[Tuple[str, Tuple[int, int]]] = (
    ("竖屏", (9, 16)),
    ("竖向", (9, 16)),
    ("竖版", (9, 16)),
    ("手机屏", (9, 16)),
    ("抖音", (9, 16)),
    ("portrait", (9, 16)),
    ("vertical", (9, 16)),
    ("横屏", (16, 9)),
    ("横向", (16, 9)),
    ("横版", (16, 9)),
    ("宽屏", (16, 9)),
    ("landscape", (16, 9)),
    ("方形", (1, 1)),
    ("square", (1, 1)),
)


def aspect_ratio(style) -> Optional[Tuple[int, int]]:
    """从风格串解析目标画幅，解析不到返回 None。

    优先关键词（"竖屏" / "portrait"），其次显式比例（"9:16"）。
    """
    toks = style_tokens(style)
    if not toks:
        return None
    joined = "，".join(toks).lower()
    for kw, ratio in _KEYWORD_RATIO:
        if kw in joined:
            return ratio
    m = _RATIO_RE.search(joined)
    if m:
        a, b = int(m.group("a")), int(m.group("b"))
        if 0 < a <= 32 and 0 < b <= 32:
            return (a, b)
    return None


def is_portrait(style) -> bool:
    r = aspect_ratio(style)
    return bool(r) and r[0] < r[1]


def aspect_size(ratio: Optional[Tuple[int, int]], megapixels: float = 0.5,
                multiple: int = 32) -> Optional[Tuple[int, int]]:
    """画幅 → 宽高像素（复刻 ComfyUI ``nodes_resolution`` 的算法）

    ``(9, 16)`` + 0.5MP + 32 → ``(544, 960)``；``(16, 9)`` → ``(960, 544)``
    """
    if not ratio:
        return None
    a, b = int(ratio[0]), int(ratio[1])
    if a <= 0 or b <= 0 or multiple <= 0:
        return None
    scale = math.sqrt(float(megapixels) * 1024 * 1024 / (a * b))
    width = round(a * scale / multiple) * multiple
    height = round(b * scale / multiple) * multiple
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def aspect_label(style) -> str:
    """把画幅描述成人话，用于日志 / 界面回显"""
    r = aspect_ratio(style)
    if not r:
        return ""
    size = aspect_size(r)
    orient = "竖屏" if r[0] < r[1] else ("横屏" if r[0] > r[1] else "方形")
    return f"{orient} {r[0]}:{r[1]}" + (f"（{size[0]}×{size[1]}）" if size else "")


# --------------------------------------------------------------------------- #
# 提示词风格注入
# --------------------------------------------------------------------------- #

#: 画幅类 token 不进「图像风格后缀」正文（已由尺寸节点表达，重复描述反而污染提示词）
_ASPECT_TOKENS = ("竖屏", "横屏", "竖向", "横向", "竖版", "横版", "方形", "9:16", "16:9", "1:1",
                  "portrait", "landscape", "square")

#: 风格后缀的固定收尾（保画质、压畸形，与既有工作流负向词配合）
_QUALITY_TAIL = "画面精致，光影细腻，构图稳定，无畸形"


def style_suffix(style, *, with_tail: bool = True, with_aspect: bool = True,
                 head: str = "风格") -> str:
    """把风格串变成可拼进图像提示词的后缀。

    画幅类 token 默认不进正文（改由工作流尺寸节点落实），但会以「竖屏 9:16 构图」的
    形式补一句构图声明 —— 因为图像模型对画面比例的语义提示仍敏感。
    """
    toks = style_tokens(style)
    if not toks:
        return ""
    body = [t for t in toks if not any(a in t for a in _ASPECT_TOKENS)]
    bits: List[str] = []
    if body:
        bits.append(f"{head}：{'，'.join(body)}")
    if with_aspect:
        r = aspect_ratio(style)
        if r:
            orient = "竖屏" if r[0] < r[1] else ("横屏" if r[0] > r[1] else "方形")
            bits.append(f"{orient} {r[0]}:{r[1]} 构图")
    if with_tail and bits:
        bits.append(_QUALITY_TAIL)
    return "，".join(bits)


def with_style(prompt: str, style, *, with_tail: bool = True) -> str:
    """幂等地把风格后缀拼到提示词末尾（已带过同一风格串则不重复追加）"""
    text = str(prompt or "").strip()
    suffix = style_suffix(style, with_tail=with_tail)
    if not suffix:
        return text
    if suffix in text:
        return text
    # 只判「风格：」标记，避免不同风格串互相判定为已注入
    if "风格：" in text and _style_marker(style) in text:
        return text
    if not text:
        return suffix
    sep = "" if text.endswith(("。", "，", "；", ".", "!", "！", "?", "？")) else "。"
    return f"{text}{sep}{suffix}。"


def style_emphasis(style) -> str:
    """生成「强风格指令头」，用于需要强调风格的场景（如尾帧、风格不达标重试）。

    与 :func:`style_suffix` 的区别：后者只是拼在提示词末尾的一句话，容易被模型弱化；
    本函数把风格要求放到提示词**最前**并声明为最高优先级，用于在风格已跑偏时强力纠正。
    """
    norm = normalize_style(style)
    if not norm:
        return ""
    return (f"【风格铁律·最高优先级】本画面必须严格采用「{norm}」的视觉风格、"
            f"画风、渲染方式与配色，任何环节都不得偏离该风格。")


def _style_marker(style) -> str:
    toks = [t for t in style_tokens(style) if not any(a in t for a in _ASPECT_TOKENS)]
    return "，".join(toks)


def negative_for_style(style) -> List[str]:
    """按风格给出需要压制的负向词（避免风格互相打架）"""
    toks = "，".join(style_tokens(style))
    neg: List[str] = []
    if any(k in toks for k in ("国漫", "国风", "水墨", "日式", "赛璐璐", "动漫", "卡通")):
        neg += ["写实照片", "真人摄影", "3D 写实渲染"]
    if "写实" in toks and "偏写实" not in toks:
        neg += ["卡通描边", "平涂色块"]
    return neg


# --------------------------------------------------------------------------- #
# 工作流尺寸覆写
# --------------------------------------------------------------------------- #

#: 直接带 width / height 输入的潜空间尺寸节点（ComfyUI 各版本命名差异）
_LATENT_SIZE_TYPES = (
    "EmptySD3LatentImage",
    "EmptyLatentImage",
    "EmptyLatentImagePresets",
    "SDXL Empty Latent Image",
    "EmptyLatentSDXL",
    "EmptyHunyuanLatentVideo",
)

#: 带 aspect_ratio / megapixels 的分辨率选择器节点
_RESOLUTION_SELECTOR_TYPES = ("ResolutionSelector", "Resolution Selector")

#: 画幅 → ResolutionSelector 的下拉文本（不同版本用词不同，这里给出规范值）
_SELECTOR_ASPECT_TEXT = {
    (1, 1): "1:1 (Square)",
    (2, 3): "2:3 (Portrait Photo)",
    (3, 2): "3:2 (Photo)",
    (3, 4): "3:4 (Portrait Standard)",
    (4, 3): "4:3 (Standard)",
    (9, 16): "9:16 (Portrait Widescreen)",
    (16, 9): "16:9 (Widescreen)",
    (21, 9): "21:9 (Ultrawide)",
}


def apply_latent_size(api_prompt: dict, size: Optional[Tuple[int, int]]) -> List[str]:
    """把 API prompt 里所有尺寸节点的宽高改成 ``size``，返回被改写的节点描述。

    只改「同时具备 width/height 输入」的节点，命中即算成功；未命中返回空列表，
    由调用方决定是记 warning 还是静默放行（老模板无尺寸节点时不应阻断生成）。
    """
    if not api_prompt or not size:
        return []
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0:
        return []
    changed: List[str] = []
    for node_id, node in (api_prompt or {}).items():
        if not isinstance(node, dict):
            continue
        ctype = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if ctype in _LATENT_SIZE_TYPES and "width" in inputs and "height" in inputs:
            inputs["width"] = width
            inputs["height"] = height
            changed.append(f"{node_id}({ctype})")
            continue
        # 兜底：任何同时声明 width/height 的潜空间/尺寸节点
        if "width" in inputs and "height" in inputs and ("Latent" in ctype or "Size" in ctype):
            inputs["width"] = width
            inputs["height"] = height
            changed.append(f"{node_id}({ctype})")
    return changed


def apply_resolution_widgets(nodes: List[dict],
                             ratio: Optional[Tuple[int, int]]) -> List[str]:
    """覆写 UI 工作流里 ResolutionSelector 节点的 widget 值，返回被改写节点描述。

    仅改 ``widgets_values`` 与 ``widgets_values_named``，不动连线结构。
    """
    if not nodes or not ratio:
        return []
    text = _SELECTOR_ASPECT_TEXT.get(tuple(ratio))
    if not text:
        # 非标准画幅（如 5:4）没有对应下拉项，保持原样让调用方走显式宽高覆写路径
        return []
    changed: List[str] = []
    for n in nodes:
        if str(n.get("type") or "") not in _RESOLUTION_SELECTOR_TYPES:
            continue
        named = dict(n.get("widgets_values_named") or {})
        named["aspect_ratio"] = text
        n["widgets_values_named"] = named
        vals = list(n.get("widgets_values") or [])
        if vals:
            vals[0] = text
        else:
            vals = [text]
        n["widgets_values"] = vals
        changed.append(f"{n.get('id')}(ResolutionSelector→{text})")
    return changed


# --------------------------------------------------------------------------- #
# 便捷组合
# --------------------------------------------------------------------------- #

def resolve(style, *, default_ratio: Optional[Tuple[int, int]] = None
            ) -> Dict[str, object]:
    """一次拿到风格落地所需的全部派生值（供各生成入口统一调用）"""
    norm = normalize_style(style)
    ratio = aspect_ratio(norm) or default_ratio
    size = aspect_size(ratio)
    return {
        "style": norm,
        "tokens": style_tokens(norm),
        "ratio": ratio,
        "size": size,
        "suffix": style_suffix(norm),
        "label": aspect_label(norm),
    }


def apply_asset_style(assets: Optional[List[dict]], style,
                      *, key: str = "reference_prompt_zh") -> int:
    """把风格后缀确定性地补进资产卡参考提示词，返回被改写的条目数。

    bible 提示词里虽然要求模型自带风格词，但模型经常漏（实测三个资产全部漏掉），
    因此这里做一次**不依赖模型自觉**的兜底补写 —— 提示词生成链路的最后一道保险。
    """
    norm = normalize_style(style)
    if not norm or not assets:
        return 0
    changed = 0
    for a in assets:
        if not isinstance(a, dict):
            continue
        base = a.get(key) or a.get("appearance") or ""
        merged = with_style(base, norm)
        if merged and merged != str(base or "").strip():
            a[key] = merged
            changed += 1
    return changed
