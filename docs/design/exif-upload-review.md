# Photo Gateway 上传前 EXIF 时间检查与人工补全（P0）

状态：用户已确认技术方案与两分类 UI；当前仅开发分支，未发布或部署。

## 授权增量

本方案补充 V1「原始内容不变、EXIF 只读」条款：浏览器/Mac 上的原始照片始终不变；新归档 JPEG/JPG 经人工采纳后允许对**服务端临时工作副本**补写缺失的 EXIF 时间，不重新编码图像像素。没有采纳的文件沿用原归档行为。

浏览器 P0 只原生读取 JPEG EXIF；HEIC/HEIF 能携带 EXIF，但当前浏览器检查器暂不解析，显示“无法读取”原因并让用户选择。P1 评估 HEIC/HEIF 检测与写入；PNG/RAW/视频保持原有上传功能，不自动改写元数据。

## 业务流程

1. 原有设备/文件选择、清单去重；原有服务端元数据预检仍使用来源设备 + 原文件名 + 原文件大小，疑似重复先人工确认。
2. 仅对要上传的文件在浏览器检查 DateTimeOriginal、CreateDate、ModifyDate。候选时间优先来自有效 EXIF 字段，再来自可验证的 14 位文件名时间（或仅 8 位日期的零点，需人工核对）。不自动把修改时间或上传时间当拍摄时间。
3. 待处理：EXIF 完全缺失、部分缺失、无法读取，且用户尚未决定；已处理：完整自动通过及人工已采纳、原样上传、暂不上传。页面只显示这两个分类，数量实时变化。
4. DateTimeOriginal 缺失、CreateDate 有效时显示具体字段状态和日期输入，允许采纳或保留原样；采纳时仅补写缺失或无效字段，保留所有有效字段。
5. 前端仅在用户采纳时发送 exif_time，要求 YYYY-MM-DDTHH:MM:SS（1900—2100 年有效日历日期）。每个文件的日期独立保存。
6. Worker 在原始字节哈希查重及回收站保护之后才生成 EXIF 工作副本，经 ExifTool 写入与回读验证后重新计算归档哈希，按原有时间优先级归档。

## 文件身份和数据库

- 归档资产 photo_assets.sha256 始终对应磁盘上的归档文件字节。
- v5 迁移给 upload_items 增加 exif_confirmed_time、source_sha256；新增 photo_source_hashes(source_sha256 → asset_id)，用于识别“源照片未变、仅 EXIF 补写导致归档字节变化”的重复导入。
- 同一原始照片后续重新采纳另一个时间仍按重复处理；本功能不静默修改已有资产的归档时间/位置。
- 回收站 TRASHED/ PURGED 的已删除身份优先由原始哈希/别名判定，保留人工恢复门禁。TRASHED 可恢复旧归档；PURGED 若原始字节与历史归档字节不同，阻止用错误字节冒充旧 SHA 恢复，允许保持删除。

## 故障与限制

- EXIF 写入失败或回读不一致，记录 EXIF_WRITE_FAILED；客户端原片不变，任务不进行错误归档。
- 原始哈希在补写前保存，重启后可继续识别；有界的 .exif-<item-id>.tmp 属于待清理的临时工作副本，不拼接完整原文件名。
- 浏览器 JPEG EXIF 检查限读文件头 4 MiB；无效标记、非 JPEG、缺少解析能力时均显示具体失败原因，不能误判为“没有 EXIF”。
- EXIF 日期推荐属于候选值，微信文件名时间可能是导出/保存时间，需人工判断。
- Docker 增加 libimage-exiftool-perl，尚未执行生产镜像构建/升级、NAS 同步或真实照片测试。
- 正式数据库升级前必须按既有在线备份规范执行，确保回滚材料。

## 验证范围

合成 JPEG 的全部缺失/部分缺失/现有时间保留、日期严格校验、工作副本、原始哈希别名去重、回收站、跳过后的会话收尾、原有 Worker/API/Python 与浏览器回归。生产容器及真实 NAS 需独立授权。


