"""照片预览：动态缩略图 + 签名 URL 放行（webui-spec §51/§52）。"""

from __future__ import annotations

import io
from pathlib import Path

import yaml as _y
from fastapi.testclient import TestClient
from PIL import Image

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.http import create_app
from photo_gateway.preview_sign import make_preview_token, verify_preview_token
from photo_gateway.security import hash_password


def _seed_asset(root) -> int:
    """建一张真实 jpg 资产(Photos/2026/202601/x.jpg)，返回 asset id。"""
    photo = root / "Photos" / "2026" / "202601"
    photo.mkdir(parents=True)
    Image.new("RGB", (80, 60), (90, 140, 220)).save(photo / "x.jpg")
    conn = init_db(root / "db.sqlite")
    now = "2026-09-01 10:00:00"
    conn.execute(
        "INSERT INTO photo_assets(sha256, original_filename, current_filename, file_size,"
        " mime_type, date_taken, date_source, archive_path, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("H1", "x.jpg", "x.jpg", 1024, "image/jpeg",
         "2026-01-10T10:00:00", "file_mtime", "Photos/2026/202601/x.jpg", now, now),
    )
    aid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit(); conn.close()
    return aid


def _cfg(root, auth_enabled=False, thumbnails=None):
    data = {
        "version": 1,
        "auth": {"enabled": auth_enabled,
                 "username": "admin", "password_hash": hash_password("pw") if auth_enabled else None},
        "devices": [{"id": "d", "name": "d", "enabled": True}],
        "storage": {"root": str(root)},
    }
    if thumbnails is not None:
        data["thumbnails"] = thumbnails
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


def _app(root, auth=False, thumbnails=None):
    cfg = _cfg(root, auth, thumbnails=thumbnails)
    dbp = root / "db.sqlite"
    init_db(dbp)
    app = create_app(cfg, db_path=dbp,
                     dirs={"incoming": root / "incoming", "archive": root / "Photos"})
    return app


def test_thumbnail_generated_and_cached(tmp_path):
    aid = _seed_asset(tmp_path)
    with TestClient(_app(tmp_path, auth=False)) as c:
        r = c.get(f"/api/v1/photos/{aid}/thumbnail")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert len(r.content) > 100
        cached = tmp_path / "thumbnails" / f"{aid}.jpg"
        assert cached.exists()          # 缓存落盘
        # 二次请求命中缓存
        r2 = c.get(f"/api/v1/photos/{aid}/thumbnail")
        assert r2.status_code == 200 and r2.content == r.content


def test_thumbnail_cache_dir_configurable(tmp_path):
    """thumbnails.cache_dir 生效：缓存落到自定义目录，默认位置不再使用（config-spec §63.3）。"""
    aid = _seed_asset(tmp_path)
    with TestClient(_app(tmp_path, thumbnails={"cache_dir": "cache/thumbs"})) as c:
        r = c.get(f"/api/v1/photos/{aid}/thumbnail")
        assert r.status_code == 200
        assert (tmp_path / "cache" / "thumbs" / f"{aid}.jpg").exists()
        assert not (tmp_path / "thumbnails").exists()


def test_thumbnail_cache_dropped_when_source_deleted(tmp_path):
    """原图被删后：thumbnail/file 请求 404，且孤儿缓存缩略图一并清理（回归）。"""
    aid = _seed_asset(tmp_path)
    with TestClient(_app(tmp_path, auth=False)) as c:
        assert c.get(f"/api/v1/photos/{aid}/thumbnail").status_code == 200
    cached = tmp_path / "thumbnails" / f"{aid}.jpg"
    assert cached.exists()
    # 模拟手工删除原图文件（DB 记录仍在）
    (tmp_path / "Photos" / "2026" / "202601" / "x.jpg").unlink()
    with TestClient(_app(tmp_path, auth=False)) as c:
        assert c.get(f"/api/v1/photos/{aid}/thumbnail").status_code == 404
        assert not cached.exists()          # 孤儿缓存已随原图缺失而清理
        # file 端点同样 404（不影响语义）
        assert c.get(f"/api/v1/photos/{aid}/file").status_code == 404


def test_photos_list_has_signed_urls(tmp_path):
    aid = _seed_asset(tmp_path)
    with TestClient(_app(tmp_path, auth=False)) as c:
        j = c.get("/api/v1/photos?limit=5").json()
        photo = j["data"]["photos"][0]
        assert photo["thumbnail_url"].startswith(f"/api/v1/photos/{aid}/thumbnail")
        assert "preview=" in photo["thumbnail_url"] and "preview=" in photo["file_url"]
        # 直接带签名访问缩略图 → 200
        assert c.get(photo["thumbnail_url"]).status_code == 200


def test_auth_on_requires_signature(tmp_path):
    aid = _seed_asset(tmp_path)
    with TestClient(_app(tmp_path, auth=True)) as c:
        # 无签名/Bearer → 401
        assert c.get(f"/api/v1/photos/{aid}/thumbnail").status_code == 401
        # 登录拿 Bearer → 列表含签名 url
        tok = c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"}).json()["data"]["token"]
        h = {"Authorization": f"Bearer {tok}"}
        j = c.get("/api/v1/photos?limit=5", headers=h).json()
        url = j["data"]["photos"][0]["thumbnail_url"]
        assert c.get(url).status_code == 200     # <img> 无需 header 也能加载


def test_signature_reject_wrong_asset_and_expired(tmp_path):
    aid = _seed_asset(tmp_path)
    cfg = _cfg(tmp_path, auth_enabled=True)
    app = create_app(cfg, db_path=tmp_path / "db.sqlite",
                     dirs={"incoming": tmp_path / "in", "archive": tmp_path / "Photos"})
    secret = app.state.pg["preview_secret"]
    # 串用其他 asset 的签名
    with TestClient(app) as c:
        other = make_preview_token(secret, aid + 999)
        assert c.get(f"/api/v1/photos/{aid}/thumbnail?preview={other}").status_code == 401
    # 过期签名单元判定
    expired = make_preview_token(secret, aid, ttl_seconds=-10)
    assert verify_preview_token(secret, aid, expired) is False
    assert verify_preview_token(secret, aid + 1, make_preview_token(secret, aid)) is False


def test_file_download_vs_inline(tmp_path):
    """file 默认附件下载；?inline=1 浏览器内联预览（无 content-disposition attachment）。"""
    aid = _seed_asset(tmp_path)
    with TestClient(_app(tmp_path, auth=False)) as c:
        r = c.get(f"/api/v1/photos/{aid}/file")
        assert r.status_code == 200
        assert "attachment" in (r.headers.get("content-disposition") or "")
        r2 = c.get(f"/api/v1/photos/{aid}/file?inline=1")
        assert r2.status_code == 200
        assert "content-disposition" not in (r2.headers or {})
