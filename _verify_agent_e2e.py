# -*- coding: utf-8 -*-
"""总控 AI 端到端验证：假 LLM + 临时实例（默认 5077）。

前置：
  python _mock_llm.py                       # 5099
  MJSCXT_AUTOPILOT=0 APP_PORT=5077 python serve.py
运行：
  python _verify_agent_e2e.py
"""
import atexit
import json
import os
import sys
import time

import requests

BASE = os.environ.get("AGENT_E2E_BASE", "http://127.0.0.1:5077")
MOCK = "http://127.0.0.1:5099/v1"
PROJ = "e2e_agent_test"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  <- {detail}" if detail and not cond else ""))


def api(method, path, **kw):
    return requests.request(method, BASE + path, timeout=30, **kw)


print(f"\n[0] 目标实例 {BASE}")
try:
    r = api("GET", "/api/status")
    check("实例可达", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
except Exception as e:  # noqa: BLE001
    print("实例不可达：", e)
    sys.exit(1)

print("\n[1] 临时把对话总控模型指向假 LLM（结束后自动还原）")
# ⚠️ 血泪教训：ai_config.json 是**全实例共用**的，测试完不还原会把用户真配置冲掉；
# 而 api_key 一旦被新值覆盖就再也找不回来（密钥存在加密库里）。
# 因此这里：① 先快照 ② 绝不传 api_key（留空 = 保持原密钥不动）③ finally 里还原。
_before = api("GET", "/api/ai/config").json().get("config", {}).get("modules", {}).get("chat", {})
_orig = {"base_url": _before.get("base_url") or "", "model": _before.get("model") or ""}


_restored = {"done": False}


def _restore_chat():
    if _restored["done"]:
        return
    _restored["done"] = True
    if not _orig["base_url"] or not _orig["model"]:
        api("POST", "/api/ai/config/clear", json={"module": "chat"})
        print(f"  （原 chat 模块未配置，已清空还原）")
        return
    api("POST", "/api/ai/config",
        json={"module": "chat", "base_url": _orig["base_url"], "model": _orig["model"]})
    print(f"  （已还原 chat -> {_orig['base_url']} / {_orig['model']}）")


r = api("POST", "/api/ai/config", json={"module": "chat", "base_url": MOCK, "model": "mock-1"})
check("chat 模块临时改指向假 LLM", r.status_code == 200 and r.json().get("success"),
      f"{r.status_code} {r.text[:200]}")
# 兜底：即便中途 sys.exit / 抛异常，也保证把用户配置还原回去
atexit.register(lambda: _restore_chat())

print("\n[2] 工具清单与护栏")
r = api("GET", "/api/agent/tools")
d = r.json()
check("/api/agent/tools 200", r.status_code == 200, r.text[:200])
check("工具数 = 31（13 只读 + 11 配置 + 7 昂贵）", d.get("count") == 31, str(d.get("count")))
check("含 expensive 类工具", any(t["risk"] == "expensive" for t in d.get("tools", [])))
check("暴露护栏参数", all(k in d.get("guards", {}) for k in
                     ("max_steps", "max_expensive", "max_turn_sec", "cooldown_sec")))

print("\n[3] 下发指令 → 总控自主执行")
r = api("POST", "/api/agent/chat", json={"message": "看看现在生产到哪了", "project": PROJ})
check("/api/agent/chat 200", r.status_code == 200, f"{r.status_code} {r.text[:250]}")
jid = r.json().get("job_id") if r.status_code == 200 else None
check("返回 job_id", bool(jid), r.text[:200])
if not jid:
    sys.exit(1)

deadline = time.time() + 40
job = None
while time.time() < deadline:
    job = api("GET", f"/api/agent/job/{jid}").json()
    if job.get("status") != "running":
        break
    time.sleep(1)
check("任务最终结束", job.get("status") == "done", json.dumps(job, ensure_ascii=False)[:300])
steps = job.get("steps") or []
check("执行了 2 个工具", len(steps) == 2, json.dumps(steps, ensure_ascii=False)[:300])
check("工具为只读探针", [s["tool"] for s in steps] == ["get_status", "get_progress"],
      str([s["tool"] for s in steps]))
check("每个步骤都有耗时", all(isinstance(s.get("elapsed_sec"), (int, float)) for s in steps))
check("返回了中文汇报", bool(job.get("reply")), str(job.get("reply"))[:120])
check("未消耗昂贵配额", job.get("expensive_used") == 0, str(job.get("expensive_used")))

print("\n[4] 审计日志")
r = api("GET", "/api/agent/log?limit=50")
d = r.json()
check("/api/agent/log 200", r.status_code == 200, r.text[:200])
check("日志里有本次调用", any(i.get("job_id") == jid for i in d.get("items", [])),
      str([i.get("tool") for i in d.get("items", [])][:10]))
check("日志记录成功与否", all("ok" in i for i in d.get("items", [])))

print("\n[5] 急停开关")
r = api("POST", "/api/agent/kill", json={"on": True, "reason": "e2e 测试"})
check("急停可开启", r.json().get("kill", {}).get("on") is True, r.text[:200])
r = api("POST", "/api/agent/chat", json={"message": "再跑一次", "project": PROJ})
check("急停期间拒绝下发", r.status_code >= 400, f"{r.status_code} {r.text[:160]}")
r = api("POST", "/api/agent/kill", json={"on": False})
check("急停可解除", r.json().get("kill", {}).get("on") is False, r.text[:200])

print("\n[6] 聊天历史仍然连贯")
r = api("GET", f"/api/ai/chat/history?project={PROJ}")
msgs = (r.json().get("state") or {}).get("messages") or []
roles = [m.get("role") for m in msgs]
check("用户消息已落历史", "user" in roles, str(roles))
check("总控回复已落历史", "assistant" in roles, str(roles))

print("\n[7] 清理")
api("POST", "/api/ai/chat/clear", json={"project": PROJ})
print("  已清空 e2e 测试会话")

print("\n[8] 还原被临时改动的 AI 配置")
try:
    _restore_chat()
    check("chat 模块已还原", True)
except Exception as e:  # noqa: BLE001
    check("chat 模块已还原", False, repr(e))

print("\n" + "=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("总控 AI 端到端验证全部通过")
