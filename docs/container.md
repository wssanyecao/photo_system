# Docker Compose 部署

本项目提供 `Dockerfile` 和 `compose.yaml`，可以在支持 Docker 的 Linux 主机上运行。镜像默认目标架构为 `linux/amd64`；其他架构的部署需自行验证依赖和镜像构建。

## 准备文件

```bash
cp deploy/compose/env.example .env
mkdir -p config
cp deploy/compose/config.example.yaml config/config.yaml
```

编辑 `.env` 中的 `PHOTO_STORAGE_ROOT`、`PHOTO_CONFIG_PATH`、`PHOTO_NAS_KEY_PATH` 和 `PHOTO_KNOWN_HOSTS_PATH`，分别指定主机上的持久化数据目录、配置文件、NAS SSH 私钥和 `known_hosts`。Compose 使用 bind mount，**这些路径必须在启动前实际存在**。

编辑 `config/config.yaml` 中的 `devices`、`auth` 和存储设置。容器配置的 `storage.root` 应指向 `/data/photo-system/storage`，HTTP 服务端口为 `18080`。启用 NAS 同步时，应在配置里使用容器内路径 `/home/photo-gateway/.ssh/nas_key` 与 `/home/photo-gateway/.ssh/known_hosts`。

容器以非 root UID/GID 运行（默认 `1000:1000`）；宿主机数据目录需要对该数字身份可读写，配置与 SSH 文件应限制权限。默认端口绑定为 `127.0.0.1:18080`，请通过可信反向代理或按需设置内网监听地址。

## 启动

```bash
docker compose --env-file .env -f compose.yaml build
docker compose --env-file .env -f compose.yaml up -d --no-build
docker compose --env-file .env -f compose.yaml ps
```

服务状态与日志：

```bash
docker compose --env-file .env -f compose.yaml logs --tail=100 photo-gateway
```

## 升级与数据保护

升级前停止新增上传，确认同步任务结束，并在现有版本上执行 SQLite 一致性备份：

```bash
docker compose --env-file .env -f compose.yaml exec -T photo-gateway \
  python -m photo_gateway db-backup --config /etc/photo-gateway/config.yaml
```

将备份复制到独立存储，并保留原镜像、原配置和数据库快照。更新镜像后再启动新版本。程序可能在启动时自动迁移 SQLite schema，**不要直接用旧版本镜像打开已经迁移的新数据库**；回滚时应配套恢复升级前的数据库备份，并妥善保留升级期间新增的文件。

NAS 同步不会删除目标文件；本地回收站默认 30 天后清理实体文件，且不接触 NAS。涉及文件清理前请先确认备份完整性。

如需查看全部配置项，请参考 [配置样例](../config/photo-gateway.example.yaml) 与 [README](../README.md)。
