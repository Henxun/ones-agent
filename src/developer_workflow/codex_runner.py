"""Bounded, non-interactive Codex execution for isolated developer workflows."""

from __future__ import annotations

import ast
import json
import hmac
import math
import os
import re
import signal
import secrets
import shlex
import stat
import subprocess
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

from .codex_runtime import (
    CodexRuntimePreparer,
    LockedPrivateCodex,
    NativeCodexIdentity,
    _PreparedCodexRuntime,
    verify_locked_private_codex_for_execution,
)
from .contracts import (
    AcceptanceCoverage,
    CodexResult,
    CommandResult,
    PreparedWorktree,
    RepositoryChangeClaim,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositorySnapshot,
    RootCauseEvidence,
)
from .repository import HeadChangedError, WorktreeRepository
from .repository_group import PreparedRepository


class CodexRunnerError(RuntimeError):
    """Base error for a safely rejected Codex execution."""


class UnsafeCodexRunError(CodexRunnerError):
    """The requested execution would cross a local safety boundary."""


class CodexExecutionError(CodexRunnerError):
    """Codex could not be executed successfully."""


class CodexProcessStartError(CodexExecutionError):
    """The requested executable could not be started."""


class CodexTimeoutError(CodexExecutionError):
    """Codex exceeded its execution deadline."""


class CodexOutputError(CodexRunnerError):
    """Codex returned invalid or unsafe structured output."""

    def __init__(
        self,
        message: str,
        *,
        validation_hint: str = "",
        raw_output: str = "",
    ) -> None:
        super().__init__(message)
        self.validation_hint = validation_hint
        self.raw_output = raw_output


class _UnsafeReportedCommandError(ValueError):
    """A reported command is outside the bounded read/verification policy."""


_COMMAND_ATTESTATION_NONCE = object()
_COMMAND_ATTESTATION_SECRET = secrets.token_bytes(32)


def _is_native_codex_name(name: str) -> bool:
    return name == "codex" or name.casefold() == "codex.exe"


def _command_attestation_mac(
    prefix: tuple[str, ...],
    path: Path,
    identity: NativeCodexIdentity,
    sha256: str,
    cache_root: Path,
) -> bytes:
    snapshot = json.dumps(
        [
            "codex-command-v1",
            list(prefix),
            str(path),
            sha256,
            str(cache_root),
            identity.volume_serial,
            identity.file_index,
            identity.size,
            identity.mtime_ns,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    return hmac.digest(_COMMAND_ATTESTATION_SECRET, snapshot, "sha256")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class CodexCommand:
    """An immutable argv prefix produced by the private runtime staging layer."""

    prefix: tuple[str, ...]
    _path: Path = field(repr=False)
    _identity: object = field(repr=False)
    _sha256: str = field(repr=False)
    _cache_root: Path = field(repr=False)
    _lease: LockedPrivateCodex = field(repr=False)
    _seal: bytes = field(repr=False)
    _nonce: object = field(repr=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("Codex commands cannot be constructed directly")

    @classmethod
    def _from_runtime(cls, runtime: object) -> CodexCommand:
        if (
            type(runtime) is not _PreparedCodexRuntime
            or not runtime._is_attested()
            or runtime.path != runtime.path.resolve(strict=True)
            or not _is_native_codex_name(runtime.path.name)
        ):
            raise TypeError("Codex runtime attestation is invalid")
        prefix = (str(runtime.path),)
        seal = _command_attestation_mac(
            prefix,
            runtime.path,
            runtime.identity,
            runtime.sha256,
            runtime._cache_root,
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "prefix", prefix)
        object.__setattr__(instance, "_path", runtime.path)
        object.__setattr__(instance, "_identity", runtime.identity)
        object.__setattr__(instance, "_sha256", runtime.sha256)
        object.__setattr__(instance, "_cache_root", runtime._cache_root)
        object.__setattr__(instance, "_lease", runtime._lease)
        object.__setattr__(instance, "_seal", seal)
        object.__setattr__(instance, "_nonce", _COMMAND_ATTESTATION_NONCE)
        return instance

    def _is_attested(self) -> bool:
        try:
            if (
                self._nonce is not _COMMAND_ATTESTATION_NONCE
                or type(self.prefix) is not tuple
                or not isinstance(self._path, Path)
                or type(self._identity) is not NativeCodexIdentity
                or type(self._sha256) is not str
                or len(self._sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self._sha256
                )
                or not isinstance(self._cache_root, Path)
                or type(self._lease) is not LockedPrivateCodex
                or not self._lease.verify()
                or self._lease.path != self._path
                or self._lease.identity != self._identity
                or self._lease.sha256 != self._sha256
                or self._lease._cache_root != self._cache_root
                or type(self._seal) is not bytes
                or self.prefix != (str(self._path),)
                or self._path.resolve(strict=True) != self._path
                or self._cache_root.resolve(strict=True) != self._cache_root
                or not _is_native_codex_name(self._path.name)
                or self._path.parent.name != self._sha256
                or self._path.parent.parent != self._cache_root
            ):
                return False
            expected = _command_attestation_mac(
                self.prefix,
                self._path,
                self._identity,
                self._sha256,
                self._cache_root,
            )
            return hmac.compare_digest(self._seal, expected)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def argv(self, *arguments: str) -> list[str]:
        if not self._is_attested():
            raise TypeError("Codex command attestation is invalid")
        if any(
            type(argument) is not str or not argument or "\x00" in argument
            for argument in arguments
        ):
            raise ValueError("Codex command argument is invalid")
        return [*self.prefix, *arguments]

    def close(self) -> None:
        try:
            lease = self._lease
        except AttributeError:
            return
        if type(lease) is LockedPrivateCodex:
            lease.close()

    def __copy__(self) -> CodexCommand:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> CodexCommand:
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("Codex commands cannot be serialized")


def _raise_codex_executable_unavailable() -> None:
    raise CodexProcessStartError("Codex executable is unavailable") from None


def resolve_codex_command(
    *,
    _prepare: Callable[[], _PreparedCodexRuntime] | None = None,
) -> CodexCommand:
    """Return only the verified native executable staged in the private cache."""

    runtime: _PreparedCodexRuntime | None = None
    failed = False
    try:
        runtime = (
            CodexRuntimePreparer().prepare_verified()
            if _prepare is None
            else _prepare()
        )
    except BaseException as error:
        if (
            isinstance(error, MemoryError)
            or not isinstance(error, Exception)
            or not isinstance(error, OSError)
        ):
            raise
        failed = True
    if failed:
        del _prepare, runtime
        _raise_codex_executable_unavailable()
    assert runtime is not None
    return CodexCommand._from_runtime(runtime)


class RepositoryGuard(Protocol):
    def assert_head_unchanged(self, prepared: PreparedWorktree) -> None: ...

    def snapshot(self, prepared: PreparedWorktree, mapping: RepositoryMapping) -> Any: ...

    def contains_sensitive_content(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping,
        secrets: tuple[str, ...],
    ) -> bool: ...


class CommandExecutor(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ALLOWED_ENV = {
    "comspec", "lang", "lc_all",
    "no_color", "path", "pathext", "systemroot", "temp", "term", "tmp",
    "tmpdir", "windir",
    "ssl_cert_dir", "ssl_cert_file", "requests_ca_bundle", "curl_ca_bundle",
    "http_proxy", "https_proxy", "no_proxy",
    "codex_api_key", "codex_auth_token",
    "openai_api_key", "openai_api_version", "openai_base_url",
    "openai_organization", "openai_org_id", "openai_project",
}
_SECRET_ENV_TOKENS = (
    "token", "password", "secret", "credential", "authorization", "cookie",
    "api_key", "apikey", "private_key", "access_key", "askpass",
    "connection_string", "dsn",
)
_ACTIVITY_FILE = "codex-activity.jsonl"
_PENDING_ROOT_CAUSE_FILE = "pending-root-cause-output.txt"
_CODEX_SESSION_FILE = "codex-session-id.txt"
_CODEX_SESSION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)
_MAX_ACTIVITY_BYTES = 512 * 1024
_SENSITIVE_COMMAND_VALUE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|authorization)"
    r"(\s*[:=]\s*|\s+)\S+"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+\S+")

_PAYLOAD_DEFAULTS: dict[str, object] = {
    "changed_files": (), "repository_changes": (), "commands": (),
    "evidence": (), "review_findings": (), "review_repair_scope": (),
    "review_external_validation": (), "verification_needs": (), "risks": (),
    "unresolved_items": (), "acceptance_coverage": (),
    "unrelated_changes_checked": False, "root_cause_evidence": (),
    "investigation_suggestions": (), "behavior_before": "",
    "behavior_after": "", "impact_scope": (), "risk_level": "",
}
_ROOT_EVIDENCE_DEFAULTS: dict[str, object] = {
    "repository_file": None, "start_line": None, "end_line": None,
    "symbol": "", "code_excerpt": "", "call_chain": (),
    "reproduction_file": None, "impacted_repository_files": (),
}
_SUPPORT_POINT_DEFAULTS: dict[str, object] = {
    "file_path": "", "repository_file": None, "snippet": "",
    "start_line": None, "end_line": None, "direct_root_cause": False,
}
_ROOT_CAUSE_STAGE_IRRELEVANT_FIELDS = frozenset(
    {
        "changed_files",
        "repository_changes",
        "commands",
        "evidence",
        "review_findings",
        "review_repair_scope",
        "review_external_validation",
        "verification_needs",
        "acceptance_coverage",
        "unrelated_changes_checked",
        "behavior_after",
    }
)


def _fresh_json_default(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def _normalize_structural_defaults(payload: object) -> object:
    """Fill only contract defaults; never fabricate substantive evidence."""

    if type(payload) is not dict:
        return payload
    for key, value in _PAYLOAD_DEFAULTS.items():
        payload.setdefault(key, _fresh_json_default(value))
    evidence_items = payload.get("root_cause_evidence")
    if type(evidence_items) is list:
        for evidence in evidence_items:
            if type(evidence) is not dict:
                continue
            for key, value in _ROOT_EVIDENCE_DEFAULTS.items():
                evidence.setdefault(key, _fresh_json_default(value))
            supporting = evidence.get("supporting_points")
            if type(supporting) is list:
                for point in supporting:
                    if type(point) is not dict:
                        continue
                    for key, value in _SUPPORT_POINT_DEFAULTS.items():
                        point.setdefault(key, _fresh_json_default(value))
    return payload


def _normalize_root_cause_cross_fields(payload: object) -> object:
    """Canonicalize duplicated paths without changing analysis semantics.

    Repository-qualified claims are the unambiguous identity in a repository
    group.  Models occasionally repeat several related paths in the adjacent
    display-only path field, which then fails the stricter Pydantic contract.
    Keep the claim and make the duplicated field agree with it before schema
    and contract validation.  No evidence, repository key, or path is invented.
    """

    if type(payload) is not dict:
        return payload
    evidence_items = payload.get("root_cause_evidence")
    if type(evidence_items) is not list:
        return payload
    for evidence in evidence_items:
        if type(evidence) is not dict:
            continue
        repository_file = evidence.get("repository_file")
        if type(repository_file) is dict and type(repository_file.get("path")) is str:
            evidence["file_path"] = repository_file["path"]
        reproduction_file = evidence.get("reproduction_file")
        if type(reproduction_file) is dict and type(reproduction_file.get("path")) is str:
            reproduction_path = reproduction_file["path"]
            evidence["reproduction_test"] = reproduction_path
            selector = evidence.get("test_selector")
            if type(selector) is str:
                _, separator, suffix = selector.partition("::")
                evidence["test_selector"] = (
                    reproduction_path + separator + suffix
                    if separator and suffix
                    else reproduction_path
                )
        impacted_claims = evidence.get("impacted_repository_files")
        if type(impacted_claims) is list and impacted_claims and all(
            type(item) is dict and type(item.get("path")) is str
            for item in impacted_claims
        ):
            evidence["impacted_files"] = [item["path"] for item in impacted_claims]
        supporting = evidence.get("supporting_points")
        if type(supporting) is not list:
            continue
        for point in supporting:
            if type(point) is not dict:
                continue
            claim = point.get("repository_file")
            if type(claim) is dict and type(claim.get("path")) is str:
                point["file_path"] = claim["path"]
    return payload


def _safe_validation_hint(error: BaseException) -> str:
    """Return a bounded field path and validator type, never rejected values."""

    if isinstance(error, _UnsafeReportedCommandError):
        return "commands (unsafe_command)"
    absolute_path = getattr(error, "absolute_path", None)
    validator = getattr(error, "validator", None)
    if absolute_path is not None and type(validator) is str:
        segments = tuple(str(item) for item in absolute_path)
        if all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in segments):
            path = ".".join(segments) or "$"
            rule = validator if re.fullmatch(r"[A-Za-z_]{1,32}", validator) else "schema"
            return f"{path} ({rule})"
    errors_method = getattr(error, "errors", None)
    if callable(errors_method):
        try:
            errors = errors_method(include_url=False)
        except (TypeError, ValueError):
            errors = ()
        if type(errors) is list and errors and type(errors[0]) is dict:
            location = errors[0].get("loc", ())
            error_type = errors[0].get("type", "validation")
            if type(location) is tuple and all(
                re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(item))
                for item in location
            ):
                path = ".".join(str(item) for item in location) or "$"
                rule = (
                    error_type
                    if type(error_type) is str
                    and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", error_type)
                    else "validation"
                )
                return f"{path} ({rule})"
    return "workflow result contract"


def _is_positive_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except (OverflowError, TypeError, ValueError):
        return False


def _safe_command_activity(
    command: object, secrets_to_remove: tuple[str, ...]
) -> str:
    if type(command) is not str:
        return "repository command"
    value = " ".join(command.split())
    for secret in secrets_to_remove:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = _SENSITIVE_COMMAND_VALUE.sub(r"\1=[redacted]", value)
    value = _BEARER_VALUE.sub("Bearer [redacted]", value)
    if not value or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        return "repository command"
    return value[:240] + ("..." if len(value) > 240 else "")


def _safe_agent_activity(
    text: object, secrets_to_remove: tuple[str, ...]
) -> str:
    """Return a bounded public agent update, never private reasoning text."""

    if type(text) is not str:
        return "AI produced an analysis update"
    value = text
    try:
        structured = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        structured = None
    if type(structured) is dict and type(structured.get("summary")) is str:
        value = f"Analysis result: {structured['summary']}"
    else:
        value = f"AI update: {value}"
    value = " ".join(value.split())
    for secret in secrets_to_remove:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = _SENSITIVE_COMMAND_VALUE.sub(r"\1=[redacted]", value)
    value = _BEARER_VALUE.sub("Bearer [redacted]", value)
    if not value or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        return "AI produced an analysis update"
    return value[:480] + ("..." if len(value) > 480 else "")


def _codex_activity_from_event(
    line: str, secrets_to_remove: tuple[str, ...]
) -> tuple[str, str] | None:
    """Map Codex JSONL to an auditable event without exposing reasoning text."""

    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if type(event) is not dict or type(event.get("type")) is not str:
        return None
    event_type = event["type"]
    if event_type == "thread.started":
        return "session", "Codex session started"
    if event_type == "turn.started":
        return "analysis", "AI analysis started"
    if event_type == "turn.completed":
        usage = event.get("usage")
        if type(usage) is dict and type(usage.get("output_tokens")) is int:
            return (
                "analysis",
                f"AI analysis completed ({usage['output_tokens']} output tokens)",
            )
        return "analysis", "AI analysis completed"
    if event_type == "turn.failed":
        return "error", "AI analysis failed safely"
    if event_type == "error":
        return "error", "Codex reported an execution error"
    if event_type not in {"item.started", "item.updated", "item.completed"}:
        return None
    item = event.get("item")
    if type(item) is not dict or type(item.get("type")) is not str:
        return None
    item_type = item["type"]
    completed = event_type == "item.completed"
    if item_type == "command_execution":
        command = _safe_command_activity(item.get("command"), secrets_to_remove)
        if completed:
            exit_code = item.get("exit_code")
            suffix = f" (exit {exit_code})" if type(exit_code) is int else ""
            return "command", f"Command completed: {command}{suffix}"
        if event_type == "item.started":
            return "command", f"Running: {command}"
        return None
    if item_type == "file_change":
        return (
            "file",
            "Repository file changes recorded"
            if completed
            else "Reviewing repository file changes",
        )
    if item_type == "mcp_tool_call":
        tool = item.get("tool") or item.get("name")
        label = (
            tool
            if type(tool) is str
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tool)
            else "external tool"
        )
        return (
            "tool",
            f"Tool completed: {label}"
            if completed
            else f"Calling tool: {label}",
        )
    if item_type == "web_search":
        return (
            "search",
            "Web search completed"
            if completed
            else "Searching supporting documentation",
        )
    if item_type in {"todo_list", "plan", "plan_update"}:
        return "plan", "Analysis plan updated"
    if item_type == "reasoning":
        return (
            "reasoning",
            "Evidence evaluation completed"
            if completed
            else "Evaluating repository evidence",
        )
    if item_type == "agent_message" and completed:
        return "message", _safe_agent_activity(
            item.get("text"), secrets_to_remove
        )
    return None


