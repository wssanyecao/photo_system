"""V1 配置加载与校验。

实现依据：`docs/photo-gateway-v1-config-spec.md` (Status: Frozen Baseline)。

设计要点（对应 spec §2 / §3）：
- 格式：YAML（spec §2，非 TOML）。
- 所有业务默认值必须显式声明（§3.2）→ 见各 Field(default=...)。
- 未知配置项默认拒绝（§3.3）→ Pydantic `extra="forbid"`。
- 启动时必须完整校验，任何致命错误不得启动（§3.4）→ `load_config()` 抛异常。
- 配置(应该怎么运行)与 SQLite 状态(运行到哪)分离（§3.1）：本模块不触任何运行时状态。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# 配置 Schema（顶层结构固定，逐项对齐 config-spec §55 结构图 / §56 总表）
# ---------------------------------------------------------------------------


class _DeviceConfig(BaseModel):
    """已注册照片来源设备。

    语义依据 v1-spec §10~§11 与 config-spec §8~§10：
    设备只允许来自配置文件，用户不能自由填写 source_device。
    """

    model_config = {"extra": "forbid"}

    id: str
    name: str = ""
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("devices[].id 不能为空")
        # spec：设备 ID 用于稳定关联；禁止空白/路径分隔符等进入归档逻辑
        if re.search(r"[\s/\\:?#\[\]@]", v):
            raise ValueError(f"devices[].id 含不允许字符: {v!r}")
        return v

    @field_validator("name")
    @classmethod
    def _default_name_from_id(cls, v: str, info) -> str:
        # 名称缺省时回退到 id，保证设备始终有展示名（spec 总表 name 必须）
        if not v:
            return str(info.data.get("id", ""))
        return v.strip()


class _StorageConfig(BaseModel):
    """本地存储目录（config-spec §13~§22）。

    子目录若为相对路径，均解析到 root 之下（config-spec 默认值语义）。
    """

    model_config = {"extra": "forbid"}

    root: str
    incoming: str = "incoming"
    processing: str = "processing"
    failed: str = "failed"
    archive: str = "Photos"
    database: str = "database/photo-gateway.db"
    logs: str = "logs"

    def resolve(self, sub: str) -> Path:
        """把某子目录配置项解析为绝对路径并返回（不创建）。"""
        p = Path(sub).expanduser()
        if not p.is_absolute():
            p = Path(self.root).expanduser() / p
        return p

    def resolved_incoming(self) -> Path:
        return self.resolve(self.incoming)

    def resolved_processing(self) -> Path:
        return self.resolve(self.processing)

    def resolved_failed(self) -> Path:
        return self.resolve(self.failed)

    def resolved_archive(self) -> Path:
        return self.resolve(self.archive)

    def resolved_database(self) -> Path:
        return self.resolve(self.database)

    def resolved_logs(self) -> Path:
        return self.resolve(self.logs)


# spec 推荐扩展名集合（config-spec §4 基线 + §28；视频支持见 config-spec §63.4）
DEFAULT_ALLOWED_EXTENSIONS: List[str] = [
    "jpg", "jpeg", "png", "heic", "heif", "webp",
    "tif", "tiff", "cr2", "cr3", "nef", "arw", "dng", "raf", "orf", "rw2",
    # 视频（上传/归档/同步；网页内预览播放见 README“视频”说明）
    "mp4", "mov", "m4v", "3gp", "webm", "mkv", "avi",
]

# spec 日期优先级默认（config-spec §35 / v1-spec §30）
DEFAULT_DATE_PRIORITY: List[str] = [
    "DateTimeOriginal", "CreateDate", "ModifyDate", "file_mtime",
]

_VALID_DATE_SOURCES = {"DateTimeOriginal", "CreateDate", "ModifyDate", "file_mtime"}


class _UploadConfig(BaseModel):
    model_config = {"extra": "forbid"}

    concurrency: int = 1
    precheck: "_PrecheckConfig" = Field(default_factory=lambda: _PrecheckConfig())
    allowed_extensions: List[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_EXTENSIONS))
    tmp_cleanup: "_TmpCleanupConfig" = Field(default_factory=lambda: _TmpCleanupConfig())

    @field_validator("concurrency")
    @classmethod
    def _validate_concurrency(cls, v: int) -> int:
        if v < 1:
            raise ValueError("upload.concurrency 必须 >= 1")
        return v


class _PrecheckConfig(BaseModel):
    model_config = {"extra": "forbid"}

    batch_size: int = 5

    @field_validator("batch_size")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("upload.precheck.batch_size 必须 >= 1")
        return v


class _TmpCleanupConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    max_age_days: int = 7

    @field_validator("max_age_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("upload.tmp_cleanup.max_age_days 必须 >= 1")
        return v


class _ProcessingConfig(BaseModel):
    model_config = {"extra": "forbid"}

    worker_count: int = 1

    @field_validator("worker_count")
    @classmethod
    def _validate_worker_count(cls, v: int) -> int:
        if v != 1:
            # v1-spec §42 / spec 冻结：默认单 worker、顺序处理；当前只支持 1
            raise ValueError("processing.worker_count 当前仅支持 1 (V1 单 worker 冻结)")
        return v


class _MetadataConfig(BaseModel):
    model_config = {"extra": "forbid"}

    date_priority: List[str] = Field(default_factory=lambda: list(DEFAULT_DATE_PRIORITY))

    @field_validator("date_priority")
    @classmethod
    def _validate_priority(cls, vals: List[str]) -> List[str]:
        invalid = [x for x in vals if x not in _VALID_DATE_SOURCES]
        if invalid:
            raise ValueError(
                f"metadata.date_priority 含无效来源: {invalid}；"
                f"合法值: {sorted(_VALID_DATE_SOURCES)}"
            )
        seen = set()
        for x in vals:
            if x in seen:
                raise ValueError(f"metadata.date_priority 重复: {x}")
            seen.add(x)
        if not vals:
            raise ValueError("metadata.date_priority 不能为空")
        return vals


class _DiskConfig(BaseModel):
    model_config = {"extra": "forbid"}

    warning_percent: int = 80
    critical_percent: int = 90
    stop_percent: int = 95

    @model_validator(mode="after")
    def _check_order(self) -> "_DiskConfig":
        # config-spec §44：磁盘阈值之间必须单调（warning < critical < stop），并有安全余量
        if not (0 < self.warning_percent < self.critical_percent < self.stop_percent <= 100):
            raise ValueError(
                "磁盘阈值必须满足 0 < warning_percent < critical_percent "
                "< stop_percent <= 100"
            )
        return self


class _RotationConfig(BaseModel):
    model_config = {"extra": "forbid"}

    max_size_mb: int = 50
    retention_days: int = 30

    @field_validator("max_size_mb", "retention_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("logging.rotation 值必须 >= 1")
        return v


class _LoggingConfig(BaseModel):
    model_config = {"extra": "forbid"}

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    rotation: _RotationConfig = Field(default_factory=_RotationConfig)


class _AuthConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    username: Optional[str] = None
    password_hash: Optional[str] = None
    session_ttl_hours: int = 24

    @field_validator("session_ttl_hours")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("auth.session_ttl_hours 必须 >= 1")
        return v

    @model_validator(mode="after")
    def _require_creds_when_enabled(self) -> "_AuthConfig":
        if self.enabled and (not self.username or not self.password_hash):
            raise ValueError(
                "auth.enabled=true 时 auth.username 和 auth.password_hash 必须存在 (spec §56 *)"
            )
        return self


class _ServerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    host: str = "0.0.0.0"
    port: int = 8080

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if not (0 < v < 65536):
            raise ValueError(f"server.port 越界: {v}")
        return v


# ---------------------------------------------------------------------------
# V2 sync 配置（可选；仅在提供 `sync` 块时启用，字段来源 v2-spec §41）
# ---------------------------------------------------------------------------
class _SyncNas(BaseModel):
    model_config = {"extra": "forbid"}

    host: Optional[str] = None
    ssh_port: int = 22
    ssh_user: Optional[str] = None
    ssh_key: Optional[str] = None
    target_root: Optional[str] = None

    @field_validator("ssh_port")
    @classmethod
    def _positive_port(cls, v: int) -> int:
        if not (0 < v < 65536):
            raise ValueError(f"nas.ssh_port 越界: {v}")
        return v


class _SyncSource(BaseModel):
    model_config = {"extra": "forbid"}

    root: Optional[str] = None


class _SyncWorkerCfg(BaseModel):
    model_config = {"extra": "forbid"}

    cycle_interval_seconds: int = 300

    @field_validator("cycle_interval_seconds")
    @classmethod
    def _nonneg(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("worker.cycle_interval_seconds 必须 > 0")
        return v


class _SyncFailureCfg(BaseModel):
    model_config = {"extra": "forbid"}

    threshold: int = 3

    @field_validator("threshold")
    @classmethod
    def _nonneg(cls, v: int) -> int:
        if v < 1:
            raise ValueError("failure.threshold 必须 >= 1")
        return v


class _SyncCleanupCfg(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    synced_retention_days: int = 90

    @field_validator("synced_retention_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("cleanup.synced_retention_days 必须 >= 1")
        return v


class _SyncConfig(BaseModel):
    """V2 同步配置根（sync.*）。默认整块缺省=不启用。"""

    model_config = {"extra": "forbid"}

    enabled: bool = False
    nas: _SyncNas = Field(default_factory=_SyncNas)
    source: _SyncSource = Field(default_factory=_SyncSource)
    worker: _SyncWorkerCfg = Field(default_factory=_SyncWorkerCfg)
    failure: _SyncFailureCfg = Field(default_factory=_SyncFailureCfg)
    cleanup: _SyncCleanupCfg = Field(default_factory=_SyncCleanupCfg)

    @model_validator(mode="after")
    def _require_nas_when_enabled(self) -> "_SyncConfig":
        if self.enabled:
            nas = self.nas
            missing = [k for k in ("host", "ssh_user", "ssh_key", "target_root")
                       if not getattr(nas, k)]
            if missing:
                raise ValueError(f"sync.enabled=true 时 sync.nas 必填缺失: {missing}")
        return self


class _WebuiConfig(BaseModel):
    """WebUI 偏好（可选；用于照片页默认每页张数等）。"""

    model_config = {"extra": "forbid"}

    photos_page_size: int = 12

    @field_validator("photos_page_size")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1 or v > 200:
            raise ValueError("webui.photos_page_size 必须在 1..200")
        return v


class _ThumbnailsCleanupCfg(BaseModel):
    """缩略图缓存过期清理（config-spec §63.3；默认清理 90 天前的缓存）。"""

    model_config = {"extra": "forbid"}

    enabled: bool = True
    max_age_days: int = 90

    @field_validator("max_age_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("thumbnails.cleanup.max_age_days 必须 >= 1")
        return v


class _ThumbnailsConfig(BaseModel):
    """缩略图缓存策略（可选顶层段；未提供时使用默认值，行为与 V1.1 一致）。

    依据 config-spec §63.3：缓存属非正式库，可随时删除重建（api-spec §88.3）。
    cache_dir 语义同 storage 子目录：相对路径解析到 storage.root 之下，绝对路径直接使用。
    """

    model_config = {"extra": "forbid"}

    cache_dir: str = "thumbnails"
    cleanup: _ThumbnailsCleanupCfg = Field(default_factory=_ThumbnailsCleanupCfg)

    @field_validator("cache_dir")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("thumbnails.cache_dir 不能为空")
        return v

    def resolve(self, root: str | Path) -> Path:
        """把 cache_dir 解析为绝对路径（相对 storage.root 或绝对；不创建）。"""
        p = Path(self.cache_dir).expanduser()
        if not p.is_absolute():
            p = Path(root).expanduser() / p
        return p


class V1Config(BaseModel):
    """V1 完整配置（顶层），与 config-spec §55 结构对应；`sync` 为 V2 可选块、`webui`/`thumbnails` 为可选段。"""

    model_config = {"extra": "forbid"}

    version: int = 1
    server: _ServerConfig = Field(default_factory=_ServerConfig)
    auth: _AuthConfig = Field(default_factory=_AuthConfig)
    devices: List[_DeviceConfig]
    storage: _StorageConfig
    upload: _UploadConfig = Field(default_factory=_UploadConfig)
    processing: _ProcessingConfig = Field(default_factory=_ProcessingConfig)
    metadata: _MetadataConfig = Field(default_factory=_MetadataConfig)
    disk: _DiskConfig = Field(default_factory=_DiskConfig)
    logging: _LoggingConfig = Field(default_factory=_LoggingConfig)
    # WebUI 偏好（缺省使用默认值）
    webui: _WebuiConfig = Field(default_factory=_WebuiConfig)
    # 缩略图缓存（可选顶层段，缺省使用默认值；config-spec §63.3）
    thumbnails: _ThumbnailsConfig = Field(default_factory=_ThumbnailsConfig)
    # V2（可选）：不提供 sync → None；提供时强校验
    sync: Optional[_SyncConfig] = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"不支持的配置 version={v}，当前支持 version=1")
        return v

    @model_validator(mode="after")
    def _require_devices(self) -> "V1Config":
        if not self.devices:
            raise ValueError("devices 必须至少包含一个已注册设备 (spec §56 devices 必须)")
        return self

    def device_ids(self) -> List[str]:
        return [d.id for d in self.devices]

    def enabled_device_ids(self) -> List[str]:
        return [d.id for d in self.devices if d.enabled]

    def resolved_thumbnails(self) -> Path:
        """缩略图缓存目录（config-spec §63.3；相对 storage.root 解析，不创建）。"""
        return self.thumbnails.resolve(self.storage.root)


class ConfigError(Exception):
    """一切配置层面的致命错误。启动遇到一律终止（spec §3.4）。"""


def load_yaml_text(text: str) -> V1Config:
    """从 YAML 文本加载并强校验，返回 V1Config。校验失败抛 ConfigError。"""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 格式错误: {exc}") from exc

    if raw is None:
        raise ConfigError("配置文件为空")

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是一个映射(对象)，实际类型: {type(raw).__name__}")

    try:
        return V1Config.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError / ValueError 统一包装
        raise ConfigError(f"配置校验失败: {exc}") from exc


def load_config(path: str | Path) -> V1Config:
    """从文件系统加载、解析、强校验配置。失败一律抛 ConfigError。"""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"配置文件不存在: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败: {p}: {exc}") from exc
    return load_yaml_text(text)
