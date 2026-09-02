# Photo Gateway V1 技术规格书

**项目名称：Photo Gateway**  
**版本：V1.0**  
**状态：冻结基线（Frozen Baseline）**  
**目标平台：FriendlyElec NanoPi R5S / ImmortalWrt 24.10.5**

---

# 1. 文档目的

本文档定义 Photo Gateway V1.0 的功能范围、技术架构、数据模型、文件生命周期、去重规则、配置规范、日志规范以及异常恢复机制。

V1 的目标是完成：

> **局域网照片上传 → R5S 本地接收 → 重复检测 → 元数据提取 → 按日期归档 → 提供任务状态查询。**

V1 不负责 NAS 同步。

后续 NAS 同步方案将作为独立阶段重新设计。

---

# 2. V1 系统定位

Photo Gateway 是整个家庭照片系统中的：

```text
照片导入网关 + 临时处理节点 + 本地照片归档节点
```

而不是完整的照片管理系统。

架构关系：

```text
Android
iOS
macOS
Windows
其他局域网设备
        │
        │ HTTP
        ▼
┌─────────────────────────────┐
│       Photo Gateway         │
│          NanoPi R5S         │
│                             │
│ WebUI                       │
│ Upload API                  │
│ Upload Session              │
│ Precheck                    │
│ SHA-256                     │
│ EXIF                        │
│ Processing Worker           │
│ SQLite                      │
│ Local Archive               │
│ Logging                     │
└─────────────────────────────┘
```

V1 结束后：

```text
R5S
└── Photos/
    ├── 2004/
    │   ├── 200401/
    │   └── 200402/
    └── 2026/
        ├── 202601/
        └── 202608/
```

这部分目录就是后续 NAS 同步阶段需要同步的数据源。

---

# 3. V1 明确不负责的内容

以下内容全部不属于 V1：

```text
NAS 探测
NAS 挂载
NAS rsync
NAS 同步
NAS 主备
NAS 同步状态
NAS 数据删除策略
Immich
人脸识别
AI 分类
照片搜索
照片时间线
公网访问
云端同步
iCloud
Google Photos
视频处理
视频上传
照片编辑
照片格式转换
图片压缩
字节级断点续传
```

尤其明确：

> V1 不访问 NAS。

V1 不应该因为未来需要 NAS，而在当前代码中提前加入 NAS 探测、rsync 或 NAS API。

---

# 4. 硬件环境

## 4.1 Photo Gateway

设备：

```text
FriendlyElec NanoPi R5S
```

系统：

```text
ImmortalWrt 24.10.5
```

硬件：

```text
CPU：ARMv8 4 Core
RAM：约 4GB
```

当前照片临时存储：

```text
USB 3.0
2TB HDD
```

该硬盘当前约：

```text
总容量：1.8TB
已使用：约1.3TB
可用：约443GB
```

该硬盘：

> 只作为 R5S 本地照片存储，不视为可靠备份。

---

# 5. V1 存储目录

R5S：

```text
/mnt/sda1/photo-gateway/
```

目录：

```text
photo-gateway/
├── incoming/
├── processing/
├── failed/
├── Photos/
├── logs/
└── database/
```

---

## 5.1 incoming

保存上传中的临时文件：

```text
incoming/
└── IMG_001.jpg.tmp
```

命名规则：

```text
<原始文件名>.tmp
```

例如：

```text
IMG_001.jpg
```

上传时：

```text
IMG_001.jpg.tmp
```

---

## 5.2 processing

Worker 当前正在处理的文件：

```text
processing/
└── IMG_001.jpg
```

---

## 5.3 failed

处理失败且无法自动恢复的文件：

```text
failed/
└── IMG_001.jpg
```

---

## 5.4 Photos

R5S 上的正式照片目录：

```text
Photos/
├── 2004/
│   ├── 200401/
│   └── 200402/
├── 2005/
└── 2026/
    ├── 202601/
    └── 202608/
```

V1 使用：

```text
Photos/YYYY/YYYYMM/
```

作为正式归档结构。

