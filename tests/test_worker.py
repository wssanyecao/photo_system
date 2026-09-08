"""Worker 阶段4 测试：归档新照片 / SHA 去重 / 文件名冲突递增。"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import yaml as _y

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.worker import (  # noqa: F401
    _current_filename_unique,
    align_asset_mtimes,
    compute_sha256,
    epoch_from_date_taken,
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
    import uuid
    incoming = Path(root) / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (Path(root) / "db").mkdir(parents=True, exist_ok=True)
    db_path = Path(root) / "db" / "pg.db"
    conn = init_db(db_path)
    now = "2026-09-01 10:00:00"
    if not conn.execute("SELECT 1 FROM devices WHERE id='d1'").fetchone():
        conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('d1','d',1,?,?)", (now, now))
    sid = f"S{str(uuid.uuid4())[:8]}"
    conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at) VALUES (?, 'd1','CREATED',?)", (sid, now))
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


def test_sha256_hex_len():
    p = Path(__file__)
    assert len(compute_sha256(p)) == 64


def test_worker_archives_new_photo(tmp_path):
    root = tmp_path
    conn, item_id, incoming, _ = _seed(root, b"hello-worker")
    cfg = _cfg_yaml(root)
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (item_id,)).fetchone()
    res = process_once(conn, cfg, incoming, Path(root) / "Photos", item, None)
    assert res["status"] == "COMPLETED"
    assert res["asset_id"] is not None
    asset = conn.execute("SELECT * FROM photo_assets WHERE id=?", (res["asset_id"],)).fetchone()
    assert asset["date_source"] == "file_mtime"
    # archive 路径形如 Photos/2026/202608/...
    parts = asset["archive_path"].split("/")
    assert parts[:2] == ["Photos", "2026"] and parts[2].startswith("2026")
    assert (Path(root) / "Photos" / parts[1] / parts[2] / asset["current_filename"]).exists()
    up = conn.execute("SELECT status,asset_id FROM upload_items WHERE id=?", (item_id,)).fetchone()
    assert up["status"] == "COMPLETED" and up["asset_id"] == res["asset_id"]
    conn.close()


def test_duplicate_reuses_asset(tmp_path):
    root = tmp_path
    content = b"dupcontent"
    conn, it1, incoming, _ = _seed(root, content)
    i1 = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    r1 = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", i1, None)
    base_asset = r1["asset_id"]
    conn.close()

    conn, it2, incoming, _ = _seed(root, content, "second.jpg")
    item2 = conn.execute("SELECT * FROM upload_items WHERE id=?", (it2,)).fetchone()
    r2 = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item2, None)
    assert r2["status"] == "DUPLICATE"
    assert r2["asset_id"] == base_asset
    conn.close()


def test_filename_conflict_increment(tmp_path):
    arch = Path(tmp_path) / "Photos" / "2026" / "202608"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "shot.jpg").write_bytes(b"occupied")
    assert _current_filename_unique(arch, "shot.jpg") == "shot_1.jpg"


def test_run_round_finalizes_session(tmp_path):
    from photo_gateway.worker import run_round
    root = tmp_path
    conn, it1, incoming, _ = _seed(root, b"final-test")
    dirs = {"incoming": incoming, "archive": Path(root) / "Photos",
            "logs": Path(root) / "logs"}
    res = run_round(conn, _cfg_yaml(root), dirs, None)
    assert res["status"] == "COMPLETED"
    # 会话应终态 COMPLETED, 上传已数=1
    sess = conn.execute("SELECT status, uploaded_files FROM upload_sessions "
                        "ORDER BY created_at DESC LIMIT 1").fetchone()
    assert sess["status"] == "COMPLETED"
    assert sess["uploaded_files"] == 1
    conn.close()


def _jpeg_bytes_with_exif(tag_value=b"2026:08:25 21:29:19"):
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    im = Image.new("RGB", (16, 16), (60, 120, 200))
    ex = im.getexif()
    ex[0x0132] = tag_value       # DateTime
    ex[0x9003] = tag_value       # DateTimeOriginal
    im.save(buf, "JPEG", exif=ex.tobytes())
    return buf.getvalue()


def test_exif_json_recorded_on_archive(tmp_path):
    root = tmp_path
    content = _jpeg_bytes_with_exif()
    conn, it1, incoming, _ = _seed(root, content, "exif.jpg")
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    res = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item, None)
    assert res["status"] == "COMPLETED"
    asset = conn.execute("SELECT exif_json, date_source FROM photo_assets WHERE id=?",
                         (res["asset_id"],)).fetchone()
    assert asset["exif_json"] and "DateTimeOriginal" in asset["exif_json"]
    assert asset["date_source"] == "DateTimeOriginal"  # EXIF 优先，不再退回 mtime
    conn.close()


def test_backfill_exif_json(tmp_path):
    root = tmp_path
    content = _jpeg_bytes_with_exif()
    conn, it1, incoming, _ = _seed(root, content, "bf.jpg")
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    r = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item, None)
    # 清空该 asset 的 exif_json 模拟“历史遗留为空”
    conn.execute("UPDATE photo_assets SET exif_json=NULL WHERE id=?", (r["asset_id"],))
    conn.commit()
    from photo_gateway.worker import backfill_exif_json
    assert backfill_exif_json(conn, Path(root) / "Photos") == 1
    asset = conn.execute("SELECT exif_json FROM photo_assets WHERE id=?", (r["asset_id"],)).fetchone()
    assert asset["exif_json"] and "DateTimeOriginal" in asset["exif_json"]
    conn.close()


def test_duplicate_reupload_reanchors_date_and_moves(tmp_path):
    root = tmp_path
    content = b"same-content-for-repair"
    # 第一次上传：无客户端时间（模拟旧 bug，归档到“今天/服务器时间”）
    conn, it1, incoming, _ = _seed(root, content, "dup.png")
    cfg = _cfg_yaml(root)
    item1 = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    r1 = process_once(conn, cfg, incoming, Path(root) / "Photos", item1, None)
    assert r1["status"] == "COMPLETED"
    before = conn.execute("SELECT archive_path, date_taken FROM photo_assets WHERE id=?",
                          (r1["asset_id"],)).fetchone()

    # 第二次上传同内容，携带客户端时间 2020-08（修正目标）
    conn.close()
    conn, it2, incoming2, _ = _seed(root, content, "dup.png")
    t2020 = time.mktime(datetime(2020, 8, 15).timetuple())
    os.utime(incoming2 / "dup.png", (t2020, t2020))
    item2 = conn.execute("SELECT * FROM upload_items WHERE id=?", (it2,)).fetchone()
    r2 = process_once(conn, cfg, incoming2, Path(root) / "Photos", item2, None)
    assert r2["status"] == "DUPLICATE"
    assert r2["repaired"] is True

    after = conn.execute("SELECT archive_path, date_taken, date_source FROM photo_assets WHERE id=?",
                         (r1["asset_id"],)).fetchone()
    assert after["date_source"] == "file_mtime"
    assert after["date_taken"].startswith("2020-08-15")
    assert "2020/202008/" in after["archive_path"]
    # 文件确实被移动：旧位置不存在、新位置存在
    old_file = Path(root) / before["archive_path"]
    new_file = Path(root) / after["archive_path"]
    assert not old_file.exists() and new_file.exists()
    conn.close()


def test_epoch_from_date_taken_aware_and_naive():
    from photo_gateway.worker import epoch_from_date_taken
    aware = "2026-08-31T20:02:01.549000+08:00"
    exp = epoch_from_date_taken(aware)
    assert abs(exp - datetime.fromisoformat(aware).timestamp()) < 1e-3
    # naive 视为上海墙钟(UTC+8) → 2020-08-15T12:00+08:00 = 04:00 UTC = 1597464000
    naive = epoch_from_date_taken("2020-08-15T12:00:00")
    assert abs(naive - 1597464000.0) < 1e-6
    assert epoch_from_date_taken("bad") is None


def test_new_archive_file_mtime_matches_asset_date(tmp_path):
    """归档后的磁盘文件 mtime 应与资产时间(EXIF 日期)一致，而非上传时间。"""
    import io as _io
    from PIL import Image
    root = tmp_path
    tag = b"2026:08:25 21:29:19"
    buf = _io.BytesIO()
    im = Image.new("RGB", (20, 20), (30, 200, 90))
    ex = im.getexif(); ex[0x9003] = tag
    im.save(buf, "JPEG", exif=ex.tobytes())
    content = buf.getvalue()
    conn, it1, incoming, _ = _seed(root, content, "dated.jpg")
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    r = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item, None)
    asset = conn.execute("SELECT date_taken, archive_path FROM photo_assets WHERE id=?",
                         (r["asset_id"],)).fetchone()
    full = Path(root) / asset["archive_path"]
    expect = epoch_from_date_taken(asset["date_taken"])
    assert abs(full.stat().st_mtime - expect) < 1.0     # mtime=照片时间，非“现在”
    conn.close()


def test_align_asset_mtimes_fixes_legacy_file(tmp_path):
    from photo_gateway.worker import align_asset_mtimes
    root = tmp_path
    conn, it1, incoming, _ = _seed(root, b"alignme", "al.jpg")
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    r = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item, None)
    asset = conn.execute("SELECT date_taken, archive_path FROM photo_assets WHERE id=?",
                         (r["asset_id"],)).fetchone()
    full = Path(root) / asset["archive_path"]
    os.utime(full, (time.time(), time.time()))            # 模拟旧 bug 的“最新上传时间”
    assert align_asset_mtimes(conn, Path(root) / "Photos") == 1
    assert abs(full.stat().st_mtime - epoch_from_date_taken(asset["date_taken"])) < 1.0
    conn.close()


# ---------------------------------------------------------------------------
# EXIF 多层 IFD 日期读取回归（真实手机/相机：0x9003/0x9004 位于 0x8769 子 IFD，
# 不在顶层——Pillow 合成 EXIF 是扁平的，测不出此 bug，需模拟嵌套结构）
# ---------------------------------------------------------------------------

class _FakeExif(dict):
    """模拟 Pillow Exif：顶层(IFD0) + 0x8769 EXIF 子 IFD。"""

    def __init__(self, top=None, sub=None):
        super().__init__(top or {})
        self._sub = sub or {}

    def get_ifd(self, tag):
        return self._sub if tag == 0x8769 else {}


class _FakeExifImg:
    def __init__(self, exif):
        self._exif = exif

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getexif(self):
        return self._exif


def _worker_with_exif(monkeypatch, exif):
    import photo_gateway.worker as w
    monkeypatch.setattr("PIL.Image.open", lambda _p: _FakeExifImg(exif))
    return w


def test_exif_date_prefers_original_in_sub_ifd(monkeypatch):
    """顶层 DateTime 与子 IFD DateTimeOriginal 不一致 → 必须取 DateTimeOriginal(0x9003)。"""
    exif = _FakeExif({0x0132: "2026:08:25 22:52:30"},
                     {0x9003: "2026:08:13 18:42:38", 0x9004: "2026:08:13 18:42:38"})
    w = _worker_with_exif(monkeypatch, exif)
    assert w._exif_date_string(Path("x.jpg")) == "2026-08-13T18:42:38"


def test_exif_date_falls_back_to_top_level_datetime(monkeypatch):
    """无 0x8769 子 IFD（老相机只有顶层 DateTime）→ 回退顶层 0x0132。"""
    exif = _FakeExif({0x0132: "2015:04:09 21:53:04"})
    w = _worker_with_exif(monkeypatch, exif)
    assert w._exif_date_string(Path("x.jpg")) == "2015-04-09T21:53:04"


def test_exif_date_invalid_string_returns_none(monkeypatch):
    exif = _FakeExif({0x0132: "not-a-date"})
    w = _worker_with_exif(monkeypatch, exif)
    assert w._exif_date_string(Path("x.jpg")) is None


def test_exif_json_includes_sub_ifd_tags(monkeypatch):
    """exif_json 快照应含子 IFD 的 DateTimeOriginal（此前只存顶层而缺失）。"""
    import json
    exif = _FakeExif({0x0132: "2026:08:25 22:52:30", 0x010F: "Xiaomi"},
                     {0x9003: "2026:08:13 18:42:38"})
    w = _worker_with_exif(monkeypatch, exif)
    j = json.loads(w._exif_json(Path("x.jpg")))
    assert j["DateTimeOriginal"] == "2026:08:13 18:42:38"
    assert j["DateTime"] == "2026:08:25 22:52:30"
    assert j["Make"] == "Xiaomi"
