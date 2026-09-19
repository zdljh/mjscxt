# -*- coding: utf-8 -*-
"""协作式中止信号 —— 让「暂停 / 停止」在秒级生效，但绝不打断在飞的 GPU 渲染。

为什么需要（2026-09-19 实测教训）
-------------------------------
`autopilot.pause()` 原本只在**步骤边界**生效，这是有意的：中途打断 ComfyUI 渲染会
留下半成品。但实测发现，一个步骤内部还嵌套着**两层重试**：

    pipeline._run_step_with_retry    （步骤级重试，退避 3~10s）
      └─ llm_client.chat_json_robust （截断 / 思考吃光额度的提额重试）
           └─ llm_client.chat_ex     （HTTP 瞬时故障退避重试，退避 3~8s）

这两层**从不检查任何中止信号**，叠加起来单步能卡十几分钟。用户点完「暂停」界面
毫无反应，会以为功能失效 —— 实际队列早停了，只是当前这一步还在自顾自重试。

本模块提供进程内、**上下文作用域**的协作式中止信号：
- 只在「准备发起新调用 / 准备退避休眠」这些**还没有产出任何文件**的时刻检查；
- 不杀线程、不中断在飞的 HTTP 请求或 GPU 任务 → 依旧不会产生半成品；
- 用 contextvars 限定作用域 → **只影响发起方自己那条执行链**：托管线程里跑的
  那一集会被暂停打断，用户手动触发的生产（没传 should_stop）完全不受影响。

用法::

    token = cancellation.push(should_stop)     # should_stop: fn() -> bool
    try:
        ...
        cancellation.check()                   # 该中止就抛 Cancelled
        cancellation.sleep(3)                  # 可被打断的退避休眠
    finally:
        cancellation.reset(token)
"""
from __future__ import annotations

import contextvars
import time

__all__ = ["Cancelled", "push", "reset", "should_stop", "check", "sleep", "active"]

#: 默认不可变空元组（contextvars 的默认值在多个上下文间共享，必须是不可变的）
_CHECKS: contextvars.ContextVar = contextvars.ContextVar("mjscxt_cancel_checks",
                                                       default=())


class Cancelled(Exception):
    """协作式中止：由上层（托管暂停 / 用户停止）触发，**不是错误**。

    刻意不继承 LLMError / PipelineError —— 那些会被各层 `except Exception` 捞走
    并当成「失败」记一次重试；本信号必须一路穿透到 run_episode 才转成 cancelled。
    """


def push(should_stop_fn) -> contextvars.Token:
    """在当前执行上下文注册一个「是否应中止」判定器，返回 token（供 reset）。"""
    if should_stop_fn is None:
        return None
    return _CHECKS.set(_CHECKS.get() + (should_stop_fn,))


def reset(token) -> None:
    """撤销 push 注册的判定器（务必放在 finally 里）。"""
    if token is None:
        return
    try:
        _CHECKS.reset(token)
    except (ValueError, LookupError):
        # 跨上下文 reset（例如在别的线程里 reset）会抛错；此时按「已失效」处理，
        # 不能让它影响主流程。真正的泄漏由 push/reset 成对调用避免。
        pass


def active() -> bool:
    """当前上下文是否注册了中止判定器（用于诊断 / 测试）"""
    return bool(_CHECKS.get())


def should_stop() -> bool:
    """是否已收到中止请求。判定器自身抛错时按「不中止」处理（fail-open）。"""
    for fn in _CHECKS.get():
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001  判定器故障不得影响生产
            continue
    return False


def check(reason: str = "") -> None:
    """该中止就抛 Cancelled；否则原样返回。

    调用点必须选在**尚未产出文件**的位置（发起 LLM/HTTP 调用前、退避休眠前），
    这样中断不会留下半成品。
    """
    if should_stop():
        raise Cancelled(reason or "已收到中止信号（托管暂停 / 用户停止）")


def sleep(seconds: float, slice_sec: float = 0.25) -> None:
    """可被打断的休眠。

    替代重试退避里的 `time.sleep(n)`：长退避期间点「暂停」原本要等满 n 秒才有
    反应，现在最多等 `slice_sec` 秒。
    """
    end = time.time() + max(0.0, float(seconds or 0))
    while True:
        left = end - time.time()
        if left <= 0:
            return
        check("退避等待期间收到中止信号")
        time.sleep(min(slice_sec, left))
