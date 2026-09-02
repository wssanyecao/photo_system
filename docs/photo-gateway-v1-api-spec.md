# Photo Gateway V1 API Specification

**文件名：** `photo-gateway-v1-api-spec.md`  
**版本：** V1.0  
**状态：** Frozen Baseline  
**项目：** Photo Gateway

---

# 1. 文档目的

本文档定义 Photo Gateway V1 HTTP API。

API 面向：

- Android 手机浏览器
- iOS 浏览器
- macOS 浏览器
- Windows 浏览器
- 后续可能的专用客户端
- WebUI

API 负责：

1. 认证
2. 设备识别
3. 创建上传 Session
4. 上传文件
5. Precheck
6. 查询上传进度
7. 查询失败文件
8. 重新上传失败文件
9. 确认疑似重复文件
10. 查询照片资产
11. 查询照片元信息
12. 查询系统状态

V1 不负责：

- NAS 同步
- 公网访问
- Immich 管理
- 人脸识别
- 视频管理
- 照片删除
- 修改 EXIF
- 修改正式照片目录结构

---

# 2. API 基础规范

默认 Base URL：

```text
/api/v1
```

例如：

```text
http://photo-gateway.local/api/v1
```

实际监听地址和端口由配置文件决定。

---

# 3. HTTP 方法

| 方法   | 用途              |
| ------ | ----------------- |
| GET    | 查询              |
| POST   | 创建/执行操作     |
| PATCH  | 修改资源状态      |
| DELETE | V1 不用于删除照片 |

---

# 4. Content-Type

普通 API：

```http
Content-Type: application/json
```

文件上传：

```http
Content-Type: multipart/form-data
```

---

# 5. 字符编码

统一：

```text
UTF-8
```

文件名必须支持 Unicode。

例如：

```text
IMG_001.jpg
微信图片_20260831_001.jpg
照片①.jpg
```

---

# 6. API 认证

V1 使用简单认证。

认证方式：

```http
Authorization: Bearer <token>
```

例如：

```http
Authorization: Bearer xxxxxxxxx
```

Token 从配置文件读取。

后续 WebUI 可以通过登录接口获取 Session Token，但 V1 不要求实现复杂用户体系。

---

# 7. 未认证响应

HTTP：

```text
401 Unauthorized
```

响应：

```json
{
  "error": {
    "code": "AUTH_REQUIRED",
    "message": "authentication required"
  }
}
```

---

# 8. 权限不足

HTTP：

```text
403 Forbidden
```

响应：

```json
{
  "error": {
    "code": "FORBIDDEN",
    "message": "operation not permitted"
  }
}
```

---

# 9. 统一响应结构

成功：

```json
{
  "success": true,
  "data": {}
}
```

失败：

```json
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "human readable message"
  }
}
```

---

# 10. 错误响应规范

HTTP 状态码负责表达 HTTP 层面的结果。

业务错误通过：

```text
error.code
```

表达。

例如：

```json
{
  "success": false,
  "error": {
    "code": "DEVICE_DISABLED",
    "message": "device is disabled"
  }
}
```

客户端不得依赖 `message` 判断业务逻辑。

---

# 11. HTTP 状态码

V1 使用：

| HTTP | 含义           |
| ---: | -------------- |
|  200 | 成功           |
|  201 | 创建成功       |
|  400 | 参数错误       |
|  401 | 未认证         |
|  403 | 无权限         |
|  404 | 资源不存在     |
|  409 | 状态/资源冲突  |
|  413 | 文件过大       |
|  415 | 文件类型不支持 |
|  422 | 参数语义错误   |
|  429 | 请求过于频繁   |
|  500 | 服务内部错误   |
|  503 | 服务暂不可用   |

---

# 12. 设备 API

## 12.1 获取设备列表

```http
GET /api/v1/devices
```

响应：

```json
{
  "success": true,
  "data": {
    "devices": [
      {
        "id": "xiaomi13ultra",
        "name": "小米 13 Ultra",
        "type": "android",
        "enabled": true
      },
      {
        "id": "xiaomi14",
        "name": "小米 14",
        "type": "android",
        "enabled": true
      }
    ]
  }
}
```

