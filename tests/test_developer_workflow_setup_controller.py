from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from types import MappingProxyType
from typing import Any

import pytest

from src.developer_workflow.config import (
    BUILTIN_WORKSPACE_PROFILE,
    DeveloperWorkflowConfig,
    PublishingConfig,
    PublishingProvider,
    SandboxPermissionProfileSource,
)
from src.developer_workflow.contracts import RepositoryMapping
from src.developer_workflow.credential_store import CredentialStoreError
from src.developer_workflow.setup_controller import SetupActionError, SetupController
from src.developer_workflow.setup_models import (
    ActiveSetup,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
    SetupDocument,
    SetupDraft,
    SetupValidationError,
    WorkflowDraft,
)
from src.developer_workflow.setup_store import SetupStore
from src.developer_workflow.setup_validation import (
    ConnectionTestResult,
    SetupStep,
    ValidationStatus,
)


def _runtime() -> RuntimePublicConfig:
    return RuntimePublicConfig(
        ones_base_url="https://ones.example.test",
        ones_team_id="team-1",
        ones_issue_type_id="issue-1",
        ones_comment_list_path_template="/team/{team_id}/item/{item_id}",
        provider_host="provider.example.test",
        provider_api_url="https://provider.example.test/api",
        git_author_name="ONES Agent",
        git_author_email="agent@example.test",
        codex_auth_mode="credential",
    )


def _workflow(tmp_path: Path) -> WorkflowDraft:
    repository = RepositoryMapping(
        key="sdk",
        project_id="project-1",
        iteration_id="iteration-1",
        repo_url="https://git.example.test/sdk.git",
        repo_name="sdk",
    )
    return WorkflowDraft(
        run_root=tmp_path / "runs",
        mirror_root=tmp_path / "mirrors",
        worktree_root=tmp_path / "worktrees",
        sandbox_permission_profile="managed-profile",
        repositories=(repository,),
        publishing=PublishingConfig(
            provider=PublishingProvider.GITHUB,
            default_target_branch="main",
        ),
    )


def _candidate_for_store(tmp_path: Path, generation: str) -> ActiveSetup:
    return ActiveSetup(
        generation=generation,
        runtime=_runtime(),
        workflow=DeveloperWorkflowConfig.model_validate(
            _workflow(tmp_path).model_dump(mode="python", round_trip=True)
        ),
        credential_kinds=(SecretKind.ONES_PASSWORD, SecretKind.PROVIDER_TOKEN),
    )


def _persisted_secrets() -> RuntimeSecrets:
    return RuntimeSecrets(
        {
            SecretKind.ONES_PASSWORD: "persisted-password",
            SecretKind.PROVIDER_TOKEN: "persisted-provider",
        }
    )


def _result(step: SetupStep, passed: bool = True) -> ConnectionTestResult:
    return ConnectionTestResult(
        step=step,
        status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
        category="ok" if passed else "unreachable",
    )


class FakeCatalog:
    def __init__(self) -> None:
        self.selections: list[tuple[str, SandboxPermissionProfileSource]] = []

    def list_profiles(self) -> tuple[str, ...]:
        return ("managed-profile",)

    def require_selected(
        self,
        profile: str,
        source: SandboxPermissionProfileSource,
    ) -> str:
        self.selections.append((profile, source))
        if (
            type(source) is not SandboxPermissionProfileSource
            or source is not SandboxPermissionProfileSource.MANAGED
            or profile != "managed-profile"
        ):
            raise ValueError("SECRET profile")
        return profile


class FakeValidator:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.probes: list[tuple[SetupStep, object]] = []

    async def _probe(self, step: SetupStep, probe: object) -> ConnectionTestResult:
        self.probes.append((step, probe))
        if self.block:
            self.started.set()
            await self.release.wait()
        return _result(step)

    async def probe_ones(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.ONES, probe)

    async def probe_repository(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.REPOSITORIES, probe)

    async def probe_provider(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.PROVIDER, probe)

    async def probe_codex(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.CODEX, probe)

    async def probe_private_paths(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.PRIVATE_PATHS, probe)


class FakeBootstrap:
    def __init__(self) -> None:
        self.catalog = FakeCatalog()
        self.validator = FakeValidator()


@pytest.mark.asyncio
async def test_managed_profile_snapshot_is_frozen_and_loaded_off_loop() -> None:
    import threading

    class Catalog(FakeCatalog):
        def __init__(self) -> None:
            super().__init__()
            self.thread_id: int | None = None

        def list_profiles(self) -> tuple[str, ...]:
            self.thread_id = threading.get_ident()
            return ("restricted", "managed-profile")

    catalog = Catalog()
    bootstrap = FakeBootstrap()
    bootstrap.catalog = catalog
    controller = SetupController(
        profile_id="managed-profile",
        store=FakeStore(),
        runtime_builder=FakeRuntimeBuilder(),
        runtime_bootstrap=bootstrap,
    )
    loop_thread = threading.get_ident()

    profiles = await controller.list_managed_profiles()

    assert profiles == ("restricted", "managed-profile")
    assert type(profiles) is tuple
    assert catalog.thread_id != loop_thread


@pytest.mark.asyncio
async def test_managed_profile_cancel_is_propagated_and_worker_is_owned() -> None:
    import threading

    entered, release = threading.Event(), threading.Event()

    class Catalog(FakeCatalog):
        def list_profiles(self) -> tuple[str, ...]:
            entered.set()
            release.wait(2)
            return ("managed-profile",)

    bootstrap = FakeBootstrap()
    bootstrap.catalog = Catalog()
    controller = SetupController(
        profile_id="managed-profile",
        store=FakeStore(),
        runtime_builder=FakeRuntimeBuilder(),
        runtime_bootstrap=bootstrap,
        cleanup_timeout=0.1,
    )
    task = asyncio.create_task(controller.list_managed_profiles())
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller._catalog_tasks
    release.set()
    await controller.aclose()
    assert controller._catalog_tasks == set()


class _BuiltinCatalog(FakeCatalog):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self.block = False
        self.failure: BaseException | None = None
        self.active = 0
        self.max_active = 0

    def verify_builtin_workspace_profile(self) -> str:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.entered.set()
            if self.block:
                self.release.wait(2)
            if self.failure is not None:
                raise self.failure
            return BUILTIN_WORKSPACE_PROFILE
        finally:
            self.active -= 1


def _builtin_controller(
    tmp_path: Path, catalog: _BuiltinCatalog, *, cleanup_timeout: float = 0.1,
) -> SetupController:
    bootstrap = FakeBootstrap()
    bootstrap.catalog = catalog
    return SetupController(
        profile_id="managed-profile",
        store=FakeStore(),
        runtime_builder=FakeRuntimeBuilder(),
        runtime_bootstrap=bootstrap,
        draft=SetupDraft(workflow=_workflow(tmp_path)),
        cleanup_timeout=cleanup_timeout,
    )


@pytest.mark.asyncio
async def test_builtin_profile_commits_only_after_successful_probe(
    tmp_path: Path,
) -> None:
    catalog = _BuiltinCatalog()
    catalog.block = True
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft.workflow
    revision = controller.revision

    task = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    assert await asyncio.to_thread(catalog.entered.wait, 1)
    assert controller.draft.workflow == original
    assert controller.revision == revision
    catalog.release.set()

    assert await task == BUILTIN_WORKSPACE_PROFILE
    assert controller.draft.workflow.sandbox_permission_profile == BUILTIN_WORKSPACE_PROFILE
    assert (
        controller.draft.workflow.sandbox_permission_profile_source
        is SandboxPermissionProfileSource.BUILTIN_WORKSPACE
    )
    assert controller.revision == revision + 1


@pytest.mark.asyncio
async def test_builtin_profile_rejects_stale_revision_without_overwriting_edit(
    tmp_path: Path,
) -> None:
    catalog = _BuiltinCatalog()
    catalog.block = True
    controller = _builtin_controller(tmp_path, catalog)
    task = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    assert await asyncio.to_thread(catalog.entered.wait, 1)
    changed = controller.draft.workflow.model_copy(deep=True)
    changed.run_root = tmp_path / "changed-runs"
    controller.apply_workflow(changed, changed_step=SetupStep.PRIVATE_PATHS)
    catalog.release.set()

    with pytest.raises(
        SetupActionError, match="configuration changed during validation"
    ) as caught:
        await task
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert controller.draft.workflow.run_root == tmp_path / "changed-runs"
    assert controller.draft.workflow.sandbox_permission_profile == "managed-profile"


