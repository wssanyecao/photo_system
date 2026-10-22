# 版本发布

对外发布使用已经验收的 `main` + **一个指定版本 Tag** + GitHub Release；无需永久发布分支。日常推送 `main` 仅同步代码，**不自动表示软件正式发布**。

## 版本来源

`photo_gateway/__init__.py` 的 `__version__` 是应用版本号的唯一来源；CLI `--version`、HTTP 健康检查和 FastAPI 元数据都应与之相符。应用源码自报版本与正式 Tag 是两个独立概念；是否已经正式发行，以 [GitHub Releases](https://github.com/wssanyecao/photo_system/releases) 中的记录为准。

## 发布前检查

1. 完成任务分支测试与审查，并将公开内容 Squash Merge 到 `main`；核对远端 Public 状态与待推送提交的可达历史，不发布本地 AI 交接、环境资料、凭据及个人路径。
2. 确定正式版本号，必要时先更新 `__version__`；检查 Tag 尚未占用、CLI/API 版本一致、`CHANGELOG.md` 的 `Unreleased` 内容已整理成对应版本条目。
3. 运行 `python -m pytest tests -q`，核对 GitHub Actions 对目标提交的检查；浏览器交互回归和生产环境验证如未执行应明确标记，不以 CI 的 Python 测试代替。
4. 对数据库或容器升级，检查 [备份和回滚说明](container.md#升级与数据保护)；版本说明仅列新增、修复、兼容性变化和必要升级注意。
5. **分别取得**代码推送、指定 Tag/GitHub Release、生产部署的授权。任何一步都不能代替其他步骤。

## 发布动作（仅在审批后）

首先核对 `main` 干净并已同步远端，确认版本号与 Tag 对应，再创建带注释的 `vX.Y.Z` Tag，**只推送这一条 Tag**，随后创建 GitHub Release 并复核远端标签、说明和产物。不得使用 `git push --tags`、`--all`、`--mirror` 或未批准的强制推送。

仓库 CI 仅运行 Python 自动化测试和指定 Tag 与应用版本的一致性检查；**不会自动发布或部署**。首次 Release 应在有人审阅过以上检查结果后进行。
