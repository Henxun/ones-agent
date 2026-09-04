# ONES Dev Agent

ONES 缺陷/需求驱动的安全开发工作台，提供 `ones-dev tui` 终端界面。

## 已支持能力

- MVP 配置向导：全局只填写 ONES 地址、团队、缺陷类型和账号密码；Project、迭代和仓库在创建工作区时配置。
- ONES 缺陷：按项目、迭代和负责人拉取缺陷候选；每条缺陷可选择只读的 **AI 分析**，或在隔离 worktree 中执行 **分析并修复**。
- 本地优先：MVP 到审核包为止，不推送、不创建 PR；后续发布能力再单独启用。按当前 MVP 约定，Codex 与复现/测试命令不启用 OS sandbox，只保留隔离 worktree、`shell=False`、超时和输出上限；请仅在可信仓库中运行。
- 全流程门禁：分析和代码修改在隔离 worktree 中执行；提交、推送、PR 和 ONES 评论必须经过显式审核。
- 凭据安全：秘密不写入配置 JSON、日志或 TUI；Windows 使用 Credential Manager，macOS Preview 使用当前用户默认 Keychain/search list，非敏感配置使用用户私有存储。

## 启动

Windows 或 macOS、Python 3.11+：

```console
uv sync
uv run ones-dev tui
```

macOS 使用 Terminal 或 iTerm2 执行相同命令；Apple Silicon 和 Intel 的原生
Codex 发现路径均已适配。首次运行需先安装 Git、uv 和 Codex CLI 并完成本机
Codex 登录。安装、路径、安全边界和实机验收以
[macOS TUI 指南](docs/ones_dev_tui_macos.md) 为准。

首次启动会进入 MVP 全局配置页。填写 ONES 站点根地址、Team ID、缺陷 Issue Type ID、账号和密码，然后点击 **Save and open Dashboard**。Dashboard 首页只展示工作区列表和配置入口；点击 **Create workspace** 后从 ONES 选择 Project 和迭代，并添加一个或多个本地仓库或远程 Git 仓库。点击工作区进入详情，再从详情打开缺陷查询。Status ID 不属于全局配置，由工作区内的缺陷查询筛选器从 ONES 加载；Task/Defect ID 也不保存为全局配置，而是在查询结果中选择。TUI 使用本机 Codex CLI 的登录状态。MVP 的 run、mirror 和 worktree 根目录位于当前仓库同级的 `.ones-dev-runtime`；Windows 的应用配置和 Codex 缓存位于 `%LOCALAPPDATA%\ones-dev`，macOS 的应用配置位于 `~/Library/Application Support/ones-dev`，Codex 缓存位于 `~/Library/Caches/ones-dev/codex-runtime`。配置阶段只读访问 ONES，不创建 run、mirror、worktree，也不执行远端写操作。

### macOS Preview 边界

macOS 上的 ONES 凭据使用当前用户默认 Keychain/search list，Codex 操作
受原生可执行文件的来源、签名、权限和执行前重验保护。当前 MVP 以
`danger-full-access` 运行 Codex，不启用 OS sandbox；用户可主动添加可信的远程
Git 仓库进行只读 clone/fetch，但显式导入 Git 凭据及
commit/push/PR/ONES 评论发布均禁用。请只添加和运行可信仓库。

已有非交互环境配置仍可使用：

```powershell
uv run ones-dev defects list --project <project> --iteration <iteration> --assignee <assignee>
uv run ones-dev defect --project <project> --iteration <iteration> --assignee <assignee> --select <uuid>
```

## TUI 操作

- **Workspaces**：主界面；创建或打开工作区
- **Create workspace**：从 ONES 选择 Project 和迭代，添加多个本地/远程仓库
- **Query defects**：进入工作区详情后查询该 Project/迭代下的缺陷，并选择“AI 分析”或“分析并修复”
- **Configuration**：查看运行配置；**Edit global configuration** 重新配置 ONES 连接
- `?`：帮助，`q`：退出但不取消正在执行的任务

## 安全边界

配置向导只执行认证和读取 ONES 元数据。生产运行时通过本机 `codex` CLI 使用本机登录状态，并把分析和修复限制在隔离 worktree；连接失败显示固定类别，不回显响应正文或秘密。MVP 不接受 Provider token，也不会推送、创建 PR 或自动发布。上线检查清单见 `docs/ones_dev_tui_release_readiness.md`。

## 后端与工具入口

- `uv run ones-dev --help`：查看开发工作流命令和终端界面入口。
- `uv run python server.py`：启动 MCP 工具服务。
- `uv run python main.py --host 0.0.0.0 --port 8000`：启动 REST API 和 Webhook 服务。
- `uv run pytest`：运行后端测试。

网页前端已移除，无需 Node.js 或前端构建。HTTP 服务保留 `/docs`、`/redoc` 和 `/openapi.json` API 文档；根路径及原网页路由返回 404。

开发工作流说明见 [docs/ones_dev_cli.md](docs/ones_dev_cli.md)。
