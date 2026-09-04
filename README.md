# Photo Gateway

Photo Gateway 是一个面向家庭照片管理场景设计的**照片导入网关与存储中间层**。

项目运行在 NanoPi R5S 上，负责将手机、电脑及其他局域网设备中的照片安全、可控地导入本地照片库，完成重复检测、元数据处理和日期归档。V2 在此基础上负责将 R5S 上的照片同步到 NAS，并按照安全策略管理 R5S 上的历史照片生命周期。

Photo Gateway 本身**不是完整的照片管理软件**。照片浏览、时间线、人脸识别、搜索等能力交给独立的照片管理软件，例如 Immich。Photo Gateway 只负责照片文件从客户端进入照片库，以及照片在 R5S 和 NAS 之间的可靠流转。

---

## 1. 项目定位

整个家庭照片系统的职责划分如下：

```text
手机 / 电脑 / 其他设备
          │
          │ 局域网
          ▼
┌─────────────────────────────┐
│       Photo Gateway         │
│          NanoPi R5S         │
│                             │
│  照片接收                   │
│  重复预检                   │
│  SHA-256                    │
│  EXIF / 元数据处理          │
│  日期归档                   │
│  V1 Processing Worker       │
│  V2 NAS 同步                │
│  历史数据清理               │
└──────────────┬──────────────┘
               │
               │ rsync over SSH
               ▼
┌─────────────────────────────┐
│             NAS             │
│       最终正式照片存储       │
└──────────────┬──────────────┘
               │
               │ External Library
               ▼
┌─────────────────────────────┐
│        照片管理软件          │
│    例如 Immich / 其他软件    │
└─────────────────────────────┘
```

各组件职责明确：

- **Photo Gateway**：本项目负责实施和维护。
- **R5S**：照片接收、处理、归档以及 NAS 同步的中间节点。
- **NAS**：照片最终正式存储空间。
- **照片管理软件**：负责照片浏览、搜索、时间线等上层管理能力，不参与 Photo Gateway 的导入和同步流程。

---

## 2. 核心设计原则

Photo Gateway 遵循以下几个核心原则：

### 2.1 照片文件与管理软件解耦

Photo Gateway 只负责维护原始照片文件和稳定的目录结构。

上层照片管理软件可以随时替换，不应该影响已经保存的照片。

### 2.2 照片导入与 NAS 同步解耦

V1 只负责：

```text
客户端 → R5S → 本地 Photos
```

V2 再负责：

```text
R5S → NAS
```

V1 不访问 NAS，也不提前引入 NAS 相关逻辑。

### 2.3 临时存储与最终存储解耦

R5S 是照片导入、处理和同步过程中的中间节点。

NAS 才是照片的最终正式存储位置。

因此：

```text
R5S
  = 接收 + 处理 + 临时保存 + 同步源

NAS
  = 最终正式存储
```

### 2.4 宁可不删除，也不能误删除

任何 R5S 照片删除操作都必须建立在明确的同步成功和生命周期条件之上。

V2 的 Cleanup 不根据“曾经同步成功过”简单判断，而是按照当前月份状态、最新同步任务以及保留期限进行独立判断。

---

## 3. 照片目录结构

整个项目统一使用：

```text
Photos/YYYY/YYYYMM/
```

例如：

```text
Photos/
├── 2004/
│   ├── 200401/
│   └── 200402/
├── 2025/
│   ├── 202501/
│   └── 202512/
└── 2026/
    ├── 202601/
    ├── 202602/
    └── 202608/
```

这是整个项目最重要的基础约束之一。

**V1 和 V2 均必须使用 `Photos/YYYY/YYYYMM/`，不得使用 `Photos/YYYY/MM/` 或其他日期目录结构。**

### 3.1 归档月份由照片时间确定

照片归档月份不是上传月份，而是根据照片元数据确定。

例如：

```text
2026-08-10 上传

照片 EXIF 拍摄时间：
2026-04-05

        ↓

Photos/2026/202604/
```

因此，即使一张历史照片在很久以后才上传，也仍然归档到它实际所属的月份。

---

## 4. V1：照片导入与本地归档

V1 是 Photo Gateway 的本地照片导入阶段。

整体流程：

```text
客户端
  ↓
创建 Upload Session
  ↓
Precheck
  ↓
筛选真正需要上传的文件
  ↓
上传到 R5S
  ↓
Processing Worker
  ↓
SHA-256 / 元数据处理
  ↓
确定归档日期
  ↓
Photos/YYYY/YYYYMM/
```

