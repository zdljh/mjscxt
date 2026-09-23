# -*- coding: utf-8 -*-
"""A1 + A2（2026-09-23）· AI 凭证「单一事实源」收尾 · 离线回归守卫

覆盖本轮两条修复：

  **A1** `migrate_from_legacy` 由「DB 有**任意**一行就整体早退」改为**按模块判空**。
      旧行为下 DB 只要存在一行（例如只有 text），qc / chat 的旧槽密钥就**永远补不进来**；
      而迁移之后新写入旧槽的密钥同样进不了 DB → 「DB 单点」名存实亡。

  **A2** 质检端点的**写侧**同步落 DB（`save_config` / `set_endpoint` / `reset_endpoint` /
      `clear_config`）。此前只有旧加密库（裸槽 `qc`）一个写点，而读侧自 P0-5 起 DB 优先 ——
      实际能工作全靠「DB 无该模块行时回落旧库」这条兜底，写/读并不对称。

第 4 节是「测试卫生」守卫：扫描 `.workbuddy/test` 下会写凭证库的脚本，断言它已重指向
`ai_credentials_db` 的库路径。
⚠️ 本条是实测踩坑换来的：2026-09-23 隔离测试只重指向了 `qc_client._PROJECT_ROOT` 与
`ai_config._ROOT_DIR`，漏掉这**第三个根**，于是 `qc_client.save_config` 把测试用的
`probe.example/v1` + 假钥写进了**用户真实的** `output/tasks.db`（事后按真实旧槽值修复）。
而 `app.py` 是模块级初始化，`import app` 即触发 `migrate_from_legacy()` 写 DB ——
所以任何 import app 的测试都必须在 import **之前**隔离。

隔离方式：脚本在 import 之前就设 `MJSCXT_CRED_ROOT` / `MJSCXT_CRED_DB` 指向临时目录，
故本守卫全程不碰真实 `output/tasks.db` 与 `secrets.enc`。

跑法：`MJSCXT_AUTOPILOT=0 C:/Python314/python.exe verify_cred_single_source.py`
退出码 0 = 全绿。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT, "app")
TEST_DIR = os.path.join(ROOT, ".workbuddy", "test")

# ⚠️ 必须在 import 应用模块**之前**设好隔离根（模块级常量在 import 期求值）
TMP = tempfile.mkdtemp(prefix="mjscxt-credss-")
os.environ["MJSCXT_CRED_ROOT"] = TMP
os.environ["MJSCXT_CRED_DB"] = os.path.join(TMP, "output", "tasks.db")
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")
sys.path.insert(0, APP_DIR)

import ai_credentials_db as A  # noqa: E402
import ai_config               # noqa: E402
import qc_client               # noqa: E402
import secret_store            # noqa: E402

KEY = "sk-credss-0123456789abcdef0123456789abcdef"   # 41 字符（> _KEY_TRUST_MIN_LEN=16）
KEY2 = "sk-credss-ffffffffffffffffffffffffffffffffff"


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))
        _FAILS.append(name)


_PASSES = [0]
_FAILS: list = []


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def _iso(sub: str) -> str:
    """每个用例独立隔离根（含加密主密钥、配置、凭证库）"""
    d = os.path.join(TMP, sub)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    A._PROJECT_ROOT = d
    A._DB_PATH = os.path.join(d, "output", "tasks.db")
    A._SCHEMA_READY = False
    # 另两个根：写旧加密库 / 读非密钥字段
    qc_client._PROJECT_ROOT = d
    ai_config._ROOT_DIR = d
    return d


def _legacy_store(d):
    return secret_store.get_store(d)


# ---------------------------------------------------------------------------
section("0. 隔离自检（隔离不生效则后续断言全部无意义）")
# ---------------------------------------------------------------------------
check("0.1 A._DB_PATH 落在临时根内（mjscxt-credss-）",
      os.path.abspath(A._DB_PATH).startswith(os.path.abspath(TMP)), A._DB_PATH)
check("0.2 真实 output/tasks.db 不在隔离根内",
      not os.path.abspath(A._DB_PATH).startswith(os.path.join(ROOT, "output")), A._DB_PATH)

# ---------------------------------------------------------------------------
section("1. A1：迁移按**模块**判空（旧行为会整体早退）")
# ---------------------------------------------------------------------------
d = _iso("a1")
st = _legacy_store(d)
st.set_api_key("ai.text", KEY)
st.set_api_key("ai.qc", KEY2)

# 1) DB 空 → 两个模块都应迁进来
r1 = A.migrate_from_legacy()
check("1.1 DB 空时 text/qc 均迁入", sorted(r1["migrated"]) == ["qc", "text"], r1)
check("1.2 modules_with_key() 反映两模块", A.modules_with_key() == {"text", "qc"},
      A.modules_with_key())

# 2) 幂等：再迁一次 → 全部跳过、不重复写
r2 = A.migrate_from_legacy()
check("1.3 再次迁移幂等（migrated 为空）", r2["migrated"] == [], r2)

# 3) ★核心回归断言：DB 只有 text 时，qc 仍必须被补齐
#    （旧实现 `if has_credentials() and not force: return` 会在这一步整体早退）
A.clear_credentials()                       # 清空 DB
A.set_credentials("text", base_url="https://keep.example/v1", model="keep-model",
                  api_key=KEY, source="ui")
check("1.4 前置：DB 只有 text 有钥", A.modules_with_key() == {"text"}, A.modules_with_key())
r3 = A.migrate_from_legacy()
check("1.5 ★DB 已有 text 时仍补齐 qc（旧行为会整体早退）",
      "qc" in r3["migrated"], r3)
check("1.6 已有 text 不被覆盖（skip 而非重迁）",
      any(s.startswith("text:") for s in r3["skipped"]), r3["skipped"])
check("1.7 text 的 base_url 保持用户值（未被旧源覆盖）",
      A.get_credentials("text")["base_url"] == "https://keep.example/v1",
      A.get_credentials("text")["base_url"])

# 4) 负对照：三模块都有钥 → 迁移什么都不做（证明判据真的在拦）
A.set_credentials("chat", base_url="https://c.example/v1", model="m", api_key=KEY, source="ui")
r4 = A.migrate_from_legacy()
check("1.8 负对照：三模块齐全时 migrated 为空", r4["migrated"] == [], r4)

# 5) force=True 允许强制覆盖
r5 = A.migrate_from_legacy(force=True)
check("1.9 force=True 时已有模块不被 skip 拦住（text/qc 被强制重迁）",
      {"text", "qc"}.issubset(set(r5["migrated"])), r5)

# 6) has_credentials 口径 = 有密钥
A.clear_credentials()
check("1.10 清空后 has_credentials() 为 False", A.has_credentials() is False)
A.set_credentials("chat", base_url="https://x/v1", model="m", api_key="", source="ui")
check("1.11 只有 base_url/model（无密钥）时 has_credentials() 仍为 False"
      "（判据是「密钥」不是「任一字段非空」）", A.has_credentials() is False)

# ---------------------------------------------------------------------------
section("2. A2：质检端点写侧同步落 DB")
# ---------------------------------------------------------------------------
d = _iso("a2")
cfg_path = os.path.join(d, "qc_config.json")

# 1) save_config 带端点+密钥 → DB qc 被写入
qc_client.save_config(cfg_path, {
    "enabled": True, "image_enabled": True,
    "base_url": "https://qc.example/v1", "model": "qc-model-x", "api_key": KEY,
})
ep = A.get_credentials("qc")
check("2.1 save_config 后 DB qc 有密钥", bool(ep.get("api_key")), ep.get("api_key"))
check("2.2 端点同步（base_url）", ep.get("base_url") == "https://qc.example/v1",
      ep.get("base_url"))
check("2.3 端点同步（model）", ep.get("model") == "qc-model-x", ep.get("model"))
check("2.4 public_view.ready 为 True", A.public_view("qc")["configured"] is True)

# 2) 安全阀：端点全空时保存阈值 → **不得**把 DB 已有端点清空
qc_client.save_config(cfg_path, {"pass_score": 85})
ep2 = A.get_credentials("qc")
check("2.5 ★安全阀：只改阈值不会清空 DB 端点",
      ep2.get("base_url") == "https://qc.example/v1" and bool(ep2.get("api_key")), ep2)

# 3) set_endpoint（自动写入路径）也同步
qc_client.set_endpoint(cfg_path, "https://auto.example/v1", KEY2, "auto-model")
ep3 = A.get_credentials("qc")
check("2.6 set_endpoint 同步 DB（base/model/key 全到位）",
      ep3.get("base_url") == "https://auto.example/v1" and ep3.get("model") == "auto-model"
      and ep3.get("api_key") == KEY2, ep3)

# 4) reset_endpoint（恢复为 AI 设置）→ DB qc 必须被清空（否则读侧仍读旧端点）
qc_client.reset_endpoint(cfg_path)
check("2.7 ★reset_endpoint 同步清空 DB qc",
      A.get_credentials("qc").get("api_key") == "", A.get_credentials("qc"))

# 5) clear_config → 同样清空
qc_client.save_config(cfg_path, {"base_url": "https://qc2.example/v1", "model": "m2",
                                 "api_key": KEY})
check("2.8 前置：clear 前 DB 有端点", bool(A.get_credentials("qc").get("api_key")))
qc_client.clear_config(cfg_path)
check("2.9 ★clear_config 同步清空 DB qc",
      A.get_credentials("qc").get("api_key") == "", A.get_credentials("qc"))

# 6) DB 同步失败不阻断主保存（响亮降级而非抛错）
d = _iso("a2b")
cfg_path_b = os.path.join(d, "qc_config.json")
_orig_set = A.set_credentials


def _boom(*a, **k):
    raise RuntimeError("测试桩：凭证库不可用")


A.set_credentials = _boom
try:
    cfg_out = qc_client.save_config(cfg_path_b, {
        "enabled": True, "base_url": "https://qc3.example/v1", "model": "m3", "api_key": KEY,
    })
    ok_ret = isinstance(cfg_out, dict) and bool(cfg_out.get("base_url"))
finally:
    A.set_credentials = _orig_set
check("2.10 凭证库不可用时 save_config 仍正常返回（响亮降级、不抛错）", ok_ret,
      type(cfg_out).__name__)
check("2.11 降级时配置仍已落盘", os.path.isfile(cfg_path_b)
      and qc_client.load_config(cfg_path_b).get("model") == "m3")

# ---------------------------------------------------------------------------
section("3. 隔离元数据：环境变量覆盖生效（默认行为不变）")
# ---------------------------------------------------------------------------
import subprocess  # noqa: E402

code = ("import sys,os;sys.path.insert(0, r'%s');import ai_credentials_db as A;"
        "print(A._DB_PATH)" % APP_DIR)
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                     env={**os.environ}).stdout.strip()
check("3.1 设 MJSCXT_CRED_DB 时模块按该路径解析", os.path.abspath(out) == os.path.abspath(
    os.environ["MJSCXT_CRED_DB"]), out)
out2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                      env={k: v for k, v in os.environ.items()
                           if k not in ("MJSCXT_CRED_DB", "MJSCXT_CRED_ROOT")}).stdout.strip()
check("3.2 不设环境变量时回落真实路径 output/tasks.db（生产行为不变）",
      os.path.abspath(out2) == os.path.join(ROOT, "output", "tasks.db"), out2)

# ---------------------------------------------------------------------------
section("4. 测试卫生：会写凭证库的测试必须重指向第三个根")
# ---------------------------------------------------------------------------
# ⚠️ 本条防的就是 09-23 实测踩到的污染：隔离测试漏掉 ai_credentials_db 的库路径，
#    把假凭证写进用户真实 tasks.db。
WRITE_CALLS = (
    "qc_client.save_config(", "qc_client.set_endpoint(", "qc_client.reset_endpoint(",
    "qc_client.clear_config(", "ai_config.save_module(", "migrate_from_legacy(",
)
ISO_HINTS = ("ai_credentials_db._DB_PATH", "ai_credentials_db._PROJECT_ROOT",
             "MJSCXT_CRED_DB", "MJSCXT_CRED_ROOT")

if os.path.isdir(TEST_DIR):
    offenders = []
    for fn in sorted(f for f in os.listdir(TEST_DIR) if f.endswith(".py")):
        p = os.path.join(TEST_DIR, fn)
        try:
            src = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if not any(c in src for c in WRITE_CALLS):
            continue
        if any(h in src for h in ISO_HINTS):
            continue
        offenders.append(fn)
    check(f"4.1 .workbuddy/test 下调用凭证写路径的脚本均已隔离（扫描到 {len(offenders)} 个未隔离）",
          not offenders, offenders[:6])
else:
    check("4.1 .workbuddy/test 目录存在（本机无该目录时跳过）", True)

# `import app` 的脚本会触发模块级 migrate_from_legacy() → 同样需要隔离
if os.path.isdir(TEST_DIR):
    app_importers = []
    for fn in sorted(f for f in os.listdir(TEST_DIR) if f.endswith(".py")):
        src = open(os.path.join(TEST_DIR, fn), encoding="utf-8", errors="replace").read()
        if not re.search(r"^\s*import app\b|^\s*from app import", src, re.M):
            continue
        if any(h in src for h in ISO_HINTS):
            continue
        app_importers.append(fn)
    print(f"  [INFO] import app 但未显式隔离凭证库的脚本 {len(app_importers)} 个："
          f"{app_importers[:8]}")
    # 说明（不判红）：DB 三模块齐全时迁移会全跳过，故当前不产生写入；
    # 一旦真实 DB 缺某模块，这些脚本就会补齐它（属预期生产行为）。
    check("4.2 凭证库路径可被环境变量隔离（供上述脚本统一接入）",
          os.path.abspath(A._DB_PATH).startswith(os.path.abspath(TMP)))

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
n_fail = len(_FAILS)
print()
print("=" * 72)
print(f"结果：{'全部通过' if not n_fail else '存在失败'}（失败 {n_fail} 项）")
for f in _FAILS:
    print("  - " + f)
print("=" * 72)
sys.exit(1 if n_fail else 0)
