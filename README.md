# 家庭照片管理系统 V1 技术规格书

**版本：V1.0**  
**状态：基线设计 / 待实施**  
**日期：2026-09-01**

---

## 1. 项目目标

建立一套面向家庭场景的私有化照片管理体系，实现：

1. 从小米手机、Mac、小米笔记本以及微信、QQ、网盘、网页下载等来源，手工选择照片上传。
2. 使用长期在线的 NanoPi R5S 作为照片接收与临时缓存节点。
3. NAS 不需要长期运行；NAS 开机后自动发现并同步 R5S 上待归档照片。
4. 按照片实际拍摄时间自动归档。
5. 保持现有照片目录结构：

```text
Photos/
├── 2004/
│   ├── 200401/
│   ├── 200402/
│   └── ...
├── 2005/
│   └── ...
└── 2026/
    ├── 202601/
    ├── 202602/
    └── ...
```

6. 不修改原始照片文件内容及原始 EXIF 信息。
7. 使用 SHA-256 对照片进行内容级精确去重。
8. NAS 照片主盘与备份盘保持独立文件系统，通过 rsync 实现主备。
9. Immich 仅作为照片管理、搜索、人脸识别、地图、时间线等索引和展示系统。
10. Immich 使用 External Library，不接管原始照片存储。
11. 更换 Immich 或其他照片管理软件时，不需要迁移或重新组织原始照片。
12. 整套系统仅在家庭局域网内运行，V1 不提供公网访问。
13. 系统必须能够处理中断、重复上传、NAS 离线、同步失败等情况。

---

# 2. 核心设计原则

## 2.1 原始照片优先

原始照片是系统中最高优先级的数据资产。

```text
原始照片 > 照片目录结构 > 照片元数据 > 照片管理软件数据库
```

任何管理软件都不能成为照片唯一存储位置。

---

## 2.2 存储与管理分离

照片文件：

```text
NAS/Photos/
```

管理软件：

```text
Immich
```

两者完全解耦。

Immich 不负责决定原始照片应该放在哪里。

---

## 2.3 Immich 不拥有原始照片

Immich 使用 External Library：

```text
NAS
└── Photos/
        ↓
Immich External Library
```

原则上使用只读挂载：

```text
Photos:/photos:ro
```

Immich 可以：

- 扫描；
- 建立索引；
- 生成人脸识别结果；
- 生成缩略图；
- 建立时间线；
- 建立地图；
- 提供搜索；
- 提供相册视图。

Immich 不允许：

- 删除原图；
- 移动原图；
- 重命名原图；
- 修改原图。

---

# 3. 总体架构

```text
┌─────────────────────────────────────────────┐
│                  家庭局域网                  │
│                                             │
│  小米13 Ultra   小米14   Mac   小米笔记本    │
│       │           │       │        │        │
│       └───────────┴───────┴────────┘        │
│                       │                     │
│                  手工选择照片               │
│                       │                     │
│                       ▼                     │
│              ┌─────────────────┐            │
│              │      R5S        │            │
│              │  ImmortalWrt    │            │
│              │                 │            │
│              │ Photo Gateway   │            │
│              │                 │            │
│              │ Upload          │            │
│              │ SHA256          │            │
│              │ EXIF            │            │
│              │ Metadata        │            │
│              │ Queue           │            │
│              └────────┬────────┘            │
│                       │                     │
│                  NAS 开机后                  │
│                  自动同步                    │
│                       │                     │
│                       ▼                     │
│              ┌─────────────────┐            │
│              │       NAS       │            │
│              │                 │            │
│              │ Photos 主盘     │            │
│              │       │         │            │
│              │       │ rsync   │            │
│              │       ▼         │            │
│              │ Photos 备份盘   │            │
│              └────────┬────────┘            │
│                       │                     │
│                       ▼                     │
│              ┌─────────────────┐            │
│              │     Immich      │            │
│              │ External Lib    │            │
│              └─────────────────┘            │
└─────────────────────────────────────────────┘
```

---

# 4. 设备角色

## 4.1 NanoPi R5S

硬件：

```text
CPU：RK3568B2 / 4× Cortex-A55
RAM：4GB
系统：ImmortalWrt 24.10.5
存储：USB 3.0 2TB HDD
```

角色：