V1 主要负责：

- 局域网照片上传
- Upload Session
- 批量重复预检
- 用户确认机制
- SHA-256 最终重复判断
- EXIF / 元数据解析
- 日期归档
- SQLite 数据管理
- Processing Worker
- 结构化日志
- R5S 本地照片存储

### 4.1 两级重复检测

V1 使用两级重复检测机制。

第一阶段使用：

```text
source_device
+
original_filename
+
file_size
```

进行快速预检。

第二阶段使用：

```text
SHA-256
```

进行最终判断。

这样可以避免在大量明显重复的情况下，对所有文件立即执行完整 SHA-256 计算。

### 4.2 V1 不负责 NAS

V1 明确不负责：

```text
NAS 探测
NAS 挂载
NAS rsync
NAS 同步
NAS 同步状态
NAS 数据删除
```

V1 的最终输出就是 R5S 上稳定的：

```text
Photos/YYYY/YYYYMM/
```

---

## 5. V2：NAS 同步与 R5S 生命周期管理

V2 建立在 V1 正式照片目录之上，负责：

```text
NAS 状态检查
        ↓
V1 任务隔离
        ↓
NAS Ready Check
        ↓
Difference Check
        ↓
创建 Sync Batch / Sync Task
        ↓
rsync over SSH
        ↓
同步状态记录
        ↓
历史月份 Cleanup
```

V2 不改变照片目录结构，也不重新定义照片存储方式。

---

## 6. V2 Worker 生命周期

V2 Worker 是系统启动后持续运行的后台服务。

每一个 V2 工作周期都必须从 **NAS Alive Check** 开始。

整体生命周期：

```text
┌──────────────────────┐
│   NAS Alive Check    │
└──────────┬───────────┘
           │
      NAS 不可用
           │
           ▼
       等待下一轮
           
           │ NAS 可用
           ▼
┌──────────────────────┐
│   V1 Activity Check  │
└──────────┬───────────┘
           │
      V1 仍有任务
           │
           ▼
       等待 V1 空闲

           │ V1 空闲
           ▼
┌──────────────────────┐
│   NAS Ready Check    │
└──────────┬───────────┘
           │
      Ready 失败
           │
           ▼
       等待下一轮

           │ Ready 成功
           ▼
┌──────────────────────┐
│   Formal Processing  │
│                      │
│ Difference Check     │
│ Batch / Sync Task    │
│ Sync                 │
│ Cleanup              │
└──────────────────────┘
           │
           ▼
       等待下一轮
```

因此，V2 不会在 NAS 不可用、V1 仍然工作或者 NAS 尚未满足同步条件时进行正式同步操作。

---

## 7. V1 / V2 资源隔离

R5S 的 CPU、内存、USB HDD 和网络资源有限。

V2 不应该与 V1 的上传和照片处理任务竞争大量系统资源。

因此 V2 在正式同步之前必须确认 V1 已经完全空闲。

V1 Activity 包括：

- 活动中的 Upload Session
- 正在上传的任务
- 客户端上传完成但尚未处理完成的任务
- Worker 正在处理的任务
- Worker 队列中等待处理的任务

只有这些任务全部结束后，V2 才进入 NAS Ready 和正式同步阶段。

---

## 8. NAS 同步

V2 使用：

```text
rsync over SSH
```

执行：

```text
R5S → NAS
```

的单向同步。

照片同步以：

```text
YYYYMM
```

月份目录作为管理单位。

例如：

```text
Photos/2026/202604/
```

是一个同步管理单位。

不会：

- 将整个 `Photos/` 作为一个同步任务；
- 为每一张照片创建一个同步任务。

同一个月份可以在不同时间产生多次 Sync Task。

例如：

```text
202604 → SUCCESS
202604 → FAILED
202604 → SUCCESS
```

这样可以支持历史月份后续新增照片后的再次同步，同时保留完整的同步执行历史。

---

## 9. Difference Check

Difference Check 是 V2 判断“是否真正需要同步”的权威依据。

一个月份之前已经同步成功，并不意味着以后永远不会再次产生 Sync Task。

例如：

```text
202604
  ↓
第一次同步
  ↓
SUCCESS
```

之后如果 R5S 的：

