"""Textual screens for the read-only workflow dashboard."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import re
import uuid
from typing import Literal
import unicodedata
from urllib.parse import urlsplit

from rich.text import Text
from rich.console import Group
from rich.panel import Panel
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)

from ..contracts import WorkflowState, WorkflowType
from .schedule_settings import SchedulePane
from .controller import (
    PublicationConfigurationError,
    StaleCandidateError,
    StaleTuiActionError,
    TuiController,
    TuiControllerError,
)
from .models import (
    DangerousActionRequest,
    DefectChoice,
    FilterChoice,
    MappingCandidateView,
    RequirementChoice,
    RepositoryView,
    ReviewView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TuiDisplayError,
    WorkspaceRepositoryInput,
    WorkspaceSummary,
    validate_tui_input_text,
)
from .supervisor import RunTaskSupervisor
from .verification_modal import VerificationModal, VerificationSubmission
from .verification_settings import VerificationNodesPane
from .ones_settings import OnesSettingsPane
from .provider_settings import ProviderSettingsPane
from .coding_agent_settings import CodingAgentSettingsPane
from . import detail_rendering
from ..verification import public_text


_DETAIL_TABS = (
    "overview",
    "ai-activity",
    "repositories",
    "tests",
    "review",
    "publication",
    "history",
)
_STORAGE_CORRUPTED = "workflow storage is corrupted safely"
_LIST_UNAVAILABLE = "workflow list is unavailable safely"
_DISPLAY_UNAVAILABLE = "workflow display is unavailable safely"
_WIZARD_UNAVAILABLE = "workflow wizard action failed safely"
_CANDIDATE_STALE = "candidate selection is no longer valid"
_MAPPING_REQUIRED = "repository mapping selection is invalid"
_NO_MAPPINGS = "no authorized repository mappings available"
_INPUT_REQUIRED = "required workflow fields are missing"
_NO_CANDIDATES = "no defect candidates available"
_ACTION_UNAVAILABLE = "workflow action is unavailable"
_ACTION_FAILED = "workflow action failed safely"
_ACTION_STALE = "workflow changed; review again"
_ACTION_INPUT_REQUIRED = "required action fields are missing"


def _workflow_phase(detail: RunDetail) -> str:
    if detail.summary.state is WorkflowState.COMPLETED and detail.status_message:
        return detail.status_message
    if detail.summary.state is WorkflowState.BLOCKED:
        return detail.status_message or "workflow stopped safely"
    if (
        detail.summary.state is WorkflowState.VALIDATING
        and detail.mapping_candidates
    ):
        return "waiting for repository selection"
    state = (
        detail.resume_state
        if detail.summary.state is WorkflowState.BLOCKED
        and detail.resume_state is not None
        else detail.summary.state
    )
    return {
        WorkflowState.CREATED: "creating analysis run",
        WorkflowState.READING_ONES: "reading ONES defect details",
        WorkflowState.VALIDATING: "validating repository mapping",
        WorkflowState.PREPARING_REPO: "preparing isolated repositories",
        WorkflowState.IMPLEMENTING: "AI root-cause analysis in progress",
        WorkflowState.TESTING: "verifying repository evidence",
        WorkflowState.AI_REVIEW: "reviewing analysis evidence",
        WorkflowState.WAITING_APPROVAL: "waiting for approval",
        WorkflowState.PUBLISHING: "publishing approved changes",
        WorkflowState.COMPLETED: "workflow completed",
        WorkflowState.WAITING_PR_VERIFICATION: "Draft PR 已交付，等待 PR 人工验证",
        WorkflowState.BLOCKED: "workflow stopped safely",
        WorkflowState.CANCELLED: "workflow cancelled",
        WorkflowState.FAILED: "workflow failed safely",
    }.get(state, state.value)


def _workflow_progress_text(
    detail: RunDetail, activity: tuple[str, ...] = ()
) -> str:
    lines = [
        f"phase: {_workflow_phase(detail)}",
        f"state: {detail.summary.state.value}",
        f"version: {detail.summary.version}",
    ]
    lines.extend(
        f"{item.source} -> {item.target}  {item.occurred_at.isoformat()}"
        for item in detail.history[-4:]
    )
    if activity:
        lines.append("")
        lines.append("AI activity:")
        lines.extend(f"  {item}" for item in activity[-12:])
    return "\n".join(lines)


_POWERSHELL_COMMAND = re.compile(
    r'^"[^"]*powershell(?:\.exe)?"\s+-Command\s+"(?P<command>.*)"$',
    re.IGNORECASE,
)
_COMMAND_EXIT = re.compile(r"\s+\(exit\s+(?P<code>-?\d+)\)\s*$")


def _compact_activity_command(command: str) -> str:
    """Hide the repetitive shell launcher while preserving the real command."""

    match = _POWERSHELL_COMMAND.fullmatch(command.strip())
    return match.group("command") if match is not None else command.strip()


def _ai_activity_renderable(activity: tuple[str, ...]) -> Text:
    """Build a readable, styled activity stream from sanitized runtime events."""

    rendered = Text()
    rendered.append("AI ANALYSIS\n", style="bold cyan")
    rendered.append(
        "Live repository investigation and reasoning\n\n",
        style="dim",
    )
    for event in activity:
        line = event.strip()
        if line.startswith("Running: "):
            command = _compact_activity_command(line.removeprefix("Running: "))
            rendered.append("› ", style="bold cyan")
            rendered.append("Running: ", style="bold cyan")
            rendered.append(command, style="cyan")
        elif line.startswith("Command completed: "):
            command = line.removeprefix("Command completed: ")
            exit_match = _COMMAND_EXIT.search(command)
            exit_code = int(exit_match.group("code")) if exit_match else None
            if exit_match is not None:
                command = command[: exit_match.start()]
            command = _compact_activity_command(command)
            successful = exit_code == 0
            rendered.append("✓ " if successful else "✕ ", style=(
                "bold green" if successful else "bold red"
            ))
            rendered.append("Command completed: ", style="bold")
            rendered.append(command)
            if exit_code is not None:
                rendered.append(
                    f"  [exit {exit_code}]",
                    style="green" if successful else "red",
                )
        elif line.startswith("Analysis result: "):
            rendered.append("◆ Analysis result: ", style="bold magenta")
            rendered.append(line.removeprefix("Analysis result: "))
        elif line.startswith("Verified root cause: "):
            rendered.append("ROOT CAUSE  ", style="bold magenta")
            rendered.append(line.removeprefix("Verified root cause: "))
        elif line.startswith("Recommended fix "):
            label, _, value = line.partition(": ")
            rendered.append(label.upper() + "  ", style="bold green")
            rendered.append(value)
        elif line.startswith("Validation: "):
            rendered.append("VALIDATION  ", style="bold cyan")
            rendered.append(line.removeprefix("Validation: "))
        elif line.startswith("Next investigation "):
            label, _, value = line.partition(": ")
            rendered.append(label.upper() + "  ", style="bold yellow")
            rendered.append(value)
        elif line == "Codex session started":
            rendered.append("● Codex session started", style="bold blue")
        elif line == "AI analysis started":
            rendered.append("● AI analysis started", style="bold cyan")
        elif line.startswith("AI analysis completed"):
            rendered.append(f"✓ {line}", style="bold green")
        elif line.startswith("Structured analysis result validated"):
            rendered.append(f"✓ {line}", style="bold green")
        elif line.startswith("Structured result needs correction"):
            rendered.append(f"↻ {line}", style="bold yellow")
        elif line.startswith("Correcting structured analysis result"):
            rendered.append(f"↻ {line}", style="bold yellow")
        elif "mismatch" in line.casefold() or "failed" in line.casefold():
            rendered.append(f"! {line}", style="bold red")
        elif line.startswith("Preparing "):
            rendered.append(f"● {line}", style="blue")
        else:
            rendered.append(f"· {line}", style="dim")
        rendered.append("\n")
    return rendered


_SAFE_MAPPING_KEY = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def _repository_name_from_source(
    source: str,
    existing: tuple[WorkspaceRepositoryInput, ...],
) -> str:
    """Derive a stable safe repository name from a local path or remote URL."""

    normalized = source.replace("\\", "/").rstrip("/")
    if "://" in normalized:
        normalized = urlsplit(normalized).path.rstrip("/")
    elif re.match(r"[^/@:]+@[^/:]+:", normalized):
        normalized = normalized.split(":", 1)[1].rstrip("/")
    leaf = normalized.rsplit("/", 1)[-1]
    if leaf.casefold().endswith(".git"):
        leaf = leaf[:-4]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", leaf).strip("._-")
    if not base:
        base = "repository"
    if base.casefold().endswith(".lock"):
        base = f"{base[:-5]}-repository".strip("-") or "repository"
    base = base[:128].rstrip("._-") or "repository"

    occupied = {item.key.casefold() for item in existing}
    candidate = base
    suffix = 2
    while candidate.casefold() in occupied:
        marker = f"-{suffix}"
        candidate = f"{base[: 128 - len(marker)].rstrip('._-')}{marker}"
        suffix += 1
    return candidate


def _workspace_key_from_scope(
    project_id: str,
    iteration_id: str,
    preferred_name: str = "",
) -> str:
    """Build a safe internal key from a friendly name or the ONES scope."""

    raw = preferred_name or f"{project_id}-{iteration_id}"
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("._-")
    if not key:
        raw = f"{project_id}-{iteration_id}"
        key = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("._-")
    if not key:
        key = "workspace"
    if key.casefold().endswith(".lock"):
        key = f"{key[:-5]}-workspace".strip("-") or "workspace"
    return key[:128].rstrip("._-") or "workspace"


@dataclass(frozen=True, slots=True)
class SettingsView:
    """A deliberately small, credential-free settings projection."""

    max_concurrency: int
    provider_type: str
    sandbox_configured: bool
    root_labels: tuple[str, str, str] = field(
        default=(
            "private run root",
            "private mirror root",
            "managed worktree root",
        ),
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        if self.provider_type not in {"github", "gitlab", "local_fake", "configured"}:
            raise ValueError("provider type is invalid")
        if type(self.sandbox_configured) is not bool:
            raise ValueError("sandbox configuration state is invalid")

    def display_text(self) -> str:
        roots = ", ".join(self.root_labels)
        sandbox = "configured" if self.sandbox_configured else "not configured"
        return (
            f"max concurrency: {self.max_concurrency}\n"
            f"storage labels: {roots}\n"
            f"provider: {self.provider_type}\n"
            f"sandbox profile: {sandbox}"
        )


class NavigationPane(Vertical):
    """Mouse and keyboard reachable top-level destinations."""

    def __init__(self, *, workspace_mode: bool, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._workspace_mode = workspace_mode

    def compose(self) -> ComposeResult:
        if self._workspace_mode:
            yield Button("Workspaces", id="nav-workspaces", variant="primary")
            yield Button("Tasks", id="nav-runs")
            yield Button("Configuration", id="nav-settings")
        else:
            yield Button("Tasks", id="nav-runs", variant="primary")
            yield Button("Defects", id="nav-defects")
            yield Button("Requirements", id="nav-requirements")
            yield Button("New Run", id="nav-new-run")
            yield Button("Runtime setup", id="nav-runtime-setup")
            yield Button("Settings", id="nav-settings")


class WorkspaceListPane(Vertical):
    """Top-level list of project/iteration workspaces."""

    def compose(self) -> ComposeResult:
        yield Label("工作区", classes="pane-title")
        yield Static("按项目和迭代组织仓库，进入工作区查询缺陷、需求及管理任务。",
                     id="workspace-home-description", markup=False)
        yield Button("创建工作区", id="create-workspace", variant="primary")
        yield Static("", id="workspace-list-summary", markup=False)
        yield Static(
            "还没有工作区。点击「创建工作区」，选择 ONES 项目、迭代和本地仓库。",
            id="workspace-empty",
            markup=False,
        )
        yield ListView(id="workspace-list")

    async def replace_workspaces(
        self, workspaces: tuple[WorkspaceSummary, ...]
    ) -> None:
        listing = self.query_one("#workspace-list", ListView)
        self.query_one("#workspace-empty", Static).display = not workspaces
        self.query_one("#workspace-list-summary", Static).update(
            f"共 {len(workspaces)} 个工作区 · 点击卡片或 ↑↓ 选择后按 Enter 进入" if workspaces else "")
        await listing.clear()
        await listing.extend(
            ListItem(
                Vertical(
                    Static(item.label, markup=False, classes="workspace-home-title"),
                    Static(f"项目：{item.project_id}  ·  迭代：{item.iteration_id}",
                           markup=False, classes="workspace-home-scope"),
                    Static(f"关联仓库 · {len(item.repositories)} 个", markup=False,
                           classes="workspace-home-repo-count"),
                    Static("、".join(item.repositories[:3]) + (" …（进入查看全部）" if len(item.repositories) > 3 else "")
                           if item.repositories else "暂无关联仓库", markup=False, classes="workspace-home-repos"),
                    Static("进入工作区 →", classes="workspace-home-open"),
                    classes="workspace-home-content",
                ),
                id=f"workspace-item-{index}",
                classes="workspace-home-card",
            )
            for index, item in enumerate(workspaces)
        )


class RunListPane(Vertical):
    """Selectable workflow summaries."""

    def compose(self) -> ComposeResult:
        yield Label("Tasks", classes="pane-title")
        yield Button("Delete task", id="delete-task", variant="error")
        yield ListView(id="run-list")

    @staticmethod
    def _summary_text(item: RunSummary) -> Text:
        return Text.from_markup(
            "  ".join(
                (
                    item.state.value,
                    item.work_item_id,
                    item.updated_at.astimezone(UTC)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                    item.activity.value,
                )
            )
        )

    async def replace_runs(self, runs: tuple[RunSummary, ...]) -> None:
        run_list = self.query_one("#run-list", ListView)
        existing = tuple(run_list.query("ListItem"))
        if tuple(item.name for item in existing) == tuple(
            item.run_id for item in runs
        ):
            for row, summary in zip(existing, runs, strict=True):
                row.query_one(Label).update(self._summary_text(summary))
            if runs and run_list.index is None:
                run_list.index = 0
            return

        await run_list.clear()
        await run_list.extend(
            [
                ListItem(
                    Label(
                        self._summary_text(item),
                        markup=False,
                    ),
                    id=f"run-item-{index}",
                    name=item.run_id,
                )
                for index, item in enumerate(runs)
            ]
        )
        if runs:
            run_list.index = 0


def _review_text(value: str) -> Text:
    # Values are escaped at the view-model boundary. Render inline code as
    # literal text, never as Markdown links or executable Rich markup.
    text = Text.from_markup(value)
    for match in re.finditer(r"`([^`]+)`", text.plain):
        text.stylize("cyan", match.start(), match.end())
    return text


def _review_renderable(report: ReviewView) -> Group:
    def numbered(items: tuple[str, ...]) -> Text:
        text = Text()
        for index, item in enumerate(items, 1):
            if index > 1:
                text.append("\n\n")
            text.append(f"{index}. ", style="bold")
            text.append_text(_review_text(item))
        return text

    sections: list[Panel] = []
    if report.pause_reason:
        sections.append(Panel(
            Text(f"{report.pause_reason}。累计自动回修 {report.review_repair_attempts} 轮。\n"
                 "点击 Continue repair，确认继续或补充修复方向后开启新一轮；不会自动发布。"),
            title=Text("自动回修已暂停", style="bold yellow"), title_align="left", border_style="yellow",
        ))
    if report.blockers:
        sections.append(Panel(
            numbered(report.blockers), title=Text(f"待处理问题 · {len(report.blockers)}", style="bold bright_red"),
            title_align="left", border_style="bright_red", padding=(1, 2),
        ))
        action = "点击 Continue repair 确认续修；无需重复运行同一份 review。" if report.pause_reason else (
            "可执行问题会自动交回修复，再运行测试并重新审查；旧任务点击 Continue repair 可恢复。"
            "若自动修复无进展或达到轮次上限，请先查看 Overview 中的暂停原因。"
        )
        sections.append(Panel(Text(action), title=Text("下一步", style="bold yellow"), title_align="left", border_style="yellow"))
    sections.append(Panel(_review_text(report.summary), title=Text("审查结论", style="bold cyan"), title_align="left", border_style="cyan", padding=(1, 2)))
    if report.external_validation:
        note = Text()
        note.append(
            "以下是环境或发布验证限制，与上方待处理问题分开记录。\n"
            + ("不单独阻断本地验证完成；不代表发布验收已通过。\n\n" if report.verification_only
               else "待执行验证可随 Draft PR 交给人工审核；不代表已通过。合并/发布前仍需对应验证证据。\n\n"), style="dim",
        )
        note.append_text(numbered(report.external_validation))
        sections.append(Panel(note, title=Text(f"外部验证 · {len(report.external_validation)}", style="bold yellow"), title_align="left", border_style="yellow", padding=(1, 2)))
    if report.findings:
        sections.append(Panel(numbered(report.findings), title=Text(f"审查依据 · {len(report.findings)}", style="bold white"), title_align="left", border_style="bright_black", padding=(1, 2)))
    return Group(*sections)


class RunDetailPane(Vertical):
    """Fixed evidence tabs backed only by safe view-model fields."""

    def __init__(self, *args, initial_tab: str = "overview", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._initial_tab = initial_tab
        self._report_opened = False
        self._review_identity: tuple[object, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Label("Task detail", classes="pane-title")
        with TabbedContent(initial=self._initial_tab, id="detail-tabs"):
            with TabPane("Overview", id="overview"):
                with VerticalScroll(classes="detail-scroll", id="overview-scroll"):
                    yield Static("请选择一个任务查看详情。", id="overview-content", markup=False)
            with TabPane("AI activity", id="ai-activity"):
                with VerticalScroll(classes="detail-scroll", id="ai-activity-scroll"):
                    yield Static("No AI activity", id="ai-activity-content", markup=False)
            with TabPane("Repositories", id="repositories"):
                with VerticalScroll(classes="detail-scroll", id="repositories-scroll"):
                    yield Static(
                        "暂无仓库证据。",
                        id="repositories-content",
                        markup=False,
                    )
            with TabPane("Tests", id="tests"):
                with VerticalScroll(classes="detail-scroll", id="tests-scroll"):
                    yield Static("暂无测试记录。没有记录不代表测试通过。", id="tests-content", markup=False)
            with TabPane("Review", id="review"):
                yield Static("", id="review-status", markup=False)
                with VerticalScroll(classes="detail-scroll", id="review-scroll"):
                    yield Static("No review evidence", id="review-content", markup=True)
            with TabPane("Publication", id="publication"):
                with VerticalScroll(classes="detail-scroll", id="publication-scroll"):
                    yield Static(
                        "暂无发布记录。",
                        id="publication-content",
                        markup=False,
                    )
            with TabPane("History", id="history"):
                with VerticalScroll(classes="detail-scroll", id="history-scroll"):
                    yield Static("暂无状态变更记录。", id="history-content", markup=False)

    def set_detail(self, detail: RunDetail) -> None:
        summary = detail.summary
        self.query_one("#overview-content", Static).update(detail_rendering.overview(detail))
        if detail.ai_activity:
            activity_text: str | Text = _ai_activity_renderable(
                detail.ai_activity
            )
        else:
            activity_text = "\n".join(
                (
                    "Workflow started",
                    f"Current phase: {_workflow_phase(detail)}",
                    "Preparing the workflow before the Codex session starts...",
                    "AI activity will appear here as soon as it is available.",
                )
            )
        activity_scroll = self.query_one("#ai-activity-scroll", VerticalScroll)
        follow_latest = activity_scroll.scroll_y >= activity_scroll.max_scroll_y - 1
        if follow_latest:
            activity_scroll.anchor(animate=False)
        self.query_one("#ai-activity-content", Static).update(activity_text)
        self.query_one("#repositories-content", Static).update(detail_rendering.repositories(detail))
        self.query_one("#tests-content", Static).update(detail_rendering.tests(detail))
        report = detail.review_report
        status = self.query_one("#review-status", Static)
        status.display = report is not None
        if report is not None:
            heading = (
                f"审查待处理  ·  {len(report.blockers)} 项问题"
                if report.blockers else "未发现待处理问题"
            )
            heading += f"  ·  {len(report.external_validation)} 项外部验证"
            status.update(Text(heading, style="bold yellow" if report.blockers else "bold cyan"))
            status.border_title = (
                "本地验证 · 含复审修正（不发布）" if report.verification_only and report.review_repair_attempts
                else "当前代码验证 · 未实施生产修复" if report.verification_only else "代码审查"
            )
        review_body = _review_renderable(report) if report is not None else Text.from_markup("\n\n".join(detail.review) or "No review evidence")
        if detail.verification_tasks:
            states = {"ready": "待确认执行", "waiting_environment": "等待匹配环境", "manual": "等待人工验证",
                      "running": "正在验证", "passed": "已通过", "failed": "验证失败", "error": "执行异常", "stale": "证据已过期"}
            lines = [f"{states.get(t.status, t.status)} · {t.node_key or '未分配节点'} · {t.need.description}"
                     for t in detail.verification_tasks]
            review_body = Group(Panel(Text(public_text("\n\n".join(lines))), title="环境验证计划", border_style="cyan"), review_body)
        if detail.summary.state is WorkflowState.BLOCKED and detail.status_message:
            review_body = Group(Panel(
                Text(detail.status_message + "\n\n" + detail_rendering.next_step(detail)),
                title="当前流程暂停原因（与待验证清单分开）", border_style="yellow",
            ), review_body)
        if detail.verification_results:
            review_body = Group(review_body, Panel(Text("\n\n".join(detail.verification_results)),
                title="最近验证记录（以当前快照为准）", border_style="cyan"))
        self.query_one("#review-content", Static).update(review_body)
        review_identity = (summary.run_id, report, detail.verification_tasks, detail.verification_results)
        if self._review_identity != review_identity:
            self.query_one("#review-scroll", VerticalScroll).scroll_home(animate=False)
            self._review_identity = review_identity
            self.call_after_refresh(self.query_one("#review-scroll", VerticalScroll).scroll_home, animate=False)
        self.query_one("#publication-content", Static).update(detail_rendering.publication(detail))
        self.query_one("#history-content", Static).update(detail_rendering.history(detail))
        if (
            not self._report_opened
            and summary.state is WorkflowState.COMPLETED
            and detail.review
        ):
            tabs = self.query_one("#detail-tabs", TabbedContent)
            if tabs.active == "ai-activity":
                tabs.active = "review"
                self._report_opened = True

    def clear_detail(self) -> None:
        self._review_identity = None
        self.query_one("#review-status", Static).display = False
        self.query_one("#overview-content", Static).update("请选择一个任务查看详情。")
        self.query_one("#ai-activity-content", Static).update("No AI activity")
        self.query_one("#repositories-content", Static).update(
            "暂无仓库证据。"
        )
        self.query_one("#tests-content", Static).update("暂无测试记录。没有记录不代表测试通过。")
        self.query_one("#review-content", Static).update("No review evidence")
        self.query_one("#publication-content", Static).update(
            "暂无发布记录。"
        )
        self.query_one("#history-content", Static).update("暂无状态变更记录。")

    def show_error(self, message: str) -> None:
        self.clear_detail()
        self.query_one("#overview-content", Static).update(message)

    def next_tab(self) -> None:
        tabs = self.query_one("#detail-tabs", TabbedContent)
        index = _DETAIL_TABS.index(tabs.active)
        tabs.active = _DETAIL_TABS[(index + 1) % len(_DETAIL_TABS)]

    def previous_tab(self) -> None:
        tabs = self.query_one("#detail-tabs", TabbedContent)
        index = _DETAIL_TABS.index(tabs.active)
        tabs.active = _DETAIL_TABS[(index - 1) % len(_DETAIL_TABS)]


def _mapping_candidate_text(candidate: MappingCandidateView) -> str:
    topology = " -> ".join(item.key for item in candidate.repositories)
    lines = [
        f"{candidate.key}  {candidate.kind}",
        f"primary: {candidate.primary_repository}",
        f"topology: {topology}",
    ]
    for repository in candidate.repositories:
        dependencies = ", ".join(repository.depends_on) or "none"
        allowed_paths = ", ".join(repository.allowed_paths) or "none configured"
        lines.extend(
            (
                f"repository: {repository.key}  role: {repository.role}",
                f"source: {repository.source}",
                f"depends on: {dependencies}",
                repository.lint_summary,
                repository.build_summary,
                repository.test_summary,
                f"allowed paths: {allowed_paths}",
                f"side effects: {repository.side_effects}",
            )
        )
    lines.append(candidate.integration_test_summary)
    return "\n".join(lines)


class RunDetailScreen(Screen[None]):
    """Independent detail page used by one-column terminals."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("left", "back", "Back", show=False),
        Binding("tab", "next_tab", "Next tab", show=False, priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
    ]

    def __init__(
        self,
        detail: RunDetail | None,
        *,
        error: str = "",
        controller: TuiController | None = None,
        supervisor: RunTaskSupervisor | None = None,
    ) -> None:
        super().__init__(id="run-detail-screen")
        self._detail = detail
        self._error = error
        self._controller = controller
        self._supervisor = supervisor

    def compose(self) -> ComposeResult:
        yield RunDetailPane(id="run-detail")
        with Horizontal(id="analysis-decision-bar"):
            yield Button(
                "Accept solution",
                id="detail-accept-analysis",
                variant="success",
            )
            yield Button(
                "Regenerate solution",
                id="detail-regenerate-analysis",
            )
        yield Static("", id="detail-action-notice", markup=False)

    def on_mount(self) -> None:
        pane = self.query_one(RunDetailPane)
        if self._detail is not None:
            pane.set_detail(self._detail)
        else:
            pane.show_error(self._error or _DISPLAY_UNAVAILABLE)
        self._update_analysis_actions()

    def _update_analysis_actions(self) -> None:
        enabled = self._controller is not None and self._supervisor is not None
        detail = self._detail
        self.query_one("#detail-accept-analysis", Button).display = bool(
            enabled and detail and detail.can_accept_analysis
        )
        self.query_one("#detail-regenerate-analysis", Button).display = bool(
            enabled and detail and detail.can_regenerate_analysis
        )

    async def _analysis_decision(self, *, accept: bool) -> None:
        detail = self._detail
        controller = self._controller
        supervisor = self._supervisor
        if detail is None or controller is None or supervisor is None:
            return
        allowed = detail.can_accept_analysis if accept else detail.can_regenerate_analysis
        if not allowed:
            return
        self.query_one("#detail-accept-analysis", Button).display = False
        self.query_one("#detail-regenerate-analysis", Button).display = False
        self.query_one("#detail-action-notice", Static).update(
            "Repair workflow started" if accept else "Regenerating analysis solution"
        )
        call = (
            controller.accept_analysis_solution
            if accept
            else controller.regenerate_analysis_solution
        )
        try:
            self._detail = await supervisor.run_mutation(
                detail.summary.run_id,
                "accept-analysis" if accept else "regenerate-analysis",
                call,
                detail.summary.run_id,
                detail.summary.version,
            )
        except Exception:
            self.query_one("#detail-action-notice", Static).update(_ACTION_FAILED)
            self._update_analysis_actions()
            return
        self.query_one(RunDetailPane).set_detail(self._detail)
        self.query_one("#detail-action-notice", Static).update("")
        self._update_analysis_actions()

    @on(Button.Pressed, "#detail-accept-analysis")
    async def _accept_analysis(self) -> None:
        await self._analysis_decision(accept=True)

    @on(Button.Pressed, "#detail-regenerate-analysis")
    async def _regenerate_analysis(self) -> None:
        await self._analysis_decision(accept=False)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_next_tab(self) -> None:
        self.query_one(RunDetailPane).next_tab()

    def action_previous_tab(self) -> None:
        self.query_one(RunDetailPane).previous_tab()


