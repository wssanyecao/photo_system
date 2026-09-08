"""可选 HEIC/HEIF 解码支持（依赖 pillow-heif，非强制）。

Pillow 默认无法解析 HEIC/HEIF（iPhone/微信实拍常见格式）；安装可选依赖后：
    pip install pillow-heif
然后调用 register() 注册解码器，Pillow 即可读取 HEIC（EXIF/缩略图/JPEG 预览都能用）。

未安装时不抛错、返回 False，各调用方按既有行为降级（读取失败 → 归档回退文件时间 /
缩略图回退原图 / 网页显示“无法预览”占位）。不把该依赖写进 requirements，
避免在不支持平台（如部分 OpenWrt/低内存设备）上阻断整体安装。
"""

from __future__ import annotations

_registered = False


def register() -> bool:
    """注册 pillow-heif 解码器（幂等）。成功返回 True；未安装/注册失败返回 False。"""
    global _registered
    if _registered:
        return True
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        _registered = True
    except Exception:
        _registered = False
    return _registered
