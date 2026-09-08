"""V1 文件上传（.tmp 语义）。

对齐 api-spec：
- POST /api/v1/upload-items/{item_id}/file，multipart 字段 `file`（§29）
- 上传允许性（§30）：NO_MATCH 或(POSSIBLE_DUPLICATE 且 confirmation=1)
    → 其余 REUPLOAD_CONFIRMATION_REQUIRED
- 写 `incoming/<filename>.tmp`，完成后改名 `incoming/<filename>`（§31）
- 无 Range 断点续传（§33）；`.tmp` 只是未完成上传的临时文件
- DB 状态 UPLOADING → UPLOADED（§31）
- 允许扩展名由配置(allowed_extensions)校验，超出→ 415
- 磁盘达到 stop_percent 拒绝写新文件（v1-spec 磁盘保护，507 STORAGE_ERROR）
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from .config import V1Config
from .security import PgApiError

# 上传允许码/原因
_UPLOAD_ALLOW_PRECHECK = {"NO_MATCH"}
_UPLOAD_ALLOW_DUP_CONFIRM = {"POSSIBLE_DUPLICATE"}


def extension_supported(cfg: V1Config, filename: str) -> bool:
    ext = Path(filename).suffix.lstrip(".").lower()
    return ext in cfg.upload.allowed_extensions


def gate_upload_allowed(item_row) -> None:
    """依据 §30 判断该 item 是否允许上传，否则抛 PgApiError。"""
    pre = item_row["precheck_status"]
    conf = item_row["user_confirmation"]
    if pre in _UPLOAD_ALLOW_PRECHECK:
        return
    if pre in _UPLOAD_ALLOW_DUP_CONFIRM and conf == 1:
        return
    if pre == "POSSIBLE_DUPLICATE" and conf == 0:
        raise PgApiError(
            "REUPLOAD_CONFIRMATION_REQUIRED",
            "confirmation required before upload",
            409,
        )
    raise PgApiError(
        "UPLOAD_NOT_ALLOWED", "this item is not allowed to upload", 409
    )


def disk_allows_write(target: Path, stop_percent: int) -> bool:
    """目标所在磁盘使用率 < stop_percent 才允许写入。"""
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return True  # 无法探测(如 tmp 目录)时放行，避免误封
    percent = usage.used * 100.0 / usage.total
    return percent < stop_percent


async def save_item_file(target_incoming: Path, incoming: str, upload_stream,
                         expected_size: int, stop_percent: int = 95,
                         client_mtime_ms: int | None = None) -> Path:
    """把上传流写到 incoming/<fn>.tmp 并 rename 到 incoming/<fn>。

    target_incoming 为解析好的 incoming 目录(绝对)，incoming 为配置文件名字段。
    client_mtime_ms：客户端文件修改时间(epoch 毫秒, 可选)。无 EXIF 的照片将以此作为
    file_mtime 兜底，避免把“上传时间”误当照片时间(浏览器 File.lastModified 传递)。
    仅在 rename 后的正式文件上 os.utime（.tmp 阶段保持服务器时间，不影响 .tmp 清理语义）。
    非法/未来值被忽略(保持服务器时间)，不阻塞上传。
    返回最终文件 Path。校验：文件内容流非空 & 大小与 item.file_size 一致。
    """
    # incoming/filename 与数据库 original_filename 对应；禁止路径分隔符早已在 precheck/server 校验
    filename = Path(incoming).name
    if not filename:
        raise PgApiError("BAD_FILENAME", "invalid filename", 400)
    base = Path(target_incoming)
    if not disk_allows_write(base, stop_percent):
        raise PgApiError("STORAGE_ERROR", "insufficient storage", 507)

    tmp_path = base / f"{filename}.tmp"
    final_path = base / filename

    # 未完成可能遗留 .tmp，直接覆盖（无断点续传，靠重传解决，§32）
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    received = 0
    try:
        with open(tmp_path, "wb") as fh:
            while chunk := await upload_stream.read(1024 * 512):
                received += len(chunk)
                fh.write(chunk)
    except Exception:
        # 中断后 `.tmp` 即是未完成证据（保留，不删；清理交给 tmp_cleanup）
        raise PgApiError("UPLOAD_INTERRUPTED", "upload interrupted", 400) from None

    if received != expected_size:
        # 文件内容与 Precheck 声明大小不一致：保留 .tmp 供诊断，交由用户重传（§32 语义）
        raise PgApiError(
            "SIZE_MISMATCH",
            f"received {received} bytes, expected {expected_size}",
            400,
        )

    # 完成后改名为正式 incoming 文件（§31）
    tmp_path.replace(final_path)
    # 客户端 mtime：仅设置正式文件(非 .tmp)，时间由客户端原文件修改时间决定。
    # 合法性校验：正数、不早于 1990-01-01(防御乱值)、不晚于“未来 7 天”（容忍时钟偏差）。
    # 不合法则忽略（保留服务器时间），保证上传不因异常时间被阻塞。
    if client_mtime_ms is not None:
        import time as _time
        now_ms = _time.time() * 1000
        min_ms = 631152000000          # 1990-01-01 UTC
        max_ms = now_ms + 7 * 86400 * 1000
        if min_ms <= client_mtime_ms <= max_ms:
            sec = client_mtime_ms / 1000.0
            os.utime(final_path, (sec, sec))
    return final_path


def mark_upload_complete(conn: sqlite3.Connection, item_id: int) -> None:
    """item 进入 UPLOADED（§31）并回写会话语义。"""
    from datetime import datetime, timedelta, timezone

    sh = timezone(timedelta(hours=8))
    now = datetime.now(sh).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE upload_items SET status='UPLOADED', uploaded_at=?, updated_at=?,"
        " processing_started_at=NULL WHERE id=?",
        (now, now, item_id),
    )
    # 触发会话统计刷新（uploaded_files 采用口径）
    conn.commit()
