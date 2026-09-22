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

独立复核又发现两个真缺陷，本脚本据此补三组**回归用例**（对应实现已修，
见 ``app/disk_reclaim.py`` 的 ``_norm`` / ``is_comfy_output_artifact``）：
  A1. **真实残留命名必须能回收**（缺陷 2）：ComfyUI ``SaveImage`` 只在
      ``filename_prefix`` 末段追加自动编号后缀，故真实残留是
      ``proj_shot_01_00001_.png`` —— **不含 ``_retry``**。旧判定「stem 含
      ``_retry/_try``」六个生产 prefix 只命中一个，真实残留一个都收不到
      （实测 3 轮增量比 100%）。含独立的 3 轮模拟（**全程只用真实命名**，
      不靠 ``_retry`` 自证），直接反映验收①。
  A2. **路径大小写不一致不得击穿「不碰正式目录」防线**（缺陷 1）：
      ``COMFYUI_OUTPUT_DIR=...\\assets`` 与正式目录 ``...\\Assets`` 是同一路径，
      ``os.path.commonpath`` 不做 normcase 会把「在里面」判成「不在里面」，
      于是正式目录内的文件被删。非 Windows（``normcase`` 不转换）时
      **跳过并明确打印**，不伪装通过。
  A3. **命名判定并集**（缺陷 2 根因）：六个生产 ``filename_prefix`` 的真实产物名
      必须全部命中 ``is_comfy_output_artifact`` / ``is_reclaimable_candidate``，
      交付件名（``base.png`` / ``shot_01.png`` / ``ep01_final.mp4``）必须全部不命中；
      ``is_artifact_scope`` 只认 ``comic_drama*`` 目录、不认根上散文件与别的工具目录。

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

#: 测试用项目名（模拟生产 ``filename_prefix`` 里的项目段）。
_PROJ = "proj"

#: 当前平台路径是否大小写不敏感。``os.path.normcase`` 在 POSIX 上是恒等函数，
#: 在 Windows 上做 lower + 反斜杠归一 —— 用它判定能否构造「同路径不同写法」场景。
_CASE_INSENSITIVE = os.path.normcase("Assets") != "Assets"


def _real_residue_names(counter: int):
    """真实残留命名：``(comfy 子目录, 相对名)``，**不含 ``_retry``**。

    ComfyUI ``SaveImage`` 把 ``filename_prefix`` 逐字当路径，再在末段追加
    ``_{counter:05}_``，所以 ``filename_prefix="comic_drama_retry/proj_shot_01"``
    产出的文件名是 ``proj_shot_01_00001_.png`` —— 目录名带 retry，**文件名不带**。
    """
    return [
        ("comic_drama_sb", f"{_PROJ}_shot_01_{counter:05d}_.png"),
        ("comic_drama", f"{_PROJ}/character/林川_{counter:05d}_.png"),
        ("comic_drama_retry", f"{_PROJ}_shot_02_{counter:05d}_.png"),
    ]


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
print()
print("=" * 72)
print("D-11a §A1　真实残留命名（无 _retry）必须能回收 · 缺陷 2 回归")
print("=" * 72)

base, comfy, official = _mkroot("realnames")
_TMPDIRS.append(base)
_real_paths = []
for _i, (_sub, _rel) in enumerate(_real_residue_names(1)):        # 00001：与生产形态一致
    _content = f"real-naming-residue-{_i}-".encode("utf-8") * 400
    _write(os.path.join(official, "characters", _PROJ, f"asset_real_{_i}.png"), _content)
    _p = _write(os.path.join(comfy, _sub, *_rel.split("/")), _content)
    _age(_p, 25 * 3600)                                            # 模拟 > 24h 的历史残留
    _real_paths.append(_p)

check("A1.0 前置条件：3 个真实残留文件名均不含 _retry/_try（测的不是自证）",
      all(not disk_reclaim.is_retry_artifact(p) for p in _real_paths),
      f"{[os.path.basename(p) for p in _real_paths]}")
stats = disk_reclaim.reclaim_comfyui_output(comfy, [official], min_age_sec=86400)
_deleted = set(stats["delete"])
check("A1.1 三个真实命名残留全部进入 delete", all(p in _deleted for p in _real_paths),
      f"delete={sorted(os.path.basename(p) for p in _deleted)}")
check("A1.2 三个真实命名残留确实已从盘上消失",
      all(not os.path.isfile(p) for p in _real_paths))
