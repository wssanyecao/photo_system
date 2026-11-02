"""本地照片回收站。所有修改必须在 BackgroundWorkers.archive_lock 下执行。

仅操作 storage.root 内的 Photos 与 recycle_bin；绝不访问 NAS。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .security import PgApiError

SH = timezone(timedelta(hours=8))


def now() -> str:
    return datetime.now(SH).strftime("%Y-%m-%d %H:%M:%S")


def _paths(cfg, asset):
    """数据库路径不可信：限制归档路径和实际文件类型，阻止跨目录/符号链接。"""
    root = Path(cfg.storage.root).expanduser().resolve()
    archive = cfg.storage.resolved_archive().resolve()
    rel = Path(asset["archive_path"] or "")
    if (rel.is_absolute() or len(rel.parts) != 4 or rel.parts[0] != "Photos"
            or not rel.parts[1].isdigit() or len(rel.parts[1]) != 4
            or not rel.parts[2].isdigit() or len(rel.parts[2]) != 6
            or rel.parts[2][:4] != rel.parts[1]
            or rel.name in ("", ".", "..") or any(x in (".", "..") for x in rel.parts)):
        raise PgApiError("UNSAFE_PATH", "invalid archive path", 409)
    src = (archive / Path(*rel.parts[1:]))
    recycle = cfg.resolved_recycle_bin()
    if recycle.is_symlink() or archive.is_symlink():
        raise PgApiError("UNSAFE_PATH", "unsafe storage symlink", 409)
    if not archive.is_relative_to(root) or recycle.resolve() == archive:
        raise PgApiError("UNSAFE_PATH", "storage roots are not isolated", 409)
    dst = recycle / str(int(asset["id"])) / rel.name
    for obj, allowed in ((src, archive), (dst, root)):
        if not obj.resolve().is_relative_to(allowed):
            raise PgApiError("UNSAFE_PATH", "path escaped storage root", 409)
        if obj.is_symlink() or obj.parent.is_symlink():
            raise PgApiError("UNSAFE_PATH", "symlinked photo path", 409)
    if not dst.resolve().is_relative_to(recycle.resolve()):
        raise PgApiError("UNSAFE_PATH", "recycle path escaped", 409)
    return src, dst


def _asset(conn, asset_id):
    row = conn.execute("SELECT * FROM photo_assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise PgApiError("ASSET_NOT_FOUND", "photo asset not found", 404)
    return row


def _event(conn, asset_id, action):
    conn.execute(
        "INSERT INTO photo_recycle_events(asset_id, action, created_at) VALUES (?,?,?)",
        (asset_id, action, now()),
    )


def trash(conn, cfg, asset_id):
    if not cfg.recycle_bin.enabled:
        raise PgApiError("RECYCLE_DISABLED", "recycle bin disabled", 409)
    row = _asset(conn, asset_id)
    if row["lifecycle_state"] != "ACTIVE" or row["file_missing_at"] is not None:
        raise PgApiError("NOT_ACTIVE", "photo is not available for deletion", 409)
    src, dst = _paths(cfg, row)
    if not src.is_file():
        raise PgApiError("FILE_MISSING", "original photo not present locally", 409)
    if dst.exists():
        raise PgApiError("TARGET_EXISTS", "recycle target already exists", 409)
    # 先提交 TRASHED，以防移动后重启被 rsync 扫描到活动状态。
    t = now()
    with conn:
        conn.execute("UPDATE photo_assets SET lifecycle_state='TRASHED', deleted_at=?,"
                     " purged_at=NULL, updated_at=? WHERE id=?", (t, t, asset_id))
        _event(conn, asset_id, "TRASHED")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise OSError("recycle target occupied")
        src.replace(dst)
    except OSError as exc:
        # 失败时本地照片仍存在，回滚状态；如实际移动成功，让重启对账接管。
        if src.exists() and not dst.exists():
            with conn:
                conn.execute("UPDATE photo_assets SET lifecycle_state='ACTIVE',"
                             " deleted_at=NULL, updated_at=? WHERE id=?", (now(), asset_id))
                _event(conn, asset_id, "TRASH_FAILED")
        raise PgApiError("TRASH_FAILED", str(exc), 409) from exc
    return {"asset_id": asset_id, "state": "TRASHED", "deleted_at": t}


def restore(conn, cfg, asset_id):
    row = _asset(conn, asset_id)
    if row["lifecycle_state"] != "TRASHED":
        raise PgApiError("NOT_TRASHED", "photo is not in recycle bin", 409)
    src, dst = _paths(cfg, row)
    if not dst.is_file():
        raise PgApiError("FILE_MISSING", "recycle file missing", 409)
    if src.exists():
        raise PgApiError("TARGET_EXISTS", "original path already occupied", 409)
    # ACTIVE 标记先落库；重启对账负责继续未完成的恢复移动。
    with conn:
        conn.execute("UPDATE photo_assets SET lifecycle_state='ACTIVE', deleted_at=NULL,"
                     " file_missing_at=NULL, updated_at=? WHERE id=?", (now(), asset_id))
        _event(conn, asset_id, "RESTORED")
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.replace(src)
    except OSError as exc:
        if dst.exists() and not src.exists():
            with conn:
                conn.execute("UPDATE photo_assets SET lifecycle_state='TRASHED',"
                             " deleted_at=?, updated_at=? WHERE id=?",
                             (row["deleted_at"], now(), asset_id))
                _event(conn, asset_id, "RESTORE_FAILED")
        raise PgApiError("RESTORE_FAILED", str(exc), 409) from exc
    try:
        dst.parent.rmdir()
    except OSError:
        pass
    return {"asset_id": asset_id, "state": "ACTIVE"}


def list_trash(conn, cfg, *, offset=0, limit=None, date_from="", date_to="",
               field="", order="desc", source_device="", mime_type=""):
    """与正式照片列表使用相同的过滤、排序和分页语义。

    taken: 拍摄时间；uploaded: 入库时间，均非删除时间。
    """
    if limit is None or limit == 0:
        limit = cfg.webui.photos_page_size
    if not 1 <= limit <= 200 or offset < 0:
        raise PgApiError("BAD_PARAM", "limit 1..200 & offset>=0", 400)
    if field not in ("", "taken", "uploaded") or order not in ("asc", "desc"):
        raise PgApiError("BAD_PARAM", "invalid field or order", 400)
    fcol = "created_at" if field == "uploaded" else "date_taken"
    sort_col = "id" if field == "" else fcol
    direction = "DESC" if order == "desc" else "ASC"
    where = ["lifecycle_state='TRASHED'"]
    args = []
    if date_from:
        where.append(f"{fcol} >= ?")
        args.append(date_from)
    if date_to:
        where.append(f"{fcol} < ?")
        args.append(date_to)
    if source_device:
        where.append("EXISTS (SELECT 1 FROM upload_items ui"
                     " WHERE ui.asset_id=photo_assets.id AND ui.source_device=?)")
        args.append(source_device)
    if mime_type:
        where.append("mime_type=?")
        args.append(mime_type)
    condition = " WHERE " + " AND ".join(where)
    total = conn.execute("SELECT COUNT(*) FROM photo_assets" + condition, args).fetchone()[0]
    rows = conn.execute(
        "SELECT id, current_filename, date_taken, date_source, created_at,"
        " file_size, mime_type, archive_path, deleted_at,"
        " (SELECT ui.source_device FROM upload_items ui WHERE ui.asset_id=photo_assets.id"
        " ORDER BY COALESCE(ui.completed_at,ui.created_at) DESC,ui.id DESC LIMIT 1)"
        " AS source_device"
        " FROM photo_assets" + condition +
        f" ORDER BY {sort_col} {direction}, id {direction} LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    result = [
        dict(row, expires_at=(datetime.strptime(row["deleted_at"], "%Y-%m-%d %H:%M:%S")
            + timedelta(days=cfg.recycle_bin.retention_days)).strftime("%Y-%m-%d %H:%M:%S"))
        for row in rows if row["deleted_at"]
    ]
    return result, total, limit



def purge_expired(conn, cfg, at=None):
    """删除到期回收实体文件；不修改 NAS、不删除身份。调用方持归档锁。"""
    t = at or datetime.now(SH)
    cutoff = (t - timedelta(days=cfg.recycle_bin.retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT * FROM photo_assets WHERE lifecycle_state='TRASHED' AND deleted_at<=?",
        (cutoff,),
    ).fetchall()
    count = 0
    for row in rows:
        _, dst = _paths(cfg, row)
        try:
            if dst.exists():
                dst.unlink()
            # 回收期结束时清理本地 JPEG 缩略图/预览缓存，避免仍保留已删内容。
            from .thumbnails import drop_thumbnail
            drop_thumbnail(cfg.resolved_thumbnails(), row["id"])
            with conn:
                conn.execute("UPDATE photo_assets SET lifecycle_state='PURGED', purged_at=?,"
                             " updated_at=? WHERE id=?", (now(), now(), row["id"]))
                _event(conn, row["id"], "PURGED")
            count += 1
        except OSError:
            continue
        try:
            dst.parent.rmdir()
        except OSError:
            pass
    return count


def reconcile(conn, cfg):
    """启动前修复本地文件移动的崩溃窗口，不误伤同路径不同 SHA 的新资产。"""
    from .worker import compute_sha256

    rows = conn.execute(
        "SELECT * FROM photo_assets WHERE lifecycle_state IN ('TRASHED','PURGED')"
        " OR id IN (SELECT asset_id FROM photo_recycle_events)"
    ).fetchall()
    for row in rows:
        src, dst = _paths(cfg, row)
        state = row["lifecycle_state"]
        src_exists, dst_exists = src.is_file(), dst.is_file()
        # 原文件名可能已被其他不同内容的新照片复用，不应把新照片移入旧回收站。
        src_is_old = src_exists and compute_sha256(src) == row["sha256"]

        if state == "ACTIVE" and dst_exists:
            if not src_exists:
                src.parent.mkdir(parents=True, exist_ok=True)
                dst.replace(src)  # 中断的恢复操作
            elif not src_is_old:
                # 旧照片仍在回收站，新照片占据原路径；旧记录必须保持 TRASHED。
                with conn:
                    conn.execute(
                        "UPDATE photo_assets SET lifecycle_state='TRASHED', deleted_at=?, updated_at=?"
                        " WHERE id=?", (row["deleted_at"] or now(), now(), row["id"])
                    )
                    _event(conn, row["id"], "RESTORE_CONFLICT_RECOVERED")
            else:
                raise RuntimeError(f"duplicate files during restore, asset={row['id']}")
        elif state == "TRASHED":
            if dst_exists:
                if src_is_old:
                    raise RuntimeError(f"trashed asset still in sync source, asset={row['id']}")
                continue  # src 不存在或由另一有效资产占用
            if src_is_old:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.replace(dst)  # 中断的删除操作
            else:
                # 两处均不再存在旧内容，原路径可能被新照片占据。
                with conn:
                    conn.execute(
                        "UPDATE photo_assets SET lifecycle_state='PURGED', purged_at=?, updated_at=?"
                        " WHERE id=?", (now(), now(), row["id"])
                    )
                    _event(conn, row["id"], "MISSING_RECOVERED")
        elif state == "PURGED":
            if src_is_old:
                raise RuntimeError(f"purged asset unexpectedly reappeared, asset={row['id']}")
            if dst_exists:
                dst.unlink()  # 中断的过期清理，不接触复用原文件名的新资产



def pending_path(cfg, item_id: int, filename: str) -> Path:
    """隔离待决策文件，防止同文件名的后续上传覆盖。"""
    rel = Path(filename)
    if rel.name != filename or filename in ("", ".", ".."):
        raise PgApiError("UNSAFE_PATH", "unsafe upload filename", 409)
    root = Path(cfg.storage.root).expanduser().resolve()
    stage_root = root / "review_pending"
    if stage_root.is_symlink() or not stage_root.resolve().is_relative_to(root):
        raise PgApiError("UNSAFE_PATH", "unsafe pending directory", 409)
    path = stage_root / str(int(item_id)) / filename
    if path.is_symlink() or path.parent.is_symlink():
        raise PgApiError("UNSAFE_PATH", "unsafe pending file", 409)
    if not path.resolve().is_relative_to(stage_root.resolve()):
        raise PgApiError("UNSAFE_PATH", "review path escaped", 409)
    return path


def stage_deleted_review(cfg, item_id: int, name: str, src: Path) -> None:
    dst = pending_path(cfg, item_id, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or not src.is_file():
        raise OSError("review target exists or original upload unavailable")
    src.replace(dst)


def expire_pending_reviews(conn, cfg, days=7, at=None) -> int:
    """待决策超时按失败收尾，不自动恢复已删除资产。"""
    t = at or datetime.now(SH)
    cutoff = (t - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    items = conn.execute(
        "SELECT id, session_id, original_filename FROM upload_items"
        " WHERE status='REVIEW_REQUIRED' AND updated_at<?", (cutoff,)
    ).fetchall()
    count = 0
    for row in items:
        path = pending_path(cfg, row["id"], row["original_filename"])
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        try:
            path.parent.rmdir()
        except OSError:
            pass
        with conn:
            conn.execute("UPDATE upload_items SET status='FAILED', error_code='REVIEW_EXPIRED',"
                         " error_message='deleted photo review expired', updated_at=? WHERE id=?",
                         (now(), row["id"]))
        from .store import finalize_session
        finalize_session(conn, row["session_id"])
        count += 1
    return count


def decide_deleted_upload(conn, cfg, item_id, choice):
    """内容哈希已经验证匹配；由 API 加同一把归档锁决定是否恢复。"""
    row = conn.execute(
        "SELECT ui.id, ui.session_id, ui.original_filename, ui.asset_id,"
        " ui.status,ui.source_sha256,pa.lifecycle_state FROM upload_items ui"
        " JOIN photo_assets pa ON pa.id=ui.asset_id WHERE ui.id=?", (item_id,)
    ).fetchone()
    if not row or row["status"] != "REVIEW_REQUIRED" or row["lifecycle_state"] not in ("TRASHED","PURGED"):
        raise PgApiError("NOT_REVIEWABLE", "no pending deleted photo decision", 409)
    incoming = pending_path(cfg, item_id, row["original_filename"])
    asset = _asset(conn, row["asset_id"])
    src, dst = _paths(cfg, asset)
    if choice not in ("restore", "keep_deleted"):
        raise PgApiError("BAD_CHOICE", "invalid decision", 422)
    if not incoming.is_file():
        raise PgApiError("FILE_MISSING", "pending upload file missing", 409)
    from .worker import compute_sha256
    incoming_sha = compute_sha256(incoming)
    bytes_match = incoming_sha == asset["sha256"]
    source_alias = (row["source_sha256"] == incoming_sha
                    and conn.execute(
                        "SELECT 1 FROM photo_source_hashes WHERE source_sha256=? AND asset_id=?",
                        (incoming_sha, row["asset_id"])
                    ).fetchone() is not None)
    if not bytes_match and not source_alias:
        raise PgApiError("HASH_MISMATCH", "pending upload content changed", 409)
    if choice == "restore" and row["lifecycle_state"] == "PURGED" and not bytes_match:
        # 归档字节已经被永久清理；raw 源文件与归档不同，禁止冒用旧 SHA 恢复。
        raise PgApiError("ORIGINAL_BYTES_UNAVAILABLE",
                         "metadata-edited archive unavailable; cannot safely restore", 409)
    if choice == "restore":
        if row["lifecycle_state"] == "TRASHED":
            restore(conn, cfg, row["asset_id"])
        else:
            if not incoming.is_file() or src.exists():
                raise PgApiError("FILE_MISSING", "incoming missing or original occupied", 409)
            src.parent.mkdir(parents=True, exist_ok=True)
            with conn:
                conn.execute("UPDATE photo_assets SET lifecycle_state='ACTIVE',"
                             " deleted_at=NULL, purged_at=NULL, file_missing_at=NULL,"
                             " updated_at=? WHERE id=?", (now(), row["asset_id"]))
                _event(conn, row["asset_id"], "REIMPORTED")
            try:
                incoming.replace(src)
            except OSError as exc:
                with conn:
                    conn.execute("UPDATE photo_assets SET lifecycle_state='PURGED',"
                                 " deleted_at=?, purged_at=?, updated_at=? WHERE id=?",
                                 (asset["deleted_at"], asset["purged_at"], now(), row["asset_id"]))
                    _event(conn, row["asset_id"], "REIMPORT_FAILED")
                raise PgApiError("RESTORE_FAILED", str(exc), 409) from exc
    if incoming.exists():
        incoming.unlink()
    try:
        incoming.parent.rmdir()
    except OSError:
        pass
    with conn:
        conn.execute("UPDATE upload_items SET status='DUPLICATE', completed_at=?, updated_at=?"
                     " WHERE id=?", (now(), now(), item_id))
        conn.execute("INSERT INTO photo_events(upload_item_id,event_type,message,created_at)"
                     " VALUES (?,?,?,?)",
                     (item_id, "DELETED_DECISION", choice, now()))
    from .store import finalize_session
    finalize_session(conn, row["session_id"])
    return {"item_id": item_id, "choice": choice, "asset_id": row["asset_id"]}
