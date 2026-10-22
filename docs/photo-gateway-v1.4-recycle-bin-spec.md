# V1.4 — 本地照片回收站与删除管理（已确认）
状态：APPROVED FOR IMPLEMENTATION · 2026-10-22

## 边界和原则
系统仅管理运行主机的本地存储，NAS 是单向同步目标。任何删除、回收、恢复和清理操作都不得修改 NAS 上的数据，`rsync --delete` 永远禁止。已同步照片允许从本地删除，但不会从 NAS 撤回。开源文件不出现个人设备名称、个人绝对路径和可识别环境数据。历史月份 Cleanup 与用户主动删除为两条独立生命周期。

## 功能契约
1. 正常照片预览提供“移入回收站”操作（单张），二次确认后在归档锁下将实体文件从 `storage.archive` 移到 `storage.root/recycle_bin/<asset_id>/<filename>`。归档内不可留待同步原件，资产从正常列表隐藏，审计事件保留。
2. 照片页有回收站入口，显示文件名、删除时间、预计清理时间和预览。支持手工恢复到原 `archive_path`；目标冲突拒绝覆盖。恢复后回到正常列表、允许下次差异同步。已达期清理文件不支持直接恢复。
3. `recycle_bin.enabled=true`、`recycle_bin.retention_days=30`、`recycle_bin.auto_purge=true` 可配置；后台维护每日在归档锁下扫描过期项目，清除运行主机实体文件，保留 SHA-256、原始上传历史和删除事件。定期清理不得接触 NAS。
4. 数据库中 ACTIVE、TRASHED、PURGED 是用户操作生命周期；`file_missing_at` 仅表示因旧月份 Cleanup 或外部原因造成的本地文件缺失，不能作为主动删除依据。被主动删除照片始终保留 SHA 身份，重传时不得自动“复活”。
5. 上传前沿用仅根据设备/文件名/大小的弱 Precheck；内容落至 incoming 后，Worker 以 SHA-256 做最终判定。命中 TRASHED/PURGED 时状态转为 REVIEW_REQUIRED，暂存文件绝不进入 Photos，不参加 NAS 同步。WebUI 提醒用户选恢复/重新导入或保持删除。恢复 TRASHED 时以回收站原件为准；恢复 PURGED 使用本次上传字节。拒绝则清理 incoming 临时文件。
6. 操作必须与 V1 归档、V2 差异检测和 rsync 共用归档锁。服务重启时需要在 V2 启动前执行一致性对账，解决文件移动成功但 DB 写入未完成等中断窗口；遇不可恢复的冲突必须停止危险操作而非自行覆盖。
7. API 鉴权继承既有登录机制，所有文件路径必须限定在配置的归档根、回收根内并验证无路径穿越/符号链接逃逸；记录审计日志；错误必须返回可读提示。
8. 单张照片操作优先，不在此版实现整月批量删除、NAS 清理或 Immich 控制。

## 接口与配置（计划）
- `POST /api/v1/photos/{asset_id}/trash`
- `GET /api/v1/recycle-bin`
- `POST /api/v1/recycle-bin/{asset_id}/restore`
- `GET /api/v1/recycle-bin/{asset_id}/preview`
- `POST /api/v1/upload-items/{item_id}/deleted-decision`（choice=restore|keep_deleted）
- `GET /api/v1/upload-sessions/{session_id}/deleted-reviews`（上传界面轮询）
- `recycle_bin: { enabled: true, retention_days: 30, auto_purge: true }`

## 安全与验收门禁
- 确认前后均不能同步已进入回收站的文件；NAS 保持不可删除边界。
- 旧数据迁移幂等、原历史完好；本地 ACTIVE/TRASHED/PURGED 展示和数量正确。
- 恢复、过期清理、重复上传选择、临时文件留存、重启对账、异常处理和并发竞争有测试。
- 禁止引入任何可用于从 NAS 删除文件的调用。
- 开发在独立分支，自动回归与审查完成后提交人工验收；**未经人工明确批准不得合并 main、推送或部署生产**。

## V1.4 UI 调整 — 回收站复用照片工作区（2026-10-22）

“照片”页点击“回收站”应切换同一工作区的数据源，不额外呈现第二套列表或卡片。回收站模式下隐藏全部正常照片，仅展示回收站照片；页面标题变为“回收站”，说明文案为“浏览本地回收站的照片，保留期内可恢复，回收站文件不参与 NAS 同步”，页面右侧“刷新”改为“返回照片”。点击“返回照片”恢复原标题、说明、按钮和正常照片列表。

回收站继续复用原照片网格、右侧信息栏（增加删除时间和预计清理时间）、灯箱放大、缩略图、文件类型与来源设备、拍摄时间/上传时间维度、日期区间、排序与分页（页大小、上一页/下一页）。日期与排序仍按原始拍摄时间或入库时间，不按删除时间。切换模式从第 1 页开始，保留筛选条件和页大小。普通预览灯箱按钮为“移入回收站”，回收站预览灯箱同一位置的按钮为“恢复照片”；恢复须二次确认，成功后从回收站列表移除。

`GET /api/v1/recycle-bin` 与 `GET /api/v1/photos` 共用查询字段：`offset`、`limit`、`field`、`order`、`date_from`、`date_to`、`source_device`、`mime_type`，返回 `photos/total/offset/limit`。新增 `GET /api/v1/recycle-bin/{id}` 用于相同右侧元数据详情，`GET /api/v1/recycle-bin/{id}/thumbnail` 用于网格缩略图，沿用原访问控制和签名预览机制。全部操作仅作用于运行主机本地。
