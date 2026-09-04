"""Synchronous, display-only boundary for the developer workflow TUI."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future, wait
from dataclasses import dataclass, replace
import secrets
import subprocess
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import Literal

from ..config import DeveloperWorkflowConfig
from ..verification import digest
from ..schedules import serialized_mutation
from ..contracts import (
    DefectAction,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    WorkflowRun,
    WorkflowState,
)
from ..defect_flow import DefectCandidateError
from ..orchestrator import DeveloperWorkflowOrchestrator, InvalidWorkflowAction
from .models import (
    DangerousActionRequest,
    DefectChoice,
    DefectFilterOptions,
    FilterChoice,
    RequirementChoice,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TuiDisplayError,
    WorkspaceRepositoryInput,
    WorkspaceSummary,
    safe_tui_text,
    validate_tui_input_text,
)
from .run_index import RunIndex


_STALE = "workflow changed; review again"
_CANDIDATE_ERROR = "candidate snapshot is invalid"
_ACTION_ERROR = "workflow action failed safely"
_DEFECT_START_ERROR = "defect workflow could not be started"
_LIST_ERROR = "workflow list is unavailable"
_DISPLAY_ERROR = "workflow display is unavailable"
_ACTION_UNAVAILABLE = "workflow action is unavailable"
_QUERY_UNAVAILABLE = "candidate query is unavailable"
_RUNTIME_CLOSE_TIMEOUT = 5.0


def _local_repository_url(source: Path) -> str:
    """Use a local checkout for cloning while retaining its publishable origin."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            shell=False,
        )
        if completed.returncode == 0:
            value = completed.stdout.decode("utf-8", "strict").strip()
            if (
                value
                and len(value.encode("utf-8", "strict")) <= 4096
                and "\x00" not in value
                and not any(character in "\r\n" for character in value)
            ):
                return value
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass
    return str(source)


class TuiControllerError(RuntimeError):
    """Fixed-message failure at the interactive application boundary."""


class StaleCandidateError(TuiControllerError):
    """The one-shot defect candidate capability is invalid or expired."""


class ApprovalMembersError(TuiControllerError):
    """Only fixed, non-sensitive member lookup diagnostics may cross the UI."""


class PublicationConfigurationError(TuiControllerError):
    """Publication requires the user to configure provider credentials."""


class StaleTuiActionError(TuiControllerError):
    """The authoritative workflow no longer matches the reviewed facts."""


class _AsyncRuntimeError(RuntimeError):
    """Internal fixed-message signal for async runtime lifecycle failures."""


