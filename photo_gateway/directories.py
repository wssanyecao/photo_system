"""V1 存储目录就绪。

依据 config-spec §22 / v1-spec §5：
- storage.root 对应的存储必须在程序启动前已存在；程序不得自动 mkdir 模拟存储(root 不存在→启动失败，
  防 USB HDD 掉线后误写入系统 Flash)。
- root 之下的业务子目录 (incoming/processing/failed/Photos/database/logs) 首启可自动创建。
- 原始照片目录 Photos 顶层由此创建； Photos/YYYY/YYYYMM 由归档流程按需生成。

本模块只负责"确保 worker/HTTP 能安全拿到齐全目录结构"。
"""

from __future__ import annotations

from pathlib import Path

from .config import ConfigError, V1Config


def ensure_storage_dirs(cfg: V1Config) -> dict[str, Path]:
    """校验 root 透传并确保子目录就绪，返回 {语义名: 绝对Path}。

    若 root 不存在或不是目录 → 抛 ConfigError（绝不自动替代挂载点，见 config-spec §22）。
    """
    s = cfg.storage
    root = Path(s.root).expanduser()
    if not root.exists():
        raise ConfigError(
            f"storage.root 不存在: {root}。"
            "存储必须预先就位，程序不会为模拟磁盘而自动创建普通目录 (config-spec §22)。"
        )
    if not root.is_dir():
        raise ConfigError(f"storage.root 不是目录: {root}")

    def _mk(d: Path) -> Path:
        d = d.expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d

    # database 是文件路径，确保其父目录
    db_path = s.resolved_database()

    resolved = {
        "root": root,
        "incoming": _mk(s.resolved_incoming()),
        "processing": _mk(s.resolved_processing()),
        "failed": _mk(s.resolved_failed()),
        "archive": _mk(s.resolved_archive()),
        "database": db_path,
        "logs": _mk(s.resolved_logs()),
    }
    if db_path.parent:
        _mk(db_path.parent)
    return resolved