注意：

> 当前阶段该目录位于 R5S。

后续 NAS 阶段再将其同步到 NAS。

---

## 5.5 logs

日志目录：

```text
logs/
```

例如：

```text
logs/
├── application.log
├── upload.log
├── processing.log
├── audit.log
└── error.log
```

具体日志策略见第 18 章。

---

## 5.6 database

SQLite：

```text
database/
└── photo-gateway.db
```

---

# 6. 原始照片目录不可改变

用户现有照片目录：

```text
Photos/
├── 2004/
│   ├── 200401/
│   └── 200402/
```

必须保持。

V1 不允许使用：

```text
YYYY/MM/DD
```

等其他结构。

正式目录：

```text
Photos/YYYY/YYYYMM/
```

例如：

```text
Photos/2026/202608/IMG_001.jpg
```

---

# 7. 存储与管理软件解耦

Photo Gateway 只负责保存原始照片。

未来 Immich 使用：

```text
External Library
```

读取：

```text
Photos/
```

Photo Gateway 不使用 Immich 的内部存储机制。

因此未来更换：

```text
Immich
→ 其他照片管理软件
```

不会影响原始照片目录。

---

# 8. 配置系统

V1 所有部署环境和业务规则都通过配置文件控制。

原则：

> 环境参数、设备定义、业务规则进入配置；照片运行状态进入数据库。

---

# 9. 配置项目

以下内容进入配置：

### 服务

```text
监听地址
监听端口
```

### 认证

```text
认证开关
用户名
密码 Hash
Session 有效期
```

### 设备

```text
设备 ID
设备名称
启用状态
```

### 上传

```text
允许的扩展名
上传并发
Precheck 批量大小
临时文件清理
```

### 处理

```text
Worker 数量
```

### 元数据

```text
EXIF 日期优先级
```

### 存储

```text
R5S 根目录
incoming
processing
failed
Photos
database
logs
```

### 磁盘

```text
告警阈值
停止上传阈值
```

### 日志

```text
日志级别
日志目录
日志保留时间
日志文件大小
```

---

# 10. 设备定义

由于：

```text
source_device
```

参与重复预检，因此：

> source_device 必须存在。

且：

> 用户不能自由填写 source_device。

配置示例：

```yaml
devices:
  - id: xiaomi13ultra
    name: 小米13 Ultra
    enabled: true

  - id: xiaomi14
    name: 小米14
    enabled: true

  - id: macbook
    name: MacBook Air
    enabled: true

  - id: windows-pc
    name: Windows PC
    enabled: true

  - id: iphone
    name: iPhone
    enabled: true
```

---

# 11. Device ID 规则

`id`：

> 是系统内部稳定标识。

例如：

```text
xiaomi14
```

历史数据中会保存：

```text
source_device = xiaomi14
```

因此：

> 已使用的 Device ID 不允许修改。

可以修改：

```text
name
enabled
```

例如：

```text
id: xiaomi14
name: 小米14
```

以后可以改成：

```text
id: xiaomi14
name: 黄先生的小米14
```

但：

```text
id
```

保持不变。

---

# 12. 浏览器信息

除：

```text
source_device
```

之外，客户端请求中还可能携带：

```text
user_agent
```

V1 对 `user_agent` 的统一规则：

> `user_agent` 仅记录到结构化日志，不进入业务数据库，不参与业务逻辑，也不参与去重判断。

例如：

```text
Mozilla/5.0 ...
```

如果没有明确设备信息：

> V1 不允许用用户自由填写的信息替代 source_device。

设备必须先在配置中注册。

---

# 13. 简单认证

V1 实现简单认证。

例如：

```yaml
auth:
  enabled: true
  username: admin
  password_hash: "$argon2id$..."
  session_ttl_hours: 24
```

认证职责链路固定为：

```text
config.yaml
    ↓
username + password_hash
    ↓
POST /api/v1/auth/login
    ↓
Session Token
    ↓
Authorization: Bearer <token>
    ↓
其他 API
```

