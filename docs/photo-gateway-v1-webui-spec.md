# Photo Gateway V1 WebUI Specification

**文件名：** `photo-gateway-v1-webui-spec.md`  
**版本：** V1.0  
**状态：** Frozen Baseline  
**项目：** Photo Gateway

---

# 1. 文档目的

本文档定义 Photo Gateway V1 WebUI 的功能、页面、交互、状态展示及 API 调用规范。

WebUI 的主要职责：

1. 选择上传设备
2. 选择照片
3. 创建上传任务
4. 执行上传前 Precheck
5. 展示疑似重复文件
6. 上传照片
7. 展示实时上传进度
8. 展示处理进度
9. 展示失败文件
10. 支持失败重试
11. 支持中断后恢复任务
12. 查询已导入照片及基本信息
13. 查看系统及 Worker 状态

WebUI 不负责：

- 修改原始照片
- 修改 EXIF
- 管理 NAS
- 管理正式照片目录
- 人脸识别
- 视频管理
- 用户权限管理
- Immich 管理

---

# 2. 设计原则

## 2.1 WebUI 是 Photo Gateway 的控制面

整体结构：

```text
┌─────────────────────────────┐
│           WebUI             │
│                             │
│ 上传 / 任务 / 照片 / 系统状态 │
└──────────────┬──────────────┘
               │ HTTP API
               ▼
┌─────────────────────────────┐
│        Photo Gateway        │
│                             │
│ API + Worker + SQLite       │
└──────────────┬──────────────┘
               │
               ▼
          文件系统
```

WebUI 不直接访问 SQLite。

WebUI 不直接访问服务器文件系统。

所有数据通过 API 获取。

---

# 3. 使用场景

V1 主要针对家庭局域网环境。

典型使用方式：

```text
手机
 │
 │ WiFi
 ▼
R5S / Photo Gateway
 │
 ▼
上传照片
```

用户通过手机浏览器访问 WebUI。

也可以通过：

```text
MacBook
Windows PC
```

访问 WebUI。

---

# 4. V1 设备

设备由 Photo Gateway 配置文件定义。

示例：

```text
小米 13 Ultra
小米 14
MacBook Air
Windows PC
iPhone
```

WebUI 不允许用户自由输入设备名称。

---

# 5. 首页

URL：

```text
/
```

首页显示系统概况。

推荐布局：

```text
┌─────────────────────────────────┐
│ Photo Gateway                   │
├─────────────────────────────────┤
│                                 │
│  系统状态     正常              │
│  Worker       空闲              │
│  待处理       12                │
│  失败         2                 │
│                                 │
├─────────────────────────────────┤
│                                 │
│  最近上传任务                   │
│                                 │
│  小米 14     2026-09-02 09:20   │
│  1000 文件   处理中              │
│                                 │
│  小米13 Ultra 2026-09-01 20:10  │
│  325 文件    已完成              │
│                                 │
├─────────────────────────────────┤
│                                 │
│       [ 开始上传照片 ]           │
│                                 │
└─────────────────────────────────┘
```

---

# 6. 首页状态

系统状态来自：

```http
GET /api/v1/system/status
```

Worker 状态来自：

```http
GET /api/v1/system/worker
```

---

# 7. 导航栏

V1 建议提供：

```text
首页
上传
任务
照片
系统
```

其中：

```text
上传
```

为最主要入口。

---

# 8. 上传页面

URL：

```text
/upload
```

页面流程：

```text
选择设备
    ↓
选择照片
    ↓
Precheck
    ↓
确认疑似重复
    ↓
开始上传
    ↓
上传完成
    ↓
后台处理
```

---

# 9. 第一步：选择设备

页面：

```text
选择照片来源设备
```

例如：

```text
○ 小米 13 Ultra
○ 小米 14
○ MacBook Air
○ Windows PC
○ iPhone
```

设备列表：

```http
GET /api/v1/devices
```

---

# 10. 设备选择规则

必须选择：

```text
source_device
```

才能进入下一步。

不能：

```text
手工输入设备名称
```

不能：

```text
留空
```

不能使用：

```text
浏览器 User-Agent
```

代替设备身份。

---

# 11. 第二步：选择照片

使用浏览器原生：

```text
<input type="file" multiple>
```

允许：

```text
多选
```

支持目录选择时，可以使用浏览器支持的目录选择能力。

