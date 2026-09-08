"""存储目录就绪逻辑测试：config-spec §22 不变量。"""

from __future__ import annotations

import pytest

from photo_gateway import config as cfgmod
from photo_gateway.config import ConfigError, load_yaml_text
from photo_gateway.directories import ensure_storage_dirs


def _cfg(root, **extra):
    import yaml as _y
    data = {
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "a"}],
        "storage": {"root": str(root)},
    }
    data.update(extra)
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


def test_root_missing_raises_and_is_not_created(tmp_path):
    missing = tmp_path / "no-such-store"
    with pytest.raises(ConfigError, match="storage.root 不存在"):
        ensure_storage_dirs(_cfg(missing))
    assert not missing.exists()  # 绝不自动创建模拟磁盘


def test_root_is_file_raises(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是目录"):
        ensure_storage_dirs(_cfg(f))


def test_creates_subdirs_and_db_parent(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    res = ensure_storage_dirs(_cfg(root))
    for key in ("incoming", "processing", "failed", "archive", "logs"):
        assert res[key].is_dir(), key
    assert res["database"].parent.is_dir()
    # archive==Photos 名
    assert res["archive"].name == "Photos"
    assert res["database"].name == "photo-gateway.db"