> 照片接收节点 + 临时缓存节点 + 照片处理节点 + 同步队列节点。

不承担：

- Immich；
- 人脸识别；
- AI 图片分析；
- 长期照片存储。

---

## 4.2 R5S USB HDD

当前：

```text
容量：2TB
已使用：约 1.3TB
```

定位：

> 临时缓存。

不是正式照片资产存储。

照片成功归档到 NAS 并完成校验后，可以删除 R5S 上对应的临时文件。

---

## 4.3 NAS

硬件：

```text
CPU：Intel Core i5-8500
内存：32GB
SSD：1TB NVMe
HDD：4TB × 4
系统：PVE + Linux VM
```

磁盘结构：

```text
照片：
4TB 主盘
   ↓ rsync
4TB 备份盘

电影：
4TB 主盘
   ↓ rsync
4TB 备份盘
```

照片系统只使用照片对应的主盘和备份盘。

---

# 5. 原始照片目录规范

现有目录结构必须保持不变：

```text
Photos/
├── 2004/
│   ├── 200401/
│   ├── 200402/
│   └── ...
├── 2005/
│   ├── 200501/
│   └── ...
...
└── 2026/
    ├── 202601/
    ├── 202602/
    └── 202609/
```

目录规则：

```text
Photos/YYYY/YYYYMM/
```

禁止：

```text
Photos/YYYY/MM/DD/
```

禁止因为新系统上线而迁移现有照片。

---

# 6. 照片来源

V1 支持以下来源：

```text
小米13 Ultra
小米14
Mac
小米笔记本
微信
QQ
网页下载
网盘
其他能够访问上传接口的设备
```

原则：

> 用户主动选择照片后上传。

V1 不执行手机 Camera 相册的自动全量同步。

---

# 7. R5S 接收区目录

建议：

```text
/mnt/sda1/photo-system/
├── incoming/
├── processing/
├── pending/
├── quarantine/
├── archive/
├── state/
└── logs/
```

说明：

### incoming

刚上传的照片。

### processing

正在处理的照片。

### pending

已经完成检查，等待 NAS 归档。

### quarantine

无法自动处理的照片。

例如：

- EXIF 损坏；
- 文件损坏；
- 日期无法判断；
- 文件类型异常；
- 元数据解析异常。

### archive

用于保存必要的归档状态信息，不作为永久照片库。

### state

SQLite 数据库等状态数据。

### logs

系统日志。

---

# 8. 照片生命周期

照片状态定义：

```text
NEW
  ↓
CHECKING
  ↓
CHECKED
  ↓
PENDING
  ↓
SYNCING
  ↓
VERIFIED
  ↓
ARCHIVED
```

异常状态：

```text
DUPLICATE
QUARANTINE
FAILED
```

完整状态机：

```text
                 ┌────────────┐
                 │    NEW     │
                 └─────┬──────┘
                       ↓
                 ┌────────────┐
                 │  CHECKING  │
                 └─────┬──────┘
                       ↓
                 ┌────────────┐
                 │  CHECKED   │
                 └─────┬──────┘
                       ↓
                 ┌────────────┐
                 │   PENDING  │
                 └─────┬──────┘
                       │
                   NAS 在线
                       │
                       ↓
                 ┌────────────┐
                 │  SYNCING   │
                 └─────┬──────┘
                       ↓
                 ┌────────────┐
                 │  VERIFIED  │
                 └─────┬──────┘
                       ↓
                 ┌────────────┐
                 │  ARCHIVED  │
                 └────────────┘


CHECKING ───────→ QUARANTINE
CHECKED ────────→ DUPLICATE
SYNCING ────────→ FAILED
```

---

# 9. 文件上传要求

上传必须保证：

> 文件完全上传成功以后，才能进入处理阶段。

不能出现：

```text
上传到 50%
↓
同步程序误认为上传完成
```

因此推荐使用：

```text
临时文件
    ↓
上传完成
    ↓
原子 rename
    ↓
进入 incoming
```

例如：

```text
incoming/
IMG001.jpg.uploading
```

上传完成：

```text
IMG001.jpg.uploading
        ↓
IMG001.jpg
```

只有 `.uploading` 消失以后，处理程序才允许读取。

---

# 10. SHA-256 去重

