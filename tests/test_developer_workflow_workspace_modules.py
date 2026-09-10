from __future__ import annotations

from pathlib import Path
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest
from textual.app import App
from textual import on
from textual.widgets import Button, Input, ListView, Select, Static, TabbedContent

from src.developer_workflow.contracts import RepositoryMapping, WorkflowRun, WorkflowState, WorkflowType
from src.developer_workflow.tui.models import (
    FilterChoice,
    RequirementChoice,
    RequirementFilterOptions,
    RunActivity,
    RunFilter,
    RunSummary,
    WorkspaceSummary,
)
from src.developer_workflow.tui.run_index import RunIndex
from src.developer_workflow.tui.screens import WorkspaceDetailScreen, RequirementWizardScreen, WorkspaceListPane, WorkspaceRenameScreen
from dataclasses import replace


WORKSPACE = WorkspaceSummary("camera", "project", "iteration", ("camera-sdk", "desktop"))


class HomeCardsApp(App):
    CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

    def compose(self):
        yield WorkspaceListPane(id="workspace-list-pane")

    @on(ListView.Selected, "#workspace-list")
    def selected(self, event):
        self.selected_id = event.item.id


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (140, 42)])
async def test_home_workspace_cards_layout_and_selection(size):
    app = HomeCardsApp()
    workspaces = (WORKSPACE, WorkspaceSummary("[literal] 工作区", "project-two", "iteration-two",
                                              ("仓库一", "仓库二", "仓库三", "仓库四")))
    async with app.run_test(size=size) as pilot:
        pane = app.query_one(WorkspaceListPane)
        await pane.replace_workspaces(workspaces)
        await pilot.pause()
        cards = list(app.query(".workspace-home-card"))
        assert len(cards) == 2
        assert cards[1].region.y > cards[0].region.bottom
        assert not app.query_one("#workspace-empty").display
        for card in cards:
            assert card.query_one(".workspace-home-title", Static).markup is False
            assert card.query_one(".workspace-home-open").region.bottom < card.region.bottom
            assert card.region.right <= size[0]
        assert "进入查看全部" in str(cards[1].query_one(".workspace-home-repos", Static).render())
        await pilot.click(cards[0].query_one(".workspace-home-title"))
        await pilot.pause()
        assert app.selected_id == "workspace-item-0"
        listing = app.query_one("#workspace-list", ListView)
        listing.focus()
        listing.index = 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected_id == "workspace-item-1"
        await pane.replace_workspaces(())
        assert app.query_one("#workspace-empty").display
        assert len(listing.children) == 0


def test_workspace_tasks_use_mapping_not_only_project_and_iteration():
    def mapping(key, iteration="iteration"):
        return RepositoryMapping(key=key, project_id="project", iteration_id=iteration,
                                 repo_url="https://example.test/repo.git", repo_name="repo")

    runs = {
        "a" * 32: WorkflowRun.new("requirement", "REQ-1").validated_update(
            run_id="a" * 32, repository=mapping("camera")),
        "b" * 32: WorkflowRun.new("requirement", "REQ-2").validated_update(
            run_id="b" * 32, repository=mapping("other")),
        "c" * 32: WorkflowRun.new("requirement", "REQ-3").validated_update(
            run_id="c" * 32, repository=mapping("camera", "older")),
        "d" * 32: WorkflowRun.new("requirement", "REQ-4").validated_update(run_id="d" * 32),
    }
    store = SimpleNamespace(list_run_ids=lambda: tuple(runs), load=lambda key, **_: runs[key])
    index = RunIndex(store)
    assert [r.work_item_id for r in index.list(RunFilter(), workspace=WORKSPACE)] == ["REQ-1"]
    assert len(index.list(RunFilter())) == 4


class Supervisor:
    async def run_readonly(self, _action, call, *args):
        return call(*args)

    async def run_mutation(self, _run_id, _action, call, *args):
        return call(*args)


class Controller:
    def __init__(self):
        self.queries = []
        self.fail = False
        self.requirement_queries = []
        self.requirement_starts = []
        self.requirement_fail = False
        self.discarded_requirement_sessions = []

    def list_workspace_runs(self, workspace):
        self.queries.append(workspace)
        if self.fail:
            raise RuntimeError("do not show backend details")
        return ()

    def query_requirements(self, project, iteration, assignee, status_ids, issue_type_id):
        self.requirement_queries.append(
            (project, iteration, assignee, status_ids, issue_type_id)
        )
        if self.requirement_fail:
            raise RuntimeError("do not show backend details")
        return (
            "requirement-session",
            (
                RequirementChoice(
                    requirement_id="requirement-1",
                    number="REQ-1",
                    title="支持工作区需求收件箱",
                    project_id=project,
                    iteration_id=iteration,
                    status_id="open",
                ),
            ),
        )

    def load_requirement_filter_options(self, project):
        return RequirementFilterOptions(
            issue_types=(FilterChoice(id="story", name="Requirement", selected=True),)
        )

    def start_requirement(self, requirement_id, session_id=None):
        self.requirement_starts.append((requirement_id, session_id))
        raise RuntimeError("stop after proving the candidate capability was forwarded")

    def discard_requirement_session(self, session_id):
        self.discarded_requirement_sessions.append(session_id)


