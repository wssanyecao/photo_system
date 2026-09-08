"""V1 结构化日志体系。

实现依据：v1-spec §18(五类日志) / §19(统一字段) / §20(轮转) / §21(与数据库职责)。

- 域 → 文件：application→application.log, upload→upload.log,
  processing→processing.log, audit→audit.log；error.log 聚合全系统 ERROR/CRITICAL。
- 格式：每行一个 JSON 对象；字段对齐 spec §19
  (timestamp/level/module/event/session_id/photo_id/source_device/filename/message/error)，
  缺失字段不出现在该行（spec：不必每条都含全部字段）。
- 时间：统一上海时区(UTC+8 固定、无夏令时)，见 README §20。
- 轮转：按文件大小(RotatingFileHandler,max_bytes=config) + 保留最近 retention_days 个文件
  (backupCount)。按天归档的二级精化列入 Milestone C5(可靠性) 处理。
- 日志=状态变化过程；不替代 SQLite 当前状态(spec §21)。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 上海 = UTC+8 固定，无夏令时：避免 tzdata 依赖
SHANGHAI_TZ = timezone(timedelta(hours=8))

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# domain → 文件名（v1-spec §18.x）
DOMAIN_FILE = {
    "application": "application.log",
    "upload": "upload.log",
    "processing": "processing.log",
    "audit": "audit.log",
    "error": "error.log",
}


def _iso_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")


class JsonFormatter(logging.Formatter):
    """将 LogRecord 渲染为单行 JSON，带 spec §19 上下文字段。"""

    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "ctx", None) or {}
        entry = {
            "timestamp": _iso_ts(record.created),
            "level": record.levelname,
            "module": ctx.get("module") or record.name.split(".")[-1],
            "event": ctx.get("event") or None,
            "session_id": ctx.get("session_id"),
            "photo_id": ctx.get("photo_id"),
            "source_device": ctx.get("source_device"),
            "filename": ctx.get("filename"),
            "message": record.getMessage(),
            "error": self._err_text(record),
        }
        return json.dumps({k: v for k, v in entry.items() if v is not None}, ensure_ascii=False)

    @staticmethod
    def _err_text(record: logging.LogRecord) -> str | None:
        if record.exc_info:
            return repr(record.exc_info[1])
        return None


class _OnlyErrors(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


# 占位大小：RotatingFileHandler 需要正整数 maxBytes
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # spec logging.rotation.max_size_mb 默认 50MB


class LoggingManager:
    """装配并持有五个域级 logger。应用启动处构建，之后经 logger() 取用。"""

    def __init__(
        self,
        log_dir: Path,
        level: str = "INFO",
        retention_days: int = 30,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.level = _LEVELS.get(level.upper(), logging.INFO)
        self.retention_days = max(1, int(retention_days))
        self.max_bytes = max(1024, int(max_bytes))
        logging.setLoggerClass(PgLogger)
        self._loggers: dict[str, PgLogger] = {}

    # -- 装配 ---------------------------------------------------------------
    def configure(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._install()

    # 实际安装
    def _install(self) -> None:
        import logging as _logging
        # 先清掉域 logger 可能的历史 handlers（configure 反复调用幂等）
        for domain in DOMAIN_FILE:
            lg = _logging.getLogger(f"photo_gateway.{domain}")
            for h in list(lg.handlers):
                lg.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass

        for domain, filename in DOMAIN_FILE.items():
            lg = _logging.getLogger(f"photo_gateway.{domain}")
            lg.setLevel(self.level)
            lg.propagate = False

            full = self.log_dir / filename
            primary = logging.handlers.RotatingFileHandler(
                full, maxBytes=self.max_bytes,
                backupCount=self.retention_days, encoding="utf-8",
            )
            primary.setLevel(self.level if domain != "error" else logging.ERROR)
            primary.setFormatter(JsonFormatter())
            lg.addHandler(primary)

            if domain != "error":
                # 非 error 域额外把 ERROR/CRITICAL 镜像到 error.log
                err_handler = logging.handlers.RotatingFileHandler(
                    self.log_dir / DOMAIN_FILE["error"],
                    maxBytes=self.max_bytes,
                    backupCount=self.retention_days, encoding="utf-8",
                )
                err_handler.setLevel(logging.ERROR)
                err_handler.addFilter(_OnlyErrors())
                err_handler.setFormatter(JsonFormatter())
                lg.addHandler(err_handler)

            self._loggers[domain] = lg  # type: ignore[assignment]

    # -- 取用 ----------------------------------------------------------------
    def logger(self, domain: str) -> "PgLogger":
        if domain not in DOMAIN_FILE:
            raise ValueError(f"未知日志域 {domain!r}; 可选 {list(DOMAIN_FILE)}")
        # lazy: 若尚未安装(直接调 logger)先补建一份内存 logger 以便容忍
        if domain not in self._loggers:
            lg = logging.getLogger(f"photo_gateway.{domain}")
            lg.setLevel(self.level)
            lg.propagate = False
            self._loggers[domain] = lg  # type: ignore[assignment]
        return self._loggers[domain]  # type: ignore[return-value]

    def application(self) -> "PgLogger":
        return self.logger("application")

    def upload(self) -> "PgLogger":
        return self.logger("upload")

    def processing(self) -> "PgLogger":
        return self.logger("processing")

    def audit(self) -> "PgLogger":
        return self.logger("audit")

    def error(self) -> "PgLogger":
        return self.logger("error")


class PgLogger(logging.Logger):
    """域日志，支持以关键字附带 spec §19 结构字段。

        log.info("upload finished",
                 event="upload_completed", session_id="s1", photo_id=1,
                 source_device="xiaomi14", filename="a.jpg")
    """

    # 结构化字段：event/session_id/photo_id/source_device/filename/module... 直接以关键字传入。
    #   例如 log.info("upload finished", event="upload_completed", session_id="s1", photo_id=1)
    def _emit(self, lvl: int, msg, exc_info, *args, **fields) -> None:
        extra = {"ctx": fields}
        super()._log(lvl, msg, args, extra=extra, exc_info=exc_info)

    def debug(self, msg, *args, exc_info=None, **fields):
        self._emit(logging.DEBUG, msg, exc_info, *args, **fields)

    def info(self, msg, *args, exc_info=None, **fields):
        self._emit(logging.INFO, msg, exc_info, *args, **fields)

    def warning(self, msg, *args, exc_info=None, **fields):
        self._emit(logging.WARNING, msg, exc_info, *args, **fields)

    def error(self, msg, *args, exc_info=None, **fields):
        self._emit(logging.ERROR, msg, exc_info, *args, **fields)

    def critical(self, msg, *args, exc_info=None, **fields):
        self._emit(logging.CRITICAL, msg, exc_info, *args, **fields)


def build_logging(log_dir: Path, level="INFO", retention_days=30, max_bytes=None) -> LoggingManager:
    """便捷构建 + configure，供应用/测试装配。"""
    if max_bytes is None:
        max_bytes = _DEFAULT_MAX_BYTES
    m = LoggingManager(log_dir, level=level, retention_days=retention_days, max_bytes=max_bytes)
    m.configure()
    return m
