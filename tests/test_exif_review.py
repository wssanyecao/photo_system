"""EXIF review: synthetic photos and isolated SQLite only."""
import hashlib
import io
import shutil
import subprocess
import uuid

import pytest
import yaml
from PIL import Image
from fastapi.testclient import TestClient

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.exif_time import validate_confirmed_time, read_exif_times, make_updated_copy
from photo_gateway.http import create_app
from photo_gateway.worker import process_once


def cfg(root):
    return load_yaml_text(yaml.safe_dump({
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "camera", "name": "Camera", "enabled": True}],
        "storage": {"root": str(root)},
    }))


def jpeg():
    data = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 24, 36)).save(data, "JPEG")
    return data.getvalue()


def seed(conn, root, content, name, confirmed):
    directory = root / "incoming"
    directory.mkdir(exist_ok=True)
    now = "2026-11-02 12:00:00"
    sid = str(uuid.uuid4())
    conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at)"
                 " VALUES (?,'camera','UPLOADING',?)", (sid, now))
    cursor = conn.execute(
        "INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
        "file_size,status,precheck_status,user_confirmation,exif_confirmed_time,created_at,updated_at)"
        " VALUES (?,'camera',?, ?,?,'UPLOADED','NO_MATCH',0,?,?,?)",
        (sid, str(uuid.uuid4()), name, len(content), confirmed, now, now))
    conn.commit()
    (directory / name).write_bytes(content)
    return conn.execute("SELECT * FROM upload_items WHERE id=?", (cursor.lastrowid,)).fetchone()


def test_confirmed_time_validation():
    assert validate_confirmed_time("2026-10-26T16:47:17") == "2026:10:26 16:47:17"
    for date in ("2026-02-30T10:00:00", "2026-10-26T25:00:00",
                 "2026-10-26T16:47", "2026-10-26T16:47:17+08:00"):
        with pytest.raises(ValueError):
            validate_confirmed_time(date)


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_copy_preserves_original_and_existing_fields(tmp_path):
    img = tmp_path / "sample.jpg"
    img.write_bytes(jpeg())
    raw = img.read_bytes()
    modified = make_updated_copy(img, 1, "2026-10-26T16:47:17")
    assert img.read_bytes() == raw
    assert modified.read_bytes() != raw
    assert set(read_exif_times(modified).values()) == {"2026:10:26 16:47:17"}

    partial = tmp_path / "partial.jpg"
    partial.write_bytes(raw)
    subprocess.run(["exiftool", "-overwrite_original", "-CreateDate=2023:09:15 10:45:12",
                    str(partial)], check=True, capture_output=True)
    before = read_exif_times(partial)
    assert before["DateTimeOriginal"] is None
    edited = make_updated_copy(partial, 2, "2023-09-16T12:00:01")
    after = read_exif_times(edited)
    assert after["CreateDate"] == before["CreateDate"]
    assert after["DateTimeOriginal"] == "2023:09:16 12:00:01"
    assert read_exif_times(partial) == before


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_worker_original_hash_alias_and_duplicate(tmp_path):
    conn = init_db(tmp_path / "photos.db")
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                 " VALUES ('camera','Camera',1,'t','t')")
    conn.commit()
    content = jpeg()
    raw_sha = hashlib.sha256(content).hexdigest()
    first = seed(conn, tmp_path, content, "first.jpg", "2026-10-26T16:47:17")
    result = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                          tmp_path / "Photos", first, None)
    assert result["status"] == "COMPLETED"
    asset = conn.execute("SELECT * FROM photo_assets WHERE id=?",
                         (result["asset_id"],)).fetchone()
    assert asset["sha256"] != raw_sha
    assert asset["date_taken"].startswith("2026-10-26")
    assert conn.execute("SELECT asset_id FROM photo_source_hashes WHERE source_sha256=?",
                        (raw_sha,)).fetchone()[0] == result["asset_id"]
    archived = tmp_path / asset["archive_path"]
    data = archived.read_bytes()
    second = seed(conn, tmp_path, content, "second.jpg", "2028-01-01T10:00:00")
    again = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                         tmp_path / "Photos", second, None)
    assert again["status"] == "DUPLICATE"
    assert archived.read_bytes() == data
    assert conn.execute("SELECT COUNT(*) FROM photo_assets").fetchone()[0] == 1
    conn.close()