class WorkspaceApp(App):
    CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

    def __init__(self):
        super().__init__()
        self.controller = Controller()

    async def on_mount(self):
        await self.push_screen(WorkspaceDetailScreen(self.controller, Supervisor(), WORKSPACE))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (140, 42)])
async def test_workspace_rename_changes_only_label(size):
    app = WorkspaceApp()
    calls = []
    def rename(key, name):
        calls.append((key, name))
        return replace(WORKSPACE, display_name=name)
    app.controller.rename_workspace = rename
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        detail = app.screen
        detail.query_one("#workspace-rename", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.query("#workspace-rename-name"):
                break
        assert isinstance(app.screen, WorkspaceRenameScreen)
        app.screen.query_one("#workspace-rename-name", Input).value = "桌面端 [回归测试]"
        app.screen.query_one("#workspace-rename-save", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen is detail:
                break
        assert detail.workspace.key == WORKSPACE.key
        assert detail.workspace.label == "桌面端 [回归测试]"
        assert calls == [(WORKSPACE.key, "桌面端 [回归测试]")]
        assert "桌面端 [回归测试]" in str(detail.query_one("#workspace-heading", Static).render())
        assert detail.query_one("#workspace-delete").region.right <= size[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (140, 42)])
async def test_workspace_task_cards_and_navigation(size):
    app = WorkspaceApp()
    runs = tuple(RunSummary(
        run_id=str(i) * 32, workflow_type=WorkflowType.DEFECT if i == 1 else WorkflowType.REQUIREMENT,
        work_item_id=f"ITEM-{i}", state=state, version=i, updated_at=datetime.now(UTC), activity=RunActivity.IDLE)
        for i, state in enumerate((WorkflowState.BLOCKED, WorkflowState.COMPLETED), 1))
    app.controller.list_workspace_runs = lambda workspace: runs
    app.controller.show = lambda run_id: run_id
    opened = []
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = app.screen
        screen._workflow_started = opened.append
        screen.query_one("#workspace-modules", TabbedContent).active = "workspace-tasks-tab"
        for _ in range(30):
            await pilot.pause(0.05)
            if len(screen.query(".workspace-task-card")) == 2:
                break
        cards = list(screen.query(".workspace-task-card"))
        assert len(cards) == 2
        assert cards[1].region.y > cards[0].region.bottom
        assert cards[0].has_class("attention")
        assert cards[1].has_class("complete")
        for card in cards:
            assert len(card.query(".workspace-task-meta")) == 2
            assert card.query_one(".workspace-task-open").region.bottom < card.region.bottom
        assert screen.query_one("#workspace-detail-back").region.bottom <= size[1]
        await pilot.click(cards[0].query_one(".workspace-task-title"))
        await pilot.pause()
        assert opened == [runs[0].run_id]
        listing = screen.query_one("#workspace-task-list", ListView)
        listing.focus()
        listing.index = 1
        await pilot.press("enter")
        await pilot.pause()
        assert opened[-1] == runs[1].run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(60, 24), (80, 24), (140, 42), (190, 42)])
