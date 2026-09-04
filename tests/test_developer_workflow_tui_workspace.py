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
        DefectFilterOptions,
        FilterChoice,
        WorkspaceSummary,
    )
    from src.developer_workflow.tui.screens import (
        DashboardScreen,
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

        def create_workspace(self, *args):
            self.create_calls.append(args)
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

    async with WorkspaceApp().run_test(size=(120, 36)) as pilot:
        assert screen.query_one("#workspace-home").display
        assert not screen.query_one("#workspace").display
        assert not screen.query("#nav-defects")
        assert screen.query_one("#workspace-empty").display
        assert "Create workspace" in str(
            screen.query_one("#workspace-empty").render()
        )
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
        assert not screen.query_one("#workspace-empty").display
        assert pilot.app.screen.workspace == created
        assert len(controller.create_calls) == 1
        assert controller.create_calls[0][0] == "project-1-iteration-1"
        repositories = controller.create_calls[0][3]
        assert len(repositories) == 2
        assert repositories[0].local is True
        assert repositories[0].key == "primary"
        assert repositories[0].name == "primary"
        assert repositories[1].local is False
        assert repositories[1].key == "dependency"
        assert repositories[1].name == "dependency"
        pilot.app.screen.query_one("#workspace-query-defects").press()
        await pilot.pause()
        assert isinstance(pilot.app.screen, DefectWizardScreen)
        assert pilot.app.screen._workspace == created
        assert pilot.app.screen.query_one("#project").value == "project-1"
        assert pilot.app.screen.query_one("#project").disabled
        for _ in range(10):
            await pilot.pause()
            if pilot.app.screen.query_one("#iteration").value == "iteration-1":
                break
        assert pilot.app.screen.query_one("#iteration").value == "iteration-1"
        assert pilot.app.screen.query_one("#iteration").disabled
