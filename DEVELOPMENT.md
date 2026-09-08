# Photo Gateway — 开发 / 运行说明

（依据 docs 各 Frozen Spec 落地；总览/使用说明见 README，开发细节以本文件为准。）

## 环境
- conda 虚拟环境：`photo-system`（Python 3.12）
  ```bash
  conda activate photo-system
  pip install -r requirements-dev.txt
  ```

## 常用命令
```bash
# 校验配置（config-spec §53）
python -m photo_gateway check --config config/photo-gateway.example.yaml

# 生成 auth.password_hash（先在无认证配置测试；启用认证前用于 config）
python -m photo_gateway hash-password

# 启动 HTTP 服务（FastAPI + uvicorn + 内置后台 worker）
python -m photo_gateway serve --config config/photo-gateway.example.yaml

# 手动执行一轮过期文件清理（incoming/*.tmp 与缩略图缓存；= serve 维护任务同款逻辑）
python -m photo_gateway maintenance --config config/photo-gateway.example.yaml
# 对账：把 DB 有记录但本地文件缺失的资产标记 file_missing_at（照片页隐藏；历史/外部删除兜底）
python -m photo_gateway maintenance --config config/photo-gateway.example.yaml --scan-missing

# 存量资产补录 EXIF(exif_json) / 磁盘 mtime 对齐到 date_taken（历史数据修复）
python -m photo_gateway backfill-exif --config config/photo-gateway.example.yaml
python -m photo_gateway align-mtime --config config/photo-gateway.example.yaml

# V2 Cleanup：预览候选月份（默认 dry-run，不删任何文件）
python -m photo_gateway cleanup --config config/photo-gateway.example.yaml
# 安全删除已达保留期(synced_retention_days)的本地已同步月份（--apply --yes 双确认，rmtree 不可逆；写 audit.log）
python -m photo_gateway cleanup --config config/photo-gateway.example.yaml --apply --yes
```

## 本地试运行的存储根
spec 认为 `storage.root` 必须先于程序存在（禁止程序自动 mkdir “模拟”磁盘，config-spec §22）。
本地测试可复制一份配置，把 `storage.root` 指到本机已存在目录；子目录
(`incoming/…/Photos/database/logs`) 由程序自动创建。

## 测试
```bash
python -m pytest tests/ -q
```
覆盖：配置强校验 / 结构化日志 / 目录就绪 / SQLite v1+v2 schema 与约束 / Bearer 认证与信封 /
会话与 Precheck / `.tmp` 上传 / Worker 归档去重 / V2 cycle(含 ABNORMAL/Retry/Cleanup) / storage·Reliability /
V2 概览与 RsyncNas 适配等。

## 已落地功能（更新到当前主线）

### V1（Phase 1–5）
- 配置：YAML 强校验 + `--check`；目录就绪(root 必须已存在)
- 日志：五类 JSON 结构化(application/upload/processing/audit/error) + error 聚合 + 上海时区 + 轮转
- SQLite v1 schema(devices / upload_sessions / upload_items / photo_assets / photo_events)
- API: `/api/v1` 前缀 + 统一信封；Bearer 简单认证(login/logout/devices)
- Session/Precheck：创建 Session、批量 Precheck（两级去重命中/未命中/疑似重复）、确认(重传/跳过)、逐条查看
- 上传：multipart 写 `incoming/<file>.tmp` → rename `UPLOADED`（无 Range），扩展名/磁盘门槛，会话统计；
  可选表单字段 `modified_ms`（客户端原文件修改时间, epoch ms）——无 EXIF 照片以它作为 file_mtime 兜底归档，
  避免把“上传时间”误当照片时间（EXIF 存在仍优先；未来/异常值自动忽略）
- Worker(V1)：后台 ticker 自动归档 `Photos/YYYY/YYYYMM`（SHA256→EXIF/date→去重→photo_assets），
  文件名 `_1/_2` 冲突递增不覆盖；重启恢复(processing→incoming+同步状态)
- WebUI：上传向导（建 Session→预检→疑似重复决定→逐文件进度→完成后汇总）+ 照片页 + 系统页
- 只读：`/photos`(分页)、`/photos/{id}/file`、`/system/status`
- 缩略图：`/photos/{id}/thumbnail`（动态 256px、失败回退原图）；缓存目录可配置
  `thumbnails.cache_dir`（config-spec §63.3，默认 `<root>/thumbnails`）
- 维护任务（serve 周期，启动先执行一轮、之后每 24h）：`upload.tmp_cleanup`(incoming `*.tmp`，
  默认 7 天) 与 `thumbnails.cleanup`(缓存 `*.jpg`，默认 90 天) 过期清理在此接线运行