```text
Photos/2026/202604/
```

又新增了历史照片：

```text
Difference Check
  ↓
发现差异
  ↓
创建新的 Sync Task
```

因此：

> **Sync Task 是一次具体执行记录，而不是某个 YYYYMM 的永久唯一记录。**

---

## 10. Batch、Sync Task 与 Month State

V2 使用三个相互独立的核心对象管理同步过程。

### 10.1 Batch

Batch 表示一次 V2 工作周期中，通过 Difference Check 发现的一组月份同步任务。

```text
Batch #100
├── 202604
├── 202605
└── 202606
```

Batch 主要用于组织一次同步周期。

### 10.2 Sync Task

Sync Task 表示某一个 `YYYYMM` 的一次具体同步执行。

例如：

```text
202604 → SUCCESS
202604 → FAILED
202604 → SUCCESS
```

每次实际执行都产生独立的 Sync Task。

Sync Task 状态包括：

```text
PENDING
RUNNING
SUCCESS
FAILED
```

### 10.3 Month State

Month State 表示某个 `YYYYMM` 的同步健康状态历史。

状态包括：

```text
NORMAL
ABNORMAL
RETRY_REQUESTED
```

Month State 是**历史记录**，而不是固定的一行当前状态。

同一个月份可以拥有：

```text
NORMAL
ABNORMAL
RETRY_REQUESTED
NORMAL
ABNORMAL
```

等多条历史记录。

当前状态通过该月份最新的 Month State 记录确定。

---

## 11. Failure Cycle 与故障隔离

V2 不会因为一次同步失败就永久阻塞某个月份。

每个 `YYYYMM` 都拥有独立的 failure cycle。

一个典型生命周期：

```text
首次发现 YYYYMM
        ↓
NORMAL
        ↓
当前 failure cycle
        ↓
连续 N 次 Sync Task FAILED
        ↓
ABNORMAL
```

当连续失败达到配置阈值后：

```text
ABNORMAL
```

该月份进入自动同步隔离。

后续 Difference Check 不会自动将该月份加入新的同步 Batch。

其他月份不受影响。

例如：

```text
202604 → ABNORMAL
202605 → NORMAL
202606 → NORMAL
```

202604 的同步故障不会阻止 202605、202606 正常同步或执行 Cleanup。

---

## 12. Retry 与新的 Failure Cycle

`ABNORMAL` 月份不能自动解除隔离。

只有人工通过 WebUI 执行 Retry，才能重新允许该月份进入同步流程。

Retry 状态转换：

```text
ABNORMAL
    ↓
RETRY_REQUESTED
    ↓
创建第一个新的 Sync Task
    ↓
NORMAL
    ↓
新的 failure cycle
```

这里有一个重要原则：

> **人工 Retry 会建立新的 failure cycle。**

因此 Retry 之前的失败记录不会参与新的连续失败计算。

例如失败阈值为 3：

```text
Cycle 1:

#101 FAILED
#102 FAILED
#103 FAILED
        ↓
ABNORMAL

人工 Retry
        ↓
RETRY_REQUESTED

Cycle 2:

创建 #104
        ↓
NORMAL

#104 FAILED
        ↓
连续失败 = 1

#105 FAILED
        ↓
连续失败 = 2

#106 FAILED
        ↓
连续失败 = 3
        ↓
ABNORMAL
```

旧 failure cycle 中的失败不会污染新的 failure cycle。

---

## 13. Cleanup：R5S 历史照片生命周期

R5S 是临时存储与同步节点，因此历史照片在满足安全条件后可以从 R5S 删除。

默认保留期限：

```text
synced_retention_days = 90
```

Cleanup 不是“同步完成后的附属步骤”，而是 V2 每个满足前置条件的工作周期都会执行的独立阶段。

也就是说：

```text
NAS Alive
    ↓
V1 Idle
    ↓
NAS Ready
    ↓
正式处理
    ├── Difference / Sync
    └── Cleanup
```

即使：

- 本轮没有 Batch；
- 本轮没有 Sync Task；
- 本轮部分 Sync Task 失败；

只要 V2 已经满足：

```text
NAS Alive
V1 Idle
NAS Ready
```

仍然会执行 Cleanup。

---

## 14. Cleanup 安全规则

Cleanup 对所有 `YYYYMM` 目录独立判断。

一个月份只有在满足全部必要条件后才能删除。

