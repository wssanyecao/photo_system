# Photo Gateway V1 Configuration Specification

**文件名：** `photo-gateway-v1-config-spec.md`  
**版本：** V1.0  
**状态：** Frozen Baseline  
**上位规范：** `photo-gateway-v1-spec.md`

---

# 1. 文档目的

本文档定义 Photo Gateway V1 的配置文件格式、配置项、默认值、数据类型、校验规则以及运行时行为。

配置文件负责描述：

- 服务运行参数
- 简单认证
- 已注册照片来源设备
- 文件上传规则
- 本地存储路径
- Worker 参数
- EXIF 日期解析规则
- 磁盘空间保护
- 日志参数

配置文件不保存照片运行状态。

照片状态、上传任务状态、SHA-256、处理结果等运行数据必须保存到 SQLite 数据库。

---

# 2. 配置文件格式

V1 使用：

```text
YAML
```

推荐文件：

```text
/etc/photo-gateway/config.yaml
```

如果 ImmortalWrt 环境不适合使用 `/etc/photo-gateway`，可以通过启动参数指定：

```text
photo-gateway --config /path/to/config.yaml
```

配置文件路径本身不写入配置文件。

---

# 3. 配置设计原则

## 3.1 配置与运行状态分离

配置文件：

```text
定义系统应该怎么运行
```

SQLite：

```text
记录系统现在运行到了哪里
```

例如：

```yaml
processing:
  worker_count: 1
```

属于配置。

而：

```text
当前有 37 个文件处于 PROCESSING
```

属于数据库状态。

---

## 3.2 所有业务默认值必须明确

程序不能依赖：

```text
代码中的隐式默认值
```

所有具有业务含义的参数都应该在 Schema 中定义默认值。

---

## 3.3 未知配置项默认拒绝

例如：

```yaml
upload:
  max_speed: 100
```

如果 V1 不支持 `max_speed`：

> 配置校验必须失败。

这样可以避免配置文件存在拼写错误但程序静默忽略的问题。

---

## 3.4 启动时完整校验

程序启动时必须完成：

```text
读取配置
 ↓
YAML 解析
 ↓
Schema 校验
 ↓
路径校验
 ↓
设备配置校验
 ↓
业务规则校验
 ↓
加载配置
 ↓
启动服务
```

任何致命配置错误：

> Photo Gateway 不得启动。

---

# 4. 完整配置示例

以下配置作为 V1 推荐基线：

```yaml
server:
  host: 0.0.0.0
  port: 8080

auth:
  enabled: true
  username: admin
  password_hash: "$..."
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

# 5. server

定义 HTTP 服务参数。

```yaml
server:
  host: 0.0.0.0
  port: 8080
```

## 5.1 host

类型：

```text
string
```

默认：

```text
0.0.0.0
```

含义：

> HTTP Server 监听地址。

V1 默认监听所有局域网接口。

允许：

```text
0.0.0.0
127.0.0.1
具体 IPv4 地址
```

---

## 5.2 port

类型：

```text
integer
```

默认：

```text
8080
```

范围：

```text
1-65535
```

推荐使用：

```text
8080
```

V1 不强制 HTTPS。

原因：

> V1 定位为家庭局域网服务，不提供公网访问。

后续如果增加公网访问或 HTTPS，需要在后续版本单独设计。

---

# 6. auth

定义简单认证。

```yaml
auth:
  enabled: true
  username: admin
  password_hash: "$..."
  session_ttl_hours: 24
