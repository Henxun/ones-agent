from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import pickle
import subprocess
import sys
import time
import traceback
from unittest import mock
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from jsonschema import ValidationError

import src.developer_workflow.codex_runner as codex_runner_module
from src.developer_workflow.codex_runtime import NativeCodexIdentity
from src.developer_workflow.codex_runner import (
    CodexExecutionError,
    CodexOutputError,
    CodexRunner,
    CodexTimeoutError,
    UnsafeCodexRunError,
    validate_codex_auth_source,
)
from src.developer_workflow.contracts import (
    PreparedWorktree,
    RepositoryChangeClaim,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    RepositorySnapshot,
)
from src.developer_workflow.repository import HeadChangedError
from src.developer_workflow.repository_group import PreparedRepository


OID = "a" * 40
EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _verify_test_execution_lease(
    lease: object, *, cache_root: Path,
) -> None:
    if (
        not lease.verify()  # type: ignore[attr-defined]
        or lease._cache_root != cache_root  # type: ignore[attr-defined]
    ):
        raise OSError("test execution lease is invalid")


class _TestingCodexRunner(CodexRunner):
    """Explicit test-only verifier seam; production CodexRunner is untouched."""

    def _invoke(self, *args: object, **kwargs: object) -> tuple[str, tuple[str, ...]]:
        with mock.patch.object(
            codex_runner_module,
            "verify_locked_private_codex_for_execution",
            _verify_test_execution_lease,
        ):
            return super()._invoke(*args, **kwargs)  # type: ignore[arg-type]


class _TinyLockedRuntime:
    def __init__(self) -> None:
        self.payload = b"verified-private-codex"
        self.offset = 0
        self.identity = NativeCodexIdentity(1, 2, len(self.payload), 3)
        self.size = len(self.payload)

    def rewind(self) -> None:
        self.offset = 0

    def read_chunk(self, size: int) -> bytes:
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def current_identity(self) -> NativeCodexIdentity:
        return self.identity

    def close(self) -> None:
        return None


class _TinyCacheAdapter:
    def prepare_private_directory(self, path: Path) -> Path:
        path.mkdir(exist_ok=True)
        return path.resolve(strict=True)

    def validate_private_directory(self, path: Path) -> None:
        if not path.is_dir():
            raise OSError("unsafe")

    def validate_cache_ancestor_chain(self, root: Path) -> None:
        return None

    def protect_private_file(self, path: Path) -> None:
        return None

    def validate_private_file(self, path: Path) -> tuple[int, int]:
        metadata = path.stat()
        return metadata.st_dev, metadata.st_ino

    def read_private_text(self, path: Path) -> str:
        return path.read_text("utf-8")

    def inspect_private_executable(
        self, path: Path,
    ) -> tuple[NativeCodexIdentity, str]:
        metadata = path.stat()
        return (
            NativeCodexIdentity(
                metadata.st_dev, metadata.st_ino, metadata.st_size,
                metadata.st_mtime_ns,
            ),
            "OpenAI OpCo, LLC",
        )

    def fsync_directory(self, path: Path) -> None:
        return None

    def smoke(
        self, executable: Path, *, environment: dict[str, str], timeout: float,
    ) -> None:
        return None


def _verified_runtime(root: Path) -> object:
    return codex_runner_module.CodexRuntimePreparer(
        cache_root=(root / "private-runtime" / "codex-runtime").resolve(),
        discover=_TinyLockedRuntime,
        _cache_adapter=_TinyCacheAdapter(),  # type: ignore[arg-type]
        chunk_size=4,
    ).prepare_verified()


def _attested_command(root: Path) -> codex_runner_module.CodexCommand:
    return codex_runner_module.CodexCommand._from_runtime(
        _verified_runtime(root)
    )


def test_codex_command_keeps_an_immutable_validated_prefix(tmp_path: Path) -> None:
    command = _attested_command(tmp_path)

    assert command.argv("exec", "--version") == [
        command.prefix[0], "exec", "--version",
    ]
    with pytest.raises(FrozenInstanceError):
        command.prefix = ("replacement",)  # type: ignore[misc]


@pytest.mark.parametrize(
    "prefix",
    [(), ("",), ("safe", "bad\x00component"), (r"C:\private\codex.exe",)],
)
def test_codex_command_rejects_all_public_construction(
    prefix: tuple[str, ...],
) -> None:
    with pytest.raises(TypeError):
        codex_runner_module.CodexCommand(prefix)


@pytest.mark.parametrize("argument", ["", "bad\x00argument"])
def test_codex_command_rejects_empty_or_nul_arguments(
    tmp_path: Path, argument: str,
) -> None:
    command = _attested_command(tmp_path)
    with pytest.raises(ValueError):
        command.argv(argument)


def test_resolver_returns_only_prepared_private_native_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _verified_runtime(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Node, JS, cmd, ps1, and shell execution is forbidden")

    monkeypatch.setattr(codex_runner_module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(codex_runner_module.subprocess, "run", forbidden)
    monkeypatch.setattr(codex_runner_module.os, "system", forbidden)

    command = codex_runner_module.resolve_codex_command(_prepare=lambda: runtime)

    assert command.prefix[0].endswith("codex.exe")
    assert command.argv("exec") == [command.prefix[0], "exec"]


def test_resolver_sanitizes_preparation_failure_and_project_traceback_locals() -> None:
    callable_canary = "prepare-callable-canary"
    failure_canary = "private-path-failure-canary"

    class Discovery:
        def __repr__(self) -> str:
            return f"<{callable_canary}>"

        def __call__(self) -> object:
            raise OSError(f"C:/sensitive/{failure_canary}/codex.exe")

    with pytest.raises(codex_runner_module.CodexProcessStartError) as caught:
        codex_runner_module.resolve_codex_command(_prepare=Discovery())

    assert str(caught.value) == "Codex executable is unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert callable_canary not in rendered
    assert failure_canary not in rendered
    captured = traceback.TracebackException.from_exception(
        caught.value, capture_locals=True
    )
    module_path = Path(codex_runner_module.__file__).resolve()
    project_locals = "\n".join(
        value
        for frame in captured.stack
        if Path(frame.filename).resolve() == module_path
        for value in (frame.locals or {}).values()
    )
    assert callable_canary not in project_locals
    assert failure_canary not in project_locals


@pytest.mark.parametrize(
    "control_flow",
    [
        MemoryError(), KeyboardInterrupt(), SystemExit(), GeneratorExit(),
        asyncio.CancelledError(),
    ],
)
def test_resolver_preserves_memory_and_control_flow(control_flow: BaseException) -> None:
    def prepare() -> object:
        raise control_flow

    with pytest.raises(type(control_flow)):
        codex_runner_module.resolve_codex_command(_prepare=prepare)  # type: ignore[arg-type]


@pytest.mark.parametrize("error_type", [AssertionError, TypeError, AttributeError])
def test_resolver_preserves_internal_preparation_failures(
    error_type: type[Exception],
) -> None:
    internal = error_type("internal-discovery-canary")

    def prepare() -> object:
        raise internal

    with pytest.raises(error_type) as caught:
        codex_runner_module.resolve_codex_command(_prepare=prepare)  # type: ignore[arg-type]

    assert caught.value is internal


@pytest.mark.parametrize(
    "prefix",
    [
        (r"C:\\private\\node.exe", r"C:\\private\\codex.js"),
        (r"C:\\private\\codex.cmd",),
        (r"C:\\private\\codex.ps1",),
        (r"C:\\private\\node.exe",),
    ],
)
def test_codex_command_rejects_non_native_or_multi_component_prefix(
    prefix: tuple[str, ...],
) -> None:
    with pytest.raises(TypeError):
        codex_runner_module.CodexCommand(prefix)


def test_codex_command_copy_preserves_attestation_and_pickle_is_rejected(
    tmp_path: Path,
) -> None:
    command = _attested_command(tmp_path)
    assert copy.copy(command) is command
    assert copy.deepcopy(command) is command
    with pytest.raises(TypeError):
        pickle.dumps(command)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("prefix", (r"C:\\forged\\codex.exe",)),
        ("_path", Path("C:/forged-cache/0/codex.exe")),
        ("_sha256", "f" * 64),
        ("_identity", NativeCodexIdentity(99, 98, 97, 96)),
        ("_cache_root", Path("C:/forged-cache")),
    ],
)
def test_command_mac_rejects_object_setattr_mutation_before_executor(
    tmp_path: Path,
    field_name: str,
    replacement: object,
) -> None:
    command = _attested_command(tmp_path)
    assert command._is_attested()
    object.__setattr__(command, field_name, replacement)
    assert not command._is_attested()
    executor = FakeExecutor()
    runner = CodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=executor,
        command_resolver=lambda: command,
    )

    with pytest.raises(codex_runner_module.CodexProcessStartError):
        runner.run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="mutated-command",
            prompt="safe prompt",
        )
    assert not executor.calls
    assert command._lease._closed


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("path", Path("C:/forged-cache/0/codex.exe")),
        ("sha256", "f" * 64),
        ("identity", NativeCodexIdentity(99, 98, 97, 96)),
        ("_cache_root", Path("C:/forged-cache")),
    ],
)
def test_runtime_mac_rejects_object_setattr_mutation(
    tmp_path: Path,
    field_name: str,
    replacement: object,
) -> None:
    runtime = _verified_runtime(tmp_path)
    assert runtime._is_attested()  # type: ignore[attr-defined]
    object.__setattr__(runtime, field_name, replacement)
    assert not runtime._is_attested()  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="attestation"):
        codex_runner_module.CodexCommand._from_runtime(runtime)


def test_command_mac_rejects_nested_identity_mutation(tmp_path: Path) -> None:
    command = _attested_command(tmp_path)
    object.__setattr__(command._identity, "size", command._identity.size + 1)
    assert not command._is_attested()


def test_command_requires_open_verified_execution_lease(tmp_path: Path) -> None:
    command = _attested_command(tmp_path)
    assert command._lease.verify()  # type: ignore[attr-defined]

    command._lease.close()  # type: ignore[attr-defined]
    command._lease.close()  # idempotent  # type: ignore[attr-defined]

    assert not command._is_attested()
    with pytest.raises(TypeError, match="attestation"):
        command.argv("exec")


def test_execution_lease_seal_rejects_reflection_mutation(tmp_path: Path) -> None:
    command = _attested_command(tmp_path)
    object.__setattr__(command._lease, "_cache_adapter", object())
    assert not command._is_attested()
    with pytest.raises(TypeError, match="attestation"):
        command.argv("exec")
    command.close()


