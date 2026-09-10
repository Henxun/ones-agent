"""Frozen, display-only view models for the developer workflow TUI."""

from __future__ import annotations

from ..verification_models import VerificationTask
from ..verification import VERIFICATION_REASONS, public_text

import hmac
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from rich.markup import escape as escape_markup

from ..approval import approval_fingerprint, validate_for_approval
from ..command_utils import CommandArgvError, parse_command_argv
from ..contracts import (
    ApprovalPackage,
    CommandResult,
    DefectAction,
    DefectCandidate,
    DefectRecord,
    PublicationResult,
    RepositoryMapping,
    RepositoryPublicationResult,
    RequirementRecord,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)
from ..pr_provider import PullRequestProviderError, parse_repository_identity
from ..schedules import (
    ScheduleRun,
    ScheduleRunItem,
    ScheduleRunItemStatus,
    ScheduleRunStatus,
    ScheduleRunTrigger,
)
from .defect_text import defect_display_text


_INVALID_DISPLAY = "display value is invalid"
_INVALID_PR_URL = "PR URL is invalid"
_PUBLICATION_FAILED = "publication failed safely"
_BLOCKED = "workflow blocked safely"
_INVALID_FACTS = "workflow display facts are invalid"
_ACTIONS = {"approve", "revise", "cancel", "resume-publication"}


class TuiDisplayError(ValueError):
    """A fixed-message failure at the untrusted display boundary."""