### V2（同步 / Cleanup）
- 配置：新增可选 `sync.*`（见下节）；仅在给出 `sync:` 块且 `enabled: true` 时开启，启用必填 nas 字段
- Worker：`sync.enabled` 时按 `worker.cycle_interval_seconds` 周期跑 run_cycle
  (NAS Alive→V1 Idle(backlog 空)→NAS Ready→Difference→Batch→Sync Task→Month State)
- 日志（v2-spec §42）：每轮 cycle 记入 `processing.log`（event= v2_cycle_gate / v2_batch_created /
  v2_task_result(含 month/batch/exit_code/error 前 400 字符) / v2_month_abnormal /
  v2_cleanup_removed / v2_cycle_done；周期异常 v2_cycle_error）——此前 V2 全程无日志
- Month State：历史追加模型；首次 NORMAL(§13.1)；连续失败达阈值→ABNORMAL(隔离不进 Batch §16)；
  人工 Retry(仅 ABNORMAL)→RETRY_REQUESTED；出现其首个 Task 时开新 NORMAL cycle
- Cleanup：全条件(当前 NORMAL·有历史 Task·最新 Task SUCCESS·达 synced_retention_days)才能删 `YYYY/YYYYMM`
  并带根限定/结构安全/审计；**周期默认 dry_run；真删走人工安全入口 `cleanup --apply --yes` CLI（见常用命令）**
- 传输：`RsyncNas`(rsync over SSH 单向、禁 --delete、timeout)、`FakeNas`(本机无 NAS 测试)
- 只读/操作：`/sync/status`(阈值/保留 + 各月最新 state)、`/sync/overview`(per-month + lowest task/success + cleanup eligibility
  + recent batches)、`POST /sync/months/{ym}/retry`(仅 ABNORMAL)
- WebUI：“同步”页展示 overview/月份表/ABNORMAL Retry 按钮

## V2 同步配置示例（追加到 config YAML）
```yaml
sync:
  enabled: false           # true 时开启 V2 Worker(周期)
  nas:
    host: "nas.example.local"
    ssh_port: 22
    ssh_user: "photo-sync"
    ssh_key: "/root/.ssh/photo-sync-key"
    target_root: "/data/Photos"     # NAS Photo Root
  # source.root 缺省为 storage.root 的 Photos；构造由归档根推导
  worker:
    cycle_interval_seconds: 300
  failure:
    threshold: 3
  cleanup:
    enabled: true
    synced_retention_days: 90
```
> 本地无 NAS 验证时用 `FakeNas`（tests）驱动 cycle；真删除必须走人工安全入口。

## 数据库字段中文速查
- [docs/db-field-dictionary.md](docs/db-field-dictionary.md)（V1 + V2 全部表字段的中文与语义；SQLite 无列注释，此文档为唯一描述入口）

