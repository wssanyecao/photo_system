# Photo Gateway V2 技术规范书

**文档名称：** Photo Gateway V2 Specification  
**文档版本：** V2.2  
**文档状态：** Design Baseline  
**更新时间：** 2026-09-03  
**适用范围：** Photo Gateway V2  
**上游版本：** Photo Gateway V1

---

## 1. 文档目的

Photo Gateway V2 在 V1 的基础上增加 NAS 同步、同步任务管理、失败隔离以及 R5S 临时照片目录生命周期管理能力。

V2 的核心职责是将 R5S `Photos/YYYY/YYYYMM/` 中已经完成 V1 处理的正式照片安全同步至 NAS，并在满足同步成功、保留周期及异常隔离等条件的情况下，清理 R5S 上已经完成生命周期管理的历史照片目录。

V2 不负责照片上传、照片元数据解析或照片归档，上述功能由 V1 完成。

V2 不依赖 Immich 的内部存储机制。Immich 仅作为独立的照片管理系统，通过 External Library 读取 NAS 上的正式照片目录。

---

## 2. 设计原则

V2 必须遵循以下设计原则：

1. **V2 常驻运行。** V2 Worker 随 Photo Gateway 系统启动，在后台持续运行。
2. **NAS 是 V2 的核心前置条件。** 每一轮 V2 工作周期的第一项操作必须是 NAS Alive Check。
3. **V1 与 V2 串行处理。** V1 存在活动任务时，V2 不得执行 NAS Ready Check、Difference Check、Sync 或 Cleanup。
4. **NAS Ready 是正式处理条件。** NAS Alive 仅表示 NAS 的 SSH 服务可达；只有 NAS Ready Check 成功后，V2 才能进入正式处理阶段。
5. **Difference Check 是同步任务发现的唯一权威来源。** V2 不通过历史任务状态推测是否需要同步，而是通过实际 rsync 差异判断当前是否存在需要同步的数据。
6. **Sync Task 表示一次实际同步执行。** 同一个 `YYYYMM` 可以对应多个历史 Sync Task，不得使用 `YYYYMM` 作为 Sync Task 的唯一键。
7. **Batch 表示一次 V2 Cycle 中发现并计划执行的同步任务集合。**
8. **Month State 表示某个 `YYYYMM` 在某个时间点的运行状态，并以历史记录形式保存。** 同一个 `YYYYMM` 可以存在多条 Month State 记录。
9. **任何需要获取 YYYYMM 当前状态的业务逻辑，都必须获取该月份最新的一条 Month State 记录作为当前状态。**
10. **每次进入 ABNORMAL 都必须产生一条独立的 Month State 历史记录。** 历史异常记录不得被后续状态覆盖。
11. **同步失败必须局部隔离。** 单个月份同步失败不得影响其他月份的同步任务及 Cleanup 判断。
12. **Cleanup 是每轮正式处理阶段的固定步骤。** Cleanup 不以本轮 Batch 是否存在、是否全部 SUCCESS 作为前置条件。
13. **Cleanup 判断以月份为单位独立执行。** 某个月份是否允许删除，只取决于该月份自身的同步状态、当前 Month State 和保留周期。
14. **删除操作不得影响 NAS 上的数据。** V2 禁止使用可能删除 NAS 文件的 `rsync --delete`。
15. **历史任务和历史状态必须保留。** Sync Task 以及 Month State 均用于形成完整的执行和状态变化历史。

---

# 3. 系统角色

V2 涉及以下三个主要存储位置：

| 组件   | 角色          | 主要职责                 |
| ------ | ------------- | ------------------------ |
| Client | 数据来源      | 上传照片                 |
| R5S    | Photo Gateway | 接收、处理并暂存正式照片 |
| NAS    | 最终存储      | 保存正式照片             |

R5S 上的正式照片目录固定为：

```text
/mnt/sda1/photo-gateway/Photos/YYYY/YYYYMM/
```

NAS 上的目标目录保持与 R5S 相同的年月结构：

```text
<NAS_TARGET_ROOT>/YYYY/YYYYMM/
```

例如：

```text
R5S:
/mnt/sda1/photo-gateway/Photos/2026/202604/

NAS:
/data/Photos/2026/202604/
```

V2 不改变照片的归档目录结构。

---

# 4. V2 Worker 生命周期

V2 Worker 是 Photo Gateway 的后台常驻服务。

系统启动后，V2 Worker 应自动启动，并持续执行工作 Cycle。

每一个 Cycle 都必须按照以下顺序执行：

```text
V2 Worker 启动
        │
        ▼
NAS Alive Check
        │
        ├── FAIL ──────► 等待下一轮
        │
        ▼
V1 Activity Check
        │
        ├── BUSY ──────► 等待下一轮
        │
        ▼
NAS Ready Check
        │
        ├── FAIL ──────► 等待下一轮
        │
        ▼
正式处理阶段
        │
        ├── Difference Check
        │
        ├── 创建 Batch / Sync Tasks
        │
        ├── 执行 Sync Tasks
        │
        └── Cleanup Check
        │
        ▼
本轮结束
        │
        ▼
Sleep
        │
        ▼
下一轮重新从 NAS Alive Check 开始
```

## 4.1 Cycle 的定义

一个 Cycle 是从 NAS Alive Check 开始，到本轮正式处理完成并进入 Sleep 为止的一次完整 V2 工作周期。

下一轮 Cycle 不得继承上一轮的前置条件。

