"""V2 领域辅助（NAS 同步月份 & Month State）——先落数据闭环，rsync 传输适配另置于 M6b。

对齐 v2-spec：
- 归档月管理单位为 Photos/YYYY/YYYYMM（§ V）
- Month State 为 “历史追加”模型，同月多条记录；当前状态 = max(created_at,id)（§38/§40）
- state 取值：NORMAL/ABNORMAL/RETRY_REQUESTED（§11-13）
- Sync Task/Batch 字段在 db 迁移 v2 中（photo_sync_batches/tasks/month_states）
- 本模块不执行真实删除/NAS 传输（M6 Cleanup/rsync adapter 单列）
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SH = timezone(timedelta(hours=8))

# 状态
MS_NORMAL = "NORMAL"
MS_ABNORMAL = "ABNORMAL"
MS_RETRY = "RETRY_REQUESTED"
ALL_STATES = frozenset({MS_NORMAL, MS_ABNORMAL, MS_RETRY})

TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_SUCCESS = "SUCCESS"
TASK_FAILED = "FAILED"
TASK_TERMINAL = frozenset({TASK_SUCCESS, TASK_FAILED})


def now_str(t: datetime | None = None) -> str:
    return (t or datetime.now(SH)).strftime("%Y-%m-%d %H:%M:%S")


def valid_month(month: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", month or ""))


def month_dirs(archive_root: Path) -> list[tuple[str, str]]:
    """扫描 Photos 归档根下的 YYYY/YYYYMM。返回 [(rel_path'2026/202608', 'YYYYMM'), …]按 YYYYMM 排序。"""
    out: list[tuple[str, str]] = []
    if not archive_root.is_dir():
        return out
    for year_dir in sorted(p for p in archive_root.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}", p.name)):
        for m in sorted(q.name for q in year_dir.iterdir()
                        if q.is_dir() and re.fullmatch(r"\d{6}", q.name) and q.name.startswith(year_dir.name)):
            out.append((f"{year_dir.name}/{m}", m))
    return out


# ---- Month State 历史（追加） ----
def append_month_state(conn, month: str, state: str, reason: str = "",
                       consecutive_failed: int = 0, abnormal_at=None,
                       retry_at=None, retry_by=None) -> int:
    if not valid_month(month):
        raise ValueError(f"非法 YYYYMM: {month!r}")
    if state not in ALL_STATES:
        raise ValueError(f"非法的 state: {state!r}")
    now = now_str()
    cur = conn.execute(
        "INSERT INTO photo_sync_month_states"
        " (archive_month, state, consecutive_failed_count, abnormal_at, retry_requested_at,"
        "  retry_requested_by, reason, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (month, state, consecutive_failed, abnormal_at, retry_at, retry_by, reason, now, now),
    )
    return cur.lastrowid


def current_month_state(conn, month: str):
    """返回该月最新 Month State（无则 None）。追加历史不修改旧记录（§39）。"""
    if not valid_month(month):
        raise ValueError(f"非法 YYYYMM: {month!r}")
    return conn.execute(
        "SELECT * FROM photo_sync_month_states WHERE archive_month=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1", (month,)
    ).fetchone()


def months_by_state(conn, state: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ms.* FROM photo_sync_month_states ms"
        " JOIN (SELECT archive_month m, MAX(id) maxid FROM photo_sync_month_states"
        "       GROUP BY archive_month) t ON ms.id=t.maxid"
        " WHERE ms.state=? ORDER BY ms.archive_month", (state,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---- Batch / Tasks 支撑 ----
def create_batch(conn) -> int:
    now = now_str()
    cur = conn.execute(
        "INSERT INTO photo_sync_batches (started_at,status,task_count,created_at,updated_at)"
        " VALUES (?,'RUNNING',0,?,?)", (now, now, now),
    )
    conn.commit()
    return cur.lastrowid


def create_sync_task(conn, batch_id: int, month: str,
                     source_path: str, target_path: str) -> int:
    now = now_str()
    cur = conn.execute(
        "INSERT INTO photo_sync_tasks"
        " (batch_id, archive_month, source_path, target_path, status, created_at, updated_at)"
        " VALUES (?,?,?,?, 'PENDING',?,?)",
        (batch_id, month, source_path, target_path, now, now),
    )
    conn.commit()
    return cur.lastrowid


def finish_sync_task(conn, task_id: int, ok: bool, exit_code=None,
                     stdout="", stderr="", error_message="") -> None:
    now = now_str()
    status = TASK_SUCCESS if ok else TASK_FAILED
    conn.execute(
        "UPDATE photo_sync_tasks SET status=?, started_at=COALESCE(started_at,?), completed_at=?,"
        " exit_code=?, stdout=?, stderr=?, error_message=?, updated_at=? WHERE id=?",
        (status, now, now, exit_code, stdout, stderr, error_message, now, task_id),
    )
    conn.commit()
