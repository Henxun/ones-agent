"""Command boundary for approval-gated developer workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Literal
import unicodedata

from .approval import issue_approval
from .config import DeveloperWorkflowConfig, RepositoryMappingNotFound
from .contracts import (
    CodingAgentProvenance,
    DefectAction,
    PublicationResult,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)
from .defect_flow import DefectCandidateService, DefectFlow
from .publisher import Publisher
from .requirement_flow import RequirementFlow
from .state_store import FileRunStore
from . import verification, pr_handoff
from .verification_models import VerificationRecord


class InvalidWorkflowAction(RuntimeError):
    """Raised when a command is unsafe for the persisted workflow state."""


_BIDI_CONTROLS = {
    "LRE",
    "RLE",
    "LRO",
    "RLO",
    "PDF",
    "LRI",
    "RLI",
    "FSI",
    "PDI",
}
_BIDI_CONTROL_CHARACTERS = {
    "\u061c",  # Arabic letter mark
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
}
_INVALID_REVISION_SCOPE = (
    "revision scope is invalid; start a new defect run to rebuild evidence"
)
_STALE_WORKFLOW = "workflow changed; review again"


def _require_expected_version(run: WorkflowRun, expected_version: int | None) -> None:
    if expected_version is not None and (
        type(expected_version) is not int or run.version != expected_version
    ):
        raise InvalidWorkflowAction(_STALE_WORKFLOW)


def _validated_text(value: str, *, kind: str, max_length: int) -> str:
    """Validate command metadata without reflecting untrusted text in errors."""

    if type(value) is not str or value != value.strip() or not value:
        raise InvalidWorkflowAction(f"{kind} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise InvalidWorkflowAction(f"{kind} is invalid") from None
    if (
        len(value) > max_length
        or len(encoded) > max_length * 4
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            or character in _BIDI_CONTROL_CHARACTERS
            for character in value
        )
    ):
        raise InvalidWorkflowAction(f"{kind} is invalid")
    return value


def _validated_revision_scope(
    scope: Literal["implementation", "repair"] | None,
) -> Literal["implementation", "repair"] | None:
    if scope is None:
        return None
    try:
        validated = _validated_text(scope, kind="revision scope", max_length=32)
    except InvalidWorkflowAction:
        raise InvalidWorkflowAction(_INVALID_REVISION_SCOPE) from None
    if validated == "implementation":
        return "implementation"
    if validated == "repair":
        return "repair"
    raise InvalidWorkflowAction(_INVALID_REVISION_SCOPE)


@dataclass(slots=True)
class DeveloperWorkflowOrchestrator:
    """Coordinate persisted workflow commands through injected services."""

    store: FileRunStore
    requirement_flow: RequirementFlow
    defect_flow: DefectFlow
    publisher: Publisher
    config: DeveloperWorkflowConfig
    defect_candidates: DefectCandidateService
    coding_agent: CodingAgentProvenance | None = None

    def start_requirement(self, requirement_id: str) -> WorkflowRun:
        created = self.store.create(
            WorkflowRun.new(
                WorkflowType.REQUIREMENT,
                requirement_id,
                coding_agent=self.coding_agent,
            )
        )
        with self.store.operation_lock(created.run_id, "orchestrate"):
            current = self.store.load(created.run_id)
            return self.requirement_flow.execute(current)

    def start_defect(
        self,
        project_id: str,
        iteration_id: str,
        assignee: str,
        snapshot_token: str,
        candidate_id: str,
        *,
        action: DefectAction = DefectAction.ANALYZE_AND_REPAIR,
    ) -> WorkflowRun:
        if type(action) is not DefectAction:
            raise InvalidWorkflowAction("defect action is invalid")
        selected = self.defect_candidates.select(
            snapshot_token,
            candidate_id,
            project_id=project_id,
            iteration_id=iteration_id,
            assignee_id=assignee,
        )
        defect = selected.defect
        if (
            selected.type is not WorkflowType.DEFECT
            or defect is None
            or selected.work_item_id != defect.defect_id
            or selected.candidate_id != defect.defect_id
            or selected.project_id != project_id
            or selected.iteration_id != iteration_id
            or selected.assignee_id != assignee
            or defect.project.id != project_id
            or defect.assignee is None
            or defect.assignee.id != assignee
        ):
            raise InvalidWorkflowAction(
                "defect selection requires a verified candidate snapshot"
            )
        selected = selected.validated_update(defect_action=action)
        created = self.store.create(selected)
        with self.store.operation_lock(created.run_id, "orchestrate"):
            current = self.store.load(created.run_id)
            return self.defect_flow.execute(current)

    def show(self, run_id: str, *, read_only: bool = False) -> WorkflowRun:
        """Load a run, optionally without creating storage lock artifacts."""

        run = self.store.load(run_id, read_only=read_only)
        if run.state is WorkflowState.BLOCKED and run.blocked_reason in verification.VERIFICATION_REASONS:
            run = run.validated_update(verification_plan=verification.plan(run, self.config.verification_nodes))
        return run

    def accept_analysis_solution(
        self, run_id: str, *, expected_version: int
    ) -> WorkflowRun:
        with self.store.operation_lock(run_id, "analysis-decision"):
            current = self.store.decide_completed_analysis(
                run_id, expected_version, accept=True
            )
            return self.defect_flow.execute(current)

    def regenerate_analysis_solution(
        self, run_id: str, *, expected_version: int
    ) -> WorkflowRun:
        with self.store.operation_lock(run_id, "analysis-decision"):
            current = self.store.decide_completed_analysis(
                run_id, expected_version, accept=False
            )
            return self.defect_flow.execute(current)

    def ai_activity(self, run_id: str) -> tuple[str, ...]:
        """Read sanitized Codex activity without changing workflow state."""

        source = getattr(self.defect_flow.codex, "activity", None)
        if not callable(source):
            return ()
        result = source(run_id)
        if type(result) is not tuple or any(type(item) is not str for item in result):
            return ()
        return result

    def verification_nodes(self) -> tuple[dict, ...]:
        return tuple(node.model_dump(mode="json") for node in self.config.verification_nodes)

    def replan_verification(self, run_id: str, *, expected_version: int) -> WorkflowRun:
        """Explicit one-shot review refresh for legacy free-text environment needs."""
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            _require_expected_version(run, expected_version)
            if (run.state is not WorkflowState.BLOCKED or run.blocked_reason not in verification.VERIFICATION_REASONS
                    or run.review is None or run.review.unresolved_items):
                raise InvalidWorkflowAction("verification planning is unavailable")
            run = self.store.save(run.validated_update(review=None, verification_plan=(), approval=None), run.version)
            return self._flow_for(run).execute(run)

    def probe_verification_node(self, node_key: str) -> dict:
        node = next((item for item in self.config.verification_nodes if item.key == node_key and item.enabled), None)
        if node is None:
            raise InvalidWorkflowAction("verification node is unavailable")
        return verification.invoke(node, {"operation": "probe"}, 20)

    def verify(self, run_id: str, task_key: str, actor: str, *, expected_version: int,
               manual_evidence: str | None = None, passed: bool = True,
               expected_recipe_digest: str | None = None) -> WorkflowRun:
        """Explicitly authorize one configured verifier or attest manual evidence."""
        if type(passed) is not bool:
            raise InvalidWorkflowAction("verification result must be a boolean")
        actor = _validated_text(actor, kind="verification actor", max_length=128)
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            _require_expected_version(run, expected_version)
            if (run.state is not WorkflowState.BLOCKED or run.resume_state is not WorkflowState.AI_REVIEW
                    or run.review is None or run.review.unresolved_items or run.approval is not None):
                raise InvalidWorkflowAction("verification requires a clean review waiting for environment validation")
            flow = self._flow_for(run)
            verification.assert_current(run, flow.repository)
            tasks = verification.plan(run, self.config.verification_nodes)
            task = next((item for item in tasks if item.key == task_key), None)
            if task is None or task.status == "passed":
                raise InvalidWorkflowAction("verification task is absent or already passed")
            if manual_evidence is not None:
                evidence = _validated_text(manual_evidence, kind="manual verification evidence", max_length=4096)
                record = VerificationRecord(task_key=task.key, snapshot_digest=task.snapshot_digest,
                    node_key="manual", actor=actor, status="passed" if passed else "failed", evidence=verification.public_text(evidence, 4096),
                    output_sha256=hashlib.sha256(evidence.encode()).hexdigest(), occurred_at=datetime.now(timezone.utc).isoformat())
            else:
                node = next((item for item in self.config.verification_nodes if item.key == task.node_key and item.enabled), None)
                if node is None or not task.recipe_key:
                    raise InvalidWorkflowAction("no authorized matching validation node/recipe")
                if expected_recipe_digest != task.recipe_digest:
                    raise InvalidWorkflowAction("verification recipe changed; inspect and confirm its digest")
                run = self.store.save(run.validated_update(verification_plan=tuple(
                    item.model_copy(update={"status": "running"}) if item.key == task.key else item for item in tasks
                )), run.version)
                try:
                    record = verification.execute(run, task, node, actor)
                    verification.assert_current(run, flow.repository)
                except Exception as error:
                    record = VerificationRecord(task_key=task.key, snapshot_digest=task.snapshot_digest,
                        node_key=node.key, recipe_key=task.recipe_key, recipe_digest=task.recipe_digest,
                        actor=actor, status="error", evidence=verification.failure_message(error),
                        output_sha256=hashlib.sha256(b"verification error").hexdigest(), occurred_at=datetime.now(timezone.utc).isoformat())
            run = self.store.save(run.validated_update(verification_records=(*run.verification_records, record),
                # A test failure is assessed by review, not assumed to be a code defect.
                review=None if record.status == "failed" else run.review), run.version)
            return flow.execute(run)

    def confirm_repository(
        self,
        run_id: str,
        mapping_key: str,
        *,
        expected_version: int | None = None,
    ) -> WorkflowRun:
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            _require_expected_version(run, expected_version)
            if run.state is not WorkflowState.VALIDATING:
                raise InvalidWorkflowAction("repository confirmation requires VALIDATING")
            persisted_group = next(
                (
                    candidate for candidate in run.repository_group_candidates
                    if candidate.key == mapping_key
                ),
                None,
            )
            if persisted_group is not None:
                try:
                    configured_group = self.config.resolve_group_key(
                        mapping_key, run.project_id, run.iteration_id
                    )
                except RepositoryMappingNotFound:
                    raise InvalidWorkflowAction(
                        "repository group is not configured for this workflow"
                    ) from None
                if configured_group != persisted_group:
                    raise InvalidWorkflowAction(
                        "repository group differs from the persisted candidate"
                    )
                saved = self.store.save(
                    run.validated_update(
                        repository_model_version=2,
                        repository_group=configured_group,
                        repository=None,
                    ),
                    expected_version=run.version,
                )
                return self._flow_for(saved).execute(saved)
            try:
                mapping = self.config.resolve_mapping_key(
                    mapping_key, run.project_id, run.iteration_id
                )
            except RepositoryMappingNotFound:
                raise InvalidWorkflowAction(
                    "repository mapping is not configured for this workflow"
                ) from None
            if not any(
                candidate.key == mapping_key and candidate == mapping
                for candidate in run.repository_candidates
            ):
                raise InvalidWorkflowAction(
                    "repository mapping is not an authorized persisted candidate"
                )
            saved = self.store.save(
                run.validated_update(repository=mapping), expected_version=run.version
            )
            return self._flow_for(saved).execute(saved)

    def resume(
        self, run_id: str, *, expected_version: int | None = None
    ) -> WorkflowRun:
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            _require_expected_version(run, expected_version)
            if run.state is WorkflowState.WAITING_PR_VERIFICATION:
                # Handoff is complete; human/CI verification now belongs to the PR.
                return run
            if run.state is WorkflowState.PARTIAL_SUCCESS:
                return (
                    self.publisher.publish(run)
                    if run.repository_group is not None
                    else self.publisher.retry_comment(run)
                )
            if run.state is WorkflowState.PUBLISHING:
                return self.publisher.publish(run)
            if run.state is WorkflowState.BLOCKED and run.resume_state is None:
                raise InvalidWorkflowAction("Blocked run has no safe resume state")
            if (
                run.state is WorkflowState.BLOCKED
                and run.resume_state is WorkflowState.PUBLISHING
            ):
                publishing = self.store.transition(
                    run.run_id,
                    run.version,
                    WorkflowState.PUBLISHING,
                    "resume publication from persisted safe checkpoint",
                )
                return self.publisher.publish(publishing)
            return self._flow_for(run).execute(run)

    def revise(
        self,
        run_id: str,
        feedback: str,
        *,
        scope: Literal["implementation", "repair"] | None = None,
        expected_version: int | None = None,
    ) -> WorkflowRun:
        scope = _validated_revision_scope(scope)
        feedback = _validated_text(feedback, kind="revision feedback", max_length=4096)
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            _require_expected_version(run, expected_version)
            expected_scope = (
                "implementation"
                if run.workflow_type is WorkflowType.REQUIREMENT
                else "repair"
                if run.workflow_type is WorkflowType.DEFECT
                else None
            )
            if expected_scope is None or (scope is not None and scope != expected_scope):
                raise InvalidWorkflowAction(_INVALID_REVISION_SCOPE)
            if run.state not in {
                WorkflowState.WAITING_APPROVAL,
                WorkflowState.BLOCKED,
            }:
                raise InvalidWorkflowAction(
                    "revise requires WAITING_APPROVAL or BLOCKED"
                )
            if run.publication != PublicationResult() or run.group_publication is not None:
                raise InvalidWorkflowAction(
                    "published workflow evidence cannot be revised; create a new workflow run"
                )
            if run.state is WorkflowState.BLOCKED:
                if run.resume_state not in {
                    WorkflowState.IMPLEMENTING,
                    WorkflowState.TESTING,
                    WorkflowState.AI_REVIEW,
                    WorkflowState.WAITING_APPROVAL,
                }:
                    raise InvalidWorkflowAction(
                        "blocked run has no safe implementation revision checkpoint"
                    )
                if run.resume_state is WorkflowState.IMPLEMENTING:
                    blocked = run
                else:
                    resumed = self.store.transition(
                        run.run_id,
                        run.version,
                        run.resume_state,
                        "restore late workflow checkpoint for revision",
                    )
                    blocked = self.store.transition(
                        resumed.run_id,
                        resumed.version,
                        WorkflowState.BLOCKED,
                        "revision requested",
                        resume_state=WorkflowState.IMPLEMENTING,
                    )
            else:
                blocked = self.store.transition(
                    run.run_id,
                    run.version,
                    WorkflowState.BLOCKED,
                    "revision requested",
                    resume_state=WorkflowState.IMPLEMENTING,
                )
            revised = blocked.for_revision(feedback)
            saved = self.store.save(revised, expected_version=blocked.version)
            return self._flow_for(saved).execute(saved)

    def approve(
        self,
        run_id: str,
        approved_by: str,
        *,
        expected_version: int | None = None,
    ) -> WorkflowRun:
        approved_by = _validated_text(
            approved_by, kind="approver identity", max_length=128
        )
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            _require_expected_version(run, expected_version)
            if run.state is not WorkflowState.WAITING_APPROVAL:
                raise InvalidWorkflowAction("approve requires WAITING_APPROVAL")
            if run.approval is None:
                raise InvalidWorkflowAction("approval package is missing")
            tasks = verification.plan(run, self.config.verification_nodes)
            if (pr_handoff.blocking_reason(tasks, defer=self.config.publishing.defer_external_verification_to_pr)
                    or tasks != run.verification_plan):
                run = self.store.save(run.validated_update(approval=None, verification_plan=tasks), run.version)
                return self.store.transition(run.run_id, run.version, WorkflowState.BLOCKED,
                    verification.pending_reason(tasks) or verification.VERIFICATION_READY,
                    resume_state=WorkflowState.AI_REVIEW)
            pr_handoff.assert_bound(run)
            approval = issue_approval(run.approval, approved_by=approved_by)
            saved = self.store.save(
                run.validated_update(approval=approval), expected_version=run.version
            )
            return self.publisher.publish(saved)

    def cancel(
        self,
        run_id: str,
        actor: str,
        *,
        expected_version: int | None = None,
    ) -> WorkflowRun:
        actor = _validated_text(actor, kind="cancellation actor", max_length=128)
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            _require_expected_version(run, expected_version)
            return self.store.transition(
                run_id,
                run.version,
                WorkflowState.CANCELLED,
                f"cancelled by {actor}",
            )

    def _flow_for(self, run: WorkflowRun) -> RequirementFlow | DefectFlow:
        if run.workflow_type is WorkflowType.REQUIREMENT:
            return self.requirement_flow
        if run.workflow_type is WorkflowType.DEFECT:
            return self.defect_flow
        raise InvalidWorkflowAction("Unknown workflow type")


__all__ = ["DeveloperWorkflowOrchestrator", "InvalidWorkflowAction"]