def _final_agent_message(json_lines: str) -> str:
    final = ""
    for line in json_lines.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if type(event) is not dict or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            type(item) is dict
            and item.get("type") == "agent_message"
            and type(item.get("text")) is str
        ):
            final = item["text"]
    if not final:
        raise CodexOutputError("Codex returned invalid structured output")
    return final


def _codex_session_id(json_lines: str) -> str | None:
    """Return the validated Codex session id announced by a JSONL stream."""

    for line in json_lines.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            type(event) is dict
            and event.get("type") == "thread.started"
            and type(event.get("thread_id")) is str
            and _CODEX_SESSION_ID.fullmatch(event["thread_id"])
        ):
            return event["thread_id"].lower()
    return None


def _is_priority_failure(error: BaseException) -> bool:
    return isinstance(error, MemoryError) or not isinstance(error, Exception)


if os.name == "nt":  # pragma: no cover - definitions are exercised on Windows
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD


class _ProcessTreeGuard:
    """Own a Windows job when available; POSIX uses the process session."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.handle: Any = None
        if os.name != "nt":
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "could not create process job")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not _kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        ):
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise OSError(error, "could not configure process job")
        if not _kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise OSError(error, "could not assign process job")
        self.handle = handle

    def terminate(self) -> None:
        if os.name == "nt":
            if self.handle:
                _kernel32.TerminateJobObject(self.handle, 1)
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            pass

    def force_kill(self) -> None:
        if os.name == "nt":
            self.terminate()
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except OSError:
            pass

    def close(self) -> None:
        if os.name == "nt" and self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def _resume_suspended_process(process: subprocess.Popen[bytes]) -> None:
    """Resume the sole initial Windows thread after job assignment."""

    if os.name != "nt":
        return
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "could not enumerate process threads")
    thread_handle: Any = None
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == process.pid:
                thread_handle = _kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                break
            found = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if not thread_handle:
            raise OSError(ctypes.get_last_error(), "could not open suspended process thread")
        previous_suspend_count = _kernel32.ResumeThread(thread_handle)
        if previous_suspend_count == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "could not resume isolated process")
        if previous_suspend_count != 1:
            raise OSError("isolated process had an unexpected suspend count")
    finally:
        if thread_handle:
            _kernel32.CloseHandle(thread_handle)
        _kernel32.CloseHandle(snapshot)


def _start_isolated_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    pipe_stdin: bool = False,
    merge_stderr: bool = False,
) -> tuple[subprocess.Popen[bytes], _ProcessTreeGuard]:
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if pipe_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except OSError as error:
        raise CodexProcessStartError("Codex process could not be started") from error

    tree: _ProcessTreeGuard | None = None
    try:
        tree = _ProcessTreeGuard(process)
        _resume_suspended_process(process)
        return process, tree
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        try:
            if tree is not None:
                tree.terminate()
            else:
                process.kill()
        except BaseException as cleanup:
            cleanup_errors.append(cleanup)
        try:
            process.wait(timeout=2)
        except BaseException as cleanup:
            cleanup_errors.append(cleanup)
            try:
                process.kill()
            except BaseException as nested:
                cleanup_errors.append(nested)
            try:
                process.wait(timeout=2)
            except BaseException as nested:
                cleanup_errors.append(nested)
        if tree is not None:
            try:
                tree.close()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if _is_priority_failure(error):
            raise
        for cleanup in cleanup_errors:
            if _is_priority_failure(cleanup):
                raise cleanup
        if isinstance(error, OSError):
            raise CodexExecutionError(
                "Codex process could not be isolated"
            ) from error
        raise


def _terminate(process: subprocess.Popen[bytes], tree: _ProcessTreeGuard) -> None:
    failures: list[BaseException] = []
    try:
        tree.terminate()
    except BaseException as error:
        failures.append(error)
    try:
        if process.poll() is None:
            process.wait(timeout=2)
    except BaseException as error:
        failures.append(error)
        try:
            tree.force_kill()
        except BaseException as nested:
            failures.append(nested)
        try:
            if process.poll() is None:
                process.wait(timeout=2)
        except BaseException as nested:
            failures.append(nested)
    for failure in failures:
        if _is_priority_failure(failure):
            raise failure


def _bounded_subprocess(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_output_bytes: int,
    stdin: bytes | None = None,
    on_output_line: Callable[[str, str], None] | None = None,
    retain_output_tail: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Capture bounded output, optionally retaining only a live stream's tail."""

    if not _is_positive_finite_number(timeout):
        raise ValueError("timeout must be finite and positive")
    if stdin is not None and not isinstance(stdin, bytes):
        raise TypeError("stdin must be bytes or None")
    if type(retain_output_tail) is not bool:
        raise TypeError("retain_output_tail must be bool")
    process, tree = _start_isolated_process(
        command, cwd=cwd, env=env, pipe_stdin=stdin is not None
    )

    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    captured: dict[str, int] = {"stdout": 0, "stderr": 0}
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def feed_stdin() -> None:
        if stdin is None:
            return
        assert process.stdin is not None
        try:
            process.stdin.write(stdin)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    def drain(name: str, stream: Any) -> None:
        nonlocal total
        pending = b""
        try:
            descriptor = stream.fileno()
            while data := os.read(descriptor, 64 * 1024):
                with lock:
                    total += len(data)
                    if total > max_output_bytes and not retain_output_tail:
                        overflow.set()
                        return
                    chunks[name].append(data)
                    captured[name] += len(data)
                    if retain_output_tail:
                        while captured[name] > max_output_bytes and chunks[name]:
                            excess = captured[name] - max_output_bytes
                            first = chunks[name][0]
                            if len(first) <= excess:
                                captured[name] -= len(first)
                                chunks[name].pop(0)
                            else:
                                chunks[name][0] = first[excess:]
                                captured[name] -= excess
                if on_output_line is not None:
                    pending += data
                    if b"\n" not in pending and len(pending) > max_output_bytes:
                        pending = pending[-max_output_bytes:]
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        try:
                            on_output_line(name, line.rstrip(b"\r").decode("utf-8", "strict"))
                        except Exception:
                            # Activity rendering is best-effort and must never alter execution.
                            continue
        finally:
            if on_output_line is not None and pending:
                try:
                    on_output_line(name, pending.rstrip(b"\r").decode("utf-8", "strict"))
                except Exception:
                    pass
            stream.close()

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    stdin_thread = threading.Thread(target=feed_stdin, daemon=True)
    for thread in threads:
        thread.start()
    stdin_thread.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if overflow.is_set():
                _terminate(process, tree)
                raise CodexOutputError("Codex output limit exceeded")
            if time.monotonic() >= deadline:
                _terminate(process, tree)
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.02)
        for thread in threads:
            thread.join(timeout=0.05)
        if any(thread.is_alive() for thread in threads):
            _terminate(process, tree)
        for thread in threads:
            thread.join(timeout=2)
        stdin_thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise CodexExecutionError("Codex process output did not close")
        if stdin_thread.is_alive():
            _terminate(process, tree)
            stdin_thread.join(timeout=2)
            if stdin_thread.is_alive():
                raise CodexExecutionError("Codex process input did not close")
        if overflow.is_set() and not retain_output_tail:
            raise CodexOutputError("Codex output limit exceeded")
        try:
            stdout = b"".join(chunks["stdout"]).decode("utf-8", "strict")
            stderr = b"".join(chunks["stderr"]).decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise CodexOutputError("Codex returned invalid structured output") from error
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            _terminate(process, tree)
        tree.close()
        for thread in threads:
            thread.join(timeout=2)
        stdin_thread.join(timeout=2)


