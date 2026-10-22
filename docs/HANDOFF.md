# Photo Gateway — Release Handoff

更新日期：2026-10-15
状态：发布内容已整理并验证，按 Squash 方式集成本地 main；尚未推送 GitHub 或执行生产部署。

## 版本与工作流
- 集成目标：`main`；主线基线 `bfba93c`，完整开发历史保留在本地 `chore/ai-workflow-adoption`（整理前版本 `e6ecaf8`）。集成完成后以 `git log -1 main` 核验正式提交。
- `ai-dev-workflow` 初始接入基线 `82541ee`；本轮按负责人明确批准采用 2026-10-15 分支与发布治理（来源讨论记录 `597e086`，未在本机获取上游源码）。
- 短期分支开发、验证和隐私审查后 Squash Merge 到 main；只推送 main。完整开发提交与过程记录留在本地；不默认改写已有公开历史。
- 本地协作规则不进入公开仓库。新执行者必须独立核验 Git、规格和测试状态。

## 交付范围与权威入口
- 五页 WebUI、品牌图标、自适应照片网格与分页、文字详情和圆角灯箱、卡内上传状态与滚动、系统存储容量、说明流程与命令清单。
- 只读最近导入、照片筛选与详情接口、HEIC 预览；API 与界面约定见 `docs/photo-gateway-v1-api-spec.md`、`docs/photo-gateway-v1-webui-spec.md`。
- 容器后台执行、离线镜像构建交付、运行身份校验及数据库在线备份；运维入口见 `ENVIRONMENT.md`、`DEVELOPMENT.md` 和部署文档。
- 文件名筛选及灯箱独立新窗口入口已按批准的规格移除；存储容量接口只读，单位按 1024 换算。
- 本轮整理不修改应用行为、现有照片、正式数据库、缓存、归档及 NAS 数据。

## 发布整理
- 公开树移除逐轮原型讨论和过程计划；原始文件保留在本地开发历史中。
- 示例 NAS 地址使用域名占位符；移除无关私人设备故障描述。保留版权署名、许可证和必要的通用部署路径。
- 正式提交使用 GitHub noreply 邮箱；隐私检查覆盖文件、差异和待公开提交，不将本地开发历史推送。

## 验证与限制
- 整理前后全量单元/集成回归均为 171 passed，2 条现有依赖弃用警告；命令：`python -m pytest tests/ -q`。
- `node tests/browser/webui-layout.cjs` 隔离浏览器回归 PASS；上传空/有文件卡片等高、独立滚动、上传状态流程、灯箱、容量和窄屏断言通过。运行环境需按 ENVIRONMENT.md 配置 Playwright 与 Chromium。
- 全部待集成差异 `git diff main --check`、部署脚本 `bash -n` 通过；最终文件敏感模式扫描未发现私人路径、内网地址、私钥或令牌。扫描不替代人工评审。
- 浏览器回归仅使用临时 loopback 服务、合成图片及拦截 API，不访问业务数据。
- 回归清单：`REGRESSION_CHECKLIST.md`。生产部署、真实 NAS、真实照片与完整格式兼容性不属于本轮验收。
- 只读 `git ls-remote` 确认远端 main 为 `bfba93c`，与本地主线一致；推送前仍须重新核对远端、仓库可见性及公开发布确认。

## 下一步
核对正式 main 提交与公开差异后，仅推送 main；按部署说明在目标环境构建、加载并验收。不得以本地测试替代生产验收。

## 2026-10-22 V1.4 功能分支（待人工验收）

- 工作分支：`feat/local-photo-recycle-bin`；主分支仍未合并本次改动，严禁自动部署生产。
- 已批准需求规格：`docs/photo-gateway-v1.4-recycle-bin-spec.md`。
- 功能：仅本地单张删除/回收/恢复、默认 30 日实体清理；内容 SHA 命中主动删除资产时人工确认；V1/V2 归档互斥；NAS 永不删除。
- DB schema v4 迁移；新增 `recycle_bin` 和 `review_pending` 目录，待决策超时 7 日。
- 开源约束：公开文件使用部署角色描述（如容器宿主机、旧网关），不得硬编码个人设备别名或用户路径。部署目录通用化为 `deploy/docker-host/`。
- 回归：`pytest tests -q`，`node tests/browser/webui-layout.cjs`，`node tests/browser/recycle-bin.cjs`，`bash -n`，YAML 校验与 Git diff 检查。
- 启用新镜像前必须执行 SQLite Online Backup 并离机保存；回滚必须恢复升级前 DB 快照及旧镜像。
- 人工验收前不合并 main、不推送 GitHub、不进入生产环境。

## 2026-10-22 V1.4 回收站展示优化

- 同一照片浏览工作区支持照片/回收站两种互斥状态，回收站与正常照片不同时展示。
- 回收站沿用原拍摄/上传时间、日期区间、设备与类型筛选和分页，右侧详细信息及灯箱主按钮切换为恢复语义。
- 代码与文档调整仍停留在 `feat/local-photo-recycle-bin`，人工验收之前不得合并主分支或部署生产。
