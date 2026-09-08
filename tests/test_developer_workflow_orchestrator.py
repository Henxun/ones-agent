from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import asyncio
import hashlib
import threading
import time

import pytest

from src.contracts import (
    DefectRecord,
    IdentityRef,
    IssueTypeRef,
    PriorityRef,
    ProjectRef,
    StatusRef,
    WikiPageSnapshot,
)
from src.developer_workflow.approval import issue_approval
from src.developer_workflow.config import DeveloperWorkflowConfig, PublishingConfig
from src.developer_workflow.contracts import (
    AcceptanceCoverage,
    ApprovalPackage,
    CodingAgentProvenance,
    CodexResult,
    CommandResult,
    PreparedWorktree,
    PublicationResult,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    RepositorySnapshot,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
    utc_now,
)
from src.developer_workflow.orchestrator import (
    DeveloperWorkflowOrchestrator,
    InvalidWorkflowAction,
)
from src.developer_workflow.defect_flow import DefectCandidateService
from src.developer_workflow.requirement_flow import RequirementFlow
from src.developer_workflow.state_store import ConcurrentRunUpdateError
from src.developer_workflow.state_store import FileRunStore, InvalidRunTransitionError


class RecordingStore:
    def __init__(self, run: WorkflowRun | None = None) -> None:
        self.run = run
        self.calls: list[tuple[object, ...]] = []
        self.save_error: Exception | None = None

    def create(self, run: WorkflowRun) -> WorkflowRun:
        self.calls.append(("create", run))
        self.run = run.validated_update(version=1)
        return self.run

    def load(self, run_id: str, *, read_only: bool = False) -> WorkflowRun:
        self.calls.append(
            ("load_read_only", run_id) if read_only else ("load", run_id)
        )
        assert self.run is not None and self.run.run_id == run_id
        return self.run

    def save(self, run: WorkflowRun, expected_version: int) -> WorkflowRun:
        self.calls.append(("save", run, expected_version))
        if self.save_error is not None:
            raise self.save_error
        assert self.run is not None and expected_version == self.run.version
        self.run = run.validated_update(version=expected_version + 1)
        return self.run

    def transition(
        self,
        run_id: str,
        expected_version: int,
        target: WorkflowState,
        reason: str,
        resume_state: WorkflowState | None = None,
    ) -> WorkflowRun:
        self.calls.append(("transition", target, reason, resume_state))
        assert self.run is not None and self.run.run_id == run_id
        assert expected_version == self.run.version
        self.run = self.run.validated_update(
            state=target,
            version=expected_version + 1,
            resume_state=resume_state if target is WorkflowState.BLOCKED else None,
            blocked_reason=reason if target is WorkflowState.BLOCKED else "",
        )
        return self.run

    def operation_lock(self, run_id: str, purpose: str):
        self.calls.append(("operation_lock", run_id, purpose))
        return nullcontext()


def test_show_supports_explicit_read_only_storage_access() -> None:
    run = WorkflowRun.new("requirement", "REQ-read-only")
    store = RecordingStore(run)
    orchestrator = DeveloperWorkflowOrchestrator(
        store=store,  # type: ignore[arg-type]
        requirement_flow=None,  # type: ignore[arg-type]
        defect_flow=None,  # type: ignore[arg-type]
        publisher=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        defect_candidates=None,  # type: ignore[arg-type]
    )

    assert orchestrator.show(run.run_id, read_only=True) is run
    assert store.calls == [("load_read_only", run.run_id)]


class RecordingFlow:
    def __init__(self) -> None:
        self.calls: list[WorkflowRun] = []

    def execute(self, run: WorkflowRun) -> WorkflowRun:
        self.calls.append(run)
        return run


class RecordingPublisher:
    def __init__(self) -> None:
        self.publish_calls: list[WorkflowRun] = []
        self.retry_calls: list[WorkflowRun] = []

    def publish(self, run: WorkflowRun) -> WorkflowRun:
        self.publish_calls.append(run)
        return run

    def retry_comment(self, run: WorkflowRun) -> WorkflowRun:
        self.retry_calls.append(run)
        return run


class RecordingCandidates:
    def __init__(self, selected: WorkflowRun | None = None) -> None:
        self.selected = selected
        self.calls: list[tuple[object, ...]] = []

    def select(
        self,
        snapshot_token: str,
        candidate_id: str,
        *,
        project_id: str,
        iteration_id: str,
        assignee_id: str,
    ) -> WorkflowRun:
        self.calls.append(
            (snapshot_token, candidate_id, project_id, iteration_id, assignee_id)
        )
        return self.selected or WorkflowRun.new_defect(
            project_id, iteration_id, assignee_id, candidate_id
        ).validated_update(
            defect=_defect(candidate_id)
        )


class RequirementResetHarness:
    """Exercise the production RequirementFlow revision reset without later stages."""

    def __init__(self, store: FileRunStore) -> None:
        self.store = store
        self.calls: list[WorkflowRun] = []
        self.implementation_calls = 0
        self.delegate = RequirementFlow(
            store=store,
            gateway=None,  # type: ignore[arg-type]
            config=None,  # type: ignore[arg-type]
            repository=None,  # type: ignore[arg-type]
            codex=None,  # type: ignore[arg-type]
            test_runner=None,  # type: ignore[arg-type]
        )

    def execute(self, run: WorkflowRun) -> WorkflowRun:
        self.calls.append(run)
        assert run.state is WorkflowState.BLOCKED
        assert run.resume_state is WorkflowState.IMPLEMENTING
        current = self.store.transition(
            run.run_id,
            run.version,
            WorkflowState.IMPLEMENTING,
            "resume from persisted safe checkpoint",
        )
        current = self.delegate._reset_resumed_stage(
            current, WorkflowState.IMPLEMENTING
        )
        self.implementation_calls += 1
        return current


