"""回收站管理 API：用户授权、存储隔离与审计。"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
import json

from fastapi import APIRouter, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse

from . import recycle
from . import api_sessions as sessions
from .security import PgApiError

router = APIRouter(prefix="/api/v1", tags=["recycle-bin"])


def _lock(req):
    w = sessions._state(req).get("workers")
    return w.archive_lock if w is not None else nullcontext()


def _audit(req, action, asset_id):
    logman = sessions._state(req).get("logman")
    if logman:
        logman.audit().info(f"recycle action={action} asset_id={asset_id}",
                            event="recycle_action", module="recycle")


@router.post("/photos/{asset_id}/trash")
def trash_photo(asset_id: int, req: Request):
    sessions._guard(req)
    with _lock(req):
        conn = sessions._db(req)
        try:
            result = recycle.trash(conn, sessions._state(req)["cfg"], asset_id)
        finally:
            conn.close()
    _audit(req, "trash", asset_id)
    return {"success": True, "data": result}


@router.get("/recycle-bin")
def recycle_list(req: Request, offset: int = 0, limit: int = 0,
                 date_from: str = "", date_to: str = "",
                 field: str = "", order: str = "desc",
                 source_device: str = "", mime_type: str = ""):
    """回收站列表与正式照片列表维持一致的筛选、分页契约。"""
    sessions._guard(req)
    state = sessions._state(req)
    conn = sessions._db(req)
    try:
        rows, total, page_size = recycle.list_trash(
            conn, state["cfg"], offset=offset, limit=limit, date_from=date_from,
            date_to=date_to, field=field, order=order,
            source_device=source_device, mime_type=mime_type,
        )
    finally:
        conn.close()
    from .preview_sign import make_preview_token
    secret = state.get("preview_secret")
    for item in rows:
        query = f"?preview={make_preview_token(secret, item['id'])}" if secret else ""
        item["preview_url"] = f"/api/v1/recycle-bin/{item['id']}/preview{query}"
        item["thumbnail_url"] = f"/api/v1/recycle-bin/{item['id']}/thumbnail{query}"
        item["file_url"] = item["preview_url"]
        item["is_recycled"] = True
    return {"success": True, "data": {
        "photos": rows, "total": total, "offset": offset, "limit": page_size,
    }}


@router.get("/recycle-bin/{asset_id}")
def recycle_detail(asset_id: int, req: Request):
    sessions._guard(req)
    conn = sessions._db(req)
    try:
        row = recycle._asset(conn, asset_id)
        if row["lifecycle_state"] != "TRASHED":
            raise PgApiError("NOT_TRASHED", "photo not in recycle bin", 404)
        photo = dict(row)
        source = conn.execute(
            "SELECT source_device FROM upload_items WHERE asset_id=?"
            " ORDER BY COALESCE(completed_at,created_at) DESC,id DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        photo["source_device"] = source["source_device"] if source else None
    finally:
        conn.close()
    raw = photo.pop("exif_json", None)
    try:
        photo["exif"] = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        photo["exif"] = None
    photo["expires_at"] = (
        datetime.strptime(photo["deleted_at"], "%Y-%m-%d %H:%M:%S")
        + timedelta(days=sessions._state(req)["cfg"].recycle_bin.retention_days)
    ).strftime("%Y-%m-%d %H:%M:%S")
    photo["is_recycled"] = True
    return {"success": True, "data": {"photo": photo}}


@router.post("/recycle-bin/{asset_id}/restore")
def restore_photo(asset_id: int, req: Request):
    sessions._guard(req)
    with _lock(req):
        conn = sessions._db(req)
        try:
            result = recycle.restore(conn, sessions._state(req)["cfg"], asset_id)
        finally:
            conn.close()
    _audit(req, "restore", asset_id)
    return {"success": True, "data": result}


@router.get("/recycle-bin/{asset_id}/thumbnail")
def recycle_thumbnail(asset_id: int, req: Request):
    """沿用正式照片的缩略图缓存逻辑，避免网格载入整张高清原图。"""
    sessions._require_photo_access(req, asset_id)
    cfg = sessions._state(req)["cfg"]
    conn = sessions._db(req)
    try:
        asset = recycle._asset(conn, asset_id)
        if asset["lifecycle_state"] != "TRASHED":
            raise PgApiError("NOT_TRASHED", "photo not in recycle bin", 404)
        _, file = recycle._paths(cfg, asset)
    finally:
        conn.close()
    if not file.is_file():
        raise PgApiError("FILE_MISSING", "recycle file missing", 404)
    from .thumbnails import ensure_thumbnail
    thumbnail = ensure_thumbnail(cfg.resolved_thumbnails(), asset_id, file)
    if thumbnail is not None:
        return FileResponse(thumbnail, media_type="image/jpeg")
    return FileResponse(file, media_type=asset["mime_type"] or "application/octet-stream")


@router.get("/recycle-bin/{asset_id}/preview")
def recycle_preview(asset_id: int, req: Request):
    sessions._require_photo_access(req, asset_id)
    cfg = sessions._state(req)["cfg"]
    conn = sessions._db(req)
    try:
        asset = recycle._asset(conn, asset_id)
        if asset["lifecycle_state"] != "TRASHED":
            raise PgApiError("NOT_TRASHED", "photo not in recycle bin", 404)
        _, file = recycle._paths(cfg, asset)
    finally:
        conn.close()
    if not file.is_file():
        raise PgApiError("FILE_MISSING", "recycle file missing", 404)
    from .thumbnails import ensure_preview
    if str(asset["current_filename"]).lower().endswith((".heic", ".heif")):
        jpeg = ensure_preview(cfg.resolved_thumbnails(), asset_id, file)
        if jpeg is not None:
            return FileResponse(jpeg, media_type="image/jpeg")
    return FileResponse(file, media_type=asset["mime_type"] or "application/octet-stream")


class DeletedDecision(BaseModel):
    choice: str


@router.post("/upload-items/{item_id}/deleted-decision")
def deleted_decision(item_id: int, body: DeletedDecision, req: Request):
    sessions._guard(req)
    with _lock(req):
        conn = sessions._db(req)
        try:
            result = recycle.decide_deleted_upload(
                conn, sessions._state(req)["cfg"], item_id, body.choice
            )
        finally:
            conn.close()
    _audit(req, body.choice, result["asset_id"])
    return {"success": True, "data": result}
