"""Local coding-agent discovery without executing third-party CLIs."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CodingAgentInstallation:
    key: str
    label: str
    installed: bool
    supported: bool
    usable: bool
    executable: Path | None = None
    detail: str = ""


_AGENTS = (
    ("codex", "Codex", "codex", True),
    ("claude", "Claude Code", "claude", True),
    ("gemini", "Gemini CLI", "gemini", False),
    ("aider", "Aider", "aider", False),
    ("cursor-agent", "Cursor Agent", "cursor-agent", False),
)


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


def discover_coding_agents(
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[CodingAgentInstallation, ...]:
    """Return a stable catalog; discovery never launches an installed program."""

    found: list[CodingAgentInstallation] = []
    for key, label, command, supported in _AGENTS:
        raw = which(command)
        executable = _canonical_executable(raw) if raw else None
        installed = raw is not None
        # Codex has its own signed-runtime staging layer, which safely resolves
        # common npm wrappers to the native binary at execution time.
        usable = supported and (executable is not None or (key == "codex" and installed))
        if not installed:
            detail = "未安装或不在 PATH 中"
        elif not supported:
            detail = "已检测，执行协议尚未接入"
        elif executable is None and key != "codex":
            detail = "已检测到命令包装脚本；当前仅支持原生可执行文件"
        elif executable is None:
            detail = "可用于工作流；启动时将验证原生 Codex 运行时"
        else:
            detail = "可用于工作流"
        found.append(
            CodingAgentInstallation(
                key=key,
                label=label,
                installed=installed,
                supported=supported,
                usable=usable,
                executable=executable or (Path(raw).resolve(strict=False) if raw else None),
                detail=detail,
            )
        )
    return tuple(found)


def resolve_coding_agent_executable(key: str) -> Path:
    for installation in discover_coding_agents():
        if installation.key == key and installation.usable and installation.executable:
            return installation.executable
    raise RuntimeError("selected coding agent is unavailable")


__all__ = [
    "CodingAgentInstallation",
    "discover_coding_agents",
    "resolve_coding_agent_executable",
]