每张照片进入系统后计算：

```text
SHA-256(file)
```

SHA-256 作为照片内容唯一指纹。

数据库至少保存：

```text
sha256
file_size
original_filename
```

如果 SHA-256 已经存在：

```text
新文件
 ↓
SHA256
 ↓
数据库已有
 ↓
DUPLICATE
```

不再次写入 NAS。

---

# 11. 文件名处理规则

V1 原则：

> 不主动修改原始文件名。

例如：

```text
IMG_1234.jpg
DSC00001.JPG
photo.jpg
```

均保持原名称。

如果目标目录中已经存在同名文件：

### 情况 A

SHA-256 相同：

```text
视为重复
```

### 情况 B

SHA-256 不同：

```text
发生文件名冲突
```

禁止覆盖原文件。

自动生成不破坏原始名称的冲突名称：

```text
IMG_1234.jpg
IMG_1234_2.jpg
IMG_1234_3.jpg
```

同时记录：

```text
original_filename
stored_filename
```

---

# 12. EXIF 处理原则

系统读取 EXIF，但不修改 EXIF。

重点读取：

```text
DateTimeOriginal
CreateDate
ModifyDate
DateTimeDigitized
GPSLatitude
GPSLongitude
Make
Model
LensModel
Orientation
```

其中最重要的是：

```text
DateTimeOriginal
```

---

# 13. 拍摄时间判定规则

目标：

```text
确定 YYYYMM
```

优先级：

```text
1. EXIF DateTimeOriginal
2. EXIF DateTimeDigitized
3. EXIF CreateDate
4. 其他可靠媒体元数据
5. 文件名中明确可解析的日期
6. 文件系统 mtime
7. 无法判断 → QUARANTINE
```

V1 禁止：

> 使用上传时间作为照片拍摄时间。

例如：

```text
2005 年照片
2026 年上传
```

不能进入：

```text
Photos/2026/202609/
```

---

# 14. 时区处理

照片 EXIF 时间可能没有时区信息。

V1 原则：

> 不主动修改照片中的 EXIF 时间。

归档时直接使用解析后的本地拍摄时间计算：

```text
YYYY
YYYYMM
```

如果后续需要处理跨时区照片，再增加明确的时区策略。

---

# 15. 自动归档规则

例如：

```text
DateTimeOriginal:
2026-08-31 19:32:15
```

目标：

```text
Photos/
└── 2026/
    └── 202608/
        └── IMG001.jpg
```

例如：

```text
DateTimeOriginal:
2004-02-15 10:22:00
```

目标：

```text
Photos/
└── 2004/
    └── 200402/
        └── xxx.jpg
```

---

# 16. 原始文件完整性要求

归档过程中：

```text
R5S
 ↓
SHA256_A
 ↓
复制
 ↓
NAS
 ↓
SHA256_B
```

只有：

```text
SHA256_A == SHA256_B
```

才认为归档成功。

否则：

```text
FAILED
```

不得删除 R5S 临时文件。

---

# 17. NAS 离线处理

NAS 不在线属于正常状态。

例如：

```text
R5S

pending/
├── A.jpg
├── B.jpg
└── C.jpg
```

NAS 关闭：

```text
保持 pending
```

不报错，不删除，不重复处理。

NAS 下一次开机：

```text
检测 NAS
 ↓
发现在线
 ↓
继续处理 pending
```

---

# 18. NAS 自动同步

NAS 启动后执行同步检查。

推荐流程：

```text
NAS 启动
 ↓
检测 R5S
 ↓
连接照片服务
 ↓
获取 pending 列表
 ↓
逐个处理
```

而不是简单执行：

```bash
rsync incoming/ NAS/Photos/
```

因为归档需要：

- EXIF；
- 日期；
- 去重；
- 文件名冲突；
- SHA256；
- 状态管理。

---

# 19. 同步必须支持断点恢复

例如：

```text
100 张照片

已经成功：
87 张

第 88 张：
NAS 断电
```

重启以后：

```text
1-87：
ARCHIVED

88：
继续处理

89-100：
PENDING
```

不能重新复制全部 100 张。

---

# 20. NAS 主备策略

现有设计继续使用：

```text
Photos Primary
       ↓
     rsync
       ↓
Photos Backup
```

两块硬盘保持独立文件系统。

