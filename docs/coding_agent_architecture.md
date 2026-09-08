# Coding Agent 执行架构

工作流只依赖 `CodingAgentRunner` 协议，不依赖具体 CLI。公共执行层负责结构化结果、
仓库 HEAD 与快照、只读阶段、敏感信息扫描、活动记录和根因结果恢复；Provider 适配器
只负责可执行文件、环境白名单、参数、会话连续性和响应信封解析。

## 边界

- `coding_agents.py`：静态 Agent 目录、支持状态、本机发现和选择键。
- `coding_agent_runner.py`：Provider 无关的 Runner 协议、结果别名和异常层级。
- `codex_runner.py::GuardedCodingAgentRunner`：公共安全与证据实现。
- `codex_runner.py::CodexRunner`：Codex CLI 传输和 Codex 会话状态。
- `claude_runner.py::ClaudeRunner`：Claude Code 传输和 Claude 会话状态。
- `runtime_bootstrap.py`：按选择键构造 Provider；部署适配器使用
  `RuntimeAdapterBundle.coding_agent_factory`，旧 `codex_factory` 仅为兼容保留。
- `WorkflowRun.coding_agent`：保存任务创建时选择的 Agent 稳定键与显示名；旧任务为
  `None` 并在界面标记为未记录。该字段不保存本机可执行路径、凭据或其它环境信息。

`CodexRunner`、`CodexRequirementAdapter` 和原 Codex 异常名称继续导出，供旧调用方
迁移；新代码应使用 Coding Agent 名称。

结构化结果的主类型为 `CodingAgentResult`，`CodexResult` 是兼容别名。任务 JSON
暂时继续使用 `codex_results` 字段以保证旧任务可恢复；新读取代码使用
`WorkflowRun.coding_agent_results`。持久化字段只能通过独立版本迁移更名，不能直接
修改后导致已有任务不可加载。

自定义 `coding_agent_factory` 返回值按 `CodingAgentRunner` 协议进行结构化校验，
不要求继承 Codex 或公共实现基类。旧 `codex_factory` 保留原有宽松兼容行为。

## 接入新的 Agent

1. 在目录中登记 Agent，但未完成以下步骤前保持 `supported=False`。
2. 实现 `GuardedCodingAgentRunner`，只覆盖 `_invoke` 与三个会话状态方法。
3. 为 Provider 定义最小环境白名单，禁止继承无关令牌、Git 注入变量和交互认证。
4. 在运行时构造表中登记，并使用公共 `CodingAgentRequirementAdapter`。
5. 覆盖无交互执行、结构化输出、会话续接、超时、输出上限、只读变更拒绝、
   单/多仓库快照以及敏感信息清理测试。
6. 不得因 Provider 不支持某项能力而跳过公共 Git、测试、审查或发布审批门禁。

Agent 版本只有在可信执行边界能够采集时才写入；未知版本保持空值，不从路径、包装脚本
或模型输出推断。配置切换只影响之后创建的任务，已创建任务继续展示其原始 Agent 身份。
