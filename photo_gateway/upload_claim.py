"""持久化的上传占用/发布权：以 SQLite 原子操作和私有暂存隔离请求。

RECEIVING 只表示有 token 的写入者；UPLOADING 只表示已确认的上传意图。
Worker 仅消费 UPLOADED，不能读取未发布的字节。
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

from .security import PgApiError
from .store import _now
from .upload import gate_upload_allowed

# 避免无进度的进程异常留下永久占用；请求令牌使过期请求无法提交旧字节。
CLAIM_TIMEOUT_SECONDS = 2 * 60 * 60


def claim(conn: sqlite3.Connection, item_id: int) -> tuple[sqlite3.Row, str]:
    token = secrets.token_hex(16)
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id,session_id,source_device,original_filename,file_size,status,"
            "precheck_status,user_confirmation,upload_token,upload_claimed_at "
            "FROM upload_items WHERE id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise PgApiError("ITEM_NOT_FOUND", "item not found", 404)
        gate_upload_allowed(row)
        # POSSIBLE_DUPLICATE + confirmation=1 的 UPLOADING 是有效的“待上传”状态。
        valid = (row["status"] in ("PRECHECK", "FAILED")
                 or (row["status"] == "UPLOADING"
                     and row["precheck_status"] == "POSSIBLE_DUPLICATE"
                     and row["user_confirmation"] == 1))
        stale = (row["status"] == "RECEIVING"
                 and row["upload_claimed_at"] is not None
                 and now - int(row["upload_claimed_at"]) > CLAIM_TIMEOUT_SECONDS)
        if not valid and not stale:
            raise PgApiError("ITEM_NOT_RETRYABLE", "item is already claimed or processed", 409)
        # 清理同名其他 item 已过期的“正在上传”令牌，禁止旧请求随后发布。
        # 必须在同一个 BEGIN IMMEDIATE 事务内撤销，单纯忽略过期状态
        # 会让老写入者在新 item 发布后覆盖其字节。
        conn.execute(
            "UPDATE upload_items SET status='FAILED', upload_token=NULL,"
            "upload_claimed_at=NULL,error_code='UPLOAD_CLAIM_EXPIRED',"
            "error_message='upload claim expired',updated_at=?"
            " WHERE original_filename=? AND id<>? AND status='RECEIVING'"
            " AND upload_claimed_at IS NOT NULL AND upload_claimed_at < ?",
            (_now(), row["original_filename"], item_id, now - CLAIM_TIMEOUT_SECONDS)
        )
        # 当前 incoming/<filename> 仍是 V1 共享的消费路径。跨 item 同名在
        # 已发布但未归档期间不得覆盖。后续可独立迁至每 item 私有 incoming。
        occupied = conn.execute(
            "SELECT id FROM upload_items WHERE original_filename=? AND id<>?"
            " AND status IN ('RECEIVING','UPLOADED','PROCESSING') LIMIT 1",
            (row["original_filename"], item_id)
        ).fetchone()
        if occupied:
            raise PgApiError("FILENAME_IN_USE",
                             "same filename is being uploaded or processed", 409)
        conn.execute(
            "UPDATE upload_items SET status='RECEIVING',upload_token=?,"
            "upload_claimed_at=?,updated_at=? WHERE id=?",
            (token, now, _now(), item_id)
        )
        conn.commit()
        return row, token
    except BaseException:
        conn.rollback()
        raise


def private_stage_path(incoming: Path, filename: str, item_id: int, token: str) -> Path:
    # 原名可能已经接近文件系统分量上限；staging 只用有界身份字段。
    return incoming / f".upload-{item_id}-{token}.tmp"


def publish(conn: sqlite3.Connection, item_id: int, token: str, stage: Path,
            final: Path, exif_time: str | None) -> str:
    """持有同一请求令牌才能将完整字节原子发布并打开 Worker 状态。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT session_id,status,upload_token FROM upload_items WHERE id=?",
            (item_id,)
        ).fetchone()
        if not row or row["status"] != "RECEIVING" or row["upload_token"] != token:
            raise PgApiError("UPLOAD_CLAIM_LOST",
                             "upload ownership expired or was replaced", 409)
        # 发布动作与 DB 更新在同一个受保护的临界区；Worker 只读取 UPLOADED。
        stage.replace(final)
        conn.execute(
            "UPDATE upload_items SET status='UPLOADED',exif_confirmed_time=?,"
            "source_sha256=NULL,error_code=NULL,error_message=NULL,"
            "completed_at=NULL,uploaded_at=?,"
            "processing_started_at=NULL,upload_token=NULL,upload_claimed_at=NULL,"
            "updated_at=? WHERE id=? AND status='RECEIVING' AND upload_token=?",
            (exif_time, _now(), _now(), item_id, token)
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise PgApiError("UPLOAD_CLAIM_LOST", "upload ownership changed", 409)
        conn.execute(
            "UPDATE upload_sessions SET status='UPLOADING',completed_at=NULL"
            " WHERE id=? AND status IN ('PARTIAL','FAILED')",
            (row["session_id"],)
        )
        conn.commit()
        return row["session_id"]
    except BaseException:
        conn.rollback()
        raise


def release_failed(conn: sqlite3.Connection, item_id: int, token: str,
                   message: str) -> bool:
    """失败只能释放自己的占用，不得回滚已由后来请求获取的令牌。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE upload_items SET status='FAILED',upload_token=NULL,"
            "upload_claimed_at=NULL,error_code='UPLOAD_FAILED',error_message=?,"
            "updated_at=? WHERE id=? AND status='RECEIVING' AND upload_token=?",
            (str(message)[:400], _now(), item_id, token)
        )
        conn.commit()
        return cur.rowcount == 1
    except BaseException:
        conn.rollback()
        raise