即使上一轮 NAS Alive、NAS Ready 均成功，下一轮仍必须重新执行 NAS Alive Check。

---

# 5. NAS Alive Check

NAS Alive Check 是每个 Cycle 的第一项操作。

在 NAS Alive Check 失败的情况下，V2 不得执行任何后续业务操作，包括：

- V1 Activity Check；
- NAS Ready Check；
- Difference Check；
- Sync Task 创建；
- Sync Task 执行；
- Cleanup。

NAS Alive Check 的目的仅是判断 NAS 当前是否具备基本网络及 SSH 可达性。

推荐使用 TCP/SSH 端口连通性检查。

Alive Check 成功仅表示 NAS 服务可达，不代表 NAS 已经满足正式同步条件。

---

# 6. V1 Activity Check

NAS Alive Check 成功后，V2 必须检查 V1 当前是否存在活动任务。

V1 以下 Session 状态属于活动状态：

```text
CREATED
UPLOADING
UPLOADED
PROCESSING
```

其中 `UPLOADED` 仍属于活动状态，因为 V1 的 `/complete` 操作可以将 Session 从 `UPLOADED` 推进至 `PROCESSING`。

除 Upload Session 外，V1 Worker 中处于 `PENDING` 或 `PROCESSING` 状态的处理任务同样属于活动任务。

只要存在任意 V1 活动任务，V2 必须等待下一轮，不得继续执行 NAS Ready Check、Difference Check、Sync 或 Cleanup。

V1 完全空闲后，V2 才能进入 NAS Ready Check。

---

# 7. NAS Ready Check

NAS Alive Check 成功且 V1 完全 Idle 后，V2 执行 NAS Ready Check。

NAS Ready Check 用于确认 NAS 不仅在线，而且具备实际同步条件。

NAS Ready Check 至少应验证：

| 检查项       | 要求                                       |
| ------------ | ------------------------------------------ |
| SSH          | 可以建立 SSH 会话                          |
| 目标文件系统 | 已正确挂载                                 |
| 文件系统状态 | 可正常访问                                 |
| 写权限       | Photo Gateway 使用的账号具有目标目录写权限 |
| 剩余空间     | 满足最低剩余空间要求                       |
| 目标根目录   | 存在或可以正常创建                         |
| rsync        | NAS 端 rsync 可正常执行                    |

NAS Ready Check 任一关键项目失败，则本轮结束，不得执行 Difference Check、Sync 或 Cleanup。

如果 NAS 在 Alive Check 与 Ready Check 之间发生故障，Ready Check 必须失败，本轮直接结束。

---

# 8. 正式处理阶段

只有以下三个条件全部满足后，本轮才进入正式处理阶段：

```text
NAS Alive = PASS
V1 Activity = IDLE
NAS Ready = PASS
```

三项条件构成 V2 正式处理阶段的统一安全前提。

进入正式处理阶段后，本轮依次执行：

1. Difference Check；
2. Batch / Sync Task 创建；
3. Sync Task 执行；
4. Cleanup Check。

其中 Cleanup 不依赖本轮是否创建了 Sync Task，也不依赖本轮 Sync Task 是否全部成功。

---

# 9. Difference Check

Difference Check 用于确定当前 R5S 上哪些 `YYYYMM` 目录存在尚未同步至 NAS 的内容。

Difference Check 必须以 R5S 当前实际文件状态与 NAS 当前实际文件状态为依据。

V2 不得仅根据历史 Sync Task 判断某个月份是否需要同步。

推荐使用 rsync 的 dry-run / comparison 能力执行差异检查。

例如：

```bash
rsync -rnt ...
```

实际参数由实现配置决定。

Difference Check 的结果应至少包含：

```text
archive_month
source_path
target_path
difference_detected
```

只有 `difference_detected = true` 的月份才能创建新的 Sync Task。

---

# 10. 已成功同步月份的重新同步

一个 `YYYYMM` 曾经成功同步，并不意味着该月份永久不再产生 Sync Task。

例如：

```text
2026/202604
```

在第一次同步完成后，如果未来又通过 V1 上传了该月份的历史照片：

```text
Photos/2026/202604/new-photo.jpg
```

Difference Check 检测到新的差异后，必须再次创建新的 Sync Task。

因此：

```text
archive_month
```

不得作为 `photo_sync_tasks` 的唯一键。

同一个 `YYYYMM` 可以存在多个历史 Sync Task，每条记录分别代表一次实际同步执行。

---

# 11. Month State

Month State 用于描述某个 `YYYYMM` 在特定时间点所处的运行状态。

与 Sync Task 不同，Month State 是**状态历史记录**，而不是一个只保存当前状态的单行对象。

同一个 `YYYYMM` 可以存在多条 Month State 记录。

例如：

```text
202604
NORMAL
ABNORMAL
NORMAL
ABNORMAL
```

上述记录分别代表该月份在不同时间发生的状态变化。

任何业务逻辑需要获取某个 `YYYYMM` 当前状态时，都必须按照该月份的记录创建时间或状态发生时间获取最新的一条记录，并以该记录的 `state` 作为当前状态。

因此：

```text
archive_month
```

**不得作为 `photo_sync_month_states` 的唯一键。**

Month State 推荐状态：

```text
NORMAL
ABNORMAL
RETRY_REQUESTED
```

其中：

- `NORMAL`：该月份当前未处于异常隔离状态，可以正常参与自动同步和 Cleanup 判断。
- `ABNORMAL`：该月份已达到连续失败隔离条件，禁止自动同步和 Cleanup。
- `RETRY_REQUESTED`：管理员已经通过 WebUI 请求恢复该月份的自动同步资格。