核心条件包括：

```text
当前 Month State = NORMAL
        +
存在 Sync Task
        +
最新 Sync Task = SUCCESS
        +
满足 retention
```

如果最新 Sync Task 是：

```text
PENDING
RUNNING
FAILED
```

则禁止删除。

如果该月份没有任何 Sync Task，也禁止删除。

如果月份当前状态为：

```text
ABNORMAL
```

或：

```text
RETRY_REQUESTED
```

同样禁止删除。

### 14.1 Cleanup 不依赖本轮 Batch

Cleanup 检查的是 R5S 上全部月份目录，而不是只检查当前 Batch 中的月份。

例如：

```text
Photos/
├── 2025/202501/
├── 2025/202512/
├── 2026/202601/
└── 2026/202608/
```

即使本轮 Batch 只有：

```text
2026/202608
```

Cleanup 仍然会逐个检查其他月份。

因此：

> **同步任务与 Cleanup 的执行范围相互独立。**

### 14.2 Cleanup 不进行第二次 NAS Check

当前 V2 周期已经完成：

```text
NAS Alive
    ↓
V1 Idle
    ↓
NAS Ready
```

之后进入正式处理阶段。

Cleanup 前不再重复执行一次 NAS Alive / NAS Ready Check。

如果 NAS 在同步过程中发生故障，对应 Sync Task 会失败；后续工作周期重新从 NAS Alive Check 开始。

---

## 15. 数据安全边界

V2 使用：

```text
R5S → NAS
```

单向同步。

项目不实现：

```text
NAS → R5S
```

反向同步，也不实现双向文件同步。

正常同步禁止使用：

```text
rsync --delete
```

V2 不通过 rsync 的删除能力反向修改 NAS 数据。

R5S 上月份目录的删除只能由 Cleanup 根据项目定义的状态和保留策略执行。

---

## 16. Immich 与照片管理软件

Photo Gateway 不负责照片浏览、搜索、时间线、人脸识别等上层照片管理能力。

当前计划使用 Immich 作为上层照片管理软件，通过 External Library 读取正式照片目录。

逻辑关系：

```text
Photo Gateway
      ↓
Photos/YYYY/YYYYMM/
      ↓
NAS
      ↓
External Library
      ↓
Immich
```

Photo Gateway：

- 不使用 Immich 内部照片存储机制；
- 不依赖 Immich 数据库；
- 不把 Immich 作为核心业务组件；
- 不要求 Photo Gateway 与 Immich 强耦合。

如果未来更换照片管理软件，只要新的软件能够读取现有照片目录，就不应该影响 Photo Gateway 保存的原始照片。

---

## 17. 存储架构

当前整体数据流为：

```text
                         ┌─────────────────┐
                         │    Client       │
                         │ 手机 / 电脑等    │
                         └────────┬────────┘
                                  │
                                  │ LAN
                                  ▼
                         ┌─────────────────┐
                         │ Photo Gateway   │
                         │   NanoPi R5S    │
                         │                 │
                         │ Upload          │
                         │ Precheck        │
                         │ Processing      │
                         │ Archive         │
                         │ Sync            │
                         │ Cleanup         │
                         └────────┬────────┘
                                  │
                         Photos/YYYY/YYYYMM/
                                  │
                           rsync over SSH
                                  │
                                  ▼
                         ┌─────────────────┐
                         │       NAS       │
                         │  正式照片存储    │
                         └────────┬────────┘
                                  │
                           External Library
                                  │
                                  ▼
                         ┌─────────────────┐
                         │ Photo Manager   │
                         │ Immich / other  │
                         └─────────────────┘
```

核心数据流方向：

```text
Client → R5S → NAS
```

照片管理软件只读取照片库，不参与照片导入和 NAS 同步。

---

## 18. R5S 目录

V1 在 R5S 上使用：

```text
/mnt/sda1/photo-gateway/
```

主要目录：

```text
photo-gateway/
├── incoming/
├── processing/
├── failed/
├── Photos/
├── logs/
└── database/
```

其中：

```text
incoming/
```

用于保存上传中的临时文件。

```text
processing/
```

用于保存 Worker 当前正在处理的文件。

```text
failed/
```

用于保存处理失败且无法自动恢复的文件。

```text
Photos/
```

是正式照片目录。

```text
logs/
```

保存系统运行、上传、处理、审计和错误日志。