def test_locked_execution_lease_blocks_or_detects_disk_replacement(
    tmp_path: Path,
) -> None:
    command = _attested_command(tmp_path)
    target = Path(command.prefix[0])
    replacement = target.with_name("replacement.exe")
    replacement.write_bytes(b"not-the-verified-runtime")
    try:
        os.replace(replacement, target)
    except PermissionError:
        assert command._is_attested()
    else:
        assert not command._is_attested()
        with pytest.raises(TypeError, match="attestation"):
            command.argv("exec")
    finally:
        command.close()


def test_reflection_cannot_issue_runtime_without_verified_locked_handle(
    tmp_path: Path,
) -> None:
    import src.developer_workflow.codex_runtime as runtime_module

    valid = _verified_runtime(tmp_path)
    forged_lease = object.__new__(runtime_module.LockedPrivateCodex)
    with pytest.raises((AttributeError, TypeError, ValueError)):
        runtime_module._PreparedCodexRuntime._issue(
            valid.path,  # type: ignore[attr-defined]
            valid.identity,  # type: ignore[attr-defined]
            valid.sha256,  # type: ignore[attr-defined]
            valid._cache_root,  # type: ignore[attr-defined]
            forged_lease,
            nonce=runtime_module._RUNTIME_ATTESTATION_NONCE,
        )
    valid._lease.close()  # type: ignore[attr-defined]


def test_production_os_verifier_rejects_test_adapter_lease(
    tmp_path: Path,
) -> None:
    import src.developer_workflow.codex_runtime as runtime_module

    command = _attested_command(tmp_path)
    try:
        with pytest.raises(OSError, match="OS verification"):
            runtime_module.verify_locked_private_codex_for_execution(
                command._lease,
                cache_root=command._cache_root,
            )
    finally:
        command.close()


def test_production_invoke_rejects_reflection_issued_fake_adapter_lease(
    tmp_path: Path,
) -> None:
    import src.developer_workflow.codex_runtime as runtime_module

    original = _verified_runtime(tmp_path)
    reflected = runtime_module._PreparedCodexRuntime._issue(
        original.path,  # type: ignore[attr-defined]
        original.identity,  # type: ignore[attr-defined]
        original.sha256,  # type: ignore[attr-defined]
        original._cache_root,  # type: ignore[attr-defined]
        original._lease,  # type: ignore[attr-defined]
        nonce=runtime_module._RUNTIME_ATTESTATION_NONCE,
    )
    command = codex_runner_module.CodexCommand._from_runtime(reflected)
    executor = FakeExecutor()
    runner = CodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=executor,
        command_resolver=lambda: command,
    )

    with pytest.raises(codex_runner_module.CodexProcessStartError):
        runner.run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="reflected-lease",
            prompt="safe prompt",
        )
    assert not executor.calls
    assert command._lease._closed


@pytest.mark.parametrize("mode", ["invalid", "backend", "control"])
@pytest.mark.parametrize("cleanup_error", [OSError("close"), MemoryError("close")])
def test_invoke_closes_lease_once_with_primary_cleanup_priority(
    tmp_path: Path,
    mode: str,
    cleanup_error: BaseException,
) -> None:
    import src.developer_workflow.codex_runtime as runtime_module

    command = _attested_command(tmp_path)
    original_descriptor = command._lease._descriptor

    class CountingDescriptor:
        calls = 0

        def fileno(self) -> int:
            return original_descriptor.fileno()

        def close(self) -> None:
            self.calls += 1
            raise cleanup_error

    descriptor = CountingDescriptor()
    object.__setattr__(command._lease, "_descriptor", descriptor)
    object.__setattr__(
        command._lease,
        "_seal",
        runtime_module._private_lease_mac(command._lease),
    )
    if mode == "invalid":
        object.__setattr__(command, "prefix", ("invalid",))
        primary_type: type[BaseException] = codex_runner_module.CodexProcessStartError
        executor = FakeExecutor()
    elif mode == "backend":
        primary_type = codex_runner_module.CodexExecutionError
        executor = FakeExecutor(error=RuntimeError("backend"))
    else:
        primary_type = KeyboardInterrupt
        executor = FakeExecutor(error=KeyboardInterrupt())  # type: ignore[arg-type]
    runner = _TestingCodexRunner(
        run_root=(tmp_path / f"runs-{mode}").resolve(),
        repository=FakeRepository(),
        command_executor=executor,
        command_resolver=lambda: command,
    )
    expected = (
        MemoryError
        if isinstance(cleanup_error, MemoryError) and mode != "control"
        else primary_type
    )
    try:
        with pytest.raises(expected):
            runner.run(
                _prepared(tmp_path), _mapping(tmp_path),
                run_id=f"close-{mode}", prompt="safe prompt",
            )
        assert descriptor.calls == 1
    finally:
        original_descriptor.close()


def test_runner_rejects_forged_unattested_command_before_executor(
    tmp_path: Path,
) -> None:
    forged = object.__new__(codex_runner_module.CodexCommand)
    object.__setattr__(forged, "prefix", (str(tmp_path / "codex.exe"),))
    object.__setattr__(forged, "_identity", object())
    object.__setattr__(forged, "_sha256", "0" * 64)
    object.__setattr__(forged, "_nonce", object())
    executor = FakeExecutor()
    runner = CodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=executor,
        command_resolver=lambda: forged,
    )
    with pytest.raises(
        codex_runner_module.CodexProcessStartError,
        match="^Codex executable is unavailable$",
    ):
        runner.run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="safe prompt",
        )
    assert not executor.calls


def test_codex_auth_source_accepts_environment_auth_without_returning_secret() -> None:
    assert validate_codex_auth_source(
        {"CODEX_API_KEY": "runtime-only-codex-auth"}
    ) is None


def test_codex_auth_source_requires_a_regular_auth_file_in_codex_home(
    tmp_path: Path,
) -> None:
    codex_home = (tmp_path / "codex-home").resolve()
    codex_home.mkdir()

    with pytest.raises(UnsafeCodexRunError, match="authentication source"):
        validate_codex_auth_source({"CODEX_HOME": str(codex_home)})

    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    assert validate_codex_auth_source({"CODEX_HOME": str(codex_home)}) == codex_home


def _prepared(root: Path) -> PreparedWorktree:
    worktree = root / "worktree"
    mirror = root / "mirror.git"
    worktree.mkdir(exist_ok=True)
    mirror.mkdir(exist_ok=True)
    return PreparedWorktree(
        path=worktree.resolve(), branch="ai/run-1", base_commit=OID,
        head_commit=OID, mirror_path=mirror.resolve(),
    )


def _mapping(root: Path) -> RepositoryMapping:
    return RepositoryMapping(
        key="repo", project_id="project", iteration_id="iteration",
        repo_url=str((root / "origin.git").resolve()), repo_name="repo",
        allowed_paths=("src",),
    )


def _payload(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "summary": "implemented safely",
        "changed_files": ["src/app.py"],
        "repository_changes": [],
        "commands": [{"command": "pytest", "exit_code": 0, "summary": "passed"}],
        "evidence": ["tests pass"],
        "review_findings": [],
        "review_repair_scope": [],
        "review_external_validation": [],
        "risks": [],
        "unresolved_items": [],
        "acceptance_coverage": [],
        "unrelated_changes_checked": False,
        "root_cause_evidence": [],
        "investigation_suggestions": [],
        "behavior_before": "",
        "behavior_after": "",
        "impact_scope": [],
        "risk_level": "",
    }
    value.update(updates)
    return value


class FakeRepository:
    def __init__(
        self, *, changed_files: tuple[str, ...] = ("src/app.py",),
        contains_sensitive_content: bool = False,
        changed_by_mapping: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.head_checks = 0
        self.changed_files = changed_files
        self.sensitive_content = contains_sensitive_content
        self.changed_by_mapping = changed_by_mapping

    def assert_head_unchanged(self, prepared: PreparedWorktree) -> None:
        self.head_checks += 1

    def snapshot(self, prepared: PreparedWorktree, mapping: RepositoryMapping) -> RepositorySnapshot:
        changed_files = (
            self.changed_files
            if self.changed_by_mapping is None
            else self.changed_by_mapping[mapping.key]
        )
        return RepositorySnapshot(
            head_commit=OID,
            diff_sha256="b" * 64 if changed_files else EMPTY_HASH,
            changed_files=changed_files,
            patch="diff" if changed_files else "",
            is_clean=not changed_files,
        )

    def contains_sensitive_content(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping,
        secrets: tuple[str, ...],
    ) -> bool:
        return self.sensitive_content


class FakeExecutor:
    def __init__(self, stdout: str | None = None, *, returncode: int = 0, error: Exception | None = None) -> None:
        self.stdout = stdout or json.dumps(_payload())
        self.returncode = returncode
        self.error = error
        self.calls: list[
            tuple[list[str], Path, dict[str, str], float, int, bytes | None]
        ] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, cwd, env, timeout, max_output_bytes, stdin))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "sensitive stderr")


def _runner(root: Path, executor: FakeExecutor, repository: FakeRepository | None = None) -> CodexRunner:
    return _TestingCodexRunner(
        run_root=(root / "runs").resolve(), repository=repository or FakeRepository(),
        command_executor=executor,
        command_resolver=lambda: _attested_command(root),
    )


def test_runner_holds_execution_lease_through_executor_then_closes_it(
    tmp_path: Path,
) -> None:
    command = _attested_command(tmp_path)

    class CheckingExecutor(FakeExecutor):
        def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            assert command._lease.verify()
            return super().__call__(*args, **kwargs)  # type: ignore[arg-type]

    runner = _TestingCodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=CheckingExecutor(),
        command_resolver=lambda: command,
    )
    runner.run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="lease-lifetime",
        prompt="safe prompt",
    )
    assert not command._lease.verify()


def test_default_runner_fails_closed_when_private_runtime_preparation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = FakeExecutor()
    monkeypatch.setattr(
        codex_runner_module.CodexRuntimePreparer,
        "prepare_verified",
        lambda self: (_ for _ in ()).throw(OSError("unavailable")),
    )
    runner = _TestingCodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=executor,
    )

    with pytest.raises(
        codex_runner_module.CodexProcessStartError,
        match="^Codex executable is unavailable$",
    ):
        runner.run(
            _prepared(tmp_path), _mapping(tmp_path),
            run_id="run-1", prompt="safe prompt",
        )
    assert not executor.calls