def _mapping(key: str = "repo", iteration: str = "ITER-1") -> RepositoryMapping:
    return RepositoryMapping(
        key=key,
        project_id="PROJ-1",
        iteration_id=iteration,
        repo_url="ssh://git@example.invalid/team/repo.git",
        repo_name=key,
    )


def _config(tmp_path: Path) -> DeveloperWorkflowConfig:
    return DeveloperWorkflowConfig(
        run_root=tmp_path / "runs",
        worktree_root=tmp_path / "worktrees",
        mirror_root=tmp_path / "mirrors",
        sandbox_permission_profile="managed-dev",
        max_codex_attempts=2,
        repositories=(_mapping(), _mapping("repo-default", "*")),
        publishing=PublishingConfig(
            provider="github",
            default_target_branch="main",
        ),
    )


def _orchestrator(
    tmp_path: Path, run: WorkflowRun | None = None
) -> tuple[
    DeveloperWorkflowOrchestrator,
    RecordingStore,
    RecordingFlow,
    RecordingFlow,
    RecordingPublisher,
]:
    store = RecordingStore(run)
    requirement = RecordingFlow()
    defect = RecordingFlow()
    publisher = RecordingPublisher()
    candidates = RecordingCandidates()
    return (
        DeveloperWorkflowOrchestrator(
            store, requirement, defect, publisher, _config(tmp_path), candidates
        ),
        store,
        requirement,
        defect,
        publisher,
    )


@pytest.mark.parametrize("method,args", [
    ("resume", ()),
    ("confirm_repository", ("repo",)),
    ("revise", ("feedback",)),
    ("approve", ("operator",)),
    ("cancel", ("operator",)),
])
def test_expected_version_is_checked_after_lock_and_load_before_side_effects(
    tmp_path: Path, method: str, args: tuple[str, ...]
) -> None:
    run = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1").validated_update(
        version=7, state=WorkflowState.WAITING_APPROVAL
    )
    orchestrator, store, requirement, defect, publisher = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="^workflow changed; review again$"):
        getattr(orchestrator, method)(run.run_id, *args, expected_version=6)

    assert store.calls == [
        ("operation_lock", run.run_id, "orchestrate"),
        ("load", run.run_id),
    ]
    assert requirement.calls == defect.calls == []
    assert publisher.publish_calls == publisher.retry_calls == []


def test_expected_version_rejects_bool_even_when_equal(tmp_path: Path) -> None:
    run = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1").validated_update(version=1)
    orchestrator, store, *_ = _orchestrator(tmp_path, run)
    with pytest.raises(InvalidWorkflowAction, match="^workflow changed; review again$"):
        orchestrator.resume(run.run_id, expected_version=True)
    assert store.calls[-1] == ("load", run.run_id)


def _defect(defect_id: str = "1" * 32) -> DefectRecord:
    return DefectRecord(
        defect_id=defect_id,
        number="7",
        title="Export crashes",
        project=ProjectRef(id="PROJ-1", name="Project"),
        status=StatusRef(id="doing", name="Doing", category="doing"),
        issue_type=IssueTypeRef(id="bug", name="Bug"),
        priority=PriorityRef(id="high", value="High"),
        assignee=IdentityRef(id="USER-1", name="Alice"),
        description="Exporting an empty report crashes.",
        updated_at="2026-08-10T01:02:03Z",
        raw={"key": "BUG-7", "sprint": {"uuid": "ITER-1"}},
    )


def _at_state(run: WorkflowRun, state: WorkflowState, **updates: object) -> WorkflowRun:
    return run.validated_update(state=state, version=7, **updates)


def _approval() -> ApprovalPackage:
    timestamp = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    repository = _mapping()
    wiki = WikiPageSnapshot(
        team_id="TEAM-1",
        space_id="SPACE-1",
        page_id="PAGE-1",
        title="Design",
        version="3",
        updated_at="2026-08-10T09:00:00Z",
        normalized_content="acceptance criteria",
        content_sha256="b" * 64,
        source_url="http://ones.local/wiki/team/TEAM-1/space/SPACE-1/page/PAGE-1",
    )
    return ApprovalPackage(
        work_item_id="REQ-1",
        work_item_title="Requirement",
        work_item_status="In progress",
        source_versions={"requirement_version": "7", "content_sha256": "a" * 64},
        wiki_hashes={"PAGE-1": "b" * 64},
        wiki_snapshots=(wiki,),
        repository=repository,
        repo_url=repository.repo_url,
        base_branch="main",
        base_commit="c" * 40,
        head_commit="d" * 40,
        diff_hash="e" * 64,
        diff_summary="2 files changed",
        branch="requirement/REQ-1",
        changed_files=("src/a.py", "tests/test_a.py"),
        coverage={"criterion": "tests/test_a.py::test_a"},
        evidence=("src/a.py:1",),
        tests=(
            CommandResult(
                command="uv run pytest tests/test_a.py",
                argv=("uv", "run", "pytest", "tests/test_a.py"),
                exit_code=0,
                summary="1 passed",
                started_at=timestamp,
                finished_at=timestamp,
            ),
        ),
        review=("No blocking findings",),
        unrelated_changes_checked=True,
        commit_message="feat: requirement",
        pr_title="feat: requirement",
        pr_body="Implements REQ-1",
    )


