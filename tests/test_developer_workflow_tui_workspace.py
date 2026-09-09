from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from src.developer_workflow.contracts import (
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
)
from src.developer_workflow.setup_models import WorkflowDraft
from src.developer_workflow.setup_validation import SetupStep
from src.developer_workflow.tui.setup_screens import SetupWizardScreen


def _repository(
    key: str,
    *,
    role: RepositoryRole = RepositoryRole.PRIMARY,
    depends_on: tuple[str, ...] = (),
) -> RepositoryMapping:
    return RepositoryMapping(
        key=key,
        project_id="project",
        iteration_id="iteration",
        repo_url=f"https://git.example.test/{key}.git",
        repo_name=key,
        source_path=Path("/workspace") / key,
        role=role,
        depends_on=depends_on,
    )


def test_repository_name_is_derived_and_deduplicated_from_source() -> None:
    from src.developer_workflow.tui.models import WorkspaceRepositoryInput
    from src.developer_workflow.tui.screens import _repository_name_from_source

    existing = (
        WorkspaceRepositoryInput(
            key="ones-agent",
            name="ones-agent",
            source="C:/workspace/ones-agent",
            local=True,
        ),
    )

    assert _repository_name_from_source("C:/workspace/local.git", ()) == "local"
    assert (
        _repository_name_from_source(
            "https://git.example.test/team/ones-agent.git", existing
        )
        == "ones-agent-2"
    )


def test_friendly_workspace_name_is_normalized_without_blocking_creation() -> None:
    from src.developer_workflow.tui.screens import _workspace_key_from_scope

    assert (
        _workspace_key_from_scope("project-1", "iteration-1", "Camera Workspace")
        == "Camera-Workspace"
    )
    assert (
        _workspace_key_from_scope("project-1", "iteration-1", "相机工作区")
        == "project-1-iteration-1"
    )


def test_workspace_folder_entries_include_all_standalone_and_group_members() -> None:
    standalone = _repository("standalone")
    dependency = _repository(
        "dependency", role=RepositoryRole.DEPENDENCY
    )
    primary = _repository("primary")
    group = RepositoryGroupMapping(
        key="workspace",
        project_id="project",
        iteration_id="iteration",
        primary_repository="primary",
        repositories=(dependency, primary),
    )
    workflow = WorkflowDraft(
        repositories=(standalone,),
        repository_groups=(group,),
    )
    controller = SimpleNamespace(
        current_step=SetupStep.REPOSITORIES,
        draft=SimpleNamespace(workflow=workflow),
    )

    screen = SetupWizardScreen(controller)

    entries = screen._repository_entries()

    assert [entry[0] for entry in entries] == [
        "repo:standalone",
        "group:workspace/dependency",
        "group:workspace/primary",
    ]
    labels = [entry[1] for entry in entries]
    assert labels == [
        "standalone · standalone · primary",
        "workspace/dependency · dependency · dependency",
        "workspace/primary · primary · primary",
    ]
    assert all("git.example.test" not in label for label in labels)
    assert all("/workspace" not in label for label in labels)


def _workflow_config(tmp_path: Path):
    from src.developer_workflow.config import (
        BUILTIN_WORKSPACE_PROFILE,
        DeveloperWorkflowConfig,
        PublishingConfig,
        PublishingProvider,
        SandboxPermissionProfileSource,
    )

    return DeveloperWorkflowConfig(
        run_root=tmp_path / "runs",
        mirror_root=tmp_path / "mirrors",
        worktree_root=tmp_path / "worktrees",
        sandbox_permission_profile=BUILTIN_WORKSPACE_PROFILE,
        sandbox_permission_profile_source=(
            SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        ),
        max_codex_attempts=1,
        repositories=(
            RepositoryMapping(
                key="workspace",
                project_id="pending-project",
                iteration_id="*",
                repo_url=str(tmp_path),
                repo_name="workspace",
            ),
        ),
        publishing=PublishingConfig(provider=PublishingProvider.GITHUB),
    )


