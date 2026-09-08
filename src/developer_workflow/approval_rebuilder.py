"""Production reconstruction of approval evidence at the publication boundary."""

from __future__ import annotations

from . import pr_handoff

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol

from src.contracts import DefectRecord, RequirementRecord, WikiPageSnapshot

from .approval import collect_defect_risks, validate_for_approval
from .command_utils import display_argv, parse_command_argv
from .contracts import (
    ApprovalPackage,
    RepositoryApprovalEvidence,
    RepositorySnapshot,
    WorkflowRun,
    WorkflowType,
)
from .group_evidence import GroupEvidenceError, assert_group_snapshots_equal
from .test_evidence import (
    FinalTestEvidenceError,
    defect_reproduction_argv,
    select_defect_final_tests,
    select_group_final_tests,
    select_requirement_final_tests,
)


class ApprovalRebuildError(RuntimeError):
    """Live approval evidence cannot be reconstructed safely."""


class ReadOnlyApprovalGateway(Protocol):
    def get_normalized_requirement_sync(self, issue_id: str) -> RequirementRecord: ...
    def get_normalized_defect_sync(self, issue_id: str, **kwargs: object) -> DefectRecord: ...
    def get_wiki_snapshot_by_ids_sync(
        self, space_id: str, page_id: str, *, source_url: str | None = None
    ) -> WikiPageSnapshot: ...


class ApprovalSnapshotRepository(Protocol):
    def assert_remote_base_unchanged(self, prepared: object, mapping: object) -> None: ...
    def assert_head_unchanged(self, prepared: object) -> None: ...
    def snapshot(self, prepared: object, mapping: object) -> RepositorySnapshot: ...