## RECORD（工程轨迹）/ 设计自查摘要
- 目录 / 时区约束：归档一律 `Photos/YYYY/YYYYMM`，时间为上海墙钟（无 tzio）。
- 一致性边界：Session 上传与 Worker 归档/去重 / V2 cycle 均以 SQLite 状态为准，日志只记过程。
- 人工最终控制点：认证口令启用、NAS 真删(rmtree)、真实 rsync 上线均为需要 operator 的操作；代码默认持 dry_run。失败隔离默认不进自动处理。
- 缓存一致性：缩略图为非正式缓存（可重建，api-spec §88.3）；过期清理按文件 mtime、仅限缓存目录 `*.jpg`，不触 `photo_assets`；缓存命中不刷 mtime，被清理后下次访问自动重建。
- V1.2 配置扩展（config-spec §63.3 / api-spec §88.4）：`thumbnails.cache_dir` 与 `thumbnails.cleanup{enabled, max_age_days=90}`；同时把 `upload.tmp_cleanup`（此前仅有配置与原语、未在运行时执行）正式接入 serve 维护任务（启动一轮 + 每 24h）。
- V2 真机验证修复（git b212b34 之后）：RsyncNas `--rsh=` 不含 user@host（rsync 自行追加 receiver）；needs_sync 用 `-rtni` itemize（rsync≥3.4 无 -v/-i 时 dry-run 无输出）并先建远端目录；`/sync/overview` 月份键用 YYYYMM（与 Task/State 一致）。
- V2 运维修复：`POST /sync/months/{ym}/retry` 须 `conn.commit()`（append_month_state 不自动提交；缺步曾致返回成功但未落盘、WebUI 点击无效）；overview 增 `abnormal_reason`/`latest_error`/`latest_error_exit` 供 UI 展示 ABNORMAL 原因与最近 rsync 错误。
- V1 EXIF 多层 IFD 修复（worker.py）：DateTimeOriginal(0x9003)/DateTimeDigitized(0x9004) 位于 EXIF 子 IFD（tag 0x8769 指向），不能只对 `getexif()` 顶层 `get()`——否则手机/相机照片恒丢原始拍摄时间、退化为顶层 DateTime（常为后期修改时间；真实样本偏差 3~12 天）。日期读取 `_exif_date_string` 与 `exif_json` 快照均合并 IFD0 + 0x8769 子 IFD；Pillow 合成 EXIF 是扁平结构测不出此 bug，回归测试用 mock 嵌套 IFD。
- V1.3 soft-missing：photo_assets 加 `file_missing_at`（schema migration v3）；记录是 sha256 去重身份、不随文件删除而删。置位来源：Cleanup 真删批量标记（v2cycle.cleanup_expired）、thumbnail/file 404 惰性标记、`maintenance --scan-missing` 对账；`/photos` 默认过滤缺失（total 同步）；同内容重传**复活**（新文件落回原 archive_path、清标记、asset id 不变——同时修复旧实现"旧文件缺失重传内容丢失"bug）；WebUI img onerror 兜底。对应 db-spec §40 / api-spec §88.5。
- WebUI 桌面增强与灯箱（2026-09）：桌面内容随浏览器宽度弹性缩放（上限 1700px、vw padding）、顶栏三分布局导航居中、panel 标题色条、body 渐变；照片点击改为**灯箱原图预览**（按视口 contain 缩放，Esc/点空白关闭）；`GET /photos/{id}/file?inline=1` 返回无 content-disposition 的内联响应（新窗口查看原图），默认仍为附件下载。
- 照片检索扩展（2026-09）：`GET /photos` 新增 `field=taken|uploaded`（检索维度：拍摄时间 date_taken / 上传时间 created_at；决定 date_from/date_to 过滤字段与排序键）与 `order=asc|desc`（默认 desc；同值按 id 定序）；缺省 field 保持旧语义。WebUI 照片页：维度/方向切换、快捷范围联动起止日期（至=含当天）、每页张数动态（默认按视口宽度估算整行倍数，可选 12/24/48/96）。
- 视频支持（feature/video-support）：默认 `allowed_extensions` 追加 mp4/mov/m4v/3gp/webm/mkv/avi；Worker 用零依赖 QuickTime(moov/mvhd) 解析取容器创建时间（映射 date_source=CreateDate，年份异常回退 file_mtime）；WebUI 照片页视频项以 `<video preload=metadata>` 首帧 + ▶ 角标展示、灯箱内 `<video controls>` 播放（文件端点已支持 Range/206）；服务端不为视频生成缩略图（低性能设备考量）。浏览器对编码支持差异只影响网页播放，不影响上传/归档/同步。
- 同步页增强（2026-09）：新增 `GET /sync/gates`（实时门控：nas_alive/nas_ready/v1_idle/months_total/checked_at，2 次 SSH 探测，供页面手动检查）；「照片同步状态」页展示前置条件卡与最近同步记录时间、月份/批次两表前端排序+分页（overview batches 上限 8→50）；月份“－”状态=目录存在但从未建立同步状态（满足门控后首轮自动建立）。
- 缩略图缓存一致性：原图文件缺失时（`/photos/{id}/thumbnail`、`/photos/{id}/file` 的 FILE_MISSING 分支）先删除该 asset 的孤儿缓存缩略图（thumbnails.drop_thumbnail）再返回 404——手工删除原图后缓存不再残留；DB photo_assets 记录仍保留（如需彻底删除资产（文件+DB+缓存）需另行提供删除流程，当前不在 V1 范围）。
- Known gaps（诚实记录）：
  1. “逐文件失败重传”在 WebUI 内主要是回到上传会话重传（尚无 autofix 逐 file UI 的精修）；
  2. 浏览器 `<img>/<a>` 无法带 Authorization（启用认证时部分查看仅控制台提示用 curl/token）；
  3. EXIF 的 DateTimeOriginal/CreateDate/ModifyDate 读取做了一定简化(非严格三源区分)，worker date_source 可能为近似；
  4. V2 真删除触发未在 WebUI 提供（CLI 人工安全入口已落地：`cleanup --apply --yes`，写 audit.log；WebUI 删除按钮仍可后续加）。真实 NAS(rsync over SSH) 已在局域网一台 NAS 上完成端到端验证（首次同步 / ABNORMAL 隔离 / Retry 恢复链路）；在其他 ARM/Linux 小主机上的实机部署演练尚未进行。

_详见 git history 的里程碑 commit 与代码 docstring。_
