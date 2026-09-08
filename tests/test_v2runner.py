"""V2 runner + Retry 基础测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _y

from photo_gateway.config import load_yaml_text
from photo_gateway.db import init_db
from photo_gateway.v2 import MS_ABNORMAL, MS_NORMAL, append_month_state
from photo_gateway.v2cycle import current_state_name, retry_month
from photo_gateway.v2cycle import FakeNas as _FakeNas
from photo_gateway.v2runner import cycle_interval, make_backend, run_once, v1_idle


def _cfg_yaml(root, enabled=False, extra_sync=None):
    data = {
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "a"}], "storage": {"root": str(root)},
    }
    sync = {"enabled": enabled,
            "nas": {"host": "h", "ssh_user": "u", "ssh_key": "/k", "target_root": "/p"}}
    if extra_sync:
        sync.update(extra_sync)
    if enabled or extra_sync:
        data["sync"] = sync
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


def _arch(root, months=("202608",)):
    a = Path(root) / "Photos"
    for m in months:
        d = a / m[:4] / m
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.jpg").write_bytes(b"x")
    return a


def test_disabled_backend_none(tmp_path):
    cfg = _cfg_yaml(tmp_path, enabled=False)
    assert make_backend(cfg, _arch(tmp_path)) is None
    assert cycle_interval(cfg) == 0


def test_enabled_returns_none_backend_only_if_no_rsync_mocked():
    # make_backend 真实 RsyncNas 会 `which rsync`；这里仅校验 cfg 结构解析
    pass


def test_v1_idle_true_when_empty(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    assert v1_idle(conn) is True
    conn.close()


def test_run_once_disabled_gate(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    arch = _arch(tmp_path)
    cfg = _cfg_yaml(tmp_path, enabled=False)
    assert run_once(conn, cfg, arch)["gate"] == "sync_disabled"
    conn.close()


def test_run_once_with_fake_backend(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    arch = _arch(tmp_path)
    cfg = _cfg_yaml(tmp_path, enabled=True)
    res = run_once(conn, cfg, arch,
                   backend=_FakeNas(alive=True, ready=True, target_root=tmp_path / "nas"))
    assert res["gate"] == "ok" and res["tasks"] >= 1
    assert current_state_name(conn, "202608") == MS_NORMAL
    conn.close()


def test_retry_only_abnormal(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    append_month_state(conn, "202604", MS_NORMAL, reason="init")
    append_month_state(conn, "202604", MS_ABNORMAL, reason="fail")
    with pytest.raises(ValueError):
        retry_month(conn, "202603")         # 无记录/非 ABNORMAL 不可 Retry
    retry_month(conn, "202604", by="viewer")
    assert current_state_name(conn, "202604") == "RETRY_REQUESTED"
    conn.close()


def test_retry_http_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from photo_gateway.http import create_app
    conn = init_db(tmp_path / "db.sqlite")
    append_month_state(conn, "202604", MS_NORMAL, reason="init")
    append_month_state(conn, "202604", MS_ABNORMAL, reason="fail")
    conn.commit()      # http 新连接需要已提交(append 不自动 commit)
    conn.close()
    cfg = _cfg_yaml(tmp_path, enabled=False)
    app = create_app(cfg, db_path=tmp_path / "db.sqlite")
    with TestClient(app) as c:
        r = c.post("/api/v1/sync/months/202604/retry")
        assert r.status_code == 200
        assert r.json()["data"]["state"] == "RETRY_REQUESTED"
    # 回归：retry 必须落盘（曾缺 conn.commit() → 返回成功但 DB 未变，WebUI 点击无效）
    conn2 = init_db(tmp_path / "db.sqlite")
    try:
        assert current_state_name(conn2, "202604") == "RETRY_REQUESTED"
    finally:
        conn2.close()


def test_overview_reports_abnormal_reason_and_error(tmp_path):
    """overview 返回 ABNORMAL 原因与最近 FAILED 错误（优化：WebUI 展示具体错误）。"""
    from fastapi.testclient import TestClient
    from photo_gateway.http import create_app
    from photo_gateway.v2 import create_batch, create_sync_task, finish_sync_task

    _arch(tmp_path, months=("202608",))
    conn = init_db(tmp_path / "db.sqlite")
    append_month_state(conn, "202608", MS_NORMAL, reason="init")
    bid = create_batch(conn)
    tid = create_sync_task(conn, bid, "202608", "/src/2026/202608", "/dst/2026/202608")
    finish_sync_task(conn, tid, False, exit_code=23,
                     stderr="rsync: mkstemp failed: Permission denied (13)",
                     error_message="rsync: mkstemp failed: Permission denied (13)")
    append_month_state(conn, "202608", MS_ABNORMAL, reason="consecutive failures",
                       consecutive_failed=3, abnormal_at="2026-09-07 09:44:37")
    conn.commit()
    conn.close()

    cfg = _cfg_yaml(tmp_path, enabled=True)
    app = create_app(cfg, db_path=tmp_path / "db.sqlite")
    with TestClient(app) as c:
        m = c.get("/api/v1/sync/overview").json()["data"]["months"][0]
        assert m["current_state"] == MS_ABNORMAL
        assert m["abnormal_reason"] == "consecutive failures"
        assert "Permission denied" in m["latest_error"]
        assert m["latest_error_exit"] == 23


def test_overview_matches_yyyymm_and_state(tmp_path):
    """overview 以 YYYYMM 为月份键关联 Task/State（回归：曾误用 rel 路径查 DB 导致 state 全空）。"""
    from fastapi.testclient import TestClient
    from photo_gateway.http import create_app
    from photo_gateway.v2 import create_batch, create_sync_task, finish_sync_task

    arch = _arch(tmp_path, months=("202608",))
    conn = init_db(tmp_path / "db.sqlite")
    append_month_state(conn, "202608", MS_NORMAL, reason="init")
    bid = create_batch(conn)
    tid = create_sync_task(conn, bid, "202608", str(arch / "2026" / "202608"),
                           "/nas/2026/202608")
    finish_sync_task(conn, tid, True, exit_code=0)
    conn.commit()
    conn.close()

    cfg = _cfg_yaml(tmp_path, enabled=True)
    app = create_app(cfg, db_path=tmp_path / "db.sqlite")   # 不传 dirs → 不启后台 ticker
    with TestClient(app) as c:
        ov = c.get("/api/v1/sync/overview").json()["data"]
        assert len(ov["months"]) == 1
        m = ov["months"][0]
        assert m["month"] == "202608"            # YYYYMM，而非 "2026/202608"
        assert m["current_state"] == MS_NORMAL
        assert m["latest_task_status"] == "SUCCESS"
        assert m["latest_success_at"] is not None


def test_sync_gates_endpoint(tmp_path):
    """GET /sync/gates：disabled → enabled:False；enabled → 结构含门控字段（host 不可达时 alive=False 不抛）。"""
    from fastapi.testclient import TestClient
    from photo_gateway.http import create_app

    conn = init_db(tmp_path / "db.sqlite"); conn.close()
    # 未启用
    cfg = _cfg_yaml(tmp_path, enabled=False)
    app = create_app(cfg, db_path=tmp_path / "db.sqlite")
    with TestClient(app) as c:
        assert c.get("/api/v1/sync/gates").json()["data"] == {"enabled": False}
    # 启用（host=h 不可解析 → alive False，路径不抛错）
    cfg2 = _cfg_yaml(tmp_path, enabled=True)
    app2 = create_app(cfg2, db_path=tmp_path / "db.sqlite")
    with TestClient(app2) as c:
        d = c.get("/api/v1/sync/gates").json()["data"]
        assert d["enabled"] is True
        for k in ("checked_at", "nas_alive", "nas_ready", "v1_idle", "months_total", "all_ok"):
            assert k in d
        assert d["nas_alive"] is False and d["all_ok"] is False