V1 不引入用户表、不实现 RBAC、不实现复杂账户体系。

认证具体契约以：

```text
photo-gateway-v1-api-spec.md
```

为准。

V1 不实现：

```text
OAuth
LDAP
SSO
多用户
RBAC
公网认证
```

---

# 14. 文件类型

文件扩展名由配置控制：

```yaml
upload:
  allowed_extensions:
    - jpg
    - jpeg
    - png
    - heic
    - heif
    - webp
    - tif
    - tiff
    - cr2
    - cr3
    - nef
    - arw
    - dng
    - raf
    - orf
    - rw2
```

扩展名：

> 大小写不敏感。

例如：

```text
JPG
jpg
Jpg
```

均允许。

V1：

> 视频文件不允许上传。

---

# 15. 上传模型

用户选择照片后：

```text
创建 Upload Session
        │
        ▼
获取文件列表
        │
        ▼
Precheck
        │
        ▼
筛选真正需要上传的文件
        │
        ▼
上传
        │
        ▼
Processing
```

---

# 16. Upload Session

一次选择：

```text
1000 张
```

生成一个 Session。

例如：

```text
20260901-ABCD1234
```

Session 保存：

```text
session_id
source_device
状态
文件数量统计
时间信息

具体字段以 DB Spec 为准
```

---

# 17. 上传顺序

V1 后台处理：

```text
worker_count = 1
```

即：

> 后台照片处理严格串行。

但需要区分：

### 上传

Web 上传请求可以根据配置允许一定并发。

### Processing

照片进入 R5S 后：

```text
Worker 1
```

按队列：

```text
文件1
 ↓
文件2
 ↓
文件3
 ↓
...
```

逐个处理。

这样可以避免 R5S 同时进行大量：

```text
SHA256
磁盘读写
EXIF 解析
文件移动
```

造成资源压力。

---

# 18. 日志体系

V1 必须从第一版开始实现完整日志。

日志不是调试阶段临时增加的功能，而是正式系统的一部分。

---

## 18.1 application.log

记录系统运行信息：

```text
启动
停止
配置加载
配置错误
数据库初始化
Worker 启动
Worker 停止
系统异常
```

例如：

```text
2026-09-01 10:20:31 INFO  application started
2026-09-01 10:20:32 INFO  database initialized
2026-09-01 10:20:32 INFO  processing worker started
```

---

## 18.2 upload.log

记录上传相关事件：

```text
Session 创建
文件开始上传
文件上传完成
文件上传失败
客户端断开
上传取消
```

必须包含：

```text
session_id
photo_id
source_device
filename
```

例如：

```text
2026-09-01 10:21:01 INFO
session=xxx
device=xiaomi14
file=IMG_001.jpg
upload started
```

---

## 18.3 processing.log

记录后台处理：

```text
开始处理
SHA256
EXIF
日期解析
文件归档
重复判断
处理完成
处理失败
```

例如：

```text
photo=12345
sha256=...
date=2026-08-31
target=Photos/2026/202608/IMG_001.jpg
status=success
```

---

## 18.4 audit.log

记录重要用户操作：

```text
登录
上传
确认疑似重复
重新上传
删除失败文件
重新处理
修改配置
启用设备
禁用设备
```

审计日志与普通运行日志分开。

---

## 18.5 error.log

记录 ERROR / CRITICAL 级别异常。

例如：

```text
磁盘空间不足
数据库异常
文件损坏
SHA256 失败
EXIF 解析失败
文件移动失败
权限错误
```

---

# 19. 日志字段

建议统一结构化日志。

至少包含：

```text
timestamp
level
module
event
session_id
photo_id
source_device
filename
message
error
```

不是所有日志都必须存在所有字段。

例如：

```json
{
  "timestamp": "2026-09-01T10:21:01+08:00",
  "level": "INFO",
  "module": "upload",
  "event": "upload_completed",
  "session_id": "20260901-ABCD1234",
  "photo_id": 10001,
  "source_device": "xiaomi14",
  "filename": "IMG_001.jpg"
}
```

---

