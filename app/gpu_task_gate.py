# -*- coding: utf-8 -*-
"""B-01 P1-12：GPU 任务并发闸门 + /free 互斥守卫

解决的问题
----------
- 单 GPU 场景下，手动接口 + 托管守护线程 + 总控 AI 三方并发时无并发上限，
  同时打 ComfyUI → OOM / 任务随机失败 / 显存反复换入换出。
- 超分 / TTS 调用 ComfyUI /free（unload_models=True）时**无条件**卸载其它
  任务正在使用的模型 → 反复换入换出，比「无上限」更直接的伤害来源。

设计
----
1. **进程级 Semaphore(TASK_QUEUE_CONCURRENCY)**：覆盖手动 GPU 链路
   （分镜 / 视频 / 关键帧 / 超分），超出上限时排队 + 告警，不静默丢弃。
2. **running 计数**：供 /free 调用方判断「本进程是否有其它 running GPU 任务」，
   有则不发 /free（避免卸掉他人正在用的模型）。
3. **可观测性**：status() 暴露 running/concurrency/queued，
   供 /api/system/status 展示，修复 TaskQueue.status() 失真（恒 running:false）。

不做什么
--------
- 不接管 task_db 生命周期（start/finish/fail）——各 worker 内部已写好，
  gate 只做并发限制 + 互斥守卫。
- 不删除 TaskQueue 模块（保留 .status() 可观测性，但**不真正接线 submit**——
  改用 Semaphore 方案对齐 P1-12 修复建议 ②，改动面最小且最稳）。
- 不接管非 GPU worker（dub / mix / novel / episodes / analyze）——它们不占
  GPU，无需闸门。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Optional, Set

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

_concurrency_loaded = False
_concurrency = 1


def _ensure_loaded() -> None:
    global _concurrency_loaded, _concurrency
    if _concurrency_loaded:
        return
    try:
        from config import TASK_QUEUE_CONCURRENCY
        _concurrency = max(1, int(TASK_QUEUE_CONCURRENCY))
    except Exception:  # noqa: BLE001
        _concurrency = 1
    _concurrency_loaded = True


def _ensure_state() -> None:
    """延迟初始化（避免 import 时 config 未就绪）"""
    _ensure_loaded()
    with _LOCK:
        if _module_state._sem is None:
            _module_state._sem = threading.Semaphore(_concurrency)


class _State:
    """模块级状态容器"""
    _sem: Optional[threading.Semaphore] = None
    _running_count: int = 0
    _running_tasks: "Set[str]" = set()


_module_state = _State()


def concurrency() -> int:
    """当前配置的 GPU 并发上限"""
    _ensure_loaded()
    return _concurrency


@contextmanager
def run_gpu_task(task_id: str, label: str = ""):
    """GPU 任务并发闸门（context manager）

    用法：
        with gpu_task_gate.run_gpu_task(task_id, "视频生成第1集"):
            do_gpu_work()

    行为：
    - 获取 Semaphore（超出并发上限时阻塞排队）
    - running 计数 +1
    - 执行主体
    - finally 中 running 计数 -1，释放 semaphore
    - 排队超过 30 秒打 warning（可观测性：不静默）

    注意：本函数**不吞异常**——主体抛错向上传播，由调用方（worker）
    自行记录 task_db.fail()。
    本函数**不接管 task_db 生命周期**（start/finish/fail 保留在 worker 内）。
    """
    _ensure_state()
    sem = _module_state._sem

    import time as _time
    _t0 = _time.time()
    acquired = sem.acquire(timeout=3600 * 24)  # 长期等待，实际永不超时
    waited = _time.time() - _t0
    if not acquired:
        logger.error("GPU 任务 %s 排队超时（24h），放弃执行", task_id)
        return
    if waited > 30:
        with _LOCK:
            _cur = _module_state._running_count
        logger.warning("GPU 任务 %s 排队 %.1fs（当前并发 %d/%d）",
                       task_id, waited, _cur, _concurrency)

    with _LOCK:
        _module_state._running_count += 1
        _module_state._running_tasks.add(task_id)
        _cur = _module_state._running_count
    logger.info("GPU 任务 %s 开始执行（%s；当前 running=%d/%d）",
                task_id, label, _cur, _concurrency)

    try:
        yield
    finally:
        with _LOCK:
            _module_state._running_count -= 1
            _module_state._running_tasks.discard(task_id)
        sem.release()
        logger.info("GPU 任务 %s 执行结束（%s）", task_id, label)


def has_other_running_gpu_tasks(exclude_task_id: str = "") -> bool:
    """本进程是否有其它 running 的 GPU 任务（/free 互斥守卫用）

    调用方：upscale_client / tts_client 在发 /free 前检查。
    若 exclude_task_id 非空，排除自身（「我」不算「他人」）。
    本函数**无阻塞**（仅读 running 计数），不影响调用方性能。
    """
    _ensure_state()
    with _LOCK:
        if _module_state._running_count == 0:
            return False
        if exclude_task_id and _module_state._running_tasks == {exclude_task_id}:
            return False
        return True


def status() -> dict:
    """当前闸门状态（供 /api/system/status 暴露，修复失真可观测性）"""
    _ensure_state()
    with _LOCK:
        return {
            "running": _module_state._running_count,
            "concurrency": _concurrency,
            "running_tasks": list(_module_state._running_tasks),
        }
