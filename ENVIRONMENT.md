# Photo Gateway Environment Notes

**Status:** APPROVED
**Effective:** 2026-09-18
**Scope:** 运行、开发、测试、生产环境的唯一环境说明；业务与数据契约仍以 `docs/` 各规格为准。

## 环境矩阵

| 环境 | 平台 | Python | 启动方式 | 数据 |
| --- | --- | --- | --- | --- |
| 开发 | macOS / ARM64 | conda `photo-system`, Python 3.12 | `python -m photo_gateway` | `data_runtime/`，不视为生产证据 |
| 测试 | macOS/CI | Python 3.12 | `pytest` | 临时目录/FakeNas |
| 旧生产 | embedded gateway legacy-host / ImmortalWrt | Python 3.12 venv | procd | USB HDD；迁移完成前保留 |
| 新生产 | docker-host / Debian 13 amd64 | 容器内 Python 3.12 | Docker Compose（仅 load/run） | `/data/compose/photo-system/storage`（NVMe） |

统一时区为 `Asia/Shanghai`。SQLite 必须位于本地文件系统，禁止放在 NFS/SMB。

## docker-host 生产目录

- Docker Engine：`/data/docker`
- Compose 项目：`/data/compose/photo-system`
- 镜像交付文件：`/data/compose/photo-system/images`
- 运行数据：`/data/compose/photo-system/storage`
- 配置：`/data/compose/photo-system/config/config.yaml`
- NAS 私钥与 host key：`/data/compose/photo-system/secrets/`
- 数据库备份：`/data/compose/photo-system/storage/backups`

容器固定单实例。API 运行在主线程；V1 与 V2 各有一个后台线程，并通过归档锁协调。
生产安装、迁移和切换必须按 `docs/photo-gateway-container-deployment.md` 执行。

## 依赖与平台差异

- 运行依赖见 `requirements.txt`；`pillow-heif` 为必装依赖。
- 容器镜像额外包含 `rsync`、`openssh-client`、`tzdata`。
- Debian 13 宿主机 Python 版本不作为应用运行时；应用固定使用镜像内 Python 3.12。
- 容器运行用户默认 `1000:1000`，与 docker-host 操作系统用户及 bind mount 所有权一致；权限按数字 UID/GID 判定。
- 开发机为 ARM64，docker-host 为 x86_64；`docker_build_rsync.sh` 在 Mac 生成上海时间标签的
  `linux/amd64` 镜像并 rsync，`load_start.sh` 在 docker-host 选择最新完整产物、校验并启动；docker-host 不构建镜像，
  当前不部署私有 Registry。Git commit 保存在镜像 OCI revision label 中。
- NAS 上线前须确认实际路由和 SSH 权限。

## 可选 WebUI 浏览器回归环境

`tests/browser/webui-layout.cjs` 使用 Node.js 18+、Playwright 和兼容 Chromium，仅作为本地测试工具，不加入服务运行依赖。运行 `node tests/browser/webui-layout.cjs`；若使用既有浏览器，可通过 `PHOTO_WEBUI_CHROMIUM` 指定可执行文件。结果截图默认写入系统临时目录，亦可由 `PHOTO_WEBUI_EVIDENCE_DIR` 指定。脚本启动临时 loopback HTTP 服务，所有 API 响应及照片均为合成数据，不连接正式服务或访问业务数据。浏览器/Node 包安装路径由各开发环境配置，不写入公开脚本。