```

---

## 6.1 enabled

类型：

```text
boolean
```

默认：

```text
true
```

V1 推荐保持：

```text
true
```

---

## 6.2 username

类型：

```text
string
```

要求：

```text
不能为空
长度 1-64
```

默认不提供固定用户名。

部署时必须设置。

---

## 6.3 password_hash

类型：

```text
string
```

保存：

> 密码 Hash，而不是明文密码。

禁止：

```yaml
password: 123456
```

这种形式。推荐使用 Argon2id。

例如：

```text
$argon2id$v=19$...
```

---
## 6.4 session_ttl_hours

类型：

```text
int
```

默认：

```text
24
```

含义：

> 登录成功后 Session Token 的有效期（小时）。

默认 24 小时。

到达有效期后：

> 客户端需要重新调用 `/api/v1/auth/login` 获取新的 Session Token。

该配置不引入用户表，仅用于控制简单认证的 Token 生命周期。

---

# 7. devices

设备配置是 V1 的核心配置之一。

```yaml
devices:
  - id: xiaomi14
    name: 小米14
    enabled: true
```

---

# 8. device.id

类型：

```text
string
```

要求：

```text
必须存在
必须唯一
创建后保持稳定
```

推荐字符：

```text
[a-z0-9][a-z0-9_-]*
```

例如：

```text
xiaomi13ultra
xiaomi14
macbook
windows-pc
iphone
```

---

## 8.1 id 的重要性

`device.id` 会写入数据库：

```text
source_device
```

并参与：

```text
source_device
+
original_filename
+
file_size
```

的预检去重。

因此：

> 不允许随意修改已经使用过的 device.id。

如果物理设备发生变化，应创建新的 Device ID。

---

# 9. device.name

类型：

```text
string
```

作用：

> WebUI 展示。

例如：

```yaml
id: xiaomi14
name: 小米14
```

`name` 可以修改。

但：

```text
id
```

不能随意修改。

---

# 10. device.enabled

类型：

```text
boolean
```

默认：

```text
true
```

如果：

```yaml
enabled: false
```

则：

> WebUI 不允许选择该设备进行新的上传。

历史照片中的：

```text
source_device
```

不受影响。

---

# 11. 设备配置规则

启动时检查：

```text
devices 不为空
id 不重复
id 格式正确
name 不为空
```

如果：

```text
两个设备使用相同 id
```

启动失败。

---

# 12. source_device 的使用原则

上传时：

```text
source_device
```

必须来自：

```text
devices[].id
```

禁止客户端直接传入任意字符串作为可信设备标识。

客户端只能提交：

```text
xiaomi14
```

系统必须检查：

```text
xiaomi14 是否存在
是否 enabled
```

---

# 13. storage

定义 R5S 本地存储结构。

```yaml
storage:
  root: /mnt/sda1/photo-gateway

  incoming: incoming
  processing: processing
  failed: failed

  archive: Photos

  database: database/photo-gateway.db
  logs: logs
```

---

# 14. storage.root

类型：

```text
string
```

示例：

```text
/mnt/sda1/photo-gateway
```

这是所有 Photo Gateway 数据的根目录。

---

# 15. storage.incoming

类型：

```text
string
```

默认：

```text
incoming
```

最终路径：

```text
${storage.root}/${storage.incoming}
```

例如：

```text
/mnt/sda1/photo-gateway/incoming
```

用于保存：

```text
*.tmp
```

---

# 16. storage.processing

默认：

```text
processing
```

最终：

```text
/mnt/sda1/photo-gateway/processing
```

用于保存 Worker 正在处理的文件。

---

# 17. storage.failed

默认：

```text
failed
```

最终：

```text
/mnt/sda1/photo-gateway/failed
```

用于保存无法自动恢复的失败文件。

---

# 18. storage.archive

默认：

```text
Photos
```

最终：

```text
/mnt/sda1/photo-gateway/Photos
```

正式照片目录结构：

```text
Photos/YYYY/YYYYMM/
```

例如：

```text
Photos/2026/202608/
```

---

# 19. storage.database

默认：

```text
database/photo-gateway.db
```

最终：

```text
/mnt/sda1/photo-gateway/database/photo-gateway.db
```

数据库必须位于：

```text
storage.root
```

下面。

---

# 20. storage.logs

默认：

```text
logs
```

最终：

```text
/mnt/sda1/photo-gateway/logs
```

日志文件保存于此。

---

# 21. 路径安全规则

以下配置：

```yaml
storage:
  root: /mnt/sda1/photo-gateway
  incoming: incoming