---

# 12. Month State 状态历史规则

Month State 必须采用追加式历史记录模型。

当 Month State 发生变化时，不得修改历史记录，而必须创建新的记录。

例如：

```text
202604
2026-08-01 10:00:00 → NORMAL
2026-08-02 10:00:00 → ABNORMAL
2026-08-03 09:00:00 → RETRY_REQUESTED
2026-08-03 10:30:00 → NORMAL
2026-08-20 14:00:00 → ABNORMAL
```

此时数据库中必须保留上述全部记录。

当前状态为：

```text
ABNORMAL
```

因为最新一条 Month State 记录为 `ABNORMAL`。

因此，后续业务不应通过修改历史 `ABNORMAL` 记录的方式表示恢复，而应新增一条新的状态记录。

---

# 13. Month State 的创建规则

以下情况必须创建新的 Month State 记录：

1. 系统首次为某个 `YYYYMM` 建立状态；
2. 月份达到连续失败阈值并进入 `ABNORMAL`；
3. 管理员执行 Retry；
4. Retry 后月份恢复为 `NORMAL`；
5. 其他导致 Month State 发生变化的明确状态转换。

如果某次 Sync Task 失败但尚未达到 ABNORMAL 条件，则不要求仅因为 Task 失败而创建新的 Month State 记录。

例如：

```text
Month State = NORMAL
Task = FAILED
consecutive_failed_count = 1
```

如果月份仍然属于正常自动处理状态，则 Month State 可以继续保持最新状态为 `NORMAL`。

---

# 14. 获取当前 Month State

任何需要判断某个 `YYYYMM` 当前状态的业务逻辑，都必须使用以下语义：

```text
查询 archive_month = YYYYMM
按照最新状态记录排序
获取第一条记录
将该记录作为当前 Month State
```

例如：

```text
SELECT *
FROM photo_sync_month_states
WHERE archive_month = '202604'
ORDER BY created_at DESC, id DESC
LIMIT 1;
```

实际 SQL 以最终数据库实现为准。

如果某个 `YYYYMM` 不存在任何 Month State 记录，则按照系统初始化规则视为 `NORMAL`，或者在首次发现该月份时创建一条 `NORMAL` 记录。实现必须统一采用其中一种方式，不得在不同业务流程中使用不同解释。

推荐在首次发现月份时创建：

```text
archive_month = 202604
state = NORMAL
```

这样数据库中始终具有明确的状态历史起点。

---

# 15. Sync Batch

Batch 表示某一个 Cycle 中 Difference Check 发现的待同步月份集合。

例如本轮 Difference Check 发现：

```text
202604
202606
202608
```

则创建一个 Batch：

```text
Batch #202609030001
```

并关联三个 Sync Task：

```text
Task #1 → 202604
Task #2 → 202606
Task #3 → 202608
```

Batch 创建后，其任务集合应保持不变。

本轮新增的文件不会自动追加到已经创建的 Batch。

如果下一轮 Difference Check 再次发现差异，则创建新的 Batch 和新的 Sync Task。

---

# 16. ABNORMAL 月份过滤

Difference Check 发现存在差异后，V2 必须获取该月份最新的 Month State 记录。

如果最新状态为：

```text
ABNORMAL
```

则该月份不得进入新的 Sync Batch。

如果最新状态为：

```text
RETRY_REQUESTED
```

则允许该月份重新进入同步流程。

如果最新状态为：

```text
NORMAL
```

则按照正常规则处理。

ABNORMAL 月份可以继续存在于 R5S `Photos/` 中，但必须保持自动处理隔离状态。

---

# 17. Sync Task

Sync Task 表示一次针对单个 `YYYYMM` 的实际同步执行。

Sync Task 状态：

```text
PENDING
RUNNING
SUCCESS
FAILED
```

推荐状态转换：

```text
PENDING
   │
   ▼
RUNNING
   │
   ├── SUCCESS
   │
   └── FAILED
```

每执行一次实际同步，都必须创建独立的 Sync Task。

不得通过修改旧 Task 的状态来表示重试。

例如：

```text
第一次执行：
Task #101 → FAILED

第二次执行：
Task #102 → SUCCESS
```

而不是：

```text
Task #101：
FAILED → RUNNING → SUCCESS
```

因此 `photo_sync_tasks` 不需要 `attempt_count` 作为重试次数语义。

---

# 18. Sync Task 执行规则

Batch 中的 Sync Task 按顺序执行。

单个 Task 执行时：

1. 状态由 `PENDING` 修改为 `RUNNING`；
2. 执行对应 `YYYYMM` 目录的 rsync；
3. 记录命令、开始时间、结束时间、退出码及错误信息；
4. rsync 成功则状态修改为 `SUCCESS`；
5. rsync 失败或发生未捕获异常则状态修改为 `FAILED`。

单个 Task 失败后，不得删除或修改 NAS 上已经存在的数据。

同时，该失败只影响当前 `YYYYMM` 的同步结果，不得直接将其他月份标记为失败。

---

# 19. Rsync 安全要求

V2 使用 R5S → NAS 的单向同步模型。

方向固定为：

```text
R5S → NAS
```

V2 禁止使用：

```bash
rsync --delete
```

或任何可能根据 R5S 状态删除 NAS 文件的等效操作。

R5S 上文件的删除只由 V2 Cleanup 阶段执行。

