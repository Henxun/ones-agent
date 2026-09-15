from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from src.developer_workflow.contracts import (
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    WorkflowType,
)
from src.developer_workflow.repository import MirrorOriginMismatch, WorktreeRepository
from src.developer_workflow.repository_group import (
    RepositoryGroupError,
    RepositoryGroupWorkspace,
    RepositoryPreparationProgress,
    preparation_activity_message,
    repository_branch,
)


def _git(*args: str, cwd: Path | None = None) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _source_and_remote(root: Path, name: str) -> tuple[Path, Path]:
    source = root / name
    remote = root / f"{name}.git"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "Group Test", cwd=source)
    _git("config", "user.email", "group@example.invalid", cwd=source)
    (source / "src").mkdir()
    (source / "src" / "base.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")
    _git("add", "src/base.py", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    _git("clone", "--bare", str(source), str(remote), cwd=root)
    _git("remote", "add", "origin", str(remote), cwd=source)
    return source, remote


def _mapping(
    key: str,
    source: Path,
    remote: Path,
    *,
    role: RepositoryRole,
    depends_on: tuple[str, ...] = (),
) -> RepositoryMapping:
    return RepositoryMapping(
        key=key,
        project_id="project",
        iteration_id="iteration",
        repo_url=str(remote.resolve()),
        repo_name=key,
        source_path=source.resolve(),
        role=role,
        depends_on=depends_on,
        allowed_paths=("src", "tests"),
        test_commands=("uv run pytest",),
    )


@pytest.fixture
def repository_group(tmp_path: Path) -> tuple[RepositoryGroupMapping, dict[str, Path]]:
    sdk_source, sdk_remote = _source_and_remote(tmp_path, "shared-sdk")
    app_source, app_remote = _source_and_remote(tmp_path, "desktop-app")
    group = RepositoryGroupMapping(
        key="desktop-suite",
        project_id="project",
        iteration_id="iteration",
        primary_repository="desktop-app",
        repositories=(
            _mapping(
                "shared-sdk",
                sdk_source,
                sdk_remote,
                role=RepositoryRole.DEPENDENCY,
            ),
            _mapping(
                "desktop-app",
                app_source,
                app_remote,
                role=RepositoryRole.PRIMARY,
                depends_on=("shared-sdk",),
            ),
        ),
        integration_test_commands=("uv run pytest tests/integration",),
    )
    return group, {"shared-sdk": sdk_source, "desktop-app": app_source}


def _source_facts(source: Path) -> tuple[str, str, str]:
    return (
        _git("rev-parse", "HEAD", cwd=source),
        _git("status", "--porcelain=v1", "--untracked-files=all", cwd=source),
        (source / "src" / "base.py").read_text(encoding="utf-8"),
    )


def test_group_prepare_uses_local_sources_without_mutating_them(
    tmp_path: Path,
    repository_group: tuple[RepositoryGroupMapping, dict[str, Path]],
) -> None:
    group, sources = repository_group
    before = {key: _source_facts(source) for key, source in sources.items()}
    workspace = RepositoryGroupWorkspace(
        WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")
    )

    prepared = workspace.prepare_group(
        "run-1", group, WorkflowType.DEFECT, "DEF-1", "shortcut lifecycle"
    )

    assert tuple(item.repository_key for item in prepared) == (
        "shared-sdk",
        "desktop-app",
    )
    assert prepared[0].prepared.path.parent == prepared[1].prepared.path.parent
    assert prepared[0].prepared.path.name == "shared-sdk"
    assert prepared[1].prepared.path.name == "desktop-app"
    assert {key: _source_facts(source) for key, source in sources.items()} == before


def test_group_prepare_reports_each_repository_milestone(
    tmp_path: Path,
    repository_group: tuple[RepositoryGroupMapping, dict[str, Path]],
) -> None:
    group, _ = repository_group
    workspace = RepositoryGroupWorkspace(
        WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")
    )
    progress: list[RepositoryPreparationProgress] = []

    workspace.prepare_group(
        "run-progress",
        group,
        WorkflowType.DEFECT,
        "DEF-1",
        "visible preparation",
        progress=progress.append,
    )

    assert [
        (item.repository_key, item.index, item.total, item.stage)
        for item in progress
    ] == [
        ("shared-sdk", 1, 2, "checking"),
        ("shared-sdk", 1, 2, "preparing"),
        ("shared-sdk", 1, 2, "ready"),
        ("desktop-app", 2, 2, "checking"),
        ("desktop-app", 2, 2, "preparing"),
        ("desktop-app", 2, 2, "ready"),
    ]
    assert preparation_activity_message(progress[0]) == (
        "Repository setup 1/2 · shared-sdk · checking existing isolated worktree"
    )
    assert preparation_activity_message(progress[-1]).startswith(
        "Repository setup 2/2 · desktop-app · ready ("
    )


def test_group_prepare_recovers_exact_existing_worktrees(
    tmp_path: Path,
    repository_group: tuple[RepositoryGroupMapping, dict[str, Path]],
) -> None:
    group, _ = repository_group
    workspace = RepositoryGroupWorkspace(
        WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")
    )
    first = workspace.prepare_group(
        "run-1", group, WorkflowType.REQUIREMENT, "REQ-1", "multi repository"
    )

    second = workspace.prepare_group(
        "run-1", group, WorkflowType.REQUIREMENT, "REQ-1", "multi repository"
    )

    assert second == first


def test_group_approval_trees_are_deterministic_and_do_not_touch_real_index(
    tmp_path: Path,
    repository_group: tuple[RepositoryGroupMapping, dict[str, Path]],
) -> None:
    group, _ = repository_group
    workspace = RepositoryGroupWorkspace(
        WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")
    )
    prepared = workspace.prepare_group(
        "run-tree", group, WorkflowType.REQUIREMENT, "REQ-1", "signed trees"
    )
    for item in prepared:
        (item.prepared.path / "src" / "base.py").write_text(
            f"NAME = {item.repository_key!r}\nCHANGED = True\n", encoding="utf-8"
        )
    snapshots = workspace.snapshots(prepared)
    messages = {
        item.repository_key: f"feat({item.repository_key}): signed tree"
        for item in prepared
    }
    before_status = {
        item.repository_key: _git("status", "--porcelain=v1", cwd=item.prepared.path)
        for item in prepared
    }

    first = workspace.approval_trees(prepared, snapshots, messages)
    second = workspace.approval_trees(prepared, snapshots, messages)

    assert first == second
    assert tuple(first) == group.topological_keys()
    assert all(len(value) == 40 for value in first.values())
    assert {
        item.repository_key: _git("status", "--porcelain=v1", cwd=item.prepared.path)
        for item in prepared
    } == before_status


def test_group_resolve_path_rejects_cross_repository_and_disallowed_path(
    tmp_path: Path,
    repository_group: tuple[RepositoryGroupMapping, dict[str, Path]],
) -> None:
    group, _ = repository_group
    workspace = RepositoryGroupWorkspace(
        WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")
    )
    prepared = workspace.prepare_group(
        "run-1", group, WorkflowType.DEFECT, "DEF-1", "safe paths"
    )

    assert workspace.resolve_path(prepared, "desktop-app", "src/base.py").name == "base.py"
    with pytest.raises(RepositoryGroupError, match="unsafe repository-qualified path"):
        workspace.resolve_path(prepared, "desktop-app", "../shared-sdk/src/base.py")
    with pytest.raises(RepositoryGroupError, match="unknown repository"):
        workspace.resolve_path(prepared, "other", "src/base.py")
    with pytest.raises(RepositoryGroupError, match="outside allowed paths"):
        workspace.resolve_path(prepared, "desktop-app", "README.md")


def test_local_source_origin_must_match_authoritative_remote(
    tmp_path: Path,
    repository_group: tuple[RepositoryGroupMapping, dict[str, Path]],
) -> None:
    group, sources = repository_group
    other = tmp_path / "other.git"
    _git("init", "--bare", str(other), cwd=tmp_path)
    _git("remote", "set-url", "origin", str(other), cwd=sources["shared-sdk"])
    workspace = RepositoryGroupWorkspace(
        WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")
    )

    with pytest.raises(MirrorOriginMismatch, match="local source origin"):
        workspace.prepare_group(
            "run-1", group, WorkflowType.DEFECT, "DEF-1", "identity mismatch"
        )


def test_repository_branch_is_unique_per_repository_and_bounded() -> None:
    first = repository_branch(
        WorkflowType.DEFECT, "DEF-1", "x" * 300, "shared-sdk", "a" * 32
    )
    second = repository_branch(
        WorkflowType.DEFECT, "DEF-1", "x" * 300, "desktop-app", "a" * 32
    )
    next_run = repository_branch(
        WorkflowType.DEFECT, "DEF-1", "x" * 300, "shared-sdk", "b" * 32
    )

    assert first.startswith("bugfix/DEF-1-")
    assert second.startswith("bugfix/DEF-1-")
    assert first != second
    assert first != next_run
    assert first.endswith("-shared-sdk")
    assert second.endswith("-desktop-app")
    assert len(first) <= 120