def test_start_commands_create_exact_run_and_dispatch(tmp_path: Path) -> None:
    orchestrator, store, requirement, defect, _ = _orchestrator(tmp_path)

    requirement_run = orchestrator.start_requirement("REQ-1")
    assert requirement_run.type is WorkflowType.REQUIREMENT
    assert requirement.calls == [requirement_run]
    assert defect.calls == []

    defect_run = orchestrator.start_defect(
        "PROJ-1", "ITER-1", "USER-1", "snapshot-token", "DEF-1"
    )
    assert defect_run.type is WorkflowType.DEFECT
    assert (defect_run.project_id, defect_run.iteration_id, defect_run.assignee_id) == (
        "PROJ-1",
        "ITER-1",
        "USER-1",
    )
    assert defect.calls == [defect_run]
    assert [call[0] for call in store.calls].count("create") == 2
    assert [call[0] for call in store.calls].count("operation_lock") == 2
    assert [call[0] for call in store.calls].count("load") == 2


def test_start_requirement_records_selected_coding_agent(tmp_path: Path) -> None:
    orchestrator, _, requirement, _, _ = _orchestrator(tmp_path)
    orchestrator.coding_agent = CodingAgentProvenance(
        key="claude", label="Claude Code"
    )

    run = orchestrator.start_requirement("REQ-agent")

    assert run.coding_agent == orchestrator.coding_agent
    assert requirement.calls == [run]


def test_start_defect_selects_scoped_snapshot_before_create(tmp_path: Path) -> None:
    class Gateway:
        async def list_open_defects(self, **kwargs: object) -> list[DefectRecord]:
            return [_defect()]

    candidates = DefectCandidateService(gateway=Gateway(), issue_type_id="bug")
    listed = asyncio.run(candidates.list_candidates("PROJ-1", "ITER-1", "USER-1"))
    store, requirement, defect, publisher = (
        RecordingStore(),
        RecordingFlow(),
        RecordingFlow(),
        RecordingPublisher(),
    )
    orchestrator = DeveloperWorkflowOrchestrator(
        store, requirement, defect, publisher, _config(tmp_path), candidates
    )

    run = orchestrator.start_defect(
        "PROJ-1",
        "ITER-1",
        "USER-1",
        listed[0].snapshot_token,
        "BUG-7",
    )

    assert run.defect == _defect()
    assert run.work_item_id == "1" * 32
    assert defect.calls == [run]


def test_start_defect_analysis_persists_action_with_real_store(tmp_path: Path) -> None:
    from src.developer_workflow.contracts import DefectAction
    from src.developer_workflow.state_store import FileRunStore

    class Gateway:
        async def list_open_defects(self, **kwargs: object) -> list[DefectRecord]:
            return [_defect()]

    candidates = DefectCandidateService(gateway=Gateway(), issue_type_id="bug")
    listed = asyncio.run(candidates.list_candidates("PROJ-1", "ITER-1", "USER-1"))
    requirement, defect, publisher = (
        RecordingFlow(),
        RecordingFlow(),
        RecordingPublisher(),
    )
    orchestrator = DeveloperWorkflowOrchestrator(
        FileRunStore(tmp_path / "runs"),
        requirement,
        defect,
        publisher,
        _config(tmp_path),
        candidates,
    )

    run = orchestrator.start_defect(
        "PROJ-1",
        "ITER-1",
        "USER-1",
        listed[0].snapshot_token,
        "BUG-7",
        action=DefectAction.ANALYZE,
    )

    assert run.defect_action is DefectAction.ANALYZE
    assert defect.calls == [run]


def test_start_defect_rejects_candidate_service_without_frozen_defect(
    tmp_path: Path,
) -> None:
    bare = WorkflowRun.new_defect("PROJ-1", "ITER-1", "USER-1", "DEF-1")
    store, requirement, defect, publisher = (
        RecordingStore(),
        RecordingFlow(),
        RecordingFlow(),
        RecordingPublisher(),
    )
    orchestrator = DeveloperWorkflowOrchestrator(
        store,
        requirement,
        defect,
        publisher,
        _config(tmp_path),
        RecordingCandidates(bare),
    )

    with pytest.raises(InvalidWorkflowAction, match="verified candidate snapshot"):
        orchestrator.start_defect(
            "PROJ-1", "ITER-1", "USER-1", "snapshot-token", "DEF-1"
        )

    assert not store.calls and not defect.calls


def test_start_reloads_inside_operation_lock_before_execution(tmp_path: Path) -> None:
    class ReloadingStore(RecordingStore):
        def create(self, run: WorkflowRun) -> WorkflowRun:
            created = super().create(run)
            assert self.run is not None
            self.run = self.run.validated_update(state=WorkflowState.VALIDATING)
            return created

    store = ReloadingStore()
    requirement, defect, publisher = RecordingFlow(), RecordingFlow(), RecordingPublisher()
    orchestrator = DeveloperWorkflowOrchestrator(
        store,
        requirement,
        defect,
        publisher,
        _config(tmp_path),
        RecordingCandidates(),
    )

    result = orchestrator.start_requirement("REQ-1")

    assert result.state is WorkflowState.VALIDATING
    assert requirement.calls == [result]
    assert [call[0] for call in store.calls][:3] == [
        "create",
        "operation_lock",
        "load",
    ]