NAS 是正式照片存储位置，因此同步过程中不得因为 R5S 文件缺失而删除 NAS 上的对应文件。

---

# 20. 同步成功判定

V2 正常同步不要求每次都对 NAS 上的全部照片重新执行 SHA-256 校验。

Sync Task 的成功判定主要依据 rsync 的执行结果。

至少应记录：

- rsync exit code；
- stdout；
- stderr；
- 开始时间；
- 完成时间；
- 同步源目录；
- 同步目标目录；
- 同步结果。

正常情况下，rsync exit code 为 `0` 时，Task 标记为 `SUCCESS`。

如果 rsync 返回非零退出码，则 Task 标记为 `FAILED`。

---

# 21. 连续失败与 ABNORMAL

V2 必须对单个 `YYYYMM` 的连续同步失败进行隔离。

默认检查最近：

```text
3
```

个实际 Sync Task。

该数量应可配置。

例如某月份历史执行记录为：

```text
SUCCESS
FAILED
SUCCESS
FAILED
FAILED
FAILED
```

则只检查最近三个实际执行：

```text
FAILED
FAILED
FAILED
```

因此该月份进入：

```text
ABNORMAL
```

历史累计失败次数不参与 ABNORMAL 判断。

---

# 22. ABNORMAL 状态的产生

当某个 `YYYYMM` 达到连续失败阈值时：

1. 当前 Sync Task 保持 `FAILED`；
2. 计算该月份最近 N 个实际 Sync Task；
3. 如果最近 N 个 Task 全部为 `FAILED`，则该月份进入 `ABNORMAL`；
4. 创建一条新的 Month State 历史记录；
5. 新记录的 `state` 为 `ABNORMAL`；
6. 记录异常发生时间、连续失败次数及异常原因。

例如：

```text
Task #101 → FAILED
Task #102 → FAILED
Task #103 → FAILED
```

达到阈值后新增：

```text
Month State #55
archive_month = 202608
state = ABNORMAL
consecutive_failed_count = 3
```

历史 Month State 记录不得被覆盖。

---

# 23. ABNORMAL 状态的意义

ABNORMAL 表示：

> 当前 `YYYYMM` 已达到连续失败隔离阈值，V2 自动同步流程不得继续处理该月份，必须等待人工确认。

进入 ABNORMAL 后：

- 不得自动创建新的 Sync Task；
- 不得加入新的 Sync Batch；
- 不得执行 Cleanup 删除；
- 必须保留 R5S 上的原始目录；
- 历史 Sync Task 必须保留；
- 历史 Month State 必须保留；
- WebUI 应明确显示异常原因及最近失败记录。

ABNORMAL 是当前月份的运行控制状态，而不是历史同步结果。

---

# 24. WebUI Retry

对于 ABNORMAL 月份，WebUI 必须提供人工 Retry 操作。

管理员执行 Retry 后，不得直接修改原有 ABNORMAL 记录。

系统必须新增一条 Month State 记录：

```text
ABNORMAL → RETRY_REQUESTED
```

该记录表示管理员已经明确允许该月份重新进入同步流程。

下一轮 Difference Check 时，如果发现该月份仍然存在差异，则允许其进入新的 Sync Batch。

当新的 Sync Task 开始执行时，该月份已经重新获得自动同步资格。

---

# 25. Retry 成功

如果 Retry 后：

```text
Task #104 → SUCCESS
```

则必须新增一条 Month State 记录：

```text
state = NORMAL
```

因此状态历史可能为：

```text
NORMAL
ABNORMAL
RETRY_REQUESTED
NORMAL
```

此时最新 Month State 为 `NORMAL`，该月份恢复正常自动同步和 Cleanup 生命周期。

---

# 26. Retry 再次失败

如果 Retry 后新的 Sync Task 失败，则继续按照最近 N 个实际 Sync Task 判断连续失败情况。

例如：

```text
Task #101 → FAILED
Task #102 → FAILED
Task #103 → FAILED
→ ABNORMAL

管理员 Retry

Task #104 → FAILED
```

此时应根据系统定义的“连续失败计数重置规则”计算新的连续失败次数。

推荐规则为：

> 一次人工 Retry 表示一次新的故障处理周期。Retry 请求后开始的新同步执行，应重新计算该故障周期的连续失败次数。

因此：

```text
Task #104 → FAILED
consecutive_failed_count = 1
Month State = NORMAL
```

只有 Retry 后重新连续失败达到阈值，才再次创建新的：

```text
ABNORMAL
```

Month State 历史记录。

这样可以明确区分不同故障周期，避免历史故障与人工修复后的新故障混合计算。

---

# 27. Cleanup

Cleanup 是每一个满足以下三个前置条件的 V2 Cycle 的固定处理阶段：

```text
NAS Alive = PASS
V1 Activity = IDLE
NAS Ready = PASS
```

Cleanup 不要求本轮必须存在 Sync Batch。

Cleanup 不要求本轮必须存在 Sync Task。

Cleanup 不要求本轮 Sync Task 全部 SUCCESS。

Cleanup 也不因为其他 `YYYYMM` 的 Sync Task 失败而整体跳过。

因此，即使某一轮没有任何新增照片：

```text
Difference Check
    ↓
无差异
    ↓
无 Sync Task
    ↓
Cleanup Check
```

仍然必须执行 Cleanup。

这样可以确保长期没有新增照片的历史月份仍然能够按照保留策略最终被清理。

---

