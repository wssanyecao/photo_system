# Photo Gateway V2 Specification

**文档版本：** V2.0  
**文档状态：** Design Baseline  
**适用项目：** `photo_system` / Photo Gateway  
**运行节点：** NanoPi R5S  
**最终存储：** NAS  
**更新时间：** 2026-09-02

---

## 1. 文档目的

本文档定义 Photo Gateway V2 的 NAS 同步与 R5S 临时存储清理机制。

V2 建立在 V1 已完成的照片接收、去重、元数据提取、日期归档和 Photo Asset 管理能力之上，主要解决以下问题：

1. 将 R5S 上已经完成归档的照片同步到 NAS。
2. 以月份目录为同步管理单位，对每次实际同步操作建立数据库记录。
3. 支持同一个月份目录被多次同步，例如历史月份再次上传新照片。
4. 在同步失败时支持后续重试，并保留完整同步历史。
5. 在确认数据已经成功同步并满足保留期限后，删除 R5S 上的历史临时数据。
6. 保证 V1 照片接收/处理任务与 V2 NAS 同步任务之间不存在资源竞争。
7. 保证任何情况下都不会因为 R5S 清理而删除 NAS 上的正式照片。

V2 不改变 V1 已定义的照片接收、去重、元数据提取和正式归档规则。

---

# 2. 核心设计原则

## 2.1 全项目统一照片目录结构

无论 V1 还是 V2，照片正式归档目录统一使用：

```text
Photos/YYYY/YYYYMM/
```

例如：

```text
Photos/
├── 2025/
│   ├── 202501/
│   ├── 202502/
│   └── 202512/
└── 2026/
    ├── 202601/
    ├── 202602/
    └── 202604/
```

**严禁使用 `Photos/YYYY/MM/` 作为项目中的照片存储结构。**

例如一张照片 EXIF 时间为：

```text
2026-04-05 12:30:00
```

无论客户端实际上传时间是 2026 年 8 月还是其他时间，该照片均归档到：

```text
Photos/2026/202604/
```

V2 同步时也必须以此目录作为同步源。

---

## 2.2 R5S 与 NAS 的角色

V2 中：

```text
R5S = 临时存储 / 同步源
NAS = 最终正式存储
```

照片生命周期：

```text
客户端
  ↓
R5S
  ↓
V1 接收、去重、处理
  ↓
Photos/YYYY/YYYYMM/
  ↓
V2 rsync
  ↓
NAS Photos/YYYY/YYYYMM/
  ↓
R5S 保留一段时间
  ↓
Cleanup
  ↓
删除 R5S 历史月份目录
```

R5S 上的照片在 V2 中属于最终同步前的临时副本。

NAS 是照片的最终正式存储空间。

---

## 2.3 V2 不负责修改 V1 的归档规则

V2 不重新计算照片归档月份。

照片所属月份已经由 V1 根据照片元数据确定。

例如：

```text
客户端上传时间：
2026-08-10

照片 EXIF：
2026-04-05
```

V1 将照片归档：

```text
Photos/2026/202604/
```

V2 发现 `202604` 目录存在需要同步的数据后，负责将：

```text
Photos/2026/202604/
```

同步到 NAS。

---

## 2.4 Sync Task 的业务含义

`photo_sync_tasks` 表记录的是：

> **一次实际发生的“某个月份目录同步操作”。**

它不是“月份状态表”。

因此：

```text
202604
```

不是唯一键。

同一个月份可以在不同时间产生多次 Sync Task。

例如：

```text
2026-05-01：
202604 → SUCCESS

2026-08-10：
又上传 3 张 202604 的历史照片
202604 → SUCCESS
```

数据库应该保留两次同步记录，而不是覆盖第一次记录。

---

## 2.5 时间格式统一

V2 所有数据库时间字段、API 示例、日志示例和文档中的业务时间统一使用：

```text
YYYY-MM-DD HH:MM:SS
```

例如：

```text
2026-08-10 09:30:08
```

不使用 ISO 8601 的：

```text
2026-08-10T09:30:08
```

作为业务层标准展示格式。

---

# 3. V2 整体架构

```text
                     ┌─────────────────────┐
                     │      Client          │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │     V1 Worker        │
                     │ 接收 / 去重 / 处理     │
                     └──────────┬──────────┘
                                │
                                ▼
                    R5S Photos/YYYY/YYYYMM/
                                │
                                │
                    ┌───────────▼───────────┐
                    │      V2 Sync Worker    │
                    └───────────┬───────────┘
                                │
                       等待 V1 完全空闲
                                │
                                ▼
                     NAS Ready Check
                                │
                                ▼
                      Sync Phase
                                │
                   ┌────────────┴────────────┐
                   │                         │
                   ▼                         ▼
             202604 Task                202608 Task
                   │                         │
                   └────────────┬────────────┘
                                │
                             rsync
                                │
                                ▼
                     NAS Photos/YYYY/YYYYMM/
                                │
                                ▼
                     Sync Phase Complete
                                │
                                ▼
                       Cleanup Phase
                                │
                       满足全部删除条件
                                │
                                ▼
                  删除 R5S/YYYY/YYYYMM/
```

