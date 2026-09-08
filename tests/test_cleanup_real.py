"""Cleanup 真实删除(非dry)单元（安全内其实测删目录; 默认入周期仍 dry_run）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from photo_gateway.db import init_db
from photo_gateway.v2 import append_month_state
from photo_gateway.v2cycle import cleanup_expired


def _seed_removable(root, month="202609"):
    arch = Path(root) / "Photos"
    dd = arch / month[:4] / month
    dd.mkdir(parents=True)
    (dd / "p.jpg").write_bytes(b"x")
    conn = init_db(root / "db.sqlite")
    append_month_state(conn, month, "NORMAL", reason="init"); conn.commit()
    now = datetime.now() - timedelta(days=400)
    ts = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    conn.execute("INSERT INTO photo_sync_batches(started_at,status,task_count,success_count,failed_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                 (ts(now), "COMPLETED", 1, 1, 0, ts(now), ts(now)))
    b = conn.execute("SELECT max(id) m FROM photo_sync_batches").fetchone()["m"]
    conn.execute("INSERT INTO photo_sync_tasks(batch_id,archive_month,source_path,target_path,status,completed_at,created_at,updated_at) VALUES (?,?,?,?,'SUCCESS',?,?,?)",
                 (b, month, "/s", "/t", ts(now), ts(now), ts(now)))
    conn.commit()
    return conn, arch, dd


def test_real_delete_removes_month_dir(tmp_path):
    conn, arch, dd = _seed_removable(tmp_path, "202609")
    removed = cleanup_expired(conn, arch, retention_days=90, dry_run=False)
    assert removed == ["202609"]
    assert not dd.exists()
    conn.close()


def test_locked_abnormal_not_deleted(tmp_path):
    conn, arch, dd = _seed_removable(tmp_path, "202610")
    append_month_state(conn, "202610", "ABNORMAL", reason="fail"); conn.commit()
    removed = cleanup_expired(conn, arch, retention_days=90, dry_run=False)
    assert removed == [] and dd.exists()
    conn.close()