# 28. Cleanup 检查范围

Cleanup 必须扫描 R5S 正式照片目录下的所有年月目录：

```text
Photos/YYYY/YYYYMM/
```

Cleanup 不仅检查本轮 Sync Batch 中的月份。

例如：

```text
本轮 Batch：
202608

R5S 当前存在：
202604
202605
202606
202607
202608
```

Cleanup 必须检查上述所有月份。

---

# 29. Cleanup 当前 Month State 判断

Cleanup 检查某个 `YYYYMM` 时，必须首先获取该月份最新的一条 Month State 记录。

如果最新状态为：

```text
ABNORMAL
```

则该月份不得删除。

如果最新状态为：

```text
NORMAL
```

则继续进行删除资格判断。

如果最新状态为：

```text
RETRY_REQUESTED
```

则视为该月份当前处于恢复处理阶段，不得删除。

因此，只有当前 Month State 为 `NORMAL` 时，月份才具备进入后续 Cleanup 判断的资格。

---

# 30. Cleanup 删除条件

一个 `YYYYMM` 只有同时满足以下全部条件时，才允许删除：

### 30.1 当前 Month State 为 NORMAL

当前 Month State 必须为：

```text
NORMAL
```

任何：

```text
ABNORMAL
RETRY_REQUESTED
```

状态均不得删除。

---

### 30.2 存在历史 Sync Task

如果该月份从未执行过同步，则不得删除。

即：

```text
无 Sync Task → 保留
```

---

### 30.3 最近一次 Sync Task 必须 SUCCESS

获取该月份最新的一条 Sync Task。

如果：

```text
latest_task.status != SUCCESS
```

则保留。

例如：

```text
Task #101 → SUCCESS
Task #102 → FAILED
```

最新 Task 为 FAILED，因此不得删除。

历史上曾经 SUCCESS 不能覆盖最近一次 FAILED。

---

### 30.4 最新成功同步时间达到保留周期

对于该月份最新一次 `SUCCESS` Sync Task，使用其完成时间作为该月份最近一次成功同步时间。

如果：

```text
current_time - latest_success.completed_at
    >= synced_retention_days
```

则满足保留周期条件。

否则继续保留。

---

# 31. Cleanup 判断示例

假设：

```text
synced_retention_days = 90
```

当前日期为：

```text
2026-09-03
```

某月份：

```text
202604

Latest Sync Task:
SUCCESS

completed_at:
2026-05-01 10:00:00

Latest Month State:
NORMAL
```

由于最新成功同步已经超过 90 天，并且该月份没有异常状态，因此允许删除：

```text
Photos/2026/202604/
```

而：

```text
202608

Latest Sync Task:
SUCCESS

completed_at:
2026-08-25 10:00:00

Latest Month State:
NORMAL
```

虽然同步成功，但未达到 90 天，因此保留。

如果：

```text
202607

Latest Sync Task:
FAILED

Latest Month State:
NORMAL
```

即使历史上曾经存在 SUCCESS，也不得删除，因为最近一次同步失败。

---

# 32. Cleanup 与同步失败的关系

同步失败不会导致整个 Cleanup 阶段跳过。

例如本轮：

```text
202604 → SUCCESS
202605 → FAILED
202606 → SUCCESS
```

Cleanup 仍然检查所有月份。

对于：

```text
202604
```

如果满足删除条件，则可以删除。

对于：

```text
202605
```

由于最新 Task 为 FAILED，因此不得删除。

对于：

```text
202606
```

如果满足删除条件，则可以删除。

因此 Sync Task 的失败只影响对应月份，不影响其他月份的 Cleanup。

---

# 33. Cleanup 与 NAS 检查的关系

本轮正式处理阶段开始前已经完成：

```text
NAS Alive Check
NAS Ready Check
```

只要这两个检查均成功，本轮即可进入正式处理阶段。

Cleanup 阶段不再重复执行 NAS Alive Check 或 NAS Ready Check。

Cleanup 的删除对象是 R5S 本地：

```text
Photos/YYYY/YYYYMM/
```

删除操作不会修改 NAS 数据。

因此无需在 Cleanup 前再次进行 NAS 状态检查。

---

# 34. Cleanup 删除操作

删除必须以 `YYYYMM` 目录为最小操作单位。

例如：

```text
Photos/2026/202604/
```

满足全部删除条件后，删除整个：

```text
202604/
```

目录。

删除操作必须具备以下保护：

1. 删除目标必须位于配置的 Photo Root 下；
2. 删除目标必须符合 `YYYY/YYYYMM` 目录结构；
3. 不允许删除 Photo Root 本身；
4. 不允许删除年份目录；
5. 不允许删除结构之外的任意目录；
6. 删除操作必须记录审计日志；
7. 删除失败必须记录错误，但不得影响其他月份。

---

# 35. V2 数据模型

V2 使用三个核心数据对象：

```text
photo_sync_batches
photo_sync_tasks
photo_sync_month_states
```

其职责分别为：

| 数据对象    | 职责                                      |
| ----------- | ----------------------------------------- |
| Batch       | 记录一次 Cycle 中发现并计划执行的任务集合 |
| Sync Task   | 记录一次具体 YYYYMM 同步执行              |
| Month State | 记录 YYYYMM 的状态变化历史                |

三者必须保持语义分离。

---

# 36. photo_sync_batches

建议字段：

