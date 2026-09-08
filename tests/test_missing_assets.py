"""V1.3 soft-missing：已删(本地缺失)资产标记与展示/复活/对账全链路测试。

覆盖：
- DB 迁移 v1 → 加 file_missing_at 列
- /photos 列表与 total 过滤缺失资产
- thumbnail/file 404 惰性标记
- Cleanup 真删后批量标记该月资产
- worker：同内容重传复活缺失资产（asset id 不变）
- mark_missing_assets 对账（历史/外部删除）
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml as _y

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.worker import (  # noqa: F401
    compute_sha256,
    mark_missing_assets,
    process_once,
)


def _cfg_yaml(root):
    return load_yaml_text(_y.safe_dump({
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "d1", "enabled": True}],
        "storage": {"root": str(root)},
        "metadata": {"date_priority": ["DateTimeOriginal", "file_mtime"]},
    }, sort_keys=False))


def _seed(root, content, filename="IMG_001.jpg"):
    """建 UPLOADED upload_item + incoming 文件；返回 (conn, item_id, incoming, db_path)。"""
    import uuid
    incoming = Path(root) / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    db_path = Path(root) / "db" / "pg.db"
    conn = init_db(db_path)
    now = "2026-09-01 10:00:00"
    if not conn.execute("SELECT 1 FROM devices WHERE id='d1'").fetchone():
        conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                     " VALUES ('d1','d',1,?,?)", (now, now))
    sid = f"S{str(uuid.uuid4())[:8]}"
    conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at)"
                 " VALUES (?, 'd1','CREATED',?)", (sid, now))
    conn.execute(
        "INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
        "file_size,status,precheck_status,user_confirmation,created_at,updated_at)"
        " VALUES (?, 'd1',?,?,?, 'UPLOADED','NO_MATCH',0,?,?)",
        (sid, filename, filename, len(content), now, now))
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    src = incoming / filename
    src.write_bytes(content)
    os.utime(src, (time.mktime(datetime(2026, 8, 15).timetuple()),) * 2)
    return conn, item_id, incoming, db_path


def _insert_asset(conn, sha: str, archive_path: str, name: str = "a.jpg") -> int:
    now = "2026-09-01 10:00:00"
    conn.execute(
        "INSERT INTO photo_assets(sha256,original_filename,current_filename,file_size,"
        " mime_type,date_taken,date_source,archive_path,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sha, name, name, 5, "image/jpeg", "2026-08-01T10:00:00", "file_mtime",
         archive_path, now, now))
    aid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return aid


# ---------------------------------------------------------------------------
# DB 迁移：v1 库加 file_missing_at 列
# ---------------------------------------------------------------------------

def test_migration_adds_file_missing_at_column(tmp_path):
    import sqlite3
    from photo_gateway import db as d
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(d.SCHEMA_SCRIPTS[0][1])          # v1（V1 全套表）
    conn.execute("INSERT INTO schema_version (version, updated_at) VALUES (1,'x')")
    conn.commit()
    conn.close()
    c = init_db(p)
    assert c.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] == 3
    cols = [r[1] for r in c.execute("PRAGMA table_info(photo_assets)")]
    assert "file_missing_at" in cols
    c.close()


# ---------------------------------------------------------------------------
# /photos 过滤缺失资产（列表与 total）
# ---------------------------------------------------------------------------

def test_photos_list_hides_missing_and_total(tmp_path):
    from fastapi.testclient import TestClient
    from photo_gateway.http import create_app
    root = tmp_path
    dbp = root / "db.sqlite"
    conn = init_db(dbp)
    _insert_asset(conn, "A" * 64, "Photos/2026/202601/keep.jpg", "keep.jpg")
    _insert_asset(conn, "B" * 64, "Photos/2026/202601/gone.jpg", "gone.jpg")
    # 标记 gone 为缺失
    conn.execute("UPDATE photo_assets SET file_missing_at='2026-09-02 00:00:00'"
                 " WHERE archive_path LIKE '%gone%'")
    conn.commit()
    conn.close()
    app = create_app(_cfg_yaml(root), db_path=dbp)
    with TestClient(app) as c:
        j = c.get("/api/v1/photos?limit=50").json()["data"]
        assert j["total"] == 1
        assert len(j["photos"]) == 1
        assert "keep.jpg" in j["photos"][0]["archive_path"]


# ---------------------------------------------------------------------------
# thumbnail/file 404 → 惰性标记缺失
# ---------------------------------------------------------------------------

def test_file_404_marks_asset_missing(tmp_path):
    from fastapi.testclient import TestClient
    from photo_gateway.http import create_app
    root = tmp_path
    photo = root / "Photos" / "2026" / "202601"
    photo.mkdir(parents=True)
    (photo / "x.jpg").write_bytes(b"12345")
    dbp = root / "db.sqlite"
    conn = init_db(dbp)
    aid = _insert_asset(conn, "C" * 64, "Photos/2026/202601/x.jpg", "x.jpg")
    conn.close()
    # 删除原图 → thumbnail 404 且落库标记
    (photo / "x.jpg").unlink()
    app = create_app(_cfg_yaml(root), db_path=dbp)
    with TestClient(app) as c:
        assert c.get(f"/api/v1/photos/{aid}/thumbnail").status_code == 404
        assert c.get(f"/api/v1/photos/{aid}/file").status_code == 404
    conn = init_db(dbp)
    row = conn.execute("SELECT file_missing_at FROM photo_assets WHERE id=?", (aid,)).fetchone()
    assert row["file_missing_at"] is not None
    conn.close()


# ---------------------------------------------------------------------------
# worker：同内容重传复活缺失资产
# ---------------------------------------------------------------------------

def test_reupload_resurrects_missing_asset(tmp_path):
    root = tmp_path
    content = b"resurrect-me-bytes"
    conn, it1, incoming, _ = _seed(root, content)
    item1 = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    r1 = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item1, None)
    assert r1["status"] == "COMPLETED"
    aid = r1["asset_id"]
    asset = conn.execute("SELECT archive_path, sha256 FROM photo_assets WHERE id=?", (aid,)).fetchone()
    full = Path(root) / "Photos" / Path(asset["archive_path"]).relative_to("Photos")
    assert full.exists()
    # 模拟 Cleanup/外部删除：删文件 + 标记缺失
    full.unlink()
    conn.execute("UPDATE photo_assets SET file_missing_at='2026-09-02 00:00:00' WHERE id=?", (aid,))
    conn.commit()
    conn.close()

    # 同内容再次导入（不同文件名也会 sha 命中）
    conn, it2, incoming, _ = _seed(root, content, "rename.jpg")
    item2 = conn.execute("SELECT * FROM upload_items WHERE id=?", (it2,)).fetchone()
    r2 = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item2, None)
    assert r2["status"] == "DUPLICATE"
    assert r2["repaired"] is True
    assert r2["asset_id"] == aid                      # 身份不变
    asset2 = conn.execute(
        "SELECT archive_path, file_missing_at, sha256 FROM photo_assets WHERE id=?", (aid,)
    ).fetchone()
    assert asset2["file_missing_at"] is None          # 标记已清
    assert asset2["archive_path"] == asset["archive_path"]
    assert (Path(root) / "Photos" / Path(asset["archive_path"]).relative_to("Photos")).exists()
    conn.close()


# ---------------------------------------------------------------------------
# Cleanup 真删 → 批量标记该月资产；dry-run 不标记
# ---------------------------------------------------------------------------

def test_cleanup_applied_marks_month_assets(tmp_path):
    from photo_gateway.v2 import append_month_state
    from photo_gateway.v2cycle import cleanup_expired
    root = tmp_path
    arch = root / "Photos"
    month_dir = arch / "2026" / "202601"
    month_dir.mkdir(parents=True)
    (month_dir / "a.jpg").write_bytes(b"a")
    (month_dir / "b.jpg").write_bytes(b"b")
    dbp = root / "db.sqlite"
    conn = init_db(dbp)
    _insert_asset(conn, "D" * 64, "Photos/2026/202601/a.jpg", "a.jpg")
    _insert_asset(conn, "E" * 64, "Photos/2026/202601/b.jpg", "b.jpg")
    # 202601：NORMAL + 200 天前 SUCCESS（达保留期）
    append_month_state(conn, "202601", "NORMAL", reason="init")
    ct = (datetime(2026, 9, 7) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO photo_sync_batches(started_at,status,task_count,success_count,failed_count,created_at,updated_at)"
                 " VALUES (?,?,1,1,0,?,?)", (ct, "COMPLETED", ct, ct))
    bid = conn.execute("SELECT MAX(id) m FROM photo_sync_batches").fetchone()["m"]
    conn.execute("INSERT INTO photo_sync_tasks(batch_id,archive_month,source_path,target_path,status,completed_at,created_at,updated_at)"
                 " VALUES (?,?,'/s','/t','SUCCESS',?,?,?)", (bid, "202601", ct, ct, ct))
    conn.commit()

    removed = cleanup_expired(conn, arch, retention_days=90, dry_run=True)
    assert "202601" in removed
    assert conn.execute("SELECT COUNT(*) c FROM photo_assets WHERE file_missing_at IS NULL").fetchone()["c"] == 2
    # 真删 → 目录删除 + 两资产标记
    removed2 = cleanup_expired(conn, arch, retention_days=90, dry_run=False)
    assert "202601" in removed2
    assert not month_dir.exists()
    assert conn.execute("SELECT COUNT(*) c FROM photo_assets WHERE file_missing_at IS NOT NULL").fetchone()["c"] == 2
    conn.close()


# ---------------------------------------------------------------------------
# 对账：mark_missing_assets
# ---------------------------------------------------------------------------

def test_mark_missing_assets_reconciles(tmp_path):
    root = tmp_path
    photo = root / "Photos" / "2026" / "202601"
    photo.mkdir(parents=True)
    (photo / "keep.jpg").write_bytes(b"k")
    dbp = root / "db.sqlite"
    conn = init_db(dbp)
    _insert_asset(conn, "F" * 64, "Photos/2026/202601/keep.jpg", "keep.jpg")
    _insert_asset(conn, "G" * 64, "Photos/2026/202601/gone.jpg", "gone.jpg")  # 文件不存在
    n = mark_missing_assets(conn, root / "Photos")
    assert n == 1
    row = conn.execute("SELECT file_missing_at FROM photo_assets WHERE archive_path LIKE '%gone%'").fetchone()
    assert row["file_missing_at"] is not None
    assert mark_missing_assets(conn, root / "Photos") == 0   # 幂等
    conn.close()


# ---------------------------------------------------------------------------
# /photos field(taken/uploaded) + order(asc/desc)
# ---------------------------------------------------------------------------

def test_photos_sort_field_and_order(tmp_path):
    from fastapi.testclient import TestClient
    from photo_gateway.http import create_app
    root = tmp_path
    dbp = root / "db.sqlite"
    conn = init_db(dbp)
    now = "2026-09-01 10:00:00"
    rows = [
        # (sha, name, date_taken, created_at)
        ("H1" * 32, "taken-202601.jpg", "2026-01-01T10:00:00", "2026-09-01 10:00:00"),
        ("H2" * 32, "taken-202603.jpg", "2026-03-01T10:00:00", "2026-09-02 10:00:00"),
        ("H3" * 32, "taken-202602.jpg", "2026-02-01T10:00:00", "2026-09-03 10:00:00"),
    ]
    for sha, name, dt, ct in rows:
        conn.execute(
            "INSERT INTO photo_assets(sha256,original_filename,current_filename,file_size,mime_type,"
            " date_taken,date_source,archive_path,created_at,updated_at)"
            " VALUES (?,?,?,5,'image/jpeg',?,'DateTimeOriginal',?,?,?)",
            (sha, name, name, dt, f"Photos/2026/{name}", ct, ct))
    conn.commit()
    conn.close()
    app = create_app(_cfg_yaml(root), db_path=dbp)
    with TestClient(app) as c:
        def names(q):
            return [p["current_filename"] for p in c.get("/api/v1/photos?limit=50&" + q).json()["data"]["photos"]]
        # 拍摄时间 正序：202601 < 202602 < 202603
        assert names("field=taken&order=asc") == ["taken-202601.jpg", "taken-202602.jpg", "taken-202603.jpg"]
        # 拍摄时间 倒序
        assert names("field=taken&order=desc") == ["taken-202603.jpg", "taken-202602.jpg", "taken-202601.jpg"]
        # 上传时间 倒序（created_at 最新在前）
        assert names("field=uploaded&order=desc") == ["taken-202602.jpg", "taken-202603.jpg", "taken-202601.jpg"]
        # 上传时间 正序
        assert names("field=uploaded&order=asc") == ["taken-202601.jpg", "taken-202603.jpg", "taken-202602.jpg"]
        # 非法值拒绝
        assert c.get("/api/v1/photos?field=bogus").status_code == 400
        assert c.get("/api/v1/photos?order=up").status_code == 400