---

# 12. 文件类型

前端只允许选择服务器配置中允许的照片类型。

例如：

```text
jpg
jpeg
png
heic
webp
```

最终允许类型以服务器配置为准。

前端限制只是：

> 用户体验优化。

不能替代服务器校验。

---

# 13. 文件选择后的信息

选择完成后立即显示：

```text
已选择：
1000 个文件

总大小：
8.72 GB
```

列表：

| 文件名      |  大小 | 状态   |
| ----------- | ----: | ------ |
| IMG_001.jpg | 12 MB | 待检查 |
| IMG_002.jpg | 15 MB | 待检查 |
| IMG_003.jpg | 18 MB | 待检查 |

---

# 14. 创建 Session

用户点击：

```text
开始检查
```

WebUI：

```http
POST /api/v1/upload-sessions
```

请求：

```json
{
  "source_device": "xiaomi14"
}
```

获得：

```text
session_id
```

然后逐个创建 Upload Item。

---

# 15. Precheck 阶段

WebUI 对文件执行 Precheck。

可以逐条调用：

```http
POST /api/v1/upload-sessions/{session_id}/items
```

请求：

```json
{
  "client_item_id": "uuid-1",
  "filename": "IMG_001.jpg",
  "file_size": 12345678
}
```

也可以使用批量 Precheck：

```http
POST /api/v1/upload-sessions/{session_id}/items/precheck
```

单批大小：

```text
upload.precheck.batch_size
```

默认 5。

批量请求：

```json
{
  "items": [
    {
      "client_item_id": "uuid-1",
      "filename": "IMG_001.jpg",
      "file_size": 12345678
    },
    {
      "client_item_id": "uuid-2",
      "filename": "IMG_002.jpg",
      "file_size": 23456789
    }
  ]
}
```

服务器返回：

```text
NO_MATCH
```

或者：

```text
POSSIBLE_DUPLICATE
```

`client_item_id` 保证批量 Precheck 可安全重试（幂等）。

---

# 16. Precheck 页面

例如：

```text
正在检查照片……

已检查：800 / 1000

无匹配：650
疑似重复：150
待检查：200
```

Precheck 本身不上传文件内容。

因此速度应该明显快于实际上传。

---

# 17. Precheck 完成

例如：

```text
检查完成

总计：1000

需要上传：100
疑似重复：900
```

WebUI 必须明确区分：

```text
需要上传
```

和：

```text
疑似重复
```

---

# 18. 疑似重复页面

显示：

```text
发现 900 个疑似重复文件

这些文件根据：

设备 + 原始文件名 + 文件大小

判断为可能已经上传。

这不是最终 SHA-256 重复判断。
```

---

# 19. 疑似重复列表

例如：

| 文件名      |  大小 | 状态     | 操作     |
| ----------- | ----: | -------- | -------- |
| IMG_001.jpg | 12 MB | 疑似重复 | 重新上传 |
| IMG_002.jpg | 15 MB | 疑似重复 | 重新上传 |
| IMG_003.jpg | 18 MB | 疑似重复 | 重新上传 |

提供：

```text
[ 全部重新上传 ]
[ 选择重新上传 ]
[ 跳过 ]
```

---

# 20. 全部重新上传

点击：

```text
全部重新上传
```

WebUI 对所有：

```text
POSSIBLE_DUPLICATE
```

调用：

```http
POST /api/v1/upload-items/{item_id}/reupload
```

请求：

```json
{
  "confirmed": true
}
```

然后这些文件进入实际上传队列。

---

# 21. 选择性重新上传

用户可以勾选：

```text
IMG_001.jpg
IMG_008.jpg
IMG_021.jpg
```

只确认这些文件。

其余：

```text
POSSIBLE_DUPLICATE
```

保持不上传。

---

# 22. 正式上传页面

上传页面显示：

```text
正在上传

设备：小米 14

总文件：1000
需要上传：120
已完成：83
失败：1

当前文件：

IMG_083.jpg

12.4 MB / 18.2 MB

68%
```

---

# 23. 单文件上传进度

进度条必须显示：

```text
当前文件
```

以及：

```text
已上传字节
总字节
百分比
```

例如：

```text
IMG_083.jpg

12.4 MB / 18.2 MB

━━━━━━━━━━━━░░░░░

68%
```

---

