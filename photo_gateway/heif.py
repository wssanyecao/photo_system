"""HEIC/HEIF 解码支持（运行依赖 pillow-heif）。

Pillow 默认无法解析 HEIC/HEIF（iPhone/微信实拍常见格式）；项目依赖安装会同时安装：
    pip install pillow-heif
然后调用 register() 注册解码器，Pillow 即可读取 HEIC（EXIF/缩略图/JPEG 预览都能用）。

注册失败时返回 False 并记录日志；调用方仍保留降级路径，避免单个损坏文件拖垮服务。
"""

from __future__ import annotations

import logging

_LOG = logging.getLogger("photo_gateway.heif")

_registered = False


def register() -> bool:
    """注册 pillow-heif 解码器（幂等）。成功返回 True；注册失败记录并返回 False。"""
    global _registered
    if _registered:
        return True
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        _registered = True
    except Exception as exc:
        _registered = False
        _LOG.error("pillow-heif registration failed: %s", exc, exc_info=True)
    return _registered
