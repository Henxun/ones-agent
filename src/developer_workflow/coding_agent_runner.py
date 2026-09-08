"""Provider-neutral contracts for guarded coding-agent execution."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .contracts import (
    CodingAgentResult,
    PreparedWorktree,
    RepositoryGroupMapping,
    RepositoryMapping,
)
from .repository_group import PreparedRepository


class CodingAgentRunnerError(RuntimeError):
    """Base error for a safely rejected coding-agent execution."""


class UnsafeCodingAgentRunError(CodingAgentRunnerError):
    """The requested execution would cross a local safety boundary."""


class CodingAgentExecutionError(CodingAgentRunnerError):
    """The selected coding agent could not be executed successfully."""


class CodingAgentProcessStartError(CodingAgentExecutionError):
    """The selected coding-agent executable could not be started."""


class CodingAgentTimeoutError(CodingAgentExecutionError):
    """The selected coding agent exceeded its execution deadline."""


class CodingAgentOutputError(CodingAgentRunnerError):
    """The selected coding agent returned invalid or unsafe output."""

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


@runtime_checkable
class CodingAgentRunner(Protocol):
    """Stable workflow-facing runner contract, independent of a CLI vendor."""

    run_root: Path
    schema_path: Path
    root_cause_schema_path: Path

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
    ) -> CodingAgentResult: ...

    def run_preflight(
        self, *, run_id: str, prompt: str, timeout_seconds: float = 1800
    ) -> CodingAgentResult: ...

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
    ) -> CodingAgentResult: ...

    def activity(self, run_id: str, *, limit: int = 40) -> tuple[str, ...]: ...


__all__ = [
    "CodingAgentExecutionError",
    "CodingAgentOutputError",
    "CodingAgentProcessStartError",
    "CodingAgentResult",
    "CodingAgentRunner",
    "CodingAgentRunnerError",
    "CodingAgentTimeoutError",
    "UnsafeCodingAgentRunError",
]
