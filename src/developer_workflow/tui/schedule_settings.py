"""Workspace schedule forms; execution remains in the existing workflow."""
from __future__ import annotations

from datetime import datetime
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, SelectionList, Static
from ..schedules import Schedule
from ..contracts import DefectAction


class ScheduleEditor(ModalScreen):
    DEFAULT_CSS = """
    ScheduleEditor { align: center middle; }
    ScheduleEditor > VerticalScroll { width: 90%; height: 90%; border: round $primary; padding: 1 2; }
    ScheduleEditor Input, ScheduleEditor Select { margin-bottom: 1; }
    ScheduleEditor SelectionList { height: 8; }
    ScheduleEditor Horizontal { height: 3; }
    """
    BINDINGS = [("escape", "cancel", "返回")]

    def __init__(self, controller, supervisor, workspace, plan=None):
        super().__init__()
        self.controller, self.supervisor, self.workspace, self.plan = controller, supervisor, workspace, plan
        self.options = None

    def compose(self) -> ComposeResult:
        p = self.plan
        with VerticalScroll():
            yield Label("编辑定时任务" if p else "添加定时任务")
            yield Static("只在 TUI 运行时调度。启用授权扫描和现有分析/修复流程；提交和 MR 仍需人工审批。")
            yield Label("名称")
            yield Input(p.name if p else "定时缺陷扫描", id="schedule-name")
            yield Label("扫描间隔（分钟，5～10080）")
            yield Input(str(p.interval_minutes if p else 60), id="schedule-interval", type="integer")
            yield Label("每轮最多缺陷数（1～3）")
            yield Input(str(p.max_candidates if p else 3), id="schedule-limit", type="integer")
            yield Label("执行方式")
            yield Select([("仅分析", "analyze"), ("分析并修复", "analyze_and_repair")],
                         value=p.action.value if p else "analyze", allow_blank=False, id="schedule-action")
            yield Label("负责人")
            yield Select([], id="schedule-assignee")
            yield Label("缺陷状态（空格勾选）")
            yield SelectionList(id="schedule-statuses")
            yield Static("正在加载成员和状态…", id="schedule-editor-message", markup=False)
            with Horizontal():
                yield Button("返回", id="schedule-cancel")
                yield Button("保存并暂停", id="schedule-save", disabled=True)
                yield Button("保存并启用", id="schedule-enable", variant="primary", disabled=True)

    async def on_mount(self):
        try:
            self.options = await self.supervisor.run_readonly(
                "schedule-options", self.controller.load_defect_filter_options, self.workspace.project_id)
            p = self.plan
            if not self.options.assignees or not self.options.statuses:
                raise ValueError()
            if p and (p.assignee not in {a.id for a in self.options.assignees}
                      or not set(p.status_ids) <= {s.id for s in self.options.statuses}):
                raise ValueError()
            members = self.query_one("#schedule-assignee", Select)
            members.set_options([(Text(a.name), a.id) for a in self.options.assignees])
            selected = p.assignee if p else next((a.id for a in self.options.assignees if a.selected), Select.BLANK)
            members.value = selected
            self.query_one(SelectionList).add_options([
                (Text(s.name), s.id, s.id in p.status_ids if p else s.selected) for s in self.options.statuses])
            self.query_one("#schedule-editor-message", Static).update("新任务默认暂停；选择保存并启用后开始计时。")
            self.query_one("#schedule-save", Button).disabled = False
            self.query_one("#schedule-enable", Button).disabled = False
        except Exception:
            self.query_one("#schedule-editor-message", Static).update("成员或状态加载失败/原选项已失效，请检查配置后重新打开。")

    @on(Button.Pressed)
    async def pressed(self, event):
        event.stop()
        if event.button.id == "schedule-cancel":
            self.dismiss(None)
            return
        if event.button.id not in {"schedule-save", "schedule-enable"}:
            return
        try:
            assignee = self.query_one("#schedule-assignee", Select).value
            statuses = tuple(self.query_one(SelectionList).selected)
            if self.options is None or assignee not in {a.id for a in self.options.assignees}:
                raise ValueError()
            values = self.plan.model_dump() if self.plan else {}
            values.update(workspace=self.workspace.key, project=self.workspace.project_id,
                          iteration=self.workspace.iteration_id,
                          name=self.query_one("#schedule-name", Input).value,
                          interval_minutes=int(self.query_one("#schedule-interval", Input).value),
                          max_candidates=int(self.query_one("#schedule-limit", Input).value),
                          action=DefectAction(self.query_one("#schedule-action", Select).value),
                          assignee=assignee, status_ids=statuses, enabled=event.button.id == "schedule-enable")
            plan = Schedule.model_validate(values)
            await self.supervisor.run_readonly("save-schedule", lambda: self.controller.schedule_store.save(
                plan, expected_version=self.plan.version if self.plan else None))
            self.dismiss(True)
        except Exception:
            self.query_one("#schedule-editor-message", Static).update("保存失败：检查必填项和数值范围；计划若已被修改，请返回刷新重试。")

    def action_cancel(self):
        self.dismiss(None)