class _MappingWizardScreen(Screen[RunDetail | None]):
    """Shared mapping, review, and authoritative confirmation stages."""

    STEP_FILTER = 0
    STEP_CANDIDATE = 1
    STEP_MAPPING = 2
    STEP_CONFIRM = 3
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(
        self,
        controller: TuiController,
        supervisor: RunTaskSupervisor,
        *,
        screen_id: str,
    ) -> None:
        super().__init__(id=screen_id)
        self._controller = controller
        self._supervisor = supervisor
        self._preview: RunDetail | None = None
        self._mapping_key = ""
        self._mapping_candidates: tuple[MappingCandidateView, ...] = ()
        self._step = self.STEP_FILTER
        self._confirmation_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="wizard-body"):
            yield from self._initial_widgets()
        yield Static("", id="wizard-notice", markup=False)
        yield Button("Cancel", id="cancel-wizard")

    def _initial_widgets(self) -> tuple[Widget, ...]:
        raise NotImplementedError

    async def _show_mapping(self, preview: RunDetail) -> None:
        if preview.summary.state is not WorkflowState.VALIDATING:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        self._preview = preview
        self._mapping_candidates = preview.mapping_candidates
        self._step = self.STEP_MAPPING
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        if not self._mapping_candidates:
            self._show_notice(_NO_MAPPINGS)
            await body.mount(Label("No authorized repository mappings"))
            return
        self._show_notice("")
        await body.mount(Label("Select an authorized repository mapping"))
        for index, candidate in enumerate(self._mapping_candidates):
            await body.mount(
                Button(
                    f"Select {candidate.key}",
                    id=f"mapping-{index}",
                    variant="primary",
                ),
                Static(
                    _mapping_candidate_text(candidate),
                    id=f"mapping-candidate-{index}",
                    markup=True,
                ),
            )

    async def _show_confirmation(self, index: int) -> None:
        preview = self._preview
        if (
            preview is None
            or self._step != self.STEP_MAPPING
            or not 0 <= index < len(self._mapping_candidates)
        ):
            self._show_notice(_MAPPING_REQUIRED)
            return
        candidate = self._mapping_candidates[index]
        self._mapping_key = candidate.key
        self._step = self.STEP_CONFIRM
        self._show_notice("")
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        await body.mount(
            Label("Confirm workflow"),
            Button("Confirm", id="confirm-start", variant="success"),
            Static(
                "\n".join(
                    (
                        f"work item: {preview.summary.work_item_id}",
                        _mapping_candidate_text(candidate),
                        f"state: {preview.summary.state.value}",
                    )
                ),
                id="workflow-summary",
                markup=True,
            ),
        )

    async def _confirm(self) -> None:
        preview = self._preview
        if preview is None or not self._mapping_key:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        running_detail = RunDetailPane(
            id="running-detail", initial_tab="ai-activity"
        )
        await body.mount(running_detail)
        running_detail.set_detail(preview)
        task = self._supervisor.submit(
            preview.summary.run_id,
            "confirm-repository",
            lambda: self._controller.confirm_repository(
                preview.summary.run_id,
                self._mapping_key,
                preview.summary.version,
            ),
        )

        # Do not await the workflow from the button handler.  Textual may defer
        # painting widget changes until that handler returns, which made the
        # confirmation page appear frozen for the whole Codex run.
        self._confirmation_task = asyncio.create_task(
            self._finish_confirmation(task, preview),
            name=f"tui-confirm-{preview.summary.run_id}",
        )

    async def _finish_confirmation(
        self, task: asyncio.Task[RunDetail], preview: RunDetail
    ) -> None:

        async def refresh_progress() -> None:
            while True:
                await asyncio.sleep(0.2)
                if not self.is_attached:
                    continue
                try:
                    progress = await asyncio.to_thread(
                        self._controller.show, preview.summary.run_id
                    )
                except Exception:
                    continue
                activity: tuple[str, ...] = ()
                activity_source = getattr(self._controller, "ai_activity", None)
                if callable(activity_source):
                    try:
                        activity = await asyncio.to_thread(
                            activity_source, preview.summary.run_id
                        )
                    except Exception:
                        activity = ()
                if activity:
                    progress = replace(progress, ai_activity=activity)
                self.query_one("#running-detail", RunDetailPane).set_detail(progress)

        progress_task = asyncio.create_task(refresh_progress())
        try:
            detail = await task
        except Exception:
            if self.is_attached:
                self._show_notice(_WIZARD_UNAVAILABLE)
            return
        finally:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
        if self.is_attached:
            self.dismiss(detail)

    def _show_notice(self, message: str) -> None:
        self.query_one("#wizard-notice", Static).update(message)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    async def _handle_mapping_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "cancel-wizard":
            self.action_cancel()
        elif button_id is not None and button_id.startswith("mapping-"):
            try:
                index = int(button_id.removeprefix("mapping-"))
            except ValueError:
                self._show_notice(_MAPPING_REQUIRED)
                return
            await self._show_confirmation(index)
        elif button_id == "confirm-start" and self._step == self.STEP_CONFIRM:
            await self._confirm()