---

# 4. V2 工作阶段

V2 Worker 每次运行按照以下阶段执行：

```text
IDLE
  ↓
PRECHECK
  ↓
WAIT_V1_IDLE
  ↓
SYNC
  ↓
SYNC_COMPLETED
  ↓
CLEANUP
  ↓
IDLE
```

如果 NAS 不可用、V1 正在工作或者其他前置条件不满足，Worker 不执行危险操作，等待下一轮处理。

---

# 5. V1 / V2 资源隔离

V1 和 V2 共用 R5S 的 CPU、内存、磁盘和网络资源。

由于 R5S 使用 USB HDD 作为照片存储，必须避免 V1 文件接收/处理与 V2 rsync 同时进行大规模 I/O。

因此 V2 必须遵守：

> **V1 存在活动任务时，V2 可以创建或维护同步任务记录，但不得开始实际 rsync。**

---

## 5.1 什么是 V1 活动任务

V2 判断 V1 是否空闲时，不能只检查某一个数据库表，也不能只判断是否存在 Session。

V1 以下任一状态均视为存在活动任务：

```text
CREATED
UPLOADING
UPLOADED
PROCESSING
```

其中：

- `CREATED`：Session 已建立但尚未完成。
- `UPLOADING`：客户端仍在上传。
- `UPLOADED`：客户端上传阶段已经完成，但 V1 Worker 仍可能尚未完成处理。
- `PROCESSING`：V1 Worker 正在执行照片处理。

特别注意：

> `UPLOADED` 不能被视为 V1 已经完全空闲。

因为 V1 的 `/complete` 只代表客户端上传阶段结束，之后仍可能进入 Worker Processing。

---

## 5.2 V1 Worker 活动状态

除 Session 状态外，V2 还必须考虑 V1 Worker 是否存在：

```text
pending
processing
```

等尚未完成的处理任务。

因此：

> **V2 只有在 V1 没有活动 Upload Session，并且 V1 Worker 没有待处理或正在处理的照片任务时，才能开始实际同步。**

最终判断目标是：

```text
V1 Activity = 0
```

而不是简单判断：

```text
upload_sessions = 0
```

---

## 5.3 V1 忙碌时 V2 的行为

例如：

```text
V1：
正在处理 202608 的 1000 张照片

V2：
发现 202605、202608 有待同步数据
```

V2 可以：

```text
创建 Sync Task
```

或者保留已有：

```text
PENDING
```

任务。

但不能执行：

```text
rsync
```

必须等待：

```text
V1 Activity = 0
```

之后才能进入 Sync Phase。

---

# 6. Sync Task 模型

## 6.1 Task 与月份的关系

一个 Sync Task 对应：

```text
一次实际的某个 YYYYMM 目录同步操作
```

例如：

```text
Task 1001
archive_month = 202604

Task 1002
archive_month = 202608
```

同一个月份可以拥有多个 Task：

```text
Task 1001 → 202604 → SUCCESS
Task 1058 → 202604 → FAILED
Task 1092 → 202604 → SUCCESS
```

这些记录全部保留。

---

## 6.2 不允许使用 YYYYMM 作为唯一键

以下设计禁止：

```text
UNIQUE(archive_month)
```

因为旧月份可以再次出现新照片。

例如：

```text
2026-08-10
客户端上传一张 EXIF 为 2026-04-05 的照片
```

V1：

```text
Photos/2026/202604/new.jpg
```

如果 `202604` 之前已经同步过，V2 仍然必须创建新的同步任务。

---

# 7. Sync Task 状态

V2 建议使用以下状态：

```text
PENDING
RUNNING
SUCCESS
FAILED
```

## 7.1 PENDING

表示：

> 该月份存在待同步内容，但尚未执行同步。

可能原因：

- V1 当前仍然忙碌。
- NAS 暂时不可用。
- 前一个同步任务尚未完成。
- Worker 尚未调度该任务。

---

## 7.2 RUNNING

表示：

> 当前正在执行该月份目录的 rsync。

---

## 7.3 SUCCESS

表示：

> 该次月份目录同步已经成功完成。

`completed_at` 记录本次成功完成时间。

---

## 7.4 FAILED

表示：

> 本次月份目录同步失败。

必须记录失败原因以及 rsync exit code。

失败任务允许后续重新同步。

---

# 8. Sync Task 数据库设计

V2 新增：

```text
photo_sync_tasks
```

建议字段：

| 字段             | 类型     | 说明                                 |
| ---------------- | -------- | ------------------------------------ |
| id               | INTEGER  | 主键                                 |
| archive_month    | TEXT     | 同步月份，格式 `YYYYMM`              |
| status           | TEXT     | PENDING / RUNNING / SUCCESS / FAILED |
| started_at       | DATETIME | 本次同步开始时间                     |
| completed_at     | DATETIME | 本次同步结束时间                     |
| duration_seconds | INTEGER  | 同步耗时                             |
| file_count       | INTEGER  | 本次同步处理/传输的文件数            |
| total_size_bytes | INTEGER  | 本次同步文件总大小                   |
| rsync_exit_code  | INTEGER  | rsync 返回码                         |
| attempt_count    | INTEGER  | 尝试次数                             |
| error_message    | TEXT     | 最后一次错误信息                     |
| created_at       | DATETIME | 创建时间                             |
| updated_at       | DATETIME | 更新时间                             |

