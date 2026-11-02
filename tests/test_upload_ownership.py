"""上传请求持久占用：真实 ASGI 并发、Worker 交错、疑似重复确认。合成数据。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from PIL import Image
from starlette.datastructures import UploadFile

from photo_gateway.db import connect, init_db
from photo_gateway.http import create_app
from photo_gateway.worker import process_once, run_round

from test_exif_review import cfg, jpeg


def make_img(color, size=2 * 1024 * 1024) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(b, "JPEG")
    prefix = b.getvalue()
    return prefix + bytes([color[0]]) * (size - len(prefix))


async def register(client, filename: str, size: int):
    sess = await client.post("/api/v1/upload-sessions", json={"source_device": "camera"})
    assert sess.status_code == 201
    sid = sess.json()["data"]["session"]["id"]
    pre = await client.post(f"/api/v1/upload-sessions/{sid}/items/precheck",
                            json={"items": [{"client_item_id": str(uuid.uuid4()),
                                             "filename": filename, "file_size": size}]})
    assert pre.status_code == 200
    return sid, pre.json()["data"]["items"][0]


def app_env(root):
    db = root / "test.db"
    init_db(db).close()
    return db, create_app(cfg(root), db_path=db)


def test_same_item_concurrent_stress_only_one_publishes(tmp_path):
    """不 patch 上传实现，20×2 MiB 同时 POST：一份成功，一份在触盘前 409。"""
    async def scenario():
        db, app = app_env(tmp_path)
        a, b = make_img((12, 24, 36)), make_img((36, 24, 12))
        assert len(a) == len(b)
        sha = {hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()}
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            for i in range(20):
                filename = f"stress-{i}.jpg"
                _, item = await register(client, filename, len(a))
                async def send(blob):
                    return await client.post(
                        f"/api/v1/upload-items/{item['item_id']}/file",
                        files={"file": (filename, blob, "image/jpeg")})
                one, two = await asyncio.gather(send(a), send(b))
                assert sorted([one.status_code, two.status_code]) == [200, 409], (
                    i, one.status_code, one.text[:100], two.status_code, two.text[:100])
                rejected = one if one.status_code == 409 else two
                assert rejected.json()["error"]["code"] == "ITEM_NOT_RETRYABLE"
                final = tmp_path / "incoming" / filename
                assert hashlib.sha256(final.read_bytes()).hexdigest() in sha
                assert not list((tmp_path / "incoming").glob(f".upload-{item['item_id']}-*.tmp"))
                conn = connect(db)
                row = conn.execute("SELECT status,upload_token,source_sha256 FROM upload_items"
                                   " WHERE id=?", (item["item_id"],)).fetchone()
                assert row["status"] == "UPLOADED" and row["upload_token"] is None
                assert row["source_sha256"] is None
                conn.close()
    asyncio.run(scenario())


def test_denied_request_never_opens_stream_or_touch_worker_file(tmp_path):
    """在真实 UploadFile.read 的 await 边界控制顺序，Worker 不得消费或混读。"""
    async def scenario():
        db, app = app_env(tmp_path)
        a, b = make_img((12, 24, 36)), make_img((36, 24, 12))
        assert a != b and len(a) == len(b)
        await_first_read = asyncio.Event()
        release_first_read = asyncio.Event()
        reads = set()
        original_read = UploadFile.read

        async def barrier_read(self, *args, **kwargs):
            reads.add(self.filename)
            if self.filename == "A.jpg" and not await_first_read.is_set():
                await_first_read.set()
                await asyncio.wait_for(release_first_read.wait(), timeout=8)
            return await original_read(self, *args, **kwargs)

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            _, item = await register(client, "overlap.jpg", len(a))
            iid = item["item_id"]
            async def send(content, name):
                return await client.post(
                    f"/api/v1/upload-items/{iid}/file",
                    data={"exif_time": "2026-03-04T11:22:33"},
                    files={"file": (name, content, "image/jpeg")})
            with patch.object(UploadFile, "read", barrier_read):
                first = asyncio.create_task(send(a, "A.jpg"))
                await asyncio.wait_for(await_first_read.wait(), timeout=8)
                conn = connect(db)
                before = conn.execute("SELECT status,upload_token FROM upload_items"
                                      " WHERE id=?", (iid,)).fetchone()
                assert before["status"] == "RECEIVING" and before["upload_token"]
                # 请求 A 仍持有写入权；无完整文件可供 Worker 消费。
                stats = run_round(conn, cfg(tmp_path),
                                  {"incoming": tmp_path/"incoming",
                                   "archive": tmp_path/"Photos"}, None)
                assert stats["processed"] == 0
                conn.close()
                denied = await send(b, "B.jpg")
                assert denied.status_code == 409
                assert denied.json()["error"]["code"] == "ITEM_NOT_RETRYABLE"
                assert "B.jpg" not in reads
                release_first_read.set()
                accepted = await first
                assert accepted.status_code == 200, accepted.text
            final = tmp_path / "incoming" / "overlap.jpg"
            assert hashlib.sha256(final.read_bytes()).hexdigest() == hashlib.sha256(a).hexdigest()
            conn = connect(db)
            item_row = conn.execute("SELECT * FROM upload_items WHERE id=?", (iid,)).fetchone()
            archived = process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                    tmp_path/"Photos", item_row, None)
            assert archived["status"] == "COMPLETED"
            alias = conn.execute("SELECT asset_id FROM photo_source_hashes WHERE"
                                 " source_sha256=?", (hashlib.sha256(a).hexdigest(),)).fetchone()
            assert alias is not None
            assert alias[0] == archived["asset_id"]
            # B 作为新照片必须有机会被接收并归档。
            _, separate = await register(client, "real-B.jpg", len(b))
            posted = await client.post(f"/api/v1/upload-items/{separate['item_id']}/file",
                                       data={"exif_time": "2026-03-04T11:22:33"},
                                       files={"file": ("real-B.jpg", b, "image/jpeg")})
            assert posted.status_code == 200
            second_row = conn.execute("SELECT * FROM upload_items WHERE id=?",
                                      (separate["item_id"],)).fetchone()
            newer = process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                 tmp_path/"Photos", second_row, None)
            assert newer["status"] == "COMPLETED"
            assert newer["asset_id"] != archived["asset_id"]
            assert conn.execute("SELECT COUNT(*) FROM photo_assets").fetchone()[0] == 2
            conn.close()
    asyncio.run(scenario())


def test_confirmed_possible_duplicate_can_upload_and_worker_checks_bytes(tmp_path):
    """C2: POSSIBLE_DUPLICATE→confirmation=1→UPLOADING→/file→Worker DUPLICATE。"""
    async def scenario():
        db, app = app_env(tmp_path)
        raw = jpeg()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            _, item = await register(client, "repeat.jpg", len(raw))
            first = await client.post(f"/api/v1/upload-items/{item['item_id']}/file",
                                      files={"file": ("repeat.jpg", raw, "image/jpeg")})
            assert first.status_code == 200
            conn = connect(db)
            row = conn.execute("SELECT * FROM upload_items WHERE id=?",
                               (item["item_id"],)).fetchone()
            archived = process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                    tmp_path/"Photos", row, None)
            assert archived["status"] == "COMPLETED"
            conn.close()

            _, repeated = await register(client, "repeat.jpg", len(raw))
            assert repeated["precheck_status"] == "POSSIBLE_DUPLICATE"
            iid = repeated["item_id"]
            decision = await client.post(f"/api/v1/upload-items/{iid}/confirmation",
                                         json={"confirmation": 1})
            assert decision.status_code == 200 and decision.json()["data"]["upload_required"]
            conn = connect(db)
            assert conn.execute("SELECT status FROM upload_items WHERE id=?",
                                (iid,)).fetchone()[0] == "UPLOADING"
            conn.close()
            sent = await client.post(f"/api/v1/upload-items/{iid}/file",
                                     files={"file": ("repeat.jpg", raw, "image/jpeg")})
            assert sent.status_code == 200, sent.text
            conn = connect(db)
            follow = conn.execute("SELECT * FROM upload_items WHERE id=?", (iid,)).fetchone()
            assert follow["status"] == "UPLOADED"
            result = process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                  tmp_path/"Photos", follow, None)
            assert result["status"] == "DUPLICATE"
            assert result["asset_id"] == archived["asset_id"]
            assert conn.execute("SELECT COUNT(*) FROM photo_assets").fetchone()[0] == 1
            conn.close()
    asyncio.run(scenario())


def test_expired_upload_token_cannot_publish_or_abort_new_owner(tmp_path):
    """旧上传超时后旧请求仍运行：旧 token 不能发布/释放新 token。"""
    from photo_gateway.upload_claim import claim, private_stage_path, publish, release_failed
    from photo_gateway.security import PgApiError
    db, app = app_env(tmp_path)
    import asyncio as _asyncio

    async def make_item():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as c:
            _, item = await register(c, "stale.jpg", 4)
            return item["item_id"]
    iid = _asyncio.run(make_item())
    conn = connect(db)
    first_row, old_token = claim(conn, iid)
    stage_a = private_stage_path(tmp_path/"incoming", "stale.jpg", iid, old_token)
    stage_a.parent.mkdir(parents=True, exist_ok=True)
    stage_a.write_bytes(b"AAAA")
    # 不等待真实两小时；只回拨当前测试库里过期的认领时间。
    conn.execute("UPDATE upload_items SET upload_claimed_at=1 WHERE id=?", (iid,))
    conn.commit()
    _, new_token = claim(conn, iid)
    assert old_token != new_token
    stage_b = private_stage_path(tmp_path/"incoming", "stale.jpg", iid, new_token)
    stage_b.write_bytes(b"BBBB")
    with __import__("pytest").raises(PgApiError) as info:
        publish(conn, iid, old_token, stage_a, tmp_path/"incoming"/"stale.jpg", None)
    assert info.value.code == "UPLOAD_CLAIM_LOST"
    assert release_failed(conn, iid, old_token, "stale request") is False
    assert not (tmp_path/"incoming"/"stale.jpg").exists()
    publish(conn, iid, new_token, stage_b, tmp_path/"incoming"/"stale.jpg", None)
    assert (tmp_path/"incoming"/"stale.jpg").read_bytes() == b"BBBB"
    assert conn.execute("SELECT status,upload_token FROM upload_items WHERE id=?",
                        (iid,)).fetchone()["status"] == "UPLOADED"
    conn.close()


def test_aborted_upload_releases_claim_without_publishing(tmp_path):
    """上传大小错误只可留下自己的失败状态，不得暴露半文件。"""
    async def scenario():
        db, app = app_env(tmp_path)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            _, item = await register(client, "short.jpg", 10)
            iid = item["item_id"]
            bad = await client.post(f"/api/v1/upload-items/{iid}/file",
                                    files={"file": ("short.jpg", b"abc", "image/jpeg")})
            assert bad.status_code == 400
            assert not (tmp_path/"incoming"/"short.jpg").exists()
            assert not list((tmp_path/"incoming").glob(f".upload-{iid}-*.tmp"))
            conn = connect(db)
            row = conn.execute("SELECT status,upload_token FROM upload_items WHERE id=?",
                               (iid,)).fetchone()
            assert row["status"] == "FAILED" and row["upload_token"] is None
            conn.close()
            retry = await client.post(f"/api/v1/upload-items/{iid}/file",
                                      files={"file": ("short.jpg", b"0123456789", "image/jpeg")})
            assert retry.status_code == 200, retry.text
            assert (tmp_path/"incoming"/"short.jpg").read_bytes() == b"0123456789"
    asyncio.run(scenario())


def test_same_filename_across_items_isolated_until_worker_finishes(tmp_path):
    """同名不同 item 的 V1 incoming 路径必须串行，第二个不得覆盖第一个。"""
    async def scenario():
        db, app = app_env(tmp_path)
        raw = jpeg()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            _, a = await register(client, "shared.jpg", len(raw))
            _, b = await register(client, "shared.jpg", len(raw))
            ok = await client.post(f"/api/v1/upload-items/{a['item_id']}/file",
                                   files={"file": ("shared.jpg", raw, "image/jpeg")})
            assert ok.status_code == 200
            denied = await client.post(f"/api/v1/upload-items/{b['item_id']}/file",
                                       files={"file": ("shared.jpg", raw, "image/jpeg")})
            assert denied.status_code == 409
            assert denied.json()["error"]["code"] == "FILENAME_IN_USE"
            assert (tmp_path/"incoming"/"shared.jpg").read_bytes() == raw
            conn = connect(db)
            item = conn.execute("SELECT * FROM upload_items WHERE id=?", (a["item_id"],)).fetchone()
            assert process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                tmp_path/"Photos", item, None)["status"] == "COMPLETED"
            conn.close()
            sent = await client.post(f"/api/v1/upload-items/{b['item_id']}/file",
                                     files={"file": ("shared.jpg", raw, "image/jpeg")})
            assert sent.status_code == 200
    asyncio.run(scenario())


def test_confirmed_deleted_photo_still_requires_review(tmp_path):
    """疑似重复→确认重新上传→资产已删，仍须人工恢复审核。"""
    from photo_gateway.recycle import trash
    async def scenario():
        db, app = app_env(tmp_path)
        raw = jpeg()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            _, first = await register(client, "deleted.jpg", len(raw))
            res = await client.post(f"/api/v1/upload-items/{first['item_id']}/file",
                                    files={"file": ("deleted.jpg", raw, "image/jpeg")})
            assert res.status_code == 200
            conn = connect(db)
            item = conn.execute("SELECT * FROM upload_items WHERE id=?",
                                (first["item_id"],)).fetchone()
            result = process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                  tmp_path/"Photos", item, None)
            assert result["status"] == "COMPLETED"
            trash(conn, cfg(tmp_path), result["asset_id"])
            conn.close()
            _, candidate = await register(client, "deleted.jpg", len(raw))
            assert candidate["precheck_status"] == "POSSIBLE_DUPLICATE"
            decision = await client.post(
                f"/api/v1/upload-items/{candidate['item_id']}/confirmation",
                json={"confirmation": 1})
            assert decision.status_code == 200
            uploaded = await client.post(
                f"/api/v1/upload-items/{candidate['item_id']}/file",
                files={"file": ("deleted.jpg", raw, "image/jpeg")})
            assert uploaded.status_code == 200
            conn = connect(db)
            item = conn.execute("SELECT * FROM upload_items WHERE id=?",
                                (candidate["item_id"],)).fetchone()
            review = process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                  tmp_path/"Photos", item, None)
            assert review["status"] == "REVIEW_REQUIRED"
            assert review["asset_id"] == result["asset_id"]
            assert conn.execute("SELECT lifecycle_state FROM photo_assets WHERE id=?",
                                (result["asset_id"],)).fetchone()[0] == "TRASHED"
            conn.close()
    asyncio.run(scenario())


def test_expired_other_item_same_filename_is_revoked_atomically(tmp_path):
    """过期的同名旧请求不得在另一个 item 获得上传权后重新发布。"""
    from photo_gateway.upload_claim import claim, private_stage_path, publish
    from photo_gateway.security import PgApiError
    import pytest

    db, app = app_env(tmp_path)

    async def items():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            _, first = await register(client, "stale-name.jpg", 4)
            _, second = await register(client, "stale-name.jpg", 4)
            return first["item_id"], second["item_id"]

    first_id, second_id = asyncio.run(items())
    conn = connect(db)
    _, old_token = claim(conn, first_id)
    first_stage = private_stage_path(tmp_path/"incoming", "stale-name.jpg",
                                     first_id, old_token)
    first_stage.parent.mkdir(exist_ok=True)
    first_stage.write_bytes(b"AAAA")
    conn.execute("UPDATE upload_items SET upload_claimed_at=1 WHERE id=?", (first_id,))
    conn.commit()

    _, second_token = claim(conn, second_id)
    old = conn.execute("SELECT status,upload_token FROM upload_items WHERE id=?",
                       (first_id,)).fetchone()
    assert old["status"] == "FAILED" and old["upload_token"] is None
    with pytest.raises(PgApiError) as error:
        publish(conn, first_id, old_token, first_stage,
                tmp_path/"incoming"/"stale-name.jpg", None)
    assert error.value.code == "UPLOAD_CLAIM_LOST"
    second_stage = private_stage_path(tmp_path/"incoming", "stale-name.jpg",
                                      second_id, second_token)
    second_stage.write_bytes(b"BBBB")
    publish(conn, second_id, second_token, second_stage,
            tmp_path/"incoming"/"stale-name.jpg", None)
    assert (tmp_path/"incoming"/"stale-name.jpg").read_bytes() == b"BBBB"
    conn.close()


def test_skip_cannot_override_receiving_claim(tmp_path):
    """EXIF 页的暂不上传与 /file 并发时，不能把已认领的上传记为跳过。"""
    from photo_gateway.upload_claim import claim
    from photo_gateway.store import skip_unmatched_item

    db, app = app_env(tmp_path)

    async def item():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            _, entry = await register(client, "skip-race.jpg", 4)
            return entry["item_id"]

    iid = asyncio.run(item())
    conn = connect(db)
    _, token = claim(conn, iid)
    assert skip_unmatched_item(conn, iid) is None
    record = conn.execute("SELECT status,user_confirmation,upload_token "
                          "FROM upload_items WHERE id=?", (iid,)).fetchone()
    assert record["status"] == "RECEIVING"
    assert record["user_confirmation"] == 0
    assert record["upload_token"] == token
    conn.close()


def test_v5_to_v6_migration_preserves_confirmed_upload_intent(tmp_path):
    """真实 v5 迁移至 v6 后，旧的 UPLOADING（已确认）可安全申请发布权。"""
    from photo_gateway.db import SCHEMA_SCRIPTS, connect, migrate
    from photo_gateway.upload_claim import claim

    db = tmp_path / "legacy-v5.db"
    conn = connect(db)
    for version, sql in SCHEMA_SCRIPTS:
        if version > 5:
            break
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version(version,updated_at) VALUES (?,?)",
                     (version, "2026-11-02 12:00:00"))
        conn.commit()
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at)"
                 " VALUES ('camera','Camera',1,'t','t')")
    conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at)"
                 " VALUES ('legacy','camera','UPLOADING','t')")
    conn.execute(
        "INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
        "file_size,status,precheck_status,user_confirmation,created_at,updated_at)"
        " VALUES ('legacy','camera','old-confirmed','old.jpg',4,'UPLOADING',"
        "'POSSIBLE_DUPLICATE',1,'t','t')")
    conn.commit()
    iid = conn.execute("SELECT id FROM upload_items WHERE session_id='legacy'").fetchone()[0]
    assert migrate(conn) == 6
    assert migrate(conn) == 6
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    cols = {row[1] for row in conn.execute("PRAGMA table_info(upload_items)")}
    assert {"upload_token", "upload_claimed_at"}.issubset(cols)
    record, token = claim(conn, iid)
    assert record["status"] == "UPLOADING"
    after = conn.execute("SELECT status,upload_token FROM upload_items WHERE id=?",
                         (iid,)).fetchone()
    assert after["status"] == "RECEIVING"
    assert after["upload_token"] == token
    conn.close()


def test_duplicate_cleanup_keeps_filename_owned_until_last_unlink(tmp_path):
    """A duplicate Worker may never unlink a later item's successfully published file."""
    async def scenario():
        db, app = app_env(tmp_path)
        a, b = make_img((12, 24, 36)), make_img((36, 24, 12))
        reached, resume = threading.Event(), threading.Event()
        shared = tmp_path / "incoming" / "shared.jpg"
        unlink = Path.unlink

        def pause_cleanup(path, *args, **kwargs):
            if path == shared:
                reached.set()
                assert resume.wait(8), "cleanup barrier timed out"
            return unlink(path, *args, **kwargs)

        def worker(iid):
            conn = connect(db)
            try:
                row = conn.execute("SELECT * FROM upload_items WHERE id=?", (iid,)).fetchone()
                return process_once(conn, cfg(tmp_path), tmp_path/"incoming",
                                    tmp_path/"Photos", row, None)
            finally:
                conn.close()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test.local") as client:
            _, original = await register(client, "original.jpg", len(a))
            assert (await client.post(f"/api/v1/upload-items/{original['item_id']}/file",
                                     files={"file": ("A.jpg", a, "image/jpeg")})).status_code == 200
            assert worker(original["item_id"])["status"] == "COMPLETED"
            _, duplicate = await register(client, "shared.jpg", len(a))
            _, newer = await register(client, "shared.jpg", len(b))
            url = f"/api/v1/upload-items/{newer['item_id']}/file"
            assert (await client.post(f"/api/v1/upload-items/{duplicate['item_id']}/file",
                                     files={"file": ("A.jpg", a, "image/jpeg")})).status_code == 200
            with patch.object(Path, "unlink", pause_cleanup):
                task = asyncio.create_task(asyncio.to_thread(worker, duplicate["item_id"]))
                try:
                    assert await asyncio.to_thread(reached.wait, 8)
                    denied = await client.post(url, files={"file": ("B.jpg", b, "image/jpeg")})
                finally:
                    resume.set()
                    completed = await task
            assert denied.status_code == 409, denied.text
            assert denied.json()["error"]["code"] == "FILENAME_IN_USE"
            assert completed["status"] == "DUPLICATE"
            assert (await client.post(url, files={"file": ("B.jpg", b, "image/jpeg")})).status_code == 200
            assert shared.read_bytes() == b
            result = worker(newer["item_id"])
            assert result["status"] == "COMPLETED"
            conn = connect(db)
            path = conn.execute("SELECT archive_path FROM photo_assets WHERE id=?",
                                (result["asset_id"],)).fetchone()[0]
            assert (tmp_path/path).read_bytes() == b
            conn.close()
    asyncio.run(scenario())