class DefectWizardScreen(_MappingWizardScreen):
    """Four-stage defect wizard with a read-only candidate query."""

    def __init__(
        self,
        controller: TuiController,
        supervisor: RunTaskSupervisor,
        *,
        workspace: WorkspaceSummary | None = None,
    ) -> None:
        super().__init__(
            controller,
            supervisor,
            screen_id="defect-wizard-screen",
        )
        self._candidate_session_id: str | None = None
        self._candidates: tuple[DefectChoice, ...] = ()
        self._workspace = workspace

    def _initial_widgets(self) -> tuple[Widget, ...]:
        project = (
            self._workspace.project_id
            if self._workspace is not None
            else getattr(self._controller, "default_defect_project", "")
        )
        if type(project) is not str:
            project = ""
        return (
            Label("Analyze and repair an ONES defect in the current workspace"),
            Input(
                value=project,
                placeholder="ONES project ID",
                id="project",
                disabled=bool(project),
            ),
            Select([], prompt="Loading ONES iterations…", id="iteration", disabled=True),
            Select([], prompt="Loading ONES users…", id="assignee", disabled=True),
            Label("Open defect statuses"),
            SelectionList(id="status-ids", disabled=True),
            Button("Reload ONES options", id="reload-defect-options"),
            Button("Query defects", id="query-defects", variant="primary"),
        )

    async def on_mount(self) -> None:
        await self._load_filter_options()

    async def _load_filter_options(self) -> None:
        project = self.query_one("#project", Input).value.strip()
        if not project:
            self._show_notice(_INPUT_REQUIRED)
            return
        self._show_notice("Loading iterations, users, and statuses from ONES")
        query_button = self.query_one("#query-defects", Button)
        query_button.disabled = True
        try:
            options = await self._supervisor.run_readonly(
                "load-defect-options",
                self._controller.load_defect_filter_options,
                project,
            )
        except Exception:
            self._show_notice("ONES filter options are unavailable")
            return
        iteration = self.query_one("#iteration", Select)
        assignee = self.query_one("#assignee", Select)
        statuses = self.query_one("#status-ids", SelectionList)
        configured_iteration = (
            self._workspace.iteration_id if self._workspace is not None else ""
        )
        iteration_options = options.iterations
        if configured_iteration and not any(
            item.id == configured_iteration for item in iteration_options
        ):
            iteration_options = (
                FilterChoice(configured_iteration, "Configured iteration"),
                *iteration_options,
            )
        iteration.set_options((item.name, item.id) for item in iteration_options)
        assignee.set_options((item.name, item.id) for item in options.assignees)
        statuses.clear_options()
        statuses.add_options(
            (
                item.name,
                item.id,
                item.selected
                or not any(candidate.selected for candidate in options.statuses)
                and index == 0,
            )
            for index, item in enumerate(options.statuses)
        )
        if iteration_options:
            iteration.value = next(
                (
                    item.id
                    for item in iteration_options
                    if item.id == configured_iteration
                ),
                iteration_options[0].id,
            )
        iteration.disabled = not iteration_options or self._workspace is not None
        if options.assignees:
            assignee.value = next(
                (item.id for item in options.assignees if item.selected),
                options.assignees[0].id,
            )
        assignee.disabled = not options.assignees
        statuses.disabled = False
        query_button.disabled = not iteration_options or not options.assignees
        if options.unavailable:
            self._show_notice(
                "Some ONES options are unavailable: " + ", ".join(options.unavailable)
            )
        else:
            self._show_notice("")

    async def _query(self) -> None:
        project = self.query_one("#project", Input).value.strip()
        iteration = self.query_one("#iteration", Select).value
        assignee = self.query_one("#assignee", Select).value
        status_ids = tuple(self.query_one("#status-ids", SelectionList).selected)
        if (
            not project
            or type(iteration) is not str
            or not iteration
            or type(assignee) is not str
            or not assignee
        ):
            self._show_notice(_INPUT_REQUIRED)
            return
        if any(not _SAFE_MAPPING_KEY.fullmatch(item) for item in status_ids):
            self._show_notice(_INPUT_REQUIRED)
            return
        try:
            session = await self._supervisor.run_readonly(
                "query-defects",
                self._controller.query_defects,
                project,
                iteration,
                assignee,
                status_ids,
            )
        except Exception:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        if not session.items:
            self._show_notice(_NO_CANDIDATES)
            return
        self._candidate_session_id = session.session_id
        self._candidates = session.items
        self._step = self.STEP_CANDIDATE
        self._show_notice("")
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        await body.mount(Label("Select an action for a defect"))
        for index, candidate in enumerate(session.items):
            await body.mount(
                Static(
                    "  ".join(
                        (
                            candidate.priority,
                            candidate.status_id or "status unavailable",
                            candidate.title,
                        )
                    ),
                    markup=False,
                ),
                Horizontal(
                    Button("AI analysis", id=f"analyze-candidate-{index}"),
                    Button(
                        "Analyze and repair",
                        id=f"candidate-{index}",
                        variant="primary",
                    ),
                    classes="candidate-actions",
                ),
            )

    async def _select_candidate(self, index: int, *, analyze_only: bool) -> None:
        session_id = self._candidate_session_id
        if (
            self._step != self.STEP_CANDIDATE
            or session_id is None
            or not 0 <= index < len(self._candidates)
        ):
            self._show_notice(_CANDIDATE_STALE)
            return
        candidate_id = self._candidates[index].candidate_id
        # Make the UI-side capability one-shot before yielding to background work.
        self._candidate_session_id = None
        try:
            action_name = "analyze-defect" if analyze_only else "start-defect"
            action = (
                self._controller.analyze_defect
                if analyze_only
                else self._controller.start_defect
            )
            preview = await self._supervisor.run_mutation(
                "new-defect",
                action_name,
                action,
                session_id,
                candidate_id,
            )
        except StaleCandidateError:
            self._show_notice(_CANDIDATE_STALE)
            return
        except Exception:
            self._show_notice(
                "AI analysis could not be started"
                if analyze_only
                else "Analysis and repair could not be started"
            )
            return
        await self._show_mapping(preview)

    @on(Button.Pressed)
    async def _handle_defect_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "reload-defect-options" and self._step == self.STEP_FILTER:
            await self._load_filter_options()
        elif button_id == "query-defects" and self._step == self.STEP_FILTER:
            await self._query()
        elif button_id.startswith(("candidate-", "analyze-candidate-")):
            try:
                analyze_only = button_id.startswith("analyze-candidate-")
                prefix = "analyze-candidate-" if analyze_only else "candidate-"
                index = int(button_id.removeprefix(prefix))
            except ValueError:
                self._show_notice(_CANDIDATE_STALE)
                return
            await self._select_candidate(index, analyze_only=analyze_only)