所有时间字段使用：

```text
YYYY-MM-DD HH:MM:SS
```

---

# 9. Sync Task 的文件统计

V2 不建立“每张照片一个 Sync Task”的模型。

例如：

```text
202604/
├── 001.jpg
├── 002.jpg
├── ...
└── 500.jpg
```

只产生一个：

```text
photo_sync_tasks
archive_month = 202604
```

rsync 负责目录内部的增量判断。

Task 层记录：

```text
file_count
total_size_bytes
duration_seconds
```

用于审计和统计。

不需要为每个照片建立同步数据库记录。

---

# 10. 同步目录

R5S：

```text
Photos/YYYY/YYYYMM/
```

NAS：

```text
Photos/YYYY/YYYYMM/
```

例如：

```text
R5S:
Photos/2026/202604/

NAS:
Photos/2026/202604/
```

必须保持相同的目录结构。

---

# 11. rsync 同步策略

V2 使用：

```text
R5S → rsync over SSH → NAS
```

而不是 rsyncd。

V2 第一版默认：

```text
SSH
+
rsync
```

如果未来实际部署中 SSH 方式存在问题，再考虑 rsyncd 作为替代方案。

---

## 11.1 同步粒度

一次 Task 对应一次月份目录级 rsync：

```text
Photos/2026/202604/
```

而不是：

```text
photo1 → rsync
photo2 → rsync
photo3 → rsync
```

因此：

```text
一个月份 = 一个 Sync Task = 一次目录级 rsync
```

rsync 自身负责判断：

- 新文件；
- 已存在文件；
- 需要更新的文件。

---

## 11.2 禁止 `--delete`

V2 正常同步禁止使用：

```text
rsync --delete
```

原因：

R5S 是临时存储。

当 R5S Cleanup 删除：

```text
Photos/2026/202604/
```

时，不能导致 NAS：

```text
Photos/2026/202604/
```

被删除。

因此：

```text
R5S 删除
    ↓
不会影响 NAS
```

NAS 数据只允许通过明确的 NAS 管理流程删除，不允许被 R5S Cleanup 间接删除。

---

## 11.3 NAS SHA-256 校验

V2 不执行 NAS 端 SHA-256 校验。

不执行：

```text
R5S SHA-256
    ↓
SSH 到 NAS
    ↓
NAS 重新计算 SHA-256
    ↓
逐文件比较
```

V2 的同步成功依据主要使用：

```text
rsync exit code
```

如果未来需要 NAS 数据完整性检查，应设计独立的 NAS Integrity Audit，而不是作为普通 Sync Worker 的组成部分。

---

# 12. NAS Ready Check

V2 Worker 启动后以及执行 Cleanup 前，都必须检查 NAS 是否 Ready。

检查分为两层。

---

## 12.1 第一层：SSH TCP Port Check

R5S 首先检查 NAS SSH 端口是否可连接。

例如：

```text
NAS:22
```

使用短超时进行 TCP Connect。

这一阶段只判断：

```text
SSH Port Open
```

不直接执行 SSH 命令。

目的：

> 避免 NAS SSH 服务异常时，直接 SSH 导致 Worker 长时间阻塞。

---

## 12.2 第二层：NAS Ready Check

SSH 端口可用后，R5S 使用专用同步账户执行 NAS Ready Check Shell。

Ready Check 至少检查：

1. NAS 目标磁盘已经挂载。
2. 文件系统处于可写状态。
3. 可用空间满足配置要求。
4. NAS 目标目录存在。
5. 同步用户对目标目录具有写权限。
6. NAS 已安装 rsync。
7. 必要的文件系统和存储条件正常。

建议脚本：

```text
/usr/local/sbin/photo-gateway-nas-ready.sh
```

---

## 12.3 Ready Check 返回值

建议：

```text
0 = READY
1 = NOT_READY
2 = CONFIG_ERROR
```

Worker 必须根据返回值决定后续动作。

---

# 13. Sync Worker 启动流程

Worker 启动后：

```text
1. 加载配置
2. 初始化数据库
3. 检查 NAS SSH Port
4. 执行 NAS Ready Check
5. 检查 V1 Activity
6. 扫描 Photos/YYYY/YYYYMM
7. 创建/维护待同步任务
8. 执行 Sync Phase
9. 判断 Sync Phase 是否完成
10. 满足条件后进入 Cleanup Phase
11. 返回 IDLE
```

Worker 应作为系统服务启动。

---

# 14. 待同步月份发现

V2 Worker 需要扫描：

```text
Photos/YYYY/YYYYMM/
```

发现需要同步的月份目录。

需要特别处理以下情况：

### 情况 A：新月份

例如：

```text
Photos/2026/202608/
```

此前没有同步记录。

创建：

```text
PENDING
```

Task。

### 情况 B：历史月份再次出现新照片

例如：

```text
202604
```

此前已经：

```text
SUCCESS
```

但 V1 又向：