def test_skip_no_match_and_reject_bad_date(tmp_path):
    db_path = tmp_path / "data.db"
    init_db(db_path).close()
    client = TestClient(create_app(cfg(tmp_path), db_path=db_path))
    sid = client.post("/api/v1/upload-sessions",
                      json={"source_device": "camera"}).json()["data"]["session"]["id"]
    res = client.post(f"/api/v1/upload-sessions/{sid}/items/precheck",
                      json={"items": [{"client_item_id": "1", "filename": "a.jpg",
                                       "file_size": 5}]}).json()
    iid = res["data"]["items"][0]["item_id"]
    invalid = client.post(f"/api/v1/upload-items/{iid}/file",
                          data={"exif_time": "2026-02-30T10:00:00"},
                          files={"file": ("a.jpg", b"12345", "image/jpeg")})
    assert invalid.status_code == 422
    assert client.post(f"/api/v1/upload-items/{iid}/skip", json={}).status_code == 200
    assert client.post(f"/api/v1/upload-items/{iid}/skip", json={}).status_code == 409
    data = client.get(f"/api/v1/upload-sessions/{sid}").json()["data"]["session"]
    assert data["status"] == "COMPLETED"
    assert data["skipped_files"] == 1


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_metadata_alias_deleted_photo_still_requires_review(tmp_path):
    """改过 EXIF 的资产，原始字节再次上传也必须进入回收站审查。"""
    from photo_gateway.recycle import trash, decide_deleted_upload
    root = tmp_path
    conn = init_db(root / "photos.db")
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                 " VALUES ('camera','Camera',1,'t','t')")
    conn.commit()
    original = jpeg()
    first = seed(conn, root, original, "first.jpg", "2026-10-26T16:47:17")
    first_result = process_once(conn, cfg(root), root / "incoming",
                                root / "Photos", first, None)
    asset_id = first_result["asset_id"]
    stored = conn.execute("SELECT sha256 FROM photo_assets WHERE id=?",
                          (asset_id,)).fetchone()[0]
    assert stored != hashlib.sha256(original).hexdigest()
    trash(conn, cfg(root), asset_id)
    second = seed(conn, root, original, "again.jpg", "2027-01-01T09:00:00")
    result = process_once(conn, cfg(root), root / "incoming",
                          root / "Photos", second, None)
    assert result["status"] == "REVIEW_REQUIRED"
    assert conn.execute("SELECT lifecycle_state FROM photo_assets WHERE id=?",
                        (asset_id,)).fetchone()[0] == "TRASHED"
    # 用户恢复已有回收站副本，原始 SHA 不变，pending raw 源文件被安全移除。
    response = decide_deleted_upload(conn, cfg(root), second["id"], "restore")
    assert response["choice"] == "restore"
    asset = conn.execute("SELECT * FROM photo_assets WHERE id=?", (asset_id,)).fetchone()
    assert asset["lifecycle_state"] == "ACTIVE"
    assert (root / asset["archive_path"]).is_file()
    assert hashlib.sha256((root / asset["archive_path"]).read_bytes()).hexdigest() == stored
    conn.close()