V1 不改变当前磁盘组织方式。

---

# 21. 主备同步安全要求

现有 rsync 机制后续需要避免：

```bash
rsync --delete
```

导致误删除立即传播。

例如：

```text
主盘误删照片
 ↓
rsync
 ↓
备盘立即删除
```

是不安全的。

V1 推荐增加：

```text
备盘删除隔离
```

或者：

```text
备盘历史版本 / 快照 / 回收站
```

具体实现方式在实施阶段根据 NAS 文件系统确定。

---

# 22. Immich 定位

Immich：

> 照片管理和索引系统。

不是：

> 原始照片存储系统。

Immich 不参与：

```text
手机 → R5S
R5S → NAS
NAS → 备盘
```

这些数据链路。

---

# 23. Immich External Library

Immich 配置：

```text
External Library
        ↓
NAS
        ↓
Photos/
```

例如：

```text
/mnt/photos/Photos
```

使用只读方式挂载：

```text
:ro
```

Immich 只负责：

```text
扫描
↓
索引
↓
缩略图
↓
人物识别
↓
地图
↓
时间线
↓
搜索
```

---

# 24. Immich 扫描策略

新照片进入：

```text
Photos/2026/202609/
```

以后由 Immich：

```text
定时扫描
```

发现新文件。

V1 不要求照片同步程序主动调用 Immich API。

这样进一步降低系统耦合。

---

# 25. Immich Mobile App

Immich Mobile App V1 定位：

> 手机端照片查看客户端。

可以连接：

```text
http://NAS-IP:2283
```

但：

> V1 不启用 Mobile Backup。

手机上传统一通过：

```text
手机
 ↓
R5S Photo Gateway
```

而不是：

```text
手机
 ↓
Immich Mobile Backup
```

避免 Immich 自己建立一套照片存储结构。

---

# 26. 数据库设计

R5S 使用 SQLite。

数据库：

```text
photo-system.db
```

核心表：

```text
photos
```

建议字段：

```text
id
sha256
file_size
original_filename
stored_filename
source
source_device
mime_type
width
height
datetime_original
datetime_source
year
year_month
target_path
status
created_at
updated_at
archived_at
error_message
```

---

# 27. SHA-256 索引

建议建立唯一索引：

```text
UNIQUE(sha256)
```

保证：

> 相同文件内容只能存在一个正式记录。

---

# 28. 状态记录

示例：

```text
id: 10001

sha256:
a7e31c...

original_filename:
IMG_1234.jpg

source_device:
xiaomi14

datetime_original:
2026-08-31 19:32:15

year:
2026

year_month:
202608

target_path:
Photos/2026/202608/IMG_1234.jpg

status:
ARCHIVED
```

---

# 29. Quarantine 机制

以下情况不得自动归档：

```text
无法读取照片
EXIF 损坏
无法确定拍摄时间
文件类型未知
文件大小异常
SHA256 计算失败
归档路径异常
```

进入：

```text
quarantine/
```

数据库状态：

```text
QUARANTINE
```

并记录：

```text
error_message
```

---

# 30. 日志

至少分为：

```text
system.log
upload.log
process.log
sync.log
error.log
```

重要事件必须记录：

```text
上传
去重
EXIF 解析
归档
SHA256 校验
同步失败
重试
异常
```

---

# 31. 幂等性

所有操作必须尽可能设计成幂等。

例如：

```text
同一张照片上传 10 次
```

结果应该仍然：

```text
NAS：
只有一份原始文件
```

而不是：

```text
IMG.jpg
IMG_2.jpg
IMG_3.jpg
...
IMG_10.jpg
```

---

# 32. 失败重试

同步失败不能直接丢弃。

例如：

```text
PENDING
 ↓
SYNCING
 ↓
NAS 网络异常
 ↓
FAILED
```

下次同步：

```text
FAILED
 ↓
重新检查
 ↓
继续同步
```

建议采用有限次数重试 + 指数退避。

---

# 33. 数据删除原则

V1 不提供自动删除 NAS 原始照片功能。

照片一旦：

```text
ARCHIVED
```

系统不得因为：

- 重复；
- Immich；
- 同步；
- 清理；
- 缓存回收；

而删除 NAS 正式照片。

R5S 临时文件只有在：

