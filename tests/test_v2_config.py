"""V2 sync 配置模型测试（可选块/启用校验）。"""

from __future__ import annotations

import yaml as _y

from photo_gateway.config import ConfigError, load_yaml_text


def _core(**extra):
    data = {
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "a"}], "storage": {"root": "/tmp/pg"},
    }
    data.update(extra)
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_absent_sync_is_none_and_ok():
    cfg = _core()
    assert cfg.sync is None


def test_sync_disabled_with_partial_ok():
    cfg = _core(sync={"enabled": False, "nas": {}})
    assert cfg.sync.enabled is False


def test_sync_enabled_defaults_loaded():
    cfg = _core(sync={"enabled": True, "nas": {
        "host": "nas.local", "ssh_user": "u", "ssh_key": "/k",
        "target_root": "/data/Photos",
    }})
    assert cfg.sync.enabled is True
    assert cfg.sync.worker.cycle_interval_seconds == 300
    assert cfg.sync.failure.threshold == 3
    assert cfg.sync.cleanup.synced_retention_days == 90


def test_sync_enabled_missing_nas_fields_rejected():
    with pytest_raises(ConfigError, "必填"):
        _core(sync={"enabled": True, "nas": {"host": "h"}})
    # 上面用别名


def pytest_raises(exc, needle):
    import pytest
    return pytest.raises(exc, match=needle)


def test_sync_bad_threshold():
    with pytest_raises(ConfigError, "threshold"):
        _core(sync={"enabled": True, "nas": {"host": "h", "ssh_user": "u",
                                             "ssh_key": "/k", "target_root": "/p",
                                             }, "failure": {"threshold": 0}})


def test_sync_bad_retention():
    with pytest_raises(ConfigError, "retention"):
        _core(sync={"enabled": True, "nas": {"host": "h", "ssh_user": "u",
                                             "ssh_key": "/k", "target_root": "/p"},
                    "cleanup": {"synced_retention_days": 0}})