def test_start_flow_crash_leaves_created_run_resumable_without_recreate(
    tmp_path: Path,
) -> None:
    class RaisingFlow:
        def execute(self, run: WorkflowRun) -> WorkflowRun:
            raise RuntimeError("simulated local crash")

    store, defect, publisher = RecordingStore(), RecordingFlow(), RecordingPublisher()
    failing = DeveloperWorkflowOrchestrator(
        store,
        RaisingFlow(),
        defect,
        publisher,
        _config(tmp_path),
        RecordingCandidates(),
    )

    with pytest.raises(RuntimeError, match="simulated local crash"):
        failing.start_requirement("REQ-1")

    assert store.run is not None and store.run.state is WorkflowState.CREATED
    resumed_flow = RecordingFlow()
    resumed = DeveloperWorkflowOrchestrator(
        store,
        resumed_flow,
        defect,
        publisher,
        _config(tmp_path),
        RecordingCandidates(),
    ).resume(store.run.run_id)
    assert resumed_flow.calls == [resumed]
    assert [call[0] for call in store.calls].count("create") == 1


def test_show_only_loads_persisted_run(tmp_path: Path) -> None:
    run = WorkflowRun.new("requirement", "REQ-1").validated_update(version=3)
    orchestrator, store, requirement, defect, publisher = _orchestrator(tmp_path, run)

    assert orchestrator.show(run.run_id) is run
    assert store.calls == [("load", run.run_id)]
    assert not requirement.calls and not defect.calls and not publisher.publish_calls


@pytest.mark.parametrize("state", [WorkflowState.CREATED, WorkflowState.IMPLEMENTING])
def test_confirm_repository_requires_validating(tmp_path: Path, state: WorkflowState) -> None:
    run = _at_state(WorkflowRun.new("requirement", "REQ-1"), state)
    orchestrator, store, requirement, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="requires VALIDATING"):
        orchestrator.confirm_repository(run.run_id, "repo")

    assert not requirement.calls
    assert not any(call[0] == "save" for call in store.calls)


@pytest.mark.parametrize("key", ["missing", "repo-default"])
def test_confirm_repository_rejects_unconfigured_or_wrong_scope_without_echo(
    tmp_path: Path, key: str
) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.VALIDATING,
        project_id="OTHER-PROJECT",
        iteration_id="ITER-1",
    )
    orchestrator, store, requirement, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction) as caught:
        orchestrator.confirm_repository(run.run_id, key)

    assert key not in str(caught.value)
    assert not requirement.calls
    assert not any(call[0] == "save" for call in store.calls)


@pytest.mark.parametrize("key", ["repo", "repo-default"])
def test_confirm_repository_saves_exact_or_project_default_mapping_then_resumes(
    tmp_path: Path, key: str
) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.VALIDATING,
        project_id="PROJ-1",
        iteration_id="ITER-1",
        repository_candidates=(_mapping(), _mapping("repo-default", "*")),
    )
    orchestrator, store, requirement, _, _ = _orchestrator(tmp_path, run)

    result = orchestrator.confirm_repository(run.run_id, key)

    assert result.repository is not None and result.repository.key == key
    assert requirement.calls == [result]
    assert any(call[0] == "save" and call[2] == 7 for call in store.calls)


def test_confirm_repository_group_requires_exact_persisted_candidate(
    tmp_path: Path,
) -> None:
    dependency = _mapping("sdk").validated_update(role=RepositoryRole.DEPENDENCY)
    primary = _mapping("app").validated_update(
        role=RepositoryRole.PRIMARY, depends_on=("sdk",)
    )
    group = RepositoryGroupMapping(
        key="suite", project_id="PROJ-1", iteration_id="ITER-1",
        primary_repository="app", repositories=(dependency, primary),
    )
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"), WorkflowState.VALIDATING,
        project_id="PROJ-1", iteration_id="ITER-1",
        repository_group_candidates=(group,),
    )
    orchestrator, store, requirement, defect, publisher = _orchestrator(tmp_path, run)
    orchestrator.config = orchestrator.config.model_copy(
        update={"repositories": (), "repository_groups": (group,)}
    )

    result = orchestrator.confirm_repository(run.run_id, "suite")

    assert result.repository_model_version == 2
    assert result.repository_group == group
    assert result.repository is None
    assert requirement.calls == [result]


def test_confirm_repository_rejects_mapping_added_after_candidate_snapshot(
    tmp_path: Path,
) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.VALIDATING,
        project_id="PROJ-1",
        iteration_id="ITER-1",
        repository_candidates=(_mapping(),),
    )
    orchestrator, store, requirement, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="persisted candidate"):
        orchestrator.confirm_repository(run.run_id, "repo-default")

    assert not requirement.calls
    assert not any(call[0] == "save" for call in store.calls)


def test_confirm_repository_rejects_same_key_configuration_drift(tmp_path: Path) -> None:
    drifted = _mapping().validated_update(
        repo_url="ssh://git@example.invalid/team/old-repo.git"
    )
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.VALIDATING,
        project_id="PROJ-1",
        iteration_id="ITER-1",
        repository_candidates=(drifted,),
    )
    orchestrator, store, requirement, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="persisted candidate"):
        orchestrator.confirm_repository(run.run_id, "repo")

    assert not requirement.calls
    assert not any(call[0] == "save" for call in store.calls)