```text
NAS 写入成功
+
SHA256 校验成功
```

以后才能删除。

---

# 34. R5S 缓存清理

成功归档：

```text
ARCHIVED
```

以后：

```text
R5S temporary file
        ↓
删除
```

如果 NAS 没有确认成功：

```text
不得删除
```

---

# 35. R5S 磁盘空间保护

由于当前 USB HDD：

```text
总容量：约 1.8TB
已使用：约 1.3TB
```

因此必须设置：

```text
磁盘空间预警
```

例如：

```text
> 80%
WARNING

> 90%
CRITICAL

> 95%
停止接受新照片
```

具体阈值实施阶段可调整。

---

# 36. 系统安全

V1 只允许家庭局域网访问。

不开放：

```text
公网
WAN
Cloudflare Tunnel
公网端口
```

R5S 上传服务应该限制在 LAN。

NAS 和 R5S 通信使用：

```text
LAN
SSH / HTTPS / API
```

具体协议实施阶段确定。

---

# 37. 原始照片不可变原则

系统中存在两个概念：

```text
原始照片
```

和：

```text
系统生成数据
```

原始照片：

```text
不可修改
```

系统数据：

```text
可以重新生成
```

例如：

```text
Immich 数据库损坏
```

可以：

```text
重新扫描 Photos/
```

重新建立索引。

---

# 38. 可迁移性要求

未来可以：

```text
Immich
 ↓
PhotoPrism
```

或者：

```text
Immich
 ↓
其他软件
```

只需要让新软件读取：

```text
Photos/
```

不需要：

```text
移动照片
重命名照片
重新组织目录
批量修改 EXIF
```

---

# 39. 软件依赖边界

系统分为三个独立层：

```text
Layer 1
原始照片
Photos/

Layer 2
Photo Gateway
R5S

Layer 3
Photo Management
Immich
```

任何一层都不应该成为其他层的强依赖。

---

# 40. V1 不实现的功能

以下功能明确排除在 V1：

```text
自动备份手机全部照片
R5S 人脸识别
R5S AI 分类
视觉相似照片自动删除
公网访问
第三方云同步
自动修改 EXIF
自动重命名照片
自动创建 Immich 相册
自动创建人物
照片编辑
视频智能分析
```

以后根据实际需求再增加。

---

# 41. V1 推荐技术栈

## R5S

优先考虑：

```text
Python 3
SQLite
ExifTool
SHA-256
rsync / SSH
systemd 或 ImmortalWrt 对应服务管理机制
```

其中：

> ExifTool 负责读取照片元数据。

Python 负责：

```text
业务逻辑
状态机
SQLite
目录计算
文件复制
校验
日志
```

---

# 42. 上传接口

V1 不强制绑定某个手机 App。

优先设计为：

```text
HTTP/HTTPS Upload API
```

这样：

```text
Android
iOS
Mac
Windows
Linux
浏览器
```

都可以成为客户端。

上传接口只负责：

```text
接收文件
```

不负责：

```text
决定最终目录
```

---

# 43. 上传接口设计原则

客户端：

```text
POST /upload
```

发送：

```text
file
```

可以附加：

```text
source_device
source_app
original_filename
```

服务器最终仍然以文件本身为准。

---

# 44. 文件类型

V1 优先支持：

```text
JPEG
JPG
PNG
HEIC
HEIF
WEBP
TIFF
RAW
```

视频暂不纳入第一版照片归档流程。

后续可以独立设计：

```text
Videos/
```

避免照片和视频处理逻辑过早耦合。

---

# 45. RAW 照片

RAW 文件：

```text
CR2
CR3
NEF
ARW
DNG
RAF
ORF
RW2
```

原则与 JPEG 相同：

```text
原始文件只复制
不修改
不重新编码
```

EXIF 能否正确解析由 ExifTool 决定。

---

# 46. 视频

V1 不强制实现视频自动归档。

但架构设计需要保证以后能够扩展：

```text
Photos/
Videos/
```

两个资产类别可以共用：

```text
Upload
Hash
Queue
Sync
State
```

但拥有独立的：

```text
Metadata
Archive Rule
```

---

# 47. 备份策略

系统至少存在：

```text
R5S 临时缓存
NAS 照片主盘
NAS 照片备份盘
```