```

允许。

以下配置应该拒绝：

```yaml
incoming: ../../tmp
```

以及：

```yaml
archive: /etc
```

所有子路径必须解析后仍位于：

```text
storage.root
```

内部。

避免路径穿越导致照片写入系统其他目录。

---

# 22. 目录自动创建

首次启动时，如果目录不存在：

```text
incoming
processing
failed
Photos
database
logs
```

程序可以自动创建。

但是：

> `storage.root` 对应的挂载点必须已经存在。

例如：

```text
/mnt/sda1
```

不存在时：

> 程序必须启动失败。

程序不得自动创建一个普通目录来“冒充”存储盘。

这是为了防止：

```text
USB HDD 掉线
```

后程序继续运行并把照片写入系统 Flash。

---

# 23. upload

定义上传行为。

```yaml
upload:
  concurrency: 1

  precheck:
    batch_size: 5

  allowed_extensions:
    - jpg
    - jpeg
    ...

  tmp_cleanup:
    enabled: true
    max_age_days: 7
```

---

# 24. upload.concurrency

类型：

```text
integer
```

推荐默认：

```text
1
```

含义：

> HTTP 上传层允许同时处理的照片文件传输数量。

注意：

```text
upload.concurrency
```

和：

```text
upload.precheck.batch_size
```

以及：

```text
processing.worker_count
```

是三个不同概念：

```text
precheck.batch_size = 5
一次 HTTP Precheck 请求处理多少个文件的元数据

upload.concurrency = 1
同时传输多少个实际照片文件

worker_count = 1
后台同时处理多少个照片文件
```

由于 V1 实际运行在 R5S + USB HDD 上，推荐：

```text
upload.concurrency = 1
```

---

# 25. upload.precheck.batch_size

类型：

```text
integer
```

默认：

```text
5
```

含义：

> 单次 HTTP Precheck 请求最多包含的文件元数据数量。

例如一次选择 1000 张照片：

```text
1000 / 5 = 200 次请求
```

而不是：

```text
1000 次请求
```

这样可以在批量 Precheck 时显著减少 HTTP 往返次数。

该配置只影响 Precheck 请求的批量大小，不影响：

```text
upload.concurrency
```

和：

```text
processing.worker_count
```

---

# 26. processing.worker_count

V1：

```yaml
processing:
  worker_count: 1
```

固定推荐：

```text
1
```

原因：

R5S 的主要压力来自：

```text
磁盘 I/O
SHA-256
EXIF 解析
文件移动
```

因此：

> 上传可以存在一定并发，但照片后台处理严格单 Worker。

V1 可以允许配置该值，但推荐保持：

```text
1
```

如果配置：

```text
worker_count > 1
```

程序可以启动，但应该产生 WARNING。

---

# 27. upload.allowed_extensions

类型：

```text
list[string]
```

作用：

> 控制允许上传的照片文件扩展名。

扩展名：

```text
大小写不敏感
```

程序内部统一转换为小写比较。

例如：

```text
IMG_001.JPG
```

等价于：

```text
IMG_001.jpg
```

---

# 28. 推荐照片扩展名

V1 默认：

```yaml
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

---

# 29. 视频

V1：

> 不支持视频。

因此：

```text
mp4
mov
avi
mkv
```

不应该出现在默认允许列表中。

如果客户端上传：

```text
IMG_001.mp4
```

应在上传前直接拒绝。

---

# 30. 扩展名不是 MIME 类型校验

V1 第一层只根据：

```text
filename extension
```

判断是否允许。

但是处理阶段仍应读取文件实际内容。

例如：

```text
test.jpg
```

如果实际不是合法 JPEG：

> Processing 阶段必须失败。

不能因为扩展名正确就认为文件一定有效。

---

# 31. tmp_cleanup

定义 `.tmp` 自动清理。

```yaml
tmp_cleanup:
  enabled: true
  max_age_days: 7
```