```text
Photos/2026/202604/
```

写入新照片。

V2 必须产生新的同步任务，而不是复用或覆盖旧 Task。

### 情况 C：上一次同步失败

例如：

```text
202604
FAILED
```

后续 Worker 应允许重新执行。

---

# 15. Sync Phase

Sync Phase 是一个明确的阶段。

其职责只有：

> 将所有当前需要同步的月份目录同步到 NAS。

例如：

```text
PENDING:
202604
202607
202608
```

Worker：

```text
202604 → rsync → SUCCESS
202607 → rsync → SUCCESS
202608 → rsync → FAILED
```

此时不能直接进入 Cleanup。

---

# 16. Sync Phase 完成条件

只有满足以下条件，Sync Phase 才能标记为完成：

> **当前不存在需要继续处理的待同步任务。**

如果仍存在：

```text
PENDING
```

或者需要重试的：

```text
FAILED
```

任务，则 Sync Phase 尚未完成。

因此：

```text
存在待同步任务
        ↓
不能 Cleanup
```

这是 V2 的硬性安全规则。

---

# 17. 同步失败与重试

rsync 返回非 0 时：

```text
Task.status = FAILED
```

并记录：

```text
rsync_exit_code
error_message
attempt_count
completed_at
```

下一次 Worker 可以重新调度该月份。

重新执行时：

```text
FAILED
  ↓
RUNNING
  ↓
SUCCESS
```

或者：

```text
FAILED
  ↓
RUNNING
  ↓
FAILED
```

每次实际执行均产生独立的 Sync Task 记录。

历史记录不得覆盖。

---

# 18. 首次历史数据同步

V2 首次部署时，R5S 可能已经存在大量历史照片，例如：

```text
> 1 TB
```

首次同步仍然按照月份目录进行。

例如：

```text
Photos/
├── 2024/
│   ├── 202401/
│   ├── 202402/
│   └── ...
├── 2025/
│   ├── 202501/
│   └── ...
└── 2026/
    ├── 202601/
    └── ...
```

Worker 根据月份目录建立同步任务。

不创建：

```text
1 个文件 = 1 个 Task
```

而是：

```text
1 个月份目录 = 1 个 Task
```

首次同步可以持续多个 Worker 周期完成。

---

# 19. Cleanup Phase

Cleanup 是独立于 Sync Phase 的第二阶段。

流程必须是：

```text
Sync Phase
    ↓
确认 Sync Phase 完成
    ↓
Cleanup Phase
```

禁止：

```text
同步一个月份
    ↓
马上删除这个月份
    ↓
再同步下一个月份
```

同步和删除必须分离。

---

# 20. Cleanup 的第一道安全条件

Cleanup 开始前必须确认：

> **Sync Phase 已明确完成，并且不存在待同步任务。**

如果仍然存在：

```text
PENDING
```

或者：

```text
FAILED
```

等尚未完成的同步任务：

```text
禁止 Cleanup
```

这样可以避免：

```text
某个月份尚未成功同步
        ↓
Cleanup 却开始删除
        ↓
R5S 数据丢失
```

---

# 21. Cleanup 的月份判断

Cleanup 对每一个：

```text
Photos/YYYY/YYYYMM/
```

目录独立判断。

例如：

```text
Photos/2026/202604/
```

首先查询：

```text
photo_sync_tasks
```

中：

```text
archive_month = '202604'
```

的任务记录。

然后按照任务创建/执行时间确定**最新的一条 Sync Task**。

---

# 22. Cleanup 最核心的判断规则

这是 V2 最重要的安全规则之一：

> **判断一个月份目录是否可以删除，只能依据该月份最新一条 Sync Task 的最终状态。**

不能使用：

> “历史上存在一条 SUCCESS 就可以删除”。

也不能使用：

> “最近一次 SUCCESS 的时间满足 retention 就可以删除”。

必须首先取得：

```text
该 archive_month 最新的一条 Sync Task
```

然后判断：

```text
latest_task.status
```

---

# 23. 最新状态不是 SUCCESS：禁止删除

例如：

```text
202604

Task 1001
2026-05-01 10:00:00
SUCCESS

Task 1058
2026-08-10 09:00:00
FAILED
```

虽然存在：

```text
SUCCESS
```

但最新任务是：

```text
FAILED
```

因此：

```text
Photos/2026/202604/
```

**禁止删除。**

---

# 24. 最新状态为 SUCCESS：才允许继续判断

例如：

```text
202604

Task 1001
SUCCESS
2026-05-01 10:00:00

Task 1058
SUCCESS
2026-08-10 09:00:00
```

最新任务：

```text
Task 1058
status = SUCCESS
completed_at = 2026-08-10 09:00:00
```

此时才进入 retention 判断。

---

# 25. Cleanup Retention 判断

配置：

```yaml
sync:
  cleanup:
    enabled: true
    synced_retention_days: 90
```

Retention 的计算基准为：

> **该月份最新一条 SUCCESS Sync Task 的 `completed_at`。**

计算：

```text
delete_after =
    latest_success_task.completed_at
    + synced_retention_days
```

只有：

```text
current_time >= delete_after
```

才允许继续删除。

---