class RequirementWizardScreen(_MappingWizardScreen):
    """List ONES requirements, then enter the shared mapping flow."""

    def __init__(self, controller: TuiController, supervisor: RunTaskSupervisor,
                 *, workspace: WorkspaceSummary | None = None) -> None:
        super().__init__(
            controller,
            supervisor,
            screen_id="requirement-wizard-screen",
        )
        self._requirement_session_id: str | None = None
        self._requirements: tuple[RequirementChoice, ...] = ()
        self.workspace = workspace

    def _initial_widgets(self) -> tuple[Widget, ...]:
        widgets = (
            Label("Requirements from ONES"),
            Input(placeholder="ONES requirement ID", id="requirement-id"),
            Button("Fetch requirement", id="start-requirement"),
            Label("Or query the requirement list"),
            Input(self.workspace.project_id if self.workspace else "",
                  placeholder="Optional ONES project ID", id="requirement-project",
                  disabled=self.workspace is not None),
            Input(self.workspace.iteration_id if self.workspace else "",
                  placeholder="Optional ONES iteration ID", id="requirement-iteration",
                  disabled=self.workspace is not None),
            Input(placeholder="Optional ONES assignee ID", id="requirement-assignee"),
            Input(placeholder="Optional status IDs, comma-separated", id="requirement-status-ids"),
            Input(placeholder="Requirement issue type ID", id="requirement-type-id"),
            Button("Query requirements", id="query-requirements", variant="primary"),
        )
        # A workspace entry must not offer the unscoped direct-ID mutation path.
        return widgets[3:] if self.workspace else widgets

    async def _query_requirements(self) -> None:
        issue_type_id = self.query_one("#requirement-type-id", Input).value.strip()
        status_values = self.query_one("#requirement-status-ids", Input).value
        status_ids = tuple(item.strip() for item in status_values.split(",") if item.strip())
        if not issue_type_id or any(not _SAFE_MAPPING_KEY.fullmatch(item) for item in status_ids):
            self._show_notice(_INPUT_REQUIRED)
            return
        try:
            session_id, items = await self._supervisor.run_readonly(
                "query-requirements",
                self._controller.query_requirements,
                self.query_one("#requirement-project", Input).value.strip(),
                self.query_one("#requirement-iteration", Input).value.strip(),
                self.query_one("#requirement-assignee", Input).value.strip(),
                status_ids,
                issue_type_id,
            )
        except Exception:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        if not items:
            self._show_notice(_NO_CANDIDATES)
            return
        self._requirement_session_id = session_id
        self._requirements = tuple(items)
        self._step = self.STEP_CANDIDATE
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        await body.mount(Label("Select a requirement to analyze"))
        for index, item in enumerate(self._requirements):
            await body.mount(
                Button(
                    "  ".join((item.number or item.requirement_id, item.status_id, item.title)),
                    id=f"requirement-candidate-{index}",
                    classes="requirement-candidate",
                )
            )

    async def _start_requirement(self) -> None:
        requirement_id = self.query_one("#requirement-id", Input).value.strip()
        if not requirement_id:
            self._show_notice(_INPUT_REQUIRED)
            return
        try:
            preview = await self._supervisor.run_mutation(
                "new-requirement",
                "start-requirement",
                self._controller.start_requirement,
                requirement_id,
            )
        except Exception:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        await self._show_mapping(preview)

    async def _select_requirement(self, index: int) -> None:
        session_id = self._requirement_session_id
        if self._step != self.STEP_CANDIDATE or session_id is None or not 0 <= index < len(self._requirements):
            self._show_notice(_CANDIDATE_STALE)
            return
        requirement_id = self._requirements[index].requirement_id
        self._requirement_session_id = None
        try:
            preview = await self._supervisor.run_mutation(
                "new-requirement",
                "start-requirement",
                self._controller.start_requirement,
                requirement_id,
                session_id,
            )
        except Exception:
            self._show_notice(_CANDIDATE_STALE)
            return
        await self._show_mapping(preview)

    @on(Button.Pressed, "#query-requirements")
    async def _handle_requirement_query(self) -> None:
        if self._step == self.STEP_FILTER:
            await self._query_requirements()

    @on(Button.Pressed, "#start-requirement")
    async def _handle_requirement_direct_start(self) -> None:
        if self._step == self.STEP_FILTER:
            await self._start_requirement()

    @on(Button.Pressed, ".requirement-candidate")
    async def _handle_requirement_candidate(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("requirement-candidate-"):
            return
        try:
            index = int(button_id.removeprefix("requirement-candidate-"))
        except ValueError:
            self._show_notice(_CANDIDATE_STALE)
            return
        await self._select_requirement(index)


class WorkflowTypeScreen(Screen[RunDetail | None]):
    """Choose the only two supported workflow creation paths."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, controller: TuiController, supervisor: RunTaskSupervisor) -> None:
        super().__init__(id="workflow-type-screen")
        self._controller = controller
        self._supervisor = supervisor

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("New workflow")
            yield Button("Defect", id="workflow-defect", variant="primary")
            yield Button("Requirement", id="workflow-requirement")
            yield Button("Cancel", id="cancel-workflow-type")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _wizard_done(self, detail: RunDetail | None) -> None:
        if detail is not None:
            self.dismiss(detail)

    @on(Button.Pressed)
    def _choose(self, event: Button.Pressed) -> None:
        if event.button.id == "workflow-defect":
            self.app.push_screen(
                DefectWizardScreen(self._controller, self._supervisor),
                callback=self._wizard_done,
            )
        elif event.button.id == "workflow-requirement":
            self.app.push_screen(
                RequirementWizardScreen(self._controller, self._supervisor),
                callback=self._wizard_done,
            )
        elif event.button.id == "cancel-workflow-type":
            self.action_cancel()


_HELP_TEXT = """Developer workflow help

Workspaces  list or create workspaces
Create workspace  choose ONES Project, iteration, and local/remote repositories
Query defects  open a workspace, query defects, then choose AI analysis or analysis and repair
Configuration  ONES settings, verification nodes, and runtime information
g  return to workspace list
s  show configuration
n  legacy new workflow shortcut
/  legacy run search
f  legacy run filter
r/v/a/x  legacy run resume, revise, approve, and cancel actions
q  quit without cancelling running workflow work
Escape  return

Defect queries and run listing are read-only. Repository changes use managed worktrees.
Remote publication requires explicit approval.
"""


class HelpScreen(Screen[None]):
    """Fixed credential-free keyboard help."""

    BINDINGS = [Binding("escape", "back", "Back", priority=True)]

    def __init__(self) -> None:
        super().__init__(id="help-screen")

    def compose(self) -> ComposeResult:
        yield Static(_HELP_TEXT, id="help-content", markup=False)

    def action_back(self) -> None:
        self.app.pop_screen()


class RunFilterScreen(Screen[RunFilter | None]):
    """Explicit read-only search/filter editor for persisted run summaries."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, current: RunFilter, *, search_only: bool) -> None:
        super().__init__(id="search-screen" if search_only else "filter-screen")
        self._current = current
        self._search_only = search_only

    def compose(self) -> ComposeResult:
        yield Label("Search runs" if self._search_only else "Filter runs")
        yield Input(
            value=self._current.query,
            placeholder="Run ID or work item ID",
            id="work-item-query",
            max_length=256,
        )
        if not self._search_only:
            yield Input(
                value=",".join(item.value for item in self._current.states),
                placeholder="States, comma-separated",
                id="filter-states",
                max_length=512,
            )
            yield Input(
                value=",".join(item.value for item in self._current.workflow_types),
                placeholder="Workflow types, comma-separated",
                id="filter-types",
                max_length=128,
            )
            yield Input(
                value=(
                    self._current.updated_after.isoformat()
                    if self._current.updated_after is not None
                    else ""
                ),
                placeholder="Updated after, ISO 8601",
                id="updated-after",
                max_length=64,
            )
            yield Input(
                value=(
                    self._current.updated_before.isoformat()
                    if self._current.updated_before is not None
                    else ""
                ),
                placeholder="Updated before, ISO 8601",
                id="updated-before",
                max_length=64,
            )
        yield Static("", id="run-filter-notice", markup=False)
        yield Button("Apply", id="apply-run-filter", variant="primary")
        yield Button("Clear", id="clear-run-filter")
        yield Button("Cancel", id="cancel-run-filter")

    @staticmethod
    def _timestamp(value: str) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed

    def _build_filter(self) -> RunFilter:
        query = validate_tui_input_text(
            self.query_one("#work-item-query", Input).value,
            maximum=256,
            allow_empty=True,
        ).strip()
        if self._search_only:
            return RunFilter(
                states=self._current.states,
                workflow_types=self._current.workflow_types,
                query=query,
                updated_after=self._current.updated_after,
                updated_before=self._current.updated_before,
            )
        state_values = tuple(
            value.strip().upper()
            for value in self.query_one("#filter-states", Input).value.split(",")
            if value.strip()
        )
        type_values = tuple(
            value.strip().casefold()
            for value in self.query_one("#filter-types", Input).value.split(",")
            if value.strip()
        )
        return RunFilter(
            states=tuple(WorkflowState(value) for value in state_values),
            workflow_types=tuple(WorkflowType(value) for value in type_values),
            query=query,
            updated_after=self._timestamp(
                self.query_one("#updated-after", Input).value.strip()
            ),
            updated_before=self._timestamp(
                self.query_one("#updated-before", Input).value.strip()
            ),
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-run-filter":
            self.action_cancel()
            return
        if event.button.id == "clear-run-filter":
            self.dismiss(RunFilter())
            return
        if event.button.id != "apply-run-filter":
            return
        try:
            filters = self._build_filter()
        except (TuiDisplayError, ValueError):
            self.query_one("#run-filter-notice", Static).update(
                "run filter is invalid"
            )
            return
        self.dismiss(filters)


@dataclass(frozen=True, slots=True)
class ApprovalSubmission:
    request: DangerousActionRequest
    actor: str


@dataclass(frozen=True, slots=True)
class RevisionSubmission:
    request: DangerousActionRequest
    feedback: str
    scope: Literal["implementation", "repair"]


@dataclass(frozen=True, slots=True)
class CancelSubmission:
    request: DangerousActionRequest
    actor: str


class _DangerousActionModal(ModalScreen[object | None]):
    """Explicit-confirmation shell; plain Enter is deliberately inert."""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("enter", "ignore_enter", "", show=False, priority=True),
        Binding("ctrl+enter", "confirm", "Confirm", priority=True),
    ]

    def __init__(self, request: DangerousActionRequest, *, screen_id: str) -> None:
        super().__init__(id=screen_id)
        self.request = request

    def action_back(self) -> None:
        self.dismiss(None)

    def action_ignore_enter(self) -> None:
        return

    def action_confirm(self) -> None:
        self._confirm()

    def _confirm(self) -> None:
        raise NotImplementedError

    def _notice(self, message: str) -> None:
        self.query_one("#modal-notice", Static).update(message)


def _valid_action_text(value: str, *, maximum: int) -> str | None:
    value = value.strip()
    if not value or len(value) > maximum:
        return None
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return None
    if any(
        ord(character) < 32
        or 127 <= ord(character) <= 159
        or unicodedata.category(character) in {"Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        return None
    return value


def _repository_action_facts(repository: RepositoryView) -> tuple[Widget, ...]:
    return (
        Label(f"{repository.key}  {repository.role}"),
        Static(f"base: {repository.base_commit or 'not available'}"),
        Static(f"head: {repository.head_commit or 'not available'}"),
        Static(repository.tree_hash or "not available", classes="tree-hash"),
        Static(repository.test_summary),
        Static(f"PR target: {repository.pr_target or 'not available'}"),
        Static(f"commit: {repository.commit_hash or 'not created'}"),
        Static(f"push: {'completed' if repository.pushed else 'not completed'}"),
        Static(f"PR: {repository.pr_url or 'not created'}"),
        Static(f"error: {repository.error or 'none'}"),
    )


class ApprovalModal(_DangerousActionModal):
    """Review signed approval facts before allowing an explicit approval."""

    def __init__(self, request: DangerousActionRequest) -> None:
        super().__init__(request, screen_id="approval-modal")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Enter may open/select a member, but must never press the approval button.
        if action == "ignore_enter":
            actor = self.query_one("#actor", Select)
            if actor.has_focus or actor.expanded:
                return False
        return super().check_action(action, parameters)

    def compose(self) -> ComposeResult:
        request = self.request
        changed = sum(bool(repo.changed_file_count) for repo in request.repositories)
        with Vertical(id="approval-dialog"):
            yield Label("审批并创建 Draft PR" if request.draft_pr else "审批并创建 PR", id="approval-title")
            yield Static(detail_rendering.literal(f"工作项 {request.work_item_id} · 请核对本次操作范围"), id="approval-subtitle")
            with VerticalScroll(id="approval-body"):
                with Vertical(classes="approval-card approval-warning"):
                    yield Label("本次授权范围", classes="approval-heading")
                    yield Static(
                        "提交代码 → 推送新分支 → 创建草稿 PR" if request.draft_pr else "提交代码 → 推送新分支 → 创建 PR",
                        markup=False,
                    )
                    yield Static("不授权合并或发布，不视为实机验证通过。", markup=False)
                    if request.draft_pr:
                        yield Static(f"待人工验证 {request.deferred_check_count} 项 · 随 Draft PR 交由审核人核验。", markup=False)
                if request.baseline_evidence_missing:
                    with Vertical(classes="approval-card approval-warning"):
                        yield Label("重要 · 修复证据存在缺口", classes="approval-heading")
                        yield Static("缺少修复前失败复现记录。当前测试通过不能单独证明修复有效。"
                                     "本次批准仅创建披露该缺口的 Draft PR，请由审核人补验或明确评估证据限制。", markup=False)
                with Vertical(classes="approval-card"):
                    yield Label("变更摘要", classes="approval-heading")
                    yield Static(f"关联仓库 {len(request.repositories)} · 有改动 {changed} · 变更文件 {request.changed_file_count}", markup=False)
                    yield Static(f"测试记录 {request.test_count} · 风险提示 {request.risk_count} · 未解决项 {request.unresolved_count}", markup=False)
                for repository in request.repositories:
                    with Vertical(classes="approval-card approval-repository"):
                        role = {"primary": "主仓库", "dependency": "依赖仓库"}.get(repository.role, repository.role)
                        yield Static(detail_rendering.literal(f"{repository.key} · {role}", "bold cyan"))
                        yield Static(
                            f"变更 {repository.changed_file_count} 个文件 · 纳入本次 PR 创建范围" if repository.changed_file_count
                            else "无代码变更 · 不创建 PR，仅保留关联证据",
                            classes="approval-repository-scope", markup=False,
                        )
                        yield Static(detail_rendering.literal(f"目标分支：{repository.pr_target or '暂无记录'}"))
                        test_summary = re.sub(r"^(\d+) verified test facts?$", r"\1 条已核验测试记录", repository.test_summary)
                        yield Static(detail_rendering.literal(test_summary))
                        if repository.changed_files:
                            yield Static(detail_rendering.literal("变更文件：\n" + "\n".join(f"  • {path}" for path in repository.changed_files)))
                        if repository.error:
                            yield Static(detail_rendering.literal(f"仓库错误：{repository.error}"), classes="approval-error")
                        with Collapsible(title="查看完整快照与提交状态", collapsed=True):
                            yield Static(detail_rendering.literal(f"基线：{repository.base_commit or '暂无记录'}"))
                            yield Static(detail_rendering.literal(f"HEAD：{repository.head_commit or '暂无记录'}"))
                            yield Label("工作树哈希")
                            yield Static(detail_rendering.literal(repository.tree_hash or "暂无记录"), classes="tree-hash")
                            yield Static(detail_rendering.literal(f"提交：{repository.commit_hash or '尚未创建'}"))
                            yield Static(f"推送：{'已完成' if repository.pushed else '尚未完成'}", markup=False)
                            yield Static(detail_rendering.literal(f"PR：{repository.pr_url or '尚未创建'}"))
                with Collapsible(title="审批指纹与交付信息", collapsed=True):
                    yield Static(detail_rendering.literal(request.fingerprint), id="fingerprint")
                    comment_status = {"not delivered": "尚未交付", "delivered": "已交付"}.get(request.comment_status, request.comment_status)
                    yield Static(detail_rendering.literal(f"评论状态：{comment_status}"))
                if request.publication_error:
                    yield Static(detail_rendering.literal(f"交付错误：{request.publication_error}"), classes="approval-error")
            with Vertical(id="approval-footer"):
                yield Select(
                    [(detail_rendering.literal(f"{member.name} · {member.id}"), member.id) for member in request.approvers],
                    prompt="选择 ONES 审批成员（必选）", id="actor",
                    disabled=not request.approvers,
                )
                yield Static(request.approver_error or ("" if request.approvers else "暂无可选 ONES 成员，请重新打开审批加载。"), id="modal-notice", markup=False)
                with Horizontal(id="approval-buttons"):
                    yield Button("返回", id="cancel-action")
                    yield Button("批准创建 Draft PR" if request.draft_pr else "批准创建 PR", id="confirm-approve", variant="warning", disabled=not request.approvers)

    def _confirm(self) -> None:
        actor = self.query_one("#actor", Select).value
        if not isinstance(actor, str) or actor not in {member.id for member in self.request.approvers}:
            self._notice("请从 ONES 成员列表选择审批人")
            return
        self.dismiss(ApprovalSubmission(self.request, actor))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-approve":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


class RevisionModal(_DangerousActionModal):
    """Collect bounded revision feedback and an explicit workflow scope."""

    def __init__(self, request: DangerousActionRequest, *, review_repair: bool = False) -> None:
        super().__init__(request, screen_id="revision-modal")
        self._review_repair = review_repair

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("确认继续修复" if self._review_repair else "Revise workflow")
            yield Static(f"work item: {self.request.work_item_id}")
            if self._review_repair:
                yield Static("本轮自动回修已暂停。确认后处理最新 Review 问题，重新测试、复审；不授权发布。可补充新的修复方向。")
            yield Input(value="继续修复最新 Review 中的待处理问题，保留已完成修复和冻结测试。" if self._review_repair else "", placeholder="Revision feedback", id="feedback")
            yield Input(value="repair" if self._review_repair else "", placeholder="implementation or repair", id="scope", disabled=self._review_repair)
        yield Static("", id="modal-notice", markup=False)
        yield Button("确认继续修复" if self._review_repair else "Revise", id="confirm-revise", variant="warning")
        yield Button("Back", id="cancel-action")

    def _confirm(self) -> None:
        feedback = _valid_action_text(
            self.query_one("#feedback", Input).value, maximum=4096
        )
        scope = self.query_one("#scope", Input).value.strip()
        if feedback is None or scope not in {"implementation", "repair"}:
            self._notice(_ACTION_INPUT_REQUIRED)
            return
        self.dismiss(RevisionSubmission(self.request, feedback, scope))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-revise":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


class CancelModal(_DangerousActionModal):
    """Require an actor before cancelling an authoritative run version."""

    def __init__(self, request: DangerousActionRequest) -> None:
        super().__init__(request, screen_id="cancel-modal")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Cancel workflow")
            yield Static(f"work item: {self.request.work_item_id}")
            yield Input(placeholder="Cancellation actor", id="actor")
        yield Static("", id="modal-notice", markup=False)
        yield Button("Cancel workflow", id="confirm-cancel", variant="error")
        yield Button("Back", id="cancel-action")

    def _confirm(self) -> None:
        actor = _valid_action_text(
            self.query_one("#actor", Input).value, maximum=128
        )
        if actor is None:
            self._notice(_ACTION_INPUT_REQUIRED)
            return
        self.dismiss(CancelSubmission(self.request, actor))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-cancel":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


class PublicationResumeModal(_DangerousActionModal):
    """Show persisted per-repository publication checkpoints before retry."""

    def __init__(self, request: DangerousActionRequest) -> None:
        super().__init__(request, screen_id="publication-resume-modal")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Resume publication")
            yield Static(self.request.fingerprint, id="fingerprint")
            for repository in self.request.repositories:
                yield from _repository_action_facts(repository)
            yield Static(f"comment: {self.request.comment_status}")
            yield Static(
                f"publication error: {self.request.publication_error or 'none'}"
            )
        yield Static("", id="modal-notice", markup=False)
        yield Button(
            "Resume publication",
            id="confirm-resume-publication",
            variant="warning",
        )
        yield Button("Back", id="cancel-action")

    def _confirm(self) -> None:
        self.dismiss(self.request)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-resume-publication":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


class WorkspaceCreateScreen(Screen[WorkspaceSummary | None]):
    """Create one workspace with an ONES scope and multiple repositories."""

    BINDINGS = [Binding("escape", "cancel", "Back", priority=True)]

    def __init__(
        self, controller: TuiController, supervisor: RunTaskSupervisor
    ) -> None:
        super().__init__(id="workspace-create-screen")
        self._controller = controller
        self._supervisor = supervisor
        self._repositories: list[WorkspaceRepositoryInput] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="workspace-create-body"):
            yield Label("Create workspace", classes="pane-title")
            yield Label("工作区名称（支持中文和空格，可稍后修改）")
            yield Input(placeholder="例如：桌面端 回归测试", id="workspace-name", max_length=128)
            yield Select([], prompt="Loading ONES projects…", id="workspace-project", disabled=True)
            yield Select([], prompt="Select project first", id="workspace-iteration", disabled=True)
            yield Label("Repositories (the first repository is primary)")
            yield Select(
                (("Local repository", "local"), ("Remote repository", "remote")),
                value="local",
                id="workspace-repository-kind",
            )
            yield Input(
                placeholder="Absolute local path or remote Git URL (name detected automatically)",
                id="workspace-repository-source",
            )
            yield Input(value="main", placeholder="Base branch", id="workspace-repository-branch")
            yield Button("Add repository", id="workspace-add-repository")
            yield Static("No repositories added", id="workspace-repositories", markup=False)
            yield Button("Create workspace", id="workspace-save", variant="primary", disabled=True)
            yield Button("Back", id="workspace-cancel")
            yield Static("", id="workspace-create-notice", markup=False)

    async def on_mount(self) -> None:
        try:
            projects = await self._supervisor.run_readonly(
                "load-workspace-projects",
                self._controller.load_workspace_projects,
            )
        except Exception:
            self._notice("ONES projects are unavailable")
            return
        select = self.query_one("#workspace-project", Select)
        select.set_options((item.name, item.id) for item in projects)
        select.disabled = not projects
        if projects:
            select.value = projects[0].id

    @on(Select.Changed, "#workspace-project")
    async def _project_changed(self, event: Select.Changed) -> None:
        if type(event.value) is not str or not event.value:
            return
        iteration = self.query_one("#workspace-iteration", Select)
        iteration.disabled = True
        try:
            choices = await self._supervisor.run_readonly(
                "load-workspace-iterations",
                self._controller.load_workspace_iterations,
                event.value,
            )
        except Exception:
            self._notice("ONES iterations are unavailable")
            return
        iteration.set_options((item.name, item.id) for item in choices)
        iteration.disabled = not choices
        if choices:
            iteration.value = choices[0].id
            workspace_name = self.query_one("#workspace-name", Input)
            if not workspace_name.value.strip():
                workspace_name.value = _workspace_key_from_scope(
                    event.value, choices[0].id
                )
        self._notice("")

    @on(Button.Pressed, "#workspace-add-repository")
    def _add_repository(self) -> None:
        kind = self.query_one("#workspace-repository-kind", Select).value
        source = self.query_one("#workspace-repository-source", Input).value.strip()
        branch = self.query_one("#workspace-repository-branch", Input).value.strip()
        if (
            type(kind) is not str
            or kind not in {"local", "remote"}
            or not source
            or not branch
        ):
            self._notice("Repository fields are invalid")
            return
        key = _repository_name_from_source(source, tuple(self._repositories))
        self._repositories.append(
            WorkspaceRepositoryInput(
                key=key,
                name=key,
                source=source,
                local=kind == "local",
                branch=branch,
            )
        )
        self.query_one("#workspace-repository-source", Input).value = ""
        self.query_one("#workspace-save", Button).disabled = False
        self.query_one("#workspace-repositories", Static).update(
            "\n".join(
                f"{index + 1}. {item.name} ({'local' if item.local else 'remote'})"
                for index, item in enumerate(self._repositories)
            )
        )
        self._notice("")

    @on(Button.Pressed, "#workspace-save")
    async def _save(self) -> None:
        project = self.query_one("#workspace-project", Select).value
        iteration = self.query_one("#workspace-iteration", Select).value
        if type(project) is not str or not project:
            self._notice("Select an ONES project")
            return
        if type(iteration) is not str or not iteration:
            self._notice("Select an ONES iteration")
            return
        if not self._repositories:
            self._notice("Add at least one repository")
            return
        entered_name = self.query_one("#workspace-name", Input).value.strip()
        if not hasattr(self, "_workspace_id"):
            self._workspace_id = f"workspace-{uuid.uuid4().hex}"
        key = self._workspace_id
        try:
            created = await asyncio.to_thread(
                self._controller.create_workspace,
                key,
                project,
                iteration,
                tuple(self._repositories),
                display_name=entered_name or f"{project}-{iteration}",
            )
        except Exception:
            self._notice("Workspace could not be saved")
            return
        self.dismiss(created)

    @on(Button.Pressed, "#workspace-cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _notice(self, value: str) -> None:
        self.query_one("#workspace-create-notice", Static).update(value)


class TaskDeleteConfirmation(ModalScreen[bool]):
    """Require explicit confirmation before deleting local task data."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, summary: RunSummary) -> None:
        super().__init__(id="task-delete-confirmation")
        self.summary = summary

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Delete task", classes="pane-title")
            yield Static(
                f"Delete task for '{self.summary.work_item_id}'?\n"
                "Task history, prompts, and analysis logs will be removed. "
                "Repositories, worktrees, and ONES data will not be deleted.",
                markup=False,
            )
            yield Button(
                "Delete task",
                id="confirm-task-delete",
                variant="error",
            )
            yield Button("Keep task", id="cancel-task-delete")

    @on(Button.Pressed, "#confirm-task-delete")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-task-delete")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True, slots=True)
class RepositoryMappingSelection:
    run_id: str
    version: int
    mapping_key: str


class RepositoryMappingModal(ModalScreen[RepositoryMappingSelection | None]):
    """Resume the repository-confirmation checkpoint from the task detail page."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, detail: RunDetail) -> None:
        super().__init__(id="repository-mapping-modal")
        self.detail = detail

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Select an authorized repository mapping", classes="pane-title")
            for index, candidate in enumerate(self.detail.mapping_candidates):
                yield Button(
                    f"Select {candidate.key}",
                    id=f"resume-mapping-{index}",
                    variant="primary" if index == 0 else "default",
                )
                yield Static(_mapping_candidate_text(candidate), markup=True)
            yield Button("Cancel", id="cancel-repository-mapping")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "cancel-repository-mapping":
            self.action_cancel()
            return
        if not button_id.startswith("resume-mapping-"):
            return
        try:
            index = int(button_id.removeprefix("resume-mapping-"))
            candidate = self.detail.mapping_candidates[index]
        except (ValueError, IndexError):
            self.action_cancel()
            return
        self.dismiss(RepositoryMappingSelection(
            run_id=self.detail.summary.run_id,
            version=self.detail.summary.version,
            mapping_key=candidate.key,
        ))

    def action_cancel(self) -> None:
        self.dismiss(None)


class WorkspaceDeleteConfirmation(ModalScreen[bool]):
    """Require an explicit click before removing a workspace configuration."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, workspace: WorkspaceSummary) -> None:
        super().__init__(id="workspace-delete-confirmation")
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Delete workspace", classes="pane-title")
            yield Static(
                f"Delete workspace '{self.workspace.label}' (ID: {self.workspace.key}) from this app?\n"
                "Local repositories, remote repositories, and ONES data will not be deleted.",
                markup=False,
            )
            yield Button(
                "Delete workspace",
                id="confirm-workspace-delete",
                variant="error",
            )
            yield Button("Keep workspace", id="cancel-workspace-delete")

    @on(Button.Pressed, "#confirm-workspace-delete")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-workspace-delete")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(False)


class WorkspaceRenameScreen(ModalScreen[WorkspaceSummary | None]):
    DEFAULT_CSS = """
    WorkspaceRenameScreen { align: center middle; }
    WorkspaceRenameScreen > VerticalScroll {
        width: 90%; max-width: 80; height: auto; max-height: 90%;
        border: round $primary; padding: 1 2;
    }
    WorkspaceRenameScreen Horizontal { height: 3; }
    """
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, controller, supervisor, workspace: WorkspaceSummary) -> None:
        super().__init__()
        self.controller, self.supervisor, self.workspace = controller, supervisor, workspace
        self._saving = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("重命名工作区")
            yield Static(f"内部 ID：{self.workspace.key}\n只修改显示名称，不影响任务和定时计划。", markup=False)
            yield Input(self.workspace.label, max_length=128, id="workspace-rename-name")
            yield Static("", id="workspace-rename-error", markup=False)
            with Horizontal():
                yield Button("取消", id="workspace-rename-cancel")
                yield Button("保存名称", id="workspace-rename-save", variant="primary")

    @on(Button.Pressed)
    async def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "workspace-rename-cancel":
            self.action_cancel()
        elif event.button.id == "workspace-rename-save" and not self._saving:
            self._saving = True
            event.button.disabled = True
            try:
                workspace = await self.supervisor.run_readonly("rename-workspace", self.controller.rename_workspace,
                    self.workspace.key, self.query_one(Input).value)
                self.dismiss(workspace)
            except Exception:
                self.query_one("#workspace-rename-error", Static).update("保存失败，请检查名称（1～128 字符）和配置权限。")
                event.button.disabled = False
            finally:
                self._saving = False

    def action_cancel(self) -> None:
        if not self._saving:
            self.dismiss(None)


class WorkspaceDetailScreen(Screen[bool]):
    """Workspace-scoped entry point for defect queries."""

    BINDINGS = [Binding("escape", "back", "Back", priority=True)]

    def __init__(
        self,
        controller: TuiController,
        supervisor: RunTaskSupervisor,
        workspace: WorkspaceSummary,
    ) -> None:
        super().__init__(id="workspace-detail-screen")
        self._controller = controller
        self._supervisor = supervisor
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace-detail-body"):
            yield Static(Text(self.workspace.label, style="bold cyan"), id="workspace-heading")
            yield Static(f"内部 ID：{self.workspace.key}", markup=False, id="workspace-identity")
            yield Static(
                f"项目 {self.workspace.project_id}  ·  迭代 {self.workspace.iteration_id}  ·  "
                f"{len(self.workspace.repositories)} 个仓库", markup=False, id="workspace-scope")
            with TabbedContent(id="workspace-modules"):
                with TabPane("查询缺陷", id="workspace-defects-tab"):
                    with VerticalScroll(classes="workspace-module-body"):
                        yield Static("缺陷分析与修复", classes="workspace-module-title")
                        yield Static("查询当前项目和迭代的缺陷，按负责人、状态筛选，再选择仅分析或分析并修复。",
                                     classes="workspace-module-description")
                        yield Button("查询缺陷", id="workspace-query-defects", variant="primary")
                        with Collapsible(title="关联仓库", collapsed=True):
                            yield Static("\n".join(self.workspace.repositories), markup=False)
                with TabPane("查询需求", id="workspace-requirements-tab"):
                    with VerticalScroll(classes="workspace-module-body"):
                        yield Static("需求查询与实现", classes="workspace-module-title")
                        yield Static("自动带入本工作区项目和迭代。查询需求后选择目标，继续仓库映射与实现流程。",
                                     classes="workspace-module-description")
                        yield Button("查询需求", id="workspace-query-requirements", variant="primary")
                with TabPane("任务列表", id="workspace-tasks-tab"):
                    yield Static("本工作区的缺陷与需求任务；未绑定工作区的任务请到全局 Tasks 查看。",
                                 classes="workspace-module-description")
                    yield Button("刷新任务", id="workspace-refresh-tasks")
                    yield Static("尚未加载", id="workspace-task-status", markup=False)
                    yield ListView(id="workspace-task-list")
                with TabPane("定时任务", id="workspace-schedules-tab"):
                    yield SchedulePane(self._controller, self._supervisor, self.workspace)
        yield Static("", id="workspace-detail-notice", markup=False)
        with Horizontal(id="workspace-detail-footer"):
            yield Button("返回工作区", id="workspace-detail-back")
            yield Button("重命名", id="workspace-rename")
            yield Button("删除工作区", id="workspace-delete", variant="error")

    @on(Button.Pressed, "#workspace-rename")
    def _rename_workspace(self) -> None:
        self.app.push_screen(WorkspaceRenameScreen(self._controller, self._supervisor, self.workspace),
                             callback=self._renamed)

    def _renamed(self, workspace: WorkspaceSummary | None) -> None:
        if workspace is not None:
            self.workspace = workspace
            self.query_one("#workspace-heading", Static).update(Text(workspace.label, style="bold cyan"))

    @on(Button.Pressed, "#workspace-query-requirements")
    def _query_requirements(self) -> None:
        self.app.push_screen(RequirementWizardScreen(
            self._controller, self._supervisor, workspace=self.workspace,
        ), callback=self._workflow_started)

    @on(TabbedContent.TabActivated, "#workspace-modules")
    async def _module_changed(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "workspace-tasks-tab":
            await self._refresh_tasks()

    @on(Button.Pressed, "#workspace-refresh-tasks")
    async def _refresh_tasks(self) -> None:
        status = self.query_one("#workspace-task-status", Static)
        button = self.query_one("#workspace-refresh-tasks", Button)
        button.disabled = True
        status.update("正在加载任务…")
        listing = self.query_one("#workspace-task-list", ListView)
        try:
            runs = await self._supervisor.run_readonly(
                "workspace-tasks", self._controller.list_workspace_runs, self.workspace)
            await listing.clear()
            await listing.extend([
                self._workspace_task_card(item)
                for item in runs
            ])
            status.update(f"共 {len(runs)} 项任务 · 点击卡片或 ↑↓ 选择后按 Enter 查看详情" if runs else "本工作区暂无已绑定任务")
        except Exception:
            await listing.clear()
            status.update("任务加载失败，请重试")
        finally:
            button.disabled = False

    @staticmethod
    def _workspace_task_card(item: RunSummary) -> ListItem:
        state = item.state.value
        tone = ("attention" if state in {"BLOCKED", "WAITING_APPROVAL", "WAITING_PR_VERIFICATION", "PARTIAL_SUCCESS"}
                else "failed" if state == "FAILED"
                else "complete" if state == "COMPLETED" else "normal")
        return ListItem(
            Vertical(
                Static(Text.from_markup(
                    f"{'缺陷' if item.workflow_type is WorkflowType.DEFECT else '需求'}  ·  {item.work_item_id}"),
                    markup=False, classes="workspace-task-title"),
                Static(f"{detail_rendering.state_name(state)}  ·  {state}",
                       markup=False, classes="workspace-task-state"),
                Static(f"更新时间：{item.updated_at.astimezone().strftime('%Y-%m-%d %H:%M')}  ·  版本 {item.version}",
                       markup=False, classes="workspace-task-meta"),
                Static(Text.from_markup(f"任务 ID：{item.run_id}"), markup=False,
                       classes="workspace-task-meta"),
                Static("查看任务详情 →", classes="workspace-task-open"),
                classes="workspace-task-content"),
            name=item.run_id, classes=f"workspace-task-card {tone}")

    @on(ListView.Selected, "#workspace-task-list")
    async def _open_workspace_task(self, event: ListView.Selected) -> None:
        if event.item.name:
            try:
                detail = await self._supervisor.run_readonly(
                    "workspace-task-detail", self._controller.show, event.item.name)
                self._workflow_started(detail)
            except Exception:
                self.query_one("#workspace-task-status", Static).update("任务详情暂不可用，请刷新后重试")

    @on(Button.Pressed, "#workspace-query-defects")
    def _query_defects(self) -> None:
        self.app.push_screen(
            DefectWizardScreen(
                self._controller,
                self._supervisor,
                workspace=self.workspace,
            ),
            callback=self._workflow_started,
        )

    def _workflow_started(self, detail: RunDetail | None) -> None:
        if detail is not None:
            self.app.push_screen(RunDetailScreen(
                detail,
                controller=self._controller,
                supervisor=self._supervisor,
            ))

    @on(Button.Pressed, "#workspace-delete")
    def _delete_workspace(self) -> None:
        self.app.push_screen(
            WorkspaceDeleteConfirmation(self.workspace),
            callback=self._delete_confirmed,
        )

    async def _delete_confirmed(self, confirmed: bool | None) -> None:
        if confirmed is not True:
            return
        delete_button = self.query_one("#workspace-delete", Button)
        delete_button.disabled = True
        try:
            await asyncio.to_thread(
                self._controller.delete_workspace,
                self.workspace.key,
            )
        except Exception:
            delete_button.disabled = False
            self.query_one("#workspace-detail-notice", Static).update(
                "Workspace could not be deleted"
            )
            return
        self.dismiss(True)

    @on(Button.Pressed, "#workspace-detail-back")
    def _back_pressed(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.dismiss(False)


class DashboardScreen(Screen[None]):
    """Responsive three/two/one-column workflow dashboard."""

    BINDINGS = [
        Binding("j", "cursor_down", "Next run", show=False, priority=True),
        Binding("down", "cursor_down", "Next run", show=False, priority=True),
        Binding("k", "cursor_up", "Previous run", show=False, priority=True),
        Binding("up", "cursor_up", "Previous run", show=False, priority=True),
        Binding("enter", "open_run", "Open run", show=False, priority=True),
        Binding("tab", "next_tab", "Next tab", show=False, priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("s", "show_settings", "Settings", show=False, priority=True),
        Binding("g", "show_runs", "Workspaces", show=False, priority=True),
        Binding("n", "new_run", "New run", show=False, priority=True),
        Binding("r", "resume", "Resume", show=False, priority=True),
        Binding("v", "revise", "Revise", show=False, priority=True),
        Binding("a", "approve", "Approve", show=False, priority=True),
        Binding("x", "cancel_run", "Cancel", show=False, priority=True),
        Binding("delete", "delete_task", "Delete task", show=False, priority=True),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        settings = self.query("#settings-page")
        if settings and settings.first().display and action in {
            "cursor_down", "cursor_up", "open_run", "resume", "revise", "approve", "cancel_run", "delete_task",
        }:
            # Let the focused configuration list/button handle arrows and Enter;
            # never route configuration input to the hidden selected task.
            return False
        return True

    def __init__(
        self,
        controller: TuiController,
        supervisor: RunTaskSupervisor,
        settings: SettingsView,
        *,
        publishing_enabled: bool = True,
        initial_settings_tab: str | None = None,
    ) -> None:
        super().__init__(id="dashboard-screen")
        self._initial_settings_tab = initial_settings_tab
        if type(publishing_enabled) is not bool:
            raise ValueError("publishing capability is invalid")
        self._controller = controller
        self._supervisor = supervisor
        self._settings = settings
        self._publishing_enabled = publishing_enabled
        self._runs: tuple[RunSummary, ...] = ()
        self._detail_error = ""
        self._refreshing = False
        self._refresh_requested = False
        self._refresh_activities: Mapping[str, RunActivity] | None = None
        self._refresh_done = asyncio.Event()
        self._refresh_done.set()
        self._detail_sequence = 0
        self._selection_generation = 0
        self._applying_refresh = False
        self._mount_generation = 0
        self._lifecycle_active = False
        self._teardown_started = False
        self._filters = RunFilter()
        self._workspaces: tuple[WorkspaceSummary, ...] = ()
        self._workspace_mode = callable(getattr(controller, "list_workspaces", None))
        self._selected_detail: RunDetail | None = None
        self._workflow_watchers: set[asyncio.Task[None]] = set()
        self._workflow_running_run_ids: set[str] = set()
        self._pending_delete_run_id: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="dashboard", classes="three"):
            yield NavigationPane(workspace_mode=self._workspace_mode, id="navigation")
            with Vertical(id="workspace-home"):
                yield WorkspaceListPane(id="workspace-list-pane")
            with Horizontal(id="workspace"):
                yield RunListPane(id="run-list-pane")
                with Vertical(id="task-detail-column"):
                    yield RunDetailPane(id="run-detail")
                    with Horizontal(id="action-bar"):
                        yield Button("审批并创建 PR", id="action-approve", variant="warning")
                        yield Button(
                            "Continue task",
                            id="action-resume",
                            variant="primary",
                        )
                        yield Button(
                            "Accept solution",
                            id="action-accept-analysis",
                            variant="success",
                        )
                        yield Button(
                            "Regenerate solution",
                            id="action-regenerate-analysis",
                        )
                        yield Button(
                            "Repair result format",
                            id="action-retry-analysis",
                            variant="warning",
                        )
            with Vertical(id="settings-page"):
                with TabbedContent(initial="settings-ones", id="configuration-tabs"):
                    with TabPane("ONES 配置", id="settings-ones"):
                        yield OnesSettingsPane()
                    with TabPane("GitHub / GitLab", id="settings-provider"):
                        yield ProviderSettingsPane()
                    with TabPane("Coding Agent", id="settings-agent"):
                        yield CodingAgentSettingsPane()
                    with TabPane("验证节点", id="settings-nodes"):
                        yield VerificationNodesPane(self._controller, self._supervisor)
                    with TabPane("运行信息", id="settings-runtime"):
                        with VerticalScroll():
                            yield Static(Text(self._settings.display_text()), id="settings", markup=False)
        yield Static("", id="notice", markup=False)

    def on_mount(self) -> None:
        self._mount_generation += 1
        self._lifecycle_active = True
        self._teardown_started = False
        self._set_mode(self.size.width)
        self.query_one("#workspace-home").display = self._workspace_mode
        self.query_one("#workspace").display = not self._workspace_mode
        self.query_one("#settings-page").display = False
        self._set_analysis_actions(None)
        if self._initial_settings_tab is not None:
            self.query_one("#configuration-tabs", TabbedContent).active = self._initial_settings_tab
            self.action_show_settings()

    def _set_analysis_actions(self, detail: RunDetail | None) -> None:
        self._selected_detail = detail
        accept = self.query_one("#action-accept-analysis", Button)
        regenerate = self.query_one("#action-regenerate-analysis", Button)
        retry = self.query_one("#action-retry-analysis", Button)
        resume = self.query_one("#action-resume", Button)
        approve = self.query_one("#action-approve", Button)
        workflow_running = bool(
            detail
            and detail.summary.run_id in self._workflow_running_run_ids
        )
        approve.display = bool(
            detail
            and not workflow_running
            and detail.summary.state is WorkflowState.WAITING_APPROVAL
        )
        approve.disabled = not self._publishing_enabled
        approve.label = (
            "MVP 未启用 PR 发布"
            if not self._publishing_enabled
            else "审批并创建 Draft PR"
            if detail and detail.draft_pr
            else "审批并创建 PR"
        )
        accept.display = bool(
            detail and detail.can_accept_analysis and not workflow_running
        )
        regenerate.display = bool(
            detail and detail.can_regenerate_analysis and not workflow_running
        )
        retry.display = bool(
            detail
            and not workflow_running
            and detail.summary.state is WorkflowState.BLOCKED
            and detail.resume_state is WorkflowState.IMPLEMENTING
            and detail.status_message == "Codex result format repair failed"
        )
        resumable = bool(
            detail
            and not workflow_running
            and detail.summary.state is WorkflowState.BLOCKED
            and detail.resume_state is not None
            and detail.resume_state is not WorkflowState.PUBLISHING
        )
        mapping_required = bool(
            detail
            and not workflow_running
            and detail.summary.state is WorkflowState.VALIDATING
            and detail.mapping_candidates
        )
        resume.display = (resumable or mapping_required) and not retry.display
        if resume.display and detail is not None:
            resume.label = (
                "重新检查审批条件" if detail.status_message.startswith(("缺少修复前失败复现记录", "远程目标分支已变化", "Git 命令执行失败", "流程内部检查失败")) else
                "环境验证" if detail.can_verify else
                "Continue repair" if detail.can_request_review_repair else "Select repository"
                if mapping_required
                else {
                    WorkflowState.IMPLEMENTING: "Continue repair",
                    WorkflowState.TESTING: "Continue testing",
                    WorkflowState.AI_REVIEW: "Continue review",
                    WorkflowState.WAITING_APPROVAL: "Continue task",
                }.get(detail.resume_state, "Resume task")
            )
        self.query_one("#action-bar").display = bool(
            self.query_one("#workspace").display
            and (resume.display or approve.display or accept.display or regenerate.display or retry.display)
        )

    def _set_active_navigation(self, active_id: str) -> None:
        """Keep the selected navigation highlight aligned with the visible page."""

        for button in self.query("#navigation Button"):
            if isinstance(button, Button):
                button.variant = (
                    "primary" if button.id == active_id else "default"
                )

    async def refresh_workspaces(self) -> None:
        """Refresh only the top-level workspace list."""

        if not self._lifecycle_active or not self._workspace_mode:
            return
        try:
            workspaces = await asyncio.to_thread(self._controller.list_workspaces)
        except Exception:
            self.query_one("#notice", Static).update(
                "Workspace list is unavailable"
            )
            return
        self._workspaces = workspaces
        await self.query_one(WorkspaceListPane).replace_workspaces(workspaces)
        self.query_one("#notice", Static).update("")

    def on_unmount(self) -> None:
        self.begin_teardown()

    def begin_teardown(self) -> None:
        """Synchronously invalidate refresh ownership before DOM pruning."""

        if self._teardown_started:
            return
        self._teardown_started = True
        self._lifecycle_active = False
        self._mount_generation += 1
        self._refresh_requested = False

    @property
    def mount_generation(self) -> int:
        """Return the current widget-tree generation for refresh ownership."""

        return self._mount_generation

    def owns_refresh(self, generation: int) -> bool:
        """Whether a refresh may still read or update this mounted tree."""

        return (
            self._lifecycle_active
            and self.is_mounted
            and generation == self._mount_generation
        )

    def is_tearing_down(self) -> bool:
        """Whether Textual has begun closing even if unmount is not delivered."""

        return self._teardown_started or not self.app.is_running

    def on_resize(self, event: events.Resize) -> None:
        self._set_mode(event.size.width)

    def _set_mode(self, width: int) -> None:
        dashboard = self.query_one("#dashboard")
        dashboard.remove_class("three", "two", "one")
        mode = "three" if width >= 100 else "two" if width >= 70 else "one"
        dashboard.add_class(mode)

    async def refresh_runs(
        self,
        activities: Mapping[str, RunActivity] | None = None,
        *,
        mount_generation: int | None = None,
    ) -> None:
        generation = (
            self._mount_generation
            if mount_generation is None
            else mount_generation
        )
        if not self.owns_refresh(generation):
            return
        self._refresh_activities = activities
        if self._refreshing:
            self._refresh_requested = True
            await self._refresh_done.wait()
            return
        self._refreshing = True
        self._refresh_done.clear()
        try:
            while True:
                self._refresh_requested = False
                current_activities = self._refresh_activities
                try:
                    await self._refresh_runs(current_activities, generation)
                except NoMatches:
                    if (
                        not self.owns_refresh(generation)
                        or self.is_tearing_down()
                    ):
                        return
                    raise
                if not self._refresh_requested:
                    break
        finally:
            self._refreshing = False
            self._refresh_done.set()

    async def _refresh_runs(
        self,
        activities: Mapping[str, RunActivity] | None,
        mount_generation: int,
    ) -> None:
        if not self.owns_refresh(mount_generation):
            return
        selected_index = self._selected_index()
        selected_run_id = (
            self._runs[selected_index].run_id
            if selected_index is not None
            and 0 <= selected_index < len(self._runs)
            else None
        )
        selection_generation = self._selection_generation
        try:
            runs = await asyncio.to_thread(
                self._controller.list_runs,
                self._filters,
                activities,
            )
        except TuiControllerError:
            if not self.owns_refresh(mount_generation):
                return
            runs = ()
            self._detail_sequence += 1
            self._runs = runs
            await self.query_one(RunListPane).replace_runs(runs)
            self._detail_error = _LIST_UNAVAILABLE
            self.query_one(RunDetailPane).show_error(self._detail_error)
            return
        if not self.owns_refresh(mount_generation):
            return
        if selection_generation != self._selection_generation:
            current_index = self._selected_index()
            selected_run_id = (
                self._runs[current_index].run_id
                if current_index is not None
                and 0 <= current_index < len(self._runs)
                else None
            )
        self._detail_sequence += 1
        self._runs = runs
        self._applying_refresh = True
        try:
            await self.query_one(RunListPane).replace_runs(runs)
        finally:
            self._applying_refresh = False
        if not runs:
            self._detail_error = ""
            self.query_one(RunDetailPane).clear_detail()
            self._set_analysis_actions(None)
            return
        target = next(
            (
                index
                for index, item in enumerate(runs)
                if item.run_id == selected_run_id
            ),
            0,
        )
        run_list = self.query_one("#run-list", ListView)
        if run_list.index != target:
            run_list.index = target
        await self._show_detail(target)

    def _selected_index(self) -> int | None:
        return self.query_one("#run-list", ListView).index

    async def _show_detail(self, index: int) -> RunDetail | None:
        summary = self._runs[index]
        self._detail_sequence += 1
        sequence = self._detail_sequence
        if summary.corrupted:
            self._detail_error = _STORAGE_CORRUPTED
            self.query_one(RunDetailPane).show_error(self._detail_error)
            return None
        try:
            detail = await asyncio.to_thread(
                self._controller.show, summary.run_id
            )
        except TuiControllerError:
            if not self.is_mounted or sequence != self._detail_sequence:
                return None
            self._detail_error = _DISPLAY_UNAVAILABLE
            self.query_one(RunDetailPane).show_error(self._detail_error)
            return None
        if not self.is_mounted or sequence != self._detail_sequence:
            return None
        self._detail_error = ""
        self.query_one(RunDetailPane).set_detail(detail)
        self._set_analysis_actions(detail)
        return detail

    def action_cursor_down(self) -> None:
        self._selection_generation += 1
        self.query_one("#run-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self._selection_generation += 1
        self.query_one("#run-list", ListView).action_cursor_up()

    async def action_open_run(self) -> None:
        index = self._selected_index()
        if index is None or not 0 <= index < len(self._runs):
            return
        detail = await self._show_detail(index)
        if self.query_one("#dashboard").has_class("one"):
            self.app.push_screen(
                RunDetailScreen(
                    detail,
                    error=self._detail_error,
                    controller=self._controller,
                    supervisor=self._supervisor,
                )
            )

    def action_next_tab(self) -> None:
        if self.query_one("#settings-page").display:
            self._step_configuration_tab(1)
            return
        self.query_one(RunDetailPane).next_tab()

    def action_previous_tab(self) -> None:
        if self.query_one("#settings-page").display:
            self._step_configuration_tab(-1)
            return
        self.query_one(RunDetailPane).previous_tab()

    def _step_configuration_tab(self, step: int) -> None:
        tabs = self.query_one("#configuration-tabs", TabbedContent)
        names = (
            "settings-ones", "settings-provider", "settings-agent",
            "settings-nodes", "settings-runtime",
        )
        tabs.active = names[(names.index(tabs.active) + step) % len(names)]

    def rebind_runtime(self, controller: TuiController, supervisor: RunTaskSupervisor) -> None:
        """Keep the mounted configuration shell while replacing closed runtime owners."""
        self._controller = controller
        self._supervisor = supervisor
        self._mount_generation += 1
        self._teardown_started = False
        self._lifecycle_active = True
        nodes = self.query_one(VerificationNodesPane)
        nodes.controller = controller
        nodes.supervisor = supervisor
        nodes.loaded = False
        self.disabled = False

    def action_show_settings(self) -> None:
        self.query_one("#workspace-home").display = False
        self.query_one("#workspace").display = False
        self.query_one("#settings-page").display = True
        self.query_one("#action-bar").display = False
        self._set_active_navigation("nav-settings")
        self.run_worker(self.query_one(OnesSettingsPane).load())
        self.run_worker(self.query_one(ProviderSettingsPane).load())
        self.run_worker(self.query_one(CodingAgentSettingsPane).load())
        if self.query_one("#configuration-tabs", TabbedContent).active == "settings-nodes":
            self.run_worker(self.query_one(VerificationNodesPane).load_nodes())

    @on(TabbedContent.TabActivated, "#configuration-tabs")
    async def _configuration_tab_changed(self, event: TabbedContent.TabActivated) -> None:
        event.stop()
        if event.pane.id == "settings-nodes":
            await self.query_one(VerificationNodesPane).load_nodes()
        elif event.pane.id == "settings-agent":
            await self.query_one(CodingAgentSettingsPane).load()

    def action_show_runs(self) -> None:
        self.query_one("#settings-page").display = False
        self.query_one("#workspace-home").display = False
        self.query_one("#workspace").display = True
        self._set_analysis_actions(self._selected_detail)
        self._set_active_navigation("nav-runs")

    def action_show_workspaces(self) -> None:
        if not self._workspace_mode:
            return
        self.query_one("#settings-page").display = False
        self.query_one("#workspace").display = False
        self.query_one("#workspace-home").display = True
        self.query_one("#action-bar").display = False
        self._set_active_navigation("nav-workspaces")

    def action_new_run(self) -> None:
        self.app.push_screen(
            WorkflowTypeScreen(self._controller, self._supervisor),
            callback=lambda detail: None,
        )

    def action_defects(self) -> None:
        self.app.push_screen(
            DefectWizardScreen(self._controller, self._supervisor),
            callback=lambda detail: None,
        )

    def action_requirements(self) -> None:
        self.app.push_screen(
            RequirementWizardScreen(self._controller, self._supervisor),
            callback=lambda detail: None,
        )

    def action_search(self) -> None:
        self.app.push_screen(
            RunFilterScreen(self._filters, search_only=True),
            callback=self._filter_done,
        )

    def action_filter(self) -> None:
        self.app.push_screen(
            RunFilterScreen(self._filters, search_only=False),
            callback=self._filter_done,
        )

    async def _filter_done(self, filters: RunFilter | None) -> None:
        if filters is None:
            return
        self._filters = filters
        await self.refresh_runs()

    def _selected_summary(self) -> RunSummary | None:
        index = self._selected_index()
        if index is None or not 0 <= index < len(self._runs):
            return None
        summary = self._runs[index]
        return None if summary.corrupted else summary

    def _show_action_notice(self, message: str) -> None:
        self.query_one("#notice", Static).update(message)

    async def _prepare_action(
        self, action: str
    ) -> DangerousActionRequest | None:
        summary = self._selected_summary()
        if summary is None:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return None
        try:
            return await self._supervisor.run_readonly(
                f"prepare-{action}",
                self._controller.prepare_action,
                summary.run_id,
                action,
            )
        except PublicationConfigurationError:
            self._show_action_notice("请先到 Configuration → 运行信息 → 配置 PR/MR 发布，设置平台令牌。")
            return None
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
            return None

    @staticmethod
    def _terminal(summary: RunSummary) -> bool:
        return summary.state in {
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
            WorkflowState.FAILED,
        }

    async def action_approve(self) -> None:
        if not self._publishing_enabled:
            self._show_action_notice("MVP 未启用 commit、push 或 PR 发布")
            return
        summary = self._selected_summary()
        if summary is None or summary.state is not WorkflowState.WAITING_APPROVAL:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        request = await self._prepare_action("approve")
        if request is not None and request.state is WorkflowState.WAITING_APPROVAL:
            self.app.push_screen(ApprovalModal(request), callback=self._approval_done)
        elif request is not None:
            self._show_action_notice(_ACTION_UNAVAILABLE)

    async def action_revise(self) -> None:
        summary = self._selected_summary()
        if summary is not None and self._supervisor.is_run_active(summary.run_id):
            self._show_action_notice("Workflow is already running; see AI activity")
            return
        if summary is None or summary.state not in {
            WorkflowState.WAITING_APPROVAL,
            WorkflowState.BLOCKED,
        }:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        request = await self._prepare_action("revise")
        allowed = request is not None and (
            request.state is WorkflowState.WAITING_APPROVAL
            or (
                request.state is WorkflowState.BLOCKED
                and request.resume_state
                in {
                    WorkflowState.IMPLEMENTING,
                    WorkflowState.TESTING,
                    WorkflowState.AI_REVIEW,
                    WorkflowState.WAITING_APPROVAL,
                }
            )
        )
        if allowed:
            assert request is not None
            self.app.push_screen(RevisionModal(request), callback=self._revision_done)
        elif request is not None:
            self._show_action_notice(_ACTION_UNAVAILABLE)

    async def action_cancel_run(self) -> None:
        summary = self._selected_summary()
        if summary is None or self._terminal(summary):
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        request = await self._prepare_action("cancel")
        if request is not None and request.state not in {
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
            WorkflowState.FAILED,
        }:
            self.app.push_screen(CancelModal(request), callback=self._cancel_done)
        elif request is not None:
            self._show_action_notice(_ACTION_UNAVAILABLE)

    def action_delete_task(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        if (
            summary.run_id in self._workflow_running_run_ids
            or self._supervisor.is_run_active(summary.run_id)
        ):
            self._show_action_notice("Running tasks cannot be deleted")
            return
        self._pending_delete_run_id = summary.run_id
        self.app.push_screen(
            TaskDeleteConfirmation(summary),
            callback=self._task_delete_confirmed,
        )

    async def _task_delete_confirmed(self, confirmed: bool | None) -> None:
        run_id = self._pending_delete_run_id
        self._pending_delete_run_id = None
        if confirmed is not True:
            return
        if run_id is None or not any(item.run_id == run_id for item in self._runs):
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        if (
            run_id in self._workflow_running_run_ids
            or self._supervisor.is_run_active(run_id)
        ):
            self._show_action_notice("Running tasks cannot be deleted")
            return
        try:
            await self._supervisor.run_mutation(
                run_id,
                "delete-task",
                self._controller.delete_task,
                run_id,
            )
        except Exception:
            self._show_action_notice("Task could not be deleted")
            return
        self._selected_detail = None
        self._show_action_notice("Task deleted")
        await self.refresh_runs()

    async def action_resume(self) -> None:
        summary = self._selected_summary()
        if summary is not None and self._supervisor.is_run_active(summary.run_id):
            self._show_action_notice("Workflow is already running; see AI activity")
            return
        if (
            summary is None
            or self._terminal(summary)
            or summary.state is WorkflowState.WAITING_APPROVAL
        ):
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        try:
            detail = await self._supervisor.run_readonly(
                "review-resume", self._controller.show, summary.run_id
            )
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
            return
        if detail.can_verify and detail.verification_tasks:
            nodes = await self._supervisor.run_readonly("verification-nodes", self._controller.verification_nodes)
            self.app.push_screen(VerificationModal(detail, nodes), callback=self._verification_done)
            return
        if detail.can_request_review_repair:
            request = await self._prepare_action("revise")
            if request is not None and request.version == detail.summary.version:
                self.app.push_screen(RevisionModal(request, review_repair=True), callback=self._revision_done)
            else:
                self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        if (
            detail.summary.state is WorkflowState.VALIDATING
            and detail.mapping_candidates
        ):
            self.app.push_screen(
                RepositoryMappingModal(detail),
                callback=self._repository_mapping_selected,
            )
            return
        publication_resume = (
            detail.summary.state
            in {WorkflowState.PARTIAL_SUCCESS, WorkflowState.PUBLISHING}
            or (
                detail.summary.state is WorkflowState.BLOCKED
                and detail.resume_state is WorkflowState.PUBLISHING
            )
        )
        if publication_resume:
            if not self._publishing_enabled:
                self._show_action_notice("MVP 未启用 commit、push 或 PR 发布")
                return
            request = await self._prepare_action("resume-publication")
            valid_publication_request = request is not None and (
                request.state
                in {WorkflowState.PARTIAL_SUCCESS, WorkflowState.PUBLISHING}
                or (
                    request.state is WorkflowState.BLOCKED
                    and request.resume_state is WorkflowState.PUBLISHING
                )
            )
            if valid_publication_request:
                assert request is not None
                self.app.push_screen(
                    PublicationResumeModal(request),
                    callback=self._publication_resume_done,
                )
            elif request is not None:
                self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        self._set_analysis_actions(None)
        self._show_action_notice(
            "Continuing workflow from "
            + (detail.resume_state.value if detail.resume_state is not None else "saved state")
        )
        self._submit_workflow_task(
            detail.summary.run_id,
            "resume",
            lambda: self._controller.resume(
                detail.summary.run_id,
                detail.summary.version,
            ),
        )

    def _verification_done(self, submission: VerificationSubmission | None) -> None:
        if submission is None:
            return
        if self._supervisor.is_run_active(submission.run_id):
            self._show_action_notice("Workflow is already running; see AI activity")
            return
        if submission.replan:
            self._submit_workflow_task(submission.run_id, "verification-plan", lambda:
                self._controller.replan_verification(submission.run_id, submission.version))
            return
        if submission.defer_to_pr:
            self._submit_workflow_task(submission.run_id, "resume", lambda:
                self._controller.resume(submission.run_id, submission.version))
            return
        self._show_action_notice("正在执行环境验证；完成后会自动推进，失败或缺少环境会保留具体状态。")
        self._submit_workflow_task(submission.run_id, "verify", lambda: self._controller.verify(
            submission.run_id, submission.task_key, submission.actor, submission.version,
            submission.evidence, submission.passed, submission.recipe_digest))

    def _repository_mapping_selected(
        self, selection: RepositoryMappingSelection | None
    ) -> None:
        if selection is None:
            return
        self._set_analysis_actions(None)
        self._show_action_notice("Repository mapping confirmed; continuing workflow")
        self._submit_workflow_task(
            selection.run_id,
            "confirm-repository",
            lambda: self._controller.confirm_repository(
                selection.run_id,
                selection.mapping_key,
                selection.version,
            ),
        )

    async def _run_analysis_decision(self, *, accept: bool) -> None:
        detail = self._selected_detail
        allowed = (
            detail is not None
            and (
                detail.can_accept_analysis
                if accept
                else detail.can_regenerate_analysis
            )
        )
        if not allowed:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        assert detail is not None
        self._set_analysis_actions(None)
        self._show_action_notice(
            "Repair workflow started" if accept else "Regenerating analysis solution"
        )
        call = (
            self._controller.accept_analysis_solution
            if accept
            else self._controller.regenerate_analysis_solution
        )
        self._submit_workflow_task(
            detail.summary.run_id,
            "accept-analysis" if accept else "regenerate-analysis",
            lambda: call(
                detail.summary.run_id,
                detail.summary.version,
            ),
        )

    def _submit_workflow_task(
        self,
        run_id: str,
        action: str,
        call: Callable[[], RunDetail],
    ) -> None:
        """Submit a long workflow without blocking Textual's event loop."""

        try:
            task = self._supervisor.submit(run_id, action, call)
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
            return
        self._workflow_running_run_ids.add(run_id)
        watcher = asyncio.create_task(
            self._finish_workflow_task(run_id, task),
            name=f"tui-watch-{action}-{run_id}",
        )
        self._workflow_watchers.add(watcher)
        watcher.add_done_callback(self._workflow_watchers.discard)

    async def _finish_workflow_task(
        self, run_id: str, task: asyncio.Task[RunDetail]
    ) -> None:
        try:
            await task
        except asyncio.CancelledError:
            return
        except StaleTuiActionError:
            if self._lifecycle_active:
                self._show_action_notice(_ACTION_STALE)
        except Exception:
            if self._lifecycle_active:
                self._show_action_notice(_ACTION_FAILED)
        finally:
            self._workflow_running_run_ids.discard(run_id)
        if self._lifecycle_active:
            await self.refresh_runs()

    def _retry_analysis(self) -> None:
        detail = self._selected_detail
        if (
            detail is None
            or detail.summary.state is not WorkflowState.BLOCKED
            or detail.resume_state is not WorkflowState.IMPLEMENTING
            or detail.status_message != "Codex result format repair failed"
        ):
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        self._set_analysis_actions(None)
        self._show_action_notice("Repairing structured analysis result")
        self._submit_workflow_task(
            detail.summary.run_id,
            "repair-analysis-format",
            lambda: self._controller.resume(
                detail.summary.run_id,
                detail.summary.version,
            ),
        )

    async def _approval_done(self, submission: object | None) -> None:
        if not isinstance(submission, ApprovalSubmission):
            return
        await self._run_dangerous(
            submission.request,
            "approve",
            self._controller.approve,
            submission.request,
            submission.actor,
        )

    async def _revision_done(self, submission: object | None) -> None:
        if not isinstance(submission, RevisionSubmission):
            return
        run_id = submission.request.run_id
        if self._supervisor.is_run_active(run_id):
            self._show_action_notice("Workflow is already running; see AI activity")
            return
        self._set_analysis_actions(None)
        self._show_action_notice("Repair workflow started; see AI activity")
        # Modal dismissal callbacks run on Textual's message pump. Awaiting the
        # entire repair here stalls UI messages even though Codex runs in a thread.
        self._submit_workflow_task(
            run_id,
            "revise",
            lambda: self._controller.revise(
                submission.request, submission.feedback, submission.scope,
            ),
        )

    async def _cancel_done(self, submission: object | None) -> None:
        if not isinstance(submission, CancelSubmission):
            return
        await self._run_dangerous(
            submission.request,
            "cancel",
            self._controller.cancel,
            submission.request,
            submission.actor,
        )

    async def _publication_resume_done(self, submission: object | None) -> None:
        if not isinstance(submission, DangerousActionRequest):
            return
        if not self._publishing_enabled:
            self._show_action_notice("MVP 未启用 commit、push 或 PR 发布")
            return
        await self._run_dangerous(
            submission,
            "resume-publication",
            self._controller.resume_publication,
            submission,
        )

    async def _run_dangerous(
        self,
        request: DangerousActionRequest,
        action: str,
        call,
        *args: object,
    ) -> None:
        try:
            await self._supervisor.run_mutation(
                request.run_id, action, call, *args
            )
        except StaleTuiActionError:
            self._show_action_notice(_ACTION_STALE)
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
        await self.refresh_runs()

    @on(ListView.Selected, "#run-list")
    async def select_run(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if (
            index is not None
            and 0 <= index < len(self._runs)
            and event.item.name == self._runs[index].run_id
        ):
            if not self._applying_refresh:
                self._selection_generation += 1
            await self._show_detail(index)

    @on(events.Click, "#run-list ListItem")
    async def click_run(self, event: events.Click) -> None:
        run_id = event.widget.name
        if type(run_id) is not str:
            return
        matches = tuple(
            index
            for index, item in enumerate(self._runs)
            if item.run_id == run_id
        )
        if len(matches) != 1:
            return
        index = matches[0]
        self._selection_generation += 1
        self.query_one("#run-list", ListView).index = index
        await self._show_detail(index)

    @on(Button.Pressed, "#create-workspace")
    def create_workspace(self) -> None:
        self.app.push_screen(
            WorkspaceCreateScreen(self._controller, self._supervisor),
            callback=self._workspace_created,
        )

    @on(Button.Pressed, "#delete-task")
    def delete_task_button(self) -> None:
        self.action_delete_task()

    async def _workspace_created(self, workspace: WorkspaceSummary | None) -> None:
        if workspace is None:
            return
        await self.refresh_workspaces()
        self.app.push_screen(
            WorkspaceDetailScreen(self._controller, self._supervisor, workspace),
            callback=self._workspace_detail_closed,
        )

    async def _workspace_detail_closed(self, deleted: bool | None) -> None:
        if deleted is not None:
            await self.refresh_workspaces()

    @on(ListView.Selected, "#workspace-list")
    def select_workspace(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None or not 0 <= index < len(self._workspaces):
            return
        self.app.push_screen(
            WorkspaceDetailScreen(
                self._controller,
                self._supervisor,
                self._workspaces[index],
            ),
            callback=self._workspace_detail_closed,
        )

    @on(Button.Pressed, "#nav-runs")
    def show_runs(self) -> None:
        self.action_show_runs()

    @on(Button.Pressed, "#nav-workspaces")
    def show_workspaces(self) -> None:
        self.action_show_workspaces()

    @on(Button.Pressed, "#nav-settings")
    def show_settings(self) -> None:
        self.action_show_settings()

    @on(Button.Pressed, "#nav-defects")
    def defects(self) -> None:
        self.action_defects()

    @on(Button.Pressed, "#nav-requirements")
    def requirements(self) -> None:
        self.action_requirements()

    @on(Button.Pressed, "#nav-new-run")
    def new_run(self) -> None:
        self.action_new_run()

    @on(Button.Pressed, "#action-resume")
    async def resume_button(self) -> None:
        await self.action_resume()

    @on(Button.Pressed, "#action-revise")
    async def revise_button(self) -> None:
        await self.action_revise()

    @on(Button.Pressed, "#action-approve")
    async def approve_button(self) -> None:
        await self.action_approve()

    @on(Button.Pressed, "#action-cancel")
    async def cancel_button(self) -> None:
        await self.action_cancel_run()

    @on(Button.Pressed, "#action-accept-analysis")
    async def accept_analysis_button(self) -> None:
        await self._run_analysis_decision(accept=True)

    @on(Button.Pressed, "#action-regenerate-analysis")
    async def regenerate_analysis_button(self) -> None:
        await self._run_analysis_decision(accept=False)

    @on(Button.Pressed, "#action-retry-analysis")
    def retry_analysis_button(self) -> None:
        self._retry_analysis()


__all__ = [
    "ApprovalModal",
    "CancelModal",
    "DashboardScreen",
    "DefectWizardScreen",
    "HelpScreen",
    "NavigationPane",
    "PublicationResumeModal",
    "RepositoryMappingModal",
    "RepositoryMappingSelection",
    "RunDetailPane",
    "RunDetailScreen",
    "RunListPane",
    "RunFilterScreen",
    "RequirementWizardScreen",
    "RevisionModal",
    "SettingsView",
    "TaskDeleteConfirmation",
    "WorkflowTypeScreen",
    "WorkspaceCreateScreen",
    "WorkspaceDetailScreen",
    "WorkspaceListPane",
]
