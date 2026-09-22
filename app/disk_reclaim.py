# -*- coding: utf-8 -*-
"""D-11a（P2）ComfyUI 输出目录滚动回收 —— 纯判定逻辑（零第三方依赖，可离线单测）

对应缺陷
--------
全项目缺陷审计报告 **D-11**（原报告 C-5「ComfyUI 输出目录回收」）：
`COMFYUI_OUTPUT_DIR`（见 ``app/config.py:65``，由环境变量决定）下的重试产物
**只增不减** —— 历史事故：暂存区膨胀到 1.1GB、另有一次 80GB 写满 C 盘。
现有清理函数 ``app/app.py _qc_prune_attempts`` 只覆盖
``output/qc/<项目>/{assets,storyboard,video}_scratch/`` 下的 ``*_tryN``，
**作用域不含 ComfyUI 的 output 目录**。

本模块把「滚动清理」推广到 ``COMFYUI_OUTPUT_DIR`` 内、由 ``filename_prefix``
决定的重试产物（``comic_drama/…`` / ``comic_drama_sb/…`` /
``comic_drama_retry/…`` / ``comic_drama_kf/…``，重试件文件名带 ``_retry``）。

为什么这样判定是安全的
----------------------
这是**删文件**的代码，安全优先于回收率。只有**同时**满足以下全部条件才允许删：

1. 路径确实位于 ``COMFYUI_OUTPUT_DIR`` 之内（由调用方传入 ``comfy_root``）；
2. 文件名 stem 匹配重试产物命名（含 ``_retry`` 或 ``_try``）——
   正式交付名（``base.png`` / ``shot_NN.png`` 等）天然不含这两个标记；
3. ``mtime`` 距今 **> 24 小时**（阈值参数化 ``min_age_sec=86400``）——
   给「刚生成、正在被后续环节读取」的产物留足缓冲；
4. 在「正式产物目录」中能找到**同内容副本** —— 判定用 ``(size, sha256)``
   **双匹配**，绝不只看 size（同名不同内容的撞车会造成误删）；
5. ``os.stat().st_nlink == 1``：**硬链接直接跳过** —— 删了不释放空间，
   且说明它与正式产物共享同一 inode。本项目有过「把硬链接误判成重复副本」的
   教训（见仓库根 ``ComfyUI冗余资源扫描报告.md`` 的「⚠️ 一处修正」段：
   ``models/vae/MiniMax`` 与 ``models/vae/minimax-h3`` 是同一 inode 的硬链接，
   删任何一方都不释放空间）；
6. 候选文件**不在任何「正式产物目录」之内** —— 防御性断言，
   宁可漏删（少回收一点空间）也绝不误删正式交付物。

哪些情况会被跳过
----------------
- ``skip["recent"]``    ：``mtime`` 距今 ≤ 阈值（默认 24h），太新不动；
- ``skip["no_copy"]``   ：正式产物目录里找不到 ``(size, sha256)`` 完全一致的副本；
- ``skip["hardlink"]``  ：``st_nlink != 1``（硬链接 / 别名，删了不释放空间）；
- ``skip["unreadable"]``：stat / 读取失败（被占用、权限不足、条目结构异常）。

清理绝不向上抛异常
------------------
回收是**优化**而非功能：:func:`reclaim_comfyui_output` 全程 try/except，
任何异常只经 ``logger.warning`` / ``logger.debug`` 留痕，绝不阻断生产。
（遵守 D-09 静默吞异常整治口径：不新增 ``except: pass``，异常一律绑定对象。）

运行环境
--------
仅依赖标准库（``hashlib`` / ``logging`` / ``os`` / ``time``），
测试环境无 flask / requests / PIL 也能被
``MJSCXT_AUTOPILOT=0 python verify_comfyui_reclaim.py`` 直接 import 跑通。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

__all__ = [
    "DEFAULT_MIN_AGE_SEC",
    "RETRY_MARKERS",
    "is_retry_artifact",
    "official_fingerprints",
    "plan_reclaim",
    "reclaim_comfyui_output",
]

#: 回收的最小「年龄」：``mtime`` 距今必须超过该秒数（默认 24 小时）。
DEFAULT_MIN_AGE_SEC = 86400

#: 重试产物的命名标记。ComfyUI 的产物名形如
#: ``{filename_prefix}_{counter:05}_.png``，其中 filename_prefix 带 ``_retry``
#: （单镜重跑）或 ``_try``（关键帧链路）时，文件名 stem 里就会出现该标记。
RETRY_MARKERS = ("_retry", "_try")

#: 计算内容指纹时的分块大小（1 MiB）—— 避免把大图整块读进内存。
_CHUNK = 1 << 20

#: ``plan_reclaim`` 的跳过计数键（返回值结构对调用方是稳定契约）。
_SKIP_KEYS = ("recent", "no_copy", "hardlink", "unreadable")


def _sha256(path: str) -> str:
    """流式计算文件内容 sha256（调用方需自行捕获 ``OSError``）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def is_retry_artifact(name: str) -> bool:
    """文件名（stem）是否匹配「重试产物」命名（含 ``_retry`` 或 ``_try``）。

    ``is_retry_artifact("proj_shot_01_retry_00001_.png") -> True``
    ``is_retry_artifact("proj_shot_01_00001_.png")       -> False``
    ``is_retry_artifact("base.png")                      -> False``

    只看 stem（去掉扩展名），不看目录名 —— 目录名可能含 ``retry`` 等巧合词，
    正式交付目录里的文件不应据此被判定为临时产物。
    """
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    if not stem:
        return False
    return any(marker in stem for marker in RETRY_MARKERS)


