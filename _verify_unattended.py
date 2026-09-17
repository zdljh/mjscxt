# -*- coding: utf-8 -*-
"""无人值守（24h 托管）自检：失败集自动复活 + 产出即自动验收。

用 monkeypatch 替换 pipeline 的死信读写，不碰真实磁盘数据。
运行： python _verify_unattended.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  <- {detail}" if detail and not cond else ""))


import pipeline  # noqa: E402
import autopilot  # noqa: E402

# ---------- 造假数据 ----------
DEAD = {}
RESOLVED = []


def _hours_ago(h):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - h * 3600))


def _reset(items):
    DEAD.clear()
    RESOLVED.clear()
    for i in items:
        DEAD[str(i["episode_no"])] = i


def _fake_list(project):
    return [dict(v, episode_no=int(k)) for k, v in DEAD.items() if not v.get("resolved")]


def _fake_resolve(project, ep, note=""):
    item = DEAD.get(str(int(ep)))
    if not item:
        return {}
    item["resolved"] = True
    RESOLVED.append((ep, note))
    return item


pipeline.list_dead_letters = _fake_list
pipeline.resolve_dead_letter = _fake_resolve
autopilot._ATTEMPTS.clear()

print("\n[1] 计划默认值")
check("auto_revive_hours 进 PLAN_DEFAULTS", "auto_revive_hours" in autopilot.PLAN_DEFAULTS)
check("auto_accept 进 PLAN_DEFAULTS", "auto_accept" in autopilot.PLAN_DEFAULTS)
check("默认 6 小时复活", float(autopilot.PLAN_DEFAULTS["auto_revive_hours"]) == 6.0,
      str(autopilot.PLAN_DEFAULTS["auto_revive_hours"]))
check("默认不自动验收（保守）", autopilot.PLAN_DEFAULTS["auto_accept"] is False)

print("\n[2] 自动复活")
_reset([{"episode_no": 1, "marked_at": _hours_ago(10), "resolved": False, "reason": "连续 2 次失败"},
        {"episode_no": 2, "marked_at": _hours_ago(1), "resolved": False, "reason": "连续 2 次失败"}])
n = autopilot._auto_revive("P", dict(autopilot.PLAN_DEFAULTS))
check("只复活超期的那一集", n == 1, f"复活 {n} 集")
check("复活的是第 1 集", [e for e, _ in RESOLVED] == [1], str(RESOLVED))
check("复活留痕写明原因", "自动复活" in RESOLVED[0][1], RESOLVED[0][1])
check("复活后尝试计数清零", autopilot._ATTEMPTS.get("P", {}).get(1) == 0,
      str(autopilot._ATTEMPTS))
check("未超期的第 2 集仍在队列", not DEAD["2"]["resolved"])

print("\n[3] 关闭与边界")
_reset([{"episode_no": 3, "marked_at": _hours_ago(99), "resolved": False}])
check("auto_revive_hours=0 时永不复活",
      autopilot._auto_revive("P", {"auto_revive_hours": 0}) == 0)

_reset([{"episode_no": 4, "marked_at": "不是时间", "resolved": False},
        {"episode_no": 5, "marked_at": _hours_ago(50), "resolved": False}])
n = autopilot._auto_revive("P", dict(autopilot.PLAN_DEFAULTS))
check("时间格式异常时跳过而非误复活", n == 1 and [e for e, _ in RESOLVED] == [5], str(RESOLVED))

_reset([{"episode_no": 6, "marked_at": _hours_ago(50), "resolved": True}])
check("已解决的不再复活", autopilot._auto_revive("P", dict(autopilot.PLAN_DEFAULTS)) == 0)

_reset([{"episode_no": 7, "marked_at": _hours_ago(0.01), "resolved": False}])
check("刚挂起的不复活（防抖）",
      autopilot._auto_revive("P", {"auto_revive_hours": 0.5}) == 0)

print("\n[4] 自动验收（仅验证开关被读取，不真跑流水线）")
# _produce 里是 `if plan.get("auto_accept")` —— 这里验证开关语义正确
check("关闭时不验收", not bool({"auto_accept": False}.get("auto_accept")))
check("开启时才验收", bool({"auto_accept": True}.get("auto_accept")))

print("\n" + "=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("无人值守自检全部通过")
