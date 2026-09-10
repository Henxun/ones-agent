"""Local coding-agent discovery and bounded readiness probing."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


CodingAgentKey = Literal["codex", "claude"]


@dataclass(frozen=True, slots=True)
class CodingAgentDefinition:
    """Static capability metadata; runtime construction remains adapter-owned."""

    key: str
    label: str
    command: str
    supported: bool


@dataclass(frozen=True, slots=True)
class CodingAgentInstallation:
    key: str
    label: str
    installed: bool
    supported: bool
    usable: bool
    executable: Path | None = None
    detail: str = ""
    launchable: bool = False
    version: str = ""


@dataclass(frozen=True, slots=True)
class CodingAgentProbeResult:
    """Non-secret readiness facts collected from a bounded ``--version`` call."""

    launchable: bool
    version: str = ""
    diagnostic: str = ""


CODING_AGENT_CATALOG = (
    CodingAgentDefinition("codex", "Codex", "codex", True),
    CodingAgentDefinition("claude", "Claude Code", "claude", True),
    CodingAgentDefinition("gemini", "Gemini CLI", "gemini", False),
    CodingAgentDefinition("aider", "Aider", "aider", False),
    CodingAgentDefinition("cursor-agent", "Cursor Agent", "cursor-agent", False),
)
SUPPORTED_CODING_AGENT_KEYS = frozenset(
    item.key for item in CODING_AGENT_CATALOG if item.supported
)

_VERSION_PATTERNS = {
    "codex": re.compile(r"codex-cli (?P<version>\d+\.\d+\.\d+)\Z"),
    "claude": re.compile(r"(?P<version>\d+\.\d+\.\d+) \(Claude Code\)\Z"),
}
_PROBE_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)


def coding_agent_definition(key: str) -> CodingAgentDefinition:
    for item in CODING_AGENT_CATALOG:
        if item.key == key:
            return item
    raise ValueError("unknown coding agent")


def validate_coding_agent_provider_keys(keys: object) -> None:
    """Fail fast when the runtime registry drifts from selectable providers."""

    try:
        registered = frozenset(keys)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("coding agent provider registry is invalid") from None
    if registered != SUPPORTED_CODING_AGENT_KEYS:
        raise ValueError("coding agent provider registry is incomplete")


def _canonical_executable(raw: str) -> Path | None:
    try:
        path = Path(raw).resolve(strict=True)
        metadata = path.stat()
    except (OSError, RuntimeError):
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    if os.name == "nt":
        # Batch wrappers are resolved by cmd.exe and would cross the workflow's
        # shell/quoting trust boundary. Native agent executables remain safe argv.
        if path.suffix.casefold() not in {".exe", ".com"}:
            return None
    elif not os.access(path, os.X_OK):
        return None
    return path


def _safe_probe_environment(source: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in source.items()
        if type(key) is str
        and type(value) is str
        and key.upper() in _PROBE_ENVIRONMENT
        and "\x00" not in value
    }


def _version_from_output(key: str, stdout: object, stderr: object) -> str:
    """Accept exactly one provider-specific version line, fail closed otherwise."""

    pattern = _VERSION_PATTERNS.get(key)
    if pattern is None:
        return ""
    values = [
        value.strip()
        for value in (stdout, stderr)
        if type(value) is str and value.strip()
    ]
    if len(values) != 1 or len(values[0]) > 128:
        return ""
    match = pattern.fullmatch(values[0])
    return match.group("version") if match is not None else ""


def probe_coding_agent(
    key: str,
    executable: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    codex_command_resolver: Callable[[], object] | None = None,
) -> CodingAgentProbeResult:
    """Verify that a supported agent starts, without exposing its raw output.

    Codex npm wrappers are resolved through the existing attested native-runtime
    boundary. Other providers must already resolve to a native executable.
    """

    command = None
    argv: list[str]
    try:
        if key not in SUPPORTED_CODING_AGENT_KEYS:
            return CodingAgentProbeResult(False, diagnostic="Agent 尚未接入工作流")
        if key == "codex":
            from .codex_runner import resolve_codex_command

            command = (
                resolve_codex_command()
                if codex_command_resolver is None
                else codex_command_resolver()
            )
            argv = command.argv("--version")
        else:
            canonical = _canonical_executable(str(executable))
            if canonical is None:
                return CodingAgentProbeResult(
                    False, diagnostic="命令不是受支持的原生可执行文件"
                )
            argv = [str(canonical), "--version"]
        if run is None:
            from .codex_runner import _bounded_subprocess

            run = _bounded_subprocess
        completed = run(
            argv,
            cwd=Path(argv[0]).resolve(strict=True).parent,
            env=_safe_probe_environment(dict(os.environ)),
            timeout=5.0,
            max_output_bytes=16 * 1024,
        )
        if completed.returncode != 0:
            return CodingAgentProbeResult(False, diagnostic="版本探测命令执行失败")
        version = _version_from_output(key, completed.stdout, completed.stderr)
        if not version:
            return CodingAgentProbeResult(False, diagnostic="未返回可识别的版本号")
        return CodingAgentProbeResult(True, version=version, diagnostic="已验证可启动")
    except subprocess.TimeoutExpired:
        return CodingAgentProbeResult(False, diagnostic="版本探测超时")
    except Exception:
        return CodingAgentProbeResult(False, diagnostic="无法安全启动命令")
    finally:
        if command is not None:
            command.close()


def discover_coding_agents(
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[str, Path], CodingAgentProbeResult] = probe_coding_agent,
) -> tuple[CodingAgentInstallation, ...]:
    """Return a stable catalog with bounded launch and version readiness facts."""

    found: list[CodingAgentInstallation] = []
    for definition in CODING_AGENT_CATALOG:
        key = definition.key
        raw = which(definition.command)
        executable = _canonical_executable(raw) if raw else None
        installed = raw is not None
        display_path = executable or (Path(raw).resolve(strict=False) if raw else None)
        can_probe = definition.supported and installed and (
            executable is not None or key == "codex"
        )
        readiness = (
            probe(key, display_path)
            if can_probe and display_path is not None
            else CodingAgentProbeResult(False)
        )
        usable = definition.supported and readiness.launchable
        if not installed:
            detail = "未安装或不在 PATH 中"
        elif not definition.supported:
            detail = "已检测，执行协议尚未接入"
        elif executable is None and key != "codex":
            detail = "已检测到命令包装脚本；当前仅支持原生可执行文件"
        elif not readiness.launchable:
            detail = readiness.diagnostic or "命令未通过启动检查"
        else:
            detail = f"可启动 · 版本 {readiness.version}；认证将在任务启动时校验"
        found.append(
            CodingAgentInstallation(
                key=key,
                label=definition.label,
                installed=installed,
                supported=definition.supported,
                usable=usable,
                executable=display_path,
                detail=detail,
                launchable=readiness.launchable,
                version=readiness.version,
            )
        )
    return tuple(found)


def resolve_coding_agent_executable(key: str) -> Path:
    definition = coding_agent_definition(key)
    if not definition.supported:
        raise RuntimeError("selected coding agent is unavailable")
    raw = shutil.which(definition.command)
    executable = _canonical_executable(raw) if raw else None
    if executable is None:
        raise RuntimeError("selected coding agent is unavailable")
    return executable


__all__ = [
    "CODING_AGENT_CATALOG",
    "SUPPORTED_CODING_AGENT_KEYS",
    "CodingAgentDefinition",
    "CodingAgentInstallation",
    "CodingAgentProbeResult",
    "CodingAgentKey",
    "coding_agent_definition",
    "discover_coding_agents",
    "probe_coding_agent",
    "resolve_coding_agent_executable",
    "validate_coding_agent_provider_keys",
]