def _is_within(path: str, roots) -> bool:
    """``path`` 是否位于 ``roots`` 中任一目录之内（防御性断言用）。"""
    try:
        target = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            if os.path.commonpath([target, root]) == root:
                return True
        except (OSError, ValueError):
            continue
    return False


def official_fingerprints(root_dirs) -> set:
    """扫描「正式产物目录」，返回 ``(size, sha256)`` 指纹集合。

    ``root_dirs`` 由调用方从 config 常量推导（如 ``CHARACTERS_DIR`` /
    ``ITEMS_DIR`` / ``SCENES_DIR`` / ``STORYBOARDS_DIR`` / ``KEYFRAMES_DIR`` /
    ``VIDEOS_DIR`` / ``FINAL_DIR``），**不硬编码任何盘符路径**。

    读不了 / stat 不了的文件直接跳过（不影响其它文件），
    因此本函数不会因个别坏文件而抛异常。
    """
    fps: set = set()
    for root in (root_dirs or []):
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                    fps.add((int(st.st_size), _sha256(p)))
                except OSError:
                    continue
                except Exception as e:  # noqa: BLE001 指纹扫描是旁路，坏文件不该中断整体
                    logging.getLogger(__name__).debug(
                        "D-11a 指纹扫描跳过异常文件 %s：%s: %s", p, type(e).__name__, e)
                    continue
    return fps


