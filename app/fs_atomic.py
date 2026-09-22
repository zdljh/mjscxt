# -*- coding: utf-8 -*-
"""共享「原子落盘 / fail-loud 读取」工具（P1 A-3 / A-4）

问题（审计 A-3）
--------------
多处状态/索引文件仍沿用**固定临时名**写法：

    tmp = path + ".tmp"                    # ← 固定名，多线程/多进程共用同一临时文件
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

`os.replace` 本身原子，但两个写者共用同一个 `.tmp`：A 写了一半、B 以 `"w"` 截断重写,
A 再 `os.replace` → 发布出去的是**交错/截断的 JSON**。而对应的读取要么不看内容
（`_nonempty` 只判大小 > 0），要么 `except Exception: return default` —— 于是
「文件被写坏」被静默读成「空」，下游「读改写」再基于空快照写回 → **数据被永久清空**。

修复（A-3）
----------
1. `atomic_write_json`：临时名唯一化 `{path}.{pid}.{tid}.{urandom}.tmp`，
   `json.dump → flush → fsync → os.replace`（同分区 rename 原子）；
   `os.replace` 对 Windows 共享冲突做退避重试；
2. 发布前先把「当前**可解析**的活文件」快照到 `{path}.bak`（`last_good_bak`），
   语义与 `project_store._snapshot_bak` 完全一致 —— 活文件已损坏时**绝不**把
   损坏内容盖到 `.bak` 上（否则连唯一可恢复来源都丢）；
3. 任何失败：尽力清理唯一临时文件后**原样抛出**，绝不静默吞掉。

修复（A-4）
----------
`read_json_strict` 是**三态**读取器，取代「解析失败就返默认值」的静默降级：
  * 文件不存在  → 返回 `default`（首次运行/全新部署的正常初始态）；
  * 解析成功    → 返回内容；
  * 解析失败    → 先尝试 `{path}.bak`（最后一份好版本）并原子写回活文件；
                  无可用 `.bak` 时 `logger.error` 后**抛错 fail-loud** ——
                  绝不把「损坏」读成「空」再被下游写回。
另外对 Windows 上 `os.replace` 换目录项的**瞬时**共享冲突做退避重试；
「文件存在但读不到」同样 fail-loud，不与「文件不存在」混为一谈。

⚠️ 本模块只依赖标准库，可被任意业务模块顶层 import，无循环导入风险。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time

logger = logging.getLogger(__name__)

__all__ = ["atomic_write_json", "read_json_strict", "last_good_bak"]


# =====================================================================
# 备份：最后一份「可解析的好版本」
# =====================================================================

def last_good_bak(path: str) -> str:
    """数据文件「上一份可解析好版本」备份路径：``path + '.bak'``。

    与 `project_store._last_good_bak` 保持一致 —— 活文件损坏时可从这里恢复。
    """
    return path + ".bak"


def _snapshot_bak(path: str) -> None:
    """发布前快照：若活文件当前**可解析**，复制到 `.bak`（保留最后一次好版本）。

    只对「可解析」的活文件做快照 —— 若活文件已损坏，绝不把它当好版本盖到
    `.bak` 上（否则连唯一可恢复的来源都丢）。语义与
    `project_store._snapshot_bak` 完全一致。
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError) as e:
        # 活文件已不可解析 → 不覆盖既有 .bak。这是**预期分支**（不是故障），
        # 但必须留痕：否则「为什么 .bak 一直停在旧版本」将无法追查。
        # D-09 口径：清理/旁路类失败一律 logger.debug，不静默吞。
        # 级别说明（C4-2）：**本分支是预期分支**（活文件本就不可解析，不该把坏内容盖到
        # 好版本上）故用 debug；下方「.bak 写失败」属可恢复性降级，故用 warning ——
        # 两处级别不同是有意为之，不是遗漏。
        logger.debug("跳过 .bak 快照（活文件不可解析）：%s: %s", type(e).__name__, e)
        return
    try:
        shutil.copy2(path, last_good_bak(path))
    except OSError as e:
        # C4-2（2026-09-22 复验）：级别对齐 project_store._snapshot_bak
        # （app/project_store.py:139 用 logger.warning）。.bak 快照是**可恢复性**保障，
        # 写失败意味着下次损坏无法自愈；生产 root 级别是 WARNING，用 debug 会完全不可见；
        # 且这不是逐帧热路径（仅快照失败时触发）。
        logger.warning("写 .bak 快照失败（忽略，损坏时退化为无法恢复）：%s", e)