---

# 32. tmp_cleanup.enabled

默认：

```text
true
```

如果：

```yaml
enabled: false
```

系统不自动删除历史 `.tmp` 文件。

但 WebUI 应显示：

> `.tmp` 文件可能持续占用磁盘空间。

---

# 33. tmp_cleanup.max_age_days

默认：

```text
7
```

类型：

```text
integer
```

最小：

```text
1
```

超过：

```text
7 × 24h
```

的 `.tmp` 文件进入清理范围。

清理动作必须记录：

```text
audit.log
```

或：

```text
processing.log
```

---

# 34. metadata

定义照片元数据处理规则。

```yaml
metadata:
  date_priority:
    - DateTimeOriginal
    - CreateDate
    - ModifyDate
    - file_mtime
```

`upload_time` 不作为归档日期来源，已从 V1 中删除。

---

# 35. metadata.date_priority

类型：

```text
list[string]
```

定义：

> 照片归档日期的候选来源优先级。

V1 支持：

```text
DateTimeOriginal
CreateDate
ModifyDate
file_mtime
```

不允许：

```text
upload_time
```

作为 `date_source`。

---

# 36. DateTimeOriginal

优先使用：

```text
EXIF DateTimeOriginal
```

这是照片拍摄时间的首选来源。

例如：

```text
2026:08:31 19:32:15
```

最终：

```text
2026/202608
```

---

# 37. CreateDate

如果没有：

```text
DateTimeOriginal
```

尝试：

```text
CreateDate
```

---

# 38. ModifyDate

如果前面都不存在：

```text
ModifyDate
```

---

# 39. file_mtime

如果 EXIF 时间均不存在：

> 使用文件 mtime。

---

# 40. disk

定义磁盘空间保护。

```yaml
disk:
  warning_percent: 80
  critical_percent: 90
  stop_percent: 95
```

---

# 41. disk.warning_percent

默认：

```text
80
```

达到后：

```text
WARNING
```

WebUI 显示磁盘空间警告。

---

# 42. disk.critical_percent

默认：

```text
90
```

达到后：

```text
CRITICAL
```

WebUI 显示严重空间不足。

---

# 43. disk.stop_percent

默认：

```text
95
```

达到后：

> 禁止新的上传任务开始。

已经上传到：

```text
incoming
```

中的文件可以继续处理。

---

# 44. 磁盘阈值关系

必须满足：

```text
warning_percent < critical_percent < stop_percent
```

例如：

```text
80 < 90 < 95
```

以下配置必须拒绝：

```yaml
warning_percent: 90
critical_percent: 80
stop_percent: 95
```

---

# 45. logging

日志配置：

```yaml
logging:
  level: INFO

  rotation:
    max_size_mb: 50
    retention_days: 30
```

---

# 46. logging.level

支持：

```text
DEBUG
INFO
WARNING
ERROR
CRITICAL
```

默认：

```text
INFO
```

生产环境推荐：

```text
INFO
```

调试问题时临时使用：

```text
DEBUG
```

---

# 47. logging.rotation.max_size_mb

默认：

```text
50
```

单个日志文件超过：

```text
50MB
```

进行轮转。

---

# 48. logging.rotation.retention_days

默认：

```text
30
```

日志保留：

```text
30 天
```

过期日志自动删除。

---

# 49. 日志文件

V1 至少生成：

```text
application.log
upload.log
processing.log
audit.log
error.log
```

配置文件只控制：

```text
level
rotation
retention
```

不允许通过配置关闭关键日志。

---

# 50. 不允许关闭的日志

以下事件必须记录：

```text
用户登录
上传 Session 创建
文件上传
上传失败
Precheck 命中
用户确认重新上传
SHA-256 结果
重复文件
文件归档
文件处理失败
任务恢复
临时文件清理
磁盘空间不足
配置加载失败
```

---

# 51. 配置热更新

V1：

> 不支持自动热加载。

修改：

```text
config.yaml
```

后需要：