# 20. 日志轮转

日志必须支持：

```text
按日期轮转
+
按文件大小轮转
```

配置：

```yaml
logging:
  level: INFO

  rotation:
    max_size_mb: 50
    retention_days: 30
```

默认：

```text
单文件最大 50MB
保留 30 天
```

日志达到阈值后自动轮转。

---

# 21. 日志与数据库职责

日志不能替代数据库。

数据库保存：

```text
当前状态
```

日志保存：

```text
状态变化过程
```

例如数据库：

```text
status = PROCESSING
```

而日志记录：

```text
10:20 upload completed
10:21 sha256 completed
10:21 exif parsed
10:21 archive completed
```

这样可以在出现异常时重建处理过程。

---

# 22. 重复预检

V1 采用两级去重。

第一层：

```text
source_device
+
original_filename
+
file_size
```

第二层：

```text
SHA-256
```

两者职责不同。

---

# 23. 第一阶段：Precheck

假设用户上传：

```text
1000 张
```

系统首先获得：

```text
filename
file_size
source_device
```

然后查询数据库。

例如：

```text
xiaomi14
IMG_001.jpg
8237461
```

如果数据库已经存在：

```text
POSSIBLE_DUPLICATE
        │
        ├── user_confirmation = 0
        │       ↓
        │    等待用户决定
        │
        ├── user_confirmation = 1
        │       ↓
        │    UPLOADING
        │
        └── user_confirmation = 2
                ↓
              跳过
```

POSSIBLE_DUPLICATE 是 Precheck 结果，不是最终处理结果；user_confirmation 是用户决策，不属于 Item 状态机。

---

# 24. Precheck 的性能目标

假设历史已有：

```text
900 张
```

本次再次选择：

```text
1000 张
```

其中：

```text
900 张
```

已经上传过。

V1 应做到：

```text
1000
 ↓
数据库快速查询
 ↓
900 疑似重复
100 新文件
 ↓
实际只上传 100 张
```

而不是：

```text
1000
 ↓
全部上传
 ↓
全部 SHA256
```

---

# 25. 疑似重复列表

Precheck 完成后：

```text
总计：1000
新照片：100
疑似重复：900
失败：0
```

用户可以查看：

```text
文件名
文件大小
source_device
历史上传时间
照片拍摄时间
历史路径
```

---

# 26. 用户确认重新上传

用户可以选择：

```text
IMG_001.jpg
IMG_003.jpg
IMG_007.jpg
```

点击：

```text
重新上传
```

系统才允许这些文件重新进入：

```text
incoming/
```

数据库记录：

```text
user_confirmation = 1
```

---

# 27. 第二阶段：SHA-256

重新上传的照片：

```text
IMG_001.jpg.tmp
```

上传完成后：

```text
SHA-256
```

---

## SHA-256 相同

```text
DUPLICATE
```

直接删除临时文件。

---

## SHA-256 不同

说明：

```text
文件名相同
文件大小可能相同
但是内容不同
```

禁止覆盖。

例如：

```text
IMG_001.jpg
```

改为：

```text
IMG_001_1.jpg
```

数据库同时保存：

```text
original_filename
current_filename
sha256
```
规则：
1. 第一个文件保持原名。
2. 同名且 SHA-256 相同 → DUPLICATE。
3. 同名但 SHA-256 不同 → _1。
4. _1 已存在 → _2。
5. 依次递增。
6. 永远禁止覆盖。
7. original_filename 永远保存原始名称。
8. current_filename 保存最终名称。

---

# 28. SHA-256 定义

SHA-256 必须针对：

> 完整原始文件内容。

不得针对：

```text
EXIF
缩略图
压缩结果
重新编码结果
```

计算。

V1 不修改照片内容。

---

# 29. EXIF

V1 读取照片元数据。

至少支持：

```text
DateTimeOriginal
CreateDate
ModifyDate
Make
Model
LensModel
GPS
Width
Height
```

具体字段根据文件格式实际可读取情况决定。

---

# 30. 日期来源

默认优先级：

