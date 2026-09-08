"""V2 周期引擎（Alignment v2-spec §4-34）。

结构：一个 V2 Worker 工作周期 = NAS Alive → V1 Idle → NAS Ready → Difference →
Batch → Sync Task(PENDING→RUNNING→SUCCESS/FAILED) → 失败循环/Month State → Cleanup。

设计要点：
- 真实传输通过注入的 `NasBackend` 抽象（alive/ready/needs_sync/sync），便于无 NAS 环境以 FakeNas 可测；
  真实 rsync adapter 留待适配（RsyncNas 骨架在此）。
- Monthly 自动隔离：current ABNORMAL 的月份不参与 Batch；
  RETRY_REQUESTED 是人工放开始，新建其第一个 Sync Task 时追加 NORMAL（新的 failure cycle）。
- 连续失败只统计当前 failure cycle（有 RETRY 边界）内最近连续 FAILED（SUCCESS 归零）；
  达到阈值时追加 ABNORMAL 记录（一次穿越触发一条，后续重复待管理员）。
- Cleanup 自 §27-34：全部删除条件(30.1 NORMAL · 30.2 有 Task · 30.3 最新 SUCCESS ·
  30.4 达 retention)通过才对 YYYY/YYYYMM 目录执行 rmtree，并做路径安全与审计。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import v2 as d
from .v2 import (
    MS_ABNORMAL,
    MS_NORMAL,
    MS_RETRY,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SUCCESS,
    append_month_state,
    create_batch,
    create_sync_task,
    current_month_state,
    finish_sync_task,
    now_str,
)

SH = timezone(timedelta(hours=8))
DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_RETENTION_DAYS = 90
import re as _re
_MM_RE = _re.compile(r"\d{6}")     # YYYYMM
_YYYY_RE = _re.compile(r"\d{4}")   # YYYY


# ---------------------------------------------------------------------------
# NAS 抽象
# ---------------------------------------------------------------------------
class NasBackend:
    """同步后端抽象。子类实现：alive(连通)、ready(可同步)、needs_sync(diff)、sync(执行单向 rsync-*)。"""

    def alive(self) -> bool:          # noqa: D401 - spec §5
        raise NotImplementedError

    def ready(self) -> bool:          # spec §7
        raise NotImplementedError

    def needs_sync(self, source: Path, target: Path) -> bool:     # diff §9
        raise NotImplementedError

    def sync(self, source: Path, target: Path) -> tuple[int, str, str]:
        """执行单向同步(禁 --delete)。返回 (exit_code, stdout, stderr)。"""
        raise NotImplementedError


class FakeNas(NasBackend):
    """测试用后端：内存目标 + 可选开关可调（不真实传输，仅建占位表示已同步）。"""

    def __init__(self, alive=True, ready=True, target_root: Path | None = None):
        self.alive_ = alive
        self.ready_ = ready
        self.target_root = target_root
        self._flip_random = 0      # not random for determinism; failure injected externally

    def alive(self) -> bool:
        return self.alive_

    def ready(self) -> bool:
        return self.ready_

    def needs_sync(self, source: Path, target: Path) -> bool:
        if self.target_root is None:
            return True
        d = target if target.is_absolute() else (self.target_root / target)
        old = set(p.name for p in d.glob("**/*") if p.is_file()) if d.is_dir() else set()
        src = set(p.name for p in source.glob("**/*") if p.is_file())
        return bool(src - old)

    def sync(self, source: Path, target: Path) -> tuple[int, str, str]:
        t = target if target.is_absolute() else (self.target_root / target)
        t.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and source.exists():
            for p in source.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(source)
                    (t / rel).parent.mkdir(parents=True, exist_ok=True)
                    (t / rel).write_bytes(p.read_bytes())
        return 0, "", ""


# ---------------------------------------------------------------------------
# 引擎辅助
# ---------------------------------------------------------------------------
def ensure_initial_state(conn, month: str) -> None:
    """首次发现该 YYYYMM 且无任何 Month State 时，建初始 NORMAL（§13.1）。"""
    row = conn.execute(
        "SELECT 1 FROM photo_sync_month_states WHERE archive_month=? LIMIT 1", (month,)
    ).fetchone()
    if row is None:
        append_month_state(conn, month, MS_NORMAL, reason="init")  # noqa: N816 month


def current_state_name(conn, month: str) -> str | None:
    cur = current_month_state(conn, month)
    return cur["state"] if cur else None


def _consecutive_failed(conn, month: str, boundary_older_from: str | None = None) -> int:
    """当前 failure cycle 内(自 boundary 起)最近连续失败 Task 数；SUCCESS 归零(§21.1)。"""
    sql = ("SELECT id,status,created_at FROM photo_sync_tasks WHERE archive_month=?"
           " ORDER BY id DESC")
    rows = conn.execute(sql, (month,)).fetchall()
    cnt = 0
    for r in rows:
        if boundary_older_from and r["created_at"] < boundary_older_from:
            break          # 只统计 RETRY 边界之后
        if r["status"] == TASK_FAILED:
            cnt += 1
        elif r["status"] == TASK_SUCCESS:
            break           # 归零，序列结束
    return cnt


def retry_month(conn, month: str, by: str = "admin") -> None:
    """人工 Retry：给 ABNORMAL 月追加 RETRY_REQUESTED（不改历史，作为新 cycle 边界）。"""
    if month is None:
        raise ValueError("month empty")
    if current_state_name(conn, month) != MS_ABNORMAL:
        raise ValueError("只允许对 ABNORMAL 月份执行 Retry")
    append_month_state(conn, month, MS_RETRY, reason="manual retry",
                       retry_by=by, retry_at=now_str())


# ---------------------------------------------------------------------------
# 一个 Cycle
# ---------------------------------------------------------------------------
def run_cycle(conn, archive_root: Path, nas: NasBackend, *,
              fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
              retention_days: int = DEFAULT_RETENTION_DAYS,
              v1_idle: bool = True, dry_run_cleanup: bool = True,
              cleanup: bool = True, log=None) -> dict:
    """执行一轮 V2 工作周期；返回各阶段摘要。Cleanup 采用 safety+audit。

    log：结构化域日志（logman.processing() 等，需带 info/warning/error）；None=静默
    （对齐 v2-spec §42：记录 cycle/gate/task/ABNORMAL/cleanup 生命周期；上下文并入 message，
    外层字段只使用 spec §19 白名单）。
    """

    def _emit(lvl: str, msg: str, event: str) -> None:
        if log is not None:
            getattr(log, lvl)(msg, event=event)

    if not nas.alive():
        _emit("warning", "v2 cycle gate blocked gate=not_alive", "v2_cycle_gate")
        return {"gate": "not_alive"}
    if not v1_idle:
        _emit("warning", "v2 cycle gate blocked gate=v1_busy", "v2_cycle_gate")
        return {"gate": "v1_busy"}
    if not nas.ready():
        _emit("warning", "v2 cycle gate blocked gate=not_ready", "v2_cycle_gate")
        return {"gate": "not_ready"}
    if not archive_root.is_dir():
        _emit("warning", "v2 cycle gate blocked gate=bad_root", "v2_cycle_gate")
        return {"gate": "bad_root"}

    batch_id = None
    made_tasks = 0
    abnormal_created = []
    removed_months: list[str] = []

    # 月份清单 & 初始 NORMAL
    months = [m for _, m in d.month_dirs(archive_root)]
    for m in months:
        ensure_initial_state(conn, m)

    # Difference → 候选（排除 ABNORMAL；RETRY 放行）
    diff = []
    for rel, month in d.month_dirs(archive_root):
        st = current_state_name(conn, month)
        if st == MS_ABNORMAL:
            continue
        src = archive_root / rel
        target = Path("/nas") / rel          # NAS 目标映射由 transport 解析，此处为记录性默认
        if nas.needs_sync(src, target):
            diff.append((rel, month, src, target))

    if diff:
        batch_id = create_batch(conn)
        _emit("info", f"v2 batch created batch_id={batch_id} months={len(diff)}",
              "v2_batch_created")
        for rel, month, src, target in diff:
            # RETRY 放行 → 创建它的第一个新 Task 时开新 failure cycle（追加 NORMAL）
            if current_state_name(conn, month) == MS_RETRY:
                append_month_state(conn, month, MS_NORMAL, reason="retry cycle start")
            tid = create_sync_task(conn, batch_id, month, str(src), str(target))
            made_tasks += 1

            # 执行
            conn.execute("UPDATE photo_sync_tasks SET status='RUNNING', started_at=?,"
                         " updated_at=? WHERE id=?", (now_str(), now_str(), tid))
            conn.commit()
            try:
                code, out, err = nas.sync(src, target)
            except Exception as exc:  # 未捕获异常按失败计（§18.5）
                code, out, err = 255, "", f"sngc-err: {exc}"
            ok = code == 0
            finish_sync_task(conn, tid, ok, exit_code=code, stdout=out, stderr=err,
                             error_message=err)
            _emit("info" if ok else "warning",
                  f"v2 task finished month={month} batch_id={batch_id} task_id={tid}"
                  f" status={'SUCCESS' if ok else 'FAILED'} exit_code={code}"
                  + (f" error={err[:400]}" if not ok else ""),
                  "v2_task_result")

            boundary = None
            after = conn.execute(
                "SELECT retry_requested_at FROM photo_sync_month_states"
                " WHERE archive_month=? ORDER BY created_at DESC, id DESC LIMIT 1", (month,)
            ).fetchone()
            # 若当前状态刚在此 Task 创建前从 RETRY 切 NORMAL（新 cycle 边界）→ 之前 RETRY 时间作为边界
            row_b = conn.execute(
                "SELECT created_at FROM photo_sync_month_states WHERE archive_month=? AND"
                " state='RETRY_REQUESTED' ORDER BY id DESC LIMIT 1", (month,)
            ).fetchone()
            if row_b:
                boundary = row_b["created_at"]
            if not ok:
                cnt = _consecutive_failed(conn, month, boundary)
                if cnt >= fail_threshold and current_state_name(conn, month) != MS_ABNORMAL:
                    append_month_state(
                        conn, month, MS_ABNORMAL, reason="consecutive failures",
                        consecutive_failed=cnt,
                        abnormal_at=now_str(),
                    )
                    abnormal_created.append(month)
                    _emit("warning",
                          f"v2 month abnormal month={month} consecutive_failed={cnt}",
                          "v2_month_abnormal")
        # Batch 完成计数
        bstats = conn.execute(
            "SELECT COALESCE(SUM(status='SUCCESS'),0) ok, COALESCE(SUM(status='FAILED'),0) bad"
            " FROM photo_sync_tasks WHERE batch_id=?", (batch_id,)
        ).fetchone()
        conn.execute(
            "UPDATE photo_sync_batches SET task_count=?, success_count=?, failed_count=?,"
            " status='COMPLETED', completed_at=?, updated_at=? WHERE id=?",
            (made_tasks, bstats["ok"], bstats["bad"], now_str(), now_str(), batch_id),
        )
        conn.commit()

    if cleanup:
        removed_months = cleanup_expired(conn, archive_root, retention_days=retention_days,
                                         dry_run=dry_run_cleanup)
        if removed_months:
            _emit("info", "v2 cleanup removed months=" + ",".join(removed_months),
                  "v2_cleanup_removed")
    _emit("info",
          f"v2 cycle done gate=ok batch_id={batch_id or '-'} tasks={made_tasks}"
          f" abnormal_created={len(abnormal_created)} cleanup_removed={len(removed_months)}",
          "v2_cycle_done")
    return {
        "gate": "ok",
        "batch_id": batch_id,
        "tasks": made_tasks,
        "abnormal_created": abnormal_created,
        "cleanup_removed": removed_months,
    }


# ---------------------------------------------------------------------------
# Cleanup（§27-34）
# ---------------------------------------------------------------------------
def cleanup_candidates(conn, archive_root: Path, retention_days: int,
                       now: datetime | None = None) -> list[dict]:
    """返回满足全部删除条件的 YYYYMM(仍未执行删除)。每项含是否达标与原因。"""
    now = now or datetime.now(SH)
    out = []
    for rel, month in d.month_dirs(archive_root):
        # 30.1 当前 NORMAL
        if current_state_name(conn, month) != MS_NORMAL:
            out.append({"month": month, "remove": False, "reason": "state!=NORMAL"})
            continue
        # 30.2 有历史 Task
        latest = conn.execute(
            "SELECT id,status,completed_at FROM photo_sync_tasks WHERE archive_month=?"
            " ORDER BY id DESC LIMIT 1", (month,)
        ).fetchone()
        if latest is None:
            out.append({"month": month, "remove": False, "reason": "no task"})
            continue
        # 30.3 最新的必须 SUCCESS
        if latest["status"] != TASK_SUCCESS:
            out.append({"month": month, "remove": False, "reason": f"latest={latest['status']}"})
            continue
        # 30.4 最新 SUCCESS 完成时间达保留期(时间存 SH wall-clock na，避免 tz 相减)
        try:
            # 存库时间无时区=上海墙钟；比较统一用 naive
            finished = datetime.strptime(latest["completed_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError) as exc:
            out.append({"month": month, "remove": False, "reason": "bad success time"})
            continue
        now_naive = now.replace(tzinfo=None) if now.tzinfo else now
        aged = (now_naive - finished) >= timedelta(days=retention_days)
        if not aged:
            out.append({"month": month, "remove": False,
                        "reason": "within retention", "until": (finished + timedelta(days=retention_days)).isoformat()})
            continue
        out.append({"month": month, "remove": True, "dir": rel})
    return out


def cleanup_expired(conn, archive_root: Path, retention_days: int,
                    dry_run: bool = True, audit=None) -> list[str]:
    """安全删除满足条件的月份目录(以归档根限定 YYYY/YYYYMM)。dry_run=default 只计数返回待删。"""
    finals = [c for c in cleanup_candidates(conn, archive_root, retention_days) if c["remove"]]
    removed: list[str] = []
    for c in finals:
        month = c["month"]; rel = c["dir"]
        target = (archive_root / rel).resolve()
        root = archive_root.resolve()
        if root not in target.parents:
            continue                                  # §34.1/.3 越界拒绝
        if not _MM_RE.fullmatch(target.name) or not _YYYY_RE.fullmatch(target.parent.name):
            continue                                  # §34.2 结构校验(YYYY/YYYYMM)
        if dry_run:
            removed.append(month); continue
        try:
            shutil.rmtree(target)
            removed.append(month)
            if audit:
                audit(month, rel)
            # 本地已删：标记该月资产缺失（列表/UI 不再展示；去重身份保留）
            conn.execute(
                "UPDATE photo_assets SET file_missing_at=?, updated_at=?"
                " WHERE archive_path LIKE ?",
                (now_str(), now_str(), f"Photos/{rel}/%"),
            )
            conn.commit()
        except OSError:
            continue                                  # 失败只影响本月(§34.7)
    return removed