def test_controller_creates_and_persists_multi_repository_workspace(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.tui.controller import TuiController
    from src.developer_workflow.tui.models import WorkspaceRepositoryInput

    local = tmp_path / "local-repository"
    (local / ".git").mkdir(parents=True)
    config = _workflow_config(tmp_path)
    saved: list[object] = []
    orchestrator = SimpleNamespace(
        config=config,
        defect_candidates=SimpleNamespace(gateway=object()),
    )
    controller = TuiController(
        orchestrator,
        object(),
        workflow_saver=saved.append,
    )

    assert controller.list_workspaces() == ()
    created = controller.create_workspace(
        "desktop",
        "project-1",
        "iteration-1",
        (
            WorkspaceRepositoryInput(
                key="primary",
                name="primary",
                source=str(local),
                local=True,
            ),
            WorkspaceRepositoryInput(
                key="dependency",
                name="dependency",
                source="https://git.example.test/team/dependency.git",
                local=False,
                branch="develop",
            ),
        ),
    )

    assert created.key == "desktop"
    assert created.project_id == "project-1"
    assert created.iteration_id == "iteration-1"
    assert created.repositories == ("primary", "dependency")
    assert len(saved) == 1
    assert config.repository_groups[0].primary_repository == "primary"
    assert config.repository_groups[0].repositories[0].source_path == local.resolve()
    assert config.repository_groups[0].repositories[1].source_path is None
    assert controller.list_workspaces() == (created,)
    controller.delete_workspace("desktop")
    assert controller.list_workspaces() == ()
    assert config.repository_groups == ()
    assert len(saved) == 2
    with pytest.raises(Exception, match="workspace configuration could not be saved"):
        controller.delete_workspace("desktop")
    controller.close()


def test_workspace_display_names_preserve_mapping_and_schedule_identity(tmp_path):
    from src.developer_workflow.tui.controller import TuiController
    from src.developer_workflow.tui.models import WorkspaceRepositoryInput
    from src.developer_workflow.schedules import Schedule, ScheduleStore

    config = _workflow_config(tmp_path)
    saved = []
    controller = TuiController(SimpleNamespace(config=config), object(), workflow_saver=saved.append)
    workspace = controller.create_workspace("stable-id", "project-1", "iteration-1", (
        WorkspaceRepositoryInput("repo", "repo", "https://example.test/repo.git", False),),
        display_name="桌面端 回归 [测试]")
    original_mapping = config.repository_groups[0].model_dump_json()
    assert workspace.label == "桌面端 回归 [测试]"
    assert workspace.key == "stable-id"
    store = ScheduleStore(tmp_path / "plans")
    controller.schedule_store = store
    plan = store.save(Schedule(workspace=workspace.key, project="project-1", iteration="iteration-1",
                               name="扫描", assignee="user", status_ids=("open",)), expected_version=None)
    renamed = controller.rename_workspace(workspace.key, "新名称 生产环境")
    assert renamed.key == workspace.key
    assert renamed.label == "新名称 生产环境"
    assert config.repository_groups[0].model_dump_json() == original_mapping
    assert store.list() == (plan,)
    reloaded = type(config).model_validate_json(saved[-1].model_dump_json())
    assert reloaded.workspace_names == {"stable-id": "新名称 生产环境"}
    from src.developer_workflow.setup_models import WorkflowDraft
    draft = WorkflowDraft.model_validate(reloaded.model_dump(mode="python"))
    assert draft.workspace_names == reloaded.workspace_names
    for invalid in ("", "  ", "x" * 129, "name\nnewline"):
        with pytest.raises(Exception):
            controller.rename_workspace(workspace.key, invalid)
    assert config.workspace_names == reloaded.workspace_names
    controller._workflow_saver = lambda value: (_ for _ in ()).throw(OSError())
    with pytest.raises(Exception):
        controller.rename_workspace(workspace.key, "保存失败的名称")
    assert config.workspace_names == reloaded.workspace_names
    controller._workflow_saver = saved.append
    controller.delete_workspace(workspace.key)
    assert config.workspace_names == {}
    controller.close()


def test_local_workspace_repository_keeps_real_origin_as_repo_url(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.tui.controller import TuiController
    from src.developer_workflow.tui.models import WorkspaceRepositoryInput

    local = tmp_path / "local-with-origin"
    subprocess.run(
        ["git", "init", str(local)], check=True, capture_output=True
    )
    origin = "https://git.example.test/team/local-with-origin.git"
    subprocess.run(
        ["git", "-C", str(local), "remote", "add", "origin", origin],
        check=True,
        capture_output=True,
    )
    config = _workflow_config(tmp_path)
    controller = TuiController(
        SimpleNamespace(
            config=config,
            defect_candidates=SimpleNamespace(gateway=object()),
        ),
        object(),
        workflow_saver=lambda _workflow: None,
    )

    controller.create_workspace(
        "desktop",
        "project-1",
        "iteration-1",
        (
            WorkspaceRepositoryInput(
                key="primary",
                name="primary",
                source=str(local),
                local=True,
            ),
        ),
    )

    mapping = config.repository_groups[0].repositories[0]
    assert mapping.source_path == local.resolve()
    assert mapping.repo_url == origin
    controller.close()


@pytest.mark.asyncio
async def test_workspace_delete_requires_confirmation_and_removes_only_mapping() -> None:
    from textual.app import App
    from textual.widgets import Button

    from src.developer_workflow.tui.models import WorkspaceSummary
    from src.developer_workflow.tui.screens import (
        WorkspaceDeleteConfirmation,
        WorkspaceDetailScreen,
    )

    workspace = WorkspaceSummary(
        key="desktop",
        project_id="project-1",
        iteration_id="iteration-1",
        repositories=("primary", "dependency"),
    )

    class Controller:
        deleted: list[str] = []

        def delete_workspace(self, key: str) -> None:
            self.deleted.append(key)

    class Supervisor:
        pass

    controller = Controller()
    results: list[bool | None] = []

    class DeleteApp(App[None]):
        CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

        async def on_mount(self) -> None:
            await self.push_screen(
                WorkspaceDetailScreen(  # type: ignore[arg-type]
                    controller,
                    Supervisor(),  # type: ignore[arg-type]
                    workspace,
                ),
                callback=results.append,
            )

    async with DeleteApp().run_test(size=(100, 30)) as pilot:
        pilot.app.screen.query_one("#workspace-delete", Button).press()
        for _ in range(10):
            await pilot.pause()
            if isinstance(pilot.app.screen, WorkspaceDeleteConfirmation):
                break
        assert isinstance(pilot.app.screen, WorkspaceDeleteConfirmation)
        for _ in range(10):
            await pilot.pause()
            button = pilot.app.screen.query_one("#cancel-workspace-delete")
            if button.region.width and button.region.height:
                break
        await pilot.click("#cancel-workspace-delete")
        assert controller.deleted == []
        for _ in range(10):
            await pilot.pause()
            if isinstance(pilot.app.screen, WorkspaceDetailScreen):
                break
        assert isinstance(pilot.app.screen, WorkspaceDetailScreen)

        pilot.app.screen.query_one("#workspace-delete", Button).press()
        for _ in range(10):
            await pilot.pause()
            if isinstance(pilot.app.screen, WorkspaceDeleteConfirmation):
                break
        assert isinstance(pilot.app.screen, WorkspaceDeleteConfirmation)
        for _ in range(10):
            await pilot.pause()
            button = pilot.app.screen.query_one("#confirm-workspace-delete")
            if button.region.width and button.region.height:
                break
        await pilot.click("#confirm-workspace-delete")
        for _ in range(20):
            await pilot.pause()
            if controller.deleted:
                break

        assert controller.deleted == ["desktop"]
        assert results == [True]


def test_controller_loads_project_and_iteration_choices_from_ones(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.tui.controller import TuiController

    class Gateway:
        async def list_projects(self):
            return [{"uuid": "project-1", "name": "Desktop"}]

        async def list_iterations(self, project_id: str):
            assert project_id == "project-1"
            return [{"uuid": "iteration-1", "title": "Sprint 1"}]

    controller = TuiController(
        SimpleNamespace(
            config=_workflow_config(tmp_path),
            defect_candidates=SimpleNamespace(gateway=Gateway()),
        ),
        object(),
    )

    assert [(item.id, item.name) for item in controller.load_workspace_projects()] == [
        ("project-1", "Desktop")
    ]
    assert [
        (item.id, item.name)
        for item in controller.load_workspace_iterations("project-1")
    ] == [("iteration-1", "Sprint 1")]
    controller.close()


@pytest.mark.asyncio
async def test_dashboard_creates_multi_repository_workspace_and_opens_detail() -> None:
    from textual.app import App

    from src.developer_workflow.tui.models import (
        DefectChoice,
        DefectFilterOptions,
        FilterChoice,
        WorkspaceSummary,
    )
    from src.developer_workflow.tui.screens import (
        DashboardScreen,
        DefectStatusFilterScreen,
        DefectWizardScreen,
        SettingsView,
        WorkspaceDetailScreen,
    )

    created = WorkspaceSummary(
        key="desktop",
        project_id="project-1",
        iteration_id="iteration-1",
        repositories=("primary", "dependency"),
    )

    class Controller:
        workspaces: tuple[WorkspaceSummary, ...] = ()
        create_calls: list[tuple[object, ...]] = []

        def list_workspaces(self):
            return self.workspaces

        def list_runs(self, *_args):
            return ()

        def load_workspace_projects(self):
            return (FilterChoice(id="project-1", name="Desktop"),)

        def load_workspace_iterations(self, project_id: str):
            assert project_id == "project-1"
            return (FilterChoice(id="iteration-1", name="Sprint 1"),)

        def create_workspace(self, *args, display_name=""):
            self.create_calls.append(args)
            self.created_name = display_name
            self.workspaces = (created,)
            return created

        def load_defect_filter_options(self, project_id: str):
            assert project_id == "project-1"
            return DefectFilterOptions(
                iterations=(
                    FilterChoice(id="iteration-1", name="Sprint 1"),
                    FilterChoice(id="iteration-2", name="Sprint 2"),
                ),
                assignees=(
                    FilterChoice(id="user-1", name="User", selected=True),
                ),
                statuses=(
                    FilterChoice(id="open", name="Open", selected=True),
                ),
            )

        def query_defects(self, project, iteration, assignee, status_ids):
            assert (project, iteration, assignee, status_ids) == (
                "project-1", "iteration-1", "user-1", ("open",)
            )
            return SimpleNamespace(
                session_id="candidate-session",
                items=(
                    DefectChoice(
                        candidate_id="DEFECT-1",
                        title="预览画面颜色异常",
                        status_id="open",
                        priority="高",
                    ),
                ),
            )

    class Supervisor:
        async def run_readonly(self, _name, call, *args):
            return call(*args)

    controller = Controller()
    screen = DashboardScreen(
        controller,  # type: ignore[arg-type]
        Supervisor(),  # type: ignore[arg-type]
        SettingsView(3, "configured", True),
    )

    class WorkspaceApp(App[None]):
        CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

        async def on_mount(self) -> None:
            await self.push_screen(screen)
            await screen.refresh_workspaces()

    async with WorkspaceApp().run_test(size=(100, 32)) as pilot:
        assert screen.query_one("#workspace-home").display
        assert not screen.query_one("#workspace").display
        assert not screen.query("#nav-defects")
        assert screen.query_one("#workspace-empty").display
        assert "创建工作区" in str(
            screen.query_one("#workspace-empty").render()
        )
        await pilot.resize_terminal(190, 42)
        assert pilot.app.screen.region.width == 190
        await pilot.click("#create-workspace")
        for _ in range(20):
            await pilot.pause()
            if pilot.app.screen.query_one("#workspace-iteration").value == "iteration-1":
                break
        form = pilot.app.screen
        assert form.query_one("#workspace-project").value == "project-1"
        assert form.query_one("#workspace-iteration").value == "iteration-1"
        assert form.query_one("#workspace-name").value == "project-1-iteration-1"
        form.query_one("#workspace-name").value = ""
        assert not form.query("#workspace-repository-key")
        assert not form.query("#workspace-repository-name")
        form.query_one("#workspace-repository-source").value = "C:/repos/primary"
        form.query_one("#workspace-add-repository").press()
        await pilot.pause()
        form.query_one("#workspace-repository-kind").value = "remote"
        form.query_one("#workspace-repository-source").value = (
            "https://git.example.test/team/dependency.git"
        )
        form.query_one("#workspace-add-repository").press()
        await pilot.pause()
        assert form.query_one("#workspace-save").disabled is False
        form.query_one("#workspace-save").press()
        for _ in range(20):
            await pilot.pause()
            if isinstance(pilot.app.screen, WorkspaceDetailScreen):
                break

        assert isinstance(pilot.app.screen, WorkspaceDetailScreen)
        assert sum(
            isinstance(item, WorkspaceDetailScreen)
            for item in pilot.app.screen_stack
        ) == 1
        screen._open_workspace_detail(created)
        await pilot.pause()
        assert sum(
            isinstance(item, WorkspaceDetailScreen)
            for item in pilot.app.screen_stack
        ) == 1
        assert not screen.query_one("#workspace-empty").display
        assert pilot.app.screen.workspace == created
        assert len(controller.create_calls) == 1
        assert controller.create_calls[0][0].startswith("workspace-")
        assert controller.created_name == "project-1-iteration-1"
        repositories = controller.create_calls[0][3]
        assert len(repositories) == 2
        assert repositories[0].local is True
        assert repositories[0].key == "primary"
        assert repositories[0].name == "primary"
        assert repositories[1].local is False
        assert repositories[1].key == "dependency"
        assert repositories[1].name == "dependency"
        for _ in range(20):
            await pilot.pause(0.05)
            if (
                not pilot.app.screen.query_one("#workspace-query-defects").disabled
                and pilot.app.screen._filter_interactions_armed
            ):
                break
        assert not pilot.app.screen.query_one("#workspace-query-defects").disabled
        assert pilot.app.screen._filter_interactions_armed
        filter_card = pilot.app.screen.query_one(".workspace-defect-filter-card")
        toolbar = filter_card.query_one(".workspace-defect-filter-toolbar")
        module_body = filter_card.parent
        assert module_body is not None
        assert module_body.region.width >= pilot.app.screen.region.width - 6
        assert toolbar.region.width == filter_card.content_region.width
        assignee = toolbar.query_one("#workspace-defect-assignee")
        status_filter = toolbar.query_one("#workspace-defect-status-filter-button")
        reload_button = toolbar.query_one("#workspace-reload-defect-options")
        query_button = filter_card.query_one("#workspace-query-defects")
        assert assignee.region.right < status_filter.region.x
        assert status_filter.region.right < reload_button.region.x
        assert reload_button.region.right < query_button.region.x
        assert query_button.region.right < filter_card.region.right
        assert query_button.region.height == 3
        assert query_button.styles.margin.bottom == 0
        status_button = filter_card.query_one("#workspace-defect-status-filter-button")
        assert "1 项" in str(status_button.label)
        status_button.press()
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(pilot.app.screen, DefectStatusFilterScreen) and tuple(
                pilot.app.screen.query_one("#defect-status-filter-list").selected
            ):
                break
        assert isinstance(pilot.app.screen, DefectStatusFilterScreen)
        assert tuple(
            pilot.app.screen.query_one("#defect-status-filter-list").selected
        ) == ("open",)
        pilot.app.screen.query_one("#defect-status-filter-apply").press()
        await pilot.pause()
        assert isinstance(pilot.app.screen, WorkspaceDetailScreen)
        assert pilot.app.screen.region.width == 190
        pilot.app.screen.query_one("#workspace-query-defects").press()
        for _ in range(20):
            await pilot.pause(0.05)
            if pilot.app.screen.query(".workspace-defect-card"):
                break
        assert isinstance(pilot.app.screen, WorkspaceDetailScreen)
        assert "共找到 1 个缺陷" in str(
            pilot.app.screen.query_one("#workspace-defect-status").render()
        )
        cards = list(pilot.app.screen.query(".workspace-defect-card"))
        assert len(cards) == 1
        assert cards[0].has_class("high")
        assert not pilot.app.screen.query(".workspace-defect-scope")
        assert "预览画面颜色异常" in str(
            cards[0].query_one(".workspace-defect-title").render()
        )
        assert "状态：Open" in str(
            cards[0].query_one(".workspace-defect-meta").render()
        )
        info = cards[0].query_one(".workspace-defect-info")
        actions = cards[0].query_one(".workspace-defect-actions")
        assert actions.region.x > info.region.x
        action_buttons = list(actions.query("Button"))
        assert len(action_buttons) == 2
        assert action_buttons[0].region.y == action_buttons[1].region.y
        assert pilot.app.screen.query_one("#workspace-repair-defect-0")
        pilot.app.screen.query_one("#workspace-repair-defect-0").press()
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(pilot.app.screen, DefectWizardScreen):
                break
        assert isinstance(pilot.app.screen, DefectWizardScreen)
        assert pilot.app.screen._workspace == created
        pilot.app.screen.action_cancel()
        await pilot.pause()
        assert isinstance(pilot.app.screen, WorkspaceDetailScreen)
        pilot.app.screen.query_one("#workspace-detail-back").press()
        for _ in range(20):
            await pilot.pause(0.05)
            if pilot.app.screen is screen:
                break
        assert pilot.app.screen is screen
        assert screen.region.width == 190
        assert screen.query_one("#workspace-home").display
        assert len(screen.query(".workspace-home-card")) == 1
        assert created.label in str(
            screen.query_one(".workspace-home-title").render()
        )
        assert created.label in pilot.app.export_screenshot()
        await pilot.click(screen.query_one(".workspace-home-card"))
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(pilot.app.screen, WorkspaceDetailScreen):
                break
        await pilot.pause(0.3)
        assert isinstance(pilot.app.screen, WorkspaceDetailScreen)
        assert not isinstance(pilot.app.screen, DefectStatusFilterScreen)
