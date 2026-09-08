"""SQLite 访问层测试：db-spec 不变量（schema/id/constraints）。"""

from __future__ import annotations

import sqlite3

import pytest

from photo_gateway.db import TABLES, connect, init_db, migrate


def _user_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def test_init_creates_all_tables(tmp_path):
    db = tmp_path / "t.db"
    conn = init_db(db)
    assert _user_tables(conn) >= set(TABLES)
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    c1 = init_db(db)
    target = migrate(c1)
    c1.execute("INSERT INTO devices (id,name,enabled,created_at,updated_at) "
               "VALUES ('x','x',1,'t','t')")
    c1.commit()
    c1.close()
    # 二次打开重复 migrate 不应报错/不丢数据
    c2 = init_db(db)
    assert migrate(c2) == target
    assert c2.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
    c2.close()


def test_sha256_unique_constraint(tmp_path):
    conn = init_db(tmp_path / "t.db")
    now = "2026-09-01 10:00:00"
    ins = (
        "INSERT INTO photo_assets "
        "(sha256, original_filename, current_filename, file_size, archive_path, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)"
    )
    conn.execute(ins, ("AA", "a.jpg", "a.jpg", 1, "/p", now, now))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("AA", "b.jpg", "b.jpg", 1, "/p2", now, now))
        conn.commit()
    conn.close()


def test_foreign_key_enforced(tmp_path):
    conn = init_db(tmp_path / "t.db")
    now = "2026-09-01 10:00:00"
    # upload_item 引用不存在的 source_device => FK 错误
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO upload_sessions (id,source_device,status,created_at) "
            "VALUES ('s1','ghost','CREATED',?)",
            (now,),
        )
    conn.close()


def test_upload_sessions_layout(tmp_path):
    conn = init_db(tmp_path / "t.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(upload_sessions)")}
    assert {"id", "source_device", "client_ip", "total_files", "uploaded_files",
            "skipped_files", "duplicate_files", "failed_files", "status"} <= cols
    conn.close()