def _prepared_group(root: Path) -> tuple[RepositoryGroupMapping, tuple[PreparedRepository, ...]]:
    workspace = root / "workspace"
    workspace.mkdir()
    mappings = (
        RepositoryMapping(
            key="shared-sdk", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/shared-sdk.git", repo_name="shared-sdk",
            role=RepositoryRole.DEPENDENCY, allowed_paths=("src",),
        ),
        RepositoryMapping(
            key="desktop-app", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/desktop-app.git", repo_name="desktop-app",
            role=RepositoryRole.PRIMARY, depends_on=("shared-sdk",),
            allowed_paths=("src",),
        ),
    )
    prepared: list[PreparedRepository] = []
    for mapping in mappings:
        worktree = workspace / mapping.key
        mirror = root / f"{mapping.key}.git"
        worktree.mkdir()
        mirror.mkdir()
        prepared.append(PreparedRepository(
            repository_key=mapping.key,
            mapping=mapping,
            prepared=PreparedWorktree(
                path=worktree.resolve(), branch=f"bugfix/DEF-1-{mapping.key}",
                base_commit=OID, head_commit=OID, mirror_path=mirror.resolve(),
            ),
        ))
    group = RepositoryGroupMapping(
        key="desktop-suite", project_id="project", iteration_id="iteration",
        primary_repository="desktop-app", repositories=mappings,
    )
    return group, tuple(prepared)


def test_group_run_requires_exact_repository_qualified_claims(tmp_path: Path) -> None:
    group, prepared = _prepared_group(tmp_path)
    repository = FakeRepository(changed_by_mapping={
        "shared-sdk": ("src/shortcut.py",),
        "desktop-app": ("src/window.py",),
    })
    executor = FakeExecutor(json.dumps(_payload(
        changed_files=[],
        repository_changes=[
            {"repository_key": "shared-sdk", "path": "src/shortcut.py"},
            {"repository_key": "desktop-app", "path": "src/window.py"},
        ],
        acceptance_coverage=[{
            "criterion_id": "AC-1",
            "criterion_text": "window recreation remains safe",
            "files": [],
            "repository_files": [
                {"repository_key": "shared-sdk", "path": "src/shortcut.py"},
                {"repository_key": "desktop-app", "path": "src/window.py"},
            ],
            "tests": ["pytest"],
        }],
    )))

    result = _runner(tmp_path, executor, repository).run_group(
        group, prepared, run_id="group-run", prompt="fix across repositories"
    )

    assert result.repository_changes == (
        RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
        RepositoryChangeClaim(repository_key="desktop-app", path="src/window.py"),
    )
    assert result.acceptance_coverage[0].files == ()
    assert len(result.acceptance_coverage[0].repository_files) == 2
    group_command, group_cwd, *_ = executor.calls[0]
    primary = next(
        item.prepared.path
        for item in prepared
        if item.repository_key == group.primary_repository
    )
    additional = tuple(
        item.prepared.path
        for item in prepared
        if item.repository_key != group.primary_repository
    )
    assert group_cwd == primary
    assert "--skip-git-repo-check" not in group_command
    assert tuple(
        Path(group_command[index + 1])
        for index, argument in enumerate(group_command)
        if argument == "--add-dir"
    ) == additional
    assert repository.head_checks == 6


def test_group_run_normalizes_equivalent_unqualified_change_paths(tmp_path: Path) -> None:
    group, prepared = _prepared_group(tmp_path)
    repository = FakeRepository(changed_by_mapping={
        "shared-sdk": ("src/shortcut.py",), "desktop-app": (),
    })
    executor = FakeExecutor(json.dumps(_payload(
        changed_files=["src/shortcut.py"],
        repository_changes=[
            {"repository_key": "shared-sdk", "path": "src/shortcut.py"},
        ],
        acceptance_coverage=[
            {
                "criterion_id": "AC-1",
                "criterion_text": "shortcut behavior is covered",
                "files": ["src/shortcut.py"],
                "repository_files": [
                    {"repository_key": "shared-sdk", "path": "src/shortcut.py"},
                ],
                "tests": ["pytest"],
            },
            {
                "criterion_id": "AC-2",
                "criterion_text": "no external state changed",
                "files": [],
                "repository_files": [],
                "tests": ["git status"],
            },
        ],
    )))

    result = _runner(tmp_path, executor, repository).run_group(
        group, prepared, run_id="group-equivalent-claims", prompt="fix"
    )

    assert result.changed_files == ()
    assert result.repository_changes == (
        RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
    )
    assert len(result.acceptance_coverage) == 1
    assert result.acceptance_coverage[0].files == ()
    assert result.acceptance_coverage[0].repository_files == (
        RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
    )


def test_group_contract_accepts_reproduction_payload_with_safe_inline_compile(
    tmp_path: Path,
) -> None:
    group, _ = _prepared_group(tmp_path)
    mappings = tuple(
        mapping.validated_update(allowed_paths=("src", "tests"))
        for mapping in group.repositories
    )
    group = group.validated_update(repositories=mappings)
    payload = json.dumps(_payload(
        changed_files=["tests/test_user_repository_encryption.py"],
        repository_changes=[{
            "repository_key": "desktop-app",
            "path": "tests/test_user_repository_encryption.py",
        }],
        commands=[{
            "command": (
                "python -c \"compile(open('tests/test_user_repository_encryption.py', "
                "encoding='utf-8').read(), "
                "'tests/test_user_repository_encryption.py', 'exec')\""
            ),
            "exit_code": 0,
            "summary": "test source compiles",
        }],
        acceptance_coverage=[{
            "criterion_id": "AC-1",
            "criterion_text": "focused reproduction exists",
            "files": ["tests/test_user_repository_encryption.py"],
            "repository_files": [{
                "repository_key": "desktop-app",
                "path": "tests/test_user_repository_encryption.py",
            }],
            "tests": ["tests/test_user_repository_encryption.py::test_reproduction"],
        }],
        unrelated_changes_checked=True,
    ))
    runner = _runner(tmp_path, FakeExecutor())

    normalized = runner._validate_group_output(payload, group)
    result = runner._result_from_payload(normalized)

    assert result.changed_files == ()
    assert result.repository_changes == (
        RepositoryChangeClaim(
            repository_key="desktop-app",
            path="tests/test_user_repository_encryption.py",
        ),
    )
    assert result.commands[0].command.startswith("python -c \"compile(open(")


def test_group_run_rejects_ambiguous_unqualified_change_paths(tmp_path: Path) -> None:
    group, prepared = _prepared_group(tmp_path)
    repository = FakeRepository(changed_by_mapping={
        "shared-sdk": ("src/shortcut.py",), "desktop-app": (),
    })
    executor = FakeExecutor(json.dumps(_payload(
        changed_files=["src/other.py"],
        repository_changes=[
            {"repository_key": "shared-sdk", "path": "src/shortcut.py"},
        ],
    )))

    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, executor, repository).run_group(
            group, prepared, run_id="group-ambiguous-claims", prompt="fix"
        )


def test_group_root_cause_discards_stage_irrelevant_acceptance_coverage(
    tmp_path: Path,
) -> None:
    group, prepared = _prepared_group(tmp_path)
    payload = _payload(
        changed_files=[],
        repository_changes=[],
        acceptance_coverage=[{"criterion_id": 9}],
    )

    repository = FakeRepository(
        changed_by_mapping={"shared-sdk": (), "desktop-app": ()}
    )
    result = _runner(
        tmp_path, FakeExecutor(json.dumps(payload)), repository
    ).run_group_root_cause(
        group,
        prepared,
        run_id="group-root-cause-coverage",
        prompt="analyze",
    )

    assert result.acceptance_coverage == ()


def test_group_run_rejects_unknown_repository_but_uses_git_for_claim_drift(tmp_path: Path) -> None:
    group, prepared = _prepared_group(tmp_path)
    repository = FakeRepository(changed_by_mapping={
        "shared-sdk": ("src/shortcut.py",), "desktop-app": (),
    })
    unknown = FakeExecutor(json.dumps(_payload(
        changed_files=[],
        repository_changes=[{"repository_key": "other", "path": "src/x.py"}],
    )))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path / "unknown", unknown, repository).run_group(
            group, prepared, run_id="unknown", prompt="fix"
        )

    drift = FakeExecutor(json.dumps(_payload(
        changed_files=[],
        repository_changes=[
            {"repository_key": "shared-sdk", "path": "src/different.py"}
        ],
    )))
    result = _runner(tmp_path / "drift", drift, repository).run_group(
        group, prepared, run_id="drift", prompt="fix"
    )
    assert result.repository_changes == (
        RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
    )


@pytest.mark.parametrize("group_mode", [False, True])
@pytest.mark.parametrize("mutates", [False, True])
def test_read_only_review_uses_verified_diff_without_losing_negative_findings(tmp_path: Path, group_mode: bool, mutates: bool) -> None:
    repository = FakeRepository(changed_by_mapping={"shared-sdk": ("src/shortcut.py",), "desktop-app": ()}
                                if group_mode else None)
    repair_scope = [{
        "repository_key": "shared-sdk" if group_mode else "repo",
        "path": "setup.py",
    }]
    payload = _payload(changed_files=[], repository_changes=[],
                       summary="Review rejects this repair", review_findings=["Legacy session migration can lose data"],
                       review_repair_scope=repair_scope,
                       unresolved_items=["Fix legacy data loss before publication"])

    class Executor(FakeExecutor):
        def __call__(self, *args, **kwargs):
            if mutates:
                repository.changed_files = ("src/other.py",)
                repository.changed_by_mapping = {"shared-sdk": ("src/other.py",), "desktop-app": ()} if group_mode else None
            return super().__call__(*args, **kwargs)

    runner = _runner(tmp_path, Executor(json.dumps(payload)), repository)

    def review():
        if group_mode:
            group, prepared = _prepared_group(tmp_path)
            return runner.run_group(group, prepared, run_id="readonly-review", prompt="review", allow_changes=False)
        return runner.run(_prepared(tmp_path), _mapping(tmp_path), run_id="readonly-review", prompt="review", allow_changes=False)

    if mutates:
        with pytest.raises(UnsafeCodexRunError, match="read-only Codex stage modified"):
            review()
    else:
        result = review()
        assert result.review_findings == tuple(payload["review_findings"])
        assert result.unresolved_items == tuple(payload["unresolved_items"])
        assert result.review_external_validation == ()
        assert tuple(
            (claim.repository_key, claim.path)
            for claim in result.review_repair_scope
        ) == ((repair_scope[0]["repository_key"], "setup.py"),)
        if group_mode:
            assert result.repository_changes == (RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),)
        else:
            assert result.changed_files == ("src/app.py",)