def _restore_from_bak(path: str):
    """活文件损坏/丢失时，尝试从 `.bak` 恢复并返回其内容。

    恢复成功会把 `.bak` 内容**原子写回**活文件，返回解析后的数据；
    `.bak` 不存在或也不可解析时返回 `None`（调用方据此决定 fail-loud 还是降级）。
    """
    bak = last_good_bak(path)
    if not os.path.isfile(bak):
        return None
    try:
        with open(bak, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        atomic_write_json(path, data)
    except Exception as e:  # noqa: BLE001  写回失败不影响「已拿到好数据」这一事实
        logger.error("从 .bak 恢复 %s 时写回失败（内存中已恢复，可继续）：%s",
                     os.path.basename(path), e)
    return data


# =====================================================================
# 原子写
# =====================================================================

def _atomic_replace(tmp: str, path: str) -> None:
    """`os.replace` + Windows 共享冲突退避重试（与 project_store 同策略）。

    Windows 上若目标文件正被另一线程/进程打开读取（例如刚读完、句柄尚未释放），
    `os.replace` 会抛 `PermissionError: [WinError 5] 拒绝访问`。这是「暂时拿不到」
    而非「文件坏了」，退避重试即可；重试耗尽才抛出。
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


def atomic_write_json(path: str, data, *, indent: int = 2) -> None:
    """原子写 JSON：唯一临时名 + flush/fsync + 快照 .bak + os.replace 重试。

    参数:
        path: 目标文件路径（父目录会自动创建）；
        data: 可 JSON 序列化对象；
        indent: 缩进，默认 2（与既有落盘格式一致）。

    失败语义：任何异常都会先尽力删除唯一临时文件，然后**原样抛出**。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # 发布前快照「当前可解析的好版本」，损坏时才能从 .bak 恢复
    _snapshot_bak(path)
    # ⚠️ 临时名必须**每次唯一**：固定 `.tmp` 会被并发写者互相截断
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())        # 先落盘再 replace，避免断电后只剩空文件
        _atomic_replace(tmp, path)      # 同分区 replace 原子 + Windows 占用重试
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)          # 失败不留垃圾临时文件
        except OSError as e:
            # 清理失败不能掩盖原始异常（下面照常 raise），但也不能完全静默 —— 否则
            # 磁盘会被看不见的唯一名 .tmp 垃圾慢慢吃满。D-09 口径：清理类 → debug。
            logger.debug("清理临时文件失败（忽略）：%s", e)
        raise


# =====================================================================
# 三态严格读取
# =====================================================================

def read_json_strict(path: str, default):
    """读 JSON：**三态**语义 —— 缺失返默认 / 正常返内容 / 损坏 .bak-or-raise。

    * 文件不存在 → 返回 `default`；但若 `.bak` 存在（活文件被误删）则从 `.bak`
      恢复最后一份好版本（而不是静默返回空，避免下游「读改写」把空快照写回）；
    * 解析成功 → 返回内容；
    * 解析失败（`ValueError`）→ 尝试 `.bak`：可解析则 `logger.error` 提示后
      **原子写回活文件**并返回；`.bak` 也没有/不可解析 → `logger.error` 后**抛出**，
      拒绝静默清空；
    * `PermissionError`（Windows 瞬时共享冲突）→ 退避重试 6 轮；
    * 文件存在但读取失败（非瞬时）→ `logger.error` 后**抛出**（fail-loud）。
    """
    for i in range(6):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            # 活文件不存在 = 正常初始态；但 .bak 还在说明「被误删」→ 恢复
            restored = _restore_from_bak(path)
            if restored is not None:
                logger.error("读取 %s：活文件不存在但存在 .bak，已从 .bak 恢复最后一份好版本",
                             os.path.basename(path))
                return restored
            return default
        except PermissionError:
            # Windows 瞬时占用：退避重试，不当作损坏
            time.sleep(0.02 * (i + 1))
        except ValueError:
            # JSON 解析失败 = 文件真损坏（非瞬时占用）
            restored = _restore_from_bak(path)
            if restored is not None:
                logger.error("读取 %s 解析失败，已从 .bak 恢复最后一份好版本",
                             os.path.basename(path))
                return restored
            logger.error("读取 %s 损坏且无可用 .bak，拒绝静默清空（fail-loud）",
                         os.path.basename(path))
            raise
        except OSError as e:
            if not os.path.exists(path):
                # 与 FileNotFoundError 同义（竞态删除）→ 按缺失处理
                restored = _restore_from_bak(path)
                if restored is not None:
                    logger.error("读取 %s：活文件不存在但存在 .bak，已从 .bak 恢复最后一份好版本",
                                 os.path.basename(path))
                    return restored
                return default
            logger.error("读取 %s 失败（文件存在但不可读：%s），fail-loud",
                         os.path.basename(path), e)
            raise
    # 瞬时占用（PermissionError）重试 6 轮仍未读到的兜底出口
    if not os.path.exists(path):
        restored = _restore_from_bak(path)
        if restored is not None:
            logger.error("读取 %s：活文件不存在但存在 .bak，已从 .bak 恢复最后一份好版本",
                         os.path.basename(path))
            return restored
        return default
    logger.error("读取 %s 反复被占用（已重试 6 次）且文件存在，fail-loud，"
                 "绝不把「读不到」当成「空」", os.path.basename(path))
    raise PermissionError(f"读取 {path} 反复被占用（已重试 6 次）")