| 字段          | 类型     | 说明           |
| ------------- | -------- | -------------- |
| id            | INTEGER  | Batch 主键     |
| started_at    | DATETIME | Batch 创建时间 |
| completed_at  | DATETIME | Batch 完成时间 |
| status        | TEXT     | Batch 状态     |
| task_count    | INTEGER  | Task 数量      |
| success_count | INTEGER  | 成功数量       |
| failed_count  | INTEGER  | 失败数量       |
| created_at    | DATETIME | 创建时间       |
| updated_at    | DATETIME | 更新时间       |

Batch 仅用于描述一次同步批次及其结果，不承担月份异常隔离功能。

---

# 37. photo_sync_tasks

建议字段：

| 字段          | 类型     | 说明                           |
| ------------- | -------- | ------------------------------ |
| id            | INTEGER  | Task 主键                      |
| batch_id      | INTEGER  | 所属 Batch                     |
| archive_month | TEXT     | `YYYYMM`                       |
| source_path   | TEXT     | R5S 源目录                     |
| target_path   | TEXT     | NAS 目标目录                   |
| status        | TEXT     | PENDING/RUNNING/SUCCESS/FAILED |
| started_at    | DATETIME | 开始时间                       |
| completed_at  | DATETIME | 完成时间                       |
| exit_code     | INTEGER  | rsync exit code                |
| stdout        | TEXT     | rsync 输出                     |
| stderr        | TEXT     | rsync 错误输出                 |
| error_message | TEXT     | 异常信息                       |
| created_at    | DATETIME | 创建时间                       |
| updated_at    | DATETIME | 更新时间                       |

关键约束：

```text
archive_month 不得 UNIQUE
```

因为同一个月份允许存在多个历史 Sync Task。

---

# 38. photo_sync_month_states

`photo_sync_month_states` 是 Month State 历史表。

该表记录每个 `YYYYMM` 的状态变化历史，不保存一个永久覆盖的“当前状态”。

建议字段：

| 字段                     | 类型     | 说明                            |
| ------------------------ | -------- | ------------------------------- |
| id                       | INTEGER  | 状态记录主键                    |
| archive_month            | TEXT     | `YYYYMM`                        |
| state                    | TEXT     | NORMAL/ABNORMAL/RETRY_REQUESTED |
| consecutive_failed_count | INTEGER  | 当前连续失败次数                |
| abnormal_at              | DATETIME | 本次进入 ABNORMAL 的时间        |
| retry_requested_at       | DATETIME | 本次 Retry 请求时间             |
| retry_requested_by       | TEXT     | 执行 Retry 的用户               |
| reason                   | TEXT     | 本次状态记录的原因              |
| created_at               | DATETIME | 状态记录创建时间                |
| updated_at               | DATETIME | 状态记录更新时间                |

关键约束：

```text
archive_month 不得 UNIQUE
```

同一个 `YYYYMM` 可以存在多条历史记录。

例如：

```text
id  archive_month  state
1   202604         NORMAL
2   202604         ABNORMAL
3   202604         RETRY_REQUESTED
4   202604         NORMAL
5   202604         ABNORMAL
```

当前状态通过：

```text
archive_month = 202604
ORDER BY created_at DESC, id DESC
LIMIT 1
```

获得。

因此：

```text
当前状态 = 最新一条 Month State 记录
```

而不是：

```text
当前状态 = 某一条固定数据库记录
```

---

# 39. Month State 历史完整性

Month State 采用追加式记录模型后，任何历史状态记录都不得因为后续恢复而被修改或删除。

例如：

```text
202604
    ↓
NORMAL
    ↓
ABNORMAL
    ↓
RETRY_REQUESTED
    ↓
NORMAL
    ↓
ABNORMAL
```

数据库必须保留完整历史。

这样可以回答以下问题：

- 该月份曾经发生过几次异常？
- 每次异常发生在什么时候？
- 每次异常持续多久？
- 管理员何时执行了 Retry？
- Retry 后是否恢复正常？
- 后续是否再次发生异常？

历史 Month State 与历史 Sync Task 一样，都属于系统审计数据。

---

# 40. 时间规范

V2 所有数据库时间字段统一使用上海时区语义。

数据库时间格式统一为：

```text
YYYY-MM-DD HH:MM:SS
```

例如：

```text
2026-09-03 20:15:30
```

V2 不使用 UTC ISO-8601 字符串作为数据库业务时间字段。

---

# 41. V2 配置

建议配置结构：

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
    cycle_interval_seconds: 300

  failure:
    threshold: 3

  cleanup:
    enabled: true
    synced_retention_days: 90