```yaml
metadata:
  date_priority:
    - DateTimeOriginal
    - CreateDate
    - ModifyDate
    - file_mtime
```

`upload_time` 不作为归档日期来源。

原因：

> 照片拍摄时间无法从 EXIF 获得时，使用文件 mtime；
> 不允许用上传时间代替拍摄时间。

例如一张 2005 年拍摄的照片在 2026 年导入，其归档目录必须是：

```text
Photos/2005/2005xx/
```

而不是：

```text
Photos/2026/202609/
```

最终只需要：

```text
YYYY
YYYYMM
```

例如：

```text
2026
202608
```

---

# 31. 正式归档

处理完成后：

```text
incoming/
IMG_001.jpg.tmp
        │
        ▼
SHA256
        │
        ▼
EXIF
        │
        ▼
确定日期
        │
        ▼
Photos/YYYY/YYYYMM/
```

例如：

```text
Photos/2026/202608/IMG_001.jpg
```

---

# 32. 原始文件保护

V1 禁止：

```text
修改 EXIF
重新编码
压缩
改变图片尺寸
修改拍摄时间
修改 GPS
```

照片必须保持原始内容。

---

# 33. 文件状态

V1 采用与数据库一致的状态机（具体以 `photo-gateway-v1-db-spec.md` 为准）：

```text
PRECHECK
   ↓
UPLOADING
   ↓
UPLOADED
   ↓
PROCESSING
   ├── COMPLETED
   ├── DUPLICATE
   └── FAILED
```
---

# 34. 重启恢复

R5S 重启后执行 Recovery。

---

## incoming/*.tmp

V1 不支持：

> 字节级断点续传。

因此 `.tmp` 不需要维护上传 offset。

系统可以保留 `.tmp`，等待重新上传或自动清理。

---

## processing/

如果发现：

```text
processing/IMG_001.jpg
```

系统启动时：

```text
读取数据库状态
 ↓
判断任务是否未完成
 ↓
移动回 incoming/
 ↓
更新数据库状态
 ↓
重新进入 Processing Queue
```

数据库状态必须同步修改。

不能只移动文件而不修改数据库。

---

# 35. `.tmp` 清理

配置：

```yaml
upload:
  tmp_cleanup:
    enabled: true
    max_age_days: 7
```

默认：

```text
enabled: true
max_age_days: 7
```

超过 7 天：

```text
*.tmp
```

自动清理。

---

# 36. 磁盘保护

R5S 临时硬盘容量有限，因此 V1 必须监控磁盘空间。

配置：

```yaml
disk:
  warning_percent: 80
  critical_percent: 90
  stop_percent: 95
```

行为：

```text
<80%
正常

>=80%
WARNING

>=90%
CRITICAL

>=95%
停止接受新的上传
```

已经进入处理队列的任务可以继续执行。

---

# 37. API

本总规格负责架构与业务规则，不再定义完整 API Schema，也不重复列出 API 端点清单。

V1 的 HTTP API 资源路径族包括：

```text
/api/v1/auth
/api/v1/devices
/api/v1/upload-sessions
/api/v1/upload-items
/api/v1/photos
/api/v1/system
```

具体 HTTP API 以：

```text
photo-gateway-v1-api-spec.md
```

为准。

具体数据库结构以：

```text
photo-gateway-v1-db-spec.md
```

为准。

具体配置以：

```text
photo-gateway-v1-config-spec.md
```

为准。

具体 WebUI 行为以：

```text
photo-gateway-v1-webui-spec.md
```

为准。

---

# 38. 照片状态 API

V1 的正式照片数据模型以：

```text
photo-gateway-v1-db-spec.md
```

中的 `photo_assets` 表为准。

具体照片列表、详情、原图等 HTTP API 以：

```text
photo-gateway-v1-api-spec.md
```

为准。

本总规格不再保留旧的 `photos` 对象模型（如 `year`、`year_month`、`current_path`、`width`、`height`、`datetime_original`、`status` 等字段），避免与数据库模型产生不一致。

---

# 39. 数据库核心原则

