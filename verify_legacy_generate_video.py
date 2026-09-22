# -*- coding: utf-8 -*-
"""D-08（P2）遗留死代码防回潮守卫 · 离线回归脚本

对应缺陷：全项目缺陷审计报告 D-08 ——「``comfyui_client.generate_video()`` 遗留死代码
（2156 行文件内的第 1640 行）」。开发 C 工作包。

背景：该方法的 docstring 自称 `[LEGACY · 已停用]`，「一次产出 10 段拼接长视频」，
但函数名 ``generate_video`` 语义上像「生成一个视频」—— 新同学按名字调用会让
**每个分镜跑 10 段**。修复方式为**删除**（经全仓核实零调用者）。

验收标准（报告 §5 逐条）：
  ① `grep -rn 'generate_video(' app/ frontend/src` 无输出
  ② 全量离线测试绿（本脚本即该项守卫）
  ③ 守卫脚本自带「零引用不回潮」断言

运行（零第三方依赖）：
    MJSCXT_AUTOPILOT=0 python verify_legacy_generate_video.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import os
import py_compile
import re
import sys
ROOT = os.path.dirname(os.path.abspath(__file__))
SEARCH_DIRS = [os.path.join(ROOT, "app"), os.path.join(ROOT, "frontend", "src")]
SCAN_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".html")
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", ".git", ".venv", "venv"}

#: 被删除的遗留方法名（调用点判定用；只匹配「函数调用」形态，避免误伤注释/文件名）
LEGACY_CALL_RE = re.compile(r"\bgenerate_video\s*\(")
LEGACY_DEF_RE = re.compile(r"^\s*def\s+generate_video\s*\(", re.M)
#: 替代实现必须仍在（删除的是死代码，不能连正主一起删）
REPLACEMENT_RE = re.compile(r"^\s*def\s+generate_h3_sequence(_sequential)?\s*\(", re.M)

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _iter_files():
    for base in SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(SCAN_EXT):
                    yield os.path.join(dirpath, fn)


def _iter_py():
    base = os.path.join(ROOT, "app")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


# ============================================================
print("=" * 72)
print("D-08 §1　全仓零引用断言（app/ + frontend/src）")
print("=" * 72)

scanned = 0
hits = []
for path in _iter_files():
    scanned += 1
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"  [WARN] 读取失败（跳过）：{path} —— {e}")
        continue
    for m in LEGACY_CALL_RE.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        hits.append(f"{os.path.relpath(path, ROOT)}:{line_no}")

print(f"  扫描文件 {scanned} 个（目录：{[os.path.relpath(d, ROOT) for d in SEARCH_DIRS]}）")
check("1.1 全仓无任何 generate_video( 调用/定义（含前端）", not hits,
      f"命中：{hits[:8]}")
check("1.2 扫描范围非空（守卫本身有效，不是空跑）", scanned > 0,
      f"扫描 {scanned} 个文件")

# ============================================================
print()
print("=" * 72)
print("D-08 §2　app/ 下无遗留定义 + 替代实现仍在")
print("=" * 72)

defs, repl = [], []
for path in _iter_py():
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    rel = os.path.relpath(path, ROOT)
    for m in LEGACY_DEF_RE.finditer(text):
        defs.append("%s:%d" % (rel, text[:m.start()].count("\n") + 1))
    for m in REPLACEMENT_RE.finditer(text):
        repl.append("%s:%d" % (rel, text[:m.start()].count("\n") + 1))

check("2.1 app/ 下无 def generate_video（已删除，未回潮）", not defs,
      f"定义残留：{defs}")
check("2.2 替代实现 generate_h3_sequence[_sequential] 仍在", bool(repl),
      f"找到：{repl}")

# ============================================================
print()
print("=" * 72)
print("D-08 §3　模块可编译 / 关键符号仍在（删除未误伤）")
print("=" * 72)

client = os.path.join(ROOT, "app", "comfyui_client.py")
try:
    py_compile.compile(client, doraise=True)
    check("3.1 comfyui_client.py 编译通过", True)
except py_compile.PyCompileError as e:
    check("3.1 comfyui_client.py 编译通过", False, str(e)[:200])

with open(client, "r", encoding="utf-8") as f:
    ctext = f.read()
for sym in ("def strip_h3_audio_inputs", "def annotate_file_ref", "def upload_image",
            "def generate_storyboard"):
    check(f"3.2 被删除方法引用过的辅助函数仍在：{sym.split()[-1]}",
          sym in ctext)

# ============================================================
print()
print("=" * 72)
total = _PASSES[0] + len(_FAILS)
print(f"结果：{_PASSES[0]}/{total} 通过")
if _FAILS:
    print("失败项：")
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("✅ D-08 遗留死代码零引用守卫全部通过")
sys.exit(0)