# 26. 成功后再次失败时的特殊情况

例如：

```text
202604

2026-06-05 12:00:00
SUCCESS

2026-08-10 12:00:00
FAILED
```

最终状态：

```text
FAILED
```

因此禁止删除。

即使：

```text
2026-09-05
```

已经超过此前 SUCCESS 的 90 天，也不能删除。

必须等待新的同步成功：

```text
2026-08-11 10:00:00
SUCCESS
```

之后 retention 重新从：

```text
2026-08-11 10:00:00
```

开始计算。

这意味着：

> **每次新的成功同步都会重新开始该月份目录的 retention 周期。**

---

# 27. 没有 Sync Task 记录：禁止删除

如果：

```text
Photos/2026/202604/
```

存在，但数据库中：

```text
archive_month = 202604
```

完全没有 Sync Task：

```text
禁止删除
```

这是绝对安全原则。

---

# 28. 最新 Task 为其他非 SUCCESS 状态：禁止删除

以下状态均不能删除：

```text
PENDING
RUNNING
FAILED
```

只有：

```text
SUCCESS
```

才允许继续进行 retention 判断。

---

# 29. Cleanup 时必须再次检查 NAS Ready

即使同步阶段已经确认 NAS Ready，Cleanup 阶段也必须重新检查。

原因：

```text
Sync Phase：
NAS Ready

        ↓

经过一段时间

        ↓

Cleanup：
NAS 已掉线
```

此时必须：

```text
禁止删除
```

因此 Cleanup 前必须重新执行：

```text
SSH Port Check
+
NAS Ready Check
```

---

# 30. Cleanup 的完整判断顺序

对于：

```text
Photos/YYYY/YYYYMM/
```

必须严格按照以下逻辑：

```text
① Cleanup 是否启用？
        │
        ├─ 否 → 不删除
        │
        ▼
② Sync Phase 是否已经完成？
        │
        ├─ 否 → 不删除
        │
        ▼
③ 当前是否仍存在待同步任务？
        │
        ├─ 是 → 不删除
        │
        ▼
④ 查询该 YYYYMM 最新 Sync Task
        │
        ├─ 无记录 → 不删除
        │
        ▼
⑤ 最新 Task.status 是否为 SUCCESS？
        │
        ├─ 否 → 不删除
        │
        ▼
⑥ 使用最新 SUCCESS Task.completed_at
   判断 synced_retention_days
        │
        ├─ 未到期 → 不删除
        │
        ▼
⑦ 再次检查 NAS Ready
        │
        ├─ NOT_READY → 不删除
        │
        ▼
⑧ 删除 Photos/YYYY/YYYYMM/
```

这是 V2 Cleanup 的正式安全规则。

---

# 31. Cleanup 删除粒度

正常情况下，Cleanup 以：

```text
YYYYMM
```

目录为删除单位。

例如：

```text
Photos/2026/202604/
```

满足删除条件后：

```text
删除 202604/
```

---

# 32. 严禁删除上级年份目录

删除：

```text
Photos/2026/202604/
```

之后，即使：

```text
Photos/2026/
```

已经为空，也不得自动删除：

```text
Photos/2026/
```

因此：

```text
允许：
Photos/2026/202604/

禁止：
Photos/2026/
Photos/
```

V2 Cleanup 不负责删除年份目录和根目录。

---

# 33. Cleanup 与 `rsync --delete` 的关系

两者必须完全独立。

R5S：

```text
Cleanup
    ↓
删除 Photos/YYYY/YYYYMM/
```

不会执行：

```text
rsync --delete
```

因此：

```text
R5S：
202604 已删除

NAS：
202604 仍然保留
```

NAS 是最终正式存储。

---

# 34. 磁盘紧张情况下的扩展

正式 V2 的默认 Cleanup 单位为：

```text
YYYYMM
```

但配置仍然使用：

```text
synced_retention_days
```

而不是：

```text
synced_retention_months
```

原因是未来可能存在：

> R5S 磁盘空间紧张，需要更细粒度的清理策略。

例如未来可以扩展为按天判断，而不需要改变整个 retention 配置模型。

V2 第一版不要求实现按天删除，但数据模型和配置不应阻碍未来扩展。

---

# 35. Cleanup 操作必须具备安全边界

Cleanup 必须满足以下原则：

1. 没有成功同步记录 → 不删除。
2. 最新同步状态不是 SUCCESS → 不删除。
3. retention 未到期 → 不删除。
4. 当前 Sync Phase 未完成 → 不删除。
5. 当前仍存在待同步任务 → 不删除。
6. NAS 当前不是 Ready → 不删除。
7. 只删除 `YYYYMM` 目录。
8. 不删除 `YYYY` 上级目录。
9. 不使用 `rsync --delete`。
10. 不因为 NAS Ready Check 临时失败而继续删除。

---

# 36. V2 与 V1 的数据一致性关系

V1 负责：

```text
客户端数据
    ↓
接收
    ↓
去重
    ↓
元数据
    ↓
归档
    ↓
Photos/YYYY/YYYYMM/
```

V2 负责：

```text
Photos/YYYY/YYYYMM/
    ↓
NAS
```

