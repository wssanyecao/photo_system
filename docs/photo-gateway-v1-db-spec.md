# Photo Gateway V1 Database Specification

**文件名：** `photo-gateway-v1-db-spec.md`  
**版本：** V1.0  
**状态：** Frozen Baseline  
**项目：** Photo Gateway  
**数据库：** SQLite 3

---

## 1. 文档目的

本文档定义 Photo Gateway V1 的数据库结构、字段、索引、状态机、数据关系、事务要求以及数据库生命周期。

数据库负责记录：

- 上传设备
- 上传 Session
- 上传文件记录
- 照片资产
- 文件 SHA-256
- EXIF / 元数据
- Precheck 结果
- 上传及处理状态
- 重复文件关系
- 错误信息
- 重要业务事件

数据库不负责：

- 保存照片二进制内容
- 决定照片目录结构
- 修改照片 EXIF
- 保存 NAS 状态
- 保存 Immich 数据
- 保存人脸识别结果
- 保存视频管理信息

照片仍然以普通文件形式保存。

---

# 2. 核心设计原则

Photo Gateway V1 明确区分：

> **上传行为（Upload）**

和：

> **照片资产（Photo Asset）**

因此不使用单一 `photos` 表同时承担两个职责。

数据库采用：

```text
devices
    │
    └── upload_sessions
            │
            └── upload_items
                    │
                    └── photo_assets
```

其中：

- `devices`：设备身份
- `upload_sessions`：一次批量上传任务
- `upload_items`：一次上传中的单个文件
- `photo_assets`：照片库中的实际照片
- `photo_events`：业务事件历史

---

# 3. 数据库文件

数据库路径由配置文件决定。

示例：

```yaml
storage:
  database: database/photo-gateway.db
```

实际路径：

```text
${storage.root}/${storage.database}
```

例如：

```text
/mnt/sda1/photo-gateway/database/photo-gateway.db
```

---

# 4. SQLite 配置

Photo Gateway V1 使用 SQLite 3。

启动时执行：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

使用 WAL 的目的：

- 允许 WebUI/API 读取数据库时 Worker 继续工作
- 降低读写冲突
- 提高 R5S 上的稳定性

V1 Worker 数量固定为：

```text
1
```

---

# 5. 表结构总览

V1 数据库包含：

```text
photo-gateway.db
│
├── schema_version
├── devices
├── upload_sessions
├── upload_items
├── photo_assets
└── photo_events
```

---

# 6. ER 关系

```text
┌─────────────┐
│   devices   │
└──────┬──────┘
       │
       │ 1:N
       ▼
┌─────────────────┐
│ upload_sessions │
└────────┬────────┘
         │
         │ 1:N
         ▼
┌─────────────────┐
│  upload_items   │
└────────┬────────┘
         │
         │ 0..1
         ▼
┌─────────────────┐
│  photo_assets   │
└─────────────────┘

upload_items
      │
      │ 1:N
      ▼
┌─────────────────┐
│  photo_events   │
└─────────────────┘
```

---

# 7. schema_version

用于数据库 Schema 版本管理。

## 7.1 Schema

```sql
CREATE TABLE schema_version (
    version     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
```

V1：

```text
version = 1
```

---

# 8. devices

设备身份表。

用于保存系统允许上传照片的设备。

## 8.1 Schema

```sql
CREATE TABLE devices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

---

# 9. devices.id

设备唯一 ID。

示例：

```text
xiaomi13ultra
xiaomi14
macbook-air
windows-pc
iphone
```

该 ID 由配置文件定义。

不能由用户在上传时自由输入。

---

# 10. devices.name

设备显示名称。

例如：

```text
小米 13 Ultra
小米 14
MacBook Air
Windows PC
```

用于 WebUI 展示。

---

# 11. devices.type

设备类型。

V1 支持：

```text
android
ios
macos
windows
```

该字段用于描述设备所属平台。

设备定义时已经确定，因此不再在 `upload_sessions` 中保存 `client_type`。

---

# 12. devices.enabled

SQLite Boolean：

```text
0 = disabled
1 = enabled
```

默认：

```text
1
```

禁用设备后：

- 不允许创建新的上传 Session
- 历史上传记录不受影响
- 历史照片不受影响

---

# 13. 设备配置同步

设备定义来源于配置文件。

例如：

```yaml
devices:
  - id: xiaomi14
    name: 小米 14
    type: android
    enabled: true