def _safe_environment(
    source: Mapping[str, str], isolation_root: Path, codex_home: Path | None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    safe: dict[str, str] = {}
    removed_values: list[str] = []
    for key, value in source.items():
        folded = key.casefold()
        sensitive = any(token in folded for token in _SECRET_ENV_TOKENS)
        if sensitive and len(value) >= 6:
            removed_values.append(value)
        if folded not in _ALLOWED_ENV:
            continue
        if folded in {"http_proxy", "https_proxy", "openai_base_url"}:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                for credential in (parsed.username, parsed.password):
                    if credential is not None and len(unquote(credential)) >= 6:
                        removed_values.append(unquote(credential))
                continue
        safe[key] = value
    locations = {
        "HOME": isolation_root / "home",
        "XDG_CONFIG_HOME": isolation_root / "xdg-config",
        "XDG_CACHE_HOME": isolation_root / "xdg-cache",
    }
    if os.name == "nt":
        locations.update({
            "USERPROFILE": isolation_root / "home",
            "APPDATA": isolation_root / "appdata",
            "LOCALAPPDATA": isolation_root / "localappdata",
        })
    for path in set(locations.values()):
        path.mkdir(exist_ok=True)
        if _is_reparse_or_link(path) or not path.is_dir():
            raise UnsafeCodexRunError("Codex environment directory is unsafe")
    empty_git_config = isolation_root / "empty.gitconfig"
    empty_git_config.write_bytes(b"")
    safe.update({name: str(path) for name, path in locations.items()})
    safe.update({
        "GIT_CONFIG_GLOBAL": str(empty_git_config),
        "GIT_CONFIG_SYSTEM": str(empty_git_config),
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
    })
    if os.name == "nt":
        # Windows PowerShell otherwise derives this cache location from an
        # incomplete/non-standard profile environment and can create
        # ``Microsoft/Windows/PowerShell/ModuleAnalysisCache`` in the current
        # repository. Keep the interpreter cache in the per-run directory.
        safe["PSModuleAnalysisCachePath"] = str(
            isolation_root / "powershell-module-analysis-cache"
        )
    if codex_home is not None:
        safe["CODEX_HOME"] = str(codex_home)
    return safe, tuple(removed_values)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _path_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    if not allowed_paths:
        return True
    return any(path == allowed or path.startswith(f"{allowed}/") for allowed in allowed_paths)


def _contains_secret(value: Any, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secrets) or _contains_secret(nested, secrets)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_secret(nested, secrets) for nested in value)
    return False


_WRITE_VERBS = {
    "create", "update", "delete", "post", "put", "patch", "transition",
    "assign", "close", "commit", "push", "merge", "reopen", "review",
}
_GIT_GLOBAL_VALUE_OPTIONS = {
    "-c", "-C", "--config-env", "--exec-path", "--git-dir", "--namespace",
    "--super-prefix", "--work-tree",
}


