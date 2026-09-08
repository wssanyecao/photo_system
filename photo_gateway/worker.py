"""V1 Worker（阶段4 处理归档）。

对齐 v1-spec/db-spec：
- 由 upload_items status='UPLOADED' 驱动（incoming 文件已就绪）
- SHA-256 流式计算；查 ux_photo_assets_sha256 唯一 → 相同 ⇒ status=DUPLICATE + asset_id（不建新资产）
- 否则归档 Photos/YYYY/YYYYMM：目录由 date 推导；current_filename 冲突加 _1/_2 递增且禁止覆盖(§46/§75)
- photo_assets 写入(sha256/original_filename/current_filename/file_size/mime/date_taken/date_source/archive_path=Photos/{Y}/{YM}/{cur})
- upload_item → COMPLETED + asset_id；DUPLICATE 同样写回但不再建照片
- 归档月的日期优先级 metadata.date_priority（config metadata §35），未解析到则回退 file_mtime → date_source=file_mtime
- 重复已存在文件在 incoming 删除(不入库 new); incoming.final moved-> archive
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import V1Config

SH = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 原语
# ---------------------------------------------------------------------------
def _now_str() -> str:
    return datetime.now(SH).strftime("%Y-%m-%d %H:%M:%S")


def _iso_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 512), b""):
            h.update(chunk)
    return h.hexdigest()


def guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# EXIF 日期 tag（Pillow getexif 顶层=IFD0；0x9003/0x9004 位于 EXIF 子 IFD(0x8769)）
_EXIF_TAG_DATETIME = 0x0132            # DateTime（IFD0/顶层）
_EXIF_TAG_EXIF_IFD = 0x8769            # EXIF 子 IFD 指针
_EXIF_TAG_DATETIME_ORIGINAL = 0x9003   # DateTimeOriginal（EXIF 子 IFD）
_EXIF_TAG_DATETIME_DIGITIZED = 0x9004  # DateTimeDigitized（EXIF 子 IFD）
_EXIF_DATE_TAGS = (
    _EXIF_TAG_DATETIME_ORIGINAL,
    _EXIF_TAG_DATETIME,
    _EXIF_TAG_DATETIME_DIGITIZED,
)


def _exif_date_string(path: Path) -> str | None:
    """尽力用 Pillow 读取 EXIF 日期(DateTimeOriginal/DateTime/DateTimeDigitized)；失败返回 None。

    注意多层结构：0x9003/0x9004 位于 EXIF 子 IFD（tag 0x8769 指向），0x0132 位于 IFD0
    顶层——不能只对顶层 `exif.get()` 取数（否则 DateTimeOriginal/CreateDate 恒丢失，
    会退化成用顶层 DateTime 兜底，如把“后期修改时间”误当拍摄时间）。先合并 IFD0 与
    0x8769 子 IFD，再按 0x9003→0x0132→0x9004 顺序取首个可解析值。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
    except Exception:
        return None
    top = dict(exif.items())
    sub: dict = {}
    try:
        sub = dict(exif.get_ifd(_EXIF_TAG_EXIF_IFD) or {})
    except Exception:
        sub = {}   # 旧 Pillow 无 get_ifd 等：退化为仅顶层（尽力而为）
    for tag in _EXIF_DATE_TAGS:
        v = top.get(tag)
        if v is None:
            v = sub.get(tag)
        if v and isinstance(v, (str, bytes)):
            s = v.decode() if isinstance(v, bytes) else str(v)
            # 形如 2026:08:31 19:32:15
            try:
                dt = datetime.strptime(s.strip(), "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
            return dt.isoformat(sep="T")
    return None


def _current_filename_unique(dir_path: Path, wanted: str) -> str:
    """返回冲突不覆盖的最终文件名(IM_002_1.jpg 递增)；保留扩展名与大小写。"""
    ext = Path(wanted).suffix
    stem = Path(wanted).name[: -len(ext)] if ext else Path(wanted).name
    candidate = wanted
    n = 1
    while (dir_path / candidate).exists():
        candidate = f"{stem}_{n}{ext}"
        n += 1
    return candidate


def epoch_from_date_taken(value: str) -> float | None:
    """把 date_taken(ISO, 可带 +08:00 与小数)换算为 epoch 秒。

    naive 视为上海墙钟(项目统一语义，README §20)；解析失败返回 None。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.timestamp()


def set_file_mtime_from(path: Path, date_taken: str) -> bool:
    """把文件 mtime(及 atime)设置为 date_taken 对应时刻。失败(时间非法)返回 False。"""
    exp = epoch_from_date_taken(date_taken)
    if exp is None:
        return False
    try:
        os.utime(path, (exp, exp))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 处理单文件
# ---------------------------------------------------------------------------
_DATE_SOURCE_RANK = {"file_mtime": 0, "ModifyDate": 1, "CreateDate": 2,
                     "DateTimeOriginal": 3}


def _exif_json(path: Path) -> str | None:
    """抽取 EXIF(IFD0 + 0x8769 子 IFD)为 JSON(便于调试/Immich 索引旁路)。bytes→base64 前缀 b64:。

    同 _exif_date_string：DateTimeOriginal(0x9003)/DateTimeDigitized(0x9004) 位于 EXIF
    子 IFD，不能只序列化顶层（否则多数手机照片缺 DateTimeOriginal）。
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
    except Exception:
        return None
    if not exif:
        return None
    sub: dict = {}
    try:
        sub = dict(exif.get_ifd(_EXIF_TAG_EXIF_IFD) or {})
    except Exception:
        sub = {}   # 旧 Pillow 无 get_ifd：退化为仅顶层
    out = {}

    def _put(tag: int, val) -> None:
        name = TAGS.get(tag, str(tag))
        if name in out:
            return                       # IFD0 优先，重复键不覆盖
        if isinstance(val, bytes):
            import base64
            out[name] = "b64:" + base64.b64encode(val).decode("ascii")
        else:
            try:
                import json as _json
                _json.dumps(val)
                out[name] = val
            except (TypeError, ValueError):
                out[name] = str(val)

    for tag, val in exif.items():
        _put(tag, val)
    for tag, val in sub.items():
        _put(tag, val)
    import json as _json
    try:
        return _json.dumps(out, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _maybe_reanchor_duplicate(conn, cfg, archive_root: Path,
                              existing, src: Path) -> bool:
    """同图重传(SHA 相同)时，用本次文件的元数据修正已有 asset 的日期/归档位置。

    规则：
      - 由本次文件解析日期(EXIF 或客户端 mtime 重建的 file_mtime)；解析失败则不改；
      - 新来源强度弱于旧来源时不覆盖(如旧资产已有 EXIF, 本次仅有 mtime)；
      - 仅当 date_taken 与旧值不同才改写；目标月目录变化时移动文件到正确目录并更新
        archive_path/current_filename(目标同名冲突自动 _N 递增, 不覆盖)。
      若旧资产 exif_json 为空而本次文件带 EXIF，也一并回填。
    返回是否发生修正。
    """
    yyyymm, date_taken, new_source = _resolve_archive_dir(cfg, src)
    if yyyymm is None or date_taken is None:
        return False
    old_source = existing["date_source"]
    # 源强度比较（弱源不覆盖强源）
    if _DATE_SOURCE_RANK.get(new_source, 0) < _DATE_SOURCE_RANK.get(old_source, 0):
        return False
    old_ap = existing["archive_path"]
    old_full = (archive_root.parent / old_ap) if old_ap else None
    new_dir = archive_root / yyyymm[:4] / yyyymm
    new_dir.mkdir(parents=True, exist_ok=True)
    moving = bool(old_full and old_full.parent != new_dir)
    exif_text = _exif_json(src)
    date_changed = (date_taken != existing["date_taken"])
    exif_backfill = (not existing["exif_json"] and exif_text is not None)
    if not (date_changed or moving or exif_backfill):
        return False                      # 无实际需要修正的内容

    new_path = old_full
    new_rel = existing["archive_path"]
    new_name = existing["current_filename"]
    if moving:
        new_name = _current_filename_unique(new_dir, existing["current_filename"])
        new_path = new_dir / new_name
        try:
            if old_full.exists():
                shutil.move(str(old_full), str(new_path))
            new_rel = _archive_rel(archive_root, new_path)
        except OSError:
            return False          # 移动失败则不改库，避免库/文件不一致

    now = _now_str()
    with conn:
        conn.execute(
            "UPDATE photo_assets SET date_taken=?, date_source=?,"
            " archive_path=?, current_filename=?, updated_at=?,"
            " exif_json=COALESCE(exif_json, ?) WHERE id=?",
            (date_taken, new_source, new_rel, new_name, now,
             exif_text, existing["id"]),
        )
    if new_path and new_path.exists():
        set_file_mtime_from(new_path, date_taken)   # 磁盘 mtime 同步资产时间
    return True


def _resurrect_missing_asset(conn, cfg, archive_root: Path,
                             existing, src: Path) -> bool:
    """复活被删(Cleanup/外部)资产：同内容重传时把新文件放回原归档路径。

    修复点：旧文件缺失时原 reanchor 只改 DB 不落文件、随后 incoming 新文件被删
    → 内容彻底丢失。本函数把 src 落到原 archive_path（目录重建），恢复记录并清
    file_missing_at（asset id 不变=sha256 去重身份不变）。date/位置保持原记录
    （同 sha=同内容，EXIF 一致）；exif_json 空时回填。返回是否成功。
    """
    old_ap = existing["archive_path"]
    if not old_ap:
        return False
    old_full = (archive_root.parent / old_ap)
    now = _now_str()
    exif_text = _exif_json(src) if not existing["exif_json"] else None
    try:
        if old_full.exists():
            # 文件已（外部）恢复但标记未清：仅清标记
            with conn:
                conn.execute(
                    "UPDATE photo_assets SET file_missing_at=NULL, updated_at=?,"
                    " exif_json=COALESCE(exif_json, ?) WHERE id=?",
                    (now, exif_text, existing["id"]),
                )
            return True
        old_full.parent.mkdir(parents=True, exist_ok=True)
        src.replace(old_full)                    # incoming → 归档（同卷原子）
        with conn:
            conn.execute(
                "UPDATE photo_assets SET file_missing_at=NULL, updated_at=?,"
                " exif_json=COALESCE(exif_json, ?) WHERE id=?",
                (now, exif_text, existing["id"]),
            )
        if existing["date_taken"]:
            set_file_mtime_from(old_full, existing["date_taken"])
        return True
    except OSError:
        return False


def process_once(conn, cfg, incoming_dir: Path, archive_root: Path,
                 item, log) -> dict:
    """处理一个 status='UPLOADED' 的 upload_item(同步, 单线程)。返回{"item_id", "status", "asset_id", "archive_path"}"""
    item_id = item["id"]
    original_filename = item["original_filename"]
    src = incoming_dir / original_filename
    if not src.exists():
        # incoming 丢失：状态 FAILED 告警（重启恢复留阶段5兜底）
        _mark_item_failed(conn, item_id, "FILE_MISSING", "incoming file missing")
        return {"item_id": item_id, "status": "FAILED"}

    sha = compute_sha256(src)
    existing = conn.execute(
        "SELECT id, date_taken, date_source, current_filename, archive_path,"
        " exif_json, file_missing_at"
        " FROM photo_assets WHERE sha256=?", (sha,)
    ).fetchone()

    if existing is not None:
        repaired = False
        event_msg = "duplicate content"
        if existing["file_missing_at"]:
            # 本地文件已被删(Cleanup/外部)：重传=复活本地副本（asset id 不变）
            repaired = _resurrect_missing_asset(conn, cfg, archive_root, existing, src)
            if repaired:
                event_msg = "duplicate restored: missing file resurrected"
        else:
            # 内容重复：无新资产；但允许“用本次重传的元数据修正旧资产错误日期/归档位置”
            repaired = _maybe_reanchor_duplicate(conn, cfg, archive_root, existing, src)
            if repaired:
                event_msg = "duplicate repaired: date/archive re-anchored"
        with conn:
            conn.execute(
                "UPDATE upload_items SET status='DUPLICATE', asset_id=?, completed_at=?,"
                " updated_at=? WHERE id=?",
                (existing["id"], _now_str(), _now_str(), item_id),
            )
            _append_event(conn, item_id, "PROCESSED", event_msg)
        try:
            src.unlink()
        except OSError:
            pass
        return {"item_id": item_id, "status": "DUPLICATE", "asset_id": existing["id"],
                "repaired": repaired, "archive_path": existing["archive_path"]}

    # 新资产：决定日期与归档目录
    yyyymm, date_taken, date_source = _resolve_archive_dir(cfg, src)
    if yyyymm is None:
        _mark_item_failed(conn, item_id, "NO_DATE",
                          "cannot determine archive month")
        return {"item_id": item_id, "status": "FAILED"}

    archive_dir = archive_root / yyyymm[:4] / yyyymm
    archive_dir.mkdir(parents=True, exist_ok=True)
    current_filename = _current_filename_unique(archive_dir, original_filename)
    final = archive_dir / current_filename
    file_size = src.stat().st_size
    mime = guess_mime(current_filename)
    now = _now_str()
    exif_text = _exif_json(src)

    try:
        shutil.move(str(src), str(final))
    except OSError as exc:
        _mark_item_failed(conn, item_id, "ARCHIVE_MOVE_FAILED", str(exc))
        return {"item_id": item_id, "status": "FAILED"}
    # 磁盘 mtime 与资产时间保持一致（无 EXIF 照片供 Immich/NAS mtime 兜底）
    set_file_mtime_from(final, date_taken)

    rel_archive = _archive_rel(archive_root, final)
    cur = conn.execute(
        "INSERT INTO photo_assets (sha256, original_filename, current_filename, file_size,"
        " mime_type, date_taken, date_source, exif_json, archive_path, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sha, original_filename, current_filename, file_size, mime,
         date_taken, date_source, exif_text, rel_archive, now, now),
    )
    asset_id = cur.lastrowid
    with conn:
        conn.execute(
            "UPDATE upload_items SET status='COMPLETED', asset_id=?, completed_at=?,"
            " updated_at=? WHERE id=?",
            (asset_id, now, now, item_id),
        )
        _append_event(conn, item_id, "PROCESSED", "archived")
    return {"item_id": item_id, "status": "COMPLETED",
            "asset_id": asset_id, "archive_path": rel_archive}


def _archive_rel(archive_root: Path, final: Path) -> str:
    # 返回形如 Photos/2026/202608/IMG_002.jpg（archive 目录名 + 相对其内部路径）
    try:
        rel = final.relative_to(archive_root.parent)
        return rel.as_posix()
    except ValueError:
        return final.as_posix()


def _resolve_archive_dir(cfg: V1Config, img: Path):
    """按 date_priority 推月目录 (YYYYMM)、date_taken、date_source。"""
    priority = list(cfg.metadata.date_priority)
    # 未配置到 priority 时的默认源
    default_order = ["DateTimeOriginal", "CreateDate", "ModifyDate", "file_mtime"]
    for source in (priority or default_order):
        val = None
        if source in ("DateTimeOriginal", "CreateDate", "ModifyDate"):
            val = _exif_date_string(img)  # 尽力读取(不严格区分三类源)，供 priority 落位
            if source != "DateTimeOriginal":  # 其余 exif 源未解析则也到此 source
                pass
        if val is None and source == "file_mtime":
            mtime = datetime.fromtimestamp(img.stat().st_mtime, SH)
            val = mtime.isoformat(sep="T")
        if val:
            try:
                dt = datetime.fromisoformat(val)
            except ValueError:
                continue
            return dt.strftime("%Y%m"), val, source
    return None, None, None


def _mark_item_failed(conn, item_id: int, err_code: str, message: str) -> None:
    with conn:
        now = _now_str()
        conn.execute(
            "UPDATE upload_items SET status='FAILED', error_code=?, error_message=?,"
            " updated_at=? WHERE id=?",
            (err_code, message, now, item_id),
        )
        _append_event(conn, item_id, "PROCESS_FAILED", message)


def _append_event(conn, item_id: int, event_type: str, message: str) -> None:
    try:
        conn.execute(
            "INSERT INTO photo_events (upload_item_id, event_type, message, created_at)"
            " VALUES (?,?,?,?)",
            (item_id, event_type, message, _now_str()),
        )
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# 单次扫描(供 worker 主循环/测试)
# ---------------------------------------------------------------------------
def run_round(conn, cfg, dirs, log) -> dict:
    """取一条待处理 item 处理(进程内单 worker)。返回计数。处理完后刷新该会话并尝试收尾。"""
    item = conn.execute(
        "SELECT id, session_id, original_filename, file_size, status FROM upload_items"
        " WHERE status='UPLOADED' ORDER BY id LIMIT 1"
    ).fetchone()
    if item is None:
        return {"processed": 0}
    res = process_once(
        conn, cfg, dirs["incoming"],
        dirs["archive"], item, log,
    )
    status = res["status"]
    log and log.processing().info(
        "item processed", event="processing_completed",
        photo_id=item["id"], status=status,
        filename=item["original_filename"],
        session_id=item["session_id"],
    )
    _finalize_session(conn, item["session_id"])
    return {"processed": 1, **res}


def _finalize_session(conn, session_id: str) -> None:
    """收尾会话为终态（语义统一于 store.finalize_session）。"""
    from .store import finalize_session as _fs

    _fs(conn, session_id)


def backfill_exif_json(conn, archive_root: Path) -> int:
    """对 exif_json 为空的存量 photo_assets 回填 EXIF(读归档文件)。返回更新条数。"""
    rows = conn.execute(
        "SELECT id, archive_path FROM photo_assets WHERE exif_json IS NULL OR exif_json=''"
    ).fetchall()
    updated = 0
    for r in rows:
        ap = r["archive_path"]
        if not ap:
            continue
        full = (archive_root.parent / ap)
        if not full.exists():
            continue
        text = _exif_json(full)
        if text is None:
            continue
        with conn:
            conn.execute("UPDATE photo_assets SET exif_json=?, updated_at=? WHERE id=?",
                         (text, _now_str(), r["id"]))
        updated += 1
    return updated


def mark_missing_assets(conn, archive_root: Path) -> int:
    """对账：把 DB 有记录但本地文件缺失的资产标记 file_missing_at（仅扫未标记项）。

    用于历史/外部删除的兜底（低频：maintenance --scan-missing）；系统内删除
    （Cleanup/请求 404）已即时标记，无需常跑。返回新标记数。
    """
    rows = conn.execute(
        "SELECT id, archive_path FROM photo_assets WHERE file_missing_at IS NULL"
    ).fetchall()
    now = _now_str()
    missing_ids = []
    for r in rows:
        ap = r["archive_path"]
        if not ap:
            continue
        full = (archive_root.parent / ap) if not Path(ap).is_absolute() else Path(ap)
        if not full.exists():
            missing_ids.append(r["id"])
    if missing_ids:
        with conn:
            conn.executemany(
                "UPDATE photo_assets SET file_missing_at=?, updated_at=?"
                " WHERE id=? AND file_missing_at IS NULL",
                [(now, now, i) for i in missing_ids],
            )
    return len(missing_ids)


def align_asset_mtimes(conn, archive_root: Path) -> int:
    """存量资产磁盘 mtime 对齐到 date_taken(差异>1s 才改)，返回调整数。

    用途：历史归档文件 mtime 可能是上传/修正时间而非照片时间；
    NAS 同步(rsync -t)后 Immich 无 EXIF 时按 mtime 索引，必须先对齐。
    """
    rows = conn.execute(
        "SELECT id, date_taken, archive_path FROM photo_assets"
        " WHERE date_taken IS NOT NULL"
    ).fetchall()
    aligned = 0
    for r in rows:
        exp = epoch_from_date_taken(r["date_taken"])
        if exp is None or not r["archive_path"]:
            continue
        full = archive_root.parent / r["archive_path"]
        if not full.exists():
            continue
        try:
            if abs(full.stat().st_mtime - exp) > 1.0:
                set_file_mtime_from(full, r["date_taken"])
                aligned += 1
        except OSError:
            continue
    return aligned