---

# 13. 设备 ID

上传时必须使用：

```text
devices.id
```

不允许客户端自由创建设备 ID。

例如：

```text
xiaomi14
```

而不是：

```text
xiaomi14-abc123
```

---

# 14. 创建上传 Session

```http
POST /api/v1/upload-sessions
```

请求：

```json
{
  "source_device": "xiaomi14"
}
```

服务器验证：

1. 设备存在
2. 设备启用
3. Token 有效

成功：

```http
201 Created
```

响应：

```json
{
  "success": true,
  "data": {
    "session": {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "source_device": "xiaomi14",
      "status": "CREATED",
      "created_at": "2026-09-02T00:30:00Z"
    }
  }
}
```

---

# 15. Session 创建失败

设备不存在：

```text
DEVICE_NOT_FOUND
```

设备禁用：

```text
DEVICE_DISABLED
```

例如：

```json
{
  "success": false,
  "error": {
    "code": "DEVICE_DISABLED",
    "message": "device is disabled"
  }
}
```

---

# 16. 获取 Session

```http
GET /api/v1/upload-sessions/{session_id}
```

例如：

```http
GET /api/v1/upload-sessions/550e8400-e29b-41d4-a716-446655440000
```

响应：

```json
{
  "success": true,
  "data": {
    "session": {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "source_device": "xiaomi14",
      "status": "PROCESSING",
      "created_at": "2026-09-02T00:30:00Z",
      "started_at": "2026-09-02T00:31:00Z",
      "completed_at": null,
      "total_files": 1000,
      "uploaded_files": 850,
      "skipped_files": 120,
      "duplicate_files": 20,
      "failed_files": 10
    }
  }
}
```

---

# 17. Session 进度

Session API 必须提供足够的信息让 WebUI 展示：

```text
总文件数
已上传
已跳过
重复
失败
处理状态
```

例如：

```text
1000 / 1000
```

表示客户端上传阶段已经完成。

但：

```text
PROCESSING
```

说明 Worker 仍可能正在处理。

---

# 18. Session 完成判断

只有：

```text
status = COMPLETED
```

才能认为该 Session 的所有文件已经最终处理完成。

不能仅通过：

```text
uploaded_files == total_files
```

判断。

---

# 19. 创建 Upload Item

客户端获取 Session 后，对每个待上传文件先调用：

```http
POST /api/v1/upload-sessions/{session_id}/items
```

请求：

```json
{
  "filename": "IMG_001.jpg",
  "file_size": 12345678
}
```

服务器执行 Precheck。

---

# 20. Precheck 请求

Precheck 只需要：

```text
source_device
filename
file_size
```

不上传文件内容。

这样可以避免已经成功上传过的照片再次传输到 R5S。

---

# 21. Precheck：无匹配

响应：

```json
{
  "success": true,
  "data": {
    "item": {
      "id": 1001,
      "filename": "IMG_001.jpg",
      "file_size": 12345678,
      "precheck_status": "NO_MATCH",
      "status": "PRECHECK",
      "upload_required": true
    }
  }
}
```

客户端随后上传文件。

---

# 22. Precheck：疑似重复

如果数据库存在：

```text
source_device
+
original_filename
+
file_size
```

相同的历史成功记录：

```json
{
  "success": true,
  "data": {
    "item": {
      "id": 1002,
      "filename": "IMG_002.jpg",
      "file_size": 23456789,
      "precheck_status": "POSSIBLE_DUPLICATE",
      "status": "PRECHECK",
      "upload_required": false
    }
  }
}
```

此时：

> 文件不会进入 R5S。

---

# 23. Precheck 的设计目的

假设用户一次上传：

```text
1000 张
```

其中：

```text
900 张以前已经上传
100 张新照片
```

系统应该：

```text
900 张
    ↓
POSSIBLE_DUPLICATE
    ↓
不传输文件

100 张
    ↓
NO_MATCH
    ↓
正常上传
```

避免重复传输大量照片。

---

# 24. Precheck 不等于最终重复

`POSSIBLE_DUPLICATE` 只是：

> 用户确认之前的快速判断。