def test_run_uses_noninteractive_command_safe_environment_and_persisted_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _prepared(tmp_path)
    mapping = _mapping(tmp_path)
    executor = FakeExecutor()
    monkeypatch.setenv("ONES_TOKEN", "ones-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret-value")
    monkeypatch.setenv("GIT_ASKPASS", "askpass")
    monkeypatch.setenv("CODEX_API_KEY", "codex-auth-value")

    result = _runner(tmp_path, executor).run(
        prepared, mapping, run_id="run-1", prompt="Implement the requested change", timeout_seconds=30,
    )

    command, cwd, env, timeout, _, stdin = executor.calls[0]
    schema = Path(command[command.index("--output-schema") + 1])
    assert command == [
        command[0], "exec", "--cd", str(prepared.path),
        "--sandbox", "workspace-write",
        "--output-schema", str(schema), "-",
    ]
    assert Path(command[0]).name == "codex.exe"
    assert Path(command[0]).is_relative_to(tmp_path)
    assert schema.is_file()
    assert cwd == prepared.path
    assert timeout == 30
    assert stdin == b"Implement the requested change"
    assert "ONES_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GIT_ASKPASS" not in env
    assert env["CODEX_API_KEY"] == "codex-auth-value"
    assert "CODEX_HOME" not in env
    assert (tmp_path / "runs" / "run-1" / "codex-prompt.txt").read_text(encoding="utf-8") == "Implement the requested change"
    assert result.summary == "implemented safely"
    assert result.changed_files == ("src/app.py",)


def test_long_prompt_is_streamed_on_stdin_and_never_placed_in_argv(tmp_path: Path) -> None:
    prompt = "完整 Wiki 正文" * 5000
    executor = FakeExecutor(
        json.dumps(_payload(changed_files=[], commands=[]))
    )

    _runner(tmp_path, executor).run_preflight(run_id="long-prompt", prompt=prompt)

    command, _, _, _, _, stdin = executor.calls[0]
    assert command[-1] == "-"
    assert prompt not in command
    assert all(len(argument) < 32768 for argument in command)
    assert stdin == prompt.encode("utf-8")
    assert (tmp_path / "runs" / "long-prompt" / "codex-prompt.txt").read_text(
        encoding="utf-8"
    ) == prompt


def test_run_read_only_phase_uses_read_only_sandbox(tmp_path: Path) -> None:
    executor = FakeExecutor()

    _runner(tmp_path, executor).run(
        _prepared(tmp_path),
        _mapping(tmp_path),
        run_id="run-review",
        prompt="Review only",
        allow_changes=False,
    )

    assert executor.calls[0][0][
        executor.calls[0][0].index("--sandbox") + 1
    ] == "read-only"


def test_preflight_runs_without_worktree_in_read_only_sandbox(tmp_path: Path) -> None:
    executor = FakeExecutor(
        json.dumps(_payload(changed_files=[], commands=[], evidence=["sources checked"]))
    )
    runner = _runner(tmp_path, executor)

    result = runner.run_preflight(
        run_id="preflight-1", prompt="Check sources only", timeout_seconds=30
    )

    command, cwd, _, timeout, _, _ = executor.calls[0]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in command
    assert cwd == (tmp_path / "runs" / "preflight-1").resolve()
    assert timeout == 30
    assert result.changed_files == ()


def test_preflight_rejects_claimed_repository_changes(tmp_path: Path) -> None:
    runner = _runner(tmp_path, FakeExecutor())

    with pytest.raises(CodexOutputError):
        runner.run_preflight(run_id="preflight-2", prompt="Check sources only")


def test_run_parses_optional_strict_acceptance_coverage_and_review_flag(tmp_path: Path) -> None:
    payload = _payload(
        acceptance_coverage=[
            {
                "criterion_id": "AC-1",
                "criterion_text": "works",
                "files": ["src/app.py"],
                "repository_files": [],
                "tests": ["pytest"],
            }
        ],
        unrelated_changes_checked=True,
    )

    result = _runner(tmp_path, FakeExecutor(json.dumps(payload))).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="coverage", prompt="implement"
    )

    assert result.acceptance_coverage[0].criterion_id == "AC-1"
    assert result.unrelated_changes_checked is True


def test_root_cause_discards_stage_irrelevant_acceptance_coverage(tmp_path: Path) -> None:
    payload = _payload(
        acceptance_coverage=[{"criterion_id": 9}]
    )

    result = _runner(
        tmp_path,
        FakeExecutor(json.dumps(payload)),
        FakeRepository(changed_files=()),
    ).run_root_cause(
        _prepared(tmp_path),
        _mapping(tmp_path),
        run_id="root-cause-coverage",
        prompt="analyze",
    )

    assert result.acceptance_coverage == ()


def test_root_cause_uses_stage_specific_output_schema(tmp_path: Path) -> None:
    executor = FakeExecutor(
        json.dumps(
            {
                "summary": "verified analysis",
                "risks": [],
                "unresolved_items": [],
                "root_cause_evidence": [],
                "investigation_suggestions": ["collect reproduction evidence"],
                "behavior_before": "login state crosses environments",
                "impact_scope": ["src/app.py"],
                "risk_level": "medium",
            }
        )
    )
    runner = _runner(tmp_path, executor, FakeRepository(changed_files=()))

    result = runner.run_root_cause(
        _prepared(tmp_path),
        _mapping(tmp_path),
        run_id="root-schema",
        prompt="analyze",
    )

    command = executor.calls[0][0]
    schema = Path(command[command.index("--output-schema") + 1])
    assert schema.name == "root-cause-result.schema.json"
    assert result.summary == "verified analysis"
    assert result.changed_files == ()


def test_format_repair_validates_existing_result_without_starting_codex(
    tmp_path: Path,
) -> None:
    raw_output = json.dumps({
        "summary": "converted existing report",
        "risks": [],
        "unresolved_items": [],
        "root_cause_evidence": [],
        "investigation_suggestions": ["add exact reproduction evidence"],
        "behavior_before": "login state crosses environments",
        "impact_scope": ["src/app.py"],
        "risk_level": "medium",
    })
    executor = FakeExecutor()
    runner = _runner(tmp_path, executor, FakeRepository(changed_files=()))

    result = runner.repair_root_cause_result(
        run_id="format-repair",
        raw_output=raw_output,
        validation_hint="workflow result contract",
    )

    assert executor.calls == []
    assert result.summary == "converted existing report"
    assert result.root_cause_evidence == ()


def test_pending_valid_report_resumes_without_codex_or_repository_analysis(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    runner = _runner(tmp_path, executor, FakeRepository(changed_files=()))
    runner._prepare_run_directory("pending-format")
    pending = tmp_path / "runs" / "pending-format" / "pending-root-cause-output.txt"
    pending.write_text(json.dumps({
        "summary": "recovered without reanalysis",
        "risks": [],
        "unresolved_items": [],
        "root_cause_evidence": [],
        "investigation_suggestions": ["collect exact evidence"],
        "behavior_before": "login state crosses environments",
        "impact_scope": ["src/app.py"],
        "risk_level": "medium",
    }), encoding="utf-8")

    result = runner.repair_pending_root_cause_result(run_id="pending-format")

    assert result is not None
    assert result.summary == "recovered without reanalysis"
    assert executor.calls == []
    assert not pending.exists()


def test_completed_prose_activity_is_not_sent_to_codex_for_format_retry(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(json.dumps({
        "summary": "recovered completed analysis",
        "risks": [],
        "unresolved_items": [],
        "root_cause_evidence": [],
        "investigation_suggestions": ["collect exact evidence"],
        "behavior_before": "login state crosses environments",
        "impact_scope": ["src/app.py"],
        "risk_level": "medium",
    }))
    runner = _runner(tmp_path, executor, FakeRepository(changed_files=()))
    directory = runner._prepare_run_directory("activity-recovery")
    runner._record_activity(
        directory,
        "message",
        "Analysis result: verified root cause and selected the best fix",
    )
    runner._record_activity(
        directory,
        "analysis",
        "AI analysis completed (10514 output tokens)",
    )

    with pytest.raises(CodexOutputError, match="invalid structured output"):
        runner.repair_pending_root_cause_result(run_id="activity-recovery")

    assert executor.calls == []


def test_run_parses_repository_qualified_root_cause_evidence(tmp_path: Path) -> None:
    root = {
        "file_path": "src/app.py",
        "repository_file": {"repository_key": "repo", "path": "src/app.py"},
        "location": "app:1",
        "start_line": 1,
        "end_line": 1,
        "symbol": "run",
        "mechanism": "invalid lifecycle",
        "code_excerpt": "run()",
        "call_chain": [],
        "reproduction_test": "tests/test_app.py",
        "reproduction_file": {
            "repository_key": "repo", "path": "tests/test_app.py"
        },
        "test_selector": "tests/test_app.py::test_lifecycle",
        "reproduction_command": "pytest",
        "confidence": 0.9,
        "insufficient_evidence": False,
        "impacted_files": ["src/app.py"],
        "impacted_repository_files": [
            {"repository_key": "repo", "path": "src/app.py"}
        ],
        "fix_steps": ["guard lifecycle"],
        "supporting_points": [{
            "kind": "code",
            "description": "unsafe call",
            "source": "repo",
            "file_path": "src/app.py",
            "repository_file": {"repository_key": "repo", "path": "src/app.py"},
            "snippet": "run()",
            "start_line": 1,
            "end_line": 1,
            "direct_root_cause": True,
        }],
    }
    runner = _runner(
        tmp_path,
        FakeExecutor(json.dumps(_payload(root_cause_evidence=[root]))),
        FakeRepository(changed_files=()),
    )
    result = runner.run_root_cause(
        _prepared(tmp_path), _mapping(tmp_path), run_id="root", prompt="analyze"
    )

    assert result.root_cause_evidence[0].repository_file is not None
    assert result.root_cause_evidence[0].reproduction_file is not None
    activity = runner.activity("root")
    assert "Verified root cause: invalid lifecycle" in activity
    assert "Recommended fix 1: guard lifecycle" in activity
    assert "Planned post-repair validation: pytest" in activity


def test_root_cause_canonicalizes_duplicate_group_paths_before_contract_validation(
    tmp_path: Path,
) -> None:
    root = {
        "file_path": "src/app.py; src/legacy.py",
        "repository_file": {"repository_key": "repo", "path": "src/app.py"},
        "location": "run:1",
        "start_line": 1,
        "end_line": 1,
        "symbol": "run",
        "mechanism": "invalid lifecycle",
        "code_excerpt": "run()",
        "call_chain": [],
        "reproduction_test": "wrong/test.py",
        "reproduction_file": {
            "repository_key": "repo", "path": "tests/test_app.py"
        },
        "test_selector": "wrong/test.py::test_lifecycle",
        "reproduction_command": "pytest",
        "confidence": 0.9,
        "insufficient_evidence": False,
        "impacted_files": ["src/app.py; src/legacy.py"],
        "impacted_repository_files": [
            {"repository_key": "repo", "path": "src/app.py"}
        ],
        "fix_steps": ["guard lifecycle"],
        "supporting_points": [{
            "kind": "cross_file",
            "description": "unsafe call",
            "source": "repo",
            "file_path": "src/app.py; src/legacy.py",
            "repository_file": {"repository_key": "repo", "path": "src/app.py"},
            "snippet": "run()",
            "start_line": 1,
            "end_line": 1,
            "direct_root_cause": True,
        }],
    }
    runner = _runner(tmp_path, FakeExecutor())

    payload = runner._parse_output(
        json.dumps({
            "summary": "verified analysis",
            "risks": [],
            "unresolved_items": [],
            "root_cause_evidence": [root],
            "investigation_suggestions": [],
            "behavior_before": "unsafe lifecycle",
            "impact_scope": ["src/app.py"],
            "risk_level": "medium",
        }),
        root_cause_result=True,
    )
    evidence = payload["root_cause_evidence"][0]

    assert evidence["file_path"] == "src/app.py"
    assert evidence["reproduction_test"] == "tests/test_app.py"
    assert evidence["test_selector"] == "tests/test_app.py::test_lifecycle"
    assert evidence["impacted_files"] == ["src/app.py"]
    assert evidence["supporting_points"][0]["file_path"] == "src/app.py"
    result = runner._result_from_payload(payload)
    assert result.root_cause_evidence[0].supporting_points[0].file_path == "src/app.py"


def test_root_cause_schema_rejects_multi_path_without_qualified_claim(
    tmp_path: Path,
) -> None:
    root = {
        "file_path": "src/app.py",
        "repository_file": None,
        "location": "run:1",
        "start_line": 1,
        "end_line": 1,
        "symbol": "run",
        "mechanism": "invalid lifecycle",
        "code_excerpt": "run()",
        "call_chain": [],
        "reproduction_test": "tests/test_app.py",
        "reproduction_file": None,
        "test_selector": "tests/test_app.py::test_lifecycle",
        "reproduction_command": "pytest",
        "confidence": 0.9,
        "insufficient_evidence": False,
        "impacted_files": ["src/app.py"],
        "impacted_repository_files": [],
        "fix_steps": ["guard lifecycle"],
        "supporting_points": [{
            "kind": "cross_file",
            "description": "unsafe call",
            "source": "repo",
            "file_path": "src/app.py; src/legacy.py",
            "repository_file": None,
            "snippet": "run()",
            "start_line": 1,
            "end_line": 1,
            "direct_root_cause": True,
        }],
    }
    runner = _runner(tmp_path, FakeExecutor())

    with pytest.raises(ValidationError):
        runner._parse_output(
            json.dumps({
                "summary": "verified analysis",
                "risks": [],
                "unresolved_items": [],
                "root_cause_evidence": [root],
                "investigation_suggestions": [],
                "behavior_before": "unsafe lifecycle",
                "impact_scope": ["src/app.py"],
                "risk_level": "medium",
            }),
            root_cause_result=True,
        )


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        json.dumps({key: value for key, value in _payload().items() if key != "summary"}),
        json.dumps(_payload(commit="deadbeef")),
        json.dumps(_payload(commands=[{"command": "pytest", "exit_code": 0}])),
        json.dumps(_payload(changed_files=["../escape.py"])),
        json.dumps(_payload(changed_files=["src//escape.py"])),
        json.dumps(_payload(changed_files=["src/escape.py/"])),
        json.dumps(_payload(changed_files=["docs/outside.md"])),
    ],
)
def test_run_rejects_invalid_or_unsafe_structured_output(tmp_path: Path, stdout: str) -> None:
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, FakeExecutor(stdout)).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


