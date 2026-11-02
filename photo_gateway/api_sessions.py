"""上传会话 / Precheck / 疑似重复确认 的 API 路由（M2，垂直切片）。

契约对齐 api-spec：
- POST /api/v1/upload-sessions         创建(§14)，body {source_device}，201
- 创建失败 DEVICE_NOT_FOUND / DEVICE_DISABLED（§15）
- GET  /api/v1/upload-sessions/{id}    获取+进度(§16/§17)
- POST /api/v1/upload-sessions/{id}/items/precheck   批量，幂等(§19/§20 body {items[]})
- GET  /api/v1/upload-items/{id}       单条详情(§26)
- POST /api/v1/upload-items/{id}/confirmation {confirmation:1|2}（§27）
DB 依赖：app.state.pg['db_path']；未配置(纯管理模式)→ 503。
"""

from __future__ import annotations

import sqlite3
import logging
from typing import Any
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from pydantic import BaseModel, Field

from . import store as st
from .security import AuthError, PgApiError, SessionStore, get_bearer_token

router = APIRouter(prefix="/api/v1", tags=["session"])
_FALLBACK_LOG = logging.getLogger("photo_gateway.api_sessions")


# ---------------------------------------------------------------------------
# 模型（api-spec §14/§19/§20/§27）
# ---------------------------------------------------------------------------
class CreateSessionBody(BaseModel):
    source_device: str = Field(min_length=1)


class PrecheckItem(BaseModel):
    client_item_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    file_size: int = Field(gt=0)


class BulkPrecheckBody(BaseModel):
    items: list[PrecheckItem] = Field(min_length=1)


class ConfirmationBody(BaseModel):
    confirmation: int


def _state(req: Request):
    return req.app.state.pg


def _guard(req: Request) -> None:
    state = _state(req)
    if not state["cfg"].auth.enabled:
        return
    auth = req.headers.get("authorization")
    token = get_bearer_token(auth)
    if token is None or not state["store"].verify(token):
        raise AuthError("AUTH_REQUIRED", "authentication required", 401)


def _upload_logger(req: Request):
    """返回 upload 域 logger；未装配 logging 时 None（静默，不影响功能）。"""
    m = _state(req).get("logman")
    return m.upload() if m is not None else None


def _preview_allowed(req: Request, asset_id: int) -> bool:
    """图片/缩略图访问：签名有效、Bearer 有效或 auth 未启用，任一即放行。"""
    state = _state(req)
    if not state["cfg"].auth.enabled:
        return True
    secret = state.get("preview_secret")
    if secret:
        from .preview_sign import verify_preview_token
        if verify_preview_token(secret, asset_id, req.query_params.get("preview")):
            return True
    auth = req.headers.get("authorization")
    token = get_bearer_token(auth)
    return bool(token and state["store"].verify(token))


def _require_photo_access(req: Request, asset_id: int) -> None:
    if not _preview_allowed(req, asset_id):
        raise AuthError("AUTH_REQUIRED", "authentication required", 401)


def _db(req: Request) -> "sqlite3.Connection":
    path = _state(req).get("db_path")
    if not path:
        raise PgApiError("DB_UNAVAILABLE", "database not configured", 503)
    conn = st_db_connect(path)
    return conn


# 延迟 import db.connect 以避免顶层循环
from .db import connect as st_db_connect  # noqa: E402


def _device_from_cfg(state, device_id: str):
    for d in state["cfg"].devices:
        if d.id == device_id:
            return d
    return None


@router.post("/upload-sessions", status_code=201)
def create_session(body: CreateSessionBody, req: Request, authorization: str = Header(default=None)):
    _guard(req)
    state = _state(req)
    dev = _device_from_cfg(state, body.source_device)
    if dev is None:
        raise PgApiError("DEVICE_NOT_FOUND", "device not found", 404)
    if not dev.enabled:
        raise PgApiError("DEVICE_DISABLED", "device is disabled", 403)
    conn = _db(req)
    try:
        st.sync_devices_from_config(conn, state["cfg"].devices)
        ip = req.client.host if req.client else None
        sid = st.create_upload_session(conn, body.source_device, ip)
        row = st.get_session(conn, sid)
        lg = _upload_logger(req)
        if lg is not None:
            lg.info("upload session created",
                    event="session_created", session_id=sid,
                    source_device=body.source_device)
        return _session_payload(row)
    finally:
        conn.close()


