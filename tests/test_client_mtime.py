"""Bug 回归：无 EXIF 历史照片(带客户端 lastModified)不应按上传时间归档。"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import yaml as _y
from fastapi.testclient import TestClient
from PIL import Image

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db, connect
from photo_gateway.http import create_app
from photo_gateway.worker import process_once


def _cfg(root):
    return load_yaml_text(_y.safe_dump({
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "d", "name": "d", "enabled": True}],
        "storage": {"root": str(root)},
    }, sort_keys=False))


def _setup(root):
    dbp = root / "db.sqlite"
    init_db(dbp)
    app = create_app(_cfg(root), db_path=dbp)   # 无 dirs：不起后台 ticker
    return app, dbp


def _client_old_ms() -> int:
    # 2020-08-15 12:00 UTC → epoch 毫秒
    return int(datetime(2020, 8, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _png_bytes_no_exif():
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 80, 60)).save(buf, "JPEG")
    return buf.getvalue()


def test_upload_with_client_mtime_archives_to_client_date(tmp_path):
    app, dbp = _setup(tmp_path)
    content = _png_bytes_no_exif()
    with TestClient(app) as c:
        sid = c.post("/api/v1/upload-sessions", json={"source_device": "d"}).json()["data"]["session"]["id"]
        j = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck",
                   json={"items": [{"client_item_id": "k1", "filename": "old.jpg", "file_size": len(content)}]}).json()
        iid = j["data"]["items"][0]["item_id"]
        ms = _client_old_ms()
        up = c.post(f"/api/v1/upload-items/{iid}/file",
                    files={"file": ("old.jpg", content, "image/jpeg")},
                    data={"modified_ms": str(ms)})
        assert up.status_code == 200
        # incoming 正式文件的 mtime 应为客户端时间（不是上传时间）
        stat = (tmp_path / "incoming" / "old.jpg").stat()
        assert abs(stat.st_mtime - ms / 1000.0) < 2.0
        # 直接跑一次 worker 归档
        conn = connect(dbp)
        item = conn.execute("SELECT * FROM upload_items WHERE id=?", (iid,)).fetchone()
        archive = tmp_path / "Photos"
        res = process_once(conn, _cfg(tmp_path), tmp_path / "incoming", archive, item, None)
        assert res["status"] == "COMPLETED"
        asset = conn.execute("SELECT date_taken, date_source, archive_path FROM photo_assets WHERE id=?",
                             (res["asset_id"],)).fetchone()
        assert asset["date_source"] == "file_mtime"
        assert asset["date_taken"].startswith("2020-08-15")
        assert "/2020/202008/" in asset["archive_path"]   # 不再按“上传时间”归档
        conn.close()


def test_future_client_mtime_ignored(tmp_path):
    """未来时间被忽略(保留服务器时间)，上传仍成功。"""
    app, dbp = _setup(tmp_path)
    content = _png_bytes_no_exif()
    future_ms = 4102444800000  # 2100-01-01
    with TestClient(app) as c:
        sid = c.post("/api/v1/upload-sessions", json={"source_device": "d"}).json()["data"]["session"]["id"]
        j = c.post(f"/api/v1/upload-sessions/{sid}/items/precheck",
                   json={"items": [{"client_item_id": "k2", "filename": "f.jpg", "file_size": len(content)}]}).json()
        iid = j["data"]["items"][0]["item_id"]
        up = c.post(f"/api/v1/upload-items/{iid}/file",
                    files={"file": ("f.jpg", content, "image/jpeg")},
                    data={"modified_ms": str(future_ms)})
        assert up.status_code == 200