```

启动时同步到：

```text
devices
```

如果配置中增加设备：

> 自动创建数据库记录。

如果修改设备名称或启用状态：

> 更新数据库。

如果设备从配置中删除：

> 不删除数据库记录。

而是：

```text
enabled = 0
```

避免破坏历史数据。

---

# 14. upload_sessions

表示一次完整的批量上传任务。

例如：

> 用户从小米 14 选择 1000 张照片并点击上传。

产生：

```text
upload_session = UUID
```

## 14.1 Schema

```sql
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

    FOREIGN KEY (source_device)
        REFERENCES devices(id)
);
```

---

# 15. upload_sessions.id

使用 UUID。

例如：

```text
550e8400-e29b-41d4-a716-446655440000
```

代表一次完整上传任务。

---

# 16. upload_sessions.source_device

对应：

```text
devices.id
```

例如：

```text
xiaomi14
```

该字段不能为空。

---

# 17. upload_sessions.client_ip

记录创建 Session 时客户端的 IP。

用途：

- 日志排查
- 安全审计
- 故障定位

不参与去重。

---

# 18. upload_sessions 时间字段

统一使用 UTC ISO-8601。

例如：

```text
2026-09-01T05:30:00Z
```

字段含义：

### created_at

Session 创建时间。

### started_at

第一个文件开始实际上传的时间。

### completed_at

整个 Session 处理完成的时间。

---

# 19. upload_sessions 统计字段

```text
total_files
uploaded_files
skipped_files
duplicate_files
failed_files
```

用于 WebUI 显示批量上传进度。

例如：

```text
总计：1000

已上传：800
疑似重复：150
实际重复：30
失败：20
```

这些字段属于：

> 汇总缓存。

最终准确结果以：

```text
upload_items
```

为准。

如果发生异常，可以重新统计。

---

# 20. Session 状态

V1 支持：

```text
CREATED
UPLOADING
UPLOADED
PROCESSING
COMPLETED
PARTIAL
FAILED
CANCELLED
```

---

# 21. Session 状态说明

## CREATED

Session 已创建，但尚未开始上传。

## UPLOADING

至少存在一个文件正在上传。

## UPLOADED

客户端上传阶段完成。

所有文件已经：

- 上传完成
- 或被 Precheck 跳过

## PROCESSING

Worker 正在处理文件。

## COMPLETED

所有文件处理完成。

## PARTIAL

部分成功、部分失败。

## FAILED

Session 无法继续。

## CANCELLED

用户主动取消。

---

# 22. upload_items

这是：

> **一次上传操作中的单个文件记录。**

例如：

```text
Session A
├── IMG_001.jpg
├── IMG_002.jpg
├── IMG_003.jpg
└── ...
```

每个文件对应一个 `upload_items`。

## 22.1 Schema

```sql
CREATE TABLE upload_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    session_id          TEXT NOT NULL,
    source_device       TEXT NOT NULL,

    original_filename   TEXT NOT NULL,
    file_size           INTEGER NOT NULL,

    status              TEXT NOT NULL,

    precheck_status     TEXT NOT NULL DEFAULT 'NOT_CHECKED',

    reupload_confirmed  INTEGER NOT NULL DEFAULT 0,

    asset_id            INTEGER,

    error_code          TEXT,
    error_message       TEXT,

    created_at          TEXT NOT NULL,
    uploaded_at         TEXT,
    processing_started_at TEXT,
    completed_at        TEXT,
    updated_at          TEXT NOT NULL,

    FOREIGN KEY (session_id)
        REFERENCES upload_sessions(id),

    FOREIGN KEY (source_device)
        REFERENCES devices(id),

    FOREIGN KEY (asset_id)
        REFERENCES photo_assets(id)
);
```

---

# 23. upload_items 与 photo_assets 的区别

这是本数据库最重要的设计。

例如：

第一次上传：

```text
upload_item #100
        │
        ▼
photo_asset #500
```

第二次上传同一张照片：

```text
upload_item #200
        │
        ▼