@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_metadata_alias_purged_photo_cannot_restore_wrong_bytes(tmp_path):
    from photo_gateway.recycle import decide_deleted_upload
    from photo_gateway.security import PgApiError
    conn = init_db(tmp_path / "photos.db")
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                 " VALUES ('camera','Camera',1,'t','t')")
    conn.commit()
    content = jpeg()
    one = seed(conn, tmp_path, content, "old.jpg", "2026-10-26T16:47:17")
    original = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                            tmp_path / "Photos", one, None)
    aid = original["asset_id"]
    asset = conn.execute("SELECT * FROM photo_assets WHERE id=?", (aid,)).fetchone()
    (tmp_path / asset["archive_path"]).unlink()
    conn.execute("UPDATE photo_assets SET lifecycle_state='PURGED',purged_at='t' WHERE id=?",
                 (aid,))
    conn.commit()
    second = seed(conn, tmp_path, content, "again.jpg", None)
    result = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                          tmp_path / "Photos", second, None)
    assert result["status"] == "REVIEW_REQUIRED"
    with pytest.raises(PgApiError) as error:
        decide_deleted_upload(conn, cfg(tmp_path), second["id"], "restore")
    assert error.value.code == "ORIGINAL_BYTES_UNAVAILABLE"
    assert decide_deleted_upload(conn, cfg(tmp_path), second["id"],
                                 "keep_deleted")["choice"] == "keep_deleted"
    assert conn.execute("SELECT lifecycle_state FROM photo_assets WHERE id=?",
                        (aid,)).fetchone()[0] == "PURGED"
    conn.close()


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_retry_same_item_new_bytes_must_not_keep_old_source_sha(tmp_path, monkeypatch):
    """R1: A 失败→同一 item 换等长 B；不能把 A 绑定到 B 导致丢片。"""
    from photo_gateway.db import connect
    from photo_gateway.exif_time import ExifTimeError
    import photo_gateway.exif_time as exif_impl

    def picture(rgb):
        stream = io.BytesIO()
        Image.new("RGB", (8, 8), rgb).save(stream, "JPEG")
        return stream.getvalue()

    a, b = picture((12, 24, 36)), picture((36, 24, 12))
    assert len(a) == len(b) and a != b
    sha_a, sha_b = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
    db = tmp_path / "photos.db"
    init_db(db).close()
    client = TestClient(create_app(cfg(tmp_path), db_path=db))

    def item_for(name):
        sid = client.post("/api/v1/upload-sessions",
                          json={"source_device": "camera"}).json()["data"]["session"]["id"]
        result = client.post(f"/api/v1/upload-sessions/{sid}/items/precheck",
                             json={"items": [{"client_item_id": str(uuid.uuid4()),
                                              "filename": name, "file_size": len(a)}]})
        assert result.status_code == 200
        return result.json()["data"]["items"][0]["item_id"]

    item_id = item_for("replace.jpg")
    def upload(item_id, name, blob):
        result = client.post(f"/api/v1/upload-items/{item_id}/file",
                             data={"exif_time": "2026-10-26T16:47:17"},
                             files={"file": (name, blob, "image/jpeg")})
        assert result.status_code == 200, result.text

    upload(item_id, "replace.jpg", a)
    conn = connect(db)
    with monkeypatch.context() as patch:
        patch.setattr(exif_impl, "make_updated_copy",
                      lambda *_: (_ for _ in ()).throw(ExifTimeError("synthetic failure")))
        item = conn.execute("SELECT * FROM upload_items WHERE id=?", (item_id,)).fetchone()
        assert process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                            tmp_path / "Photos", item, None)["status"] == "FAILED"
    assert conn.execute("SELECT source_sha256 FROM upload_items WHERE id=?",
                        (item_id,)).fetchone()[0] == sha_a
    # 生产 Worker 在处理失败后会对会话收尾；重传应正确重新打开会话。
    from photo_gateway.store import finalize_session
    sid = conn.execute("SELECT session_id FROM upload_items WHERE id=?",
                       (item_id,)).fetchone()[0]
    finalize_session(conn, sid)
    assert conn.execute("SELECT status FROM upload_sessions WHERE id=?",
                        (sid,)).fetchone()[0] == "PARTIAL"

    upload(item_id, "replace.jpg", b)
    assert conn.execute("SELECT status FROM upload_sessions WHERE id=?",
                        (sid,)).fetchone()[0] == "UPLOADING"
    assert conn.execute("SELECT source_sha256 FROM upload_items WHERE id=?",
                        (item_id,)).fetchone()[0] is None
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (item_id,)).fetchone()
    result_b = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                            tmp_path / "Photos", item, None)
    assert result_b["status"] == "COMPLETED"
    assert conn.execute("SELECT asset_id FROM photo_source_hashes WHERE source_sha256=?",
                        (sha_b,)).fetchone()[0] == result_b["asset_id"]
    assert conn.execute("SELECT 1 FROM photo_source_hashes WHERE source_sha256=?",
                        (sha_a,)).fetchone() is None

    second_id = item_for("real_a.jpg")
    upload(second_id, "real_a.jpg", a)
    item = conn.execute("SELECT * FROM upload_items WHERE id=?", (second_id,)).fetchone()
    result_a = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                            tmp_path / "Photos", item, None)
    assert result_a["status"] == "COMPLETED"
    assert result_a["asset_id"] != result_b["asset_id"]
    assert conn.execute("SELECT COUNT(*) FROM photo_assets").fetchone()[0] == 2
    conn.close()


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_xmp_only_is_not_exif_and_exif_wins_conflict(tmp_path):
    """R2: XMP 同名字段不得冒充真实 EXIF，也不得被新补写覆盖。"""
    src = tmp_path / "xmp_only.jpg"
    src.write_bytes(jpeg())
    xmp_date = "2023:09:15 10:45:12"
    subprocess.run(["exiftool", "-overwrite_original",
                    f"-XMP-exif:DateTimeOriginal={xmp_date}",
                    f"-XMP-xmp:CreateDate={xmp_date}",
                    f"-XMP-xmp:ModifyDate={xmp_date}", str(src)],
                   check=True, capture_output=True)
    assert all(value is None for value in read_exif_times(src).values())
    out = make_updated_copy(src, 100, "2026-01-01T00:00:00")
    assert out != src
    assert set(read_exif_times(out).values()) == {"2026:01:01 00:00:00"}
    assert all(value is None for value in read_exif_times(src).values())
    xmp = subprocess.run(["exiftool", "-s3", "-XMP-exif:DateTimeOriginal",
                          str(out)], capture_output=True, check=True, text=True)
    assert xmp.stdout.strip() == xmp_date

    conflict = tmp_path / "conflict.jpg"
    conflict.write_bytes(jpeg())
    subprocess.run(["exiftool", "-overwrite_original",
                    "-EXIF:DateTimeOriginal=2024:03:04 11:22:00",
                    "-XMP-exif:DateTimeOriginal=2021:01:01 01:01:01",
                    str(conflict)], check=True, capture_output=True)
    assert read_exif_times(conflict)["DateTimeOriginal"] == "2024:03:04 11:22:00"
    amended = make_updated_copy(conflict, 101, "2026-01-01T00:00:00")
    assert amended != conflict
    assert read_exif_times(amended)["DateTimeOriginal"] == "2024:03:04 11:22:00"


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_worker_interrupted_after_exif_rewrite_resumes_with_source_identity(
        tmp_path, monkeypatch):
    """R1 边界：Worker 补写 EXIF 成功后崩溃，重跑不能把已改写 SHA 当原始 SHA。"""
    import photo_gateway.worker as worker
    conn = init_db(tmp_path / "photos.db")
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                 " VALUES ('camera','Camera',1,'t','t')")
    conn.commit()
    raw = jpeg()
    item = seed(conn, tmp_path, raw, "crash.jpg", "2026-10-26T16:47:17")
    source_sha = hashlib.sha256(raw).hexdigest()
    src = tmp_path / "incoming" / "crash.jpg"
    with monkeypatch.context() as m:
        m.setattr(worker, "_resolve_archive_dir",
                  lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
        with pytest.raises(RuntimeError, match="synthetic crash"):
            process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                         tmp_path / "Photos", item, None)
    stored = conn.execute("SELECT source_sha256,status FROM upload_items WHERE id=?",
                          (item["id"],)).fetchone()
    assert stored["source_sha256"] == source_sha
    assert stored["status"] == "UPLOADED"
    changed_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    assert changed_sha != source_sha
    assert read_exif_times(src)["DateTimeOriginal"] == "2026:10:26 16:47:17"

    # 以新连接模拟 Worker 重启，使用同一上传项继续处理。
    conn.close()
    from photo_gateway.db import connect
    conn = connect(tmp_path / "photos.db")
    fresh = conn.execute("SELECT * FROM upload_items WHERE id=?", (item["id"],)).fetchone()
    result = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                          tmp_path / "Photos", fresh, None)
    assert result["status"] == "COMPLETED"
    asset = conn.execute("SELECT * FROM photo_assets WHERE id=?",
                         (result["asset_id"],)).fetchone()
    assert asset["sha256"] == changed_sha
    assert conn.execute("SELECT asset_id FROM photo_source_hashes WHERE source_sha256=?",
                        (source_sha,)).fetchone()[0] == result["asset_id"]
    conn.close()


