from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import ctypes
from ctypes import wintypes

import pytest

import src.developer_workflow.setup_store as setup_store_module
from src.developer_workflow.config import DeveloperWorkflowConfig, PublishingConfig
from src.developer_workflow.contracts import RepositoryMapping
from src.developer_workflow.credential_store import CredentialStoreError
from src.developer_workflow.platform_support import HostPaths
from src.developer_workflow.private_paths import PrivatePathError, prepare_private_directory
from src.developer_workflow.setup_models import (
    ActiveSetup,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
    SetupDocument,
)
from src.developer_workflow.setup_store import SetupStore, SetupStoreError


class FakeCredentials:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], RuntimeSecrets] = {}
        self.fail_delete = False
        self.writes: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    def write_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> None:
        self.writes.append((profile_id, generation))
        key = (profile_id, generation)
        if key in self.data:
            raise CredentialStoreError("credential operation failed")
        self.data[key] = secrets

    def write_fresh_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool:
        key = (profile_id, generation)
        if key in self.data:
            raise CredentialStoreError("credential operation failed")
        self.writes.append(key)
        self.data[key] = secrets
        return True

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets:
        try:
            stored = self.data[(profile_id, generation)]
            return RuntimeSecrets({kind: stored.require(kind) for kind in kinds})
        except (KeyError, ValueError):
            raise CredentialStoreError("credential operation failed") from None

    def delete_generation(self, profile_id: str, generation: str) -> None:
        self.deletes.append((profile_id, generation))
        if self.fail_delete:
            raise CredentialStoreError("credential operation failed")
        self.data.pop((profile_id, generation), None)

    def list_generations(self, profile_id: str) -> tuple[str, ...]:
        return tuple(sorted(g for p, g in self.data if p == profile_id))


class IdempotentFakeCredentials(FakeCredentials):
    def write_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> None:
        self.writes.append((profile_id, generation))
        key = (profile_id, generation)
        existing = self.data.get(key)
        if existing is not None and existing.values != secrets.values:
            raise CredentialStoreError("credential operation failed")
        self.data[key] = secrets


class SharedCredentials:
    """Credential protocol backed by multiprocessing manager proxies."""

    def __init__(self, data: object, lock: object) -> None:
        self.data = data
        self.lock = lock

    def write_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> None:
        with self.lock:  # type: ignore[attr-defined]
            existing = tuple(
                key
                for key in self.data.keys()  # type: ignore[attr-defined]
                if key[:2] == (profile_id, generation)
            )
            if existing:
                raise CredentialStoreError("credential operation failed")
            for kind, value in secrets.values.items():
                self.data[(profile_id, generation, kind.value)] = value  # type: ignore[index]

    def write_fresh_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool:
        with self.lock:  # type: ignore[attr-defined]
            if any(
                key[:2] == (profile_id, generation)
                for key in self.data.keys()  # type: ignore[attr-defined]
            ):
                raise CredentialStoreError("credential operation failed")
            for kind, value in secrets.values.items():
                self.data[(profile_id, generation, kind.value)] = value  # type: ignore[index]
        return True

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets:
        try:
            return RuntimeSecrets(
                {
                    kind: self.data[(profile_id, generation, kind.value)]  # type: ignore[index]
                    for kind in kinds
                }
            )
        except KeyError:
            raise CredentialStoreError("credential operation failed") from None

    def delete_generation(self, profile_id: str, generation: str) -> None:
        with self.lock:  # type: ignore[attr-defined]
            for key in tuple(self.data.keys()):  # type: ignore[attr-defined]
                if key[:2] == (profile_id, generation):
                    del self.data[key]  # type: ignore[index]

    def list_generations(self, profile_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    key[1]
                    for key in self.data.keys()  # type: ignore[attr-defined]
                    if key[0] == profile_id
                }
            )
        )


def _candidate(tmp_path: Path, generation: str) -> ActiveSetup:
    return ActiveSetup(
        generation=generation,
        runtime=RuntimePublicConfig(
            ones_base_url="https://ones.example.invalid",
            ones_team_id="team",
            ones_issue_type_id="type",
            ones_comment_list_path_template=(
                "/project/api/project/team/{team_id}/task/{item_id}/comment"
            ),
            provider_host="github.example.invalid",
            provider_api_url="https://github.example.invalid/api/v3",
            git_author_name="ONES Agent",
            git_author_email="agent@example.invalid",
            codex_auth_mode="credential",
        ),
        workflow=DeveloperWorkflowConfig(
            run_root=(tmp_path / "runs").resolve(),
            mirror_root=(tmp_path / "mirrors").resolve(),
            worktree_root=(tmp_path / "worktrees").resolve(),
            sandbox_permission_profile="tests",
            max_codex_attempts=3,
            repositories=(
                RepositoryMapping(
                    key="repo",
                    project_id="project",
                    iteration_id="iteration",
                    repo_url="https://example.invalid/repo.git",
                    repo_name="repo",
                ),
            ),
            publishing=PublishingConfig(provider="local_fake"),
        ),
        credential_kinds=(SecretKind.ONES_PASSWORD,),
    )