def test_confirm_repository_cas_conflict_has_no_flow_side_effect(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.VALIDATING,
        project_id="PROJ-1",
        iteration_id="ITER-1",
        repository_candidates=(_mapping(),),
    )
    orchestrator, store, requirement, _, publisher = _orchestrator(tmp_path, run)
    store.save_error = ConcurrentRunUpdateError("workflow run version does not match")

    with pytest.raises(ConcurrentRunUpdateError):
        orchestrator.confirm_repository(run.run_id, "repo")

    assert not requirement.calls
    assert not publisher.publish_calls and not publisher.retry_calls


def test_resume_partial_only_retries_comment_and_publishing_only_publishes(tmp_path: Path) -> None:
    partial = _at_state(WorkflowRun.new("requirement", "REQ-1"), WorkflowState.PARTIAL_SUCCESS)
    orchestrator, store, requirement, _, publisher = _orchestrator(tmp_path, partial)
    assert orchestrator.resume(partial.run_id) is partial
    assert publisher.retry_calls == [partial]
    assert not publisher.publish_calls and not requirement.calls

    publishing = partial.validated_update(state=WorkflowState.PUBLISHING)
    store.run = publishing
    assert orchestrator.resume(publishing.run_id) is publishing
    assert publisher.publish_calls == [publishing]
    assert publisher.retry_calls == [partial] and not requirement.calls


def test_resume_blocked_without_safe_checkpoint_is_rejected(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.BLOCKED,
        resume_state=None,
        blocked_reason="corrupt legacy record",
    )
    orchestrator, _, requirement, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="no safe resume state"):
        orchestrator.resume(run.run_id)
    assert not requirement.calls


def test_resume_blocked_for_publishing_resumes_then_only_publishes(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.BLOCKED,
        resume_state=WorkflowState.PUBLISHING,
        blocked_reason="provider unavailable",
    )
    orchestrator, store, requirement, _, publisher = _orchestrator(tmp_path, run)

    result = orchestrator.resume(run.run_id)

    assert result.state is WorkflowState.PUBLISHING
    assert publisher.publish_calls == [result]
    assert not publisher.retry_calls and not requirement.calls
    assert any(
        call[0] == "transition" and call[1] is WorkflowState.PUBLISHING
        for call in store.calls
    )


@pytest.mark.parametrize(
    "state", [WorkflowState.CREATED, WorkflowState.VALIDATING, WorkflowState.TESTING]
)
def test_resume_never_skips_current_main_chain_state(tmp_path: Path, state: WorkflowState) -> None:
    run = _at_state(WorkflowRun.new("requirement", "REQ-1"), state)
    orchestrator, _, requirement, _, publisher = _orchestrator(tmp_path, run)

    assert orchestrator.resume(run.run_id) is run
    assert requirement.calls == [run]
    assert not publisher.publish_calls and not publisher.retry_calls


def test_unknown_workflow_type_is_rejected_without_dispatch(tmp_path: Path) -> None:
    orchestrator, _, requirement, defect, _ = _orchestrator(tmp_path)

    with pytest.raises(InvalidWorkflowAction, match="Unknown workflow type"):
        orchestrator._flow_for(SimpleNamespace(workflow_type="alien"))
    assert not requirement.calls and not defect.calls


def test_public_package_exports_orchestrator() -> None:
    from src.developer_workflow import (
        DeveloperWorkflowOrchestrator as ExportedOrchestrator,
        InvalidWorkflowAction as ExportedError,
    )

    assert ExportedOrchestrator is DeveloperWorkflowOrchestrator
    assert ExportedError is InvalidWorkflowAction


def test_revise_waiting_approval_clears_signature_and_restarts_implementation(
    tmp_path: Path,
) -> None:
    signed = issue_approval(_approval(), approved_by="reviewer")
    prepared = PreparedWorktree(
        path=(tmp_path / "worktree").resolve(),
        branch="requirement/REQ-1",
        base_commit="c" * 40,
        head_commit="d" * 40,
        mirror_path=(tmp_path / "mirror").resolve(),
    )
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.WAITING_APPROVAL,
        approval=signed,
        prepared_worktree=prepared,
        investigation_suggestions=("retain diagnostic",),
    )
    orchestrator, store, requirement, _, publisher = _orchestrator(tmp_path, run)

    result = orchestrator.revise(run.run_id, "cover an edge case")

    assert result.state is WorkflowState.BLOCKED
    assert result.resume_state is WorkflowState.IMPLEMENTING
    assert result.approval is not None
    assert result.approval.fingerprint == ""
    assert result.approval.approved_by is None and result.approval.approved_at is None
    assert result.revisions[-1].feedback == "cover an edge case"
    assert result.prepared_worktree == prepared
    assert result.investigation_suggestions == ("retain diagnostic",)
    assert requirement.calls == [result]
    assert not publisher.publish_calls
    assert any(call[0] == "transition" for call in store.calls)


def test_revise_requirement_accepts_explicit_implementation_scope(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"), WorkflowState.WAITING_APPROVAL
    )
    orchestrator, _, requirement, defect, _ = _orchestrator(tmp_path, run)

    result = orchestrator.revise(
        run.run_id, "cover the boundary", scope="implementation"
    )

    assert result.revisions[-1].feedback == "cover the boundary"
    assert requirement.calls == [result]
    assert not defect.calls


