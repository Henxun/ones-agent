"""Canonical, tamper-evident approval package fingerprints."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from asyncio import CancelledError
from datetime import UTC, datetime
from typing import Any, Callable, TypeVar

from src.contracts import WikiPageSnapshot

from .contracts import (
    ApprovalPackage,
    CommandResult,
    RepositoryApprovalEvidence,
    RepositoryGroupMapping,
    RepositoryMapping,
    RootCauseEvidence,
    WorkflowRun,
    utc_now,
    validate_git_ref_name,
)
from .command_utils import parse_command_argv
from .test_evidence import defect_reproduction_argv, defect_verification_prefix


_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
_EXCLUDED_FIELDS = {"fingerprint", "approved_by", "approved_at"}
_GENERIC_ERROR = "Approval package is invalid"
_SAFE_FAILURE_MESSAGES = frozenset(
    {
        _GENERIC_ERROR,
        "Approval package has invalid field types",
        "Approval package is not canonical JSON",
        "Approval package is not ready for approval",
        "Approver identity is invalid",
        "Approval timestamp is invalid",
    }
)
_T = TypeVar("_T")


def collect_defect_risks(run: WorkflowRun) -> tuple[str, ...]:
    """Keep reviewable limitations identical at preview and publication time."""
    results = (
        *run.coding_agent_results,
        *((run.review,) if run.review is not None else ()),
    )
    return tuple(dict.fromkeys((
        *(risk for result in results for risk in result.risks),
        *(
            f"Implementation follow-up: {note}"
            for result in run.coding_agent_results[2:]
            for note in result.unresolved_items
        ),
        f"risk_level={run.risk_level}",
    )))


class ApprovalError(RuntimeError):
    """Base error for approval package processing."""


class ApprovalValidationError(ApprovalError):
    """The supplied approval package cannot be safely canonicalized."""


class ApprovalInvalidatedError(ApprovalError):
    """The approved evidence no longer matches the current evidence."""


class _ApprovalFailure(Exception):
    """Private signal carrying only messages authored in this module."""


def _sanitized(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except (KeyboardInterrupt, SystemExit, CancelledError, GeneratorExit):
        raise
    except _ApprovalFailure as error:
        message = (
            error.args[0]
            if type(error) is _ApprovalFailure
            and len(error.args) == 1
            and type(error.args[0]) is str
            and error.args[0] in _SAFE_FAILURE_MESSAGES
            else _GENERIC_ERROR
        )
        raise ApprovalValidationError(message) from None
    except BaseException:
        raise ApprovalValidationError(_GENERIC_ERROR) from None


def _require_exact(value: Any, expected: type[Any]) -> None:
    if type(value) is not expected:
        raise _ApprovalFailure("Approval package has invalid field types")


def _require_string_tuple(value: Any) -> None:
    _require_exact(value, tuple)
    for item in value:
        _require_exact(item, str)


def _require_string_map(value: Any) -> None:
    _require_exact(value, dict)
    for key, item in value.items():
        _require_exact(key, str)
        _require_exact(item, str)


def _validate_repository_types(repository: RepositoryMapping) -> None:
    if type(repository) is not RepositoryMapping:
        raise _ApprovalFailure("Approval package has invalid field types")
    for field in (
        "key",
        "project_id",
        "iteration_id",
        "repo_url",
        "repo_name",
        "base_branch",
    ):
        _require_exact(getattr(repository, field), str)
    for field in (
        "test_commands",
        "lint_commands",
        "build_commands",
        "allowed_paths",
    ):
        _require_string_tuple(getattr(repository, field))


def _validate_wiki_types(snapshot: WikiPageSnapshot) -> None:
    if type(snapshot) is not WikiPageSnapshot:
        raise _ApprovalFailure("Approval package has invalid field types")
    for field in (
        "team_id",
        "space_id",
        "page_id",
        "title",
        "version",
        "updated_at",
        "normalized_content",
        "content_sha256",
        "source_url",
    ):
        _require_exact(getattr(snapshot, field), str)


def _validate_command_types(result: CommandResult) -> None:
    if type(result) is not CommandResult:
        raise _ApprovalFailure("Approval package has invalid field types")
    _require_exact(result.command, str)
    _require_exact(result.exit_code, int)
    _require_exact(result.summary, str)
    _require_exact(result.started_at, datetime)
    _require_exact(result.finished_at, datetime)


def _validate_runtime_types(package: ApprovalPackage) -> None:
    if type(package) is not ApprovalPackage:
        raise _ApprovalFailure("Approval package is invalid")
    for field in (
        "work_item_id",
        "work_item_title",
        "work_item_status",
        "repo_url",
        "base_branch",
        "base_commit",
        "head_commit",
        "diff_hash",
        "diff_summary",
        "branch",
        "commit_message",
        "pr_title",
        "pr_body",
        "behavior_before",
        "behavior_after",
        "risk_level",
        "fingerprint",
    ):
        _require_exact(getattr(package, field), str)
    _require_string_map(package.source_versions)
    _require_string_map(package.wiki_hashes)
    _require_string_map(package.coverage)
    for field in (
        "changed_files",
        "evidence",
        "review",
        "risks",
        "unresolved_items",
        "manual_checks",
        "impact_scope",
    ):
        _require_string_tuple(getattr(package, field))
    _require_exact(package.unrelated_changes_checked, bool)
    if package.repository is not None:
        _validate_repository_types(package.repository)
    if package.repository_group is not None:
        _require_exact(package.repository_group, RepositoryGroupMapping)
    _require_exact(package.repositories, tuple)
    for item in package.repositories:
        if type(item) is not RepositoryApprovalEvidence:
            raise _ApprovalFailure("Approval package has invalid field types")
        _require_exact(item.repository_key, str)
        _validate_repository_types(item.mapping)
        for field in (
            "base_commit", "head_commit", "diff_hash", "diff_summary", "branch",
            "commit_message", "pr_title", "pr_body",
        ):
            _require_exact(getattr(item, field), str)
        _require_string_tuple(item.changed_files)
        _require_exact(item.tests, tuple)
        for result in item.tests:
            _validate_command_types(result)
    _require_exact(package.integration_tests, tuple)
    for result in package.integration_tests:
        _validate_command_types(result)
    _require_exact(package.wiki_snapshots, tuple)
    for snapshot in package.wiki_snapshots:
        _validate_wiki_types(snapshot)
    _require_exact(package.tests, tuple)
    for result in package.tests:
        _validate_command_types(result)
    _require_exact(package.pre_fix_tests, tuple)
    for result in package.pre_fix_tests:
        _validate_command_types(result)
    _require_exact(package.root_cause_evidence, tuple)
    for evidence in package.root_cause_evidence:
        if type(evidence) is not RootCauseEvidence:
            raise _ApprovalFailure("Approval package has invalid field types")
    if package.approved_by is not None:
        _require_exact(package.approved_by, str)
    if package.approved_at is not None:
        _require_exact(package.approved_at, datetime)


def _normalized_package(package: ApprovalPackage) -> ApprovalPackage:
    """Rebuild an untrusted model so in-place mutations cannot bypass validation."""

    def normalize() -> ApprovalPackage:
        _validate_runtime_types(package)
        raw = package.model_dump(mode="python", round_trip=True, warnings="none")
        return ApprovalPackage.model_validate(raw)

    return _sanitized(normalize)


def _validate_json_tree(value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _ApprovalFailure("Approval package is not canonical JSON")
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise _ApprovalFailure("Approval package is not canonical JSON") from None
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_tree(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _ApprovalFailure("Approval package is not canonical JSON")
            _validate_json_tree(key)
            _validate_json_tree(item)
        return
    raise _ApprovalFailure("Approval package is not canonical JSON")


def _canonical_bytes(package: ApprovalPackage) -> bytes:
    normalized = _normalized_package(package)

    def encode() -> bytes:
        payload = normalized.model_dump(mode="json", exclude=_EXCLUDED_FIELDS)
        # Preserve fingerprints of legacy approvals that did not need node verification.
        if not payload.get("verification_records"):
            payload.pop("verification_records", None)
        if not payload.get("deferred_verification"):
            payload.pop("deferred_verification", None)
        if not payload.get("baseline_evidence_missing"):
            payload.pop("baseline_evidence_missing", None)
        _validate_json_tree(payload)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return canonical.encode("utf-8", errors="strict")

    return _sanitized(encode)


def approval_fingerprint(package: ApprovalPackage) -> str:
    """Return the canonical SHA-256 fingerprint for approval-relevant evidence."""

    return hashlib.sha256(_canonical_bytes(package)).hexdigest()


def _validate_group_package(
    package: ApprovalPackage, *, defect: bool
) -> ApprovalPackage:
    group = package.repository_group
    if group is None or not package.repositories:
        raise _ApprovalFailure("Approval package is not ready for approval")
    if any(
        (
            package.repo_url,
            package.base_branch,
            package.base_commit,
            package.head_commit,
            package.diff_hash,
            package.diff_summary,
            package.branch,
            package.changed_files,
            package.tests,
            package.commit_message,
            package.pr_title,
            package.pr_body,
        )
    ):
        raise _ApprovalFailure("Approval package is not ready for approval")
    if (
        package.unrelated_changes_checked is not True
        or not package.review
        or package.unresolved_items
        or not any(item.changed_files for item in package.repositories)
    ):
        raise _ApprovalFailure("Approval package is not ready for approval")
    reproduction_binding: tuple[str, str, str] | None = None
    if defect:
        bindings = {
            (
                item.reproduction_file.repository_key,
                item.reproduction_command,
                item.test_selector,
            )
            for item in package.root_cause_evidence
            if item.reproduction_file is not None
        }
        if (
            len(bindings) != 1
            or len(bindings) != len({
                (item.reproduction_command, item.test_selector)
                for item in package.root_cause_evidence
            })
            or not package.behavior_before.strip()
            or not package.behavior_after.strip()
            or not package.impact_scope
            or package.risk_level not in {"low", "medium", "high"}
            or (not package.baseline_evidence_missing and (len(package.pre_fix_tests) != 1
                or package.pre_fix_tests[0].outcome.value != "test_failed"))
            or re.fullmatch(r"[0-9a-f]{64}", package.reproduction_test_sha256)
            is None
        ):
            raise _ApprovalFailure("Approval package is not ready for approval")
        reproduction_binding = next(iter(bindings))

    by_key = {item.repository_key: item for item in package.repositories}
    for key in group.topological_keys():
        item = by_key[key]
        configured = (
            *item.mapping.lint_commands,
            *item.mapping.build_commands,
            *item.mapping.test_commands,
        )
        expected_argv = tuple(parse_command_argv(command) for command in configured)
        actual = item.tests
        if defect and reproduction_binding is not None and key == reproduction_binding[0]:
            focused = defect_reproduction_argv(package.root_cause_evidence, item.mapping)
            if not package.baseline_evidence_missing and (package.pre_fix_tests[0].argv != focused
                or package.pre_fix_tests[0].exit_code == 0):
                raise _ApprovalFailure("Approval package is not ready for approval")
            expected_argv = (
                *defect_verification_prefix(focused, item.changed_files),
                *expected_argv,
            )
        if defect and item.changed_files and not expected_argv:
            raise _ApprovalFailure("Approval package is not ready for approval")
        if (
            len(actual) != len(expected_argv)
            or tuple(result.argv for result in actual) != expected_argv
            or any(
                result.exit_code != 0
                or result.outcome.value != "passed"
                or not result.command.strip()
                or not result.summary.strip()
                for result in actual
            )
        ):
            raise _ApprovalFailure("Approval package is not ready for approval")
    integration_argv = tuple(
        parse_command_argv(command) for command in group.integration_test_commands
    )
    if (
        len(package.integration_tests) != len(integration_argv)
        or tuple(result.argv for result in package.integration_tests)
        != integration_argv
        or any(
            result.exit_code != 0 or result.outcome.value != "passed"
            for result in package.integration_tests
        )
    ):
        raise _ApprovalFailure("Approval package is not ready for approval")
    return package


def validate_for_approval(package: ApprovalPackage) -> ApprovalPackage:
    """Validate that a canonical package contains every approval-gate input."""

    normalized = _normalized_package(package)

    def validate() -> ApprovalPackage:
        if any(record.status != "passed" for record in normalized.verification_records):
            raise _ApprovalFailure("Approval package is not ready for approval")
        deferred = normalized.deferred_verification
        if normalized.baseline_evidence_missing:
            from .verification_models import MISSING_BASELINE_DESCRIPTION
            if (normalized.pre_fix_tests or not normalized.root_cause_evidence
                    or not any(task.need.description == MISSING_BASELINE_DESCRIPTION for task in deferred)):
                raise _ApprovalFailure("Missing baseline requires an explicit Draft PR verification obligation")
        if (any(task.status not in {"manual", "ready", "waiting_environment"} for task in deferred)
                or len({task.key for task in deferred}) != len(deferred)
                or {task.key for task in deferred} & {r.task_key for r in normalized.verification_records}):
            raise _ApprovalFailure("Deferred verification must contain only pending checks")
        common_required = (
            normalized.work_item_id,
            normalized.work_item_title,
            normalized.work_item_status,
        )
        singular_required = (
            normalized.repo_url, normalized.base_branch, normalized.base_commit,
            normalized.head_commit, normalized.diff_hash, normalized.diff_summary,
            normalized.branch, normalized.commit_message, normalized.pr_title,
            normalized.pr_body,
        ) if normalized.repository_group is None else ()
        required_text = (*common_required, *singular_required)
        if any(not value.strip() for value in required_text):
            raise _ApprovalFailure("Approval package is not ready for approval")
        if not normalized.source_versions or any(
            not key.strip() or not value.strip()
            for key, value in normalized.source_versions.items()
        ):
            raise _ApprovalFailure("Approval package is not ready for approval")

        if normalized.wiki_snapshots:
            expected_hashes: dict[str, str] = {}
            for snapshot in normalized.wiki_snapshots:
                if (
                    not snapshot.page_id.strip()
                    or not snapshot.source_url.strip()
                    or not snapshot.version.strip()
                    or not snapshot.updated_at.strip()
                    or re.fullmatch(r"[0-9a-f]{64}", snapshot.content_sha256) is None
                    or snapshot.page_id in expected_hashes
                ):
                    raise _ApprovalFailure(
                        "Approval package is not ready for approval"
                    )
                expected_hashes[snapshot.page_id] = snapshot.content_sha256
            if normalized.wiki_hashes != expected_hashes or not normalized.coverage:
                raise _ApprovalFailure("Approval package is not ready for approval")
            if normalized.repository_group is not None:
                return _validate_group_package(normalized, defect=False)
        else:
            if normalized.repository_group is not None:
                if normalized.wiki_hashes or not normalized.evidence:
                    raise _ApprovalFailure("Approval package is not ready for approval")
                return _validate_group_package(normalized, defect=True)
            reproduction_bindings = {
                (item.reproduction_command, item.test_selector)
                for item in normalized.root_cause_evidence
            }
            if len(reproduction_bindings) == 1:
                base_command, selector = next(iter(reproduction_bindings))
            if normalized.wiki_hashes or not normalized.evidence:
                raise _ApprovalFailure("Approval package is not ready for approval")
            evidence_paths = {
                item.file_path for item in normalized.root_cause_evidence
            }
            if (
                not normalized.root_cause_evidence
                or not normalized.behavior_before.strip()
                or not normalized.behavior_after.strip()
                or not normalized.impact_scope
                or not evidence_paths.issubset(set(normalized.impact_scope))
                or normalized.risk_level not in {"low", "medium", "high"}
                or len(reproduction_bindings) != 1
                or normalized.repository is None
                or not normalized.reproduction_command.strip()
                or re.fullmatch(r"[0-9a-f]{64}", normalized.reproduction_test_sha256) is None
                or (not normalized.baseline_evidence_missing and (len(normalized.pre_fix_tests) != 1
                    or normalized.pre_fix_tests[0].exit_code == 0
                    or normalized.pre_fix_tests[0].outcome.value != "test_failed"))
                or not normalized.tests
                or normalized.tests[0].exit_code != 0
                or any(item.outcome.value != "passed" for item in normalized.tests)
                or any(
                    not item.command.strip() or not item.summary.strip()
                    for item in normalized.pre_fix_tests
                )
            ):
                raise _ApprovalFailure("Approval package is not ready for approval")

            assert normalized.repository is not None
            focused_argv = defect_reproduction_argv(normalized.root_cause_evidence, normalized.repository)
            expected_argv = (
                *defect_verification_prefix(focused_argv, normalized.changed_files),
                *(parse_command_argv(command) for command in normalized.repository.lint_commands),
                *(parse_command_argv(command) for command in normalized.repository.build_commands),
                *(parse_command_argv(command) for command in normalized.repository.test_commands),
            )
            if (
                len(expected_argv) != len(set(expected_argv))
                or any(not item.argv for item in (*normalized.pre_fix_tests, *normalized.tests))
                or (not normalized.baseline_evidence_missing and normalized.pre_fix_tests[0].argv != expected_argv[0])
                or tuple(item.argv for item in normalized.tests) != expected_argv
            ):
                raise _ApprovalFailure("Approval package is not ready for approval")

        repository = normalized.repository
        if (
            repository is None
            or repository.repo_url != normalized.repo_url
            or repository.base_branch != normalized.base_branch
        ):
            raise _ApprovalFailure("Approval package is not ready for approval")
        if (
            re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized.base_commit)
            is None
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized.head_commit)
            is None
            or re.fullmatch(r"[0-9a-f]{64}", normalized.diff_hash) is None
        ):
            raise _ApprovalFailure("Approval package is not ready for approval")
        validate_git_ref_name(normalized.branch)
        if (
            not normalized.changed_files
            or any(not path.strip() for path in normalized.changed_files)
            or not normalized.diff_summary.strip()
            or normalized.unrelated_changes_checked is not True
        ):
            raise _ApprovalFailure("Approval package is not ready for approval")
        if normalized.root_cause_evidence and not set(
            normalized.changed_files
        ).issubset(set(normalized.impact_scope)):
            raise _ApprovalFailure("Approval package is not ready for approval")
        if any(
            not key.strip() or not value.strip()
            for key, value in normalized.coverage.items()
        ) or any(not item.strip() for item in normalized.evidence):
            raise _ApprovalFailure("Approval package is not ready for approval")
        if (
            not normalized.tests
            or any(
                result.exit_code != 0
                or not result.command.strip()
                or not result.summary.strip()
                for result in normalized.tests
            )
            or normalized.unresolved_items
        ):
            raise _ApprovalFailure("Approval package is not ready for approval")
        return normalized

    return _sanitized(validate)


def issue_approval(
    package: ApprovalPackage,
    *,
    approved_by: str,
    approved_at: datetime | None = None,
) -> ApprovalPackage:
    """Create a validated approval record, ignoring any pre-existing fingerprint."""

    normalized = validate_for_approval(package)

    def validate_actor() -> str:
        if type(approved_by) is not str or not approved_by.strip():
            raise _ApprovalFailure("Approver identity is invalid")
        try:
            approved_by.encode("utf-8", errors="strict")
        except UnicodeError:
            raise _ApprovalFailure("Approver identity is invalid") from None
        if any(
            ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
            for character in approved_by
        ):
            raise _ApprovalFailure("Approver identity is invalid")
        return approved_by

    actor = _sanitized(validate_actor)
    timestamp = utc_now() if approved_at is None else approved_at

    def sign() -> ApprovalPackage:
        if (
            type(timestamp) is not datetime
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise _ApprovalFailure("Approval timestamp is invalid")
        return normalized.model_copy(
            update={
                "fingerprint": approval_fingerprint(normalized),
                "approved_by": actor,
                "approved_at": timestamp.astimezone(UTC),
            }
        )

    return _sanitized(sign)


def verify_approval(expected: str, current: ApprovalPackage) -> None:
    """Require current evidence to match an externally stored approved fingerprint."""

    if type(expected) is not str or _FINGERPRINT_PATTERN.fullmatch(expected) is None:
        raise ApprovalInvalidatedError("Approved fingerprint is invalid")
    actual = approval_fingerprint(current)
    if not hmac.compare_digest(expected, actual):
        raise ApprovalInvalidatedError(
            "Source, repository, tests, risks, or publication content changed"
        )


__all__ = [
    "ApprovalError",
    "ApprovalInvalidatedError",
    "ApprovalValidationError",
    "approval_fingerprint",
    "collect_defect_risks",
    "issue_approval",
    "validate_for_approval",
    "verify_approval",
]