def _secrets(value: str = "TOKEN-SECRET") -> RuntimeSecrets:
    return RuntimeSecrets({SecretKind.ONES_PASSWORD: value})


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return tuple(result)


_EXPECTED_OWNER_ERRORS = {5, 1307, 1314}


def _select_assignable_owner(
    attempts: tuple[tuple[str, str], ...], setter: object
) -> str:
    failures: list[str] = []
    for label, sid in attempts:
        try:
            setter(sid)  # type: ignore[operator]
        except OSError as error:
            code = getattr(error, "winerror", None)
            if code not in _EXPECTED_OWNER_ERRORS:
                raise AssertionError(
                    f"unexpected owner error for {label}: {code}"
                ) from None
            failures.append(f"{label}={code}")
            continue
        return sid
    pytest.skip("owner assignment unavailable: " + ", ".join(failures))


def _commit_process(
    config_path: str,
    root: str,
    generation: str,
    credentials: object,
    ready: object,
    start: object,
) -> None:
    setup = SetupStore(credentials, config_path=Path(config_path))  # type: ignore[arg-type]
    ready.put(True)  # type: ignore[attr-defined]
    start.wait(10)  # type: ignore[attr-defined]
    try:
        setup.commit(
            "profile-1", _candidate(Path(root), generation), _secrets(generation)
        )
    except SetupStoreError as error:
        if str(error) != "configuration activation is already pending":
            raise


def _fresh_claim_process(
    config_path: str,
    root: str,
    generation: str,
    credentials: object,
    start: object,
    results: object,
    fail_before_replace: bool = False,
) -> None:
    setup = SetupStore(credentials, config_path=Path(config_path))  # type: ignore[arg-type]
    if fail_before_replace:
        setup._atomic_write = lambda _document: (_ for _ in ()).throw(  # type: ignore[method-assign]
            OSError("TOKEN-SECRET target-path")
        )
    start.wait(10)  # type: ignore[attr-defined]
    try:
        setup.commit(
            "profile-1", _candidate(Path(root), generation), _secrets(generation)
        )
    except SetupStoreError:
        results.put(False)  # type: ignore[attr-defined]
    else:
        results.put(True)  # type: ignore[attr-defined]


@pytest.fixture
def store(tmp_path: Path) -> tuple[SetupStore, FakeCredentials, Path]:
    credentials = FakeCredentials()
    path = tmp_path / "local" / "ones-dev" / "config.json"
    return SetupStore(credentials, config_path=path), credentials, path


def test_default_path_uses_platform_support_source_of_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = tmp_path / "platform" / "ones-dev" / "config.json"
    monkeypatch.setattr(
        setup_store_module,
        "default_host_paths",
        lambda: HostPaths(
            config_path=expected,
            cache_root=tmp_path / "cache",
            credential_lock_root=tmp_path / "locks",
        ),
    )
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    configured = SetupStore(FakeCredentials())

    assert configured.config_path == expected


def test_explicit_config_path_does_not_depend_on_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "application-support" / "ones-dev" / "config.json"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    configured = SetupStore(FakeCredentials(), config_path=explicit)

    assert configured.config_path == explicit


def test_prepare_private_directory_is_private_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    private = prepare_private_directory(tmp_path / "private")
    assert private.is_dir()
    if os.name != "nt":
        assert private.stat().st_mode & 0o077 == 0
    link = tmp_path / "link"
    try:
        link.symlink_to(private, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(PrivatePathError, match="^private workflow root is unsafe$"):
        prepare_private_directory(link)


def test_commit_switches_generation_and_reads_only_active_kinds(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, _, _ = store
    first = setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.finalize_activation("profile-1", "a" * 32)
    second = setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))

    assert setup.load() == second
    assert second.previous == first.active
    assert setup.read_active_secrets(second).require(SecretKind.ONES_PASSWORD) == "new"