def test_parser_fills_only_safe_structural_defaults(tmp_path: Path) -> None:
    runner = _runner(tmp_path, FakeExecutor())

    payload = runner._parse_output(json.dumps({"summary": "verified analysis"}))

    assert payload["summary"] == "verified analysis"
    assert payload["root_cause_evidence"] == []
    assert payload["changed_files"] == []
    assert payload["commands"] == []
    assert payload["unrelated_changes_checked"] is False


def test_schema_rejection_exposes_only_safe_field_hint(tmp_path: Path) -> None:
    invalid = _payload(
        commands=[{"command": "pytest", "exit_code": 0}]
    )

    with pytest.raises(CodexOutputError) as caught:
        _runner(tmp_path, FakeExecutor(json.dumps(invalid))).run(
            _prepared(tmp_path),
            _mapping(tmp_path),
            run_id="hint",
            prompt="safe prompt",
        )

    assert caught.value.validation_hint == "commands.0 (required)"
    assert "pytest" not in caught.value.validation_hint


def test_run_uses_git_inventory_when_model_claims_do_not_match(tmp_path: Path) -> None:
    repository = FakeRepository(changed_files=("src/other.py",))
    result = _runner(tmp_path, FakeExecutor(), repository).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert result.changed_files == ("src/other.py",)


def test_run_rejects_nonzero_exit_without_leaking_stderr(tmp_path: Path) -> None:
    with pytest.raises(CodexExecutionError) as caught:
        _runner(tmp_path, FakeExecutor(returncode=2)).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "sensitive stderr" not in str(caught.value)


def test_run_maps_timeout_to_safe_error(tmp_path: Path) -> None:
    with pytest.raises(CodexTimeoutError):
        _runner(tmp_path, FakeExecutor(error=subprocess.TimeoutExpired("codex", 2))).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf])
def test_run_rejects_non_finite_timeout_before_executor(
    tmp_path: Path, timeout: float,
) -> None:
    executor = FakeExecutor()
    with pytest.raises(UnsafeCodexRunError, match="timeout"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="safe prompt", timeout_seconds=timeout,
        )
    assert not executor.calls


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, 0.0])
def test_default_executor_rejects_invalid_timeout_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float,
) -> None:
    import src.developer_workflow.codex_runner as module

    monkeypatch.setattr(
        module, "_start_isolated_process",
        lambda *args, **kwargs: pytest.fail("invalid timeout started a process"),
    )
    with pytest.raises(ValueError, match="timeout"):
        module._bounded_subprocess(
            [sys.executable, "--version"], cwd=tmp_path, env={},
            timeout=timeout, max_output_bytes=1024,
        )


def test_process_start_file_not_found_uses_structured_sanitized_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.developer_workflow.codex_runner as module
    from src.developer_workflow.codex_runner import CodexProcessStartError

    canary = "SECRET-CODEX-EXECUTABLE-PATH"
    cause = FileNotFoundError(canary)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(cause),
    )

    with pytest.raises(CodexProcessStartError) as raised:
        module._start_isolated_process(["codex"], cwd=tmp_path, env={"PATH": ""})

    assert isinstance(raised.value, CodexExecutionError)
    assert str(raised.value) == "Codex process could not be started"
    assert raised.value.__cause__ is cause
    assert canary not in str(raised.value)


def test_run_wraps_unexpected_executor_error_without_leaking_details(tmp_path: Path) -> None:
    with pytest.raises(CodexExecutionError) as caught:
        _runner(tmp_path, FakeExecutor(error=RuntimeError("credential-in-error"))).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "credential-in-error" not in str(caught.value)


@pytest.mark.parametrize(
    "executor_error",
    [
        subprocess.TimeoutExpired("codex", 1),
        CodexOutputError("output limit"),
        CodexExecutionError("isolation failed"),
        RuntimeError("unexpected"),
    ],
)
def test_repository_boundary_failure_wins_over_executor_failure(
    tmp_path: Path, executor_error: Exception,
) -> None:
    repository = FakeRepository()

    def guard(prepared: PreparedWorktree) -> None:
        repository.head_checks += 1
        if repository.head_checks == 2:
            raise HeadChangedError("changed during failed execution")

    repository.assert_head_unchanged = guard  # type: ignore[method-assign]
    with pytest.raises(HeadChangedError, match="changed during failed execution"):
        _runner(tmp_path, FakeExecutor(error=executor_error), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="safe prompt",
        )
    assert repository.head_checks == 2


def test_run_checks_repository_identity_and_head_before_and_after(tmp_path: Path) -> None:
    repository = FakeRepository()

    def changed_head(prepared: PreparedWorktree) -> None:
        repository.head_checks += 1
        if repository.head_checks == 2:
            raise HeadChangedError("changed")

    repository.assert_head_unchanged = changed_head  # type: ignore[method-assign]
    with pytest.raises(HeadChangedError):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert repository.head_checks == 2


@pytest.mark.parametrize("run_id", ["../escape", "bad/name", ".", "run id"])
def test_run_rejects_unsafe_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(UnsafeCodexRunError):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id=run_id, prompt="safe prompt",
        )


def test_run_rejects_prompt_containing_removed_credential_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONES_TOKEN", "a-real-secret-value")
    with pytest.raises(UnsafeCodexRunError, match="credential"):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="Use a-real-secret-value while working",
        )


def test_run_removes_all_git_process_control_and_ssh_agent_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = FakeExecutor()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil-helper")
    monkeypatch.setenv("SSH_AUTH_SOCK", "agent-socket")
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    assert not any(
        key in environment
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")
    )
    assert {
        key for key in environment if key.casefold().startswith("git_")
    } == {
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT",
    }
    assert "SSH_AUTH_SOCK" not in environment