因此：

> V2 不参与 V1 的照片处理决策。

V2 不修改：

- EXIF；
- 归档月份；
- Photo Asset；
- Duplicate 判断；
- User Confirmation；
- V1 Processing 状态。

---

# 37. V2 不应该在 V1 处理过程中同步

例如：

```text
V1：
Photo A 正在 PROCESSING

V2：
发现 Photo A 对应的月份目录已经存在
```

V2 不应该立即 rsync。

必须等待：

```text
V1 Activity = 0
```

之后再执行。

这样保证同步时 V1 不再持续向该目录写入数据。

---

# 38. 同步前的数据稳定性

V2 实际开始 rsync 前：

```text
V1 Activity = 0
```

因此 V1 当前没有：

```text
UPLOAD
PROCESSING
```

等活动任务。

随后 V2 执行目录级 rsync。

如果新的 V1 上传任务在 Sync Phase 过程中产生，则这些新任务不得被错误地认为已经属于当前同步结果。

下一轮 Sync Worker 应再次扫描目录并建立新的 Sync Task。

因此：

> **V2 不假设一个月份目录永远不会再次发生变化。**

这也是 `photo_sync_tasks` 必须允许同一月份存在多条记录的根本原因。

---

# 39. 日志

V2 应记录至少以下日志：

### Worker 生命周期

```text
Worker started
Worker stopped
Worker idle
```

### NAS

```text
SSH port check started
SSH port check failed
NAS ready check started
NAS ready check failed
NAS ready
```

### V1 Activity

```text
V1 active
V1 idle
Waiting for V1 worker
```

### Sync Task

```text
Task created
Task started
Task completed
Task failed
Task retry
```

日志至少应包含：

```text
task_id
archive_month
status
```

方便定位具体月份。

### Cleanup

必须记录：

```text
archive_month
latest_task_id
latest_task_status
latest_success_completed_at
retention_days
delete_after
cleanup_result
```

如果拒绝删除，应记录具体原因。

例如：

```text
Cleanup skipped:
archive_month=202604
latest_task_status=FAILED
```

---

# 40. 同步统计

每次 Sync Task 应尽可能记录：

```text
file_count
total_size_bytes
duration_seconds
```

例如：

```text
archive_month = 202604
status = SUCCESS
started_at = 2026-08-10 09:30:00
completed_at = 2026-08-10 09:31:12
duration_seconds = 72
file_count = 135
total_size_bytes = 524288000
```

这些信息用于：

- 同步历史审计；
- 性能分析；
- 故障排查；
- NAS 同步进度观察。

---

# 41. V2 配置

V2 配置应至少包含以下内容：

```yaml
sync:
  enabled: true

  nas:
    host: "nas.example.local"
    ssh_port: 22
    ssh_user: "photo-sync"
    ssh_key: "/path/to/private/key"
    target_root: "/data/Photos"

  source:
    root: "/mnt/sda1/photo-gateway/Photos"

  worker:
    poll_interval_seconds: 30

  cleanup:
    enabled: true
    synced_retention_days: 90
```

具体字段名称以最终 Config Spec 为准。

---

# 42. SSH 安全要求

V2 使用专用 NAS 同步账户。

不建议：

```text
root
```

作为正常同步账户。

SSH 必须使用：

- 独立 SSH Key；
- Host Key 验证；
- 固定 NAS 主机；
- 明确 SSH 端口；
- 短连接超时。

禁止：

```text
StrictHostKeyChecking=no
```

等绕过 Host Key 验证的配置。

---

# 43. V2 Worker 异常处理

## 43.1 NAS SSH 不可用

```text
SSH Port Check
    ↓
失败
```

行为：

```text
不执行 Sync
不执行 Cleanup
等待下一轮
```

---

## 43.2 NAS Ready Check 失败

行为：

```text
不执行 Sync
不执行 Cleanup
等待下一轮
```

---

## 43.3 V1 忙碌

行为：

```text
允许创建/维护 PENDING Task
不执行 rsync
不执行 Cleanup
等待 V1 Activity = 0
```

---

## 43.4 rsync 失败

行为：

```text
Task = FAILED
记录 exit code
记录错误
保留 R5S 数据
不允许该月份进入 Cleanup
```

---

## 43.5 Worker 在 rsync 中异常退出

重新启动后应根据数据库 Task 状态和实际目录重新判断。

不能因为数据库中存在：

```text
RUNNING
```

就直接认为同步成功。

需要重新调度和确认该月份。

---

# 44. 数据安全原则

V2 的设计必须遵守：

> **宁可不删除，也不能误删除。**

所有 Cleanup 判断默认采用 Fail Safe：

```text
无法确认
    ↓
禁止删除
```

包括：

- 数据库查询失败；
- 无 Sync Task；
- 最新 Task 无法确定；
- 最新 Task 非 SUCCESS；
- 时间字段异常；
- NAS Ready Check 失败；
- Worker 状态异常；
- 配置异常。

---

# 45. V2 不做 NAS SHA-256

V2 明确不包含：

```text
NAS SHA-256 verification
```

原因：

