"""动态缩略图生成（缓存目录可配置，不改 photo_assets schema）。

依据 webui-spec §52 / api-spec §88.3：提供 GET /photos/{id}/thumbnail 后 WebUI 优先使用缩略图；
生成失败(如 Pillow 不支持该格式)由调用端回退原图。

- 尺寸：最长边 256px，等比；JPEG q82。
- 方向：EXIF orientation 由 Pillow exif_transpose 处理。
- 缓存目录：config-spec §63.3 `thumbnails.cache_dir`（默认 `<storage.root>/thumbnails`）；
  文件 `<cache_dir>/<asset_id>.jpg`（asset 只增不改，ID 可作 key；非正式目录可随时重建）。
- 清理：`cleanup_thumbnails()` 按文件 mtime 删除超过 `max_age_days` 的缓存
  （默认 90 天，规则经 `thumbnails.cleanup` 配置；由 serve 维护任务周期执行，
   见 http._run_maintenance）。
"""

from __future__ import annotations

import time
from pathlib import Path
from . import heif as _heif_support
_heif_support.register()

_THUMBNAIL_MAX = 256
_QUALITY = 82


def thumbnail_file(cache_dir: Path, asset_id: int) -> Path:
    """返回某 asset 的缩略图缓存文件路径（不保证存在）。"""
    return Path(cache_dir) / f"{asset_id}.jpg"


def drop_thumbnail(cache_dir: Path, asset_id: int) -> bool:
    """删除某 asset 的缓存缩略图（原图删除/缺失时清理孤儿缓存）。返回是否实际删除。"""
    p = thumbnail_file(cache_dir, asset_id)
    try:
        if p.exists():
            p.unlink()
            return True
    except OSError:
        pass
    return False


def ensure_thumbnail(cache_dir: Path, asset_id: int, source_path: Path) -> Path | None:
    """生成并缓存缩略图到 cache_dir；返回缓存文件路径，生成失败(不支持/非图)返回 None。"""
    cached = thumbnail_file(cache_dir, asset_id)
    if cached.exists():
        return cached
    try:
        from PIL import Image, ImageOps
        with Image.open(source_path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((_THUMBNAIL_MAX, _THUMBNAIL_MAX))
            cached.parent.mkdir(parents=True, exist_ok=True)
            im.save(cached, "JPEG", quality=_QUALITY, optimize=True)
        return cached
    except Exception:
        # Pillow 不支持/损坏等：回退由调用端决定（返回原图）
        return None


def cleanup_thumbnails(cache_dir: Path, max_age_days: int = 90) -> int:
    """删除 cache_dir 中 mtime 超过 max_age_days 的 `*.jpg` 缓存文件。返回删除数。

    语义：缩略图是非正式缓存（api-spec §88.3），过期删除后可随时重建；
    判定标准与 `reliability.cleanup_tmp` 一致——文件 mtime（生成时间）。
    目录不存在/为空时返回 0。
    """
    if max_age_days < 0:
        raise ValueError("max_age_days 不能为负")
    d = Path(cache_dir)
    if not d.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for p in d.glob("*.jpg"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed
