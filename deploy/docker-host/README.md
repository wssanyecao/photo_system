# docker-host 部署资产

本目录提供 Docker 安装和 Photo Gateway 部署资产；提交这些文件不会修改 docker-host。生产执行和数据迁移需单独批准。

## 1. 安装 Docker/Compose

`install-docker.sh` 支持 Debian 13 amd64，默认：

- Docker CE APT：清华 TUNA；
- Docker Hub mirror：`https://docker.1ms.run`（可用 `DOCKER_REGISTRY_MIRROR` 覆盖或置空）；
- Docker data-root：`/data/docker`。

```bash
sudo ./deploy/docker-host/install-docker.sh
```

通过 `sudo` 运行时，脚本默认把发起 sudo 的当前用户加入 `docker` 组；可用 `DOCKER_USER=其他用户`
覆盖，或以 `sudo DOCKER_USER= ./deploy/docker-host/install-docker.sh` 明确关闭。加入 `docker` 组等同授予主机
root 能力，不接受该风险时应关闭并始终使用 sudo。
第三方 Docker Hub mirror 可能失效或存在供应链风险，生产执行前应验证 TLS、拉取结果和镜像摘要。

脚本只安装和配置 Docker，不创建 `/data/photo-system` 或任何业务项目目录。它会在写入
`/etc/docker/daemon.json` 后校验配置并显式重启 Docker，再核对 data-root 与 mirror 是否生效。
GPG key 和 APT 下载带重试；`hello-world` 连续三次失败会明确报告为外部拉取验证失败。

## 2. UID/GID 与部署目录

镜像中的 `photo-gateway` 用户以及 Compose 运行身份默认都是 `1000:1000`。bind mount 不做用户名映射，
只比较数字 UID/GID，因此 docker-host 上可写目录应由同样的 UID/GID 拥有。先确认：

```bash
id -u
id -g
```

docker-host 当前结果应均为 `1000`。部署目录统一放在：

```text
/data/compose/photo-system/
├── compose.yaml
├── .env
├── images/                 # Mac 导出的镜像 tar 与 sha256
├── config/config.yaml
├── secrets/nas_key
├── secrets/known_hosts
└── storage/                # incoming/Photos/database/logs/backups 等持久化数据
```

目录初始化示例（只创建项目目录，不属于 Docker 安装脚本）：

```bash
sudo install -d -m 0750 -o 1000 -g 1000 \
  /data/compose/photo-system \
  /data/compose/photo-system/images \
  /data/compose/photo-system/config \
  /data/compose/photo-system/secrets \
  /data/compose/photo-system/storage
```

这些目录必须在启动 Compose 前显式创建；Compose 配置使用 `create_host_path: false`，不会在路径写错时
悄悄创建空目录。`storage/` 必须允许 `1000:1000` 写入；配置建议 `0640`，私钥必须 `0600`。
可用 `stat -c '%u:%g %a %n' 路径` 检查 docker-host 上实际的数字所有者和权限。

配置必须把 NAS key 指向容器路径 `/home/photo-gateway/.ssh/nas_key`。使用 `ssh-keyscan` 得到的
host key 必须通过独立可信渠道核对指纹后，才能写入 `known_hosts`；不得关闭 host key 校验。

首次启动保持 `sync.enabled=false`。认证、NAS 与生产数据迁移完成前不得暴露到公网。

## 3. Mac 构建并导出镜像

暂不部署私有 Registry。开发机为 macOS/ARM64，目标 docker-host 为 Linux/x86_64。提交并保持工作区干净后，
执行仓库根目录脚本：

```bash
./docker_build_rsync.sh
```

脚本使用上海时间标签 `YYYYMMDD-HHMMSS`，强制构建 `linux/amd64`，验证镜像内 `1000:1000`，导出到
`.artifacts/images/`，生成 SHA-256 后用延迟更新方式 rsync 到 docker-host。Git commit 仍写入 OCI
`org.opencontainers.image.revision` label，时间标签可读性与源码可追溯性同时保留。

默认远端为 `docker-host:/data/compose/photo-system/images/`；可通过 `REMOTE_HOST`、`REMOTE_IMAGE_DIR`、
`APP_UID`、`APP_GID` 覆盖。工作区不干净时默认拒绝发布；仅临时实验可显式设置 `ALLOW_DIRTY=1`。

## 4. rsync 到 docker-host 并导入

首次部署或 Compose/样例变化时，先从 Mac 同步不含秘密的部署文件；不得覆盖 docker-host 上的真实 `.env`
和 `config/config.yaml`：

```bash
rsync -rtvP compose.yaml docker-host:/data/compose/photo-system/compose.yaml
rsync -rtvP deploy/docker-host/env.example \
  docker-host:/data/compose/photo-system/env.example
rsync -rtvP deploy/docker-host/photo-gateway.example.yaml \
  docker-host:/data/compose/photo-system/config/config.example.yaml
rsync -rtvP deploy/docker-host/load_start.sh \
  docker-host:/data/compose/photo-system/load_start.sh
ssh docker-host chmod 0750 /data/compose/photo-system/load_start.sh
```

这里使用 `-rt` 而不是 archive 模式 `-a`：只递归传输文件并保留修改时间，不跨 macOS/Linux 复制
属主、属组和权限。docker-host 上的文件归属由 SSH 登录用户和本地 umask 决定，需要执行权限的脚本再显式
`chmod`，避免两端账号或权限模型不同导致警告和意外权限。

镜像和校验文件已由 `docker_build_rsync.sh` 同步。传输使用 rsync 临时目录与延迟更新，未完成文件不会
匹配 docker-host 的发布文件规则。

然后登录 docker-host 执行：

```bash
cd /data/compose/photo-system
./load_start.sh
```

`load_start.sh` 按文件名时间选择最新完整 tar，并依次验证 sidecar 结构、SHA-256、镜像 tag、
`linux/amd64`、镜像 UID/GID、Compose image/platform、一次性容器的实际 UID/GID、应用配置和
bind mount 读写权限。
任一检查失败立即退出，不更新 `.env`、不启动新版本。

## 5. 配置并启动

首次部署时以 `env.example` 和 `config/config.example.yaml` 为模板生成真实文件；后续不得用样例覆盖。
确认 UID/GID、宿主路径和秘密文件后，由 `load_start.sh` 在所有启动前检查通过后原子更新
`.env` 的 `PHOTO_IMAGE_TAG`，再使用 `--no-build --pull never --wait` 启动并核对运行镜像 ID。

```bash
cd /data/compose/photo-system
./load_start.sh
```

docker-host 不执行镜像构建，`--no-build` 是强制边界。回滚时把 `PHOTO_IMAGE_TAG` 改回已导入的前一版本，
再次执行 `up -d --no-build`。镜像 tar 只是交付物，不替代 Git、数据备份或 NAS 备份。

## 6. SQLite 一致性备份

数据库使用 WAL 模式；服务运行中不得只复制 `photo-gateway.db`。应通过运行中的容器执行 `db-backup`，
生成包含已提交 WAL 事务的单文件一致快照：

```bash
cd /data/compose/photo-system
docker compose --env-file .env -f compose.yaml exec -T photo-gateway \
  python -m photo_gateway db-backup --config /etc/photo-gateway/config.yaml
```

默认目标是 `/data/compose/photo-system/storage/backups/photo-gateway-时间.db`；也可在命令末尾追加
`--output /data/photo-system/storage/backups/自定义名称.db`。该命令拒绝覆盖已有文件，不修改原 `.db`、
`-wal` 或 `-shm`。生成成功后必须校验快照，并复制到 docker-host 之外的独立存储；同一块 NVMe 上的备份不能替代
灾难恢复备份。

生产架构见 `docs/photo-gateway-container-deployment.md`，迁移和回滚步骤见
`docs/photo-gateway-container-migration-runbook.md`。

## 本地回收站升级（v1.4）

升级前先停止新的上传操作、检查 NAS 正在同步的任务已完成，执行应用的 `db-backup`，确认 SQLite 快照有效并**离机保留**；此外要保留当前镜像标签与新旧 Compose/配置文件。容器下次启动自动迁移数据库到 schema v4。生产上禁止直接用旧镜像读取已升级数据库：回滚时停止服务，使用升级前快照恢复数据库，连同旧镜像和配置同步回退，**保留并单独隔离升级期间产生的照片、recycle_bin、review_pending**，不要直接覆盖照片目录。首启确认：健康检查、照片和回收站接口、上传重复照片人工确认、NAS 同步无删除操作、审计日志。发布过程不触发任何 NAS 删除。