photo_asset #500
```

因此：

```text
upload_item
```

表示：

> 我曾经尝试上传这个文件。

而：

```text
photo_asset
```

表示：

> 我的照片库中存在这张照片。

---

# 24. original_filename

记录客户端上传时的原始文件名。

例如：

```text
IMG_001.jpg
```

该字段永远保存原始名称。

即使最终因为文件名冲突变成：

```text
IMG_001_1.jpg
```

这里仍然是：

```text
IMG_001.jpg
```

---

# 25. file_size

单位：

```text
bytes
```

必须大于 0。

参与 Precheck：

```text
source_device
+
original_filename
+
file_size
```

---

# 26. upload_items.status

支持：

```text
PRECHECK
UPLOADING
UPLOADED
PROCESSING
COMPLETED
DUPLICATE
FAILED
```

---

# 27. upload_item 状态机

```text
PRECHECK
   │
   ├── POSSIBLE_DUPLICATE
   │
   └── NO_MATCH
          │
          ▼
      UPLOADING
          │
          ▼
       UPLOADED
          │
          ▼
      PROCESSING
        │    │
        │    └── FAILED
        │
        ├── DUPLICATE
        │
        └── COMPLETED
```

---

# 28. PRECHECK

文件尚未真正上传。

服务器仅获得：

```text
source_device
original_filename
file_size
```

然后执行快速预检。

---

# 29. precheck_status

支持：

```text
NOT_CHECKED
NO_MATCH
POSSIBLE_DUPLICATE
USER_CONFIRMED
```

---

# 30. Precheck 的意义

`precheck_status` 是：

> **这一次上传文件的预检查结果。**

它不是索引，也不是为了提高 SQL 查询性能。

例如：

```text
xiaomi14
IMG_001.jpg
12345678
```

数据库发现历史上成功处理过同样的：

```text
source_device
+
original_filename
+
file_size
```

则：

```text
precheck_status = POSSIBLE_DUPLICATE
```

否则：

```text
precheck_status = NO_MATCH
```

---

# 31. Precheck 不等于重复

Precheck 只能判断：

> 疑似已经上传。

不能判断：

> 文件内容一定相同。

最终重复判断必须依靠：

```text
SHA-256
```

---

# 32. Precheck 联合索引

不使用人为拼接的 `precheck_key`。

直接建立联合索引：

```sql
CREATE INDEX idx_upload_items_precheck
ON upload_items (
    source_device,
    original_filename,
    file_size
);
```

SQLite 使用 B-Tree 索引完成快速检索。

---

# 33. Precheck 查询

只对历史上已经成功进入照片库的记录进行判断。

逻辑等价于：

```sql
SELECT ui.id
FROM upload_items ui
JOIN photo_assets pa
  ON ui.asset_id = pa.id
WHERE ui.source_device = ?
  AND ui.original_filename = ?
  AND ui.file_size = ?