```

其中：

| 配置                            | 说明                      |
| ------------------------------- | ------------------------- |
| `sync.enabled`                  | 是否启用 V2               |
| `nas.host`                      | NAS 地址                  |
| `nas.ssh_port`                  | SSH 端口                  |
| `nas.ssh_user`                  | NAS 同步账号              |
| `nas.ssh_key`                   | SSH 私钥                  |
| `nas.target_root`               | NAS Photo Root            |
| `source.root`                   | R5S Photo Root            |
| `worker.cycle_interval_seconds` | Cycle 间隔                |
| `failure.threshold`             | 连续失败阈值              |
| `cleanup.enabled`               | 是否启用 Cleanup          |
| `cleanup.synced_retention_days` | 同步成功后的 R5S 保留时间 |

---

# 42. 日志要求

V2 必须记录完整的生命周期日志。

至少包括：

- Worker 启动；
- Cycle 开始；
- NAS Alive Check；
- V1 Activity Check；
- NAS Ready Check；
- Difference Check；
- Batch 创建；
- Sync Task 创建；
- Sync Task 开始；
- rsync 命令执行结果；
- Sync Task SUCCESS / FAILED；
- 连续失败计数；
- Month State 状态变化；
- ABNORMAL；
- WebUI Retry；
- Cleanup Check；
- Cleanup 删除；
- Cleanup 删除失败；
- Cycle 完成。

日志中的路径、任务 ID、Batch ID、archive_month 应保持可关联。

---

# 43. WebUI 要求

WebUI 至少应能够查看以下内容。

## 43.1 V2 Worker 状态

显示：

```text
Worker Status
Current Cycle
Last Cycle
Next Cycle
NAS Alive
V1 Activity
NAS Ready
```

## 43.2 Sync Batch

显示：

```text
Batch ID
Created At
Completed At
Total Tasks
Success
Failed
Status
```

## 43.3 Sync Task

支持按照以下字段查询历史任务：

```text
archive_month
status
created_at
batch_id
```

## 43.4 Month State

Month State 页面应展示状态历史，而不仅仅是当前状态。

至少应显示：

```text
archive_month
current state
consecutive_failed_count
latest task
latest success
abnormal_at
```

同时应能够查看该月份完整的 Month State 历史，例如：

```text
NORMAL
ABNORMAL
RETRY_REQUESTED
NORMAL
ABNORMAL
```

对于当前状态为 ABNORMAL 的月份必须提供 Retry 操作。

## 43.5 Cleanup

显示：

```text
archive_month
latest sync status
latest successful sync time
current month state
retention status
cleanup eligibility
```

---

# 44. 异常处理

V2 必须保证单个异常不会导致 Worker 进程退出。

典型异常包括：

- NAS 不可达；
- SSH 连接失败；
- NAS Ready Check 失败；
- rsync 返回非零状态；
- rsync 执行异常；
- SQLite 操作失败；
- Cleanup 删除失败；
- 单个月份目录损坏或路径异常。

异常必须：

1. 记录日志；
2. 更新对应任务或状态；
3. 保持 Worker 继续运行；
4. 在下一轮重新从 NAS Alive Check 开始。

---

# 45. 完整 Cycle 规则

V2 每轮的完整逻辑定义如下：

```text
Cycle Start
    │
    ▼
NAS Alive Check
    │
    ├── FAIL
    │      └── Sleep → Next Cycle
    │
    ▼
V1 Activity Check
    │
    ├── BUSY
    │      └── Sleep → Next Cycle
    │
    ▼
NAS Ready Check
    │
    ├── FAIL
    │      └── Sleep → Next Cycle
    │
    ▼
Difference Check
    │
    ▼
获取各 YYYYMM 最新 Month State
    │
    ▼
过滤 ABNORMAL 月份
    │
    ▼
创建 Batch
    │
    ▼
创建 Sync Tasks
    │
    ▼
顺序执行 Sync Tasks
    │
    ├── SUCCESS
    └── FAILED
    │
    ▼
更新 Month State
    │
    ▼
Cleanup Check
    │
    ├── 遍历全部 YYYYMM
    ├── 获取最新 Month State
    ├── 检查 Latest Sync Task
    ├── 检查 Latest SUCCESS
    ├── 检查 Retention
    └── 删除符合条件的月份
    │
    ▼
Cycle Complete
    │
    ▼
Sleep
    │
    ▼
Next Cycle
```

其中：

> **Cleanup 是每轮正式处理阶段的组成部分，而不是 Sync Batch 的成功后置动作。**

---

# 46. 典型场景

## 46.1 正常同步

```text
NAS Alive       PASS
V1              IDLE
NAS Ready       PASS

Difference:
202608 有差异

Sync:
202608 → SUCCESS

Month State:
NORMAL

Cleanup:
正常执行
```

---

## 46.2 本轮没有新增照片

```text
NAS Alive       PASS
V1              IDLE
NAS Ready       PASS

Difference:
无差异

Sync:
无 Task

Cleanup:
正常执行
```

历史月份仍然会被检查，因此不会因为本轮没有同步任务而阻止历史照片清理。

---

## 46.3 某月份同步失败

本轮：

```text
202604 → SUCCESS
202605 → FAILED
202606 → SUCCESS
```

Cleanup 仍然执行。

```text
202604 → 如果满足删除条件 → 删除
202605 → Latest Task FAILED → 保留
202606 → 如果满足删除条件 → 删除
```

单个月份失败不会阻止其他月份 Cleanup。

---

## 46.4 月份进入 ABNORMAL

假设阈值为 3：

```text
202608:

Task #101 → FAILED
Task #102 → FAILED
Task #103 → FAILED
```

新增：

```text
Month State #55
202608 → ABNORMAL
```

下一轮：

```text
202608
```

不得自动加入 Sync Batch。

同时：

```text
202608
```

不得 Cleanup。

---

## 46.5 ABNORMAL 人工恢复后再次异常

假设历史状态：

```text
202608:

NORMAL
ABNORMAL
RETRY_REQUESTED
NORMAL
```

表示第一次故障已经通过人工处理并恢复。

之后再次发生连续失败：

```text
Task #201 → FAILED
Task #202 → FAILED
Task #203 → FAILED
```

系统必须新增：

```text
Month State:
ABNORMAL
```

而不是修改之前的 ABNORMAL 记录。

最终历史为：

```text
NORMAL
ABNORMAL
RETRY_REQUESTED
NORMAL
ABNORMAL
```

当前状态由最新记录确定：

```text
ABNORMAL
```

这样可以完整保留两次独立异常事件。

---

## 46.6 ABNORMAL 人工恢复

管理员在 WebUI 对 `202608` 执行 Retry：

```text
ABNORMAL
    ↓
