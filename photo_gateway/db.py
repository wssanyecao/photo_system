"""SQLite 访问层（V1）。

对齐 db-spec §92 完整 Schema 及索引。实现要点：
- schema_version 记录当前结构版本；migration runner 幂等、可分步。
- 每个连接启用 foreign_keys 与外键约束（spec 数据一致性），并对冲突开启 busy_timeout。
- 照片相关强不变量（sha256 唯一、文件名不覆盖）最终由 service 遵守；DB 层把唯一约束落到 ux_photo_assets_sha256。
- V2(同步月模型)属另一里程碑，schema 通过新增迁移扩展。

时间统一文本(Y-M-D H:M:S 上海)。service 负责填充，DB 不校验格式。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# 迁移脚本：版本 → 完整 SQL。仅在 schema_version 记录 < 该版本时执行。全程在单事务内跑。
SCHEMA_SCRIPTS: list[tuple[int, str]] = [
    (
        1,
        """
CREATE TABLE schema_version (
    version     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE devices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE upload_sessions (
    id                  TEXT PRIMARY KEY,
    source_device       TEXT NOT NULL,
    client_ip           TEXT,

    created_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,

    total_files         INTEGER NOT NULL DEFAULT 0,
    uploaded_files      INTEGER NOT NULL DEFAULT 0,
    skipped_files       INTEGER NOT NULL DEFAULT 0,
    duplicate_files     INTEGER NOT NULL DEFAULT 0,
    failed_files        INTEGER NOT NULL DEFAULT 0,

    status              TEXT NOT NULL,

    FOREIGN KEY (source_device) REFERENCES devices(id)
);

CREATE TABLE photo_assets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    sha256              TEXT NOT NULL,

    original_filename   TEXT NOT NULL,
    current_filename    TEXT NOT NULL,

    file_size           INTEGER NOT NULL,
    mime_type           TEXT,

    date_taken          TEXT,
    date_source         TEXT,

    exif_json           TEXT,

    archive_path        TEXT NOT NULL,

    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE upload_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,

    session_id            TEXT NOT NULL,
    source_device         TEXT NOT NULL,
    client_item_id        TEXT NOT NULL,

    original_filename     TEXT NOT NULL,
    file_size             INTEGER NOT NULL,

    status                TEXT NOT NULL,

    precheck_status       TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    user_confirmation     INTEGER NOT NULL DEFAULT 0,

    asset_id              INTEGER,

    error_code            TEXT,
    error_message         TEXT,

    created_at            TEXT NOT NULL,
    uploaded_at           TEXT,
    processing_started_at TEXT,
    completed_at          TEXT,
    updated_at            TEXT NOT NULL,

    FOREIGN KEY (session_id)   REFERENCES upload_sessions(id),
    FOREIGN KEY (source_device) REFERENCES devices(id),
    FOREIGN KEY (asset_id)     REFERENCES photo_assets(id),

    UNIQUE (session_id, client_item_id)
);

CREATE TABLE photo_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    upload_item_id  INTEGER NOT NULL,

    event_type      TEXT NOT NULL,

    from_status     TEXT,
    to_status       TEXT,

    message         TEXT,
    details_json    TEXT,

    created_at      TEXT NOT NULL,

    FOREIGN KEY (upload_item_id) REFERENCES upload_items(id)
);

CREATE INDEX idx_upload_items_precheck ON upload_items (
    source_device, original_filename, file_size
);
CREATE INDEX idx_upload_items_session ON upload_items (session_id);
CREATE INDEX idx_upload_items_status  ON upload_items (status);
CREATE INDEX idx_upload_items_asset   ON upload_items (asset_id);
CREATE INDEX idx_photo_assets_date_taken ON photo_assets (date_taken);
CREATE INDEX idx_photo_events_item    ON photo_events (upload_item_id);
CREATE INDEX idx_photo_events_created ON photo_events (created_at);
CREATE UNIQUE INDEX ux_photo_assets_sha256 ON photo_assets (sha256);
""",
    ),
    (
        2,
        """
CREATE TABLE photo_sync_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    status        TEXT NOT NULL,
    task_count    INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE photo_sync_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL,
    archive_month TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    target_path   TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT,
    exit_code     INTEGER,
    stdout        TEXT,
    stderr        TEXT,
    error_message TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES photo_sync_batches(id)
);

CREATE TABLE photo_sync_month_states (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_month             TEXT NOT NULL,
    state                     TEXT NOT NULL,
    consecutive_failed_count  INTEGER NOT NULL DEFAULT 0,
    abnormal_at               TEXT,
    retry_requested_at        TEXT,
    retry_requested_by        TEXT,
    reason                    TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX idx_v2_task_month   ON photo_sync_tasks (archive_month);
CREATE INDEX idx_v2_task_batch   ON photo_sync_tasks (batch_id);
CREATE INDEX idx_v2_state_month  ON photo_sync_month_states (archive_month, created_at DESC, id DESC);
""",
    ),
    # V1.3：资产本地缺失标记（Cleanup 删除/外部删除时置位；NULL=本地文件在）
    (
        3,
        """
ALTER TABLE photo_assets ADD COLUMN file_missing_at TEXT;
""",
    ),
]


# 核心表清单（供测试/后续直接 SQL 使用）
TABLES = [
    "schema_version",
    "devices",
    "upload_sessions",
    "upload_items",
    "photo_assets",
    "photo_events",
]


class DbError(RuntimeError):
    """数据库层错误统一类型。由调用方决定日志/响应。"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并施加每次连接必需的 PRAGMA。不做迁移（由 init_db 负责）。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # 单写者 + 外键数据一致性（db-spec 数据一致核心）
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    """返回当前 schema_version（无表/无记录按 0）。"""
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        return int(row["v"] or 0)
    except sqlite3.OperationalError:
        return 0  # schema_version 还不存在


def migrate(conn: sqlite3.Connection) -> int:
    """把数据库推进到最新 schema。幂等。返回当前(最新)版本。"""
    cur = _current_version(conn)
    for version, sql in SCHEMA_SCRIPTS:
        if version > cur:
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_version (version, updated_at) VALUES (?,?)",
                    (version, _now_str()),
                )
            cur = version
    return cur


def init_db(path: str | Path) -> sqlite3.Connection:
    """打开 + 迁移到最新，返回就绪连接。目录须已由 directories 层保证存在。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(p)
    migrate(conn)
    return conn


def _now_str() -> str:
    from datetime import datetime, timedelta, timezone

    sh = timezone(timedelta(hours=8))
    return datetime.now(sh).strftime("%Y-%m-%d %H:%M:%S")
