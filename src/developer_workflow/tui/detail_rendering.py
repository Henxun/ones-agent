"""Readable task reports from credential-free views, without workflow mutations."""
from __future__ import annotations

from io import StringIO

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..contracts import WorkflowState, WorkflowType
from .models import RepositoryView, RunDetail


STATES = {
    "CREATED": "已创建", "READING_ONES": "读取 ONES", "VALIDATING": "校验仓库映射",
    "PREPARING_REPO": "准备代码仓库", "IMPLEMENTING": "代码修复 / 实现", "TESTING": "执行测试",
    "AI_REVIEW": "代码审查", "WAITING_APPROVAL": "等待人工审批", "PUBLISHING": "正在发布",
    "COMPLETED": "已完成", "BLOCKED": "已暂停", "CANCELLED": "已取消",
    "WAITING_PR_VERIFICATION": "等待 PR 人工验证",
    "PARTIAL_SUCCESS": "部分成功", "FAILED": "执行失败",
}
OUTCOMES = {
    "passed": ("通过", "green"), "test_failed": ("测试失败", "red"),
    "command_error": ("命令执行异常", "red"), "timeout": ("执行超时", "yellow"),
    "sandbox_error": ("隔离环境异常", "red"),
}
MESSAGES = {
    "workflow blocked safely": "流程已安全暂停，尚未完成当前阶段。",
    "workflow stopped safely": "流程已安全暂停，请检查当前阶段与相关记录。",
    "publication failed safely": "发布操作未完成，请检查各仓库的发布记录。",
    "The code review found no further repair to apply, but external or platform validation is still missing. See the Review tab; publication remains blocked.":
        "代码审查未发现需要继续修复的问题，但仍缺少环境或平台验证。完成验证前不能发布。",
    "Review found unresolved issues; see the Review tab. Publication is blocked.":
        "审查发现尚未解决的问题，请查看 Review 中的待处理项；当前不能发布。",
    "Review evidence is incomplete; see the Review tab before continuing.":
        "审查证据不完整，请先查看 Review 中缺失的证据。",
    "Automatic repair paused: code/test content is unchanged and review still has unresolved issues. See Review for the remaining issues; explicit direction is needed.":
        "自动回修未取得进展：代码与测试未变化，审查仍有待处理问题，需要补充处理方向。",
    "Automatic review/repair attempt limit reached. See Review for remaining issues and provide direction.":
        "已达到本轮自动回修上限，请查看审查问题并确认后续处理方向。",
    "Code changes were not accepted as a completed repair; repair scope, reported evidence, or the frozen reproduction test did not match.":
        "修复尚未被验收：修改范围、报告证据或冻结复现测试不匹配。",
    "Codex analysis returned invalid structured output": "分析结果未通过格式校验。",
    "Codex analysis timed out": "分析执行超时。",
    "Codex process could not be started": "无法启动分析进程。",
    "Codex analysis exited unsuccessfully": "分析进程异常退出。",
    "Codex runtime safety validation failed": "分析运行环境未通过安全校验。",
    "coding agent analysis returned invalid structured output": "分析结果未通过格式校验。",
    "coding agent result format repair failed": "分析结果格式修复失败。",
    "coding agent analysis timed out": "分析执行超时。",
    "coding agent process could not be started": "无法启动分析进程。",
    "coding agent analysis exited unsuccessfully": "分析进程异常退出。",
    "coding agent runtime safety validation failed": "分析运行环境未通过安全校验。",
    "Repository safety validation failed": "代码仓库未通过安全校验。",
    "waiting for repository selection": "等待选择并确认代码仓库。",
    "Current checkout verified; no production repair or publication. See Review for limitations.":
        "当前代码已完成本地验证；本次没有生产代码修复或发布，验证限制请查看 Review。",
    "Review-driven corrections verified locally; no publication. See Review for limitations.":
        "审查反馈对应的修正已通过本地验证；本次不发布，验证限制请查看 Review。",
}


class DetailReport(Group):
    """Rich report with a readable text representation for diagnostics/accessibility."""

    @property
    def plain(self) -> str:
        output = StringIO()
        Console(file=output, width=180, color_system=None).print(self)
        return output.getvalue()

    def __str__(self) -> str:
        return self.plain


