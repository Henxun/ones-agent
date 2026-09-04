# macOS TUI 启动与验收

## 范围

TUI 使用同一套 Textual 界面；macOS Preview 适配配置路径、Keychain
凭据存储以及原生 Codex 发现、签名和私有缓存。Apple Silicon 使用
arm64 payload，Intel 使用 x86_64 payload；不执行 npm 的 JavaScript 启动器。

首版承诺可信仓库、ONES 查询、配置恢复、Codex 分析/修复、隔离 worktree 和
审核包。用户可主动添加远程 Git 仓库；只读 clone/fetch 可使用下文限定的本机
Git/SSH 认证环境，但显式导入 Git 凭据及 commit、push、PR、ONES 评论发布均禁用。
单元测试不能替代每台目标 Mac 上的 Keychain、codesign、终端交互和 Codex 登录验收。

## 启动

1. 安装 Python 3.11+、uv、Git。确认终端中 `git --version` 和 `uv --version` 可用。
2. 按 [OpenAI Codex CLI 文档](https://learn.chatgpt.com/docs/codex/cli) 安装 Codex，
   在本机终端完成登录并确认 `codex --version` 可运行。支持原生可执行文件、
   npm 原生平台包，以及 Homebrew 链接最终指向的原生文件。
3. 将 ones-agent 放到当前用户可写的目录，在该项目根目录执行：

```sh
uv sync
uv run ones-dev tui
```

建议终端至少 120 列 × 32 行并使用支持中文的等宽字体；小窗口可滚动操作。
不需要 Windows、PowerShell 或管理员权限。应用仍会校验本机 Codex 认证来源；
现有认证契约读取 `CODEX_HOME/auth.json`（未指定时为 `~/.codex/auth.json`），
仅存在 Codex Keychain 登录、没有此文件时不能视为已经通过该契约。
不要复制其他人的凭据，也不要把认证内容填写进命令行。

## 数据与安全

| 数据 | macOS 位置 / 行为 |
| --- | --- |
| 非敏感配置 | `~/Library/Application Support/ones-dev/config.json` |
| ONES 凭据 | 当前用户默认 Keychain/search list，服务 `ones-dev.credentials`，按 profile / generation 分隔 |
| 原生程序缓存 | `~/Library/Caches/ones-dev/codex-runtime/<SHA-256>/codex` |
| 默认工作流数据 | 项目所在目录的上一级 `.ones-dev-runtime`，以界面显示的配置为准 |

默认路径从启动 TUI 的当前用户 home 解析，任务不能在执行时覆盖已解析路径；
私有目录 0700、配置文件
0600、缓存可执行文件 0700。Keychain 锁定、访问被拒绝或条目损坏时会
fail-closed，不会回退成明文文件。当前 Python console-script 宿主不声明
Data Protection Keychain 的 `ThisDeviceOnly` 语义。
不要将 Windows 配置中的盘符路径直接复制到 Mac；仓库与节点路径应在 Mac 重新配置。

原生程序须为 Mach-O，且通过 Apple 信任链和 OpenAI 组织签名校验；缓存继续绑定
内容 SHA-256、文件身份、manifest，并在执行前重验。不支持无签名/临时签名的
自编译 Codex，不以关闭 Gatekeeper、重签名或跳过校验作为解决办法。安装路径不能
位于任务仓库内，也不能允许其他用户写入。已有 code-mode companion 同样须验签。

当前 TUI 允许用户主动添加远程 Git 仓库。未配置显式 askpass 时，只读 clone/fetch
可使用受约束的 `osxkeychain` credential helper；SSH 只读取经过安全校验的
`~/.ssh/known_hosts` 以及 `id_ed25519`、`id_ecdsa`、`id_rsa`，禁用 SSH config、
agent 和交互提示。显式导入 Git 凭据尚未启用，commit、push、PR 或 ONES 评论发布
仍由平台 capability gate 禁用；上述只读认证合同不代表发布能力已启用。

## 运行时安全边界

AI 分析和修复前会检查 Codex 可执行文件的来源、Mach-O 架构、OpenAI 签名、
文件权限、内容哈希与私有缓存身份，并在每次执行前重验。任一检查失败时
只阻断需要 Codex 的动作，返回固定安全错误；TUI 的只读配置入口仍可使用。

当前 MVP 显式以 `danger-full-access` 运行 Codex，没有 macOS OS sandbox 或
direct-network 隔离保证。worktree 隔离、`shell=False`、超时和输出上限是防御边界，
但不等价于 OS sandbox；因此只能添加和运行可信仓库，包括用户主动添加的远程仓库。

## 实机验收清单

- `uv sync` 成功，`uv run ones-dev tui` 打开配置页，不出现 Windows API 错误。
- 配置 ONES 后保存，退出并重新打开，配置可恢复且密码不回显；Keychain 拒绝访问时提示失败。
- 创建工作区，添加可信的本地或远程仓库，查询缺陷并浏览 Configuration 各 tab，
  中文和滚动正常；若需验证私有远程连接，必须在目标 Mac 上另行实测。
- 本机已登录 Codex 的情况下，启动一次只读分析，确认原生缓存、签名检查和退出清理成功。
- 确认发布操作在 UI 中明确禁用，且不会执行 commit、push、PR 或 ONES 评论。
- 在 Apple Silicon / Intel 各自执行平台测试：

```sh
uv run pytest -q \
  tests/test_developer_workflow_macos.py \
  tests/test_developer_workflow_host_platform.py \
  tests/test_developer_workflow_macos_codex_runtime.py \
  tests/test_developer_workflow_macos_credential_store.py
```

当前定向自动化证据为上述四个测试文件 `40 passed`，覆盖 native vault 与 SSH 环境
合同；该证据不表示已经连接并验证真实私有 Git 服务器。

人工/实机验证待完成仍可按既有规则交接 Draft PR；这不代表验证已通过，也不授予合并/发布权限。