```text
restart photo-gateway
```

重新加载。

原因：

V1 优先保证：

```text
配置一致性
实现简单
运行稳定
```

---

# 52. 配置修改的安全原则

修改设备：

```text
devices
```

或：

```text
storage
```

等重要配置时，必须经过：

```text
配置校验
```

成功后才允许应用。

如果配置错误：

> 保持当前正在运行的进程不受影响。

因此推荐运行机制：

```text
修改配置
 ↓
config validate
 ↓
成功
 ↓
restart
```

而不是运行中直接替换。

---

# 53. 配置验证命令

建议 V1 提供：

```bash
photo-gateway config validate
```

或者：

```bash
photo-gateway --config /etc/photo-gateway/config.yaml --check
```

成功：

```text
Configuration is valid.
```

失败：

```text
Configuration validation failed:
storage.root does not exist
```

---

# 54. 配置版本

配置文件顶层增加：

```yaml
version: 1
```

完整示例：

```yaml
version: 1

server:
  ...
```

程序读取配置时：

```text
version == 1
```

才按 V1 Schema 解析。

未来：

```text
version: 2
```

可以进行配置 Schema 升级。

---

# 55. 最终 Schema

V1 顶层结构固定为：

```text
version
server
auth
devices
storage
upload
processing
metadata
disk
logging
```

结构：

```text
config
├── version
├── server
│   ├── host
│   └── port
├── auth
│   ├── enabled
│   ├── username
│   ├── password_hash
│   └── session_ttl_hours
├── devices[]
│   ├── id
│   ├── name
│   └── enabled
├── storage
│   ├── root
│   ├── incoming
│   ├── processing
│   ├── failed
│   ├── archive
│   ├── database
│   └── logs
├── upload
│   ├── concurrency
│   ├── precheck
│   │   └── batch_size
│   ├── allowed_extensions[]
│   └── tmp_cleanup
│       ├── enabled
│       └── max_age_days
├── processing
│   └── worker_count
├── metadata
│   └── date_priority[]
├── disk
│   ├── warning_percent
│   ├── critical_percent
│   └── stop_percent
└── logging
    ├── level
    └── rotation
        ├── max_size_mb
        └── retention_days
```

---

# 56. 配置项总表

| 配置项                            | 类型   |                      默认值 | 必须 | 说明          |
| --------------------------------- | ------ | --------------------------: | ---- | ------------- |
| `version`                         | int    |                         `1` | 是   | 配置版本      |
| `server.host`                     | string |                   `0.0.0.0` | 否   | HTTP 监听地址 |
| `server.port`                     | int    |                      `8080` | 否   | HTTP 监听端口 |
| `auth.enabled`                    | bool   |                      `true` | 否   | 是否启用认证  |
| `auth.username`                   | string |                          无 | 是*  | 登录用户名    |
| `auth.password_hash`              | string |                          无 | 是*  | 密码 Hash     |
| `auth.session_ttl_hours`          | int    |                        `24` | 否   | Token 有效期  |
| `devices`                         | list   |                          无 | 是   | 设备列表      |
| `devices[].id`                    | string |                          无 | 是   | 稳定设备 ID   |
| `devices[].name`                  | string |                          无 | 是   | 展示名称      |
| `devices[].enabled`               | bool   |                      `true` | 否   | 是否允许上传  |
| `storage.root`                    | string |                          无 | 是   | 存储根目录    |
| `storage.incoming`                | string |                  `incoming` | 否   | 临时目录      |
| `storage.processing`              | string |                `processing` | 否   | 处理目录      |
| `storage.failed`                  | string |                    `failed` | 否   | 失败目录      |
| `storage.archive`                 | string |                    `Photos` | 否   | 正式照片目录  |
| `storage.database`                | string | `database/photo-gateway.db` | 否   | SQLite        |
| `storage.logs`                    | string |                      `logs` | 否   | 日志目录      |
| `upload.concurrency`              | int    |                         `1` | 否   | 上传并发      |
| `upload.precheck.batch_size`      | int    |                         `5` | 否   | Precheck 批量 |
| `upload.allowed_extensions`       | list   |                      见正文 | 是   | 允许扩展名    |
| `upload.tmp_cleanup.enabled`      | bool   |                      `true` | 否   | 是否清理 tmp  |
| `upload.tmp_cleanup.max_age_days` | int    |                         `7` | 否   | tmp 保留时间  |
| `processing.worker_count`         | int    |                         `1` | 否   | Worker 数量   |
| `metadata.date_priority`          | list   |                      见正文 | 否   | 日期优先级    |
| `disk.warning_percent`            | int    |                        `80` | 否   | WARNING       |
| `disk.critical_percent`           | int    |                        `90` | 否   | CRITICAL      |
| `disk.stop_percent`               | int    |                        `95` | 否   | 停止上传      |
| `logging.level`                   | enum   |                      `INFO` | 否   | 日志等级      |
| `logging.rotation.max_size_mb`    | int    |                        `50` | 否   | 单文件大小    |
| `logging.rotation.retention_days` | int    |                        `30` | 否   | 日志保留      |