async def test_workspace_tabs_footer_and_requirement_scope(size):
    app = WorkspaceApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = app.screen
        tabs = screen.query_one("#workspace-modules", TabbedContent)
        module_body = screen.query_one("#workspace-defects-tab .workspace-module-body")
        assert module_body.region.width >= screen.region.width - 6
        assert screen.query_one("#workspace-detail-footer").region.width == screen.region.width
        assert len(screen.query("TabPane")) == 5
        assert screen.query_one("#workspace-repositories-tab").query_one("#workspace-repositories")
        assert not screen.query_one("#workspace-defects-tab").query("#workspace-repositories")
        assert screen.query_one("#workspace-detail-back", Button).region.bottom <= size[1]
        tabs.active = "workspace-schedules-tab"
        await pilot.pause()
        assert not screen.query("#schedule-start")
        tabs.active = "workspace-tasks-tab"
        await pilot.pause()
        assert app.controller.queries == [WORKSPACE]
        assert len(screen.query_one("#workspace-task-list", ListView).children) == 0
        app.controller.fail = True
        screen.query_one("#workspace-refresh-tasks", Button).press()
        await pilot.pause()
        assert "任务加载失败" in str(screen.query_one("#workspace-task-status", Static).render())
        assert not screen.query_one("#workspace-refresh-tasks", Button).disabled
        tabs.active = "workspace-requirements-tab"
        await pilot.pause()
        screen.query_one("#workspace-requirement-assignee", Input).value = "user-1"
        screen.query_one("#workspace-requirement-status-ids", Input).value = "open,ready"
        screen.query_one("#workspace-requirement-type-id", Select).value = "story"
        screen.query_one("#workspace-query-requirements", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            mounted_cards = list(screen.query(".workspace-requirement-card"))
            if mounted_cards and mounted_cards[0].region.width:
                break
        assert app.controller.requirement_queries == [
            ("project", "iteration", "user-1", ("open", "ready"), "story")
        ]
        cards = list(screen.query(".workspace-requirement-card"))
        assert len(cards) == 1
        card = cards[0]
        info = card.query_one(".workspace-requirement-info")
        actions = card.query_one(".workspace-requirement-actions")
        assert actions.region.x > info.region.x
        assert actions.region.right < card.region.right
        assert card.region.right <= screen.region.right
        assert len(actions.query("Button")) == 1
        assert "支持工作区需求收件箱" in str(
            card.query_one(".workspace-requirement-title", Static).render()
        )
        card.query_one(".workspace-requirement-start", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if app.controller.requirement_starts:
                break
        assert isinstance(app.screen, RequirementWizardScreen)
        assert app.controller.requirement_starts == [
            ("requirement-1", "requirement-session")
        ]
        assert not app.screen.query("#start-requirement")
        assert not app.screen.query("#requirement-project")


@pytest.mark.asyncio
async def test_failed_requirement_refresh_revokes_visible_candidate_session():
    app = WorkspaceApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#workspace-modules", TabbedContent).active = (
            "workspace-requirements-tab"
        )
        await pilot.pause()
        screen.query_one("#workspace-requirement-type-id", Select).value = "story"
        screen.query_one("#workspace-query-requirements", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if screen.query(".workspace-requirement-card"):
                break
        assert screen._requirement_session_id == "requirement-session"
        assert len(screen.query(".workspace-requirement-card")) == 1

        app.controller.requirement_fail = True
        screen.query_one("#workspace-query-requirements", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if "查询失败" in str(
                screen.query_one("#workspace-requirement-status", Static).render()
            ):
                break

        assert screen._requirement_session_id is None
        assert not screen.query(".workspace-requirement-card")
        assert app.controller.discarded_requirement_sessions == [
            "requirement-session"
        ]
        assert "查询失败" in str(
            screen.query_one("#workspace-requirement-status", Static).render()
        )


@pytest.mark.asyncio
async def test_requirement_render_failure_revokes_new_candidate_session():
    app = WorkspaceApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#workspace-modules", TabbedContent).active = (
            "workspace-requirements-tab"
        )
        await pilot.pause()
        screen.query_one("#workspace-requirement-type-id", Select).value = "story"
        original_render = screen._render_requirement_candidates
        render_calls = 0

        async def fail_after_query():
            nonlocal render_calls
            render_calls += 1
            if render_calls == 2:
                raise RuntimeError("render failed")
            await original_render()

        screen._render_requirement_candidates = fail_after_query
        screen.query_one("#workspace-query-requirements", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if app.controller.discarded_requirement_sessions:
                break

        assert app.controller.discarded_requirement_sessions == [
            "requirement-session"
        ]
        assert screen._requirement_session_id is None
        assert not screen.query(".workspace-requirement-card")


@pytest.mark.asyncio
async def test_requirement_push_failure_revokes_session_and_restores_action():
    app = WorkspaceApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#workspace-modules", TabbedContent).active = (
            "workspace-requirements-tab"
        )
        for _ in range(30):
            await pilot.pause(0.05)
            if not screen.query_one("#workspace-query-requirements", Button).disabled:
                break
        screen.query_one("#workspace-requirement-type-id", Select).value = "story"
        screen.query_one("#workspace-query-requirements", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if screen.query(".workspace-requirement-card"):
                break
        original_push_screen = app.push_screen

        def fail_push(*_args, **_kwargs):
            raise RuntimeError("push failed")

        app.push_screen = fail_push
        action = screen.query_one(".workspace-requirement-start", Button)
        action.press()
        await pilot.pause()
        app.push_screen = original_push_screen

        assert app.controller.discarded_requirement_sessions == [
            "requirement-session"
        ]
        assert screen._requirement_session_id is None
        assert not action.disabled
        assert "重新查询" in str(
            screen.query_one("#workspace-requirement-status", Static).render()
        )


@pytest.mark.asyncio
async def test_leaving_workspace_revokes_visible_requirement_session():
    app = WorkspaceApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#workspace-modules", TabbedContent).active = (
            "workspace-requirements-tab"
        )
        for _ in range(30):
            await pilot.pause(0.05)
            if not screen.query_one("#workspace-query-requirements", Button).disabled:
                break
        screen.query_one("#workspace-requirement-type-id", Select).value = "story"
        screen.query_one("#workspace-query-requirements", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if screen.query(".workspace-requirement-card"):
                break

        screen.query_one("#workspace-detail-back", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if app.controller.discarded_requirement_sessions:
                break

        assert app.controller.discarded_requirement_sessions == [
            "requirement-session"
        ]
