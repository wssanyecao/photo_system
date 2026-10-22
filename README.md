# Photo Gateway

Photo Gateway 是一个自托管的照片与视频导入服务：通过浏览器上传文件，按拍摄时间归档、按内容去重，并可将本地归档**单向同步**到 NAS。可与 Immich 等支持外部图库的相册应用搭配使用。

**照片与数据库保存在自有设备上，不依赖第三方云存储。**

## 主要功能

- **上传与归档**：支持电脑和手机浏览器批量上传，按 `Photos/YYYY/YYYYMM` 组织原始文件；通过 SHA-256 去重，不压缩原文件。
- **图片与视频**：支持 JPEG、PNG、HEIC/HEIF、RAW、MP4/MOV 等格式（具体以配置为准）；提供缩略图、原图预览和浏览器支持的视频播放。
- **照片浏览**：按拍摄时间或上传时间筛选，支持日期范围、来源设备、类型、排序及分页。
- **本地回收站**：单张照片移入回收站、查看和恢复；默认保留 30 天。重新上传已主动删除的相同内容时要求人工确认。
- **NAS 同步**：通过 SSH/rsync 单向增量同步；提供同步状态、失败重试与本地历史清理。**不会删除 NAS 上已有的照片。**
- **数据管理**：SQLite 本地数据库、在线一致性备份、上传处理日志与基本的磁盘容量监控。

```text
手机 / 电脑
    │ 浏览器上传
    ▼
Photo Gateway
    ├── Photos/YYYY/YYYYMM   本地归档
    ├── recycle_bin          本地回收站
    └── SQLite               文件索引与上传记录
    │
    └── SSH / rsync ────────► NAS（可供 Immich 外部图库读取）
```

## 运行要求

- Python 3.12
- 可写的本地文件系统（用于照片与 SQLite；请勿将数据库放在网络文件系统上）
- NAS 同步为可选功能，需要目标支持 SSH 与 rsync
- 如使用容器部署，需要 Docker Engine 和 Docker Compose

## 快速开始（Python）

以下示例使用独立 Python 虚拟环境。Linux 和 macOS 均可运行；请自行准备已挂载且有足够空间的数据目录。

```bash
git clone https://github.com/wssanyecao/photo_system.git
cd photo_system

python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

mkdir -p "$HOME/photo-gateway-data"
cp config/photo-gateway.example.yaml config/config.yaml
```

编辑 `config/config.yaml`，至少设置：

- `storage.root`：改成实际数据目录，例如 `/home/user/photo-gateway-data`
- `devices`：配置允许上传的来源设备及显示名称
- `auth`：按需启用登录认证。生成密码哈希：`python -m photo_gateway hash-password`

`config/config.yaml` 是**本地配置文件，不应提交到 Git**。请确保数据目录事先存在并具有写权限。

```bash
python -m photo_gateway check --config config/config.yaml
python -m photo_gateway serve --config config/config.yaml
```

浏览器访问 `http://127.0.0.1:8080/`。需要其他设备访问时，在配置中指定合适的内网监听地址，并建议启用认证；**不要将未启用认证的服务直接暴露到公网**。

容器部署见 [Docker Compose 指南](docs/container.md)。

## 配置 NAS 同步

在 `config/config.yaml` 中设置 `sync.enabled: true`，并配置 NAS 地址、SSH 用户、私钥、已验证的 `known_hosts` 和目标路径。配置示例见 [config/photo-gateway.example.yaml](config/photo-gateway.example.yaml)。

NAS 是同步目标，回收站只处理本地文件。移入回收站的照片不再参与后续同步；先前已同步到 NAS 的文件**不会被远程删除**。照片恢复后允许再次同步。

## 网页使用

| 页面 | 主要操作 |
| --- | --- |
| 上传 | 选择来源设备、批量选择文件、检查重复并上传 |
| 照片 | 浏览、筛选、分页、预览与移入回收站 |
| 回收站 | 在照片页切换；沿用筛选与分页，点击大图后恢复照片 |
| 同步 | 查看 NAS 同步条件、月份状态和失败重试 |
| 系统 | 查看存储容量、处理状态和相关统计 |
| 说明 | 查看基础操作与维护说明 |

## 维护命令

所有命令均在应用环境中执行。以下示例使用相同的配置文件：

```bash
python -m photo_gateway check --config config/config.yaml
python -m photo_gateway db-backup --config config/config.yaml
python -m photo_gateway maintenance --config config/config.yaml
python -m photo_gateway cleanup --config config/config.yaml
```

`cleanup` 默认仅预览可清理的**本地**历史月份；`cleanup --apply --yes` 才执行不可逆删除。执行前应验证 NAS 副本与数据库备份。数据库建议定期使用 `db-backup` 创建一致性快照，并将备份保存在独立存储位置。

回收站默认 30 天后清理本地实体文件，保留内容哈希与删除记录；可以通过 `recycle_bin.retention_days` 调整。

## 项目结构

```text
photo_gateway/       服务与 WebUI 源码
config/              配置样例
deploy/compose/      Compose 部署配置样例
docs/container.md    容器部署指南
tests/               自动化测试
Dockerfile           容器镜像构建
compose.yaml         Docker Compose 服务定义
```

运行自动化测试：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

## 更新记录

源码自报版本以 `python -m photo_gateway --version` 为准。**`main` 是持续更新的代码，不等同于正式稳定版本**；安装时请优先选择 [GitHub Releases](https://github.com/wssanyecao/photo_system/releases) 列出的已验证版本。如果暂时没有 Release，表示尚未建立正式软件版本。

- [更新记录](CHANGELOG.md)：按正式版本维护用户可感知的新增与修复
- [版本发布说明](docs/release.md)：版本规则、自动检查与正式发布流程
- [升级与回滚注意事项](docs/container.md#升级与数据保护)：数据库迁移前必须备份，应用发布与生产升级分别执行

## 许可证

[MIT License](LICENSE)