@router.get("/upload-sessions/recent")
def recent_sessions(req: Request, authorization: str = Header(default=None), limit: int = 8):
    """最近上传会话的只读摘要；不公开客户端地址或服务器路径。"""
    _guard(req)
    if limit < 1 or limit > 50:
        raise PgApiError("BAD_PARAM", "limit 1..50", 400)
    conn = _db(req)
    try:
        rows = conn.execute(
            "SELECT id, source_device, status, created_at, started_at, completed_at,"
            " total_files, uploaded_files, skipped_files, duplicate_files, failed_files"
            " FROM upload_sessions ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"success": True, "data": {
            "limit": limit, "sessions": [dict(row) for row in rows],
        }}
    finally:
        conn.close()


@router.get("/upload-sessions/{session_id}")
def get_session(session_id: str, req: Request, authorization: str = Header(default=None)):
    _guard(req)
    conn = _db(req)
    try:
        row = st.get_session(conn, session_id)
        if row is None:
            raise PgApiError("SESSION_NOT_FOUND", "session not found", 404)
        return _session_payload(row)
    finally:
        conn.close()


@router.post("/upload-sessions/{session_id}/items/precheck")
def precheck_batch(session_id: str, body: BulkPrecheckBody, req: Request,
                   authorization: str = Header(default=None)):
    _guard(req)
    conn = _db(req)
    try:
        session = st.get_session(conn, session_id)
        if session is None:
            raise PgApiError("SESSION_NOT_FOUND", "session not found", 404)
        device = session["source_device"]
        items = [i.model_dump() for i in body.items]
        for it in items:
            it["original_filename"] = it.pop("filename")
        results = st.bulk_precheck(conn, session_id, device, items)
        return {"success": True, "data": {"items": results}}
    finally:
        conn.close()


@router.get("/upload-sessions/{session_id}/deleted-reviews")
def deleted_reviews(session_id: str, req: Request):
    """返回内容 SHA 命中用户删除资产而尚待人工决策的上传项。"""
    _guard(req)
    conn = _db(req)
    try:
        if st.get_session(conn, session_id) is None:
            raise PgApiError("SESSION_NOT_FOUND", "session not found", 404)
        rows = conn.execute(
            "SELECT ui.id AS item_id, ui.original_filename, ui.asset_id,"
            " pa.lifecycle_state,ui.source_sha256,pa.sha256 AS archived_sha256 FROM upload_items ui"
            " JOIN photo_assets pa ON pa.id=ui.asset_id"
            " WHERE ui.session_id=? AND ui.status='REVIEW_REQUIRED'"
            " ORDER BY ui.id", (session_id,),
        ).fetchall()
        items = [{
            "item_id": r["item_id"], "original_filename": r["original_filename"],
            "asset_id": r["asset_id"], "lifecycle_state": r["lifecycle_state"],
            "restore_allowed": (r["lifecycle_state"] == "TRASHED"
                                or r["source_sha256"] is None
                                or r["source_sha256"] == r["archived_sha256"])
        } for r in rows]
        return {"success": True, "data": {"items": items}}
    finally:
        conn.close()


