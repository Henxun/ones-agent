"""Claude Code backend using the workflow's existing result and Git guards."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .codex_runner import (
    CodexExecutionError,
    CodexOutputError,
    CodexRunner,
    CodexRunnerError,
    CodexTimeoutError,
    UnsafeCodexRunError,
    _RUN_ID,
    _bounded_subprocess,
    _is_positive_finite_number,
    _is_reparse_or_link,
)
from .coding_agents import resolve_coding_agent_executable


_CLAUDE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_CLAUDE_ENV = frozenset(
    {
        "APPDATA", "COMSPEC", "HOME", "LANG", "LC_ALL", "LOCALAPPDATA",
        "NO_COLOR", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TERM", "TMP",
        "TMPDIR", "USERPROFILE", "WINDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
        "SSL_CERT_DIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ANTHROPIC_BASE_URL",
    }
)


def safe_claude_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in source.items():
        if (
            type(key) is str
            and type(value) is str
            and key.upper() in _CLAUDE_ENV
            and "\x00" not in value
        ):
            environment[key] = value
    return environment


@dataclass(slots=True)
class ClaudeRunner(CodexRunner):
    """Reuse CodexRunner's schema, repository and evidence validation layers."""

    claude_command_resolver: Callable[[], Path] = field(
        default=lambda: resolve_coding_agent_executable("claude"), repr=False
    )

    def __post_init__(self) -> None:
        super(ClaudeRunner, self).__post_init__()
        if not callable(self.claude_command_resolver):
            raise ValueError("Claude command resolver is invalid")

    def _session_path(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise UnsafeCodexRunError("run_id is not a safe path segment")
        return self._prepare_run_directory(run_id) / ".claude-session-id"

    def _read_session_id(self, run_id: str) -> str | None:
        path = self._session_path(run_id)
        try:
            metadata = path.lstat()
            if (
                _is_reparse_or_link(path)
                or not stat.S_ISREG(metadata.st_mode)
                or not 8 <= metadata.st_size <= 129
            ):
                raise UnsafeCodexRunError("Claude session state is unsafe")
            value = path.read_text(encoding="ascii", errors="strict").strip()
            if _CLAUDE_SESSION_ID.fullmatch(value) is None:
                raise UnsafeCodexRunError("Claude session state is unsafe")
            return value
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as error:
            raise UnsafeCodexRunError("Claude session state is unsafe") from error

    def _store_session_id(self, run_id: str, session_id: str) -> None:
        if _CLAUDE_SESSION_ID.fullmatch(session_id) is None:
            raise UnsafeCodexRunError("Claude returned an invalid session id")
        self._write_prompt(
            self._session_path(run_id), (session_id + "\n").encode("ascii", "strict")
        )

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
            raise UnsafeCodexRunError("Claude permission mode is invalid")
        if type(skip_git_repo_check) is not bool:
            raise UnsafeCodexRunError("Claude Git repository policy is invalid")
        if type(additional_directories) is not tuple or any(
            not isinstance(path, Path) or not path.is_absolute()
            for path in additional_directories
        ):
            raise UnsafeCodexRunError("Claude additional directories are invalid")
        if output_schema not in {self.schema_path, self.root_cause_schema_path}:
            raise UnsafeCodexRunError("Claude output schema selection is invalid")
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
            executable = Path(self.claude_command_resolver()).resolve(strict=True)
            if not executable.is_file() or _is_reparse_or_link(executable):
                raise OSError
            effective_cwd = (cwd or self._prepare_run_directory(run_id)).resolve(strict=True)
            canonical_additional = tuple(path.resolve(strict=True) for path in additional_directories)
            if (
                not effective_cwd.is_dir()
                or _is_reparse_or_link(effective_cwd)
                or len(set(canonical_additional)) != len(canonical_additional)
                or any(
                    not path.is_dir()
                    or _is_reparse_or_link(path)
                    or path == effective_cwd
                    for path in canonical_additional
                )
            ):
                raise OSError
            schema = json.loads(output_schema.read_text(encoding="utf-8", errors="strict"))
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise UnsafeCodexRunError("Claude runtime is unavailable") from None
        run_directory = self._prepare_run_directory(run_id)
        self._write_prompt(run_directory / "claude-prompt.txt", prompt_bytes)
        self._record_activity(run_directory, "prepare", "Preparing Claude Code runtime")
        arguments = [
            str(executable), "-p", "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=True, separators=(",", ":")),
            "--permission-mode", "plan" if sandbox == "read-only" else "acceptEdits",
        ]
        for path in canonical_additional:
            arguments.extend(("--add-dir", str(path)))
        existing_session_id = self._read_session_id(run_id)
        if existing_session_id is not None:
            arguments.extend(("--resume", existing_session_id))
        self._record_activity(run_directory, "analysis", "Claude Code session started")
        try:
            completed = self.command_executor(
                tuple(arguments),
                cwd=effective_cwd,
                env=safe_claude_environment(source),
                timeout=float(timeout_seconds),
                max_output_bytes=self.max_output_bytes,
                stdin=prompt_bytes,
            )
        except subprocess.TimeoutExpired as error:
            raise CodexTimeoutError("Claude execution timed out") from error
        except CodexRunnerError:
            raise
        except Exception as error:
            raise CodexExecutionError("Claude process could not be executed") from error
        if completed.returncode != 0:
            raise CodexExecutionError("Claude exited unsuccessfully")
        if not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str):
            raise CodexOutputError("Claude returned invalid structured output")
        try:
            if len(completed.stdout.encode("utf-8")) + len(completed.stderr.encode("utf-8")) > self.max_output_bytes:
                raise ValueError
            envelope = json.loads(completed.stdout)
            if (
                type(envelope) is not dict
                or envelope.get("type") != "result"
                or envelope.get("is_error") is not False
                or type(envelope.get("session_id")) is not str
                or _CLAUDE_SESSION_ID.fullmatch(envelope["session_id"]) is None
                or type(envelope.get("structured_output")) is not dict
            ):
                raise ValueError
            if existing_session_id is not None and envelope["session_id"] != existing_session_id:
                raise ValueError
            if existing_session_id is None:
                self._store_session_id(run_id, envelope["session_id"])
            output = json.dumps(envelope["structured_output"], ensure_ascii=False)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise CodexOutputError("Claude returned invalid structured output") from error
        self._record_activity(run_directory, "analysis", "Claude Code analysis completed")
        return output, ()


__all__ = ["ClaudeRunner", "safe_claude_environment"]
