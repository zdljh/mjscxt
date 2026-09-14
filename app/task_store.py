# -*- coding: utf-8 -*-
"""持久化任务队列 + 断点续跑（P0-4）

解决的问题
----------
改造前：任务状态（generation_state / upscale_tasks / dub_tasks ...）全部存在内存 dict 里，
进程重启即丢，H3 单集生成动辄数小时，中途崩一次就得从零重来。

本模块提供
----------
1. **任务落盘**：SQLite（默认 output/tasks.db）记录任务全生命周期，
   重启后仍可查询「哪些任务跑到哪了」；
2. **断点续跑**：以「最小可复用单元产物是否已落盘」为完成判据，
   重跑时自动跳过已完成单元（见 `is_unit_done` / `filter_pending_units`）；
3. **串行消费**：单 GPU 场景下任务必须排队（避免与 ComfyUI 抢显存），
   提供 `TaskQueue` 做串行调度与失败记录；
4. **崩溃恢复**：启动时把残留的 running 任务标记为 interrupted，
   便于前端提示「可继续」。

设计约束
--------
- **不侵入现有流程**：既有的内存 task_id 机制保持不动，本模块作为「旁路记录 + 续跑判据」接入；
- **单文件存储**：SQLite 免运维，不引入 Redis 等外部依赖；
- 所有异常都不得打断主流程（记录到 error 字段即可）。

任务状态机
----------
pending → running → (done | failed | interrupted | cancelled)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# 任务状态
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_INTERRUPTED = "interrupted"   # 进程重启导致的「跑到一半丢失」
ST_CANCELLED = "cancelled"

TERMINAL_STATES = (ST_DONE, ST_FAILED, ST_CANCELLED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    progress    REAL NOT NULL DEFAULT 0,
    total       REAL NOT NULL DEFAULT 0,
    result_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind);

CREATE TABLE IF NOT EXISTS units (
    task_id     TEXT NOT NULL,
    unit_key    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    result_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, unit_key)
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskStore:
    """SQLite 任务存储（线程安全；各方法独立开连接，避免跨线程复用）"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ---------- 底层 ----------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:  # noqa: BLE001
            pass
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # ---------- 任务 CRUD ----------

    def create(self, kind: str, project: str = "", label: str = "",
               payload: dict = None, total: float = 0,
               task_id: str = None) -> str:
        tid = task_id or uuid.uuid4().hex[:16]
        now = _now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO tasks (id, project, kind, label, payload, status, "
                    "progress, total, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tid, project, kind, label,
                     json.dumps(payload or {}, ensure_ascii=False),
                     ST_PENDING, 0, total, now, now))
                conn.commit()
            finally:
                conn.close()
        return tid

    def start(self, task_id: str) -> None:
        self._update(task_id, status=ST_RUNNING, started_at=_now())

    def set_progress(self, task_id: str, progress: float = None, total: float = None) -> None:
        fields = {}
        if progress is not None:
            fields["progress"] = float(progress)
        if total is not None:
            fields["total"] = float(total)
        if fields:
            self._update(task_id, **fields)

    def finish(self, task_id: str, result_path: str = "") -> None:
        self._update(task_id, status=ST_DONE, progress=None,
                     result_path=result_path, finished_at=_now(), error="")

    def fail(self, task_id: str, error: str) -> None:
        self._update(task_id, status=ST_FAILED, error=str(error)[:2000], finished_at=_now())

    def cancel(self, task_id: str) -> None:
        self._update(task_id, status=ST_CANCELLED, finished_at=_now())

    def _update(self, task_id: str, status: str = None, progress: float = None,
                result_path: str = None, error: str = None,
                started_at: str = None, finished_at: str = None,
                total: float = None) -> None:
        sets, vals = [], []
        mapping = [("status", status), ("progress", progress), ("result_path", result_path),
                   ("error", error), ("started_at", started_at),
                   ("finished_at", finished_at), ("total", total)]
        for col, val in mapping:
            if val is not None and not (col == "progress" and isinstance(val, float) and val != val):
                sets.append(f"{col}=?")
                vals.append(val)
        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(task_id)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
                conn.commit()
            finally:
                conn.close()

    def get(self, task_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def list(self, project: str = None, status: str = None,
             kind: str = None, limit: int = 100) -> list:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args = []
        if project:
            sql += " AND project=?"
            args.append(project)
        if status:
            sql += " AND status=?"
            args.append(status)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        conn = self._conn()
        try:
            rows = conn.execute(sql, args).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except Exception:  # noqa: BLE001
            d["payload"] = {}
        return d

    # ---------- 断点续跑：单元级 ----------

    def mark_unit(self, task_id: str, unit_key: str, status: str,
                  result_path: str = "", error: str = "") -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO units (task_id, unit_key, status, result_path, error, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (task_id, unit_key, status, result_path, error[:1000], _now()))
                conn.commit()
            finally:
                conn.close()

    def list_units(self, task_id: str) -> list:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM units WHERE task_id=?", (task_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def done_units(self, task_id: str) -> set:
        """已完成的单元键集合（用于跳过重算）"""
        return {u["unit_key"] for u in self.list_units(task_id)
                if u.get("status") == ST_DONE}

    def recycle_interrupted(self) -> int:
        """启动时调用：把残留的 running 任务标记为 interrupted，返回处理条数"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE tasks SET status=?, error=?, finished_at=?, updated_at=? "
                    "WHERE status=?",
                    (ST_INTERRUPTED, "进程重启导致任务中断，可从已完成单元继续",
                     _now(), _now(), ST_RUNNING))
                conn.commit()
                n = cur.rowcount or 0
            finally:
                conn.close()
        if n:
            logger.warning(f"检测到 {n} 个中断任务（进程重启），已标记为 interrupted，可续跑")
        return n


# ===================== 断点续跑判据 =====================

def is_unit_done(result_path: str, min_bytes: int = 1) -> bool:
    """单元完成判据：产物文件存在且非空。

    这是断点续跑的核心约定——**产物落盘即视为该单元已完成**，
    因此重跑时无需依赖任务状态表，磁盘本身就是最可靠的状态源。
    """
    if not result_path:
        return False
    try:
        return os.path.isfile(result_path) and os.path.getsize(result_path) >= min_bytes
    except OSError:
        return False


def filter_pending_units(units: Iterable[dict], key: str = "unit_key",
                         path: str = "result_path") -> list:
    """从单元列表中筛出「尚未完成」的单元（产物不存在或为空）

    units 形如 [{"unit_key": "shot_01", "result_path": "D:/.../shot_01.mp4"}, ...]
    """
    pending = []
    for u in units or []:
        if not isinstance(u, dict):
            continue
        p = u.get(path) or ""
        # 有产物路径且产物已存在 → 已完成，跳过
        if p and is_unit_done(p):
            continue
        pending.append(u)
    return pending


# ===================== 串行任务队列 =====================

class TaskQueue:
    """串行任务队列（单 GPU 场景：同一时刻只跑一个重任务，避免抢显存）

    用法：
        q = TaskQueue(store)
        q.submit(task_id, fn=lambda: do_work(), on_error=lambda e: ...)
        q.start()          # 启动后台消费线程（幂等）
        q.join(timeout=...)  # 等待队列排空（可选）
    """

    def __init__(self, store: TaskStore, concurrency: int = 1):
        self.store = store
        self.concurrency = max(1, int(concurrency))
        self._q: list = []
        self._cv = threading.Condition()
        self._threads: list = []
        self._running = False
        self._current: Optional[str] = None
        self._current_started: float = 0.0

    # ---------- 提交 ----------

    def submit(self, task_id: str, fn: Callable[[], object],
               on_error: Callable[[Exception], None] = None) -> None:
        with self._cv:
            self._q.append((task_id, fn, on_error))
            self._cv.notify()

    # ---------- 生命周期 ----------

    def start(self, thread_name: str = "mjscxt-taskq") -> None:
        if self._running:
            return
        self._running = True
        for i in range(self.concurrency):
            t = threading.Thread(target=self._worker, name=f"{thread_name}-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info(f"任务队列已启动（并发 {self.concurrency}）")

    def stop(self) -> None:
        with self._cv:
            self._running = False
            self._cv.notify_all()

    def join(self, timeout: float = None) -> bool:
        """等待队列排空 + 当前任务结束；返回是否已排空"""
        deadline = time.time() + timeout if timeout else None
        while True:
            with self._cv:
                empty = not self._q and self._current is None
            if empty:
                return True
            if deadline and time.time() > deadline:
                return False
            time.sleep(0.5)

    def status(self) -> dict:
        with self._cv:
            return {
                "running": self._running,
                "concurrency": self.concurrency,
                "queued": len(self._q),
                "current": self._current,
                "current_elapsed_sec": round(time.time() - self._current_started, 1)
                if self._current else 0,
            }

    # ---------- 消费 ----------

    def _worker(self) -> None:
        while True:
            with self._cv:
                while self._running and not self._q:
                    self._cv.wait(timeout=1.0)
                if not self._running and not self._q:
                    return
                if not self._q:
                    continue
                task_id, fn, on_error = self._q.pop(0)
                self._current = task_id
                self._current_started = time.time()

            try:
                self.store.start(task_id)
                result = fn()
                path = result if isinstance(result, str) else ""
                self.store.finish(task_id, result_path=path)
                logger.info(f"任务完成：{task_id}")
            except Exception as e:  # noqa: BLE001  任务失败不得拖垮队列
                logger.exception(f"任务失败：{task_id} - {e}")
                try:
                    self.store.fail(task_id, f"{type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass
                if on_error:
                    try:
                        on_error(e)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_error 回调自身抛错")
            finally:
                with self._cv:
                    self._current = None
                    self._cv.notify_all()


# ===================== 全局单例 =====================

_STORE: Optional[TaskStore] = None
_QUEUE: Optional[TaskQueue] = None
# 用 RLock（可重入）：get_queue 内部会调 get_store，两者共用同一把锁，
# 普通 Lock 会导致自锁死（已踩过坑，勿改回 Lock）。
_LOCK = threading.RLock()


def get_store(db_path: str) -> TaskStore:
    global _STORE
    with _LOCK:
        if _STORE is None or os.path.abspath(_STORE.db_path) != os.path.abspath(db_path):
            _STORE = TaskStore(db_path)
        return _STORE


def get_queue(db_path: str) -> TaskQueue:
    """取全局任务队列（注意：不能在 _LOCK 内调用 get_store，否则与 _LOCK 互锁）"""
    global _QUEUE
    with _LOCK:
        if _QUEUE is not None:
            return _QUEUE
    # 先在锁外取 store（get_store 自身需要 _LOCK），再回锁内写入 _QUEUE
    store = get_store(db_path)
    with _LOCK:
        if _QUEUE is None:
            _QUEUE = TaskQueue(store)
        return _QUEUE