@pytest.mark.parametrize(
    "scope",
    [
        "root_cause",
        "reproduction",
        "analysis",
        "unknown",
        " repair",
        "repair\n",
        "repair\u200d",
        1,
        True,
    ],
)
def test_revise_rejects_unsafe_scope_before_lock_or_state_change(
    tmp_path: Path, scope: object
) -> None:
    run = _at_state(
        WorkflowRun.new_defect("PROJ-1", "ITER-1", "alice", "BUG-1"),
        WorkflowState.WAITING_APPROVAL,
    )
    orchestrator, store, requirement, defect, _ = _orchestrator(tmp_path, run)
    before = store.run

    with pytest.raises(
        InvalidWorkflowAction,
        match="start a new defect run to rebuild evidence",
    ) as caught:
        orchestrator.revise(  # type: ignore[arg-type]
            run.run_id, "secret revision feedback", scope=scope
        )

    assert "secret revision feedback" not in str(caught.value)
    assert store.run == before
    assert not store.calls
    assert not requirement.calls and not defect.calls


def test_revise_defect_rejects_implementation_scope_without_mutation(
    tmp_path: Path,
) -> None:
    run = _at_state(
        WorkflowRun.new_defect("PROJ-1", "ITER-1", "alice", "BUG-1"),
        WorkflowState.WAITING_APPROVAL,
    )
    orchestrator, store, requirement, defect, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="start a new defect run"):
        orchestrator.revise(run.run_id, "safe feedback", scope="implementation")

    assert store.run == run
    assert not any(call[0] in {"save", "transition"} for call in store.calls)
    assert not requirement.calls and not defect.calls


def test_revise_defect_defaults_to_repair_scope(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new_defect("PROJ-1", "ITER-1", "alice", "BUG-1"),
        WorkflowState.WAITING_APPROVAL,
    )
    orchestrator, _, requirement, defect, _ = _orchestrator(tmp_path, run)

    result = orchestrator.revise(run.run_id, "tighten the existing repair")

    assert result.revisions[-1].feedback == "tighten the existing repair"
    assert defect.calls == [result]
    assert not requirement.calls


def test_revise_requirement_rejects_repair_scope_without_mutation(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"), WorkflowState.WAITING_APPROVAL
    )
    orchestrator, store, requirement, defect, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="revision scope is invalid"):
        orchestrator.revise(run.run_id, "safe feedback", scope="repair")

    assert store.run == run
    assert not any(call[0] in {"save", "transition"} for call in store.calls)
    assert not requirement.calls and not defect.calls


def test_revise_blocked_at_implementation_uses_saved_checkpoint(tmp_path: Path) -> None:
    signed = issue_approval(_approval(), approved_by="reviewer")
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.BLOCKED,
        approval=signed,
        resume_state=WorkflowState.IMPLEMENTING,
        blocked_reason="review requested",
        investigation_suggestions=("retain diagnostic",),
    )
    orchestrator, _, requirement, _, _ = _orchestrator(tmp_path, run)

    result = orchestrator.revise(run.run_id, "change implementation")

    assert result.state is WorkflowState.BLOCKED
    assert result.resume_state is WorkflowState.IMPLEMENTING
    assert result.revisions[-1].feedback == "change implementation"
    assert result.approval is not None and not result.approval.fingerprint
    assert result.investigation_suggestions == ("retain diagnostic",)
    assert requirement.calls == [result]


def test_revise_waiting_approval_obeys_real_store_history_and_cas(tmp_path: Path) -> None:
    real_store = FileRunStore(tmp_path / "revision-runs")
    run = real_store.create(WorkflowRun.new("requirement", "REQ-1"))
    for state in (
        WorkflowState.READING_ONES,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING,
        WorkflowState.AI_REVIEW,
        WorkflowState.WAITING_APPROVAL,
    ):
        run = real_store.transition(run.run_id, run.version, state, f"enter {state.value}")
    signed = issue_approval(_approval(), approved_by="reviewer")
    source_result = CodexResult(summary="preflight")
    stale_implementation = CodexResult(summary="old implementation")
    stale_review = CodexResult(summary="old review")
    stale_snapshot = RepositorySnapshot(
        head_commit="d" * 40,
        diff_sha256=hashlib.sha256(b"").hexdigest(),
        is_clean=True,
    )
    stale_coverage = AcceptanceCoverage(
        criterion_id="AC-1",
        criterion_text="old criterion",
        files=("src/a.py",),
        tests=("uv run pytest",),
    )
    run = real_store.save(
        run.validated_update(
            approval=signed,
            codex_results=(source_result, stale_implementation),
            test_results=_approval().tests,
            tested_snapshot=stale_snapshot,
            acceptance_coverage=(stale_coverage,),
            retry_count=3,
            review=stale_review,
        ),
        run.version,
    )
    requirement = RequirementResetHarness(real_store)
    defect, publisher = RecordingFlow(), RecordingPublisher()
    orchestrator = DeveloperWorkflowOrchestrator(
        real_store,
        requirement,
        defect,
        publisher,
        _config(tmp_path),
        RecordingCandidates(),
    )

    revised = orchestrator.revise(run.run_id, "cover edge case")

    assert revised.state is WorkflowState.IMPLEMENTING
    assert revised.history[-2].target is WorkflowState.BLOCKED
    assert revised.history[-1].source is WorkflowState.BLOCKED
    assert revised.history[-1].target is WorkflowState.IMPLEMENTING
    assert revised.codex_results == (source_result,)
    assert revised.test_results == ()
    assert revised.tested_snapshot is None
    assert revised.acceptance_coverage == ()
    assert revised.retry_count == 0
    assert revised.review is None and revised.approval is None
    assert revised.revisions[-1].feedback == "cover edge case"
    assert requirement.implementation_calls == 1
    assert requirement.calls[0].state is WorkflowState.BLOCKED
    assert real_store.load(run.run_id) == revised