用户可以选择：

```text
重新上传
```

然后文件才真正上传。

最终仍然必须计算：

```text
SHA-256
```

---

# 25. 获取疑似重复文件

```http
GET /api/v1/upload-sessions/{session_id}/duplicates
```

响应：

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": 1002,
        "filename": "IMG_002.jpg",
        "file_size": 23456789,
        "precheck_status": "POSSIBLE_DUPLICATE",
        "reupload_confirmed": false
      }
    ],
    "total": 900
  }
}
```

---

# 26. 获取单个 Upload Item

```http
GET /api/v1/upload-items/{item_id}
```

响应：

```json
{
  "success": true,
  "data": {
    "item": {
      "id": 1002,
      "session_id": "550e8400-e29b-41d4-a716-446655440000",
      "source_device": "xiaomi14",
      "original_filename": "IMG_002.jpg",
      "file_size": 23456789,
      "status": "PRECHECK",
      "precheck_status": "POSSIBLE_DUPLICATE",
      "reupload_confirmed": false,
      "asset_id": null,
      "error_code": null,
      "error_message": null
    }
  }
}
```

---

# 27. 确认重新上传

```http
POST /api/v1/upload-items/{item_id}/reupload
```

请求：

```json
{
  "confirmed": true
}
```

服务器：

```text
reupload_confirmed = 1
```

并允许客户端上传该文件。

响应：

```json
{
  "success": true,
  "data": {
    "item_id": 1002,
    "reupload_confirmed": true,
    "upload_required": true
  }
}
```

---

# 28. 取消重新上传

```http
POST /api/v1/upload-items/{item_id}/reupload
```

请求：

```json
{
  "confirmed": false
}
```

保持：

```text
reupload_confirmed = 0
```

该文件不会上传。

---

# 29. 文件上传

```http
POST /api/v1/upload-items/{item_id}/file
```

使用：

```http
Content-Type: multipart/form-data
```

字段：

```text
file
```

例如：

```text
file = IMG_001.jpg
```

---

# 30. 文件上传前置条件

以下情况允许上传：

```text
precheck_status = NO_MATCH
```

或者：

```text
reupload_confirmed = true
```

以下情况拒绝：

```text
POSSIBLE_DUPLICATE
+
reupload_confirmed = false
```

错误：

```text
REUPLOAD_CONFIRMATION_REQUIRED
```

---

# 31. `.tmp` 上传

客户端上传过程中：

```text
incoming/IMG_001.jpg.tmp
```

上传完成后：

```text
incoming/IMG_001.jpg
```

数据库状态：

```text
UPLOADING
        ↓
UPLOADED
```

V1 不实现 HTTP Range 断点续传。

---

# 32. 手机锁屏/断网

假设：

```text
IMG_001.jpg
```

上传过程中手机锁屏，网络断开。

服务器可能留下：

```text
IMG_001.jpg.tmp
```

数据库：

```text
status = UPLOADING
```

该文件不会进入 Worker。

客户端恢复网络后：

> 重新上传该文件。

如果重新创建 Session：

> 重新执行 Precheck。

如果原 Session 仍然存在：

> 可以继续使用原 Session 上传失败项目。

---

# 33. `.tmp` 不支持断点续传

V1 明确：

```text
HTTP Range Resume = 不实现
```

因此：

```text
IMG_001.jpg.tmp
```

不是断点续传文件。

而是：

> 未完成上传的临时文件。

---

# 34. Session 中断后的处理

例如：

```text
1000 张
```

手机只上传成功：

```text
700 张
```

然后断网。

Session 保持：

```text
UPLOADING
```

客户端恢复后查询：

```http
GET /api/v1/upload-sessions/{session_id}/items
```

获得每个文件状态。

例如：

```text
700 COMPLETED/PROCESSING
200 PRECHECK
100 FAILED
```

客户端只需要重新处理：

```text
PRECHECK
FAILED
```

文件。

---

# 35. 获取 Session 文件列表

```http
GET /api/v1/upload-sessions/{session_id}/items
```

支持分页：

```text
?page=1&page_size=100
```

响应：

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": 1001,
        "filename": "IMG_001.jpg",
        "file_size": 12345678,
        "status": "COMPLETED",
        "precheck_status": "NO_MATCH"
      }
    ],
    "pagination": {
      "page": 1,
      "page_size": 100,
      "total": 1000
    }
  }
}
```

