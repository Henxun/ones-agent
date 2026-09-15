# 架构规范

本规范定义新增和修改代码的职责边界，不宣称历史代码已完全分层。后端、MCP 和 CLI/TUI 共存；重构采用逐步迁移，保留明确的兼容入口。

## 1. 模块职责

| 模块 | 职责 | 不应承担的职责 |
| --- | --- | --- |
| `main.py` | FastAPI 生命周期、API/Webhook 接入与服务组装 | 继续堆叠新的分析、仓库解析和 Git 业务规则 |
| `server.py`、`tools.json`、`skill.md` | MCP 工具实现、机器 schema 和使用契约 | 定义另一套业务状态或分析真相来源 |
| `src/services/` | 后端应用编排、ONES 归一化、仓库解析、证据整理、执行门禁 | UI 渲染、底层网络传输细节 |
| `src/core/` | 后端引擎、队列、调度及状态持久化 | 在调度器中复制分析或发布流程 |
| `src/integrations/` | ONES、Git、通知等外部系统适配 | 自行作出领域审批和流程推进决定 |
| `src/llm/` | Planner 规划、Analyzer 分析及提示词 | ONES 归一化、仓库解析、Git 写入 |
| `src/contracts.py` | 后端跨层中立契约 | 传输、存储或界面实现 |
| `src/developer_workflow/` | 隔离开发任务、分析/修复编排、审批、发布及恢复 | 绕过公共门禁建立快捷修复链 |
| `src/developer_workflow/tui/`、`cli.py` | 用户输入、命令路由、任务和状态展示 | 在按钮回调或命令入口复制业务门禁 |
| `src/utils/` | 日志、认证、审计、重试等通用能力 | 聚集业务工作流 |
| `config/settings.py` | 后端环境设置 | 保存工作流任务状态或明文凭据 |

## 2. 依赖与组装

- 接入层调用已有应用服务或工作流编排，再由其调用协议/适配器；存在服务边界时不得从入口直接绕到低层实现。
- 后端新 ONES 业务逻辑优先经过 `OnesGateway`；执行校验由 `ExecutionService` 承担，再进入 Git 适配。CLI/TUI 则沿用自身 repository、approval、publisher 边界，不强行合并两套运行时。
- 服务可协调 core、LLM、integrations 和 contracts；低层模块不得反向依赖 `main.py`、`server.py` 或界面。复用协议和构造注入解决依赖，避免循环导入与隐式全局实例。
- `src/contracts.py` 保持中立；工作流持久化契约放在 `src/developer_workflow/contracts.py`，可复用中立契约，不由中立层反向导入工作流。
- 运行时对象在入口或 `runtime_bootstrap.py` 等既有组装层创建。不要把具体 Provider 选择散落到各流程分支。

## 3. 分析与执行分离

- Planner 负责计划，Analyzer 负责分析；不能为了减少文件数量合并职责，也不能让 LLM 输出直接触发 Git 写入。
- `AnalysisResult` 是后端结构化分析记录；Markdown 只是同一结论的展示形式。证据不足时必须显式阻断可执行结论。
- 证据质量、置信度和可执行性遵循 [分析验收规则](analysis_acceptance_rules.md)，不能以缺陷描述、仓库匹配成功或文案质量代替代码证据。
- 定时扫描只负责发现、去重和触发，复用已有分析/修复流程及其审批门禁。
- 新 Coding Agent 接入遵循 [执行架构](coding_agent_architecture.md)：业务依赖 `CodingAgentRunner` 协议；公共执行层保留安全与证据检查，Provider 负责 CLI 传输、环境和会话适配。

## 4. 状态、审批与恢复

- 状态推进经现有编排和存储边界完成；明确前置条件、允许的操作、失败分类及恢复入口，不让 UI 单独改写持久化状态。
- 对重复请求、进程中断、多仓库及并发编辑保持幂等性和版本检查；保留审计历史，不覆盖旧证据。
- 任务身份、工作区稳定 ID 与显示名称分离；已有任务不能按当前配置倒推历史 Provider、凭据或审批身份。
- Draft PR 交接、合并就绪与发布就绪是不同阶段。未运行的外部验证可以按默认策略交接，但实际失败、快照不一致等必须阻断。
- 修改上述行为前阅读 [工作流长期约束](developer_workflow_constraints.md) 与 [PR 验证交接](developer_workflow_pr_verification_handoff.md)，补充新规则和旧任务恢复的回归测试。

## 5. 契约与兼容性

- MCP 工具名称、参数或语义变更必须同步 `server.py`、`tools.json`、`skill.md`，并检查工具测试。
- API 请求/响应、配置默认值、枚举、结构化结果 schema 与持久化字段都视为契约；变更说明受影响调用方和迁移办法。
- 保留历史兼容入口的边界和弃用信号；`/api/v1/ai/trigger` 仅为弃用兼容路径，新增能力使用规范的 defect/task 路由。
- 不以清理历史代码为由直接移除仍在使用的 Scheduler、ScheduleManager 或旧任务字段。迁移参考 [重构边界](ones_defect_refactor_boundaries.md) 和 [旧运行时保留决策](m7_legacy_runtime_retention.md)。历史阶段描述须结合当前代码核验。

## 6. 架构变更审查

涉及新服务、依赖方向、存储迁移、外部系统、状态机或安全门禁时，在 PR 或 `docs/`/`openspec/` 中记录：问题与目标、模块归属、契约变化、兼容与恢复方案、验证方式。OpenSpec 规范使用中文。

审查应能回答：业务规则由谁负责；是否出现重复流程或绕过门禁；旧配置与任务能否恢复；外部副作用如何授权与去重；失败是否可观测且可恢复。架构文档和实现应在同一变更中更新。