@pytest.mark.parametrize("resume_state", [WorkflowState.TESTING, WorkflowState.AI_REVIEW])
def test_revise_late_blocked_checkpoint_legally_returns_to_implementation(
    tmp_path: Path, resume_state: WorkflowState
) -> None:
    real_store = FileRunStore(tmp_path / f"blocked-{resume_state.value.lower()}")
    run = real_store.create(WorkflowRun.new("requirement", "REQ-1"))
    chain = (
        WorkflowState.READING_ONES,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING,
        WorkflowState.AI_REVIEW,
    )
    for state in chain:
        run = real_store.transition(run.run_id, run.version, state, f"enter {state.value}")
        if state is resume_state:
            break
    run = real_store.transition(
        run.run_id,
        run.version,
        WorkflowState.BLOCKED,
        "temporary failure",
        resume_state=resume_state,
    )
    requirement = RequirementResetHarness(real_store)
    defect, publisher = RecordingFlow(), RecordingPublisher()
    orchestrator = DeveloperWorkflowOrchestrator(
        real_store,
        requirement,
        defect,
        publisher,
        _config(tmp_path),
        RecordingCandidates(),
    )

    revised = orchestrator.revise(run.run_id, "change implementation")

    assert revised.state is WorkflowState.IMPLEMENTING
    assert [event.target for event in revised.history[-3:]] == [
        resume_state,
        WorkflowState.BLOCKED,
        WorkflowState.IMPLEMENTING,
    ]
    assert requirement.calls[0].state is WorkflowState.BLOCKED
    assert requirement.implementation_calls == 1


@pytest.mark.parametrize(
    "resume_state",
    [
        WorkflowState.READING_ONES,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO,
    ],
)
def test_revise_early_blocked_checkpoint_fails_closed(
    tmp_path: Path, resume_state: WorkflowState
) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.BLOCKED,
        resume_state=resume_state,
        blocked_reason="early failure",
    )
    orchestrator, store, requirement, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="implementation revision checkpoint"):
        orchestrator.revise(run.run_id, "feedback")

    assert not requirement.calls
    assert not any(call[0] in {"save", "transition"} for call in store.calls)


@pytest.mark.parametrize(
    "feedback",
    [
        "",
        " leading",
        "trailing ",
        "line\nforged",
        "\u202esecret",
        "\u200fsecret",
        "\u200bsecret",
        "\u200dsecret",
        "line\u2028secret",
        "line\u2029secret",
        "x" * 4097,
        "\ud800secret",
    ],
)
def test_revise_validates_feedback_before_any_state_change_without_echo(
    tmp_path: Path, feedback: str
) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"), WorkflowState.WAITING_APPROVAL
    )
    orchestrator, store, _, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction) as caught:
        orchestrator.revise(run.run_id, feedback)

    assert "secret" not in str(caught.value)
    assert not store.calls


def test_revise_rejects_any_publication_checkpoint_before_transition(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.WAITING_APPROVAL,
        publication=PublicationResult(error="prior publication attempt"),
    )
    orchestrator, store, _, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="new workflow run"):
        orchestrator.revise(run.run_id, "change implementation")

    assert not any(call[0] in {"save", "transition"} for call in store.calls)


@pytest.mark.parametrize(
    "state",
    [WorkflowState.CREATED, WorkflowState.TESTING, WorkflowState.PUBLISHING],
)
def test_revise_rejects_all_other_states(tmp_path: Path, state: WorkflowState) -> None:
    run = _at_state(WorkflowRun.new("requirement", "REQ-1"), state)
    orchestrator, store, requirement, _, publisher = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="revise requires"):
        orchestrator.revise(run.run_id, "feedback")
    assert not requirement.calls and not publisher.publish_calls
    assert not any(call[0] in {"save", "transition"} for call in store.calls)


def test_approve_issues_utc_fingerprint_persists_before_publish(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.WAITING_APPROVAL,
        approval=_approval(),
    )
    orchestrator, store, _, _, publisher = _orchestrator(tmp_path, run)

    result = orchestrator.approve(run.run_id, "reviewer")

    assert result.approval is not None
    assert result.approval.fingerprint
    assert result.approval.approved_by == "reviewer"
    assert result.approval.approved_at is not None
    assert result.approval.approved_at.tzinfo is UTC
    assert publisher.publish_calls == [result]
    save_index = next(i for i, call in enumerate(store.calls) if call[0] == "save")
    assert save_index < len(store.calls)


@pytest.mark.parametrize(
    "state",
    [WorkflowState.CREATED, WorkflowState.AI_REVIEW, WorkflowState.PUBLISHING],
)
def test_approve_cannot_skip_waiting_approval(tmp_path: Path, state: WorkflowState) -> None:
    run = _at_state(WorkflowRun.new("requirement", "REQ-1"), state, approval=_approval())
    orchestrator, store, _, _, publisher = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction, match="approve requires WAITING_APPROVAL"):
        orchestrator.approve(run.run_id, "reviewer")
    assert not publisher.publish_calls
    assert not any(call[0] == "save" for call in store.calls)


