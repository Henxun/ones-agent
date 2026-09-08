"""Secret-safe six-step setup orchestration without UI dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
import unicodedata
from uuid import uuid4

from .config import (
    BUILTIN_WORKSPACE_PROFILE,
    DeveloperWorkflowConfig,
    SandboxPermissionProfileSource,
)
from .contracts import RepositoryGroupMapping, RepositoryMapping
from .setup_import import ImportDetection, import_selected
from .setup_models import (
    ActiveSetup,
    DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE,
    OnesProbePublicConfig,
    ProviderProbePublicConfig,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
    SetupDraft,
    WorkflowDraft,
)
from .setup_repository import RepositoryGroupDraftBuilder, build_repository
from .setup_store import SetupStore, SetupStoreError
from .setup_validation import (
    CodexProbeInput,
    ConnectionTestResult,
    OnesProbeInput,
    PrivatePathsProbeInput,
    ProviderProbeInput,
    RepositoryProbeInput,
    SetupStep,
    SetupValidator,
    ValidationStatus,
)


class SetupActionError(RuntimeError):
    """A fixed, non-sensitive setup action failure."""


class InlineValidationError(SetupActionError):
    """Only known categories and stage labels may reach the UI."""

    def __init__(self, step: SetupStep, category: str) -> None:
        stage = {SetupStep.ONES: "ONES", SetupStep.PROVIDER: "GitHub / GitLab"}.get(step, "运行配置")
        reason = {"authentication": "认证失败：请检查令牌是否有效及账号权限",
                  "tls": "TLS 证书校验失败：请检查证书信任链",
                  "timeout": "连接超时", "unreachable": "服务不可达：请检查网络、域名与代理",
                  "incompatible": "API 返回非成功状态：请检查 API 地址及反向代理",
                  "invalid_field": "必填配置或凭据缺失"}.get(category, "校验未通过")
        super().__init__(f"{stage}：{reason}。")


class _ConfigurationChanged(RuntimeError):
    """Internal CAS failure without draft or credential details."""


def _is_setup_priority_failure(error: BaseException) -> bool:
    return isinstance(
        error,
        (
            MemoryError,
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ),
    )


def _raise_sanitized_action_exception(error: BaseException) -> None:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    raise error from None


def _raise_sanitized_action_failure(message: str) -> None:
    _raise_sanitized_action_exception(SetupActionError(message))


_NO_HANDLE = object()


class _CloseOnce:
    """Allow loop and fallback workers to race without closing twice."""

    def __init__(self, handle: object) -> None:
        self._handle = handle
        self._lock = Lock()
        self.started = Event()
        self._claimed = False

    def run(self) -> None:
        with self._lock:
            if self._claimed:
                return
            self._claimed = True
            self.started.set()
            handle = self._handle
        try:
            _close_handle(handle)
        finally:
            with self._lock:
                self._handle = None


class _BuildAttempt:
    """Transfer ownership of a runtime handle exactly once across loop shutdown."""

    def __init__(
        self,
        cleanup_timeout: float,
        abandoned_handoff: Callable[[object], None],
    ) -> None:
        self._lock = Lock()
        self._cleanup_timeout = cleanup_timeout
        self._abandoned_handoff = abandoned_handoff
        self._abandoned = False
        self._claimed = False
        self._completed: object = _NO_HANDLE

    def run(
        self,
        builder: RuntimeBuilder,
        active: ActiveSetup,
        secrets: RuntimeSecrets,
    ) -> object:
        handle = builder.build(active, secrets)
        close_here = False
        with self._lock:
            self._completed = handle
            if self._abandoned and not self._claimed:
                self._claimed = True
                close_here = True
        if close_here:
            self._abandoned_handoff(handle)
        return handle

    def abandon(self) -> object | None:
        with self._lock:
            self._abandoned = True
            if self._completed is _NO_HANDLE or self._claimed:
                return None
            self._claimed = True
            return self._completed

    def claim_late(self) -> bool:
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    def deliver(self) -> None:
        with self._lock:
            self._claimed = True


class RuntimeBuilder(Protocol):
    """Synchronous production runtime construction boundary."""

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> object: ...


@dataclass(frozen=True, slots=True)
class SetupControllerState:
    """Safe summary for future views; paths and raw values are deliberately absent."""

    current_step: SetupStep
    results: tuple[ConnectionTestResult, ...]
    repository_count: int
    repository_group_count: int
    secret_count: int
    review_confirmed: bool
    closed: bool
    error_category: str | None


@dataclass(frozen=True, slots=True)
class SetupRecoveryState:
    """Secret-free crash recovery summary for the restricted setup host."""

    owner_generation: str | None
    previous_available: bool
    orphan_count: int
    error_category: str | None


@dataclass(frozen=True, slots=True)
class SetupStepTransaction:
    runtime: RuntimePublicConfig | None = None
    workflow: WorkflowDraft | None = None
    runtime_fields: Mapping[str, str] | None = None
    repository: RepositoryMapping | None = None
    repository_group: RepositoryGroupMapping | None = None


_TRANSACTION_MEMBERS: dict[SetupStep, frozenset[str]] = {
    SetupStep.PROFILE: frozenset({"workflow"}),
    SetupStep.ONES: frozenset({"runtime_fields"}),
    SetupStep.REPOSITORIES: frozenset({"repository", "repository_group"}),
    SetupStep.PROVIDER: frozenset({"workflow", "runtime_fields"}),
    SetupStep.CODEX: frozenset({"runtime"}),
    SetupStep.PRIVATE_PATHS: frozenset({"workflow"}),
    SetupStep.REVIEW: frozenset(),
}
_TRANSACTION_RUNTIME_FIELDS: dict[SetupStep, frozenset[str]] = {
    SetupStep.ONES: frozenset(
        {
            "ones_base_url",
            "ones_team_id",
            "ones_issue_type_id",
        }
    ),
    SetupStep.PROVIDER: frozenset(
        {
            "provider_host", "provider_api_url", "git_author_name",
            "git_author_email", "provider",
        }
    ),
}


_NOT_CONFIGURED = "invalid_field"
_SECRET_STEP: dict[SecretKind, SetupStep] = {
    SecretKind.ONES_EMAIL: SetupStep.ONES,
    SecretKind.ONES_PASSWORD: SetupStep.ONES,
    SecretKind.PROVIDER_TOKEN: SetupStep.PROVIDER,
    SecretKind.CODEX_API_KEY: SetupStep.CODEX,
    SecretKind.CODEX_AUTH_TOKEN: SetupStep.CODEX,
    SecretKind.GIT_ASKPASS: SetupStep.REPOSITORIES,
    SecretKind.GIT_SSH: SetupStep.REPOSITORIES,
    SecretKind.GIT_SSH_COMMAND: SetupStep.REPOSITORIES,
    SecretKind.SSH_ASKPASS: SetupStep.REPOSITORIES,
    SecretKind.SSH_AUTH_SOCK: SetupStep.REPOSITORIES,
}


def _fixed_result(
    step: SetupStep,
    *,
    status: ValidationStatus = ValidationStatus.NOT_CONFIGURED,
    category: str = _NOT_CONFIGURED,
) -> ConnectionTestResult:
    return ConnectionTestResult(step=step, status=status, category=category)


def _close_handle(handle: object | None) -> None:
    if handle is None:
        return
    close = getattr(handle, "close", None)
    if not callable(close):
        return
    try:
        close()
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise


class SetupController:
    """Serialize setup probes, commit one generation, then activate it once."""

    STEPS = (
        SetupStep.PROFILE,
        SetupStep.ONES,
        SetupStep.REPOSITORIES,
        SetupStep.PROVIDER,
        SetupStep.PRIVATE_PATHS,
        SetupStep.REVIEW,
    )

    def __init__(
        self,
        *,
        profile_id: str,
        store: SetupStore,
        runtime_builder: RuntimeBuilder,
        runtime_bootstrap: object | None = None,
        validator: SetupValidator | None = None,
        profile_catalog: object | None = None,
        activation_timeout: float = 30.0,
        cleanup_timeout: float = 0.25,
        draft: SetupDraft | None = None,
        steps: tuple[SetupStep, ...] | None = None,
    ) -> None:
        if (
            type(profile_id) is not str
            or not profile_id
            or isinstance(activation_timeout, bool)
            or not isinstance(activation_timeout, (int, float))
            or activation_timeout <= 0
            or isinstance(cleanup_timeout, bool)
            or not isinstance(cleanup_timeout, (int, float))
            or cleanup_timeout <= 0
        ):
            raise SetupActionError("setup configuration is invalid")
        selected_steps = self.STEPS if steps is None else steps
        if (
            type(selected_steps) is not tuple
            or not selected_steps
            or selected_steps[-1] is not SetupStep.REVIEW
            or len(selected_steps) != len(set(selected_steps))
            or any(type(step) is not SetupStep for step in selected_steps)
        ):
            raise SetupActionError("setup configuration is invalid")
        bootstrap_validator = getattr(runtime_bootstrap, "validator", None)
        bootstrap_catalog = getattr(runtime_bootstrap, "catalog", None)
        actual_validator = validator or bootstrap_validator
        actual_catalog = profile_catalog or bootstrap_catalog
        if actual_validator is None or actual_catalog is None:
            raise SetupActionError("setup configuration is invalid")
        self._profile_id = profile_id
        self.STEPS = selected_steps
        self._store = store
        self._runtime_builder = runtime_builder
        self._validator = actual_validator
        self._profile_catalog = actual_catalog
        self._activation_timeout = float(activation_timeout)
        self._cleanup_timeout = float(cleanup_timeout)
        self._draft = (draft or SetupDraft()).model_copy(deep=True)
        self._results: dict[SetupStep, ConnectionTestResult] = {}
        self._runtime_fields: dict[str, str] = {}
        self._secrets: dict[SecretKind, bytearray] = {}
        self._revision = 0
        self._review_confirmed = False
        self._closed = False
        self._closing = False
        self._operation_lock = asyncio.Lock()
        self._operation_task: asyncio.Task[Any] | None = None
        self._activation_error: str | None = None
        self._finalizing = False
        self._background_close_tasks: set[asyncio.Future[None]] = set()
        self._abandoned_build_tasks: set[asyncio.Task[object]] = set()
        self._catalog_tasks: set[asyncio.Task[Any]] = set()
        self._catalog_owner_task: asyncio.Task[Any] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def activation_error(self) -> str | None:
        return self._activation_error

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def runtime_public_fields(self) -> Mapping[str, str]:
        """Return a detached, secret-free snapshot of validated runtime fragments."""

        return MappingProxyType(dict(self._runtime_fields))

    async def list_managed_profiles(self) -> tuple[str, ...]:
        """Return a detached trusted catalog snapshot without blocking the UI loop."""

        async with self._operation_lock:
            self._ensure_mutable()
            owner = asyncio.current_task()
            self._operation_task = owner
            self._catalog_owner_task = owner
            task = asyncio.create_task(
                asyncio.to_thread(self._profile_catalog.list_profiles)
            )
            try:
                profiles = tuple(await asyncio.shield(task))
            except asyncio.CancelledError:
                self._catalog_tasks.add(task)
                task.add_done_callback(self._finish_catalog_task)
                raise
            except BaseException as error:
                if _is_setup_priority_failure(error):
                    raise
                raise SetupActionError("managed profiles are unavailable") from None
            finally:
                if self._operation_task is owner:
                    self._operation_task = None
                if self._catalog_owner_task is owner:
                    self._catalog_owner_task = None
        try:
            if (
                any(type(profile) is not str or not profile for profile in profiles)
                or len(profiles) != len(set(profiles))
            ):
                raise ValueError
            return profiles
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            raise SetupActionError("managed profiles are unavailable") from None

    async def confirm_builtin_workspace_profile(self) -> str:
        """Probe, then atomically bind, the fixed runtime workspace profile."""

        verified_profile: object | None = None
        priority_failure: BaseException | None = None
        ordinary_failure = False
        revision = -1
        probe_task: asyncio.Task[Any] | None = None
        worker_awaitable: Any = None

        def verify_in_worker() -> object:
            verifier = getattr(
                self._profile_catalog,
                "verify_builtin_workspace_profile",
            )
            if not callable(verifier):
                raise TypeError
            return verifier()

        async with self._operation_lock:
            self._ensure_mutable()
            owner = asyncio.current_task()
            try:
                self._operation_task = owner
                self._catalog_owner_task = owner
                revision = self._revision
                worker_awaitable = asyncio.to_thread(verify_in_worker)
                probe_task = asyncio.create_task(worker_awaitable)
                worker_awaitable = None
                verified_profile = await asyncio.shield(probe_task)
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError) and probe_task is not None:
                    if probe_task.done():
                        self._finish_catalog_task(probe_task)
                    else:
                        self._catalog_tasks.add(probe_task)
                        probe_task.add_done_callback(self._finish_catalog_task)
                if _is_setup_priority_failure(error):
                    priority_failure = error
                else:
                    ordinary_failure = True
            finally:
                if worker_awaitable is not None:
                    try:
                        close = getattr(worker_awaitable, "close", None)
                        if callable(close):
                            close()
                    except BaseException as error:
                        if _is_setup_priority_failure(error):
                            if priority_failure is None:
                                priority_failure = error
                        elif priority_failure is None:
                            ordinary_failure = True
                    worker_awaitable = None
                if self._operation_task is owner:
                    self._operation_task = None
                if self._catalog_owner_task is owner:
                    self._catalog_owner_task = None

            if priority_failure is not None:
                failure = priority_failure
                del probe_task, priority_failure, verified_profile, verify_in_worker
                _raise_sanitized_action_exception(failure)
            if (
                ordinary_failure
                or type(verified_profile) is not str
                or verified_profile != BUILTIN_WORKSPACE_PROFILE
            ):
                del probe_task, priority_failure, verified_profile, verify_in_worker
                _raise_sanitized_action_failure(
                    "built-in workspace profile is unavailable"
                )
            self._ensure_mutable()
            if revision != self._revision:
                del probe_task, verified_profile
                _raise_sanitized_action_failure(
                    "configuration changed during validation"
                )
            candidate_data = self._draft.workflow.model_dump(
                mode="python", round_trip=True
            )
            candidate_data.update(
                {
                    "sandbox_permission_profile": BUILTIN_WORKSPACE_PROFILE,
                    "sandbox_permission_profile_source": (
                        SandboxPermissionProfileSource.BUILTIN_WORKSPACE
                    ),
                }
            )
            try:
                candidate = WorkflowDraft.model_validate(candidate_data)
                self.apply_workflow(
                    candidate,
                    changed_step=SetupStep.PROFILE,
                )
            except BaseException as error:
                if _is_setup_priority_failure(error):
                    raise
                ordinary_failure = True
            finally:
                candidate_data.clear()
            if ordinary_failure:
                del probe_task, verified_profile
                _raise_sanitized_action_failure(
                    "built-in workspace profile is unavailable"
                )
        return BUILTIN_WORKSPACE_PROFILE

    def _finish_catalog_task(self, task: asyncio.Task[Any]) -> None:
        self._catalog_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException:
            pass

    def apply_step_transaction(
        self,
        step: SetupStep,
        transaction: SetupStepTransaction,
        *,
        expected_revision: int,
        secrets: Mapping[SecretKind, str] = MappingProxyType({}),
    ) -> None:
        """Atomically commit one prevalidated UI candidate under a revision CAS."""

        self._ensure_mutable()
        self._require_step(step)
        if (
            type(transaction) is not SetupStepTransaction
            or type(expected_revision) is not int
            or expected_revision != self._revision
            or any(type(kind) is not SecretKind or type(value) is not str or not value
                   for kind, value in secrets.items())
        ):
            raise SetupActionError("configuration changed")
        populated = {
            name
            for name in (
                "runtime", "workflow", "runtime_fields", "repository",
                "repository_group",
            )
            if getattr(transaction, name) is not None
        }
        allowed_secrets = {
            kind for kind, owner in _SECRET_STEP.items() if owner is step
        }
        expected_runtime_fields = _TRANSACTION_RUNTIME_FIELDS.get(step)
        if (
            not populated <= _TRANSACTION_MEMBERS[step]
            or not set(secrets) <= allowed_secrets
            or transaction.runtime_fields is not None
            and (
                expected_runtime_fields is None
                or frozenset(transaction.runtime_fields) != expected_runtime_fields
            )
            or transaction.repository is not None
            and transaction.repository_group is not None
        ):
            raise SetupActionError("step transaction is invalid")
        previous_draft = self._draft.model_copy(deep=True)
        draft = self._draft.model_copy(deep=True)
        runtime_fields = dict(self._runtime_fields)
        encoded: dict[SecretKind, bytearray] = {}
        try:
            if transaction.runtime_fields is not None:
                raw_fields = dict(transaction.runtime_fields)
                if step is SetupStep.ONES:
                    OnesProbePublicConfig(
                        base_url=raw_fields["ones_base_url"],
                        team_id=raw_fields["ones_team_id"],
                        issue_type_id=raw_fields["ones_issue_type_id"],
                    )
                elif step is SetupStep.PROVIDER:
                    ProviderProbePublicConfig(
                        host=raw_fields["provider_host"],
                        api_url=raw_fields["provider_api_url"],
                        provider=raw_fields["provider"],
                    )
            for kind, value in secrets.items():
                raw = bytearray(value.encode("utf-8", errors="strict"))
                if len(raw) > 2560 or any(
                    unicodedata.category(character)
                    in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                    for character in value
                ):
                    raise ValueError
                encoded[kind] = raw
            if transaction.runtime is not None:
                draft.runtime = self._validated_codex_runtime(
                    transaction.runtime,
                    previous=draft.runtime,
                    fragments=runtime_fields,
                )
            if transaction.workflow is not None:
                draft.workflow = transaction.workflow.model_copy(deep=True)
            if transaction.runtime_fields is not None:
                runtime_fields.update(dict(transaction.runtime_fields))
                if step is SetupStep.ONES and draft.runtime is not None:
                    runtime_data = draft.runtime.model_dump(
                        mode="python", round_trip=True
                    )
                    runtime_data.update(
                        {
                            "ones_base_url": runtime_fields["ones_base_url"],
                            "ones_team_id": runtime_fields["ones_team_id"],
                            "ones_issue_type_id": runtime_fields[
                                "ones_issue_type_id"
                            ],
                        }
                    )
                    draft.runtime = RuntimePublicConfig.model_validate(runtime_data)
                elif step is SetupStep.PROVIDER and draft.runtime is not None:
                    runtime_data = draft.runtime.model_dump(mode="python", round_trip=True)
                    runtime_data.update({name: runtime_fields[name] for name in (
                        "provider_host", "provider_api_url", "git_author_name", "git_author_email")})
                    draft.runtime = RuntimePublicConfig.model_validate(runtime_data)
            if draft.runtime is None and all(
                name in runtime_fields
                for name in (
                    "ones_base_url",
                    "ones_team_id",
                    "ones_issue_type_id",
                    "provider_host",
                    "provider_api_url",
                    "git_author_name",
                    "git_author_email",
                )
            ):
                # Codex is deliberately not a setup surface. The runtime uses
                # the local CLI's ambient auth/configuration instead.
                draft.runtime = RuntimePublicConfig(
                    ones_base_url=runtime_fields["ones_base_url"],
                    ones_team_id=runtime_fields["ones_team_id"],
                    ones_issue_type_id=runtime_fields["ones_issue_type_id"],
                    ones_comment_list_path_template=DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE,
                    provider_host=runtime_fields["provider_host"],
                    provider_api_url=runtime_fields["provider_api_url"],
                    git_author_name=runtime_fields["git_author_name"],
                    git_author_email=runtime_fields["git_author_email"],
                    codex_auth_mode="file",
                    codex_home=None,
                )
            workflow = draft.workflow
            if transaction.repository is not None:
                repository = transaction.repository.model_copy(deep=True)
                repositories = tuple(
                    repository if item.key == repository.key else item
                    for item in workflow.repositories
                )
                if not any(item.key == repository.key for item in workflow.repositories):
                    repositories = (*repositories, repository)
                workflow.repositories = repositories
            if transaction.repository_group is not None:
                group = transaction.repository_group.model_copy(deep=True)
                member_keys = {item.key for item in group.repositories}
                workflow.repositories = tuple(
                    item for item in workflow.repositories if item.key not in member_keys
                )
                groups = tuple(
                    group if item.key == group.key else item
                    for item in workflow.repository_groups
                )
                if not any(item.key == group.key for item in workflow.repository_groups):
                    groups = (*groups, group)
                workflow.repository_groups = groups
            # Validate the complete draft shape before any owned state changes.
            WorkflowDraft.model_validate(workflow.model_dump(round_trip=True))
        except BaseException as error:
            for raw in encoded.values():
                self._zero(raw)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("step configuration is invalid") from None
        self._draft = draft
        self._runtime_fields = runtime_fields
        for kind, raw in encoded.items():
            old = self._secrets.get(kind)
            if old is not None:
                self._zero(old)
            self._secrets[kind] = raw
        affected = step
        if transaction.runtime is not None:
            if previous_draft.runtime is not None:
                detected = self._runtime_invalidation_step(
                    previous_draft.runtime, draft.runtime, SetupStep.REVIEW
                )
                if detected is not SetupStep.REVIEW:
                    affected = detected
        if transaction.workflow is not None:
            detected = self._workflow_invalidation_step(
                previous_draft.workflow, draft.workflow, SetupStep.REVIEW
            )
            if detected is not SetupStep.REVIEW:
                affected = detected
        self._changed(affected)

    @staticmethod
    def _validated_codex_runtime(
        candidate: RuntimePublicConfig,
        *,
        previous: RuntimePublicConfig | None,
        fragments: Mapping[str, str],
    ) -> RuntimePublicConfig:
        """Bind Codex-only edits to public prefixes validated by earlier steps."""

        prefix_names = (
            "ones_base_url",
            "ones_team_id",
            "ones_issue_type_id",
            "provider_host",
            "provider_api_url",
            "git_author_name",
            "git_author_email",
        )
        if previous is None and any(name not in fragments for name in prefix_names):
            raise ValueError
        prefix = {
            name: (
                fragments[name]
                if name in fragments
                else getattr(previous, name)
            )
            for name in prefix_names
        }
        expected = RuntimePublicConfig(
            **prefix,
            ones_comment_list_path_template=(
                previous.ones_comment_list_path_template
                if previous is not None
                else DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE
            ),
            codex_auth_mode=candidate.codex_auth_mode,
            codex_home=candidate.codex_home,
            coding_agent=candidate.coding_agent,
        )
        if candidate != expected:
            raise ValueError
        return expected

    @property
    def current_step(self) -> SetupStep:
        for step in self.STEPS[:-1]:
            result = self._results.get(step)
            if result is None or result.status is not ValidationStatus.PASSED:
                return step
        return SetupStep.REVIEW

    @property
    def state(self) -> SetupControllerState:
        workflow = self._draft.workflow
        return SetupControllerState(
            current_step=self.current_step,
            results=tuple(self.result_for(step) for step in self.STEPS),
            repository_count=len(workflow.repositories),
            repository_group_count=len(workflow.repository_groups),
            secret_count=len(self._secrets),
            review_confirmed=self._review_confirmed,
            closed=self._closed,
            error_category=self._activation_error,
        )

    @property
    def draft(self) -> SetupDraft:
        """Return a detached non-secret draft for form population."""

        return self._draft.model_copy(deep=True)

    @property
    def recovery_state(self) -> SetupRecoveryState:
        """Inspect pointer state without reading credentials or building a runtime."""

        try:
            document = self._store.load_or_empty(profile_id=self._profile_id)
            orphans = self._store.orphan_generations() if document.active else ()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return SetupRecoveryState(
                owner_generation=None,
                previous_available=False,
                orphan_count=0,
                error_category="recovery_unavailable",
            )
        owner = document.activation_owner_generation
        return SetupRecoveryState(
            owner_generation=owner,
            previous_available=document.previous is not None,
            orphan_count=len(orphans),
            error_category=("activation_recovery_required" if owner else None),
        )

    def load_active_public_draft(self) -> None:
        """Populate a reconfiguration draft from public active data only."""

        self._ensure_mutable()
        failed = False
        try:
            document = self._store.load_or_empty(profile_id=self._profile_id)
            if document.activation_owner_generation is not None or document.active is None:
                raise SetupStoreError("active configuration is unavailable")
            active = document.active
            workflow = WorkflowDraft.model_validate(
                active.workflow.model_dump(mode="python", round_trip=True)
            )
            self._draft = SetupDraft(
                runtime=active.runtime.model_copy(deep=True), workflow=workflow
            )
            self._runtime_fields = {
                "ones_base_url": active.runtime.ones_base_url,
                "ones_team_id": active.runtime.ones_team_id,
                "ones_issue_type_id": active.runtime.ones_issue_type_id,
                "provider_host": active.runtime.provider_host,
                "provider_api_url": active.runtime.provider_api_url,
                "git_author_name": active.runtime.git_author_name,
                "git_author_email": active.runtime.git_author_email,
                "provider": active.workflow.publishing.provider.value,
                "codex_auth_mode": active.runtime.codex_auth_mode,
                "codex_home": str(active.runtime.codex_home or ""),
                "coding_agent": active.runtime.coding_agent,
            }
            self._results.clear()
            self._review_confirmed = False
            self._activation_error = None
            self._revision += 1
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            failed = True
        if failed:
            raise SetupActionError("active configuration is unavailable") from None

    async def prepare_inline_ones(
        self, fields: Mapping[str, str], replacements: Mapping[SecretKind, str],
    ) -> None:
        """Validate an ONES-only edit while retaining unrelated saved secrets."""
        if set(fields) != {"ones_base_url", "ones_team_id", "ones_issue_type_id"}:
            raise SetupActionError("ONES configuration is invalid")
        if not set(replacements) <= {SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD}:
            raise SetupActionError("ONES credentials are invalid")
        await asyncio.to_thread(self.load_active_public_draft)
        document = await asyncio.to_thread(self._store.load_or_empty, profile_id=self._profile_id)
        if document.active is None or document.activation_owner_generation is not None:
            raise SetupActionError("active configuration is unavailable")
        if (fields["ones_base_url"].rstrip("/") != document.active.runtime.ones_base_url.rstrip("/")
                and not all(replacements.get(kind) for kind in (SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD))):
            raise SetupActionError("a new ONES endpoint requires explicit credentials")
        # Never send retained credentials back to the UI or a public document.
        retained = await asyncio.to_thread(self._store.read_active_secrets, document)
        try:
            for kind, value in retained.values.items():
                self.set_secret(kind, value)
            for kind, value in replacements.items():
                if value:
                    self.set_secret(kind, value)
            self.apply_runtime_fields(SetupStep.ONES, fields)
            for step in self.STEPS:
                if step is SetupStep.REVIEW:
                    break
                result = await self.test_step(step, self._inline_probe(step))
                if result.status is not ValidationStatus.PASSED:
                    raise InlineValidationError(step, result.category)
            self.confirm_review()
        except BaseException:
            self._clear_transient_secrets()
            raise

    async def prepare_inline_provider(
        self, fields: Mapping[str, str], replacements: Mapping[SecretKind, str],
    ) -> None:
        """Edit the provider without exposing retained secrets to the form."""
        if set(fields) != {"provider_host", "provider_api_url", "provider", "git_author_name", "git_author_email"}:
            raise SetupActionError("provider configuration is invalid")
        if not set(replacements) <= {SecretKind.PROVIDER_TOKEN}:
            raise SetupActionError("provider credentials are invalid")
        await asyncio.to_thread(self.load_active_public_draft)
        document = await asyncio.to_thread(self._store.load_or_empty, profile_id=self._profile_id)
        if document.active is None or document.activation_owner_generation is not None:
            raise SetupActionError("active configuration is unavailable")
        active = document.active.runtime
        if ((fields["provider_host"] != active.provider_host or fields["provider_api_url"].rstrip("/") != active.provider_api_url.rstrip("/"))
                and not replacements.get(SecretKind.PROVIDER_TOKEN)):
            raise SetupActionError("a new provider endpoint requires explicit credentials")
        retained = await asyncio.to_thread(self._store.read_active_secrets, document)
        try:
            for kind, value in retained.values.items():
                self.set_secret(kind, value)
            for kind, value in replacements.items():
                if value:
                    self.set_secret(kind, value)
            self.apply_step_transaction(SetupStep.PROVIDER, SetupStepTransaction(runtime_fields=dict(fields)), expected_revision=self.revision)
            workflow = self.draft.workflow.model_copy(deep=True)
            publishing = workflow.publishing.model_dump(mode="python")
            publishing["provider"] = fields["provider"]
            workflow.publishing = type(workflow.publishing).model_validate(publishing)
            self.apply_workflow(workflow, changed_step=SetupStep.PROVIDER)
            for step in self.STEPS:
                if step is SetupStep.REVIEW:
                    break
                result = await self.test_step(step, self._inline_probe(step))
                if result.status is not ValidationStatus.PASSED:
                    raise InlineValidationError(step, result.category)
            self.confirm_review()
        except BaseException:
            self._clear_transient_secrets()
            raise

    async def prepare_inline_coding_agent(self, coding_agent: str) -> None:
        """Select a supported local coding backend without exposing secrets."""
        if coding_agent not in {"codex", "claude"}:
            raise SetupActionError("coding agent configuration is invalid")
        await asyncio.to_thread(self.load_active_public_draft)
        document = await asyncio.to_thread(
            self._store.load_or_empty, profile_id=self._profile_id
        )
        if document.active is None or document.activation_owner_generation is not None:
            raise SetupActionError("active configuration is unavailable")
        retained = await asyncio.to_thread(self._store.read_active_secrets, document)
        try:
            for kind, value in retained.values.items():
                self.set_secret(kind, value)
            current = self.draft.runtime
            if current is None:
                raise SetupActionError("active configuration is unavailable")
            updated = RuntimePublicConfig.model_validate(
                current.model_copy(update={"coding_agent": coding_agent}).model_dump(
                    mode="python", round_trip=True
                )
            )
            self.apply_runtime(updated, changed_step=SetupStep.PRIVATE_PATHS)
            self._runtime_fields["coding_agent"] = coding_agent
            for step in self.STEPS:
                if step is SetupStep.REVIEW:
                    break
                result = await self.test_step(step, self._inline_probe(step))
                if result.status is not ValidationStatus.PASSED:
                    raise InlineValidationError(step, result.category)
            self.confirm_review()
        except BaseException:
            self._clear_transient_secrets()
            raise

    def _inline_probe(self, step: SetupStep) -> object | None:
        """Build the same typed connection inputs as the setup wizard."""
        if step is SetupStep.ONES:
            return OnesProbeInput(team_id=self._runtime_fields["ones_team_id"],
                                  issue_type_id=self._runtime_fields["ones_issue_type_id"] or None)
        if step is SetupStep.PROVIDER:
            return ProviderProbeInput(host=self._runtime_fields["provider_host"],
                                      api_url=self._runtime_fields["provider_api_url"])
        return None

    async def list_orphan_generations(self) -> tuple[str, ...]:
        self._ensure_open()
        failed = False
        result: tuple[str, ...] = ()
        async with self._operation_lock:
            self._ensure_open()
            self._operation_task = asyncio.current_task()
            try:
                result = await self._await_store_call(
                    self._store.orphan_generations
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
            finally:
                self._operation_task = None
        if failed:
            raise SetupActionError("credential cleanup unavailable") from None
        return result

    async def restore_pending(self, expected_generation: str) -> None:
        await self._resolve_pending(expected_generation)

    async def discard_pending(self, expected_generation: str) -> None:
        await self._resolve_pending(expected_generation)

    async def _resolve_pending(self, expected_generation: str) -> None:
        self._ensure_open()
        failed = False
        async with self._operation_lock:
            self._ensure_open()
            self._operation_task = asyncio.current_task()
            try:
                await self._await_store_call(
                    self._store.restore_previous,
                    self._profile_id,
                    expected_generation,
                )
                self._activation_error = None
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                self._activation_error = "activation recovery failed"
                failed = True
            finally:
                self._operation_task = None
        if failed:
            raise SetupActionError("activation recovery failed") from None

    async def cleanup_orphans(self, generations: tuple[str, ...]) -> None:
        self._ensure_open()
        failed = False
        async with self._operation_lock:
            self._ensure_open()
            self._operation_task = asyncio.current_task()
            try:
                current = await self._await_store_call(
                    self._store.orphan_generations
                )
                if generations != current:
                    raise SetupStoreError("credential cleanup refused")
                await self._await_store_call(
                    self._store.cleanup_orphan_generations, generations
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
            finally:
                self._operation_task = None
        if failed:
            raise SetupActionError("credential cleanup failed") from None

    async def _await_store_call(self, call: Callable[..., Any], *args: object) -> Any:
        """Retain operation ownership until a non-cancellable store call finishes."""

        task = asyncio.create_task(asyncio.to_thread(call, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._harvest_task(task)
            raise

    def result_for(self, step: SetupStep) -> ConnectionTestResult:
        self._require_step(step)
        return self._results.get(step, _fixed_result(step))

    def apply_runtime(
        self,
        runtime: RuntimePublicConfig,
        *,
        changed_step: SetupStep = SetupStep.ONES,
    ) -> None:
        self._ensure_mutable()
        if type(runtime) is not RuntimePublicConfig:
            raise SetupActionError("public configuration is invalid")
        invalidation_step = self._runtime_invalidation_step(
            self._draft.runtime, runtime, changed_step
        )
        self._draft.runtime = runtime.model_copy(deep=True)
        self._changed(invalidation_step)

    def apply_runtime_fields(
        self, step: SetupStep, fields: Mapping[str, str]
    ) -> None:
        """Retain strict public fragments needed to build fresh setup transports."""

        self._ensure_mutable()
        allowed = {
            SetupStep.ONES: frozenset(
                {"ones_base_url", "ones_team_id", "ones_issue_type_id"}
            ),
            SetupStep.PROVIDER: frozenset(
                {
                    "provider_host",
                    "provider_api_url",
                    "git_author_name",
                    "git_author_email",
                    "provider",
                }
            ),
            SetupStep.CODEX: frozenset({"codex_auth_mode", "codex_home"}),
        }.get(step)
        if allowed is None or set(fields) != set(allowed) or any(
            type(value) is not str for value in fields.values()
        ):
            raise SetupActionError("public configuration fields are invalid")
        changed = any(self._runtime_fields.get(key) != fields[key] for key in allowed)
        self._runtime_fields.update({key: fields[key] for key in allowed})
        runtime = self._draft.runtime
        if runtime is not None:
            updates: dict[str, object] = {
                key: value for key, value in fields.items() if key != "provider"
            }
            if "codex_home" in updates:
                raw_home = updates["codex_home"]
                updates["codex_home"] = Path(raw_home) if raw_home else None
            try:
                updated = runtime.model_copy(update=updates, deep=True)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise SetupActionError("public configuration fields are invalid") from None
            self.apply_runtime(updated, changed_step=step)
            return
        if changed:
            self._changed(step)

    def apply_workflow(
        self,
        workflow: WorkflowDraft,
        *,
        changed_step: SetupStep = SetupStep.REPOSITORIES,
    ) -> None:
        self._ensure_mutable()
        if type(workflow) is not WorkflowDraft:
            raise SetupActionError("workflow configuration is invalid")
        invalidation_step = self._workflow_invalidation_step(
            self._draft.workflow, workflow, changed_step
        )
        self._draft.workflow = workflow.model_copy(deep=True)
        self._changed(invalidation_step)

    def add_repository(self, **fields: object) -> RepositoryMapping:
        """Build a strict repository draft through the setup repository boundary."""

        self._ensure_mutable()
        try:
            repository = build_repository(**fields)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("repository configuration is invalid") from None
        workflow = self._draft.workflow.model_copy(deep=True)
        workflow.repositories = (*workflow.repositories, repository)
        self.apply_workflow(workflow, changed_step=SetupStep.REPOSITORIES)
        return repository.model_copy(deep=True)

    def upsert_repository(self, **fields: object) -> RepositoryMapping:
        """Validate first, then atomically insert or replace one mapping by key."""

        self._ensure_mutable()
        try:
            repository = build_repository(**fields)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("repository configuration is invalid") from None
        workflow = self._draft.workflow.model_copy(deep=True)
        existing = tuple(workflow.repositories)
        replacement = tuple(
            repository if item.key == repository.key else item for item in existing
        )
        if not any(item.key == repository.key for item in existing):
            replacement = (*existing, repository)
        workflow.repositories = replacement
        self.apply_workflow(workflow, changed_step=SetupStep.REPOSITORIES)
        return repository.model_copy(deep=True)

    def add_repository_group(
        self,
        builder: RepositoryGroupDraftBuilder,
        *,
        primary: str,
    ) -> RepositoryGroupMapping:
        self._ensure_mutable()
        if not isinstance(builder, RepositoryGroupDraftBuilder):
            raise SetupActionError("repository group is invalid")
        try:
            group = builder.build(primary=primary)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("repository group is invalid") from None
        workflow = self._draft.workflow.model_copy(deep=True)
        workflow.repository_groups = (*workflow.repository_groups, group)
        self.apply_workflow(workflow, changed_step=SetupStep.REPOSITORIES)
        return group.model_copy(deep=True)

    def set_secret(self, kind: SecretKind, value: str) -> None:
        self._ensure_mutable()
        if type(kind) is not SecretKind or type(value) is not str or not value:
            raise SetupActionError("credential is invalid")
        try:
            encoded = bytearray(value.encode("utf-8", errors="strict"))
        except UnicodeError:
            raise SetupActionError("credential is invalid") from None
        if len(encoded) > 2560 or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        ):
            self._zero(encoded)
            raise SetupActionError("credential is invalid")
        old = self._secrets.get(kind)
        if old is not None:
            self._zero(old)
        self._secrets[kind] = encoded
        self._changed(_SECRET_STEP[kind])

    def import_secrets(
        self,
        *,
        detection: ImportDetection,
        environment: Mapping[str, str],
        dotenv_values: Mapping[str, str],
        selected: tuple[SecretKind, ...],
        source_choice: Mapping[SecretKind, str] | None = None,
    ) -> None:
        """Import only an explicitly selected subset of previously detected kinds."""

        self._ensure_mutable()
        detected = set(detection.environment) | set(detection.dotenv)
        if any(kind not in detected for kind in selected):
            raise SetupActionError("credential selection is invalid")
        try:
            imported = import_selected(
                environment,
                dotenv_values,
                selected,
                source_choice=source_choice,  # type: ignore[arg-type]
            )
            for kind, value in imported.values.items():
                self.set_secret(kind, value)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("credential import failed") from None

    def secret_presence(self, kind: SecretKind) -> bool:
        if type(kind) is not SecretKind:
            return False
        value = self._secrets.get(kind)
        return value is not None and bool(value)

    def cancel_edit(self) -> None:
        """Cancel an active probe and forget every transient credential."""

        if self._closed:
            self._clear_transient_secrets()
            return
        self._revision += 1
        self._review_confirmed = False
        task = self._operation_task
        current = asyncio.current_task() if self._has_running_loop() else None
        if task is not None and task is not current and not task.done():
            task.cancel()
        for step, result in tuple(self._results.items()):
            if result.status is ValidationStatus.PENDING:
                self._results.pop(step, None)
                self._invalidate_after(step, include_step=False)
        self._clear_transient_secrets()

    def confirm_review(self) -> None:
        self._ensure_open()
        if not self._all_required_passed():
            raise SetupActionError("configuration is incomplete")
        self._review_confirmed = True
        self._results[SetupStep.REVIEW] = _fixed_result(
            SetupStep.REVIEW,
            status=ValidationStatus.PASSED,
            category="ok",
        )

    async def test_step(
        self, step: SetupStep, probe: object | None = None
    ) -> ConnectionTestResult:
        self._ensure_open()
        self._require_step(step)
        if step is SetupStep.REVIEW:
            raise SetupActionError("review requires explicit confirmation")
        if not self._step_available(step):
            raise SetupActionError("step is unavailable")
        async with self._operation_lock:
            self._ensure_open()
            if not self._step_available(step):
                raise SetupActionError("step is unavailable")
            task = asyncio.current_task()
            self._operation_task = task
            revision = self._revision
            self._results[step] = _fixed_result(
                step, status=ValidationStatus.PENDING, category="ok"
            )
            try:
                result = await self._probe(step, probe)
                if revision != self._revision:
                    self._results.pop(step, None)
                    raise SetupActionError("configuration changed")
                if result.step is not step:
                    result = _fixed_result(
                        step,
                        status=ValidationStatus.FAILED,
                        category="incompatible",
                    )
                self._results[step] = result
                if result.status is not ValidationStatus.PASSED:
                    self._invalidate_after(step, include_step=False)
                return result
            except asyncio.CancelledError:
                self._results.pop(step, None)
                raise
            except SetupActionError:
                if (
                    self._results.get(step, _fixed_result(step)).status
                    is ValidationStatus.PENDING
                ):
                    self._results[step] = _fixed_result(
                        step,
                        status=ValidationStatus.FAILED,
                        category="incompatible",
                    )
                    self._invalidate_after(step, include_step=False)
                raise
            except BaseException as error:
                if _is_setup_priority_failure(error):
                    raise
                result = _fixed_result(
                    step,
                    status=ValidationStatus.FAILED,
                    category="incompatible",
                )
                self._results[step] = result
                self._invalidate_after(step, include_step=False)
                return result
            finally:
                if self._operation_task is task:
                    self._operation_task = None

    async def activate_existing(self) -> object | None:
        """Load without mutating the active pointer; failures enter safe recovery."""

        if self._closed or self._closing:
            return None
        async with self._operation_lock:
            if self._closed or self._closing:
                self._activation_error = "setup is closed"
                return None
            self._operation_task = asyncio.current_task()
            try:
                document = await asyncio.to_thread(
                    self._store.load_or_empty, profile_id=self._profile_id
                )
                if document.activation_owner_generation is not None:
                    self._activation_error = "activation recovery required"
                    return None
                if document.active is None:
                    if self._activation_error == "activation recovery required":
                        self._activation_error = None
                    return None
                secrets = await asyncio.to_thread(
                    self._store.read_active_secrets, document
                )
                handle = await self._bounded_build(document.active, secrets)
                self._activation_error = None
                return handle
            except asyncio.CancelledError:
                self._clear_transient_secrets()
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    self._clear_transient_secrets()
                    raise
                self._activation_error = "runtime validation failed"
                return None
            finally:
                self._operation_task = None

    async def save_and_activate(self, *, confirmed: bool = False) -> object:
        """Commit, reload, build, and finalize exactly one candidate generation."""

        self._ensure_open()
        async with self._operation_lock:
            self._ensure_open()
            self._operation_task = asyncio.current_task()
            if not self._all_required_passed():
                self._operation_task = None
                raise SetupActionError("configuration is incomplete")
            if confirmed is True:
                self.confirm_review()
            if not self._review_confirmed:
                self._operation_task = None
                raise SetupActionError("review confirmation is required")
            committed = False
            finalized = False
            handle: object | None = None
            failure_message: str | None = None
            candidate: ActiveSetup | None = None
            profile_id = self._profile_id
            try:
                candidate, secrets = self._build_candidate()
                revision = self._revision
                commit_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._store.commit, profile_id, candidate, secrets
                    )
                )
                try:
                    document = await asyncio.shield(commit_task)
                except asyncio.CancelledError:
                    succeeded, harvested, _ = await self._harvest_task(commit_task)
                    if succeeded:
                        document = harvested
                        committed = True
                    elif candidate is not None:
                        detection_task = asyncio.create_task(
                            self._candidate_is_active(candidate.generation)
                        )
                        detected, active, _ = await self._harvest_task(
                            detection_task
                        )
                        committed = bool(detected and active)
                    raise
                committed = True
                self._require_revision(revision)
                if document.active is None:
                    raise SetupStoreError("active configuration is unavailable")
                persisted = await asyncio.to_thread(
                    self._store.read_active_secrets, document
                )
                self._require_revision(revision)
                handle = await self._bounded_build(document.active, persisted)
                self._require_revision(revision)
                # Once finalization starts, synchronous form mutations are rejected.
                # This closes the only gap between the last revision check and the
                # irreversible cleanup of the previous generation.
                self._finalizing = True
                self._require_revision(revision)
                finalize_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._store.finalize_activation,
                        profile_id,
                        candidate.generation,
                    )
                )
                try:
                    await asyncio.shield(finalize_task)
                    finalized = True
                except asyncio.CancelledError:
                    finalized, _, _ = await self._harvest_task(finalize_task)
                    raise
                finally:
                    self._finalizing = False
                self._activation_error = None
                self._clear_transient_secrets()
                self._closed = True
                return handle
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    self._cleanup_save(
                        handle,
                        profile_id=profile_id,
                        expected_generation=(
                            candidate.generation if candidate is not None else None
                        ),
                        rollback=committed and not finalized,
                    )
                )
                await self._harvest_task(cleanup_task)
                raise
            except BaseException as error:
                control_flow = isinstance(error, (KeyboardInterrupt, SystemExit))
                cleanup_cancelled = False
                if not committed and candidate is not None:
                    detection_task = asyncio.create_task(
                        self._candidate_is_active(candidate.generation)
                    )
                    detected, active, interrupted = await self._harvest_task(
                        detection_task
                    )
                    cleanup_cancelled = cleanup_cancelled or interrupted
                    committed = bool(detected and active)
                cleanup_task = asyncio.create_task(
                    self._cleanup_save(
                        handle,
                        profile_id=profile_id,
                        expected_generation=(
                            candidate.generation if candidate is not None else None
                        ),
                        rollback=committed and not finalized,
                    )
                )
                _, _, interrupted = await self._harvest_task(cleanup_task)
                cleanup_cancelled = cleanup_cancelled or interrupted
                if isinstance(error, _ConfigurationChanged):
                    self._activation_error = "configuration changed"
                else:
                    self._activation_error = (
                        "runtime validation failed"
                        if committed
                        else "configuration save failed"
                    )
                self._review_confirmed = False
                self._results[SetupStep.REVIEW] = _fixed_result(
                    SetupStep.REVIEW,
                    status=ValidationStatus.FAILED,
                    category="incompatible",
                )
                if control_flow:
                    raise
                if cleanup_cancelled:
                    failure_message = None
                else:
                    failure_message = self._activation_error
            finally:
                self._finalizing = False
                self._operation_task = None
            if failure_message is not None:
                # Raise outside the raw exception handler so neither __cause__ nor
                # __context__ retains a backend exception that could carry a secret.
                raise SetupActionError(failure_message) from None
            raise asyncio.CancelledError from None

    def close(self) -> None:
        if self._closed:
            self._clear_transient_secrets()
            return
        self._closing = True
        task = self._operation_task
        catalog_owner = self._catalog_owner_task
        if catalog_owner is not None and not catalog_owner.done():
            catalog_owner.cancel()
        if task is None or task.done():
            self._closed = True
            self._clear_transient_secrets()
        else:
            def finish_close(_done: asyncio.Task[Any]) -> None:
                self._closed = True
                self._clear_transient_secrets()

            task.add_done_callback(finish_close)

    async def aclose(self) -> None:
        """Close setup and drain owned background work up to the cleanup deadline."""

        self.close()
        async with self._operation_lock:
            self._closed = True
            self._clear_transient_secrets()
        tasks: set[asyncio.Task[Any]] = {
            *self._background_close_tasks,
            *self._abandoned_build_tasks,
            *self._catalog_tasks,
        }
        if not tasks:
            return
        await asyncio.wait(tasks, timeout=self._cleanup_timeout)

    async def _probe(
        self, step: SetupStep, probe: object | None
    ) -> ConnectionTestResult:
        if step is SetupStep.PROFILE:
            workflow = self._draft.workflow
            selected = workflow.sandbox_permission_profile
            source = workflow.sandbox_permission_profile_source
            if (
                type(selected) is not str
                or type(source) is not SandboxPermissionProfileSource
                or (
                    source is SandboxPermissionProfileSource.BUILTIN_WORKSPACE
                    and selected != BUILTIN_WORKSPACE_PROFILE
                )
                or (
                    source is SandboxPermissionProfileSource.MANAGED
                    and selected == BUILTIN_WORKSPACE_PROFILE
                )
            ):
                raise ValueError
            await asyncio.to_thread(
                self._profile_catalog.require_selected,
                selected,
                source,
            )
            return _fixed_result(
                step, status=ValidationStatus.PASSED, category="ok"
            )
        probe = self._normalize_ui_probe(step, probe)
        method_name = {
            SetupStep.ONES: "probe_ones",
            SetupStep.REPOSITORIES: "probe_repository",
            SetupStep.PROVIDER: "probe_provider",
            SetupStep.CODEX: "probe_codex",
            SetupStep.PRIVATE_PATHS: "probe_private_paths",
        }[step]
        method = getattr(self._validator, method_name)
        temporary: object | None = None
        original: object | None = None
        attribute: str | None = None
        try:
            if step is SetupStep.ONES and getattr(
                self._validator, "ones_gateway", None
            ) is None:
                factory = getattr(
                    self._runtime_builder, "build_ones_probe_gateway", None
                )
                if callable(factory):
                    temporary = factory(
                        MappingProxyType(dict(self._runtime_fields)),
                        self._probe_secrets(
                            SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD
                        ),
                    )
                    attribute = "ones_gateway"
            elif step is SetupStep.PROVIDER and getattr(
                self._validator, "provider_transport", None
            ) is None:
                factory = getattr(
                    self._runtime_builder, "build_provider_probe_transport", None
                )
                if callable(factory):
                    temporary = factory(
                        MappingProxyType(dict(self._runtime_fields)),
                        self._probe_secrets(SecretKind.PROVIDER_TOKEN),
                    )
                    attribute = "provider_transport"
            elif step is SetupStep.CODEX:
                factory = getattr(
                    self._runtime_builder, "build_codex_probe_auth_checker", None
                )
                runtime = self._draft.runtime
                if callable(factory) and runtime is not None:
                    codex_kinds = tuple(
                        kind
                        for kind in (
                            SecretKind.CODEX_API_KEY,
                            SecretKind.CODEX_AUTH_TOKEN,
                        )
                        if kind in self._secrets
                    )
                    temporary = factory(
                        runtime,
                        self._probe_secrets(*codex_kinds),
                    )
                    attribute = "codex_auth_metadata"
            if attribute is not None:
                original = getattr(self._validator, attribute, None)
                setattr(self._validator, attribute, temporary)
            if step is SetupStep.REPOSITORIES and isinstance(probe, (tuple, list)):
                if not probe:
                    return _fixed_result(
                        step,
                        status=ValidationStatus.FAILED,
                        category="invalid_field",
                    )
                for item in probe:
                    result = await method(item)
                    if result.status is not ValidationStatus.PASSED:
                        return result
                return _fixed_result(step, status=ValidationStatus.PASSED, category="ok")
            return await method(probe)
        finally:
            if attribute is not None:
                setattr(self._validator, attribute, original)
            if temporary is not None:
                close = getattr(temporary, "aclose", None)
                if not callable(close):
                    close = getattr(temporary, "close", None)
                if callable(close):
                    outcome = close()
                    if asyncio.iscoroutine(outcome):
                        await outcome

    def _probe_secrets(self, *kinds: SecretKind) -> RuntimeSecrets:
        values: dict[SecretKind, str] = {}
        try:
            for kind in kinds:
                raw = self._secrets.get(kind)
                if raw is None:
                    raise ValueError
                values[kind] = bytes(raw).decode("utf-8", errors="strict")
            return RuntimeSecrets(values)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("credential is unavailable") from None

    def _normalize_ui_probe(
        self, step: SetupStep, probe: object | None
    ) -> object | None:
        """Convert the TUI's immutable transient snapshot through strict models."""

        if not isinstance(probe, Mapping):
            return probe
        values = probe
        try:
            if step is SetupStep.ONES:
                return OnesProbeInput(
                    team_id=values["ones-team-id"],
                    project_id=values.get("ones-project-id") or None,
                    status_id=values.get("ones-status-id") or None,
                    item_id=values.get("ones-item-id") or None,
                    issue_type_id=values.get("ones-issue-type-id") or None,
                )
            if step is SetupStep.PROVIDER:
                return ProviderProbeInput(
                    host=values["provider-host"],
                    api_url=values["provider-api-url"],
                )
            if step is SetupStep.REPOSITORIES:
                return RepositoryProbeInput(
                    path=Path(values["repository-path"]),
                    remote_url=values["repository-url"],
                )
            if step is SetupStep.CODEX:
                profile = self._draft.workflow.sandbox_permission_profile
                source = self._draft.workflow.sandbox_permission_profile_source
                supplied = values["codex-profile"]
                if (
                    type(profile) is not str
                    or type(source) is not SandboxPermissionProfileSource
                    or type(supplied) is not str
                    or supplied != profile
                ):
                    raise ValueError
                return CodexProbeInput(
                    profile=profile,
                    profile_source=source,
                    worktree=Path(values["codex-worktree"]),
                )
            if step is SetupStep.PRIVATE_PATHS:
                return PrivatePathsProbeInput(
                    paths=(
                        Path(values["run-root"]),
                        Path(values["mirror-root"]),
                        Path(values["worktree-root"]),
                    )
                )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("connection test fields are invalid") from None
        return probe

    def _build_candidate(self) -> tuple[ActiveSetup, RuntimeSecrets]:
        try:
            runtime = self._draft.runtime
            if runtime is None or not self._secrets:
                raise ValueError
            raw_workflow = self._draft.workflow.model_dump(
                mode="python", round_trip=True
            )
            workflow = DeveloperWorkflowConfig.model_validate(raw_workflow)
            values = {
                kind: bytes(raw).decode("utf-8", errors="strict")
                for kind, raw in self._secrets.items()
            }
            secrets = RuntimeSecrets(MappingProxyType(values))
            kinds = tuple(sorted(values, key=lambda kind: kind.value))
            candidate = ActiveSetup(
                generation=uuid4().hex,
                runtime=runtime,
                workflow=workflow,
                credential_kinds=kinds,
            )
            return candidate, secrets
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("configuration is incomplete") from None

    async def _bounded_build(
        self, active: ActiveSetup, secrets: RuntimeSecrets
    ) -> object:
        loop = asyncio.get_running_loop()
        attempt = _BuildAttempt(
            self._cleanup_timeout,
            lambda handle: self._handoff_abandoned_handle(loop, handle),
        )
        task = asyncio.create_task(
            asyncio.to_thread(
                attempt.run,
                self._runtime_builder,
                active,
                secrets,
            )
        )
        try:
            handle = await asyncio.wait_for(
                asyncio.shield(task), timeout=self._activation_timeout
            )
            attempt.deliver()
            return handle
        except (asyncio.TimeoutError, asyncio.CancelledError):
            completed = attempt.abandon()
            if completed is not None:
                self._schedule_background_close(completed)
            self._abandoned_build_tasks.add(task)
            task.add_done_callback(
                lambda finished: self._finish_abandoned_build(finished, attempt)
            )
            raise

    async def _harvest_task(
        self, task: asyncio.Task[Any]
    ) -> tuple[bool, Any | None, bool]:
        """Wait for one owned task to become terminal despite repeated cancellation."""

        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
                continue
            except BaseException:
                break
        try:
            return True, task.result(), interrupted
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return False, None, interrupted

    async def _cleanup_save(
        self,
        handle: object | None,
        *,
        profile_id: str,
        expected_generation: str | None,
        rollback: bool,
    ) -> None:
        """Run all failure cleanup in one child task protected from parent cancellation."""

        try:
            if rollback and expected_generation is not None:
                await self._rollback(profile_id, expected_generation)
        finally:
            self._clear_transient_secrets()
        if handle is not None:
            close_task = self._schedule_background_close(handle)
            done, _ = await asyncio.wait(
                {close_task}, timeout=self._cleanup_timeout
            )
            if close_task in done:
                self._consume_close_task(close_task)

    def _schedule_background_close(self, handle: object) -> asyncio.Future[None]:
        """Schedule exactly one observed close without running it on the event loop."""

        return self._schedule_close_once(_CloseOnce(handle))

    def _schedule_close_once(self, closer: _CloseOnce) -> asyncio.Future[None]:
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[None] = loop.create_future()
        self._background_close_tasks.add(completion)
        completion.add_done_callback(self._consume_close_task)

        def finish_on_loop() -> None:
            if not completion.done():
                completion.set_result(None)

        def worker() -> None:
            try:
                closer.run()
            except BaseException:
                pass
            finally:
                self._background_close_tasks.discard(completion)
                try:
                    loop.call_soon_threadsafe(finish_on_loop)
                except RuntimeError:
                    pass

        Thread(
            target=worker,
            name="ones-setup-runtime-close",
            daemon=True,
        ).start()
        return completion

    def _handoff_abandoned_handle(
        self, loop: asyncio.AbstractEventLoop, handle: object
    ) -> None:
        """Give a late handle to a live loop once, otherwise close off-loop once."""

        closer = _CloseOnce(handle)

        def accept() -> None:
            try:
                self._schedule_close_once(closer)
            except BaseException:
                pass

        try:
            loop.call_soon_threadsafe(accept)
        except RuntimeError:
            pass
        if closer.started.wait(self._cleanup_timeout):
            return
        fallback_thread = Thread(
            target=closer.run,
            name="ones-setup-abandoned-close",
            daemon=True,
        )
        fallback_thread.start()
        fallback_thread.join(self._cleanup_timeout)

    def _consume_close_task(self, task: asyncio.Future[None]) -> None:
        """Consume a detached close outcome and release its lifecycle reference."""

        self._background_close_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException:
            pass

    def _finish_abandoned_build(
        self, task: asyncio.Task[object], attempt: _BuildAttempt
    ) -> None:
        self._abandoned_build_tasks.discard(task)
        if task.cancelled():
            return
        try:
            handle = task.result()
        except BaseException:
            return
        if attempt.claim_late():
            self._schedule_background_close(handle)

    async def _rollback(self, profile_id: str, expected_generation: str) -> None:
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    self._store.restore_previous,
                    profile_id,
                    expected_generation,
                )
            )
        except BaseException:
            # Cleanup is best-effort and must not replace the primary control-flow
            # exception; the store itself preserves whichever pointer is durable.
            pass

    async def _candidate_is_active(self, generation: str) -> bool:
        try:
            document = await asyncio.to_thread(
                self._store.load_or_empty, profile_id=self._profile_id
            )
            return (
                document.active is not None
                and document.active.generation == generation
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return False

    def _require_revision(self, expected: int) -> None:
        if self._revision != expected:
            raise _ConfigurationChanged("configuration changed")

    def _runtime_invalidation_step(
        self,
        previous: RuntimePublicConfig | None,
        current: RuntimePublicConfig,
        requested: SetupStep,
    ) -> SetupStep:
        self._require_step(requested)
        if previous is None:
            # The wizard validates earlier capability probes from immutable field
            # snapshots before the complete cross-step public model exists.
            return requested
        detected: list[SetupStep] = []
        field_steps = {
            SetupStep.ONES: (
                "ones_base_url",
                "ones_team_id",
                "ones_issue_type_id",
                "ones_comment_list_path_template",
            ),
            SetupStep.PROVIDER: (
                "provider_host",
                "provider_api_url",
                "git_author_name",
                "git_author_email",
            ),
        }
        for step, fields in field_steps.items():
            if any(
                getattr(previous, field_name) != getattr(current, field_name)
                for field_name in fields
            ):
                detected.append(step)
        return self._earliest_step(requested, *detected)

    def _workflow_invalidation_step(
        self,
        previous: WorkflowDraft,
        current: WorkflowDraft,
        requested: SetupStep,
    ) -> SetupStep:
        self._require_step(requested)
        field_steps = {
            "sandbox_permission_profile": SetupStep.PROFILE,
            "repositories": SetupStep.REPOSITORIES,
            "repository_groups": SetupStep.REPOSITORIES,
            "publishing": SetupStep.PROVIDER,
            "max_codex_attempts": SetupStep.PRIVATE_PATHS,
            "tui_max_concurrency": SetupStep.PRIVATE_PATHS,
            "run_root": SetupStep.PRIVATE_PATHS,
            "mirror_root": SetupStep.PRIVATE_PATHS,
            "worktree_root": SetupStep.PRIVATE_PATHS,
        }
        detected = tuple(
            step
            for field_name, step in field_steps.items()
            if getattr(previous, field_name) != getattr(current, field_name)
        )
        return self._earliest_step(requested, *detected)

    def _earliest_step(
        self, requested: SetupStep, *detected: SetupStep
    ) -> SetupStep:
        return min(
            (requested, *detected),
            key=self.STEPS.index,
        )

    def _changed(self, step: SetupStep) -> None:
        self._require_step(step)
        self._revision += 1
        self._review_confirmed = False
        self._activation_error = None
        self._invalidate_after(step, include_step=True)

    def _invalidate_after(self, step: SetupStep, *, include_step: bool) -> None:
        start = self.STEPS.index(step) + (0 if include_step else 1)
        for candidate in self.STEPS[start:]:
            self._results.pop(candidate, None)

    def _step_available(self, step: SetupStep) -> bool:
        index = self.STEPS.index(step)
        if index == 0:
            return True
        return all(
            self._results.get(previous) is not None
            and self._results[previous].status is ValidationStatus.PASSED
            for previous in self.STEPS[:index]
        ) or (
            self._results.get(step) is not None
            and all(
                self._results.get(previous) is not None
                and self._results[previous].status is ValidationStatus.PASSED
                for previous in self.STEPS[:index]
            )
        )

    def _all_required_passed(self) -> bool:
        return all(
            self._results.get(step) is not None
            and self._results[step].status is ValidationStatus.PASSED
            for step in self.STEPS[:-1]
        )

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise SetupActionError("setup is closed")

    def _ensure_mutable(self) -> None:
        self._ensure_open()
        if self._finalizing:
            raise SetupActionError("setup operation is in progress")

    def _require_step(self, step: SetupStep) -> None:
        if type(step) is not SetupStep or step not in self.STEPS:
            raise SetupActionError("step is invalid")

    def _clear_transient_secrets(self) -> None:
        for raw in self._secrets.values():
            self._zero(raw)
        self._secrets.clear()

    @staticmethod
    def _zero(raw: bytearray) -> None:
        for index in range(len(raw)):
            raw[index] = 0

    @staticmethod
    def _has_running_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True


__all__ = [
    "RuntimeBuilder",
    "SetupActionError",
    "SetupController",
    "SetupControllerState",
    "SetupStepTransaction",
]
