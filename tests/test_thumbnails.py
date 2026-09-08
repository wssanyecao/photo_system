"""缩略图缓存：目录参数化 + 过期清理(cleanup_thumbnails) + serve 维护轮接线。

对应 api-spec §88.3 / config-spec §63.3；维护轮同时覆盖 upload.tmp_cleanup
的运行时接线（此前该清理只有配置+原语、未真正执行）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml as _y
from PIL import Image

from photo_gateway.config import load_yaml_text
from photo_gateway.http import MAINTENANCE_INTERVAL_SECONDS, _run_maintenance
from photo_gateway.thumbnails import cleanup_thumbnails, ensure_thumbnail, thumbnail_file


def _age_file(p: Path, seconds_old: float) -> None:
    stat = os.stat(p)
    os.utime(p, (stat.st_atime, time.time() - seconds_old))


def _make_source(tmp_path: Path, name: str = "src.jpg") -> Path:
    p = tmp_path / name
    Image.new("RGB", (300, 200), (10, 20, 30)).save(p)
    return p


def _cfg_yaml(root: Path, tmp_cleanup=None, thumbnails=None):
    data = {
        "version": 1, "auth": {"enabled": False},
        "devices": [{"id": "a"}], "storage": {"root": str(root)},
    }
    if tmp_cleanup is not None:
        data["upload"] = {"tmp_cleanup": tmp_cleanup}
    if thumbnails is not None:
        data["thumbnails"] = thumbnails
    return load_yaml_text(_y.safe_dump(data, sort_keys=False))


# ---------------------------------------------------------------------------
# 缓存目录参数化
# ---------------------------------------------------------------------------

def test_ensure_thumbnail_uses_given_cache_dir(tmp_path):
    cache = tmp_path / "my-cache"
    src = _make_source(tmp_path)
    out = ensure_thumbnail(cache, 7, src)
    assert out == thumbnail_file(cache, 7) == cache / "7.jpg"
    assert out.exists()
    assert ensure_thumbnail(cache, 7, src) == out  # 命中缓存，不重复生成


def test_thumbnail_file_path_shape(tmp_path):
    assert thumbnail_file(tmp_path / "t", 123) == tmp_path / "t" / "123.jpg"


# ---------------------------------------------------------------------------
# cleanup_thumbnails 过期清理（默认 90 天）
# ---------------------------------------------------------------------------

def test_cleanup_removes_only_expired_jpg(tmp_path):
    cache = tmp_path / "cache"
    src = _make_source(tmp_path)
    for aid in (1, 2, 3):
        ensure_thumbnail(cache, aid, src)
    (cache / "other.jpg").write_bytes(b"x")  # 目录内其它 jpg 同规则按 mtime
    _age_file(cache / "1.jpg", 91 * 86400)   # > 90 天
    _age_file(cache / "other.jpg", 120 * 86400)
    n = cleanup_thumbnails(cache, max_age_days=90)
    assert n == 2
    assert not (cache / "1.jpg").exists()
    assert not (cache / "other.jpg").exists()
    assert (cache / "2.jpg").exists()        # 新鲜保留
    assert (cache / "3.jpg").exists()


def test_cleanup_absent_or_empty_dir_returns_zero(tmp_path):
    assert cleanup_thumbnails(tmp_path / "absent", 90) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cleanup_thumbnails(empty, 90) == 0


def test_cleanup_rejects_negative_age():
    with pytest.raises(ValueError):
        cleanup_thumbnails(Path("/nope"), max_age_days=-1)


# ---------------------------------------------------------------------------
# serve 维护轮：tmp_cleanup + thumbnails.cleanup 接线
# ---------------------------------------------------------------------------

def test_maintenance_round_cleans_tmp_and_thumbnails(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    cfg = _cfg_yaml(
        tmp_path,
        tmp_cleanup={"enabled": True, "max_age_days": 7},
        thumbnails={"cache_dir": "cache",
                    "cleanup": {"enabled": True, "max_age_days": 90}},
    )
    cache = cfg.resolved_thumbnails()
    src = _make_source(tmp_path)
    ensure_thumbnail(cache, 1, src)
    ensure_thumbnail(cache, 2, src)
    (incoming / "old.tmp").write_bytes(b"x")
    (incoming / "fresh.tmp").write_bytes(b"y")
    _age_file(incoming / "old.tmp", 8 * 86400)
    _age_file(cache / "1.jpg", 91 * 86400)

    res = _run_maintenance(cfg, {"incoming": incoming})
    assert res == {"tmp_removed": 1, "thumbnails_removed": 1}
    assert not (incoming / "old.tmp").exists()
    assert (incoming / "fresh.tmp").exists()
    assert not (cache / "1.jpg").exists()
    assert (cache / "2.jpg").exists()
    assert MAINTENANCE_INTERVAL_SECONDS == 24 * 3600


def test_maintenance_round_skips_when_disabled(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    cfg = _cfg_yaml(
        tmp_path,
        tmp_cleanup={"enabled": False, "max_age_days": 7},
        thumbnails={"cache_dir": "cache",
                    "cleanup": {"enabled": False, "max_age_days": 90}},
    )
    cache = cfg.resolved_thumbnails()
    cache.mkdir(parents=True)
    (incoming / "old.tmp").write_bytes(b"x")
    (cache / "old.jpg").write_bytes(b"y")
    _age_file(incoming / "old.tmp", 30 * 86400)
    _age_file(cache / "old.jpg", 200 * 86400)

    res = _run_maintenance(cfg, {"incoming": incoming})
    assert res == {"tmp_removed": 0, "thumbnails_removed": 0}
    assert (incoming / "old.tmp").exists()   # disabled → 不删
    assert (cache / "old.jpg").exists()