---

# 36. 获取失败文件

```http
GET /api/v1/upload-sessions/{session_id}/failed
```

响应：

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": 1101,
        "filename": "IMG_101.jpg",
        "file_size": 12000000,
        "status": "FAILED",
        "error_code": "EXIF_FAILED",
        "error_message": "failed to parse EXIF"
      }
    ],
    "total": 10
  }
}
```

---

# 37. 重新上传失败文件

失败文件重新上传：

```http
POST /api/v1/upload-items/{item_id}/retry
```

响应：

```json
{
  "success": true,
  "data": {
    "item_id": 1101,
    "status": "PRECHECK",
    "upload_required": true
  }
}
```

然后客户端重新上传文件。

---

# 38. Retry 的行为

Retry 不创建新的 `upload_item`。

仍然使用：

```text
原 upload_item
```

原因：

> 这是同一次上传任务中的失败重试。

如果用户以后重新建立新的 Session：

> 会创建新的 `upload_item`。

---

# 39. 上传取消

Session：

```http
POST /api/v1/upload-sessions/{session_id}/cancel
```

响应：

```json
{
  "success": true,
  "data": {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "CANCELLED"
  }
}
```

---

# 40. Cancel 行为

取消 Session：

- 不删除已经成功归档的照片
- 不删除历史 `upload_items`
- 不删除 `photo_assets`
- 不影响其他 Session

尚未处理的：

```text
PRECHECK
UPLOADING
UPLOADED
```

文件根据 Worker 安全策略进行清理/恢复。

---

# 41. Session 完成

客户端上传完所有应该上传的文件后：

```http
POST /api/v1/upload-sessions/{session_id}/complete
```

请求：

```json
{}
```

服务器检查：

```text
所有需要上传的文件
```

是否已经：

```text
UPLOADED
```

或者：

```text
POSSIBLE_DUPLICATE
```

且未确认重新上传。

---

# 42. Complete 后状态

如果所有文件都可以继续处理：

```text
UPLOADED
```

Session：

```text
PROCESSING
```

Worker 开始处理。

---

# 43. Session Partial

例如：

```text
1000 文件
```

最终：

```text
950 COMPLETED
30 DUPLICATE
20 FAILED
```

Session：

```text
PARTIAL
```

WebUI 必须允许查看：

```text
20 FAILED
```

并提供：

```text
Retry
```

---

# 44. Session 完成后的重复提示

如果：

```text
POSSIBLE_DUPLICATE = 900
```

WebUI 应显示：

```text
本次上传发现 900 个疑似已经上传过的文件。
```

用户可以：

```text
查看
```

并选择：

```text
全部重新上传
```

或：

```text
选择部分重新上传
```

---

# 45. 获取照片资产列表

```http
GET /api/v1/photos
```

支持分页：

```text
?page=1
&page_size=100
```

支持排序：

```text
?sort=date_taken
&order=desc
```

---

# 46. 照片查询参数

V1 支持：

```text
date_from
date_to
filename
source_device
mime_type
```

例如：

```http
GET /api/v1/photos?date_from=2026-01-01&date_to=2026-08-31
```

---

# 47. 照片列表响应

```json
{
  "success": true,
  "data": {
    "photos": [
      {
        "id": 501,
        "original_filename": "IMG_001.jpg",
        "current_filename": "IMG_001.jpg",
        "file_size": 12345678,
        "mime_type": "image/jpeg",
        "date_taken": "2026-08-31T19:32:15",
        "date_source": "DateTimeOriginal",
        "archive_path": "Photos/2026/202608/IMG_001.jpg"
      }
    ],
    "pagination": {
      "page": 1,
      "page_size": 100,
      "total": 1
    }
  }
}
```

---

# 48. 获取照片详情

```http
GET /api/v1/photos/{photo_id}
```

响应：

```json
{
  "success": true,
  "data": {
    "photo": {
      "id": 501,
      "sha256": "abcdef...",
      "original_filename": "IMG_001.jpg",
      "current_filename": "IMG_001.jpg",
      "file_size": 12345678,
      "mime_type": "image/jpeg",
      "date_taken": "2026-08-31T19:32:15",
      "date_source": "DateTimeOriginal",
      "archive_path": "Photos/2026/202608/IMG_001.jpg",
      "exif": {
        "Make": "Xiaomi",
        "Model": "2312DRA50C",
        "ImageWidth": 4080,
        "ImageHeight": 3072
      },
      "created_at": "2026-09-02T00:35:00Z",
      "updated_at": "2026-09-02T00:35:00Z"
    }
  }
}
```

---

# 49. 获取照片文件

```http
GET /api/v1/photos/{photo_id}/file
```

服务器返回：

```http
Content-Type: image/jpeg
```

直接输出原始照片文件。

不得重新编码。

不得修改 EXIF。

---

# 50. 获取缩略图

V1 可以提供：

```http
GET /api/v1/photos/{photo_id}/thumbnail
```

但缩略图属于：

> WebUI 性能优化。

原始照片必须保持不变。

如果 V1 第一阶段暂不实现缩略图，可以由 WebUI 后续实现。

---

# 51. 获取照片事件

```http
GET /api/v1/photos/{photo_id}/events
```

服务器通过：

```text
photo_assets
    ↓