check("A1.3 scanned 恰好 3（全部命中，且未误纳正式目录）", stats["scanned"] == 3,
      f"scanned={stats['scanned']} delete={len(_deleted)}")
check("A1.4 正式产物零删除（真实残留被回收不影响交付物）",
      len(_snapshot(official)) == 3 and stats["skip"]["no_copy"] == 0,
      f"official={len(_snapshot(official))} no_copy={stats['skip']['no_copy']}")

# —— A1 独立的 3 轮模拟：三轮**只用真实命名**（递增自动编号，仍不含 _retry）——
base3, comfy3, official3 = _mkroot("real3rounds")
_TMPDIRS.append(base3)
_per_round_added3 = 0
_per_round_deleted3 = []
for _r in range(3):
    _round_bytes = 0
    for _i, (_sub, _rel) in enumerate(_real_residue_names(_r + 1)):
        _content = (f"real3-r{_r}-f{_i}-").encode("utf-8") * 600
        _write(os.path.join(official3, "characters", _PROJ, f"asset3_r{_r}_{_i}.png"), _content)
        _p = _write(os.path.join(comfy3, _sub, *_rel.split("/")), _content)
        _age(_p, 25 * 3600)
        _round_bytes += len(_content)
    if _r == 0:
        _per_round_added3 = _round_bytes
    _st3 = disk_reclaim.reclaim_comfyui_output(comfy3, [official3], min_age_sec=86400)
    _per_round_deleted3.append(len(_st3["delete"]))

_net3 = _tree_size(comfy3)
_ratio3 = (_net3 / _per_round_added3) if _per_round_added3 else 1.0
check("A1.5 3 轮（全程真实命名）后 COMFYUI_OUTPUT_DIR 净增量 < 单轮新增量的 30%",
      _net3 < _per_round_added3 * 0.3,
      f"net={_net3}B 单轮新增={_per_round_added3}B 比值={_ratio3 * 100:.1f}%")
check("A1.6 3 轮每轮都删满 3 个真实命名残留（无一轮落空）",
      _per_round_deleted3 == [3, 3, 3], f"每轮删除={_per_round_deleted3}")
check("A1.7 3 轮全程未出现 _retry/_try 命名（回收率非靠重试命名自证）",
      all(not disk_reclaim.is_retry_artifact(_rel)
          for _c in (1, 2, 3) for _s, _rel in _real_residue_names(_c)))


# ============================================================
print()
print("=" * 72)
print("D-11a §A2　路径大小写不一致不得击穿「不碰正式目录」防线 · 缺陷 1 回归")
print("=" * 72)

if not _CASE_INSENSITIVE:
    print("    ⚠ 当前平台 os.path.normcase 不做大小写转换（非 Windows），"
          "无法构造「同路径不同写法」场景")
    print("    ⚠ 平台不支持大小写差异，已跳过 §A2 断言（不伪装通过）")
else:
    base = tempfile.mkdtemp(prefix="mjscxt-d11a-case-")
    _TMPDIRS.append(base)
    # 正式目录写成 Assets（大写），COMFYUI_OUTPUT_DIR 写成 assets（小写）——
    # 大小写不敏感的 Windows 上两者指向**同一个目录**，misconfig 即可触发。
    official_case = os.path.join(base, "Assets")
    os.makedirs(official_case, exist_ok=True)
    comfy_case = os.path.join(base, "assets")
    _case_content = b"official-asset-content" * 300

    # 样本①：正式目录**根上**的交付件 —— 命名带 _retry，但在 comfy 根上（不入作用域）
    _victim_flat = _write(
        os.path.join(official_case, "p_shot_01_retry_00001_.png"), _case_content)
    _age(_victim_flat, 25 * 3600)
    # 样本②（**关键，决定本用例真假**）：文件名带 _retry **且**落在 comic_drama*/ 内。
    # 它同时满足「命名判定」与「作用域判定」，因此**唯一**能拦住它的就是条件 6
    # （候选不得落在正式目录内）—— 正是缺陷 1 击穿的那道防线。
    # 若改用 `proj_shot_01_00001_.png` 这类编号名，被退回的旧候选判定（只认 _retry）
    # 会先把它过滤掉，用例将因「候选判定」而非「防线」通过 —— 假通过，测不到缺陷 1。
    _victim_deep = _write(
        os.path.join(official_case, "comic_drama", "p_shot_01_retry_00001_.png"), _case_content)
    _age(_victim_deep, 25 * 3600)

    check("A2.0 前置条件：<base>/assets 与 <base>/Assets 指向同一目录（大小写不敏感）",
          os.path.isdir(comfy_case) and os.path.isdir(official_case)
          and os.path.samefile(comfy_case, official_case))
    _before_case = _snapshot(official_case)
    _stats_case = disk_reclaim.reclaim_comfyui_output(
        comfy_case, [official_case], min_age_sec=86400)
    _after_case = _snapshot(official_case)
    check("A2.1 大小写写法不一致时，正式目录内的候选仍在（防线未被击穿）",
          os.path.isfile(_victim_flat) and os.path.isfile(_victim_deep))
    check("A2.2 delete 为空 —— 一个正式目录内的文件都没被删",
          _stats_case["delete"] == [], f"delete={_stats_case['delete']}")
    check("A2.3 正式目录文件清单 + 内容 sha256 零变化（2 个文件原样）",
          _before_case == _after_case == {
              "p_shot_01_retry_00001_.png": hashlib.sha256(_case_content).hexdigest(),
              os.path.join("comic_drama", "p_shot_01_retry_00001_.png"):
                  hashlib.sha256(_case_content).hexdigest(),
          },
          f"after={sorted(_after_case)}")