def test_run_isolates_home_and_git_credentials_from_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_home = tmp_path / "parent-home"
    parent_appdata = tmp_path / "parent-appdata"
    parent_local = tmp_path / "parent-local"
    parent_home.mkdir()
    parent_appdata.mkdir()
    parent_local.mkdir()
    (parent_home / ".gitconfig").write_text(
        "[credential]\n\thelper = parent-secret-helper\n", encoding="utf-8",
    )
    (parent_home / ".git-credentials").write_text(
        "https://parent-secret@example.invalid\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(parent_home))
    monkeypatch.setenv("USERPROFILE", str(parent_home))
    monkeypatch.setenv("APPDATA", str(parent_appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(parent_local))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(parent_home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(parent_home / ".gitconfig"))
    monkeypatch.setenv("GIT_ASKPASS", "parent-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "parent-ssh-askpass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "parent-agent")

    executor = FakeExecutor()
    prepared = _prepared(tmp_path)
    _runner(tmp_path, executor).run(
        prepared, _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    run_directory = (tmp_path / "runs" / "run-1").resolve()
    expected_locations = ["HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"]
    if os.name == "nt":
        expected_locations.extend(("USERPROFILE", "APPDATA", "LOCALAPPDATA"))
    for name in expected_locations:
        value = Path(environment[name]).resolve()
        assert value != parent_home.resolve()
        assert value != parent_appdata.resolve()
        assert value != parent_local.resolve()
        assert value.is_relative_to(run_directory)
    for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        config = Path(environment[name]).resolve()
        assert config.is_relative_to(run_directory)
        assert config.read_bytes() == b""
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"
    if os.name == "nt":
        powershell_cache = Path(environment["PSModuleAnalysisCachePath"]).resolve()
        assert powershell_cache.is_relative_to(run_directory)
        assert not powershell_cache.is_relative_to(prepared.path.resolve())
    else:
        assert not any(
            name in environment
            for name in (
                "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "PSModuleAnalysisCachePath",
            )
        )
    assert not any(
        name in environment
        for name in ("GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK")
    )


def test_run_uses_minimal_environment_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = FakeExecutor()
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-sensitive-value")
    monkeypatch.setenv("NPM_TOKEN", "npm-sensitive-value")
    monkeypatch.setenv("CI_JOB_TOKEN", "ci-sensitive-value")
    monkeypatch.setenv("RANDOM_PARENT_VALUE", "not-needed")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-auth-value")
    codex_home = (tmp_path / "codex-home").resolve()
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )

    environment = executor.calls[0][2]
    assert environment["OPENAI_API_KEY"] == "openai-auth-value"
    isolated_home = Path(environment["CODEX_HOME"])
    assert isolated_home.is_relative_to(tmp_path / "runs" / "run-1")
    assert isolated_home != codex_home
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "NPM_TOKEN" not in environment
    assert "CI_JOB_TOKEN" not in environment
    assert "RANDOM_PARENT_VALUE" not in environment


def test_run_maps_default_userprofile_codex_login_without_restoring_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_profile = (tmp_path / "parent-profile").resolve()
    parent_home = (tmp_path / "parent-home").resolve()
    codex_home = parent_profile / ".codex"
    codex_home.mkdir(parents=True)
    parent_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    forbidden_skill = codex_home / "skills" / "ones-dev-workflow"
    forbidden_skill.mkdir(parents=True)
    (forbidden_skill / "SKILL.md").write_text("must not be copied", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(parent_profile))
    monkeypatch.setenv("HOME", str(parent_home))
    for name in ("CODEX_HOME", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    isolated_home = Path(environment["CODEX_HOME"])
    assert isolated_home.is_relative_to(tmp_path / "runs" / "run-1")
    assert isolated_home != codex_home
    assert (isolated_home / "auth.json").read_text(encoding="utf-8") == "{}"
    assert not (isolated_home / "skills").exists()
    assert Path(environment["HOME"]).is_relative_to(tmp_path / "runs" / "run-1")
    assert Path(environment["HOME"]) != parent_home
    if os.name == "nt":
        assert Path(environment["USERPROFILE"]).is_relative_to(
            tmp_path / "runs" / "run-1"
        )
        assert Path(environment["USERPROFILE"]) != parent_profile
    else:
        assert "USERPROFILE" not in environment


def test_run_does_not_invent_codex_home_when_default_login_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_profile = (tmp_path / "empty-profile").resolve()
    parent_profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(parent_profile))
    monkeypatch.setenv("HOME", str(parent_profile))
    for name in ("CODEX_HOME", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert "CODEX_HOME" not in executor.calls[0][2]


def test_explicit_codex_home_takes_priority_over_default_user_login_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_profile = (tmp_path / "parent-profile").resolve()
    default_home = parent_profile / ".codex"
    explicit_home = (tmp_path / "explicit-codex-home").resolve()
    default_home.mkdir(parents=True)
    explicit_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(parent_profile))
    monkeypatch.setenv("HOME", str(parent_profile))
    monkeypatch.setenv("CODEX_HOME", str(explicit_home))

    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    isolated_home = Path(executor.calls[0][2]["CODEX_HOME"])
    assert isolated_home.is_relative_to(tmp_path / "runs" / "run-1")
    assert isolated_home != explicit_home


def test_explicit_unsafe_codex_home_is_rejected_without_path_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "relative-parent-secret-codex-home"
    monkeypatch.setenv("CODEX_HOME", secret_path)
    with pytest.raises(UnsafeCodexRunError) as caught:
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert secret_path not in str(caught.value)


def test_reparse_codex_home_is_resolved_without_restoring_parent_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    codex_home = (tmp_path / "reparse-codex-home").resolve()
    codex_home.mkdir()
    original = module._is_reparse_or_link
    monkeypatch.setattr(
        module, "_is_reparse_or_link",
        lambda path: Path(path) == codex_home or original(path),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    assert Path(environment["CODEX_HOME"]).is_relative_to(
        tmp_path / "runs" / "run-1"
    )
    assert Path(environment["CODEX_HOME"]) != codex_home.resolve()
    assert Path(environment["HOME"]) != codex_home.parent


def test_unreadable_codex_home_is_rejected_without_os_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    codex_home = (tmp_path / "unreadable-codex-home").resolve()
    codex_home.mkdir()
    original_scandir = module.os.scandir

    def guarded_scandir(path: object) -> object:
        if Path(path) == codex_home:
            raise PermissionError("parent-secret-permission-detail")
        return original_scandir(path)

    monkeypatch.setattr(module.os, "scandir", guarded_scandir)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    with pytest.raises(UnsafeCodexRunError) as caught:
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "parent-secret-permission-detail" not in str(caught.value)


@pytest.mark.parametrize("environment_name", ["HTTPS_PROXY", "OPENAI_BASE_URL"])
def test_auth_url_userinfo_is_removed_and_treated_as_sensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment_name: str
) -> None:
    executor = FakeExecutor()
    monkeypatch.setenv(environment_name, "https://user:proxy-secret-value@proxy.local:8443")
    with pytest.raises(UnsafeCodexRunError, match="credential"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="Do not copy proxy-secret-value",
        )
    assert not executor.calls


def test_prompt_and_output_cannot_echo_retained_codex_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "codex-sensitive-value")
    with pytest.raises(UnsafeCodexRunError, match="credential"):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="Never copy codex-sensitive-value",
        )
    executor = FakeExecutor(json.dumps(_payload(summary="codex-sensitive-value")))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-2", prompt="safe prompt",
        )


def test_repository_snapshot_cannot_contain_parent_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-file-secret-value")
    repository = FakeRepository()

    def sensitive_snapshot(prepared: PreparedWorktree, mapping: RepositoryMapping) -> RepositorySnapshot:
        return RepositorySnapshot(
            head_commit=OID,
            diff_sha256="b" * 64,
            changed_files=("src/app.py",),
            patch="+aws-file-secret-value",
            is_clean=False,
        )

    repository.snapshot = sensitive_snapshot  # type: ignore[method-assign]
    executor = FakeExecutor(json.dumps(_payload(changed_files=["src/app.py"])))
    with pytest.raises(CodexOutputError, match="invalid structured output") as caught:
        _runner(tmp_path, executor, repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "aws-file-secret-value" not in str(caught.value)


def test_changed_file_content_cannot_contain_parent_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "codex-auth-written-to-file")
    repository = FakeRepository(contains_sensitive_content=True)
    with pytest.raises(CodexOutputError, match="invalid structured output") as caught:
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "codex-auth-written-to-file" not in str(caught.value)
    assert repository.head_checks == 3

