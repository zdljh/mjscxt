# -*- coding: utf-8 -*-
"""D-07（P2）任务队列背压 + task_id 去重 · 离线回归脚本

对应缺陷：全项目缺陷审计报告 D-07 ——「任务队列 ``submit`` 无背压、无 ``task_id`` 去重
（同任务可被执行两次）」。开发 C 工作包。

验收标准（报告 §5 逐条）：
  ① 同 task_id 连提 3 次 → 只执行 1 次，其余返回 False 且日志有 warning
  ② 队列到 max_queue 后提交被拒且返回明确错误
  ③ concurrency=2 下并发提交不同 task_id 各自只跑一次

运行（零第三方依赖；``task_store`` 仅用标准库）：
    MJSCXT_AUTOPILOT=0 python verify_task_queue_backpressure.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import task_store  # noqa: E402

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


class _LogCapture(logging.Handler):
    """捕获 task_store logger 的告警/错误，用于断言「拒绝时留痕」"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):  # noqa: D102
        self.records.append(record)

    def texts(self, level=logging.WARNING):
        return [r.getMessage() for r in self.records if r.levelno >= level]


class _FakeStore:
    """替代 TaskStore（避免测试写 SQLite），记录 start/finish/fail 调用"""

    def __init__(self):
        self.lock = threading.Lock()
        self.started, self.finished, self.failed = [], [], []

    def start(self, task_id):
        with self.lock:
            self.started.append(task_id)

    def finish(self, task_id, result_path=""):
        with self.lock:
            self.finished.append(task_id)
            return ""

    def fail(self, task_id, error=""):
        with self.lock:
            self.failed.append(task_id)


def _new_queue(store, **kw):
    q = task_store.TaskQueue(store, **kw)
    q.start(thread_name="verify-taskq")
    return q


# ============================================================
print("=" * 72)
print("D-07 §1　同 task_id 连提 3 次 → 只执行 1 次")
print("=" * 72)

cap = _LogCapture()
task_store.logger.addHandler(cap)

store = _FakeStore()
runs = {"t1": 0}
ran_lock = threading.Lock()
q = _new_queue(store, concurrency=1, max_queue=8)

results = []


def _fn():
    with ran_lock:
        runs["t1"] += 1
    return ""


results.append(q.submit("t1", _fn))
results.append(q.submit("t1", _fn))
results.append(q.submit("t1", _fn))
q.join(timeout=10)
q.stop()

check("1.1 首次提交被接受（返回 True）", results[0] is True, f"results={results}")
check("1.2 重复提交返回 False", results[1] is False and results[2] is False,
      f"results={results}")
check("1.3 任务主体只执行 1 次（修复前会执行 2~3 次）", runs["t1"] == 1,
      f"实际执行 {runs['t1']} 次")
warns = cap.texts(logging.WARNING)
check("1.4 重复提交留下 warning 日志", any("重复提交" in t for t in warns),
      f"warns={warns}")
check("1.5 store 只 start 一次", store.started.count("t1") == 1,
      f"started={store.started}")

# ============================================================
print()
print("=" * 72)
print("D-07 §2　队列到 max_queue 后提交被拒（背压）")
print("=" * 72)

cap.records.clear()
store2 = _FakeStore()
# 不启动消费线程：任务全部积压在队列里，便于精确验证背压
q2 = task_store.TaskQueue(store2, concurrency=1, max_queue=3)
accepted = [q2.submit(f"job{i}", _fn) for i in range(5)]
rejected = [i for i, ok in enumerate(accepted) if not ok]

check("2.1 队列满（3）后后续提交被拒", accepted[:3] == [True, True, True]
      and accepted[3:] == [False, False], f"accepted={accepted}")
check("2.2 被拒数量正确（5 提 3 收）", len(rejected) == 2, f"rejected={rejected}")
errs = cap.texts(logging.ERROR)
check("2.3 队列满留下 error 日志（明确错误）",
      any("队列已满" in t for t in errs), f"errs={errs}")
check("2.4 status 暴露 max_queue / queued 可供观测",
      q2.status().get("max_queue") == 3 and q2.status().get("queued") == 3,
      f"status={q2.status()}")

# ============================================================
print()
print("=" * 72)
print("D-07 §3　concurrency=2 下不同 task_id 各跑一次")
print("=" * 72)

store3 = _FakeStore()
seen = {}
seen_lock = threading.Lock()
q3 = _new_queue(store3, concurrency=2, max_queue=16)

ids = [f"c{i}" for i in range(6)]
for tid in ids:
    def _mk(t):
        def _body():
            with seen_lock:
                seen[t] = seen.get(t, 0) + 1
            return ""
        return _body
    ok = q3.submit(tid, _mk(tid))
    assert ok, f"{tid} 不应被拒"

q3.join(timeout=15)
q3.stop()

check("3.1 6 个不同 task_id 全部被执行", sorted(seen.keys()) == sorted(ids),
      f"seen={sorted(seen.keys())}")
check("3.2 每个 task_id 恰好执行 1 次（无并发重复）",
      all(v == 1 for v in seen.values()), f"seen={seen}")
check("3.3 并发 2 下无重复 start",
      len(store3.started) == len(set(store3.started)) == 6,
      f"started={store3.started}")

# ============================================================
print()
print("=" * 72)
print("D-07 §4　边界：执行中重复提交被拒 / 完成后可再提交")
print("=" * 72)

store4 = _FakeStore()
gate = threading.Event()
q4 = _new_queue(store4, concurrency=1, max_queue=8)


def _blocking():
    gate.wait(timeout=10)
    return ""


check("4.1 提交阻塞任务被接受", q4.submit("busy", _blocking) is True)

# 等它真正进入执行态（_current == busy），再重复提交
for _ in range(100):
    if q4.status().get("current") == "busy":
        break
    time.sleep(0.02)
check("4.2 任务已进入执行态", q4.status().get("current") == "busy",
      f"status={q4.status()}")
check("4.3 执行中重复提交被拒（去重覆盖「执行中」而不只「队列中」）",
      q4.submit("busy", _blocking) is False)

gate.set()
q4.join(timeout=10)
check("4.4 任务完成后去重占位释放（可再次提交）",
      q4.submit("busy", lambda: "") is True, f"status={q4.status()}")
for _ in range(100):
    if q4.status().get("queued") == 0 and q4.status().get("current") is None:
        break
    time.sleep(0.02)
q4.stop()

# max_queue=0 表示不限制（保留旧行为，显式传才生效）
q5 = task_store.TaskQueue(_FakeStore(), concurrency=1, max_queue=0)
unbounded = [q5.submit(f"u{i}", lambda: "") for i in range(200)]
check("4.5 max_queue=0 显式关闭背压（不拒绝）", all(unbounded))
check("4.6 max_queue=0 时 status 如实呈现",
      q5.status().get("max_queue") == 0)

task_store.logger.removeHandler(cap)

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
print("✅ D-07 任务队列背压与去重全部通过")
sys.exit(0)