@pytest.mark.asyncio
async def test_builtin_profile_failure_is_fixed_and_does_not_mutate_draft(
    tmp_path: Path,
) -> None:
    catalog = _BuiltinCatalog()
    secret = "controller-builtin-secret-canary"
    catalog.failure = RuntimeError(secret)
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft
    revision = controller.revision

    with pytest.raises(
        SetupActionError, match="built-in workspace profile is unavailable"
    ) as caught:
        await controller.confirm_builtin_workspace_profile()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value)
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__") == (
            "src.developer_workflow.setup_controller"
        ):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert controller.draft == original
    assert controller.revision == revision


@pytest.mark.asyncio
async def test_builtin_profile_cancel_and_late_result_never_mutate(
    tmp_path: Path,
) -> None:
    catalog = _BuiltinCatalog()
    catalog.block = True
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft
    revision = controller.revision
    task = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    assert await asyncio.to_thread(catalog.entered.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller._catalog_tasks
    catalog.release.set()
    await controller.aclose()
    assert controller._catalog_tasks == set()
    assert controller.draft == original
    assert controller.revision == revision


@pytest.mark.asyncio
async def test_builtin_profile_close_during_probe_cancels_without_mutation(
    tmp_path: Path,
) -> None:
    catalog = _BuiltinCatalog()
    catalog.block = True
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft
    task = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    assert await asyncio.to_thread(catalog.entered.wait, 1)

    controller.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    catalog.release.set()
    await controller.aclose()
    assert controller.closed
    assert controller.draft == original


@pytest.mark.asyncio
async def test_builtin_profile_repeat_reprobes_and_invalidates_downstream(
    tmp_path: Path,
) -> None:
    catalog = _BuiltinCatalog()
    controller = _builtin_controller(tmp_path, catalog)
    controller._results = {
        step: _result(step) for step in controller.STEPS
    }

    assert await controller.confirm_builtin_workspace_profile() == BUILTIN_WORKSPACE_PROFILE
    assert all(
        controller.result_for(step).status is ValidationStatus.NOT_CONFIGURED
        for step in controller.STEPS
    )
    revision = controller.revision
    assert await controller.confirm_builtin_workspace_profile() == BUILTIN_WORKSPACE_PROFILE
    assert catalog.calls == 2
    assert controller.revision == revision + 1


@pytest.mark.asyncio
async def test_builtin_profile_confirmation_is_single_flight(
    tmp_path: Path,
) -> None:
    catalog = _BuiltinCatalog()
    catalog.block = True
    controller = _builtin_controller(tmp_path, catalog)
    first = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    assert await asyncio.to_thread(catalog.entered.wait, 1)
    second = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    await asyncio.sleep(0)
    assert catalog.calls == 1
    catalog.release.set()

    assert await first == BUILTIN_WORKSPACE_PROFILE
    assert await second == BUILTIN_WORKSPACE_PROFILE
    assert catalog.calls == 2
    assert catalog.max_active == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [MemoryError, GeneratorExit],
)
async def test_builtin_profile_confirmation_propagates_priority_failures(
    tmp_path: Path, failure_type: type[BaseException],
) -> None:
    catalog = _BuiltinCatalog()
    failure = failure_type()
    catalog.failure = failure
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft

    with pytest.raises(failure_type) as caught:
        await controller.confirm_builtin_workspace_profile()
    assert caught.value is failure
    assert controller.draft == original


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit])
async def test_builtin_profile_confirmation_propagates_control_flow_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    catalog = _BuiltinCatalog()
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft
    failure = failure_type()
    real_shield = asyncio.shield

    async def raise_after_probe(task: asyncio.Task[object]) -> object:
        await real_shield(task)
        raise failure

    monkeypatch.setattr(asyncio, "shield", raise_after_probe)
    with pytest.raises(failure_type) as caught:
        await controller.confirm_builtin_workspace_profile()
    assert caught.value is failure
    assert controller.draft == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_profile",
    [
        None,
        b"ones-dev-workspace",
        type("ProfileSubclass", (str,), {})(BUILTIN_WORKSPACE_PROFILE),
    ],
)
async def test_builtin_profile_confirmation_rejects_non_exact_string_results(
    tmp_path: Path, invalid_profile: object,
) -> None:
    catalog = _BuiltinCatalog()
    catalog.verify_builtin_workspace_profile = lambda: invalid_profile  # type: ignore[method-assign]
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft
    revision = controller.revision

    with pytest.raises(
        SetupActionError, match="built-in workspace profile is unavailable"
    ):
        await controller.confirm_builtin_workspace_profile()

    assert controller.draft == original
    assert controller.revision == revision


@pytest.mark.asyncio
async def test_builtin_profile_confirmation_never_invokes_untrusted_equality(
    tmp_path: Path,
) -> None:
    class EqualProfile:
        compared = False

        def __eq__(self, other: object) -> bool:
            self.compared = True
            return True

    invalid_profile = EqualProfile()
    catalog = _BuiltinCatalog()
    catalog.verify_builtin_workspace_profile = lambda: invalid_profile  # type: ignore[method-assign]
    controller = _builtin_controller(tmp_path, catalog)
    original = controller.draft

    with pytest.raises(
        SetupActionError, match="built-in workspace profile is unavailable"
    ):
        await controller.confirm_builtin_workspace_profile()

    assert invalid_profile.compared is False
    assert controller.draft == original


@pytest.mark.asyncio
async def test_builtin_profile_getter_failure_is_sanitized_and_controller_recovers(
    tmp_path: Path,
) -> None:
    secret = "builtin-getter-secret-canary"

    class GetterCatalog:
        @property
        def verify_builtin_workspace_profile(self) -> object:
            getter_secret = secret
            raise RuntimeError(getter_secret)

    controller = _builtin_controller(tmp_path, _BuiltinCatalog())
    controller._profile_catalog = GetterCatalog()
    original = controller.draft
    revision = controller.revision

    with pytest.raises(
        SetupActionError, match="built-in workspace profile is unavailable"
    ) as caught:
        await controller.confirm_builtin_workspace_profile()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    trace = caught.value.__traceback__
    while trace is not None:
        if Path(trace.tb_frame.f_code.co_filename).name == "setup_controller.py":
            assert secret not in repr(trace.tb_frame.f_locals)
        trace = trace.tb_next
    assert controller._operation_task is None
    assert controller._catalog_owner_task is None
    assert controller._catalog_tasks == set()
    assert controller.draft == original
    assert controller.revision == revision

    controller._profile_catalog = _BuiltinCatalog()
    assert await controller.confirm_builtin_workspace_profile() == BUILTIN_WORKSPACE_PROFILE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalog",
    [
        object(),
        type("Catalog", (), {"verify_builtin_workspace_profile": object()})(),
    ],
)
async def test_builtin_profile_invalid_catalog_releases_all_operation_ownership(
    tmp_path: Path, catalog: object,
) -> None:
    controller = _builtin_controller(tmp_path, _BuiltinCatalog())
    controller._profile_catalog = catalog
    original = controller.draft

    with pytest.raises(
        SetupActionError, match="built-in workspace profile is unavailable"
    ):
        await controller.confirm_builtin_workspace_profile()

    assert controller._operation_task is None
    assert controller._catalog_owner_task is None
    assert controller._catalog_tasks == set()
    assert controller.draft == original
    controller.close()
    assert controller.closed


@pytest.mark.asyncio
async def test_builtin_profile_create_task_failure_closes_unstarted_coroutine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "create-task-secret-canary"
    controller = _builtin_controller(tmp_path, _BuiltinCatalog())
    original = controller.draft
    revision = controller.revision
    received: list[object] = []
    real_create_task = asyncio.create_task

    def fail_create_task(awaitable: object) -> object:
        received.append(awaitable)
        raise RuntimeError(secret)

    monkeypatch.setattr(asyncio, "create_task", fail_create_task)
    with pytest.raises(
        SetupActionError, match="built-in workspace profile is unavailable"
    ) as caught:
        await controller.confirm_builtin_workspace_profile()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(received) == 1
    assert getattr(received[0], "cr_frame", object()) is None
    assert controller._operation_task is None
    assert controller._catalog_owner_task is None
    assert controller._catalog_tasks == set()
    assert controller.draft == original
    assert controller.revision == revision

    monkeypatch.setattr(asyncio, "create_task", real_create_task)
    assert await controller.confirm_builtin_workspace_profile() == BUILTIN_WORKSPACE_PROFILE