def literal(value: str, style: str = "") -> Text:
    # Views already escape Rich '[' markers. Decode that escaping once and
    # render as literal Text, never interpret data as markup or terminal links.
    return Text(value.replace("\\[", "["), style=style, overflow="fold")


def state_name(value: str) -> str:
    return f"{STATES[value]} · {value}" if value in STATES else value


def message(value: str) -> str:
    return MESSAGES.get(value, value)


def card(title: str, body, color: str = "cyan") -> Panel:
    return Panel(body, title=literal(title, f"bold {color}"), title_align="left",
                 border_style=color, padding=(1, 1))


def fields(*rows: tuple[str, str]) -> Table:
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(style="dim", width=12)
    table.add_column(ratio=1, overflow="fold")
    for key, value in rows:
        table.add_row(Text(key), literal(value or "暂无记录"))
    return table


def next_step(detail: RunDetail) -> str:
    state = detail.summary.state
    if state is WorkflowState.BLOCKED and detail.status_message.startswith("远程目标分支已变化"):
        return "点击继续将尝试自动更新基线：保留旧工作区、迁移已有修复，再测试和审查；不会自动推送或覆盖旧修复。"
    if state is WorkflowState.BLOCKED and detail.status_message.startswith(("自动基线更新", "基线迁移未完成", "基线冲突尚未解决")):
        return "查看暂停说明与基线迁移记录；不要把迁移失败改成人工验证待办，也不要跳过新基线的测试和审查。"
    if state is WorkflowState.BLOCKED and detail.status_message.startswith(("Git 命令执行失败", "流程内部检查失败")):
        return "查看暂停说明并排查具体失败；待人工验证清单本身不阻止默认 Draft PR 交接。处理后重新检查审批条件。"
    if state is WorkflowState.BLOCKED and detail.status_message.startswith("缺少修复前失败复现记录"):
        return "点击“重新检查审批条件”，核对当前代码与测试快照，再生成带证据缺口说明的 Draft PR 审批包；无需重复 Review。"
    if detail.can_accept_analysis:
        return "确认分析方案后，点击 Accept solution 进入修复。"
    if state is WorkflowState.WAITING_PR_VERIFICATION:
        return "Draft PR 已交付。请在 PR 对应提交上完成人工验证并回填证据；未通过前不要转为可合并或发布。"
    if detail.can_defer_verification:
        return "可将待执行验证转交 PR 审核，先生成 Draft PR 审批包；也可在环境验证中先完成验证。"
    if detail.can_verify:
        return "点击“环境验证”，核对匹配节点与验收标准；没有合适环境时可配置节点或记录真实人工验证证据。"
    if state is WorkflowState.BLOCKED:
        if detail.can_request_review_repair:
            return "查看 Review 中的剩余问题，补充方向并确认续修。"
        if detail.review_report and detail.review_report.blockers:
            return "先处理 Review 中的待修复问题，再测试并重新审查。"
        if detail.review_report and detail.review_report.external_validation:
            return "查看 Review 中的外部验证要求，完成相应环境验证；本地测试通过不代表实机验证通过。"
        return "查看下方暂停说明及相关页签，处理原因后再继续当前阶段。"
    if state is WorkflowState.WAITING_APPROVAL:
        if detail.draft_pr:
            return "审批仅授权提交、推送并创建 Draft PR；人工验证清单仍待处理，不授权合并或发布。"
        return "核对测试、审查和发布范围后，由操作者确认审批。"
    if state is WorkflowState.PARTIAL_SUCCESS:
        return "查看 Publication 中每个仓库的结果，核对已完成的操作后再处理剩余发布项。"
    if state is WorkflowState.COMPLETED:
        return "本次流程已结束。是否已推送、创建 PR 或回写 ONES，请以 Publication 的独立记录为准。"
    if state in {WorkflowState.CANCELLED, WorkflowState.FAILED}:
        return "查看历史及执行记录，确认原因后再决定是否重新运行。"
    return "查看 AI activity 了解执行进度；测试、审查与发布证据会随流程更新。"