```text
database/
```

保存 SQLite 数据库。

---

## 19. 数据与状态管理

V1 使用 SQLite 保存照片导入和处理相关状态。

核心数据包括：

- 设备
- Upload Session
- Photo Asset
- Upload Item
- Photo Event

V2 在此基础上增加：

- Sync Batch
- Sync Task
- Month State History

数据库负责保存系统状态和历史数据。

日志负责记录运行过程和状态变化过程。

两者职责不同，日志不能替代数据库。

---

## 20. 时间规范

整个项目统一使用：

```text
上海时区语义
YYYY-MM-DD HH:MM:SS
```

时间字段的具体使用方式和数据库字段定义，以对应版本的 Spec 为准。

---

## 21. 当前项目范围

### 本项目负责

- 局域网照片上传
- Upload Session
- 重复预检
- SHA-256
- EXIF / 元数据处理
- 照片日期归档
- R5S 本地照片存储
- SQLite 数据管理
- Processing Worker
- 结构化日志
- R5S → NAS 同步
- Sync Batch / Sync Task
- 同步历史记录
- Month State History
- Failure Cycle
- ABNORMAL 故障隔离
- 人工 Retry
- R5S 历史照片 Cleanup

### 本项目不负责

- Immich 本身的开发
- 照片浏览器
- 人脸识别
- AI 照片分类
- 公网照片服务
- 云端照片同步
- NAS 操作系统
- NAS RAID 管理
- NAS → R5S 反向同步
- 双向文件同步
- 视频管理
- 视频上传
- 照片编辑
- 照片格式转换
- 图片压缩

---

## 22. 版本规划

```text
V1
│
├── 客户端上传
├── Upload Session
├── Batch Precheck
├── 用户确认机制
├── SHA-256
├── EXIF / 元数据
├── 日期归档
├── SQLite
├── Processing Worker
├── 结构化日志
└── R5S 本地 Photos/YYYY/YYYYMM/
        │
        ▼
V2
│
├── NAS Alive Check
├── V1 Activity Check
├── NAS Ready Check
├── Difference Check
├── Sync Batch
├── 月份级 Sync Task
├── Failure Cycle
├── Month State History
├── ABNORMAL 故障隔离
├── 人工 Retry
├── R5S → NAS rsync over SSH
└── R5S 历史月份 Cleanup
        │
        ▼
照片管理软件
│
└── External Library
    └── 读取正式照片目录
```

V1 和 V2 的详细功能边界以各自版本的技术规格书为准。

---

## 23. 项目文档

项目详细设计由以下文档定义：

- [V1 技术规格](docs/photo-gateway-v1-spec.md)
- [V1 配置规格](docs/photo-gateway-v1-config-spec.md)
- [V1 数据库规格](docs/photo-gateway-v1-db-spec.md)
- [V1 API 规格](docs/photo-gateway-v1-api-spec.md)
- [V1 WebUI 规格](docs/photo-gateway-v1-webui-spec.md)
- [V2 技术规格](docs/photo-gateway-v2-spec.md)

文档职责：

```text
README
  ↓
项目整体定位 / 架构 / 职责 / 生命周期

V1 Spec
  ↓
V1 功能与技术设计

V1 Config Spec
  ↓
V1 配置规范

V1 DB Spec
  ↓
V1 数据模型与数据库约束

V1 API Spec
  ↓
V1 HTTP API 契约

V1 WebUI Spec
  ↓
V1 WebUI 行为与界面规范

V2 Spec
  ↓
NAS 同步 / Failure Cycle / Cleanup / V2 数据模型
```

README 只负责描述项目整体设计，不重复各 Spec 中的详细实现规则。

**具体实现以对应版本的 Spec 为最终依据。**

---

## 24. 设计理念

Photo Gateway 的核心设计可以概括为：

> **照片文件与照片管理软件解耦，照片导入与照片同步解耦，临时存储与最终存储解耦。**

整个系统最重要的长期资产不是 Photo Gateway 本身，而是照片文件以及稳定的：

```text
Photos/YYYY/YYYYMM/
```

目录结构。

只要这一核心目录结构保持稳定：

```text
客户端如何变化
    +
Photo Gateway 如何演进
    +
NAS 如何变化
    +
上层照片管理软件如何替换
```

都不应该影响已经保存的原始照片。

这也是 Photo Gateway 整个项目设计的核心目标。