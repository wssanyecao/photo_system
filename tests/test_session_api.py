"""上传会话 / Precheck / 确认 API 集成测试 (api-spec §14-27 垂直切片)。"""

from __future__ import annotations

from pathlib import Path

import yaml as _y
from fastapi.testclient import TestClient

from photo_gateway.config import load_yaml_text
from photo_gateway.db import connect, init_db
from photo_gateway.http import create_app


def _cfg_yaml(root):
    data = {
        "version": 1,
        "auth": {"enabled": False},
        "devices": [{"id": "xiaomi14", "name": "小米14", "enabled": True}],
        "storage": {"root": str(root)},
    }
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


def _client(root):
    db_path = root / "db" / "photo-gateway.db"
    init_db(db_path)
    app = create_app(_cfg_yaml(root), db_path=db_path)
    return TestClient(app), db_path, app


def test_create_session_created(tmp_path):
    c, _, app = _client(tmp_path)
    r = c.post("/api/v1/upload-sessions", json={"source_device": "xiaomi14"})
    assert r.status_code == 201
    s = r.json()["data"]["session"]
    assert s["status"] == "CREATED"
    assert s["source_device"] == "xiaomi14"


def test_create_session_unknown_device(tmp_path):
    c, _, _ = _client(tmp_path)
    r = c.post("/api/v1/upload-sessions", json={"source_device": "ghost"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DEVICE_NOT_FOUND"


def test_precheck_no_match_flow(tmp_path):
    c, _, app = _client(tmp_path)
    sid = c.post("/api/v1/upload-sessions", json={"source_device": "xiaomi14"}).json()["data"]["session"]["id"]
    body = {"items": [{"client_item_id": "u1", "filename": "IMG_001.jpg", "file_size": 1234}]}
    r = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck", json=body)
    assert r.status_code == 200
    item = r.json()["data"]["items"][0]
    assert item["precheck_status"] == "NO_MATCH"
    assert item["upload_required"] is True
    assert item["status"] == "PRECHECK"
    assert isinstance(item["item_id"], int)


def test_precheck_idempotent(tmp_path):
    c, _, _ = _client(tmp_path)
    sid = c.post("/api/v1/upload-sessions", json={"source_device": "xiaomi14"}).json()["data"]["session"]["id"]
    body = {"items": [{"client_item_id": "u1", "filename": "a.jpg", "file_size": 10}]}
    a = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck", json=body).json()["data"]["items"][0]
    b = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck", json=body).json()["data"]["items"][0]
    assert a["item_id"] == b["item_id"]


def test_possible_duplicate_detection_and_confirm(tmp_path):
    c, db_path, app = _client(tmp_path)
    # 预置一段"历史成功"记录：photo_asset + upload_item(COMPLETED)
    now = "2026-09-01 10:00:00"
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO devices(id,name,enabled,created_at,updated_at) "
            "VALUES ('xiaomi14','x',1,?,?)", (now, now))
        conn.execute(
            "INSERT INTO photo_assets"
            " (sha256, original_filename, current_filename, file_size, archive_path, created_at, updated_at)"
            " VALUES ('HASHX','IMG_OLD.jpg','IMG_OLD.jpg',99,'/Photos',?,?)", (now, now))
        asset_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO upload_sessions(id, source_device, status, created_at, total_files,"
            " uploaded_files, skipped_files, duplicate_files, failed_files) VALUES"
            " ('old-session','xiaomi14','COMPLETED',?,1,1,0,0,0)", (now,))
        conn.execute("SELECT last_insert_rowid()")  # noop
        conn.execute(
            "INSERT INTO upload_items"
            " (session_id, source_device, client_item_id, original_filename, file_size, status,"
            "  precheck_status, user_confirmation, asset_id, created_at, updated_at)"
            " VALUES ('old-session','xiaomi14','old1','IMG_OLD.jpg',99,'COMPLETED','NO_MATCH',"
            " 0,?,?,?)", (asset_id, now, now))
        conn.commit()
    finally:
        conn.close()

    sid = c.post("/api/v1/upload-sessions", json={"source_device": "xiaomi14"}).json()["data"]["session"]["id"]
    # 相同 triple → 应判定 POSSIBLE_DUPLICATE 且不需要上传
    body = {"items": [{"client_item_id": "uX", "filename": "IMG_OLD.jpg", "file_size": 99}]}
    r = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck", json=body)
    item = r.json()["data"]["items"][0]
    assert r.json()["success"] is True
    assert item["precheck_status"] == "POSSIBLE_DUPLICATE"
    assert item["upload_required"] is False
    # 单条详情
    single = c.get(f"/api/v1/upload-items/{item['item_id']}")
    assert single.status_code == 200
    assert single.json()["data"]["item"]["user_confirmation"] == 0
    # 用户选择跳过
    conf = c.post(f"/api/v1/upload-items/{item['item_id']}/confirmation", json={"confirmation": 2})
    assert conf.status_code == 200
    assert conf.json()["data"]["upload_required"] is False
    assert conf.json()["data"]["user_confirmation"] == 2


def test_confirm_reupload_allows_upload(tmp_path):
    c, db_path, _ = _client(tmp_path)
    now = "2026-09-01 10:00:00"
    conn = connect(db_path)
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('xiaomi14','x',1,?,?)", (now, now))
    conn.execute("INSERT INTO upload_sessions(id, source_device, status, created_at) VALUES ('s9','xiaomi14','CREATED',?)", (now,))
    conn.execute(
        "INSERT INTO upload_items(session_id, source_device, client_item_id, original_filename,"
        " file_size, status, precheck_status, user_confirmation, created_at, updated_at)"
        " VALUES ('s9','xiaomi14','d1','x.jpg',1,'PRECHECK','POSSIBLE_DUPLICATE',0,?,?)", (now, now))
    conn.commit()
    item_id = conn.execute("SELECT id FROM upload_items WHERE client_item_id='d1'").fetchone()[0]
    conn.close()
    c.post("/api/v1/upload-sessions", json={"source_device": "xiaomi14"})  # ensure sync ok
    r = c.post(f"/api/v1/upload-items/{item_id}/confirmation", json={"confirmation": 1})
    assert r.json()["data"]["upload_required"] is True
    assert r.json()["data"]["user_confirmation"] == 1


def _multipart(content: bytes):
    return {"file": (content, "IMG_U.jpg")}


def test_upload_no_match_writes_tmp_then_rename(tmp_path):
    c, _, _ = _client(tmp_path)
    sid = c.post("/api/v1/upload-sessions", json={"source_device": "xiaomi14"}).json()["data"]["session"]["id"]
    body = {"items": [{"client_item_id": "u1", "filename": "IMG_NEW.jpg", "file_size": 11}]}
    item = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck", json=body).json()["data"]["items"][0]
    iid = item["item_id"]
    data = {"file": ("IMG_NEW.jpg", b"hello-world")}
    r = c.post(f"/api/v1/upload-items/{iid}/file", files=data)
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "UPLOADED"
    # incoming/IMG_NEW.jpg 存在且无 .tmp 残留
    final = Path(tmp_path) / "incoming" / "IMG_NEW.jpg"
    assert final.exists()
    assert not (Path(tmp_path) / "incoming" / "IMG_NEW.jpg.tmp").exists()
    # 会话上传计数 ≥1
    sess = c.get(f"/api/v1/upload-sessions/{sid}").json()["data"]["session"]
    assert sess["uploaded_files"] >= 1


def test_upload_unconfirmed_duplicate_rejected(tmp_path):
    c, db_path, _ = _client(tmp_path)
    now = "2026-09-01 10:00:00"
    conn = connect(db_path)
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('xiaomi14','x',1,?,?)", (now, now))
    conn.execute("INSERT INTO upload_sessions(id, source_device, status, created_at) VALUES ('sA','xiaomi14','CREATED',?)", (now,))
    conn.execute(
        "INSERT INTO upload_items(session_id, source_device, client_item_id, original_filename,"
        " file_size, status, precheck_status, user_confirmation, created_at, updated_at)"
        " VALUES ('sA','xiaomi14','DUP','D.jpg',3,'PRECHECK','POSSIBLE_DUPLICATE',0,?,?)", (now, now))
    conn.commit()
    iid = conn.execute("SELECT id FROM upload_items WHERE client_item_id='DUP'").fetchone()[0]
    conn.close()
    r = c.post(f"/api/v1/upload-items/{iid}/file", files={"file": ("D.jpg", b"abc")})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REUPLOAD_CONFIRMATION_REQUIRED"


def test_webui_readonly_photos_and_system(tmp_path):
    c, _, _ = _client(tmp_path)
    ph = c.get("/api/v1/photos?limit=5")
    assert ph.status_code == 200
    assert ph.json()["data"]["photos"] == []  # 暂无资产
    sy = c.get("/api/v1/system/status")
    assert sy.status_code == 200
    assert sy.json()["data"]["counts"] is not None
    assert sy.json()["success"] is True
    ss = c.get("/api/v1/sync/status")          # V2 未配置 → enabled:False
    assert ss.status_code == 200
    assert ss.json()["data"]["enabled"] is False
    ov = c.get("/api/v1/sync/overview")            # 概览聚合端点(未启用 同空)
    assert ov.status_code == 200
    assert ov.json()["data"]["enabled"] is False


def test_lifecycle_with_background_ticker(tmp_path):
    """serve 形态：提 worker(dir/2s ticker)，上传结束自动归档并收尾。"""
    import time as _t
    conn = init_db(tmp_path / "db" / "pg.db")
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('xiaomi14','x',1,'t','t')")
    conn.commit(); conn.close()
    _in = tmp_path / "incoming"; _in.mkdir(exist_ok=True)
    (tmp_path / "Photos").mkdir(exist_ok=True)
    app = create_app(_cfg_yaml(tmp_path), db_path=tmp_path / "db" / "pg.db",
                     dirs={"incoming": _in, "archive": tmp_path / "Photos"})
    with TestClient(app) as c:  # with 让 lifespan(worker ticker) 运行
        sid = c.post("/api/v1/upload-sessions", json={"source_device": "xiaomi14"}).json()["data"]["session"]["id"]
        body = {"items": [{"client_item_id": "w1", "filename": "pic.jpg", "file_size": 8}]}
        item = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck", json=body).json()["data"]["items"][0]
        up = c.post(f"/api/v1/upload-items/{item['item_id']}/file", files={"file": ("pic.jpg", b"12345678")})
        assert up.status_code == 200
        # 等待 ticker(<=2s/次)处理归档
        status = "UPLOAD"
        for _ in range(12):
            _t.sleep(0.6)
            st = c.get(f"/api/v1/upload-sessions/{sid}").json()["data"]["session"]["status"]
            if st in ("COMPLETED", "PARTIAL"):
                status = st
                break
        assert status in ("COMPLETED", "PARTIAL")
        ph = c.get("/api/v1/photos?limit=5").json()["data"]["photos"]
        assert len(ph) >= 1  # 归档产物可见