# 24. 总体上传进度

同时显示：

```text
83 / 120
```

表示已经完成 83 个文件。

不使用所有文件大小简单计算百分比作为唯一进度。

因为：

```text
文件大小差异可能非常大。
```

---

# 25. 上传速度

可以在浏览器端计算：

```text
MB/s
```

例如：

```text
速度：18.3 MB/s
```

该数据属于：

> 前端实时状态。

不写入数据库。

---

# 26. 剩余时间

WebUI 可以根据近期上传速度估算：

```text
预计剩余：2 分 35 秒
```

如果无法可靠计算：

```text
不显示
```

不能显示虚假的精确时间。

---

# 27. 上传顺序

V1 推荐：

```text
一个文件上传完成
        ↓
下一个文件
```

即客户端顺序上传。

不要求同时上传多个大文件。

---

# 28. 为什么不使用多并发

R5S 是低功耗网关。

V1 优先保证：

```text
稳定性
```

而不是：

```text
极限上传速度
```

因此：

```text
客户端上传并发 = 1
Worker 并发 = 1
```

作为 V1 默认行为。

---

# 29. 上传完成

文件成功传输到：

```text
incoming/
```

之后 WebUI 显示：

```text
上传完成
```

但：

> 上传完成不等于照片最终处理完成。

---

# 30. Processing 状态

Worker 开始处理后：

```text
UPLOADED
    ↓
PROCESSING
```

WebUI 显示：

```text
正在处理
```

---

# 31. Worker 处理流程

WebUI 不需要展示内部所有步骤，但可以显示当前阶段：

```text
SHA-256
    ↓
EXIF
    ↓
重复判断
    ↓
归档
```

例如：

```text
当前：
正在解析 EXIF
```

如果后端暂时无法提供细粒度阶段，则统一显示：

```text
处理中
```

---

# 32. 文件最终状态

WebUI 必须支持：

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

# 33. 状态颜色

建议使用固定语义：

```text
待处理       中性
上传中       信息
处理中       信息
完成         成功
重复         警告
失败         错误
```

具体颜色由 UI 框架决定。

状态不能只依赖颜色表达。

必须同时显示文字。

---

# 34. 任务列表

URL：

```text
/tasks
```

显示历史 Session。

例如：

| 时间        | 设备         | 文件 | 状态     |
| ----------- | ------------ | ---: | -------- |
| 09-02 09:20 | 小米14       | 1000 | 处理中   |
| 09-01 20:10 | 小米13 Ultra |  325 | 已完成   |
| 08-31 18:30 | 小米14       |   87 | 部分失败 |

---

# 35. 任务详情

URL：

```text
/tasks/{session_id}
```

页面：

```text
小米 14
2026-09-02 09:20

总计：1000
完成：850
重复：50
失败：10
处理中：90
疑似重复：0
```

---

# 36. 任务文件筛选

支持：

```text
全部
上传中
处理中
完成
重复
失败
疑似重复
```

例如：

```text
失败：10
```

点击后只显示：

```text
FAILED
```

文件。

---

# 37. 失败文件页面

例如：

```text
失败文件：10

IMG_001.jpg
EXIF_FAILED

IMG_002.jpg
ARCHIVE_FAILED

IMG_003.jpg
STORAGE_ERROR
```

每项提供：

```text
[ 重试 ]
```

---

# 38. 全部重试

如果存在多个失败文件：

```text
[ 全部重试 ]
```

WebUI 对失败项目逐个调用：

```http
POST /api/v1/upload-items/{item_id}/retry
```

然后重新上传。

---

# 39. 失败原因

错误信息必须同时显示：

```text
error_code
```

和：

```text
error_message
```

`error_code` 使用 API Spec 中的统一 Error Code Registry。

例如：

```text
EXIF_FAILED
EXIF metadata parsing failed
```

用户主要看到：

```text
EXIF 解析失败
```

详细技术信息可以通过：

```text
查看详情
```

展开。

---

# 40. 手机锁屏后的恢复

用户手机上传过程中：

```text
锁屏
```

或者：

```text
浏览器被系统暂停
```

导致连接中断。

重新打开 WebUI 后：

```text
查询最近 Session
```

用户可以进入：

```text
继续上传
```

---

# 41. 恢复任务

WebUI 首先：

```http
GET /api/v1/upload-sessions/{session_id}
```

