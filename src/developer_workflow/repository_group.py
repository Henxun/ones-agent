"""Composition boundary for deterministic multi-repository workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Callable, Literal

from .contracts import (
    ApprovalPackage,
    PreparedWorktree,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositorySnapshot,
    WorkflowRun,
    WorkflowType,
    validate_git_ref_name,
)
from .repository import (
    RepositoryBoundaryError,
    WorktreeRepository,
    build_run_branch_name,
)


class RepositoryGroupError(RuntimeError):
    """A repository group cannot be prepared or addressed safely."""


@dataclass(frozen=True, slots=True)
class PreparedRepository:
    repository_key: str
    mapping: RepositoryMapping
    prepared: PreparedWorktree


@dataclass(frozen=True, slots=True)
class RepositoryPreparationProgress:
    """One bounded, display-safe repository preparation milestone."""

    repository_key: str
    index: int
    total: int
    stage: Literal["checking", "preparing", "ready"]
    elapsed_seconds: float = 0.0


RepositoryPreparationSink = Callable[[RepositoryPreparationProgress], None]


def preparation_activity_message(progress: RepositoryPreparationProgress) -> str:
    """Render a compact activity line shared by defect and requirement flows."""

    prefix = (
        f"Repository setup {progress.index}/{progress.total} · "
        f"{progress.repository_key} · "
    )
    if progress.stage == "checking":
        return prefix + "checking existing isolated worktree"
    if progress.stage == "preparing":
        return prefix + "syncing mirror and creating worktree"
    return prefix + f"ready ({progress.elapsed_seconds:.1f}s)"


def repository_branch(
    workflow_type: WorkflowType | str,
    work_item_id: str,
    title: str,
    repository_key: str,
    run_id: str,
) -> str:
    """Return one stable, bounded branch name per repository."""

    if not repository_key or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for character in repository_key
    ):
        raise RepositoryGroupError("repository key is unsafe for branch construction")
    try:
        return build_run_branch_name(
            workflow_type,
            work_item_id,
            title,
            run_id,
            repository_key=repository_key,
        )
    except ValueError:
        raise RepositoryGroupError("repository branch could not be constructed safely") from None


@dataclass(slots=True)
class RepositoryGroupWorkspace:
    repository: WorktreeRepository

    def prepare_group(
        self,
        run_id: str,
        group: RepositoryGroupMapping,
        workflow_type: WorkflowType | str,
        work_item_id: str,
        title: str,
        *,
        progress: RepositoryPreparationSink | None = None,
    ) -> tuple[PreparedRepository, ...]:
        mappings = {item.key: item for item in group.repositories}
        prepared: list[PreparedRepository] = []
        keys = group.topological_keys()
        total = len(keys)
        for index, key in enumerate(keys, 1):
            started = perf_counter()
            if progress is not None:
                progress(RepositoryPreparationProgress(key, index, total, "checking"))
            mapping = mappings[key]
            branch = repository_branch(
                workflow_type, work_item_id, title, key, run_id
            )
            worktree = self.repository.recover(
                run_id, mapping, branch, repository_key=key
            )
            if worktree is None:
                if progress is not None:
                    progress(
                        RepositoryPreparationProgress(
                            key, index, total, "preparing"
                        )
                    )
                worktree = self.repository.prepare(
                    run_id, mapping, branch, repository_key=key
                )
            prepared.append(PreparedRepository(key, mapping, worktree))
            if progress is not None:
                progress(
                    RepositoryPreparationProgress(
                        key,
                        index,
                        total,
                        "ready",
                        max(0.0, perf_counter() - started),
                    )
                )
        result = tuple(prepared)
        self._assert_sibling_layout(result)
        return result

    @staticmethod
    def _assert_sibling_layout(prepared: tuple[PreparedRepository, ...]) -> None:
        if not prepared:
            raise RepositoryGroupError("repository group has no prepared repositories")
        parents = {item.prepared.path.parent.resolve(strict=True) for item in prepared}
        if len(parents) != 1:
            raise RepositoryGroupError("repository worktrees do not share one workspace")
        for item in prepared:
            if item.prepared.path.name != item.repository_key:
                raise RepositoryGroupError("repository worktree directory is not deterministic")

    def resolve_path(
        self,
        prepared: tuple[PreparedRepository, ...],
        repository_key: str,
        repository_path: str,
    ) -> Path:
        item = next(
            (candidate for candidate in prepared if candidate.repository_key == repository_key),
            None,
        )
        if item is None:
            raise RepositoryGroupError("unknown repository in repository-qualified path")
        try:
            RepositorySnapshot._validate_repository_path(repository_path)
        except ValueError:
            raise RepositoryGroupError("unsafe repository-qualified path") from None
        if not WorktreeRepository._allowed(repository_path, item.mapping.allowed_paths):
            raise RepositoryGroupError("repository-qualified path is outside allowed paths")
        try:
            return self.repository.resolve_repository_path(
                item.prepared, item.mapping, repository_path
            )
        except RepositoryBoundaryError:
            raise RepositoryGroupError("unsafe repository-qualified path") from None

    def snapshots(
        self, prepared: tuple[PreparedRepository, ...]
    ) -> dict[str, RepositorySnapshot]:
        return {
            item.repository_key: self.repository.snapshot(item.prepared, item.mapping)
            for item in prepared
        }

    def assert_heads_unchanged(
        self, prepared: tuple[PreparedRepository, ...]
    ) -> None:
        for item in prepared:
            self.repository.assert_head_unchanged(item.prepared)

    def approval_trees(
        self,
        prepared: tuple[PreparedRepository, ...],
        snapshots: dict[str, RepositorySnapshot],
        commit_messages: dict[str, str],
    ) -> dict[str, str]:
        """Compute deterministic trees before approval so they enter its signature."""

        keys = tuple(item.repository_key for item in prepared)
        if tuple(snapshots) != keys or tuple(commit_messages) != keys:
            raise RepositoryGroupError("approval tree inputs do not follow topology")
        result: dict[str, str] = {}
        for item in prepared:
            snapshot = snapshots[item.repository_key]
            message = commit_messages[item.repository_key]
            if not snapshot.changed_files:
                if message:
                    raise RepositoryGroupError("unchanged repository cannot publish")
                result[item.repository_key] = ""
                continue
            approval = ApprovalPackage(
                work_item_id="repository-tree-intent",
                repository=item.mapping,
                repo_url=item.mapping.repo_url,
                base_branch=item.mapping.base_branch,
                base_commit=item.prepared.base_commit,
                head_commit=snapshot.head_commit,
                diff_hash=snapshot.diff_sha256,
                diff_summary=(
                    f"changed {len(snapshot.changed_files)} file(s): "
                    f"{', '.join(snapshot.changed_files)}"
                ),
                branch=item.prepared.branch,
                changed_files=snapshot.changed_files,
                commit_message=message,
            )
            projection = WorkflowRun.new("requirement", "repository-tree-intent").validated_update(
                repository=item.mapping,
                prepared_worktree=item.prepared,
                tested_snapshot=snapshot,
                approval=approval,
            )
            result[item.repository_key] = self.repository.prepare_commit_intent(
                projection, approval
            )
        return result


__all__ = [
    "PreparedRepository",
    "RepositoryPreparationProgress",
    "RepositoryPreparationSink",
    "preparation_activity_message",
    "RepositoryGroupError",
    "RepositoryGroupWorkspace",
    "repository_branch",
]