def _executable_name(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name[:-4] if name.endswith(".exe") else name


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n(){}!")
    # POSIX shlex treats every backslash as an escape character, including the
    # separators in an unquoted Windows executable path.  That turns e.g.
    # ``D:\\Python311\\python.exe`` into ``D:Python311python.exe`` and makes an
    # otherwise allow-listed ``python -m pytest`` command look like an unknown
    # executable.  Quotes still delimit arguments with escaping disabled, while
    # Windows path separators remain available to ``_executable_name``.
    lexer.escape = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(character in ";&|\n(){}!" for character in token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _option_value(args: list[str], index: int) -> tuple[str | None, int]:
    token = args[index]
    if "=" in token:
        return token.split("=", 1)[1], index + 1
    if index + 1 < len(args):
        return args[index + 1], index + 2
    return None, index + 1


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        option = token.split("=", 1)[0]
        if option in _GIT_GLOBAL_VALUE_OPTIONS:
            _, index = _option_value(args, index)
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return None, []
    return args[index].casefold(), args[index + 1 :]


def _git_uses_dynamic_alias(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-c":
            value, index = _option_value(args, index)
            if value and value.casefold().startswith("alias."):
                return True
            continue
        if token == "--config-env":
            value, index = _option_value(args, index)
            if value and value.casefold().startswith("alias."):
                return True
            continue
        if token.startswith("-calias.") or token.casefold().startswith(
            "--config-env=alias."
        ):
            return True
        index += 1
    return False


def _has_dynamic_token(args: list[str]) -> bool:
    return any(
        token.startswith("$") or "$(" in token or "`" in token for token in args
    )


def _next_non_option(args: list[str]) -> str | None:
    return next((value.casefold() for value in args if not value.startswith("-")), None)


def _curl_writes(args: list[str]) -> bool:
    safe_long_flags = {
        "--compressed", "--fail", "--get", "--head", "--include",
        "--location", "--show-error", "--silent",
    }
    safe_long_value_options = {
        "--connect-timeout", "--max-time", "--retry", "--retry-delay",
        "--user-agent",
    }
    index = 0
    while index < len(args):
        token = args[index]
        folded = token.casefold()
        option = folded.split("=", 1)[0]
        if token == "-X" or option == "--request":
            value, index = _option_value(args, index)
            if value is None or value.casefold() not in {"get", "head"}:
                return True
            continue
        if token.startswith("-X") and len(token) > 2:
            if token[2:].casefold() not in {"get", "head"}:
                return True
            index += 1
            continue
        if (
            token.startswith(("-d", "-F", "-T"))
            or option.startswith("--data")
            or option.startswith("--form")
            or option in {"--upload-file", "--json"}
        ):
            return True
        if token.startswith("-") and not token.startswith("--"):
            if all(character in "fsSLIG" for character in token[1:]):
                index += 1
                continue
            return True
        if option in safe_long_flags:
            index += 1
            continue
        if option in safe_long_value_options:
            _, index = _option_value(args, index)
            continue
        if token.startswith("--"):
            return True
        index += 1
    return False


def _gh_writes(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        token = args[index]
        option = token.split("=", 1)[0]
        if option in {"-R", "--repo", "--hostname"}:
            _, index = _option_value(args, index)
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    subcommand = args[index].casefold() if index < len(args) else None
    remainder = args[index + 1 :] if index < len(args) else []
    if subcommand == "pr":
        action = _next_non_option(remainder)
        return action not in {"view", "status", "list", "checks", "diff"}
    if subcommand != "api":
        read_actions = {
            "repo": {"view", "list"},
            "issue": {"view", "list", "status"},
            "run": {"view", "list", "watch"},
            "workflow": {"view", "list"},
            "release": {"view", "list", "download"},
            "label": {"list"},
            "auth": {"status"},
            "search": {"code", "commits", "issues", "prs", "repos"},
        }
        if subcommand == "status":
            return False
        return (
            subcommand not in read_actions
            or _next_non_option(remainder) not in read_actions[subcommand]
        )
    explicit_get = False
    for index, token in enumerate(remainder):
        option = token.casefold().split("=", 1)[0]
        if option in {"-f", "-F", "--field", "--raw-field", "--input"}:
            return True
        if option in {"-x", "--method"}:
            value, _ = _option_value(remainder, index)
            if value is None or value.casefold() != "get":
                return True
            explicit_get = True
    return not explicit_get


def _contains_write_verb(args: list[str]) -> bool:
    words = re.findall(r"[A-Za-z]+", " ".join(args).casefold())
    return any(word in _WRITE_VERBS for word in words)


def _is_safe_python_inline_compile(args: list[str]) -> bool:
    """Allow only compile(open(test).read(), same_test, 'exec') as inline Python.

    Inline interpreters are otherwise rejected because they can hide arbitrary
    side effects.  This one expression is a read-only syntax check occasionally
    emitted by Codex when ``py_compile`` is unavailable or not selected.
    """

    if len(args) != 2 or args[0] != "-c":
        return False
    try:
        module = ast.parse(args[1], mode="exec")
    except (SyntaxError, TypeError, ValueError):
        return False
    if len(module.body) != 1 or not isinstance(module.body[0], ast.Expr):
        return False
    compile_call = module.body[0].value
    if (
        not isinstance(compile_call, ast.Call)
        or not isinstance(compile_call.func, ast.Name)
        or compile_call.func.id != "compile"
        or compile_call.keywords
        or len(compile_call.args) != 3
    ):
        return False
    source, filename, mode = compile_call.args
    if (
        not isinstance(source, ast.Call)
        or source.args
        or source.keywords
        or not isinstance(source.func, ast.Attribute)
        or source.func.attr != "read"
    ):
        return False
    open_call = source.func.value
    if (
        not isinstance(open_call, ast.Call)
        or not isinstance(open_call.func, ast.Name)
        or open_call.func.id != "open"
        or len(open_call.args) != 1
        or len(open_call.keywords) != 1
        or open_call.keywords[0].arg != "encoding"
    ):
        return False
    path_node = open_call.args[0]
    encoding_node = open_call.keywords[0].value
    if not (
        isinstance(path_node, ast.Constant)
        and type(path_node.value) is str
        and isinstance(filename, ast.Constant)
        and type(filename.value) is str
        and path_node.value == filename.value
        and isinstance(encoding_node, ast.Constant)
        and type(encoding_node.value) is str
        and encoding_node.value.casefold().replace("_", "-") == "utf-8"
        and isinstance(mode, ast.Constant)
        and mode.value == "exec"
    ):
        return False
    path = path_node.value
    parsed = PurePosixPath(path)
    return (
        parsed.as_posix() == path
        and not parsed.is_absolute()
        and len(parsed.parts) >= 2
        and parsed.parts[0].casefold() == "tests"
        and all(part not in {"", ".", ".."} for part in parsed.parts)
        and parsed.suffix.casefold() == ".py"
    )


def _segment_writes(tokens: list[str], *, depth: int = 0) -> bool:
    if not tokens or depth > 4:
        return depth > 4
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    while tokens and tokens[0].casefold() in {
        "if", "then", "elif", "else", "while", "until", "do",
    }:
        tokens = tokens[1:]
    if tokens and tokens[0].casefold() in {"fi", "done", "esac"}:
        return False
    if not tokens:
        return False
    executable = _executable_name(tokens[0])
    args = tokens[1:]
    if executable == "env":
        while args and (args[0].startswith("-") or "=" in args[0]):
            args = args[1:]
        return _segment_writes(args, depth=depth + 1)
    if executable in {"command", "nohup"}:
        while args and (args[0] == "--" or args[0].startswith("-")):
            args = args[1:]
        return _segment_writes(args, depth=depth + 1)
    if executable == "sudo":
        value_options = {
            "-u", "--user", "-g", "--group", "-h", "--host", "-p",
            "--prompt", "-C", "--chdir", "-T", "--command-timeout",
        }
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--":
                index += 1
                break
            option = token.split("=", 1)[0]
            if option in value_options:
                _, index = _option_value(args, index)
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        return _segment_writes(args[index:], depth=depth + 1)
    if executable == "nice":
        index = 0
        while index < len(args):
            token = args[index]
            if token in {"-n", "--adjustment"}:
                _, index = _option_value(args, index)
                continue
            if token.startswith("--adjustment=") or re.fullmatch(r"-[0-9]+", token):
                index += 1
                continue
            break
        return _segment_writes(args[index:], depth=depth + 1)
    if executable in {"timeout", "gtimeout"}:
        index = 0
        while index < len(args):
            token = args[index]
            option = token.split("=", 1)[0]
            if option in {"-k", "--kill-after", "-s", "--signal"}:
                _, index = _option_value(args, index)
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        if index >= len(args):
            return True
        return _segment_writes(args[index + 1 :], depth=depth + 1)
    if executable in {"sh", "bash", "zsh", "dash", "ksh"}:
        indexes = [
            index
            for index, value in enumerate(args)
            if value.startswith("-")
            and not value.startswith("--")
            and "c" in value.lstrip("-")
        ]
        if not indexes:
            return True
        index = indexes[0]
        return index + 1 >= len(args) or _is_forbidden_command(
            args[index + 1], depth=depth + 1
        )
    if executable == "cmd":
        indexes = [i for i, value in enumerate(args) if value.casefold() in {"/c", "/k"}]
        return not indexes or _is_forbidden_command(
            " ".join(args[indexes[0] + 1 :]), depth=depth + 1
        )
    if executable in {"powershell", "pwsh"}:
        if any(
            value.casefold() in {
                "-e", "-ec", "-enc", "-enco", "-encodedcommand",
                "-f", "-fi", "-fil", "-file",
            }
            for value in args
        ):
            return True
        indexes = [i for i, value in enumerate(args) if value.casefold() in {"-c", "-command"}]
        return not indexes or _is_forbidden_command(
            " ".join(args[indexes[0] + 1 :]), depth=depth + 1
        )
    if executable == "eval":
        return not args or _is_forbidden_command(" ".join(args), depth=depth + 1)
    if executable == "git":
        if _has_dynamic_token(args) or _git_uses_dynamic_alias(args):
            return True
        subcommand, remainder = _git_subcommand(args)
        if subcommand == "remote":
            action = _next_non_option(remainder)
            return action not in {None, "get-url"}
        read_subcommands = {
            "status", "diff", "log", "show", "rev-parse", "grep", "ls-files",
            "ls-tree", "cat-file", "check-ignore", "check-attr", "merge-base",
            "describe", "name-rev", "shortlog", "blame", "version", "help",
            "whatchanged", "for-each-ref", "show-ref",
        }
        if subcommand == "config":
            return not any(
                value.split("=", 1)[0]
                in {
                    "--get", "--get-all", "--get-regexp", "--list",
                    "--show-origin", "--show-scope",
                }
                for value in remainder
            )
        return subcommand not in read_subcommands
    if executable.startswith("git-remote-") or executable in {"git-send-pack", "git-receive-pack"}:
        return True
    if executable == "gh":
        if _has_dynamic_token(args):
            return True
        return _gh_writes(args)
    if executable == "curl":
        if _has_dynamic_token(args):
            return True
        return _curl_writes(args)
    if executable in {"http", "https"}:
        method = _next_non_option(args)
        return (
            method not in {"get", "head"}
            or _has_dynamic_token(args)
            or any("=" in value and not value.startswith("http") for value in args)
        )
    if executable in {"invoke-webrequest", "invoke-restmethod"}:
        for index, token in enumerate(args):
            if token.casefold().split("=", 1)[0] == "-method":
                value, _ = _option_value(args, index)
                return value is None or value.casefold() in {"post", "put", "patch", "delete"}
        return False
    if executable in {"python", "python3", "py", "node", "perl", "ruby"}:
        dynamic_flags = (
            {"-c"}
            if executable in {"python", "python3", "py"}
            else {"-e", "--eval"}
        )
        if any(value.split("=", 1)[0] in dynamic_flags for value in args):
            return not (
                executable in {"python", "python3", "py"}
                and _is_safe_python_inline_compile(args)
            )
        if any(value.casefold() in {"--version", "-v"} for value in args):
            return False
        if executable in {"node", "perl", "ruby"}:
            return True
        try:
            module_index = args.index("-m")
        except ValueError:
            return True
        return (
            module_index + 1 >= len(args)
            or args[module_index + 1].casefold()
            not in {"pytest", "unittest", "compileall", "py_compile"}
        )
    if "ones" in executable:
        if _has_dynamic_token(args):
            return True
        words = {
            word.casefold()
            for value in args
            for word in re.findall(r"[A-Za-z]+", value)
        }
        return (
            bool(words & _WRITE_VERBS)
            or not bool(words & {"get", "list", "show", "search", "query", "read"})
        )
    if executable == "uv":
        if not args or args[0].casefold() != "run":
            return not (
                len(args) >= 2
                and args[0].casefold() == "lock"
                and "--check" in args[1:]
            )
        nested = args[1:]
        while nested and nested[0] in {"--frozen", "--locked", "--offline", "--no-sync"}:
            nested = nested[1:]
        return _segment_writes(nested, depth=depth + 1)
    if executable == "make":
        if any(value.startswith("-") for value in args):
            return True
        targets = [value.casefold() for value in args if not value.startswith("-")]
        return not targets or any(
            target not in {"test", "check", "lint"} for target in targets
        )
    if executable == "ruff":
        return _next_non_option(args) != "check"
    if executable == "get-filehash":
        # Hashing a literal file is read-only; do not admit script expressions,
        # abbreviated switches, streams, or arbitrary PowerShell parameters.
        if _has_dynamic_token(args):
            return True
        index = 0
        paths = 0
        while index < len(args):
            option = args[index].casefold()
            if option == "-algorithm":
                if index + 1 >= len(args) or args[index + 1].casefold() not in {
                    "sha1", "sha256", "sha384", "sha512", "md5",
                }:
                    return True
                index += 2
            elif option in {"-path", "-literalpath"}:
                if index + 1 >= len(args) or args[index + 1].startswith("-"):
                    return True
                paths += 1
                index += 2
            elif option.startswith("-"):
                return True
            else:
                paths += 1
                index += 1
        return paths != 1
    if executable in {
        "pytest", "py.test", "mypy", "pyright", "rg", "ripgrep", "grep",
        "cat", "type", "get-content", "select-string", "findstr", "ls",
        "dir", "pwd", "get-childitem", "test-path", "true", "false",
    }:
        return False
    if executable.startswith("$"):
        return True
    return True


def _is_forbidden_command(command: str, *, depth: int = 0) -> bool:
    if ("$(" in command or "`" in command) and re.search(
        r"\b(?:git|gh|curl|ones)\b", command, re.IGNORECASE
    ):
        return True
    try:
        return any(_segment_writes(segment, depth=depth) for segment in _segments(command))
    except ValueError:
        return bool(re.search(r"\b(?:git|gh|curl|ones)\b", command, re.IGNORECASE))


def _is_reparse_or_link(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _environment_value(source: Mapping[str, str], name: str) -> str | None:
    folded_name = name.casefold()
    return next(
        (value for key, value in source.items() if key.casefold() == folded_name),
        None,
    )


def _resolve_codex_home(source: Mapping[str, str]) -> Path | None:
    """Resolve only Codex authentication state before isolating the child HOME."""

    explicit = _environment_value(source, "CODEX_HOME")
    is_explicit = explicit is not None
    if is_explicit:
        raw_path = explicit or ""
    else:
        has_environment_auth = any(
            bool(_environment_value(source, name))
            for name in ("CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY")
        )
        if has_environment_auth:
            return None
        parent_home = _environment_value(source, "USERPROFILE") or _environment_value(
            source, "HOME"
        )
        if not parent_home:
            return None
        raw_path = str(Path(parent_home) / ".codex")

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        )
    try:
        candidate.lstat()
    except FileNotFoundError as error:
        if not is_explicit:
            return None
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        ) from error
    except OSError as error:
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        ) from error
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir() or not resolved.is_absolute():
            raise UnsafeCodexRunError(
                "Codex authentication directory is unsafe or unavailable"
            )
        with os.scandir(resolved):
            pass
        return resolved
    except UnsafeCodexRunError:
        raise
    except OSError as error:
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        ) from error


def validate_codex_auth_source(source: Mapping[str, str]) -> Path | None:
    """Validate Codex authentication shape without reading credential content."""

    explicit_home = _environment_value(source, "CODEX_HOME")
    if explicit_home is None:
        configured_auth = tuple(
            value
            for name in ("CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY")
            if (value := _environment_value(source, name))
        )
        if configured_auth:
            if any(
                value != value.strip()
                or len(value) > 65536
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in value
                )
                for value in configured_auth
            ):
                raise UnsafeCodexRunError(
                    "Codex authentication source is unavailable"
                )
            return None

    codex_home = _resolve_codex_home(source)
    if codex_home is None:
        raise UnsafeCodexRunError("Codex authentication source is unavailable")
    auth_file = codex_home / "auth.json"
    try:
        metadata = auth_file.lstat()
        resolved = auth_file.resolve(strict=True)
        if (
            _is_reparse_or_link(auth_file)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > 1024 * 1024
            or resolved.parent != codex_home
        ):
            raise UnsafeCodexRunError("Codex authentication source is unavailable")
    except UnsafeCodexRunError:
        raise
    except OSError:
        raise UnsafeCodexRunError(
            "Codex authentication source is unavailable"
        ) from None
    return codex_home


