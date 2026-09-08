"""Shared deterministic evidence rules for repository-group workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .contracts import (
    CodingAgentResult,
    CommandOutcome,
    CommandResult,
    RepositoryGroupMapping,
    RepositoryRunEvidence,
    RepositorySnapshot,
)
from .repository_group import PreparedRepository
from .command_utils import parse_command_argv


class GroupEvidenceError(RuntimeError):
    """Repository-group evidence is incomplete or inconsistent."""


class GroupCommandRunner(Protocol):
    def run(self, command: str, *, cwd: Path) -> CommandResult:
        raise NotImplementedError


def aggregate_claims(result: CodingAgentResult) -> dict[str, tuple[str, ...]]:
    """Group repository-qualified claims without losing model output order."""

    grouped: dict[str, list[str]] = {}
    for claim in result.repository_changes:
        grouped.setdefault(claim.repository_key, []).append(claim.path)
    return {key: tuple(paths) for key, paths in grouped.items()}


def assert_group_snapshots_equal(
    expected: tuple[RepositoryRunEvidence, ...],
    actual: dict[str, RepositorySnapshot],
    group: RepositoryGroupMapping,
) -> None:
    """Require every live repository snapshot to equal its tested evidence."""

    expected_keys = group.topological_keys()
    if tuple(item.repository_key for item in expected) != expected_keys:
        raise GroupEvidenceError("repository group evidence does not follow topology")
    if tuple(actual) != expected_keys:
        raise GroupEvidenceError("repository group snapshots do not follow topology")
    expected_by_key = {
        item.repository_key: item.tested_snapshot for item in expected
    }
    if any(snapshot is None for snapshot in expected_by_key.values()) or any(
        expected_by_key[key] != actual[key] for key in expected_keys
    ):
        raise GroupEvidenceError("repository group differs from tested evidence")


def assert_group_claims(
    result: CodingAgentResult,
    snapshots: dict[str, RepositorySnapshot],
    group: RepositoryGroupMapping,
) -> None:
    expected = tuple(
        (repository_key, path)
        for repository_key in group.topological_keys()
        for path in snapshots[repository_key].changed_files
    )
    claimed = tuple(
        (item.repository_key, item.path) for item in result.repository_changes
    )
    if len(claimed) != len(expected) or set(claimed) != set(expected):
        raise GroupEvidenceError("repository change claims do not match group snapshots")


def run_group_commands(
    group: RepositoryGroupMapping,
    prepared: tuple[PreparedRepository, ...],
    runner: GroupCommandRunner,
) -> tuple[tuple[tuple[str, CommandResult], ...], tuple[CommandResult, ...]]:
    prepared_by_key = {item.repository_key: item for item in prepared}
    if tuple(prepared_by_key) != group.topological_keys():
        raise GroupEvidenceError("prepared repositories do not follow group topology")
    repository_results: list[tuple[str, CommandResult]] = []
    for key in group.topological_keys():
        item = prepared_by_key[key]
        commands = (
            *item.mapping.lint_commands,
            *item.mapping.build_commands,
            *item.mapping.test_commands,
        )
        for command in commands:
            result = runner.run(command, cwd=item.prepared.path)
            if result.command != command or result.argv != parse_command_argv(command):
                raise GroupEvidenceError("test runner substituted a configured command")
            repository_results.append((key, result))
            if (
                result.exit_code != 0
                or result.outcome is not CommandOutcome.PASSED
            ):
                return tuple(repository_results), ()
    primary = prepared_by_key[group.primary_repository]
    integration_results: list[CommandResult] = []
    for command in group.integration_test_commands:
        result = runner.run(command, cwd=primary.prepared.path)
        if result.command != command or result.argv != parse_command_argv(command):
            raise GroupEvidenceError("test runner substituted an integration command")
        integration_results.append(result)
        if result.exit_code != 0 or result.outcome is not CommandOutcome.PASSED:
            break
    return tuple(repository_results), tuple(integration_results)


def assert_group_commands_passed(
    repository_results: tuple[tuple[str, CommandResult], ...],
    integration_results: tuple[CommandResult, ...],
) -> None:
    results = (
        *(result for _, result in repository_results),
        *integration_results,
    )
    if not results or any(
        result.exit_code != 0 or result.outcome is not CommandOutcome.PASSED
        for result in results
    ):
        raise GroupEvidenceError("configured repository group command did not pass")


__all__ = [
    "GroupCommandRunner",
    "GroupEvidenceError",
    "aggregate_claims",
    "assert_group_commands_passed",
    "assert_group_claims",
    "assert_group_snapshots_equal",
    "run_group_commands",
]