LIMIT 1;
```

这里：

```text
photo_assets
```

存在意味着该上传记录最终对应了一个照片资产。

因此：

```text
FAILED
CANCELLED
```

等历史失败记录不会造成误判。

---

# 34. 为什么不使用 precheck_key

不增加：

```text
precheck_key
```

原因：

```text
source_device
+
original_filename
+
file_size
```

建立联合索引已经足够。

自己拼接：

```text
xiaomi14|IMG_001.jpg|12345678
```

不会产生本质性的性能优势，反而：

- 增加编码规则
- 增加特殊字符处理
- 降低可读性
- 不利于后续按单独字段查询

因此 V1 使用标准联合索引。

---

# 35. reupload_confirmed

SQLite Boolean：

```text
0 = false
1 = true
```

默认：

```text
0
```

只有用户在 WebUI 中明确选择：

> 重新上传

才设置：

```text
1
```

---

# 36. 疑似重复的处理

例如一次上传：

```text
1000 张
```

其中：

```text
900 张 POSSIBLE_DUPLICATE
100 张 NO_MATCH
```

系统：

> 不应该因为 900 张疑似重复而阻塞另外 100 张。

100 张正常进入：

```text
incoming/
```

900 张暂时不上传。

Session 完成后，WebUI 展示：

```text
疑似重复照片：900
```

用户可以：

```text
全部重新上传
```

或者：

```text
选择部分重新上传
```

确认后的文件设置：

```text
reupload_confirmed = 1
```

然后才允许进入真正上传阶段。

---

# 37. asset_id

表示：

> 当前上传文件最终对应的照片资产。

例如：

```text
upload_item #100
asset_id = 500
```

表示：

```text
photo_asset #500
```

是它对应的照片。

---

# 38. DUPLICATE 的 asset_id

如果 SHA-256 判断重复：

```text
upload_item
status = DUPLICATE
asset_id = 500
```

表示：

> 当前上传文件与已有照片资产 #500 内容完全一致。

不会生成新的 `photo_assets`。

---

# 39. COMPLETED 的 asset_id

如果是新照片：

```text
upload_item
status = COMPLETED
asset_id = 501
```

对应：

```text
photo_asset #501
```

---

# 40. photo_assets

代表：

> **照片库中实际存在的照片。**

这是未来 WebUI 浏览照片、API 获取照片信息时的主要数据表。

## 40.1 Schema

```sql
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
```

---

# 41. photo_assets.sha256

完整文件 SHA-256。

格式：

```text
64 位十六进制字符串
```

例如：

```text
a5c...9f
```

SHA-256 是 V1 最终判断文件内容是否重复的依据。

---

# 42. SHA-256 唯一性

正式照片资产：

> 同一个 SHA-256 只能对应一个照片资产。

因此 V1 建议：

```sql
CREATE UNIQUE INDEX ux_photo_assets_sha256
ON photo_assets (sha256);
```

这是整个数据库中非常重要的最终去重约束。

---

# 43. 为什么 SHA-256 放在 photo_assets

因为：

```text
SHA-256
```

描述的是：

> 照片资产本身。

而不是一次上传行为。

同一照片可以：

```text
上传 1 次
上传 2 次
上传 10 次
```

但它始终只有一个：

```text
photo_asset
```

所以 SHA-256 应该属于：

```text
photo_assets
```

---

# 44. original_filename

照片第一次进入照片资产库时的原始文件名。

例如：

```text
IMG_001.jpg
```

该字段不因为文件冲突而修改。

---

# 45. current_filename

照片最终实际保存的文件名。

正常情况：

```text
original_filename = current_filename
```

如果发生冲突：

```text
original_filename = IMG_001.jpg
current_filename = IMG_001_1.jpg
```

---

# 46. 文件名冲突规则

如果目标目录已经存在：

```text
IMG_001.jpg
```

且 SHA-256 不相同：

```text
IMG_001_1.jpg
```

继续冲突：

```text
IMG_001_2.jpg
```

依次递增。

禁止覆盖已有照片。

---

# 47. file_size

照片资产最终文件大小。

单位：

```text
bytes
```

---

# 48. mime_type

实际检测到的 MIME Type。

例如：

```text
image/jpeg
image/png
image/heic
```

V1 第一阶段只允许配置文件定义的图片格式。

视频不属于 V1。

---

# 49. date_taken

照片最终使用的拍摄日期时间。

例如：

```text
2026-08-31T19:32:15
```

用于确定：

```text
YYYY/YYYYMM
```

正式归档目录。

---

# 50. date_source

日期来源。

支持：

```text
DateTimeOriginal
CreateDate
ModifyDate
file_mtime
upload_time
```

优先级：

```text
DateTimeOriginal
    ↓
CreateDate
    ↓
ModifyDate
    ↓
file_mtime
    ↓
upload_time
```

---

# 51. exif_json

保存照片解析出的 EXIF / Metadata。

类型：

```text
TEXT
```

内容：

```text
JSON
```

示例：

```json
{
  "DateTimeOriginal": "2026:08:31 19:32:15",
  "Make": "Xiaomi",
  "Model": "2312DRA50C",
  "ImageWidth": 4080,
  "ImageHeight": 3072,
  "GPSLatitude": 23.1291,
  "GPSLongitude": 113.2644
}
```

---

# 52. EXIF 原始信息保护

Photo Gateway V1：

> 只读取 EXIF，不修改照片 EXIF。

数据库中的：

```text
exif_json
```

是照片元数据的解析副本。

不得根据数据库内容反向修改照片。

---

# 53. archive_path

正式照片文件的永久路径。

例如：

```text
Photos/2026/202608/IMG_001.jpg
```

路径规则由配置文件决定。

V1 目标结构：

```text
path_prefix/YYYY/YYYYMM/
```

例如：

```text
Photos/
├── 2004/
│   ├── 200401/
│   └── 200402/
├── 2025/
│   └── 202512/
└── 2026/
    └── 202608/
```

---

# 54. archive_path 的特殊性

与：

```text
incoming
processing
```

不同。

`archive_path` 是：

> 照片资产的永久位置。

因此必须保存。

---

# 55. 不保存 incoming_path

V1 不在数据库保存：

```text
incoming_path
```

原因：

`incoming/` 是固定的工作目录。

例如：

```text
incoming/IMG_001.jpg
```

属于临时运行状态，不是照片资产属性。

---

# 56. 不保存 processing_path

同理，不保存：

```text
processing_path
```

Worker 可以通过：

```text
processing/
```

目录扫描 + 数据库状态进行恢复。

这样可以避免数据库保存大量短生命周期路径。

---

# 57. Worker 重启恢复

如果重启时数据库存在：

```text
upload_item.status = PROCESSING
```

同时：

```text
processing/
```

存在对应文件。

Worker 启动扫描后：

```text
processing/file.jpg
        ↓