@pytest.mark.asyncio
async def test_builtin_profile_to_thread_construction_failure_releases_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "to-thread-secret-canary"
    controller = _builtin_controller(tmp_path, _BuiltinCatalog())
    original = controller.draft

    def fail_to_thread(*args: object, **kwargs: object) -> object:
        construction_secret = secret
        raise RuntimeError(construction_secret)

    monkeypatch.setattr(asyncio, "to_thread", fail_to_thread)
    with pytest.raises(
        SetupActionError, match="built-in workspace profile is unavailable"
    ) as caught:
        await controller.confirm_builtin_workspace_profile()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert controller._operation_task is None
    assert controller._catalog_owner_task is None
    assert controller._catalog_tasks == set()
    assert controller.draft == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [
        MemoryError,
        asyncio.CancelledError,
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ],
)
async def test_builtin_profile_startup_priority_failure_is_propagated_and_cleared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    failure = failure_type()
    controller = _builtin_controller(tmp_path, _BuiltinCatalog())
    original = controller.draft

    def fail_to_thread(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(asyncio, "to_thread", fail_to_thread)
    with pytest.raises(failure_type) as caught:
        await controller.confirm_builtin_workspace_profile()

    assert caught.value is failure
    assert controller._operation_task is None
    assert controller._catalog_owner_task is None
    assert controller._catalog_tasks == set()
    assert controller.draft == original


class Handle:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeRuntimeBuilder:
    def __init__(self) -> None:
        self.calls: list[tuple[ActiveSetup, RuntimeSecrets]] = []
        self.handle = Handle()
        self.error: BaseException | None = None

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
        self.calls.append((active, secrets))
        if self.error is not None:
            raise self.error
        return self.handle


class FakeStore:
    def __init__(self) -> None:
        self.document = SetupDocument(profile_id="managed-profile")
        self.commits = 0
        self.restores = 0
        self.finalizes = 0
        self.finalize_error: BaseException | None = None

    def load_or_empty(self, *, profile_id: str) -> SetupDocument:
        assert profile_id == "managed-profile"
        return self.document

    def commit(
        self, profile_id: str, candidate: ActiveSetup, secrets: RuntimeSecrets
    ) -> SetupDocument:
        assert profile_id == "managed-profile"
        assert set(candidate.credential_kinds) == set(secrets.values)
        self.commits += 1
        self.document = self.document.validated_update(
            active=candidate,
            previous=self.document.active,
            activation_owner_generation=candidate.generation,
        )
        return self.document

    def read_active_secrets(self, document: SetupDocument) -> RuntimeSecrets:
        assert document.active is not None
        return RuntimeSecrets(
            MappingProxyType(
                {kind: "persisted-value" for kind in document.active.credential_kinds}
            )
        )

    def restore_previous(
        self, profile_id: str, expected_generation: str
    ) -> SetupDocument:
        assert profile_id == "managed-profile"
        if (
            self.document.active is None
            or self.document.active.generation != expected_generation
            or self.document.activation_owner_generation != expected_generation
        ):
            raise SetupStoreError("configuration generation is superseded")
        self.restores += 1
        self.document = self.document.validated_update(
            active=self.document.previous,
            previous=None,
            activation_owner_generation=None,
        )
        return self.document

    def finalize_activation(
        self, profile_id: str, expected_generation: str
    ) -> SetupDocument:
        assert profile_id == "managed-profile"
        if (
            self.document.active is None
            or self.document.active.generation != expected_generation
            or self.document.activation_owner_generation != expected_generation
        ):
            raise SetupStoreError("configuration generation is superseded")
        self.finalizes += 1
        if self.finalize_error is not None:
            raise self.finalize_error
        self.document = self.document.validated_update(
            previous=None, activation_owner_generation=None
        )
        return self.document


class IntegrationCredentials:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], RuntimeSecrets] = {}
        self.reads = 0

    def write_fresh_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool:
        key = (profile_id, generation)
        if key in self.data:
            raise CredentialStoreError("credential operation failed")
        self.data[key] = secrets
        return True

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets:
        self.reads += 1
        try:
            values = self.data[(profile_id, generation)]
            return RuntimeSecrets({kind: values.require(kind) for kind in kinds})
        except (KeyError, SetupValidationError):
            raise CredentialStoreError("credential operation failed") from None

    def delete_generation(self, profile_id: str, generation: str) -> None:
        self.data.pop((profile_id, generation), None)

    def list_generations(self, profile_id: str) -> tuple[str, ...]:
        return tuple(sorted(generation for profile, generation in self.data if profile == profile_id))

def _controller(
    tmp_path: Path,
    *,
    store: FakeStore | None = None,
    builder: FakeRuntimeBuilder | None = None,
) -> tuple[SetupController, FakeStore, FakeRuntimeBuilder, FakeBootstrap]:
    actual_store = store or FakeStore()
    actual_builder = builder or FakeRuntimeBuilder()
    bootstrap = FakeBootstrap()
    controller = SetupController(
        profile_id="managed-profile",
        store=actual_store,
        runtime_bootstrap=bootstrap,
        runtime_builder=actual_builder,
        activation_timeout=1.0,
        cleanup_timeout=0.05,
    )
    controller.apply_runtime(_runtime())
    controller.apply_workflow(_workflow(tmp_path))
    for kind in (SecretKind.ONES_PASSWORD, SecretKind.PROVIDER_TOKEN):
        controller.set_secret(kind, "TOKEN-SECRET")
    return controller, actual_store, actual_builder, bootstrap


async def _pass_all(controller: SetupController) -> None:
    await controller.test_step(SetupStep.PROFILE)
    for step in SetupController.STEPS[1:-1]:
        await controller.test_step(step, object())


@pytest.mark.asyncio
async def test_profile_retest_passes_exact_managed_provenance_to_catalog(
    tmp_path: Path,
) -> None:
    controller, _, _, bootstrap = _controller(tmp_path)

    result = await controller.test_step(SetupStep.PROFILE)

    assert result.status is ValidationStatus.PASSED
    assert bootstrap.catalog.selections == [
        ("managed-profile", SandboxPermissionProfileSource.MANAGED)
    ]


@pytest.mark.asyncio
async def test_profile_retest_passes_builtin_provenance_without_managed_fallback(
    tmp_path: Path,
) -> None:
    class Catalog(FakeCatalog):
        def __init__(self) -> None:
            super().__init__()
            self.managed_lists = 0
            self.builtin_calls = 0

        def list_profiles(self) -> tuple[str, ...]:
            self.managed_lists += 1
            return ()

        def verify_builtin_workspace_profile(self) -> str:
            self.builtin_calls += 1
            return BUILTIN_WORKSPACE_PROFILE

        def require_selected(
            self,
            profile: str,
            source: SandboxPermissionProfileSource,
        ) -> str:
            self.selections.append((profile, source))
            if (
                type(profile) is not str
                or type(source) is not SandboxPermissionProfileSource
                or source is not SandboxPermissionProfileSource.BUILTIN_WORKSPACE
                or profile != BUILTIN_WORKSPACE_PROFILE
            ):
                raise ValueError
            verified = self.verify_builtin_workspace_profile()
            if type(verified) is not str or verified != BUILTIN_WORKSPACE_PROFILE:
                raise ValueError
            return verified

    controller, _, _, _ = _controller(tmp_path)
    data = controller.draft.workflow.model_dump(mode="python", round_trip=True)
    data.update(
        sandbox_permission_profile=BUILTIN_WORKSPACE_PROFILE,
        sandbox_permission_profile_source=(
            SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        ),
    )
    controller.apply_workflow(WorkflowDraft.model_validate(data))
    catalog = Catalog()
    controller._profile_catalog = catalog

    result = await controller.test_step(SetupStep.PROFILE)

    assert result.status is ValidationStatus.PASSED
    assert catalog.selections == [
        (
            BUILTIN_WORKSPACE_PROFILE,
            SandboxPermissionProfileSource.BUILTIN_WORKSPACE,
        )
    ]
    assert catalog.builtin_calls == 1
    assert catalog.managed_lists == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "source"),
    [
        (BUILTIN_WORKSPACE_PROFILE, SandboxPermissionProfileSource.MANAGED),
        ("managed-profile", SandboxPermissionProfileSource.BUILTIN_WORKSPACE),
        ("managed-profile", "managed"),
        ("managed-profile", type("SourceText", (str,), {})("managed")),
        (
            type("ProfileText", (str,), {})("managed-profile"),
            SandboxPermissionProfileSource.MANAGED,
        ),
    ],
)
async def test_profile_retest_rejects_confused_provenance_without_catalog_calls(
    tmp_path: Path,
    profile: object,
    source: object,
) -> None:
    controller, _, _, bootstrap = _controller(tmp_path)
    object.__setattr__(
        controller._draft.workflow,
        "sandbox_permission_profile",
        profile,
    )
    object.__setattr__(
        controller._draft.workflow,
        "sandbox_permission_profile_source",
        source,
    )

    result = await controller.test_step(SetupStep.PROFILE)

    assert result == ConnectionTestResult(
        step=SetupStep.PROFILE,
        status=ValidationStatus.FAILED,
        category="incompatible",
    )
    assert bootstrap.catalog.selections == []


