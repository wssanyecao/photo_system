# Photo Gateway 数据库字段字典（中文）

> 用途：给运维/开发提供各表字段的中文含义速查。
> 权威来源：`docs/photo-gateway-v1-db-spec.md`、`docs/photo-gateway-v2-spec.md`、实际 schema（`photo_gateway/db.py`）。
> SQLite 不原生支持列注释，故以本文档作为字段描述的唯一入口（避免把中文注释散落多处造成漂移）。

时间约定：除单独说明外，均为上海时区墙钟文本 `YYYY-MM-DD HH:MM:SS`；`date_taken` 除外（ISO `YYYY-MM-DDTHH:MM:SS`）。

---

## V1 表

### devices — 已注册照片来源设备
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | TEXT PK | 设备 ID | 稳定标识，来自配置 devices[].id，禁止用户自填 |
| name | TEXT | 设备名 | 展示名 |
| enabled | INTEGER | 是否启用 | 1=允许上传，0=禁用；配置删除设备时置 0 保留历史 |
| created_at / updated_at | TEXT | 创建/更新时间 | |

### upload_sessions — 一次上传会话
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | TEXT PK | 会话 ID | UUID |
| source_device | TEXT | 来源设备 | FK→devices.id |
| client_ip | TEXT | 客户端 IP | |
| created_at | TEXT | 创建时间 | |
| started_at / completed_at | TEXT | 开始/完成时间 | 完成=进入终态(COMPLETED/PARTIAL/FAILED)时 |
| total_files | INTEGER | 总文件数 | items 数 |
| uploaded_files | INTEGER | 已上传数 | status ∈ UPLOADED/PROCESSING/COMPLETED/DUPLICATE |
| skipped_files | INTEGER | 跳过数 | 仅统计用户明确跳过(user_confirmation=2)，不是全部疑似重复 |
| duplicate_files | INTEGER | 重复数 | 最终判定为重复的 item 数 |
| failed_files | INTEGER | 失败数 | status=FAILED 的 item 数 |
| status | TEXT | 会话状态 | CREATED/UPLOADING/UPLOADED/PROCESSING/COMPLETED/PARTIAL/FAILED/CANCELLED |

### upload_items — 会话内单个文件
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER PK | 条目 ID | 自增 |
| session_id | TEXT | 会话 | FK→upload_sessions.id，(session_id, client_item_id) 唯一 |
| source_device | TEXT | 来源设备 | FK→devices.id |
| client_item_id | TEXT | 客户端幂等键 | 客户端生成，重试不重复建行 |
| original_filename | TEXT | 原始文件名 | 用户文件原名（含扩展名/大小写） |
| file_size | INTEGER | 文件大小(字节) | Precheck 与真实上传大小需一致 |
| status | TEXT | 条目状态 | PRECHECK/UPLOADING/UPLOADED/PROCESSING/COMPLETED/DUPLICATE/FAILED |
| precheck_status | TEXT | 预检结果 | NOT_CHECKED/NO_MATCH/POSSIBLE_DUPLICATE |
| user_confirmation | INTEGER | 用户确认 | 0=未定 1=重新上传 2=明确跳过(仅用于疑似重复) |
| asset_id | INTEGER | 对应照片资产 | FK→photo_assets.id；DUPLICATE=指已有资产，COMPLETED=新资产 |
| error_code / error_message | TEXT | 错误码/信息 | 失败原因 |
| created_at/uploaded_at/processing_started_at/completed_at/updated_at | TEXT | 生命周期时间 | |

### photo_assets — 照片库资产
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER PK | 资产 ID | 自增 |
| sha256 | TEXT | 内容哈希 | 全局唯一索引（去重依据） |
| original_filename | TEXT | 原始文件名 | 用户原文件名（持久保存） |
| current_filename | TEXT | 实际文件名 | 归档后实际落盘名（冲突时 _1/_2 递增） |
| file_size | INTEGER | 文件大小(字节) | |
| mime_type | TEXT | MIME | 如 image/jpeg |
| date_taken | TEXT | 拍摄时间 | ISO，用于决定归档月份 |
| date_source | TEXT | 日期来源 | DateTimeOriginal/CreateDate/ModifyDate/file_mtime |
| exif_json | TEXT | EXIF 原文 JSON | 保护原始 EXIF |
| file_missing_at | TEXT | 本地缺失标记 | Cleanup 删除/外部删除时置位（时间戳）；NULL=本地文件在。记录不删（sha256 去重身份），照片页据此过滤 |
| archive_path | TEXT | 归档路径 | `Photos/YYYY/YYYYMM/<current_filename>`（相对 storage.root） |
| created_at / updated_at | TEXT | 创建/更新时间 | |

### photo_events — 处理过程事件（审计/复盘）
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER PK | 事件 ID | |
| upload_item_id | INTEGER | 关联条目 | FK→upload_items.id |
| event_type | TEXT | 事件类型 | 如 PROCESSED/PROCESS_FAILED |
| from_status / to_status | TEXT | 状态迁移 | 可选 |
| message | TEXT | 说明 | |
| details_json | TEXT | 详情 JSON | 可选扩展 |
| created_at | TEXT | 创建时间 | |

---

## V2 表

### photo_sync_batches — 一次同步批次（一个 Cycle 的任务集合）
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER PK | 批次 ID | 自增 |
| started_at / completed_at | TEXT | 批次开始/完成时间 | |
| status | TEXT | 批次状态 | 如 RUNNING/COMPLETED |
| task_count | INTEGER | 任务数 | 本轮 Sync Task 数 |
| success_count / failed_count | INTEGER | 成功/失败任务数 | |
| created_at / updated_at | TEXT | 创建/更新时间 | |

### photo_sync_tasks — 单个月份的一次同步执行
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER PK | 任务 ID | |
| batch_id | INTEGER | 所属批次 | FK→photo_sync_batches.id |
| archive_month | TEXT | 归档月份 | `YYYYMM`（不唯一：同月允许多次 Task） |
| source_path / target_path | TEXT | 源(R5S)/目标(NAS)目录 | |
| status | TEXT | 任务状态 | PENDING/RUNNING/SUCCESS/FAILED |
| started_at / completed_at | TEXT | 执行时间 | |
| exit_code | INTEGER | rsync 退出码 | |
| stdout / stderr | TEXT | 命令输出 | |
| error_message | TEXT | 异常信息 | |
| created_at / updated_at | TEXT | 创建/更新时间 | |

### photo_sync_month_states — 月份同步健康状态历史（追加式）
| 字段 | 类型 | 中文 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER PK | 状态记录 ID | |
| archive_month | TEXT | 归档月份 | `YYYYMM`（同月多行=历史） |
| state | TEXT | 状态 | NORMAL/ABNORMAL/RETRY_REQUESTED |
| consecutive_failed_count | INTEGER | 连续失败次数 | 当前 failure cycle 内 |
| abnormal_at | TEXT | 进入 ABNORMAL 时间 | |
| retry_requested_at / retry_requested_by | TEXT | Retry 时间/操作者 | |
| reason | TEXT | 原因 | 如 init/failure/manual retry |
| created_at / updated_at | TEXT | 创建/更新时间 | |

当前状态语义 = 同 `archive_month` 下 `ORDER BY created_at DESC, id DESC LIMIT 1`（历史不得改写/删除）。