incoming/file.jpg
        ↓
status = UPLOADED
        ↓
重新进入 Worker
```

同时写入：

```text
photo_event = RECOVERED
```

---

# 58. `.tmp` 文件

上传过程中：

```text
incoming/IMG_001.jpg.tmp
```

数据库：

```text
status = UPLOADING
```

上传完成：

```text
incoming/IMG_001.jpg
```

数据库：

```text
status = UPLOADED
```

V1 不实现 `.tmp` 断点续传。

---

# 59. `.tmp` 清理

`.tmp` 清理策略由配置文件控制。

默认：

```yaml
upload:
  tmp_cleanup:
    enabled: true
    max_age_days: 7
```

数据库不记录 `.tmp` 文件路径。

---

# 60. photo_assets 的删除

V1：

> Photo Gateway 不提供正式照片资产删除。

因此数据库中的：

```text
photo_assets
```

不会因为 WebUI 操作而删除。

正式照片删除属于后续照片资产管理功能。

---

# 61. upload_items 历史记录

V1：

> 不自动删除。

即使：

```text
DUPLICATE
FAILED
```

也保留记录。

原因：

这些记录用于：

- Precheck
- 上传历史
- WebUI 状态展示
- 故障排查
- 审计

---

# 62. photo_events

记录照片上传及处理过程中的重要事件。

## 62.1 Schema

```sql
CREATE TABLE photo_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    upload_item_id  INTEGER NOT NULL,

    event_type      TEXT NOT NULL,

    from_status     TEXT,
    to_status       TEXT,

    message         TEXT,
    details_json    TEXT,

    created_at      TEXT NOT NULL,

    FOREIGN KEY (upload_item_id)
        REFERENCES upload_items(id)
);
```

---

# 63. photo_events 与 photo_assets 的关系

事件主要属于：

> 一次上传行为。

因此：

```text
photo_events.upload_item_id
```

关联：

```text
upload_items
```

而不是直接关联：

```text
photo_assets
```

例如同一张照片被上传两次：

```text
photo_asset #500

upload_item #100
  └── events

upload_item #200
  └── events
```

两个上传过程都有自己的历史。

---

# 64. event_type

V1 支持：

```text
PRECHECK
UPLOAD_STARTED
UPLOAD_COMPLETED
UPLOAD_FAILED
PROCESSING_STARTED
SHA256_CALCULATED
DUPLICATE_DETECTED
REUPLOAD_CONFIRMED
ARCHIVED
PROCESSING_FAILED
RECOVERED
TMP_CLEANED
```

---

# 65. 事件示例

正常：

```text
PRECHECK
↓
UPLOAD_STARTED
↓
UPLOAD_COMPLETED
↓
PROCESSING_STARTED
↓
SHA256_CALCULATED
↓
ARCHIVED
```

重复：

```text
PRECHECK
↓
UPLOAD_STARTED
↓
UPLOAD_COMPLETED
↓
PROCESSING_STARTED
↓
SHA256_CALCULATED
↓
DUPLICATE_DETECTED
```

失败：

```text
PROCESSING_STARTED
↓
PROCESSING_FAILED
```

---

# 66. error_code

失败时使用机器可识别的错误码。

例如：

```text
UPLOAD_FAILED
INVALID_FILE
SHA256_FAILED
EXIF_FAILED
ARCHIVE_FAILED
FILE_CONFLICT
DATABASE_ERROR
UNKNOWN_ERROR
```

---

# 67. error_message

保存面向开发人员/管理员的错误描述。

例如：

```text
failed to calculate SHA-256: permission denied
```

不应该依赖错误文字进行程序逻辑判断。

程序逻辑使用：

```text
error_code
```

---

# 68. 数据库索引

V1 必须建立以下索引：

```sql
CREATE INDEX idx_upload_items_precheck
ON upload_items (
    source_device,
    original_filename,
    file_size
);

CREATE INDEX idx_upload_items_session
ON upload_items (session_id);

CREATE INDEX idx_upload_items_status
ON upload_items (status);

