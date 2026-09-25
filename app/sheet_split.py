# -*- coding: utf-8 -*-
"""角色「分档设定图」→ 各视角单图：本地投影切分（零 GPU、零质检）。

背景（2026-09-24 改造，勿回退到「多视角编辑」）
--------------------------------------------------------------------------------
角色资产原本有两类图：

  · ``base.png`` —— 角色设定图的**整图**（一次 T2I 出图）。
  · ``front/left/right/back.png`` —— 由「多视角编辑」工作流
    （``分镜生成_Qwen21.json``）逐视角**重渲染**得到。

实测（``output/assets/characters/逆天系统/*/``，2026-09-24）：4 张「视角图」与
``base.png`` **内容完全一致**，都是同一张设定图 —— 因为参考图编辑在该工作流下
KSampler ``cfg=1.0`` → ``uncond_ = None``（负向根本不评估、无引导放大）→
参考图条件压过文字，只会 1:1 复刻参考图里**已可见**的机位，不会凭空补全没见过的面
（机制与 8 组对照实验见 ``config.MULTIVIEW_CONFIG`` 顶部注释）。

后果（2026-09-24 改造消掉的三笔账）：
  1. 每个角色白烧 **4 次 GPU 渲染 + 4 次质检**（物品/场景同理）；
  2. 多视角不达标会把**整个资产判为 failed**（旧 ``success = not blocked_views``），
     即使基础图已经达标；
  3. 下游（``app._build_asset_index`` / ``app._collect_asset_refs``）**只取
     ``front.png``（缺则 ``base.png``）**，``left/right/back`` 零消费 —— 4 张产物纯属浪费。

本模块的定位
--------------------------------------------------------------------------------
把「单视角图」从「GPU 重渲染」降级为「从整图本地切分」：整图本来就是**一次** T2I 出图，
各格位共享同一角色，切出来天然是同一人物的不同机位，零 GPU 成本、零质检成本，
且**不引入身份漂移**。于是同时拿到两样东西：

  · 「整图」= ``base.png``（1 张，用于人工审阅 / 资产预览）
  · 「单图」= ``front.png`` / ``left.png`` / ``back.png`` / ``half.png``

⚠️ 2026-09-25：版式由「三张全身横排」升级为 **2x2 分档**
--------------------------------------------------------------------------------
根因（实测，见 ``.workbuddy/tools/diag_framing_control.py``）：
分镜模板 ``<image1>`` 是主画布，近方形**全身**立绘 cover 到 9:16 竖屏要左右各裁一半
→ 模型为保住「完整的全身」只能把人物缩小 → **系统性偏全景/远景**，
而近景/特写与立绘方向相反 → 被反向拉回、画不出来（实测 shot_24 规定近景、出图近全身）。
业界通行做法是「**按景别分档出图**」（正脸特写 / 正脸半身 / 全身）。

新版式（``config.CHARACTER_SHEET_CELLS``）：

      ┌─────────┬─────────┬─────────┐
      │ 正面全身 │ 左侧全身 │ 背面全身 │   上排 3 格
      ├─────────┼─────────┴─────────┤
      │ 正面半身 │      （留白）      │   下排仅左 1 格
      └─────────┴───────────────────┘

半身格给「近景/特写/中景」镜头当主画布，画幅与景别**同向**，画幅对抗即消失。

⚠️ 为什么不用硬切分
    实测人物外接框**不落在等分线上**。以「羡进」为例，列投影得到的三人段是
    ``(25,256) / (290,437) / (474,706)``，而硬三等分边界 ``x = 245 / 490`` 处**有内容**
    —— 等分切法会切到人物的手臂。故 3 格横排用**列投影找空隙 + 取相邻段中缝**；
    2 行版式再用**行投影**定上下排的水平切分线（空隙中缝），两向都不碰内容。

⚠️ 为什么切好要贴回「与原整图同尺寸的方底画布」
    ``分镜生成_Qwen21.json`` 是 ``TextEncodeQwenImage21(latent) → KSampler(latent_image)``
    的参考图编辑模板，**没有尺寸节点**（无 ``ResolutionSelector`` / ``EmptyLatentImage``），
    分镜图的输出画幅**继承第一张参考图**（见 ``comfyui_client.generate_storyboard`` 的日志分支）。
    若把切出的窄条（如 231×736）直接当参考图，分镜图会被带成 231:736 的怪画幅。
    故每张单图都居中贴在**与整图同尺寸**的纯白画布上 —— 画幅口径不变。
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
    """把设定图切成 ``expected`` 张单视角图（**旧版式：单行横排**）。

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
            f"设定图列投影得到 {len(segs)} 段，与期望的 {expected} 段不符"
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