def test_default_executor_enforces_timeout_and_output_limit(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        _bounded_subprocess(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path, env=dict(os.environ), timeout=0.05, max_output_bytes=1024,
        )
    with pytest.raises(CodexOutputError, match="output limit"):
        _bounded_subprocess(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
            cwd=tmp_path, env=dict(os.environ), timeout=5, max_output_bytes=1024,
        )


def test_default_executor_streams_stdin_larger_than_windows_command_line_limit(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    content = ("完整 Wiki 正文" * 5000).encode("utf-8")
    completed = _bounded_subprocess(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); print(len(data))",
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=10,
        max_output_bytes=1024,
        stdin=content,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == str(len(content))


def test_default_executor_delivers_stdout_jsonl_while_process_runs(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    observed: list[tuple[str, str]] = []
    completed = _bounded_subprocess(
        [
            sys.executable,
            "-c",
            "print('{\"type\":\"turn.started\"}'); "
            "print('{\"type\":\"turn.completed\"}')",
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=5,
        max_output_bytes=1024,
        on_output_line=lambda name, line: observed.append((name, line)),
    )

    assert completed.returncode == 0
    assert observed == [
        ("stdout", '{"type":"turn.started"}'),
        ("stdout", '{"type":"turn.completed"}'),
    ]


def test_streaming_executor_discards_old_events_and_retains_final_message(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.codex_runner import (
        _bounded_subprocess,
        _final_agent_message,
    )

    final = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "final report"},
    })
    script = f"print('x' * 4096); print({final!r})"
    completed = _bounded_subprocess(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=5,
        max_output_bytes=512,
        retain_output_tail=True,
    )

    assert completed.returncode == 0
    assert len(completed.stdout.encode("utf-8")) <= 512
    assert _final_agent_message(completed.stdout) == "final report"


def test_default_executor_timeout_kills_child_blocking_stdin_writer(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _bounded_subprocess(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.05,
            max_output_bytes=1024,
            stdin=b"x" * (10 * 1024 * 1024),
        )

    assert time.monotonic() - started < 3


def test_default_executor_reaps_descendant_holding_output_pipe(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    child = "import time; time.sleep(10)"
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "sys.stdout.write('done'); sys.stdout.flush()"
    )
    started = time.monotonic()
    completed = _bounded_subprocess(
        [sys.executable, "-c", parent], cwd=tmp_path, env=dict(os.environ),
        timeout=5, max_output_bytes=1024,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 0
    assert completed.stdout == "done"
    assert elapsed < 2


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launcher invariant")
def test_windows_launcher_assigns_job_before_resuming_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    events: list[str] = []

    class FakeProcess:
        _handle = 42

    process = FakeProcess()

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        flags = int(kwargs["creationflags"])
        assert flags & 0x00000004  # CREATE_SUSPENDED
        events.append("spawn-suspended")
        return process

    class FakeGuard:
        def __init__(self, actual: object) -> None:
            assert actual is process
            events.append("assigned-job")

    def fake_resume(actual: object) -> None:
        assert actual is process
        assert events == ["spawn-suspended", "assigned-job"]
        events.append("resumed")

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "_ProcessTreeGuard", FakeGuard)
    monkeypatch.setattr(module, "_resume_suspended_process", fake_resume)

    actual_process, guard = module._start_isolated_process(
        ["codex", "exec"], cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")},
    )
    assert actual_process is process
    assert isinstance(guard, FakeGuard)
    assert events == ["spawn-suspended", "assigned-job", "resumed"]


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launcher invariant")
def test_windows_launcher_failure_kills_suspended_process_before_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    events: list[str] = []

    class FakeProcess:
        _handle = 42

        def kill(self) -> None:
            events.append("kill-suspended")

        def wait(self, timeout: float) -> int:
            events.append("wait")
            return 1

    process = FakeProcess()

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        assert int(kwargs["creationflags"]) & 0x00000004
        events.append("spawn-suspended")
        return process

    class FailingGuard:
        def __init__(self, actual: object) -> None:
            events.append("job-failed")
            raise OSError("job unavailable")

    def must_not_resume(actual: object) -> None:
        raise AssertionError("an unassigned process must never resume")

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "_ProcessTreeGuard", FailingGuard)
    monkeypatch.setattr(module, "_resume_suspended_process", must_not_resume)

    with pytest.raises(CodexExecutionError, match="isolated"):
        module._start_isolated_process(
            ["codex", "exec"], cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
        )
    assert events == ["spawn-suspended", "job-failed", "kill-suspended", "wait"]


def test_run_rejects_snapshot_head_race_and_guards_after_snapshot(tmp_path: Path) -> None:
    class RacingRepository(FakeRepository):
        def snapshot(self, prepared: PreparedWorktree, mapping: RepositoryMapping) -> RepositorySnapshot:
            return RepositorySnapshot(
                head_commit="b" * 40, diff_sha256="b" * 64,
                changed_files=("src/app.py",), patch="diff", is_clean=False,
            )

    repository = RacingRepository()
    with pytest.raises(HeadChangedError):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert repository.head_checks == 2


def test_run_performs_full_repository_guard_after_snapshot(tmp_path: Path) -> None:
    repository = FakeRepository()

    def guard(prepared: PreparedWorktree) -> None:
        repository.head_checks += 1
        if repository.head_checks == 3:
            raise HeadChangedError("raced after snapshot")

    repository.assert_head_unchanged = guard  # type: ignore[method-assign]
    with pytest.raises(HeadChangedError):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert repository.head_checks == 3


def test_run_rejects_snapshot_content_race_after_sensitive_file_scan(
    tmp_path: Path,
) -> None:
    class RacingRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_calls = 0

        def snapshot(
            self, prepared: PreparedWorktree, mapping: RepositoryMapping,
        ) -> RepositorySnapshot:
            self.snapshot_calls += 1
            return RepositorySnapshot(
                head_commit=OID,
                diff_sha256=("b" if self.snapshot_calls == 1 else "c") * 64,
                changed_files=("src/app.py",), patch="diff", is_clean=False,
            )

    repository = RacingRepository()
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="safe prompt",
        )
    assert repository.snapshot_calls == 2
    assert repository.head_checks == 3


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m generated",
        "git -c user.name=AI push origin branch",
        "gh pr create --title generated",
        "gh --repo org/repo pr create --title generated",
        "curl -X PATCH http://aputureones.local/api/tasks/1",
        "curl --request=DELETE http://aputureones.local/api/tasks/1",
        "curl https://aputureones.local/api/tasks -d '{\"status\":\"done\"}'",
        "curl --form file=@report.txt https://example.invalid/upload",
        "curl -dstatus=done https://aputureones.local/api/tasks",
        "curl -Ffile=@report.txt https://example.invalid/upload",
        "curl -Treport.txt https://example.invalid/upload",
        "gh api --method PATCH repos/org/repo/pulls/1",
        "gh api repos/org/repo/pulls -f title=generated",
        "git send-pack origin refs/heads/main",
        "git-remote-http origin https://example.invalid/repo.git",
        "python ones_client.py --method PATCH --url http://aputureones.local/task/1",
        "ones --method patch task task-1",
        "ones task update task-1",
    ],
)
def test_run_rejects_self_reported_publication_commands(tmp_path: Path, command: str) -> None:
    executor = FakeExecutor(json.dumps(_payload(commands=[{
        "command": command, "exit_code": 0, "summary": "done",
    }])))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


def test_run_accepts_read_only_and_verification_commands(tmp_path: Path) -> None:
    commands = [
        {"command": "git diff --check", "exit_code": 0, "summary": "clean"},
        {"command": "git remote -v", "exit_code": 0, "summary": "listed"},
        {"command": "git diff -- src/remote/client.py", "exit_code": 0, "summary": "read"},
        {"command": "git diff -- 'src/remote client.py' | rg update", "exit_code": 0, "summary": "read"},
        {"command": "'C:/Program Files/Git/bin/git.exe' log --grep='commit update'", "exit_code": 0, "summary": "read"},
        {"command": "git log --grep=commit", "exit_code": 0, "summary": "read"},
        {"command": "gh pr view 12", "exit_code": 0, "summary": "read"},
        {"command": "gh api --method GET repos/org/repo", "exit_code": 0, "summary": "read"},
        {"command": "curl -fsS https://example.invalid/status", "exit_code": 0, "summary": "read"},
        {"command": "ones task get task-1", "exit_code": 0, "summary": "read"},
        {"command": "bash -lc 'git status && pytest -q'", "exit_code": 0, "summary": "read"},
        {"command": "( git status )", "exit_code": 0, "summary": "read"},
        {"command": "timeout 10 pytest -q", "exit_code": 0, "summary": "passed"},
        {"command": "python -m pytest -q", "exit_code": 0, "summary": "passed"},
        {
            "command": (
                r"D:\DevelopeEnviroment\Python311\python.exe -m pytest "
                "tests/test_user_repository_encryption.py "
                "tests/test_user_repository_keyring.py"
            ),
            "exit_code": 1,
            "summary": "dependency unavailable",
        },
        {"command": "python -m py_compile tests/test_example.py; git diff --check", "exit_code": 0, "summary": "validated"},
        {"command": "pytest -q", "exit_code": 0, "summary": "passed"},
        {"command": "pytest -k update", "exit_code": 0, "summary": "passed"},
        {"command": "rg update src", "exit_code": 0, "summary": "searched"},
        {"command": "rg 'update; commit' src", "exit_code": 0, "summary": "searched"},
    ]
    result = _runner(tmp_path, FakeExecutor(json.dumps(_payload(commands=commands)))).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert tuple(item.command for item in result.commands) == tuple(
        item["command"] for item in commands
    )


def test_command_policy_accepts_exact_read_only_inline_test_compile() -> None:
    command = (
        "python -c \"compile(open('tests/test_example.py', encoding='utf-8').read(), "
        "'tests/test_example.py', 'exec')\""
    )

    assert codex_runner_module._is_forbidden_command(command) is False


@pytest.mark.parametrize("command", [
    "Get-FileHash -Algorithm SHA256 tests/test_user_repository_encryption.py",
    "Get-FileHash -LiteralPath 'tests/test example.py' -Algorithm SHA256",
    'powershell -Command "Get-FileHash -Algorithm SHA256 tests/test_example.py"',
])
def test_read_only_file_hash_command_is_accepted(tmp_path: Path, command: str) -> None:
    runner = _runner(tmp_path, FakeExecutor())
    result = runner._validate_output(json.dumps(_payload(commands=[{
        "command": command, "exit_code": 0, "summary": "frozen evidence verified",
    }])), _mapping(tmp_path))
    assert result["commands"][0]["command"] == command


@pytest.mark.parametrize("command", [
    "Get-FileHash -Algorithm SHA256 tests/test_example.py; git push origin main",
    "Get-FileHash -Path $(Remove-Item tests/test_example.py)",
    "Get-FileHash -InputStream $stream",
    "Get-FileHash -LiteralPath tests/test_example.py -OutVariable result",
    "Get-FileHash -Algorithm unknown tests/test_example.py",
])
def test_file_hash_allowlist_does_not_admit_dynamic_or_mutating_commands(command: str) -> None:
    assert codex_runner_module._is_forbidden_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        (
            "python -c \"compile(open('src/app.py', encoding='utf-8').read(), "
            "'src/app.py', 'exec')\""
        ),
        (
            "python -c \"compile(open('../tests/test_example.py', encoding='utf-8').read(), "
            "'../tests/test_example.py', 'exec')\""
        ),
        (
            "python -c \"compile(open('tests/test_example.py', encoding='utf-8').read(), "
            "'tests/other.py', 'exec')\""
        ),
        (
            "python -c \"compile(open('tests/test_example.py', 'w', encoding='utf-8').read(), "
            "'tests/test_example.py', 'exec')\""
        ),
        (
            "python -c \"import subprocess; "
            "subprocess.run(['git', 'push'])\""
        ),
    ],
)
def test_run_rejects_python_inline_code_outside_exact_test_compile_contract(
    tmp_path: Path, command: str,
) -> None:
    payload = json.dumps(_payload(commands=[{
        "command": command, "exit_code": 0, "summary": "done",
    }]))
    runner = _runner(tmp_path, FakeExecutor())

    with pytest.raises(CodexOutputError, match="invalid structured output") as caught:
        runner._validate_output(payload, _mapping(tmp_path))

    assert caught.value.validation_hint == "commands (unsafe_command)"


