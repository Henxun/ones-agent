"""Canonical backend contracts for defect analysis workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class IdentityRef:
    id: str = ""
    name: str = ""
    avatar: str = ""


@dataclass(slots=True)
class CommitActorRef:
    email: str = ""
    name: str = ""
    username: str = ""


@dataclass(slots=True)
class ProjectRef:
    id: str = ""
    name: str = ""


@dataclass(slots=True)
class StatusRef:
    id: str = ""
    name: str = ""
    category: str = ""


@dataclass(slots=True)
class WorkflowStatusRef:
    id: str = ""
    name: str = ""
    category: str = ""
    position: int = 0
    default: bool = False
    built_in: bool = False
    detail_type: str = ""
    name_pinyin: str = ""


@dataclass(slots=True)
class IssueTypeRef:
    id: str = ""
    name: str = ""
    built_in: bool = False
    detail_type: str = ""
    component_type: str = ""


@dataclass(slots=True)
class PriorityRef:
    id: str = ""
    value: str = ""
    position: int | None = None


@dataclass(slots=True)
class RepoTarget:
    repo_url: str = ""
    repo_name: str = ""
    default_branch: str = "main"


@dataclass(slots=True)
class RepoCandidate:
    repo: RepoTarget = field(default_factory=RepoTarget)
    branch: str = "main"
    source: str = ""
    confidence: float = 0.0
    rationale: str = ""


@dataclass(slots=True)
class CommentRecord:
    id: str = ""
    text: str = ""
    author_id: str = ""
    author_name: str = ""
    created_at: str = ""


@dataclass(slots=True)
class DefectRecord:
    defect_id: str = ""
    title: str = ""
    number: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    status: StatusRef = field(default_factory=StatusRef)
    issue_type: IssueTypeRef = field(default_factory=IssueTypeRef)
    priority: PriorityRef = field(default_factory=PriorityRef)
    assignee: IdentityRef | None = None
    owner: IdentityRef | None = None
    parent: IdentityRef | None = None
    path: str = ""
    description: str = ""
    deadline: str = ""
    created_at: str = ""
    updated_at: str = ""
    comments: list[CommentRecord] = field(default_factory=list)
    comments_loaded: bool = False
    source: str = "ones"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WikiPageRef:
    team_id: str = ""
    space_id: str = ""
    page_id: str = ""
    title: str = ""
    source_url: str = ""


@dataclass(slots=True)
class WikiPageSnapshot:
    team_id: str = ""
    space_id: str = ""
    page_id: str = ""
    title: str = ""
    version: str = ""
    updated_at: str = ""
    normalized_content: str = ""
    content_sha256: str = ""
    source_url: str = ""


@dataclass(slots=True)
class RequirementRecord:
    requirement_id: str = ""
    number: str = ""
    title: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    iteration: ProjectRef = field(default_factory=ProjectRef)
    assignee: IdentityRef | None = None
    status: StatusRef = field(default_factory=StatusRef)
    issue_type: IssueTypeRef = field(default_factory=IssueTypeRef)
    description: str = ""
    wiki_refs: list[WikiPageRef] = field(default_factory=list)
    source: str = "ones"


@dataclass(slots=True)
class RepoResolution:
    defect_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    selected_repo: RepoTarget = field(default_factory=RepoTarget)
    selected_branch: str = "main"
    confidence: float = 0.0
    source: str = ""
    rationale: str = ""
    candidates: list[RepoCandidate] = field(default_factory=list)


@dataclass(slots=True)
class EvidenceReference:
    kind: str = "file"
    file_path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    snippet: str = ""
    description: str = ""
    source: str = ""


@dataclass(slots=True)
class FixSuggestion:
    title: str = ""
    description: str = ""
    impacted_files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    risk_level: str = "medium"


@dataclass(slots=True)
class AnalysisResult:
    defect_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    repo_resolution: RepoResolution | None = None
    locale: str = "zh-CN"
    analysis_summary: str = ""
    root_cause: str = ""
    evidence: list[EvidenceReference] = field(default_factory=list)
    confidence: float = 0.0
    impacted_files: list[str] = field(default_factory=list)
    fix_suggestions: list[FixSuggestion] = field(default_factory=list)
    repair_prompt: str = ""
    insufficient_evidence: bool = False
    rendered_markdown: str = ""


@dataclass(slots=True)
class AnalysisSessionSummary:
    session_id: str = ""
    defect_id: str = ""
    status: str = "running"
    latest_stage: str = ""
    locale: str = "zh-CN"
    analysis_status: str = ""
    analysis_summary: str = ""
    blocked_reason: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class AnalysisSessionEvent:
    event_id: str = ""
    session_id: str = ""
    defect_id: str = ""
    event_type: str = ""
    stage: str = ""
    status: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(slots=True)
class ExecutionRequest:
    defect_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    repo_resolution: RepoResolution | None = None
    request_type: Literal["bugfix", "requirement_development"] = "bugfix"
    proposed_branch_name: str = ""
    target_branch: str = "main"
    requested_by: str = ""
    reason: str = ""
    confidence: float = 0.0
    source: str = "analysis"
    locale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommitCandidate:
    commit_hash: str = ""
    summary: str = ""
    authored_at: str = ""
    committed_at: str = ""
    work_date: str = ""
    branch: str = ""
    repo_url: str = ""
    repo_name: str = ""
    author: CommitActorRef = field(default_factory=CommitActorRef)
    source_paths: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommitterOwnerMapping:
    project: ProjectRef = field(default_factory=ProjectRef)
    committer: CommitActorRef = field(default_factory=CommitActorRef)
    owner: IdentityRef = field(default_factory=IdentityRef)
    source: str = ""
    confidence: float = 0.0
    matched: bool = False
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskAttribution:
    task_id: str = ""
    task_key: str = ""
    title: str = ""
    matched: bool = False
    confidence: float = 0.0
    source: str = ""
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AttendanceDayResult:
    work_date: str = ""
    committer: CommitActorRef = field(default_factory=CommitActorRef)
    verified: bool = False
    status: str = "unverified"
    first_check_in_at: str = ""
    last_check_out_at: str = ""
    allocatable_hours: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error_message: str = ""


@dataclass(slots=True)
class ManhourDraft:
    draft_id: str = ""
    run_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    owner: IdentityRef = field(default_factory=IdentityRef)
    task: TaskAttribution = field(default_factory=TaskAttribution)
    work_date: str = ""
    hours: float = 0.0
    description: str = ""
    status: str = "draft"
    attribution_status: str = "pending"
    attendance_status: str = "unverified"
    attendance: AttendanceDayResult | None = None
    source_commits: list[CommitCandidate] = field(default_factory=list)
    idempotency_key: str = ""
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ManhourSubmissionAttempt:
    attempt_id: str = ""
    draft_id: str = ""
    run_id: str = ""
    mode: Literal["dry_run", "submit"] = "dry_run"
    status: str = "pending"
    idempotency_key: str = ""
    request_payload: dict[str, Any] = field(default_factory=dict)
    response_payload: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    created_at: str = ""


@dataclass(slots=True)
class ManhourRun:
    run_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    repo: RepoTarget = field(default_factory=RepoTarget)
    committer: CommitActorRef = field(default_factory=CommitActorRef)
    requested_by: str = ""
    start_date: str = ""
    end_date: str = ""
    mode: Literal["dry_run", "submit"] = "dry_run"
    status: str = "pending"
    source: str = ""
    candidate_count: int = 0
    draft_count: int = 0
    review_needed_count: int = 0
    submitted_count: int = 0
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AnalysisResult",
    "AnalysisSessionEvent",
    "AnalysisSessionSummary",
    "AttendanceDayResult",
    "CommitActorRef",
    "CommitCandidate",
    "CommitterOwnerMapping",
    "CommentRecord",
    "DefectRecord",
    "EvidenceReference",
    "ExecutionRequest",
    "FixSuggestion",
    "IdentityRef",
    "IssueTypeRef",
    "ManhourDraft",
    "ManhourRun",
    "ManhourSubmissionAttempt",
    "PriorityRef",
    "ProjectRef",
    "RequirementRecord",
    "RepoCandidate",
    "RepoResolution",
    "RepoTarget",
    "StatusRef",
    "TaskAttribution",
    "WikiPageRef",
    "WikiPageSnapshot",
]