def test_reupload_while_uploaded_is_rejected_without_overwrite(tmp_path):
    """Worker 待处理时禁止再次覆盖相同 item 的 incoming 文件。"""
    db = tmp_path / "photos.db"
    init_db(db).close()
    client = TestClient(create_app(cfg(tmp_path), db_path=db))
    sid = client.post("/api/v1/upload-sessions",
                      json={"source_device": "camera"}).json()["data"]["session"]["id"]
    payload = jpeg()
    reg = client.post(f"/api/v1/upload-sessions/{sid}/items/precheck",
                      json={"items": [{"client_item_id": "test", "filename": "hold.jpg",
                                       "file_size": len(payload)}]})
    iid = reg.json()["data"]["items"][0]["item_id"]
    first = client.post(f"/api/v1/upload-items/{iid}/file",
                        files={"file": ("hold.jpg", payload, "image/jpeg")})
    assert first.status_code == 200
    other = io.BytesIO()
    Image.new("RGB", (8, 8), (50, 60, 70)).save(other, "JPEG")
    assert len(other.getvalue()) == len(payload)
    retry = client.post(f"/api/v1/upload-items/{iid}/file",
                        files={"file": ("hold.jpg", other.getvalue(), "image/jpeg")})
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "ITEM_NOT_RETRYABLE"
    assert (tmp_path / "incoming" / "hold.jpg").read_bytes() == payload


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool unavailable")
def test_xmp_only_upload_archives_using_confirmed_exif_month(tmp_path):
    """R2 端到端：XMP-only 的旧日期不得覆盖用户采纳的 EXIF 归档月份。"""
    incoming = tmp_path / "xmp-source.jpg"
    incoming.write_bytes(jpeg())
    subprocess.run([
        "exiftool", "-overwrite_original",
        "-XMP-exif:DateTimeOriginal=2022:09:15 10:45:12",
        "-XMP-xmp:CreateDate=2022:09:15 10:45:12",
        "-XMP-xmp:ModifyDate=2022:09:15 10:45:12",
        str(incoming),
    ], check=True, capture_output=True)
    source_bytes = incoming.read_bytes()
    assert all(v is None for v in read_exif_times(incoming).values())
    conn = init_db(tmp_path / "photos.db")
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                 " VALUES ('camera','Camera',1,'t','t')")
    conn.commit()
    item = seed(conn, tmp_path, source_bytes, "xmp-only.jpg", "2026-01-01T00:00:00")
    result = process_once(conn, cfg(tmp_path), tmp_path / "incoming",
                          tmp_path / "Photos", item, None)
    assert result["status"] == "COMPLETED"
    asset = conn.execute("SELECT * FROM photo_assets WHERE id=?", (result["asset_id"],)).fetchone()
    assert asset["date_taken"].startswith("2026-01-01")
    assert asset["date_source"] == "DateTimeOriginal"
    assert asset["archive_path"].startswith("Photos/2026/202601/")
    assert set(read_exif_times(tmp_path / asset["archive_path"]).values()) == {"2026:01:01 00:00:00"}
    assert incoming.read_bytes() == source_bytes
    conn.close()
