# -*- coding: utf-8 -*-
"""角色「三视图整图」→ 各视角单图：本地列投影切分（零 GPU、零质检）。

背景（2026-09-24 改造，勿回退到「多视角编辑」）
--------------------------------------------------------------------------------
角色资产原本有两类图：

  · ``base.png`` —— 「正面 / 左侧面 / 背面 三张全身视图横排」的**整图**。
    由 T2I（``角色生成_Qwen21.json``）一次出图，画幅 1:1 的由来就是它（见
    ``style_kit.ASSET_BASE_RATIO`` 的实测注释）。
  · ``front/left/right/back.png`` —— 由「多视角编辑」工作流
    （``分镜生成_Qwen21.json``）逐视角**重渲染**得到。

实测（``output/assets/characters/逆天系统/*/``，2026-09-24）：4 张「视角图」与
``base.png`` **内容完全一致**，都是同一张三视图整图 —— 因为参考图编辑在该工作流下
KSampler ``cfg=1.0`` → ``uncond_ = None``（负向根本不评估、无引导放大）→
参考图条件压过文字，只会 1:1 复刻参考图里**已可见**的机位，不会凭空补全没见过的面
（机制与 8 组对照实验见 ``config.MULTIVIEW_CONFIG`` 顶部注释）。

后果（本次改造要消掉的三笔账）：
  1. 每个角色白烧 **4 次 GPU 渲染 + 4 次质检**（物品/场景同理）；
  2. 多视角不达标会把**整个资产判为 failed**（旧 ``success = not blocked_views``），
     即使基础图已经达标；
  3. 下游（``app._build_asset_index`` / ``app._collect_asset_refs``）**只取
     ``front.png``（缺则 ``base.png``）**，``left/right/back`` 零消费 —— 4 张产物纯属浪费。

本模块的定位
--------------------------------------------------------------------------------
把「单视角图」从「GPU 重渲染」降级为「从整图本地切分」：整图本来就是**一次** T2I 出图，
三个格位共享同一角色，切出来天然是同一人物的三个**真机位**，零 GPU 成本、零质检成本，
且**不引入身份漂移**。于是同时拿到用户要的两样东西：

  · 「整图」= ``base.png``（1 张，三格横排，用于人工审阅 / 资产预览）
  · 「单图」= ``front.png`` / ``left.png`` / ``back.png``（各 1 张真单机位，供下游当参考图）

⚠️ 为什么不用硬三等分
    实测三张整图的人物外接框**不落在等分线上**。以「羡进」为例，列投影得到的三人段是
    ``(25,256) / (290,437) / (474,706)``，而硬三等分边界 ``x = 245 / 490`` 处**有内容**
    —— 等分切法会切到人物的手臂。这里改用**列投影找空隙 + 取相邻段中缝**切分；
    三张实测图都能得到 3 段，段间空隙 12~54px、外侧留白 8~43px。

⚠️ 为什么切好要贴回「与原整图同尺寸的方底画布」
    ``分镜生成_Qwen21.json`` 是 ``TextEncodeQwenImage21(latent) → KSampler(latent_image)``
    的参考图编辑模板，**没有尺寸节点**（无 ``ResolutionSelector`` / ``EmptyLatentImage``），
    分镜图的输出画幅**继承第一张参考图**（见 ``comfyui_client.generate_storyboard`` 的日志分支）。
    若把切出的窄条（如 231×736）直接当参考图，分镜图会被带成 231:736 的怪画幅。
    故每张单图都居中贴在**与整图同尺寸**的纯白画布上 —— 人物在画面中的相对占比与
    原整图一致（整图里每人也只占约 1/3 宽），画幅口径不变。
"""
from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: 背景判定容差（与四角中位色的最大通道差）。整图为纯白背景，但 PNG 仍有极浅噪声，
#: 容差过小会把背景噪声当成「内容」→ 列投影切不出空隙（实测 tol=12 时三张图都只剩 1 段）。
BG_TOL = 24
#: 一列至少要有这么多「内容像素」才算被人物占用（滤掉零星噪声列）。
MIN_OCC_PX = 3
#: 相邻人物之间的最小空隙列数，达到即认为可分。
MIN_GAP_PX = 5
#: 切出的单段最小宽度，过窄视为误检。
MIN_SEG_W = 8


class SheetSplitError(RuntimeError):
    """整图无法按期望格位切分（格位数不符 / 无可用背景分离）。调用方应降级。"""


def _content_mask(rgb):
    """返回 (H, W) 的 bool 掩码：True = 人物（非背景）。

    背景色取**四角中位色**（比单角更稳：个别整图角落会有一点阴影/脏点）。
    """
    import numpy as np

    corners = np.stack([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]])
    bg = np.median(corners, axis=0)
    diff = np.abs(rgb - bg).max(axis=2)
    return diff > BG_TOL