CREATE INDEX idx_upload_items_asset
ON upload_items (asset_id);

CREATE INDEX idx_photo_assets_date_taken
ON photo_assets (date_taken);

CREATE INDEX idx_photo_events_item
ON photo_events (upload_item_id);

CREATE INDEX idx_photo_events_created
ON photo_events (created_at);
```

SHA-256：

```sql
CREATE UNIQUE INDEX ux_photo_assets_sha256
ON photo_assets (sha256);
```

---

# 69. 为什么 Precheck 索引放在 upload_items

因为：

```text
source_device
original_filename
file_size
```

属于：

> 上传行为的输入信息。

一次照片上传，无论最后：

```text
COMPLETED
DUPLICATE
FAILED
```

都属于 `upload_items`。

因此 Precheck 的输入信息应该放在：

```text
upload_items
```

而最终照片内容 Hash 属于：

```text
photo_assets
```

---

# 70. Session 进度计算

WebUI 可以直接读取：

```text
upload_sessions
```

中的汇总字段。

需要精确统计时：

```sql
SELECT status, COUNT(*)
FROM upload_items
WHERE session_id = ?
GROUP BY status;
```

因此即使 Session 的统计字段因为异常暂时不准确，也可以通过 `upload_items` 重建。

---

# 71. WebUI 照片信息

照片详情主要来自：

```text
photo_assets
```

可以直接提供：

```text
id
original_filename
current_filename
file_size
sha256
mime_type
date_taken
date_source
exif_json
archive_path
created_at
updated_at
```

---

# 72. WebUI 上传历史

上传历史主要来自：

```text
upload_sessions
upload_items
```

可以显示：

```text
设备
原始文件名
文件大小
上传时间
状态
Precheck 结果
错误信息
对应照片资产
```

---

# 73. 一个完整示例

用户从：

```text
小米 14
```

选择：

```text
IMG_001.jpg
```

系统创建：

```text
upload_session #S001
```

然后：

```text
upload_item #100
source_device = xiaomi14
original_filename = IMG_001.jpg
file_size = 12345678
```

Precheck：

```text
POSSIBLE_DUPLICATE
```

用户确认重新上传。

然后：

```text
reupload_confirmed = 1
```

上传：

```text
incoming/IMG_001.jpg.tmp
```

完成：

```text
incoming/IMG_001.jpg
```

Worker 计算：

```text
sha256 = ABC
```

发现：

```text
photo_asset #500
sha256 = ABC
```

于是：

```text
upload_item #100
status = DUPLICATE
asset_id = 500
```

不产生新的：

```text
photo_asset
```

---

# 74. 新照片示例

如果 SHA-256 不存在：

创建：

```text
photo_asset #501
```

例如：

```text
sha256 = DEF
original_filename = IMG_002.jpg
current_filename = IMG_002.jpg
file_size = 12000000
date_taken = 2026-08-31T20:00:00
date_source = DateTimeOriginal
archive_path = Photos/2026/202608/IMG_002.jpg
```

然后：

```text
upload_item
status = COMPLETED
asset_id = 501
```

---

# 75. 文件名冲突示例

如果：

```text
Photos/2026/202608/IMG_002.jpg
```

已经存在，但 SHA-256 不同：

```text
current_filename = IMG_002_1.jpg
```

最终：

```text
archive_path =
Photos/2026/202608/IMG_002_1.jpg
```

数据库仍然保存：

```text
original_filename = IMG_002.jpg
```

---

# 76. 数据一致性原则

数据库和文件系统不是同一个事务系统。

因此：

> 文件移动与 SQLite Commit 不可能依赖普通 SQL Transaction 实现真正的原子操作。

V1 使用：

```text
文件系统状态
+
数据库状态
+
Worker 启动恢复
```

保证最终一致。

---

# 77. 最终归档流程

推荐流程：

```text
PROCESSING
    │
    ├── 读取 EXIF
    │
    ├── 计算日期
    │
    ├── 计算 SHA256
    │
    ├── 查询 photo_assets
    │
    ├── 重复 → DUPLICATE
    │
    └── 新照片
           │
           ▼
       确定 archive_path
           │
           ▼
       文件 rename/move
           │
           ▼
       创建 photo_asset
           │
           ▼
       upload_item = COMPLETED
