"""阶段5 可靠性测试：.tmp 清理过期文件 / 磁盘保护三档分类。"""

from __future__ import annotations

import os
import time

from photo_gateway.reliability import (
    StorageState,
    cleanup_tmp,
    disk_level,
)


def _age_file(p: "os.PathLike", seconds_old: float) -> None:
    import pathlib

    path = pathlib.Path(p)
    stat = os.stat(path)
    os.utime(path, (stat.st_atime, time.time() - seconds_old))


def test_cleanup_removes_only_old_tmp(tmp_path):
    (tmp_path / "old.tmp").write_bytes(b"x")
    (tmp_path / "fresh.tmp").write_bytes(b"y")
    (tmp_path / "normal.jpg").write_bytes(b"z")  # 不是 .tmp，不清理
    _age_file(tmp_path / "old.tmp", 8 * 86400)   # 8 天 > 7
    n = cleanup_tmp(tmp_path, max_age_days=7)
    assert n == 1
    assert not (tmp_path / "old.tmp").exists()
    assert (tmp_path / "fresh.tmp").exists()
    assert (tmp_path / "normal.jpg").exists()


def test_cleanup_empty_dir(tmp_path):
    assert cleanup_tmp(tmp_path / "absent") == 0


def test_disk_level_classification():
    assert disk_level(50, 80, 90, 95) == "normal"
    assert disk_level(80, 80, 90, 95) == "warning"
    assert disk_level(91, 80, 90, 95) == "critical"
    assert disk_level(95, 80, 90, 95) == "stop"
    assert disk_level(99, 80, 90, 95) == "stop"


def test_storage_state_report_and_accept(tmp_path):
    s = StorageState(tmp_path, warning=80, critical=90, stop=95)
    rep = s.report(use_override=60.0)
    assert rep["level"] == "normal"
    assert s.accepts_uploads(use_override=60.0) is True
    assert StorageState(tmp_path, 80, 90, 95).accepts_uploads(use_override=97.0) is False


def test_recover_processing_moves_back_and_syncs(tmp_path):
    from photo_gateway.db import init_db
    from photo_gateway.reliability import recover_processing

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    incoming.mkdir(); processing.mkdir()
    conn = init_db(tmp_path / "db" / "pg.db")
    now = "2026-09-01 10:00:00"
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('d','d',1,?,?)", (now, now))
    conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at) VALUES ('S','d','CREATED',?)", (now,))
    conn.execute(
        "INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
        "file_size,status,precheck_status,user_confirmation,created_at,updated_at)"
        " VALUES ('S','d','P','resume.jpg',5,'PROCESSING','NO_MATCH',1,?,?)", (now, now))
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    (processing / "resume.jpg").write_bytes(b"12345")

    n = recover_processing(conn, processing, incoming)
    assert n == 1
    assert not (processing / "resume.jpg").exists()
    assert (incoming / "resume.jpg").exists()
    row = conn.execute("SELECT status FROM upload_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "UPLOADED"
    conn.close()
