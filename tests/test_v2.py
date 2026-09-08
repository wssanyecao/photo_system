"""V2 领域测试：schema v2 / 月份目录 / Month State 历史追加 / Batch·Task。"""

from __future__ import annotations

from pathlib import Path

import pytest

from photo_gateway.db import init_db, migrate
from photo_gateway.v2 import (
    MS_ABNORMAL,
    MS_NORMAL,
    MS_RETRY,
    append_month_state,
    create_batch,
    create_sync_task,
    current_month_state,
    finish_sync_task,
    month_dirs,
    months_by_state,
    valid_month,
)


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "db.sqlite")
    yield c
    c.close()


def test_valid_month():
    assert valid_month("202604")
    assert not valid_month("2026041")
    assert not valid_month("")


def test_migration_v2_creates_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"photo_sync_batches", "photo_sync_tasks", "photo_sync_month_states"} <= names


def test_migrate_idempotent_v2(conn):
    m = migrate(conn)
    assert m == 3   # 1=V1 表, 2=V2 表, 3=photo_assets.file_missing_at（幂等）


def _mk_months(root, months):
    for m in months:
        d = root / m[:4] / m
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.jpg").write_bytes(b"x")


def test_month_dirs_scan(tmp_path):
    _mk_months(tmp_path, ["202604", "202608", "202601"])
    rels = [rel for rel, _ in month_dirs(tmp_path)]
    # rel 形如 "2026/202608"，yyyymm 为该 path basename
    months_of = {m: Path(r).name for r, m in month_dirs(tmp_path)}
    got = {Path(r).name for r, _ in month_dirs(tmp_path)}
    assert {"202601", "202604", "202608"} <= got
    assert months_of["202608"] == "202608"


def test_month_state_append_history_and_current(conn):
    append_month_state(conn, "202604", MS_NORMAL, reason="init")
    append_month_state(conn, "202604", MS_ABNORMAL, reason="fail", consecutive_failed=3,
                       abnormal_at="2026-09-01 00:00:00")
    append_month_state(conn, "202604", MS_RETRY, retry_at="2026-09-02 00:00:00", retry_by="admin")
    # 三条全部保留，当前=最新(RETRY_REQUESTED)
    cnt = conn.execute("SELECT COUNT(*) FROM photo_sync_month_states WHERE archive_month='202604'").fetchone()[0]
    assert cnt == 3
    cur = current_month_state(conn, "202604")
    assert cur["state"] == MS_RETRY and cur["retry_requested_by"] == "admin"


def test_history_never_modified_after_retry(conn):
    append_month_state(conn, "202604", MS_ABNORMAL, reason="first fail")
    append_month_state(conn, "202604", MS_NORMAL, reason="recovered")
    first = conn.execute(
        "SELECT state, reason FROM photo_sync_month_states WHERE archive_month='202604'"
        " ORDER BY id LIMIT 1").fetchone()
    assert first["state"] == MS_ABNORMAL  # 未被改写


def test_batch_task_flow(conn):
    bid = create_batch(conn)
    tid1 = create_sync_task(conn, bid, "202608", "/Photos/2026/202608", "/nas/Photos/2026/202608")
    tid2 = create_sync_task(conn, bid, "202608", "/Photos/2026/202608", "/nas/Photos/2026/202608")
    finish_sync_task(conn, tid1, ok=True, exit_code=0)
    finish_sync_task(conn, tid2, ok=False, exit_code=23, stderr="rsync: some error")
    rows = conn.execute(
        "SELECT id,status,exit_code FROM photo_sync_tasks WHERE batch_id=? ORDER BY id", (bid,)
    ).fetchall()
    assert rows[0]["status"] == "SUCCESS" and rows[0]["exit_code"] == 0
    assert rows[1]["status"] == "FAILED"
    # 同一月两条 task 均保留（archive_month 不 UNIQUE）
    assert conn.execute("SELECT COUNT(*) FROM photo_sync_tasks").fetchone()[0] == 2