1. R5S USB HDD 上执行额外内容校验成本较高。
2. rsync 已经负责正常的数据传输与校验。
3. V1 已经建立 Photo Asset 的 SHA-256 信息。
4. 日常同步没有必要重新扫描 NAS 全部照片计算 SHA-256。

未来如有需要，应设计独立的：

```text
NAS Integrity Audit
```

功能。

---

# 46. V2 不做全库单 Task 同步

以下设计不采用：

```text
Task:
Photos/
    ↓
NAS Photos/
```

作为唯一 Sync Task。

原因：

无法直接表达：

```text
哪个月份同步成功？
哪个月份失败？
哪个月份重试？
哪个月份已经满足 Cleanup 条件？
```

V2 使用：

```text
一个实际月份目录同步操作 = 一个 Sync Task
```

---

# 47. V2 不做逐文件 Sync Task

以下设计同样不采用：

```text
Photo A → Task
Photo B → Task
Photo C → Task
```

原因：

- 数据库记录数量会快速膨胀；
- rsync 本身已经负责文件级增量判断；
- 月份目录才是本项目实际的生命周期管理单位。

V2 的最小同步单位为：

```text
Photos/YYYY/YYYYMM/
```

---

# 48. 完整生命周期示例

假设：

```text
2026-08-10
```

客户端上传了一张：

```text
EXIF DateTimeOriginal:
2026-04-05 12:00:00
```

V1 将其归档：

```text
Photos/2026/202604/photo.jpg
```

---

## 第一次同步

V2：

```text
Task 1001
archive_month = 202604
status = PENDING
```

V1 空闲后：

```text
PENDING
  ↓
RUNNING
  ↓
rsync Photos/2026/202604/
  ↓
SUCCESS
```

记录：

```text
completed_at = 2026-08-10 10:00:00
```

---

## 后续又上传历史照片

2026-08-20 又上传：

```text
Photos/2026/202604/photo2.jpg
```

V2 创建：

```text
Task 1002
archive_month = 202604
status = PENDING
```

然后：

```text
Task 1002
RUNNING
  ↓
rsync
  ↓
FAILED
```

此时：

```text
202604 最新 Task = FAILED
```

即使 Task 1001 是 SUCCESS，也：

```text
禁止 Cleanup
```

---

## 再次同步成功

2026-08-21：

```text
Task 1003
archive_month = 202604
SUCCESS
completed_at = 2026-08-21 09:00:00
```

现在：

```text
202604 最新 Task = SUCCESS
```

retention 起点为：

```text
2026-08-21 09:00:00
```

不是 Task 1001 的：

```text
2026-08-10 10:00:00
```

如果：

```text
synced_retention_days = 90
```

则最早：

```text
2026-11-19 09:00:00
```

之后才允许进入删除判断。

---

# 49. Cleanup 示例

假设：

```text
archive_month = 202604

最新 Task：
status = SUCCESS
completed_at = 2026-06-05 12:00:00

synced_retention_days = 90
```

则：

```text
delete_after = 2026-09-03 12:00:00
```

当：

```text
current_time < 2026-09-03 12:00:00
```

禁止删除。

当：

```text
current_time >= 2026-09-03 12:00:00
```

并且：

```text
Sync Phase 已完成
不存在待同步任务
NAS Ready
```

才允许：

```text
删除：
Photos/2026/202604/
```

但：

```text
Photos/2026/
```

不得删除。

---

# 50. V2 状态与安全关系

可以将核心安全关系总结为：

```text
V1 Activity > 0
    → 不同步

NAS != READY
    → 不同步、不删除

存在待同步 Task
    → 不删除

最新月份 Task != SUCCESS
    → 不删除

最新 SUCCESS 未达到 retention
    → 不删除

以上全部满足
    → 才允许删除 YYYYMM
```

---

# 51. V2 与 NAS 的最终关系

V2 不是 NAS 的双向同步系统。

方向固定：

```text
R5S
 ↓
NAS
```

NAS 不反向同步到 R5S。

V2 不处理：

- NAS → R5S；
- NAS → Client；
- 双向冲突解决；
- NAS 文件修改同步回 R5S。

---

# 52. V2 数据生命周期总结

```text
Client
  ↓
V1
  ↓
Photos/YYYY/YYYYMM/
  ↓
发现月份
  ↓
PENDING Sync Task
  ↓
等待 V1 Activity = 0
  ↓
NAS Ready
  ↓
RUNNING
  ↓
rsync
  ├─ FAILED → 保留 R5S 数据
  │
  └─ SUCCESS
          ↓
     Sync Phase Complete
          ↓
       Cleanup
          ↓
     查询最新 Task
          ↓
     最新状态 SUCCESS
          ↓
     retention 到期
          ↓
       NAS Ready
          ↓
   删除 Photos/YYYY/YYYYMM/
```

---

# 53. V2 明确不包含的功能

以下内容不属于 V2 第一版范围：

1. NAS SHA-256 全量校验。
2. NAS → R5S 反向同步。
3. 双向同步。
4. NAS 文件冲突解决。
5. 每个照片独立 Sync Task。
6. 整个 `Photos/` 作为单一 Sync Task。
7. `rsync --delete`。
8. 自动删除 `YYYY` 年份目录。
9. 默认按天删除照片。
10. NAS Integrity Audit。
11. NAS RAID 管理。
12. NAS 快照管理。
13. 视频同步策略。
14. 云端同步。

