"""Fail-closed discovery and read-only bootstrap capability checks.

This module deliberately returns only a small, fixed result vocabulary.  Raw
transport, process, credential and filesystem errors never cross its boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import inspect
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import tomllib
from typing import Any, Callable, Literal, Mapping, Protocol
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from .codex_runner import (
    CodexCommand,
    CodexProcessStartError,
    _bounded_subprocess,
    resolve_codex_command,
    validate_codex_auth_source,
)
from .codex_runtime import CodexRuntimePreparer
from .config import (
    BUILTIN_WORKSPACE_PROFILE,
    DeveloperWorkflowConfig,
    SandboxPermissionProfileSource,
)
from .contracts import WorkflowModel
from .private_paths import (
    _ADMINISTRATORS_SID,
    _SYSTEM_SID,
    _current_user_sid,
    _has_link_or_reparse_ancestor,
    _is_link_or_reparse,
    _validate_shape,
    _windows_descriptor,
    prepare_private_directory,
)
from .requirement_flow import SandboxCommandExecutor, sandbox_preflight_command
from .setup_models import SetupModel, SetupValidationError


PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_DOCUMENT_BYTES = 1024 * 1024
_BUILTIN_PARENTS = frozenset({":read-only", ":workspace"})
_DOCTOR_CONFIG_DETAIL_KEYS = frozenset(
    {
        "CODEX_HOME",
        "config.toml",
        "config.toml parse",
        "cwd",
        "enabled feature flags",
        "feature flag overrides",
        "feature flags enabled",
        "log dir",
        "mcp servers",
        "model",
        "model provider",
        "sqlite home",
    }
)
_PRODUCTION_CONSTRUCTION = object()
_TEST_CONSTRUCTION = object()
_LOCKED_SSH_COMMAND = (
    "ssh -F none -oBatchMode=yes -oIdentitiesOnly=yes -oProxyCommand=none "
    "-oStrictHostKeyChecking=yes -oUpdateHostKeys=no"
)
_CATEGORIES = Literal[
    "ok",
    "git_unavailable",
    "authentication",
    "unreachable",
    "tls",
    "timeout",
    "incompatible",
    "unsafe_path",
    "sandbox",
    "invalid_field",
]


class GitExecutableUnavailableError(RuntimeError):
    """The read-only repository probe cannot start the Git executable."""


def _is_probe_priority_failure(error: BaseException) -> bool:
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


def _select_doctor_failure(
    primary: BaseException | None, close_failure: BaseException | None
) -> BaseException | None:
    if primary is not None and _is_probe_priority_failure(primary):
        return primary
    if close_failure is not None and _is_probe_priority_failure(close_failure):
        return close_failure
    if primary is not None:
        return primary
    if close_failure is not None:
        return SetupValidationError("Codex doctor cleanup failed")
    return None


def _raise_sanitized_setup_failure(error: BaseException) -> None:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    raise error from None


def _raise_git_executable_unavailable() -> None:
    raise GitExecutableUnavailableError("Git executable is unavailable") from None


class SetupStep(str, Enum):
    PROFILE = "profile"
    ONES = "ones"
    REPOSITORIES = "repositories"
    PROVIDER = "provider"
    CODEX = "codex"
    PRIVATE_PATHS = "private_paths"
    REVIEW = "review"


class ValidationStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ConnectionTestResult(WorkflowModel):
    """Non-sensitive result safe for logs and TUI rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    step: SetupStep
    status: ValidationStatus
    category: _CATEGORIES


