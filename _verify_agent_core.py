# -*- coding: utf-8 -*-
"""agent_core 离线自检：不需要真实 LLM，验证工具定义与护栏是否成立。

运行： python _verify_agent_core.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  <- {detail}" if detail and not cond else ""))


import agent_core  # noqa: E402

# ---------- 1. 工具定义完整性 ----------
print("\n[1] 工具定义")
check("工具数量 > 15", len(agent_core.TOOLS) > 15, str(len(agent_core.TOOLS)))
names = [t["name"] for t in agent_core.TOOLS]
check("工具名无重复", len(names) == len(set(names)),
      str([n for n in names if names.count(n) > 1]))
check("每个工具都有 description/risk/call",
      all(t.get("description") and t.get("risk") and callable(t.get("call")) for t in agent_core.TOOLS))
check("risk 取值合法", all(t["risk"] in ("safe", "write", "expensive") for t in agent_core.TOOLS))
check("expensive 标记与 risk 一致",
      all(bool(t["expensive"]) == (t["risk"] == "expensive") for t in agent_core.TOOLS))
check("参数 schema 可 JSON 序列化",
      all(isinstance(json.loads(json.dumps(t["parameters"])), dict) for t in agent_core.TOOLS))
check("TOOL_SCHEMAS 为 function 形态",
      all(s["type"] == "function" and s["function"]["name"] for s in agent_core.TOOL_SCHEMAS))
check("无删除类工具", not any(k in n for n in names for k in ("delete", "remove", "clear", "drop", "purge")))

# ---------- 2. 每个工具都能构造出合法请求 ----------
print("\n[2] 请求构造（不实际执行）")
CTX = {"project": "测试项目", "episode": 1}
SAMPLE = {
    "shot_id": "3", "episode": 2, "scale": 2, "char_id": "c1", "q": "节奏",
    "patch": {"enable_upscale": True}, "settings": {"style": "国漫"},
    "video_path": "/tmp/a.mp4", "formats": ["fcpxml"], "action": "approve",
    "exception_id": "e1", "novel_id": "n1", "mode": "reference",
    "name": "主角", "outfit": "深蓝长衫",
}
for t in agent_core.TOOLS:
    try:
        method, path, payload = t["call"]({k: v for k, v in SAMPLE.items()}, CTX)
        ok = method in ("GET", "POST", "PUT") and path.startswith("/api/") and isinstance(payload, dict)
        check(f"{t['name']} -> {method} {path.split('?')[0][:52]}", ok, f"{path} {payload}")
    except Exception as e:  # noqa: BLE001
        check(f"{t['name']} 构造", False, repr(e))

# ---------- 3. 护栏 ----------
print("\n[3] 护栏")


class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def get_json(self, silent=True):
        return self._d


class _Client:
    def __init__(self):
        self.calls = []

    def get(self, path, **k):
        self.calls.append(("GET", path))
        return _Resp({"success": True, "echo": path})

    def post(self, path, json=None, **k):
        self.calls.append(("POST", path, json))
        return _Resp({"success": True, "echo": path})

    def put(self, path, json=None, **k):
        self.calls.append(("PUT", path, json))
        return _Resp({"success": True, "echo": path})


class _FakeApp:
    def __init__(self):
        self._c = _Client()

    def test_client(self):
        return self._c


fake = _FakeApp()
agent_core.bind_app(fake)

check("未知工具被拒", agent_core.execute_tool("no_such_tool", {}, CTX, {})["ok"] is False)

# 昂贵动作配额
stats = {"expensive": 0}
for i in range(agent_core.MAX_EXPENSIVE):
    r = agent_core.execute_tool("produce_episode", {"episode": 10 + i}, CTX, stats)
    check(f"昂贵动作第 {i+1} 次放行", r.get("ok") is True, str(r)[:120])
over = agent_core.execute_tool("produce_episode", {"episode": 99}, CTX, stats)
check("超出昂贵配额被拒", over.get("ok") is False and over.get("blocked") is True, str(over)[:160])
check("被拒时不消耗配额", stats["expensive"] == agent_core.MAX_EXPENSIVE, str(stats))

# 冷却（同参数复用）
s2 = {"expensive": 0}
r1 = agent_core.execute_tool("generate_tts", {"episode": 7}, CTX, s2)
n_before = len(fake._c.calls)
r2 = agent_core.execute_tool("generate_tts", {"episode": 7}, CTX, s2)
check("同参数冷却命中（不再重复烧卡）", r2.get("cached") is True, str(r2)[:160])
check("冷却时未真的再调一次", len(fake._c.calls) == n_before, f"{n_before} -> {len(fake._c.calls)}")
check("冷却复用时不额外计配额", s2["expensive"] == 1, str(s2))
r3 = agent_core.execute_tool("generate_tts", {"episode": 8}, CTX, s2)
check("不同参数不受冷却影响", r3.get("cached") is not True)

# 失败不进缓存
agent_core._APP = None
bad = agent_core.execute_tool("get_status", {}, CTX, {"expensive": 0})
check("内核未绑定时返回失败", bad.get("ok") is False)
agent_core.bind_app(fake)
ok2 = agent_core.execute_tool("get_status", {}, CTX, {"expensive": 0})
check("失败结果不进冷却缓存", ok2.get("cached") is not True and ok2.get("ok") is True)

# 急停
agent_core.set_kill(True, "单元测试")
k = agent_core.execute_tool("get_status", {}, CTX, {"expensive": 0})
check("急停时拒绝一切动作", k.get("ok") is False and k.get("blocked") is True, str(k)[:160])
agent_core.set_kill(False)
check("急停可解除", agent_core.kill_state()["on"] is False)

# ---------- 4. 降级解析 ----------
print("\n[4] 无 function-calling 时的降级解析")
txt = '我先看看进度。\n```json\n{"actions":[{"tool":"get_progress","args":{}},' \
      '{"tool":"retry_shot","args":{"shot_id":"3"}}],"reply":"已经在处理第3镜"}\n```\n完。'
acts, reply = agent_core._fallback_parse_actions(txt)
check("解析出 2 个动作", len(acts) == 2, str(acts))
check("动作字段正确", acts[0]["name"] == "get_progress" and acts[1]["args"]["shot_id"] == "3")
check("解析出回复文本", "第3镜" in reply, reply[:60])
a0, r0 = agent_core._fallback_parse_actions("这句话里没有任何 JSON")
check("无 JSON 时不误判", a0 == [] and isinstance(r0, str))

# ---------- 5. 任务互斥 ----------
print("\n[5] 任务互斥")
agent_core._BUSY.clear()
ep = {"base_url": "http://127.0.0.1:1/v1", "api_key": "x", "model": "m"}
j1 = agent_core.start_job("测试", "P", ep, [], timeout=1)
check("首个任务可启动", j1.get("ok") is True, str(j1))
j2 = agent_core.start_job("测试2", "P", ep, [], timeout=1)
check("同项目第二个任务被拒", j2.get("ok") is False, str(j2))
j3 = agent_core.start_job("测试3", "OTHER", ep, [], timeout=1)
check("不同项目可并行", j3.get("ok") is True, str(j3))
time.sleep(2.2)
agent_core._BUSY.clear()

# ---------- 汇总 ----------
print("\n" + "=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("agent_core 自检全部通过")