upload_items
    ↓
photo_events
```

查询历史。

例如：

```json
{
  "success": true,
  "data": {
    "events": [
      {
        "event_type": "ARCHIVED",
        "created_at": "2026-09-02T00:36:00Z"
      }
    ]
  }
}
```

---

# 52. 获取系统状态

```http
GET /api/v1/system/status
```

响应：

```json
{
  "success": true,
  "data": {
    "status": "running",
    "worker": {
      "status": "idle",
      "current_item_id": null
    },
    "storage": {
      "incoming_available_bytes": 123456789,
      "incoming_used_bytes": 4567890
    },
    "database": {
      "status": "ok"
    }
  }
}
```

---

# 53. Worker 状态

V1：

```text
idle
processing
error
```

由于 Worker 数量固定为：

```text
1
```

WebUI 可以直接展示当前正在处理：

```text
IMG_001.jpg
```

---

# 54. 当前处理文件

```http
GET /api/v1/system/worker
```

响应：

```json
{
  "success": true,
  "data": {
    "worker": {
      "status": "processing",
      "item_id": 1001,
      "filename": "IMG_001.jpg",
      "started_at": "2026-09-02T00:40:00Z"
    }
  }
}
```

---

# 55. 上传进度设计

不要求客户端持续上传进度到服务器。

例如一个 50 MB 文件：

```text
0%
10%
20%
...
```

这些进度属于：

> HTTP 上传连接状态。

不写数据库。

Session 层只关心：

```text
文件是否已经上传完成
```

---

# 56. 1000 文件批量上传流程

完整流程：

```text
手机
 │
 │ 创建 Session
 ▼
Photo Gateway
 │
 ├── IMG_001.jpg
 │     ↓
 │   Precheck
 │     ↓
 │   NO_MATCH
 │     ↓
 │   上传
 │
 ├── IMG_002.jpg
 │     ↓
 │   POSSIBLE_DUPLICATE
 │     ↓
 │   暂不上传
 │
 ├── IMG_003.jpg
 │     ↓
 │   NO_MATCH
 │     ↓
 │   上传
 │
 └── ...
```

1000 个文件完成 Precheck 后：

```text
900 POSSIBLE_DUPLICATE
100 NO_MATCH
```

100 个进入实际上传。

---

# 57. 上传顺序

V1 客户端推荐：

```text
一个文件一个文件上传
```

服务器 Worker：

```text
并发数 = 1
```

两者不是同一个概念。

即：

```text
客户端上传
        ↓
incoming
        ↓
Worker 单线程处理
```

客户端可以在网络允许的情况下继续上传下一个文件。

Worker 按队列顺序处理。

---

# 58. 推荐客户端行为

客户端：

```text
for file in selected_files:

    precheck(file)

    if possible_duplicate:
        record_for_review()
        continue

    upload(file)
