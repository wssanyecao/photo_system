"""V1.4 回收站端到端测试：本地删除/恢复、去重人工决策和同步隔离。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import yaml
from fastapi.testclient import TestClient

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.http import create_app
from photo_gateway.recycle import purge_expired, reconcile
from photo_gateway.v2cycle import FakeNas, run_cycle
from photo_gateway.worker import process_once


def make_env(tmp_path, content=b"original image bytes"):
    root = tmp_path
    cfg = load_yaml_text(yaml.safe_dump({
        "version": 1, "storage": {"root": str(root)},
        "devices": [{"id": "camera", "enabled": True}],
        "auth": {"enabled": False},
        "recycle_bin": {"retention_days": 30, "auto_purge": True},
    }))
    db = root / "database" / "photo-gateway.db"
    conn = init_db(db)
    path = root / "Photos" / "2026" / "202610" / "img.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    now = "2026-10-22 12:00:00"
    digest = hashlib.sha256(content).hexdigest()
    cur = conn.execute(
        "INSERT INTO photo_assets(sha256,original_filename,current_filename,file_size,"
        "mime_type,date_taken,date_source,archive_path,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (digest, "img.jpg", "img.jpg", len(content), "image/jpeg", now, "file_mtime",
         "Photos/2026/202610/img.jpg", now, now),
    )
    conn.commit()
    return cfg, conn, db, path, cur.lastrowid, content


def test_trash_list_preview_and_restore(tmp_path):
    cfg, conn, db, photo, aid, _ = make_env(tmp_path)
    conn.close()
    app = create_app(cfg, db_path=db)
    with TestClient(app) as client:
        assert client.get("/api/v1/photos").json()["data"]["total"] == 1
        result = client.post(f"/api/v1/photos/{aid}/trash")
        assert result.status_code == 200, result.text
        assert not photo.exists()
        assert client.get("/api/v1/photos").json()["data"]["total"] == 0
        assert client.get(f"/api/v1/photos/{aid}/file").status_code == 404
        trash = client.get("/api/v1/recycle-bin").json()["data"]["photos"]
        assert len(trash) == 1 and trash[0]["id"] == aid
        expected = (datetime.strptime(trash[0]["deleted_at"], "%Y-%m-%d %H:%M:%S")
                    + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        assert trash[0]["expires_at"] == expected
        assert client.get(trash[0]["preview_url"]).status_code == 200
        assert client.post(f"/api/v1/recycle-bin/{aid}/restore").status_code == 200
        assert client.get("/api/v1/photos").json()["data"]["total"] == 1
        assert photo.read_bytes() == b"original image bytes"
        assert client.get("/api/v1/recycle-bin").json()["data"]["photos"] == []


def test_expired_purge_keeps_sha_and_requires_review(tmp_path):
    cfg, conn, db, photo, aid, content = make_env(tmp_path)
    with TestClient(create_app(cfg, db_path=db)) as client:
        assert client.post(f"/api/v1/photos/{aid}/trash").status_code == 200
        result = purge_expired(conn, cfg, at=datetime.now(timezone(timedelta(hours=8))) + timedelta(days=31))
        assert result == 1
        assert conn.execute("SELECT lifecycle_state FROM photo_assets WHERE id=?", (aid,)).fetchone()[0] == "PURGED"
        assert not photo.exists()
        # 同一字节在后台上传后不应自动恢复，而应挂起等待人工选择
        conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('camera','cam',1,'t','t')")
        conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at)"
                     " VALUES ('ses','camera','UPLOADING','t')")
        conn.execute("INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
                     "file_size,status,precheck_status,user_confirmation,created_at,updated_at)"
                     " VALUES ('ses','camera','i','img.jpg',?,'UPLOADED','NO_MATCH',0,'t','t')",
                     (len(content),))
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        incoming = cfg.storage.resolved_incoming()
        incoming.mkdir(parents=True, exist_ok=True)
        (incoming / "img.jpg").write_bytes(content)
        item = conn.execute("SELECT * FROM upload_items WHERE id=?", (item_id,)).fetchone()
        result = process_once(conn, cfg, incoming, cfg.storage.resolved_archive(), item, None)
        assert result["status"] == "REVIEW_REQUIRED"
        assert not photo.exists()
        review = client.get("/api/v1/upload-sessions/ses/deleted-reviews").json()["data"]["items"]
        assert len(review) == 1 and review[0]["asset_id"] == aid
        out = client.post(f"/api/v1/upload-items/{item_id}/deleted-decision", json={"choice": "restore"})
        assert out.status_code == 200, out.text
        assert photo.read_bytes() == content
        assert conn.execute("SELECT lifecycle_state FROM photo_assets WHERE id=?", (aid,)).fetchone()[0] == "ACTIVE"


def test_keep_deleted_and_sync_excludes_trash(tmp_path):
    cfg, conn, db, photo, aid, content = make_env(tmp_path)
    with TestClient(create_app(cfg, db_path=db)) as client:
        assert client.post(f"/api/v1/photos/{aid}/trash").status_code == 200
        assert not photo.exists()
        archive = cfg.storage.resolved_archive()
        # 同步周期只能看到 Photos，回收站实体绝不能落入 NAS 目标。
        from photo_gateway.v2cycle import FakeNas
        nas_root = tmp_path / "mock-remote"
        nas = FakeNas(target_root=nas_root)
        run_cycle(conn, archive, nas, cleanup=False)
        assert not list(nas_root.rglob("img.jpg"))
        assert (cfg.resolved_recycle_bin() / str(aid) / "img.jpg").is_file()
        # 同内容重复上传要求人工决定；拒绝后不恢复文件。
        conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('camera','cam',1,'t','t')")
        conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at)"
                     " VALUES ('s2','camera','UPLOADING','t')")
        conn.execute("INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
                     "file_size,status,precheck_status,user_confirmation,created_at,updated_at)"
                     " VALUES ('s2','camera','i2','img.jpg',?,'UPLOADED','NO_MATCH',0,'t','t')",
                     (len(content),))
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        incoming = cfg.storage.resolved_incoming()
        incoming.mkdir(exist_ok=True)
        (incoming / "img.jpg").write_bytes(content)
        row = conn.execute("SELECT * FROM upload_items WHERE id=?", (item_id,)).fetchone()
        assert process_once(conn, cfg, incoming, archive, row, None)["status"] == "REVIEW_REQUIRED"
        r = client.post(f"/api/v1/upload-items/{item_id}/deleted-decision",
                        json={"choice": "keep_deleted"})
        assert r.status_code == 200, r.text
        assert not (incoming / "img.jpg").exists()
        assert not photo.exists()
        assert conn.execute("SELECT lifecycle_state FROM photo_assets WHERE id=?", (aid,)).fetchone()[0] == "TRASHED"


def test_reconcile_interrupted_move_and_restore(tmp_path):
    cfg, conn, db, photo, aid, content = make_env(tmp_path)
    trashed = cfg.resolved_recycle_bin() / str(aid) / "img.jpg"
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute("UPDATE photo_assets SET lifecycle_state='TRASHED', deleted_at=? WHERE id=?", (now, aid))
    with conn:
        conn.execute("INSERT INTO photo_recycle_events(asset_id,action,created_at) VALUES (?,?,?)",
                     (aid, "TRASHED", now))
    reconcile(conn, cfg)
    assert trashed.read_bytes() == content and not photo.exists()
    with conn:
        conn.execute("UPDATE photo_assets SET lifecycle_state='ACTIVE', deleted_at=NULL WHERE id=?", (aid,))
    reconcile(conn, cfg)
    assert photo.read_bytes() == content and not trashed.exists()


def test_path_escape_rejected(tmp_path):
    cfg, conn, db, photo, aid, _ = make_env(tmp_path)
    with conn:
        conn.execute("UPDATE photo_assets SET archive_path='../outside' WHERE id=?", (aid,))
    with TestClient(create_app(cfg, db_path=db)) as client:
        assert client.post(f"/api/v1/photos/{aid}/trash").status_code == 409
    assert photo.exists()


def test_pending_review_expires_without_resurrection(tmp_path):
    from photo_gateway.recycle import expire_pending_reviews, pending_path
    cfg, conn, db, photo, aid, content = make_env(tmp_path)
    with TestClient(create_app(cfg, db_path=db)) as client:
        assert client.post(f"/api/v1/photos/{aid}/trash").status_code == 200
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('camera','cam',1,'t','t')")
    conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at)"
                 " VALUES('session','camera','UPLOADING','t')")
    conn.execute("INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
                 "file_size,status,precheck_status,user_confirmation,created_at,updated_at)"
                 " VALUES ('session','camera','item','img.jpg',?,'UPLOADED','NO_MATCH',0,'t','t')",
                 (len(content),))
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    incoming = cfg.storage.resolved_incoming()
    incoming.mkdir(parents=True)
    (incoming / "img.jpg").write_bytes(content)
    row = conn.execute("SELECT * FROM upload_items WHERE id=?", (item_id,)).fetchone()
    assert process_once(conn, cfg, incoming, cfg.storage.resolved_archive(), row, None)["status"] == "REVIEW_REQUIRED"
    staged = pending_path(cfg, item_id, "img.jpg")
    assert staged.read_bytes() == content
    conn.execute("UPDATE upload_items SET updated_at='2000-01-01 00:00:00' WHERE id=?", (item_id,))
    conn.commit()
    assert expire_pending_reviews(conn, cfg) == 1
    assert not staged.exists()
    assert conn.execute("SELECT status FROM upload_items WHERE id=?", (item_id,)).fetchone()[0] == "FAILED"
    assert conn.execute("SELECT lifecycle_state FROM photo_assets WHERE id=?", (aid,)).fetchone()[0] == "TRASHED"
    assert not photo.exists()


def test_restore_collision_does_not_overwrite(tmp_path):
    cfg, conn, db, photo, aid, _ = make_env(tmp_path)
    with TestClient(create_app(cfg, db_path=db)) as client:
        assert client.post(f"/api/v1/photos/{aid}/trash").status_code == 200
        photo.write_bytes(b"unrelated file")
        assert client.post(f"/api/v1/recycle-bin/{aid}/restore").status_code == 409
        assert photo.read_bytes() == b"unrelated file"
        assert len(client.get("/api/v1/recycle-bin").json()["data"]["photos"]) == 1


def test_purge_removes_preview_cache_but_not_identity(tmp_path):
    from photo_gateway.recycle import trash
    cfg, conn, db, photo, aid, content = make_env(tmp_path)
    cache = cfg.resolved_thumbnails()
    cache.mkdir(parents=True)
    thumb = cache / f"{aid}.jpg"
    preview = cache / f"{aid}.preview.jpg"
    thumb.write_bytes(b"cached")
    preview.write_bytes(b"cached")
    trash(conn, cfg, aid)
    assert purge_expired(conn, cfg, at=datetime.now(timezone(timedelta(hours=8))) + timedelta(days=31)) == 1
    assert not thumb.exists() and not preview.exists()
    row = conn.execute("SELECT sha256,lifecycle_state FROM photo_assets WHERE id=?", (aid,)).fetchone()
    assert row["sha256"] == hashlib.sha256(content).hexdigest() and row["lifecycle_state"] == "PURGED"



def test_reconcile_allows_new_content_reusing_deleted_filename(tmp_path):
    from photo_gateway.recycle import trash
    cfg, conn, _, photo, aid, _ = make_env(tmp_path)
    trash(conn, cfg, aid)
    # 另一个不同 SHA 的资产后来使用了相同目录和文件名。
    photo.write_bytes(b"another photo, same name")
    reconcile(conn, cfg)
    assert photo.read_bytes() == b"another photo, same name"
    assert conn.execute(
        "SELECT lifecycle_state FROM photo_assets WHERE id=?", (aid,)
    ).fetchone()[0] == "TRASHED"
    assert (cfg.resolved_recycle_bin() / str(aid) / "img.jpg").exists()
    assert purge_expired(
        conn, cfg, at=datetime.now(timezone(timedelta(hours=8))) + timedelta(days=31)
    ) == 1
    reconcile(conn, cfg)
    assert photo.read_bytes() == b"another photo, same name"
    assert conn.execute(
        "SELECT lifecycle_state FROM photo_assets WHERE id=?", (aid,)
    ).fetchone()[0] == "PURGED"



def test_restored_asset_blocks_stale_month_cleanup(tmp_path):
    from photo_gateway.recycle import trash, restore
    from photo_gateway.v2 import append_month_state
    from photo_gateway.v2cycle import cleanup_candidates
    cfg, conn, _, photo, aid, _ = make_env(tmp_path)
    old = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=200)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    append_month_state(conn, "202610", "NORMAL", reason="sync ready")
    cur = conn.execute(
        "INSERT INTO photo_sync_batches(started_at,status,task_count,success_count,"
        "failed_count,created_at,updated_at) VALUES(?,'COMPLETED',1,1,0,?,?)",
        (old, old, old),
    )
    conn.execute(
        "INSERT INTO photo_sync_tasks(batch_id,archive_month,source_path,target_path,"
        "status,completed_at,created_at,updated_at) "
        "VALUES (?,'202610','/Photos/2026/202610','/remote/2026/202610','SUCCESS',?,?,?)",
        (cur.lastrowid, old, old, old),
    )
    conn.commit()
    # 新的本地恢复使历史 SUCCESS 不足以证明目前的目录已安全同步。
    trash(conn, cfg, aid)
    restore(conn, cfg, aid)
    candidates = cleanup_candidates(conn, cfg.storage.resolved_archive(), retention_days=30)
    assert len(candidates) == 1
    assert candidates[0]["remove"] is False
    assert "resync required" in candidates[0]["reason"]
    assert photo.exists()


def test_recycle_list_uses_photo_filters_pagination_and_detail(tmp_path):
    """同一套拍摄/上传时间、设备、类型、排序和分页，不按删除时间筛选。"""
    from photo_gateway.recycle import trash
    cfg, conn, db, photo, aid, _ = make_env(tmp_path)
    with TestClient(create_app(cfg, db_path=db)) as client:
        trash(conn, cfg, aid)
        conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                     " VALUES('camera','Camera',1,'t','t')")
        conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                     " VALUES('other','Other',1,'t','t')")
        for dev in ("camera", "other"):
            conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at)"
                         " VALUES(?,?, 'COMPLETED', 't')", (dev,dev))
        extra = [
            ("b", "2026-10-19 08:00:00", "2026-10-23 08:00:00", "image/png", "other"),
            ("c", "2026-10-21 08:00:00", "2026-10-20 08:00:00", "image/jpeg", "camera"),
            ("d", "2026-10-23 08:00:00", "2026-10-21 08:00:00", "image/png", "camera"),
        ]
        for name, taken, uploaded, mime, dev in extra:
            cur = conn.execute(
                "INSERT INTO photo_assets(sha256,original_filename,current_filename,"
                "file_size,mime_type,date_taken,date_source,archive_path,created_at,updated_at,"
                "lifecycle_state,deleted_at) VALUES(?,?,?,?,?,?,?, ?,?,?,'TRASHED',?)",
                (hashlib.sha256(name.encode()).hexdigest(), f"{name}.jpg", f"{name}.jpg",
                 1, mime, taken, "file_mtime", f"Photos/2026/202610/{name}.jpg",
                 uploaded, uploaded, "2026-10-22 19:00:00"),
            )
            conn.execute(
                "INSERT INTO upload_items(session_id,source_device,client_item_id,"
                "original_filename,file_size,status,precheck_status,user_confirmation,"
                "asset_id,created_at,updated_at) VALUES(?,?,?,?,?,'COMPLETED','NO_MATCH',0,?,?,?)",
                (dev,dev,name,f"{name}.jpg",1,cur.lastrowid,uploaded,uploaded),
            )
        conn.execute(
            "INSERT INTO upload_items(session_id,source_device,client_item_id,"
            "original_filename,file_size,status,precheck_status,user_confirmation,"
            "asset_id,created_at,updated_at) VALUES"
            "('camera','camera','main','img.jpg',1,'COMPLETED','NO_MATCH',0,?,?,?)",
            (aid, "2026-10-22 12:00:00", "2026-10-22 12:00:00"),
        )
        conn.commit()
        rsp = client.get("/api/v1/recycle-bin", params={"field":"taken","order":"desc",
                                                        "limit":2, "offset":2})
        assert rsp.status_code == 200, rsp.text
        data = rsp.json()["data"]
        assert (data["total"], data["limit"], data["offset"]) == (4, 2, 2)
        assert [x["current_filename"] for x in data["photos"]] == ["c.jpg", "b.jpg"]
        assert data["photos"][0]["is_recycled"] is True
        assert "/recycle-bin/" in data["photos"][0]["thumbnail_url"]
        assert client.get(data["photos"][0]["thumbnail_url"]).status_code == 404  # no synthetic file
        assert client.get(f"/api/v1/recycle-bin/{aid}/thumbnail").status_code == 200
        assert client.get(f"/api/v1/recycle-bin/{aid}").json()["data"]["photo"]["source_device"] == "camera"
        assert client.get("/api/v1/recycle-bin", params={
            "field":"taken", "date_from":"2026-10-21","date_to":"2026-10-23"
        }).json()["data"]["total"] == 2
        assert client.get("/api/v1/recycle-bin", params={
            "field":"uploaded", "date_from":"2026-10-22"
        }).json()["data"]["total"] == 2
        other = client.get("/api/v1/recycle-bin", params={
            "source_device":"other", "mime_type":"image/png",
            "field":"taken", "order":"asc"
        }).json()["data"]
        assert [x["current_filename"] for x in other["photos"]] == ["b.jpg"]
        for params in ({"field":"deleted"}, {"order":"xyz"}, {"limit":201}, {"offset":-1}):
            assert client.get("/api/v1/recycle-bin", params=params).status_code == 400
        assert client.get("/api/v1/photos").json()["data"]["total"] == 0