@pytest.mark.asyncio
async def test_profile_retest_never_invokes_untrusted_profile_equality(
    tmp_path: Path,
) -> None:
    class EqualProfile:
        compared = False

        def __eq__(self, other: object) -> bool:
            self.compared = True
            return True

    profile = EqualProfile()
    controller, _, _, bootstrap = _controller(tmp_path)
    object.__setattr__(
        controller._draft.workflow,
        "sandbox_permission_profile",
        profile,
    )

    result = await controller.test_step(SetupStep.PROFILE)

    assert result.status is ValidationStatus.FAILED
    assert profile.compared is False
    assert bootstrap.catalog.selections == []


@pytest.mark.asyncio
async def test_profile_retest_catalog_failure_is_fixed_without_source_fallback(
    tmp_path: Path,
) -> None:
    class Catalog(FakeCatalog):
        def require_selected(self, profile, source):
            self.selections.append((profile, source))
            raise RuntimeError("CATALOG-SECRET")

    controller, _, _, _ = _controller(tmp_path)
    catalog = Catalog()
    controller._profile_catalog = catalog

    result = await controller.test_step(SetupStep.PROFILE)

    assert result == ConnectionTestResult(
        step=SetupStep.PROFILE,
        status=ValidationStatus.FAILED,
        category="incompatible",
    )
    assert catalog.selections == [
        ("managed-profile", SandboxPermissionProfileSource.MANAGED)
    ]


@pytest.mark.asyncio
async def test_profile_retest_catalog_cancellation_propagates_without_fallback(
    tmp_path: Path,
) -> None:
    failure = asyncio.CancelledError()

    class Catalog(FakeCatalog):
        def require_selected(self, profile, source):
            self.selections.append((profile, source))
            raise failure

    controller, _, _, _ = _controller(tmp_path)
    catalog = Catalog()
    controller._profile_catalog = catalog

    with pytest.raises(asyncio.CancelledError) as caught:
        await controller.test_step(SetupStep.PROFILE)

    assert caught.value is failure
    assert catalog.selections == [
        ("managed-profile", SandboxPermissionProfileSource.MANAGED)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [MemoryError, GeneratorExit])
