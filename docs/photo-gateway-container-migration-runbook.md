# Photo Gateway docker-host Migration Runbook

**Status:** REVIEW（尚未执行）
**Effective:** 2026-09-18

本文是迁移执行清单，不构成生产操作授权。任何 docker-host、legacy-host 或 NAS 变更都必须另行获得 Human 明确批准。

## 1. 前置门槛

- docker-host 的 Debian 13、`/data` 文件系统、容量、权限和时间同步已核验；硬件启动行为不在本轮范围。
- Docker/Compose 安装脚本经审阅，并在非生产或维护窗口验证。
- Mac 上 `docker_build_rsync.sh` 已生成时间标签的 `linux/amd64` 镜像；tar 与 SHA-256 已 rsync。
- docker-host `load_start.sh` 已选择最新完整产物，通过 hash、镜像、身份、Compose、挂载和应用配置检查后
  `docker load` 并启动；docker-host 未执行构建或 pull。
- 完整回归、空数据启动、只读根文件系统和重启持久化均通过。
- 容器与 docker-host bind mount 使用同一数字身份 `1000:1000`，storage 可写、配置可读、私钥权限为 0600。
- 配置、NAS 私钥及 `known_hosts` 已在 Git 外准备；host key 指纹经独立可信渠道核对。
- legacy-host 当前服务版本、配置、数据库和数据目录已记录；回滚负责人和维护窗口已确认。

## 2. 预迁移（旧服务保持运行）

1. 在 docker-host 创建目录和部署文件，导入经校验的镜像，仅以 `sync.enabled=false`、仅内网绑定并使用
   `docker compose up -d --no-build` 启动空实例。
2. 执行配置检查、健康检查和测试上传；验证 V1 归档、HEIF 预览、SQLite 在线备份及恢复。
3. 对 legacy-host 数据做首次只读预复制，记录文件数、总字节数和校验结果；不得删除源数据。
4. 在隔离测试配置下验证 NAS SSH host key、只读探测及 V2 门控；此阶段不执行 Cleanup。

## 3. 切换窗口

1. 停止新上传入口，等待 legacy-host 中 `UPLOADED/PROCESSING` 项归零并记录状态。
2. 使用 `db-backup` 生成 SQLite 一致性备份并完成 `integrity_check`；备份和配置另存一份。legacy-host 原生服务执行
   `python -m photo_gateway db-backup --config <legacy-host 配置路径>`；docker-host Docker 服务执行
   `docker compose --env-file .env -f compose.yaml exec -T photo-gateway python -m photo_gateway db-backup --config /etc/photo-gateway/config.yaml`。
3. 停止 legacy-host 应用，执行最终增量复制；再次核对数据库、文件数、字节数及抽样 SHA-256。
4. 在 docker-host 导入最终数据，以已验证的镜像标签和 `sync.enabled=false` 启动；验证健康、状态、照片读取和新上传归档。
5. 通过后再启用 V2，先验证 NAS 差异和实际同步；Cleanup 保持 dry-run。
6. 更新入口指向 docker-host，并持续观察错误日志、磁盘阈值、线程状态和同步结果。

## 4. 回滚触发与步骤

出现数据完整性异常、持续上传失败、数据库错误、NAS 错误同步或无法在维护窗口内验证时立即回滚：

1. 再次停止上传入口，停止 docker-host 容器并保留其全部数据和日志，不执行清理。
2. 若 docker-host 已接收新文件，将新增文件和数据库作为独立证据保存；不得直接覆盖 legacy-host 数据库。
3. 恢复切换前入口并启动原 legacy-host 服务，确认其健康与上传能力。
4. 对切换窗口内的新数据制定单独合并方案；未证明安全前不得自动回灌。
5. 记录时间线、影响、证据和根因，修复后重新走完整门槛。

## 5. 迁移完成标准

- 连续观察期内 API、V1、V2、SQLite 和磁盘状态正常；无静默失败。
- 新旧两侧数据核对完成，备份恢复演练通过，NAS 正式库抽样一致。
- Human 明确接受结果后，才可安排旧服务退役；旧数据删除是独立高风险任务，不包含在本流程内。