生命周期：

```text
手机
 ↓
R5S
 ↓
NAS 主盘
 ↓
NAS 备份盘
```

R5S 不作为最终备份。

---

# 48. Immich 数据备份

Immich 自身产生：

```text
数据库
配置
缩略图
人脸识别数据
索引
```

这些数据需要独立备份。

即：

```text
Photos/
```

和：

```text
Immich AppData/
```

分别进行备份。

---

# 49. 灾难恢复目标

如果 Immich 完全损坏：

```text
重新部署 Immich
 ↓
重新建立 External Library
 ↓
扫描 Photos/
```

照片仍然完整。

如果 R5S 损坏：

```text
重新部署 Photo Gateway
 ↓
重新连接 NAS
```

不会影响 NAS 已归档照片。

如果 Immich 更换：

```text
Photos 不动
```

即可迁移。

---

# 50. 当前方案中的单点风险

需要明确：

### 风险 1：R5S USB HDD

当前老旧移动硬盘是临时缓存。

如果 NAS 长时间不开：

```text
R5S HDD 损坏
```

可能损失尚未归档的照片。

因此：

> R5S 上的照片不能被视为永久保存。

---

### 风险 2：NAS 主盘 + 备盘

两块盘虽然有主备，但属于：

```text
同步镜像
```

不等于：

```text
历史备份
```

因此需要后续增加：

```text
删除保护
```

---

### 风险 3：没有异地备份

V1 暂时接受。

因为当前明确要求：

> 不使用第三方云存储。

以后如果照片价值越来越高，可以增加离线硬盘作为第三份备份。

---

# 51. 关键数据流

## 新照片

```text
手机
 ↓
选择
 ↓
Upload
 ↓
R5S incoming
 ↓
SHA256
 ↓
EXIF
 ↓
日期判断
 ↓
去重
 ↓
pending
```

---

## NAS 开机

```text
NAS
 ↓
发现 R5S
 ↓
读取 pending
 ↓
创建目标目录
 ↓
复制照片
 ↓
SHA256 校验
 ↓
ARCHIVED
 ↓
R5S 清理
```

---

## Immich

```text
NAS Photos/
 ↓
External Library
 ↓
扫描
 ↓
索引
 ↓
人物/时间线/地图
```

---

# 52. 完整示例

手机上传：

```text
IMG_20260831_193215.jpg
```

R5S：

```text
SHA256:
AABBCCDDEEFF...

EXIF:
DateTimeOriginal:
2026:08:31 19:32:15
```

判断：

```text
year = 2026
year_month = 202608
```

目标：

```text
Photos/2026/202608/IMG_20260831_193215.jpg
```

NAS：

```text
copy
 ↓
SHA256 verification
 ↓
成功
```

数据库：

```text
status = ARCHIVED
```

R5S：

```text
删除临时文件
```

Immich：

```text
扫描 Photos/2026/202608/
 ↓
发现新照片
 ↓
加入时间线
 ↓
人脸识别
 ↓
地图
```

---

# 53. 验收标准

V1 上线前必须通过以下测试。

## 测试 1：普通照片

上传：

```text
IMG001.jpg
```

结果：

```text
Photos/YYYY/YYYYMM/IMG001.jpg
```

---

## 测试 2：重复上传

同一个文件上传两次。

结果：

```text
NAS 只有一份
```

---

## 测试 3：同名不同文件

两个：

```text
IMG001.jpg
```

但 SHA256 不同。

结果：

```text
两个文件都保留
```

且不覆盖。

---

## 测试 4：没有 EXIF

结果：

```text
不能错误归档
```

进入：

```text
quarantine/
```

---

## 测试 5：NAS 关闭

上传照片：

```text
R5S pending
```

NAS 开机以后：

```text
自动归档
```

---

## 测试 6：NAS 中途断电

同步过程中断电。

再次开机：

```text
可以继续
```

不能导致：

```text
重复照片
```

---

## 测试 7：原始文件完整性

上传前：

```text
SHA256 = A
```

NAS：

```text
SHA256 = A
```

必须一致。

---

## 测试 8：已有 1TB+ 照片

现有：

```text
Photos/
```

不移动、不重命名。

Immich 能够：

```text
直接扫描
```

---

## 测试 9：更换 Immich