def test_replace_active_workflow_persists_without_rotating_credentials(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, _ = store
    committed = setup.commit(
        "profile-1", _candidate(tmp_path, "a" * 32), _secrets("stable")
    )
    setup.finalize_activation("profile-1", "a" * 32)
    assert committed.active is not None
    workflow = DeveloperWorkflowConfig.model_validate(
        committed.active.workflow.model_dump(mode="python", round_trip=True)
    )
    workflow.tui_max_concurrency = 4

    updated = setup.replace_active_workflow(
        "profile-1", "a" * 32, workflow
    )

    assert updated.active is not None
    assert updated.active.generation == "a" * 32
    assert updated.active.workflow.tui_max_concurrency == 4
    assert credentials.writes == [("profile-1", "a" * 32)]
    assert credentials.deletes == []
    assert setup.read_active_secrets(updated).require(SecretKind.ONES_PASSWORD) == (
        "stable"
    )


def test_restore_previous_clears_first_failed_generation(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, _ = store
    failed = setup.commit(
        "profile-1", _candidate(tmp_path, "f" * 32), _secrets("failed")
    )
    assert failed.previous is None

    restored = setup.restore_previous("profile-1", "f" * 32)

    assert restored.active is None
    assert restored.previous is None
    assert ("profile-1", "f" * 32) not in credentials.data
    assert setup.orphan_generations() == ()


def test_restore_first_generation_write_failure_keeps_active_and_credentials(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, _ = store
    failed = setup.commit(
        "profile-1", _candidate(tmp_path, "f" * 32), _secrets("failed")
    )
    monkeypatch.setattr(
        setup,
        "_atomic_write",
        lambda _document: (_ for _ in ()).throw(OSError("TOKEN-SECRET")),
    )

    with pytest.raises(SetupStoreError, match="^configuration save failed$") as error:
        setup.restore_previous("profile-1", "f" * 32)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert setup.load() == failed
    assert ("profile-1", "f" * 32) in credentials.data
    assert credentials.deletes == []


def test_restore_first_generation_post_replace_failure_never_deletes_active_credentials(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "f" * 32), _secrets("failed"))
    monkeypatch.setattr(
        "src.developer_workflow.setup_store._fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("TOKEN-SECRET")),
    )

    restored = setup.restore_previous("profile-1", "f" * 32)

    assert restored.active is None
    assert restored.previous is None
    assert ("profile-1", "f" * 32) not in credentials.data


@pytest.mark.parametrize("referenced", ("active", "previous"))
def test_commit_rejects_referenced_generation_before_any_mutation(
    tmp_path: Path, referenced: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = IdempotentFakeCredentials()
    path = tmp_path / "local" / "ones-dev" / "config.json"
    setup = SetupStore(credentials, config_path=path)
    first = setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.finalize_activation("profile-1", "a" * 32)
    if referenced == "active":
        generation = "a" * 32
    else:
        setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))
        generation = "a" * 32
    credentials.writes.clear()
    credentials.deletes.clear()
    atomic_calls = 0
    original = setup._atomic_write

    def count_write(document: object) -> None:
        nonlocal atomic_calls
        atomic_calls += 1
        original(document)  # type: ignore[arg-type]

    monkeypatch.setattr(setup, "_atomic_write", count_write)
    expected = (
        "configuration generation is unavailable"
        if referenced == "active"
        else "configuration activation is already pending"
    )
    with pytest.raises(SetupStoreError, match=f"^{expected}$"):
        setup.commit("profile-1", _candidate(tmp_path, generation), _secrets("old"))

    assert credentials.writes == []
    assert credentials.deletes == []
    assert atomic_calls == 0
    if referenced == "previous":
        assert setup.load().previous == first.active


def test_preexisting_orphan_generation_is_never_deleted_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = IdempotentFakeCredentials()
    path = tmp_path / "local" / "ones-dev" / "config.json"
    setup = SetupStore(credentials, config_path=path)
    generation = "c" * 32
    credentials.data[("profile-1", generation)] = _secrets("existing")
    monkeypatch.setattr(
        setup,
        "_atomic_write",
        lambda _document: (_ for _ in ()).throw(OSError("TOKEN-SECRET")),
    )

    with pytest.raises(SetupStoreError, match="^configuration generation is unavailable$"):
        setup.commit(
            "profile-1", _candidate(tmp_path, generation), _secrets("existing")
        )

    assert credentials.writes == []
    assert credentials.deletes == []
    assert ("profile-1", generation) in credentials.data


def test_commit_uses_only_atomic_fresh_claim_for_generation_ownership(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, _ = store
    fresh_calls: list[tuple[str, str]] = []
    original_fresh = credentials.write_fresh_generation

    def fresh(
        profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool:
        fresh_calls.append((profile_id, generation))
        return original_fresh(profile_id, generation, secrets)

    monkeypatch.setattr(credentials, "write_fresh_generation", fresh)
    monkeypatch.setattr(
        credentials,
        "list_generations",
        lambda _profile: (_ for _ in ()).throw(AssertionError("list-before-write")),
    )
    monkeypatch.setattr(
        credentials,
        "write_generation",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy-write")),
    )

    document = setup.commit(
        "profile-1", _candidate(tmp_path, "1" * 32), _secrets()
    )

    assert document.active is not None
    assert fresh_calls == [("profile-1", "1" * 32)]


@pytest.mark.parametrize("mismatch", ("missing", "extra"))
def test_commit_rejects_secret_kind_mismatch_before_any_mutation(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    setup, credentials, _ = store
    candidate = _candidate(tmp_path, "d" * 32)
    secrets = (
        RuntimeSecrets({})
        if mismatch == "missing"
        else RuntimeSecrets(
            {
                SecretKind.ONES_PASSWORD: "TOKEN-SECRET",
                SecretKind.PROVIDER_TOKEN: "EXTRA-SECRET",
            }
        )
    )
    atomic_calls = 0

    def count_write(_document: object) -> None:
        nonlocal atomic_calls
        atomic_calls += 1

    monkeypatch.setattr(setup, "_atomic_write", count_write)
    with pytest.raises(
        SetupStoreError, match="^configuration credentials are invalid$"
    ) as captured:
        setup.commit("profile-1", candidate, secrets)

    assert credentials.writes == []
    assert credentials.deletes == []
    assert atomic_calls == 0
    assert "provider_token" not in repr(captured.value)
    assert "EXTRA-SECRET" not in repr(captured.value)


@pytest.mark.parametrize(
    ("candidate", "secrets"),
    ((object(), _secrets()), (object(), object())),
)
def test_commit_rejects_wrong_types_before_credential_mutation(
    store: tuple[SetupStore, FakeCredentials, Path],
    candidate: object,
    secrets: object,
) -> None:
    setup, credentials, path = store

    with pytest.raises(
        SetupStoreError, match="^configuration credentials are invalid$"
    ):
        setup.commit("profile-1", candidate, secrets)  # type: ignore[arg-type]

    assert credentials.writes == []
    assert credentials.deletes == []
    assert not path.exists()


def test_json_failure_rolls_back_new_credentials_and_preserves_old_document(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, _ = store
    before = setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    before = setup.finalize_activation("profile-1", "a" * 32)

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("TOKEN-SECRET target path")

    monkeypatch.setattr(setup, "_atomic_write", fail_write)
    with pytest.raises(SetupStoreError, match="^configuration save failed$") as captured:
        setup.commit("profile-1", _candidate(tmp_path, "c" * 32), _secrets())

    assert "TOKEN-SECRET" not in repr(captured.value)
    assert setup.load() == before
    assert ("profile-1", "c" * 32) not in credentials.data


def test_reload_failure_after_replace_leaves_recoverable_new_active(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, path = store
    original = setup._load_unlocked
    calls = 0

    def fail_second_load():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SetupStoreError("configuration load failed")
        return original()

    monkeypatch.setattr(setup, "_load_unlocked", fail_second_load)
    with pytest.raises(SetupStoreError, match="^configuration save failed$"):
        setup.commit("profile-1", _candidate(tmp_path, "d" * 32), _secrets("new"))

    recovered = SetupStore(credentials, config_path=path).load()
    assert recovered.active is not None
    assert recovered.active.generation == "d" * 32
    assert ("profile-1", "d" * 32) in credentials.data


def test_exception_after_replace_keeps_new_credentials_recoverable(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, path = store
    monkeypatch.setattr(
        "src.developer_workflow.setup_store._fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("TOKEN-SECRET path")),
    )
    with pytest.raises(SetupStoreError, match="^configuration save failed$"):
        setup.commit("profile-1", _candidate(tmp_path, "e" * 32), _secrets("new"))

    recovered = SetupStore(credentials, config_path=path).load()
    assert recovered.active is not None and recovered.active.generation == "e" * 32
    assert ("profile-1", "e" * 32) in credentials.data


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through replacement")
def test_windows_atomic_write_flushes_file_then_replaces_write_through(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import setup_store as module

    setup, _, _ = store
    events: list[tuple[str, int]] = []
    original_flush = module._flush_file_descriptor

    def flush(descriptor: int) -> None:
        events.append(("flush", descriptor))
        original_flush(descriptor)

    def move(source: str, destination: str, flags: int) -> None:
        del source, destination
        events.append(("move", flags))

    monkeypatch.setattr(module, "_flush_file_descriptor", flush)
    monkeypatch.setattr(module, "_move_file_ex", move)

    with pytest.raises(SetupStoreError, match="^configuration save failed$"):
        setup.commit("profile-1", _candidate(tmp_path, "1" * 32), _secrets())

    assert [event[0] for event in events] == ["flush", "move"]
    assert events[1][1] == (
        module._MOVEFILE_REPLACE_EXISTING | module._MOVEFILE_WRITE_THROUGH
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through replacement")
def test_windows_replace_failure_closes_temp_handle_and_cleans_temp_file(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import setup_store as module

    setup, credentials, path = store
    monkeypatch.setattr(
        module,
        "_move_file_ex",
        lambda *_args: (_ for _ in ()).throw(OSError("TOKEN-SECRET move")),
    )

    with pytest.raises(SetupStoreError, match="^configuration save failed$"):
        setup.commit("profile-1", _candidate(tmp_path, "2" * 32), _secrets())

    assert not path.exists()
    assert not tuple(path.parent.glob(".config-*.tmp"))
    assert ("profile-1", "2" * 32) not in credentials.data
    renamed = path.parent.with_name("renamed-private")
    path.parent.rename(renamed)
    renamed.rename(path.parent)


@pytest.mark.parametrize("failure", ("regular", "directory", "fsync"))
def test_post_replace_failure_is_monotonic_even_when_confirmation_load_fails(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from src.developer_workflow import setup_store as module

    setup, credentials, path = store
    candidate = _candidate(tmp_path, "f" * 32)
    original_regular = module._validate_regular_file
    original_directory = setup._validate_directory

    def fail_post_replace_regular(
        checked: Path, *, descriptor: int | None = None
    ) -> tuple[int, int]:
        if checked == path and descriptor is None and path.exists():
            raise OSError("TOKEN-SECRET post replace")
        return original_regular(checked, descriptor=descriptor)

    def fail_post_replace_directory() -> None:
        if path.exists():
            raise SetupStoreError("configuration path is unsafe")
        original_directory()

    with monkeypatch.context() as faults:
        if failure == "regular":
            faults.setattr(module, "_validate_regular_file", fail_post_replace_regular)
        elif failure == "directory":
            faults.setattr(setup, "_validate_directory", fail_post_replace_directory)
        else:
            faults.setattr(
                module,
                "_fsync_directory",
                lambda _path: (_ for _ in ()).throw(OSError("TOKEN-SECRET fsync")),
            )
        faults.setattr(
            setup,
            "_load_unlocked",
            lambda: (_ for _ in ()).throw(
                SetupStoreError("configuration path is unsafe")
            ),
        )
        with pytest.raises(SetupStoreError, match="^configuration save failed$"):
            setup.commit("profile-1", candidate, _secrets("new"))

    recovered = SetupStore(credentials, config_path=path).load()
    assert recovered.active == candidate
    assert ("profile-1", "f" * 32) in credentials.data


@pytest.mark.parametrize(
    ("fault_point", "replacement"),
    (
        ("temp-regular", False),
        ("file-flush", False),
        ("replace-api", False),
        ("final-regular", True),
        ("post-directory", True),
        ("durability-flush", True),
    ),
)
def test_atomic_fault_chain_is_sanitized_without_changing_replace_outcome(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
    replacement: bool,
) -> None:
    from src.developer_workflow import setup_store as module

    setup, credentials, path = store
    generation = "8" * 32
    fault = "TOKEN-SECRET target-path"
    original_regular = module._validate_regular_file
    original_directory = setup._validate_directory

    def fail_regular(
        checked: Path, *, descriptor: int | None = None
    ) -> tuple[int, int]:
        is_target = (
            checked.name.startswith(".config-")
            if fault_point == "temp-regular"
            else checked == path and path.exists()
        )
        if is_target:
            raise OSError(fault)
        return original_regular(checked, descriptor=descriptor)

    def fail_post_directory() -> None:
        if path.exists():
            raise OSError(fault)
        original_directory()

    if fault_point in {"temp-regular", "final-regular"}:
        monkeypatch.setattr(module, "_validate_regular_file", fail_regular)
    elif fault_point == "file-flush":
        monkeypatch.setattr(
            module,
            "_flush_file_descriptor",
            lambda _descriptor: (_ for _ in ()).throw(OSError(fault)),
        )
    elif fault_point == "replace-api":
        monkeypatch.setattr(
            module,
            "_replace_atomic",
            lambda *_paths: (_ for _ in ()).throw(OSError(fault)),
        )
    elif fault_point == "post-directory":
        monkeypatch.setattr(setup, "_validate_directory", fail_post_directory)
    else:
        monkeypatch.setattr(
            module,
            "_fsync_directory",
            lambda _path: (_ for _ in ()).throw(OSError(fault)),
        )

    with pytest.raises(SetupStoreError, match="^configuration save failed$") as captured:
        setup.commit("profile-1", _candidate(tmp_path, generation), _secrets())

    chain = _exception_chain(captured.value)
    assert all(fault not in str(error) and fault not in repr(error) for error in chain)
    assert all(not isinstance(error, OSError) for error in chain)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    monkeypatch.undo()
    if replacement:
        assert SetupStore(credentials, config_path=path).load().active is not None
        assert ("profile-1", generation) in credentials.data
    else:
        assert not path.exists()
        assert ("profile-1", generation) not in credentials.data


def test_two_process_commits_are_serialized_without_mixed_document(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    _, _, path = store
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        credentials = SharedCredentials(manager.dict(), manager.RLock())
        setup = SetupStore(credentials, config_path=path)  # stale parent instance
        ready = context.Queue()
        start = context.Event()
        generations = ("a" * 32, "b" * 32)
        processes = [
            context.Process(
                target=_commit_process,
                args=(
                    str(path),
                    str(tmp_path),
                    generation,
                    credentials,
                    ready,
                    start,
                ),
            )
            for generation in generations
        ]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                assert ready.get(timeout=10) is True
            start.set()
            for process in processes:
                process.join(timeout=20)
                assert process.exitcode == 0
            document = setup.load()
            assert document.active is not None and document.previous is None
            assert document.active.generation in generations
            assert document.activation_owner_generation == document.active.generation
            assert setup.read_active_secrets(document).require(
                SecretKind.ONES_PASSWORD
            ) == document.active.generation
            assert credentials.list_generations("profile-1") == (
                document.active.generation,
            )
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)


def test_two_config_files_cannot_both_claim_the_same_generation(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        credentials = SharedCredentials(manager.dict(), manager.RLock())
        start = context.Event()
        results = context.Queue()
        generation = "e" * 32
        paths = (
            tmp_path / "first" / "ones-dev" / "config.json",
            tmp_path / "second" / "ones-dev" / "config.json",
        )
        processes = [
            context.Process(
                target=_fresh_claim_process,
                args=(
                    str(path),
                    str(tmp_path),
                    generation,
                    credentials,
                    start,
                    results,
                ),
            )
            for path in paths
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20)
                assert process.exitcode == 0
            assert sorted(results.get(timeout=5) for _ in processes) == [False, True]
            existing = tuple(path for path in paths if path.exists())
            assert len(existing) == 1
            document = SetupStore(credentials, config_path=existing[0]).load()
            assert document.active is not None
            assert document.active.generation == generation
            assert credentials.list_generations("profile-1") == (generation,)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)


def test_failed_pre_replace_claim_can_be_followed_by_success_without_deleting_it(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        credentials = SharedCredentials(manager.dict(), manager.RLock())
        generation = "f" * 32
        results = context.Queue()
        fail_start = context.Event()
        success_start = context.Event()
        failed_path = tmp_path / "failed" / "ones-dev" / "config.json"
        success_path = tmp_path / "success" / "ones-dev" / "config.json"
        failed = context.Process(
            target=_fresh_claim_process,
            args=(
                str(failed_path), str(tmp_path), generation, credentials,
                fail_start, results, True,
            ),
        )
        succeeded = context.Process(
            target=_fresh_claim_process,
            args=(
                str(success_path), str(tmp_path), generation, credentials,
                success_start, results,
            ),
        )
        try:
            failed.start()
            fail_start.set()
            assert results.get(timeout=15) is False
            failed.join(timeout=15)
            assert failed.exitcode == 0
            succeeded.start()
            success_start.set()
            assert results.get(timeout=15) is True
            succeeded.join(timeout=15)
            assert succeeded.exitcode == 0
            document = SetupStore(credentials, config_path=success_path).load()
            assert document.active is not None
            assert document.active.generation == generation
            assert credentials.read_generation(
                "profile-1", generation, document.active.credential_kinds
            ).require(SecretKind.ONES_PASSWORD) == generation
        finally:
            for process in (failed, succeeded):
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)


def test_restore_finalize_and_orphan_cleanup_are_pointer_safe(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.finalize_activation("profile-1", "a" * 32)
    setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))
    credentials.data[("profile-1", "c" * 32)] = _secrets("orphan")

    assert setup.orphan_generations() == ("c" * 32,)
    with pytest.raises(SetupStoreError, match="^credential cleanup refused$"):
        setup.cleanup_orphan_generations(("a" * 32, "c" * 32))
    setup.cleanup_orphan_generations(("c" * 32,))
    restored = setup.restore_previous("profile-1", "b" * 32)
    assert restored.active is not None and restored.active.generation == "a" * 32
    assert ("profile-1", "b" * 32) not in credentials.data


def test_finalize_keeps_new_active_when_old_credential_delete_fails(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.finalize_activation("profile-1", "a" * 32)
    setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))
    credentials.fail_delete = True

    finalized = setup.finalize_activation("profile-1", "b" * 32)

    assert finalized.previous is None
    assert finalized.active is not None and finalized.active.generation == "b" * 32
    assert setup.orphan_generations() == ("a" * 32,)


def test_finalize_post_replace_failure_is_monotonic_success(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.finalize_activation("profile-1", "a" * 32)
    setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))
    monkeypatch.setattr(
        "src.developer_workflow.setup_store._fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("TOKEN-SECRET")),
    )

    finalized = setup.finalize_activation("profile-1", "b" * 32)

    assert finalized.active is not None
    assert finalized.active.generation == "b" * 32
    assert finalized.previous is None
    assert ("profile-1", "a" * 32) not in credentials.data


@pytest.mark.parametrize("operation", ("finalize", "restore"))
def test_generation_cas_rejects_superseded_controller_without_mutation(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    operation: str,
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.finalize_activation("profile-1", "a" * 32)
    setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("first"))
    setup.finalize_activation("profile-1", "b" * 32)
    current = setup.commit(
        "profile-1", _candidate(tmp_path, "c" * 32), _secrets("second")
    )
    credentials.deletes.clear()

    with pytest.raises(
        SetupStoreError, match="^configuration generation is superseded$"
    ) as error:
        if operation == "finalize":
            setup.finalize_activation("profile-1", "b" * 32)
        else:
            setup.restore_previous("profile-1", "b" * 32)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert setup.load() == current
    assert credentials.deletes == []
    assert ("profile-1", "b" * 32) in credentials.data
    assert ("profile-1", "c" * 32) in credentials.data


def test_commit_rejects_nested_pending_activation_before_credential_mutation(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("stable"))
    setup.finalize_activation("profile-1", "a" * 32)
    pending = setup.commit(
        "profile-1", _candidate(tmp_path, "b" * 32), _secrets("pending")
    )
    credentials.writes.clear()

    with pytest.raises(
        SetupStoreError, match="^configuration activation is already pending$"
    ) as error:
        setup.commit("profile-1", _candidate(tmp_path, "c" * 32), _secrets("nested"))

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert setup.load() == pending
    assert credentials.writes == []
    assert ("profile-1", "c" * 32) not in credentials.data


def test_first_setup_pending_survives_restart_and_can_be_restored(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, path = store
    pending = setup.commit(
        "profile-1", _candidate(tmp_path, "b" * 32), _secrets("pending")
    )
    restarted = SetupStore(credentials, config_path=path)

    assert pending.activation_owner_generation == "b" * 32
    assert restarted.load() == pending

    restored = restarted.restore_previous("profile-1", "b" * 32)

    assert restored == SetupDocument(profile_id="profile-1")
    assert credentials.list_generations("profile-1") == ()


@pytest.mark.parametrize(
    "raw",
    [b"{broken", b'\xff', b"[]", b"x" * (1024 * 1024 + 1)],
    ids=("invalid-json", "invalid-utf8", "wrong-shape", "oversize"),
)
def test_load_rejects_corrupt_unsafe_or_oversize_data_without_leaks(
    store: tuple[SetupStore, FakeCredentials, Path], raw: bytes
) -> None:
    setup, _, path = store
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    from src.developer_workflow.setup_store import _protect_private_file

    _protect_private_file(path)

    with pytest.raises(SetupStoreError, match="^stored configuration is corrupted$") as captured:
        setup.load()
    assert str(path) not in repr(captured.value)


def test_load_rejects_final_symlink_without_disclosing_target(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, _, path = store
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "TOKEN-SECRET-target.json"
    target.write_text(json.dumps({"schema_version": 1, "profile_id": "profile-1"}))
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$") as captured:
        setup.load()
    assert "TOKEN-SECRET" not in repr(captured.value)


def test_load_rejects_nonregular_final_and_lock_paths(
    store: tuple[SetupStore, FakeCredentials, Path]
) -> None:
    setup, _, path = store
    path.mkdir()
    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$"):
        setup.load()
    path.rmdir()
    lock = path.parent / ".config.lock"
    lock.unlink()
    lock.mkdir()
    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$"):
        setup.load_or_empty(profile_id="profile-1")


@pytest.mark.skipif(os.name != "nt", reason="Windows lock reparse contract")
def test_load_rejects_real_windows_lock_symlink_without_touching_target(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, _, path = store
    target = tmp_path / "TOKEN-SECRET-lock-target"
    target.write_bytes(b"guard")
    lock = path.parent / ".config.lock"
    try:
        lock.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$") as captured:
        setup.load_or_empty(profile_id="profile-1")
    assert target.read_bytes() == b"guard"
    assert "TOKEN-SECRET" not in repr(captured.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction/reparse contract")
def test_setup_directory_rejects_real_windows_junction(tmp_path: Path) -> None:
    target = tmp_path / "TOKEN-SECRET-target"
    target.mkdir()
    junction = tmp_path / "junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("directory junctions are unavailable")
    try:
        with pytest.raises(
            SetupStoreError, match="^configuration path is unsafe$"
        ) as captured:
            SetupStore(FakeCredentials(), config_path=junction / "config.json")
        assert "TOKEN-SECRET" not in repr(captured.value)
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
@pytest.mark.parametrize("target", ("directory", "file"))
def test_setup_rejects_real_windows_dacl_with_extra_principal(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    target: str,
) -> None:
    setup, _, path = store
    setup.commit("profile-1", _candidate(tmp_path, "3" * 32), _secrets())
    changed = path.parent if target == "directory" else path
    grant = "*S-1-1-0:(OI)(CI)RX" if target == "directory" else "*S-1-1-0:R"
    completed = subprocess.run(
        ["icacls", str(changed), "/grant", grant, "/q"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("DACL mutation is unavailable")
    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$"):
        setup.load()


@pytest.mark.skipif(os.name != "nt", reason="Windows lock DACL contract")
def test_setup_rejects_existing_lock_dacl_with_extra_principal(
    store: tuple[SetupStore, FakeCredentials, Path]
) -> None:
    setup, _, path = store
    setup.load_or_empty(profile_id="profile-1")
    lock = path.parent / ".config.lock"
    completed = subprocess.run(
        ["icacls", str(lock), "/grant", "*S-1-1-0:R", "/q"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("DACL mutation is unavailable")

    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$"):
        setup.load_or_empty(profile_id="profile-1")


@pytest.mark.skipif(os.name != "nt", reason="Windows lock DACL contract")
def test_setup_rejects_existing_lock_with_inherited_dacl(
    store: tuple[SetupStore, FakeCredentials, Path]
) -> None:
    setup, _, path = store
    setup.load_or_empty(profile_id="profile-1")
    lock = path.parent / ".config.lock"
    completed = subprocess.run(
        ["icacls", str(lock), "/inheritance:e", "/q"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("DACL mutation is unavailable")

    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$"):
        setup.load_or_empty(profile_id="profile-1")


@pytest.mark.skipif(os.name != "nt", reason="Windows child-file DACL contract")
def test_windows_temp_final_and_lock_have_protected_noninherited_dacl(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import private_paths
    from src.developer_workflow import setup_store as module

    setup, _, path = store
    observed_temp: list[tuple[str, tuple[tuple[str, int, int, int], ...], bool]] = []
    original_replace = module._replace_atomic

    def inspect_then_replace(source: Path, destination: Path) -> None:
        observed_temp.append(private_paths._windows_descriptor(source))
        original_replace(source, destination)

    monkeypatch.setattr(module, "_replace_atomic", inspect_then_replace)
    setup.commit("profile-1", _candidate(tmp_path, "5" * 32), _secrets())

    descriptors = (
        observed_temp[0],
        private_paths._windows_descriptor(path),
        private_paths._windows_descriptor(path.parent / ".config.lock"),
    )
    user_sid = private_paths._current_user_sid()
    trusted = {user_sid, "S-1-5-18", "S-1-5-32-544"}
    for owner, entries, protected in descriptors:
        assert owner == user_sid
        assert protected is True
        assert {sid for sid, *_ in entries} <= trusted
        assert all(
            mask & 0x001F01FF == 0x001F01FF
            and flags & 0x10 == 0
            and ace_type == 0
            for _, mask, flags, ace_type in entries
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows child-file owner contract")
def test_windows_config_file_owner_mismatch_fails_closed(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import setup_store as module

    setup, _, path = store
    setup.commit("profile-1", _candidate(tmp_path, "6" * 32), _secrets())
    original = module._windows_descriptor

    def wrong_owner(checked: Path):
        owner, entries, protected = original(checked)
        if checked == path:
            owner = "S-1-1-0"
        return owner, entries, protected

    monkeypatch.setattr(module, "_windows_descriptor", wrong_owner)
    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$"):
        setup.load()


@pytest.mark.skipif(os.name != "nt", reason="Windows real owner contract")
def test_windows_config_file_real_owner_mismatch_fails_closed(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    from src.developer_workflow import private_paths

    setup, _, path = store
    setup.commit("profile-1", _candidate(tmp_path, "9" * 32), _secrets())
    current_owner = private_paths._current_user_sid()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    def set_owner(sid_text: str) -> None:
        sid = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result = advapi.SetNamedSecurityInfoW(
                str(path), 1, 0x00000001, sid, None, None, None
            )
            if result:
                raise ctypes.WinError(result)
        finally:
            kernel.LocalFree(sid)

    changed_owner = _select_assignable_owner(
        (("everyone", "S-1-1-0"), ("administrators", "S-1-5-32-544")),
        set_owner,
    )
    try:
        actual_owner, _, _ = private_paths._windows_descriptor(path)
        assert actual_owner == changed_owner
        assert actual_owner != current_owner
        with pytest.raises(
            SetupStoreError, match="^configuration path is unsafe$"
        ):
            setup.load()
    finally:
        set_owner(current_owner)


@pytest.mark.parametrize("code", (5, 1307, 1314))
def test_owner_selection_skips_only_after_both_expected_permission_errors(
    code: int,
) -> None:
    attempted: list[str] = []

    def reject(sid: str) -> None:
        attempted.append(sid)
        error = OSError("TOKEN-SECRET target-path")
        error.winerror = code  # type: ignore[attr-defined]
        raise error

    with pytest.raises(pytest.skip.Exception) as captured:
        _select_assignable_owner(
            (("everyone", "S-1-1-0"), ("administrators", "S-1-5-32-544")),
            reject,
        )

    assert attempted == ["S-1-1-0", "S-1-5-32-544"]
    message = str(captured.value)
    assert message == (
        f"owner assignment unavailable: everyone={code}, administrators={code}"
    )
    assert "TOKEN-SECRET" not in message and "target-path" not in message


def test_owner_selection_fails_immediately_on_unexpected_windows_error() -> None:
    attempted: list[str] = []

    def reject(sid: str) -> None:
        attempted.append(sid)
        error = OSError("TOKEN-SECRET target-path")
        error.winerror = 87  # type: ignore[attr-defined]
        raise error

    with pytest.raises(
        AssertionError, match="^unexpected owner error for everyone: 87$"
    ) as captured:
        _select_assignable_owner(
            (("everyone", "S-1-1-0"), ("administrators", "S-1-5-32-544")),
            reject,
        )

    assert attempted == ["S-1-1-0"]
    assert "TOKEN-SECRET" not in repr(captured.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows final-path identity contract")
@pytest.mark.parametrize("replacement_point", ("before_open", "after_handle"))
def test_windows_repeated_config_replacement_race_fails_closed(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_point: str,
) -> None:
    from src.developer_workflow import setup_store as module

    setup, _, path = store
    setup.commit("profile-1", _candidate(tmp_path, "4" * 32), _secrets())
    original_open = module._open_windows
    original_validate = module._validate_regular_file
    validation_calls = 0

    def replace_file() -> None:
        raw = path.read_bytes()
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(raw)
        os.replace(replacement, path)

    def racing_open(checked: Path) -> int:
        replace_file()
        return original_open(checked)

    def racing_validation(
        checked: Path, *, descriptor: int | None = None
    ) -> tuple[int, int]:
        nonlocal validation_calls
        if checked == path and descriptor is None:
            validation_calls += 1
            if validation_calls % 2 == 0:
                replace_file()
        return original_validate(checked, descriptor=descriptor)

    if replacement_point == "before_open":
        monkeypatch.setattr(module, "_open_windows", racing_open)
    else:
        monkeypatch.setattr(module, "_validate_regular_file", racing_validation)

    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$"):
        setup.load()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point open contract")
def test_windows_config_and_lock_handles_use_open_reparse_point(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import setup_store as module

    setup, _, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "7" * 32), _secrets())
    original = module._kernel32.CreateFileW
    flags: list[int] = []

    def capture(*args: object):
        flags.append(int(args[5]))
        return original(*args)

    monkeypatch.setattr(module._kernel32, "CreateFileW", capture)
    setup.load()

    assert len(flags) >= 2
    assert all(value & 0x00200000 for value in flags)