def column_segments(mask) -> List[Tuple[int, int]]:
    """按列投影切段，返回 [(x0, x1), ...]（左闭右开），按 x 升序。"""
    import numpy as np

    cols = np.asarray(mask).sum(axis=0)
    occupied = cols >= MIN_OCC_PX
    segs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    gap = 0
    for x, occ in enumerate(occupied):
        if occ:
            if start is None:
                start = x
            gap = 0
            continue
        if start is None:
            continue
        gap += 1
        if gap >= MIN_GAP_PX:
            segs.append((start, x - gap + 1))
            start = None
            gap = 0
    if start is not None:
        segs.append((start, len(occupied)))
    return [(a, b) for a, b in segs if b - a >= MIN_SEG_W]


def cut_points(segs: Sequence[Tuple[int, int]], width: int) -> List[int]:
    """由相邻段的中缝得到切分点（含 0 与 width），保证每段保留自己的留白。"""
    pts = [0]
    for i in range(len(segs) - 1):
        pts.append((segs[i][1] + segs[i + 1][0]) // 2)
    pts.append(int(width))
    return pts


def split_sheet(image, expected: int, pad_to_canvas: bool = True):
    """把三视图整图切成 ``expected`` 张单视角图。

    :param image: ``PIL.Image``（RGB）
    :param expected: 期望格位数量（与 ``config.CHARACTER_SHEET_VIEWS`` 等长）
    :param pad_to_canvas: True → 每张单图居中贴回与整图**同尺寸**的纯白画布
        （保持画幅口径，见模块头注释）；False → 直接返回裁切件
    :returns: ``[PIL.Image, ...]``，长度为 ``expected``，**从左到右**对应格位顺序
    :raises SheetSplitError: 列投影段数与 ``expected`` 不符（不可靠切分，调用方降级）
    """
    import numpy as np
    from PIL import Image

    im = image.convert("RGB") if image.mode != "RGB" else image
    W, H = im.size
    rgb = np.asarray(im, dtype=np.int16)
    segs = column_segments(_content_mask(rgb))
    if len(segs) != expected:
        raise SheetSplitError(
            f"三视图整图列投影得到 {len(segs)} 段，与期望的 {expected} 段不符"
            f"（段={segs}，画布 {W}x{H}）—— 不做不可靠切分")

    pts = cut_points(segs, W)
    out = []
    for i in range(expected):
        piece = im.crop((pts[i], 0, pts[i + 1], H))
        if not pad_to_canvas:
            out.append(piece)
            continue
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        left = max(0, (W - piece.width) // 2)
        canvas.paste(piece, (left, 0))
        out.append(canvas)
    return out


def prune_stale_views(asset_dir: str, keep: Iterable[str],
                      known: Sequence[str] = ("front", "left", "right", "back"),
                      logger=None) -> List[str]:
    """删除资产目录里**已不再产出**的视角图及其旁路元数据（只动白名单内的固定名）。

    典型场景：旧实现会产出 ``right.png``（右侧半侧面），新实现只切 ``front/left/back``
    —— 不清理的话，UI 会把这张陈旧的「重渲染整图」继续当成一个视角展示。
    只删 ``known`` 里的固定名（不递归、不匹配通配符），返回被删文件名列表。
    """
    keep_set = {str(k) for k in keep}
    removed: List[str] = []
    if not os.path.isdir(asset_dir):
        return removed
    for stem in known:
        if stem in keep_set:
            continue
        for fn in (f"{stem}.png", f"{stem}.meta.json"):
            p = os.path.join(asset_dir, fn)
            if not os.path.isfile(p):
                continue
            try:
                os.remove(p)
                removed.append(fn)
            except OSError as e:  # 清理失败不影响主流程，但要留痕
                if logger:
                    logger.warning("清理陈旧视角文件失败：%s（%s）", p, e)
    if removed and logger:
        logger.info("已清理陈旧视角文件 %d 个：%s", len(removed), "、".join(removed))
    return removed


def split_sheet_to_files(src_path: str, out_dir: str, keys: Sequence[str],
                         logger=None, prune: bool = True) -> Dict[str, str]:
    """把整图 ``src_path`` 切成 ``keys`` 指定的各视角单图并落盘到 ``out_dir``。

    :returns: ``{view_key: 绝对路径}``（全部成功才返回；任一步失败即抛 ``SheetSplitError``）
    """
    from PIL import Image

    if not os.path.isfile(src_path):
        raise SheetSplitError(f"整图不存在：{src_path}")
    keys = [str(k) for k in keys]
    if not keys:
        return {}
    os.makedirs(out_dir, exist_ok=True)
    with Image.open(src_path) as raw:
        pieces = split_sheet(raw, len(keys))

    out: Dict[str, str] = {}
    try:
        for key, piece in zip(keys, pieces):
            dst = os.path.join(out_dir, f"{key}.png")
            tmp = os.path.join(out_dir, f".tmp_{key}.png")
            piece.save(tmp, format="PNG")
            os.replace(tmp, dst)
            out[key] = dst
    except Exception:
        for p in list(out.values()):
            try:
                os.remove(p)
            except OSError:
                pass
        raise

    if logger:
        logger.info("三视图整图已切分为 %d 张单视角图：%s（源 %s）",
                    len(out), "、".join(keys), os.path.basename(src_path))
    if prune:
        prune_stale_views(out_dir, keep=keys, logger=logger)
    return out
