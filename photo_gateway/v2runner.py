"""V2 Worker：周期接入辅助（供 app lifespan 周期 ticker/调用）。

wrapper 于 photo_gateway/http 使用, 允许在未连 NAS 环境仍可替换 backend(测试 Fake). run_cycle 已在 v2cycle。
- v1_idle 依据后台 V1 worker 的 backlog（UPLOADED/PROCESSING 无 → true）
- 通过 make_backend(cfg,*source) 构建真实 RsyncNas；cfg.sync=None/enabled=false ⇒ 返回 None
- run_once：单轮 cycle（含 Cleanup），返回 summary；异常单轮抛由调用者 guard(Worker 不退出)
"""

from __future__ import annotations

from pathlib import Path

import sqlite3


def make_backend(cfg, source_root: Path):
    """依据配置构建 NAS 后端。未启用/缺 sync → None。真实: RsyncNas。"""
    from .v2rsync_transport import RsyncNas
    if cfg.sync is None or not cfg.sync.enabled:
        return None
    return RsyncNas(cfg.sync, source_root)


def v1_idle(conn) -> bool:
    """V1 worker(Processing 队列)无在跑/待处理项视为 idle（用于 V2 V1 Activity 门）。"""
    row = conn.execute(
        "SELECT COUNT(*) c FROM upload_items WHERE status IN ('UPLOADED','PROCESSING')"
    ).fetchone()
    return (row["c"] if row else 0) == 0


def run_once(conn: "sqlite3.Connection", cfg, archive_dir: Path, backend=None,
             cleanup: bool = True, dry_run_cleanup: bool = True,
             fail_threshold: int | None = None, retention: int | None = None,
             log=None) -> dict:
    """跑一轮 V2 cycle。backend 缺省按 cfg 建真后接；None→ 未启用 gate off。可覆盖失效/保留以测。

    log：透传给 run_cycle 的域日志（v2-spec §42），None=静默。
    """
    if backend is None:
        backend = make_backend(cfg, archive_dir)
    if backend is None:
        return {"gate": "sync_disabled"}
    from . import v2cycle as cyc

    threshold = fail_threshold if fail_threshold is not None else cfg.sync.failure.threshold
    retn = retention if retention is not None else cfg.sync.cleanup.synced_retention_days
    return cyc.run_cycle(
        conn, archive_dir, backend,
        fail_threshold=threshold, retention_days=retn,
        v1_idle=v1_idle(conn),
        cleanup=cleanup, dry_run_cleanup=dry_run_cleanup,
        log=log,
    )


def cycle_interval(cfg) -> int:
    if cfg.sync is None:
        return 0
    return cfg.sync.worker.cycle_interval_seconds