def plan_reclaim(candidates, official_fps, now, min_age_sec: int = DEFAULT_MIN_AGE_SEC) -> dict:
    """**纯判定**：给定候选文件 stat 与正式产物指纹，决定删哪些、跳过哪些（不落盘）。

    ``candidates``：``[(path, st_size, st_mtime, st_nlink), ...]``
    —— 由调用方 stat 后传入，便于单测打桩（无需真实文件即可测判定分支）。

    ``official_fps``：:func:`official_fingerprints` 的结果（``(size, sha256)`` 集合）。

    ``now``：当前时间戳（秒，由调用方注入，测试可自由指定）。

    返回 ``{"delete": [path, ...],
            "skip": {"recent": n, "no_copy": n, "hardlink": n, "unreadable": n}}``
    —— ``delete`` 为「已确认可删」的路径列表（真正删除由调用方执行）。
    """
    skip = {k: 0 for k in _SKIP_KEYS}
    delete: list = []
    fps = official_fps or set()
    for item in (candidates or []):
        try:
            path, size, mtime, nlink = item
        except (TypeError, ValueError):
            skip["unreadable"] += 1
            continue

        # 条件 5：硬链接直接跳过 —— 删了不释放空间，且与正式产物共享同一 inode
        try:
            if int(nlink) != 1:
                skip["hardlink"] += 1
                continue
        except (TypeError, ValueError):
            skip["unreadable"] += 1
            continue

        # 条件 3：mtime 距今必须 > 阈值（太新不动）
        try:
            age = float(now) - float(mtime)
        except (TypeError, ValueError):
            skip["unreadable"] += 1
            continue
        if age <= float(min_age_sec):
            skip["recent"] += 1
            continue

        # 条件 4：正式产物目录里必须能找到 (size, sha256) 双匹配的同内容副本
        try:
            digest = _sha256(path)
        except OSError:
            skip["unreadable"] += 1
            continue
        except Exception as e:  # noqa: BLE001 读取异常一律按「不可读」跳过，保守不删
            logging.getLogger(__name__).debug(
                "D-11a 读候选失败，保守跳过 %s：%s: %s", path, type(e).__name__, e)
            skip["unreadable"] += 1
            continue
        if (int(size), digest) not in fps:
            skip["no_copy"] += 1
            continue

        delete.append(path)
    return {"delete": delete, "skip": skip}


def reclaim_comfyui_output(comfy_root: str, official_dirs, min_age_sec: int = DEFAULT_MIN_AGE_SEC,
                           logger=None) -> dict:
    """落地执行：扫描 ``comfy_root`` 下的重试残留并按 :func:`plan_reclaim` 删除。

    返回与 :func:`plan_reclaim` 同构的统计，另加：

    - ``removed_bytes``：实际删除的字节数（失败的不计）；
    - ``failed``       ：删除失败的个数（被占用 / 权限不足，逐个跳过不中断）；
    - ``scanned``      ：命中的候选文件数（重试命名且不在正式目录内）。

    **绝不向上抛异常**：任何异常只经 ``logger`` 留痕，函数始终返回统计字典。
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    stats = {
        "delete": [],
        "skip": {k: 0 for k in _SKIP_KEYS},
        "removed_bytes": 0,
        "failed": 0,
        "scanned": 0,
    }
    try:
        if not comfy_root or not os.path.isdir(comfy_root):
            return stats

        official_dirs = [d for d in (official_dirs or []) if d]
        official_abs = [os.path.abspath(d) for d in official_dirs]
        official_fps = official_fingerprints(official_dirs)
        now = time.time()

        candidates = []
        for dirpath, _dirnames, filenames in os.walk(comfy_root):
            # 条件 6：候选绝不允许落在任何「正式产物目录」之内（防御性断言）
            if _is_within(dirpath, official_abs):
                continue
            for fn in filenames:
                # 条件 2：文件名 stem 必须匹配重试产物命名
                if not is_retry_artifact(fn):
                    continue
                p = os.path.join(dirpath, fn)
                if _is_within(p, official_abs):
                    continue
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                except OSError:
                    continue
                candidates.append(
                    (p, int(st.st_size), float(st.st_mtime), int(st.st_nlink)))

        stats["scanned"] = len(candidates)
        plan = plan_reclaim(candidates, official_fps, now, min_age_sec)
        stats["skip"] = plan["skip"]

        for p in plan["delete"]:
            try:
                size = os.path.getsize(p)
                os.remove(p)
                stats["delete"].append(p)
                stats["removed_bytes"] += size
            except OSError as e:
                # 单文件失败（被占用 / 权限）绝不中断整批清理
                stats["failed"] += 1
                log.debug("D-11a 删除重试残留失败（跳过）：%s: %s", p, e)

        if stats["delete"]:
            log.info("D-11a ComfyUI 输出回收：删 %d/%d 个重试残留，释放 %.2f MB",
                     len(stats["delete"]), stats["scanned"],
                     stats["removed_bytes"] / 1048576.0)
    except Exception as e:  # noqa: BLE001 回收是优化，绝不能阻断生产
        log.warning("D-11a ComfyUI 输出回收失败（不影响生产）：%s: %s",
                    type(e).__name__, e)
    return stats
