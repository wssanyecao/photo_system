"""V1 阶段5 可靠性原语：.tmp 清理 + 磁盘保护。

对齐 v1-spec §35/. §36：
- *.tmp 不做断点 offset；启动/周期可清理超过 max_age_days 的 *.tmp（§35）
- 磁盘：< warning% 正常; >=warning% WARNING; >=critical% CRITICAL; >=stop% 停止接收新上传
- 停止阈值只拦截新上传(file 上传时拒绝 507)；在途任务继续（本模块不中断队列）
- processing/ 重启恢复（§34）会让后续 Worker 接入点把残留文件移回 incoming 并同步状态；
  此处提供 scan 与基础结构。
"""

from __future__ import annotations

import time
from pathlib import Path


def cleanup_tmp(incoming_dir: Path, max_age_days: int = 7) -> int:
    """删除 `incoming/*.tmp` 中 mtime 超过 max_age_days 的文件。返回删除数。

    语义：被中断的上传残留 `.tmp`（v1-spec §32/§33）；超过期限自动清理(§35)。
    未到期的保留，供用户恢复上传（V1 无 Range 续传）。
    """
    if max_age_days < 0:
        raise ValueError("max_age_days 不能为负")
    if not incoming_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for p in incoming_dir.glob("*.tmp"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def usage_percent(path: Path) -> float:
    """返回 path 所在设备的已用百分比(0-100)。探测失败返回 0.0。"""
    import shutil

    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0.0
    if usage.total <= 0:
        return 0.0
    return usage.used * 100.0 / usage.total


def disk_level(used_percent: float, warning=80, critical=90, stop=95) -> str:
    """依据已用百分比判定(与 spec §36 单调阈值)。返回 normal/warning/critical/stop。"""
    if used_percent >= stop:
        return "stop"
    if used_percent >= critical:
        return "critical"
    if used_percent >= warning:
        return "warning"
    return "normal"


class StorageState:
    """对一个目录的磁盘健康抽象，供 worker/api 汇报。"""

    def __init__(self, path: Path, warning=80, critical=90, stop=95):
        self.path = Path(path)
        self.warning = warning
        self.critical = critical
        self.stop = stop

    def report(self, use_override: float | None = None) -> dict:
        pct = usage_percent(self.path) if use_override is None else use_override
        return {
            "path": str(self.path),
            "used_percent": round(pct, 2),
            "level": disk_level(pct, self.warning, self.critical, self.stop),
            "thresholds": {
                "warning_percent": self.warning,
                "critical_percent": self.critical,
                "stop_percent": self.stop,
            },
        }

    def accepts_uploads(self, use_override: float | None = None) -> bool:
        return disk_level(
            usage_percent(self.path) if use_override is None else use_override,
            self.warning, self.critical, self.stop,
        ) != "stop"


def recover_processing(conn, processing_dir: Path, incoming_dir: Path) -> int:
    """重启恢复(阶段5/§57.2)：把 processing/ 中未完成任务移回 incoming/ 并同步状态。

    对每个 processing/ 内文件：
      - 找到对应 upload_item(status='PROCESSING' 且 original_filename=文件名)
      - 移动文件到 incoming/<同名>；item → UPLOADED(允许重新进入处理队列)
      - DB 状态与文件必须一致(§34)
    找不到匹配 item 的文件不计入(留待人工/日志核查)。
    """
    if not processing_dir.is_dir():
        return 0
    recovered = 0
    for p in sorted(processing_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        row = conn.execute(
            "SELECT id FROM upload_items WHERE status='PROCESSING' AND original_filename=?",
            (name,),
        ).fetchone()
        if row is None:
            continue
        # 移动文件
        dst = incoming_dir / name
        try:
            incoming_dir.mkdir(parents=True, exist_ok=True)
            p.replace(dst)
        except OSError:
            continue
        # 状态同步
        import sqlite3
        from datetime import datetime, timedelta, timezone as _tz

        now = datetime.now(_tz(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE upload_items SET status='UPLOADED', processing_started_at=NULL,"
            " updated_at=? WHERE id=?", (now, row["id"]),
        )
        conn.commit()
        recovered += 1
    return recovered