```

其中：

> 文件移动必须采用同文件系统内的原子 `rename` 优先。

---

# 78. SHA-256 与数据库唯一约束

最终创建 `photo_assets` 时：

```sql
INSERT INTO photo_assets (...)
VALUES (...);
```

如果发生：

```text
UNIQUE constraint failed
```

说明：

> 另一个处理流程已经创建了相同 SHA-256 的照片资产。

V1 Worker 只有一个，因此正常情况下不会出现。

如果出现：

> 必须按 DUPLICATE 逻辑处理。

---

# 79. 数据库事务

以下操作必须使用事务。

### 创建 Session

```text
INSERT upload_sessions
```

### 创建 Upload Item

```text
INSERT upload_items
INSERT photo_events
```

### 上传完成

```text
UPDATE upload_items
INSERT photo_event
```

### SHA-256 处理

```text
UPDATE upload_items
INSERT photo_event
```

### 创建 Photo Asset

```text
INSERT photo_assets
UPDATE upload_items
INSERT photo_event
```

---

# 80. 数据库写入频率

不要求实时记录：

```text
upload percentage
```

例如：

```text
37.1%
37.2%
37.3%
```

不应该每次更新 SQLite。

实时上传进度：

> 在内存中维护。

数据库只记录关键状态。

这样可以减少 R5S 上的 SQLite 写入压力。

---

# 81. SQLite 锁处理

如果出现：

```text
SQLITE_BUSY
SQLITE_LOCKED
```

应用进行有限重试。

建议：

```text
100ms
200ms
500ms
1s
2s
```

超过重试次数后：

> 写 ERROR 日志。

不得静默忽略。

---

# 82. 数据库损坏处理

如果启动时发现 SQLite 数据库损坏：

```text
Photo Gateway
        ↓
SAFE MODE
```

停止：

```text
Worker
```

避免数据库和文件系统继续产生新的不一致。

---

# 83. 数据库备份

V1 应支持后续实现：

```text
photo-gateway db backup
```

使用 SQLite 官方 Backup 机制生成一致性数据库备份。

数据库备份属于：

> 系统配置/运行数据备份。

照片本身仍通过文件系统进行备份。

---

# 84. 已有照片的处理

当前已经存在约 1TB：

```text
Photos/
├── 2004/
├── ...
└── 2026/
```

V1：

> 不要求迁移。

不要求：

- 改目录
- 改文件名
- 修改 EXIF
- 重新计算全部 SHA-256
- 重新导入 Immich

Photo Gateway 只管理以后通过 Gateway 导入的照片。

---

# 85. 后续已有照片索引

如果未来希望：

> 让数据库知道现有 1TB 照片的全部信息。

再增加独立的：

```text
photo-gateway index
```

功能。

该功能不属于 V1 上传核心。

---

# 86. 与 Immich 的关系

Photo Gateway 数据库：

> 不保存 Immich 数据。

Immich 继续通过：

```text
External Library
```

读取：

```text
Photos/
```

Photo Gateway 不依赖 Immich 数据库。

因此未来更换：

```text
Immich
其他照片管理软件
自研 WebUI
```

都不会影响：

```text
Photos/YYYY/YYYYMM/
```

中的照片。

---

# 87. 与 NAS 的关系

V1 数据库：

> 不包含 NAS 同步状态。

不设计：

```text
nas_path
nas_status
nas_sync_time
nas_hash
```

等字段。

NAS 同步属于后续阶段。

---

# 88. 与人脸识别的关系

V1 数据库：

> 不保存人脸识别结果。

例如：

```text
person_id
face_embedding
face_cluster
```

均不属于 V1。

这些属于未来照片管理层。

---

# 89. V1 不包含视频

V1 `upload_items` 允许的文件类型由配置文件控制。

当前阶段：

> 视频不进入 Photo Gateway V1。

后续如果增加视频：

> 通过配置 + Schema Migration 扩展。

---

# 90. 时间字段规范

所有数据库时间字段统一：

```text
UTC ISO-8601
```

例如：

```text
2026-09-01T05:30:00Z
```

照片的：

```text
date_taken
```

表示照片实际拍摄时间。

不要与：

```text
created_at
uploaded_at
```

混淆。

---

# 91. 数据库 NULL 原则

允许 NULL 的字段主要是：

```text
client_ip
started_at
completed_at

asset_id

error_code
error_message

uploaded_at
processing_started_at
completed_at

