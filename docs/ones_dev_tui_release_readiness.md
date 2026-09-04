# ONES Dev TUI 上线检查

## 功能矩阵

| 能力 | 当前状态 | 入口 |
| --- | --- | --- |
| ONES 地址、团队、账号密码 | 已完成 | 配置向导的 ONES 步骤可独立打开；项目/状态/Work item 仅为可选只读探测 |
| Git provider、仓库和 Codex | 已完成 | Provider、Repositories、Private paths 可独立配置；Codex 直接使用本机 CLI，不再进入配置向导 |
| 多文件夹工作区 | 已完成 | Workspace folders：新增、选择编辑、移除、仓库组 |
| 获取缺陷 | 已完成 | Defects 列表按项目、迭代、负责人和状态查询；列表项直接启动分析 |
| 获取需求 | 已完成 | Requirements 列表按项目、迭代、负责人、状态和需求类型查询；列表项直接启动分析，仍保留 ID 入口 |
| 分析、实现、修复 | 已完成 | 列表启动后进入隔离工作流；分析完成后使用 `Revise → implementation/repair` 进入实现或修复 |
| 审核和恢复 | 已完成 | Dashboard 的显式确认、审核包、恢复和取消动作；MVP 不执行发布 |
| macOS 仓库 Preview | 受限可用 | TUI 启动、本地仓库及用户主动添加的可信远程仓库、ONES 查询、配置恢复和 Dashboard；AI 分析/修复、隔离 worktree 执行及审核包带执行时 Codex trust 门禁 |
| macOS 远程 Git 认证 / 发布 | 部分启用 / 禁用 | 只读 clone/fetch 可使用受约束的 `osxkeychain` 或限定 SSH 环境；显式 Git 凭据导入及 commit/push/PR/ONES 评论禁用 |
| macOS OS sandbox 等价能力 | 未承诺 | Codex 以 `danger-full-access` 运行；不将 worktree 隔离表述为 managed sandbox 或 direct-network 保证 |

## 发布前验证

```powershell
$tuiTests = Get-ChildItem .\tests -File -Filter 'test_developer_workflow_tui*.py' |
    ForEach-Object { $_.FullName }
uv run pytest $tuiTests -q --basetemp="$PWD\.pytest-tmp"
uv build --wheel --sdist --out-dir .tmp/dist-check
uv run ones-dev --help
```

发行包必须包含 `src/developer_workflow/tui/tui.tcss`，不得包含 `.env`、用户凭据、`data/`、测试文件或内部 `AGENTS.md` 指引。

## 环境要求

生产 Windows 环境必须满足：

- 用户配置目录具备受保护 ACL；
- Windows Credential Manager 可用；
- Codex CLI、managed sandbox profile 和完整能力探测可用；可先运行 `codex doctor --summary --no-color`，再用内置 profile 执行一次受限命令：`codex -c 'permissions.ones-dev-workspace.extends=":workspace"' sandbox -P ones-dev-workspace --include-managed-config -C <worktree> -- C:\\Windows\\System32\\cmd.exe /c echo ok`；
- ONES 与 Git provider 使用受信 TLS 端点；
- 运行根、镜像根和 worktree 根均为私有目录。

macOS Preview 环境必须满足：

- 当前用户默认 Keychain/search list 可读写；凭据不得回退到配置 JSON 或其它明文文件；当前 Python console-script 宿主不声明 Data Protection Keychain 的 `ThisDeviceOnly` 语义；
- 非敏感应用配置位于 `~/Library/Application Support/ones-dev`，Codex 缓存位于 `~/Library/Caches/ones-dev/codex-runtime`，且目录和文件只允许当前用户访问；
- AI 分析或修复前，本机 `codex` CLI 必须已安装并完成登录；可执行文件缺失、来源、签名或权限检查失败时，动作必须返回固定安全错误且不启动 Codex，但不能阻止 TUI 的只读配置入口；
- AI 分析/修复执行时必须重新校验受信 Codex runtime；没有通过证据时必须报告不可用，不得信任任意 PATH wrapper；
- 用户可主动添加可信远程仓库；未配置显式 askpass 时，只读 clone/fetch 可使用
  受约束的 `osxkeychain` credential helper，SSH 只使用安全校验后的
  `~/.ssh/known_hosts` 与 `id_ed25519`、`id_ecdsa`、`id_rsa`，并禁用 SSH config、
  agent 和交互；显式 Git 凭据导入及 commit、push、PR、ONES 评论发布仍禁用；
- 当前 MVP 以 `danger-full-access` 运行 Codex，不启用 OS sandbox；只允许添加和运行
  可信仓库。现有 `40 passed` 定向测试覆盖 native vault/SSH 环境合同，不代表已验证
  真实私有 Git 服务器连接。

其余 macOS 路径、Keychain 合同和实机步骤以
[macOS TUI 启动与验收](ones_dev_tui_macos.md) 为准。

私有目录权限或 Keychain 检查失败时，TUI 必须停留在受限配置/恢复界面；Codex 安全检查失败时，仅阻断需要 Codex 的动作并显示固定安全错误。两类失败都不能降级为明文凭据、任意可执行脚本或未隔离运行。

## ONES 连接排查

- Base URL 填站点根地址，不要填写 `/identity/api` 或 `/project/api` 等接口路径。
- Team ID 和缺陷类型 ID 必须来自当前 ONES 团队；账号必须具备读取项目元数据的权限。
- Project、Status、Work item ID 只用于可选只读探测，可以全部留空。
- 认证失败检查账号密码；主机不可达检查网络/DNS；TLS 失败检查证书；响应不兼容检查站点版本和 Base URL。