```

上传完成：

```text
显示：

成功上传 X
疑似重复 Y
失败 Z
```

然后允许用户处理：

```text
疑似重复
```

---

# 59. 重新上传疑似重复文件

用户确认：

```text
IMG_002.jpg
```

重新上传。

服务器：

```text
reupload_confirmed = 1
```

客户端：

```text
POST /upload-items/1002/file
```

文件进入：

```text
incoming/IMG_002.jpg
```

Worker：

```text
SHA256
```

如果：

```text
SHA256 == 已有 SHA256
```

结果：

```text
DUPLICATE
```

否则：

```text
NEW PHOTO
```

---

# 60. 文件名冲突

如果：

```text
IMG_002.jpg
```

已经存在，但 SHA-256 不同：

```text
IMG_002_1.jpg
```

数据库：

```text
original_filename = IMG_002.jpg
current_filename = IMG_002_1.jpg
```

---

# 61. 文件上传幂等性

同一个：

```text
upload_item_id
```

不允许同时存在两个上传请求。

如果重复请求：

```text
409 Conflict
```

错误：

```text
UPLOAD_ALREADY_IN_PROGRESS
```

如果已经：

```text
COMPLETED
DUPLICATE
```

再次上传：

```text
409 Conflict
```

除非通过：

```text
retry
```

或创建新的 Session。

---

# 62. Session API 幂等性

客户端网络异常时，可能重复调用：

```http
POST /upload-sessions
```

V1 不要求 Session 创建接口实现复杂幂等 Key。

客户端应：

> 保存已经创建的 Session ID。

恢复上传时优先查询原 Session。

---

# 63. API 分页

列表 API 默认：

```text
page_size = 100
```

最大：

```text
page_size = 500
```

超过最大值：

```text
400 Bad Request
```

---

# 64. 文件名参数

URL 中涉及文件名时：

> 必须使用 URL 编码。

API JSON 中：

> 使用 UTF-8 原始字符串。

服务器必须正确处理：

```text
中文
空格
括号
特殊 Unicode 字符
```

---

# 65. API 不允许直接修改照片

V1 不提供：

```http
PUT /photos/{id}
PATCH /photos/{id}
DELETE /photos/{id}
```

原因：

照片资产属于：

> 原始长期数据。

Photo Gateway V1 只负责导入。

---

# 66. API 不允许修改 EXIF

不提供：

```http
PATCH /photos/{id}/exif
```

数据库中的：

```text
exif_json
```

只能由 Worker 根据原照片解析生成。

---

# 67. API 不负责 NAS

不提供：

```text
/nas/*
```

API。

NAS 同步属于后续阶段。

---

# 68. API 不负责 Immich

不提供：

```text
/immich/*
```

API。

Immich 继续通过 External Library 使用照片目录。

---

# 69. API 错误码

V1 标准错误码：

```text
AUTH_REQUIRED
AUTH_INVALID
FORBIDDEN

DEVICE_NOT_FOUND
DEVICE_DISABLED

SESSION_NOT_FOUND
SESSION_INVALID_STATE
SESSION_CANCELLED

UPLOAD_ITEM_NOT_FOUND
UPLOAD_ALREADY_IN_PROGRESS
UPLOAD_ALREADY_COMPLETED
UPLOAD_NOT_ALLOWED

REUPLOAD_CONFIRMATION_REQUIRED

FILE_NOT_FOUND
FILE_TOO_LARGE
FILE_TYPE_NOT_ALLOWED
INVALID_FILENAME
INVALID_FILE_SIZE

SHA256_FAILED
EXIF_FAILED
ARCHIVE_FAILED
FILE_CONFLICT

DUPLICATE_FILE

DATABASE_ERROR
STORAGE_ERROR
WORKER_ERROR

RATE_LIMITED
INTERNAL_ERROR
SERVICE_UNAVAILABLE
```

---

# 70. API 错误示例

文件类型不允许：

```json
{
  "success": false,
  "error": {
    "code": "FILE_TYPE_NOT_ALLOWED",
    "message": "file type is not allowed"
  }
}
```

---

# 71. API 安全限制

V1 虽然只用于家庭 LAN，但仍然必须：

- 所有写操作需要认证
- 上传接口需要认证
- 文件路径不得由客户端直接指定
- 禁止客户端提交绝对路径
- 禁止 `../`
- 禁止通过文件名进行路径穿越
- 文件名必须经过安全校验

客户端只能提供：

```text
filename
file_size
file
```

正式：

```text
archive_path
```

由服务器生成。

---

# 72. 正式路径生成

客户端：

```text
IMG_001.jpg
```

服务器根据：

```text
date_taken
```

生成：

```text
Photos/YYYY/YYYYMM/IMG_001.jpg
```

例如：

```text
Photos/2026/202608/IMG_001.jpg
```

客户端不得指定：

```text
Photos/2026/08/abc/
```

---

# 73. API 与数据库职责

```text
API
 │
 ├── devices
 │
 ├── upload_sessions
 │
 ├── upload_items
 │
 └── photo_assets 查询
 │
 ▼
SQLite
```

API 不直接操作：

```text
photo_events
```

而是由业务层在状态变化时自动产生。

---

# 74. API 与 Worker

```text
Client
   │
   ▼
API
   │
   ▼
incoming/
   │
   ▼
Worker
   │
   ├── SHA256
   ├── EXIF
   ├── duplicate check
   └── archive
          │
          ▼
   photo_assets
```

API 不执行耗时的：

```text
SHA-256
EXIF parsing
文件归档
```

这些任务由 Worker 完成。

---

# 75. API 非阻塞原则

上传完成后：

```text
POST /upload-items/{id}/file
```

只需要确认：

> 文件已经安全写入 incoming。

不等待：

```text
SHA256
EXIF
archive
```

完成。

因此响应可以是：

```json
{
  "success": true,
  "data": {
    "item_id": 1001,
    "status": "UPLOADED"
  }
}
```

Worker 随后异步处理。

---

# 76. WebUI 推荐首页

V1 WebUI 首页建议显示：

```text
Photo Gateway

系统状态：正常

Worker：
空闲

今日上传：
123 张

待处理：
15

失败：
2

疑似重复：
38

存储：
1.2 TB / 1.8 TB
```

---

# 77. WebUI 上传任务页

显示：

```text
小米 14
Session: 2026-09-02 08:30

总计：1000

已上传：850
处理中：120
完成：700
重复：30
失败：10
疑似重复：150
```

---

# 78. WebUI 疑似重复页

显示：

| 文件名      |  大小 | 设备   | 操作     |
| ----------- | ----: | ------ | -------- |
| IMG_001.jpg | 12 MB | 小米14 | 重新上传 |
| IMG_002.jpg | 15 MB | 小米14 | 重新上传 |

支持：

```text
全部重新上传
```

以及：

```text
选择部分重新上传
```

---

# 79. WebUI 失败页

显示：

| 文件名      | 状态   | 错误           | 操作 |
| ----------- | ------ | -------------- | ---- |
| IMG_100.jpg | FAILED | EXIF_FAILED    | 重试 |
| IMG_101.jpg | FAILED | ARCHIVE_FAILED | 重试 |

支持：

```text
全部重试
```

---

# 80. WebUI 照片信息页

显示：

```text
照片：

IMG_001.jpg

拍摄时间：
2026-08-31 19:32:15

设备：
Xiaomi 13 Ultra

尺寸：
4080 × 3072

大小：
12.3 MB

MIME：
image/jpeg

SHA-256：
abcdef...

路径：
Photos/2026/202608/IMG_001.jpg
```

EXIF 可以展开显示。

---

# 81. API 版本控制

所有 V1 API：

```text
/api/v1/
```

未来 V2：

```text
/api/v2/
```

V2 不得直接改变 V1 API 的语义。

---

# 82. API 兼容原则

V1 冻结后：

不得修改：

```text
URL
HTTP Method
字段含义
状态值
错误码含义
```

如果必须改变：

> 增加 V2。

允许 V1 增加非破坏性的响应字段，但客户端必须忽略未知字段。

---

# 83. V1 API 总览

```text
GET    /api/v1/devices

POST   /api/v1/upload-sessions
GET    /api/v1/upload-sessions/{id}
GET    /api/v1/upload-sessions/{id}/items
GET    /api/v1/upload-sessions/{id}/duplicates
GET    /api/v1/upload-sessions/{id}/failed
POST   /api/v1/upload-sessions/{id}/complete
POST   /api/v1/upload-sessions/{id}/cancel

POST   /api/v1/upload-sessions/{id}/items

GET    /api/v1/upload-items/{id}
POST   /api/v1/upload-items/{id}/file
POST   /api/v1/upload-items/{id}/reupload
POST   /api/v1/upload-items/{id}/retry

GET    /api/v1/photos
GET    /api/v1/photos/{id}
GET    /api/v1/photos/{id}/file
GET    /api/v1/photos/{id}/thumbnail
GET    /api/v1/photos/{id}/events

GET    /api/v1/system/status
GET    /api/v1/system/worker
```

---

# 84. V1 核心上传流程

最终确定为：

```text
                ┌──────────────┐
                │    Client    │
                └──────┬───────┘
                       │
                       │ 1. Create Session
                       ▼
                ┌──────────────┐
                │    Session   │
                └──────┬───────┘
                       │
                       │ 2. Precheck
                       ▼
              ┌───────────────────┐
              │ source_device +   │
              │ filename + size   │
              └─────────┬─────────┘
                        │
              ┌─────────┴─────────┐
              │                   │
          NO_MATCH         POSSIBLE_DUPLICATE
              │                   │
              ▼                   ▼
          Upload             等待确认
              │                   │
              │              用户确认
              │                   │
              │                   ▼
              │                Upload
              │                   │
              └─────────┬─────────┘
                        ▼
                 incoming/*.tmp
                        │
                        ▼
                 upload complete
                        │
                        ▼
                 incoming/*.jpg
                        │
                        ▼
                    Worker
                        │
                        ▼
                     SHA256
                        │
              ┌─────────┴─────────┐
              │                   │
          已存在                 不存在
              │                   │
              ▼                   ▼
         DUPLICATE             EXIF
                                  │
                                  ▼
                             date_taken
                                  │
                                  ▼
                             archive_path
                                  │
                                  ▼
                              archive
                                  │
                                  ▼
                           photo_assets
                                  │
                                  ▼
                             COMPLETED
```

---

# 85. V1 冻结原则

本 API Specification 与：

```text
photo-gateway-v1-spec.md
photo-gateway-v1-config-spec.md
photo-gateway-v1-db-spec.md
```

共同构成 Photo Gateway V1 Frozen Baseline。

后续代码实现必须遵循：

```text
Config
   ↓
Database
   ↓
API
   ↓
Worker
   ↓
Filesystem
```

的职责边界。

V1 不因为具体客户端的实现方便而改变数据库模型或正式照片目录结构。

---

# 86. V1 明确不实现

以下功能全部留待后续版本：

```text
HTTP Range 断点续传
专用 Android App
专用 iOS App
公网访问
HTTPS 自动证书
多用户权限体系
NAS 自动同步
NAS 双向同步
照片删除
照片编辑
视频管理
人脸识别
AI 分类
相册
标签
地图
OCR
对象识别
Immich API 集成
```

---

# 87. V1 最终目标

V1 的目标不是实现一个完整的照片管理软件。

而是建立一个稳定的：

> **家庭照片导入网关。**

其核心职责只有：

```text
设备
  ↓
Precheck
  ↓
照片接收
  ↓
SHA-256 去重
  ↓
EXIF 解析
  ↓
按 YYYY/YYYYMM 归档
  ↓
形成稳定的原始照片库
```

照片库本身保持：

```text
Photos/
├── 2004/
│   ├── 200401/
│   ├── 200402/
│   └── ...
├── 2025/
│   └── 202512/
└── 2026/
    └── 202609/
```

不依赖 Immich，也不依赖 Photo Gateway 数据库才能读取。

因此即使未来：

```text
Photo Gateway
Immich
其他照片管理软件
WebUI
```

全部更换，原始照片仍然可以直接访问。