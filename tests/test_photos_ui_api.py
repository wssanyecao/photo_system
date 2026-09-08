"""照片页 API：默认页大小(webui.photos_page_size)、日期过滤、分页字段。"""

from __future__ import annotations

from pathlib import Path

import yaml as _y
from fastapi.testclient import TestClient

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.http import create_app


def _seed_two(tmp_path) -> int:
    conn = init_db(tmp_path / "db.sqlite")
    now = "2026-09-01 10:00:00"
    rows = [
        ("2026-01-10T10:00:00", "Photos/2026/202601/a.jpg"),
        ("2026-02-02T10:00:00", "Photos/2026/202602/b.jpg"),
    ]
    for i, (dt, ap) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO photo_assets(sha256, original_filename, current_filename, file_size,"
            " mime_type, date_taken, date_source, archive_path, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"H{i}", f"{i}.jpg", f"{i}.jpg", 1, "image/jpeg",
             dt, "file_mtime", ap, now, now),
        )
    conn.commit(); conn.close()
    return 2


def _cfg(root, page_size: int | None = None):
    data = {
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "d", "name": "d", "enabled": True}],
        "storage": {"root": str(root)},
    }
    if page_size is not None:
        data["webui"] = {"photos_page_size": page_size}
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


def _app(root, page_size=None):
    init_db(root / "db.sqlite")
    return create_app(_cfg(root, page_size), db_path=root / "db.sqlite",
                      dirs={"incoming": root / "in", "archive": root / "Photos"})


def test_default_page_size_from_config(tmp_path):
    _seed_two(tmp_path)
    with TestClient(_app(tmp_path, page_size=1)) as c:
        j = c.get("/api/v1/photos").json()          # 未显式传 limit
        assert j["data"]["limit"] == 1               # 使用配置页大小
        assert j["data"]["total"] == 2
        assert len(j["data"]["photos"]) == 1
    # 缺省配置(默认 12)时仍可用且返回前 12
    with TestClient(_app(tmp_path)) as c:
        j = c.get("/api/v1/photos").json()
        assert j["data"]["limit"] == 12


def test_paging_offset(tmp_path):
    _seed_two(tmp_path)
    with TestClient(_app(tmp_path, page_size=1)) as c:
        p1 = c.get("/api/v1/photos?offset=0").json()["data"]["photos"]
        p2 = c.get("/api/v1/photos?offset=1").json()["data"]["photos"]
        assert p1 and p2 and p1[0]["id"] != p2[0]["id"]


def test_date_filter(tmp_path):
    _seed_two(tmp_path)
    with TestClient(_app(tmp_path)) as c:
        jan = c.get("/api/v1/photos?date_from=2026-01-10&date_to=2026-01-11").json()["data"]
        assert jan["total"] == 1 and jan["photos"][0]["current_filename"] == "1.jpg"
        feb = c.get("/api/v1/photos?date_from=2026-02-01&date_to=2026-02-03").json()["data"]
        assert feb["total"] == 1 and feb["photos"][0]["current_filename"] == "2.jpg"
        none = c.get("/api/v1/photos?date_from=2025-01-01&date_to=2025-12-31").json()["data"]
        assert none["total"] == 0