def row_segments(mask) -> List[Tuple[int, int]]:
    """按**行**投影切段，返回 [(y0, y1), ...]（上闭下开），按 y 升序。

    与 :func:`column_segments` 对偶，供 2x2 版式定「上下排」的水平切分线。
    """
    import numpy as np

    rows = np.asarray(mask).sum(axis=1)
    occupied = rows >= MIN_OCC_PX
    segs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    gap = 0
    for y, occ in enumerate(occupied):
        if occ:
            if start is None:
                start = y
            gap = 0
            continue
        if start is None:
            continue
        gap += 1
        if gap >= MIN_GAP_PX:
            segs.append((start, y - gap + 1))
            start = None
            gap = 0
    if start is not None:
        segs.append((start, len(occupied)))
    return [(a, b) for a, b in segs if b - a >= MIN_SEG_W]


def split_grid_sheet(image, cells: Sequence[Tuple[int, int]],
                     grid: Tuple[int, int], pad_to_canvas: bool = True,
                     band_fallback: Optional[float] = None):
    """把**多层（上排 N 格 + 下排 M 格）**设定图按 ``cells`` 切出各格内容。

    与 :func:`split_sheet`（单行横排）的区别：多层版式里同一列上下都有内容，
    只做列投影会把上下两排**粘成一段**（实测确认），故必须**先行后列**：
      1. 行投影 → 找到「上排 / 下排」之间的水平空白带 → 切成 row_count 条横带；
      2. 每条横带内再列投影 → 按该行的实际格数切竖格。

    :param cells: ``[(row, col), ...]``，与目标视角键一一对应，0 起。
    :param grid: ``(rows, cols)`` 版式网格，用于校验。
    :param band_fallback: 行投影失败时，用「最后一排所占高度比例」做**确定性兜底**
        （``config.CHARACTER_SHEET_HALF_BAND`` 同源）。设 None 则直接抛错。
    :returns: ``[PIL.Image, ...]``，顺序与 ``cells`` 一致
    :raises SheetSplitError: 行/列投影段数不可信且无兜底时抛错（调用方降级）
    """
    import numpy as np
    from PIL import Image

    im = image.convert("RGB") if image.mode != "RGB" else image
    W, H = im.size
    mask = _content_mask(np.asarray(im, dtype=np.int16))
    rows_n = int(grid[0])

    rsegs = row_segments(mask)
    if len(rsegs) == rows_n:
        rpts = cut_points(rsegs, H)
    elif band_fallback and rows_n == 2:
        # 兜底：上排(全身)占 1-band，下排(半身)占 band。取上排底边作为唯一分界线。
        split_y = int(round(H * (1.0 - float(band_fallback))))
        rpts = [0, split_y, H]
    else:
        raise SheetSplitError(
            f"设定图行投影得到 {len(rsegs)} 段，与版式 {rows_n} 行不符"
            f"（段={rsegs}，画布 {W}x{H}）—— 不做不可靠切分")

    pieces: List[object] = []
    for r in range(rows_n):
        band = im.crop((0, rpts[r], W, rpts[r + 1]))
        band_mask = mask[rpts[r]:rpts[r + 1], :]
        csegs = column_segments(band_mask)
        want = [c for (rr, c) in cells if rr == r]
        if not want:
            continue
        need = max(want) + 1
        if len(csegs) < need:
            raise SheetSplitError(
                f"设定图第 {r + 1} 行列投影得到 {len(csegs)} 段，"
                f"少于该行需要的 {need} 格（段={csegs}）—— 不做不可靠切分")
        # 只用该行实际需要的列段（多余段是噪声/装饰，忽略尾部）
        cpts = cut_points(csegs[:need], W)
        for c in want:
            pieces.append(band.crop((cpts[c], 0, cpts[c + 1], band.height)))

    if len(pieces) != len(cells):
        raise SheetSplitError(
            f"设定图切出 {len(pieces)} 格，与请求的 {len(cells)} 格不符")

    if not pad_to_canvas:
        return pieces
    out = []
    for piece in pieces:
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        left = max(0, (W - piece.width) // 2)
        top = max(0, (H - piece.height) // 2)
        canvas.paste(piece, (left, top))
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
                         logger=None, prune: bool = True,
                         cells: Optional[Sequence[Tuple[int, int]]] = None,
                         grid: Optional[Tuple[int, int]] = None,
                         band_fallback: Optional[float] = None) -> Dict[str, str]:
    """把整图 ``src_path`` 切成 ``keys`` 指定的各视角单图并落盘到 ``out_dir``。

    :param cells: 各视角键对应的 ``(行, 列)`` 格位（0 起）。**给了就走多层网格切分**
        （:func:`split_grid_sheet`，行投影 + 行内列投影）；不给则退回单行横排切分
        （:func:`split_sheet`，列投影），保证对旧版式的 1:1 兼容。
    :param grid: ``(rows, cols)``，仅在给了 ``cells`` 时使用。
    :param band_fallback: 行投影失败时的确定性兜底比例（见 :func:`split_grid_sheet`）。
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
        if cells and grid and len(cells) == len(keys):
            pieces = split_grid_sheet(raw, cells, grid, band_fallback=band_fallback)
        else:
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