def _digest(value: object) -> str:
    try:
        payload = json.dumps(
            asdict(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        raise ApprovalRebuildError("ONES source snapshot is invalid") from None
    return hashlib.sha256(payload).hexdigest()


@dataclass(slots=True)
class WorkflowApprovalRebuilder:
    """Re-read all machine evidence; retain only explicitly human-authored fields."""

    gateway: ReadOnlyApprovalGateway
    repository: ApprovalSnapshotRepository

    def rebuild(self, run: WorkflowRun) -> ApprovalPackage:
        try:
            pr_handoff.assert_bound(run)
        except ValueError as error:
            raise ApprovalRebuildError("external verification evidence is incomplete or stale") from error
        if run.repository_group is not None:
            return self._rebuild_group(run)
        current = run.approval
        mapping, prepared, tested = (
            run.repository, run.prepared_worktree, run.tested_snapshot
        )
        if current is None or mapping is None or prepared is None or tested is None:
            raise ApprovalRebuildError("persisted approval evidence is incomplete")
        if (
            current.repository != mapping
            or current.repo_url != mapping.repo_url
            or current.base_branch != mapping.base_branch
            or current.base_commit != prepared.base_commit
            or current.branch != prepared.branch
        ):
            raise ApprovalRebuildError("repository identity no longer matches the run")

        self.repository.assert_remote_base_unchanged(prepared, mapping)
        self.repository.assert_head_unchanged(prepared)
        snapshot = self.repository.snapshot(prepared, mapping)
        self.repository.assert_head_unchanged(prepared)
        if snapshot.head_commit != prepared.head_commit:
            raise ApprovalRebuildError("repository HEAD changed")
        if snapshot != tested:
            raise ApprovalRebuildError("repository snapshot differs from tested evidence")

        review = run.review
        review_findings = () if review is None else (
            review.review_findings or ((review.summary,) if review.summary else ())
        )
        risks = tuple(dict.fromkeys(
            item for result in (*run.coding_agent_results, *((review,) if review else ()))
            for item in result.risks
        ))
        evidence = tuple(dict.fromkeys(
            item for result in (*run.coding_agent_results, *((review,) if review else ()))
            for item in result.evidence
        ))
        wiki = tuple(self._wiki_snapshot(item) for item in run.wiki_snapshots)

        if run.type is WorkflowType.REQUIREMENT:
            source = self.gateway.get_normalized_requirement_sync(run.work_item_id)
            if source.requirement_id != run.work_item_id:
                raise ApprovalRebuildError("ONES requirement identity changed")
            rebuilt = current.model_copy(update={
                "work_item_id": source.requirement_id,
                "work_item_title": source.title,
                "work_item_status": source.status.name or source.status.id,
                "source_versions": {"requirement_sha256": _digest(source)},
                "wiki_hashes": {item.page_id: item.content_sha256 for item in wiki},
                "wiki_snapshots": wiki,
                "repository": mapping,
                "repo_url": mapping.repo_url,
                "base_branch": mapping.base_branch,
                "base_commit": prepared.base_commit,
                "head_commit": snapshot.head_commit,
                "diff_hash": snapshot.diff_sha256,
                "diff_summary": self._diff_summary(snapshot),
                "branch": prepared.branch,
                "changed_files": snapshot.changed_files,
                "coverage": {
                    f"{item.criterion_id}: {item.criterion_text}":
                    f"files={','.join(item.files)}; tests={','.join(item.tests)}"
                    for item in run.acceptance_coverage
                },
                "evidence": evidence or ("verified repository diff and configured tests",),
                "tests": select_requirement_final_tests(run.test_results, mapping),
                "review": review_findings,
                "risks": risks,
            })
        else:
            source = self.gateway.get_normalized_defect_sync(
                run.work_item_id,
                project_id=run.project_id or None,
                sprint_id=run.iteration_id or None,
                assignee=run.assignee_id or None,
            )
            if source.defect_id != run.work_item_id:
                raise ApprovalRebuildError("ONES defect identity changed")
            reproduction_command = self._defect_reproduction_command(run)
            rebuilt = current.model_copy(update={
                "work_item_id": source.defect_id,
                "work_item_title": source.title,
                "work_item_status": source.status.name or source.status.id,
                "source_versions": {"defect_sha256": _digest(source)},
                "wiki_hashes": {item.page_id: item.content_sha256 for item in wiki},
                "wiki_snapshots": wiki,
                "repository": mapping,
                "repo_url": mapping.repo_url,
                "base_branch": mapping.base_branch,
                "base_commit": prepared.base_commit,
                "head_commit": snapshot.head_commit,
                "diff_hash": snapshot.diff_sha256,
                "diff_summary": self._diff_summary(snapshot),
                "branch": prepared.branch,
                "changed_files": snapshot.changed_files,
                "evidence": tuple(
                    f"{item.file_path}:{item.location} - {item.mechanism}"
                    for item in run.root_cause_evidence
                ),
                "tests": select_defect_final_tests(
                    run.test_results,
                    mapping,
                    reproduction_command=reproduction_command,
                    reproduction_argv=self._defect_reproduction_argv(run),
                    changed_files=snapshot.changed_files,
                ),
                "review": review_findings,
                "risks": collect_defect_risks(run),
                "root_cause_evidence": run.root_cause_evidence,
                "behavior_before": run.behavior_before,
                "behavior_after": run.behavior_after,
                "impact_scope": run.impact_scope,
                "risk_level": run.risk_level,
                "pre_fix_tests": run.pre_fix_test_results,
                "reproduction_command": reproduction_command,
                "reproduction_test_sha256": run.reproduction_test_sha256,
            })
        return validate_for_approval(rebuilt)

    def _rebuild_group(self, run: WorkflowRun) -> ApprovalPackage:
        current, group = run.approval, run.repository_group
        if current is None or group is None or not run.repository_evidence:
            raise ApprovalRebuildError("persisted approval evidence is incomplete")
        if current.repository_group != group:
            raise ApprovalRebuildError("repository group identity no longer matches the run")
        current_by_key = {item.repository_key: item for item in current.repositories}
        evidence_by_key = {item.repository_key: item for item in run.repository_evidence}
        keys = group.topological_keys()
        if tuple(evidence_by_key) != keys or set(current_by_key) != set(keys):
            raise ApprovalRebuildError("repository group evidence order changed")
        publication_keys: set[str] = set()
        if run.group_publication is not None:
            publication_keys = {
                item.repository_key for item in run.group_publication.repositories
            }
            expected_publication_keys = {
                item.repository_key for item in current.repositories
                if item.changed_files
            }
            if publication_keys != expected_publication_keys:
                raise ApprovalRebuildError(
                    "repository group publication differs from signed approval"
                )
        snapshots: dict[str, RepositorySnapshot] = {}
        for key in keys:
            evidence = evidence_by_key[key]
            if current_by_key[key].mapping != evidence.mapping:
                raise ApprovalRebuildError("repository mapping changed after approval")
            self.repository.assert_remote_base_unchanged(
                evidence.prepared_worktree, evidence.mapping
            )
            if key in publication_keys:
                # Publication advances the isolated worktree from the tested HEAD to
                # the approved commit. Publisher independently verifies that commit,
                # its remote branch and PR facts before trusting a recovery. Preserve
                # the immutable tested snapshot here while still rebuilding every
                # external source and remote base.
                snapshot = evidence.tested_snapshot
            else:
                self.repository.assert_head_unchanged(evidence.prepared_worktree)
                snapshot = self.repository.snapshot(
                    evidence.prepared_worktree, evidence.mapping
                )
                self.repository.assert_head_unchanged(evidence.prepared_worktree)
                if snapshot.head_commit != evidence.prepared_worktree.head_commit:
                    raise ApprovalRebuildError("repository HEAD changed")
            snapshots[key] = snapshot
        for key in keys:
            evidence = evidence_by_key[key]
            self.repository.assert_remote_base_unchanged(
                evidence.prepared_worktree, evidence.mapping
            )
            if key not in publication_keys:
                self.repository.assert_head_unchanged(evidence.prepared_worktree)
        try:
            assert_group_snapshots_equal(run.repository_evidence, snapshots, group)
            select_group_final_tests(
                run.repository_evidence, run.integration_test_results, group,
                reproduction_evidence=(run.root_cause_evidence if run.type is WorkflowType.DEFECT else ()),
            )
        except (GroupEvidenceError, FinalTestEvidenceError):
            raise ApprovalRebuildError(
                "repository group differs from tested evidence"
            ) from None
        review = run.review
        review_findings = () if review is None else (
            review.review_findings or ((review.summary,) if review.summary else ())
        )
        risks = tuple(dict.fromkeys(
            item for result in (*run.coding_agent_results, *((review,) if review else ()))
            for item in result.risks
        ))
        evidence_text = tuple(dict.fromkeys(
            item for result in (*run.coding_agent_results, *((review,) if review else ()))
            for item in result.evidence
        ))
        wiki = tuple(self._wiki_snapshot(item) for item in run.wiki_snapshots)
        repositories = tuple(
            current_by_key[key].validated_update(
                mapping=evidence_by_key[key].mapping,
                base_commit=evidence_by_key[key].prepared_worktree.base_commit,
                head_commit=snapshots[key].head_commit,
                diff_hash=snapshots[key].diff_sha256,
                diff_summary=self._diff_summary(snapshots[key]),
                branch=evidence_by_key[key].prepared_worktree.branch,
                changed_files=snapshots[key].changed_files,
                tests=evidence_by_key[key].test_results,
            )
            for key in keys
        )
        common = {
            "wiki_hashes": {item.page_id: item.content_sha256 for item in wiki},
            "wiki_snapshots": wiki,
            "repository_group": group,
            "repositories": repositories,
            "integration_tests": run.integration_test_results,
            "review": review_findings,
            "risks": risks,
        }
        if run.type is WorkflowType.REQUIREMENT:
            source = self.gateway.get_normalized_requirement_sync(run.work_item_id)
            if source.requirement_id != run.work_item_id:
                raise ApprovalRebuildError("ONES requirement identity changed")
            rebuilt = current.model_copy(update={
                **common,
                "work_item_id": source.requirement_id,
                "work_item_title": source.title,
                "work_item_status": source.status.name or source.status.id,
                "source_versions": {"requirement_sha256": _digest(source)},
                "coverage": {
                    f"{item.criterion_id}: {item.criterion_text}": (
                        "files=" + ",".join(
                            f"{claim.repository_key}:{claim.path}"
                            for claim in item.repository_files
                        ) + "; tests=" + ",".join(item.tests)
                    ) for item in run.acceptance_coverage
                },
                "evidence": evidence_text or (
                    "verified repository group diff and configured tests",
                ),
            })
        else:
            source = self.gateway.get_normalized_defect_sync(
                run.work_item_id,
                project_id=run.project_id or None,
                sprint_id=run.iteration_id or None,
                assignee=run.assignee_id or None,
            )
            if source.defect_id != run.work_item_id:
                raise ApprovalRebuildError("ONES defect identity changed")
            rebuilt = current.model_copy(update={
                **common,
                "work_item_id": source.defect_id,
                "work_item_title": source.title,
                "work_item_status": source.status.name or source.status.id,
                "source_versions": {"defect_sha256": _digest(source)},
                "risks": collect_defect_risks(run),
                "evidence": tuple(
                    f"{item.repository_file.repository_key}:{item.file_path}:"
                    f"{item.location} - {item.mechanism}"
                    for item in run.root_cause_evidence
                    if item.repository_file is not None
                ),
                "root_cause_evidence": run.root_cause_evidence,
                "behavior_before": run.behavior_before,
                "behavior_after": run.behavior_after,
                "impact_scope": run.impact_scope,
                "risk_level": run.risk_level,
                "pre_fix_tests": run.pre_fix_test_results,
                "reproduction_command": self._defect_reproduction_command(run),
                "reproduction_test_sha256": run.reproduction_test_sha256,
            })
        return validate_for_approval(rebuilt)

    def _wiki_snapshot(self, expected: WikiPageSnapshot) -> WikiPageSnapshot:
        actual = self.gateway.get_wiki_snapshot_by_ids_sync(
            expected.space_id, expected.page_id, source_url=expected.source_url
        )
        if (
            actual.team_id != expected.team_id
            or actual.space_id != expected.space_id
            or actual.page_id != expected.page_id
            or actual.source_url != expected.source_url
        ):
            raise ApprovalRebuildError("ONES wiki identity changed")
        return actual

    @staticmethod
    def _diff_summary(snapshot: RepositorySnapshot) -> str:
        return (
            f"changed {len(snapshot.changed_files)} file(s): "
            f"{', '.join(snapshot.changed_files)}"
        )

    @staticmethod
    def _defect_reproduction_command(run: WorkflowRun) -> str:
        return display_argv(WorkflowApprovalRebuilder._defect_reproduction_argv(run))

    @staticmethod
    def _defect_reproduction_argv(run: WorkflowRun) -> tuple[str, ...]:
        mapping = run.repository
        if run.repository_group is not None and run.root_cause_evidence:
            owner = run.root_cause_evidence[0].reproduction_file
            mapping = next((item for item in run.repository_group.repositories
                            if owner is not None and item.key == owner.repository_key), None)
        if mapping is None:
            raise ApprovalRebuildError("defect reproduction repository is invalid")
        try:
            return defect_reproduction_argv(run.root_cause_evidence, mapping)
        except ValueError:
            raise ApprovalRebuildError("defect reproduction evidence is invalid") from None


__all__ = ["ApprovalRebuildError", "WorkflowApprovalRebuilder"]