async def test_profile_retest_catalog_priority_failure_propagates(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    failure = failure_type()

    class Catalog(FakeCatalog):
        def require_selected(self, profile, source):
            self.selections.append((profile, source))
            raise failure

    controller, _, _, _ = _controller(tmp_path)
    catalog = Catalog()
    controller._profile_catalog = catalog

    with pytest.raises(failure_type) as caught:
        await controller.test_step(SetupStep.PROFILE)

    assert caught.value is failure
    assert catalog.selections == [
        ("managed-profile", SandboxPermissionProfileSource.MANAGED)
    ]


@pytest.mark.asyncio
async def test_incomplete_setup_never_builds_runtime(tmp_path: Path) -> None:
    controller, _, builder, _ = _controller(tmp_path)
    with pytest.raises(SetupActionError, match="configuration is incomplete"):
        await controller.save_and_activate(confirmed=True)
    assert builder.calls == []


@pytest.mark.asyncio
async def test_ui_mapping_snapshot_is_converted_to_strict_ones_probe(
    tmp_path: Path,
) -> None:
    from types import MappingProxyType

    from src.developer_workflow.setup_validation import OnesProbeInput

    controller, _, _, bootstrap = _controller(tmp_path)
    await controller.test_step(SetupStep.PROFILE)
    await controller.test_step(
        SetupStep.ONES,
        MappingProxyType(
            {
                "ones-team-id": "team-1",
                "ones-project-id": "project-1",
                "ones-status-id": "status-1",
                    "ones-item-id": "item-1",
                    "ones-issue-type-id": "issue-1",
                    "ones-password": "MUST-NOT-ENTER-PROBE",
            }
        ),
    )
    _, probe = bootstrap.validator.probes[-1]
    assert isinstance(probe, OnesProbeInput)
    assert "MUST-NOT-ENTER-PROBE" not in repr(probe)


@pytest.mark.asyncio
async def test_controller_reaches_review_with_ui_shaped_allowlist_probes(
    tmp_path: Path,
) -> None:
    from types import MappingProxyType

    controller, _, _, _ = _controller(tmp_path)
    probes = {
        SetupStep.ONES: MappingProxyType(
            {
                "ones-team-id": "team-1",
                "ones-project-id": "project-1",
                "ones-status-id": "status-1",
                "ones-item-id": "item-1",
                "ones-issue-type-id": "issue-1",
            }
        ),
        SetupStep.REPOSITORIES: MappingProxyType(
            {
                "repository-path": str(tmp_path),
                "repository-url": "https://git.example.test/sdk.git",
            }
        ),
        SetupStep.PROVIDER: MappingProxyType(
            {
                "provider-host": "provider.example.test",
                "provider-api-url": "https://provider.example.test/api",
            }
        ),
        SetupStep.CODEX: MappingProxyType(
            {
                "codex-profile": "managed-profile",
                "codex-worktree": str(tmp_path),
            }
        ),
        SetupStep.PRIVATE_PATHS: MappingProxyType(
            {
                "run-root": str(tmp_path / "runs"),
                "mirror-root": str(tmp_path / "mirrors"),
                "worktree-root": str(tmp_path / "worktrees"),
            }
        ),
    }
    await controller.test_step(SetupStep.PROFILE)
    for step in SetupController.STEPS[1:-1]:
        await controller.test_step(step, probes[step])
    assert controller.current_step is SetupStep.REVIEW
    assert controller.draft.workflow.repositories[0].key == "sdk"


def test_upsert_repository_replaces_same_key_and_invalidates_downstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    replacement = controller.draft.workflow.repositories[0].validated_update(
        base_branch="release"
    )
    monkeypatch.setattr(
        "src.developer_workflow.setup_controller.build_repository",
        lambda **fields: replacement,
    )
    controller.upsert_repository(
        key="sdk",
        project_id="project-1",
        iteration_id="iteration-1",
        repo_url="https://git.example.test/sdk.git",
        repo_name="sdk",
        base_branch="release",
    )
    repositories = controller.draft.workflow.repositories
    assert len(repositories) == 1
    assert repositories[0].base_branch == "release"


@pytest.mark.asyncio
async def test_probe_factories_receive_current_public_fields_and_transient_secrets(
    tmp_path: Path,
) -> None:
    controller, _, builder, bootstrap = _controller(tmp_path)
    bootstrap.validator.ones_gateway = None
    captured: list[tuple[object, object]] = []

    class Gateway:
        async def close(self) -> None:
            return None

    def build_gateway(public: object, secrets: object) -> Gateway:
        captured.append((public, secrets))
        return Gateway()

    builder.build_ones_probe_gateway = build_gateway  # type: ignore[attr-defined]
    controller.apply_runtime_fields(
        SetupStep.ONES,
        {
            "ones_base_url": "https://new.ones.example.test",
            "ones_team_id": "new-team",
            "ones_issue_type_id": "new-defect",
        },
    )
    controller.set_secret(SecretKind.ONES_EMAIL, "fresh@example.test")
    controller.set_secret(SecretKind.ONES_PASSWORD, "FRESH-SECRET")
    await controller.test_step(SetupStep.PROFILE)
    await controller.test_step(SetupStep.ONES, object())
    public, secrets = captured[0]
    assert public["ones_team_id"] == "new-team"
    assert secrets.require(SecretKind.ONES_PASSWORD) == "FRESH-SECRET"
    assert bootstrap.validator.ones_gateway is None


def test_runtime_field_revisit_updates_real_draft_and_invalidates_downstream(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    controller._results = {
        step: _result(step) for step in SetupController.STEPS[:-1]
    }
    controller.apply_runtime_fields(
        SetupStep.ONES,
        {
            "ones_base_url": "https://changed.ones.example.test",
            "ones_team_id": "changed-team",
            "ones_issue_type_id": "changed-issue",
        },
    )
    assert controller.draft.runtime.ones_team_id == "changed-team"
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.NOT_CONFIGURED
    assert controller.result_for(SetupStep.PROVIDER).status is ValidationStatus.NOT_CONFIGURED


def test_group_transaction_moves_members_and_replaces_nested_mapping(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.contracts import (
        RepositoryGroupMapping,
        RepositoryMapping,
        RepositoryRole,
    )
    from src.developer_workflow.setup_controller import SetupStepTransaction

    controller, _, _, _ = _controller(tmp_path)
    sdk = controller.draft.workflow.repositories[0]
    dependency = RepositoryMapping(
        key="dependency", project_id=sdk.project_id,
        iteration_id=sdk.iteration_id,
        repo_url="https://git.example.test/dependency.git",
        repo_name="dependency", role=RepositoryRole.DEPENDENCY,
        depends_on=("sdk",),
    )
    group = RepositoryGroupMapping(
        key="workspace", project_id=sdk.project_id,
        iteration_id=sdk.iteration_id, primary_repository="sdk",
        repositories=(
            sdk.validated_update(role=RepositoryRole.PRIMARY), dependency
        ),
    )
    controller.apply_step_transaction(
        SetupStep.REPOSITORIES,
        SetupStepTransaction(repository_group=group),
        expected_revision=controller.revision,
    )
    assert controller.draft.workflow.repositories == ()
    assert len(controller.draft.workflow.repository_groups) == 1
    changed = group.validated_update(
        repositories=(
            group.repositories[0].validated_update(base_branch="release"),
            group.repositories[1],
        )
    )
    controller.apply_step_transaction(
        SetupStep.REPOSITORIES,
        SetupStepTransaction(repository_group=changed),
        expected_revision=controller.revision,
    )
    workflow = controller.draft.workflow
    assert len(workflow.repository_groups) == 1
    assert workflow.repository_groups[0].repositories[0].base_branch == "release"
    DeveloperWorkflowConfig.model_validate(workflow.model_dump(round_trip=True))


def test_step_transaction_rejects_all_changes_without_partial_secret_write(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.setup_controller import SetupStepTransaction

    controller, _, _, _ = _controller(tmp_path)
    before = controller.draft
    with pytest.raises(SetupActionError):
        controller.apply_step_transaction(
            SetupStep.ONES,
            SetupStepTransaction(
                runtime_fields={
                    "ones_base_url": "https://changed.ones.example.test",
                    "ones_team_id": "changed-team",
                    "ones_issue_type_id": "changed-issue",
                }
            ),
            expected_revision=controller.revision,
            secrets={SecretKind.ONES_PASSWORD: "BAD\nSECRET"},
        )
    assert controller.draft == before
    assert controller.secret_presence(SecretKind.ONES_PASSWORD)


def test_step_transaction_rejects_cross_step_members_and_secret_kinds(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.setup_controller import SetupStepTransaction

    controller, _, _, _ = _controller(tmp_path)
    before = controller.draft
    with pytest.raises(SetupActionError):
        controller.apply_step_transaction(
            SetupStep.ONES,
            SetupStepTransaction(workflow=before.workflow),
            expected_revision=controller.revision,
        )
    with pytest.raises(SetupActionError):
        controller.apply_step_transaction(
            SetupStep.ONES,
            SetupStepTransaction(),
            expected_revision=controller.revision,
            secrets={SecretKind.PROVIDER_TOKEN: "WRONG-STEP-TOKEN"},
        )
    with pytest.raises(SetupActionError):
        controller.apply_step_transaction(
            SetupStep.ONES,
            SetupStepTransaction(
                runtime_fields={
                    "ones_base_url": "https://ones.example.test",
                    "ones_team_id": "team-1",
                    "ones_issue_type_id": "issue-1",
                    "provider_host": "cross-step.example.test",
                }
            ),
            expected_revision=controller.revision,
        )
    assert controller.draft == before


def test_first_codex_materialization_rejects_unvalidated_runtime_prefix(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.setup_controller import SetupStepTransaction

    controller = SetupController(
        profile_id="managed-profile",
        store=FakeStore(),
        runtime_builder=FakeRuntimeBuilder(),
        runtime_bootstrap=FakeBootstrap(),
    )
    controller.apply_runtime_fields(
        SetupStep.ONES,
        {
            "ones_base_url": "https://ones.example.test",
            "ones_team_id": "validated-team",
            "ones_issue_type_id": "issue-1",
        },
    )
    controller.apply_runtime_fields(
        SetupStep.PROVIDER,
        {
            "provider_host": "provider.example.test",
            "provider_api_url": "https://provider.example.test/api",
            "git_author_name": "ONES Agent",
            "git_author_email": "agent@example.test",
            "provider": "github",
        },
    )
    controller._results[SetupStep.ONES] = _result(SetupStep.ONES)
    forged = _runtime().validated_update(ones_team_id="unvalidated-team")
    before = controller.draft
    revision = controller.revision

    with pytest.raises(SetupActionError):
        controller.apply_step_transaction(
            SetupStep.CODEX,
            SetupStepTransaction(runtime=forged),
            expected_revision=revision,
        )

    assert controller.draft == before
    assert controller.revision == revision
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.PASSED


def test_provider_edit_updates_existing_runtime_not_only_form_fragments():
    from src.developer_workflow.setup_controller import SetupStepTransaction
    controller = SetupController(profile_id="managed-profile", store=FakeStore(),
        runtime_builder=FakeRuntimeBuilder(), runtime_bootstrap=FakeBootstrap(),
        draft=SetupDraft(runtime=_runtime()))
    old = controller.draft.runtime
    controller.apply_step_transaction(SetupStep.PROVIDER, SetupStepTransaction(runtime_fields={
        "provider_host": "github.com", "provider_api_url": "https://api.github.com",
        "git_author_name": "New Author", "git_author_email": "new@example.test", "provider": "github",
    }), expected_revision=controller.revision)
    current = controller.draft.runtime
    assert current.provider_host == "github.com"
    assert current.provider_api_url == "https://api.github.com"
    assert current.git_author_name == "New Author"
    assert current.ones_team_id == old.ones_team_id


def test_first_codex_materialization_rejects_missing_validated_fragments(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.setup_controller import SetupStepTransaction

    controller = SetupController(
        profile_id="managed-profile",
        store=FakeStore(),
        runtime_builder=FakeRuntimeBuilder(),
        runtime_bootstrap=FakeBootstrap(),
    )
    controller.apply_runtime_fields(
        SetupStep.ONES,
        {
            "ones_base_url": "https://ones.example.test",
            "ones_team_id": "team-1",
            "ones_issue_type_id": "issue-1",
        },
    )
    with pytest.raises(SetupActionError):
        controller.apply_step_transaction(
            SetupStep.CODEX,
            SetupStepTransaction(runtime=_runtime()),
            expected_revision=controller.revision,
        )


def test_step_transaction_invalidates_from_actual_earliest_workflow_diff(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.setup_controller import SetupStepTransaction

    controller, _, _, _ = _controller(tmp_path)
    controller._results = {
        step: _result(step) for step in SetupController.STEPS[:-1]
    }
    workflow = controller.draft.workflow.model_copy(deep=True)
    workflow.sandbox_permission_profile = "changed-profile"
    controller.apply_step_transaction(
        SetupStep.PRIVATE_PATHS,
        SetupStepTransaction(workflow=workflow),
        expected_revision=controller.revision,
    )
    assert controller.result_for(SetupStep.PROFILE).status is ValidationStatus.NOT_CONFIGURED

    controller._results = {
        candidate: _result(candidate) for candidate in SetupController.STEPS[:-1]
    }
    workflow = controller.draft.workflow.model_copy(deep=True)
    workflow.run_root = tmp_path / "new-runs"
    controller.apply_step_transaction(
        SetupStep.PROFILE,
        SetupStepTransaction(workflow=workflow),
        expected_revision=controller.revision,
    )
    assert controller.result_for(SetupStep.PROFILE).status is ValidationStatus.PASSED
    assert (
        controller.result_for(SetupStep.PRIVATE_PATHS).status
        is ValidationStatus.NOT_CONFIGURED
    )


@pytest.mark.asyncio
async def test_ones_tui_probe_uses_validated_issue_type_from_current_edit(
    tmp_path: Path,
) -> None:
    controller, _, _, bootstrap = _controller(tmp_path)
    await controller.test_step(SetupStep.PROFILE)
    await controller.test_step(
        SetupStep.ONES,
        {
            "ones-team-id": "team-1",
            "ones-project-id": "project-1",
            "ones-status-id": "status-1",
            "ones-item-id": "item-1",
            "ones-issue-type-id": "defect-1",
        },
    )
    probe = bootstrap.validator.probes[-1][1]
    assert probe.issue_type_id == "defect-1"


@pytest.mark.asyncio
async def test_complete_setup_candidate_retains_all_accepted_credentials(
    tmp_path: Path,
) -> None:
    controller, _, builder, _ = _controller(tmp_path)
    controller.set_secret(SecretKind.ONES_EMAIL, "agent@example.test")
    await _pass_all(controller)
    await controller.save_and_activate(confirmed=True)
    assert set(builder.calls[0][0].credential_kinds) == {
        SecretKind.ONES_EMAIL,
        SecretKind.ONES_PASSWORD,
        SecretKind.PROVIDER_TOKEN,
    }


def test_cancel_clears_transient_secrets(tmp_path: Path) -> None:
    controller, _, _, _ = _controller(tmp_path)
    assert "TOKEN-SECRET" not in repr(controller)
    controller.cancel_edit()
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_steps_cannot_be_skipped_and_edit_invalidates_downstream(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    with pytest.raises(SetupActionError, match="step is unavailable"):
        await controller.test_step(SetupStep.PROVIDER, object())
    await _pass_all(controller)
    assert controller.current_step is SetupStep.REVIEW
    controller.apply_runtime(_runtime(), changed_step=SetupStep.ONES)
    assert controller.result_for(SetupStep.PROFILE).status is ValidationStatus.PASSED
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.NOT_CONFIGURED
    assert controller.current_step is SetupStep.ONES


@pytest.mark.asyncio
async def test_review_requires_all_six_steps_and_explicit_confirmation(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    with pytest.raises(SetupActionError, match="review confirmation is required"):
        await controller.save_and_activate()


@pytest.mark.asyncio
async def test_concurrent_save_commits_and_builds_once(tmp_path: Path) -> None:
    controller, store, builder, _ = _controller(tmp_path)
    await _pass_all(controller)
    results = await asyncio.gather(
        controller.save_and_activate(confirmed=True),
        controller.save_and_activate(confirmed=True),
        return_exceptions=True,
    )
    assert sum(isinstance(value, Handle) for value in results) == 1
    assert store.commits == 1
    assert len(builder.calls) == 1


@pytest.mark.asyncio
async def test_draft_change_discards_stale_connection_result(tmp_path: Path) -> None:
    controller, _, _, bootstrap = _controller(tmp_path)
    await controller.test_step(SetupStep.PROFILE)
    bootstrap.validator.block = True
    task = asyncio.create_task(controller.test_step(SetupStep.ONES, object()))
    await bootstrap.validator.started.wait()
    controller.apply_runtime(_runtime(), changed_step=SetupStep.ONES)
    bootstrap.validator.release.set()
    with pytest.raises(SetupActionError, match="configuration changed"):
        await task
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_cancel_during_test_cancels_probe_and_clears_secrets(tmp_path: Path) -> None:
    controller, _, _, bootstrap = _controller(tmp_path)
    await controller.test_step(SetupStep.PROFILE)
    bootstrap.validator.block = True
    task = asyncio.create_task(controller.test_step(SetupStep.ONES, object()))
    await bootstrap.validator.started.wait()
    controller.cancel_edit()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.secret_presence(SecretKind.PROVIDER_TOKEN) is False


@pytest.mark.asyncio
async def test_generation_is_fresh_and_credential_kinds_are_sorted(
    tmp_path: Path,
) -> None:
    controller, _, builder, _ = _controller(tmp_path)
    await _pass_all(controller)
    await controller.save_and_activate(confirmed=True)
    active, _ = builder.calls[0]
    assert len(active.generation) == 32
    assert active.generation != "0" * 32
    assert active.credential_kinds == tuple(
        sorted(active.credential_kinds, key=lambda kind: kind.value)
    )
    assert controller.closed is True
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_build_failure_closes_partial_handle_rolls_back_and_sanitizes(
    tmp_path: Path,
) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    old_handle = await old_controller.save_and_activate(confirmed=True)
    assert isinstance(old_handle, Handle)
    controller, _, builder, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    builder.error = RuntimeError("TOKEN-SECRET")
    with pytest.raises(SetupActionError, match="runtime validation failed") as error:
        await controller.save_and_activate(confirmed=True)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert store.restores == 1
    assert controller.current_step is SetupStep.REVIEW
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_finalize_failure_closes_handle_and_rolls_back(tmp_path: Path) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    store.finalize_error = RuntimeError("TOKEN-SECRET")
    controller, _, builder, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    with pytest.raises(SetupActionError, match="runtime validation failed"):
        await controller.save_and_activate(confirmed=True)
    assert builder.handle.closed == 1
    assert store.restores == 1


@pytest.mark.asyncio
async def test_cancelled_save_rolls_back_committed_candidate(tmp_path: Path) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)

    class BlockingBuilder(FakeRuntimeBuilder):
        def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
            import time

            self.calls.append((active, secrets))
            time.sleep(0.2)
            return self.handle

    builder = BlockingBuilder()
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    while not builder.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.restores == 1
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False
    await asyncio.sleep(0.25)
    assert builder.handle.closed == 1


@pytest.mark.asyncio
async def test_activate_existing_failure_is_fixed_and_does_not_mutate_active(
    tmp_path: Path,
) -> None:
    controller, store, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    await controller.save_and_activate(confirmed=True)
    active = store.document.active
    failing = FakeRuntimeBuilder()
    failing.error = RuntimeError("TOKEN-SECRET")
    loader, _, _, _ = _controller(tmp_path, store=store, builder=failing)
    assert await loader.activate_existing() is None
    assert store.document.active == active
    assert loader.activation_error == "runtime validation failed"


def test_close_is_idempotent_and_public_state_has_only_summaries(tmp_path: Path) -> None:
    controller, _, _, _ = _controller(tmp_path)
    state = controller.state
    assert state.repository_count == 1
    assert state.repository_group_count == 0
    assert not hasattr(state, "run_root")
    assert not hasattr(state, "secrets")
    controller.close()
    controller.close()
    assert controller.closed is True
    with pytest.raises(SetupActionError, match="setup is closed"):
        controller.set_secret(SecretKind.ONES_PASSWORD, "TOKEN-SECRET")


@pytest.mark.asyncio
async def test_real_setup_store_commit_reload_and_finalize(tmp_path: Path) -> None:
    credentials = IntegrationCredentials()
    store = SetupStore(
        credentials,  # type: ignore[arg-type]
        config_path=tmp_path / "private" / "config.json",
    )
    builder = FakeRuntimeBuilder()
    controller, _, _, _ = _controller(
        tmp_path,
        store=store,  # type: ignore[arg-type]
        builder=builder,
    )
    await _pass_all(controller)
    handle = await controller.save_and_activate(confirmed=True)
    loaded = store.load()
    assert handle is builder.handle
    assert loaded.active is not None
    assert loaded.previous is None
    assert credentials.read_generation(
        loaded.profile_id,
        loaded.active.generation,
        loaded.active.credential_kinds,
    ).require(SecretKind.ONES_PASSWORD) == "TOKEN-SECRET"


@pytest.mark.asyncio
async def test_first_activation_failure_with_real_store_restores_empty_document(
    tmp_path: Path,
) -> None:
    credentials = IntegrationCredentials()
    store = SetupStore(
        credentials,  # type: ignore[arg-type]
        config_path=tmp_path / "private-failure" / "config.json",
    )
    builder = FakeRuntimeBuilder()
    builder.error = RuntimeError("TOKEN-SECRET")
    controller, _, _, _ = _controller(
        tmp_path,
        store=store,  # type: ignore[arg-type]
        builder=builder,
    )
    await _pass_all(controller)

    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await controller.save_and_activate(confirmed=True)

    loaded = store.load()
    assert loaded.active is None
    assert loaded.previous is None
    assert store.orphan_generations() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_stable", (False, True), ids=("first", "existing"))
async def test_activate_existing_refuses_crash_recovery_pending_before_secret_read(
    tmp_path: Path, has_stable: bool
) -> None:
    credentials = IntegrationCredentials()
    path = tmp_path / "pending-restart" / "config.json"
    store = SetupStore(credentials, config_path=path)  # type: ignore[arg-type]
    if has_stable:
        stable = _candidate_for_store(tmp_path, "a" * 32)
        store.commit("managed-profile", stable, _persisted_secrets())
        store.finalize_activation("managed-profile", stable.generation)
    pending = _candidate_for_store(tmp_path, "b" * 32)
    before = store.commit("managed-profile", pending, _persisted_secrets())
    reads_before = credentials.reads
    restarted = SetupStore(credentials, config_path=path)  # type: ignore[arg-type]
    builder = FakeRuntimeBuilder()
    loader, _, _, _ = _controller(
        tmp_path, store=restarted, builder=builder  # type: ignore[arg-type]
    )

    assert await loader.activate_existing() is None

    assert loader.activation_error == "activation recovery required"
    assert "persisted-password" not in repr(loader.state)
    assert "persisted-provider" not in repr(loader.state)
    assert builder.calls == []
    assert credentials.reads == reads_before
    assert restarted.load() == before

    restored = restarted.restore_previous("managed-profile", pending.generation)
    handle = await loader.activate_existing()
    if has_stable:
        assert handle is builder.handle
        assert len(builder.calls) == 1
        assert loader.activation_error is None
        assert restored.active is not None
        assert restored.active.generation == "a" * 32
        assert await loader.activate_existing() is builder.handle
        assert len(builder.calls) == 2
        assert loader.activation_error is None
    else:
        assert handle is None
        assert builder.calls == []
        assert loader.activation_error is None
        assert restored.active is None
        assert await loader.activate_existing() is None
        assert builder.calls == []
        assert loader.activation_error is None
        loader._activation_error = "runtime validation failed"
        assert await loader.activate_existing() is None
        assert loader.activation_error == "runtime validation failed"


@pytest.mark.asyncio
async def test_commit_post_replace_failure_is_detected_and_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = IntegrationCredentials()
    store = SetupStore(
        credentials,  # type: ignore[arg-type]
        config_path=tmp_path / "private-commit-failure" / "config.json",
    )
    builder = FakeRuntimeBuilder()
    controller, _, _, _ = _controller(
        tmp_path,
        store=store,  # type: ignore[arg-type]
        builder=builder,
    )
    await _pass_all(controller)
    monkeypatch.setattr(
        "src.developer_workflow.setup_store._fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("TOKEN-SECRET")),
    )

    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await controller.save_and_activate(confirmed=True)

    assert builder.calls == []
    assert store.load().active is None
    assert store.orphan_generations() == ()


@pytest.mark.asyncio
async def test_cancel_during_finalize_harvests_success_without_rollback(
    tmp_path: Path,
) -> None:
    class BlockingFinalizeStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_started = Event()
            self.finalize_release = Event()

        def finalize_activation(
            self, profile_id: str, expected_generation: str
        ) -> SetupDocument:
            self.finalize_started.set()
            assert self.finalize_release.wait(2)
            return super().finalize_activation(profile_id, expected_generation)

    store = BlockingFinalizeStore()
    controller, _, builder, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(store.finalize_started.wait, 2)

    task.cancel()
    for _ in range(8):
        await asyncio.sleep(0)
        task.cancel()
    store.finalize_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.finalizes == 1
    assert store.restores == 0
    assert store.document.active is not None
    assert store.document.previous is None
    assert builder.handle.closed == 1


@pytest.mark.asyncio
async def test_runtime_diff_cannot_be_hidden_by_later_changed_step(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    changed = _runtime().model_copy(
        update={"ones_team_id": "team-2"}, deep=True
    )

    controller.apply_runtime(changed, changed_step=SetupStep.PRIVATE_PATHS)

    assert controller.current_step is SetupStep.ONES
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_workflow_diff_cannot_be_hidden_by_later_changed_step(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    changed = _workflow(tmp_path)
    changed.max_codex_attempts = 4

    controller.apply_workflow(changed, changed_step=SetupStep.PRIVATE_PATHS)

    assert controller.current_step is SetupStep.PRIVATE_PATHS


class BlockingMutationBuilder(FakeRuntimeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
        self.calls.append((active, secrets))
        self.started.set()
        assert self.release.wait(2)
        return self.handle


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("draft", "secret"))
async def test_mutation_during_save_rejects_stale_candidate_and_stays_editable(
    tmp_path: Path, mutation: str
) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    builder = BlockingMutationBuilder()
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(builder.started.wait, 2)

    if mutation == "draft":
        changed = _workflow(tmp_path)
        changed.max_codex_attempts = 4
        controller.apply_workflow(changed, changed_step=SetupStep.PRIVATE_PATHS)
    else:
        controller.set_secret(SecretKind.ONES_PASSWORD, "NEW-TOKEN-SECRET")
    builder.release.set()

    with pytest.raises(SetupActionError, match="^configuration changed$"):
        await task
    assert builder.handle.closed == 1
    assert store.restores == 1
    assert store.finalizes == 1  # only the old activation was finalized
    assert controller.closed is False
    assert store.document.active is not None
    controller.set_secret(SecretKind.ONES_PASSWORD, "REENTERED-TOKEN")
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is True


@pytest.mark.asyncio
async def test_repeated_cancel_during_commit_harvests_and_rolls_back(
    tmp_path: Path,
) -> None:
    class BlockingCommitStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.commit_started = Event()
            self.commit_release = Event()

        def commit(
            self, profile_id: str, candidate: ActiveSetup, secrets: RuntimeSecrets
        ) -> SetupDocument:
            self.commit_started.set()
            assert self.commit_release.wait(2)
            return super().commit(profile_id, candidate, secrets)

    store = BlockingCommitStore()
    store.commit_release.set()
    old_controller, _, _, _ = _controller(tmp_path, store=store)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    old_active = store.document.active
    controller, _, _, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    store.commit_started.clear()
    store.commit_release.clear()
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(store.commit_started.wait, 2)

    task.cancel()
    for _ in range(8):
        await asyncio.sleep(0)
        task.cancel()
    store.commit_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.restores == 1
    assert store.document.active == old_active
    assert store.document.previous is None
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_cancelled_commit_error_after_pointer_write_is_still_rolled_back(
    tmp_path: Path,
) -> None:
    class PostWriteFailingCommitStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.block = False
            self.commit_started = Event()
            self.commit_release = Event()

        def commit(
            self, profile_id: str, candidate: ActiveSetup, secrets: RuntimeSecrets
        ) -> SetupDocument:
            if self.block:
                self.commit_started.set()
                assert self.commit_release.wait(2)
            document = super().commit(profile_id, candidate, secrets)
            if self.block:
                raise SetupStoreError("configuration save failed")
            return document

    store = PostWriteFailingCommitStore()
    old_controller, _, _, _ = _controller(tmp_path, store=store)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    old_active = store.document.active
    store.block = True
    controller, _, _, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(store.commit_started.wait, 2)

    task.cancel()
    for _ in range(8):
        await asyncio.sleep(0)
        task.cancel()
    store.commit_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.restores == 1
    assert store.document.active == old_active
    assert store.document.previous is None
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_repeated_cancel_after_finalize_completes_all_cleanup(
    tmp_path: Path,
) -> None:
    class BlockingCloseHandle(Handle):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = Event()
            self.close_release = Event()

        def close(self) -> None:
            self.close_started.set()
            assert self.close_release.wait(2)
            super().close()

    class BlockingFinalizeStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_started = Event()
            self.finalize_release = Event()

        def finalize_activation(
            self, profile_id: str, expected_generation: str
        ) -> SetupDocument:
            self.finalize_started.set()
            assert self.finalize_release.wait(2)
            return super().finalize_activation(profile_id, expected_generation)

    store = BlockingFinalizeStore()
    builder = FakeRuntimeBuilder()
    builder.handle = BlockingCloseHandle()
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(store.finalize_started.wait, 2)
    task.cancel()
    store.finalize_release.set()
    assert await asyncio.to_thread(builder.handle.close_started.wait, 2)

    task.cancel()
    for _ in range(8):
        await asyncio.sleep(0)
        task.cancel()
    builder.handle.close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.finalizes == 1
    assert store.restores == 0
    assert store.document.active is not None
    assert store.document.previous is None
    assert builder.handle.closed == 1
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_repeated_cancel_during_rollback_waits_for_pointer_restoration(
    tmp_path: Path,
) -> None:
    class BlockingRestoreStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.restore_started = Event()
            self.restore_release = Event()

        def restore_previous(
            self, profile_id: str, expected_generation: str
        ) -> SetupDocument:
            self.restore_started.set()
            assert self.restore_release.wait(2)
            return super().restore_previous(profile_id, expected_generation)

    store = BlockingRestoreStore()
    old_controller, _, _, _ = _controller(tmp_path, store=store)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    old_active = store.document.active
    builder = BlockingMutationBuilder()
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(builder.started.wait, 2)
    task.cancel()
    assert await asyncio.to_thread(store.restore_started.wait, 2)

    for _ in range(8):
        task.cancel()
        await asyncio.sleep(0)
    store.restore_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.document.active == old_active
    assert store.document.previous is None
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False
    builder.release.set()
    await asyncio.sleep(0.05)
    assert builder.handle.closed == 1


class LongBlockingCloseHandle(Handle):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0
        self.close_started = Event()
        self.close_release = Event()

    def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        assert self.close_release.wait(2)
        super().close()


@pytest.mark.asyncio
async def test_blocked_close_cannot_delay_rollback_secret_clear_or_lock_release(
    tmp_path: Path,
) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    old_active = store.document.active
    builder = FakeRuntimeBuilder()
    builder.handle = LongBlockingCloseHandle()
    store.finalize_error = RuntimeError("TOKEN-SECRET")
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(builder.handle.close_started.wait, 2)
    for _ in range(8):
        task.cancel()
        await asyncio.sleep(0)

    try:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.restores == 1
        assert store.document.active == old_active
        assert store.document.previous is None
        assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False
        result = await asyncio.wait_for(
            controller.test_step(SetupStep.PRIVATE_PATHS, object()), timeout=0.1
        )
        assert result.status is ValidationStatus.PASSED
    finally:
        builder.handle.close_release.set()
    await asyncio.sleep(0.05)
    assert builder.handle.close_calls == 1
    assert builder.handle.closed == 1
    assert controller._background_close_tasks == set()
    assert controller._abandoned_build_tasks == set()


@pytest.mark.asyncio
async def test_finalized_active_survives_background_close_timeout(
    tmp_path: Path,
) -> None:
    class BlockingFinalizeStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_started = Event()
            self.finalize_release = Event()

        def finalize_activation(
            self, profile_id: str, expected_generation: str
        ) -> SetupDocument:
            self.finalize_started.set()
            assert self.finalize_release.wait(2)
            return super().finalize_activation(profile_id, expected_generation)

    store = BlockingFinalizeStore()
    builder = FakeRuntimeBuilder()
    builder.handle = LongBlockingCloseHandle()
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(store.finalize_started.wait, 2)
    task.cancel()
    store.finalize_release.set()
    assert await asyncio.to_thread(builder.handle.close_started.wait, 2)

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
        assert store.finalizes == 1
        assert store.restores == 0
        assert store.document.active is not None
        assert store.document.previous is None
        assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False
    finally:
        builder.handle.close_release.set()
    await asyncio.sleep(0.05)
    assert builder.handle.close_calls == 1
    assert builder.handle.closed == 1
    assert controller._background_close_tasks == set()


@pytest.mark.asyncio
async def test_close_error_is_consumed_and_never_exposes_raw_exception(
    tmp_path: Path,
) -> None:
    class ErrorCloseHandle(Handle):
        def close(self) -> None:
            raise RuntimeError("TOKEN-SECRET")

    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    builder = FakeRuntimeBuilder()
    builder.handle = ErrorCloseHandle()
    store.finalize_error = RuntimeError("TOKEN-SECRET")
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)

    with pytest.raises(SetupActionError, match="^runtime validation failed$") as error:
        await controller.save_and_activate(confirmed=True)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert store.restores == 1
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


class LateRuntimeBuilder(FakeRuntimeBuilder):
    def __init__(self, handle: Handle, *, error: BaseException | None = None) -> None:
        super().__init__()
        self.handle = handle
        self.error = error
        self.started = Event()
        self.release = Event()

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
        self.calls.append((active, secrets))
        self.started.set()
        assert self.release.wait(2)
        if self.error is not None:
            raise self.error
        return self.handle


@pytest.mark.asyncio
async def test_late_handle_close_never_blocks_loop_and_is_tracked(
    tmp_path: Path,
) -> None:
    handle = LongBlockingCloseHandle()
    builder = LateRuntimeBuilder(handle)
    controller, _, _, _ = _controller(tmp_path, builder=builder)
    controller._activation_timeout = 0.02
    await _pass_all(controller)

    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await controller.save_and_activate(confirmed=True)
    controller.close()
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False

    started = asyncio.get_running_loop().time()
    builder.release.set()
    assert await asyncio.to_thread(handle.close_started.wait, 2)
    assert asyncio.get_running_loop().time() - started < 0.2
    assert len(controller._background_close_tasks) == 1
    await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.05)

    handle.close_release.set()
    for _ in range(50):
        if not controller._background_close_tasks:
            break
        await asyncio.sleep(0.01)
    assert controller._background_close_tasks == set()
    assert handle.close_calls == 1
    assert handle.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", ("close", "build"))
async def test_late_build_failures_are_consumed_without_secret_leak(
    tmp_path: Path, late_failure: str
) -> None:
    class CountingErrorCloseHandle(Handle):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def close(self) -> None:
            self.calls += 1
            raise RuntimeError("TOKEN-SECRET")

    handle = CountingErrorCloseHandle()
    builder = LateRuntimeBuilder(
        handle,
        error=RuntimeError("TOKEN-SECRET") if late_failure == "build" else None,
    )
    controller, _, _, _ = _controller(tmp_path, builder=builder)
    controller._activation_timeout = 0.02
    await _pass_all(controller)
    loop = asyncio.get_running_loop()
    observed: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: observed.append(context))
    try:
        with pytest.raises(SetupActionError, match="^runtime validation failed$"):
            await controller.save_and_activate(confirmed=True)
        builder.release.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if (
                late_failure == "close"
                and handle.calls == 1
                and not controller._background_close_tasks
            ):
                break
        assert observed == []
        assert "TOKEN-SECRET" not in repr(observed)
        assert controller._background_close_tasks == set()
        assert handle.calls == (1 if late_failure == "close" else 0)
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_superseded_controller_cannot_finalize_newer_generation(
    tmp_path: Path,
) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    first_builder = BlockingMutationBuilder()
    first, _, _, _ = _controller(tmp_path, store=store, builder=first_builder)
    await _pass_all(first)
    first_task = asyncio.create_task(first.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(first_builder.started.wait, 2)

    second_builder = FakeRuntimeBuilder()
    second, _, _, _ = _controller(tmp_path, store=store, builder=second_builder)
    await _pass_all(second)
    second_handle = await second.save_and_activate(confirmed=True)
    current = store.document.active
    first_builder.release.set()

    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await first_task
    assert second_handle is second_builder.handle
    assert store.document.active == current
    assert store.document.previous is None
    assert first_builder.handle.closed == 1
    assert first.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_superseded_rollback_cannot_restore_or_delete_newer_generation(
    tmp_path: Path,
) -> None:
    class ReleasingErrorBuilder(BlockingMutationBuilder):
        def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
            self.calls.append((active, secrets))
            self.started.set()
            assert self.release.wait(2)
            raise RuntimeError("TOKEN-SECRET")

    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    first_builder = ReleasingErrorBuilder()
    first, _, _, _ = _controller(tmp_path, store=store, builder=first_builder)
    await _pass_all(first)
    first_task = asyncio.create_task(first.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(first_builder.started.wait, 2)

    second_builder = BlockingMutationBuilder()
    second, _, _, _ = _controller(tmp_path, store=store, builder=second_builder)
    await _pass_all(second)
    second_task = asyncio.create_task(second.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(second_builder.started.wait, 2)
    newer = store.document.active
    first_builder.release.set()

    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await first_task
    assert store.document.active == newer
    assert store.document.previous is not None
    second_builder.release.set()
    await second_task
    assert store.document.active == newer
    assert store.document.previous is None


def test_asyncio_run_shutdown_still_closes_abandoned_build_handle_once(
    tmp_path: Path,
) -> None:
    class DelayedBuilder(FakeRuntimeBuilder):
        def __init__(self) -> None:
            super().__init__()
            self.handle = Handle()

        def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
            self.calls.append((active, secrets))
            time.sleep(0.1)
            return self.handle

    builder = DelayedBuilder()

    async def scenario() -> SetupController:
        controller, _, _, _ = _controller(tmp_path, builder=builder)
        controller._activation_timeout = 0.01
        await _pass_all(controller)
        with pytest.raises(SetupActionError, match="^runtime validation failed$"):
            await controller.save_and_activate(confirmed=True)
        controller.close()
        return controller

    controller = asyncio.run(scenario())

    assert builder.handle.closed == 1
    assert controller._background_close_tasks == set()
    assert controller._abandoned_build_tasks == set()


@pytest.mark.asyncio
async def test_aclose_drains_background_tasks_only_to_cleanup_deadline(
    tmp_path: Path,
) -> None:
    handle = LongBlockingCloseHandle()
    builder = LateRuntimeBuilder(handle)
    controller, _, _, _ = _controller(tmp_path, builder=builder)
    controller._activation_timeout = 0.01
    await _pass_all(controller)
    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await controller.save_and_activate(confirmed=True)
    builder.release.set()
    assert await asyncio.to_thread(handle.close_started.wait, 2)

    started = asyncio.get_running_loop().time()
    await controller.aclose()
    assert asyncio.get_running_loop().time() - started < 0.2
    assert controller.closed is True
    assert len(controller._background_close_tasks) == 1

    handle.close_release.set()
    for _ in range(50):
        if not controller._background_close_tasks:
            break
        await asyncio.sleep(0.01)
    assert controller._background_close_tasks == set()
    assert handle.close_calls == 1


def test_asyncio_run_does_not_wait_for_permanently_blocked_handle_close() -> None:
    script = r'''
import asyncio
from pathlib import Path
from threading import Event
import tempfile
import sys

sys.path.insert(0, "tests")
import test_developer_workflow_setup_controller as support
from src.developer_workflow.setup_controller import SetupActionError
from src.developer_workflow.setup_store import SetupStoreError

class BlockingHandle:
    def close(self):
        Event().wait()

class Builder(support.FakeRuntimeBuilder):
    def __init__(self):
        super().__init__()
        self.handle = BlockingHandle()

async def scenario():
    store = support.FakeStore()
    store.finalize_error = SetupStoreError("TOKEN-SECRET")
    controller, _, _, _ = support._controller(
        Path(tempfile.mkdtemp()), store=store, builder=Builder()
    )
    await support._pass_all(controller)
    try:
        await controller.save_and_activate(confirmed=True)
    except SetupActionError:
        pass
    await controller.aclose()

asyncio.run(scenario())
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
