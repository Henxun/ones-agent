from __future__ import annotations

import os
import hashlib
import io
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.developer_workflow.contracts import (
    ApprovalPackage,
    PublicationResult,
    PreparedWorktree,
    RepositoryMapping,
    RepositorySnapshot,
    WorkflowRun,
)
from src.developer_workflow.repository import (
    BaseBranchNotFound,
    BranchAlreadyExists,
    HeadChangedError,
    MirrorOriginMismatch,
    RepositoryBoundaryError,
    RepositoryCommandError,
    RepositoryIdentityError,
    SnapshotTooLargeError,
    TargetExists,
    WorktreeRepository,
    build_branch_name,
    build_run_branch_name,
)


def _git(*args: str, cwd: Path | None = None) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
    }
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    return completed.stdout.strip()


@pytest.fixture
def remote_repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "Repository Test", cwd=source)
    _git("config", "user.email", "repository@example.invalid", cwd=source)
    (source / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    _git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    return source, remote


def _mapping(remote: Path, **overrides: object) -> RepositoryMapping:
    values: dict[str, object] = {
        "key": "repo",
        "project_id": "project",
        "iteration_id": "iteration",
        "repo_url": str(remote.resolve()),
        "repo_name": "sample",
        "base_branch": "main",
    }
    values.update(overrides)
    return RepositoryMapping(**values)


def _repository(tmp_path: Path) -> WorktreeRepository:
    return WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")


def _publication_run(
    prepared: PreparedWorktree,
    mapping: RepositoryMapping,
    snapshot: RepositorySnapshot,
) -> tuple[WorkflowRun, ApprovalPackage]:
    approval = ApprovalPackage(
        work_item_id="REQ-1", repository=mapping, repo_url=mapping.repo_url,
        base_branch=mapping.base_branch, base_commit=prepared.base_commit,
        head_commit=prepared.head_commit, diff_hash=snapshot.diff_sha256,
        branch=prepared.branch, changed_files=snapshot.changed_files,
        commit_message="feat: approved change", pr_title="approved", pr_body="approved",
    )
    run = WorkflowRun.new("requirement", "REQ-1").validated_update(
        repository=mapping, prepared_worktree=prepared, tested_snapshot=snapshot,
        branch=prepared.branch, head_commit=prepared.head_commit,
        changed_files=snapshot.changed_files, approval=approval,
    )
    return run, approval


def test_approved_commit_and_push_use_exact_snapshot_and_recover_fact(
    tmp_path: Path,
    remote_repository: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, remote = remote_repository
    repository = _repository(tmp_path)
    repository.identity_env_provider = lambda: {
        "GIT_AUTHOR_NAME": "Publisher Test",
        "GIT_AUTHOR_EMAIL": "publisher@example.invalid",
        "GIT_COMMITTER_NAME": "Publisher Test",
        "GIT_COMMITTER_EMAIL": "publisher@example.invalid",
    }
    mapping = _mapping(remote, allowed_paths=("src",))
    prepared = repository.prepare("publish-run", mapping, "requirement/REQ-1-publish")
    (prepared.path / "src").mkdir()
    (prepared.path / "src" / "a.py").write_text("print('approved')\n", encoding="utf-8")
    snapshot = repository.snapshot(prepared, mapping)
    run, approval = _publication_run(prepared, mapping, snapshot)
    tree = repository.prepare_commit_intent(run, approval)
    run = run.validated_update(publication=PublicationResult(
        approved_fingerprint="a"*64, repo_url=mapping.repo_url,
        provider="github", provider_host="github.example",
        expected_parent=prepared.head_commit,
        expected_tree=tree, commit_message=approval.commit_message,
        remote_branch=prepared.branch, pr_marker="ones-dev-run:test",
        pr_base="main", pr_head=prepared.branch,
        pr_title=approval.pr_title, pr_body=approval.pr_body,
        comment_marker="marker",
    ))
    commit = repository.commit_approved(run)

    assert repository.find_approved_commit(run) == commit
    assert _git("diff-tree", "--no-commit-id", "--name-only", "-r", commit, cwd=prepared.path) == "src/a.py"
    run = run.validated_update(publication=run.publication.validated_update(commit_hash=commit))
    assert repository.remote_branch_oid(run) is None
    repository.push_approved(run)
    assert repository.remote_branch_oid(run) == commit


def test_remote_base_advance_is_detected_without_fetching_or_mutating_local_state(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    repository = _repository(tmp_path)
    mapping = _mapping(remote)
    prepared = repository.prepare("base-check", mapping, "requirement/REQ-1-base-check")
    original_head = _git("rev-parse", "HEAD", cwd=prepared.path)
    original_tracking = _git(
        "--git-dir", str(prepared.mirror_path), "rev-parse", "refs/heads/main"
    )
    repository.assert_remote_base_unchanged(prepared, mapping)

    (source / "README.md").write_text("advanced\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-m", "advance base", cwd=source)
    _git("push", str(remote), "HEAD:refs/heads/main", cwd=source)

    with pytest.raises(RepositoryIdentityError, match="remote base branch changed"):
        repository.assert_remote_base_unchanged(prepared, mapping)
    assert _git("rev-parse", "HEAD", cwd=prepared.path) == original_head
    assert _git(
        "--git-dir", str(prepared.mirror_path), "rev-parse", "refs/heads/main"
    ) == original_tracking


def test_ambient_git_redirection_variables_cannot_escape_target_repository(
    tmp_path: Path,
    remote_repository: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, remote = remote_repository
    poison = tmp_path / "poison"
    poison.mkdir()
    ambient_home = tmp_path / "ambient-home"
    (ambient_home / ".ssh").mkdir(parents=True)
    (ambient_home / ".ssh" / "id_ed25519").write_text("fake-private-key", encoding="utf-8")
    (ambient_home / ".gitconfig").write_text("[core]\nworktree = poison\n", encoding="utf-8")
    fake_askpass = tmp_path / "ambient-askpass.cmd"
    fake_askpass.write_text("exit /b 99\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(ambient_home))
    monkeypatch.setenv("USERPROFILE", str(ambient_home))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "fake-agent.sock"))
    monkeypatch.setenv("SSH_ASKPASS", str(fake_askpass))
    monkeypatch.setenv("GIT_ASKPASS", str(fake_askpass))
    poisoned = {
        "GIT_DIR": str(poison / "evil.git"),
        "GIT_WORK_TREE": str(poison),
        "GIT_COMMON_DIR": str(poison),
        "GIT_OBJECT_DIRECTORY": str(poison / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(poison / "alternate"),
        "GIT_INDEX_FILE": str(poison / "index"),
        "GIT_CONFIG_COUNT": "99",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    repository = _repository(tmp_path)
    mapping = _mapping(remote)
    prepared = repository.prepare("env-isolation", mapping, "requirement/REQ-env")
    isolated = repository._git_environment()
    assert Path(isolated["HOME"]) == repository._controlled_git_home
    assert isolated["GIT_TERMINAL_PROMPT"] == "0"
    assert isolated["GCM_INTERACTIVE"] == "Never"
    assert "SSH_AUTH_SOCK" not in isolated and "SSH_ASKPASS" not in isolated
    assert "GIT_ASKPASS" not in isolated
    ssh_command = shlex.split(isolated["GIT_SSH_COMMAND"])
    assert ssh_command[:3] == ["ssh", "-F", "none"]
    assert "-oProxyCommand=none" in ssh_command
    assert "-oStrictHostKeyChecking=yes" in ssh_command
    for key in poisoned:
        monkeypatch.delenv(key)
    assert _git("rev-parse", "HEAD", cwd=prepared.path) == prepared.head_commit
    assert not list(poison.iterdir())


def test_credential_provider_rejects_git_redirection_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ambient@example.invalid")
    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees",
        credential_env_provider=lambda: {"GIT_DIR": "poison"},
    )
    with pytest.raises(RepositoryBoundaryError, match="forbidden"):
        repository._git_environment()
    clean = WorktreeRepository(tmp_path / "clean-m", tmp_path / "clean-w")
    assert "GIT_AUTHOR_EMAIL" not in clean._git_environment()
    clean.identity_env_provider = lambda: {
        "GIT_AUTHOR_NAME":"Bad\nName", "GIT_AUTHOR_EMAIL":"bad",
        "GIT_COMMITTER_NAME":"Name", "GIT_COMMITTER_EMAIL":"ok@example.invalid",
    }
    with pytest.raises(RepositoryBoundaryError, match="invalid"):
        clean._git_environment()


def test_private_ssh_remote_without_provider_fails_fast_noninteractive(tmp_path: Path) -> None:
    repository = WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees")
    started = time.monotonic()
    completed = subprocess.run(
        ["git", "ls-remote", "ssh://127.0.0.1:1/private/repo.git"],
        env=repository._git_environment(), stdin=subprocess.DEVNULL,
        capture_output=True, check=False, timeout=5,
    )
    assert completed.returncode != 0
    assert time.monotonic() - started < 5
    assert b"password" not in completed.stderr.lower()


def test_sensitive_content_scan_detects_codex_auth_in_new_file(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    repository = _repository(tmp_path)
    mapping = _mapping(remote)
    prepared = repository.prepare("secret-scan", mapping, "requirement/secret-scan")
    secret = "codex-auth-written-to-new-file"
    (prepared.path / "generated.txt").write_text(
        "x" * (64 * 1024 - 5) + secret, encoding="utf-8"
    )

    assert repository.contains_sensitive_content(prepared, mapping, (secret,)) is True
    assert repository.contains_sensitive_content(prepared, mapping, ("not-present-secret",)) is False


def test_sensitive_content_scan_enforces_file_size_limit(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees",
        max_untracked_file_bytes=8, max_snapshot_bytes=16,
    )
    mapping = _mapping(remote)
    prepared = repository.prepare("large-scan", mapping, "requirement/large-scan")
    (prepared.path / "large.txt").write_bytes(b"x" * 9)

    with pytest.raises(SnapshotTooLargeError, match="size limit"):
        repository.contains_sensitive_content(prepared, mapping, ("secret-value",))


def test_sensitive_content_scan_enforces_total_size_limit(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees",
        max_untracked_file_bytes=8, max_snapshot_bytes=12,
    )
    mapping = _mapping(remote)
    prepared = repository.prepare("total-scan", mapping, "requirement/total-scan")
    (prepared.path / "one.bin").write_bytes(b"a\0bcdef")
    (prepared.path / "two.bin").write_bytes(b"g\0hijkl")

    with pytest.raises(SnapshotTooLargeError, match="size limit"):
        repository.contains_sensitive_content(prepared, mapping, ("secret-value",))


def test_readonly_nofollow_open_rejects_link(tmp_path: Path) -> None:
    from src.developer_workflow.repository import _open_readonly_nofollow

    target = tmp_path / "target.txt"
    link = tmp_path / "link.txt"
    target.write_text("outside-secret", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable in this Windows environment")

    with pytest.raises(RepositoryBoundaryError, match="safely opened"):
        descriptor = _open_readonly_nofollow(link)
        os.close(descriptor)


def test_readonly_nofollow_open_rejects_parent_link_escape(tmp_path: Path) -> None:
    from src.developer_workflow.repository import _open_readonly_nofollow

    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret", encoding="utf-8")
    linked_parent = worktree / "linked"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this Windows environment")

    with pytest.raises(RepositoryBoundaryError, match="outside worktree"):
        descriptor = _open_readonly_nofollow(
            linked_parent / "secret.txt", worktree=worktree
        )
        os.close(descriptor)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires Darwin F_GETPATH")
def test_darwin_descriptor_path_is_resolved_from_open_handle(tmp_path: Path) -> None:
    from src.developer_workflow.repository import _path_from_open_descriptor

    target = tmp_path / "opened.txt"
    target.write_text("opened", encoding="utf-8")
    descriptor = os.open(target, os.O_RDONLY)
    try:
        actual = _path_from_open_descriptor(descriptor)
    finally:
        os.close(descriptor)

    assert actual == target.resolve(strict=True)


def test_unsupported_posix_descriptor_resolution_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.developer_workflow.repository as repository_module

    target = tmp_path / "opened.txt"
    target.write_text("opened", encoding="utf-8")
    descriptor = os.open(target, os.O_RDONLY)
    monkeypatch.setattr(repository_module.sys, "platform", "unsupported-posix")
    try:
        with pytest.raises(RepositoryBoundaryError, match="could not be verified"):
            repository_module._path_from_open_descriptor(descriptor)
    finally:
        os.close(descriptor)


def test_readonly_open_uses_handle_bound_path_for_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.developer_workflow.repository as repository_module

    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    worktree.mkdir()
    outside.mkdir()
    target = worktree / "changed.txt"
    target.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(
        repository_module, "_path_from_open_descriptor", lambda _descriptor: outside
    )

    with pytest.raises(RepositoryBoundaryError, match="outside worktree"):
        descriptor = repository_module._open_readonly_nofollow(
            target, worktree=worktree
        )
        os.close(descriptor)


def test_prepare_creates_clean_isolated_worktree_from_remote_base(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    repository = _repository(tmp_path)

    prepared = repository.prepare(
        "run-001", _mapping(remote), "requirement/REQ-1-add-report"
    )

    assert prepared.path == (tmp_path / "worktrees" / "run-001").resolve()
    assert prepared.mirror_path == (tmp_path / "mirrors" / "sample.git").resolve()
    assert prepared.branch == "requirement/REQ-1-add-report"
    assert prepared.base_commit == _git("rev-parse", "main", cwd=remote)
    assert prepared.head_commit == prepared.base_commit
    assert _git("status", "--porcelain", cwd=prepared.path) == ""
    assert _git("config", "--get", "remote.origin.fetch", cwd=prepared.mirror_path) == (
        "+refs/heads/*:refs/remotes/origin/*"
    )


def test_recover_returns_only_the_exact_registered_clean_worktree(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    repository = _repository(tmp_path)
    mapping = _mapping(remote)
    branch = "requirement/REQ-1-recover"
    prepared = repository.prepare("recover-run", mapping, branch)

    recovered = repository.recover("recover-run", mapping, branch)

    assert recovered == prepared


def test_recover_rejects_registered_worktree_when_expected_base_moved(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    repository = _repository(tmp_path)
    mapping = _mapping(remote)
    branch = "requirement/REQ-1-stale"
    repository.prepare("stale-run", mapping, branch)
    (source / "new-base.txt").write_text("new base\n", encoding="utf-8")
    _git("add", "new-base.txt", cwd=source)
    _git("commit", "-m", "move base", cwd=source)
    _git("push", str(remote), "main", cwd=source)

    with pytest.raises(RepositoryIdentityError, match="recover"):
        repository.recover("stale-run", mapping, branch)


def test_recover_never_takes_over_an_unknown_deterministic_target(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    repository = _repository(tmp_path)
    mapping = _mapping(remote)
    repository.prepare("known-run", mapping, "requirement/REQ-1-known")
    unknown = tmp_path / "worktrees" / "unknown-run"
    unknown.mkdir()
    marker = unknown / "owner.txt"
    marker.write_text("external owner\n", encoding="utf-8")

    with pytest.raises(RepositoryIdentityError, match="recover"):
        repository.recover("unknown-run", mapping, "requirement/REQ-2-unknown")

    assert marker.read_text(encoding="utf-8") == "external owner\n"


def test_recover_rejects_crashed_worktree_when_remote_same_branch_appears(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    repository = _repository(tmp_path)
    mapping = _mapping(remote)
    branch = "requirement/REQ-3-remote-race"
    repository.prepare("remote-race-run", mapping, branch)
    _git("branch", branch, cwd=source)
    _git("push", str(remote), f"{branch}:{branch}", cwd=source)

    with pytest.raises(BranchAlreadyExists):
        repository.recover("remote-race-run", mapping, branch)


def test_prepare_does_not_touch_an_unrelated_dirty_checkout(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    (source / "README.md").write_text("dirty\n", encoding="utf-8")
    before = _git("status", "--porcelain", cwd=source)

    _repository(tmp_path).prepare("run-002", _mapping(remote), "bugfix/BUG-2-fix")

    assert _git("status", "--porcelain", cwd=source) == before
    assert (source / "README.md").read_text(encoding="utf-8") == "dirty\n"


def test_existing_mirror_fetches_a_new_base_commit(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    repository = _repository(tmp_path)
    first = repository.prepare("run-one", _mapping(remote), "requirement/REQ-1-first")
    (source / "next.txt").write_text("next\n", encoding="utf-8")
    _git("add", "next.txt", cwd=source)
    _git("commit", "-m", "next", cwd=source)
    _git("push", str(remote), "main", cwd=source)

    second = repository.prepare("run-two", _mapping(remote), "requirement/REQ-2-next")

    assert second.base_commit != first.base_commit
    assert second.base_commit == _git("rev-parse", "main", cwd=remote)
    assert (second.path / "next.txt").read_text(encoding="utf-8") == "next\n"


def test_prepare_rejects_missing_base_and_branch_collisions(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    repository = _repository(tmp_path)
    with pytest.raises(BaseBranchNotFound):
        repository.prepare(
            "missing", _mapping(remote, base_branch="missing"), "bugfix/BUG-1-missing"
        )

    repository.prepare("first", _mapping(remote), "bugfix/BUG-2-collision")
    with pytest.raises(BranchAlreadyExists):
        repository.prepare("second", _mapping(remote), "bugfix/BUG-2-collision")
    assert not (tmp_path / "worktrees" / "second").exists()


def test_failed_worktree_add_does_not_delete_a_racing_external_branch(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    branch = "bugfix/BUG-race-external"
    injected = False

    def racing_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal injected
        args = list(command)  # type: ignore[arg-type]
        if "branch" in args and branch in args and not injected:
            injected = True
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "branch",
                    branch,
                    "refs/remotes/origin/main",
                ],
                check=True,
                capture_output=True,
            )
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=racing_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("race", _mapping(remote), branch)

    assert _git("--git-dir", str(mirror), "show-ref", "--verify", f"refs/heads/{branch}")


def test_failed_worktree_add_cleans_only_the_owned_branch_and_reservation(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    branch = "bugfix/BUG-owned-cleanup"

    def failing_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        args = list(command)  # type: ignore[arg-type]
        if "worktree" in args and "add" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"checkout failed")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=failing_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("owned", _mapping(remote), branch)

    result = subprocess.run(
        ["git", "--git-dir", str(mirror), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert not (tmp_path / "worktrees" / "owned").exists()
    retried = _repository(tmp_path).prepare("owned", _mapping(remote), branch)
    assert retried.head_commit == retried.base_commit


def test_branch_is_not_deleted_when_worktree_occupancy_cannot_be_verified(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    branch = "bugfix/BUG-unknown-occupancy"

    def uncertain_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        args = list(command)  # type: ignore[arg-type]
        if "worktree" in args and "add" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"checkout failed")
        if "worktree" in args and "list" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"list failed")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=uncertain_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("uncertain", _mapping(remote), branch)

    assert _git("--git-dir", str(mirror), "show-ref", "--verify", f"refs/heads/{branch}")


def test_external_competing_worktree_remains_valid_after_prepare_failure(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    external = tmp_path / "external-worktree"
    branch = "bugfix/BUG-external-worktree"
    competed = False

    def competing_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal competed
        args = list(command)  # type: ignore[arg-type]
        if "worktree" in args and "add" in args and not competed:
            competed = True
            subprocess.run(
                ["git", "--git-dir", str(mirror), "worktree", "add", str(external), branch],
                check=True,
                capture_output=True,
            )
            return subprocess.CompletedProcess(args, 128, b"", b"lost race")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=competing_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("loser", _mapping(remote), branch)

    assert _git("symbolic-ref", "--quiet", "HEAD", cwd=external) == f"refs/heads/{branch}"
    assert _git("status", "--porcelain", cwd=external) == ""


def test_external_winner_on_same_reserved_target_is_never_removed(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    target = (tmp_path / "worktrees" / "same-target").resolve()
    branch = "bugfix/BUG-same-target-winner"
    competed = False

    def same_target_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal competed
        args = list(command)  # type: ignore[arg-type]
        if "worktree" in args and "add" in args and not competed:
            competed = True
            subprocess.run(
                ["git", "--git-dir", str(mirror), "worktree", "add", str(target), branch],
                check=True,
                capture_output=True,
            )
            return subprocess.CompletedProcess(args, 128, b"", b"lost race")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=same_target_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("same-target", _mapping(remote), branch)

    assert target.exists()
    assert _git("symbolic-ref", "--quiet", "HEAD", cwd=target) == f"refs/heads/{branch}"
    listed = _git("--git-dir", str(mirror), "worktree", "list", "--porcelain")
    assert str(target).replace("\\", "/") in listed.replace("\\", "/")


def test_post_checkout_failure_removes_only_matching_registered_worktree(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    branch = "bugfix/BUG-post-checkout"

    def post_checkout_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        args = list(command)  # type: ignore[arg-type]
        if "status" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"status failed")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=post_checkout_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("post-checkout", _mapping(remote), branch)

    result = subprocess.run(
        ["git", "--git-dir", str(mirror), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert not (tmp_path / "worktrees" / "post-checkout").exists()


def test_cleanup_does_not_delete_branch_force_updated_after_creation(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    old_oid = _git("rev-parse", "HEAD", cwd=source)
    (source / "next.txt").write_text("next\n", encoding="utf-8")
    _git("add", "next.txt", cwd=source)
    _git("commit", "-m", "next", cwd=source)
    _git("push", str(remote), "main", cwd=source)
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    branch = "bugfix/BUG-force-update"

    def force_update_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        args = list(command)  # type: ignore[arg-type]
        if "worktree" in args and "add" in args:
            subprocess.run(
                ["git", "--git-dir", str(mirror), "update-ref", f"refs/heads/{branch}", old_oid],
                check=True,
                capture_output=True,
            )
            return subprocess.CompletedProcess(args, 128, b"", b"checkout failed")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=force_update_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("force-update", _mapping(remote), branch)

    assert _git("--git-dir", str(mirror), "rev-parse", branch) == old_oid


def test_force_update_immediately_after_branch_command_is_not_owned(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    external_oid = _git("rev-parse", "HEAD", cwd=source)
    (source / "new-base.txt").write_text("new base\n", encoding="utf-8")
    _git("add", "new-base.txt", cwd=source)
    _git("commit", "-m", "new base", cwd=source)
    _git("push", str(remote), "main", cwd=source)
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    branch = "bugfix/BUG-immediate-force-update"

    def immediate_force_update_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        args = list(command)  # type: ignore[arg-type]
        if "branch" in args and branch in args:
            result = subprocess.run(
                args, cwd=cwd, shell=False, capture_output=True, check=False
            )
            assert result.returncode == 0
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "update-ref",
                    f"refs/heads/{branch}",
                    external_oid,
                ],
                check=True,
                capture_output=True,
            )
            return result
        if "worktree" in args and "add" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"checkout failed")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors",
        tmp_path / "worktrees",
        command_runner=immediate_force_update_runner,
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("immediate-force", _mapping(remote), branch)

    assert _git("--git-dir", str(mirror), "rev-parse", branch) == external_oid


def test_moving_remote_base_before_branch_creation_cannot_leak_owned_branch(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    source, remote = remote_repository
    old_oid = _git("rev-parse", "HEAD", cwd=source)
    (source / "new-head.txt").write_text("new head\n", encoding="utf-8")
    _git("add", "new-head.txt", cwd=source)
    _git("commit", "-m", "new head", cwd=source)
    _git("push", str(remote), "main", cwd=source)
    mirror = (tmp_path / "mirrors" / "sample.git").resolve()
    branch = "bugfix/BUG-moving-base"

    def moving_base_runner(
        command: object, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        args = list(command)  # type: ignore[arg-type]
        if "branch" in args and branch in args:
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "update-ref",
                    "refs/remotes/origin/main",
                    old_oid,
                ],
                check=True,
                capture_output=True,
            )
        if "worktree" in args and "add" in args:
            return subprocess.CompletedProcess(args, 128, b"", b"checkout failed")
        return subprocess.run(args, cwd=cwd, shell=False, capture_output=True, check=False)

    repository = WorktreeRepository(
        tmp_path / "mirrors", tmp_path / "worktrees", command_runner=moving_base_runner
    )
    with pytest.raises(RepositoryCommandError):
        repository.prepare("moving-base", _mapping(remote), branch)

    branch_check = subprocess.run(
        ["git", "--git-dir", str(mirror), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        check=False,
    )
    assert branch_check.returncode == 0


def test_checkout_failure_for_windows_invalid_path_cleans_owned_resources(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    if os.name != "nt":
        pytest.skip("bad:name is only an invalid checkout path on Windows")
    _, remote = remote_repository
    blob = subprocess.run(
        ["git", "--git-dir", str(remote), "hash-object", "-w", "--stdin"],
        input=b"invalid path content\n",
        check=True,
        capture_output=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "--git-dir", str(remote), "mktree"],
        input=b"100644 blob " + blob + b"\tbad:name\n",
        check=True,
        capture_output=True,
    ).stdout.strip()
    parent = _git("rev-parse", "main", cwd=remote)
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Repository Test",
        "GIT_AUTHOR_EMAIL": "repository@example.invalid",
        "GIT_COMMITTER_NAME": "Repository Test",
        "GIT_COMMITTER_EMAIL": "repository@example.invalid",
    }
    invalid_commit = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "commit-tree",
            tree.decode("ascii"),
            "-p",
            parent,
        ],
        input=b"invalid path\n",
        env=environment,
        check=True,
        capture_output=True,
    ).stdout.strip().decode("ascii")
    _git("update-ref", "refs/heads/main", invalid_commit, cwd=remote)
    repository = _repository(tmp_path)
    branch = "bugfix/BUG-bad-path"

    with pytest.raises(RepositoryCommandError):
        repository.prepare("bad-path", _mapping(remote), branch)

    mirror = tmp_path / "mirrors" / "sample.git"
    branch_check = subprocess.run(
        ["git", "--git-dir", str(mirror), "show-ref", "--verify", f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
    )
    assert branch_check.returncode == 0
    assert not (tmp_path / "worktrees" / "bad-path").exists()


def test_existing_empty_target_is_never_claimed_or_removed(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    target = tmp_path / "worktrees" / "winner"
    target.mkdir(parents=True)
    before = target.stat()

    with pytest.raises(TargetExists):
        _repository(tmp_path).prepare("winner", _mapping(remote), "bugfix/BUG-target-race")

    after = target.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_existing_mirror_must_be_bare_and_match_the_mapping_url(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    wrong = tmp_path / "wrong.git"
    _git("init", "--bare", str(wrong), cwd=tmp_path)
    _git("clone", "--bare", str(wrong), str(mirrors / "sample.git"), cwd=tmp_path)

    with pytest.raises(RepositoryIdentityError):
        _repository(tmp_path).prepare("identity", _mapping(remote), "bugfix/BUG-3-id")


def test_existing_non_bare_mirror_path_is_rejected_as_an_identity_error(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = tmp_path / "mirrors" / "sample.git"
    mirror.mkdir(parents=True)
    _git("init", cwd=mirror)

    with pytest.raises(RepositoryIdentityError):
        _repository(tmp_path).prepare("non-bare", _mapping(remote), "bugfix/BUG-3-non-bare")


@pytest.mark.parametrize("run_id", ["", "../escape", "nested/run", "-option", "run\\other"])
def test_prepare_rejects_unsafe_run_ids(
    tmp_path: Path, remote_repository: tuple[Path, Path], run_id: str
) -> None:
    _, remote = remote_repository
    with pytest.raises(RepositoryBoundaryError):
        _repository(tmp_path).prepare(run_id, _mapping(remote), "bugfix/BUG-4-safe")
    assert not (tmp_path.parent / "escape").exists()


def test_roots_reject_symbolic_links(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    with pytest.raises(RepositoryBoundaryError):
        WorktreeRepository(linked, tmp_path / "worktrees")


def test_root_that_is_a_file_is_rejected_with_a_boundary_error(tmp_path: Path) -> None:
    root_file = tmp_path / "not-a-directory"
    root_file.write_text("file", encoding="utf-8")

    with pytest.raises(RepositoryBoundaryError):
        WorktreeRepository(root_file, tmp_path / "worktrees")


@pytest.mark.parametrize(
    ("workflow_type", "work_item_id", "title", "expected"),
    [
        ("requirement", "REQ-123", "Add report export", "requirement/REQ-123-add-report-export"),
        ("defect", "BUG_9", "修复导出", "bugfix/BUG_9-change"),
        ("bugfix", "BUG.10", "Crème brûlée", "bugfix/BUG.10-creme-brulee"),
    ],
)
def test_build_branch_name_is_stable_ascii_and_valid(
    workflow_type: str, work_item_id: str, title: str, expected: str
) -> None:
    branch = build_branch_name(workflow_type, work_item_id, title)
    assert branch == expected
    assert len(branch) <= 120


def test_build_run_branch_name_is_unique_across_runs() -> None:
    first = build_run_branch_name("defect", "DEF-1", "same defect", "a" * 32)
    second = build_run_branch_name("defect", "DEF-1", "same defect", "b" * 32)

    assert first != second
    assert first.endswith("-" + "a" * 32)
    assert second.endswith("-" + "b" * 32)
    assert len(first) <= 120


@pytest.mark.parametrize("work_item_id", ["", "   ", "../REQ", "REQ/1", "REQ\\1", "-danger"])
def test_build_branch_name_rejects_unsafe_ids(work_item_id: str) -> None:
    with pytest.raises(ValueError):
        build_branch_name("requirement", work_item_id, "title")


def test_snapshot_captures_all_change_kinds_without_modifying_status(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    prepared = repository.prepare("snapshot", mapping, "requirement/REQ-5-snapshot")
    (prepared.path / "README.md").write_text("staged\n", encoding="utf-8")
    _git("add", "README.md", cwd=prepared.path)
    (prepared.path / "README.md").write_text("unstaged\n", encoding="utf-8")
    (prepared.path / "space name.txt").write_text("space\n", encoding="utf-8")
    (prepared.path / "你好.txt").write_text("unicode\n", encoding="utf-8")
    _git("mv", "README.md", "renamed file.md", cwd=prepared.path)
    before = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=prepared.path,
        check=True,
        capture_output=True,
    ).stdout

    snapshot = repository.snapshot(prepared, mapping)

    after = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=prepared.path,
        check=True,
        capture_output=True,
    ).stdout
    assert snapshot.changed_files == tuple(sorted(snapshot.changed_files))
    assert {"README.md", "renamed file.md", "space name.txt", "你好.txt"} <= set(
        snapshot.changed_files
    )
    assert set(snapshot.untracked_hashes) == {"space name.txt", "你好.txt"}
    assert "diff --git" in snapshot.patch
    assert len(snapshot.diff_sha256) == 64
    assert snapshot.head_commit == prepared.base_commit
    assert snapshot.is_clean is False
    assert after == before


def test_snapshot_hash_changes_when_only_untracked_content_changes(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    prepared = repository.prepare("hash", mapping, "bugfix/BUG-6-hash")
    untracked = prepared.path / "new.txt"
    untracked.write_text("one", encoding="utf-8")
    first = repository.snapshot(prepared, mapping)
    untracked.write_text("two", encoding="utf-8")
    second = repository.snapshot(prepared, mapping)

    assert first.patch == second.patch == ""
    assert first.diff_sha256 != second.diff_sha256
    assert first.untracked_hashes != second.untracked_hashes


def test_snapshot_enforces_allowed_paths(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote, allowed_paths=("src/feature",))
    repository = _repository(tmp_path)
    prepared = repository.prepare("allowed", mapping, "requirement/REQ-7-allowed")
    allowed = prepared.path / "src" / "feature"
    allowed.mkdir(parents=True)
    (allowed / "ok.py").write_text("ok = True\n", encoding="utf-8")
    assert repository.snapshot(prepared, mapping).changed_files == ("src/feature/ok.py",)

    (prepared.path / "outside.py").write_text("no = True\n", encoding="utf-8")
    with pytest.raises(RepositoryBoundaryError):
        repository.snapshot(prepared, mapping)


def test_snapshot_rejects_symlink_escaping_worktree(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    prepared = repository.prepare("link", mapping, "bugfix/BUG-8-link")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (prepared.path / "escape.txt").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    with pytest.raises(RepositoryBoundaryError):
        repository.snapshot(prepared, mapping)


def test_snapshot_validates_prepared_identity(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    prepared = repository.prepare("identity", mapping, "bugfix/BUG-9-identity")
    forged = prepared.model_copy(update={"mirror_path": tmp_path / "other.git"})

    with pytest.raises(RepositoryIdentityError):
        repository.snapshot(forged, mapping)


def test_snapshot_rejects_crossed_worktree_path_and_branch_identity(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    first = repository.prepare("identity-one", mapping, "bugfix/BUG-identity-one")
    second = repository.prepare("identity-two", mapping, "bugfix/BUG-identity-two")
    crossed = first.model_copy(update={"path": second.path})

    with pytest.raises(RepositoryIdentityError):
        repository.snapshot(crossed, mapping)
    with pytest.raises(RepositoryIdentityError):
        repository.assert_head_unchanged(crossed)


def test_assert_head_rejects_constructed_fake_git_identity(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    prepared = repository.prepare("real-identity", mapping, "bugfix/BUG-real-identity")
    fake = tmp_path / "worktrees" / "fake"
    fake.mkdir()
    (fake / ".git").write_text("gitdir: nowhere", encoding="utf-8")
    forged = PreparedWorktree.model_construct(
        path=fake,
        branch=prepared.branch,
        base_commit=prepared.base_commit,
        head_commit=prepared.head_commit,
        mirror_path=prepared.mirror_path,
    )

    with pytest.raises((RepositoryIdentityError, RepositoryCommandError)):
        repository.assert_head_unchanged(forged)


@pytest.mark.parametrize(
    "bad_oid",
    ["", "abc", "-" + "a" * 39, "--output=outside", "g" * 40, "a" * 39, "A" * 40],
)
def test_repository_contracts_reject_noncanonical_commit_oids(
    tmp_path: Path, bad_oid: str
) -> None:
    with pytest.raises(ValidationError):
        PreparedWorktree(
            path=tmp_path,
            branch="bugfix/BUG-oid",
            base_commit=bad_oid,
            head_commit="a" * 40,
            mirror_path=tmp_path / "mirror.git",
        )
    with pytest.raises(ValidationError):
        RepositorySnapshot(
            head_commit=bad_oid,
            diff_sha256="b" * 64,
        )


def test_snapshot_defensively_rejects_constructed_malicious_oid_without_side_effect(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    prepared = repository.prepare("forged-oid", mapping, "bugfix/BUG-forged-oid")
    outside = tmp_path / "outside"
    forged = PreparedWorktree.model_construct(
        path=prepared.path,
        branch=prepared.branch,
        base_commit="--output=outside",
        head_commit=prepared.head_commit,
        mirror_path=prepared.mirror_path,
    )

    with pytest.raises(RepositoryIdentityError, match="commit identity") as captured:
        repository.snapshot(forged, mapping)

    assert "--output" not in str(captured.value)
    assert not outside.exists()


def test_contracts_reject_invalid_paths_refs_hashes_and_clean_inconsistency(
    tmp_path: Path,
) -> None:
    valid_oid = "a" * 40
    with pytest.raises(ValidationError):
        PreparedWorktree(
            path=Path("relative"),
            branch="bad branch",
            base_commit=valid_oid,
            head_commit=valid_oid,
            mirror_path=(tmp_path / "mirror.git").resolve(),
        )
    with pytest.raises(ValidationError):
        PreparedWorktree(
            path=tmp_path.resolve(),
            branch="bugfix/BUG-path",
            base_commit=valid_oid,
            head_commit=valid_oid,
            mirror_path=tmp_path.resolve(),
        )
    for values in (
        {"diff_sha256": "x" * 64},
        {"diff_sha256": "b" * 64, "untracked_hashes": {"../bad": "c" * 64}},
        {"diff_sha256": "b" * 64, "changed_files": (".git/config",)},
        {"diff_sha256": "b" * 64, "patch": "diff", "is_clean": True},
        {"diff_sha256": "b" * 64, "is_clean": False},
    ):
        with pytest.raises(ValidationError):
            RepositorySnapshot(head_commit=valid_oid, **values)


def test_contract_json_roundtrip_preserves_strict_snapshot_validation() -> None:
    snapshot = RepositorySnapshot(
        head_commit="a" * 40,
        diff_sha256=hashlib.sha256(b"").hexdigest(),
        is_clean=True,
    )
    assert RepositorySnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    payload = json.loads(snapshot.model_dump_json())
    payload["changed_files"] = ["../escape"]
    with pytest.raises(ValidationError):
        RepositorySnapshot.model_validate(payload)


def test_snapshot_enforces_patch_untracked_and_total_size_limits(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    patch_limited = WorktreeRepository(
        tmp_path / "mirrors-a",
        tmp_path / "worktrees-a",
        max_patch_bytes=8,
        max_untracked_file_bytes=1024,
        max_snapshot_bytes=1024,
    )
    prepared = patch_limited.prepare("patch-limit", mapping, "bugfix/BUG-patch-limit")
    (prepared.path / "README.md").write_text("a much larger patch\n", encoding="utf-8")
    before = _git("status", "--porcelain", cwd=prepared.path)
    with pytest.raises(SnapshotTooLargeError):
        patch_limited.snapshot(prepared, mapping)
    assert _git("status", "--porcelain", cwd=prepared.path) == before

    file_limited = WorktreeRepository(
        tmp_path / "mirrors-b",
        tmp_path / "worktrees-b",
        max_patch_bytes=1024,
        max_untracked_file_bytes=4,
        max_snapshot_bytes=1024,
    )
    prepared_file = file_limited.prepare(
        "file-limit", _mapping(remote, repo_name="sample-b"), "bugfix/BUG-file-limit"
    )
    (prepared_file.path / "large.bin").write_bytes(b"12345")
    with pytest.raises(SnapshotTooLargeError):
        file_limited.snapshot(prepared_file, _mapping(remote, repo_name="sample-b"))

    total_limited = WorktreeRepository(
        tmp_path / "mirrors-c",
        tmp_path / "worktrees-c",
        max_patch_bytes=1024,
        max_untracked_file_bytes=10,
        max_snapshot_bytes=7,
    )
    mapping_c = _mapping(remote, repo_name="sample-c")
    prepared_total = total_limited.prepare(
        "total-limit", mapping_c, "bugfix/BUG-total-limit"
    )
    (prepared_total.path / "one.bin").write_bytes(b"1234")
    (prepared_total.path / "two.bin").write_bytes(b"5678")
    with pytest.raises(SnapshotTooLargeError):
        total_limited.snapshot(prepared_total, mapping_c)


@pytest.mark.parametrize("field", ["max_patch_bytes", "max_untracked_file_bytes", "max_snapshot_bytes"])
def test_repository_rejects_nonpositive_resource_limits(tmp_path: Path, field: str) -> None:
    values = {
        "mirror_root": tmp_path / "mirrors",
        "worktree_root": tmp_path / "worktrees",
        field: 0,
    }
    with pytest.raises(ValueError):
        WorktreeRepository(**values)


def test_streaming_patch_limit_terminates_and_waits_for_git_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.developer_workflow.repository as repository_module

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"x" * 32)
            self.stderr = io.BytesIO(b"sensitive stderr")
            self.returncode: int | None = None
            self.terminated = False
            self.waited = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            self.returncode = self.returncode if self.returncode is not None else 0
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(repository_module.subprocess, "Popen", lambda *args, **kwargs: process)
    repository = WorktreeRepository(
        tmp_path / "mirrors",
        tmp_path / "worktrees",
        max_patch_bytes=8,
        max_untracked_file_bytes=8,
        max_snapshot_bytes=8,
    )

    with pytest.raises(SnapshotTooLargeError) as captured:
        repository._read_patch(tmp_path, "a" * 40)

    assert process.terminated is True
    assert process.waited is True
    assert "sensitive" not in str(captured.value)


def test_existing_origin_with_query_is_rejected_without_echo(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mirror = tmp_path / "mirrors" / "sample.git"
    mirror.parent.mkdir()
    _git("clone", "--bare", str(remote), str(mirror), cwd=tmp_path)
    malicious = "https://example.invalid/repo.git?token=do-not-echo"
    _git("remote", "set-url", "origin", malicious, cwd=mirror)

    with pytest.raises(MirrorOriginMismatch) as captured:
        _repository(tmp_path).prepare("origin-query", _mapping(remote), "bugfix/BUG-origin")

    assert "token" not in str(captured.value)
    assert "do-not-echo" not in str(captured.value)


def test_ssh_username_case_is_not_semantically_equal(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assert repository._normalized_url("ssh://Git@example.invalid/team/repo.git") != (
        repository._normalized_url("ssh://git@example.invalid/team/repo.git")
    )


def test_posix_backslash_filename_cannot_masquerade_as_allowed_prefix(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    relative = repository._safe_relative_path(b"src\\secret.py", windows=False)

    assert relative == "src\\secret.py"
    assert repository._allowed(relative, ("src",)) is False


def test_posix_real_git_backslash_filename_is_not_inside_slash_prefix(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permits backslash as a literal filename byte")
    _, remote = remote_repository
    mapping = _mapping(remote, allowed_paths=("src",))
    repository = _repository(tmp_path)
    prepared = repository.prepare("posix-backslash", mapping, "bugfix/BUG-backslash")
    (prepared.path / "src\\secret.py").write_text("secret\n", encoding="utf-8")

    with pytest.raises(RepositoryBoundaryError):
        repository.snapshot(prepared, mapping)


def test_assert_head_unchanged_detects_commits(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    mapping = _mapping(remote)
    repository = _repository(tmp_path)
    prepared = repository.prepare("head", mapping, "bugfix/BUG-10-head")
    _git("config", "user.name", "Repository Test", cwd=prepared.path)
    _git("config", "user.email", "repository@example.invalid", cwd=prepared.path)
    (prepared.path / "commit.txt").write_text("commit\n", encoding="utf-8")
    _git("add", "commit.txt", cwd=prepared.path)
    _git("commit", "-m", "unexpected", cwd=prepared.path)

    with pytest.raises(HeadChangedError):
        repository.assert_head_unchanged(prepared)


def test_failed_prepare_does_not_delete_preexisting_target_content(
    tmp_path: Path, remote_repository: tuple[Path, Path]
) -> None:
    _, remote = remote_repository
    target = tmp_path / "worktrees" / "occupied"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(RepositoryBoundaryError):
        _repository(tmp_path).prepare("occupied", _mapping(remote), "bugfix/BUG-11-fail")

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_package_exports_repository_contracts_and_service() -> None:
    import src.developer_workflow as public

    assert public.PreparedWorktree.__name__ == "PreparedWorktree"
    assert public.RepositorySnapshot.__name__ == "RepositorySnapshot"
    assert public.WorktreeRepository is WorktreeRepository
