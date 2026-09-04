from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Button, Input, ListView, Static, TabbedContent

from src.developer_workflow.contracts import RepositoryMapping, WorkflowRun
from src.developer_workflow.tui.models import RunFilter, WorkspaceSummary
from src.developer_workflow.tui.run_index import RunIndex
from src.developer_workflow.tui.screens import WorkspaceDetailScreen, RequirementWizardScreen


WORKSPACE = WorkspaceSummary("camera", "project", "iteration", ("camera-sdk", "desktop"))


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


class Controller:
    def __init__(self):
        self.queries = []
        self.fail = False

    def list_workspace_runs(self, workspace):
        self.queries.append(workspace)
        if self.fail:
            raise RuntimeError("do not show backend details")
        return ()


class WorkspaceApp(App):
    CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

    def __init__(self):
        super().__init__()
        self.controller = Controller()

    async def on_mount(self):
        await self.push_screen(WorkspaceDetailScreen(self.controller, Supervisor(), WORKSPACE))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (140, 42)])
async def test_workspace_tabs_footer_and_requirement_scope(size):
    app = WorkspaceApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = app.screen
        tabs = screen.query_one("#workspace-modules", TabbedContent)
        assert len(screen.query("TabPane")) == 4
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
        screen.query_one("#workspace-query-requirements", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, RequirementWizardScreen)
        project = app.screen.query_one("#requirement-project", Input)
        iteration = app.screen.query_one("#requirement-iteration", Input)
        assert (project.value, iteration.value) == ("project", "iteration")
        assert project.disabled and iteration.disabled
        assert not app.screen.query("#start-requirement")
