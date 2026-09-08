"""配置模块测试 (config-spec §2 / §3 关键不变量)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from photo_gateway.config import (
    ConfigError,
    V1Config,
    load_config,
    load_yaml_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = Path(REPO_ROOT) / "config" / "photo-gateway.example.yaml"


# ---------------------------------------------------------------------------
# 正常加载
# ---------------------------------------------------------------------------

def test_example_config_loads_and_defaults():
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg, V1Config)
    assert cfg.version == 1
    # 顶层缺失键补默认值（config-spec §56 默认值列）
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 8080
    assert cfg.auth.enabled is False  # example 明确置 false（无真实 hash）
    assert cfg.processing.worker_count == 1
    assert cfg.upload.concurrency == 1
    assert cfg.upload.precheck.batch_size == 5
    assert "jpg" in cfg.upload.allowed_extensions
    assert cfg.metadata.date_priority == [
        "DateTimeOriginal", "CreateDate", "ModifyDate", "file_mtime",
    ]
    assert cfg.disk.warning_percent == 80
    assert cfg.disk.critical_percent == 90
    assert cfg.disk.stop_percent == 95
    assert cfg.logging.level == "INFO"
    assert cfg.logging.rotation.max_size_mb == 50
    assert cfg.logging.rotation.retention_days == 30
    # 设备读取
    assert cfg.device_ids() == ["phone-xiaomi", "phone-iphone", "camera-pc"]
    assert cfg.enabled_device_ids() == ["phone-xiaomi", "phone-iphone", "camera-pc"]


def test_storage_relative_resolves_under_root():
    cfg = load_config(EXAMPLE)
    assert cfg.storage.resolved_incoming() == Path("/mnt/sda1/photo-gateway") / "incoming"
    assert cfg.storage.resolved_archive() == Path("/mnt/sda1/photo-gateway") / "Photos"
    assert cfg.storage.resolved_database() == (
        Path("/mnt/sda1/photo-gateway") / "database" / "photo-gateway.db"
    )


# ---------------------------------------------------------------------------
# 最小配置（全部用默认/参数化）正确通过业务校验
# ---------------------------------------------------------------------------

def _minimal_yaml(**overrides) -> str:
    data = {
        "version": 1,
        "auth": {"enabled": False},
        "devices": [{"id": "cam1", "name": "Cam1"}],
        "storage": {"root": "/tmp/pg"},
    }
    data.update(overrides)
    import yaml as _y
    return _y.safe_dump(data, sort_keys=False)


def test_minimal_config_ok():
    cfg = load_yaml_text(_minimal_yaml())
    assert cfg.device_ids() == ["cam1"]
    assert cfg.auth.enabled is False


def test_empty_yaml_rejected():
    with pytest.raises(ConfigError):
        load_yaml_text("")


def test_top_level_must_be_mapping():
    with pytest.raises(ConfigError):
        load_yaml_text("- a\n- b\n")


# ---------------------------------------------------------------------------
# §3.3 未知配置项默认拒绝
# ---------------------------------------------------------------------------

def test_unknown_top_level_rejected():
    with pytest.raises(ConfigError, match="Extra inputs"):
        load_yaml_text(_minimal_yaml(unknown_section={"a": 1}))


def test_unknown_nested_rejected():
    with pytest.raises(ConfigError):
        # upload 对象内含未知字段
        import yaml as _y
        data = {
            "version": 1, "auth": {"enabled": False},
            "devices": [{"id": "a"}], "storage": {"root": "/tmp"},
            "upload": {"max_speed": 100},  # config-spec §3.3 示例
        }
        load_yaml_text(_y.safe_dump(data, sort_keys=False))


# ---------------------------------------------------------------------------
# §56 必须项
# ---------------------------------------------------------------------------

def test_devices_required():
    import yaml as _y
    data = {
        "version": 1, "auth": {"enabled": False},
        "storage": {"root": "/tmp"},
    }
    with pytest.raises(ConfigError, match="devices"):
        load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_empty_devices_rejected():
    with pytest.raises(ConfigError, match="必须至少包含一个已注册设备"):
        load_yaml_text(_minimal_yaml(devices=[]))


def test_version_must_be_1():
    with pytest.raises(ConfigError, match="version"):
        load_yaml_text(_minimal_yaml(version=2))


# ---------------------------------------------------------------------------
# auth 业务规则（spec §56 * ：enabled=true 时凭据必须）
# ---------------------------------------------------------------------------

def test_auth_enabled_requires_creds():
    with pytest.raises(ConfigError, match="username"):
        import yaml as _y
        data = {
            "version": 1, "auth": {"enabled": True},  # 缺 username/hash
            "devices": [{"id": "a"}], "storage": {"root": "/tmp"},
        }
        load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_auth_disabled_allows_no_creds():
    cfg = load_yaml_text(_minimal_yaml())
    assert cfg.auth.enabled is False


# ---------------------------------------------------------------------------
# §3.2 / §44 各类取值范围校验
# ---------------------------------------------------------------------------

def test_disk_threshold_order_violated():
    with pytest.raises(ConfigError, match="warning_percent"):
        import yaml as _y
        data = {
            "version": 1, "auth": {"enabled": False},
            "devices": [{"id": "a"}], "storage": {"root": "/tmp"},
            "disk": {"warning_percent": 90, "critical_percent": 80, "stop_percent": 95},
        }
        load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_invalid_date_priority_rejected():
    with pytest.raises(ConfigError, match="date_priority"):
        import yaml as _y
        data = {
            "version": 1, "auth": {"enabled": False},
            "devices": [{"id": "a"}], "storage": {"root": "/tmp"},
            "metadata": {"date_priority": ["DateTimeOriginal", "BogusTag"]},
        }
        load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_worker_count_over_one_rejected():
    with pytest.raises(ConfigError, match="worker"):
        import yaml as _y
        data = {
            "version": 1, "auth": {"enabled": False},
            "devices": [{"id": "a"}], "storage": {"root": "/tmp"},
            "processing": {"worker_count": 4},
        }
        load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_missing_config_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "nope.yaml")


def test_bad_yaml_raises_config_error(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML 格式错误"):
        load_config(f)


# ---------------------------------------------------------------------------
# §63.3 thumbnails（可选顶层段，V1.2 扩展）
# ---------------------------------------------------------------------------

def test_thumbnails_defaults_and_resolution():
    cfg = load_yaml_text(_minimal_yaml())
    th = cfg.thumbnails
    assert th.cache_dir == "thumbnails"
    assert th.cleanup.enabled is True
    assert th.cleanup.max_age_days == 90          # 默认清理 90 天前的缓存
    assert cfg.resolved_thumbnails() == Path("/tmp/pg") / "thumbnails"


def test_thumbnails_custom_cache_dir_and_cleanup():
    import yaml as _y
    data = {
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "a"}], "storage": {"root": "/mnt/pg"},
        "thumbnails": {"cache_dir": "cache/thumb",
                       "cleanup": {"enabled": False, "max_age_days": 30}},
    }
    cfg = load_yaml_text(_y.safe_dump(data, sort_keys=False))
    assert cfg.thumbnails.cache_dir == "cache/thumb"
    assert cfg.thumbnails.cleanup.max_age_days == 30
    assert cfg.thumbnails.cleanup.enabled is False
    assert cfg.resolved_thumbnails() == Path("/mnt/pg") / "cache" / "thumb"


def test_thumbnails_absolute_cache_dir_used_directly():
    import yaml as _y
    data = {
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "a"}], "storage": {"root": "/mnt/pg"},
        "thumbnails": {"cache_dir": "/srv/photo-cache/thumb"},
    }
    cfg = load_yaml_text(_y.safe_dump(data, sort_keys=False))
    assert cfg.resolved_thumbnails() == Path("/srv/photo-cache/thumb")


def test_thumbnails_bad_values_rejected():
    import yaml as _y
    base = {"version": 1, "auth": {"enabled": False},
            "devices": [{"id": "a"}], "storage": {"root": "/tmp"}}
    with pytest.raises(ConfigError, match="cache_dir"):     # 空白 cache_dir
        load_yaml_text(_y.safe_dump(
            {**base, "thumbnails": {"cache_dir": "   "}}, sort_keys=False))
    with pytest.raises(ConfigError, match="max_age_days"):  # 必须 >= 1
        load_yaml_text(_y.safe_dump(
            {**base, "thumbnails": {"cleanup": {"max_age_days": 0}}}, sort_keys=False))
    with pytest.raises(ConfigError):                        # §3.3 未知子键拒绝
        load_yaml_text(_y.safe_dump(
            {**base, "thumbnails": {"unknown": 1}}, sort_keys=False))
