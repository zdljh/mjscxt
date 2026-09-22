# -*- coding: utf-8 -*-
"""D-10（P2）`concat_videos` 失败路径日志补原因 · 离线回归脚本

对应缺陷：全项目缺陷审计报告 D-10 ——「`concat_videos` 返回 `""` 的隐式契约
（已缓解，列为观察项）」。开发 C 工作包。

缺陷本质
--------
`concat_videos` 用**空字符串**同时表达「失败」与「无输出」，且**不抛异常**
（返回值契约见 `app/video_postprocess.py` 的 docstring）。返回值本身**携带不了失败理由**：
调用方一旦漏判空，就会把 `""` 当成合法路径继续喂给 ffmpeg，报出与真实原因无关的错误。
审计核实当前两处调用点都判了空，所以不构成现行缺陷 —— 但排障链路是断的。

修复口径（报告原文：「保持返回契约不变，改为在返回前把失败原因挂到日志并带上调用方标识」）
------------------------------------------------------------------------------
- 新增 `caller` 参数：调用方自报身份，失败日志里能直接看出「谁调的」；
- 所有返回 `""` 的路径统一经 `_concat_fail()` 出口，`reason` 必须带 ffmpeg 的**真实输出**
  （stderr / 返回码 / 异常类型），不能是「合并失败」这种无信息量的套话；
- **返回契约零改动**：仍然返回 `""`、仍然不抛异常。

验收标准（报告原文）
------------------
① 失败路径日志包含具体 ffmpeg 原因；
② `verify_project_audit.py` 能断言所有 `concat_videos` 调用点均对返回值判空（见该脚本 G1）。

运行（只依赖标准库；`video_postprocess` 的依赖链只有 `config`，可离线导入）：
    MJSCXT_AUTOPILOT=0 python verify_concat_failure_logging.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import video_postprocess as vp     # noqa: E402

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.recs = []

    def emit(self, r):
        self.recs.append(r)

    def messages(self, level=logging.DEBUG):
        return [r.getMessage() for r in self.recs if r.levelno >= level]

    def errors(self):
        return self.messages(logging.ERROR)

    def warnings(self):
        return self.messages(logging.WARNING)


_LV = vp.logger.level
vp.logger.setLevel(logging.DEBUG)


def _probe_no_audio(path):
    """探针桩：无音轨 → 不触发 need_reencode，稳定走 demuxer 路径。"""
    return {"path": path, "ok": True, "has_video": True, "has_audio": False}


def _fake_run_factory(results):
    """按调用次序返回结果的 subprocess.run 桩。

    `results` 元素：`types.SimpleNamespace`（正常返回）或 `Exception` 实例（抛出）。
    """
    seq = list(results)

    def _run(cmd, **kw):
        item = seq.pop(0) if seq else types.SimpleNamespace(
            returncode=1, stderr="(桩：无更多预设返回)", stdout="")
        if isinstance(item, BaseException):
            raise item
        return item

    return _run


def _ok(rc=0, stderr="", stdout=""):
    return types.SimpleNamespace(returncode=rc, stderr=stderr, stdout=stdout)


def _run_case(fn, results, probe=None):
    """在打桩环境下跑一个用例，返回 (返回值, 是否抛出, Capture)。"""
    cap = Capture()
    vp.logger.addHandler(cap)
    _orig_probe = vp.probe_media
    _orig_run = vp.subprocess.run
    vp.probe_media = probe or _probe_no_audio
    vp.subprocess.run = _fake_run_factory(results)
    raised = None
    ret = None
    try:
        ret = fn()
    except BaseException as e:      # noqa: BLE001 —— 契约要求「任何失败都不抛」
        raised = e
    finally:
        vp.subprocess.run = _orig_run
        vp.probe_media = _orig_probe
        vp.logger.removeHandler(cap)
    return ret, raised, cap


tmpdir = tempfile.mkdtemp(prefix="mjscxt-d10-")
seg_a = os.path.join(tmpdir, "a.mp4")
seg_b = os.path.join(tmpdir, "b.mp4")
for _p in (seg_a, seg_b):
    with open(_p, "wb") as _f:
        _f.write(b"\x00" * 16)          # 内容无关紧要：探针与 ffmpeg 都已打桩

proc = vp.VideoPostProcessor(comfyui_url="http://127.0.0.1:1")

# ============================================================
print("=" * 72)
print("D-10 §1　成功路径零回归（改动只加日志，不改行为）")
print("=" * 72)

_out_ok = os.path.join(tmpdir, "ok_final.mp4")


def _success():
    with open(_out_ok, "wb") as f:      # ffmpeg 桩返回 0，但真实流程会产出文件
        f.write(b"FINAL")
    return proc.concat_videos([seg_a, seg_b], _out_ok, caller="t.success")


_ret, _raised, _cap = _run_case(_success, [_ok(0)])
check("1.1 成功时仍返回 output_path（契约不变）", _ret == _out_ok, f"实际={_ret!r}")
check("1.2 成功时不抛异常", _raised is None, f"raised={type(_raised).__name__}")
check("1.3 成功路径不产生 error 日志（日志只加在失败路径）",
      not _cap.errors(), f"errors={_cap.errors()}")
check("1.4 成功后清理 concat 清单临时文件（无 .list 残片）",
      not os.path.exists(_out_ok + ".list"))

# ============================================================
print()
print("=" * 72)
print("D-10 §2　失败路径必须留痕：调用方标识 + ffmpeg 真实原因")
print("=" * 72)

_out_f = os.path.join(tmpdir, "fail_final.mp4")

# --- 2A. 空输入 ---
_ret, _raised, _cap = _run_case(
    lambda: proc.concat_videos([], _out_f, caller="t.empty"), [])
check("2A.1 空输入返回 \"\"（契约不变）", _ret == "", f"实际={_ret!r}")
check("2A.2 空输入不抛异常", _raised is None)
check("2A.3 空输入 → error 日志（修复前只有 warning，且不含调用方）",
      len(_cap.errors()) >= 1, f"errors={_cap.errors()}")
check("2A.4 空输入的日志带调用方标识 [调用方=t.empty]",
      any("[调用方=t.empty]" in m for m in _cap.errors()), f"{_cap.errors()[:1]}")
check("2A.5 空输入的日志说明原因（含「输入片段为空」）",
      any("输入片段为空" in m for m in _cap.errors()))

# --- 2B. demuxer 失败 → 降级重编码也失败：必须带出重编码的 stderr ---
_ret, _raised, _cap = _run_case(
    lambda: proc.concat_videos([seg_a, seg_b], _out_f, caller="t.demux_fallback"),
    [_ok(1, stderr="DEMUX-STDERR-7"), _ok(1, stderr="ENC-STDERR-9")])
check("2B.1 拼接失败返回 \"\"（契约不变）", _ret == "", f"实际={_ret!r}")
check("2B.2 拼接失败不抛异常", _raised is None, f"raised={type(_raised).__name__}")
check("2B.3 error 日志带出**最终**失败原因（重编码 stderr=ENC-STDERR-9）",
      any("ENC-STDERR-9" in m for m in _cap.errors()), f"errors={_cap.errors()}")
check("2B.4 降级原因也留痕（warning 含 demuxer stderr=DEMUX-STDERR-7）",
      any("DEMUX-STDERR-7" in m for m in _cap.warnings()), f"warnings={_cap.warnings()[:1]}")
check("2B.5 error 日志带调用方标识",
      any("[调用方=t.demux_fallback]" in m for m in _cap.errors()))

# --- 2C. ffmpeg 返回码 0 但没产出文件（磁盘满/被杀）---
_ret, _raised, _cap = _run_case(
    lambda: proc.concat_videos([seg_a], os.path.join(tmpdir, "ghost.mp4"),
                               caller="t.ghost"),
    [_ok(1, stderr="x"), _ok(0, stderr="")])
check("2C.1 返回码 0 却无输出 → 仍返回 \"\"（不谎报成功）", _ret == "", f"实际={_ret!r}")
check("2C.2 该失败被明确指出（日志含「输出文件不存在」）",
      any("输出文件不存在" in m for m in _cap.errors()), f"errors={_cap.errors()}")
check("2C.3 日志给出怀疑方向（磁盘写满 / 进程被回收）",
      any("磁盘写满" in m for m in _cap.errors()))

# --- 2D. 子进程调用本身抛异常 ---
_ret, _raised, _cap = _run_case(
    lambda: proc.concat_videos([seg_a], _out_f, caller="t.spawn"), 
    [OSError("spawn-fail-77")])
check("2D.1 子进程异常不抛出（契约：「任何异常一律返回 \"\"」）", _raised is None,
      f"raised={type(_raised).__name__}")
check("2D.2 子进程异常仍返回 \"\"", _ret == "")
check("2D.3 日志带异常类型 + 原始信息（OSError / spawn-fail-77）",
      any("OSError" in m and "spawn-fail-77" in m for m in _cap.errors()),
      f"errors={_cap.errors()}")

# --- 2E. 输出目录不可建（建目录失败也被收敛，不再把异常抛给调用方）---
_blocker = os.path.join(tmpdir, "iam_a_file")
with open(_blocker, "wb") as _f:
    _f.write(b"x")
_ret, _raised, _cap = _run_case(
    lambda: proc.concat_videos([seg_a], os.path.join(_blocker, "sub", "o.mp4"),
                               caller="t.mkdir"), [_ok(0)])
check("2E.1 目录建不出来时返回 \"\" 而不是抛异常（契约收紧）", _ret == "" and _raised is None,
      f"ret={_ret!r} raised={type(_raised).__name__ if _raised else None}")
check("2E.2 该失败也留了 error 日志（不是静默吞）",
      len(_cap.errors()) >= 1, f"errors={_cap.errors()}")

# ============================================================
print()
print("=" * 72)
print("D-10 §3　失败出口契约自洽（每条失败路径都满足同一组不变量）")
print("=" * 72)

_out_g = os.path.join(tmpdir, "g.mp4")
cases = {
    "空输入": (lambda: proc.concat_videos([], _out_g, caller="t.c1"), []),
    "demuxer+重编码双失败": (
        lambda: proc.concat_videos([seg_a], _out_g, caller="t.c2"),
        [_ok(1, stderr="e1"), _ok(1, stderr="e2")]),
    "子进程异常": (lambda: proc.concat_videos([seg_a], _out_g, caller="t.c3"),
                   [RuntimeError("boom")]),
}
all_returns_empty = True
all_raise_free = True
all_have_caller = True
_detail = []
for _name, (_fn, _res) in cases.items():
    _r, _raised, _cap = _run_case(_fn, _res)
    if _r != "":
        all_returns_empty = False
        _detail.append(f"{_name} 返回 {_r!r}")
    if _raised is not None:
        all_raise_free = False
        _detail.append(f"{_name} 抛了 {type(_raised).__name__}")
    if not any("[调用方=" in m for m in _cap.errors()):
        all_have_caller = False
        _detail.append(f"{_name} 的日志缺调用方标识")
check("3.1 所有失败路径统一返回 \"\"", all_returns_empty, "；".join(_detail))
check("3.2 所有失败路径都不抛异常", all_raise_free, "；".join(_detail))
check("3.3 所有失败路径的日志都带 [调用方=…]（D-10 修复的核心诉求）",
      all_have_caller, "；".join(_detail))

# --- 3.4 _concat_fail 单测：它就是唯一出口 ---
_cap = Capture()
vp.logger.addHandler(_cap)
try:
    _r = proc._concat_fail("t.unit", "手写的失败理由")
finally:
    vp.logger.removeHandler(_cap)
check("3.4 _concat_fail 返回 \"\" 且把理由写进 error 日志",
      _r == "" and any("手写的失败理由" in m and "[调用方=t.unit]" in m
                       for m in _cap.messages(logging.ERROR)),
      f"ret={_r!r} errors={_cap.messages(logging.ERROR)}")

# --- 3.5 源码层面：不得再有绕过 _concat_fail 的裸 return "" ---
import io     # noqa: E402
import re     # noqa: E402
_src = io.open(os.path.join(APP, "video_postprocess.py"), encoding="utf-8").read()
_start = _src.index("def concat_videos(")
_end = _src.index("def add_subtitles(")
_body = _src[_start:_end]
_bare = [ln.strip() for ln in _body.splitlines() if ln.strip() == 'return ""']
check("3.5 concat_videos/_concat_reencode 内无裸 return \"\"（全部经 _concat_fail 出口）",
      not _bare, f"仍有 {len(_bare)} 处裸返回")

vp.logger.setLevel(_LV)

# ============================================================
print()
print("=" * 72)
_total = _PASSES[0] + len(_FAILS)
print(f"结果：{_PASSES[0]}/{_total} 通过")
if _FAILS:
    print("失败项：")
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("✅ D-10 concat_videos 失败日志补原因全部通过")
sys.exit(0)
