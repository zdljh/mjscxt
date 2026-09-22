# -*- coding: utf-8 -*-
"""D-11a（P2）ComfyUI 输出目录滚动回收 · 离线回归脚本

对应缺陷：全项目缺陷审计报告 **D-11**（第 1 部分「ComfyUI 输出目录只增不减」，
开发 C 工作包）。判定逻辑在 ``app/disk_reclaim.py``（零第三方依赖）。

验收标准（报告 §5 D-11 / 工作包逐条）：
  ① 连续跑 3 轮后 ``COMFYUI_OUTPUT_DIR`` 增量 < 单轮新增量的 30%（即确实发生了回收）；
  ② 删除动作仅作用于「正式产物目录已有同内容副本 且 mtime > 24h」的文件；
     回归测试必须断言**正式产物零删除**；
  ③ 全量离线测试绿。

本脚本用 ``tempfile`` 造**真实文件**（不只 mock 路径），覆盖 7 条：
  1. 连续 3 轮回收比 < 30%；
  2. 正式产物零删除（3 轮前后文件清单 + 内容 sha256 完全一致）；
  3. mtime < 24h 的重试文件不删；
  4. 正式目录里找不到同内容副本的重试文件不删；
  5. 非 ``_retry/_try`` 命名的文件（如 ``comic_drama/.../base.png``）不删；
  6. 硬链接（``st_nlink > 1``）跳过；
  7. 打桩让 ``os.remove`` 抛 ``OSError``：单文件删除失败不中断整批清理，
     且不向调用方抛异常（``reclaim_comfyui_output`` 正常返回统计）。

运行（仅需标准库）：
    MJSCXT_AUTOPILOT=0 python verify_comfyui_reclaim.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)

import disk_reclaim  # noqa: E402

_FAILS = []
_PASSES = [0]

_DAY = 86400.0


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _write(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _age(path: str, seconds: float) -> None:
    """把文件 mtime 拨回到 ``seconds`` 秒之前（模拟「历史残留」）。"""
    t = time.time() - seconds
    os.utime(path, (t, t))


def _tree_size(root: str) -> int:
    total = 0
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                continue
    return total


def _snapshot(root: str) -> dict:
    """``{相对路径: sha256}`` —— 正式目录的「文件清单 + 内容指纹」。"""
    snap = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                with open(p, "rb") as f:
                    body = f.read()
            except OSError:
                continue
            snap[os.path.relpath(p, root)] = hashlib.sha256(body).hexdigest()
    return snap


def _mkroot(tag: str):
    """造一组独立的 (comfy 输出根, 正式产物根)。"""
    base = tempfile.mkdtemp(prefix=f"mjscxt-d11a-{tag}-")
    comfy = os.path.join(base, "comfy_out")
    official = os.path.join(base, "official")
    os.makedirs(comfy, exist_ok=True)
    os.makedirs(official, exist_ok=True)
    return base, comfy, official


_TMPDIRS = []


# ============================================================
print("=" * 72)
print("D-11a §1/§2　连续 3 轮：确实回收 + 正式产物零删除")
print("=" * 72)

base, comfy, official = _mkroot("rounds")
_TMPDIRS.append(base)

per_round_added = 0
official_untouched = True
deleted_total = 0
per_round_deleted = []
for r in range(3):
    round_bytes = 0
    for i in range(3):
        content = (f"round{r}-file{i}-").encode("utf-8") * 300
        # 正式产物目录写入一份副本
        _write(os.path.join(official, "characters", "proj",
                            f"asset_r{r}_{i}.png"), content)
        # ComfyUI 侧「留下」重试源文件（同一内容，命名带 _retry）
        src = _write(os.path.join(comfy, "comic_drama_retry",
                                  f"proj_shot_{i:02d}_retry_{r:05d}_.png"), content)
        _age(src, 25 * 3600)   # 模拟 > 24h 的历史残留
        round_bytes += len(content)
    if r == 0:
        per_round_added = round_bytes

    before = _snapshot(official)
    stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
    after = _snapshot(official)
    if before != after:
        official_untouched = False
    deleted_total += len(stats["delete"])
    per_round_deleted.append(len(stats["delete"]))

net = _tree_size(comfy)
ratio = (net / per_round_added) if per_round_added else 1.0
check("1.1 3 轮后 COMFYUI_OUTPUT_DIR 净增量 < 单轮新增量的 30%",
      net < per_round_added * 0.3,
      f"net={net}B 单轮新增={per_round_added}B 比值={ratio * 100:.1f}%")
check("1.2 确实发生了回收（累计删除 > 0 且每轮都删）",
      deleted_total > 0 and all(n > 0 for n in per_round_deleted),
      f"每轮删除={per_round_deleted}")
check("2.1 正式产物零删除：3 轮前后文件清单 + 内容 sha256 完全一致",
      official_untouched)
check("2.2 正式目录 3 轮共 9 个文件且全部指纹可比对（未被清空/篡改）",
      len(_snapshot(official)) == 9, f"实际 {len(_snapshot(official))}")

check("1.3 命名判定：含 _retry / _try 才视为重试产物",
      disk_reclaim.is_retry_artifact("p_shot_01_retry_00001_.png")
      and disk_reclaim.is_retry_artifact("shot_01_try2_00001_.png")
      and not disk_reclaim.is_retry_artifact("base.png")
      and not disk_reclaim.is_retry_artifact("shot_01.png"))


# ============================================================
print()
print("=" * 72)
print("D-11a §3　mtime < 24h 的重试文件不删")
print("=" * 72)

base, comfy, official = _mkroot("fresh")
_TMPDIRS.append(base)
_content = b"fresh-retry-content" * 200
_write(os.path.join(official, "characters", "proj", "asset_fresh.png"), _content)
fresh = _write(os.path.join(comfy, "comic_drama_retry", "proj_shot_01_retry_00001_.png"),
               _content)
# 保持「刚生成」的 mtime，不做 _age
stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
check("3.1 mtime < 24h 的重试文件仍在", os.path.isfile(fresh))
check("3.2 计入 skip.recent", stats["skip"]["recent"] == 1,
      f"recent={stats['skip']['recent']} delete={len(stats['delete'])}")
# 阈值参数化：阈值调到 0 后，同一个「刚生成」的文件即可回收 ——
# 证明「不删」确实来自 24h 阈值，而不是别的偶然原因。
stats0 = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=0)
check("3.3 阈值参数化生效：min_age_sec=0 时同一文件即可回收",
      not os.path.isfile(fresh) and len(stats0["delete"]) == 1,
      f"delete={len(stats0['delete'])}")


# ============================================================
print()
print("=" * 72)
print("D-11a §4　正式目录无同内容副本的重试文件不删")
print("=" * 72)

base, comfy, official = _mkroot("nocopy")
_TMPDIRS.append(base)
orphan = _write(os.path.join(comfy, "comic_drama_sb", "proj_shot_02_retry_00001_.png"),
                b"no-official-copy" * 300)
_age(orphan, 25 * 3600)
stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
check("4.1 正式目录里找不到同内容副本 → 不删", os.path.isfile(orphan))
check("4.2 计入 skip.no_copy", stats["skip"]["no_copy"] == 1,
      f"no_copy={stats['skip']['no_copy']}")

# 4.3 同名但内容不同（size 也不同）→ 仍不删：证明不是「按名字/大小」误伤
_write(os.path.join(official, "characters", "proj", "asset_x.png"), b"x" * 4096)
decoy = _write(os.path.join(comfy, "comic_drama_sb", "proj_shot_03_retry_00001_.png"),
               b"y" * 128)
_age(decoy, 25 * 3600)
stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
check("4.3 同名但 (size, sha256) 不同 → 不删（双匹配而非只看 size）",
      os.path.isfile(decoy) and stats["skip"]["no_copy"] >= 1)


# ============================================================
print()
print("=" * 72)
print("D-11a §5　非 _retry/_try 命名的文件不删")
print("=" * 72)

base, comfy, official = _mkroot("plain")
_TMPDIRS.append(base)
_content = b"plain-base-image" * 400
_write(os.path.join(official, "characters", "proj", "asset_p.png"), _content)
plain = _write(os.path.join(comfy, "comic_drama", "proj", "base.png"), _content)
_age(plain, 25 * 3600)
stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
check("5.1 非 _retry/_try 命名的文件不删（即使内容有正式副本）",
      os.path.isfile(plain))
check("5.2 该文件根本不进入候选（未出现在 delete 中）",
      plain not in stats["delete"] and stats["scanned"] == 0,
      f"scanned={stats['scanned']}")


# ============================================================
print()
print("=" * 72)
print("D-11a §6　硬链接（st_nlink > 1）跳过")
print("=" * 72)

base, comfy, official = _mkroot("hardlink")
_TMPDIRS.append(base)
_content = b"hardlinked-content" * 500
official_file = _write(os.path.join(official, "characters", "proj", "asset_h.png"), _content)
comfy_link = os.path.join(comfy, "comic_drama", "proj_shot_04_retry_00001_.png")
os.makedirs(os.path.dirname(comfy_link), exist_ok=True)
os.link(official_file, comfy_link)          # 同一 inode 的硬链接（nlink == 2）
_age(comfy_link, 25 * 3600)
pre = os.stat(comfy_link).st_nlink
stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
check("6.0 前置条件：硬链接 st_nlink == 2", pre == 2, f"nlink={pre}")
check("6.1 硬链接跳过（删了不释放空间，仍在）", os.path.exists(comfy_link))
check("6.2 计入 skip.hardlink", stats["skip"]["hardlink"] == 1,
      f"hardlink={stats['skip']['hardlink']}")
check("6.3 正式产物未受影响", os.path.isfile(official_file))


# ============================================================
print()
print("=" * 72)
print("D-11a §7　删除失败（os.remove 抛 OSError）不中断整批、不上抛")
print("=" * 72)

base, comfy, official = _mkroot("removefail")
_TMPDIRS.append(base)
victims = []
for i in range(2):
    content = (f"victim-{i}-").encode() * 400
    _write(os.path.join(official, "characters", "proj", f"asset_v{i}.png"), content)
    p = _write(os.path.join(comfy, "comic_drama_retry",
                            f"proj_shot_1{i}_retry_00001_.png"), content)
    _age(p, 25 * 3600)
    victims.append(p)

_orig_remove = os.remove
_attempts = [0]


def _boom(path, *args, **kwargs):
    _attempts[0] += 1
    raise OSError("测试桩：删除失败（文件被占用）")


_stats = None
_raised = None
os.remove = _boom          # disk_reclaim 内部同样走 os.remove
try:
    _stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
except Exception as e:  # noqa: BLE001
    _raised = e
finally:
    os.remove = _orig_remove

check("7.1 单文件删除失败不向调用方抛异常", _raised is None, f"raised={_raised!r}")
check("7.2 正常返回统计字典（含 delete / skip / removed_bytes）",
      isinstance(_stats, dict) and "delete" in _stats and "skip" in _stats
      and "removed_bytes" in _stats)
check("7.3 失败不中断整批：两个候选都被尝试删除", _attempts[0] == 2,
      f"尝试次数={_attempts[0]}")
check("7.4 failed 计数 = 2、removed_bytes = 0（失败不虚报释放量）",
      bool(_stats) and _stats["failed"] == 2 and _stats["removed_bytes"] == 0,
      f"failed={_stats['failed'] if _stats else None} "
      f"removed={_stats['removed_bytes'] if _stats else None}")
check("7.5 文件因删除失败而仍在（清理是优化，不阻断）",
      all(os.path.isfile(v) for v in victims))


# ============================================================
print()
print("=" * 72)
print("D-11a §8　全容错：入参非法 / 目录不存在也不抛异常")
print("=" * 72)
for bad_args, label in [((None, [official]), "comfy_root=None"),
                        (("", [official]), "comfy_root=''"),
                        ((os.path.join(base, "not-exist"), [official]), "目录不存在"),
                        ((comfy, None), "official_dirs=None")]:
    try:
        s = disk_reclaim.reclaim_comfyui_output(bad_args[0], bad_args[1])
        ok = isinstance(s, dict) and "removed_bytes" in s
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"    {label} 抛异常：{e!r}")
    check(f"8.x 容错：{label} → 返回统计不抛异常", ok)


# ============================================================
for d in _TMPDIRS:
    shutil.rmtree(d, ignore_errors=True)

print()
print("=" * 72)
_total = _PASSES[0] + len(_FAILS)
print(f"结果：{_PASSES[0]}/{_total} 通过")
if _FAILS:
    print("失败项：")
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("✅ D-11a ComfyUI 输出目录滚动回收全部通过")
sys.exit(0)