`*` 当 `auth.enabled=true` 时必须存在。

---

# 57. V1 不进入配置文件的内容

以下内容明确不配置：

```text
NAS 地址
NAS 用户名
NAS 密码
NAS 挂载点
rsync 参数
NAS 同步周期
NAS 同步策略
Immich 地址
Immich API
人脸识别参数
AI 模型
公网访问
Cloudflare
视频处理参数
照片压缩参数
```

原因：

> 它们均属于 V1 范围之外。

---

# 58. 配置与 WebUI 的关系

V1 初期：

```text
config.yaml
```

作为唯一配置来源。

后续 WebUI 可以提供：

```text
设备管理
上传扩展名管理
磁盘阈值
日志设置
```

等配置界面。

但是 WebUI 修改配置时：

> 最终仍然必须生成符合本 Schema 的配置。

因此：

```text
WebUI
   │
   ▼
Config API
   │
   ▼
Config Validator
   │
   ▼
config.yaml
```

数据库不应该成为配置的唯一来源。

---

# 59. 配置文件权限

由于配置包含：

```text
password_hash
```

建议权限：

```text
0600
```

例如：

```bash
chmod 600 /etc/photo-gateway/config.yaml
```

目录建议：

```text
0700
```

---

# 60. 配置加载失败处理

以下情况必须阻止服务启动：

```text
YAML 格式错误
version 不支持
设备 ID 重复
设备 ID 非法
storage.root 不存在
路径穿越
端口非法
磁盘阈值非法
worker_count < 1
concurrency < 1
precheck.batch_size < 1
allowed_extensions 为空
date_priority 为空
认证启用但用户名为空
认证启用但 password_hash 为空
```

启动失败必须记录：

```text
application.log
```

如果日志目录本身不可用，则至少输出：

```text
stderr
```

---

# 61. 冻结结论

Photo Gateway V1 配置系统最终遵循：

```text
配置文件：
    系统规则

SQLite：
    运行状态

文件系统：
    原始照片及处理文件

日志：
    操作过程与审计记录
```

V1 配置重点保证：

```text
设备身份固定
+
上传规则可配置
+
目录结构固定
+
照片日期规则明确
+
磁盘安全阈值明确
+
日志行为明确
+
未来可以由 WebUI 管理
```

---

# 62. V1 Frozen Baseline

本文件与：

```text
photo-gateway-v1-spec.md
```

共同构成 Photo Gateway V1 的冻结设计基线。

后续：

```text
photo-gateway-v1-db-spec.md
photo-gateway-v1-api-spec.md
photo-gateway-v1-webui-spec.md
```

均必须以本文档定义的配置模型为基础。

如果后续实现发现必须修改本文档定义的配置语义，应先更新版本：

```text
V1.0
→
V1.x
```

并记录变更原因。

未经确认，不得直接修改冻结配置语义。