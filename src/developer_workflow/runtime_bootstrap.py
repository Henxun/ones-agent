"""Explicit, fail-closed construction of the production workflow graph."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import Field

from config.settings import OnesSettings
from src.services.ones_gateway import OnesGateway

from .approval_rebuilder import WorkflowApprovalRebuilder
from .codex_runner import resolve_codex_command, validate_codex_auth_source
from .codex_runtime import CodexRuntimePreparer
from .coding_agent_runtime import coding_agent_runtime_adapter
from .coding_agents import (
    coding_agent_definition,
)
from .coding_agent_runner import CodingAgentRunner
from .config import (
    BUILTIN_WORKSPACE_PROFILE,
    DeveloperWorkflowConfig,
    SandboxPermissionProfileSource,
)
from .contracts import CodingAgentProvenance
from .defect_flow import DefectCandidateService, DefectFlow
from .ones_comment import OnesCommenter
from .orchestrator import DeveloperWorkflowOrchestrator
from .pr_provider import HttpPullRequestClient, parse_repository_identity
from .private_paths import prepare_private_roots
from .publisher import Publisher
from .repository import WorktreeRepository, validate_git_identity_environment
from .repository_group import RepositoryGroupWorkspace
from .requirement_flow import (
    CodingAgentRequirementAdapter,
    RequirementFlow,
    SandboxCommandExecutor,
    sandbox_preflight_command,
)
from .setup_models import (
    DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE,
    ActiveSetup,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
    OnesProbePublicConfig,
    ProviderProbePublicConfig,
)
from .state_store import FileRunStore


class RuntimeBootstrapError(RuntimeError):
    """Production runtime inputs could not be safely activated."""


@dataclass(slots=True, repr=False)
class _CodexProbeAuthChecker:
    environment: dict[str, str] = field(repr=False)
    _closed: bool = False

    def metadata(self) -> Mapping[str, object]:
        if self._closed:
            raise RuntimeError("codex auth probe is closed")
        home = validate_codex_auth_source(self.environment)
        credential = bool(
            self.environment.get("CODEX_API_KEY")
            or self.environment.get("CODEX_AUTH_TOKEN")
        )
        return {
            "configured": True,
            "mode": "credential" if credential and home is None else "file",
        }

    def close(self) -> None:
        for key in tuple(self.environment):
            self.environment[key] = ""
        self.environment.clear()
        self._closed = True


class _RuntimeOnesSettings(OnesSettings):
    email: str = Field(default="", repr=False)
    password: str = Field(default="", repr=False)


class PrivateRootPreparer(Protocol):
    def __call__(self, roots: Sequence[Path]) -> tuple[Path, ...]: ...


class SandboxProfileValidator(Protocol):
    def __call__(
        self,
        profile: str,
        source: SandboxPermissionProfileSource,
        environment: Mapping[str, str],
    ) -> None: ...


class SandboxFactory(Protocol):
    def __call__(
        self,
        profile: str,
        source: SandboxPermissionProfileSource,
    ) -> object: ...


class CodingAgentFactory(Protocol):
    """Build one provider-neutral runner for the selected catalog key."""

    def __call__(
        self,
        key: str,
        run_root: Path,
        repository: object,
        environment_provider: Callable[[], dict[str, str]],
    ) -> CodingAgentRunner: ...


@dataclass(frozen=True, slots=True)
class RuntimeAdapterBundle:
    """Explicit test/deployment adapters; ``None`` preserves production defaults."""

    gateway_factory: Callable[..., object] | None = None
    coding_agent_factory: CodingAgentFactory | None = None
    # Deprecated compatibility seam for Codex-only test/deployment adapters.
    codex_factory: Callable[..., object] | None = None
    repository_factory: Callable[..., object] | None = None
    sandbox_factory: SandboxFactory | None = None
    pr_factory: Callable[..., object] | None = None
    commenter_factory: Callable[..., object] | None = None


_GIT_SECRET_ENV = {
    SecretKind.GIT_ASKPASS: "GIT_ASKPASS",
    SecretKind.GIT_SSH: "GIT_SSH",
    SecretKind.GIT_SSH_COMMAND: "GIT_SSH_COMMAND",
    SecretKind.SSH_ASKPASS: "SSH_ASKPASS",
    SecretKind.SSH_AUTH_SOCK: "SSH_AUTH_SOCK",
}
_PREFLIGHT_ENV = frozenset(
    {
        "COMSPEC", "LANG", "LC_ALL", "NO_COLOR", "PATH", "PATHEXT",
        "SYSTEMROOT", "TEMP", "TERM", "TMP", "TMPDIR", "WINDIR",
        "SSL_CERT_DIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    }
)


def _validate_runtime_secret(value: str) -> str:
    if type(value) is not str or not value or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        raise ValueError
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError from None
    if len(encoded) > 2560:
        raise ValueError
    return value


def _preflight_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in source.items():
        normalized = key.upper()
        if normalized not in _PREFLIGHT_ENV or type(value) is not str:
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            continue
        environment[key] = value
    return environment


def _default_sandbox_validator(
    profile: str,
    source: SandboxPermissionProfileSource,
    environment: Mapping[str, str],
    *,
    codex_preparer: CodexRuntimePreparer | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="ones-dev-sandbox-preflight-") as raw:
        cwd = Path(raw).resolve(strict=True)
        completed = SandboxCommandExecutor(
            permission_profile=profile,
            permission_profile_source=source,
            codex_preparer=codex_preparer,
        )(
            sandbox_preflight_command(),
            cwd=cwd,
            env=dict(environment),
            timeout=20,
            max_output_bytes=64 * 1024,
        )
        if completed.returncode != 0:
            raise RuntimeError("sandbox profile is unavailable")


def _run_gateway_close(gateway: OnesGateway) -> None:
    async def close() -> None:
        await gateway.close()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(close())
        return
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(close())
        except BaseException as error:  # pragma: no cover - defensive bridge
            failure.append(error)

    thread = threading.Thread(target=worker, name="ones-gateway-close")
    thread.start()
    thread.join()
    if failure:
        raise RuntimeBootstrapError("production runtime close failed") from None


@dataclass(slots=True, repr=False)
class RuntimeHandle:
    orchestrator: DeveloperWorkflowOrchestrator | None
    gateway: OnesGateway | None
    close_callback: Callable[[], None] | None = field(repr=False)
    workflow_saver: Callable[[DeveloperWorkflowConfig], None] | None = field(
        default=None, repr=False
    )
    _condition: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False
    )
    _state: str = field(default="open", init=False, repr=False)
    _close_failed: bool = field(default=False, init=False, repr=False)

    def __repr__(self) -> str:
        return "RuntimeHandle(<redacted>)"

    @property
    def close_complete(self) -> bool:
        with self._condition:
            return self._state == "closed"

    def close(self) -> None:
        with self._condition:
            while self._state == "closing":
                self._condition.wait()
            if self._state == "closed":
                if self._close_failed:
                    raise RuntimeBootstrapError(
                        "production runtime close failed"
                    ) from None
                return
            self._state = "closing"
        failed = False
        fatal: BaseException | None = None
        try:
            callback = self.close_callback
            if callback is not None:
                callback()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                fatal = error
            else:
                failed = True
        finally:
            with self._condition:
                if fatal is not None:
                    self._state = "open"
                else:
                    self._close_failed = failed
                    self._state = "closed"
                    self.close_callback = None
                    self.workflow_saver = None
                    self.orchestrator = None
                    self.gateway = None
                self._condition.notify_all()
        if fatal is not None:
            raise fatal
        if failed:
            raise RuntimeBootstrapError("production runtime close failed") from None


@dataclass(slots=True)
class RuntimeBootstrapper:
    private_root_preparer: PrivateRootPreparer = prepare_private_roots
    sandbox_profile_validator: SandboxProfileValidator = _default_sandbox_validator
    gateway_close: Callable[[OnesGateway], None] = _run_gateway_close
    ambient_environment: Callable[[], Mapping[str, str]] = lambda: os.environ
    adapters: RuntimeAdapterBundle = field(default_factory=RuntimeAdapterBundle)
    codex_runtime_preparer: CodexRuntimePreparer = field(
        default_factory=CodexRuntimePreparer,
        repr=False,
    )
    workflow_saver: (
        Callable[[ActiveSetup, DeveloperWorkflowConfig], None] | None
    ) = field(default=None, repr=False)
    use_local_git_config: bool = False

    @staticmethod
    def _profile_provenance(
        active: ActiveSetup,
    ) -> tuple[str, SandboxPermissionProfileSource]:
        workflow = active.workflow
        profile = workflow.sandbox_permission_profile
        source = workflow.sandbox_permission_profile_source
        if (
            type(profile) is not str
            or type(source) is not SandboxPermissionProfileSource
        ):
            raise ValueError
        if source is SandboxPermissionProfileSource.BUILTIN_WORKSPACE:
            if profile != BUILTIN_WORKSPACE_PROFILE:
                raise ValueError
        elif source is SandboxPermissionProfileSource.MANAGED:
            if profile == BUILTIN_WORKSPACE_PROFILE:
                raise ValueError
        else:  # pragma: no cover - exact enum type makes this defensive only
            raise ValueError
        return profile, source

    def _validate_sandbox_capability(
        self,
        profile: str,
        source: SandboxPermissionProfileSource,
        environment: Mapping[str, str],
    ) -> None:
        if self.sandbox_profile_validator is _default_sandbox_validator:
            _default_sandbox_validator(
                profile,
                source,
                environment,
                codex_preparer=self.codex_runtime_preparer,
            )
            return
        self.sandbox_profile_validator(profile, source, environment)

    def build_ones_probe_gateway(
        self, public: Mapping[str, str], secrets: RuntimeSecrets
    ) -> OnesGateway:
        """Build a fresh read-only gateway from the current setup edit."""

        checked = OnesProbePublicConfig(
            base_url=public["ones_base_url"],
            team_id=public["ones_team_id"],
            issue_type_id=public["ones_issue_type_id"],
        )
        email = secrets.require(SecretKind.ONES_EMAIL)
        password = secrets.require(SecretKind.ONES_PASSWORD)
        settings = _RuntimeOnesSettings.model_validate(
            {
                "base_url": checked.base_url,
                "email": email,
                "password": password,
                "team_id": checked.team_id,
                "project_id": "",
                "issue_type_id": checked.issue_type_id,
                "api_token": "",
                "defect_status_ids": "",
                "comment_list_path_template": DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE,
            }
        )
        return OnesGateway(settings=settings)

    def build_provider_probe_transport(
        self, public: Mapping[str, str], secrets: RuntimeSecrets
    ) -> httpx.AsyncClient:
        """Build a fresh authenticated GET-only client for one provider probe."""

        checked = ProviderProbePublicConfig(
            host=public["provider_host"],
            api_url=public["provider_api_url"],
            provider=public["provider"],
        )
        policy = HttpPullRequestClient(
            provider=checked.provider,
            provider_host=checked.host,
            api_base_url=checked.api_url,
            token_provider=lambda: secrets.require(SecretKind.PROVIDER_TOKEN),
            client=object(),  # type: ignore[arg-type]
        )
        return httpx.AsyncClient(
            base_url=checked.api_url,
            headers=policy._headers(),
            timeout=10.0,
        )

    def build_codex_probe_auth_checker(
        self, public: RuntimePublicConfig, secrets: RuntimeSecrets
    ) -> _CodexProbeAuthChecker:
        """Freeze explicit setup auth for one Codex validation probe."""

        if type(public) is not RuntimePublicConfig or type(secrets) is not RuntimeSecrets:
            raise RuntimeBootstrapError("codex probe configuration is invalid")
        try:
            environment: dict[str, str] = {}
            if public.codex_auth_mode == "credential":
                api_key = secrets.values.get(SecretKind.CODEX_API_KEY, "")
                token = secrets.values.get(SecretKind.CODEX_AUTH_TOKEN, "")
                if bool(api_key) == bool(token):
                    raise ValueError
                environment["CODEX_API_KEY" if api_key else "CODEX_AUTH_TOKEN"] = (
                    api_key or token
                )
            elif public.codex_auth_mode == "file" and public.codex_home is not None:
                environment["CODEX_HOME"] = str(public.codex_home)
            else:
                raise ValueError
            validate_codex_auth_source(environment)
            return _CodexProbeAuthChecker(environment)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise RuntimeBootstrapError("codex probe configuration is invalid") from None

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> RuntimeHandle:
        gateway: OnesGateway | None = None
        pr_client: HttpPullRequestClient | None = None
        try:
            if type(active) is not ActiveSetup or type(secrets) is not RuntimeSecrets:
                raise ValueError
            persisted_profile, persisted_source = self._profile_provenance(active)
            if set(secrets.values) != set(active.credential_kinds):
                raise ValueError
            public = active.runtime
            # ActiveSetup deliberately freezes a private committed model for
            # storage.  Re-validate it through the public runtime contract so
            # Pydantic's type-sensitive equality cannot reject an otherwise
            # identical mapping returned by an adapter.
            workflow = DeveloperWorkflowConfig.model_validate(
                active.workflow.model_dump(mode="python", round_trip=True)
            )
            if (
                type(workflow.sandbox_permission_profile) is not str
                or type(workflow.sandbox_permission_profile_source)
                is not SandboxPermissionProfileSource
                or workflow.sandbox_permission_profile != persisted_profile
                or workflow.sandbox_permission_profile_source is not persisted_source
            ):
                raise ValueError
            agent_runtime = coding_agent_runtime_adapter(public.coding_agent)
            agent_runtime.validate_configuration(
                public, frozenset(active.credential_kinds)
            )
            validated_secrets = {
                kind: _validate_runtime_secret(value)
                for kind, value in secrets.values.items()
            }
            settings = _RuntimeOnesSettings.model_validate(
                {
                    "base_url": public.ones_base_url,
                    "email": validated_secrets[SecretKind.ONES_EMAIL],
                    "password": validated_secrets[SecretKind.ONES_PASSWORD],
                    "team_id": public.ones_team_id,
                    "project_id": "",
                    "issue_type_id": public.ones_issue_type_id,
                    "api_token": "",
                    "defect_status_ids": "",
                    "comment_list_path_template": public.ones_comment_list_path_template,
                    "comment_timeout_seconds": 30.0,
                    "comment_max_pages": 50,
                    "comment_max_comments": 10_000,
                    "comment_max_payload_bytes": 10 * 1024 * 1024,
                }
            )
            provider_token = validated_secrets.get(SecretKind.PROVIDER_TOKEN, "")
            agent_environment = self._coding_agent_environment(public, secrets)
            git_credentials = {
                name: value
                for kind, name in _GIT_SECRET_ENV.items()
                if (value := secrets.values.get(kind, ""))
            }
            identity_values = {
                "GIT_AUTHOR_NAME": public.git_author_name,
                "GIT_AUTHOR_EMAIL": public.git_author_email,
                "GIT_COMMITTER_NAME": public.git_author_name,
                "GIT_COMMITTER_EMAIL": public.git_author_email,
            }
            validate_git_identity_environment(identity_values)
            agent_runtime.validate_environment(agent_environment)
            publishing_enabled = bool(provider_token) and self.adapters.pr_factory is None
            for mapping in (
                *workflow.repositories,
                *(repo for group in workflow.repository_groups for repo in group.repositories),
            ):
                # The bootstrap placeholder is not an executable ONES mapping.
                if mapping.key == "workspace" and mapping.project_id == "pending-project" and mapping.iteration_id == "*":
                    continue
                if Path(mapping.repo_url).is_absolute():
                    if provider_token:
                        raise ValueError
                else:
                    parse_repository_identity(
                        mapping.repo_url,
                        public.provider_host if publishing_enabled else None,
                    )
            self._validate_sandbox_capability(
                persisted_profile,
                persisted_source,
                _preflight_environment(agent_environment),
            )
            run_root, mirror_root, worktree_root = self.private_root_preparer(
                (workflow.run_root, workflow.mirror_root, workflow.worktree_root)
            )

            store = FileRunStore(run_root)
            credential_provider = lambda: dict(git_credentials)
            identity_provider = lambda: dict(identity_values)
            if self.adapters.repository_factory is None:
                repository = WorktreeRepository(
                    mirror_root,
                    worktree_root,
                    credential_env_provider=credential_provider,
                    identity_env_provider=identity_provider,
                    use_local_git_config=self.use_local_git_config,
                )
            else:
                repository = self.adapters.repository_factory(
                    mirror_root,
                    worktree_root,
                    credential_provider,
                    identity_provider,
                )
            gateway = (
                OnesGateway(settings=settings)
                if self.adapters.gateway_factory is None
                else self.adapters.gateway_factory(settings)
            )
            environment_provider = lambda: dict(agent_environment)
            if self.adapters.coding_agent_factory is not None:
                coding_agent_backend = self.adapters.coding_agent_factory(
                    public.coding_agent,
                    run_root,
                    repository,
                    environment_provider,
                )
            else:
                coding_agent_backend = agent_runtime.build_runner(
                    run_root,
                    repository,
                    environment_provider,
                    codex_preparer=self.codex_runtime_preparer,
                    codex_command_resolver=resolve_codex_command,
                    legacy_codex_factory=self.adapters.codex_factory,
                )
            if self.adapters.coding_agent_factory is not None and not isinstance(
                coding_agent_backend, CodingAgentRunner
            ):
                raise ValueError("coding agent factory returned an incompatible runner")
            requirement_agent = (
                CodingAgentRequirementAdapter(coding_agent_backend)
                if isinstance(coding_agent_backend, CodingAgentRunner)
                else coding_agent_backend
            )
            test_runner = (
                SandboxCommandExecutor(
                    permission_profile=persisted_profile,
                    permission_profile_source=persisted_source,
                    codex_preparer=self.codex_runtime_preparer,
                )
                if self.adapters.sandbox_factory is None
                else self.adapters.sandbox_factory(
                    persisted_profile,
                    persisted_source,
                )
            )
            group_workspace = RepositoryGroupWorkspace(repository)
            requirement_flow = RequirementFlow(
                store, gateway, workflow, repository, requirement_agent, test_runner,
                group_workspace=group_workspace,
            )
            defect_flow = DefectFlow(
                store, workflow, repository, requirement_agent, test_runner,
                group_workspace=group_workspace,
            )
            agent_definition = coding_agent_definition(public.coding_agent)
            agent_provenance = CodingAgentProvenance(
                key=agent_definition.key,
                label=agent_definition.label,
            )
            candidates = DefectCandidateService(
                gateway,
                settings.issue_type_id,
                coding_agent=agent_provenance,
            )
            pr_arguments = {
                "provider": workflow.publishing.provider.value,
                "provider_host": public.provider_host,
                "api_base_url": public.provider_api_url,
                "token_provider": lambda: provider_token,
            }
            pr_client = (
                HttpPullRequestClient(**pr_arguments)
                if self.adapters.pr_factory is None
                else self.adapters.pr_factory(**pr_arguments)
            )
            commenter = (
                OnesCommenter(gateway, store)
                if self.adapters.commenter_factory is None
                else self.adapters.commenter_factory(gateway, store)
            )
            publisher = Publisher(
                store,
                repository,
                WorkflowApprovalRebuilder(gateway, repository),
                pr_client,
                commenter,
                workflow.publishing.provider.value,
                public.provider_host,
            )
            orchestrator = DeveloperWorkflowOrchestrator(
                store,
                requirement_flow,
                defect_flow,
                publisher,
                workflow,
                candidates,
                coding_agent=agent_provenance,
            )

            def close() -> None:
                assert pr_client is not None and gateway is not None
                try:
                    client = getattr(pr_client, "client", pr_client)
                    close_client = getattr(client, "close", None)
                    if callable(close_client):
                        close_client()
                finally:
                    self.gateway_close(gateway)

            saver = self.workflow_saver
            save_workflow = (
                None if saver is None else lambda candidate: saver(active, candidate)
            )
            return RuntimeHandle(orchestrator, gateway, close, save_workflow)
        except BaseException as error:
            if pr_client is not None:
                try:
                    client = getattr(pr_client, "client", pr_client)
                    close_client = getattr(client, "close", None)
                    if callable(close_client):
                        close_client()
                except BaseException:
                    pass
            if gateway is not None:
                try:
                    self.gateway_close(gateway)
                except BaseException:
                    pass
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            if isinstance(error, RuntimeBootstrapError):
                raise
            raise RuntimeBootstrapError(
                "production runtime configuration is incomplete"
            ) from None

    def _coding_agent_environment(
        self, public: RuntimePublicConfig, secrets: RuntimeSecrets
    ) -> dict[str, str]:
        return coding_agent_runtime_adapter(public.coding_agent).build_environment(
            public, secrets, self.ambient_environment()
        )


__all__ = [
    "RuntimeAdapterBundle",
    "RuntimeBootstrapError",
    "RuntimeBootstrapper",
    "RuntimeHandle",
]


def legacy_runtime_inputs(
    workflow: DeveloperWorkflowConfig,
    environment: Mapping[str, str],
    *,
    ones_settings: OnesSettings | None = None,
) -> tuple[ActiveSetup, RuntimeSecrets]:
    """Translate the established non-interactive environment contract once."""

    try:
        settings = ones_settings or OnesSettings(_env_file=None, **{
            name: environment.get(env_name, default)
            for name, env_name, default in (
                ("base_url", "ONES_BASE_URL", "http://aputureones.com:8088"),
                ("email", "ONES_EMAIL", ""),
                ("password", "ONES_PASSWORD", ""),
                ("team_id", "ONES_TEAM_ID", ""),
                ("issue_type_id", "ONES_ISSUE_TYPE_ID", ""),
                ("comment_list_path_template", "ONES_COMMENT_LIST_PATH_TEMPLATE", None),
            )
        })
        if environment.get("ONES_API_TOKEN", ""):
            raise ValueError
        codex_home = validate_codex_auth_source(environment)
        codex_secret_kind: SecretKind | None = None
        if environment.get("CODEX_API_KEY", ""):
            codex_secret_kind = SecretKind.CODEX_API_KEY
        elif environment.get("CODEX_AUTH_TOKEN", ""):
            codex_secret_kind = SecretKind.CODEX_AUTH_TOKEN
        elif environment.get("OPENAI_API_KEY", ""):
            codex_secret_kind = SecretKind.CODEX_API_KEY
        public = RuntimePublicConfig(
            ones_base_url=settings.base_url,
            ones_team_id=settings.team_id,
            ones_issue_type_id=settings.issue_type_id,
            ones_comment_list_path_template=settings.comment_list_path_template,
            provider_host=environment.get("ONES_DEV_PROVIDER_HOST", "").casefold(),
            provider_api_url=environment.get("ONES_DEV_PROVIDER_API_URL", ""),
            git_author_name=environment.get("ONES_DEV_GIT_AUTHOR_NAME", ""),
            git_author_email=environment.get("ONES_DEV_GIT_AUTHOR_EMAIL", ""),
            codex_auth_mode="credential" if codex_secret_kind is not None else "file",
            codex_home=codex_home,
        )
        values: dict[SecretKind, str] = {
            SecretKind.ONES_EMAIL: settings.email,
            SecretKind.ONES_PASSWORD: settings.password,
            SecretKind.PROVIDER_TOKEN: environment.get("ONES_DEV_PROVIDER_TOKEN", ""),
        }
        if codex_secret_kind is not None:
            values[codex_secret_kind] = (
                environment.get("OPENAI_API_KEY", "")
                if codex_secret_kind is SecretKind.CODEX_API_KEY
                and not environment.get("CODEX_API_KEY", "")
                else environment.get(
                    "CODEX_API_KEY"
                    if codex_secret_kind is SecretKind.CODEX_API_KEY
                    else "CODEX_AUTH_TOKEN",
                    "",
                )
            )
        for kind, name in _GIT_SECRET_ENV.items():
            value = environment.get(f"ONES_DEV_{name}", "")
            if value:
                values[kind] = value
        secrets = RuntimeSecrets(values)
        active = ActiveSetup(
            generation="0" * 32,
            runtime=public,
            workflow=workflow,
            credential_kinds=tuple(values),
        )
        return active, secrets
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise RuntimeBootstrapError(
            "production runtime configuration is incomplete"
        ) from None


__all__.append("legacy_runtime_inputs")
