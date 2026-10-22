# Photo Gateway docker-host Docker Deployment Specification

**Status:** APPROVED
**Effective:** 2026-09-18

## 1. 决策

Photo Gateway 的生产计算层从 legacy-host 迁移到 docker-host。legacy-host 保留网络基础设施职责，NAS 继续是正式照片库，
docker-host NVMe 保存上传、处理、SQLite、缩略图和待同步的短期副本。

本文件只取代旧规格中的“目标运行平台/部署方式”；上传、数据库、API、V2 状态机和 NAS 单向同步语义不变。

## 2. 容器边界

- 一个 Compose service、一个应用进程、一个 Uvicorn worker。
- 镜像目标平台固定为 `linux/amd64`；macOS/ARM64 开发机通过 Buildx/QEMU 交叉构建，禁止发布 `linux/arm64` 镜像到 docker-host。
- 当前不部署 Registry/Harbor；镜像在 Mac 构建并以 `YYYYMMDD-HHMMSS` 上海时间标签导出，经 SHA-256
  校验后 rsync 到 docker-host，使用 `docker load` 导入；Git commit 写入 OCI revision label。
- docker-host 只允许 `docker load` 与 `docker compose ... --no-build`，不承担镜像构建。
- API 事件循环只负责 HTTP；V1/V2 在两个后台线程运行。
- V1 与 V2 通过进程内归档锁串行访问 `Photos`。
- SQLite 连接不得跨线程复用；每轮在所属线程内建立并关闭。
- 停止时最多等待 620 秒，覆盖单次 rsync 的 600 秒超时；Compose 保留 11 分钟退出宽限期。
- 自动 Cleanup 仍为 dry-run；真删仍仅允许人工双确认 CLI。

## 3. 安全与持久化

- 镜像内账号和 Compose `user` 均固定为非 root UID/GID `1000:1000`，与 docker-host bind mount 所有权一致；
  构建参数、运行配置和宿主目录三者不得分叉。根文件系统只读并移除 Linux capabilities。
- 数据、配置、NAS key、`known_hosts` 均使用 bind mount；秘密不得进入镜像或 Git。
- NAS SSH 必须验证 host key，不得使用 `StrictHostKeyChecking=no`。
- SQLite 使用 Online Backup API 备份到容器内 `/data/photo-system/storage/backups`，对应宿主机
  `/data/compose/photo-system/storage/backups`。
- 生产配置权限 0640 或更严格，私钥 0600。

## 4. 国内源

- Docker CE APT 默认使用清华 TUNA Debian 镜像，可通过环境变量切换。
- Docker Hub 加速器属于第三方服务，脚本默认值可覆盖；执行前必须现场验证 TLS、可用性和镜像摘要。
- Dockerfile 的 Debian/PyPI 镜像同样通过 build args 可覆盖。

## 5. 发布门槛

必须依次通过：配置校验、单元/回归测试、`docker_build_rsync.sh` 的 `linux/amd64` 构建与导出、
SHA-256 传输校验、docker-host `load_start.sh` 的镜像/身份/Compose/挂载检查、`docker load`、
Compose `--no-build --pull never` 启动、空数据启动、持久化重启、
V1 上传归档、V2 NAS 门控、数据库备份恢复及回滚演练。具体步骤见
`photo-gateway-container-migration-runbook.md`。未经 Human 最终批准不得合并 `main` 或变更生产。
