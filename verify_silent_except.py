# -*- coding: utf-8 -*-
"""D-09（P2）静默吞异常分治 · 离线回归脚本

对应缺陷：全项目缺陷审计报告 D-09 ——「静默吞异常集中整治（多行 ``except: pass``，
其中 6 处有实质风险）」。开发 C 工作包。

分治口径（报告 §5 C-3）：
  1) 清理类（``os.remove`` / ``shutil`` 收尾）→ ``logger.debug``，留痕不给用户看
  2) 安全 / 配置类（``llm_client`` / ``secret_store`` / ``qc_client``）→ ``logger.error``，**不假装成功**
  3) 统计 / 指纹类（``_CALL_STATS`` / 工作流指纹）→ ``logger.warning`` 一次性留痕

验收标准（报告 §5 逐条）：
  ① 多行 ``except: pass`` 计数 → ≤10（仅剩清理类且带 debug 日志）
  ② ``llm_client`` 密钥库清理失败场景日志出现 ``error`` 级别，且配置落盘不被中断
  ③ 全量离线测试绿

运行（仅需标准库；``requests`` 以桩替代）：
    MJSCXT_AUTOPILOT=0 python verify_silent_except.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import io
import json
import logging
import os
import py_compile
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)

_EXCEPT_PASS = re.compile(r"^\s*except([^:]*):\s*(#.*)?$")

#: 有意保留为 `pass` 的点：**logger 自身失败的兜底** —— 再调 logger 会递归。
#: 这是「已经记录过、不再重复记录」的正确写法，不属于静默吞异常。
#: ⚠️ 该白名单以**绝对行号**锚定：D-11a 在 app.py 上方新增了 2 行回收调用后，
#: 该站点由 5691 顺移到 5693（仍是同一个 `except: pass` 兜底点，未新增静默吞）。
KEEP_PASS_WHITELIST = {("app.py", 5693)}
MAX_REMAINING = 10

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _read(fn: str) -> str:
    return io.open(os.path.join(APP_DIR, fn), encoding="utf-8", errors="replace").read()


def _read_abs(p: str) -> str:
    return io.open(p, encoding="utf-8", errors="replace").read()


def _find_except_pass():
    sites = []
    for fn in sorted(f for f in os.listdir(APP_DIR) if f.endswith(".py")):
        lines = _read(fn).splitlines()
        for i, ln in enumerate(lines):
            if _EXCEPT_PASS.match(ln) and i + 1 < len(lines) and lines[i + 1].strip() == "pass":
                sites.append((fn, i + 1))
    return sites


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.recs = []

    def emit(self, r):
        self.recs.append(r)

    def messages(self, level=logging.DEBUG):
        return [r.getMessage() for r in self.recs if r.levelno >= level]


# ============================================================
print("=" * 72)
print("D-09 §1　多行 except: pass 计数收敛")
print("=" * 72)

sites = _find_except_pass()
print(f"  剩余 except: pass 共 {len(sites)} 处：")
for fn, ln in sites:
    tag = "（白名单：logger 自身失败兜底）" if (fn, ln) in KEEP_PASS_WHITELIST else "（**未处理**）"
    print(f"    {fn}:{ln} {tag}")

check(f"1.1 计数 ≤ {MAX_REMAINING}（修复前 72 处）", len(sites) <= MAX_REMAINING,
      f"实际 {len(sites)}")
unexpected = [s for s in sites if s not in KEEP_PASS_WHITELIST]
check("1.2 剩余全部在白名单内（无遗漏的静默吞）", not unexpected, f"未处理：{unexpected}")

SEC_ERROR = [
    ("llm_client.py", "清理加密库中的 LLM 密钥失败", 2),
    ("qc_client.py", "清理加密库中的质检密钥失败", 1),
    ("secret_store.py", "密钥库文件权限收紧（0o600）失败", 2),
    ("project_store.py", "回收站目录权限收紧（ACL）失败", 1),
]
for fn, needle, want in SEC_ERROR:
    txt = _read(fn)
    got = len(re.findall(re.escape(needle), txt))
    check(f"1.3 安全/配置类用 logger.error：{fn} → {needle[:15]}…（应 {want} 处）",
          got == want and f'logger.error("{needle}' in txt, f"实际 {got} 处")

for fn, needle, want in [("comfyui_client.py", "调用统计累加失败", 1),
                         ("keyframe.py", "工作流指纹计算失败", 1)]:
    txt = _read(fn)
    got = len(re.findall(re.escape(needle), txt))
    check(f"1.4 统计/指纹类用 logger.warning：{fn} → {needle[:14]}…",
          got == want and f'logger.warning("{needle}' in txt, f"实际 {got} 处")

# ============================================================
print()
print("=" * 72)
print("D-09 §2　行为验证：密钥库清理失败 → error 留痕且不中断落盘")
print("=" * 72)

# requests 桩（llm_client 在导入期即引用其 exceptions 成员）
_req = types.ModuleType("requests")
_exc = types.ModuleType("requests.exceptions")


class _ReqExc(Exception):
    pass


for _n in ("Timeout", "ConnectionError", "ChunkedEncodingError", "RequestException",
           "HTTPError"):
    setattr(_exc, _n, type(_n, (_ReqExc,), {}))
_req.exceptions = _exc
_req.Session = type("Session", (), {})
for _m in ("get", "post", "put", "delete"):
    setattr(_req, _m, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("测试桩：禁止联网")))
sys.modules.setdefault("requests", _req)
sys.modules["requests.exceptions"] = _exc

import llm_client     # noqa: E402
import qc_client      # noqa: E402
import secret_store   # noqa: E402


def _run_with_failing_store(fn, *args, **kwargs):
    """secret_store.get_store 打成必然抛错，跑 fn 并回收 llm_client 日志"""
    cap = Capture()
    # 默认 root level 是 WARNING —— 不降级的话 debug 记录在到达 handler 前就被
    # logger 级别过滤，Capture 收不到（报错级别不受影响，但保持口径一致）。
    _lv = llm_client.logger.level
    llm_client.logger.setLevel(logging.DEBUG)
    llm_client.logger.addHandler(cap)
    orig = secret_store.get_store
    secret_store.get_store = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("测试桩：加密库不可用"))
    try:
        return fn(*args, **kwargs), cap
    finally:
        secret_store.get_store = orig
        llm_client.logger.removeHandler(cap)
        llm_client.logger.setLevel(_lv)


tmpdir = tempfile.mkdtemp(prefix="mjscxt-d09-")
cfg_path = os.path.join(tmpdir, "llm_config.json")

_res, cap = _run_with_failing_store(
    llm_client.save_config, cfg_path, "http://localhost:1", "", keep_key_if_blank=False)
errs = cap.messages(logging.ERROR)
check("2.1 save_config 密钥库清理失败 → 出现 error 日志（修复前完全静默）",
      len(errs) >= 1, f"error 记录数={len(errs)}")
check("2.2 error 文案说明「加密库可能残留旧密钥」（不假装成功）",
      bool(errs) and "加密库" in errs[0], errs[0][:80] if errs else "")
check("2.3 失败不中断配置落盘（行为不变，仅新增留痕）", os.path.isfile(cfg_path))
_body = _read_abs(cfg_path)
check("2.4 配置内不残留明文 api_key（既不假装成功也不泄露明文）",
      json.loads(_body).get("api_key") == "" or "api_key" not in json.loads(_body),
      f"api_key={json.loads(_body).get('api_key')!r}")

_res2, cap2 = _run_with_failing_store(llm_client.clear_config, cfg_path)
check("2.5 clear_config 密钥库清理失败 → 出现 error 日志",
      len(cap2.messages(logging.ERROR)) >= 1,
      f"error 记录数={len(cap2.messages(logging.ERROR))}")

# ============================================================
print()
print("=" * 72)
print("D-09 §3　行为验证：清理类失败仍 fail-open（不掩盖原始异常）")
print("=" * 72)

cap3 = Capture()
_qc_lv = qc_client.logger.level
qc_client.logger.setLevel(logging.DEBUG)   # 同上：debug 记录需要放开 logger 级别
qc_client.logger.addHandler(cap3)
_orig_replace, _orig_remove = os.replace, os.remove


def _boom_replace(*a, **k):
    raise PermissionError("测试桩：目标被占用")


def _boom_remove(*a, **k):
    raise OSError("测试桩：删除失败")


os.replace, os.remove = _boom_replace, _boom_remove
raised = None
try:
    qc_client._atomic_write_json(os.path.join(tmpdir, "probe.json"), {"a": 1})
except Exception as e:  # noqa: BLE001
    raised = e
finally:
    os.replace, os.remove = _orig_replace, _orig_remove
    qc_client.logger.removeHandler(cap3)
    qc_client.logger.setLevel(_qc_lv)
    for _f in os.listdir(tmpdir):
        if _f.endswith(".tmp"):
            _orig_remove(os.path.join(tmpdir, _f))

check("3.1 写入失败时原始异常照常上抛（清理失败不掩盖它）",
      isinstance(raised, PermissionError), f"raised={type(raised).__name__}")
check("3.2 清理临时文件失败留 debug 留痕（修复前完全静默）",
      any("清理临时文件失败" in m for m in cap3.messages(logging.DEBUG)),
      f"debug={cap3.messages(logging.DEBUG)}")

# ============================================================
print()
print("=" * 72)
print("D-09 §4　全量编译 + 静态自检")
print("=" * 72)

bad = []
for fn in sorted(f for f in os.listdir(APP_DIR) if f.endswith(".py")):
    try:
        py_compile.compile(os.path.join(APP_DIR, fn), doraise=True)
    except py_compile.PyCompileError as e:
        bad.append(f"{fn}: {str(e)[:120]}")
check("4.1 app/ 全部模块编译通过", not bad, f"{bad[:3]}")

loose = []
for fn in sorted(f for f in os.listdir(APP_DIR) if f.endswith(".py")):
    txt = _read(fn)
    for m in re.finditer(r"^\s*except[^:]*:\s*(#.*)?$", txt, re.M):
        tail = txt[m.end():m.end() + 240]
        first = tail.strip().splitlines()[0] if tail.strip() else ""
        if "logger." not in first:
            continue
        # `logger.exception(...)` / `exc_info=True` 自带当前异常的类型 + traceback，
        # 即使不绑定变量也已携带真实原因，不属缺陷。
        if "logger.exception(" in first or "exc_info=True" in first:
            continue
        # 判定「未绑定异常对象」：except 行里没有任何 `as <名称>`。
        # 注意既有的 `except Exception as _e:` / `as mem_err:` 属合法绑定，
        # 同样能把真实原因带进日志，不能要求必须叫 `e`。
        if not re.search(r"\bas\s+\w+", m.group(0)):
            loose.append(f"{fn}: {m.group(0).strip()[:60]}")
check("4.2 新增日志体的 except 均已绑定异常对象（日志能带出真实原因）",
      not loose, f"{loose[:3]}")

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
print("✅ D-09 静默吞异常分治全部通过")
sys.exit(0)
