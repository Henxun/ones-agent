"""Provider-owned runtime policies for configured coding agents."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .claude_runner import ClaudeRunner, safe_claude_environment
from .codex_runner import CodexRunner, validate_codex_auth_source
from .codex_runtime import CodexRuntimePreparer
from .coding_agent_runner import CodingAgentRunner
from .coding_agents import (
    CodingAgentProbeResult,
    probe_coding_agent,
    validate_coding_agent_provider_keys,
)
from .setup_models import RuntimePublicConfig, RuntimeSecrets, SecretKind


_CODEX_BASE_ENV = frozenset(
    {
        "COMSPEC", "LANG", "LC_ALL", "NO_COLOR", "PATH", "PATHEXT",
        "SYSTEMROOT", "TEMP", "TERM", "TMP", "TMPDIR", "WINDIR",
        "SSL_CERT_DIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "OPENAI_API_VERSION",
        "OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_ORG_ID", "OPENAI_PROJECT",
    }
)


class CodingAgentRuntimeAdapter(Protocol):
    """Provider policy used after a provider has been explicitly selected."""

    key: str

    def validate_configuration(
        self, public: RuntimePublicConfig, credential_kinds: frozenset[SecretKind]
    ) -> None: ...

    def build_environment(
        self,
        public: RuntimePublicConfig,
        secrets: RuntimeSecrets,
        ambient: Mapping[str, str],
    ) -> dict[str, str]: ...

    def validate_environment(self, environment: Mapping[str, str]) -> None: ...

    def probe_readiness(
        self,
        executable: Path,
        *,
        codex_preparer: CodexRuntimePreparer,
        codex_command_resolver: Callable[..., object],
        run: Callable[..., object] | None = None,
    ) -> CodingAgentProbeResult: ...

    def build_runner(
        self,
        run_root: Path,
        repository: object,
        environment_provider: Callable[[], dict[str, str]],
        *,
        codex_preparer: CodexRuntimePreparer,
        codex_command_resolver: Callable[..., object],
        legacy_codex_factory: Callable[..., object] | None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _CodexRuntimeAdapter:
    key: str = "codex"

    def validate_configuration(
        self, public: RuntimePublicConfig, credential_kinds: frozenset[SecretKind]
    ) -> None:
        codex_kinds = {
            SecretKind.CODEX_API_KEY,
            SecretKind.CODEX_AUTH_TOKEN,
        } & credential_kinds
        if public.codex_auth_mode == "credential":
            if len(codex_kinds) != 1 or public.codex_home is not None:
                raise ValueError
        elif public.codex_auth_mode != "file" or codex_kinds:
            raise ValueError

    def build_environment(
        self,
        public: RuntimePublicConfig,
        secrets: RuntimeSecrets,
        ambient: Mapping[str, str],
    ) -> dict[str, str]:
        environment = {
            key: value
            for key, value in ambient.items()
            if type(key) is str
            and type(value) is str
            and key.upper() in _CODEX_BASE_ENV
        }
        if public.codex_auth_mode == "credential":
            api_key = secrets.values.get(SecretKind.CODEX_API_KEY, "")
            auth_token = secrets.values.get(SecretKind.CODEX_AUTH_TOKEN, "")
            if bool(api_key) == bool(auth_token):
                raise ValueError
            environment[
                "CODEX_API_KEY" if api_key else "CODEX_AUTH_TOKEN"
            ] = api_key or auth_token
        elif public.codex_home is not None:
            environment["CODEX_HOME"] = str(public.codex_home)
        else:
            discovered_home = validate_codex_auth_source(ambient)
            if discovered_home is None:
                raise ValueError
            environment["CODEX_HOME"] = str(discovered_home)
        return environment

    def validate_environment(self, environment: Mapping[str, str]) -> None:
        validate_codex_auth_source(environment)

    def probe_readiness(
        self,
        executable: Path,
        *,
        codex_preparer: CodexRuntimePreparer,
        codex_command_resolver: Callable[..., object],
        run: Callable[..., object] | None = None,
    ) -> CodingAgentProbeResult:
        options: dict[str, object] = {
            "codex_command_resolver": lambda: codex_command_resolver(
                _prepare=codex_preparer.prepare_verified
            )
        }
        if run is not None:
            options["run"] = run
        return probe_coding_agent(self.key, executable, **options)  # type: ignore[arg-type]

    def build_runner(
        self,
        run_root: Path,
        repository: object,
        environment_provider: Callable[[], dict[str, str]],
        *,
        codex_preparer: CodexRuntimePreparer,
        codex_command_resolver: Callable[..., object],
        legacy_codex_factory: Callable[..., object] | None,
    ) -> object:
        if legacy_codex_factory is not None:
            return legacy_codex_factory(run_root, repository, environment_provider)
        return CodexRunner(
            run_root,
            repository,
            command_resolver=lambda: codex_command_resolver(
                _prepare=codex_preparer.prepare_verified
            ),
            environment_provider=environment_provider,
        )


@dataclass(frozen=True, slots=True)
class _ClaudeRuntimeAdapter:
    key: str = "claude"

    def validate_configuration(
        self, public: RuntimePublicConfig, credential_kinds: frozenset[SecretKind]
    ) -> None:
        del public, credential_kinds

    def build_environment(
        self,
        public: RuntimePublicConfig,
        secrets: RuntimeSecrets,
        ambient: Mapping[str, str],
    ) -> dict[str, str]:
        del public, secrets
        return safe_claude_environment(ambient)

    def validate_environment(self, environment: Mapping[str, str]) -> None:
        del environment

    def probe_readiness(
        self,
        executable: Path,
        *,
        codex_preparer: CodexRuntimePreparer,
        codex_command_resolver: Callable[..., object],
        run: Callable[..., object] | None = None,
    ) -> CodingAgentProbeResult:
        del codex_preparer, codex_command_resolver
        if run is None:
            return probe_coding_agent(self.key, executable)
        return probe_coding_agent(self.key, executable, run=run)  # type: ignore[arg-type]

    def build_runner(
        self,
        run_root: Path,
        repository: object,
        environment_provider: Callable[[], dict[str, str]],
        *,
        codex_preparer: CodexRuntimePreparer,
        codex_command_resolver: Callable[..., object],
        legacy_codex_factory: Callable[..., object] | None,
    ) -> CodingAgentRunner:
        del codex_preparer, codex_command_resolver, legacy_codex_factory
        return ClaudeRunner(
            run_root,
            repository,
            environment_provider=environment_provider,
            sandbox_mode_override="danger-full-access",
        )


CODING_AGENT_RUNTIME_ADAPTERS: Mapping[str, CodingAgentRuntimeAdapter] = (
    MappingProxyType(
        {
            "codex": _CodexRuntimeAdapter(),
            "claude": _ClaudeRuntimeAdapter(),
        }
    )
)
validate_coding_agent_provider_keys(CODING_AGENT_RUNTIME_ADAPTERS)


def coding_agent_runtime_adapter(key: str) -> CodingAgentRuntimeAdapter:
    try:
        return CODING_AGENT_RUNTIME_ADAPTERS[key]
    except KeyError:
        raise ValueError("unsupported coding agent") from None


__all__ = [
    "CODING_AGENT_RUNTIME_ADAPTERS",
    "CodingAgentRuntimeAdapter",
    "coding_agent_runtime_adapter",
]