然后：

```http
GET /api/v1/upload-sessions/{session_id}/items
```

根据服务器状态重新建立客户端上传队列。

---

# 42. 恢复原则

已经：

```text
COMPLETED
DUPLICATE
```

的文件：

> 不重新上传。

已经：

```text
UPLOADED
PROCESSING
```

的文件：

> 不重复上传。

需要重新处理的：

```text
FAILED
```

重新执行 Retry。

尚未上传：

```text
PRECHECK
```

重新上传。

---

# 43. `.tmp` 文件

WebUI 不直接显示：

```text
.tmp
```

文件。

`.tmp` 是服务器内部实现细节。

如果上传中断：

```text
IMG_001.jpg.tmp
```

WebUI 只显示：

```text
上传失败
```

---

# 44. 浏览器关闭

浏览器关闭不会导致 Session 被删除。

重新打开：

```text
/tasks
```

可以找到原任务。

用户可以继续：

```text
失败文件
```

和：

```text
未完成文件
```

---

# 45. 网络恢复

网络恢复后：

```text
WebUI
   ↓
查询 Session
   ↓
获取未完成项目
   ↓
继续上传
```

不会重新上传已经成功完成的文件。

---

# 46. 照片页面

URL：

```text
/photos
```

用于查看已经形成 `photo_assets` 的照片。

V1 只提供基础浏览。

---

# 47. 照片列表

显示：

```text
缩略图
文件名
拍摄时间
文件大小
```

例如：

```text
┌────────┐
│        │
│ PHOTO  │
│        │
└────────┘

IMG_001.jpg
2026-08-31
12.3 MB
```

---

# 48. 照片查询

支持：

```text
日期范围
文件名
设备
MIME 类型
```

对应 API：

```http
GET /api/v1/photos
```

---

# 49. 照片详情

点击照片进入：

```text
/photos/{id}
```

显示：

```text
文件名
拍摄时间
日期来源
文件大小
MIME
SHA-256
正式路径
EXIF
```

---

# 50. EXIF 展示

EXIF 以折叠区域显示：

```text
EXIF 信息
```

例如：

```text
Make: Xiaomi
Model: Xiaomi 14
ImageWidth: 4080
ImageHeight: 3072
```

不要求 V1 对所有 EXIF 字段进行 UI 特殊处理。

未知字段可以直接展示。

---

# 51. 原图查看

点击：

```text
查看原图
```

调用：

```http
GET /api/v1/photos/{photo_id}/file
```

浏览器直接显示原图。

不修改原始文件。

---

# 52. 缩略图

如果 API 实现：

```http
GET /api/v1/photos/{photo_id}/thumbnail
```

WebUI 优先使用缩略图。

否则可以暂时使用原图。

---

# 53. 系统页面

URL：

```text
/system
```

显示：

```text
Photo Gateway

运行状态
数据库状态
Worker 状态
临时目录状态
存储空间
```

---

# 54. Worker 状态

例如：

```text
Worker

状态：处理中

当前文件：
IMG_083.jpg

开始时间：
09:32:10
```

如果空闲：

```text
Worker

状态：空闲
```

---

# 55. 存储状态

显示：

```text
可用空间
已使用空间
```

例如：

```text
临时空间

已使用：126 GB
可用：317 GB
```

具体统计方式由后端 API 决定。

---

# 56. 轮询

V1 不要求 WebSocket。

WebUI 使用 HTTP Polling 即可。

推荐：

```text
上传进行中：
1 秒轮询

处理进行中：
2 秒轮询

普通任务列表：
5~10 秒轮询或手动刷新
```

浏览器端上传百分比直接根据当前 HTTP 请求计算。

---

# 57. 页面刷新

用户刷新页面：

> 不得导致服务器上传任务丢失。

WebUI 根据：

```text
session_id
```

重新读取状态。

---

# 58. 浏览器返回

用户点击浏览器返回：

> 不得取消 Session。

只有用户明确点击：

```text
取消任务
```

才调用：

```http
POST /api/v1/upload-sessions/{session_id}/cancel
```

---

# 59. 防止误取消

点击：

```text
取消任务
```

必须弹出确认：

```text
确定取消本次上传？

已经完成的照片不会被删除。

未完成的文件将停止继续上传。

[取消] [确认取消]
```

---