def test_duplicate_cleanup_interruption_recovers_only_known_active_archive(tmp_path):
    from test_exif_review import seed
    db, _ = app_env(tmp_path)
    conn = connect(db)
    conn.execute("INSERT INTO devices VALUES ('camera','Camera',1,'t','t')")
    conn.commit()
    raw = jpeg()
    first = seed(conn, tmp_path, raw, "first.jpg", None)
    archived = process_once(conn, cfg(tmp_path), tmp_path/"incoming", tmp_path/"Photos", first, None)
    second = seed(conn, tmp_path, raw, "second.jpg", None)
    src = tmp_path/"incoming"/"second.jpg"
    unlink = Path.unlink

    def unlink_then_crash(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == src:
            raise RuntimeError("interrupted after duplicate source cleanup")
        return result

    with patch.object(Path, "unlink", unlink_then_crash):
        with pytest.raises(RuntimeError, match="interrupted after duplicate"):
            process_once(conn, cfg(tmp_path), tmp_path/"incoming", tmp_path/"Photos", second, None)
    assert conn.execute("SELECT status FROM upload_items WHERE id=?",
                        (second["id"],)).fetchone()[0] == "UPLOADED"
    conn.close()
    conn = connect(db)
    fresh = conn.execute("SELECT * FROM upload_items WHERE id=?", (second["id"],)).fetchone()
    result = process_once(conn, cfg(tmp_path), tmp_path/"incoming", tmp_path/"Photos", fresh, None)
    assert result["status"] == "DUPLICATE"
    assert result["asset_id"] == archived["asset_id"]
    # No recovery may implicitly revive deleted assets or invent an unknown asset.
    third = seed(conn, tmp_path, raw, "missing.jpg", None)
    (tmp_path/"incoming"/"missing.jpg").unlink()
    conn.execute("UPDATE upload_items SET source_sha256=? WHERE id=?",
                 (hashlib.sha256(raw).hexdigest(), third["id"]))
    conn.execute("UPDATE photo_assets SET lifecycle_state='TRASHED' WHERE id=?",
                 (archived["asset_id"],))
    conn.commit()
    assert process_once(conn, cfg(tmp_path), tmp_path/"incoming", tmp_path/"Photos",
                        third, None)["status"] == "FAILED"
    conn.close()


def test_confirmed_duplicate_can_be_skipped_before_claim_but_not_after(tmp_path):
    from photo_gateway.upload_claim import claim
    async def scenario():
        db, app = app_env(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test.local") as client:
            for receiving in (False, True):
                sid, item = await register(client, f"skip-{receiving}.heic", 4)
                iid = item["item_id"]
                conn = connect(db)
                conn.execute("UPDATE upload_items SET precheck_status='POSSIBLE_DUPLICATE' WHERE id=?", (iid,))
                conn.commit()
                assert (await client.post(f"/api/v1/upload-items/{iid}/confirmation",
                                          json={"confirmation": 1})).status_code == 200
                token = claim(conn, iid)[1] if receiving else None
                res = await client.post(f"/api/v1/upload-items/{iid}/confirmation",
                                        json={"confirmation": 2})
                assert res.status_code == (409 if receiving else 200), res.text
                row = conn.execute("SELECT status,user_confirmation,upload_token FROM upload_items WHERE id=?",
                                   (iid,)).fetchone()
                if receiving:
                    assert tuple(row) == ("RECEIVING", 1, token)
                else:
                    assert tuple(row) == ("PRECHECK", 2, None)
                    session = (await client.get(f"/api/v1/upload-sessions/{sid}")).json()["data"]["session"]
                    assert session["status"] == "COMPLETED" and session["skipped_files"] == 1
                    assert (await client.post(f"/api/v1/upload-items/{iid}/file",
                                              files={"file": ("skipped.heic", b"data", "image/heic")})).status_code == 409
                conn.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("filename", ["n"*226+".jpg", "n"*246+".jpg", "照"*76+".jpg"])
def test_long_original_filename_uses_bounded_private_stage(tmp_path, filename):
    async def scenario():
        db, app = app_env(tmp_path)
        raw = jpeg()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                    base_url="http://test.local") as client:
            _, item = await register(client, filename, len(raw))
            res = await client.post(f"/api/v1/upload-items/{item['item_id']}/file",
                                    files={"file": (filename, raw, "image/jpeg")})
            assert res.status_code == 200, res.text
            assert (tmp_path/"incoming"/filename).read_bytes() == raw
            assert not list((tmp_path/"incoming").glob("*.tmp"))
    asyncio.run(scenario())


def test_long_filename_exif_working_copy_also_fits_filesystem(tmp_path):
    from test_exif_review import seed
    db, _ = app_env(tmp_path)
    conn = connect(db)
    conn.execute("INSERT INTO devices VALUES ('camera','Camera',1,'t','t')")
    conn.commit()
    name = "n"*246+".jpg"
    item = seed(conn, tmp_path, jpeg(), name, "2026-03-04T11:22:00")
    result = process_once(conn, cfg(tmp_path), tmp_path/"incoming", tmp_path/"Photos", item, None)
    assert result["status"] == "COMPLETED", result
    conn.close()


def test_stage_cleanup_error_still_releases_token_and_keeps_original_error(tmp_path, caplog):
    async def scenario():
        db, app = app_env(tmp_path)
        unlink = Path.unlink
        def fail_cleanup(path, *args, **kwargs):
            if path.parent == tmp_path/"incoming" and path.suffix == ".tmp":
                raise OSError("synthetic private staging cleanup failure")
            return unlink(path, *args, **kwargs)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                    base_url="http://test.local") as client:
            _, item = await register(client, "cleanup.jpg", 10)
            iid = item["item_id"]
            with patch.object(Path, "unlink", fail_cleanup):
                res = await client.post(f"/api/v1/upload-items/{iid}/file",
                                        files={"file": ("cleanup.jpg", b"abc", "image/jpeg")})
            assert res.status_code == 400, res.text
            assert res.json()["error"]["code"] == "SIZE_MISMATCH"
            conn = connect(db)
            row = conn.execute("SELECT status,upload_token FROM upload_items WHERE id=?", (iid,)).fetchone()
            assert tuple(row) == ("FAILED", None)
            assert "staging cleanup failed" in caplog.text
            assert (await client.post(f"/api/v1/upload-items/{iid}/file",
                                      files={"file": ("cleanup.jpg", b"0123456789", "image/jpeg")})).status_code == 200
            conn.close()
    asyncio.run(scenario())


def test_skip_winning_before_claim_prevents_any_file_read_or_publication(tmp_path):
    async def scenario():
        db, app = app_env(tmp_path)
        raw = jpeg()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test.local") as client:
            sid, item = await register(client, "already-skipped.jpg", len(raw))
            iid = item["item_id"]
            assert (await client.post(f"/api/v1/upload-items/{iid}/skip")).status_code == 200
            async def forbidden_read(*args, **kwargs):
                raise AssertionError("skipped upload must be rejected before file I/O")
            with patch.object(UploadFile, "read", forbidden_read):
                res = await client.post(f"/api/v1/upload-items/{iid}/file",
                                        files={"file": ("already-skipped.jpg", raw, "image/jpeg")})
            assert res.status_code == 409, res.text
            session = (await client.get(f"/api/v1/upload-sessions/{sid}")).json()["data"]["session"]
            assert session["skipped_files"] == 1 and session["uploaded_files"] == 0
            assert session["status"] == "COMPLETED"
            if (tmp_path/"incoming").exists():
                assert not list((tmp_path/"incoming").glob("*"))
    asyncio.run(scenario())
