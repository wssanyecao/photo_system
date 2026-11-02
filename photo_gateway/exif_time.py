"""人工确认的上传 EXIF 时间补全：仅改写工作副本，不修改客户端原片。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# P0 支持 JPEG/JPG；其他格式在前端明确提示并保留原样上传。
WRITABLE_EXTENSIONS = {".jpg", ".jpeg"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_DATE_TAGS = ("DateTimeOriginal", "CreateDate", "ModifyDate")


class ExifTimeError(RuntimeError):
    """无法安全写入或回读 EXIF 时间。"""


def validate_confirmed_time(value: str) -> str:
    """仅接受本地墙钟时间 YYYY-MM-DDTHH:MM:SS，不接受时区/模糊值。"""
    if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
        raise ValueError("EXIF 时间须为 YYYY-MM-DDTHH:MM:SS")
    try:
        date = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ValueError("EXIF 时间不是有效日历日期") from exc
    if not 1900 <= date.year <= 2100:
        raise ValueError("EXIF 时间超出 1900—2100 年范围")
    return date.strftime("%Y:%m:%d %H:%M:%S")


def _exec(args: list[str], *, timeout: int = 30) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise ExifTimeError(f"ExifTool 无法执行: {type(exc).__name__}") from exc
    if result.returncode:
        raise ExifTimeError(f"ExifTool 执行失败: {result.stderr.strip()[:240]}")
    return result.stdout


def read_exif_times(path: Path) -> dict[str, str | None]:
    """ExifTool 读取三个真实 EXIF 时间字段，缺失/非法均记为 None。"""
    # 同名 XMP/IPTC 标签不代表真正的 EXIF，必须限定命名空间。
    result = _exec(["exiftool", "-j", "-EXIF:DateTimeOriginal", "-EXIF:CreateDate",
                    "-EXIF:ModifyDate", "--", str(path)])
    try:
        data = json.loads(result)[0]
    except (IndexError, TypeError, ValueError, KeyError) as exc:
        raise ExifTimeError("ExifTool 返回的数据无法解析") from exc
    fields = {}
    for tag in _DATE_TAGS:
        value = data.get(tag)
        if isinstance(value, str):
            try:
                validate_confirmed_time(value.replace(":", "-", 2).replace(" ", "T", 1))
                fields[tag] = value
                continue
            except ValueError:
                pass
        fields[tag] = None
    return fields


def make_updated_copy(src: Path, item_id: int, confirmed: str) -> Path:
    """以原文件创建安全工作副本，仅补不存在/无效的时间字段，回读验证成功后返回副本。

    调用方负责 os.replace(copy, src)；失败时原始 src 不受影响。
    """
    if src.suffix.lower() not in WRITABLE_EXTENSIONS:
        raise ExifTimeError("当前仅支持为 JPEG/JPG 写入 EXIF 时间")
    target = validate_confirmed_time(confirmed)
    before = read_exif_times(src)
    needs = [tag for tag, value in before.items() if value is None]
    if not needs:
        return src
    # 与上传 staging 一样避免把完整原名再次扩展到文件系统上限之外。
    stage = src.with_name(f".exif-{item_id}.tmp")
    if stage.exists():
        stage.unlink()
    try:
        shutil.copy2(src, stage)
        args = ["exiftool", "-overwrite_original", "-P"]
        args.extend(f"-EXIF:{tag}={target}" for tag in needs)
        args.extend(["--", str(stage)])
        _exec(args, timeout=90)
        after = read_exif_times(stage)
        for tag in _DATE_TAGS:
            expected = target if tag in needs else before[tag]
            if after[tag] != expected:
                raise ExifTimeError(f"EXIF 回读不一致: {tag}")
        return stage
    except Exception:
        stage.unlink(missing_ok=True)
        raise