---

# 54. V2 验收标准

V2 实现完成后，至少必须通过以下测试。

## 54.1 基础月份同步

```text
Photos/2026/202608/
```

能够通过 rsync 同步到：

```text
NAS/Photos/2026/202608/
```

并生成 SUCCESS Task。

---

## 54.2 历史月份重新上传

已有：

```text
202604 → SUCCESS
```

再次新增：

```text
Photos/2026/202604/new.jpg
```

必须生成新的 Sync Task。

不得覆盖原 Task。

---

## 54.3 同步失败

模拟 NAS 不可用或 rsync 失败：

```text
Task = FAILED
```

且：

```text
Photos/YYYY/YYYYMM/
```

不得被 Cleanup 删除。

---

## 54.4 成功后再次失败

测试：

```text
SUCCESS
↓
FAILED
```

最新状态必须为：

```text
FAILED
```

即使旧 SUCCESS 已超过 retention，也：

```text
禁止删除
```

---

## 54.5 再次成功

测试：

```text
SUCCESS
↓
FAILED
↓
SUCCESS
```

Cleanup retention 必须从**最新 SUCCESS 的 `completed_at`**重新计算。

---

## 54.6 无同步记录

存在：

```text
Photos/2026/202604/
```

但无 Sync Task：

```text
禁止删除
```

---

## 54.7 V1 正在上传

V1：

```text
UPLOADING
```

V2：

```text
不执行 rsync
```

---

## 54.8 V1 已上传但仍 Processing

V1：

```text
UPLOADED
```

或者 Worker：

```text
PROCESSING
```

V2：

```text
不执行 rsync
```

---

## 54.9 V1 完全空闲

只有：

```text
无 CREATED
无 UPLOADING
无 UPLOADED
无 PROCESSING
无 V1 pending/processing worker task
```

之后才允许实际 rsync。

---

## 54.10 Cleanup 前 NAS 掉线

即使：

```text
Sync SUCCESS
retention 已到期
```

但 Cleanup 前 NAS Ready Check 失败：

```text
禁止删除
```

---

## 54.11 不删除年份目录

删除：

```text
Photos/2026/202604/
```

后：

```text
Photos/2026/
```

必须保留。

---

## 54.12 禁止 NAS 被 R5S Cleanup 影响

R5S 删除：

```text
Photos/2026/202604/
```

之后：

```text
NAS/Photos/2026/202604/
```

必须继续存在。

---

# 55. 最终设计结论

V2 的核心模型最终确定为：

```text
R5S
  │
  │ Photos/YYYY/YYYYMM/
  │
  ▼
Sync Worker
  │
  ├── 等待 V1 Activity = 0
  ├── NAS SSH Port Check
  ├── NAS Ready Check
  │
  ▼
Sync Phase
  │
  ├── 一个 YYYYMM = 一个实际 Sync Task
  ├── 目录级 rsync
  ├── 每次同步独立记录
  └── 不使用 --delete
  │
  ▼
Sync Phase Complete
  │
  ├── 不存在待同步任务
  │
  ▼
Cleanup Phase
  │
  ├── 查询 YYYYMM 最新 Sync Task
  ├── 最新状态必须 SUCCESS
  ├── 使用最新 SUCCESS.completed_at
  ├── retention = synced_retention_days
  ├── 再次 NAS Ready Check
  │
  ▼
删除 R5S Photos/YYYY/YYYYMM/
```

其中最重要的三条不可违反的规则是：

> **第一：整个项目的照片目录统一为 `Photos/YYYY/YYYYMM/`。**

> **第二：`photo_sync_tasks` 是每一次实际同步操作的历史记录，同一个 `YYYYMM` 可以存在多条 Task，不能以月份作为唯一键。**

> **第三：Cleanup 判断一个月份能否删除时，必须先取得该月份最新的一条 Sync Task；只有最新状态为 `SUCCESS`，并且该次成功的 `completed_at` 已超过 `synced_retention_days`，同时 Sync Phase 已完成、没有待同步任务且 NAS Ready，才允许删除该 `YYYYMM` 目录。任何历史 SUCCESS 都不能绕过最新状态判断。**

---

# 56. V2 实现建议顺序

实现时建议严格按照以下顺序：

```text
1. V2 Config
       ↓
2. photo_sync_tasks DB Model
       ↓
3. NAS SSH / Ready Check
       ↓
4. V1 Activity Detection
       ↓
5. Sync Task Discovery
       ↓
6. Month-level rsync
       ↓
7. Sync Task State Management
       ↓
8. Sync Phase Completion Detection
       ↓
9. Cleanup Eligibility Detection
       ↓
10. YYYYMM Cleanup
       ↓
11. Worker System Service
       ↓
12. Logging / Metrics
       ↓
13. Historical Data Initial Sync
       ↓
14. Integration Tests
```

V2 的实现必须以本文档定义的状态和安全边界为基准，不应通过简化状态判断来绕过 Cleanup 安全条件。