数据库必须能够快速支持：

```text
设备 + 文件名 + 文件大小
```

查询。

建立索引：

```text
(source_device, original_filename, file_size)
```

同时：

```text
sha256
```

建立索引。

---

# 40. 数据库与文件系统一致性

数据库记录和文件系统必须尽量保持一致。

例如：

```text
数据库：
status = PROCESSING

文件：
processing/IMG_001.jpg
```

恢复后：

```text
数据库：
status = UPLOADED

文件：
incoming/IMG_001.jpg.tmp
```

两者必须对应。

禁止出现：

```text
数据库说文件已经完成
但实际文件不存在
```

而没有进入：

```text
FAILED
```

或恢复流程。

---

# 41. 配置文件完整示例

```yaml
server:
  host: 0.0.0.0
  port: 8080

auth:
  enabled: true
  username: admin
  password_hash: "$argon2id$..."
  session_ttl_hours: 24

devices:
  - id: xiaomi13ultra
    name: 小米13 Ultra
    enabled: true

  - id: xiaomi14
    name: 小米14
    enabled: true

  - id: macbook
    name: MacBook Air
    enabled: true

  - id: windows-pc
    name: Windows PC
    enabled: true

  - id: iphone
    name: iPhone
    enabled: true

storage:
  root: /mnt/sda1/photo-gateway

  incoming: incoming
  processing: processing
  failed: failed

  archive: Photos

  database: database/photo-gateway.db
  logs: logs

upload:
  concurrency: 1

  precheck:
    batch_size: 5

  allowed_extensions:
    - jpg
    - jpeg
    - png
    - heic
    - heif
    - webp
    - tif
    - tiff
    - cr2
    - cr3
    - nef
    - arw
    - dng
    - raf
    - orf
    - rw2

  tmp_cleanup:
    enabled: true
    max_age_days: 7

processing:
  worker_count: 1

metadata:
  date_priority:
    - DateTimeOriginal
    - CreateDate
    - ModifyDate
    - file_mtime

disk:
  warning_percent: 80
  critical_percent: 90
  stop_percent: 95

logging:
  level: INFO

  rotation:
    max_size_mb: 50
    retention_days: 30
```

---

# 42. V1 开发边界

## Phase 1

基础工程：

```text
项目结构
配置加载
SQLite
日志
HTTP Server
认证
```

## Phase 2

上传：

```text
Upload Session
文件上传
.tmp
上传进度
```

## Phase 3

预检：

```text
source_device
filename
file_size
Precheck
疑似重复
```

## Phase 4

照片处理：

```text
SHA256
EXIF
日期解析
文件归档
文件名冲突
```

## Phase 5

可靠性：

```text
重启恢复
失败恢复
.tmp 清理
磁盘空间保护
```

## Phase 6

WebUI：

```text
上传
进度
疑似重复
失败任务
照片详情
系统状态
```

---

# 43. V1 验收标准

V1 完成后必须能够验证以下场景。

### 场景 1：普通上传

```text
上传 100 张
→ 100 张成功
→ 正确进入 Photos/YYYY/YYYYMM
```

### 场景 2：重复预检

```text
已经上传 900 张
再次选择 1000 张
→ 900 张不重新上传
→ 100 张正常上传
```

### 场景 3：用户确认疑似重复

```text
选择疑似重复
→ 确认重新上传
→ 实际上传
→ SHA256
```

### 场景 4：SHA256 相同

```text
SHA256 相同
→ DUPLICATE
→ 删除临时文件
```

### 场景 5：文件名相同但内容不同

```text
IMG_001.jpg
+
IMG_001.jpg

SHA256 不同
→ 自动生成唯一文件名
→ 不覆盖旧文件
```

### 场景 6：手机断网

```text
上传过程中断网
→ 当前文件失败
→ 已完成文件保持成功
→ 可以重新进入失败任务
```

### 场景 7：R5S 重启

```text
重启
→ Recovery
→ processing 恢复
→ 数据库状态恢复一致
```

### 场景 8：磁盘空间不足

