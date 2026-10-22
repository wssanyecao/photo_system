"""后台执行器：API 主事件循环之外运行 V1 与 V2。

V1 线程负责归档轮询和每日维护，V2 线程负责 NAS 同步。两者通过归档锁串行访问
Photos，避免 V2 差异检查/rsync 与 V1 文件落盘并发；HTTP API 保持响应。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .db import connect

FALLBACK_LOG = logging.getLogger("photo_gateway.background")
V1_INTERVAL_SECONDS = 2
MAINTENANCE_INTERVAL_SECONDS = 24 * 3600


def _report_fallback(message: str, exc: BaseException) -> None:
    """结构化日志自身失败时仍向 stderr/进程日志提供可观测出口。"""
    FALLBACK_LOG.exception("%s: %s", message, exc)


class BackgroundWorkers:
    """管理两个长期后台线程及其停止/协调生命周期。"""

    def __init__(self, cfg, db_path: Path, dirs: dict, logman) -> None:
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.dirs = dirs
        self.logman = logman
        self.stop_event = threading.Event()
        self.archive_lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        if self.threads:
            return
        # 在 V2 差异检测前修复移动文件与数据库之间的崩溃窗口。
        from .recycle import reconcile
        from .db import init_db
        with self.archive_lock:
            conn = init_db(self.db_path)
            try:
                reconcile(conn, self.cfg)
            finally:
                conn.close()
        self.threads.append(self._spawn("photo-v1-worker", self._v1_loop))
        if self.cfg.sync is not None and self.cfg.sync.enabled:
            self.threads.append(self._spawn("photo-v2-worker", self._v2_loop))

    def stop(self, timeout: float = 620.0) -> None:
        """等待当前轮结束；上限覆盖 V2 rsync 的 600 秒超时。"""
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._log_error(
                    "background thread did not stop before deadline",
                    event="background_stop_timeout", thread=thread.name,
                )
        self.threads.clear()

    def status(self) -> dict:
        """返回管理 API 可公开的线程状态，不暴露内部对象。"""
        threads = {thread.name: thread.is_alive() for thread in self.threads}
        return {
            "status": "running" if threads and all(threads.values()) else "degraded",
            "mode": "threaded-v1-v2",
            "threads": threads,
        }

    def _spawn(self, name: str, target) -> threading.Thread:
        thread = threading.Thread(name=name, target=target, daemon=True)
        thread.start()
        return thread

    def _log_error(self, message: str, *, event: str, exc: BaseException | None = None,
                   **fields) -> None:
        if self.logman is None:
            _report_fallback(message, exc or RuntimeError(message))
            return
        try:
            self.logman.processing().error(
                message, event=event, error=str(exc) if exc else None,
                exc_info=exc is not None, **fields,
            )
        except Exception as log_exc:
            _report_fallback(f"{message}; structured logging failed", log_exc)

    def _v1_loop(self) -> None:
        from .http import _run_maintenance
        from .worker import run_round

        next_maintenance = 0.0  # 与旧实现一致：启动先执行一轮
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now >= next_maintenance:
                try:
                    _run_maintenance(self.cfg, self.dirs, self.logman)
                    if self.cfg.recycle_bin.enabled:
                        from .recycle import purge_expired
                        with self.archive_lock:
                            conn = connect(self.db_path)
                            try:
                                from .recycle import expire_pending_reviews
                                n = (purge_expired(conn, self.cfg)
                                     if self.cfg.recycle_bin.auto_purge else 0)
                                expire_pending_reviews(conn, self.cfg)
                                if n and self.logman:
                                    self.logman.audit().info(
                                        f"recycle bin purged count={n}", event="recycle_purged")
                            finally:
                                conn.close()
                except Exception as exc:
                    self._log_error(
                        "maintenance round error", event="maintenance_error", exc=exc
                    )
                next_maintenance = now + MAINTENANCE_INTERVAL_SECONDS

            try:
                with self.archive_lock:
                    conn = connect(self.db_path)
                    try:
                        run_round(conn, self.cfg, self.dirs, self.logman)
                    finally:
                        conn.close()
            except Exception as exc:
                self._log_error("v1 worker round error", event="v1_worker_error", exc=exc)
            self.stop_event.wait(V1_INTERVAL_SECONDS)

    def _v2_loop(self) -> None:
        from .v2runner import run_once

        interval = self.cfg.sync.worker.cycle_interval_seconds
        source_root = self.cfg.resolved_sync_source()
        log = self.logman.processing() if self.logman is not None else None
        while not self.stop_event.is_set():
            try:
                with self.archive_lock:
                    conn = connect(self.db_path)
                    try:
                        run_once(
                            conn, self.cfg, source_root,
                            cleanup=self.cfg.sync.cleanup.enabled,
                            dry_run_cleanup=True,
                            log=log,
                        )
                    finally:
                        conn.close()
            except Exception as exc:
                self._log_error("v2 cycle error", event="v2_cycle_error", exc=exc)
            self.stop_event.wait(interval)