# 60. 上传中的页面离开

如果当前文件正在上传：

```text
离开页面
```

WebUI 应提示：

```text
当前正在上传文件。

离开页面可能导致当前文件上传中断。

[留在页面] [离开]
```

服务器不会因此删除整个 Session。

---

# 61. 大批量文件列表

1000+ 文件不能一次性渲染全部 DOM。

WebUI 应使用：

```text
分页
```

或者：

```text
Virtual List
```

例如：

```text
显示 1-100 / 1000
```

---

# 62. 前端状态管理

推荐区分：

```text
Session 状态
Item 状态
当前上传状态
Worker 状态
```

不能只维护一个：

```text
uploading = true
```

变量。

---

# 63. Session 状态

前端至少支持：

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

# 64. Item 状态

前端至少支持：

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

# 65. Precheck 状态

前端至少支持：

```text
NOT_CHECKED
NO_MATCH
POSSIBLE_DUPLICATE
USER_CONFIRMED
```

---

# 66. 前端状态机

单个文件：

```text
PRECHECK
   │
   ├── NO_MATCH
   │      │
   │      ▼
   │   UPLOADING
   │      │
   │      ▼
   │   UPLOADED
   │      │
   │      ▼
   │   PROCESSING
   │      │
   │   ┌──┴─────────┐
   │   ▼            ▼
   │ COMPLETED   DUPLICATE
   │
   └── POSSIBLE_DUPLICATE
              │
              ▼
        用户确认重新上传
              │
              ▼
           UPLOADING
```

任意处理阶段可能：

```text
FAILED
```

---

# 67. 网络错误处理

网络异常：

```text
Network Error
```

WebUI 不立即把整个 Session 标记为失败。

只将当前文件标记为：

```text
待重试
```

然后允许：

```text
重试
```

---

# 68. HTTP 401

如果 API 返回：

```text
401
```

WebUI：

```text
当前认证已失效，请重新认证。
```

并引导用户跳转到登录页：

```http
POST /api/v1/auth/login
```

重新获取 Session Token。

不应该不断自动重试。

---

# 69. HTTP 409

如果：

```text
UPLOAD_ALREADY_IN_PROGRESS
```

WebUI：

```text
当前文件正在上传，请稍候。
```

不应该重复创建上传请求。

---

# 70. HTTP 413

如果：

```text
FILE_TOO_LARGE
```

WebUI：

```text
文件超过服务器允许的大小。
```

该文件标记为失败。

其他文件继续上传。

---

# 71. HTTP 415

如果：

```text
FILE_TYPE_NOT_ALLOWED
```

WebUI：

```text
不支持该照片格式。
```

其他文件继续上传。

---

# 72. HTTP 507 / 存储不足

如果后端无法继续接收：

```text
STORAGE_ERROR
```

WebUI 必须明显提示：

```text
服务器存储空间不足，上传已暂停。
```

不应该继续高速重试。

---

# 73. 文件名展示

文件名必须完整支持：

```text
中文
英文
数字
空格
Unicode
```

长文件名：

```text
最多显示一行
```

通过：

```text
tooltip
```

显示完整名称。

---

# 74. 时间显示

服务器 API 时间使用：

```text
UTC ISO-8601
```

WebUI 显示时转换为：

```text
浏览器本地时区
```

例如服务器：

```text
2026-09-02T01:30:00Z
```

用户界面可以显示：

```text
2026-09-02 09:30
```

---

# 75. 日期来源

照片详情显示：

```text
拍摄时间：
2026-08-31 19:32:15

来源：
DateTimeOriginal
```

可能的来源：

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

作为日期来源展示。

`upload_time` 已从 V1 中删除。

---

# 76. 照片路径展示

显示：

```text
Photos/2026/202608/IMG_001.jpg
```

不要求 WebUI 显示服务器：

```text
/Volumes/...
/mnt/...
/data/...
```

等绝对路径。

---

# 77. API 与 WebUI 职责

```text
WebUI
 │
 ├── 用户操作
 ├── 文件选择
 ├── 上传进度
 ├── 状态展示
 └── API 调用
       │
       ▼
      API
       │
       ├── SQLite
       └── Worker
```

WebUI 不实现：

```text
SHA256
EXIF 解析
重复判断
归档
```

这些全部属于后端。

---

# 78. 上传核心交互

完整流程：