def overview(detail: RunDetail) -> DetailReport:
    s = detail.summary
    color = "yellow" if s.state in {WorkflowState.BLOCKED, WorkflowState.PARTIAL_SUCCESS} else (
        "red" if s.state is WorkflowState.FAILED else "green" if s.state is WorkflowState.COMPLETED else "cyan")
    heading = literal(s.work_item_id, "bold")
    heading.append(f"\n{state_name(s.state.value)}", style=f"bold {color}")
    heading.append(f"\n风险项：{detail.risk_count}  ·  未解决项：{detail.unresolved_count}")
    if detail.review_report:
        heading.append(f"\n代码问题：{len(detail.review_report.blockers)}  ·  外部验证：{len(detail.review_report.external_validation)}")
    status = detail.status_message or detail.blocked_reason
    sections = []
    if s.workflow_type is WorkflowType.DEFECT:
        defect = detail.defect_info
        if defect is None:
            sections.append(card("缺陷信息", Group(
                fields(("缺陷标识", s.work_item_id)),
                Text("尚未读取或保存缺陷详情，当前仅有任务标识。", style="dim")), "bright_black"))
        else:
            body = [literal(defect.title or "未提供缺陷标题", "bold cyan"),
                fields(("缺陷编号", defect.number or "未提供"), ("缺陷 ID", defect.defect_id),
                       ("ONES 状态", defect.status), ("优先级", defect.priority),
                       ("所属项目", defect.project), ("负责人", defect.assignee or "未记录负责人")),
                Text("\n缺陷描述", style="bold"), literal(defect.description or "未提供缺陷描述。"),
                Text("\n以上为任务读取时保存的 ONES 快照，不代表实时状态。", style="dim")]
            if defect.updated_at:
                body.append(literal("记录更新时间：" + defect.updated_at, "dim"))
            sections.append(card("缺陷信息", Group(*body)))
    sections.append(card("任务概览", heading, color))
    if status:
        sections.append(card("暂停说明" if s.state is WorkflowState.BLOCKED else "当前说明", literal(message(status)), color))
    sections.append(card("下一步", Text(next_step(detail)), color))
    if detail.baseline_refresh_history:
        sections.append(card("基线更新记录 · 旧工作区保留", Text("\n\n".join(detail.baseline_refresh_history)), "cyan"))
    sections.append(card("任务信息", fields(
        ("任务类型", "缺陷" if s.workflow_type.value == "defect" else "需求"),
        ("任务 ID", s.run_id), ("状态版本", str(s.version)),
        (
            "Coding Agent",
            (
                f"{detail.coding_agent_label} ({detail.coding_agent_key})"
                if detail.coding_agent_label
                else "旧任务未记录"
            ),
        ),
        (
            "Agent 版本",
            detail.coding_agent_version or "尚未采集",
        ),
        ("更新时间", s.updated_at.isoformat(sep=" ", timespec="seconds")),
        ("恢复阶段", state_name(detail.resume_state.value) if detail.resume_state else "无待恢复阶段"),
        ("审批指纹", detail.fingerprint or "尚未生成"),
    ), "bright_black"))
    return DetailReport(*sections)


def repository_card(item: RepositoryView, *, publication: bool = False) -> Panel:
    role = {"primary": "主仓库", "dependency": "依赖仓库"}.get(item.role, item.role)
    rows = [("仓库角色", role)]
    if not publication:
        rows.extend([("基线提交", item.base_commit), ("当前提交", item.head_commit), ("代码树摘要", item.tree_hash)])
    rows.extend([("发布提交", item.commit_hash or "尚未记录"), ("推送状态", "已推送" if item.pushed else "未记录推送成功"),
                 ("PR 地址", item.pr_url or "尚未记录"), ("目标分支", item.pr_target or "暂无记录")])
    parts = [fields(*rows)]
    if not publication:
        files = Text(f"变更文件：{item.changed_file_count} 个\n", style="bold")
        if item.changed_files:
            for path in item.changed_files:
                files.append_text(literal(f"• {path}\n", "cyan"))
        else:
            files.append("暂无文件路径记录", style="dim")
        if item.changed_file_count > len(item.changed_files):
            files.append(f"另有 {item.changed_file_count - len(item.changed_files)} 个文件未在此列出。", style="dim")
        parts.append(files)
    if item.error:
        parts.append(literal("异常：" + message(item.error), "red"))
    return card(item.key, Group(*parts), "red" if item.error else "cyan")