date_taken
date_source
exif_json
mime_type
```

核心身份字段不得 NULL：

```text
source_device
original_filename
file_size
status
created_at
updated_at
```

---

# 92. V1 完整 Schema

最终可以整理为以下结构：

```sql
CREATE TABLE schema_version (
    version     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE devices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE upload_sessions (
    id                  TEXT PRIMARY KEY,
    source_device       TEXT NOT NULL,
    client_ip            TEXT,

    created_at           TEXT NOT NULL,
    started_at           TEXT,
    completed_at         TEXT,

    total_files          INTEGER NOT NULL DEFAULT 0,
    uploaded_files       INTEGER NOT NULL DEFAULT 0,
    skipped_files        INTEGER NOT NULL DEFAULT 0,
    duplicate_files      INTEGER NOT NULL DEFAULT 0,
    failed_files         INTEGER NOT NULL DEFAULT 0,

    status               TEXT NOT NULL,

    FOREIGN KEY (source_device)
        REFERENCES devices(id)
);

CREATE TABLE photo_assets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    sha256              TEXT NOT NULL,

    original_filename   TEXT NOT NULL,
    current_filename    TEXT NOT NULL,

    file_size           INTEGER NOT NULL,
    mime_type            TEXT,

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

    original_filename     TEXT NOT NULL,
    file_size             INTEGER NOT NULL,

    status                TEXT NOT NULL,

    precheck_status       TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    reupload_confirmed    INTEGER NOT NULL DEFAULT 0,

    asset_id              INTEGER,

    error_code            TEXT,
    error_message         TEXT,

    created_at            TEXT NOT NULL,
    uploaded_at           TEXT,
    processing_started_at TEXT,
    completed_at          TEXT,
    updated_at            TEXT NOT NULL,

    FOREIGN KEY (session_id)
        REFERENCES upload_sessions(id),

    FOREIGN KEY (source_device)
        REFERENCES devices(id),

    FOREIGN KEY (asset_id)
        REFERENCES photo_assets(id)
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

    FOREIGN KEY (upload_item_id)
        REFERENCES upload_items(id)
);
```

索引：

```sql
CREATE INDEX idx_upload_items_precheck
ON upload_items (
    source_device,
    original_filename,
    file_size
);

CREATE INDEX idx_upload_items_session
ON upload_items (session_id);

CREATE INDEX idx_upload_items_status
ON upload_items (status);

CREATE INDEX idx_upload_items_asset
ON upload_items (asset_id);

CREATE INDEX idx_photo_assets_date_taken
ON photo_assets (date_taken);

CREATE INDEX idx_photo_events_item
ON photo_events (upload_item_id);

CREATE INDEX idx_photo_events_created
ON photo_events (created_at);

CREATE UNIQUE INDEX ux_photo_assets_sha256
ON photo_assets (sha256);
```

---

# 93. 最终架构

V1 数据库最终确定为：

```text
                    ┌─────────────┐
                    │   devices   │
                    └──────┬──────┘
                           │
                           ▼
                 ┌──────────────────┐
                 │ upload_sessions   │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │   upload_items   │
                 │                  │
                 │ source_device    │
                 │ filename         │
                 │ size             │
                 │ precheck         │
                 │ status           │
                 │ asset_id         │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │  photo_assets    │
                 │                  │
                 │ SHA256           │
                 │ EXIF             │
                 │ date_taken       │
                 │ archive_path     │
                 └──────────────────┘

                 upload_items
                       │
                       ▼
                 ┌──────────────────┐
                 │  photo_events    │
                 └──────────────────┘
```

---

# 94. V1 Frozen Baseline

本数据库设计与：

```text
photo-gateway-v1-spec.md
photo-gateway-v1-config-spec.md
```

共同构成 Photo Gateway V1 冻结设计基线。

V1 最核心的三个原则：

### 原则一：上传行为与照片资产分离

```text
upload_items ≠ photo_assets
```

### 原则二：快速预检与最终去重分离

```text
source_device
+
original_filename
+
file_size
        ↓
POSSIBLE_DUPLICATE
```

只是快速预检。

最终判断：

```text
SHA-256
        ↓
DUPLICATE / NEW PHOTO
```

### 原则三：数据库与照片文件分离

```text
SQLite
    ↓
状态、元数据、Hash、上传历史

文件系统
    ↓
照片本体

Photos/YYYY/YYYYMM/
    ↓
长期稳定的照片资产结构
```

数据库设计不得改变现有：

```text
Photos/YYYY/YYYYMM/
```

目录规范。

后续 API、WebUI、Worker 实现必须以本 V1 数据模型为基础，不得在代码层自行增加与本文档冲突的状态、字段或数据关系。