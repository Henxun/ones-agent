"""Workspace task board for manual planning cards and workflow runs."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Literal

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Select, Static, TextArea

from ..contracts import WorkflowState, WorkflowType
from ..planning_tasks import (
    PlanningTask,
    PlanningTaskPriority,
    PlanningTaskStatus,
    PlanningTaskType,
)
from . import detail_rendering
from .models import DefectChoice, RunSummary, WorkspaceSummary


_STATUS_LABELS = {
    PlanningTaskStatus.TODO: "待处理",
    PlanningTaskStatus.IN_PROGRESS: "进行中",
    PlanningTaskStatus.DONE: "已完成",
}
_PRIORITY_LABELS = {
    PlanningTaskPriority.LOW: "低",
    PlanningTaskPriority.MEDIUM: "中",
    PlanningTaskPriority.HIGH: "高",
}
_TYPE_LABELS = {
    PlanningTaskType.TASK: "普通任务",
    PlanningTaskType.DEFECT: "缺陷",
    PlanningTaskType.REQUIREMENT: "需求",
}


class BoardDefectActionScreen(ModalScreen[Literal["analyze", "repair"] | None]):
    """Require an explicit action before consuming an ONES candidate."""

    DEFAULT_CSS = """
    BoardDefectActionScreen { align: center middle; background: $background 70%; }
    #board-defect-action-dialog {
        width: 72; max-width: 92%; height: auto;
        border: round $warning; background: $surface; padding: 1 2;
    }
    #board-defect-action-dialog Static { height: auto; }
    #board-defect-action-title { text-style: bold; color: $warning; }
    #board-defect-action-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    #board-defect-action-buttons Button { min-width: 14; margin-left: 1; }
    """
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, defect: DefectChoice) -> None:
        super().__init__()
        self.defect = defect

    def compose(self) -> ComposeResult:
        with Vertical(id="board-defect-action-dialog"):
            yield Static("处理 ONES 缺陷", id="board-defect-action-title")
            yield Static(self.defect.title, markup=False)
            yield Static(
                "该卡片来自 ONES 实时查询。请选择沿用现有流程的处理方式。",
                classes="workspace-module-description",
            )
            with Horizontal(id="board-defect-action-buttons"):
                yield Button("取消", id="board-defect-cancel")
                yield Button("仅分析", id="board-defect-analyze")
                yield Button(
                    "分析并修复", id="board-defect-repair", variant="primary"
                )

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "board-defect-analyze":
            self.dismiss("analyze")
        elif event.button.id == "board-defect-repair":
            self.dismiss("repair")
        elif event.button.id == "board-defect-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlanningTaskEditor(ModalScreen[bool | None]):
    """Create or edit one workspace-local task card."""

    DEFAULT_CSS = """
    PlanningTaskEditor { align: center middle; background: $background 70%; }
    #planning-task-dialog {
        width: 76; max-width: 92%; height: auto; max-height: 92%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #planning-task-dialog .planning-editor-title {
        height: auto; text-style: bold; color: $accent; margin-bottom: 1;
    }
    #planning-task-dialog Label { height: auto; color: $text-muted; }
    #planning-task-dialog Input, #planning-task-dialog Select {
        width: 100%; margin-bottom: 1;
    }
    #planning-task-description { height: 8; margin-bottom: 1; }
    #planning-task-message { height: auto; color: $warning; }
    #planning-task-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #planning-task-actions Button { min-width: 12; margin-left: 1; }
    """
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, controller, supervisor, workspace, task=None) -> None:
        super().__init__()
        self.controller = controller
        self.supervisor = supervisor
        self.workspace = workspace
        self.planning_task: PlanningTask | None = task
        self._delete_armed = False
        self._saving = False

    def compose(self) -> ComposeResult:
        task = self.planning_task
        with VerticalScroll(id="planning-task-dialog"):
            yield Static(
                "编辑任务" if task else "添加任务",
                classes="planning-editor-title",
                markup=False,
            )
            yield Static(
                "手动任务只用于本地看板，不会自动启动分析、修复或发布。",
                classes="workspace-module-description",
            )
            yield Label("标题")
            yield Input(
                task.title if task else "",
                placeholder="例如：补充 macOS 实机回归",
                id="planning-task-title",
                max_length=160,
            )
            yield Label("说明")
            yield TextArea(
                task.description if task else "",
                id="planning-task-description",
            )
            yield Label("任务类型")
            yield Select(
                [(label, task_type.value) for task_type, label in _TYPE_LABELS.items()],
                value=(
                    task.task_type.value
                    if task
                    else PlanningTaskType.TASK.value
                ),
                allow_blank=False,
                id="planning-task-type",
            )
            with Horizontal(classes="planning-task-fields"):
                with Vertical():
                    yield Label("状态")
                    yield Select(
                        [(label, status.value) for status, label in _STATUS_LABELS.items()],
                        value=(task.status.value if task else PlanningTaskStatus.TODO.value),
                        allow_blank=False,
                        id="planning-task-status",
                    )
                with Vertical():
                    yield Label("优先级")
                    yield Select(
                        [(label, priority.value) for priority, label in _PRIORITY_LABELS.items()],
                        value=(task.priority.value if task else PlanningTaskPriority.MEDIUM.value),
                        allow_blank=False,
                        id="planning-task-priority",
                    )
            yield Static("", id="planning-task-message", markup=False)
            with Horizontal(id="planning-task-actions"):
                if task is not None:
                    yield Button("删除任务", id="planning-task-delete", variant="error")
                yield Button("取消", id="planning-task-cancel")
                yield Button("保存", id="planning-task-save", variant="primary")

    @on(Button.Pressed)
    async def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = event.button.id
        if action == "planning-task-cancel":
            self.action_cancel()
            return
        if self._saving:
            return
        if action == "planning-task-delete":
            if not self._delete_armed:
                self._delete_armed = True
                event.button.label = "再次确认删除"
                self.query_one("#planning-task-message", Static).update(
                    "删除后无法恢复；再次点击确认删除。"
                )
                return
            await self._delete()
            return
        if action != "planning-task-save":
            return
        await self._save()

    async def _save(self) -> None:
        self._saving = True
        self._set_actions_disabled(True)
        try:
            current = self.planning_task
            values = current.model_dump(mode="python") if current else {}
            values.update(
                workspace=self.workspace.key,
                title=self.query_one("#planning-task-title", Input).value,
                description=self.query_one(
                    "#planning-task-description", TextArea
                ).text,
                task_type=PlanningTaskType(
                    self.query_one("#planning-task-type", Select).value
                ),
                status=PlanningTaskStatus(
                    self.query_one("#planning-task-status", Select).value
                ),
                priority=PlanningTaskPriority(
                    self.query_one("#planning-task-priority", Select).value
                ),
            )
            task = PlanningTask.model_validate(values)
            await self.supervisor.run_readonly(
                "save-planning-task",
                lambda: self.controller.save_planning_task(
                    self.workspace,
                    task,
                    expected_version=current.version if current else None,
                ),
            )
            self.dismiss(True)
        except Exception:
            self.query_one("#planning-task-message", Static).update(
                "保存失败：请检查标题；若任务已变化，请返回刷新后重试。"
            )
            self._set_actions_disabled(False)
            self._saving = False

    async def _delete(self) -> None:
        task = self.planning_task
        if task is None:
            return
        self._saving = True
        self._set_actions_disabled(True)
        try:
            await self.supervisor.run_readonly(
                "delete-planning-task",
                self.controller.delete_planning_task,
                self.workspace,
                task,
            )
            self.dismiss(True)
        except Exception:
            self.query_one("#planning-task-message", Static).update(
                "删除失败：任务可能已变化，请返回刷新后重试。"
            )
            self._delete_armed = False
            self._set_actions_disabled(False)
            self.query_one("#planning-task-delete", Button).label = "删除任务"
            self._saving = False

    def _set_actions_disabled(self, disabled: bool) -> None:
        for button in self.query("#planning-task-actions Button"):
            button.disabled = disabled

    def action_cancel(self) -> None:
        if not self._saving:
            self.dismiss(None)


class TaskBoardPane(Vertical):
    """A compact three-column board with manual and workflow-backed cards."""

    def __init__(
        self,
        controller,
        supervisor,
        workspace: WorkspaceSummary,
        open_workflow: Callable[[object], None],
        start_defect: Callable[
            [str, tuple[DefectChoice, ...], int, bool], None
        ],
        **widget_options,
    ) -> None:
        classes = widget_options.pop("classes", "")
        super().__init__(
            classes=f"workspace-module-body {classes}".strip(),
            **widget_options,
        )
        self.controller = controller
        self.supervisor = supervisor
        self.workspace = workspace
        self.open_workflow = open_workflow
        self.start_defect = start_defect
        self.planning_tasks: dict[str, PlanningTask] = {}
        self._defect_session_id: str | None = None
        self._defect_candidates: tuple[DefectChoice, ...] = ()
        self._pending_defect_index: int | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="task-board-header"):
            with Vertical(id="task-board-heading"):
                yield Static("任务看板", classes="workspace-module-title")
                yield Static(
                    "手动任务、ONES 待处理缺陷与自动化流程统一浏览；流程门禁保持不变。",
                    classes="workspace-module-description",
                )
            with Horizontal(id="task-board-toolbar"):
                yield Button("刷新", id="workspace-refresh-tasks")
                yield Button("添加任务", id="planning-task-add", variant="primary")
        yield Static("尚未加载", id="workspace-task-status", markup=False)
        with Horizontal(id="task-board-columns"):
            for status in PlanningTaskStatus:
                with Vertical(classes="task-board-column", id=f"task-column-{status.value}"):
                    yield Static(
                        _STATUS_LABELS[status], classes="task-board-column-title"
                    )
                    yield Static(
                        "0 项",
                        id=f"task-count-{status.value}",
                        classes="task-board-column-count",
                    )
                    yield ListView(
                        id=f"task-list-{status.value}",
                        classes="task-board-list",
                    )

    async def refresh_board(self) -> None:
        status_widget = self.query_one("#workspace-task-status", Static)
        refresh = self.query_one("#workspace-refresh-tasks", Button)
        refresh.disabled = True
        status_widget.update("正在加载任务…")
        try:
            runs = await self.supervisor.run_readonly(
                "workspace-tasks", self.controller.list_workspace_runs, self.workspace
            )
            planning = await self.supervisor.run_readonly(
                "workspace-planning-tasks",
                self.controller.list_planning_tasks,
                self.workspace,
            )
            self.planning_tasks = {task.id: task for task in planning}
            external_error = False
            await self._discard_defect_session()
            source = getattr(self.controller, "query_workspace_board_defects", None)
            if callable(source):
                try:
                    external = await self.supervisor.run_readonly(
                        "workspace-board-defects", source, self.workspace
                    )
                    self._defect_session_id = external.session_id
                    self._defect_candidates = tuple(
                        item
                        for item in external.items
                        if item.candidate_id not in {run.work_item_id for run in runs}
                    )
                except Exception:
                    external_error = True
                    self._defect_candidates = ()
            columns: dict[PlanningTaskStatus, list[ListItem]] = {
                item: [] for item in PlanningTaskStatus
            }
            for task in planning:
                columns[task.status].append(self._planning_card(task))
            for run in runs:
                columns[self._workflow_column(run)].append(self._workflow_card(run))
            columns[PlanningTaskStatus.TODO].extend(
                self._ones_defect_card(item, index)
                for index, item in enumerate(self._defect_candidates)
            )
            for column_status, cards in columns.items():
                listing = self.query_one(
                    f"#task-list-{column_status.value}", ListView
                )
                await listing.clear()
                await listing.extend(cards)
                self.query_one(
                    f"#task-count-{column_status.value}", Static
                ).update(f"{len(cards)} 项")
            summary = (
                f"{len(planning)} 项手动任务 · {len(runs)} 项自动化流程 · "
                f"{len(self._defect_candidates)} 项 ONES 待处理缺陷"
            )
            status_widget.update(
                f"{summary} · ONES 待处理项加载失败" if external_error else summary
            )
        except Exception:
            await self._clear_board()
            status_widget.update("任务加载失败，请检查运行目录后重试")
        finally:
            refresh.disabled = False

    async def _clear_board(self) -> None:
        self.planning_tasks = {}
        await self._discard_defect_session()
        for column_status in PlanningTaskStatus:
            await self.query_one(
                f"#task-list-{column_status.value}", ListView
            ).clear()
            self.query_one(f"#task-count-{column_status.value}", Static).update(
                "0 项"
            )

    @staticmethod
    def _planning_card(task: PlanningTask) -> ListItem:
        priority = _PRIORITY_LABELS[task.priority]
        task_type = _TYPE_LABELS[task.task_type]
        updated = datetime.fromtimestamp(task.updated_at).strftime("%m-%d %H:%M")
        description = task.description.splitlines()[0] if task.description else "暂无说明"
        return ListItem(
            Vertical(
                Static(task.title, markup=False, classes="task-card-title"),
                Static(
                    f"{task_type} · 手动创建 · {priority}优先级",
                    markup=False,
                    classes=f"task-card-badge priority-{task.priority.value}",
                ),
                Static(description, markup=False, classes="task-card-description"),
                Static(f"更新 {updated}", markup=False, classes="task-card-meta"),
                Static("打开编辑 →", classes="task-card-open"),
                classes="task-card-content",
            ),
            name=f"manual:{task.id}",
            classes=f"task-board-card manual priority-{task.priority.value}",
        )

    @staticmethod
    def _workflow_card(run: RunSummary) -> ListItem:
        state = run.state.value
        kind = "缺陷" if run.workflow_type is WorkflowType.DEFECT else "需求"
        tone = (
            "failed"
            if state == "FAILED"
            else "attention"
            if state
            in {
                "BLOCKED",
                "WAITING_APPROVAL",
                "WAITING_PR_VERIFICATION",
                "PARTIAL_SUCCESS",
            }
            else "complete"
            if state == "COMPLETED"
            else "normal"
        )
        return ListItem(
            Vertical(
                Static(
                    f"{kind} · {run.work_item_id}",
                    markup=False,
                    classes="task-card-title",
                ),
                Static(
                    "自动化流程",
                    markup=False,
                    classes="task-card-badge workflow",
                ),
                Static(
                    f"{detail_rendering.state_name(state)} · {state}",
                    markup=False,
                    classes="task-card-description",
                ),
                Static(
                    f"更新 {run.updated_at.astimezone().strftime('%m-%d %H:%M')}",
                    markup=False,
                    classes="task-card-meta",
                ),
                Static("查看流程详情 →", classes="task-card-open"),
                classes="task-card-content",
            ),
            name=f"run:{run.run_id}",
            classes=f"task-board-card workflow {tone}",
        )

    @staticmethod
    def _ones_defect_card(defect: DefectChoice, index: int) -> ListItem:
        return ListItem(
            Vertical(
                Static(defect.title, markup=False, classes="task-card-title"),
                Static(
                    "缺陷 · ONES 实时待处理",
                    markup=False,
                    classes="task-card-badge ones",
                ),
                Static(
                    f"优先级 {defect.priority} · 状态 "
                    f"{defect.status_name or defect.status_id}",
                    markup=False,
                    classes="task-card-description",
                ),
                Static(
                    f"ONES ID：{defect.candidate_id}",
                    markup=False,
                    classes="task-card-meta",
                ),
                Static("选择处理方式 →", classes="task-card-open"),
                classes="task-card-content",
            ),
            name=f"ones:{index}",
            classes="task-board-card ones attention",
        )

    @staticmethod
    def _workflow_column(run: RunSummary) -> PlanningTaskStatus:
        if run.state is WorkflowState.COMPLETED:
            return PlanningTaskStatus.DONE
        if run.state is WorkflowState.CREATED:
            return PlanningTaskStatus.TODO
        return PlanningTaskStatus.IN_PROGRESS

    @on(Button.Pressed, "#workspace-refresh-tasks")
    async def _refresh_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.refresh_board()

    @on(Button.Pressed, "#planning-task-add")
    def _add_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.push_screen(
            PlanningTaskEditor(
                self.controller, self.supervisor, self.workspace
            ),
            callback=self._task_saved,
        )

    @on(ListView.Selected, ".task-board-list")
    async def _card_selected(self, event: ListView.Selected) -> None:
        name = event.item.name or ""
        if name.startswith("manual:"):
            task = self.planning_tasks.get(name.removeprefix("manual:"))
            if task is not None:
                self.app.push_screen(
                    PlanningTaskEditor(
                        self.controller,
                        self.supervisor,
                        self.workspace,
                        task,
                    ),
                    callback=self._task_saved,
                )
            return
        if name.startswith("ones:"):
            try:
                index = int(name.removeprefix("ones:"))
            except ValueError:
                return
            if (
                self._defect_session_id is None
                or not 0 <= index < len(self._defect_candidates)
            ):
                self.query_one("#workspace-task-status", Static).update(
                    "ONES 查询结果已失效，请刷新看板"
                )
                return
            self._pending_defect_index = index
            self.app.push_screen(
                BoardDefectActionScreen(self._defect_candidates[index]),
                callback=self._defect_action_selected,
            )
            return
        if not name.startswith("run:"):
            return
        try:
            detail = await self.supervisor.run_readonly(
                "workspace-task-detail",
                self.controller.show,
                name.removeprefix("run:"),
            )
            self.open_workflow(detail)
        except Exception:
            self.query_one("#workspace-task-status", Static).update(
                "流程详情暂不可用，请刷新后重试"
            )

    async def _task_saved(self, result: bool | None) -> None:
        if result:
            await self.refresh_board()

    def _defect_action_selected(
        self, action: Literal["analyze", "repair"] | None
    ) -> None:
        index = self._pending_defect_index
        self._pending_defect_index = None
        session_id = self._defect_session_id
        if action is None or index is None or session_id is None:
            return
        self._defect_session_id = None
        self.start_defect(
            session_id,
            self._defect_candidates,
            index,
            action == "analyze",
        )

    async def _discard_defect_session(self) -> None:
        session_id = self._defect_session_id
        self._defect_session_id = None
        self._defect_candidates = ()
        discard = getattr(self.controller, "discard_candidate_session", None)
        if session_id is not None and callable(discard):
            try:
                await self.supervisor.run_readonly(
                    "discard-workspace-board-defects", discard, session_id
                )
            except Exception:
                pass

    def on_unmount(self) -> None:
        session_id = self._defect_session_id
        self._defect_session_id = None
        discard = getattr(self.controller, "discard_candidate_session", None)
        if session_id is not None and callable(discard):
            discard(session_id)


__all__ = ["BoardDefectActionScreen", "PlanningTaskEditor", "TaskBoardPane"]
