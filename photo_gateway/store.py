"""V1 SQLite 仓储/服务函数（Phase2/3：上传会话与 Precheck）。

对齐 db-spec：
- 设备同步 §13（配置名/db）；Devices 来自配置，id → DB。
- Session 状态集(§20)：CREATED/UPLOADING/UPLOADED/PROCESSING/COMPLETED/PARTIAL/FAILED/CANCELLED
- upload_item.status(§26)：PRECHECK/UPLOADING/UPLOADED/PROCESSING/COMPLETED/DUPLICATE/FAILED
- precheck_status(§29)：NOT_CHECKED/NO_MATCH/POSSIBLE_DUPLICATE
- 批量 Precheck 幂等 (session_id, client_item_id)（§20/22 API + §22 db 唯一键）
- Precheck 命中 = 曾出现相同 (device,filename,size) 且结果 COMPLETED/DUPLICATE（§33/§34）→ 不命中 FAILED
- 文件在 Precheck 阶段仅存元数据，不进 R5S（§28）；只有命中用户确认(→UPLOADING)或 NO_MATCH 后进入上传。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

SH = timezone(timedelta(hours=8))

SESSION_STATUSES = {
    "CREATED", "UPLOADING", "UPLOADED", "PROCESSING",
    "COMPLETED", "PARTIAL", "FAILED", "CANCELLED",
}
ITEM_STATUSES = {
    "PRECHECK", "UPLOADING", "UPLOADED", "PROCESSING",
    "COMPLETED", "DUPLICATE", "FAILED",
}
PRECHECK_STATUSES = {"NOT_CHECKED", "NO_MATCH", "POSSIBLE_DUPLICATE"}
# 命中过去"成功/重复"，不命中 FAILED
HIT_STATUSES = ("COMPLETED", "DUPLICATE")


def _now(t: datetime | None = None) -> str:
    return (t or datetime.now(SH)).strftime("%Y-%m-%d %H:%M:%S")


def new_session_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# devices 配置→DB 同步（db-spec §13：Devices 权威来自 config）
# ---------------------------------------------------------------------------
def sync_devices_from_config(conn, devices) -> None:
    """把 config.devices 同步进 devices 表。

    规则（db-spec §13）：
    - config 存 → 插入或更新 name/enabled
    - config 删(历史遗留) → 禁用(enabled=0)保留历史
    """
    now = _now()
    from_config = {d.id: d for d in devices}
    cur = conn.execute("SELECT id FROM devices").fetchall()
    existing = {r[0] for r in cur}
    for did, d in from_config.items():
        conn.execute(
            "INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,"
            " updated_at=excluded.updated_at",
            (did, d.name, 1 if d.enabled else 0, now, now),
        )
    for orphan in existing - set(from_config):
        conn.execute(
            "UPDATE devices SET enabled=0, updated_at=? WHERE id=?", (now, orphan)
        )
    conn.commit()


def device_exists(conn, device_id: str) -> bool:
    return conn.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone() is not None


def device_enabled(conn, device_id: str) -> bool:
    row = conn.execute("SELECT enabled FROM devices WHERE id=?", (device_id,)).fetchone()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Upload Session（§14 API + §21 统计口径）
# ---------------------------------------------------------------------------
def create_upload_session(conn, source_device: str, client_ip: str | None) -> str:
    sid = new_session_id()
    now = _now()
    conn.execute(
        "INSERT INTO upload_sessions(id, source_device, client_ip, created_at, status) "
        "VALUES (?,?,?,?, 'CREATED')",
        (sid, source_device, client_ip, now),
    )
    conn.commit()
    return sid


def get_session(conn, session_id: str):
    return conn.execute(
        "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
    ).fetchone()


def _recount(conn, session_id: str) -> None:
    """依据 items 重算会话统计 total/skipped/duplicate/failed/uploaded。

    口径：
    - total = 会话应有 items 数(spec 统计)
    - uploaded=已真正上传并通过网关(status UPLOADED/PROCESSING/COMPLETED/DUPLICATE)
    - skipped=用户显式跳过 user_confirmation=2(§21.1)
    - duplicate/failed 由 item 最终状态
    """
    conn.execute(
        "UPDATE upload_sessions SET"
        " total_files=(SELECT COUNT(*) FROM upload_items WHERE session_id=?),"
        " uploaded_files=(SELECT COUNT(*) FROM upload_items WHERE session_id=? AND"
        "   status IN ('UPLOADED','PROCESSING','COMPLETED','DUPLICATE')),"
        " skipped_files=(SELECT COUNT(*) FROM upload_items WHERE session_id=? AND user_confirmation=2),"
        " duplicate_files=(SELECT COUNT(*) FROM upload_items WHERE session_id=? AND status='DUPLICATE'),"
        " failed_files=(SELECT COUNT(*) FROM upload_items WHERE session_id=? AND status='FAILED')"
        " WHERE id=?",
        (session_id, session_id, session_id, session_id, session_id, session_id),
    )
    conn.commit()


def recount_uploads(conn, session_id: str) -> dict:
    _recount(conn, session_id)
    row = conn.execute(
        "SELECT total_files,uploaded_files,skipped_files,duplicate_files,failed_files FROM upload_sessions"
        " WHERE id=?", (session_id,)
    ).fetchone()
    return dict(row)


def finalize_session(conn, session_id: str) -> None:
    """当会话不再有“待上传/待处理/未决定”项时收尾到终态。

    判定为终态需要同时满足：
      - 不存在 status ∈ (UPLOADING, UPLOADED, PROCESSING)（在传/待处理）
      - 不存在 NO_MATCH 且 user_confirmation=0（还在等上传）
      - 不存在 POSSIBLE_DUPLICATE 且 user_confirmation=0（还在等用户决定）

    满足后：failed>0 → PARTIAL，否则 COMPLETED；写 completed_at（幂等，不覆盖已终态）。
    典型修复：一次 Session 的照片“全部跳过/全部重复”，不再卡在 UPLOADING。
    """
    _recount(conn, session_id)
    busy = conn.execute(
        "SELECT COUNT(*) FROM upload_items WHERE session_id=? AND"
        " status IN ('UPLOADING','UPLOADED','PROCESSING')", (session_id,)
    ).fetchone()[0]
    if busy:
        return
    await_upload = conn.execute(
        "SELECT COUNT(*) FROM upload_items WHERE session_id=? AND status='PRECHECK' AND"
        " precheck_status='NO_MATCH' AND user_confirmation=0", (session_id,)
    ).fetchone()[0]
    undecided = conn.execute(
        "SELECT COUNT(*) FROM upload_items WHERE session_id=? AND status='PRECHECK' AND"
        " precheck_status='POSSIBLE_DUPLICATE' AND user_confirmation=0", (session_id,)
    ).fetchone()[0]
    if await_upload or undecided:
        return
    failed = conn.execute(
        "SELECT COUNT(*) FROM upload_items WHERE session_id=? AND status='FAILED'",
        (session_id,),
    ).fetchone()[0]
    next_status = "PARTIAL" if failed > 0 else "COMPLETED"
    conn.execute(
        "UPDATE upload_sessions SET status=?, completed_at=? WHERE id=? AND status NOT IN"
        " ('COMPLETED','PARTIAL','FAILED','CANCELLED')",
        (next_status, _now(), session_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Precheck（§44 阶段三 / db-spec §33）：仅元数据，不传文件内容
# ---------------------------------------------------------------------------
def bulk_precheck(conn, session_id: str, source_device: str,
                  items, size_is_int=True) -> list[dict]:
    """注册/查询一组文件 Precheck 结果(幂等)。

    items: list[{client_item_id, filename|original_filename, file_size}]
    返回 list[dict(item_id, client_item_id, original_filename, file_size,
                   precheck_status, upload_required)]
    """
    now = _now()
    out: list[dict] = []
    for it in items:
        cid = str(it["client_item_id"])
        filename = it.get("original_filename") or it.get("filename")
        file_size = int(it["file_size"])
        # 幂等：已存在 (session_id, client_item_id) → 返回已有，不新创建（api-spec §20 幂等）
        row = conn.execute(
            "SELECT id, client_item_id, original_filename, file_size, precheck_status"
            " FROM upload_items WHERE session_id=? AND client_item_id=?",
            (session_id, cid),
        ).fetchone()
        if row is None:
            hit = _precheck_is_hit(conn, source_device, filename, file_size)
            precheck = "POSSIBLE_DUPLICATE" if hit else "NO_MATCH"
            cur = conn.execute(
                "INSERT INTO upload_items"
                " (session_id, source_device, client_item_id, original_filename, file_size,"
                "  status, precheck_status, user_confirmation, created_at, updated_at)"
                " VALUES (?,?,?,?,?, 'PRECHECK', ?, 0, ?, ?)",
                (session_id, source_device, cid, filename, file_size, precheck, now, now),
            )
            item_id = cur.lastrowid
        else:
            item_id = row["id"]
            filename = row["original_filename"]
            file_size = row["file_size"]
            precheck = row["precheck_status"]
        out.append({
            "item_id": item_id,
            "client_item_id": cid,
            "filename": filename,
            "file_size": file_size,
            "precheck_status": precheck,
            "status": "PRECHECK",  # api-spec §20/§21：Precheck 阶段 items 状态
            "upload_required": precheck == "NO_MATCH",
        })
    conn.execute(
        "UPDATE upload_sessions SET status='UPLOADING' WHERE id=? AND status='CREATED'",
        (session_id,),
    )
    conn.commit()
    _recount(conn, session_id)
    return out


def _precheck_is_hit(conn, source_device: str, filename: str, file_size: int) -> bool:
    """§33 命中：历史上相同(device,filename,size) 且为 COMPLETED/DUPLICATE。"""
    row = conn.execute(
        "SELECT 1 FROM upload_items ui JOIN photo_assets pa ON ui.asset_id=pa.id"
        " WHERE ui.source_device=? AND ui.original_filename=? AND ui.file_size=?"
        "   AND ui.status IN (?, ?) LIMIT 1",
        (source_device, filename, file_size, "COMPLETED", "DUPLICATE"),
    ).fetchone()
    return row is not None


def possible_duplicates(conn, session_id: str) -> list[sqlite3.Row]:
    """会话下的疑似重复(未决定)文件。"""
    return conn.execute(
        "SELECT id, session_id, source_device, client_item_id, original_filename, file_size,"
        " status, precheck_status, user_confirmation, asset_id, error_code, error_message"
        " FROM upload_items"
        " WHERE session_id=? AND precheck_status='POSSIBLE_DUPLICATE' AND user_confirmation=0"
        " ORDER BY id",
        (session_id,),
    ).fetchall()


def set_item_confirmation(conn, item_id: int, confirmation: int) -> dict | None:
    """设置 user_confirmation (1=重新上传 → 使其上传；2=跳过)。仅允许 POSSIBLE_DUPLICATE/未定。

    返回状态 dict 或 None(找不到/不允许)。
    """
    row = conn.execute(
        "SELECT id, precheck_status, status, session_id FROM upload_items WHERE id=?"
        , (item_id,)
    ).fetchone()
    if not row:
        return None
    session_id = row["session_id"]
    now = _now()
    # 需是疑似重复
    if row["precheck_status"] != "POSSIBLE_DUPLICATE":
        return None
    # 设置为用户选择
    if confirmation == 1:
        new_status = "UPLOADING"          # 允许客户端上传
    elif confirmation == 2:
        new_status = "PRECHECK"          # 明确跳过：停在 precheck，不算已上传
    else:
        raise ValueError("confirmation 必须为 1 或 2")
    conn.execute(
        "UPDATE upload_items SET user_confirmation=?, status=?, updated_at=? WHERE id=?",
        (confirmation, new_status, now, item_id),
    )
    conn.commit()
    _recount(conn, session_id)
    finalize_session(conn, session_id)  # 全部跳过/全部处理完后收尾终态(§ fix: 不卡 UPLOADING)
    return {"item_id": item_id, "user_confirmation": confirmation,
            "upload_required": new_status == "UPLOADING"}
