"""视频支持：容器时间解析与归档日期（QuickTime moov/mvhd，零外部依赖）。

- _video_date_string：从 MP4/MOV 的 moov/mvhd 读取创建时间（≈拍摄时间，映射 CreateDate）
- 时间无效（mvhd=0/无 moov/非 1990-2100）→ 返回 None，归档回退 file_mtime
- process_once：视频按 CreateDate 归档 / 无法解析时按 file_mtime，均 COMPLETED
"""

from __future__ import annotations

import os
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml as _y

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.worker import _is_video_path, _video_date_string, process_once


_QT = datetime(1904, 1, 1, tzinfo=timezone.utc)


def make_head_mp4(dt_utc: datetime | None) -> bytes:
    """合成带 moov/mvhd 的 MP4 头字节（dt_utc=None 时 mvhd 时间为 0）。"""
    if dt_utc is None:
        sec = 0
    else:
        sec = int((dt_utc - _QT).total_seconds())
    body = struct.pack(">I", 0)                      # version0 + flags
    body += struct.pack(">II", sec, sec)             # creation, modification
    body += struct.pack(">II", 1000, 60000)          # timescale, duration
    mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s4s", 24, b"ftyp", b"isom") + b"\x00\x00\x00\x00isom"
    return ftyp + moov


def _cfg_yaml(root):
    return load_yaml_text(_y.safe_dump({
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "d1", "enabled": True}],
        "storage": {"root": str(root)},
    }, sort_keys=False))


def _seed_video(root, content, filename="clip.mp4"):
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


def _asset(conn, aid):
    return conn.execute(
        "SELECT date_taken, date_source, archive_path FROM photo_assets WHERE id=?", (aid,)
    ).fetchone()


def test_is_video_path():
    assert _is_video_path(Path("a.MP4")) is True
    assert _is_video_path(Path("a.mp4")) is True
    assert _is_video_path(Path("a.jpg")) is False


def test_video_date_from_mvhd():
    p = Path("/tmp") / "pg-h.mp4"
    p.write_bytes(make_head_mp4(datetime(2026, 9, 3, 1, 27, 58, tzinfo=timezone.utc)))
    assert _video_date_string(p) == "2026-09-03T09:27:58"   # 上海 +8


def test_video_date_invalid_returns_none(tmp_path):
    # mvhd 时间为 0（如 ffmpeg 默认）→ 年份异常 → None
    zero = tmp_path / "z.mp4"
    zero.write_bytes(make_head_mp4(None))
    assert _video_date_string(zero) is None
    # 无 moov 的普通字节
    junk = tmp_path / "j.mp4"
    junk.write_bytes(b"garbage-bytes-not-a-container" * 4)
    assert _video_date_string(junk) is None


def test_process_once_video_uses_create_date(tmp_path):
    root = tmp_path
    content = make_head_mp4(datetime(2026, 9, 3, 1, 27, 58, tzinfo=timezone.utc))
    conn, it1, incoming, _ = _seed_video(root, content)
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    res = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item, None)
    assert res["status"] == "COMPLETED"
    a = _asset(conn, res["asset_id"])
    assert a["date_source"] == "CreateDate"
    assert a["date_taken"] == "2026-09-03T09:27:58"
    assert a["archive_path"].startswith("Photos/2026/202609/")
    conn.close()


def test_process_once_video_falls_back_to_mtime(tmp_path):
    root = tmp_path
    conn, it1, incoming, _ = _seed_video(root, b"not-a-real-mp4-bytes")
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (it1,)).fetchone()
    res = process_once(conn, _cfg_yaml(root), incoming, Path(root) / "Photos", item, None)
    assert res["status"] == "COMPLETED"
    a = _asset(conn, res["asset_id"])
    assert a["date_source"] == "file_mtime"
    assert a["archive_path"].startswith("Photos/2026/202608/")  # file_mtime=2026-08-15
    conn.close()