删除 Immich 数据库。

重新建立 External Library：

```text
Photos/
```

照片能够重新出现。

---

## 测试 10：R5S 重启

上传/同步过程中 R5S 重启。

系统恢复后：

```text
状态不丢失
```

---

# 54. 开发阶段规划

建议分成以下阶段。

## Phase 1：照片导入核心

实现：

```text
Upload
SHA256
SQLite
EXIF
日期解析
去重
状态机
```

---

## Phase 2：NAS 自动归档

实现：

```text
NAS Discovery
Pending Queue
Archive
SHA256 Verification
Retry
```

---

## Phase 3：移动端体验

测试：

```text
Android 浏览器
手机文件选择器
可能的第三方上传客户端
```

确定最终手机上传方式。

---

## Phase 4：Immich 集成

配置：

```text
External Library
Read-only
定时扫描
Mobile App 查看
```

---

## Phase 5：NAS 主备优化

完善：

```text
rsync
删除保护
历史版本
异常恢复
```

---

## Phase 6：系统监控

增加：

```text
R5S 磁盘空间
pending 数量
失败数量
NAS 最后同步时间
最后成功归档时间
```

---

# 55. V1 最终架构定义

最终系统定义为：

```text
             ┌──────────────────┐
             │   手机 / 电脑     │
             └────────┬─────────┘
                      │
                   手工上传
                      │
                      ▼
             ┌──────────────────┐
             │       R5S        │
             │                  │
             │ Photo Gateway    │
             │                  │
             │ Upload           │
             │ SHA256           │
             │ EXIF             │
             │ SQLite           │
             │ Queue            │
             └────────┬─────────┘
                      │
                  Pending
                      │
                NAS 开机以后
                      │
                      ▼
             ┌──────────────────┐
             │    NAS 主照片盘   │
             │                  │
             │     Photos/      │
             │                  │
             │ YYYY/YYYYMM/     │
             └────────┬─────────┘
                      │
                    rsync
                      │
                      ▼
             ┌──────────────────┐
             │   NAS 备份照片盘  │
             └──────────────────┘

                      │
                      │ External Library
                      ▼
             ┌──────────────────┐
             │      Immich      │
             │                  │
             │ Timeline         │
             │ Face Recognition │
             │ Map              │
             │ Search           │
             │ Albums           │
             └──────────────────┘
```

---

# 56. 最重要的架构约束

后续开发过程中，以下规则未经明确变更，不得修改：

**R1：** `Photos/YYYY/YYYYMM/` 是正式照片目录标准。

**R2：** 已有照片不迁移、不重命名。

**R3：** 原始照片内容不可修改。

**R4：** Immich 不拥有原始照片。

**R5：** Immich 使用 External Library。

**R6：** 手机不通过 Immich Mobile Backup 上传照片。

**R7：** R5S 是临时缓存，不是永久存储。

**R8：** NAS 是正式照片资产库。

**R9：** SHA-256 是精确去重依据。

**R10：** 相似照片检测不能自动删除。

**R11：** NAS 离线必须是正常状态。

**R12：** NAS 上线以后自动处理 pending 队列。

**R13：** 文件必须完成 SHA-256 校验以后才能从 R5S 删除。

**R14：** 所有同步过程必须支持幂等和中断恢复。

**R15：** 照片文件与照片管理软件必须保持解耦。

---

# 57. V1 成功标准

当系统完成以后，你的日常使用应该变成：

```text
看到一张值得保存的照片
        ↓
手机选择照片
        ↓
上传
        ↓
不用管了
```

之后：

```text
NAS 平时关闭
        ↓
某天开机
        ↓
系统自动发现新照片
        ↓
自动检查
        ↓
自动去重
        ↓
自动读取 EXIF
        ↓
自动归档
        ↓
自动校验
        ↓
自动同步主备
        ↓
Immich 自动发现
```

最终你在手机上打开 Immich：

```text
时间线
人物
地图
相册
搜索
```

而底层仍然是你熟悉且完全可控的：

```text
Photos/
├── 2004/
│   ├── 200401/
│   └── 200402/
...
└── 2026/
    └── 202609/
```

即使十年以后不用 Immich：

```text
Photos/
```

仍然是一套独立、清晰、可直接读取的照片资产。