@dataclass(slots=True)
class CodexRunner:
    run_root: Path
    repository: RepositoryGuard | WorktreeRepository
    command_executor: CommandExecutor = field(default=_bounded_subprocess, repr=False)
    command_resolver: Callable[[], CodexCommand] = field(
        default=resolve_codex_command, repr=False
    )
    environment_provider: Callable[[], Mapping[str, str]] = field(
        default=lambda: os.environ, repr=False
    )
    schema_path: Path = field(
        default_factory=lambda: Path(__file__).with_name("schemas") / "workflow-result.schema.json",
        init=False,
    )
    root_cause_schema_path: Path = field(
        default_factory=lambda: Path(__file__).with_name("schemas")
        / "root-cause-result.schema.json",
        init=False,
    )
    max_prompt_bytes: int = 1024 * 1024
    max_output_bytes: int = 10 * 1024 * 1024
    sandbox_mode_override: str | None = None
    _activity_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.run_root.is_absolute():
            raise ValueError("run_root must be absolute")
        canonical_root = self.run_root.resolve(strict=False)
        if os.path.normcase(str(canonical_root)) != os.path.normcase(str(self.run_root)):
            raise ValueError("run_root must be canonical")
        if not self.schema_path.is_absolute():
            self.schema_path = self.schema_path.resolve()
        if not self.root_cause_schema_path.is_absolute():
            self.root_cause_schema_path = self.root_cause_schema_path.resolve()
        for value in (self.max_prompt_bytes, self.max_output_bytes):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("Codex size limits must be positive integers")
        if not callable(self.environment_provider):
            raise ValueError("Codex environment provider is invalid")
        if not callable(self.command_resolver):
            raise ValueError("Codex command resolver is invalid")
        if self.sandbox_mode_override not in {None, "danger-full-access"}:
            raise ValueError("Codex sandbox override is invalid")
        try:
            for schema_path in (self.schema_path, self.root_cause_schema_path):
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                if _is_reparse_or_link(schema_path) or not schema_path.is_file():
                    raise ValueError("Codex output schema is not a regular file")
        except (OSError, ValueError) as error:
            raise ValueError("Codex output schema is unavailable or invalid") from error

    def run(
        self,
        prepared: PreparedWorktree,
        mapping: RepositoryMapping,
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
        allow_changes: bool = True,
        _root_cause_result: bool = False,
    ) -> CodexResult:
        if type(_root_cause_result) is not bool:
            raise UnsafeCodexRunError("Codex result profile is invalid")
        self.repository.assert_head_unchanged(prepared)
        read_only_baseline = self.repository.snapshot(prepared, mapping) if not allow_changes else None
        try:
            output, removed_secrets = self._invoke(
                run_id=run_id,
                prompt=prompt,
                cwd=prepared.path,
                sandbox=self.sandbox_mode_override
                or ("workspace-write" if allow_changes else "read-only"),
            timeout_seconds=timeout_seconds,
            skip_git_repo_check=False,
            additional_directories=(),
            output_schema=(
                self.root_cause_schema_path
                if _root_cause_result
                else self.schema_path
            ),
            )
        finally:
            self.repository.assert_head_unchanged(prepared)

        try:
            payload = self._validate_output(
                output, mapping, root_cause_result=_root_cause_result
            )
        except CodexOutputError as error:
            error.raw_output = output
            if _root_cause_result:
                self._store_pending_root_cause_output(run_id, output)
            self._record_validation_failure(run_id, error)
            raise
        snapshot_before_scan = self.repository.snapshot(prepared, mapping)
        if snapshot_before_scan.head_commit != prepared.head_commit:
            raise HeadChangedError("worktree HEAD changed")
        if _contains_secret(
            snapshot_before_scan.model_dump(mode="json"), removed_secrets
        ):
            raise CodexOutputError("Codex returned invalid structured output")
        sensitive_content_found = self.repository.contains_sensitive_content(
            prepared, mapping, removed_secrets
        )
        snapshot = self.repository.snapshot(prepared, mapping)
        snapshot_head_changed = snapshot.head_commit != prepared.head_commit
        snapshot_content_changed = snapshot.model_dump(
            mode="json"
        ) != snapshot_before_scan.model_dump(
            mode="json"
        )
        self.repository.assert_head_unchanged(prepared)
        if snapshot_head_changed:
            raise HeadChangedError("worktree HEAD changed")
        if snapshot_content_changed:
            raise CodexOutputError("Codex returned invalid structured output")
        if sensitive_content_found:
            raise CodexOutputError("Codex returned invalid structured output")
        if read_only_baseline is not None:
            if snapshot != read_only_baseline:
                raise UnsafeCodexRunError("read-only Codex stage modified repository evidence")
        # Git, not the model's recollection of this turn, owns the cumulative
        # inventory. Head, scope, secret and read-only checks above still apply.
        payload["changed_files"] = list(snapshot.changed_files)
        try:
            result = self._result_from_payload(payload)
        except Exception as error:
            wrapped = CodexOutputError(
                "Codex returned invalid structured output",
                validation_hint=_safe_validation_hint(error),
                raw_output=output,
            )
            wrapped.__cause__ = error
            if _root_cause_result:
                self._store_pending_root_cause_output(run_id, output)
            self._record_validation_failure(run_id, wrapped)
            raise wrapped
        self._record_validation_success(run_id)
        if _root_cause_result:
            self._record_root_cause_report(run_id, result, removed_secrets)
            self._clear_pending_root_cause_output(run_id)
        return result

    def run_root_cause(
        self,
        prepared: PreparedWorktree,
        mapping: RepositoryMapping,
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
        allow_changes: bool = False,
    ) -> CodexResult:
        """Run read-only root-cause analysis with stage-irrelevant fields discarded."""

        if allow_changes is not False:
            raise UnsafeCodexRunError("root-cause analysis must be read-only")

        return self.run(
            prepared,
            mapping,
            run_id=run_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            allow_changes=False,
            _root_cause_result=True,
        )

    def run_preflight(
        self,
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
    ) -> CodexResult:
        """Run source-only structured analysis without creating a worktree."""

        output, _ = self._invoke(
            run_id=run_id,
            prompt=prompt,
            cwd=None,
            sandbox=self.sandbox_mode_override or "read-only",
            timeout_seconds=timeout_seconds,
            skip_git_repo_check=True,
            additional_directories=(),
            output_schema=self.schema_path,
        )
        payload = self._validate_output(output, None)
        return self._result_from_payload(payload)

    def run_group(
        self,
        group: RepositoryGroupMapping,
        prepared: tuple[PreparedRepository, ...],
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
        allow_changes: bool = True,
        _root_cause_result: bool = False,
    ) -> CodexResult:
        if type(_root_cause_result) is not bool:
            raise UnsafeCodexRunError("Codex result profile is invalid")
        expected_keys = group.topological_keys()
        if tuple(item.repository_key for item in prepared) != expected_keys:
            raise UnsafeCodexRunError("prepared repositories do not match group topology")
        configured = {item.key: item for item in group.repositories}
        if any(item.mapping != configured[item.repository_key] for item in prepared):
            raise UnsafeCodexRunError("prepared repository mapping differs from group")
        parents = {item.prepared.path.parent.resolve(strict=True) for item in prepared}
        if len(parents) != 1:
            raise UnsafeCodexRunError("prepared repositories do not share one workspace")
        workspace = next(
            item.prepared.path
            for item in prepared
            if item.repository_key == group.primary_repository
        )
        additional_directories = tuple(
            item.prepared.path
            for item in prepared
            if item.repository_key != group.primary_repository
        )
        for item in prepared:
            self.repository.assert_head_unchanged(item.prepared)
        read_only_baseline = ({
            item.repository_key: self.repository.snapshot(item.prepared, item.mapping)
            for item in prepared
        } if not allow_changes else None)
        try:
            output, removed_secrets = self._invoke(
                run_id=run_id,
                prompt=prompt,
                cwd=workspace,
                sandbox=self.sandbox_mode_override
                or ("workspace-write" if allow_changes else "read-only"),
                timeout_seconds=timeout_seconds,
                skip_git_repo_check=False,
                additional_directories=additional_directories,
                output_schema=(
                    self.root_cause_schema_path
                    if _root_cause_result
                    else self.schema_path
                ),
            )
        finally:
            for item in prepared:
                self.repository.assert_head_unchanged(item.prepared)

        try:
            payload = self._validate_group_output(
                output, group, root_cause_result=_root_cause_result
            )
        except CodexOutputError as error:
            error.raw_output = output
            if _root_cause_result:
                self._store_pending_root_cause_output(run_id, output)
            self._record_validation_failure(run_id, error)
            raise
        before: dict[str, RepositorySnapshot] = {}
        sensitive = False
        for item in prepared:
            snapshot = self.repository.snapshot(item.prepared, item.mapping)
            if snapshot.head_commit != item.prepared.head_commit:
                raise HeadChangedError("worktree HEAD changed")
            before[item.repository_key] = snapshot
            if _contains_secret(snapshot.model_dump(mode="json"), removed_secrets):
                raise CodexOutputError("Codex returned invalid structured output")
            sensitive = self.repository.contains_sensitive_content(
                item.prepared, item.mapping, removed_secrets
            ) or sensitive

        after = {
            item.repository_key: self.repository.snapshot(item.prepared, item.mapping)
            for item in prepared
        }
        for item in prepared:
            self.repository.assert_head_unchanged(item.prepared)
        if any(
            after[key].head_commit != next(
                item.prepared.head_commit for item in prepared if item.repository_key == key
            )
            for key in after
        ):
            raise HeadChangedError("worktree HEAD changed")
        if before != after or sensitive:
            raise CodexOutputError("Codex returned invalid structured output")
        actual = tuple(
            (key, path)
            for key in expected_keys
            for path in after[key].changed_files
        )
        if read_only_baseline is not None:
            if after != read_only_baseline:
                raise UnsafeCodexRunError("read-only Codex stage modified repository evidence")
        # The verified worktrees own the cumulative inventory, not turn-local claims.
        payload["repository_changes"] = [
            {"repository_key": key, "path": path} for key, path in actual
        ]
        try:
            result = self._result_from_payload(payload)
        except Exception as error:
            wrapped = CodexOutputError(
                "Codex returned invalid structured output",
                validation_hint=_safe_validation_hint(error),
                raw_output=output,
            )
            wrapped.__cause__ = error
            if _root_cause_result:
                self._store_pending_root_cause_output(run_id, output)
            self._record_validation_failure(run_id, wrapped)
            raise wrapped
        self._record_validation_success(run_id)
        if _root_cause_result:
            self._record_root_cause_report(run_id, result, removed_secrets)
            self._clear_pending_root_cause_output(run_id)
        return result

    def repair_pending_root_cause_result(
        self,
        *,
        run_id: str,
    ) -> CodexResult | None:
        """Resume format-only recovery when a previous analysis report is available."""

        raw_output = self._read_pending_root_cause_output(run_id)
        if raw_output is None:
            activity = self.activity(run_id, limit=200)
            completed = any(
                message.startswith("AI analysis completed") for message in activity
            )
            has_report = any(
                message.startswith("Analysis result:") for message in activity
            )
            if not completed or not has_report:
                return None
            raw_output = "\n".join(activity)
        self.record_analysis_recovery(run_id)
        return self.repair_root_cause_result(
            run_id=run_id,
            raw_output=raw_output,
            validation_hint="workflow result contract",
        )

    def run_group_root_cause(
        self,
        group: RepositoryGroupMapping,
        prepared: tuple[PreparedRepository, ...],
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
        allow_changes: bool = False,
    ) -> CodexResult:
        """Run group root-cause analysis with stage-irrelevant fields discarded."""

        if allow_changes is not False:
            raise UnsafeCodexRunError("root-cause analysis must be read-only")

        return self.run_group(
            group,
            prepared,
            run_id=run_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            allow_changes=False,
            _root_cause_result=True,
        )

    def repair_root_cause_result(
        self,
        *,
        run_id: str,
        raw_output: str,
        validation_hint: str = "",
        timeout_seconds: float = 300,
    ) -> CodexResult:
        """Validate an already completed analysis without starting Codex again."""

        if (
            type(raw_output) is not str
            or not raw_output.strip()
            or len(raw_output.encode("utf-8", "strict")) > self.max_prompt_bytes // 2
            or not _is_positive_finite_number(timeout_seconds)
        ):
            raise CodexOutputError("Codex result format repair is unavailable")
        try:
            normalized = self._validate_output(
                raw_output,
                None,
                root_cause_result=True,
            )
            result = self._result_from_payload(normalized)
        except Exception as error:
            wrapped = CodexOutputError(
                "Codex returned invalid structured output",
                validation_hint=(
                    _safe_validation_hint(error)
                    or validation_hint
                    or "workflow result contract"
                ),
                raw_output=raw_output,
            )
            wrapped.__cause__ = error
            self._record_validation_failure(run_id, wrapped)
            raise wrapped
        self._record_validation_success(run_id)
        self._record_root_cause_report(run_id, result, ())
        self._clear_pending_root_cause_output(run_id)
        return result

    def activity(self, run_id: str, *, limit: int = 40) -> tuple[str, ...]:
        """Return the bounded, sanitized observable activity for one Codex run."""

        if (
            not _RUN_ID.fullmatch(run_id)
            or run_id in {".", ".."}
            or type(limit) is not int
            or isinstance(limit, bool)
            or limit <= 0
            or limit > 200
        ):
            return ()
        path = self.run_root / run_id / _ACTIVITY_FILE
        try:
            with self._activity_lock:
                if _is_reparse_or_link(path) or not path.is_file():
                    return ()
                data = path.read_bytes()
            if len(data) > _MAX_ACTIVITY_BYTES:
                return ()
            messages: list[str] = []
            for raw_line in data.splitlines():
                record = json.loads(raw_line.decode("utf-8", "strict"))
                if (
                    type(record) is dict
                    and type(record.get("message")) is str
                    and 0 < len(record["message"]) <= 512
                ):
                    messages.append(record["message"])
            return tuple(messages[-limit:])
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return ()

    def _record_activity(self, run_directory: Path, kind: str, message: str) -> None:
        if (
            re.fullmatch(r"[a-z]{1,24}", kind) is None
            or type(message) is not str
            or not message
            or len(message) > 512
        ):
            return
        path = run_directory / _ACTIVITY_FILE
        payload = (
            json.dumps(
                {
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "kind": kind,
                    "message": message,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", "strict")
            + b"\n"
        )
        try:
            with self._activity_lock:
                if path.exists() and (
                    _is_reparse_or_link(path)
                    or not path.is_file()
                    or path.stat().st_size + len(payload) > _MAX_ACTIVITY_BYTES
                ):
                    return
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_APPEND
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(path, flags, 0o600)
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        return
                    os.write(descriptor, payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, UnicodeError, TypeError, ValueError):
            return

    def _record_validation_success(self, run_id: str) -> None:
        self._record_activity(
            self.run_root / run_id,
            "validation",
            "Structured analysis result validated",
        )

    def _record_root_cause_report(
        self,
        run_id: str,
        result: CodexResult,
        secrets_to_remove: tuple[str, ...],
    ) -> None:
        """Append the actionable part of a validated report to the live activity."""

        def record(prefix: str, value: str) -> None:
            text = " ".join(value.split())
            for secret in secrets_to_remove:
                if secret:
                    text = text.replace(secret, "[redacted]")
            text = _SENSITIVE_COMMAND_VALUE.sub(r"\1=[redacted]", text)
            text = _BEARER_VALUE.sub("Bearer [redacted]", text)
            if not text or any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                for character in text
            ):
                return
            available = 512 - len(prefix)
            bounded = (
                text
                if len(text) <= available
                else text[: max(0, available - 3)] + "..."
            )
            self._record_activity(self.run_root / run_id, "report", prefix + bounded)

        fixes: list[str] = []
        validations: list[str] = []
        for evidence in result.root_cause_evidence:
            record("Verified root cause: ", evidence.mechanism)
            fixes.extend(evidence.fix_steps)
            validations.append(evidence.reproduction_command)
        for index, fix in enumerate(dict.fromkeys(fixes), 1):
            record(f"Recommended fix {index}: ", fix)
        for command in dict.fromkeys(validations):
            record("Planned post-repair validation: ", command)
        if not fixes:
            for index, suggestion in enumerate(result.investigation_suggestions, 1):
                record(f"Next investigation {index}: ", suggestion)

    def _record_validation_failure(
        self, run_id: str, error: CodexOutputError
    ) -> None:
        if error.validation_hint:
            self._record_activity(
                self.run_root / run_id,
                "validation",
                f"Structured result needs correction: {error.validation_hint}",
            )
            return
        cause = error.__cause__
        detail = str(cause) if cause is not None else ""
        if "group output must use repository change claims" in detail:
            message = "Structured result rejected: unqualified group change claims"
        elif "unsafe repository change claim" in detail:
            message = "Structured result rejected: invalid repository-qualified path"
        elif cause is not None:
            message = "Structured result rejected: output schema mismatch"
        else:
            message = "Structured result rejected by workflow validation"
        self._record_activity(self.run_root / run_id, "validation", message)

    def record_analysis_recovery(self, run_id: str) -> None:
        """Record local recovery without implying that AI was restarted."""

        if _RUN_ID.fullmatch(run_id) and run_id not in {".", ".."}:
            self._record_activity(
                self.run_root / run_id,
                "validation",
                "Recovering completed analysis result locally",
            )

    def _pending_root_cause_path(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise UnsafeCodexRunError("run_id is not a safe path segment")
        return self._prepare_run_directory(run_id) / _PENDING_ROOT_CAUSE_FILE

    def _session_path(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise UnsafeCodexRunError("run_id is not a safe path segment")
        return self._prepare_run_directory(run_id) / _CODEX_SESSION_FILE

    def _read_session_id(self, run_id: str) -> str | None:
        path = self._session_path(run_id)
        try:
            metadata = path.lstat()
            if (
                _is_reparse_or_link(path)
                or not stat.S_ISREG(metadata.st_mode)
                or not 36 <= metadata.st_size <= 37
            ):
                raise UnsafeCodexRunError("Codex session state is unsafe")
            session_id = path.read_text(encoding="ascii", errors="strict").strip()
            if _CODEX_SESSION_ID.fullmatch(session_id) is None:
                raise UnsafeCodexRunError("Codex session state is unsafe")
            return session_id.lower()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as error:
            raise UnsafeCodexRunError("Codex session state is unsafe") from error

    def _store_session_id(self, run_id: str, session_id: str) -> None:
        if _CODEX_SESSION_ID.fullmatch(session_id) is None:
            raise UnsafeCodexRunError("Codex returned an invalid session id")
        self._write_prompt(
            self._session_path(run_id),
            (session_id.lower() + "\n").encode("ascii", "strict"),
        )

    def _store_pending_root_cause_output(self, run_id: str, output: str) -> None:
        data = output.encode("utf-8", "strict")
        if not data or len(data) > self.max_prompt_bytes // 2:
            return
        self._write_prompt(self._pending_root_cause_path(run_id), data)

    def _read_pending_root_cause_output(self, run_id: str) -> str | None:
        path = self._pending_root_cause_path(run_id)
        try:
            metadata = path.lstat()
            if (
                _is_reparse_or_link(path)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > self.max_prompt_bytes // 2
            ):
                raise UnsafeCodexRunError("pending Codex result is unsafe")
            return path.read_text(encoding="utf-8", errors="strict")
        except FileNotFoundError:
            return None
        except UnicodeError as error:
            raise UnsafeCodexRunError("pending Codex result is unsafe") from error

    def _clear_pending_root_cause_output(self, run_id: str) -> None:
        path = self._pending_root_cause_path(run_id)
        try:
            if path.exists() and (_is_reparse_or_link(path) or not path.is_file()):
                raise UnsafeCodexRunError("pending Codex result is unsafe")
            path.unlink(missing_ok=True)
        except OSError as error:
            raise UnsafeCodexRunError("pending Codex result could not be cleared") from error

    def _invoke(
        self,
        *,
        run_id: str,
        prompt: str,
        cwd: Path | None,
        sandbox: str,
        timeout_seconds: float,
        skip_git_repo_check: bool,
        additional_directories: tuple[Path, ...],
        output_schema: Path,
    ) -> tuple[str, tuple[str, ...]]:
        if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise UnsafeCodexRunError("run_id is not a safe path segment")
        if not _is_positive_finite_number(timeout_seconds):
            raise UnsafeCodexRunError("timeout must be finite and positive")
        if sandbox not in {"workspace-write", "read-only", "danger-full-access"}:
            raise UnsafeCodexRunError("Codex sandbox mode is invalid")
        if type(skip_git_repo_check) is not bool:
            raise UnsafeCodexRunError("Codex Git repository policy is invalid")
        if type(additional_directories) is not tuple or any(
            not isinstance(path, Path) or not path.is_absolute()
            for path in additional_directories
        ):
            raise UnsafeCodexRunError("Codex additional directories are invalid")
        if (
            not isinstance(output_schema, Path)
            or output_schema not in {self.schema_path, self.root_cause_schema_path}
        ):
            raise UnsafeCodexRunError("Codex output schema selection is invalid")
        prompt_bytes = prompt.encode("utf-8", "strict")
        if not prompt.strip() or len(prompt_bytes) > self.max_prompt_bytes or "\x00" in prompt:
            raise UnsafeCodexRunError("prompt is empty, invalid, or exceeds its size limit")
        try:
            source = self.environment_provider()
            if not isinstance(source, Mapping) or any(
                type(key) is not str or type(value) is not str
                for key, value in source.items()
            ):
                raise TypeError
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise UnsafeCodexRunError("Codex environment is unavailable") from None
        codex_home = _resolve_codex_home(source)
        run_directory = self._prepare_run_directory(run_id)
        effective_cwd = cwd or run_directory
        try:
            canonical_additional = tuple(
                path.resolve(strict=True) for path in additional_directories
            )
        except OSError as error:
            raise UnsafeCodexRunError(
                "Codex additional directories are unavailable"
            ) from error
        if (
            len(set(canonical_additional)) != len(canonical_additional)
            or any(
                not path.is_dir()
                or _is_reparse_or_link(path)
                or path == effective_cwd.resolve(strict=True)
                for path in canonical_additional
            )
        ):
            raise UnsafeCodexRunError("Codex additional directories are invalid")
        isolation_root = self._prepare_isolation_directory(run_directory)
        existing_session_id = self._read_session_id(run_id)
        isolated_codex_home = self._prepare_isolated_codex_home(
            isolation_root,
            codex_home,
            existing_session_id=existing_session_id,
        )
        safe_env, removed_secrets = _safe_environment(
            source, isolation_root, isolated_codex_home
        )
        if any(secret in prompt for secret in removed_secrets):
            raise UnsafeCodexRunError("prompt contains credential material")
        self._write_prompt(run_directory / "codex-prompt.txt", prompt_bytes)
        self._record_activity(run_directory, "prepare", "Preparing verified Codex runtime")
        resolved_command = self.command_resolver()
        if type(resolved_command) is not CodexCommand:
            _raise_codex_executable_unavailable()
        execution_error: BaseException | None = None
        execution_traceback = None
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            if not resolved_command._is_attested():
                _raise_codex_executable_unavailable()
            verification_failed = False
            try:
                verify_locked_private_codex_for_execution(
                    resolved_command._lease,
                    cache_root=resolved_command._cache_root,
                )
            except BaseException as error:
                if _is_priority_failure(error) or not isinstance(error, OSError):
                    raise
                verification_failed = True
            if verification_failed:
                _raise_codex_executable_unavailable()
            stream_activity = self.command_executor is _bounded_subprocess
            existing_session_id = existing_session_id if stream_activity else None
            announced_session_id: str | None = None
            final_message_path = (
                run_directory / f".codex-final-{uuid.uuid4().hex}.json"
                if stream_activity
                else None
            )
            if existing_session_id is None:
                arguments = [
                    "exec", "--cd", str(effective_cwd), "--sandbox", sandbox,
                    "--output-schema", str(output_schema),
                ]
                if skip_git_repo_check:
                    arguments.append("--skip-git-repo-check")
                for path in canonical_additional:
                    arguments.extend(("--add-dir", str(path)))
            else:
                arguments = ["exec", "resume"]
                if sandbox == "danger-full-access":
                    arguments.append("--dangerously-bypass-approvals-and-sandbox")
                else:
                    arguments.extend(("-c", f'sandbox_mode="{sandbox}"'))
                if skip_git_repo_check:
                    arguments.append("--skip-git-repo-check")
                arguments.extend(("--output-schema", str(output_schema)))
            if stream_activity:
                assert final_message_path is not None
                arguments.extend(
                    ("--json", "--output-last-message", str(final_message_path))
                )
            if existing_session_id is not None:
                arguments.append(existing_session_id)
            arguments.append("-")
            command = resolved_command.argv(*arguments)
            try:
                if stream_activity:
                    def on_output_line(name: str, line: str) -> None:
                        nonlocal announced_session_id
                        if name != "stdout":
                            return
                        session_id = _codex_session_id(line)
                        if session_id is not None:
                            announced_session_id = session_id
                        activity = _codex_activity_from_event(line, removed_secrets)
                        if activity is not None:
                            self._record_activity(run_directory, *activity)

                    completed = _bounded_subprocess(
                        command,
                        cwd=effective_cwd,
                        env=safe_env,
                        timeout=float(timeout_seconds),
                        max_output_bytes=self.max_output_bytes,
                        stdin=prompt_bytes,
                        on_output_line=on_output_line,
                        retain_output_tail=True,
                    )
                else:
                    completed = self.command_executor(
                        command,
                        cwd=effective_cwd,
                        env=safe_env,
                        timeout=float(timeout_seconds),
                        max_output_bytes=self.max_output_bytes,
                        stdin=prompt_bytes,
                    )
            except subprocess.TimeoutExpired as error:
                raise CodexTimeoutError("Codex execution timed out") from error
            except CodexRunnerError:
                raise
            except Exception as error:
                raise CodexExecutionError(
                    "Codex process could not be executed"
                ) from error
        except BaseException as error:
            execution_error = error
            execution_traceback = error.__traceback__
        close_error: BaseException | None = None
        close_traceback = None
        try:
            resolved_command.close()
        except BaseException as error:
            close_error = error
            close_traceback = error.__traceback__
        if execution_error is not None:
            if _is_priority_failure(execution_error):
                raise execution_error.with_traceback(execution_traceback)
            if close_error is not None and _is_priority_failure(close_error):
                raise close_error.with_traceback(close_traceback)
            raise execution_error.with_traceback(execution_traceback)
        if close_error is not None:
            if _is_priority_failure(close_error):
                raise close_error.with_traceback(close_traceback)
            raise CodexExecutionError(
                "Codex process could not be executed"
            ) from None
        assert completed is not None
        if completed.returncode != 0:
            if final_message_path is not None:
                try:
                    final_message_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise CodexExecutionError("Codex exited unsuccessfully")
        if stream_activity:
            session_id = announced_session_id or _codex_session_id(completed.stdout)
            if session_id is None:
                session_id = existing_session_id
            if session_id is None or (
                existing_session_id is not None
                and session_id != existing_session_id
            ):
                raise CodexOutputError("Codex session continuity was not preserved")
            if existing_session_id is None:
                self._store_session_id(run_id, session_id)
        if stream_activity:
            assert final_message_path is not None
            try:
                metadata = final_message_path.lstat()
                if _is_reparse_or_link(final_message_path) or not stat.S_ISREG(
                    metadata.st_mode
                ):
                    raise CodexOutputError(
                        "Codex returned invalid structured output"
                    )
                if 0 < metadata.st_size <= self.max_output_bytes:
                    output = final_message_path.read_text(
                        encoding="utf-8", errors="strict"
                    )
                else:
                    output = _final_agent_message(completed.stdout)
            except FileNotFoundError:
                output = _final_agent_message(completed.stdout)
            except UnicodeError:
                output = _final_agent_message(completed.stdout)
            except OSError as error:
                raise CodexOutputError(
                    "Codex returned invalid structured output"
                ) from error
            finally:
                try:
                    final_message_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            output = completed.stdout
        if not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str):
            raise CodexOutputError("Codex returned invalid structured output")
        try:
            output_size = len(completed.stdout.encode("utf-8", "strict")) + len(
                completed.stderr.encode("utf-8", "strict")
            )
        except UnicodeEncodeError as error:
            raise CodexOutputError("Codex returned invalid structured output") from error
        if not stream_activity and output_size > self.max_output_bytes:
            raise CodexOutputError("Codex output limit exceeded")
        # The JSONL transport contains internal Codex events and tool output.  It is
        # never used as the workflow result (and activity derived from it is
        # separately redacted), so a credential-shaped value there must not turn a
        # valid authoritative final message into a bogus schema failure.  Keep the
        # fail-closed check on the final message that is parsed and persisted.
        if any(secret in output for secret in removed_secrets):
            raise CodexOutputError("Codex returned invalid structured output")
        return output, removed_secrets

    @staticmethod
    def _result_from_payload(payload: dict[str, Any]) -> CodexResult:
        now = datetime.now(timezone.utc)
        commands = tuple(
            CommandResult(
                command=item["command"], exit_code=item["exit_code"],
                summary=item["summary"], started_at=now, finished_at=now,
            )
            for item in payload["commands"]
        )
        return CodexResult(
            verification_needs=payload.get("verification_needs", ()),
            summary=payload["summary"], changed_files=tuple(payload["changed_files"]),
            repository_changes=tuple(
                RepositoryChangeClaim.model_validate(item)
                for item in payload.get("repository_changes", [])
            ),
            commands=commands, evidence=tuple(payload["evidence"]),
            review_findings=tuple(payload["review_findings"]), risks=tuple(payload["risks"]),
            review_repair_scope=tuple(
                RepositoryChangeClaim.model_validate(item)
                for item in payload.get("review_repair_scope", [])
            ),
            review_external_validation=tuple(
                payload.get("review_external_validation", [])
            ),
            unresolved_items=tuple(payload["unresolved_items"]),
            acceptance_coverage=tuple(
                AcceptanceCoverage.model_validate(item)
                for item in payload.get("acceptance_coverage", [])
            ),
            unrelated_changes_checked=payload.get("unrelated_changes_checked", False),
            root_cause_evidence=tuple(
                RootCauseEvidence.model_validate(item)
                for item in payload.get("root_cause_evidence", [])
            ),
            investigation_suggestions=tuple(
                payload.get("investigation_suggestions", [])
            ),
            behavior_before=payload.get("behavior_before", ""),
            behavior_after=payload.get("behavior_after", ""),
            impact_scope=tuple(payload.get("impact_scope", [])),
            risk_level=payload.get("risk_level", ""),
        )

    def _prepare_run_directory(self, run_id: str) -> Path:
        try:
            self.run_root.mkdir(parents=True, exist_ok=True)
            if _is_reparse_or_link(self.run_root) or not self.run_root.is_dir():
                raise UnsafeCodexRunError("run_root is not a safe directory")
            run_directory = self.run_root / run_id
            run_directory.mkdir(exist_ok=True)
            if _is_reparse_or_link(run_directory) or not run_directory.is_dir():
                raise UnsafeCodexRunError("run directory is not safe")
            if run_directory.resolve(strict=True).parent != self.run_root.resolve(strict=True):
                raise UnsafeCodexRunError("run directory escaped run_root")
            return run_directory
        except UnsafeCodexRunError:
            raise
        except OSError as error:
            raise UnsafeCodexRunError("run directory could not be prepared") from error

    @staticmethod
    def _prepare_isolation_directory(run_directory: Path) -> Path:
        isolation_root = run_directory / ".codex-session-env"
        try:
            isolation_root.mkdir(mode=0o700, exist_ok=True)
            if (
                _is_reparse_or_link(isolation_root)
                or not isolation_root.is_dir()
                or isolation_root.resolve(strict=True).parent
                != run_directory.resolve(strict=True)
            ):
                raise UnsafeCodexRunError("Codex environment directory is unsafe")
            return isolation_root
        except UnsafeCodexRunError:
            raise
        except OSError as error:
            raise UnsafeCodexRunError(
                "Codex environment directory could not be prepared"
            ) from error

    @classmethod
    def _prepare_isolated_codex_home(
        cls,
        isolation_root: Path,
        source_home: Path | None,
        *,
        existing_session_id: str | None,
    ) -> Path | None:
        """Copy only authentication and the current rollout, never user skills."""

        if source_home is None:
            return None
        target_home = isolation_root / "codex-home"
        try:
            target_home.mkdir(mode=0o700, exist_ok=True)
            if _is_reparse_or_link(target_home) or not target_home.is_dir():
                raise UnsafeCodexRunError("isolated Codex home is unsafe")
            if target_home.resolve(strict=True).parent != isolation_root.resolve(strict=True):
                raise UnsafeCodexRunError("isolated Codex home is unsafe")

            source_auth = source_home / "auth.json"
            if source_auth.exists():
                metadata = source_auth.lstat()
                if (
                    _is_reparse_or_link(source_auth)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size <= 0
                    or metadata.st_size > 1024 * 1024
                    or source_auth.resolve(strict=True).parent != source_home
                ):
                    raise UnsafeCodexRunError("Codex authentication source is unavailable")
                cls._write_prompt(target_home / "auth.json", source_auth.read_bytes())

            if existing_session_id is not None:
                target_matches = tuple(
                    (target_home / "sessions").glob(
                        f"*/*/*/*{existing_session_id}*.jsonl"
                    )
                )
                if not target_matches:
                    source_sessions = source_home / "sessions"
                    matches = tuple(
                        source_sessions.glob(
                            f"*/*/*/*{existing_session_id}*.jsonl"
                        )
                    )
                    if len(matches) != 1:
                        raise UnsafeCodexRunError("Codex session state is unavailable")
                    source_session = matches[0]
                    metadata = source_session.lstat()
                    resolved_sessions = source_sessions.resolve(strict=True)
                    resolved_session = source_session.resolve(strict=True)
                    if (
                        _is_reparse_or_link(source_session)
                        or not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_size <= 0
                        or metadata.st_size > 64 * 1024 * 1024
                        or not resolved_session.is_relative_to(resolved_sessions)
                    ):
                        raise UnsafeCodexRunError("Codex session state is unavailable")
                    relative = resolved_session.relative_to(resolved_sessions)
                    target_session = target_home / "sessions" / relative
                    target_session.parent.mkdir(parents=True, exist_ok=True)
                    cls._write_prompt(target_session, source_session.read_bytes())
            return target_home
        except UnsafeCodexRunError:
            raise
        except OSError as error:
            raise UnsafeCodexRunError("isolated Codex home could not be prepared") from error

    @staticmethod
    def _write_prompt(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeCodexRunError("prompt path is not a regular file")
                offset = 0
                while offset < len(content):
                    written = os.write(descriptor, content[offset:])
                    if written <= 0:
                        raise OSError("short prompt write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
                descriptor = None
            os.replace(temporary, path)
        except UnsafeCodexRunError:
            raise
        except OSError as error:
            raise UnsafeCodexRunError("prompt could not be persisted") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_output(
        self,
        text: str,
        mapping: RepositoryMapping | None,
        *,
        root_cause_result: bool = False,
    ) -> dict[str, Any]:
        try:
            payload = self._parse_output(
                text, root_cause_result=root_cause_result
            )
            if payload.get("repository_changes"):
                raise ValueError("single repository output cannot contain group claims")
            if mapping is None and (payload["changed_files"] or payload["commands"]):
                raise ValueError("preflight cannot claim repository effects")
            for path in payload["changed_files"]:
                normalized = PurePosixPath(path).as_posix()
                if mapping is None or normalized != path or not _path_allowed(
                    path, mapping.allowed_paths
                ):
                    raise ValueError("unsafe changed path")
        except Exception as error:
            raise CodexOutputError(
                "Codex returned invalid structured output",
                validation_hint=_safe_validation_hint(error),
            ) from error
        return payload

    def _validate_group_output(
        self,
        text: str,
        group: RepositoryGroupMapping,
        *,
        root_cause_result: bool = False,
    ) -> dict[str, Any]:
        try:
            payload = self._parse_output(
                text, root_cause_result=root_cause_result
            )
            mappings = {item.key: item for item in group.repositories}
            claims = payload.get("repository_changes", [])
            if not isinstance(claims, list):
                raise ValueError("group claims must be a list")
            changed_files = payload["changed_files"]
            if changed_files:
                claim_paths = [str(claim.get("path", "")) for claim in claims]
                if (
                    len(changed_files) != len(claim_paths)
                    or set(changed_files) != set(claim_paths)
                ):
                    raise ValueError("group output has ambiguous repository change claims")
                payload = dict(payload)
                payload["changed_files"] = []
            normalized_coverage: list[dict[str, Any]] = []
            for coverage in payload["acceptance_coverage"]:
                files = coverage.get("files", [])
                repository_files = coverage.get("repository_files", [])
                if files and repository_files:
                    repository_paths = [
                        str(item.get("path", "")) for item in repository_files
                    ]
                    if (
                        len(files) != len(repository_paths)
                        or set(files) != set(repository_paths)
                    ):
                        raise ValueError(
                            "group acceptance coverage has ambiguous file claims"
                        )
                    coverage = dict(coverage)
                    coverage["files"] = []
                elif not files and not repository_files:
                    continue
                normalized_coverage.append(coverage)
            if normalized_coverage != payload["acceptance_coverage"]:
                payload = dict(payload)
                payload["acceptance_coverage"] = normalized_coverage
            for claim in claims:
                parsed = RepositoryChangeClaim.model_validate(claim)
                mapping = mappings.get(parsed.repository_key)
                if mapping is None or not _path_allowed(parsed.path, mapping.allowed_paths):
                    raise ValueError("unsafe repository change claim")
        except Exception as error:
            raise CodexOutputError(
                "Codex returned invalid structured output",
                validation_hint=_safe_validation_hint(error),
            ) from error
        return payload

    def _parse_output(
        self, text: str, *, root_cause_result: bool = False
    ) -> dict[str, Any]:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        if root_cause_result:
            if type(payload) is dict:
                payload = dict(payload)
                for field in _ROOT_CAUSE_STAGE_IRRELEVANT_FIELDS:
                    payload.pop(field, None)
            payload = _normalize_root_cause_cross_fields(payload)
            root_schema = json.loads(
                self.root_cause_schema_path.read_text(encoding="utf-8")
            )
            Draft202012Validator(root_schema).validate(payload)
        payload = _normalize_structural_defaults(payload)
        if root_cause_result:
            payload = dict(payload)
            payload["acceptance_coverage"] = []
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(payload)
        for command in payload["commands"]:
            if _is_forbidden_command(command["command"]):
                raise _UnsafeReportedCommandError(
                    "publication command is forbidden"
                )
        return payload


__all__ = [
    "CodexCommand", "CodexExecutionError", "CodexOutputError", "CodexProcessStartError",
    "CodexRunner", "CodexRunnerError", "CodexTimeoutError", "UnsafeCodexRunError",
    "resolve_codex_command",
]