def _safe_identifier(value: str, field_name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


class OnesProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    team_id: StrictStr
    project_id: StrictStr | None = None
    status_id: StrictStr | None = None
    item_id: StrictStr | None = None
    issue_type_id: StrictStr | None = None

    @field_validator("team_id", "project_id", "status_id", "item_id", "issue_type_id")
    @classmethod
    def validate_ids(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _safe_identifier(value, info.field_name)


class ProviderProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    host: StrictStr
    api_url: StrictStr

    @model_validator(mode="after")
    def validate_binding(self) -> ProviderProbeInput:
        parsed = urlsplit(self.api_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != self.host.casefold()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("provider endpoint is invalid")
        return self


class RepositoryProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    path: Path
    remote_url: StrictStr

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("repository path must be absolute")
        return value

    @field_validator("remote_url")
    @classmethod
    def validate_remote(cls, value: str) -> str:
        if any(ord(character) < 0x20 for character in value) or "\\" in value:
            raise ValueError("repository remote is invalid")
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("repository remote is invalid") from error
        if (
            parsed.scheme not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or port is not None and not 1 <= port <= 65535
        ):
            raise ValueError("repository remote is invalid")
        return value


class CodexProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    profile: StrictStr
    profile_source: SandboxPermissionProfileSource = (
        SandboxPermissionProfileSource.MANAGED
    )
    worktree: Path

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if PROFILE_RE.fullmatch(value) is None:
            raise ValueError("profile is invalid")
        return value

    @model_validator(mode="after")
    def validate_profile_binding(self) -> CodexProbeInput:
        if type(self.profile_source) is not SandboxPermissionProfileSource:
            raise ValueError("profile source is invalid")
        DeveloperWorkflowConfig.validate_sandbox_permission_profile_binding(
            self.profile,
            self.profile_source,
        )
        return self

    @field_validator("worktree")
    @classmethod
    def validate_worktree(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("worktree must be absolute")
        return value


class PrivatePathsProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    paths: tuple[Path, ...] = Field(min_length=3, max_length=3)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(not path.is_absolute() for path in value):
            raise ValueError("private paths must be absolute")
        return value


class DoctorRunner(Protocol):
    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]: ...


class SandboxExecutorFactory(Protocol):
    def __call__(self, profile: str) -> Any: ...


class ProviderTransport(Protocol):
    def get(self, url: str, *, timeout: float) -> Any: ...


class GitReadOnlyRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout: float) -> Any: ...


class CodexAuthMetadata(Protocol):
    def metadata(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class SubprocessDoctorRunner:
    """Production doctor adapter with bounded process-tree cleanup."""

    codex_preparer: CodexRuntimePreparer | None = field(default=None, repr=False)
    command_provider: Callable[[], CodexCommand] | None = field(default=None, repr=False)
    backend_runner: Callable[..., subprocess.CompletedProcess[str]] = field(
        default_factory=lambda: _default_doctor_runner,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.codex_preparer is not None and self.command_provider is not None:
            raise SetupValidationError("Codex doctor command source is ambiguous")
        if self.command_provider is not None and not callable(self.command_provider):
            raise SetupValidationError("Codex doctor command provider is invalid")
        if self.codex_preparer is not None and not callable(
            getattr(self.codex_preparer, "prepare_verified", None)
        ):
            raise SetupValidationError("Codex doctor preparer is invalid")
        if not callable(self.backend_runner):
            raise SetupValidationError("Codex doctor process runner is invalid")
        if self.codex_preparer is None and self.command_provider is None:
            object.__setattr__(self, "codex_preparer", CodexRuntimePreparer())

    def _verified_command(self) -> CodexCommand:
        if self.command_provider is not None:
            command = self.command_provider()
            if type(command) is not CodexCommand:
                raise SetupValidationError("Codex doctor command is invalid")
            return command
        assert self.codex_preparer is not None
        return resolve_codex_command(_prepare=self.codex_preparer.prepare_verified)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv != ["doctor", "--json"] or kwargs.get("shell") is not False:
            raise SetupValidationError("Codex doctor command is invalid")
        command = self._verified_command()
        completed: subprocess.CompletedProcess[str] | None = None
        primary: BaseException | None = None
        try:
            completed = self.backend_runner(command.argv("doctor", "--json"), **kwargs)
        except BaseException as error:
            primary = error
        close_failure: BaseException | None = None
        try:
            command.close()
        except BaseException as error:
            close_failure = error
        selected = _select_doctor_failure(primary, close_failure)
        if selected is not None:
            failure = selected
            del argv, kwargs, command, completed, primary, close_failure, selected
            _raise_sanitized_setup_failure(failure)
        assert completed is not None
        return completed


@dataclass(slots=True)
class ManagedSandboxExecutorFactory:
    """Production-only construction of the existing capability-probing executor."""

    backend_executor: Callable[..., subprocess.CompletedProcess[str]] = field(
        default_factory=lambda: _bounded_subprocess, repr=False
    )
    codex_preparer: CodexRuntimePreparer | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.codex_preparer is None:
            self.codex_preparer = CodexRuntimePreparer()

    def __call__(self, profile: str) -> SandboxCommandExecutor:
        return SandboxCommandExecutor(
            permission_profile=profile,
            permission_profile_source=SandboxPermissionProfileSource.MANAGED,
            codex_preparer=self.codex_preparer,
            backend_executor=self.backend_executor,
        )


@dataclass(slots=True)
class BuiltinWorkspaceSandboxExecutorFactory:
    """Construct only the fixed runtime workspace profile sandbox."""

    backend_executor: Callable[..., subprocess.CompletedProcess[str]] = field(
        default_factory=lambda: _bounded_subprocess, repr=False
    )
    codex_preparer: CodexRuntimePreparer | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.codex_preparer is None:
            self.codex_preparer = CodexRuntimePreparer()

    def __call__(self, profile: str) -> SandboxCommandExecutor:
        if profile != BUILTIN_WORKSPACE_PROFILE:
            raise ValueError("sandbox permission profile source is invalid")
        return SandboxCommandExecutor(
            permission_profile=profile,
            permission_profile_source=SandboxPermissionProfileSource.BUILTIN_WORKSPACE,
            codex_preparer=self.codex_preparer,
            backend_executor=self.backend_executor,
        )


@dataclass(frozen=True, slots=True)
class CodexAuthSourceChecker:
    """Expose only auth presence metadata through the existing validated contract."""

    def metadata(self) -> Mapping[str, object]:
        environment = dict(os.environ)
        home = validate_codex_auth_source(environment)
        credential = any(
            bool(environment.get(name))
            for name in ("CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY")
        )
        return {"configured": True, "mode": "credential" if credential and home is None else "file"}


def _default_doctor_runner(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_output_bytes: int,
    shell: bool,
) -> subprocess.CompletedProcess[str]:
    if shell is not False:
        raise ValueError("doctor shell boundary is invalid")
    return _bounded_subprocess(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )


def _clean_doctor_environment() -> dict[str, str]:
    allowed = {"comspec", "path", "pathext", "systemroot", "windir", "lang", "lc_all"}
    return {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in allowed
        and not any(word in key.casefold() for word in ("token", "secret", "password", "key"))
    }


def _default_file_security(path: Path, admin: bool) -> bool:
    try:
        if not path.is_absolute() or _has_link_or_reparse_ancestor(path) or _is_link_or_reparse(path):
            return False
        metadata = path.stat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            return False
        if os.name != "nt":
            return metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
        owner, entries, protected = _windows_descriptor(path)
        user_sid = _current_user_sid()
        trusted = {_SYSTEM_SID, _ADMINISTRATORS_SID} if admin else {
            user_sid,
            _SYSTEM_SID,
            _ADMINISTRATORS_SID,
        }
        required_owners = {_SYSTEM_SID, _ADMINISTRATORS_SID} if admin else {user_sid}
        principals = {entry[0] for entry in entries}
        return (
            protected
            and owner in required_owners
            and bool(entries)
            and principals <= trusted
            and all(ace_type == 0 and flags & 0x10 == 0 for _, _, flags, ace_type in entries)
        )
    except (OSError, TypeError, ValueError):
        return False


def _read_secure_bytes(path: Path, *, admin: bool, security: Callable[[Path, bool], bool]) -> bytes:
    try:
        canonical = path.resolve(strict=True)
        if canonical != path.absolute() or not security(canonical, admin):
            raise SetupValidationError("managed profile source is unsafe")
        before = canonical.stat()
        if before.st_size > _MAX_DOCUMENT_BYTES:
            raise SetupValidationError("managed profile source is invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ):
                raise SetupValidationError("managed profile source is unsafe")
            data = os.read(descriptor, _MAX_DOCUMENT_BYTES + 1)
        finally:
            os.close(descriptor)
        after = canonical.stat()
        if len(data) > _MAX_DOCUMENT_BYTES or (after.st_dev, after.st_ino, after.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise SetupValidationError("managed profile source is unsafe")
        return data
    except SetupValidationError:
        raise
    except (OSError, TypeError, ValueError):
        raise SetupValidationError("managed profile source is unsafe") from None


def _is_string_map(value: object, allowed_values: frozenset[str]) -> bool:
    return type(value) is dict and all(
        type(key) is str and type(item) is str and item in allowed_values
        for key, item in value.items()
    )


def _is_string_list(value: object) -> bool:
    return type(value) is list and all(type(item) is str for item in value)


def _validate_mitm(value: object) -> None:
    if type(value) is not dict or not set(value) <= {"hooks", "actions"}:
        raise ValueError
    actions = value.get("actions", {})
    if type(actions) is not dict:
        raise ValueError
    for action in actions.values():
        if type(action) is not dict or not set(action) <= {
            "strip_request_headers", "inject_request_headers"
        }:
            raise ValueError
        strips = action.get("strip_request_headers", [])
        injections = action.get("inject_request_headers", [])
        if not _is_string_list(strips) or type(injections) is not list:
            raise ValueError
        for injection in injections:
            if type(injection) is not dict or not set(injection) <= {
                "name", "secret_env_var", "secret_file", "prefix"
            } or type(injection.get("name")) is not str or any(
                item is not None and type(item) is not str
                for key in ("secret_env_var", "secret_file", "prefix")
                if (item := injection.get(key)) is not None
            ):
                raise ValueError
    hooks = value.get("hooks", {})
    if type(hooks) is not dict:
        raise ValueError
    for hook in hooks.values():
        required = {"host", "methods", "path_prefixes", "action"}
        if type(hook) is not dict or not required <= set(hook) or not set(hook) <= {
            *required, "query", "headers", "body"
        } or type(hook["host"]) is not str or any(
            not _is_string_list(hook[key]) for key in ("methods", "path_prefixes", "action")
        ):
            raise ValueError
        for key in ("query", "headers"):
            mapping = hook.get(key, {})
            if type(mapping) is not dict or any(
                type(name) is not str or not _is_string_list(items)
                for name, items in mapping.items()
            ):
                raise ValueError


def _validate_network(value: object) -> None:
    allowed = {
        "enabled", "proxy_url", "enable_socks5", "socks_url", "enable_socks5_udp",
        "allow_upstream_proxy", "dangerously_allow_non_loopback_proxy",
        "dangerously_allow_all_unix_sockets", "mode", "domains", "unix_sockets",
        "allow_local_binding", "mitm",
    }
    if type(value) is not dict or not set(value) <= allowed:
        raise ValueError
    boolean_keys = {
        "enabled", "enable_socks5", "enable_socks5_udp", "allow_upstream_proxy",
        "dangerously_allow_non_loopback_proxy", "dangerously_allow_all_unix_sockets",
        "allow_local_binding",
    }
    string_keys = {"proxy_url", "socks_url"}
    if any(key in value and type(value[key]) is not bool for key in boolean_keys) or any(
        key in value and type(value[key]) is not str for key in string_keys
    ):
        raise ValueError
    if "mode" in value and value["mode"] not in {"limited", "full"}:
        raise ValueError
    for key in ("domains", "unix_sockets"):
        if key in value and not _is_string_map(value[key], frozenset({"allow", "deny"})):
            raise ValueError
    if "mitm" in value:
        _validate_mitm(value["mitm"])


def _validate_permission_profile(
    name: str, profile: object, known_profiles: frozenset[str]
) -> None:
    allowed = {"description", "extends", "workspace_roots", "filesystem", "network"}
    if PROFILE_RE.fullmatch(name) is None or type(profile) is not dict or not set(profile) <= allowed:
        raise ValueError
    if "description" in profile and type(profile["description"]) is not str:
        raise ValueError
    if "extends" in profile:
        parent = profile["extends"]
        if type(parent) is not str or parent not in _BUILTIN_PARENTS | known_profiles:
            raise ValueError
    roots = profile.get("workspace_roots")
    if roots is not None and (
        type(roots) is not dict
        or any(type(path) is not str or type(enabled) is not bool for path, enabled in roots.items())
    ):
        raise ValueError
    filesystem = profile.get("filesystem")
    if filesystem is not None:
        if type(filesystem) is not dict:
            raise ValueError
        for path, permission in filesystem.items():
            if type(path) is not str:
                raise ValueError
            if path == "glob_scan_max_depth":
                if type(permission) is not int or permission < 1:
                    raise ValueError
            elif type(permission) is str:
                if permission not in {"read", "write", "deny"}:
                    raise ValueError
            elif not _is_string_map(permission, frozenset({"read", "write", "deny"})):
                raise ValueError
    if "network" in profile:
        _validate_network(profile["network"])


def _validate_doctor_checks(checks: object) -> None:
    base_keys = {"id", "category", "status", "summary", "details", "remediation", "durationMs"}
    issue_keys = {"severity", "cause", "measured", "expected", "remedy", "fields"}
    if type(checks) is not dict:
        raise ValueError
    for check_id, check in checks.items():
        if type(check_id) is not str or type(check) is not dict or frozenset(check) not in {
            frozenset(base_keys), frozenset({*base_keys, "issues"})
        }:
            raise ValueError
        if (
            check.get("id") != check_id
            or type(check.get("category")) is not str
            or check.get("status") not in {"ok", "warning", "fail"}
            or type(check.get("summary")) is not str
            or type(check.get("details")) is not dict
            or any(type(key) is not str or type(value) is not str for key, value in check["details"].items())
            or check.get("remediation") is not None and type(check.get("remediation")) is not str
            or type(check.get("durationMs")) is not int
            or check["durationMs"] < 0
        ):
            raise ValueError
        if "issues" in check:
            issues = check["issues"]
            if type(issues) is not list:
                raise ValueError
            for issue in issues:
                if type(issue) is not dict or set(issue) != issue_keys or any(
                    type(issue[key]) is not str for key in ("severity", "cause", "measured", "expected")
                ) or issue["remedy"] is not None and type(issue["remedy"]) is not str or not _is_string_list(issue["fields"]):
                    raise ValueError


@dataclass(slots=True)
class ManagedProfileCatalog:
    """Discover only configured profiles that pass the real sandbox probe."""

    codex_doctor: DoctorRunner = field(default_factory=SubprocessDoctorRunner)
    trusted_admin_catalog: Path | None = None
    probe_worktree: Path | None = None
    executor_factory: SandboxExecutorFactory = field(default_factory=ManagedSandboxExecutorFactory)
    file_security: Callable[[Path, bool], bool] = _default_file_security
    timeout_seconds: float = 20.0
    max_output_bytes: int = 1024 * 1024
    cold_config_path: Path | None = field(default=None, repr=False)
    codex_runtime_preparer: CodexRuntimePreparer | None = field(
        default=None, repr=False
    )
    _construction_token: object | None = field(default=None, repr=False)

    @classmethod
    def production(
        cls,
        *,
        probe_parent: Path | None = None,
        codex_cache_root: Path | None = None,
    ) -> ManagedProfileCatalog:
        preparer = (
            CodexRuntimePreparer()
            if codex_cache_root is None
            else CodexRuntimePreparer(cache_root=codex_cache_root)
        )
        user_home = Path.home()
        return cls(
            codex_doctor=SubprocessDoctorRunner(codex_preparer=preparer),
            probe_worktree=probe_parent,
            executor_factory=ManagedSandboxExecutorFactory(codex_preparer=preparer),
            file_security=_default_file_security,
            cold_config_path=(user_home / ".codex" / "config.toml").absolute(),
            codex_runtime_preparer=preparer,
            _construction_token=_PRODUCTION_CONSTRUCTION,
        )

    @classmethod
    def _testing(cls, *args: Any, **kwargs: Any) -> ManagedProfileCatalog:
        kwargs["_construction_token"] = _TEST_CONSTRUCTION
        return cls(*args, **kwargs)

    def __post_init__(self) -> None:
        if (
            self._construction_token not in {_PRODUCTION_CONSTRUCTION, _TEST_CONSTRUCTION}
            or
            not callable(self.codex_doctor)
            or not callable(self.executor_factory)
            or not callable(self.file_security)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or not 1 <= self.max_output_bytes <= _MAX_DOCUMENT_BYTES
            or (
                self.cold_config_path is not None
                and (
                    not isinstance(self.cold_config_path, Path)
                    or not self.cold_config_path.is_absolute()
                    or "\x00" in os.fspath(self.cold_config_path)
                )
            )
        ):
            raise SetupValidationError("managed profile catalog is invalid")

    def list_profiles(self, *, timeout_seconds: float | None = None) -> tuple[str, ...]:
        total = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if not isinstance(total, (int, float)) or not math.isfinite(total) or total <= 0:
            raise SetupValidationError("managed profile timeout is invalid")
        deadline = time.monotonic() + total
        admin_profiles = self._read_admin_profiles()
        cold_config = self.cold_config_path
        cold_config_absent = False
        cold_config_empty = False
        if cold_config is not None:
            try:
                cold_config.stat(follow_symlinks=False)
            except FileNotFoundError:
                cold_config_absent = True
            except OSError:
                pass
            else:
                cold_config_empty = self._cold_config_has_no_profiles(cold_config)
        if (cold_config_absent or cold_config_empty) and not admin_profiles:
            return ()
        config_path = self._config_path_from_doctor(timeout_seconds=total)
        user_profiles = self._read_user_profiles(config_path)
        if len(user_profiles) != len(set(user_profiles)) or len(admin_profiles) != len(set(admin_profiles)):
            raise SetupValidationError("managed profile catalog is invalid")
        if set(user_profiles) & set(admin_profiles):
            raise SetupValidationError("managed profile catalog conflicts")
        candidates = tuple(sorted((*user_profiles, *admin_profiles)))
        available: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="ones-profile-probe-") as raw_root:
                probe_root = prepare_private_directory(
                    Path(raw_root).resolve(strict=True) / "private"
                )
                for name in candidates:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    executor = self.executor_factory(name)
                    try:
                        completed = executor(
                            sandbox_preflight_command(),
                            cwd=probe_root,
                            env=_clean_doctor_environment(),
                            timeout=remaining,
                            max_output_bytes=64 * 1024,
                        )
                        if completed.returncode != 0:
                            raise RuntimeError
                    except MemoryError:
                        raise
                    except Exception:
                        continue
                    available.append(name)
        except SetupValidationError:
            raise
        except MemoryError:
            raise
        except Exception:
            raise SetupValidationError("managed profile capability root is unavailable") from None
        return tuple(available)

    def verify_builtin_workspace_profile(
        self, *, timeout_seconds: float | None = None
    ) -> str:
        """Prove the fixed runtime-only workspace profile through the real sandbox."""

        total = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        primary_failure: BaseException | None = None
        cleanup_failure: BaseException | None = None
        executor: Any = None
        completed: subprocess.CompletedProcess[str] | None = None
        temporary_root: Any = None
        preparer: Any = None
        factory: Any = None
        raw_root: object | None = None
        probe_root: Path | None = None
        remaining: float | None = None
        try:
            if (
                isinstance(total, bool)
                or not isinstance(total, (int, float))
                or not math.isfinite(total)
                or total <= 0
            ):
                raise ValueError
            deadline = time.monotonic() + total
            preparer = self.codex_runtime_preparer
            if preparer is None:
                preparer = CodexRuntimePreparer()
            factory = BuiltinWorkspaceSandboxExecutorFactory(
                codex_preparer=preparer
            )
            executor = factory(BUILTIN_WORKSPACE_PROFILE)
            temporary_root = tempfile.TemporaryDirectory(
                prefix="ones-builtin-profile-probe-"
            )
            raw_root = temporary_root.__enter__()
            probe_root = prepare_private_directory(
                Path(raw_root).resolve(strict=True) / "private"
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            completed = executor(
                sandbox_preflight_command(),
                cwd=probe_root,
                env=_clean_doctor_environment(),
                timeout=remaining,
                max_output_bytes=64 * 1024,
            )
            if completed.returncode != 0:
                raise RuntimeError
        except BaseException as error:
            primary_failure = error
        if temporary_root is not None:
            try:
                temporary_root.__exit__(
                    type(primary_failure) if primary_failure is not None else None,
                    primary_failure,
                    (
                        primary_failure.__traceback__
                        if primary_failure is not None
                        else None
                    ),
                )
            except BaseException as error:
                cleanup_failure = error
        priority_failure = next(
            (
                failure
                for failure in (primary_failure, cleanup_failure)
                if failure is not None and _is_probe_priority_failure(failure)
            ),
            None,
        )
        if priority_failure is not None:
            failure = priority_failure
            del primary_failure, cleanup_failure, priority_failure
            del executor, completed, temporary_root, preparer, factory
            del raw_root, probe_root, remaining
            _raise_sanitized_setup_failure(failure)
        if primary_failure is not None or cleanup_failure is not None:
            del primary_failure, cleanup_failure, priority_failure
            del executor, completed, temporary_root, preparer, factory
            del raw_root, probe_root, remaining
            _raise_sanitized_setup_failure(
                SetupValidationError(
                    "built-in workspace profile is unavailable"
                )
            )
        return BUILTIN_WORKSPACE_PROFILE

    def require_selected(
        self,
        selected: str,
        source: SandboxPermissionProfileSource,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        if (
            type(selected) is not str
            or type(source) is not SandboxPermissionProfileSource
            or PROFILE_RE.fullmatch(selected) is None
        ):
            raise SetupValidationError("managed profile selection is invalid")
        if source is SandboxPermissionProfileSource.BUILTIN_WORKSPACE:
            if selected != BUILTIN_WORKSPACE_PROFILE:
                raise SetupValidationError("managed profile selection is invalid")
            verified = self.verify_builtin_workspace_profile(
                timeout_seconds=timeout_seconds
            )
            if type(verified) is not str or verified != BUILTIN_WORKSPACE_PROFILE:
                raise SetupValidationError("built-in workspace profile is unavailable")
            return verified
        if source is not SandboxPermissionProfileSource.MANAGED:
            raise SetupValidationError("managed profile selection is invalid")
        if selected == BUILTIN_WORKSPACE_PROFILE:
            raise SetupValidationError("managed profile selection is invalid")
        if selected not in self.list_profiles(timeout_seconds=timeout_seconds):
            raise SetupValidationError("managed profile is unavailable")
        return selected

    def _config_path_from_doctor(self, *, timeout_seconds: float) -> Path:
        try:
            completed = self.codex_doctor(
                ["doctor", "--json"],
                cwd=(self.probe_worktree or Path.cwd()).resolve(strict=True),
                env=_clean_doctor_environment(),
                timeout=timeout_seconds,
                max_output_bytes=self.max_output_bytes,
                shell=False,
            )
            if (
                completed.returncode not in {0, 1}
                or type(completed.stdout) is not str
                or type(completed.stderr) is not str
                or len(completed.stdout.encode("utf-8", errors="strict")) > self.max_output_bytes
                or len(completed.stderr.encode("utf-8", errors="strict")) > self.max_output_bytes
            ):
                raise ValueError
            document = json.loads(completed.stdout)
            if (
                type(document) is not dict
                or set(document) != {
                    "schemaVersion", "generatedAt", "overallStatus", "codexVersion", "checks"
                }
                or type(document["schemaVersion"]) is not int
                or document["schemaVersion"] != 1
                or type(document["generatedAt"]) is not str
                or document["overallStatus"] not in {"ok", "warning", "fail"}
                or type(document["codexVersion"]) is not str
                or type(document["checks"]) is not dict
            ):
                raise ValueError
            _validate_doctor_checks(document["checks"])
            check = document["checks"].get("config.load")
            if type(check) is not dict or set(check) != {
                "id", "category", "status", "summary", "details", "remediation", "durationMs"
            } or check.get("id") != "config.load" or check.get("status") != "ok":
                raise ValueError
            details = check["details"]
            if (
                type(details) is not dict
                or set(details) != _DOCTOR_CONFIG_DETAIL_KEYS
                or details.get("config.toml parse") != "ok"
            ):
                raise ValueError
            home = Path(details["CODEX_HOME"]).resolve(strict=True)
            config = Path(details["config.toml"]).resolve(strict=True)
            if config.name != "config.toml" or config.parent != home:
                raise ValueError
            return config
        except BaseException as error:
            if _is_probe_priority_failure(error):
                raise
            raise SetupValidationError("Codex profile discovery is unavailable") from None

    def _cold_config_has_no_profiles(self, path: Path) -> bool:
        """Return true only when the secured candidate proves an empty catalog."""

        try:
            raw = _read_secure_bytes(
                path,
                admin=False,
                security=self.file_security,
            )
            document = tomllib.loads(raw.decode("utf-8", errors="strict"))
            if type(document) is not dict or "profiles" in document:
                return False
            permissions = document.get("permissions")
            return permissions is None or (
                type(permissions) is dict and not permissions
            )
        except (
            SetupValidationError,
            UnicodeError,
            tomllib.TOMLDecodeError,
            TypeError,
            ValueError,
        ):
            return False

    def _read_user_profiles(self, path: Path) -> tuple[str, ...]:
        raw = _read_secure_bytes(path, admin=False, security=self.file_security)
        try:
            document = tomllib.loads(raw.decode("utf-8", errors="strict"))
            permissions = document.get("permissions")
            if type(permissions) is not dict:
                raise ValueError
            names: list[str] = []
            known = frozenset(permissions)
            for name, profile in permissions.items():
                _validate_permission_profile(name, profile, known)
                names.append(name)
            for name in names:
                seen: set[str] = set()
                current = name
                while current not in _BUILTIN_PARENTS:
                    if current in seen:
                        raise ValueError
                    seen.add(current)
                    parent = permissions[current].get("extends")
                    if parent is None:
                        break
                    current = parent
            return tuple(names)
        except (UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
            raise SetupValidationError("Codex permissions table is invalid") from None

    def _read_admin_profiles(self) -> tuple[str, ...]:
        path = self.trusted_admin_catalog
        if path is None:
            program_data = os.environ.get("PROGRAMDATA")
            if not program_data:
                return ()
            candidate = Path(program_data) / "ones-dev" / "managed-sandbox-profiles.json"
            if not candidate.exists():
                return ()
            path = candidate
        if not self.file_security(path.parent.resolve(strict=True), True):
            raise SetupValidationError("administrator profile catalog is unsafe")
        raw = _read_secure_bytes(path, admin=True, security=self.file_security)
        try:
            document = json.loads(raw.decode("utf-8", errors="strict"))
            if type(document) is not dict or set(document) != {"schema_version", "profiles"}:
                raise ValueError
            profiles = document["profiles"]
            if document["schema_version"] != 1 or type(profiles) is not list or any(
                type(name) is not str or PROFILE_RE.fullmatch(name) is None for name in profiles
            ):
                raise ValueError
            if len(profiles) != len(set(profiles)):
                raise ValueError
            return tuple(profiles)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise SetupValidationError("administrator profile catalog is invalid") from None


async def _await_call(call: Any) -> Any:
    return await call if inspect.isawaitable(call) else call


async def _to_thread_cleanup(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def _invoke(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    return await _to_thread_cleanup(method, *args, **kwargs)


@dataclass(frozen=True, slots=True)
class _RepositoryReadSnapshot:
    root_identity: tuple[int, int]
    git_identity: tuple[int, int, int]
    config_identity: tuple[int, int, int, int]
    config_sha256: str
    head: str
    index: str
    worktree_hashes: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class ReadOnlyRepositoryInspector:
    """Git source adapter using only plumbing reads with optional locks disabled."""

    max_output_bytes: int = 10 * 1024 * 1024

    def _environment(self, private_root: Path, hooks: Path) -> dict[str, str]:
        environment = _clean_doctor_environment()
        environment.update(
            {
                "HOME": str(private_root),
                "USERPROFILE": str(private_root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(hooks),
                "GIT_CONFIG_KEY_1": "core.fsmonitor",
                "GIT_CONFIG_VALUE_1": "false",
                "GIT_CONFIG_KEY_2": "credential.helper",
                "GIT_CONFIG_VALUE_2": "",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "GIT_SSH_COMMAND": _LOCKED_SSH_COMMAND,
            }
        )
        return environment

    @staticmethod
    def _git_configuration(hooks: Path) -> list[str]:
        return [
            "-c", f"core.hooksPath={hooks}",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            "-c", f"core.sshCommand={_LOCKED_SSH_COMMAND}",
        ]

    def _run(
        self, argv: list[str], *, cwd: Path, private_root: Path, hooks: Path, timeout: float
    ) -> str:
        git_unavailable = False
        environment = self._environment(private_root, hooks)
        try:
            completed = _bounded_subprocess(
                argv,
                cwd=cwd,
                env=environment,
                timeout=timeout,
                max_output_bytes=self.max_output_bytes,
            )
        except FileNotFoundError:
            if argv and argv[0] == "git" and cwd.is_dir():
                git_unavailable = True
            else:
                raise
        except CodexProcessStartError as error:
            if (
                argv
                and argv[0] == "git"
                and isinstance(error.__cause__, FileNotFoundError)
                and cwd.is_dir()
            ):
                git_unavailable = True
            else:
                raise
        if git_unavailable:
            _raise_git_executable_unavailable()
        if completed.returncode != 0:
            raise RuntimeError("read-only Git probe failed")
        return completed.stdout

    @staticmethod
    def _git_config_path(root: Path, git_entry: Path) -> Path:
        if git_entry.is_dir():
            return git_entry / "config"
        payload = git_entry.read_bytes()
        if len(payload) > 4096:
            raise RuntimeError("repository source path is unsafe")
        try:
            line = payload.decode("utf-8", "strict").strip()
        except UnicodeDecodeError as error:
            raise RuntimeError("repository source path is unsafe") from error
        if not line.startswith("gitdir: ") or "\n" in line or "\r" in line:
            raise RuntimeError("repository source path is unsafe")
        target = Path(line[8:])
        if not target.is_absolute():
            target = root / target
        return target.resolve(strict=True) / "config"

    @staticmethod
    def _config_fingerprint(config: Path) -> tuple[tuple[int, int, int, int], str]:
        metadata = config.lstat()
        if (
            _is_link_or_reparse(config)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_DOCUMENT_BYTES
        ):
            raise RuntimeError("repository source path is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(config, flags)
        digest = hashlib.sha256()
        try:
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            if identity != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            ):
                raise RuntimeError("repository source identity changed")
            while chunk := os.read(descriptor, 64 * 1024):
                digest.update(chunk)
        finally:
            os.close(descriptor)
        final = config.lstat()
        if identity != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
            raise RuntimeError("repository source identity changed")
        return identity, digest.hexdigest()

    def _worktree_fingerprint(self, root: Path, relative: str) -> tuple[str, str]:
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise RuntimeError("repository source path is unsafe")
        candidate = root.joinpath(*relative_path.parts)
        if _has_link_or_reparse_ancestor(candidate):
            raise RuntimeError("repository source path is unsafe")
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return relative, "missing"
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > self.max_output_bytes:
            raise RuntimeError("repository source path is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        digest = hashlib.sha256()
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            ):
                raise RuntimeError("repository source identity changed")
            while chunk := os.read(descriptor, 64 * 1024):
                digest.update(chunk)
        finally:
            os.close(descriptor)
        final = candidate.lstat()
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise RuntimeError("repository source identity changed")
        return relative, digest.hexdigest()

    def snapshot(self, path: Path, *, timeout: float = 10.0) -> _RepositoryReadSnapshot:
        root = path.resolve(strict=True)
        root_stat = root.stat()
        git_entry = root / ".git"
        git_stat = git_entry.lstat()
        config = self._git_config_path(root, git_entry)
        config_before = self._config_fingerprint(config)
        deadline = time.monotonic() + timeout
        with tempfile.TemporaryDirectory(prefix="ones-repository-probe-") as raw_private:
            private_root = prepare_private_directory(
                Path(raw_private).resolve(strict=True) / "private"
            )
            hooks = private_root / "empty-hooks"
            hooks.mkdir()

            def read(arguments: list[str]) -> str:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                argv = ["git", *self._git_configuration(hooks), "-C", str(root), *arguments]
                return self._run(
                    argv, cwd=private_root, private_root=private_root, hooks=hooks, timeout=remaining
                )

            tracked = read(["ls-files", "--cached", "-z"])
            untracked = read(["ls-files", "--others", "--exclude-standard", "-z"])
            paths = sorted(
                {item for listing in (tracked, untracked) for item in listing.split("\0") if item}
            )
            hashes = tuple(self._worktree_fingerprint(root, relative) for relative in paths)
            snapshot = _RepositoryReadSnapshot(
                root_identity=(root_stat.st_dev, root_stat.st_ino),
                git_identity=(git_stat.st_dev, git_stat.st_ino, git_stat.st_mode),
                config_identity=config_before[0],
                config_sha256=config_before[1],
                head=read(["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"]),
                index=read(["ls-files", "--stage", "-z"]),
                worktree_hashes=hashes,
            )
        if self._config_fingerprint(config) != config_before:
            raise RuntimeError("repository source identity changed")
        return snapshot

    def ls_remote(self, path: Path, url: str, *, timeout: float) -> None:
        del path
        if type(url) is not str or any(ord(character) < 0x20 for character in url):
            raise RuntimeError("read-only Git probe failed")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as error:
            raise RuntimeError("read-only Git probe failed") from error
        if (
            parsed.scheme not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or "\\" in url
            or port is not None and not 1 <= port <= 65535
        ):
            raise RuntimeError("read-only Git probe failed")
        with tempfile.TemporaryDirectory(prefix="ones-ls-remote-") as raw_private:
            private_root = prepare_private_directory(
                Path(raw_private).resolve(strict=True) / "private"
            )
            hooks = private_root / "empty-hooks"
            hooks.mkdir()
            argv = ["git", *self._git_configuration(hooks), "ls-remote", "--refs", url]
            self._run(
                argv, cwd=private_root, private_root=private_root, hooks=hooks, timeout=timeout
            )


def _result(step: SetupStep, category: _CATEGORIES = "ok") -> ConnectionTestResult:
    return ConnectionTestResult(
        step=step,
        status=ValidationStatus.PASSED if category == "ok" else ValidationStatus.FAILED,
        category=category,
    )


def _failure_category(error: Exception, *, default: _CATEGORIES = "unreachable") -> _CATEGORIES:
    if isinstance(error, GitExecutableUnavailableError):
        return "git_unavailable"
    name = type(error).__name__.casefold()
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, subprocess.TimeoutExpired)) or "timeout" in name:
        return "timeout"
    if "ssl" in name or "tls" in name or "certificate" in name:
        return "tls"
    if "auth" in name or "permission" in name or "forbidden" in name or "unauthorized" in name:
        return "authentication"
    if isinstance(error, (TypeError, ValueError)):
        return "incompatible"
    return default


@dataclass(slots=True)
class SetupValidator:
    """Orchestrate bounded probes that cannot perform business writes."""

    ones_gateway: Any = None
    ones_transport: Any = None
    provider_transport: ProviderTransport | None = None
    git_runner: GitReadOnlyRunner | None = None
    repository_inspector: Any = None
    command_executor: Any = None
    codex_auth_metadata: CodexAuthMetadata | Callable[[], Mapping[str, object]] | None = None
    profile_catalog: Any = None
    timeout_seconds: float = 10.0
    _construction_token: object | None = field(default=None, repr=False)

    @classmethod
    def production(
        cls,
        *,
        profile_catalog: ManagedProfileCatalog,
        ones_gateway: Any = None,
        provider_transport: ProviderTransport | None = None,
    ) -> SetupValidator:
        if not isinstance(profile_catalog, ManagedProfileCatalog):
            raise SetupValidationError("production profile catalog is invalid")
        return cls(
            ones_gateway=ones_gateway,
            provider_transport=provider_transport,
            repository_inspector=ReadOnlyRepositoryInspector(),
            codex_auth_metadata=CodexAuthSourceChecker(),
            profile_catalog=profile_catalog,
            _construction_token=_PRODUCTION_CONSTRUCTION,
        )

    @classmethod
    def _testing(cls, **kwargs: Any) -> SetupValidator:
        kwargs["_construction_token"] = _TEST_CONSTRUCTION
        return cls(**kwargs)

    def __post_init__(self) -> None:
        if (
            self._construction_token not in {_PRODUCTION_CONSTRUCTION, _TEST_CONSTRUCTION}
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise SetupValidationError("setup validator timeout is invalid")

    async def probe_ones(self, probe: OnesProbeInput) -> ConnectionTestResult:
        gateway = self.ones_gateway if self.ones_gateway is not None else self.ones_transport
        if gateway is None:
            return _result(SetupStep.ONES, "invalid_field")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                settings = getattr(gateway, "settings", None)
                configured_team = getattr(settings, "team_id", probe.team_id)
                if configured_team != probe.team_id:
                    return _result(SetupStep.ONES, "invalid_field")
                if hasattr(gateway, "authenticate"):
                    await _invoke(gateway.authenticate)
                if hasattr(gateway, "get_team"):
                    await _invoke(gateway.get_team, probe.team_id)
                if probe.project_id is not None and hasattr(gateway, "get_project"):
                    await _invoke(gateway.get_project, probe.project_id)
                else:
                    projects = await _invoke(gateway.list_projects, include_archived=False)
                    if probe.project_id is not None and not any(
                        str(item.get("uuid", item.get("id", ""))) == probe.project_id
                        for item in projects
                    ):
                        raise ValueError
                if probe.project_id is not None and probe.status_id is not None:
                    if hasattr(gateway, "get_status"):
                        await _invoke(gateway.get_status, probe.status_id)
                    else:
                        statuses = await _invoke(
                            gateway.list_defect_statuses,
                            probe.project_id,
                            probe.issue_type_id or probe.status_id,
                        )
                        if not any(
                            str(getattr(item, "id", "")) == probe.status_id
                            for item in statuses
                        ):
                            raise ValueError
                if probe.item_id is not None:
                    await _invoke(gateway.list_comments, probe.item_id, page_size=1)
            return _result(SetupStep.ONES)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.ONES, _failure_category(error))

    async def probe_provider(self, probe: ProviderProbeInput) -> ConnectionTestResult:
        if self.provider_transport is None:
            return _result(SetupStep.PROVIDER, "invalid_field")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await _invoke(
                    self.provider_transport.get,
                    probe.api_url,
                    timeout=self.timeout_seconds,
                )
            status = response if type(response) is int else getattr(response, "status_code", None)
            if status in {401, 403}:
                return _result(SetupStep.PROVIDER, "authentication")
            if type(status) is not int or not 200 <= status < 300:
                return _result(SetupStep.PROVIDER, "incompatible")
            return _result(SetupStep.PROVIDER)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.PROVIDER, _failure_category(error))

    async def probe_repository(self, probe: RepositoryProbeInput) -> ConnectionTestResult:
        inspector = self.repository_inspector
        if inspector is None and self.git_runner is not None:
            return _result(SetupStep.REPOSITORIES, "incompatible")
        if inspector is None:
            return _result(SetupStep.REPOSITORIES, "invalid_field")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                root = probe.path.resolve(strict=True)
                if not root.is_dir() or _has_link_or_reparse_ancestor(root):
                    return _result(SetupStep.REPOSITORIES, "unsafe_path")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.timeout_seconds
                before = await _invoke(
                    inspector.snapshot, root, timeout=max(0.001, deadline - loop.time())
                )
                await _invoke(
                    inspector.ls_remote,
                    root,
                    probe.remote_url,
                    timeout=max(0.001, deadline - loop.time()),
                )
                after = await _invoke(
                    inspector.snapshot, root, timeout=max(0.001, deadline - loop.time())
                )
            if before != after:
                return _result(SetupStep.REPOSITORIES, "unsafe_path")
            return _result(SetupStep.REPOSITORIES)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.REPOSITORIES, _failure_category(error))

    async def probe_codex(self, probe: CodexProbeInput) -> ConnectionTestResult:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.timeout_seconds
                metadata_source = self.codex_auth_metadata
                method = metadata_source.metadata if hasattr(metadata_source, "metadata") else metadata_source
                metadata = await _invoke(method)
                if type(metadata) is not dict or metadata.get("configured") is not True or set(metadata) - {"configured", "mode"}:
                    return _result(SetupStep.CODEX, "authentication")
                if self.profile_catalog is None:
                    return _result(SetupStep.CODEX, "sandbox")
                remaining = max(0.001, deadline - loop.time())
                if isinstance(self.profile_catalog, ManagedProfileCatalog):
                    await _to_thread_cleanup(
                        self.profile_catalog.require_selected,
                        probe.profile,
                        probe.profile_source,
                        timeout_seconds=remaining,
                    )
                else:
                    await _invoke(
                        self.profile_catalog.require_selected,
                        probe.profile,
                        probe.profile_source,
                    )
            return _result(SetupStep.CODEX)
        except BaseException as error:
            if _is_probe_priority_failure(error):
                raise
            return _result(SetupStep.CODEX, _failure_category(error, default="sandbox"))

    async def probe_private_paths(self, probe: PrivatePathsProbeInput) -> ConnectionTestResult:
        try:
            _validate_shape(probe.paths, require_three=True)
            for candidate in probe.paths:
                current = candidate
                while not current.exists() and current.parent != current:
                    current = current.parent
                if not current.exists() or _has_link_or_reparse_ancestor(current):
                    raise ValueError
                resolved = current.resolve(strict=True)
                if not resolved.is_dir():
                    raise ValueError
            return _result(SetupStep.PRIVATE_PATHS)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.PRIVATE_PATHS, "unsafe_path")


@dataclass(frozen=True, slots=True, init=False)
class RuntimeBootstrapper:
    """Production validation composition; dependency substitution stays test-private."""

    catalog: ManagedProfileCatalog
    validator: SetupValidator
    codex_runtime_preparer: CodexRuntimePreparer

    def __init__(
        self,
        catalog: ManagedProfileCatalog,
        validator: SetupValidator,
        codex_runtime_preparer: CodexRuntimePreparer,
        *,
        _construction_token: object,
    ) -> None:
        if _construction_token not in {_PRODUCTION_CONSTRUCTION, _TEST_CONSTRUCTION}:
            raise SetupValidationError("runtime bootstrap construction is private")
        object.__setattr__(self, "catalog", catalog)
        object.__setattr__(self, "validator", validator)
        object.__setattr__(self, "codex_runtime_preparer", codex_runtime_preparer)

    @classmethod
    def production(
        cls,
        *,
        probe_parent: Path | None = None,
        codex_cache_root: Path | None = None,
        ones_gateway: Any = None,
        provider_transport: ProviderTransport | None = None,
    ) -> RuntimeBootstrapper:
        catalog = ManagedProfileCatalog.production(
            probe_parent=probe_parent,
            codex_cache_root=codex_cache_root,
        )
        validator = SetupValidator.production(
            profile_catalog=catalog,
            ones_gateway=ones_gateway,
            provider_transport=provider_transport,
        )
        assert catalog.codex_runtime_preparer is not None
        return cls(
            catalog,
            validator,
            catalog.codex_runtime_preparer,
            _construction_token=_PRODUCTION_CONSTRUCTION,
        )


__all__ = [
    "CodexAuthSourceChecker",
    "BuiltinWorkspaceSandboxExecutorFactory",
    "CodexProbeInput",
    "CodexAuthMetadata",
    "ConnectionTestResult",
    "GitExecutableUnavailableError",
    "ManagedProfileCatalog",
    "ManagedSandboxExecutorFactory",
    "OnesProbeInput",
    "PrivatePathsProbeInput",
    "PROFILE_RE",
    "ProviderProbeInput",
    "ProviderTransport",
    "ReadOnlyRepositoryInspector",
    "GitReadOnlyRunner",
    "RepositoryProbeInput",
    "SetupStep",
    "SetupValidator",
    "RuntimeBootstrapper",
    "SubprocessDoctorRunner",
    "ValidationStatus",
]
