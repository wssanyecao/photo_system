"""V2 周期引擎测试：gate / 首次同步建立 / 连续失败→ABNORMAL 隔离 / Cleanup 条件与 dry-run。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photo_gateway.db import init_db
from photo_gateway.v2 import (MS_ABNORMAL, MS_NORMAL, MS_RETRY, TASK_SUCCESS,
                              append_month_state, current_month_state,
                              finish_sync_task)
from photo_gateway.v2cycle import (FakeNas, NasBackend, cleanup_candidates,
                                   cleanup_expired, current_state_name,
                                   retry_month, run_cycle)


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "db.sqlite")
    yield c
    c.close()


def _build_archive(root, months=("202608",)):
    a = Path(root) / "Photos"
    for m in months:
        ddir = a / m[:4] / m
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / "x.jpg").write_bytes(b"data")
    return a


class _FailNas(NasBackend):
    alive = ready = lambda self: True               # noqa: E731
    def needs_sync(self, source, target):  # noqa: A002
        return True
    def sync(self, source, target):  # noqa: A002
        return 23, "", "rsync simulated error"


def test_gate_nas_unavailable(tmp_path):
    c = init_db(tmp_path / "db.sqlite")
    r = run_cycle(c, _build_archive(tmp_path), FakeNas(alive=False, ready=True))
    assert r["gate"] == "not_alive"
    c.close()


def test_first_cycle_syncs_all_and_marks_normal(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    arch = _build_archive(tmp_path, ["202608", "202604"])
    fake = FakeNas(alive=True, ready=True, target_root=Path(tmp_path) / "nas")
    r = run_cycle(conn, arch, fake, v1_idle=True)
    assert r["gate"] == "ok" and r["tasks"] >= 1
    for m in ("202604", "202608"):
        assert current_state_name(conn, m) == MS_NORMAL
    conn.close()


def test_consecutive_failure_reaches_abnormal_and_is_isolated(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    arch = _build_archive(tmp_path, ["202608"])
    nas = _FailNas()
    abnormal = []
    # 3 轮后达到阈值 → ABNORMAL；第 4 轮隔离（不再建任务）
    for _ in range(5):
        r = run_cycle(conn, arch, nas, fail_threshold=3, cleanup=False)
        abnormal.extend(r.get("abnormal_created", []))
    assert "202608" in abnormal   # ABNORMAL_created 记录月份
    assert current_state_name(conn, "202608") == MS_ABNORMAL
    # 隔离后不再自动建任务
    r2 = run_cycle(conn, arch, nas, fail_threshold=3, cleanup=False)
    assert r2["tasks"] == 0
    conn.close()


def test_retry_only_for_abnormal_then_normal_cycle(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    append_month_state(conn, "202604", MS_NORMAL, reason="init")
    append_month_state(conn, "202604", MS_ABNORMAL, reason="failed")
    # 只有 ABNORMAL 才能 Retry
    with pytest.raises(ValueError):
        retry_month(conn, "202601")
    retry_month(conn, "202604", by="operator")
    assert current_state_name(conn, "202604") == MS_RETRY
    conn.close()


def test_cleanup_conditions_and_dry_run(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    arch = _build_archive(tmp_path, ["202601"])
    ok_month = "202601"
    append_month_state(conn, ok_month, MS_NORMAL, reason="init")
    title = "2026-01-01 00:00:00"
    finish_task_for(conn, ok_month, day_old=200)  # >90 保留期
    cands = cleanup_candidates(conn, arch, retention_days=90,
                               now=datetime.now().astimezone())
    by = {c["month"]: c for c in cands}
    assert by[ok_month]["remove"] is True
    removed = cleanup_expired(conn, arch, retention_days=90, dry_run=True)
    assert ok_month in removed
    conn.close()


def finish_task_for(conn, month, day_old):
    from photo_gateway.v2 import create_sync_task
    # 直接插入一条很久以前完成的 SUCCESS Task(历史)
    now = datetime(2026, 4, 1) - timedelta(days=day_old)
    conn.execute(
        "INSERT INTO photo_sync_batches(started_at,status,task_count,success_count,"
        " failed_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (now.strftime("%Y-%m-%d %H:%M:%S"), "COMPLETED", 1, 1, 0,
         now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")))
    bid = conn.execute("SELECT max(id) m FROM photo_sync_batches").fetchone()["m"]
    conn.execute(
        "INSERT INTO photo_sync_tasks(batch_id,archive_month,source_path,target_path,"
        " status,completed_at,created_at,updated_at) VALUES (?,?,?,?, 'SUCCESS',?,?,?)",
        (bid, month, "/src", "/dst", now.strftime("%Y-%m-%d %H:%M:%S"),
         now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


class _RecLog:
    """轻量日志采集：验证 run_cycle 生命周期事件（对齐 v2-spec §42）。"""

    def __init__(self):
        self.rows: list[tuple[str, str | None, str]] = []

    def info(self, msg, event=None):
        self.rows.append(("info", event, msg))

    def warning(self, msg, event=None):
        self.rows.append(("warning", event, msg))

    def error(self, msg, event=None):
        self.rows.append(("error", event, msg))


def test_cycle_logs_gate_fail(tmp_path):
    c = init_db(tmp_path / "db.sqlite")
    rec = _RecLog()
    r = run_cycle(c, _build_archive(tmp_path), FakeNas(alive=False), log=rec)
    assert r["gate"] == "not_alive"
    assert any(ev == "v2_cycle_gate" for _, ev, _ in rec.rows)
    c.close()


def test_cycle_logs_success_events(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    arch = _build_archive(tmp_path, ["202608"])
    rec = _RecLog()
    fake = FakeNas(alive=True, ready=True, target_root=Path(tmp_path) / "nas")
    r = run_cycle(conn, arch, fake, v1_idle=True, log=rec)
    assert r["gate"] == "ok" and r["tasks"] >= 1
    evs = [ev for _, ev, _ in rec.rows]
    assert "v2_batch_created" in evs
    assert "v2_task_result" in evs
    assert "v2_cycle_done" in evs
    conn.close()


def test_cycle_logs_abnormal(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    arch = _build_archive(tmp_path, ["202608"])
    rec = _RecLog()
    for _ in range(3):
        run_cycle(conn, arch, _FailNas(), v1_idle=True, fail_threshold=3, cleanup=False,
                  log=rec)
    assert current_state_name(conn, "202608") == MS_ABNORMAL
    assert any(ev == "v2_month_abnormal" for _, ev, _ in rec.rows)
    conn.close()