@router.get("/upload-items/{item_id}")
def get_item(item_id: int, req: Request, authorization: str = Header(default=None)):
    _guard(req)
    conn = _db(req)
    try:
        row = conn.execute(
            "SELECT id, session_id, source_device, client_item_id, original_filename, file_size,"
            " status, precheck_status, user_confirmation, asset_id, error_code, error_message"
            " FROM upload_items WHERE id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise PgApiError("ITEM_NOT_FOUND", "item not found", 404)
        return {"success": True, "data": {"item": _row_to_public(row)}}
    finally:
        conn.close()


@router.post("/upload-items/{item_id}/confirmation")
def confirm_item(item_id: int, body: ConfirmationBody, req: Request,
                 authorization: str = Header(default=None)):
    _guard(req)
    conf = body.confirmation
    if conf not in (1, 2):
        raise PgApiError("BAD_CONFIRMATION", "confirmation must be 1 or 2", 422)
    conn = _db(req)
    try:
        res = st.set_item_confirmation(conn, item_id, conf)
        if res is None:
            raise PgApiError("ITEM_NOT_CONFIRMABLE",
                             "item not found or not a pending possible duplicate", 409)
        return {"success": True, "data": res}
    finally:
        conn.close()


@router.post("/upload-items/{item_id}/skip")
def skip_unmatched_item(item_id: int, req: Request):
    """用户在 EXIF 检查阶段选择暂不上传，确保预检会话可以正确收尾。"""
    _guard(req)
    conn = _db(req)
    try:
        res = st.skip_unmatched_item(conn, item_id)
        if res is None:
            raise PgApiError("ITEM_NOT_SKIPPABLE", "item is not a pending upload", 409)
        return {"success": True, "data": res}
    finally:
        conn.close()


@router.post("/upload-items/{item_id}/file")
async def upload_item_file(item_id: int, req: Request, file: UploadFile = File(...),
                           modified_ms: int | None = Form(default=None),
                           exif_time: str | None = Form(default=None),
                           authorization: str = Header(default=None)):
    _guard(req)
    conn = _db(req)
    try:
        row = conn.execute(
            "SELECT id, session_id, source_device, original_filename, file_size, status,"
            " precheck_status, user_confirmation FROM upload_items WHERE id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise PgApiError("ITEM_NOT_FOUND", "item not found", 404)
        cfg = _state(req)["cfg"]
        # §30 允许性（NO_MATCH 或 POSSIBLE_DUPLICATE+confirmation=1）
        from .upload import extension_supported, save_item_file
        _upload_gate(cfg, row)
        if not extension_supported(cfg, row["original_filename"]):
            raise PgApiError("UNSUPPORTED_FILE_TYPE", "file type not allowed", 415)
        # EXIF 写入决策按单个 item 持久化，Worker 必须在重复内容检查后执行。
        # 这里先验证请求值，拒绝非法日期、非 JPEG 格式，避免未授权修改。
        if exif_time is not None:
            from .exif_time import validate_confirmed_time, WRITABLE_EXTENSIONS
            try:
                validate_confirmed_time(exif_time)
            except ValueError as exc:
                raise PgApiError("INVALID_EXIF_TIME", str(exc), 422) from exc
            if Path(row["original_filename"]).suffix.lower() not in WRITABLE_EXTENSIONS:
                raise PgApiError("UNSUPPORTED_EXIF_WRITE", "EXIF 时间补全暂只支持 JPEG/JPG", 422)
        # 落盘到 incoming/(§31 .tmp→rename; 无断点续传 §33)
        from .reliability import StorageState
        target_dir = cfg.storage.resolved_incoming()
        target_dir.mkdir(parents=True, exist_ok=True)
        if not StorageState(target_dir, cfg.disk.warning_percent,
                            cfg.disk.critical_percent, cfg.disk.stop_percent).accepts_uploads():
            raise PgApiError("STORAGE_ERROR", "insufficient storage", 507)
        from .upload_claim import claim, private_stage_path, publish, release_failed
        # BEGIN IMMEDIATE 中检查状态与抢占 token；只有获得权利才写入文件。
        # 用户确认重复时 UPLOADING 表示意图，此处会正确接纳。
        _claimed_row, token = claim(conn, item_id)
        stage = private_stage_path(target_dir, row["original_filename"], item_id, token)
        lg = _upload_logger(req)
        try:
            if lg is not None:
                lg.info("upload started", event="upload_started", session_id=row["session_id"],
                        photo_id=item_id, source_device=row["source_device"],
                        filename=row["original_filename"])
            await save_item_file(target_dir, row["original_filename"], file,
                                 expected_size=int(row["file_size"]),
                                 stop_percent=cfg.disk.stop_percent,
                                 client_mtime_ms=modified_ms,
                                 private_tmp=stage)
            # 发布权与状态转换同步校验；非法的过期写入者不能发布到 incoming。
            session_id = publish(conn, item_id, token, stage,
                                 target_dir / row["original_filename"], exif_time)
        except BaseException as exc:
            # 包括 ASGI 取消。失败请求的独立暂存不会触及已成功发布的字节。
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                _FALLBACK_LOG.exception("upload staging cleanup failed item_id=%s", item_id)
            try:
                if release_failed(conn, item_id, token, str(exc)):
                    st.recount_uploads(conn, row["session_id"])
                    st.finalize_session(conn, row["session_id"])
            except Exception:
                _FALLBACK_LOG.exception("upload claim cleanup failed item_id=%s", item_id)
            if lg is not None:
                lg.error("upload failed", event="upload_failed", session_id=row["session_id"],
                         photo_id=item_id, source_device=row["source_device"],
                         filename=row["original_filename"], error=str(exc))
            raise
        if lg is not None:
            lg.info("upload completed", event="upload_completed", session_id=session_id,
                    photo_id=item_id, source_device=row["source_device"],
                    filename=row["original_filename"])
        st.recount_uploads(conn, session_id)
        st.finalize_session(conn, session_id)
        return {"success": True, "data": {"item_id": item_id, "status": "UPLOADED"}}
    finally:
        conn.close()


def _upload_gate(cfg, row) -> None:
    from .upload import gate_upload_allowed
    gate_upload_allowed(row)


def _session_payload(row) -> dict:
    return {"success": True, "data": {"session": {
        "id": row["id"], "source_device": row["source_device"],
        "status": row["status"], "created_at": row["created_at"],
        "started_at": row["started_at"], "completed_at": row["completed_at"],
        "total_files": row["total_files"], "uploaded_files": row["uploaded_files"],
        "skipped_files": row["skipped_files"], "duplicate_files": row["duplicate_files"],
        "failed_files": row["failed_files"],
    }}}


def _row_to_public(row) -> dict:
    return {
        "id": row["id"], "session_id": row["session_id"],
        "source_device": row["source_device"],
        "original_filename": row["original_filename"], "file_size": row["file_size"],
        "status": row["status"], "precheck_status": row["precheck_status"],
        "user_confirmation": row["user_confirmation"], "asset_id": row["asset_id"],
        "error_code": row["error_code"], "error_message": row["error_message"],
    }


# ---------------------------------------------------------------------------
# 只读：照片列表 / 照片文件 / 系统概况（供 WebUI 照片页·系统页）
# ---------------------------------------------------------------------------
@router.get("/photos")
def photo_list(req: Request, authorization: str = Header(default=None),
               offset: int = 0, limit: int = 0,
               date_from: str = "", date_to: str = "",
               field: str = "", order: str = "desc",
               source_device: str = "", mime_type: str = ""):
    """照片列表（分页 + 时间范围过滤 + 维度/方向）。

    limit=0 → 使用配置 webui.photos_page_size(默认12)；offset 分页。
    field：taken=按拍摄时间(date_taken)；uploaded=按上传/入库时间(created_at)。
      缺省=旧行为（date_from/date_to 过滤 date_taken，ORDER BY id DESC）。
      date_from/date_to（date_to 不含）作用于所选 field 对应的时间列。
    order：desc/asc，作用于所选维度排序（同值再按 id 定序），默认 desc。
    """
    _guard(req)
    cfg = _state(req)["cfg"]
    if limit == 0:
        limit = cfg.webui.photos_page_size
    if limit < 1 or limit > 200 or offset < 0:
        raise PgApiError("BAD_PARAM", "limit 1..200 & offset>=0", 400)
    if order not in ("asc", "desc"):
        raise PgApiError("BAD_PARAM", "order 只能为 asc/desc", 400)
    if field not in ("", "taken", "uploaded"):
        raise PgApiError("BAD_PARAM", "field 只能为 taken/uploaded", 400)
    # 时间列与排序键：按维度决定（缺省 field=''=旧语义：date_taken 过滤、id 倒序）
    fcol = "date_taken" if field == "taken" else ("created_at" if field == "uploaded" else "date_taken")
    key = "date_taken" if field == "taken" else ("created_at" if field == "uploaded" else "id")
    conn = _db(req)
    try:
        where = ["file_missing_at IS NULL", "lifecycle_state = 'ACTIVE'"]   # 本地已删(Cleanup 等)资产不展示（V1.3）
        args: list = []
        if date_from:
            where.append(f"{fcol} >= ?")
            args.append(date_from)
        if date_to:
            where.append(f"{fcol} < ?")
            args.append(date_to)
        if source_device:
            where.append("EXISTS (SELECT 1 FROM upload_items ui WHERE ui.asset_id=photo_assets.id"
                         " AND ui.source_device=?)")
            args.append(source_device)
        if mime_type:
            where.append("mime_type=?")
            args.append(mime_type)
        cond = " WHERE " + " AND ".join(where)
        d = "DESC" if order == "desc" else "ASC"
        rows = conn.execute(
            "SELECT id, current_filename, date_taken, date_source, file_size, mime_type,"
            " archive_path FROM photo_assets" + cond +
            f" ORDER BY {key} {d}, id {d} LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) c FROM photo_assets" + cond, args
        ).fetchone()["c"]
        secret = _state(req).get("preview_secret")
        photos = []
        for r in rows:
            d = dict(r)
            if secret:
                from .preview_sign import make_preview_token
                d["thumbnail_url"] = (f"/api/v1/photos/{r['id']}/thumbnail"
                                      f"?preview={make_preview_token(secret, r['id'])}")
                d["preview_url"] = (f"/api/v1/photos/{r['id']}/preview"
                                    f"?preview={make_preview_token(secret, r['id'])}")
                d["file_url"] = (f"/api/v1/photos/{r['id']}/file"
                                 f"?preview={make_preview_token(secret, r['id'])}")
            photos.append(d)
        return {"success": True, "data": {
            "total": total, "offset": offset, "limit": limit,
            "photos": photos,
        }}
    finally:
        conn.close()


@router.get("/photos/{asset_id}")
def photo_detail(asset_id: int, req: Request, authorization: str = Header(default=None)):
    """返回单张照片详情及最近关联的来源设备。"""
    _guard(req)
    conn = _db(req)
    try:
        row = conn.execute(
            "SELECT id, sha256, original_filename, current_filename, file_size, mime_type,"
            " date_taken, date_source, exif_json, archive_path, created_at, updated_at,"
            " (SELECT ui.source_device FROM upload_items ui WHERE ui.asset_id=photo_assets.id"
            " ORDER BY COALESCE(ui.completed_at, ui.created_at) DESC, ui.id DESC LIMIT 1)"
            " AS source_device"
            " FROM photo_assets WHERE id=? AND lifecycle_state='ACTIVE'",
            (asset_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PgApiError("ASSET_NOT_FOUND", "photo asset not found", 404)
    photo = dict(row)
    raw_exif = photo.pop("exif_json")
    if raw_exif:
        import json
        try:
            photo["exif"] = json.loads(raw_exif)
        except json.JSONDecodeError as exc:
            _FALLBACK_LOG.warning("invalid EXIF JSON for photo detail",
                                  extra={"event": "photo_exif_invalid", "asset_id": asset_id,
                                         "error": str(exc)})
            photo["exif"] = None
    else:
        photo["exif"] = None
    return {"success": True, "data": {"photo": photo}}


@router.get("/photos/{asset_id}/file")
def photo_file(asset_id: int, req: Request, authorization: str = Header(default=None),
               inline: bool = False):
    """原图文件。默认附件下载；`?inline=1` 时浏览器内联预览（无 content-disposition attachment）。"""
    _require_photo_access(req, asset_id)
    conn = _db(req)
    try:
        row = conn.execute(
            "SELECT archive_path, current_filename, mime_type FROM photo_assets WHERE id=? AND lifecycle_state='ACTIVE'",
            (asset_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PgApiError("ASSET_NOT_FOUND", "photo asset not found", 404)
    cfg = _state(req)["cfg"]
    full = _resolve_archive(cfg, row["archive_path"])
    if not full.exists():
        _drop_orphan_thumbnail(cfg, asset_id)   # 原图缺失 → 清理孤儿缓存缩略图
        _mark_asset_missing(req, asset_id)      # 并落库标记（列表/UI 不再展示）
        raise PgApiError("FILE_MISSING", "photo file missing", 404)
    from fastapi.responses import FileResponse
    media = row["mime_type"] or guess_mime_type(row["current_filename"])
    # inline=1：无 filename → 无 content-disposition:attachment，浏览器内联预览
    return FileResponse(full, media_type=media,
                        filename=None if inline else row["current_filename"])


@router.get("/photos/{asset_id}/thumbnail")
def photo_thumbnail(asset_id: int, req: Request, authorization: str = Header(default=None)):
    """动态缩略图(webui-spec §52)：生成失败时回退原图。"""
    _require_photo_access(req, asset_id)
    conn = _db(req)
    try:
        row = conn.execute(
            "SELECT archive_path, current_filename, mime_type FROM photo_assets WHERE id=? AND lifecycle_state='ACTIVE'",
            (asset_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PgApiError("ASSET_NOT_FOUND", "photo asset not found", 404)
    cfg = _state(req)["cfg"]
    full = _resolve_archive(cfg, row["archive_path"])
    if not full.exists():
        _drop_orphan_thumbnail(cfg, asset_id)   # 原图缺失 → 清理孤儿缓存缩略图
        _mark_asset_missing(req, asset_id)      # 并落库标记（列表/UI 不再展示）
        raise PgApiError("FILE_MISSING", "photo file missing", 404)
    from fastapi.responses import FileResponse
    from .thumbnails import ensure_thumbnail
    cache_dir = cfg.resolved_thumbnails()
    thumb = ensure_thumbnail(cache_dir, asset_id, full)
    if thumb is not None:
        return FileResponse(thumb, media_type="image/jpeg",
                            filename=f"{row['current_filename']}.thumb.jpg")
    media = row["mime_type"] or guess_mime_type(row["current_filename"])
    return FileResponse(full, media_type=media, filename=row["current_filename"])


@router.get("/photos/{asset_id}/preview")
def photo_preview(asset_id: int, req: Request, authorization: str = Header(default=None)):
    """浏览器兼容的大图 JPEG 预览；转换失败时回退原文件。"""
    _require_photo_access(req, asset_id)
    conn = _db(req)
    try:
        row = conn.execute(
            "SELECT archive_path, current_filename, mime_type FROM photo_assets WHERE id=? AND lifecycle_state='ACTIVE'",
            (asset_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PgApiError("ASSET_NOT_FOUND", "photo asset not found", 404)
    cfg = _state(req)["cfg"]
    full = _resolve_archive(cfg, row["archive_path"])
    if not full.exists():
        _drop_orphan_thumbnail(cfg, asset_id)
        _mark_asset_missing(req, asset_id)
        raise PgApiError("FILE_MISSING", "photo file missing", 404)
    from fastapi.responses import FileResponse
    from .thumbnails import ensure_preview
    preview = ensure_preview(cfg.resolved_thumbnails(), asset_id, full)
    if preview is not None:
        return FileResponse(preview, media_type="image/jpeg")
    media = row["mime_type"] or guess_mime_type(row["current_filename"])
    return FileResponse(full, media_type=media)


@router.get("/system/status")
def system_status(req: Request, authorization: str = Header(default=None)):
    _guard(req)
    cfg = _state(req)["cfg"]
    try:
        root = cfg.storage.resolved_archive()
    except Exception as exc:
        _FALLBACK_LOG.error("failed to resolve storage status root: %s", exc, exc_info=True)
        root = None
    storage = None
    if root is not None and root.exists():
        from .reliability import StorageState
        storage = StorageState(root, cfg.disk.warning_percent,
                               cfg.disk.critical_percent, cfg.disk.stop_percent).report()
    conn = None
    counts = {}
    try:
        conn = _db(req)
        counts = {
            "pending_upload": conn.execute(
                "SELECT COUNT(*) FROM upload_items WHERE status='UPLOADED'").fetchone()[0],
            "processing": conn.execute(
                "SELECT COUNT(*) FROM upload_items WHERE status='PROCESSING'").fetchone()[0],
            "assets": conn.execute("SELECT COUNT(*) FROM photo_assets").fetchone()[0],
            "assets_missing": conn.execute(
                "SELECT COUNT(*) FROM photo_assets WHERE file_missing_at IS NOT NULL").fetchone()[0],
            "sessions_total": conn.execute(
                "SELECT COUNT(*) FROM upload_sessions").fetchone()[0],
        }
    except PgApiError:
        counts = {}
    finally:
        if conn:
            conn.close()
    workers = _state(req).get("workers")
    worker_status = (
        workers.status() if workers is not None
        else {"status": "not_started", "mode": "threaded-v1-v2", "threads": {}}
    )
    return {"success": True, "data": {
        "storage": storage, "counts": counts,
        "worker": worker_status,
        "allowed_extensions": list(cfg.upload.allowed_extensions),
    }}


def _resolve_archive(cfg, rel_path):
    """archive_path 形如 Photos/YYYY/YYYYMM/f 保存时相对 storage.root；据此定位绝对文件。"""
    from pathlib import Path
    p = Path(rel_path).expanduser()
    if p.is_absolute():
        return p
    return Path(cfg.storage.root).expanduser() / rel_path


def _drop_orphan_thumbnail(cfg, asset_id: int) -> None:
    """原图文件缺失时清理其缓存缩略图（缓存须随原图删除而删除，防孤儿残留）。

    清理失败不影响 404 语义（缩略图为非正式库，后续可被清理任务/重建覆盖）。
    """
    try:
        from .thumbnails import drop_thumbnail
        drop_thumbnail(cfg.resolved_thumbnails(), asset_id)
    except Exception as exc:
        _FALLBACK_LOG.warning("failed to remove orphan thumbnail asset_id=%s: %s",
                              asset_id, exc, exc_info=True)


def _mark_asset_missing(req, asset_id: int) -> None:
    """资产本地文件缺失时落库标记 file_missing_at（V1.3 soft-missing）。

    照片页/列表据此隐藏该资产；photo_assets 记录保留（sha256 去重身份）。
    失败不影响 404 语义。
    """
    try:
        from datetime import datetime, timedelta, timezone as _tz
        now = datetime.now(_tz(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        conn = _db(req)
        try:
            conn.execute(
                "UPDATE photo_assets SET file_missing_at=?, updated_at=?"
                " WHERE id=? AND file_missing_at IS NULL AND lifecycle_state='ACTIVE'",
                (now, now, asset_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _FALLBACK_LOG.error("failed to mark missing asset asset_id=%s: %s",
                            asset_id, exc, exc_info=True)


def guess_mime_type(name: str) -> str:
    from mimetypes import guess_type
    return guess_type(name)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------
# V2 只读：sync 状态与月份
# ---------------------------------------------------------------------------
@router.get("/sync/status")
def sync_status(req: Request, authorization: str = Header(default=None)):
    _guard(req)
    cfg = _state(req)["cfg"]
    sync = cfg.sync
    if not sync or not sync.enabled:
        return {"success": True, "data": {"enabled": False, "months": []}}
    conn = None
    months = []
    try:
        conn = _db(req)
        rows = conn.execute(
            "SELECT archive_month, state, consecutive_failed_count, abnormal_at"
            " FROM photo_sync_month_states WHERE id IN"
            " (SELECT MAX(id) FROM photo_sync_month_states GROUP BY archive_month)"
            " ORDER BY archive_month").fetchall()
        months = [dict(r) for r in rows]
    except PgApiError:
        months = []
    finally:
        if conn:
            conn.close()
    return {"success": True, "data": {
        "enabled": True,
        "failure_threshold": sync.failure.threshold,
        "retention_days": sync.cleanup.synced_retention_days,
        "interval_seconds": sync.worker.cycle_interval_seconds,
        "months": months,
    }}


@router.get("/sync/gates")
def sync_gates(req: Request, authorization: str = Header(default=None)):
    """同步门控实时检查：NAS Alive / V1 Idle / NAS Ready + 检查时间戳。

    供「照片同步状态」页展示系统当前处于哪个前置条件（每轮 V2 cycle 都依次经过
    NAS Alive→V1 Idle→NAS Ready→Difference）；manual 触发（页面加载/刷新时），
    因此含 1-2 次 SSH 探测。
    """
    _guard(req)
    cfg = _state(req)["cfg"]
    sync = cfg.sync
    if not sync or not sync.enabled:
        return {"success": True, "data": {"enabled": False}}
    from datetime import datetime, timedelta, timezone as _tz
    checked = datetime.now(_tz(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    archive = cfg.resolved_sync_source()
    conn = _db(req)
    try:
        from . import v2 as dv
        from .v2runner import v1_idle
        idle = bool(v1_idle(conn))
        months_total = len(dv.month_dirs(archive))
    finally:
        conn.close()
    alive = ready = False
    try:
        from .v2rsync_transport import RsyncNas
        nas = RsyncNas(sync, archive)
        alive = bool(nas.alive())
        ready = bool(nas.ready()) if alive else False
    except Exception as exc:
        _FALLBACK_LOG.error("sync gate probe failed: %s", exc, exc_info=True)
        alive = ready = False
    return {"success": True, "data": {
        "enabled": True, "checked_at": checked,
        "nas_alive": alive, "nas_ready": ready, "v1_idle": idle,
        "months_total": months_total,
        "all_ok": bool(alive and ready and idle),
    }}


@router.get("/sync/overview")
def sync_overview(req: Request, authorization: str = Header(default=None)):
    """V2 概览(v2-spec §43.2-43.5): 各月 current state/最新 task/最新成功后 cleanup eligibility; 含最近 Batch。"""
    _guard(req)
    cfg = _state(req)["cfg"]
    sync = cfg.sync
    if not sync or not sync.enabled:
        return {"success": True, "data": {"enabled": False, "months": [], "batches": []}}
    conn = _db(req)
    try:
        from . import v2 as dv
        from .v2cycle import cleanup_candidates
        archive = cfg.resolved_sync_source()
        cands = cleanup_candidates(conn, archive, retention_days=sync.cleanup.synced_retention_days)
        elig = {c["month"]: c for c in cands}
        months = []
        # month_dirs 返回 (rel 'YYYY/YYYYMM', YYYYMM)；DB/State/Task 均以 YYYYMM 为键（v2-spec §43）
        for rel, month in dv.month_dirs(archive):
            latest = conn.execute(
                "SELECT status, completed_at, exit_code FROM photo_sync_tasks"
                " WHERE archive_month=? ORDER BY id DESC LIMIT 1", (month,)).fetchone()
            latest_success = conn.execute(
                "SELECT completed_at FROM photo_sync_tasks WHERE archive_month=? AND status='SUCCESS'"
                " ORDER BY id DESC LIMIT 1", (month,)).fetchone()
            state_row = conn.execute(
                "SELECT state, consecutive_failed_count, abnormal_at"
                " FROM photo_sync_month_states WHERE archive_month=? ORDER BY id DESC LIMIT 1",
                (month,)).fetchone()
            latest_failed = conn.execute(
                "SELECT exit_code, error_message FROM photo_sync_tasks WHERE archive_month=?"
                " AND status='FAILED' ORDER BY id DESC LIMIT 1", (month,)).fetchone()
            abnormal = conn.execute(
                "SELECT reason, abnormal_at FROM photo_sync_month_states WHERE archive_month=?"
                " AND state='ABNORMAL' ORDER BY id DESC LIMIT 1", (month,)).fetchone()
            can = elig.get(month) or {}
            history = [dict(r) for r in conn.execute(
                "SELECT state, consecutive_failed_count, retry_requested_by, reason, created_at"
                " FROM photo_sync_month_states WHERE archive_month=? ORDER BY id", (month,)).fetchall()]
            months.append({
                "month": month,
                "current_state": state_row and state_row["state"],
                "consecutive_failed": (state_row and state_row["consecutive_failed_count"]) or 0,
                "abnormal_at": state_row and state_row["abnormal_at"],
                "latest_task_status": latest and latest["status"],
                "latest_task_exit": latest and latest["exit_code"],
                "latest_success_at": latest_success and latest_success["completed_at"],
                # ABNORMAL 原因（最近一条 ABNORMAL state 的 reason）
                "abnormal_reason": abnormal and abnormal["reason"],
                # 最近一次 FAILED task 的错误详情（供 WebUI 显示具体同步错误）
                "latest_error": (latest_failed and latest_failed["error_message"]
                                 or ""),
                "latest_error_exit": latest_failed and latest_failed["exit_code"],
                "cleanup_eligible": bool(can.get("remove")),
                "cleanup_reason": can.get("reason") or ("removable" if can.get("remove") else ""),
                "history": history,
            })
        batches = [dict(r) for r in conn.execute(
            "SELECT id,started_at,completed_at,status,task_count,success_count,failed_count"
            " FROM photo_sync_batches ORDER BY id DESC LIMIT 50").fetchall()]
        return {"success": True, "data": {
            "enabled": True,
            "threshold": sync.failure.threshold,
            "retention_days": sync.cleanup.synced_retention_days,
            "months": months,
            "batches": batches,
        }}
    finally:
        conn.close()


@router.post("/sync/months/{month}/retry")
def month_retry(month: str, req: Request, authorization: str = Header(default=None)):
    """人工 Retry：仅允许当前 ABNORMAL 的 YYYYMM→RETRY_REQUESTED(§24)。WebUI 才会按钮下发。"""
    _guard(req)
    from .v2 import valid_month
    from .v2cycle import current_state_name, retry_month
    if not valid_month(month):
        raise PgApiError("BAD_MONTH", "must be YYYYMM", 400)
    conn = _db(req)
    try:
        st = current_state_name(conn, month)
        if st != "ABNORMAL":
            raise PgApiError("RETRY_NOT_ALLOWED",
                             f"只有 ABNORMAL 月份可 Retry(当前={st})", 409)
        retry_month(conn, month, by="admin")
        conn.commit()   # append_month_state 不自动 commit：缺此步返回成功但未落盘（WebUI 反复点击无效）
        return {"success": True, "data": {"month": month, "state": "RETRY_REQUESTED"}}
    finally:
        conn.close()
