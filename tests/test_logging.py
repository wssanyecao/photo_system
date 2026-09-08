"""日志体系测试：v1-spec §18/§19/§20 关键不变量。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import logging

from photo_gateway.logging_util import (
    DOMAIN_FILE,
    PgLogger,
    build_logging,
)


def _fresh(tmp_path: Path, level="INFO", retention_days=2):
    m = build_logging(tmp_path, level=level, retention_days=retention_days)
    return m


def test_build_creates_all_five_files(tmp_path):
    build_logging(tmp_path)
    for name in DOMAIN_FILE.values():
        assert (tmp_path / name).exists()


def test_upload_info_yields_json_with_spec_fields_and_shanghai_tz(tmp_path):
    m = build_logging(tmp_path)
    m.upload().info(
        "upload completed",
        event="upload_completed", session_id="20260901-ABCD1234",
        photo_id=10001, source_device="xiaomi14", filename="IMG_001.jpg",
    )
    line = (tmp_path / DOMAIN_FILE["upload"]).read_text(encoding="utf-8").strip()
    obj = json.loads(line.splitlines()[-1])
    assert obj["event"] == "upload_completed"
    assert obj["session_id"] == "20260901-ABCD1234"
    assert obj["photo_id"] == 10001
    assert obj["source_device"] == "xiaomi14"
    assert obj["filename"] == "IMG_001.jpg"
    assert obj["message"] == "upload completed"
    # 上海时区时间戳：UTC+8（+0800）
    assert obj["timestamp"].endswith("+0800")
    assert obj["module"] == "upload"
    assert obj["level"] == "INFO"


def test_error_mirrored_into_error_log_but_info_not(tmp_path):
    m = build_logging(tmp_path)
    m.upload().error("upload failed", event="upload_failed", session_id="s9")
    m.upload().info("ordinary info", event="ping")
    err_text = (tmp_path / DOMAIN_FILE["error"]).read_text(encoding="utf-8")
    up_text = (tmp_path / DOMAIN_FILE["upload"]).read_text(encoding="utf-8")
    # error.log 应含 upload 域 error，不应含那条 info
    assert "upload failed" in err_text
    assert "ordinary info" not in err_text
    # 域文件同时保留自己的 info 与 error
    assert "ordinary info" in up_text
    assert "upload failed" in up_text


def test_error_domain_logged_at_error_level(tmp_path):
    m = build_logging(tmp_path)
    m.error().critical("database panic", event="db_panic")
    text = (tmp_path / DOMAIN_FILE["error"]).read_text(encoding="utf-8")
    assert '"level": "CRITICAL"' in text and "database panic" in text


def test_rotation_backup_count(tmp_path):
    # 用极小 max_bytes 触发轮转
    m = build_logging(tmp_path, retention_days=2)
    # 直接访问其 handler 手动设置低 max_bytes
    lg = m.upload()
    h = [h for h in lg.handlers if getattr(h, "baseFilename", "") .endswith("upload.log")][0]
    h.maxBytes = 200
    for i in range(400):
        lg.info(f"padding-{i:04d}", event="pad")
    # 至少发生轮转：存在 .log.1
    files = {p.name for p in tmp_path.iterdir() if p.name.startswith("upload.log")}
    assert len(files) >= 2


def test_logger_unknown_domain_raises(tmp_path):
    import pytest
    from photo_gateway.logging_util import LoggingManager
    m = build_logging(tmp_path)
    with pytest.raises(ValueError):
        m.logger("nope")
