"""HTTP 认证/信封最小 API 测试：api-spec §6/§7/§9/§10/§12.1。"""

from __future__ import annotations

import yaml as _y
from fastapi.testclient import TestClient

from photo_gateway.http import create_app
from photo_gateway.security import hash_password


def _cfg_yaml(root, enabled, username="admin", password="pw123"):
    data = {
        "version": 1,
        "server": {"host": "127.0.0.1", "port": 8080},
        "auth": {
            "enabled": enabled,
            "username": username,
            "password_hash": hash_password(password) if enabled else None,
            "session_ttl_hours": 24,
        },
        "devices": [{"id": "xiaomi14", "name": "小米14", "enabled": True},
                    {"id": "mac", "name": "Mac", "enabled": False}],
        "storage": {"root": str(root)},
    }
    from photo_gateway.config import load_yaml_text
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_health_envelope(tmp_path):
    app = create_app(_cfg_yaml(tmp_path, enabled=False))
    c = TestClient(app)
    r = c.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json()["data"]["status"] == "ok"


def test_auth_disabled_serves_devices_without_token(tmp_path):
    app = create_app(_cfg_yaml(tmp_path, enabled=False))
    c = TestClient(app)
    r = c.get("/api/v1/devices")
    assert r.status_code == 200
    devices = r.json()["data"]["devices"]
    assert devices[0]["id"] == "xiaomi14"
    assert devices[1]["enabled"] is False


def test_auth_enabled_requires_token(tmp_path):
    app = create_app(_cfg_yaml(tmp_path, enabled=True))
    c = TestClient(app)
    r = c.get("/api/v1/devices")
    assert r.status_code == 401
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUTH_REQUIRED"


def test_login_and_use_token(tmp_path):
    app = create_app(_cfg_yaml(tmp_path, enabled=True, username="admin", password="s3cret"))
    c = TestClient(app)
    bad = c.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "AUTH_FAILED"
    okr = c.post("/api/v1/auth/login", json={"username": "admin", "password": "s3cret"})
    assert okr.status_code == 200
    token = okr.json()["data"]["token"]
    assert okr.json()["data"]["expires_at"]
    r = c.get("/api/v1/devices", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    # 登出后同 token 失效
    c.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    r2 = c.get("/api/v1/devices", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 401 and r2.json()["error"]["code"] == "AUTH_REQUIRED"


def test_login_disabled_returns_auth_disabled(tmp_path):
    app = create_app(_cfg_yaml(tmp_path, enabled=False))
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "AUTH_DISABLED"


def test_webui_index_served(tmp_path):
    app = create_app(_cfg_yaml(tmp_path, enabled=False))
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "Photo Gateway" in r.text
    assert "upload-sessions" in r.text