```text
用户打开 /upload
       │
       ▼
选择设备
       │
       ▼
选择 1000 张照片
       │
       ▼
创建 Session
       │
       ▼
Precheck 1000 张
       │
       ├──────────────┐
       ▼              ▼
  NO_MATCH       POSSIBLE_DUPLICATE
       │              │
       ▼              ▼
  直接上传       显示疑似重复
       │              │
       │        用户选择重新上传
       │              │
       └───────┬──────┘
               ▼
          实际文件上传
               │
               ▼
           incoming
               │
               ▼
             Worker
               │
        ┌──────┴──────┐
        ▼             ▼
    COMPLETED      DUPLICATE
        │
        ▼
      照片库
```

---

# 79. V1 不做的 WebUI 功能

以下功能明确不属于 V1：

```text
照片删除
照片移动
照片重命名
照片编辑
EXIF 编辑
人脸识别
AI 标签
地图
时间轴高级浏览
相册管理
视频管理
NAS 管理
NAS 同步控制
公网访问
多用户管理
权限管理
```

---

# 80. 与 NAS 的边界

WebUI 不提供：

```text
NAS 在线/离线控制
NAS 同步
rsync
NAS 磁盘管理
备份管理
```

后续 NAS 同步功能应该作为独立模块加入。

---

# 81. 与 Immich 的边界

WebUI 不复制 Immich 的：

```text
照片管理
相册
人脸
地图
搜索
```

Photo Gateway 的核心目标仍然是：

> 稳定地把照片导入正式照片库。

---

# 82. V1 技术实现建议

WebUI 不要求绑定具体前端框架。

可以使用：

```text
React
Vue
Svelte
```

等实现。

但必须满足：

- 移动端可用
- 桌面端可用
- 支持多文件上传
- 支持上传进度
- 支持任务恢复
- 支持分页
- 不依赖公网 CDN
- 核心功能可以在局域网离线环境使用

---

# 83. 移动端优先

由于主要上传设备是 Android 手机，因此：

```text
手机浏览器
```

是 V1 第一优先级。

页面必须适配：

```text
375px+
```

宽度。

桌面端则使用响应式布局。

---

# 84. 移动端上传注意事项

浏览器上传大量照片时：

- 不要求一次读取全部照片到内存
- 不将照片转换为 Base64
- 不把照片全部加载进 JavaScript 内存
- 使用浏览器原生 File/Blob API
- 一个文件完成后再处理下一个文件

这样可以降低：

```text
手机内存压力
浏览器崩溃概率
```

---

# 85. 前端不计算 SHA-256

V1：

```text
SHA-256 = 服务端 Worker 计算
```

WebUI 不要求在浏览器端计算 SHA-256。

原因：

```text
手机 CPU
浏览器内存
大文件读取
```

都会增加额外开销。

而且最终判断必须以服务器计算结果为准。

---

# 86. 上传完成后的提示

全部上传请求结束后：

```text
文件传输完成。

已上传：120
疑似重复：880
失败：0

照片正在后台处理中。
```

不能提示：

```text
全部照片已经完成
```

除非 Session 已经：

```text
COMPLETED
```

---

# 87. 最终完成提示

Session：

```text
COMPLETED
```

时：

```text
本次任务已完成。

总文件：1000
成功：850
重复：150
失败：0
```

---

# 88. Partial 提示

如果：

```text
FAILED > 0
```

则：

```text
本次任务部分完成。

成功：850
重复：140
失败：10

[查看失败文件]
```

---

# 89. 系统异常提示

如果 Worker 异常：

```text
Worker 状态异常。

当前照片处理可能已经暂停。

请检查系统状态。
```

不应该要求用户重新上传已经成功接收的照片。

---

# 90. V1 数据安全原则

WebUI：

- 不缓存原图到 localStorage
- 不把照片 Base64 保存到 localStorage
- 不把 Token 写入 URL
- 不把服务器绝对路径暴露给客户端
- 不允许客户端指定归档路径
- 不允许客户端修改 `sha256`
- 不允许客户端修改 `asset_id`

---

# 91. 浏览器缓存

静态 WebUI 资源可以缓存。

但任务状态必须通过 API 获取最新数据。

不能依赖浏览器缓存判断：

```text
COMPLETED
FAILED
PROCESSING
```

---

# 92. WebUI 与 API 版本