def repositories(detail: RunDetail) -> DetailReport:
    if not detail.repositories:
        return DetailReport(card("代码仓库", Text("暂无仓库证据。选择并准备仓库后，这里会显示代码快照与变更文件。"), "bright_black"))
    return DetailReport(card("仓库概况", Text(f"{len(detail.repositories)} 个仓库 · {sum(r.changed_file_count for r in detail.repositories)} 个变更文件")),
                        *(repository_card(item) for item in detail.repositories))


def tests(detail: RunDetail) -> DetailReport:
    if not detail.tests:
        return DetailReport(card("测试记录", Text("尚无已记录的测试结果。没有测试证据不代表测试通过。"), "bright_black"))
    passed = sum(t.outcome == "passed" and t.exit_code == 0 for t in detail.tests)
    sections = [card("测试概况", Text(f"{len(detail.tests)} 条命令记录 · {passed} 条通过 · {len(detail.tests) - passed} 条需检查\n"
        "这里统计的是命令结果，不是测试用例数；也不替代环境验证或代码审查。"), "green" if passed == len(detail.tests) else "yellow")]
    for i, item in enumerate(detail.tests, 1):
        label, color = OUTCOMES.get(item.outcome, (item.outcome, "yellow"))
        if item.outcome == "passed" and item.exit_code != 0:
            label, color = "结果与退出码不一致，需核查", "yellow"
        command = "测试命令（安全视图未展示原始参数）" if item.command == "test command" else item.command
        sections.append(card(f"{i:02d} · {label}", fields(("命令", command), ("结果标识", item.outcome), ("退出码", str(item.exit_code))), color))
    return DetailReport(*sections)


def publication(detail: RunDetail) -> DetailReport:
    record = detail.publication
    items = record.repositories
    body = fields(("已记录提交", f"{sum(bool(r.commit_hash) for r in items)} / {len(items)} 个仓库"),
        ("已记录推送", f"{sum(r.pushed for r in items)} / {len(items)} 个仓库"),
        ("已记录 PR", f"{sum(bool(r.pr_url) for r in items)} / {len(items)} 个仓库"),
        ("ONES 回写", "已送达" if record.comment_id else "未记录送达"))
    parts = [card("发布记录", body, "red" if record.error else "cyan")]
    if detail.draft_pr:
        parts.append(card("Draft PR · 人工验证待处理", Text(
            "待验证项随 PR 交付，不视为测试通过。仓库须配置必需检查 ones-agent/external-verification；"
            "本工具不自动合并、发布或修改分支保护。"), "yellow"))
    if record.error:
        parts.append(card("发布异常", literal(message(record.error)), "red"))
    if detail.review_report and detail.review_report.verification_only:
        parts.append(card("发布范围", Text("本次仅验证当前代码，不执行发布。"), "yellow"))
    elif not any(r.commit_hash or r.pushed or r.pr_url for r in items) and not record.comment_id:
        parts.append(card("尚无发布成功记录", Text("分析、修复或测试完成，不等于已提交、推送或创建 PR。"), "bright_black"))
    parts.extend(repository_card(item, publication=True) for item in items)
    return DetailReport(*parts)


def history(detail: RunDetail) -> DetailReport:
    if not detail.history:
        return DetailReport(card("流程历史", Text("暂无状态变更记录。后续状态迁移将按发生顺序展示。"), "bright_black"))
    rows = []
    for i, item in enumerate(detail.history, 1):
        line = Text()
        line.append(f"{i:02d}  {item.occurred_at.isoformat(sep=' ', timespec='seconds')}\n", style="dim")
        line.append_text(literal(f"{state_name(item.source)}\n  → {state_name(item.target)}\n"))
        rows.append(line)
    return DetailReport(card(f"流程历史 · {len(rows)} 次状态变更", Group(*rows)))