# ============================================================
print()
print("=" * 72)
print("D-11a §A3　命名判定并集：ComfyUI 编号产物 ∪ 重试标记 · 缺陷 2 根因")
print("=" * 72)

# 六个生产 filename_prefix 的真实产物名（``filename_prefix`` 逐字当路径 +
# 末段追加 ``_{counter:05}_``，故它们**大多不含 _retry**）。
_PROD_NAMES = [
    f"{_PROJ}_shot_01_00001_.png",              # 批量分镜
    f"{_PROJ}_shot_01_retry_00001_.png",        # 单镜重跑（唯一含 _retry 的）
    f"{_PROJ}/character/林川_00001_.png",        # 资产
    f"{_PROJ}_ep01_00007_.png",                 # 整集视频
    "ep01_00001_.png",                          # 整集
    "shot_01_try2_00001_.png",                  # 旧式 _try 命名
]
_DELIVERABLE_NAMES = ["base.png", "shot_01.png", "ep01_final.mp4"]

check("A3.1 六个生产 prefix 的真实产物名全部被 is_comfy_output_artifact 命中",
      all(disk_reclaim.is_comfy_output_artifact(n) for n in _PROD_NAMES),
      f"漏判={[n for n in _PROD_NAMES if not disk_reclaim.is_comfy_output_artifact(n)]}")
check("A3.2 同一批真实产物名全部被 is_reclaimable_candidate 命中（并集生效）",
      all(disk_reclaim.is_reclaimable_candidate(n) for n in _PROD_NAMES),
      f"漏判={[n for n in _PROD_NAMES if not disk_reclaim.is_reclaimable_candidate(n)]}")
check("A3.3 交付件名（base.png / shot_01.png / ep01_final.mp4）一律不命中",
      not any(disk_reclaim.is_comfy_output_artifact(n) for n in _DELIVERABLE_NAMES)
      and not any(disk_reclaim.is_reclaimable_candidate(n) for n in _DELIVERABLE_NAMES),
      f"误判={[n for n in _DELIVERABLE_NAMES if disk_reclaim.is_reclaimable_candidate(n)]}")
check("A3.4 判定只看 stem：目录名含 retry 不影响（comic_drama_retry/base.png 不命中）",
      not disk_reclaim.is_retry_artifact("comic_drama_retry/base.png")
      and not disk_reclaim.is_comfy_output_artifact("comic_drama_retry/base.png"))

_croot = os.path.join(ROOT, "comfy_out_probe")
check("A3.5 is_artifact_scope：comic_drama* 产物目录内为 True（前缀匹配而非硬编码目录名）",
      all(disk_reclaim.is_artifact_scope(
          os.path.join(_croot, _d, f"{_PROJ}_shot_01_00001_.png"), _croot)
          for _d in ("comic_drama", "comic_drama_sb", "comic_drama_retry", "comic_drama_kf")))
check("A3.6 is_artifact_scope：别的工具目录 / comfy 根上散文件为 False",
      not disk_reclaim.is_artifact_scope(
          os.path.join(_croot, "other_tool", "x_00001_.png"), _croot)
      and not disk_reclaim.is_artifact_scope(
          os.path.join(_croot, "根上的散文件_00001_.png"), _croot))


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