WebUI V1 只调用：

```text
/api/v1/*
```

不能直接访问：

```text
SQLite
Filesystem
Worker internal API
```

---

# 93. 最低页面集合

V1 至少实现：

```text
/
首页

/upload
上传

/tasks
任务列表

/tasks/{session_id}
任务详情

/photos
照片列表

/photos/{id}
照片详情

/system
系统状态
```

---

# 94. V1 最低可用功能

如果第一阶段需要进一步压缩开发范围，最低可用 WebUI 可以只实现：

```text
上传
任务
系统状态
```

其中上传必须支持：

```text
设备选择
多文件选择
Precheck
疑似重复确认
上传进度
失败重试
任务恢复
```

---

# 95. V1 WebUI 验收标准

## 95.1 设备

能够：

```text
加载设备列表
选择设备
禁止未选择设备上传
```

---

## 95.2 批量上传

能够：

```text
选择 1000 个文件
创建 Session
完成 Precheck
显示 Precheck 结果
```

---

## 95.3 疑似重复

能够：

```text
显示 POSSIBLE_DUPLICATE
全部确认重新上传
选择部分重新上传
```

---

## 95.4 上传

能够：

```text
逐文件上传
显示当前文件进度
显示整体文件进度
显示上传速度
```

---

## 95.5 中断恢复

模拟：

```text
上传 500 / 1000
关闭浏览器
重新打开
```

必须能够：

```text
找到原 Session
识别已经完成的文件
继续剩余文件
```

---

## 95.6 失败重试

模拟：

```text
某文件上传失败
```

必须能够：

```text
显示失败
显示错误原因
点击重试
重新上传
```

---

## 95.7 Worker

能够看到：

```text
Worker 空闲
```

或者：

```text
Worker 处理中
```

---

## 95.8 照片

能够查看：

```text
照片列表
照片详情
拍摄时间
EXIF
SHA-256
正式相对路径
```

---

# 96. V1 最终架构

```text
                         ┌───────────────┐
                         │ Android       │
                         │ iPhone        │
                         │ Mac           │
                         │ Windows       │
                         └───────┬───────┘
                                 │
                              Browser
                                 │
                                 ▼
                      ┌────────────────────┐
                      │       WebUI        │
                      │                    │
                      │ Upload             │
                      │ Tasks              │
                      │ Photos             │
                      │ System             │
                      └─────────┬──────────┘
                                │
                             HTTP API
                                │
                                ▼
                    ┌──────────────────────┐
                    │   Photo Gateway      │
                    │                      │
                    │ API                  │
                    │ SQLite               │
                    │ Worker × 1           │
                    └──────────┬───────────┘
                               │
                     ┌─────────┴─────────┐
                     │                   │
                     ▼                   ▼
                 incoming/           photo_assets
                     │                   │
                     ▼                   │
                  Worker                 │
                     │                   │
                     ▼                   │
                 archive                 │
                     │                   │
                     └─────────┬─────────┘
                               ▼
                       Formal Photo Library
```

---

# 97. V1 冻结结论

Photo Gateway V1 WebUI 的核心定位：

> **一个面向家庭局域网的照片导入控制台，而不是完整的照片管理软件。**

V1 WebUI 最重要的能力是：

```text
选择设备
    ↓
选择大量照片
    ↓
快速 Precheck
    ↓
过滤疑似重复
    ↓
用户确认
    ↓
逐文件上传
    ↓
显示实时进度
    ↓
后台处理
    ↓
失败可重试
    ↓
中断可恢复
```

同时保证：

```text
WebUI
```

与：

```text
API
DB
Worker
Filesystem
```

职责清晰分离。

---

# 98. V1 范围冻结

以下内容属于 V1 Frozen Baseline：

```text
WebUI 页面结构
设备选择
批量文件选择
Session 管理
Precheck 展示
疑似重复确认
单文件顺序上传
实时上传进度
任务进度
失败重试
任务恢复
Worker 状态
照片基础查询
照片详情
系统状态
```

以下内容不进入 V1：

```text
NAS 同步
照片删除
照片编辑
EXIF 编辑
视频
AI
人脸
地图
相册
公网
多用户
复杂权限
Immich 集成
```

后续增加上述能力时，应通过新的规格文档和版本进行设计，不直接修改 V1 Frozen Baseline。