## 独立审查 bbd00b8 发现的修订（2026-11-02）

- 同一 upload_item 失败后再次成功上传，旧的 source_sha256、错误码、错误描述和结束时间不再沿用；新的原始 SHA 由 Worker 计算。若原会话为 PARTIAL/FAILED，在成功重传后恢复 UPLOADING。对已处于 UPLOADED/PROCESSING/COMPLETED 的 item 拒绝重新覆盖，防止文件被 Worker 消费时再替换。
- Worker 自身处理中断后续跑不经过 /file；继续使用已持久化的原始 source_sha256，以区分归档副本 SHA 与原始 SHA。
- 读取、写入和回读 EXIF 字段都限定 ExifTool 的 EXIF 分组。XMP-only、EXIF/XMP 同名值冲突必须准确区分。
- 浏览器 datetime-local 合法整分钟值可能省略秒数字段；DOM 输入边界补齐 :00，传给 API 的时间始终含完整秒数。
- 回归证据涵盖 A 失败后换等长 B（A 后续可归档）、Worker 写入后中断重启、XMP-only 归档到人工确认月份、EXIF/XMP 冲突、浏览器零秒/零点/非零秒实际上传请求。

## 并发上传所有权修复（2026-11-02，基于 Codex 对 4bb1df0 的 FAIL 复审）

- 数据库升级至 v6，为 upload_items 增加 upload_token、upload_claimed_at。用户确认疑似重复后 status=UPLOADING 仍表示“待传输”，不会被拒绝；持有效 token 的写入者才处于 RECEIVING；Worker 只消费 UPLOADED。
- 写入前在 SQLite BEGIN IMMEDIATE 事务中原子 claim 状态及 token；同一 item 的第二个请求在读文件前 409。v1 的 incoming 文件按文件名共享，故对不同 item 的同名文件在 RECEIVING/UPLOADED/PROCESSING 期间也拒绝竞争，避免覆盖未完成的 Worker 输入。
- 每次请求只写有界的 .upload-<item-id>-<token>.tmp，避免原文件名接近文件系统上限时 staging 超长。只有验证有效 token 后，才在 SQLite 事务的临界区 rename 为 incoming/<文件名>，将 EXIF 时间与 UPLOADED 状态一起提交。Worker 看不到正在写入的 stage。
- 上传失败或取消后按 token 有条件释放，不得清除后来请求的权利；令牌超过 2 小时可由同一 item 的重试请求替换，即使旧请求继续运行，也无法发布或回写。同 item 重传成功时 source_sha256 清零，由 Worker 重新绑定新原始字节身份；Worker 中断恢复直接消费已发布 UPLOADED，不改变原始身份。
- 文件归档移动后而 DB 未入库的 baseline 已知缺口仍需另行治理；生产容器构建、备份、数据库迁移、NAS 未执行。

## 上传意图与消费结束边界

- 重复项确认上传后，EXIF 页仍可撤销尚未获得 token 的 UPLOADING/1；撤销与 claim 使用条件更新原子竞争，RECEIVING 或已发布项不能被旧确认覆盖。已跳过的 NO_MATCH 不能重新 claim，前端确认失败必须显示为失败，不能计作跳过。
- Worker 完成共享 incoming 路径的最后一次清理后，才提交 DUPLICATE 释放同名占用；终态之后不得再删除该共享路径。清理失败保持日志可查，旧 Worker 不再重试删除后来发布的文件。
- 若重复源已清理而终态尚未提交即中断，仅当持久源 SHA 对应已有 ACTIVE 资产、本地归档存在且未标记缺失，才可恢复 DUPLICATE；不恢复未知资产、TRASHED/PURGED 或缺失归档。这不修复新资产归档移动后、落库前的 O1 窗口。
- staging 删除失败也必须执行按 token 的失败释放并保留原始上传错误；无法清理的私有临时文件有日志，不能发布或撤销后来请求的权利。
- 生产数据库 v6 迁移仍未执行；测试只使用临时数据库，不代表生产升级或完整崩溃恢复验收。