class ScheduleDeleteConfirmation(ModalScreen):
    DEFAULT_CSS = """
    ScheduleDeleteConfirmation { align: center middle; }
    ScheduleDeleteConfirmation > VerticalScroll {
        width: 90%; max-width: 76; height: auto; max-height: 90%;
        border: round $error; padding: 1 2;
    }
    ScheduleDeleteConfirmation Horizontal { height: 3; margin-top: 1; }
    """
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, controller, supervisor, plan: Schedule):
        super().__init__()
        self.controller, self.supervisor, self.plan = controller, supervisor, plan
        self._deleting = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("删除定时任务", markup=False)
            yield Static(self.plan.name, markup=False)
            yield Static("删除后停止后续调度，不能撤销。已启动的任务不会被取消，任务记录和去重记录仍保留。")
            yield Static("", id="schedule-delete-message", markup=False)
            with Horizontal():
                yield Button("取消", id="schedule-delete-cancel")
                yield Button("确认删除", id="schedule-delete-confirm", variant="error")

    @on(Button.Pressed)
    async def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "schedule-delete-cancel":
            self.action_cancel()
        elif event.button.id == "schedule-delete-confirm" and not self._deleting:
            self._deleting = True
            event.button.disabled = True
            try:
                await self.supervisor.run_readonly("delete-schedule", lambda: self.controller.schedule_store.delete(
                    self.plan.id, expected_version=self.plan.version))
                self.dismiss(True)
            except Exception:
                self.query_one("#schedule-delete-message", Static).update(
                    "删除失败：计划可能已变化，或存储不可用。请取消并刷新列表后重试。")
            finally:
                self._deleting = False

    def action_cancel(self) -> None:
        if not self._deleting:
            self.dismiss(False)


class SchedulePane(VerticalScroll):
    DEFAULT_CSS = """
    SchedulePane > Horizontal, #schedule-rows Horizontal { height: 3; }
    #schedule-rows { height: 1fr; }
    #schedule-rows .schedule-card {
        height: auto; border: round $primary; background: $surface;
        padding: 0 1; margin: 0 0 1 0;
    }
    #schedule-rows .schedule-card.paused { border: round $panel; }
    #schedule-rows .schedule-card:focus-within { border: round $accent; }
    #schedule-rows Static { height: auto; }
    #schedule-rows .schedule-card-title { text-style: bold; color: $text; }
    #schedule-rows .schedule-card-state { color: $success; }
    #schedule-rows .paused .schedule-card-state { color: $warning; }
    #schedule-rows .schedule-card-result { color: $text-muted; margin-top: 1; }
    #schedule-rows .schedule-card-actions { margin-top: 1; }
    #schedule-rows .schedule-card-actions Button { min-width: 8; margin-right: 1; }
    """
    def __init__(self, controller, supervisor, workspace):
        super().__init__(classes="workspace-module-body")
        self.controller, self.supervisor, self.workspace = controller, supervisor, workspace
        self.plans = {}

    def compose(self):
        yield Static("定时缺陷扫描", classes="workspace-module-title")
        yield Static("仅 TUI 运行时触发 · 暂停不取消已启动任务 · 提交/MR 仍需审批")
        with Horizontal():
            yield Button("添加定时任务", id="schedule-add", variant="primary")
            yield Button("刷新列表", id="schedule-refresh")
        yield Static("", id="schedule-list-message", markup=False)
        yield VerticalScroll(id="schedule-rows")

    async def on_mount(self):
        await self.refresh_plans()

    async def refresh_plans(self):
        try:
            plans = await self.supervisor.run_readonly("list-schedules", self.controller.schedule_store.list, self.workspace.key)
            self.plans = {p.id: p for p in plans}
            rows = self.query_one("#schedule-rows", VerticalScroll)
            await rows.remove_children()
            for p in plans:
                next_time = datetime.fromtimestamp(p.next_due).strftime("%m-%d %H:%M") if p.enabled else "—"
                await rows.mount(Vertical(
                    Static(p.name, markup=False, classes="schedule-card-title"),
                    Static(f"{'已启用' if p.enabled else '已暂停'}  ·  "
                           f"{'仅分析' if p.action == DefectAction.ANALYZE else '分析并修复'}",
                           markup=False, classes="schedule-card-state"),
                    Static(f"每 {p.interval_minutes} 分钟  ·  每轮最多 {p.max_candidates} 项  ·  下次：{next_time}",
                           markup=False),
                    Static(f"最近结果：{p.last_result}", markup=False, classes="schedule-card-result"),
                    Horizontal(Button("编辑", id=f"edit-{p.id}"),
                               Button("暂停" if p.enabled else "恢复", id=f"toggle-{p.id}"),
                               Button("删除", id=f"delete-{p.id}", variant="error"),
                               classes="schedule-card-actions"),
                    classes="schedule-card" + (" paused" if not p.enabled else "")))
            self.query_one("#schedule-list-message", Static).update(f"共 {len(plans)} 个定时任务" if plans else "暂无定时任务，点击添加。")
        except Exception:
            self.query_one("#schedule-list-message", Static).update("定时任务存储不可用，请检查运行目录权限。")

    @on(Button.Pressed)
    async def pressed(self, event):
        key = event.button.id or ""
        if key not in {"schedule-add", "schedule-refresh"} and not key.startswith(("edit-", "toggle-", "delete-")):
            return
        event.stop()
        if key == "schedule-refresh":
            await self.refresh_plans()
        elif key.startswith("delete-"):
            plan = self.plans.get(key[7:])
            if plan is not None:
                self.app.push_screen(ScheduleDeleteConfirmation(self.controller, self.supervisor, plan),
                                     callback=self._saved)
        elif key == "schedule-add" or key.startswith("edit-"):
            self.app.push_screen(ScheduleEditor(self.controller, self.supervisor, self.workspace,
                                self.plans.get(key[5:])), callback=self._saved)
        else:
            p = self.plans[key[7:]]
            try:
                await self.supervisor.run_readonly("toggle-schedule", lambda: self.controller.schedule_store.save(
                    p.model_copy(update={"enabled": not p.enabled}), expected_version=p.version))
                await self.refresh_plans()
            except Exception:
                self.query_one("#schedule-list-message", Static).update("操作失败，请刷新列表后重试。")

    async def _saved(self, result):
        if result:
            await self.refresh_plans()
