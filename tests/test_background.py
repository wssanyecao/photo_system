"""后台线程生命周期与隔离模型回归。"""

from __future__ import annotations

import yaml

from photo_gateway.background import BackgroundWorkers
from photo_gateway.config import load_yaml_text


def _cfg(root, sync=False):
    data = {
        "version": 1,
        "auth": {"enabled": False},
        "devices": [{"id": "cam"}],
        "storage": {"root": str(root)},
    }
    if sync:
        data["sync"] = {
            "enabled": True,
            "nas": {"host": "nas", "ssh_user": "u", "ssh_key": "/k",
                    "target_root": "/photos"},
        }
    return load_yaml_text(yaml.safe_dump(data, sort_keys=False))


def test_starts_v1_and_v2_as_distinct_threads(tmp_path, monkeypatch):
    workers = BackgroundWorkers(_cfg(tmp_path, sync=True), tmp_path / "db.sqlite",
                                {"incoming": tmp_path / "incoming",
                                 "archive": tmp_path / "Photos"}, None)
    monkeypatch.setattr(workers, "_v1_loop", lambda: workers.stop_event.wait())
    monkeypatch.setattr(workers, "_v2_loop", lambda: workers.stop_event.wait())
    workers.start()
    assert {t.name for t in workers.threads} == {"photo-v1-worker", "photo-v2-worker"}
    assert all(t.is_alive() for t in workers.threads)
    assert workers.status() == {
        "status": "running",
        "mode": "threaded-v1-v2",
        "threads": {"photo-v1-worker": True, "photo-v2-worker": True},
    }
    workers.stop(timeout=2)
    assert workers.threads == []


def test_sync_disabled_only_starts_v1(tmp_path, monkeypatch):
    workers = BackgroundWorkers(_cfg(tmp_path), tmp_path / "db.sqlite",
                                {"incoming": tmp_path / "incoming",
                                 "archive": tmp_path / "Photos"}, None)
    monkeypatch.setattr(workers, "_v1_loop", lambda: workers.stop_event.wait())
    workers.start()
    assert [t.name for t in workers.threads] == ["photo-v1-worker"]
    workers.stop(timeout=2)