class _AsyncRuntime:
    """Own one stable event loop for all async calls made by a controller."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._ready = Event()
        self._stopped = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._start_error = False
        self._closed = False
        self._futures: set[Future[object]] = set()
        self._coroutines: dict[Future[object], object] = {}

    def submit(self, coroutine):
        try:
            self._ensure_started()
        except Exception:
            coroutine.close()
            raise
        with self._lock:
            thread = self._thread
            loop = self._loop
            if thread is not None and current_thread() is thread:
                coroutine.close()
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            if self._closed or loop is None or not loop.is_running():
                coroutine.close()
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except Exception:
                coroutine.close()
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE) from None
            self._futures.add(future)
            self._coroutines[future] = coroutine
        try:
            try:
                return future.result()
            except CancelledError:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE) from None
        finally:
            with self._lock:
                self._futures.discard(future)
                self._coroutines.pop(future, None)

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is not None and current_thread() is thread:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            first_close = not self._closed
            self._closed = True
            futures = tuple(self._futures) if first_close else ()
        if not first_close:
            if thread is not None and not self._stopped.wait(_RUNTIME_CLOSE_TIMEOUT):
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            return
        if thread is None:
            self._stopped.set()
            return
        if not self._ready.wait(_RUNTIME_CLOSE_TIMEOUT):
            raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
        _, unfinished = wait(futures, timeout=_RUNTIME_CLOSE_TIMEOUT)
        if unfinished:
            for future in unfinished:
                future.cancel()
            wait(unfinished, timeout=_RUNTIME_CLOSE_TIMEOUT)
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        thread.join(_RUNTIME_CLOSE_TIMEOUT)
        if thread.is_alive() or not self._stopped.is_set():
            raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)

    def _ensure_started(self) -> None:
        with self._lock:
            if self._closed or self._start_error:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name=f"ones-tui-async-{id(self):x}",
                    daemon=True,
                )
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._start_error = True
                    self._ready.set()
                    self._stopped.set()
                    raise _AsyncRuntimeError(_QUERY_UNAVAILABLE) from None
        if not self._ready.wait(_RUNTIME_CLOSE_TIMEOUT):
            raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
        with self._lock:
            if self._start_error or self._loop is None:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)

    def _run(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop
                closed = self._closed
            self._ready.set()
            if not closed:
                loop.run_forever()
        except Exception:
            with self._lock:
                self._start_error = True
            self._ready.set()
        finally:
            if loop is not None:
                pending = asyncio.all_tasks(loop)
                task_coroutines = {task.get_coro() for task in pending}
                with self._lock:
                    submissions = tuple(
                        (future, self._coroutines.get(future))
                        for future in self._futures
                    )
                for future, coroutine in submissions:
                    future.cancel()
                    if coroutine is not None and coroutine not in task_coroutines:
                        coroutine.close()
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()
            self._stopped.set()


@dataclass(frozen=True, slots=True)
class CandidateSessionView:
    session_id: str
    items: tuple[DefectChoice, ...]


@dataclass(frozen=True, slots=True)
class _CandidateSession:
    project: str
    iteration: str
    assignee: str
    snapshot_token: str
    candidate_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _RequirementSession:
    candidate_ids: frozenset[str]


class TuiController:
    """Adapt workflow services to immutable TUI view models."""

    def __init__(
        self,
        orchestrator: DeveloperWorkflowOrchestrator,
        run_index: RunIndex,
        *,
        max_candidate_sessions: int = 8,
        workflow_saver: Callable[[DeveloperWorkflowConfig], None] | None = None,
        publishing_enabled: bool = True,
    ) -> None:
        if type(max_candidate_sessions) is not int or max_candidate_sessions <= 0:
            raise TuiControllerError("candidate session capacity is invalid")
        if type(publishing_enabled) is not bool:
            raise TuiControllerError("publishing capability is invalid")
        self._orchestrator = orchestrator
        self._run_index = run_index
        self._max_candidate_sessions = max_candidate_sessions
        self._workflow_saver = workflow_saver
        self._publishing_enabled = publishing_enabled
        self._candidate_sessions: OrderedDict[str, _CandidateSession] = OrderedDict()
        self._requirement_sessions: OrderedDict[str, _RequirementSession] = OrderedDict()
        self._candidate_lock = Lock()
        self._workspace_lock = Lock()
        self.schedule_store = None
        self._closed = False
        self._async_runtime = _AsyncRuntime()

    def close(self) -> None:
        """Stop the controller-owned async runtime without closing shared services."""

        with self._candidate_lock:
            self._closed = True
            self._candidate_sessions.clear()
            self._requirement_sessions.clear()
        try:
            self._async_runtime.close()
        except _AsyncRuntimeError:
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None

    def list_runs(
        self,
        filters: RunFilter,
        activities: Mapping[str, RunActivity] | None = None,
    ) -> tuple[RunSummary, ...]:
        try:
            return self._run_index.list(filters, activities)
        except Exception:
            raise TuiControllerError(_LIST_ERROR) from None

    def delete_task(self, run_id: str) -> None:
        """Delete one local task record without touching repositories or ONES."""

        try:
            self._run_index.delete(run_id)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError):
            raise
        except Exception:
            raise TuiControllerError("task could not be deleted safely") from None

    def list_workspace_runs(self, workspace: WorkspaceSummary) -> tuple[RunSummary, ...]:
        """Only include runs with an authoritative mapping to this workspace."""
        try:
            return self._run_index.list(RunFilter(), workspace=workspace)
        except Exception:
            raise TuiControllerError("workspace tasks are unavailable") from None

    @property
    def default_defect_project(self) -> str:
        """Expose the single configured project as a value-only form default."""

        try:
            projects = {
                item.project_id
                for item in self._orchestrator.config.normalized_groups()
                if type(item.project_id) is str and item.project_id
            }
        except Exception:
            return ""
        return next(iter(projects)) if len(projects) == 1 else ""

    def list_workspaces(self) -> tuple[WorkspaceSummary, ...]:
        """Return configured repository groups as top-level workspaces."""

        try:
            config = self._orchestrator.config
            workspaces = [
                WorkspaceSummary(
                    key=safe_tui_text(group.key, maximum=128),
                    display_name=getattr(config, "workspace_names", {}).get(group.key, ""),
                    project_id=safe_tui_text(group.project_id, maximum=128),
                    iteration_id=safe_tui_text(group.iteration_id, maximum=128),
                    repositories=tuple(
                        safe_tui_text(item.repo_name, maximum=128)
                        for item in group.repositories
                    ),
                )
                for group in config.repository_groups
            ]
            grouped = {
                item.key
                for group in config.repository_groups
                for item in group.repositories
            }
            workspaces.extend(
                WorkspaceSummary(
                    key=safe_tui_text(item.key, maximum=128),
                    display_name=getattr(config, "workspace_names", {}).get(item.key, ""),
                    project_id=safe_tui_text(item.project_id, maximum=128),
                    iteration_id=safe_tui_text(item.iteration_id, maximum=128),
                    repositories=(safe_tui_text(item.repo_name, maximum=128),),
                )
                for item in config.repositories
                if item.key not in grouped and item.project_id != "pending-project"
            )
            return tuple(workspaces)
        except Exception:
            raise TuiControllerError("workspace list is unavailable") from None

    def load_workspace_projects(self) -> tuple[FilterChoice, ...]:
        """Load selectable ONES projects without exposing raw payloads."""

        gateway = getattr(self._orchestrator.defect_candidates, "gateway", None)
        if gateway is None:
            raise TuiControllerError(_QUERY_UNAVAILABLE)
        try:
            projects = self._async_runtime.submit(gateway.list_projects())
            return self._named_choices(projects, name_keys=("name", "title", "key"))
        except (_AsyncRuntimeError, Exception):
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None

    def load_workspace_iterations(self, project_id: str) -> tuple[FilterChoice, ...]:
        """Load iterations for one selected ONES project."""

        if type(project_id) is not str or not project_id:
            raise TuiControllerError(_QUERY_UNAVAILABLE)
        gateway = getattr(self._orchestrator.defect_candidates, "gateway", None)
        if gateway is None:
            raise TuiControllerError(_QUERY_UNAVAILABLE)
        try:
            iterations = self._async_runtime.submit(
                gateway.list_iterations(project_id)
            )
            return self._named_choices(
                iterations, name_keys=("title", "name", "key")
            )
        except (_AsyncRuntimeError, Exception):
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None

    @staticmethod
    def _named_choices(
        values: object, *, name_keys: tuple[str, ...]
    ) -> tuple[FilterChoice, ...]:
        if not isinstance(values, list):
            raise ValueError
        choices: dict[str, FilterChoice] = {}
        for value in values:
            if not isinstance(value, dict):
                raise ValueError
            identity = value.get("uuid", value.get("id"))
            name = next(
                (
                    value.get(key)
                    for key in name_keys
                    if type(value.get(key)) is str and value.get(key).strip()
                ),
                None,
            )
            if type(identity) is not str or not identity or type(name) is not str:
                raise ValueError
            choices[identity] = FilterChoice(
                id=validate_tui_input_text(identity, maximum=128),
                name=safe_tui_text(name, maximum=256),
            )
        return tuple(sorted(choices.values(), key=lambda item: item.name.casefold()))

    def create_workspace(
        self,
        key: str,
        project_id: str,
        iteration_id: str,
        repositories: tuple[WorkspaceRepositoryInput, ...],
        *, display_name: str = "",
    ) -> WorkspaceSummary:
        """Persist one project/iteration workspace containing one or more repos."""

        saver = self._workflow_saver
        if (
            saver is None
            or type(key) is not str
            or type(project_id) is not str
            or type(iteration_id) is not str
            or type(repositories) is not tuple
            or not repositories
            or any(type(item) is not WorkspaceRepositoryInput for item in repositories)
        ):
            raise TuiControllerError("workspace configuration is unavailable")
        try:
            project_id = validate_tui_input_text(project_id, maximum=128)
            iteration_id = validate_tui_input_text(iteration_id, maximum=128)
            mappings: list[RepositoryMapping] = []
            for index, item in enumerate(repositories):
                source_path: Path | None = None
                repo_url = item.source
                if item.local:
                    source_path = Path(item.source).resolve(strict=True)
                    if not source_path.is_dir() or not (source_path / ".git").exists():
                        raise ValueError
                    repo_url = _local_repository_url(source_path)
                mappings.append(
                    RepositoryMapping(
                        key=item.key,
                        project_id=project_id,
                        iteration_id=iteration_id,
                        repo_url=repo_url,
                        repo_name=item.name,
                        base_branch=item.branch,
                        source_path=source_path,
                        role=(
                            RepositoryRole.PRIMARY
                            if index == 0
                            else RepositoryRole.DEPENDENCY
                        ),
                    )
                )
            group = RepositoryGroupMapping(
                key=key,
                project_id=project_id,
                iteration_id=iteration_id,
                primary_repository=mappings[0].key,
                repositories=tuple(mappings),
            )
            with self._workspace_lock:
                current = self._orchestrator.config
                if any(item.key == key for item in current.repository_groups):
                    raise ValueError
                data = current.model_dump(mode="python", round_trip=True)
                data["repository_groups"] = (*current.repository_groups, group)
                if display_name:
                    data["workspace_names"] = {**current.workspace_names, key: display_name}
                candidate = DeveloperWorkflowConfig.model_validate(data)
                saver(candidate)
                current.repository_groups = candidate.repository_groups
                current.workspace_names = candidate.workspace_names
                return WorkspaceSummary(
                    key=safe_tui_text(group.key, maximum=128),
                    display_name=candidate.workspace_names.get(group.key, ""),
                    project_id=safe_tui_text(group.project_id, maximum=128),
                    iteration_id=safe_tui_text(group.iteration_id, maximum=128),
                    repositories=tuple(
                        safe_tui_text(item.repo_name, maximum=128)
                        for item in group.repositories
                    ),
                )
        except (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError):
            raise
        except Exception:
            raise TuiControllerError("workspace configuration could not be saved") from None

    def rename_workspace(self, key: str, display_name: str) -> WorkspaceSummary:
        """Persist display metadata without changing mapping or workflow identities."""
        try:
            display_name = validate_tui_input_text(display_name, maximum=128).strip()
            if not display_name or self._workflow_saver is None:
                raise ValueError()
            with self._workspace_lock:
                if not any(w.key == key for w in self.list_workspaces()):
                    raise ValueError()
                current = self._orchestrator.config
                data = current.model_dump(mode="python", round_trip=True)
                data["workspace_names"] = {**current.workspace_names, key: display_name}
                candidate = DeveloperWorkflowConfig.model_validate(data)
                self._workflow_saver(candidate)
                current.workspace_names = candidate.workspace_names
                return next(w for w in self.list_workspaces() if w.key == key)
        except Exception:
            raise TuiControllerError("工作区名称保存失败，请检查名称或配置权限") from None

    @serialized_mutation
    def delete_workspace(self, key: str) -> None:
        """Remove one workspace mapping without touching repositories or ONES data."""

        saver = self._workflow_saver
        if saver is None or type(key) is not str:
            raise TuiControllerError("workspace configuration is unavailable")
        try:
            key = validate_tui_input_text(key, maximum=128)
            with self._workspace_lock:
                current = self._orchestrator.config
                matches = tuple(
                    item for item in current.repository_groups if item.key == key
                )
                if len(matches) != 1:
                    raise ValueError
                data = current.model_dump(mode="python", round_trip=True)
                data["repository_groups"] = tuple(
                    item for item in current.repository_groups if item.key != key
                )
                data["workspace_names"] = {k: v for k, v in current.workspace_names.items() if k != key}
                candidate = DeveloperWorkflowConfig.model_validate(data)
                saver(candidate)
                current.repository_groups = candidate.repository_groups
                current.workspace_names = candidate.workspace_names
                if self.schedule_store is not None:
                    for plan in self.schedule_store.list(key):
                        self.schedule_store.save(plan.model_copy(update={"enabled": False}),
                                                 expected_version=plan.version)
        except (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError):
            raise
        except Exception:
            raise TuiControllerError("workspace configuration could not be saved") from None

    def show(self, run_id: str) -> RunDetail:
        try:
            return self._detail(
                self._orchestrator.show(run_id, read_only=True)
            )
        except Exception:
            raise TuiControllerError(_DISPLAY_ERROR) from None

    def ai_activity(self, run_id: str) -> tuple[str, ...]:
        """Return only bounded, display-safe observable AI actions."""

        try:
            values = self._orchestrator.ai_activity(run_id)
            return tuple(safe_tui_text(value, maximum=512) for value in values[-40:])
        except (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError):
            raise
        except Exception:
            return ()

    def query_defects(
        self,
        project: str,
        iteration: str,
        assignee: str,
        status_ids: tuple[str, ...],
    ) -> CandidateSessionView:
        with self._candidate_lock:
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
        try:
            candidates = self._async_runtime.submit(
                self._orchestrator.defect_candidates.list_candidates(
                    project,
                    iteration,
                    assignee,
                    status_ids=None if status_ids == () else status_ids,
                )
            )
            items = tuple(DefectChoice.from_candidate(item) for item in candidates)
            tokens = {item.snapshot_token for item in candidates}
            if candidates and (
                len(tokens) != 1
                or any(type(token) is not str or not token for token in tokens)
                or len({item.candidate_id for item in items}) != len(items)
            ):
                raise ValueError
        except _AsyncRuntimeError:
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None
        except Exception:
            raise TuiControllerError(_CANDIDATE_ERROR) from None

        session_id = secrets.token_urlsafe(32)
        with self._candidate_lock:
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
            if not candidates:
                return CandidateSessionView(session_id=session_id, items=())
            session = _CandidateSession(
                project=project,
                iteration=iteration,
                assignee=assignee,
                snapshot_token=next(iter(tokens)),
                candidate_ids=frozenset(item.candidate_id for item in items),
            )
            while session_id in self._candidate_sessions:
                session_id = secrets.token_urlsafe(32)
            self._candidate_sessions[session_id] = session
            while len(self._candidate_sessions) > self._max_candidate_sessions:
                self._candidate_sessions.popitem(last=False)
        return CandidateSessionView(session_id=session_id, items=items)

    def load_defect_filter_options(self, project: str) -> DefectFilterOptions:
        """Read project-scoped iteration, member, and open-status choices."""

        if type(project) is not str or not project.strip():
            raise TuiControllerError(_QUERY_UNAVAILABLE)
        candidates = self._orchestrator.defect_candidates
        gateway = getattr(candidates, "gateway", None)
        issue_type = getattr(candidates, "issue_type_id", None)
        if gateway is None or type(issue_type) is not str or not issue_type:
            raise TuiControllerError(_QUERY_UNAVAILABLE)

        async def load() -> tuple[object, object, object, tuple[str, ...]]:
            unavailable: list[str] = []
            try:
                iterations = await gateway.list_iterations(project)
            except Exception:
                iterations = []
                unavailable.append("iterations")
            try:
                roles = await gateway.list_role_members(project)
            except Exception:
                roles = []
                unavailable.append("project roles")
            try:
                current_user_id = await gateway.get_current_user_id()
            except Exception:
                current_user_id = ""
                unavailable.append("current user")
            member_ids = sorted(
                {
                    identity
                    for role in roles
                    if isinstance(role, dict) and isinstance(role.get("members"), list)
                    for member in role["members"]
                    for identity in (
                        member
                        if type(member) is str
                        else member.get("uuid", member.get("id"))
                        if isinstance(member, dict)
                        else None,
                    )
                    if type(identity) is str and identity
                }
            )
            if current_user_id and current_user_id not in member_ids:
                member_ids.append(current_user_id)
                member_ids.sort()
            try:
                members = await gateway.list_team_members(
                    uuids=member_ids if member_ids else None
                )
                if not members and member_ids:
                    members = await gateway.list_team_members(uuids=None)
            except Exception:
                members = []
                unavailable.append("users")
            if not members and current_user_id:
                members = [{"uuid": current_user_id, "name": "Current user"}]
            try:
                statuses = await gateway.list_defect_statuses(project, issue_type)
            except Exception:
                statuses = []
                unavailable.append("statuses")
            return iterations, members, (statuses, current_user_id), tuple(unavailable)

        try:
            iterations, members, status_payload, unavailable = self._async_runtime.submit(load())
            statuses, current_user_id = status_payload

            def raw_choices(
                values: object, *, name_keys: tuple[str, ...]
            ) -> tuple[FilterChoice, ...]:
                if not isinstance(values, list):
                    raise ValueError
                choices: dict[str, FilterChoice] = {}
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    identity = value.get("uuid", value.get("id"))
                    name = next(
                        (
                            value.get(key)
                            for key in name_keys
                            if type(value.get(key)) is str and value.get(key).strip()
                        ),
                        None,
                    )
                    if type(identity) is not str or not identity or type(name) is not str:
                        continue
                    choices[identity] = FilterChoice(
                        id=validate_tui_input_text(identity, maximum=128),
                        name=safe_tui_text(name, maximum=256),
                    )
                return tuple(sorted(choices.values(), key=lambda item: item.name.casefold()))

            iteration_choices = raw_choices(
                iterations, name_keys=("title", "name", "key")
            )
            assignee_choices = tuple(
                FilterChoice(
                    id=item.id,
                    name=item.name,
                    selected=item.id == current_user_id,
                )
                for item in raw_choices(members, name_keys=("name", "email"))
            )
            status_choices = tuple(
                FilterChoice(
                    id=validate_tui_input_text(status.id, maximum=128),
                    name=safe_tui_text(status.name or status.id, maximum=256),
                    selected=status.default is True,
                )
                for status in statuses
                if type(status.category) is str
                and status.category.strip().casefold()
                in {"open", "todo", "to_do", "doing", "in_progress", "pending"}
            )
            return DefectFilterOptions(
                iterations=iteration_choices,
                assignees=assignee_choices,
                statuses=status_choices,
                unavailable=unavailable,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None

    def start_defect(self, session_id: str, candidate_id: str) -> RunDetail:
        return self._start_defect(session_id, candidate_id, analyze_only=False)

    def analyze_defect(self, session_id: str, candidate_id: str) -> RunDetail:
        """Start a read-only AI root-cause analysis for one selected defect."""

        return self._start_defect(session_id, candidate_id, analyze_only=True)

    @serialized_mutation
    def _start_defect(
        self,
        session_id: str,
        candidate_id: str,
        *,
        analyze_only: bool,
    ) -> RunDetail:
        if (
            type(session_id) is not str
            or not session_id
            or type(candidate_id) is not str
            or not candidate_id
        ):
            raise StaleCandidateError(_CANDIDATE_ERROR)
        with self._candidate_lock:
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
            session = self._candidate_sessions.pop(session_id, None)
            if session is None or candidate_id not in session.candidate_ids:
                raise StaleCandidateError(_CANDIDATE_ERROR)
        try:
            args = (
                session.project,
                session.iteration,
                session.assignee,
                session.snapshot_token,
                candidate_id,
            )
            if analyze_only:
                run = self._orchestrator.start_defect(
                    *args,
                    action=DefectAction.ANALYZE,
                )
            else:
                run = self._orchestrator.start_defect(*args)
            return self._detail(run)
        except (DefectCandidateError, InvalidWorkflowAction):
            raise StaleCandidateError(_CANDIDATE_ERROR) from None
        except (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError):
            raise
        except Exception:
            raise TuiControllerError(_DEFECT_START_ERROR) from None

    def query_requirements(
        self,
        project: str,
        iteration: str,
        assignee: str,
        status_ids: tuple[str, ...],
        issue_type_id: str,
    ) -> tuple[str, tuple[RequirementChoice, ...]]:
        with self._candidate_lock:
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
        gateway = getattr(getattr(self._orchestrator, "requirement_flow", None), "gateway", None)
        list_requirements = getattr(gateway, "list_requirements", None)
        if not callable(list_requirements):
            raise TuiControllerError(_QUERY_UNAVAILABLE)
        try:
            records = self._async_runtime.submit(
                list_requirements(
                    project_id=project or None,
                    issue_type_id=issue_type_id or None,
                    sprint_id=iteration or None,
                    assignee=assignee or None,
                    status_ids=None if status_ids == () else status_ids,
                )
            )
            items = tuple(RequirementChoice.from_requirement(item) for item in records)
            if len({item.requirement_id for item in items}) != len(items):
                raise ValueError
        except _AsyncRuntimeError:
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None
        except Exception:
            raise TuiControllerError(_CANDIDATE_ERROR) from None
        session_id = secrets.token_urlsafe(32)
        with self._candidate_lock:
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
            self._requirement_sessions[session_id] = _RequirementSession(
                candidate_ids=frozenset(item.requirement_id for item in items)
            )
            while len(self._requirement_sessions) > self._max_candidate_sessions:
                self._requirement_sessions.popitem(last=False)
        return session_id, items

    def start_requirement(self, requirement_id: str, session_id: str | None = None) -> RunDetail:
        if session_id is not None:
            with self._candidate_lock:
                session = self._requirement_sessions.pop(session_id, None)
                if session is None or requirement_id not in session.candidate_ids:
                    raise TuiControllerError(_CANDIDATE_ERROR)
        return self._command(self._orchestrator.start_requirement, requirement_id)

    def confirm_repository(
        self, run_id: str, mapping_key: str, expected_version: int
    ) -> RunDetail:
        return self._command(
            self._orchestrator.confirm_repository,
            run_id,
            mapping_key,
            expected_version=expected_version,
        )

    def accept_analysis_solution(
        self, run_id: str, expected_version: int
    ) -> RunDetail:
        return self._command(
            self._orchestrator.accept_analysis_solution,
            run_id,
            expected_version=expected_version,
        )

    def regenerate_analysis_solution(
        self, run_id: str, expected_version: int
    ) -> RunDetail:
        return self._command(
            self._orchestrator.regenerate_analysis_solution,
            run_id,
            expected_version=expected_version,
        )

    def verification_nodes(self) -> tuple[dict, ...]:
        return self._orchestrator.verification_nodes()

    def verification_repositories(self) -> tuple[str, ...]:
        """Public repository identifiers for the node recipe form."""
        config = self._orchestrator.config
        return tuple(dict.fromkeys([
            *(repository.key for repository in config.repositories),
            *(repository.key for group in config.repository_groups for repository in group.repositories),
        ]))

    def save_verification_nodes(self, raw: str, expected_digest: str) -> None:
        """Persist node configuration through the existing owned workflow saver."""
        try:
            if self._workflow_saver is None or type(raw) is not str or len(raw) > 128 * 1024:
                raise ValueError
            nodes = json.loads(raw)
            if type(nodes) is not list:
                raise ValueError
            with self._workspace_lock:
                current = self._orchestrator.config
                if digest(self.verification_nodes()) != expected_digest:
                    raise ValueError
                candidate = current.validated_update(verification_nodes=nodes)
                self._workflow_saver(candidate)
                current.verification_nodes = candidate.verification_nodes
                self._orchestrator.defect_flow.config.verification_nodes = candidate.verification_nodes
                self._orchestrator.requirement_flow.config.verification_nodes = candidate.verification_nodes
        except Exception:
            raise TuiControllerError("节点配置未保存：请检查格式、保存权限，或重新打开以加载最新配置。") from None

    def replan_verification(self, run_id: str, expected_version: int) -> RunDetail:
        return self._command(self._orchestrator.replan_verification, run_id, expected_version=expected_version)

    def verify(self, run_id: str, task_key: str, actor: str, expected_version: int,
               manual_evidence: str | None = None, passed: bool = True, expected_recipe_digest: str | None = None) -> RunDetail:
        return self._command(self._orchestrator.verify, run_id, task_key, actor,
            expected_version=expected_version, manual_evidence=manual_evidence, passed=passed,
            expected_recipe_digest=expected_recipe_digest)

    def resume(self, run_id: str, expected_version: int) -> RunDetail:
        try:
            run = self._orchestrator.show(run_id, read_only=True)
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None
        if (
            run.state in {WorkflowState.PARTIAL_SUCCESS, WorkflowState.PUBLISHING}
            or (
                run.state is WorkflowState.BLOCKED
                and run.resume_state is WorkflowState.PUBLISHING
            )
        ):
            raise TuiControllerError(_ACTION_UNAVAILABLE)
        return self._command(
            self._orchestrator.resume, run_id, expected_version=expected_version
        )

    def prepare_action(
        self,
        run_id: str,
        action: Literal["approve", "revise", "cancel", "resume-publication"],
    ) -> DangerousActionRequest:
        if action in {"approve", "resume-publication"} and not self._publishing_enabled:
            raise TuiControllerError(_ACTION_UNAVAILABLE)
        if action in {"approve", "resume-publication"}:
            self._check_publication_credentials()
        try:
            request = DangerousActionRequest.from_run(
                self._orchestrator.show(run_id, read_only=True), action=action
            )
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None
        if action == "approve":
            try:
                return replace(request, approvers=self.load_approval_members(run_id))
            except ApprovalMembersError as exc:
                return replace(request, approver_error=str(exc))
        return request

    def load_approval_members(self, run_id: str | None = None) -> tuple[FilterChoice, ...]:
        """Use the configured ONES team directory, never free-form identities."""
        try:
            gateway = getattr(self._orchestrator.defect_candidates, "gateway", None)
            if gateway is None:
                gateway = self._orchestrator.requirement_flow.gateway
            project_id = ""
            if run_id is not None:
                run = self._orchestrator.show(run_id, read_only=True)
                project_id = run.project_id
                if not project_id:
                    item = run.defect or run.requirement
                    project_id = item.project.id if item and item.project else ""

            async def load():
                if not project_id:
                    return await gateway.list_team_members(uuids=None)
                roles = await gateway.list_role_members(project_id)
                if not isinstance(roles, list):
                    raise ApprovalMembersError("ONES 项目成员响应格式异常，请重新加载。")
                identities = set()
                for role in roles:
                    if not isinstance(role, dict) or not isinstance(role.get("members"), list):
                        continue
                    for member in role["members"]:
                        identity = member if isinstance(member, str) else member.get("uuid", member.get("id")) if isinstance(member, dict) else None
                        if isinstance(identity, str) and identity:
                            identities.add(validate_tui_input_text(identity, maximum=128))
                if not identities:
                    raise ApprovalMembersError("ONES 项目未返回可选成员，请检查该项目的成员配置与访问权限。")
                members = []
                ordered = sorted(identities)
                for offset in range(0, len(ordered), 100):
                    batch = await gateway.list_team_members(uuids=ordered[offset:offset + 100])
                    if not isinstance(batch, list):
                        raise ApprovalMembersError("ONES 成员资料响应格式异常，请重新加载。")
                    members.extend(item for item in batch if isinstance(item, dict)
                                   and item.get("uuid", item.get("id")) in identities)
                return members

            members = self._async_runtime.submit(load())
            if not isinstance(members, list):
                raise ValueError
            choices: dict[str, FilterChoice] = {}
            for member in members:
                if not isinstance(member, dict):
                    continue
                identity = member.get("uuid", member.get("id"))
                name = member.get("name")
                if type(identity) is not str or not identity or type(name) is not str or not name.strip():
                    continue
                identity = validate_tui_input_text(identity, maximum=128)
                choices[identity] = FilterChoice(id=identity, name=safe_tui_text(name, maximum=256))
            if not choices:
                raise ApprovalMembersError("ONES 未返回可选成员资料，请检查项目成员是否具有可见的姓名和成员 ID。")
            return tuple(sorted(choices.values(), key=lambda item: (item.name.casefold(), item.id)))
        except ApprovalMembersError:
            raise
        except Exception:
            raise ApprovalMembersError("ONES 成员接口调用失败，请检查连接与账号权限后重新打开审批。") from None

    def approve(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        if not self._publishing_enabled:
            raise TuiControllerError(_ACTION_UNAVAILABLE)
        self._check_publication_credentials()
        self._assert_request(request, "approve")
        if actor not in {member.id for member in request.approvers}:
            raise TuiControllerError("请从 ONES 成员列表选择审批人")
        if actor not in {member.id for member in self.load_approval_members(request.run_id)}:
            raise TuiControllerError("所选 ONES 成员已不可用，请重新打开审批")
        return self._dangerous(
            self._orchestrator.approve,
            request.run_id,
            actor,
            expected_version=request.version,
        )

    def revise(
        self,
        request: DangerousActionRequest,
        feedback: str,
        scope: Literal["implementation", "repair"] | None,
    ) -> RunDetail:
        self._assert_request(request, "revise")
        return self._dangerous(
            self._orchestrator.revise,
            request.run_id,
            feedback,
            scope=scope,
            expected_version=request.version,
        )

    def cancel(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        self._assert_request(request, "cancel")
        return self._dangerous(
            self._orchestrator.cancel,
            request.run_id,
            actor,
            expected_version=request.version,
        )

    def resume_publication(self, request: DangerousActionRequest) -> RunDetail:
        if not self._publishing_enabled:
            raise TuiControllerError(_ACTION_UNAVAILABLE)
        self._check_publication_credentials()
        self._assert_request(request, "resume-publication")
        return self._dangerous(
            self._orchestrator.resume,
            request.run_id,
            expected_version=request.version,
        )

    def _check_publication_credentials(self) -> None:
        client = getattr(getattr(self._orchestrator, "publisher", None), "pr_client", None)
        check = getattr(client, "validate_credentials", None)
        if callable(check):
            try:
                check()
            except Exception:
                raise PublicationConfigurationError("请先到 Configuration → GitHub / GitLab，设置平台令牌。") from None

    def _assert_request(self, request: DangerousActionRequest, action: str) -> None:
        if not isinstance(request, DangerousActionRequest) or request.action != action:
            raise TuiControllerError(_ACTION_ERROR)
        try:
            run = self._orchestrator.show(request.run_id, read_only=True)
        except Exception:
            raise TuiControllerError(_ACTION_UNAVAILABLE) from None
        try:
            request.assert_current(run)
        except TuiDisplayError as exc:
            if str(exc) == "workflow action is stale":
                raise StaleTuiActionError(_STALE) from None
            raise TuiControllerError(_ACTION_UNAVAILABLE) from None
        except Exception:
            raise TuiControllerError(_ACTION_UNAVAILABLE) from None

    def _dangerous(self, command, *args, **kwargs) -> RunDetail:
        try:
            return self._detail(command(*args, **kwargs))
        except InvalidWorkflowAction as exc:
            if str(exc) == _STALE:
                raise StaleTuiActionError(_STALE) from None
            raise TuiControllerError(_ACTION_ERROR) from None
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None

    @serialized_mutation
    def _command(self, command, *args, **kwargs) -> RunDetail:
        try:
            return self._detail(command(*args, **kwargs))
        except InvalidWorkflowAction as exc:
            if str(exc) == _STALE:
                raise StaleTuiActionError(_STALE) from None
            raise TuiControllerError(_ACTION_ERROR) from None
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None

    def _detail(self, run: WorkflowRun) -> RunDetail:
        try:
            detail = RunDetail.from_run(run)
            publishing = getattr(getattr(self._orchestrator, "config", None), "publishing", None)
            can_defer = (detail.can_verify and bool(detail.verification_tasks)
                         and getattr(publishing, "defer_external_verification_to_pr", False) is True
                         and all(task.status in {"passed", "manual", "ready", "waiting_environment"}
                                 for task in detail.verification_tasks))
            return replace(detail, ai_activity=self.ai_activity(run.run_id), can_defer_verification=can_defer)
        except (TuiDisplayError, TypeError, AttributeError):
            raise TuiControllerError(_ACTION_ERROR) from None


__all__ = [
    "CandidateSessionView",
    "StaleCandidateError",
    "StaleTuiActionError",
    "TuiController",
    "TuiControllerError",
]