```text
>=95%
→ 禁止新的上传
→ WebUI 显示磁盘空间不足
```

### 场景 9：日志

所有关键操作：

```text
上传
预检
SHA256
EXIF
归档
失败
恢复
用户操作
```

都能够在日志中追踪。

---

# 44. V1 最终数据流

```text
用户选择照片
      │
      ▼
选择 source_device
      │
      ▼
创建 Upload Session
      │
      ▼
Precheck
      │
 ┌────┴─────┐
 │          │
命中       未命中
 │          │
 ▼          ▼
疑似重复   上传
 │          │
 │          ▼
 │       .tmp
 │          │
 │          ▼
 │       上传完成
 │          │
 │          ▼
 │       SHA-256
 │          │
 │      ┌───┴───┐
 │      │       │
 │     相同     不同
 │      │       │
 │      ▼       ▼
 │ DUPLICATE  正常处理
 │              │
 │              ▼
 │             EXIF
 │              │
 │              ▼
 │          日期解析
 │              │
 │              ▼
 │       Photos/YYYY/YYYYMM
 │
 ▼
用户查看疑似重复
 │
 ▼
确认重新上传
 │
 ▼
重新进入 incoming
 │
 ▼
SHA-256
```

---

# 45. V1 冻结原则

以下内容作为 V1 冻结基线：

1. **V1 只处理 R5S 本地照片接收和归档。**
2. **V1 不处理 NAS。**
3. **V1 不进行 NAS 探测。**
4. **V1 不运行 rsync。**
5. **NAS 同步方案留到后续阶段单独设计。**
6. `source_device` 必须存在。
7. `source_device` 必须来自配置文件。
8. 用户不能自由填写 `source_device`。
9. `source_device + filename + file_size` 用于低成本预检。
10. SHA-256 用于最终重复判断。
11. 疑似重复照片默认不重新上传。
12. 用户确认后才能重新上传。
13. `.tmp` 使用 `<原文件名>.tmp`。
14. V1 不实现字节级断点续传。
15. Worker 默认单线程。
16. 原始照片内容不修改。
17. EXIF 不修改。
18. 正式目录固定为 `Photos/YYYY/YYYYMM/`。
19. 文件名冲突不得覆盖已有文件。
20. V1 必须实现结构化日志。
21. 日志必须轮转并支持保留策略。
22. 数据库保存当前状态，日志保存过程。
23. R5S 重启后必须执行任务恢复。
24. `.tmp` 默认 7 天自动清理。
25. 磁盘达到 95% 后停止新的上传。
26. V1 不实现视频。
27. V1 不实现人脸识别。
28. V1 不实现 AI 分类。
29. V1 不依赖 Immich。
30. V1 不依赖任何第三方云服务。

---

# 46. V1 与后续系统的边界

最终系统分成三个独立层次：

```text
                    ┌───────────────────┐
                    │  Photo Management │
                    │      Immich       │
                    │                   │
                    │ 人脸 / 时间线 / 搜索│
                    └─────────▲─────────┘
                              │
                       External Library
                              │
                    ┌─────────┴─────────┐
                    │   Photo Storage   │
                    │                   │
                    │ Photos/YYYY/YYYYMM│
                    └─────────▲─────────┘
                              │
                    后续 NAS 同步阶段
                              │
                    ┌─────────┴─────────┐
                    │  Photo Gateway    │
                    │      R5S          │
                    │                   │
                    │ 上传 / 去重 / EXIF │
                    │ 本地归档 / 日志    │
                    └───────────────────┘
```

其中 V1 只负责最下面这一层。

---

# 47. 冻结状态

**Photo Gateway V1.0：Frozen Baseline**

后续开发：

```text
配置 Schema
↓
SQLite Schema
↓
API Schema
↓
项目目录结构
↓
代码实现
↓
单元测试
↓
集成测试
```

均以本文档为依据。

如需增加：

```text
NAS
视频
AI
公网
字节级断点续传
```

应进入后续版本或独立阶段，不直接修改 V1 核心设计。