def test_only_root_cause_validation_failure_persists_pending_output(
    tmp_path: Path,
) -> None:
    invalid = json.dumps(_payload(commands=[{
        "command": "python -c \"import os; os.remove('src/app.py')\"",
        "exit_code": 0,
        "summary": "unsafe",
    }]))
    runner = _runner(tmp_path, FakeExecutor(invalid))

    with pytest.raises(CodexOutputError):
        runner.run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="ordinary-stage",
            prompt="safe prompt",
        )
    assert not (
        tmp_path / "runs" / "ordinary-stage" / "pending-root-cause-output.txt"
    ).exists()

    root_runner = _runner(tmp_path, FakeExecutor("{}"), FakeRepository(changed_files=()))
    with pytest.raises(CodexOutputError):
        root_runner.run_root_cause(
            _prepared(tmp_path), _mapping(tmp_path), run_id="root-stage",
            prompt="analyze",
        )
    pending = tmp_path / "runs" / "root-stage" / "pending-root-cause-output.txt"
    assert pending.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize(
    "command",
    [
        "git diff --check && git push origin branch",
        "git remote -v; gh api --method DELETE repos/org/repo/git/refs/heads/x",
        "sh -c 'git -C repo -c user.name=AI commit -m generated'",
        "cmd /c git --git-dir repo/.git push origin branch",
        "powershell -Command \"curl -dstatus=done https://aputureones.local/api/tasks\"",
        "eval 'ones task update task-1'",
        "sudo git push origin branch",
        "sudo -u bot git commit -m generated",
        "command -- git push origin branch",
        "nohup git send-pack origin refs/heads/main",
        "nice -n 5 git push origin branch",
        "git push 'unterminated",
        "git -c alias.ship=push ship origin branch",
        "git --config-env alias.ship=GIT_ALIAS ship origin branch",
        "git $ACTION origin branch",
        "curl $CURL_ARGS https://aputureones.local/api/tasks",
        "gh $SUBCOMMAND create",
        "bash -lc 'git push origin branch'",
        "( git push origin branch )",
        "{ gh pr comment 1 --body generated; }",
        "timeout 10 git push origin branch",
        "python -c \"import subprocess; subprocess.run(['git','push'])\"",
        "node -e \"require('child_process').execSync('git push')\"",
        "gh repo create generated --private",
        "gh pr comment 1 --body generated",
        "curl --config request.conf https://example.invalid/status",
        "git add src/app.py",
        "if true; then git push origin branch; fi",
        "while false; do gh pr comment 1 --body generated; done",
        "powershell -EncodedCommand Z2ggcHIgY3JlYXRl",
        "powershell -File publish.ps1",
        "python tools/publish.py",
        r"D:\DevelopeEnviroment\Python311\python.exe tools/publish.py",
        "node tools/publish.js",
        "http https://aputureones.local/api/tasks status=done",
        "ones task synchronize task-1",
        "make publish",
    ],
)
def test_run_rejects_write_command_hidden_by_shell_or_global_options(
    tmp_path: Path, command: str
) -> None:
    executor = FakeExecutor(json.dumps(_payload(commands=[{
        "command": command, "exit_code": 0, "summary": "done",
    }])))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


@pytest.mark.parametrize(
    "command",
    [
        "python -m unittest -q",
        "python -m compileall -q src",
        "uv run pytest -q",
        "make test",
        "ruff check src",
    ],
)
def test_run_accepts_explicit_local_test_and_read_only_commands(
    tmp_path: Path, command: str,
) -> None:
    result = _runner(
        tmp_path,
        FakeExecutor(json.dumps(_payload(commands=[{
            "command": command, "exit_code": 0, "summary": "passed",
        }]))),
    ).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert result.commands[0].command == command


def test_run_rejects_oversized_prompt_and_output(tmp_path: Path) -> None:
    runner = _TestingCodexRunner(
        run_root=(tmp_path / "runs").resolve(), repository=FakeRepository(),
        command_executor=FakeExecutor(), max_prompt_bytes=8,
        command_resolver=lambda: _attested_command(tmp_path),
    )
    with pytest.raises(UnsafeCodexRunError, match="prompt"):
        runner.run(_prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="too long prompt")

    executor = FakeExecutor(json.dumps(_payload(summary="x" * 500)))
    runner = _TestingCodexRunner(
        run_root=(tmp_path / "other-runs").resolve(), repository=FakeRepository(),
        command_executor=executor, max_output_bytes=100,
        command_resolver=lambda: _attested_command(tmp_path),
    )
    with pytest.raises(CodexOutputError, match="output limit"):
        runner.run(_prepared(tmp_path), _mapping(tmp_path), run_id="run-2", prompt="safe")


def test_existing_symlinked_run_directory_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("creating a symlink requires privileges on Windows")
    outside = tmp_path / "outside"
    outside.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "run-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeCodexRunError):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe",
        )


def test_jsonl_activity_exposes_actions_without_reasoning_or_command_secrets() -> None:
    reasoning = codex_runner_module._codex_activity_from_event(
        json.dumps({
            "type": "item.started",
            "item": {"type": "reasoning", "text": "private chain of thought"},
        }),
        (),
    )
    command = codex_runner_module._codex_activity_from_event(
        json.dumps({
            "type": "item.started",
            "item": {
                "type": "command_execution",
                "command": "curl -H Authorization=secret-value https://example.test",
            },
        }),
        ("secret-value",),
    )

    assert reasoning == ("reasoning", "Evaluating repository evidence")
    assert "private chain of thought" not in reasoning[1]
    assert command is not None
    assert "secret-value" not in command[1]
    assert "[redacted]" in command[1]


def test_jsonl_activity_exposes_safe_agent_updates_and_structured_summary() -> None:
    update = codex_runner_module._codex_activity_from_event(
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "I found the failing call path in src/window.py; token=TOKEN-VALUE",
            },
        }),
        ("TOKEN-VALUE",),
    )
    result = codex_runner_module._codex_activity_from_event(
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps({"summary": "The stale handle is reused."}),
            },
        }),
        (),
    )

    assert update == (
        "message",
        "AI update: I found the failing call path in src/window.py; token=[redacted]",
    )
    assert result == ("message", "Analysis result: The stale handle is reused.")


def test_jsonl_final_agent_message_is_used_as_structured_result() -> None:
    expected = json.dumps(_payload(changed_files=[], commands=[]))
    stream = "\n".join((
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": expected,
        }}),
        json.dumps({"type": "turn.completed", "usage": {"output_tokens": 42}}),
    ))

    assert codex_runner_module._final_agent_message(stream) == expected


def test_default_streaming_runner_uses_authoritative_last_message_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = json.dumps(_payload(changed_files=[], commands=[]))
    child_message = "A delegated investigator finished after the main analysis."
    session_id = "019d2f7c-3bb7-7d21-a133-9d0d278b93b4"

    def streaming_executor(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
        on_output_line: object = None,
        retain_output_tail: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout, max_output_bytes, stdin, on_output_line
        assert retain_output_tail is True
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(expected, encoding="utf-8")
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": session_id}),
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": expected,
                }}),
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": child_message,
                }}),
                json.dumps({"type": "turn.completed"}),
            )
        )
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(codex_runner_module, "_bounded_subprocess", streaming_executor)
    runner = _TestingCodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=streaming_executor,
        command_resolver=lambda: _attested_command(tmp_path),
    )

    result = runner.run_preflight(run_id="stream-final", prompt="analyze")

    assert result.summary == "implemented safely"
    prompt_directory = tmp_path / "runs" / "stream-final"
    assert not tuple(prompt_directory.glob(".codex-final-*.json"))


def test_streaming_internal_event_secret_does_not_reject_safe_final_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "codex-sensitive-transport-value"
    expected = json.dumps(_payload(changed_files=[], commands=[]))
    session_id = "019d2f7c-3bb7-7d21-a133-9d0d278b93b7"
    monkeypatch.setenv("CODEX_API_KEY", secret)

    def streaming_executor(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
        on_output_line: object = None,
        retain_output_tail: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout, max_output_bytes, stdin
        assert retain_output_tail is True
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(expected, encoding="utf-8")
        lines = (
            json.dumps({"type": "thread.started", "thread_id": session_id}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": f"internal event {secret}",
            }}),
            json.dumps({"type": "turn.completed"}),
        )
        if callable(on_output_line):
            for line in lines:
                on_output_line("stdout", line)
        return subprocess.CompletedProcess(command, 0, "\n".join(lines), "")

    monkeypatch.setattr(codex_runner_module, "_bounded_subprocess", streaming_executor)
    runner = _TestingCodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=streaming_executor,
        command_resolver=lambda: _attested_command(tmp_path),
    )

    result = runner.run_preflight(run_id="transport-secret", prompt="analyze")

    assert result.summary == "implemented safely"
    assert all(secret not in entry for entry in runner.activity("transport-secret"))


def test_default_streaming_runner_falls_back_when_last_message_file_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = json.dumps(_payload(changed_files=[], commands=[]))
    session_id = "019d2f7c-3bb7-7d21-a133-9d0d278b93b5"

    def streaming_executor(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
        on_output_line: object = None,
        retain_output_tail: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout, max_output_bytes, stdin, on_output_line
        assert retain_output_tail is True
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": session_id}),
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": expected,
                }}),
                json.dumps({"type": "turn.completed"}),
            )
        )
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(codex_runner_module, "_bounded_subprocess", streaming_executor)
    runner = _TestingCodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(),
        command_executor=streaming_executor,
        command_resolver=lambda: _attested_command(tmp_path),
    )

    result = runner.run_preflight(run_id="stream-fallback", prompt="analyze")

    assert result.summary == "implemented safely"


def test_default_streaming_runner_resumes_one_session_across_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = json.dumps(_payload(changed_files=[], commands=[]))
    session_id = "019d2f7c-3bb7-7d21-a133-9d0d278b93b6"
    commands: list[list[str]] = []

    def streaming_executor(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
        on_output_line: object = None,
        retain_output_tail: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, max_output_bytes, stdin, on_output_line
        assert retain_output_tail is True
        commands.append(command)
        session_path = (
            Path(env["CODEX_HOME"])
            / "sessions" / "2026" / "08" / "10"
            / f"rollout-{session_id}.jsonl"
        )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text("{}\n", encoding="utf-8")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(expected, encoding="utf-8")
        stream = "\n".join((
            json.dumps({"type": "thread.started", "thread_id": session_id}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": expected,
            }}),
            json.dumps({"type": "turn.completed"}),
        ))
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(codex_runner_module, "_bounded_subprocess", streaming_executor)
    runner = _TestingCodexRunner(
        run_root=(tmp_path / "runs").resolve(),
        repository=FakeRepository(changed_files=()),
        command_executor=streaming_executor,
        command_resolver=lambda: _attested_command(tmp_path),
    )

    first = runner.run_preflight(run_id="same-session", prompt="analyze")
    second = runner.run_preflight(run_id="same-session", prompt="continue repair")

    assert first.summary == second.summary == "implemented safely"
    assert commands[0][1] == "exec"
    assert "resume" not in commands[0]
    assert commands[1][1:3] == ["exec", "resume"]
    assert session_id in commands[1]
    assert "--cd" not in commands[1]
    assert (
        tmp_path / "runs" / "same-session" / "codex-session-id.txt"
    ).read_text(encoding="ascii").strip() == session_id


def test_codex_activity_is_persisted_as_a_bounded_sanitized_trace(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, FakeExecutor())
    directory = runner._prepare_run_directory("activity-run")
    runner._record_activity(directory, "command", "Running: rg -n defect src")
    runner._record_activity(directory, "command", "Command completed: rg (exit 0)")

    assert runner.activity("activity-run") == (
        "Running: rg -n defect src",
        "Command completed: rg (exit 0)",
    )
