"""Atomic, file-backed persistence for isolated developer workflow runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from .contracts import (
    CommandOutcome,
    DefectAction,
    DefectCheckpoint,
    PublicationResult,
    StateEvent,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _store_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _store_kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _store_kernel32.CreateFileW.restype = wintypes.HANDLE
    _store_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _store_kernel32.CloseHandle.restype = wintypes.BOOL
    _store_kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _store_kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
else:
    import fcntl


class RunStoreError(RuntimeError):
    """Base error for workflow run persistence."""


class UnsafeRunPathError(RunStoreError, ValueError):
    """Raised when a run identifier or resolved path is unsafe."""


class RunAlreadyExistsError(RunStoreError):
    """Raised when creating an already persisted run."""


class RunNotFoundError(RunStoreError):
    """Raised when a persisted run does not exist."""


class RunCorruptedError(RunStoreError):
    """Raised when stored run data cannot be safely validated."""


class ConcurrentRunUpdateError(RunStoreError):
    """Raised when optimistic version comparison fails."""


class RunLockTimeoutError(RunStoreError):
    """Raised when a per-run filesystem lock cannot be acquired in time."""


class InvalidRunTransitionError(RunStoreError, ValueError):
    """Raised when a requested state change violates the workflow graph."""


class InvalidRunMutationError(RunStoreError, ValueError):
    """Raised when caller-owned updates violate persisted run invariants."""


class _RunFileReplacedError(UnsafeRunPathError):
    """Internal signal for a regular-file atomic replacement retry."""


_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_READ_ONLY_REPLACE_ATTEMPTS = 3
_MAIN_CHAIN = (
    WorkflowState.CREATED,
    WorkflowState.READING_ONES,
    WorkflowState.VALIDATING,
    WorkflowState.PREPARING_REPO,
    WorkflowState.IMPLEMENTING,
    WorkflowState.TESTING,
    WorkflowState.AI_REVIEW,
    WorkflowState.WAITING_APPROVAL,
    WorkflowState.PUBLISHING,
    WorkflowState.COMPLETED,
)
_MAIN_INDEX = {state: index for index, state in enumerate(_MAIN_CHAIN)}
_TERMINAL = {
    WorkflowState.CANCELLED,
    WorkflowState.COMPLETED,
    WorkflowState.FAILED,
}


def _is_completed_verification(run: WorkflowRun) -> bool:
    """Local verification, including review corrections, cannot grant publication."""
    review = run.review
    if not (
        run.type is WorkflowType.DEFECT and run.verification_only
        and run.defect_action is DefectAction.ANALYZE_AND_REPAIR
        and run.defect_checkpoint is DefectCheckpoint.FINAL_TESTED
        and len(run.codex_results) >= 2 and run.root_cause_evidence
        and run.reproduction_test_sha256 and len(run.pre_fix_test_results) == 1
        and run.pre_fix_snapshot is not None
        and run.test_results and run.tested_snapshot is not None
        and review is not None and review.summary.strip() and review.review_findings
        and review.unrelated_changes_checked and not review.unresolved_items
        and review.root_cause_evidence == run.root_cause_evidence
        and review.behavior_before == run.behavior_before
        and review.behavior_after == run.behavior_after
        and review.impact_scope == run.impact_scope
        and review.risk_level == run.risk_level
        and run.approval is None and run.group_publication is None
        and run.publication == PublicationResult()
    ):
        return False
    baseline = run.pre_fix_test_results[0]
    if (
        not baseline.argv or baseline.argv != run.test_results[0].argv
        or any(test.exit_code != 0 or test.outcome is not CommandOutcome.PASSED
               for test in (*run.pre_fix_test_results, *run.test_results))
    ):
        return False
    target = run.root_cause_evidence[0]
    if run.review_repair_attempts:
        # A passing baseline may subsequently receive review-driven code/test
        # corrections. This remains local validation and cannot publish.
        return (
            len(run.codex_results) >= 3
            and any(item.source == "system_review" for item in run.revisions)
            and bool(run.review_repair_snapshot_sha256)
            and (
                run.repository_group is None or (
                    {item.repository_key for item in run.repository_evidence}
                    == {item.key for item in run.repository_group.repositories}
                    and all(item.tested_snapshot is not None for item in run.repository_evidence)
                )
            )
        )
    if len(run.codex_results) != 2:
        return False
    if run.repository_group is not None:
        if target.reproduction_file is None:
            return False
        return (
            {item.repository_key for item in run.repository_evidence}
            == {item.key for item in run.repository_group.repositories}
            and all(
                item.tested_snapshot is not None
                and (
                    item.repository_key != target.reproduction_file.repository_key
                    or item.tested_snapshot == run.pre_fix_snapshot
                )
                and all(
                    item.repository_key == target.reproduction_file.repository_key
                    and path == target.reproduction_test
                    for path in item.tested_snapshot.changed_files
                )
                for item in run.repository_evidence
            )
        )
    return (
        run.tested_snapshot == run.pre_fix_snapshot
        and all(path == target.reproduction_test for path in run.tested_snapshot.changed_files)
    )


def _is_completed_analysis(run: WorkflowRun) -> bool:
    return (
        run.type is WorkflowType.DEFECT
        and run.defect_action is DefectAction.ANALYZE
        and run.defect_checkpoint is DefectCheckpoint.ROOT_VERIFIED
        and len(run.codex_results) == 1
        and bool(run.root_cause_evidence)
        and run.codex_results[0].root_cause_evidence == run.root_cause_evidence
        and not run.codex_results[0].unresolved_items
        and not run.codex_results[0].changed_files
        and not run.codex_results[0].repository_changes
        and not run.codex_results[0].commands
        and not run.changed_files
        and not run.test_results
        and run.approval is None
        and run.group_publication is None
        and run.publication.approved_fingerprint == ""
        and run.publication.commit_hash == ""
        and run.publication.comment_id == ""
    )


def _has_recorded_readonly_analysis(run: WorkflowRun) -> bool:
    results = (
        *run.previous_analysis_results,
        *run.codex_results[:1],
    )
    return any(
        bool(result.root_cause_evidence)
        and not result.unresolved_items
        and not result.changed_files
        and not result.repository_changes
        and not result.commands
        for result in results
    )


class FileRunStore:
    """Persist one strict JSON document per workflow run.

    ``run_root`` must be a private directory controlled by the current account.
    Identity and reparse checks harden ordinary path replacement, but a malicious
    same-account process racing between individual filesystem syscalls is outside
    the supported threat model.
    """

    def __init__(
        self,
        run_root: Path,
        *,
        lock_timeout: float = 5.0,
        lock_poll_interval: float = 0.05,
    ) -> None:
        if lock_timeout < 0 or lock_poll_interval <= 0:
            raise ValueError("lock timing values must be positive")
        run_root.mkdir(parents=True, exist_ok=True)
        _reject_reparse_point(run_root, "run root")
        self._run_root = run_root.resolve(strict=True)
        self._root_identity = _path_identity(self._run_root)
        self._run_identities: dict[str, tuple[int, int]] = {}
        self._lock_timeout = lock_timeout
        self._lock_poll_interval = lock_poll_interval

    def create(self, run: WorkflowRun) -> WorkflowRun:
        if run.state is not WorkflowState.CREATED or run.version != 0:
            raise InvalidRunMutationError(
                "only a CREATED run at version 0 can be created"
            )
        run_dir = self._run_dir(run.run_id)
        run_dir.mkdir(parents=False, exist_ok=True)
        self._assert_contained(run_dir)
        with self._locked(run.run_id) as run_identity:
            run_file = self._run_file(run.run_id)
            if run_file.exists():
                raise RunAlreadyExistsError("workflow run already exists")
            stored = run.validated_update(version=1, updated_at=_utc_now())
            _validate_persisted_run(stored, loading=False)
            self._atomic_write(run_file, stored, run_identity)
            return stored

    def load(self, run_id: str, *, read_only: bool = False) -> WorkflowRun:
        self._validate_run_id(run_id)
        if read_only:
            run_identity = self._validate_run_identity(run_id)
            return self._load_read_only(run_id, run_identity)
        with self._locked(run_id) as run_identity:
            return self._load_unlocked(run_id, run_identity)

    def list_run_ids(self) -> tuple[str, ...]:
        """Return canonical direct-child run identifiers without writing storage."""

        self._validate_root_identity()
        run_ids: list[str] = []
        try:
            with os.scandir(self._run_root) as entries:
                for entry in entries:
                    run_id = entry.name
                    if _RUN_ID.fullmatch(run_id) is None:
                        continue
                    run_dir = self._run_root / run_id
                    try:
                        _reject_reparse_point(run_dir, "run directory")
                        path_identity = _path_identity(run_dir)
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISDIR(info.st_mode):
                            continue
                        entry_identity = (info.st_dev, info.st_ino)
                        if entry_identity != (0, 0):
                            if entry_identity != path_identity:
                                continue
                            enumerated_identity = entry_identity
                        else:
                            enumerated_identity = path_identity
                        self._validate_run_identity(run_id, enumerated_identity)
                    except (
                        FileNotFoundError,
                        RunNotFoundError,
                        UnsafeRunPathError,
                        OSError,
                    ):
                        continue
                    run_ids.append(run_id)
        except OSError as exc:
            self._validate_root_identity()
            raise UnsafeRunPathError(
                "workflow run root cannot be safely enumerated"
            ) from exc
        self._validate_root_identity()
        return tuple(sorted(run_ids))

    def delete(self, run_id: str) -> None:
        """Atomically remove one run from the active index, then delete its data."""

        self._validate_run_id(run_id)
        self._validate_root_identity()
        run_dir = self._run_dir(run_id)
        with self._locked(run_id) as locked_identity:
            self._validate_run_identity(run_id, locked_identity)
        self._validate_root_identity()
        if _path_identity(run_dir) != locked_identity:
            raise UnsafeRunPathError("workflow run directory identity changed")
        tombstone = self._run_root / (
            f".deleted-{run_id}-{os.getpid()}-{time.monotonic_ns()}"
        )
        self._assert_contained(tombstone)
        try:
            os.replace(run_dir, tombstone)
        except FileNotFoundError as error:
            raise RunNotFoundError("workflow run does not exist") from error
        except OSError as error:
            raise UnsafeRunPathError("workflow run could not be deleted safely") from error
        self._run_identities.pop(run_id, None)
        self._validate_root_identity()
        try:
            _reject_reparse_point(tombstone, "deleted run directory")
            if _path_identity(tombstone) != locked_identity:
                raise UnsafeRunPathError("deleted workflow run identity changed")
            shutil.rmtree(tombstone)
        except (FileNotFoundError, OSError):
            # The run was already atomically removed from the active namespace.
            # A private, non-indexed tombstone may be reclaimed by maintenance.
            return

    def save(self, run: WorkflowRun, expected_version: int) -> WorkflowRun:
        with self._locked(run.run_id) as run_identity:
            return self._save_locked(run, expected_version, run_identity=run_identity)

    @contextmanager
    def operation_lock(self, run_id: str, purpose: str) -> Iterator[None]:
        """Hold a stable cross-process lease for one external-effect operation."""

        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", purpose) is None:
            raise ValueError("operation lock purpose is invalid")
        with self._advisory_lock(run_id, f".operation-{purpose}.lock"):
            yield

    def transition(
        self,
        run_id: str,
        expected_version: int,
        target: WorkflowState,
        reason: str,
        resume_state: WorkflowState | None = None,
    ) -> WorkflowRun:
        if not reason.strip():
            raise ValueError("transition reason must not be empty")
        try:
            normalized_target = WorkflowState(target)
        except ValueError as exc:
            raise InvalidRunTransitionError("invalid workflow target state") from exc
        with self._locked(run_id) as run_identity:
            current = self._load_unlocked(run_id, run_identity)
            self._check_version(current, expected_version)
            normalized_resume = self._validate_transition(
                current, normalized_target, resume_state
            )
            now = _utc_now()
            updates: dict[str, object] = {
                "state": normalized_target,
                "history": (
                    *current.history,
                    StateEvent(
                        source=current.state,
                        target=normalized_target,
                        reason=reason,
                        occurred_at=now,
                    ),
                ),
                "updated_at": now,
            }
            if normalized_target is WorkflowState.BLOCKED:
                updates.update(blocked_reason=reason, resume_state=normalized_resume)
            else:
                updates.update(blocked_reason="", resume_state=None)
            candidate = current.validated_update(**updates)
            return self._save_locked(
                candidate,
                expected_version,
                current=current,
                internal_transition=True,
                run_identity=run_identity,
            )

    def decide_completed_analysis(
        self,
        run_id: str,
        expected_version: int,
        *,
        accept: bool,
    ) -> WorkflowRun:
        """Atomically accept or regenerate one completed read-only analysis."""

        if type(accept) is not bool:
            raise ValueError("analysis decision is invalid")
        with self._locked(run_id) as run_identity:
            current = self._load_unlocked(run_id, run_identity)
            self._check_version(current, expected_version)
            if not _is_completed_analysis(current):
                raise InvalidRunTransitionError(
                    "analysis decision requires a completed read-only analysis"
                )
            now = _utc_now()
            updates: dict[str, object] = {
                "state": WorkflowState.IMPLEMENTING,
                "history": (
                    *current.history,
                    StateEvent(
                        source=WorkflowState.COMPLETED,
                        target=WorkflowState.IMPLEMENTING,
                        reason=(
                            "accept analyzed solution and continue repair"
                            if accept
                            else "regenerate analyzed solution"
                        ),
                        occurred_at=now,
                    ),
                ),
                "updated_at": now,
                "blocked_reason": "",
                "resume_state": None,
            }
            if accept:
                updates.update(
                    defect_action=DefectAction.ANALYZE_AND_REPAIR,
                    analysis_solution_accepted=True,
                )
            else:
                updates.update(
                    analysis_generation=current.analysis_generation + 1,
                    previous_analysis_results=(
                        *current.previous_analysis_results[-4:],
                        current.codex_results[0],
                    ),
                    codex_results=(),
                    root_cause_evidence=(),
                    investigation_suggestions=(),
                    behavior_before="",
                    behavior_after="",
                    impact_scope=(),
                    risk_level="",
                    defect_checkpoint=DefectCheckpoint.NONE,
                )
            candidate = current.validated_update(**updates)
            return self._save_locked(
                candidate,
                expected_version,
                current=current,
                internal_transition=True,
                run_identity=run_identity,
            )

    def _save_locked(
        self,
        run: WorkflowRun,
        expected_version: int,
        *,
        current: WorkflowRun | None = None,
        internal_transition: bool = False,
        run_identity: tuple[int, int],
    ) -> WorkflowRun:
        persisted = current or self._load_unlocked(run.run_id, run_identity)
        self._check_version(persisted, expected_version)
        if run.run_id != persisted.run_id:
            raise InvalidRunMutationError("run_id must match the stored workflow run")
        _validate_publication_progress(persisted, run)
        if not internal_transition:
            immutable_fields = (
                ("state", run.state, persisted.state),
                ("history", run.history, persisted.history),
                ("resume_state", run.resume_state, persisted.resume_state),
                ("blocked_reason", run.blocked_reason, persisted.blocked_reason),
                (
                    "defect_action",
                    run.defect_action,
                    persisted.defect_action,
                ),
                (
                    "analysis_generation",
                    run.analysis_generation,
                    persisted.analysis_generation,
                ),
                (
                    "analysis_solution_accepted",
                    run.analysis_solution_accepted,
                    persisted.analysis_solution_accepted,
                ),
                (
                    "previous_analysis_results",
                    run.previous_analysis_results,
                    persisted.previous_analysis_results,
                ),
            )
            for field_name, incoming, existing in immutable_fields:
                if incoming != existing:
                    raise InvalidRunMutationError(
                        f"{field_name} can only be changed by transition()"
                    )
        stored = run.validated_update(
            version=expected_version + 1,
            updated_at=_utc_now(),
        )
        _validate_persisted_run(stored, loading=False)
        self._atomic_write(self._run_file(run.run_id), stored, run_identity)
        return stored

    @staticmethod
    def _check_version(current: WorkflowRun, expected_version: int) -> None:
        if current.version != expected_version:
            raise ConcurrentRunUpdateError("workflow run version does not match")

    def _load_unlocked(
        self, run_id: str, run_identity: tuple[int, int]
    ) -> WorkflowRun:
        run_file = self._run_file(run_id)
        self._validate_run_identity(run_id, run_identity)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        for attempt in range(10):
            try:
                descriptor = os.open(run_file, flags)
                break
            except FileNotFoundError as exc:
                raise RunNotFoundError("workflow run was not found") from exc
            except OSError:
                if attempt == 9:
                    raise RunCorruptedError(
                        "stored workflow run is corrupted"
                    ) from None
                time.sleep(0.001)
        if descriptor is None:  # pragma: no cover - loop either opens or raises
            raise RunCorruptedError("stored workflow run is corrupted")
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                raw = stream.read()
        except (OSError, UnicodeError):
            raise RunCorruptedError("stored workflow run is corrupted") from None
        self._validate_run_identity(run_id, run_identity)
        return self._parse_loaded_run(run_id, raw)

    def _load_read_only(
        self, run_id: str, run_identity: tuple[int, int]
    ) -> WorkflowRun:
        run_file = self._run_file(run_id)
        self._validate_run_identity(run_id, run_identity)
        for attempt in range(_READ_ONLY_REPLACE_ATTEMPTS):
            try:
                raw = _read_run_file_nofollow(run_file)
            except _RunFileReplacedError as exc:
                self._validate_read_run_identity(run_id, run_identity)
                if attempt + 1 == _READ_ONLY_REPLACE_ATTEMPTS:
                    raise UnsafeRunPathError(
                        "workflow run file identity changed repeatedly"
                    ) from exc
                continue
            except Exception as exc:
                load_error: Exception | None = exc
                raw = ""
            else:
                load_error = None
            self._validate_read_run_identity(run_id, run_identity)
            if load_error is not None:
                raise load_error
            return self._parse_loaded_run(run_id, raw)
        raise UnsafeRunPathError("workflow run file identity changed repeatedly")

    def _validate_read_run_identity(
        self, run_id: str, expected: tuple[int, int]
    ) -> None:
        try:
            self._validate_run_identity(run_id, expected)
        except (RunNotFoundError, UnsafeRunPathError) as exc:
            raise UnsafeRunPathError(
                "workflow run directory identity changed"
            ) from exc

    @staticmethod
    def _parse_loaded_run(run_id: str, raw: str) -> WorkflowRun:
        try:
            run = WorkflowRun.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError):
            raise RunCorruptedError("stored workflow run is corrupted") from None
        if run.run_id != run_id:
            raise RunCorruptedError("stored workflow run is corrupted") from None
        _validate_persisted_run(run, loading=True)
        return run

    def _validate_transition(
        self,
        current: WorkflowRun,
        target: WorkflowState,
        requested_resume: WorkflowState | None,
    ) -> WorkflowState | None:
        source = current.state
        if source in _TERMINAL:
            raise InvalidRunTransitionError("terminal workflow state cannot transition")
        if source is WorkflowState.PUBLISHING and target is WorkflowState.WAITING_PR_VERIFICATION:
            if current.approval is None or not current.approval.draft_pr:
                raise InvalidRunTransitionError("PR verification wait requires a draft approval")
            return None
        if (
            source is WorkflowState.AI_REVIEW
            and target is WorkflowState.COMPLETED
            and _is_completed_verification(current)
        ):
            return None
        if (
            source is WorkflowState.IMPLEMENTING
            and target is WorkflowState.COMPLETED
            and _is_completed_analysis(current)
        ):
            return None
        if target is WorkflowState.CANCELLED:
            return None
        if source is WorkflowState.BLOCKED:
            if current.resume_state is None or target is not current.resume_state:
                raise InvalidRunTransitionError("blocked workflow can only resume its saved state")
            return None
        if source is WorkflowState.PARTIAL_SUCCESS:
            if target is not WorkflowState.PUBLISHING:
                raise InvalidRunTransitionError("partial publication can only retry publishing")
            return None
        if source is WorkflowState.PUBLISHING and target is WorkflowState.PARTIAL_SUCCESS:
            return None
        if target in {WorkflowState.BLOCKED, WorkflowState.FAILED}:
            if source not in _MAIN_INDEX or source is WorkflowState.COMPLETED:
                raise InvalidRunTransitionError("workflow cannot stop from this state")
            if target is WorkflowState.FAILED:
                return None
            resume = requested_resume or source
            if resume not in _MAIN_INDEX or resume is WorkflowState.COMPLETED:
                raise InvalidRunTransitionError("blocked resume state must be a main-chain state")
            if _MAIN_INDEX[resume] > _MAIN_INDEX[source]:
                raise InvalidRunTransitionError("blocked resume state cannot skip unfinished stages")
            return resume
        source_index = _MAIN_INDEX.get(source)
        if source_index is None or source_index + 1 >= len(_MAIN_CHAIN):
            raise InvalidRunTransitionError("invalid workflow transition")
        if target is not _MAIN_CHAIN[source_index + 1]:
            raise InvalidRunTransitionError("workflow stages cannot be skipped")
        return None

    def _run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        self._validate_root_identity()
        candidate = self._run_root / run_id
        self._assert_contained(candidate)
        return candidate

    def _run_file(self, run_id: str) -> Path:
        candidate = self._run_dir(run_id) / "run.json"
        self._assert_contained(candidate)
        return candidate

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise UnsafeRunPathError("run_id must be 32 lowercase hexadecimal characters")

    def _assert_contained(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self._run_root):
            raise UnsafeRunPathError("workflow run path escapes run root")

    def _validate_root_identity(self) -> None:
        try:
            _reject_reparse_point(self._run_root, "run root")
            identity = _path_identity(self._run_root)
        except OSError as exc:
            raise UnsafeRunPathError("workflow run root identity changed") from exc
        if identity != self._root_identity:
            raise UnsafeRunPathError("workflow run root identity changed")

    def _validate_run_identity(
        self,
        run_id: str,
        expected: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        self._validate_root_identity()
        run_dir = self._run_root / run_id
        try:
            _reject_reparse_point(run_dir, "run directory")
            info = run_dir.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RunNotFoundError("workflow run was not found") from exc
        except OSError as exc:
            raise UnsafeRunPathError("workflow run directory is unsafe") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeRunPathError("workflow run path is not a directory")
        self._assert_contained(run_dir)
        identity = (info.st_dev, info.st_ino)
        remembered = self._run_identities.setdefault(run_id, identity)
        if identity != remembered or (expected is not None and identity != expected):
            raise UnsafeRunPathError("workflow run directory identity changed")
        return identity

    @contextmanager
    def _locked(self, run_id: str) -> Iterator[tuple[int, int]]:
        with self._advisory_lock(run_id, ".lock") as identity:
            yield identity

    @contextmanager
    def _advisory_lock(
        self, run_id: str, filename: str
    ) -> Iterator[tuple[int, int]]:
        run_dir = self._run_dir(run_id)
        if not run_dir.exists():
            raise RunNotFoundError("workflow run was not found")
        initial_identity = self._validate_run_identity(run_id)
        lock_path = run_dir / filename
        self._assert_contained(lock_path)
        try:
            _reject_reparse_point(lock_path, "run lock")
        except FileNotFoundError:
            pass
        deadline = time.monotonic() + self._lock_timeout
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            lock_info = os.fstat(descriptor)
            _reject_reparse_point(lock_path, "run lock")
            if _path_identity(lock_path) != (lock_info.st_dev, lock_info.st_ino):
                raise UnsafeRunPathError("workflow run lock identity changed")
            if lock_info.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            while not acquired:
                try:
                    _try_advisory_lock(descriptor)
                    acquired = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RunLockTimeoutError(
                            "timed out acquiring workflow run lock"
                        ) from exc
                    time.sleep(self._lock_poll_interval)
            self._validate_run_identity(run_id, initial_identity)
            yield initial_identity
        finally:
            if acquired:
                try:
                    _release_advisory_lock(descriptor)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _atomic_write(
        self,
        destination: Path,
        run: WorkflowRun,
        run_identity: tuple[int, int],
    ) -> None:
        self._assert_contained(destination)
        self._validate_run_identity(run.run_id, run_identity)
        payload = json.dumps(
            run.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=".run-", suffix=".tmp", dir=destination.parent
        )
        temp_path = Path(raw_temp_path)
        try:
            self._validate_run_identity(run.run_id, run_identity)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._validate_run_identity(run.run_id, run_identity)
            os.replace(temp_path, destination)
            self._validate_run_identity(run.run_id, run_identity)
        except BaseException:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise


def _path_identity(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def _open_run_file_nofollow(
    path: Path,
) -> tuple[int, tuple[int, int], tuple[int, int, int, int]]:
    """Open one stable regular run file without following its final component."""

    try:
        _reject_reparse_point(path, "run file")
    except FileNotFoundError as exc:
        raise RunNotFoundError("workflow run was not found") from exc
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise UnsafeRunPathError("workflow run file is unsafe") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeRunPathError("workflow run file is unsafe")
    expected_identity = (metadata.st_dev, metadata.st_ino)

    if os.name == "nt":
        descriptor = _open_windows_run_file(path)
    else:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise UnsafeRunPathError("workflow run file is unsafe") from exc

    try:
        opened = os.fstat(descriptor)
        opened_identity = (opened.st_dev, opened.st_ino)
        signature = _validated_run_file_descriptor(opened, opened_identity)
    except (OSError, UnsafeRunPathError) as exc:
        os.close(descriptor)
        raise UnsafeRunPathError("workflow run file is unsafe") from exc

    if os.name != "nt":
        try:
            if sys.platform == "darwin":
                command = getattr(fcntl, "F_GETPATH", 50)
                raw_path = fcntl.fcntl(descriptor, command, b"\0" * 1024)
                value = raw_path.split(b"\0", 1)[0]
                if not value:
                    raise OSError("empty descriptor path")
                actual_path = Path(os.fsdecode(value)).resolve(strict=True)
            elif sys.platform.startswith("linux"):
                actual_path = Path(f"/proc/self/fd/{descriptor}").resolve(
                    strict=True
                )
            else:
                raise OSError("descriptor path verification is unavailable")
            expected_path = path.resolve(strict=True)
        except (OSError, UnicodeError) as exc:
            os.close(descriptor)
            try:
                _validate_run_file_identity(path, expected_identity)
            except _RunFileReplacedError:
                raise
            raise UnsafeRunPathError("workflow run file is unsafe") from exc
        if actual_path != expected_path:
            os.close(descriptor)
            _validate_run_file_identity(path, expected_identity)
            raise UnsafeRunPathError("workflow run file is unsafe")

    if opened_identity != expected_identity:
        os.close(descriptor)
        _validate_run_file_identity(path, expected_identity)
        raise UnsafeRunPathError("workflow run file is unsafe")
    return descriptor, expected_identity, signature


def _read_run_file_nofollow(path: Path) -> str:
    descriptor, file_identity, initial_signature = _open_run_file_nofollow(path)
    read_error: OSError | None = None
    chunks: list[bytes] = []
    descriptor_closed = False
    try:
        while True:
            try:
                chunk = os.read(descriptor, 64 * 1024)
            except OSError as exc:
                read_error = exc
                break
            if not chunk:
                break
            chunks.append(chunk)
        try:
            final = os.fstat(descriptor)
            final_signature = _validated_run_file_descriptor(
                final, file_identity
            )
        except (OSError, UnsafeRunPathError) as exc:
            raise UnsafeRunPathError("workflow run file identity changed") from exc
        if final_signature != initial_signature:
            raise UnsafeRunPathError("workflow run file identity changed")
        try:
            os.close(descriptor)
        except OSError as exc:
            raise UnsafeRunPathError("workflow run file is unsafe") from exc
        descriptor_closed = True
        _validate_run_file_identity(path, file_identity)
        if read_error is not None:
            raise RunCorruptedError("stored workflow run is corrupted") from None
        try:
            return b"".join(chunks).decode("utf-8", errors="strict")
        except UnicodeError:
            raise RunCorruptedError("stored workflow run is corrupted") from None
    finally:
        if not descriptor_closed:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validated_run_file_descriptor(
    metadata: os.stat_result, expected_identity: tuple[int, int]
) -> tuple[int, int, int, int]:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    identity = (metadata.st_dev, metadata.st_ino)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
        or identity != expected_identity
    ):
        raise UnsafeRunPathError("workflow run file is unsafe")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _open_windows_run_file(path: Path) -> int:
    handle = _store_kernel32.CreateFileW(  # type: ignore[name-defined]
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # Share read/write/delete.
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:  # type: ignore[name-defined]
        raise UnsafeRunPathError("workflow run file is unsafe")
    try:
        buffer = ctypes.create_unicode_buffer(32768)  # type: ignore[name-defined]
        length = _store_kernel32.GetFinalPathNameByHandleW(  # type: ignore[name-defined]
            handle, buffer, len(buffer), 0
        )
        if not length or length >= len(buffer):
            raise UnsafeRunPathError("workflow run file is unsafe")
        final_text = buffer.value
        if final_text.startswith("\\\\?\\UNC\\"):
            final_text = "\\\\" + final_text[8:]
        elif final_text.startswith("\\\\?\\"):
            final_text = final_text[4:]
        actual = os.path.normcase(os.path.abspath(final_text))
        expected = os.path.normcase(os.path.abspath(path))
        if actual != expected:
            raise UnsafeRunPathError("workflow run file is unsafe")
        try:
            return msvcrt.open_osfhandle(  # type: ignore[name-defined]
                int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except OSError as exc:
            raise UnsafeRunPathError("workflow run file is unsafe") from exc
    except BaseException:
        _store_kernel32.CloseHandle(handle)  # type: ignore[name-defined]
        raise


def _validate_run_file_identity(
    path: Path, expected_identity: tuple[int, int]
) -> None:
    try:
        _reject_reparse_point(path, "run file")
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise UnsafeRunPathError("workflow run file identity changed") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
    ):
        raise UnsafeRunPathError("workflow run file identity changed")
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise _RunFileReplacedError("workflow run file was atomically replaced")


def _reject_reparse_point(path: Path, label: str) -> None:
    info = path.lstat()
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or file_attributes & reparse_attribute:
        raise UnsafeRunPathError(f"{label} must not be a symlink or reparse point")


def _try_advisory_lock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_advisory_lock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _validate_persisted_run(run: WorkflowRun, *, loading: bool) -> None:
    """Validate state-machine invariants not expressible in the data model."""

    try:
        _validate_persisted_run_invariants(run)
    except InvalidRunMutationError:
        if loading:
            raise RunCorruptedError("stored workflow run is corrupted") from None
        raise


def _validate_persisted_run_invariants(run: WorkflowRun) -> None:
    minimum_version = len(run.history) + 1
    if run.version < minimum_version:
        raise InvalidRunMutationError(
            "persisted version must be at least the history length plus 1"
        )
    if run.retry_count < 0:
        raise InvalidRunMutationError("retry_count must not be negative")
    publication = run.publication
    group_publication = run.group_publication
    delivered = run.state in {WorkflowState.COMPLETED, WorkflowState.WAITING_PR_VERIFICATION}
    if run.state is WorkflowState.WAITING_PR_VERIFICATION:
        if run.approval is None or not run.approval.draft_pr:
            raise InvalidRunMutationError("PR verification wait requires deferred checks")
    if run.state is WorkflowState.COMPLETED and run.approval is not None and run.approval.draft_pr:
        raise InvalidRunMutationError("Draft handoff must not be marked completed")
    intents = group_publication.repositories if group_publication else (publication,)
    if run.approval is not None and any(item.approved_fingerprint and item.draft_pr != run.approval.draft_pr for item in intents):
        raise InvalidRunMutationError("Publication draft mode must match approval")
    if group_publication is not None:
        _validate_group_publication_binding(run, group_publication)
        complete = (
            bool(group_publication.repositories)
            and all(
                item.approved_fingerprint and item.commit_hash
                and item.push_completed_at is not None and item.pr_url
                and not item.error
                for item in group_publication.repositories
            )
            and bool(group_publication.comment_id)
            and not group_publication.error
        )
        partial = (
            bool(group_publication.repositories)
            and not group_publication.comment_id
            and bool(group_publication.error.strip())
        )
        if delivered and not complete:
            raise InvalidRunMutationError("completed group publication facts are incomplete")
        if run.state is WorkflowState.PARTIAL_SUCCESS and not partial:
            raise InvalidRunMutationError("partial group publication facts are invalid")
    else:
        if delivered and not _is_completed_analysis(run) and not _is_completed_verification(run) and not (
            publication.approved_fingerprint and publication.commit_hash
            and publication.push_completed_at is not None and publication.pr_url
            and publication.comment_id and not publication.error
        ):
            raise InvalidRunMutationError("completed publication facts are incomplete")
        if run.state is WorkflowState.PARTIAL_SUCCESS and not (
            publication.approved_fingerprint and publication.commit_hash
            and publication.push_completed_at is not None and publication.pr_url
            and not publication.comment_id and publication.error.strip()
        ):
            raise InvalidRunMutationError("partial publication facts are invalid")

    history = run.history
    if not history:
        if run.state is not WorkflowState.CREATED:
            raise InvalidRunMutationError("empty history requires CREATED state")
        _validate_block_metadata(run)
        return

    if history[0].source is not WorkflowState.CREATED:
        raise InvalidRunMutationError("history must start from CREATED")
    for index, event in enumerate(history):
        if not event.reason.strip():
            raise InvalidRunMutationError("history event reason must not be empty")
        if index and history[index - 1].target is not event.source:
            raise InvalidRunMutationError("history events must be continuous")
        _validate_persisted_edge(run, history, index)
    if history[-1].target is not run.state:
        raise InvalidRunMutationError("history final state must match run state")
    _validate_block_metadata(run)


def _validate_publication_progress(current: WorkflowRun, incoming: WorkflowRun) -> None:
    """Enforce immutable intent and monotonic external-effect facts."""

    intent_fields = (
        "draft_pr",
        "approved_fingerprint", "repo_url", "provider", "provider_host",
        "expected_parent", "expected_tree", "commit_message", "remote_branch",
        "pr_marker", "pr_base", "pr_head", "pr_title", "pr_body", "comment_marker",
    )
    fact_fields = ("commit_hash", "push_completed_at", "pr_url", "comment_id")
    old, new = current.publication, incoming.publication
    if (
        not old.approved_fingerprint
        and new.approved_fingerprint
        and incoming.state is not WorkflowState.PUBLISHING
    ):
        raise InvalidRunMutationError("publication intent requires PUBLISHING state")
    if old.approved_fingerprint and any(
        getattr(old, name) != getattr(new, name) for name in intent_fields
    ):
        raise InvalidRunMutationError("publication intent is immutable")
    for name in fact_fields:
        before, after = getattr(old, name), getattr(new, name)
        if before not in {"", None} and before != after:
            raise InvalidRunMutationError("publication facts are immutable")
    if old.comment_id and new.error:
        raise InvalidRunMutationError("completed comment fact cannot acquire an error")
    _validate_group_publication_progress(current, incoming)


def _validate_group_publication_progress(
    current: WorkflowRun, incoming: WorkflowRun
) -> None:
    old, new = current.group_publication, incoming.group_publication
    if old is None:
        if new is not None:
            _validate_initial_group_publication(incoming, new)
        return
    if new is None:
        raise InvalidRunMutationError("group publication cannot be removed")
    if old.order != new.order or old.comment_marker != new.comment_marker:
        raise InvalidRunMutationError("group publication intent is immutable")
    old_by_key = {item.repository_key: item for item in old.repositories}
    new_by_key = {item.repository_key: item for item in new.repositories}
    if old_by_key.keys() != new_by_key.keys():
        raise InvalidRunMutationError("group publication repositories are immutable")
    intent_fields = (
        "draft_pr",
        "approved_fingerprint", "repo_url", "provider", "provider_host",
        "expected_parent", "expected_tree", "commit_message", "remote_branch",
        "pr_marker", "pr_base", "pr_head", "pr_title", "pr_body", "comment_marker",
    )
    for key, before in old_by_key.items():
        after = new_by_key[key]
        if any(getattr(before, name) != getattr(after, name) for name in intent_fields):
            raise InvalidRunMutationError("repository publication intent is immutable")
        for name in ("commit_hash", "push_completed_at", "pr_url"):
            prior, later = getattr(before, name), getattr(after, name)
            if prior not in {"", None} and prior != later:
                raise InvalidRunMutationError("repository publication facts are immutable")
    if old.comment_id and old.comment_id != new.comment_id:
        raise InvalidRunMutationError("group comment fact is immutable")


def _validate_initial_group_publication(
    run: WorkflowRun, publication: MultiRepositoryPublicationResult
) -> None:
    if run.state is not WorkflowState.PUBLISHING:
        raise InvalidRunMutationError(
            "group publication intent requires PUBLISHING state"
        )
    _validate_group_publication_binding(run, publication)
    if publication.comment_id or publication.error:
        raise InvalidRunMutationError(
            "initial group publication cannot contain external facts"
        )
    if any(
        item.commit_hash
        or item.push_completed_at is not None
        or item.pr_url
        or item.comment_id
        or item.error
        for item in publication.repositories
    ):
        raise InvalidRunMutationError(
            "initial repository publication cannot contain external facts"
        )


def _validate_group_publication_binding(
    run: WorkflowRun, publication: MultiRepositoryPublicationResult
) -> None:
    approval, group = run.approval, run.repository_group
    if (
        approval is None
        or group is None
        or not approval.fingerprint
        or approval.repository_group != group
        or publication.order != group.topological_keys()
        or publication.comment_marker != f"<!-- ones-dev-run:{run.run_id} -->"
    ):
        raise InvalidRunMutationError(
            "group publication intent must match signed approval"
        )
    approved = tuple(item for item in approval.repositories if item.changed_files)
    if tuple(item.repository_key for item in publication.repositories) != tuple(
        item.repository_key for item in approved
    ):
        raise InvalidRunMutationError(
            "group publication must include every changed repository"
        )
    approved_by_key = {item.repository_key: item for item in approved}
    for item in publication.repositories:
        expected = approved_by_key[item.repository_key]
        if (
            item.approved_fingerprint != approval.fingerprint
            or item.draft_pr != approval.draft_pr
            or item.repo_url != expected.mapping.repo_url
            or item.expected_parent != expected.head_commit
            or item.expected_tree != expected.tree_hash
            or item.commit_message != expected.commit_message
            or item.remote_branch != expected.branch
            or item.pr_marker != f"ones-dev-run:{run.run_id}:{item.repository_key}"
            or item.pr_base != expected.mapping.base_branch
            or item.pr_head != expected.branch
            or item.pr_title != expected.pr_title
            or item.pr_body != expected.pr_body
            or item.comment_marker != publication.comment_marker
        ):
            raise InvalidRunMutationError(
                "repository publication intent must match signed approval"
            )


def _validate_persisted_edge(
    run: WorkflowRun, history: tuple[StateEvent, ...], index: int
) -> None:
    event = history[index]
    source = event.source
    target = event.target
    if (
        source is WorkflowState.COMPLETED
        and target is WorkflowState.IMPLEMENTING
        and run.type is WorkflowType.DEFECT
        and (
            run.analysis_solution_accepted
            or run.analysis_generation > 0
        )
    ):
        return
    if source in _TERMINAL:
        raise InvalidRunMutationError("terminal state cannot have outgoing history")
    if source is WorkflowState.PUBLISHING and target is WorkflowState.WAITING_PR_VERIFICATION:
        if run.approval is None or not run.approval.draft_pr:
            raise InvalidRunMutationError("PR verification history requires draft approval")
        return
    if source is WorkflowState.WAITING_PR_VERIFICATION and target is WorkflowState.CANCELLED:
        return

    if source in _MAIN_INDEX:
        if (
            source is WorkflowState.AI_REVIEW
            and target is WorkflowState.COMPLETED
            and _is_completed_verification(run)
        ):
            return
        if (
            source is WorkflowState.IMPLEMENTING
            and target is WorkflowState.COMPLETED
            and run.type is WorkflowType.DEFECT
            and _has_recorded_readonly_analysis(run)
        ):
            return
        if target in {
            WorkflowState.BLOCKED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }:
            return
        if (
            source is WorkflowState.PUBLISHING
            and target is WorkflowState.PARTIAL_SUCCESS
        ):
            return
        source_index = _MAIN_INDEX[source]
        if (
            source_index + 1 < len(_MAIN_CHAIN)
            and target is _MAIN_CHAIN[source_index + 1]
        ):
            return
        raise InvalidRunMutationError("history contains an illegal main-chain edge")

    if source is WorkflowState.PARTIAL_SUCCESS:
        if target in {WorkflowState.PUBLISHING, WorkflowState.CANCELLED}:
            return
        raise InvalidRunMutationError("partial success can only retry or cancel")

    if source is WorkflowState.BLOCKED:
        if target is WorkflowState.CANCELLED:
            return
        if index == 0:
            raise InvalidRunMutationError("blocked recovery lacks its entry event")
        entered_blocked = history[index - 1]
        if (
            entered_blocked.target is not WorkflowState.BLOCKED
            or entered_blocked.source not in _MAIN_INDEX
            or entered_blocked.source is WorkflowState.COMPLETED
            or target not in _MAIN_INDEX
            or target is WorkflowState.COMPLETED
            or _MAIN_INDEX[target] > _MAIN_INDEX[entered_blocked.source]
        ):
            raise InvalidRunMutationError("blocked recovery is not a safe main-chain state")
        return

    raise InvalidRunMutationError("history contains an illegal workflow edge")


def _validate_block_metadata(run: WorkflowRun) -> None:
    if run.state is not WorkflowState.BLOCKED:
        if run.resume_state is not None or run.blocked_reason:
            raise InvalidRunMutationError("non-blocked run has stale block metadata")
        return

    if not run.blocked_reason.strip():
        raise InvalidRunMutationError("blocked run requires a reason")
    resume = run.resume_state
    if (
        resume not in _MAIN_INDEX
        or resume is WorkflowState.COMPLETED
        or not run.history
    ):
        raise InvalidRunMutationError("blocked run requires a safe resume state")
    entered_blocked = run.history[-1]
    if run.blocked_reason != entered_blocked.reason:
        raise InvalidRunMutationError("blocked reason must match its history event")
    if (
        entered_blocked.target is not WorkflowState.BLOCKED
        or entered_blocked.source not in _MAIN_INDEX
        or entered_blocked.source is WorkflowState.COMPLETED
        or _MAIN_INDEX[resume] > _MAIN_INDEX[entered_blocked.source]
    ):
        raise InvalidRunMutationError("blocked resume state skips unfinished stages")


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "ConcurrentRunUpdateError",
    "FileRunStore",
    "InvalidRunMutationError",
    "InvalidRunTransitionError",
    "RunAlreadyExistsError",
    "RunCorruptedError",
    "RunLockTimeoutError",
    "RunNotFoundError",
    "RunStoreError",
    "UnsafeRunPathError",
]