RETRY_REQUESTED
```

下一轮 Difference Check 发现差异后：

```text
创建新的 Sync Task
```

如果：

```text
Task #204 → SUCCESS
```

则新增：

```text
Month State:
NORMAL
```

最终：

```text
ABNORMAL
RETRY_REQUESTED
NORMAL
```

最新状态为 `NORMAL`，该月份恢复正常自动同步和 Cleanup 生命周期。

---

## 46.7 已成功同步的历史月份再次产生新照片

```text
202604
Task #101 → SUCCESS
```

数月后 V1 又产生：

```text
Photos/2026/202604/new-photo.jpg
```

Difference Check 发现新差异：

```text
创建 Task #205
```

因此：

```text
Task #101 → SUCCESS
Task #205 → SUCCESS
```

均作为独立历史记录保留。

Cleanup 使用该月份**最新一次成功同步的完成时间**重新计算保留周期。

---

# 47. 最终架构定义

V2 的核心模型为：

```text
                    ┌─────────────────────┐
                    │     V2 Worker       │
                    │    Background       │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   NAS Alive Check   │
                    └──────────┬──────────┘
                               │ PASS
                               ▼
                    ┌─────────────────────┐
                    │  V1 Activity Check  │
                    └──────────┬──────────┘
                               │ IDLE
                               ▼
                    ┌─────────────────────┐
                    │   NAS Ready Check   │
                    └──────────┬──────────┘
                               │ PASS
                               ▼
                    ┌─────────────────────┐
                    │  Difference Check   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Latest Month State │
                    │      Filter         │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │        Batch        │
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
             Sync Task #1          Sync Task #N
                    │                     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   Month State       │
                    │   History Update   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   Cleanup Check     │
                    │   ALL YYYYMM        │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │    Cycle Complete   │
                    └──────────┬──────────┘
                               │
                               ▼
                             Sleep
                               │
                               ▼
                       Next Cycle
```

V2 最终形成以下三个相互独立但相互关联的核心数据语义：

```text
Batch
  │
  └── 描述“一轮同步发现了哪些任务”

Sync Task
  │
  └── 描述“某个 YYYYMM 的一次实际同步执行结果”

Month State History
  │
  └── 描述“某个 YYYYMM 在不同时间发生过哪些状态变化”
```

其中当前 Month State 并不是一个固定数据库记录，而是：

```text
当前 Month State
=
该 archive_month 最新的一条 Month State History 记录
```

---

# 48. V2 与 V1 的边界

V1 负责：

```text
Client
  ↓
Upload
  ↓
Precheck
  ↓
SHA-256
  ↓
EXIF
  ↓
Archive
  ↓
Photos/YYYY/YYYYMM/
```

V2 负责：

```text
Photos/YYYY/YYYYMM/
  ↓
NAS Alive
  ↓
V1 Idle
  ↓
NAS Ready
  ↓
Difference Check
  ↓
Rsync
  ↓
Sync Task History
  ↓
Month State History
  ↓
Cleanup Lifecycle
```

V1 与 V2 之间通过正式照片目录及 V1 数据库状态进行协作，但两者职责保持独立。

---

# 49. 版本结论

本 V2.2 Design Baseline 确定以下核心设计：

- V2 Worker 系统启动后后台常驻运行；
- 每个 Cycle 从 NAS Alive Check 开始；
- NAS Alive、V1 Idle、NAS Ready 是进入正式处理阶段的三个必要条件；
- Difference Check 决定本轮真正需要同步的月份；
- Batch 表示本轮发现的同步任务集合；
- Sync Task 表示一次实际同步执行；
- 同一 `YYYYMM` 可以拥有多个历史 Sync Task；
- Month State 采用追加式历史记录模型；
- 同一个 `YYYYMM` 可以拥有多条 Month State 记录；
- `archive_month` 不得作为 `photo_sync_month_states` 的唯一键；
- 任何业务需要获取当前 Month State 时，必须使用该月份最新的一条记录；
- 每次进入 ABNORMAL 都必须创建独立的历史记录；
- 人工 Retry 和恢复 NORMAL 同样通过新增状态记录实现，不修改历史记录；
- 连续 N 次失败后进入 ABNORMAL；
- ABNORMAL 必须人工 Retry 后才能恢复自动同步资格；
- 同步失败只影响对应月份，不影响其他月份；
- Cleanup 在每个满足 NAS Alive、V1 Idle、NAS Ready 三项前置条件的 Cycle 中执行；
- Cleanup 遍历全部历史月份，而不是仅处理当前 Batch；
- Cleanup 不要求本轮存在 Batch，也不要求本轮任务全部 SUCCESS；
- 某月份只有在当前 Month State 为 NORMAL、最新 Sync Task 为 SUCCESS 且达到保留周期时才能删除；
- 某个月份的同步失败不得阻止其他月份 Cleanup；
- 删除前不重复检查 NAS；
- R5S Cleanup 不得通过 `rsync --delete` 影响 NAS；
- 所有 Sync Task 历史和 Month State 历史必须长期保留。

该模型将**同步调度、同步执行历史、月份状态历史、异常隔离和本地生命周期管理**明确分离，可作为 Photo Gateway V2 的正式实现基线。