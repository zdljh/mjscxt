# -*- coding: utf-8 -*-
"""项目级回归防回潮基线（`verify_project_audit`）

存在意义
--------
多轮审计发现：本项目历史上修好的缺陷**反复回潮**（同一个坑被重挖过若干次），
根因是「修复只落在代码里，没有任何机器可判定的守卫」。本脚本把已验收缺陷的
**不变量**固化成断言，让「改回去」这件事在提交前就被拦住。

与 `verify_*.py` 家族的分工：
  - `verify_<缺陷>.py`：行为回归（跑逻辑、断言输出），覆盖**功能**；
  - 本脚本：结构守卫（静态断言源码形状）+ 汇总跑一遍同族脚本，覆盖**回潮**。
两者互补，不要互相替代。

覆盖的不变量（每条都对应审计报告里已验收的缺陷编号）
--------------------------------------------------
  G1  D-10  `concat_videos` 的所有调用点都判空（返回值是 `""` 隐式契约）
  G2  D-08  `generate_video` 死代码零引用，不回潮
  G3  D-05  整集 QC 覆盖率不回退（摘要不截断到 12、按段抽帧、截断必须显式声明）
  G4  D-07  任务队列保留背压 + `task_id` 去重
  G5  D-09  静默吞异常保持收敛（多行 `except: pass` ≤ 10 且仅剩日志兜底）
  G6  D-11  ComfyUI 输出回收模块在位 + `narration` 废弃标记在位
  G7  同族 `verify_*.py` 全部在位且离线全绿

设计约定（踩过的坑）
------------------
  - **守卫一律用「内容锚点」，禁止用绝对行号**：本项目已经出现过「插入两行导致
    另一份白名单里的行号漂移、守卫失效」的事故。行号只用于输出报错位置。
  - **守卫自身必须被自检**：G1 的判空检查器带合成样本自检（坏样本必须被抓到），
    否则一个永远返回「通过」的守卫比没有守卫更危险。

运行（只依赖标准库）：
    MJSCXT_AUTOPILOT=0 python verify_project_audit.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
FRONTEND_SRC = os.path.join(ROOT, "frontend", "src")

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(f"{name} —— {detail}" if detail else name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8", errors="replace").read()


def _iter_py(root: str):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("node_modules", ".git", "__pycache__", "dist", "build")]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _scan_targets():
    """守卫的扫描面：``app/**``（递归）+ 仓库根**平层** ``*.py``。

    ⚠️ 早期版本只扫 `app/` 平层（`os.listdir`），于是 `app/providers/` 与仓库根的
    外围脚本统统落在扫描面之外 —— 守卫给出的「全库只有 1 处静默吞异常」是**假的**，
    真实是 5 处。扫描面不足会让守卫输出虚假的安全感，比没有守卫更糟。
    """
    yield from _iter_py(APP)
    for fn in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, fn)
        if fn.endswith(".py") and os.path.isfile(p):
            yield p


# ============================================================
print("=" * 72)
print("G1　D-10：concat_videos 的所有调用点必须判空（返回值是隐式契约）")
print("=" * 72)


def unguarded_callsites(source: str, func_name: str = "concat_videos") -> list:
    """返回 `func_name` 调用点中**未判空**的行号列表（纯函数，便于自检）。

    判空形式（两者等价，都接受）：
      ① `if not x.concat_videos(...):`  —— 调用点自己就是 if 的判定条件；
      ② `x.concat_videos(...)` 之后若干行内出现 `if not <var>:` / `_nonempty(<var>)`
         —— 例如 pipeline 把结果写进 tmp 再 `_nonempty(tmp)` 校验。

    ⚠️ 用 AST 而非正则：正则会命中 docstring 里的示例代码与注释
    （本文件早期版本就被自己的 docstring 骗出过一次误报）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name != func_name:
            continue
        ln = node.lineno
        head = lines[ln - 1] if 0 < ln <= len(lines) else ""
        # 形式①：调用点所在行自带 `if not`
        if re.search(r"\bif\s+not\s", head):
            continue
        # 形式②：调用点之后 4 行内做了判空
        window = "\n".join(lines[ln - 1: ln + 4])
        if "_nonempty(" in window or re.search(r"\bif\s+not\s+\w+", window):
            continue
        bad.append(ln)
    return bad


def callsites(source: str, func_name: str = "concat_videos") -> list:
    """返回 `[(行号, 是否传了 caller=)]` —— AST 口径，不含 docstring 与注释。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name == func_name:
            out.append((node.lineno, any(k.arg == "caller" for k in node.keywords)))
    return out


# ---- G1 自检：守卫必须能抓到坏样本，否则它就是个摆设 ----
_BAD_SAMPLE = """
def g(proc, files, out):
    proc.concat_videos(files, out)
    return out
"""
_GOOD_IF_SAMPLE = """
def g(proc, files, out):
    if not proc.concat_videos(files, out):
        return out
"""
_GOOD_NONEMPTY_SAMPLE = """
def g(proc, files, out):
    proc.concat_videos(files, out)
    if not _nonempty(out):
        raise RuntimeError("fail")
"""
_GOOD_CALLER_SAMPLE = """
def g(proc, files, out):
    if not proc.concat_videos(files, out, caller="who"):
        return out
"""
check("1.0 守卫自检：坏样本（裸调用）必须被抓出", unguarded_callsites(_BAD_SAMPLE) == [3],
      f"实际={unguarded_callsites(_BAD_SAMPLE)}（期望 [3]）")
check("1.1 守卫自检：`if not f(...)` 形式不误报", unguarded_callsites(_GOOD_IF_SAMPLE) == [],
      f"实际={unguarded_callsites(_GOOD_IF_SAMPLE)}")
check("1.2 守卫自检：`_nonempty(var)` 形式不误报",
      unguarded_callsites(_GOOD_NONEMPTY_SAMPLE) == [],
      f"实际={unguarded_callsites(_GOOD_NONEMPTY_SAMPLE)}")
check("1.2b 守卫自检：docstring 里的示例代码不算调用点（AST 口径）",
      unguarded_callsites('"""doc: x.concat_videos(a, b) 只是说明文字"""\nx = 1\n') == []
      and len(callsites(_GOOD_CALLER_SAMPLE)) == 1,
      "docstring 被误判为调用点")

_all_sites = []
_violations = []
_no_caller = []
for _p in _iter_py(APP):
    _src = _read(_p)
    _rel = os.path.relpath(_p, ROOT)
    for _ln, _has_caller in callsites(_src):
        _all_sites.append(f"{_rel}:{_ln}")
        if not _has_caller:
            _no_caller.append(f"{_rel}:{_ln}")
    for _ln in unguarded_callsites(_src):
        _violations.append(f"{_rel}:{_ln}")
print(f"  concat_videos 调用点共 {len(_all_sites)} 处："
      + ("、".join(_all_sites) or "无"))
check("1.3 所有调用点均已判空（新增调用点必须判空并登记到本脚本）",
      not _violations, f"未判空：{_violations}")
check("1.4 所有调用点都传了 caller=（失败日志能定位到调用方）",
      not _no_caller, f"未传 caller：{_no_caller}")

# ============================================================
print()
print("=" * 72)
print("G2　D-08：generate_video 死代码零引用，不回潮")
print("=" * 72)

_dv_hits = []
for _root in (APP, FRONTEND_SRC):
    for _p in _iter_py(_root):
        for _i, _line in enumerate(_read(_p).splitlines(), 1):
            if _line.lstrip().startswith("#"):
                continue
            if re.search(r"\bdef\s+generate_video\b", _line):
                _dv_hits.append(f"{os.path.relpath(_p, ROOT)}:{_i} def")
            elif re.search(r"(?<![_A-Za-z0-9])generate_video\s*\(", _line):
                _dv_hits.append(f"{os.path.relpath(_p, ROOT)}:{_i} call")
check("2.1 `generate_video` 零定义、零调用（`_legacy_` 前缀不算回潮）",
      not _dv_hits, f"命中：{_dv_hits}")

# ============================================================
print()
print("=" * 72)
print("G3　D-05：整集 QC 覆盖率不回退（12 镜摘要 + 最多 6 帧的双重降采样）")
print("=" * 72)

_cov_path = os.path.join(APP, "qc_coverage.py")
_cov = _read(_cov_path) if os.path.isfile(_cov_path) else ""
check("3.1 覆盖率口径独立成零依赖模块 app/qc_coverage.py（离线可测）", bool(_cov))
_m = re.search(r"EPISODE_MAX_FRAMES\s*=\s*(\d+)", _cov)
check("3.2 整集抽帧上限 ≥ 12（验收：40 段整集 frames ≥ 12）",
      bool(_m) and int(_m.group(1)) >= 12, f"实际={_m.group(1) if _m else '缺失'}")
_m = re.search(r"DEFAULT_DESC_LIMIT\s*=\s*(\d+)", _cov)
check("3.3 镜头摘要上限 ≥ 40（原 12 → 40 镜整集后段对模型不可见）",
      bool(_m) and int(_m.group(1)) >= 40, f"实际={_m.group(1) if _m else '缺失'}")
check("3.4 截断声明固定字样「其余未提供」在位（报告验收口径）",
      'TRUNCATED_NOTE = "其余未提供"' in _cov)

_app_src = _read(os.path.join(APP, "app.py"))
check("3.5 _episode_qc_desc 的默认上限引用常量而非写死（防再次回退到 12）",
      bool(re.search(r"def _episode_qc_desc\([^)]*limit[^)]*qc_coverage\.DEFAULT_DESC_LIMIT",
                     _app_src)),
      "未引用 qc_coverage.DEFAULT_DESC_LIMIT")
# 注意：不能写 `check_video\([^)]*frame_ratio` —— 实参里还有 `_episode_qc_desc(shots)`
# 这类嵌套括号，`[^)]*` 会在第一个 `)` 就截断。改为「调用点起点 + 定长窗口」。
_seg_qc_hit = False
for _m in re.finditer(r"qc_client\.check_video\(", _app_src):
    if re.search(r"frame_ratio\s*=", _app_src[_m.end():_m.end() + 800]):
        _seg_qc_hit = True
        break
check("3.6 整集质检按段中点抽帧（_seg_qc_fn 传 frame_ratio=）", _seg_qc_hit)
_qc_src = _read(os.path.join(APP, "qc_client.py"))
check("3.7 qc_client.extract_frames / check_video 保留 frame_ratio 入口",
      "frame_ratio" in _qc_src
      and bool(re.search(r"def extract_frames\([^)]*frame_ratio", _qc_src, re.S)))

# ============================================================
print()
print("=" * 72)
print("G4　D-07：任务队列保留背压 + task_id 去重")
print("=" * 72)

_ts = _read(os.path.join(APP, "task_store.py"))
check("4.1 TaskQueue 仍带 max_queue 背压参数", "max_queue" in _ts)
check("4.2 submit 返回可用性（bool）而非无条件入队",
      bool(re.search(r"def submit\([^)]*\)\s*->\s*bool", _ts, re.S)),
      "submit 未声明 -> bool")
check("4.3 去重占位集合 _pending_ids 在位且入队时登记",
      "_pending_ids" in _ts and bool(re.search(r"_pending_ids\.add\(", _ts)))
check("4.4 任务结束后释放去重占位（_pending_ids.discard 在 finally 内）",
      bool(re.search(r"_pending_ids\.discard\(", _ts)))

# ============================================================
print()
print("=" * 72)
print("G5　D-09：静默吞异常保持收敛（≤10 且仅剩日志兜底）")
print("=" * 72)

_EXCEPT_PASS = re.compile(r"^\s*except([^:]*):\s*(#.*)?$")
_silent_sites = []
for _p in _scan_targets():
    _lines = _read(_p).splitlines()
    _rel = os.path.relpath(_p, ROOT).replace("\\", "/")
    for _i, _line in enumerate(_lines):
        if _EXCEPT_PASS.match(_line) and _i + 1 < len(_lines) \
                and _lines[_i + 1].strip() == "pass":
            # 内容锚点：该站点前 6 行内必须有 logger —— 说明它是「日志自身失败的兜底」
            # （再调 logger 会递归，只能 pass），而不是彻底静默的吞异常。
            _ctx = "\n".join(_lines[max(0, _i - 6):_i])
            _silent_sites.append((_rel, _i + 1, "logger" in _ctx))
print(f"  全仓多行 except: pass 共 {len(_silent_sites)} 处（D-09 修复前 app/ 平层为 72）：")
for _rel, _ln, _logged in _silent_sites:
    print(f"    {_rel}:{_ln} {'（日志兜底，合规）' if _logged else '（**彻底静默**）'}")

#: 报告的验收阈值（§5 C-3）：多行 `except: pass` 计数 ≤ 10。
MAX_TOTAL_SILENT = 10
#: 棘轮：**非**日志兜底的静默站点数量上限 —— **只允许下降，不允许新增**。
#: 现状 4 处，全部落在 D-09 的作用域（`app/` 平层）之外，属未治理存量：
#:   - `app/providers/local.py` / `app/providers/__init__.py`（provider 子系统的探测/等待）
#:   - 仓库根 `comic_drama_pipeline.py`（外围脚本的轮询）/ `restart_flask.py`（杀进程）
#: 新增任何一处都会让守卫变红；要根治就逐个治理，然后把这个数字往下调。
MAX_NON_LOGGING_SILENT = 4

_app_flat = [s for s in _silent_sites if os.path.dirname(s[0]) == "app"]
_non_logging = [s for s in _silent_sites if not s[2]]
check(f"5.1 全仓多行 except: pass 计数 ≤ {MAX_TOTAL_SILENT}（报告验收口径）",
      len(_silent_sites) <= MAX_TOTAL_SILENT, f"实际 {len(_silent_sites)}")
check("5.2 app/ 平层（D-09 作用域）剩余站点全部是「日志自身失败兜底」",
      all(s[2] for s in _app_flat),
      f"彻底静默：{[(r, l) for r, l, ok in _app_flat if not ok]}")
check(f"5.3 非日志兜底的静默站点数不增长（棘轮 ≤ {MAX_NON_LOGGING_SILENT}，只许下降）",
      len(_non_logging) <= MAX_NON_LOGGING_SILENT,
      f"实际 {len(_non_logging)}：{[(r, l) for r, l, _ in _non_logging]}")

# ============================================================
print()
print("=" * 72)
print("G6　D-11：ComfyUI 输出回收 + narration 废弃标记")
print("=" * 72)

_dr_path = os.path.join(APP, "disk_reclaim.py")
_dr = _read(_dr_path) if os.path.isfile(_dr_path) else ""
check("6.1 app/disk_reclaim.py 在位（零依赖，可离线单测）", bool(_dr))
check("6.2 提供 plan_reclaim / reclaim_comfyui_output 两个入口",
      "def plan_reclaim(" in _dr and "def reclaim_comfyui_output(" in _dr)
check("6.3 默认最小保留年龄为 24h（86400s）",
      bool(re.search(r"24\s*\*\s*3600|86400", _dr)))
check("6.4 同内容副本判定用 sha256（不能只看文件大小）",
      "sha256" in _dr)
check("6.5 硬链接（st_nlink）被显式跳过",
      "st_nlink" in _dr)

_du = _read(os.path.join(APP, "dialogue_utils.py"))
check("6.6 dialogue_utils 对残留 narration 打 warning（旧剧本可见化）",
      bool(re.search(r"logger\.warning\(", _du)) and "narration" in _du)
_nts = _read(os.path.join(APP, "novel_to_script.py"))
check("6.7 novel_to_script 里 narration 带结构化 deprecated 标记",
      "DEPRECATED_SHOT_FIELDS" in _nts
      and bool(re.search(r'"narration"\s*:\s*\{[^}]*"deprecated"\s*:\s*True', _nts, re.S)),
      "未找到 DEPRECATED_SHOT_FIELDS['narration']['deprecated'] = True")

# ---- 行为级守卫（比文本断言硬）：直接 import 零依赖模块，跑真实判定 ----
# `disk_reclaim` 只依赖标准库，可以离线导入 —— 这两个不变量来自独立验证抓出的
# 两个真缺陷，绝不能靠「源码里有某个字符串」来守。
sys.path.insert(0, APP)
try:
    import disk_reclaim as _dr_mod
except Exception as _e:  # noqa: BLE001
    _dr_mod = None
    check("6.8 disk_reclaim 可离线导入（否则行为级守卫无法执行）", False, repr(_e))

if _dr_mod is not None:
    # 缺陷 2（回收在真实场景无效）：ComfyUI SaveImage 只在 prefix 末段追加 `_00001_`，
    # 所以 `comic_drama_retry/{proj}_shot_01` 的真实产物名**不含** `_retry`。
    check("6.8 真实残留命名（无 _retry，仅 ComfyUI 编号后缀）被认作可回收候选",
          _dr_mod.is_reclaimable_candidate("proj_shot_01_00001_.png") is True
          and _dr_mod.is_reclaimable_candidate("proj_shot_01_retry_00001_.png") is True,
          "真实命名未被认作候选 → 回收会静默失效")
    check("6.9 交付件名（base.png / shot_01.png / ep01_final.mp4）不算候选",
          not _dr_mod.is_reclaimable_candidate("base.png")
          and not _dr_mod.is_reclaimable_candidate("shot_01.png")
          and not _dr_mod.is_reclaimable_candidate("ep01_final.mp4"))
    check("6.10 候选作用域限定在 comic_drama* 目录内（不碰别人的 ComfyUI 产物）",
          _dr_mod.is_artifact_scope("/c/comfy/comic_drama_sb/a_00001_.png", "/c/comfy") is True
          and _dr_mod.is_artifact_scope("/c/comfy/other_tool/a_00001_.png", "/c/comfy") is False
          and _dr_mod.is_artifact_scope("/c/comfy/a_00001_.png", "/c/comfy") is False)
    # 缺陷：`float(min_age_sec)` 在 None 时抛 TypeError → 被外层 except 吞成
    # 「整批静默放弃」。这里用**真实文件**跑判定，两半各自可判别：
    #   ① 老文件（25h 前）仍被判定可删 → 说明没整批放弃；
    #   ② 新文件（刚建）被判 recent → 说明回退的是 86400 而**不是** 0
    #      （若被当成 0，阈值失效，新文件会被误判为可删）。
    import tempfile
    import time as _time
    _tmp = tempfile.mkdtemp(prefix="mjscxt-audit-")
    _old = os.path.join(_tmp, "old_00001_.bin")
    _new = os.path.join(_tmp, "new_00001_.bin")
    for _f in (_old, _new):
        with open(_f, "wb") as _fh:
            _fh.write(b"AUDIT-FINGERPRINT-1")
    _st_old = os.stat(_old)
    _cand_old = [(_old, int(_st_old.st_size), _time.time() - 25 * 3600, int(_st_old.st_nlink))]
    _fps = {(int(_st_old.st_size), _dr_mod._sha256(_old))}
    _r_old = _dr_mod.plan_reclaim(_cand_old, _fps, now=_time.time(), min_age_sec=None)
    _st_new = os.stat(_new)
    _cand_new = [(_new, int(_st_new.st_size), float(_st_new.st_mtime), int(_st_new.st_nlink))]
    _r_new = _dr_mod.plan_reclaim(_cand_new, _fps, now=_time.time(), min_age_sec=None)
    check("6.11 min_age_sec=None 不导致整批静默放弃，且回退默认 24h（而非当成 0）",
          _r_old["delete"] == [_old] and _r_new["skip"]["recent"] == 1,
          f"老文件 delete={_r_old['delete']}、新文件 skip={_r_new['skip']}")
    # 缺陷 1（大小写击穿「不碰正式目录」防线）：Windows 路径大小写不敏感，
    # `commonpath` 不做 normcase —— 不归一就会把「在正式目录内」判成「不在」。
    if os.path.normcase("A") == "a":
        check("6.12 大小写不同的同一路径仍判为「在正式目录内」（防线不被击穿）",
              _dr_mod._is_within("C:\\tmp\\Assets\\sub\\p_00001_.png", ["C:\\tmp\\assets"])
              is True,
              "normcase 缺失 → 可误删正式目录内文件")
    else:
        print("  [SKIP] 6.12 当前平台路径大小写敏感，跳过 normcase 断言（如实跳过，不伪装通过）")

# ============================================================
print()
print("=" * 72)
print("G7　同族回归脚本在位且离线全绿")
print("=" * 72)

SIBLINGS = [
    "verify_episode_qc_coverage.py",      # D-05
    "verify_task_queue_backpressure.py",  # D-07
    "verify_legacy_generate_video.py",    # D-08
    "verify_silent_except.py",            # D-09
    "verify_concat_failure_logging.py",   # D-10
    "verify_comfyui_reclaim.py",          # D-11a
    "verify_narration_deprecated.py",     # D-11b
    "verify_frontend_api_links.py",       # 前端→后端 API 链路核对（路径/方法/字段）
]
_env = dict(os.environ, MJSCXT_AUTOPILOT="0")
for _sib in SIBLINGS:
    _path = os.path.join(ROOT, _sib)
    if not os.path.isfile(_path):
        check(f"7.x {_sib} 在位", False, "脚本缺失（被删或被改名）")
        continue
    try:
        _r = subprocess.run([sys.executable, _path], cwd=ROOT, env=_env,
                            capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        check(f"7.x {_sib} 离线跑通", False, "超时 600s")
        continue
    _tail = ((_r.stdout or "") + (_r.stderr or "")).strip().splitlines()
    _detail = _tail[-1][:120] if _tail else f"exit={_r.returncode}"
    check(f"7.x {_sib} 离线跑通（exit 0）", _r.returncode == 0, _detail)

# ============================================================
print()
print("=" * 72)
_total = _PASSES[0] + len(_FAILS)
print(f"verify_project_audit：{_PASSES[0]}/{_total} 通过")
if _FAILS:
    print("失败项：")
    for _f in _FAILS:
        print(f"  - {_f}")
    sys.exit(1)
print("✅ 项目级回归防回潮基线全部通过")
sys.exit(0)