def test_approve_cas_conflict_is_propagated_before_any_side_effect(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.WAITING_APPROVAL,
        approval=_approval(),
    )
    orchestrator, store, _, _, publisher = _orchestrator(tmp_path, run)
    store.save_error = ConcurrentRunUpdateError("workflow run version does not match")

    with pytest.raises(ConcurrentRunUpdateError):
        orchestrator.approve(run.run_id, "reviewer")
    assert not publisher.publish_calls and not publisher.retry_calls


@pytest.mark.parametrize(
    "approved_by",
    [
        "",
        " reviewer",
        "reviewer ",
        "reviewer\nforged",
        "\u2066secret",
        "\u200fsecret",
        "\u200dsecret",
        "reviewer\u2028secret",
        "x" * 129,
        "\ud800secret",
    ],
)
def test_approve_validates_identity_before_load_without_echo(
    tmp_path: Path, approved_by: str
) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.WAITING_APPROVAL,
        approval=_approval(),
    )
    orchestrator, store, _, _, publisher = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction) as caught:
        orchestrator.approve(run.run_id, approved_by)

    assert "secret" not in str(caught.value)
    assert not store.calls
    assert not publisher.publish_calls


def test_cancel_delegates_to_state_store_and_preserves_evidence(tmp_path: Path) -> None:
    run = _at_state(
        WorkflowRun.new("requirement", "REQ-1"),
        WorkflowState.TESTING,
        worktree_path=str((tmp_path / "worktree").resolve()),
        investigation_suggestions=("diagnostic",),
    )
    orchestrator, _, requirement, _, publisher = _orchestrator(tmp_path, run)

    result = orchestrator.cancel(run.run_id, "operator")

    assert result.state is WorkflowState.CANCELLED
    assert result.worktree_path == run.worktree_path
    assert result.investigation_suggestions == ("diagnostic",)
    assert not requirement.calls and not publisher.publish_calls


def test_cancel_terminal_state_is_rejected_by_real_state_store(tmp_path: Path) -> None:
    real_store = FileRunStore(tmp_path / "runs-real")
    run = real_store.create(WorkflowRun.new("requirement", "REQ-1"))
    requirement, defect, publisher = RecordingFlow(), RecordingFlow(), RecordingPublisher()
    orchestrator = DeveloperWorkflowOrchestrator(
        real_store,
        requirement,
        defect,
        publisher,
        _config(tmp_path),
        RecordingCandidates(),
    )

    cancelled = orchestrator.cancel(run.run_id, "operator")
    assert cancelled.state is WorkflowState.CANCELLED
    with pytest.raises(InvalidRunTransitionError, match="terminal"):
        orchestrator.cancel(run.run_id, "operator")


@pytest.mark.parametrize(
    "actor",
    [
        "",
        " operator",
        "operator ",
        "operator\nforged",
        "\u202esecret",
        "\u200fsecret",
        "\u200bsecret",
        "operator\u2029secret",
        "x" * 129,
        "\ud800secret",
    ],
)
def test_cancel_rejects_invalid_actor_without_echo(tmp_path: Path, actor: str) -> None:
    run = _at_state(WorkflowRun.new("requirement", "REQ-1"), WorkflowState.TESTING)
    orchestrator, store, _, _, _ = _orchestrator(tmp_path, run)

    with pytest.raises(InvalidWorkflowAction) as caught:
        orchestrator.cancel(run.run_id, actor)

    assert "secret" not in str(caught.value)
    assert not any(call[0] in {"load", "transition"} for call in store.calls)


def test_command_metadata_accepts_normal_unicode_and_emoji(tmp_path: Path) -> None:
    run = _at_state(WorkflowRun.new("requirement", "REQ-1"), WorkflowState.TESTING)
    orchestrator, _, _, _, _ = _orchestrator(tmp_path, run)

    cancelled = orchestrator.cancel(run.run_id, "审核员😀")

    assert cancelled.state is WorkflowState.CANCELLED


def test_two_store_concurrent_resume_has_one_checkpoint_side_effect(tmp_path: Path) -> None:
    run_root = tmp_path / "concurrent-runs"
    first_store, second_store = FileRunStore(run_root), FileRunStore(run_root)
    created = first_store.create(WorkflowRun.new("requirement", "REQ-1"))
    counter = [0]
    counter_lock = threading.Lock()

    class CheckpointFlow:
        def __init__(self, store: FileRunStore) -> None:
            self.store = store

        def execute(self, run: WorkflowRun) -> WorkflowRun:
            if run.state is not WorkflowState.CREATED:
                return run
            with counter_lock:
                counter[0] += 1
            time.sleep(0.05)
            return self.store.transition(
                run.run_id,
                run.version,
                WorkflowState.READING_ONES,
                "checkpointed external read",
            )

    orchestrators = [
        DeveloperWorkflowOrchestrator(
            store,
            CheckpointFlow(store),
            RecordingFlow(),
            RecordingPublisher(),
            _config(tmp_path),
            RecordingCandidates(),
        )
        for store in (first_store, second_store)
    ]
    errors: list[BaseException] = []

    def worker(orchestrator: DeveloperWorkflowOrchestrator) -> None:
        try:
            orchestrator.resume(created.run_id)
        except BaseException as error:  # pragma: no cover - assertion reports thread error
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(item,)) for item in orchestrators]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert counter == [1]
    assert first_store.load(created.run_id).state is WorkflowState.READING_ONES


def test_clock_helper_is_timezone_aware_utc() -> None:
    assert utc_now().tzinfo is UTC