class RunActivity(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class FilterChoice:
    """One trusted ONES identifier paired with a display-only name."""

    id: str
    name: str
    selected: bool = False


@dataclass(frozen=True, slots=True)
class DefectFilterOptions:
    iterations: tuple[FilterChoice, ...]
    assignees: tuple[FilterChoice, ...]
    statuses: tuple[FilterChoice, ...]
    unavailable: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RequirementFilterOptions:
    issue_types: tuple[FilterChoice, ...]
    assignees: tuple[FilterChoice, ...] = ()
    statuses: tuple[FilterChoice, ...] = ()
    unavailable: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceRepositoryInput:
    """One local or remote repository selected while creating a workspace."""

    key: str
    name: str
    source: str
    local: bool
    branch: str = "main"


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    """Credential-free workspace projection for the top-level TUI."""

    key: str
    project_id: str
    iteration_id: str
    repositories: tuple[str, ...]
    display_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.key


@dataclass(frozen=True, slots=True)
class ScheduleRunView:
    """Bounded, credential-free facts for one scheduled execution."""

    run_id: str
    schedule_id: str
    schedule_version: int
    schedule_name: str
    action: DefectAction
    trigger: ScheduleRunTrigger
    status: ScheduleRunStatus
    started_at: float
    heartbeat_at: float
    finished_at: float | None
    discovered_count: int
    skipped_count: int
    started_count: int
    failed_count: int
    error_stage: str
    interruption_reason: str

    @classmethod
    def from_run(cls, run: ScheduleRun) -> ScheduleRunView:
        return cls(
            run_id=safe_tui_text(run.id, maximum=32),
            schedule_id=safe_tui_text(run.schedule_id, maximum=32),
            schedule_version=run.schedule_version,
            schedule_name=safe_tui_text(run.plan_snapshot.name, maximum=80),
            action=run.plan_snapshot.action,
            trigger=run.trigger,
            status=run.status,
            started_at=run.started_at,
            heartbeat_at=run.heartbeat_at,
            finished_at=run.finished_at,
            discovered_count=run.discovered_count,
            skipped_count=run.skipped_count,
            started_count=run.started_count,
            failed_count=run.failed_count,
            error_stage=safe_tui_text(
                run.error_stage, maximum=80, allow_empty=True
            ),
            interruption_reason=safe_tui_text(
                run.error_message, maximum=2048, allow_empty=True
            ),
        )


@dataclass(frozen=True, slots=True)
class ScheduleRunItemView:
    """Display-safe outcome for one candidate in a scheduled execution."""

    ordinal: int
    defect_id: str
    defect_name: str
    action: DefectAction
    status: ScheduleRunItemStatus
    workflow_run_id: str
    reason: str
    updated_at: float

    @classmethod
    def from_item(cls, item: ScheduleRunItem) -> ScheduleRunItemView:
        return cls(
            ordinal=item.ordinal,
            defect_id=safe_tui_text(item.defect_id, maximum=256),
            defect_name=safe_tui_text(
                item.defect_name, maximum=512, allow_empty=True
            ),
            action=item.action,
            status=item.status,
            workflow_run_id=safe_tui_text(
                item.workflow_run_id, maximum=128, allow_empty=True
            ),
            reason=safe_tui_text(item.reason, maximum=2048, allow_empty=True),
            updated_at=item.updated_at,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceProjectOptions:
    projects: tuple[FilterChoice, ...]


def safe_tui_text(
    value: str,
    *,
    maximum: int = 4096,
    allow_empty: bool = False,
) -> str:
    """Return strict, single-line text escaped for Rich/Textual display."""

    return escape_markup(
        _strict_tui_text(value, maximum=maximum, allow_empty=allow_empty)
    )


def validate_tui_input_text(
    value: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    """Validate plain input text without applying render-layer escaping."""

    return _strict_tui_text(
        value,
        maximum=maximum,
        allow_empty=allow_empty,
    )


def _strict_tui_text(
    value: str,
    *,
    maximum: int = 4096,
    allow_empty: bool = False,
) -> str:
    """Validate untrusted text without changing its semantic value."""

    if type(value) is not str or maximum < 1:
        raise TuiDisplayError(_INVALID_DISPLAY)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise TuiDisplayError(_INVALID_DISPLAY) from None
    if (
        (not value and not allow_empty)
        or len(value) > maximum
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        )
    ):
        raise TuiDisplayError(_INVALID_DISPLAY)
    return value


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    workflow_type: WorkflowType
    work_item_id: str
    state: WorkflowState
    version: int
    updated_at: datetime
    activity: RunActivity
    corrupted: bool = False

    @classmethod
    def from_run(cls, run: WorkflowRun, *, activity: RunActivity) -> RunSummary:
        return cls(
            run_id=safe_tui_text(run.run_id, maximum=64),
            workflow_type=run.type,
            work_item_id=safe_tui_text(run.work_item_id, maximum=256),
            state=run.state,
            version=run.version,
            updated_at=run.updated_at,
            activity=activity,
        )

    @classmethod
    def corrupted_entry(cls, run_id: str) -> RunSummary:
        return cls(
            run_id=safe_tui_text(run_id, maximum=64),
            workflow_type=WorkflowType.REQUIREMENT,
            work_item_id="storage-corrupted",
            state=WorkflowState.BLOCKED,
            version=0,
            updated_at=datetime(1970, 1, 1, tzinfo=UTC),
            activity=RunActivity.IDLE,
            corrupted=True,
        )


@dataclass(frozen=True, slots=True)
class RepositoryView:
    key: str
    role: str
    base_commit: str
    head_commit: str
    tree_hash: str
    changed_files: tuple[str, ...]
    changed_file_count: int
    commit_hash: str
    pushed: bool
    pr_url: str
    error: str
    test_summary: str = "0 verified test facts"
    pr_target: str = ""


@dataclass(frozen=True, slots=True)
class TestView:
    command: str
    outcome: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class PublicationView:
    repositories: tuple[RepositoryView, ...]
    comment_id: str
    error: str


@dataclass(frozen=True, slots=True)
class HistoryView:
    source: str
    target: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewView:
    """Keep actionable findings separate from release/environment limitations."""

    summary: str
    findings: tuple[str, ...]
    blockers: tuple[str, ...]
    external_validation: tuple[str, ...]
    verification_only: bool
    review_repair_attempts: int = 0
    pause_reason: str = ""


@dataclass(frozen=True, slots=True)
class DefectInfoView:
    """Allowlisted fields from the stored ONES snapshot, not a live fetch."""

    defect_id: str
    number: str
    title: str
    status: str
    priority: str
    project: str
    assignee: str
    description: str
    updated_at: str

    @classmethod
    def from_record(cls, defect: DefectRecord) -> DefectInfoView:
        clean = defect_display_text
        return cls(
            defect_id=clean(defect.defect_id), number=clean(defect.number),
            title=clean(defect.title, maximum=1024), status=clean(defect.status.name or defect.status.id),
            priority=clean(defect.priority.value or defect.priority.id),
            project=clean(defect.project.name or defect.project.id),
            assignee=clean((defect.assignee.name or defect.assignee.id) if defect.assignee else ""),
            description=clean(defect.description, multiline=True, maximum=8000),
            updated_at=clean(defect.updated_at),
        )


@dataclass(frozen=True, slots=True)
class RunDetail:
    summary: RunSummary
    repositories: tuple[RepositoryView, ...]
    tests: tuple[TestView, ...]
    review: tuple[str, ...]
    publication: PublicationView
    history: tuple[HistoryView, ...]
    blocked_reason: str
    fingerprint: str
    risk_count: int
    unresolved_count: int
    status_message: str = ""
    mapping_candidates: tuple[MappingCandidateView, ...] = ()
    resume_state: WorkflowState | None = None
    ai_activity: tuple[str, ...] = ()
    can_accept_analysis: bool = False
    can_regenerate_analysis: bool = False
    review_report: ReviewView | None = None
    can_request_review_repair: bool = False
    verification_tasks: tuple[VerificationTask, ...] = ()
    can_verify: bool = False
    verification_results: tuple[str, ...] = ()
    defect_info: DefectInfoView | None = None
    draft_pr: bool = False
    can_defer_verification: bool = False
    baseline_refresh_history: tuple[str, ...] = ()
    coding_agent_label: str = ""
    coding_agent_key: str = ""
    coding_agent_version: str = ""
    merge_readiness_status: str = ""
    merge_readiness_actor: str = ""
    merge_readiness_evidence: str = ""
    merge_readiness_history: tuple[str, ...] = ()

    @classmethod
    def from_run(cls, run: WorkflowRun) -> RunDetail:
        return run_detail_from_run(run)


@dataclass(frozen=True, slots=True)
class DefectChoice:
    candidate_id: str
    title: str
    status_id: str
    priority: str

    @classmethod
    def from_candidate(cls, candidate: DefectCandidate) -> DefectChoice:
        return cls(
            candidate_id=safe_tui_text(candidate.uuid, maximum=128),
            title=safe_tui_text(candidate.title, maximum=512),
            status_id=safe_tui_text(candidate.status_id, maximum=128, allow_empty=True),
            priority=safe_tui_text(candidate.priority, maximum=128),
        )


@dataclass(frozen=True, slots=True)
class RequirementChoice:
    requirement_id: str
    number: str
    title: str
    project_id: str
    iteration_id: str
    status_id: str
    issue_type_id: str

    @classmethod
    def from_requirement(cls, requirement: RequirementRecord) -> RequirementChoice:
        return cls(
            requirement_id=safe_tui_text(requirement.requirement_id, maximum=128),
            number=safe_tui_text(requirement.number, maximum=128, allow_empty=True),
            title=safe_tui_text(requirement.title, maximum=512),
            project_id=safe_tui_text(requirement.project.id, maximum=128),
            iteration_id=safe_tui_text(requirement.iteration.id, maximum=128, allow_empty=True),
            status_id=safe_tui_text(requirement.status.id, maximum=128),
            issue_type_id=safe_tui_text(
                requirement.issue_type.id, maximum=128, allow_empty=True
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryCandidateView:
    """Credential-free facts for one authorized repository candidate."""

    key: str
    role: str
    source: str
    depends_on: tuple[str, ...]
    lint_summary: str
    build_summary: str
    test_summary: str
    allowed_paths: tuple[str, ...]
    side_effects: str


@dataclass(frozen=True, slots=True)
class MappingCandidateView:
    """Frozen single-repository or repository-group selection facts."""

    key: str
    kind: Literal["repository", "repository-group"]
    primary_repository: str
    repositories: tuple[RepositoryCandidateView, ...]
    integration_test_summary: str


@dataclass(frozen=True, slots=True)
class RunFilter:
    states: tuple[WorkflowState, ...] = ()
    workflow_types: tuple[WorkflowType, ...] = ()
    query: str = ""
    updated_after: datetime | None = None
    updated_before: datetime | None = None

    def __post_init__(self) -> None:
        for value in (self.updated_after, self.updated_before):
            if value is not None and (
                type(value) is not datetime
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError("run filter timestamps must be timezone-aware")
        if (
            self.updated_after is not None
            and self.updated_before is not None
            and self.updated_after > self.updated_before
        ):
            raise ValueError("run filter timestamp range is invalid")

    def matches(self, item: RunSummary) -> bool:
        return self.matches_facts(
            state=item.state,
            workflow_type=item.workflow_type,
            run_id=item.run_id,
            work_item_id=item.work_item_id,
            updated_at=item.updated_at,
        )

    def matches_facts(
        self,
        *,
        state: WorkflowState,
        workflow_type: WorkflowType,
        run_id: str,
        work_item_id: str,
        updated_at: datetime,
    ) -> bool:
        """Match canonical identity facts before display escaping."""

        query = validate_tui_input_text(
            self.query, maximum=256, allow_empty=True
        ).casefold().strip()
        return (
            (not self.states or state in self.states)
            and (
                not self.workflow_types
                or workflow_type in self.workflow_types
            )
            and (
                not query
                or query in work_item_id.casefold()
                or query in run_id.casefold()
            )
            and (
                self.updated_after is None
                or updated_at >= self.updated_after
            )
            and (
                self.updated_before is None
                or updated_at <= self.updated_before
            )
        )


@dataclass(frozen=True, slots=True)
class DangerousActionRequest:
    run_id: str
    version: int
    action: Literal["approve", "revise", "cancel", "resume-publication"]
    fingerprint: str
    work_item_id: str
    repositories: tuple[RepositoryView, ...]
    changed_file_count: int
    test_count: int
    risk_count: int
    unresolved_count: int
    comment_status: str = "not delivered"
    publication_error: str = ""
    draft_pr: bool = False
    deferred_check_count: int = 0
    baseline_evidence_missing: bool = False
    approvers: tuple[FilterChoice, ...] = ()
    approver_error: str = ""
    state: WorkflowState = WorkflowState.CREATED
    resume_state: WorkflowState | None = None

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise TuiDisplayError("workflow action is invalid")

    @classmethod
    def from_run(
        cls,
        run: WorkflowRun,
        *,
        action: str,
        expected_version: int | None = None,
    ) -> DangerousActionRequest:
        if action not in _ACTIONS:
            raise TuiDisplayError("workflow action is invalid")
        if expected_version is not None and expected_version != run.version:
            raise TuiDisplayError("workflow action is stale")
        _, fingerprint_bound, signed = _approval_status(
            run.approval,
            bind_unsigned=(
                action == "approve"
                and run.state is WorkflowState.WAITING_APPROVAL
            ),
        )
        if action == "approve" and not fingerprint_bound:
            raise TuiDisplayError("workflow action is unavailable")
        if action == "resume-publication" and not signed:
            raise TuiDisplayError("workflow action is unavailable")
        detail = run_detail_from_run(run)
        repositories, test_count = _dangerous_test_facts(
            run,
            detail,
            fingerprint_bound=fingerprint_bound,
        )
        return cls(
            run_id=detail.summary.run_id,
            version=detail.summary.version,
            action=action,
            fingerprint=detail.fingerprint,
            work_item_id=detail.summary.work_item_id,
            repositories=repositories,
            changed_file_count=sum(
                item.changed_file_count for item in detail.repositories
            ),
            test_count=test_count,
            risk_count=detail.risk_count,
            unresolved_count=detail.unresolved_count,
            comment_status=(
                "delivered" if detail.publication.comment_id else "not delivered"
            ),
            publication_error=detail.publication.error,
            draft_pr=bool(run.approval and run.approval.draft_pr),
            deferred_check_count=len(run.approval.deferred_verification) if run.approval else 0,
            baseline_evidence_missing=bool(run.approval and run.approval.baseline_evidence_missing),
            state=detail.summary.state,
            resume_state=detail.resume_state,
        )

    def assert_current(self, run: WorkflowRun) -> None:
        current = type(self).from_run(
            run, action=self.action, expected_version=self.version
        )
        # Member choices are live ONES directory data, not persisted run facts.
        # The controller revalidates the chosen ID separately before approval.
        if current != replace(self, approvers=(), approver_error=""):
            raise TuiDisplayError("workflow action is stale")


def _dangerous_test_facts(
    run: WorkflowRun,
    detail: RunDetail,
    *,
    fingerprint_bound: bool,
) -> tuple[tuple[RepositoryView, ...], int]:
    approval = run.approval
    if not fingerprint_bound or approval is None:
        return detail.repositories, len(detail.tests)
    if run.repository_group is None:
        count = len(approval.tests)
        return (
            tuple(
                replace(repository, test_summary=_test_fact_summary(count))
                for repository in detail.repositories
            ),
            count,
        )
    approved_counts = {
        item.repository_key: len(item.tests) for item in approval.repositories
    }
    repositories = tuple(
        replace(
            repository,
            test_summary=_test_fact_summary(approved_counts[repository.key]),
        )
        for repository in detail.repositories
    )
    return repositories, sum(approved_counts.values()) + len(
        approval.integration_tests
    )


def run_detail_from_run(run: WorkflowRun) -> RunDetail:
    approval = run.approval
    fingerprint, fingerprint_bound, signed = _approval_status(
        approval,
        bind_unsigned=run.state is WorkflowState.WAITING_APPROVAL,
    )
    if fingerprint_bound and approval is not None:
        if run.repository_group is not None:
            _validate_bound_group_facts(run, approval, include_publication=signed)
        else:
            _validate_bound_single_facts(run, approval, include_publication=signed)
    repositories: tuple[RepositoryView, ...]
    tests: tuple[TestView, ...]
    if run.repository_group is not None:
        repositories, tests = _group_views(
            run, fingerprint_bound=fingerprint_bound, signed=signed
        )
    else:
        repositories, tests = _single_repository_views(run, signed=signed)

    single_publication_bound = bool(
        signed and run.publication.approved_fingerprint == fingerprint
    )
    publication_error = bool(
        single_publication_bound and run.publication.error
    )
    comment = (
        "delivered"
        if single_publication_bound and run.publication.comment_id
        else ""
    )
    group_publication_bound = bool(
        signed
        and run.group_publication is not None
        and all(
            item.approved_fingerprint == fingerprint
            for item in run.group_publication.repositories
        )
    )
    if group_publication_bound and run.group_publication is not None:
        publication_error = bool(run.group_publication.error)
        comment = "delivered" if run.group_publication.comment_id else ""

    review_count = len(approval.review) if approval is not None else 0
    analysis_review: tuple[str, ...] = ()
    if run.defect_action is DefectAction.ANALYZE and run.coding_agent_results:
        result = run.coding_agent_results[0]
        defect = run.defect
        defect_label = run.work_item_id
        if defect is not None:
            number = f"#{defect.number} " if defect.number else ""
            defect_label = f"{number}{defect.title}".strip() or run.work_item_id
        lines = [
            "AI ANALYSIS REPORT",
            "Analysis mode: read-only; no repository files were modified",
            "Defect: " + safe_tui_text(defect_label, maximum=1024),
            "",
            "CONCLUSION",
            safe_tui_text(result.summary or "No conclusion returned", maximum=8192),
        ]
        if result.behavior_before:
            lines.extend((
                "",
                "OBSERVED BEHAVIOR",
                safe_tui_text(result.behavior_before, maximum=4096),
            ))
        if run.root_cause_evidence:
            lines.extend(("", "ROOT CAUSE"))
            for index, evidence in enumerate(run.root_cause_evidence, 1):
                location = evidence.file_path
                if evidence.start_line is not None:
                    location += f":{evidence.start_line}"
                lines.append(
                    f"{index}. "
                    + safe_tui_text(location, maximum=512)
                    + " — "
                    + safe_tui_text(evidence.mechanism, maximum=4096)
                )
        else:
            lines.extend((
                "",
                "ROOT CAUSE",
                "No repository-verified root cause is available.",
            ))
        solution_steps = tuple(dict.fromkeys(
            step
            for evidence in run.root_cause_evidence
            for step in evidence.fix_steps
        ))
        lines.extend(("", "BEST SOLUTION"))
        if solution_steps:
            lines.extend(
                f"{index}. " + safe_tui_text(step, maximum=4096)
                for index, step in enumerate(solution_steps, 1)
            )
        elif result.investigation_suggestions:
            lines.extend(
                f"{index}. " + safe_tui_text(step, maximum=4096)
                for index, step in enumerate(
                    result.investigation_suggestions, 1
                )
            )
        else:
            lines.append(
                "No evidence-backed solution can be recommended until the root cause is verified."
            )
        if result.impact_scope:
            lines.extend(("", "IMPACT SCOPE"))
            lines.extend(
                "- " + safe_tui_text(path, maximum=512)
                for path in result.impact_scope
            )
        lines.extend((
            "",
            "RISK",
            safe_tui_text(result.risk_level or "not assessed", maximum=128),
        ))
        if result.risks:
            lines.extend(
                "- " + safe_tui_text(item, maximum=2048)
                for item in result.risks
            )
        if result.unresolved_items:
            lines.extend(("", "UNRESOLVED / UNKNOWN"))
            lines.extend(
                "- " + safe_tui_text(item, maximum=2048)
                for item in result.unresolved_items
            )
        if result.investigation_suggestions:
            lines.extend(("", "RECOMMENDED NEXT STEPS"))
            lines.extend(
                "- " + safe_tui_text(item, maximum=2048)
                for item in result.investigation_suggestions
            )
        analysis_review = tuple(lines)
    if run.verification_only and run.review is not None:
        analysis_review = (
            "CURRENT CHECKOUT VERIFICATION — NO PRODUCTION REPAIR",
            safe_tui_text(run.review.summary, maximum=8192),
            *(safe_tui_text(item, maximum=2048) for item in run.review.review_findings),
            "LIMITATIONS / EXTERNAL VALIDATION",
            *(safe_tui_text(item, maximum=2048) for item in run.review.review_external_validation),
            *(safe_tui_text(item, maximum=2048) for item in run.review.unresolved_items),
        )
    return RunDetail(
        summary=RunSummary.from_run(run, activity=RunActivity.IDLE),
        coding_agent_label=(
            safe_tui_text(run.coding_agent.label, maximum=80)
            if run.coding_agent is not None
            else ""
        ),
        coding_agent_key=(
            safe_tui_text(run.coding_agent.key, maximum=64)
            if run.coding_agent is not None
            else ""
        ),
        coding_agent_version=(
            safe_tui_text(
                run.coding_agent.version,
                maximum=128,
                allow_empty=True,
            )
            if run.coding_agent is not None
            else ""
        ),
        merge_readiness_status=(
            run.merge_readiness.status if run.merge_readiness is not None else ""
        ),
        merge_readiness_actor=(
            safe_tui_text(run.merge_readiness.actor, maximum=128)
            if run.merge_readiness is not None
            else ""
        ),
        merge_readiness_evidence=(
            safe_tui_text(run.merge_readiness.evidence, maximum=4096)
            if run.merge_readiness is not None
            else ""
        ),
        merge_readiness_history=tuple(
            safe_tui_text(
                f"{record.status} · {record.actor} · "
                f"{record.occurred_at.isoformat()} · {record.evidence}",
                maximum=4600,
            )
            for record in run.merge_readiness_history
        ),
        baseline_refresh_history=tuple(safe_tui_text(public_text(
            f"第 {index + 1} 轮 · " + {"preparing": "准备迁移", "migrated": "迁移完成，重新验证", "conflicts": "冲突交回修复", "failed": "迁移暂停"}[record.status]
            + f" · {record.failure_reason}\n" + "\n".join(
                f"{item.repository_key}：旧基线 {item.prepared_worktree.base_commit[:12]} · 保留于 {item.prepared_worktree.path}"
                for item in record.source_repositories) + "\n" + "\n".join(
                f"{item.repository_key}：新基线 {item.prepared_worktree.base_commit[:12]} · 工作区 {item.prepared_worktree.path}"
                for item in record.destinations), 8192).replace("\n", " · "), maximum=16000)
            for index, record in enumerate(run.baseline_refreshes)),
        defect_info=DefectInfoView.from_record(run.defect) if run.type is WorkflowType.DEFECT and run.defect is not None else None,
        review_report=(
            ReviewView(
                summary=safe_tui_text(run.review.summary, maximum=8192),
                findings=tuple(safe_tui_text(item, maximum=8192) for item in run.review.review_findings),
                blockers=tuple(safe_tui_text(item, maximum=8192) for item in run.review.unresolved_items),
                external_validation=tuple(safe_tui_text(public_text(item, 8192).replace("\n", " "), maximum=8192)
                    for item in dict.fromkeys((*run.review.review_external_validation,
                                              *(need.description for need in run.review.verification_needs),
                                              *(task.need.description for task in run.verification_plan)))),
                verification_only=run.verification_only,
                review_repair_attempts=run.review_repair_attempts,
                pause_reason=({
                    "automatic review repair limit reached": "已达到本轮自动回修上限",
                    "automatic review repair made no progress": "代码与测试未变化，复审仍有待处理问题",
                }.get(run.blocked_reason, "") if run.state is WorkflowState.BLOCKED else ""),
            ) if run.review is not None else None
        ),
        repositories=repositories,
        tests=tests,
        review=analysis_review
        or tuple("review recorded" for _ in range(review_count)),
        publication=PublicationView(
            repositories=repositories,
            comment_id=comment,
            error=_PUBLICATION_FAILED if publication_error else "",
        ),
        history=tuple(
            HistoryView(
                source=event.source.value,
                target=event.target.value,
                occurred_at=event.occurred_at,
            )
            for event in run.history
        ),
        blocked_reason=_BLOCKED if run.blocked_reason or run.error else "",
        fingerprint=fingerprint,
        risk_count=(
            len(approval.risks)
            if approval is not None
            else len(run.coding_agent_results[0].risks)
            if run.defect_action is DefectAction.ANALYZE and run.coding_agent_results
            else 0
        ),
        unresolved_count=(
            len(approval.unresolved_items)
            if approval is not None
            else (
                len(run.review.unresolved_items)
                + len(run.review.review_external_validation)
            )
            if run.review is not None
            else len(run.coding_agent_results[0].unresolved_items)
            if run.defect_action is DefectAction.ANALYZE and run.coding_agent_results
            else 0
        ),
        status_message={
            "automatic review repair made no progress": (
                "Automatic repair paused: code/test content is unchanged and review still has unresolved issues. "
                "See Review for the remaining issues; explicit direction is needed."
            ),
            "automatic review repair limit reached": (
                "Automatic review/repair attempt limit reached. See Review for remaining issues and provide direction."
            ),
            "current checkout verification needs investigation": (
                "The baseline already passes, but Review found correctness or coverage issues. "
                "Inspect the Review tab; no artificial repair was applied."
            ),
            "AI review found blocking issues": (
                "Review found unresolved issues; see the Review tab. Publication is blocked."
            ),
            "AI review requires external validation": (
                "The code review found no further repair to apply, but external or platform "
                "validation is still missing. See the Review tab; publication remains blocked."
            ),
            "AI review evidence is incomplete": (
                "Review found unresolved issues; see the Review tab. Publication is blocked."
                if run.review is not None and run.review.unresolved_items else
                "Review evidence is incomplete; see the Review tab before continuing."
            ),
            "pre-fix reproduction evidence is missing": (
                "缺少修复前失败复现记录：不等于当前测试失败。可重新检查审批条件，"
                "在 Draft PR 中披露证据缺口并交由人工审核；严格模式下仍需补齐历史复现证据。"
            ),
            "repair modified the reproduction test": (
                "Repair changed the frozen reproduction test. Restore its original bytes; "
                "put additional regression tests in a separate file, then continue repair."
            ),
            "reproduction checkpoint is incomplete": (
                "Reproduction checkpoint is missing or its frozen test has changed; "
                "repair cannot continue until the original evidence is verified."
            ),
            "repair evidence is incomplete": (
                "Code changes were not accepted as a completed repair; "
                "repair scope, reported evidence, or the frozen reproduction test did not match."
            ),
            "defect source evidence is insufficient": (
                "ONES defect description is missing or lacks reproducible details"
            ),
            "Codex process could not be started": "coding agent process could not be started",
            "coding agent process could not be started": "coding agent process could not be started",
            "Codex analysis timed out": "coding agent analysis timed out",
            "coding agent analysis timed out": "coding agent analysis timed out",
            "Codex analysis returned invalid structured output": (
                "coding agent analysis returned invalid structured output"
            ),
            "coding agent analysis returned invalid structured output": (
                "coding agent analysis returned invalid structured output"
            ),
            "Codex result format repair failed": (
                "coding agent result format repair failed"
            ),
            "coding agent result format repair failed": (
                "coding agent result format repair failed"
            ),
            "Codex runtime safety validation failed": (
                "coding agent runtime safety validation failed"
            ),
            "coding agent runtime safety validation failed": (
                "coding agent runtime safety validation failed"
            ),
            "Codex analysis exited unsuccessfully": (
                "coding agent analysis exited unsuccessfully"
            ),
            "coding agent analysis exited unsuccessfully": (
                "coding agent analysis exited unsuccessfully"
            ),
            "repository safety validation failed": (
                "Repository safety validation failed"
            ),
            "remote target branch changed since baseline": (
                "远程目标分支已变化：当前任务保存的基线已过期。这不是人工验证门禁；"
                "继续后将保留旧工作区，自动迁移修复到新基线并重新测试、审查；自动恢复关闭时需人工处理。"
            ),
            "automatic baseline refresh limit reached": "自动基线更新已达上限：已保留各轮工作区，请检查目标分支是否持续变化，再决定如何继续。",
            "baseline migration needs attention": "基线迁移未完成：旧修复与已创建的新工作区均已保留，请检查迁移记录；不会覆盖旧修复或沿用旧审批。",
            "baseline conflict resolution is incomplete": "基线冲突尚未解决：新工作区仍存在冲突标记，需要继续修复，不能直接审批。",
            "repository command failed": "Git 命令执行失败：请检查仓库连接、认证及本地 Git 环境。这不是人工验证门禁。",
            "defect workflow safety validation failed": "流程内部检查失败：尚未确定具体原因，不应归因于待人工验证。请重新检查审批条件。",
            "waiting for verification environment": "等待验证环境：配置匹配节点，或完成实机验证后记录人工证据。",
            "verification execution requires confirmation": "已匹配验证节点，等待确认本次执行。",
            "external verification did not pass": "环境验证未通过：查看验证记录，排查节点环境或处理测试失败。",
            "external verification is running": "正在执行环境验证。",
            "root cause evidence is insufficient": (
                "AI root-cause evidence did not meet repository safety checks"
            ),
        }.get(run.blocked_reason, (
            (
                "Review-driven corrections verified locally; no publication. See Review for limitations."
                if run.review_repair_attempts else
                "Current checkout verified; no production repair or publication. See Review for limitations."
            )
            if run.verification_only and run.state is WorkflowState.COMPLETED else (
                "Draft PR 已交付，等待 PR 人工验证；未标记通过，不自动合并或发布。"
                if run.state is WorkflowState.WAITING_PR_VERIFICATION else ""
            )
        )),
        mapping_candidates=_mapping_candidate_views(run),
        can_request_review_repair=(
            run.type is WorkflowType.DEFECT
            and run.state is WorkflowState.BLOCKED
            and run.resume_state is WorkflowState.AI_REVIEW
            and run.blocked_reason in {
                "automatic review repair limit reached", "automatic review repair made no progress",
            }
            and run.review is not None and bool(run.review.unresolved_items)
        ),
        verification_tasks=run.verification_plan,
        draft_pr=bool(approval and approval.draft_pr),
        verification_results=tuple(public_text(
            f"{r.status} · {r.node_key} · {r.occurred_at}\n快照：{r.snapshot_digest}\n输出 SHA-256：{r.output_sha256}\n{r.evidence}", 18000
        ) for r in run.verification_records[-8:]),
        can_verify=(run.state is WorkflowState.BLOCKED
                    and run.resume_state is WorkflowState.AI_REVIEW
                    and run.blocked_reason in VERIFICATION_REASONS
                    and run.review is not None and not run.review.unresolved_items),
        resume_state=(
            WorkflowState.IMPLEMENTING
            if run.blocked_reason in {
                "AI review found blocking issues", "AI review evidence is incomplete",
                "current checkout verification needs investigation",
            }
            and run.review is not None
            and run.review.unresolved_items
            else run.resume_state
        ),
        can_accept_analysis=(
            run.state is WorkflowState.COMPLETED
            and run.defect_action is DefectAction.ANALYZE
            and bool(run.root_cause_evidence)
            and not run.coding_agent_results[0].unresolved_items
            if run.coding_agent_results
            else False
        ),
        can_regenerate_analysis=(
            run.state is WorkflowState.COMPLETED
            and run.defect_action is DefectAction.ANALYZE
            and bool(run.coding_agent_results)
        ),
    )


def _mapping_candidate_views(run: WorkflowRun) -> tuple[MappingCandidateView, ...]:
    if run.state is not WorkflowState.VALIDATING:
        return ()
    keys = tuple(item.key for item in run.repository_candidates) + tuple(
        item.key for item in run.repository_group_candidates
    )
    if len(keys) != len(set(keys)):
        raise TuiDisplayError(_INVALID_FACTS)
    result = [
        MappingCandidateView(
            key=safe_tui_text(mapping.key, maximum=128),
            kind="repository",
            primary_repository=safe_tui_text(mapping.key, maximum=128),
            repositories=(_repository_candidate_view(mapping),),
            integration_test_summary="0 configured integration test commands",
        )
        for mapping in run.repository_candidates
    ]
    for group in run.repository_group_candidates:
        try:
            by_key = {item.key: item for item in group.repositories}
            ordered = tuple(by_key[key] for key in group.topological_keys())
        except Exception:
            raise TuiDisplayError(_INVALID_FACTS) from None
        result.append(
            MappingCandidateView(
                key=safe_tui_text(group.key, maximum=128),
                kind="repository-group",
                primary_repository=safe_tui_text(
                    group.primary_repository, maximum=128
                ),
                repositories=tuple(
                    _repository_candidate_view(mapping) for mapping in ordered
                ),
                integration_test_summary=_command_summary(
                    group.integration_test_commands, "integration test"
                ),
            )
        )
    return tuple(result)


def _repository_candidate_view(mapping: RepositoryMapping) -> RepositoryCandidateView:
    return RepositoryCandidateView(
        key=safe_tui_text(mapping.key, maximum=128),
        role=mapping.role.value,
        source=(
            "local read-only source"
            if mapping.source_path is not None
            else "remote mirror"
        ),
        depends_on=tuple(
            safe_tui_text(key, maximum=128) for key in mapping.depends_on
        ),
        lint_summary=_command_summary(mapping.lint_commands, "lint"),
        build_summary=_command_summary(mapping.build_commands, "build"),
        test_summary=_command_summary(mapping.test_commands, "test"),
        allowed_paths=tuple(
            _safe_repository_path(path) for path in mapping.allowed_paths
        ),
        side_effects="changes use an isolated managed worktree",
    )


def _command_summary(commands: tuple[str, ...], label: str) -> str:
    try:
        for command in commands:
            parse_command_argv(command)
    except CommandArgvError:
        raise TuiDisplayError(_INVALID_FACTS) from None
    count = len(commands)
    suffix = "command" if count == 1 else "commands"
    return f"{count} configured {label} {suffix}"


def _single_repository_views(
    run: WorkflowRun, *, signed: bool
) -> tuple[tuple[RepositoryView, ...], tuple[TestView, ...]]:
    mapping = run.repository
    prepared = run.prepared_worktree
    if mapping is None or prepared is None:
        return (), tuple(_test_view(item) for item in run.test_results)

    approval = run.approval
    publication = run.publication
    publication_bound = bool(
        signed
        and approval is not None
        and publication.approved_fingerprint == approval.fingerprint
    )
    if run.tested_snapshot is not None:
        final_paths = run.tested_snapshot.changed_files
    elif approval is not None:
        final_paths = approval.changed_files
    else:
        final_paths = ()
    changed_files = tuple(_safe_repository_path(path) for path in final_paths)
    repository = _repository_view(
        mapping=mapping,
        base_commit=prepared.base_commit,
        head_commit=prepared.head_commit,
        tree_hash="",
        changed_files=changed_files,
        publication=publication if publication_bound else None,
        test_count=len(run.test_results),
    )
    return (repository,), tuple(_test_view(item) for item in run.test_results)


def _group_views(
    run: WorkflowRun, *, fingerprint_bound: bool, signed: bool
) -> tuple[tuple[RepositoryView, ...], tuple[TestView, ...]]:
    approval = run.approval
    approvals = {
        item.repository_key: item
        for item in (
            approval.repositories
            if fingerprint_bound and approval is not None
            else ()
        )
    }
    publications = {
        item.repository_key: item
        for item in (
            run.group_publication.repositories
            if signed and run.group_publication is not None
            else ()
        )
        if approval is not None
        and item.approved_fingerprint == approval.fingerprint
    }
    repository_views: list[RepositoryView] = []
    test_views: list[TestView] = []
    for evidence in run.repository_evidence:
        key = evidence.repository_key
        signed_evidence = approvals.get(key)
        publication = publications.get(key)
        changed_files = tuple(
            _safe_repository_path(path) for path in evidence.changed_files
        )
        repository_views.append(
            _repository_view(
                mapping=evidence.mapping,
                base_commit=evidence.prepared_worktree.base_commit,
                head_commit=evidence.prepared_worktree.head_commit,
                tree_hash=signed_evidence.tree_hash if signed_evidence else "",
                changed_files=changed_files,
                publication=publication,
                test_count=len(evidence.test_results),
            )
        )
        test_views.extend(_test_view(item) for item in evidence.test_results)
    test_views.extend(_test_view(item) for item in run.integration_test_results)
    return tuple(repository_views), tuple(test_views)


def _repository_view(
    *,
    mapping: RepositoryMapping,
    base_commit: str,
    head_commit: str,
    tree_hash: str,
    changed_files: tuple[str, ...],
    publication: PublicationResult | RepositoryPublicationResult | None,
    test_count: int,
) -> RepositoryView:
    pr_url = ""
    if publication is not None and publication.pr_url:
        try:
            repository_identity = parse_repository_identity(
                publication.repo_url, publication.provider_host
            )
        except (AttributeError, PullRequestProviderError):
            raise TuiDisplayError(_INVALID_PR_URL) from None
        pr_url = _safe_pr_url(
            publication.pr_url,
            expected_host=publication.provider_host,
            provider=publication.provider,
            repository_identity=repository_identity,
        )
    return RepositoryView(
        key=safe_tui_text(mapping.key, maximum=128),
        role=mapping.role.value,
        base_commit=safe_tui_text(base_commit, maximum=64),
        head_commit=safe_tui_text(head_commit, maximum=64),
        tree_hash=safe_tui_text(tree_hash, maximum=64, allow_empty=True),
        changed_files=changed_files,
        changed_file_count=len(changed_files),
        commit_hash=(
            safe_tui_text(publication.commit_hash, maximum=64, allow_empty=True)
            if publication is not None
            else ""
        ),
        pushed=bool(publication and publication.push_completed_at),
        pr_url=pr_url,
        error=(
            _PUBLICATION_FAILED
            if publication is not None and publication.error
            else ""
        ),
        test_summary=_test_fact_summary(test_count),
        pr_target=safe_tui_text(mapping.base_branch, maximum=256),
    )


def _test_fact_summary(count: int) -> str:
    return f"{count} verified test {'fact' if count == 1 else 'facts'}"


def _test_view(result: CommandResult) -> TestView:
    outcome = result.outcome
    if outcome is None:
        raise TuiDisplayError(_INVALID_DISPLAY)
    return TestView(
        command="test command",
        outcome=outcome.value,
        exit_code=result.exit_code,
    )


def _safe_repository_path(value: str) -> str:
    checked = _strict_tui_text(value, maximum=1024)
    parts = checked.split("/")
    if (
        checked.startswith("/")
        or ":" in checked
        or "\\" in checked
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0].casefold() == ".git"
    ):
        raise TuiDisplayError(_INVALID_DISPLAY)
    return escape_markup(checked)


def _safe_fingerprint(value: str) -> str:
    checked = _strict_tui_text(value, maximum=64, allow_empty=True)
    if checked and re.fullmatch(r"[0-9a-f]{64}", checked) is None:
        raise TuiDisplayError(_INVALID_DISPLAY)
    return checked


def _approval_status(
    approval: ApprovalPackage | None,
    *,
    bind_unsigned: bool = False,
) -> tuple[str, bool, bool]:
    if approval is None:
        return "", False, False
    fingerprint = _safe_fingerprint(approval.fingerprint)
    if fingerprint:
        try:
            actual = approval_fingerprint(approval)
        except Exception:
            raise TuiDisplayError(_INVALID_FACTS) from None
        if not hmac.compare_digest(fingerprint, actual):
            raise TuiDisplayError(_INVALID_FACTS)
    elif bind_unsigned:
        try:
            normalized = validate_for_approval(approval)
            fingerprint = approval_fingerprint(normalized)
        except Exception:
            raise TuiDisplayError(_INVALID_FACTS) from None
    fingerprint_bound = bool(fingerprint)
    signed = fingerprint_bound and _has_valid_approval_metadata(approval)
    return fingerprint, fingerprint_bound, signed


def _has_valid_approval_metadata(approval: ApprovalPackage) -> bool:
    actor = approval.approved_by
    approved_at = approval.approved_at
    if (
        type(actor) is not str
        or not actor.strip()
        or type(approved_at) is not datetime
        or approved_at.tzinfo is None
        or approved_at.utcoffset() is None
        or any(
            ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
            for character in actor
        )
    ):
        return False
    try:
        actor.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def _validate_bound_single_facts(
    run: WorkflowRun,
    approval: ApprovalPackage,
    *,
    include_publication: bool,
) -> None:
    mapping = run.repository
    prepared = run.prepared_worktree
    snapshot = run.tested_snapshot
    if (
        approval.work_item_id != run.work_item_id
        or mapping is None
        or prepared is None
        or snapshot is None
        or approval.repository != mapping
        or approval.repo_url != mapping.repo_url
        or approval.base_branch != mapping.base_branch
        or approval.base_commit != prepared.base_commit
        or approval.base_commit != run.base_commit
        or approval.head_commit != prepared.head_commit
        or approval.head_commit != snapshot.head_commit
        or approval.head_commit != run.head_commit
        or approval.diff_hash != snapshot.diff_sha256
        or approval.branch != prepared.branch
        or approval.branch != run.branch
        or approval.changed_files != snapshot.changed_files
        or approval.changed_files != run.changed_files
        or not _approved_tail_matches(run.test_results, approval.tests)
    ):
        raise TuiDisplayError(_INVALID_FACTS)
    publication = run.publication
    if include_publication and publication.approved_fingerprint:
        _validate_publication_intent(
            publication,
            run_id=run.run_id,
            approval_fingerprint=approval.fingerprint,
            repo_url=approval.repo_url,
            base_branch=approval.base_branch,
            head_commit=approval.head_commit,
            branch=approval.branch,
            commit_message=approval.commit_message,
            pr_title=approval.pr_title,
            pr_body=approval.pr_body,
        )


def _validate_bound_group_facts(
    run: WorkflowRun,
    approval: ApprovalPackage,
    *,
    include_publication: bool,
) -> None:
    group = run.repository_group
    if (
        approval.work_item_id != run.work_item_id
        or group is None
        or approval.repository_group != group
    ):
        raise TuiDisplayError(_INVALID_FACTS)
    keys = group.topological_keys()
    if (
        tuple(item.repository_key for item in run.repository_evidence) != keys
        or tuple(item.repository_key for item in approval.repositories) != keys
        or approval.integration_tests != run.integration_test_results
    ):
        raise TuiDisplayError(_INVALID_FACTS)
    approved_by_key = {
        item.repository_key: item for item in approval.repositories
    }
    for evidence in run.repository_evidence:
        approved = approved_by_key.get(evidence.repository_key)
        snapshot = evidence.tested_snapshot
        if (
            approved is None
            or snapshot is None
            or approved.repository_key != evidence.repository_key
            or approved.mapping != evidence.mapping
            or approved.base_commit != evidence.prepared_worktree.base_commit
            or approved.head_commit != evidence.prepared_worktree.head_commit
            or approved.head_commit != snapshot.head_commit
            or approved.diff_hash != snapshot.diff_sha256
            or approved.branch != evidence.prepared_worktree.branch
            or approved.changed_files != evidence.changed_files
            or approved.changed_files != snapshot.changed_files
            or approved.tests != evidence.test_results
        ):
            raise TuiDisplayError(_INVALID_FACTS)
    publication = run.group_publication
    if publication is None or not include_publication:
        return
    changed = tuple(
        item for item in approval.repositories if item.changed_files
    )
    expected_keys = tuple(item.repository_key for item in changed)
    actual_keys = tuple(
        item.repository_key for item in publication.repositories
    )
    if (
        not expected_keys
        or publication.order != keys
        or publication.comment_marker != _expected_comment_marker(run.run_id)
        or len(actual_keys) != len(set(actual_keys))
        or set(actual_keys) != set(expected_keys)
    ):
        raise TuiDisplayError(_INVALID_FACTS)
    changed_by_key = {item.repository_key: item for item in changed}
    for item in publication.repositories:
        approved = changed_by_key.get(item.repository_key)
        if (
            approved is None
        ):
            raise TuiDisplayError(_INVALID_FACTS)
        _validate_publication_intent(
            item,
            run_id=run.run_id,
            approval_fingerprint=approval.fingerprint,
            repo_url=approved.mapping.repo_url,
            base_branch=approved.mapping.base_branch,
            head_commit=approved.head_commit,
            branch=approved.branch,
            commit_message=approved.commit_message,
            pr_title=approved.pr_title,
            pr_body=approved.pr_body,
            repository_key=approved.repository_key,
            tree_hash=approved.tree_hash,
        )


def _expected_comment_marker(run_id: str) -> str:
    return f"<!-- ones-dev-run:{run_id} -->"


def _validate_publication_intent(
    publication: PublicationResult | RepositoryPublicationResult,
    *,
    run_id: str,
    approval_fingerprint: str,
    repo_url: str,
    base_branch: str,
    head_commit: str,
    branch: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    repository_key: str | None = None,
    tree_hash: str | None = None,
) -> None:
    marker = f"ones-dev-run:{run_id}"
    if repository_key is not None:
        marker = f"{marker}:{repository_key}"
    expected = (
        publication.approved_fingerprint == approval_fingerprint,
        publication.repo_url == repo_url,
        publication.provider in {"github", "gitlab"},
        publication.expected_parent == head_commit,
        publication.commit_message == commit_message,
        publication.remote_branch == branch,
        publication.pr_marker == marker,
        publication.pr_base == base_branch,
        publication.pr_head == branch,
        publication.pr_title == pr_title,
        publication.pr_body == pr_body,
        publication.comment_marker == _expected_comment_marker(run_id),
        tree_hash is None or publication.expected_tree == tree_hash,
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", publication.expected_tree)
        is not None,
    )
    if not all(expected):
        raise TuiDisplayError(_INVALID_FACTS)
    try:
        provider_host = _strict_tui_text(publication.provider_host, maximum=253)
        parse_repository_identity(publication.repo_url, provider_host)
    except (PullRequestProviderError, TuiDisplayError):
        raise TuiDisplayError(_INVALID_FACTS) from None


def _approved_tail_matches(
    results: tuple[CommandResult, ...], approved: tuple[CommandResult, ...]
) -> bool:
    if not approved:
        return not results
    return len(results) >= len(approved) and results[-len(approved):] == approved


def _safe_pr_url(
    value: str,
    *,
    expected_host: str,
    provider: str,
    repository_identity: tuple[str, str],
) -> str:
    try:
        checked = _strict_tui_text(value)
        parsed = urlsplit(checked)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname.casefold() != expected_host.casefold()
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        namespace, repository = repository_identity
        if provider == "github":
            expected_path = rf"/{re.escape(namespace)}/{re.escape(repository)}/pull/[1-9][0-9]*"
        elif provider == "gitlab":
            expected_path = rf"/{re.escape(namespace)}/{re.escape(repository)}/-/merge_requests/[1-9][0-9]*"
        else:
            raise ValueError
        if (
            any(character in parsed.path for character in "%[]")
            or re.fullmatch(expected_path, parsed.path) is None
        ):
            raise ValueError
        port = parsed.port
        hostname = parsed.hostname.casefold()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{port}" if port is not None else hostname
        sanitized = urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))
        return escape_markup(_strict_tui_text(sanitized))
    except (TuiDisplayError, ValueError, UnicodeError):
        raise TuiDisplayError(_INVALID_PR_URL) from None
