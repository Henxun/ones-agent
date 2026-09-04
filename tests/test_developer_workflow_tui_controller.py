from __future__ import annotations

from dataclasses import fields
from contextlib import contextmanager
import asyncio
from types import SimpleNamespace
import threading

import pytest

import src.developer_workflow.tui.controller as controller_module
from src.developer_workflow.contracts import (
    DefectCandidate,
    RepositoryMapping,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)
from src.contracts import ProjectRef, RequirementRecord, StatusRef, WorkflowStatusRef
from src.developer_workflow.orchestrator import (
    DeveloperWorkflowOrchestrator,
    InvalidWorkflowAction,
)
from src.developer_workflow.state_store import FileRunStore
from src.developer_workflow.tui.controller import (
    CandidateSessionView,
    StaleCandidateError,
    StaleTuiActionError,
    TuiController,
    TuiControllerError,
)
from src.developer_workflow.tui.models import (
    DangerousActionRequest,
    RunDetail,
    RunFilter,
    TuiDisplayError,
)
from src.developer_workflow.tui.run_index import RunIndex


class Candidates:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def list_candidates(self, project, iteration, assignee, *, status_ids=None):
        self.calls.append((project, iteration, assignee, status_ids))
        value = self.batches.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Requirements:
    async def list_requirements(self, **kwargs):
        self.kwargs = kwargs
        return [
            RequirementRecord(
                requirement_id="R-1",
                number="REQ-1",
                title="Export requirement",
                project=ProjectRef(id="P-1", name="Project"),
                iteration=ProjectRef(id="I-1", name="Iteration"),
                status=StatusRef(id="todo", name="Todo"),
            )
        ]


class LoopRecordingCandidates:
    def __init__(self):
        self.loops = []
        self.status_ids = []

    async def list_candidates(self, project, iteration, assignee, *, status_ids=None):
        assert status_ids != ()
        self.loops.append(asyncio.get_running_loop())
        self.status_ids.append(status_ids)
        await asyncio.sleep(0.01)
        return (candidate(),)


class Orchestrator:
    def __init__(self, batches=()):
        self.defect_candidates = Candidates(batches)
        self.calls = []
        self.run = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1")

    def show(self, run_id, *, read_only=False):
        self.calls.append(("show", run_id, read_only))
        return self.run

    def start_defect(self, *args):
        self.calls.append(("start_defect", *args))
        return self.run

    def start_requirement(self, item_id):
        self.calls.append(("start_requirement", item_id))
        return self.run

    def confirm_repository(self, run_id, mapping_key, *, expected_version=None):
        self.calls.append(("confirm", run_id, mapping_key, expected_version))
        return self.run

    def resume(self, run_id, *, expected_version=None):
        self.calls.append(("resume", run_id, expected_version))
        return self.run

    def cancel(self, run_id, actor, *, expected_version=None):
        self.calls.append(("cancel", run_id, actor, expected_version))
        return self.run


class Index:
    def list(self, filters, activities=None):
        return (filters, activities)


def candidate(candidate_id="D-1", token="SECRET-TOKEN"):
    return DefectCandidate(
        uuid=candidate_id,
        key="D-1",
        number="1",
        title="broken export",
        priority="high",
        status="doing",
        updated_at="2026-01-01",
        snapshot_token=token,
        status_id="doing",
    )


def test_load_defect_filter_options_reads_project_scoped_ones_metadata() -> None:
    calls: list[tuple[object, ...]] = []

    class Gateway:
        async def list_iterations(self, project: str):
            calls.append(("iterations", project))
            return [{"uuid": "iter-1", "title": "Iteration One"}]

        async def list_role_members(self, project: str):
            calls.append(("roles", project))
            return [{"members": ["user-2", "user-1", "user-1"]}]

        async def get_current_user_id(self):
            calls.append(("current-user",))
            return "user-2"

        async def list_team_members(self, *, uuids):
            calls.append(("members", tuple(uuids)))
            return [
                {"uuid": "user-2", "name": "Bob"},
                {"uuid": "user-1", "name": "Alice"},
            ]

        async def list_defect_statuses(self, project: str, issue_type: str):
            calls.append(("statuses", project, issue_type))
            return [
                WorkflowStatusRef(
                    id="todo", name="Todo", category="open", default=True
                ),
                WorkflowStatusRef(id="done", name="Done", category="done"),
            ]

    orchestrator = Orchestrator()
    orchestrator.defect_candidates = SimpleNamespace(
        gateway=Gateway(), issue_type_id="defect-type"
    )
    controller = TuiController(orchestrator, Index())  # type: ignore[arg-type]
    try:
        options = controller.load_defect_filter_options("project-1")
    finally:
        controller.close()

    assert tuple((item.id, item.name) for item in options.iterations) == (
        ("iter-1", "Iteration One"),
    )
    assert tuple((item.id, item.name) for item in options.assignees) == (
        ("user-1", "Alice"),
        ("user-2", "Bob"),
    )
    assert tuple((item.id, item.name) for item in options.statuses) == (
        ("todo", "Todo"),
    )
    assert tuple(item.id for item in options.assignees if item.selected) == (
        "user-2",
    )
    assert tuple(item.id for item in options.statuses if item.selected) == ("todo",)
    assert calls == [
        ("iterations", "project-1"),
        ("roles", "project-1"),
        ("current-user",),
        ("members", ("user-1", "user-2")),
        ("statuses", "project-1", "defect-type"),
    ]


def test_disabled_publishing_rejects_prepare_and_resume_without_side_effects() -> None:
    orchestrator = Orchestrator()
    orchestrator.run = orchestrator.run.validated_update(
        state=WorkflowState.PUBLISHING,
    )
    controller = TuiController(
        orchestrator,  # type: ignore[arg-type]
        Index(),  # type: ignore[arg-type]
        publishing_enabled=False,
    )
    try:
        with pytest.raises(TuiControllerError, match="workflow action is unavailable"):
            controller.prepare_action(orchestrator.run.run_id, "resume-publication")
        with pytest.raises(TuiControllerError, match="workflow action is unavailable"):
            controller.resume_publication(object())  # type: ignore[arg-type]
    finally:
        controller.close()

    assert orchestrator.calls == []


def test_load_defect_filter_options_keeps_partial_ones_metadata() -> None:
    class Gateway:
        async def list_iterations(self, project: str):
            assert project == "project-1"
            return [None, {"uuid": "iter-1", "title": "Iteration One"}]

        async def list_role_members(self, project: str):
            raise RuntimeError("role endpoint unavailable")

        async def get_current_user_id(self):
            return "user-2"

        async def list_team_members(self, *, uuids):
            raise RuntimeError("member endpoint unavailable")

        async def list_defect_statuses(self, project: str, issue_type: str):
            raise RuntimeError("status endpoint unavailable")

    orchestrator = Orchestrator()
    orchestrator.defect_candidates = SimpleNamespace(
        gateway=Gateway(), issue_type_id="defect-type"
    )
    controller = TuiController(orchestrator, Index())  # type: ignore[arg-type]
    try:
        options = controller.load_defect_filter_options("project-1")
    finally:
        controller.close()

    assert tuple((item.id, item.name) for item in options.iterations) == (
        ("iter-1", "Iteration One"),
    )
    assert tuple((item.id, item.name, item.selected) for item in options.assignees) == (
        ("user-2", "Current user", True),
    )
    assert options.statuses == ()
    assert options.unavailable == ("project roles", "users", "statuses")


def test_show_projects_persisted_repository_candidates_without_config_access() -> None:
    mapping = RepositoryMapping(
        key="primary",
        project_id="P",
        iteration_id="I",
        repo_url="https://git.example.invalid/team/primary.git",
        repo_name="primary",
        test_commands=("uv run pytest",),
        allowed_paths=("src",),
    )
    orchestrator = Orchestrator()
    orchestrator.run = WorkflowRun.new(
        WorkflowType.REQUIREMENT, "REQ-1"
    ).validated_update(
        state=WorkflowState.VALIDATING,
        repository_candidates=(mapping,),
    )
    controller = TuiController(orchestrator, Index())  # type: ignore[arg-type]

    detail = controller.show(orchestrator.run.run_id)

    assert orchestrator.calls[-1] == ("show", orchestrator.run.run_id, True)
    assert tuple(item.key for item in detail.mapping_candidates) == ("primary",)
    assert detail.mapping_candidates[0].repositories[0].test_summary == (
        "1 configured test command"
    )
    assert "git.example.invalid" not in repr(detail.mapping_candidates)


def test_query_hides_token_and_forwards_exact_status_ids():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())

    view = controller.query_defects("P", "I", "A", ("doing", "todo"))

    assert isinstance(view, CandidateSessionView)
    assert orchestrator.defect_candidates.calls == [("P", "I", "A", ("doing", "todo"))]
    assert "SECRET-TOKEN" not in repr(view)
    assert all(field.name != "snapshot_token" for field in fields(view))
    assert view.items[0].candidate_id == "D-1"


def test_query_requirements_uses_bounded_gateway_session():
    orchestrator = Orchestrator()
    gateway = Requirements()
    orchestrator.requirement_flow = SimpleNamespace(gateway=gateway)
    controller = TuiController(orchestrator, Index())
    try:
        session_id, items = controller.query_requirements(
            "P", "I", "A", ("todo",), "requirement-type"
        )
        assert session_id
        assert items[0].requirement_id == "R-1"
        assert items[0].number == "REQ-1"
        assert gateway.kwargs == {
            "project_id": "P",
            "issue_type_id": "requirement-type",
            "sprint_id": "I",
            "assignee": "A",
            "status_ids": ("todo",),
        }
    finally:
        controller.close()


def test_query_passes_none_for_empty_status_ids():
    candidates = LoopRecordingCandidates()
    orchestrator = Orchestrator()
    orchestrator.defect_candidates = candidates
    controller = TuiController(orchestrator, Index())
    try:
        controller.query_defects("P", "I", "A", ())
        controller.query_defects("P", "I", "A", ("todo",))
        assert candidates.status_ids == [None, ("todo",)]
        assert candidates.loops[0] is candidates.loops[1]
    finally:
        controller.close()


def test_queries_from_multiple_threads_share_one_stable_event_loop_and_close():
    candidates = LoopRecordingCandidates()
    orchestrator = Orchestrator()
    orchestrator.defect_candidates = candidates
    controller = TuiController(orchestrator, Index())
    errors = []

    def query():
        try:
            controller.query_defects("P", "I", "A", ())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=query) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert errors == []
    assert len(candidates.loops) == 4
    assert len({id(loop) for loop in candidates.loops}) == 1
    runtime_thread = controller._async_runtime._thread
    runtime_loop = controller._async_runtime._loop
    controller.close()
    controller.close()
    assert not runtime_thread.is_alive()
    assert runtime_loop.is_closed()
    with pytest.raises(TuiControllerError, match="^candidate query is unavailable$"):
        controller.query_defects("P", "I", "A", ())


def test_close_revokes_existing_candidate_session_and_hidden_token():
    orchestrator = Orchestrator([(candidate(token="TOKEN-CAPABILITY"),)])
    controller = TuiController(orchestrator, Index())
    view = controller.query_defects("P", "I", "A", ())

    controller.close()

    assert controller._candidate_sessions == {}
    assert "TOKEN-CAPABILITY" not in repr(controller._candidate_sessions)
    with pytest.raises(TuiControllerError):
        controller.start_defect(view.session_id, "D-1")
    assert not any(call[0] == "start_defect" for call in orchestrator.calls)


def test_analyze_defect_forwards_explicit_read_only_action() -> None:
    from src.developer_workflow.contracts import DefectAction

    class AnalysisOrchestrator(Orchestrator):
        def start_defect(self, *args, **kwargs):
            self.calls.append(("start_defect", args, kwargs))
            return self.run

    orchestrator = AnalysisOrchestrator(
        [(candidate(token="TOKEN-ANALYSIS"),)]
    )
    controller = TuiController(orchestrator, Index())
    view = controller.query_defects("P", "I", "A", ())

    controller.analyze_defect(view.session_id, "D-1")

    assert orchestrator.calls[-1][0] == "start_defect"
    assert orchestrator.calls[-1][2] == {"action": DefectAction.ANALYZE}
    controller.close()


def test_inflight_query_cannot_publish_session_after_close_begins():
    started = threading.Event()
    release = threading.Event()

    class BarrierCandidates:
        async def list_candidates(self, *args, **kwargs):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
            return (candidate(token="TOKEN-INFLIGHT"),)

    orchestrator = Orchestrator()
    orchestrator.defect_candidates = BarrierCandidates()
    controller = TuiController(orchestrator, Index())
    query_errors = []
    close_errors = []
    query_thread = threading.Thread(
        target=lambda: _capture_query_error(controller, query_errors)
    )
    close_thread = threading.Thread(
        target=lambda: _capture_close_error(controller, close_errors)
    )
    query_thread.start()
    assert started.wait(5)
    close_thread.start()
    for _ in range(5000):
        if controller._async_runtime._closed:
            break
        threading.Event().wait(0.001)
    assert controller._async_runtime._closed
    release.set()
    query_thread.join(5)
    close_thread.join(5)

    assert not query_thread.is_alive() and not close_thread.is_alive()
    assert close_errors == []
    assert query_errors and type(query_errors[0]) is TuiControllerError
    assert str(query_errors[0]) == "candidate query is unavailable"
    assert controller._candidate_sessions == {}
    assert "TOKEN-INFLIGHT" not in repr(controller._candidate_sessions)
    with pytest.raises(TuiControllerError):
        controller.start_defect("unknown", "D-1")
    assert not any(call[0] == "start_defect" for call in orchestrator.calls)


def test_query_start_and_close_threads_finish_without_deadlock():
    orchestrator = Orchestrator([(candidate(),), (candidate(),)])
    controller = TuiController(orchestrator, Index())
    view = controller.query_defects("P", "I", "A", ())
    outcomes = []

    def invoke(operation):
        try:
            operation()
            outcomes.append("ok")
        except TuiControllerError:
            outcomes.append("rejected")

    threads = [
        threading.Thread(
            target=invoke,
            args=(lambda: controller.query_defects("P", "I", "A", ()),),
        ),
        threading.Thread(
            target=invoke,
            args=(lambda: controller.start_defect(view.session_id, "D-1"),),
        ),
        threading.Thread(target=invoke, args=(controller.close,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 3


def test_close_does_not_wait_for_start_committed_before_close():
    orchestrator = Orchestrator([
        (candidate("D-1", token="TOKEN-DISPATCH"),),
        (candidate("D-2", token="TOKEN-REMAINING"),),
    ])
    controller = TuiController(orchestrator, Index())
    committed = controller.query_defects("P", "I", "A", ())
    remaining = controller.query_defects("P", "I", "A", ())
    start_entered = threading.Event()
    release_start = threading.Event()
    close_returned = threading.Event()
    original_start = orchestrator.start_defect
    start_results = []

    def record_start(*args):
        start_entered.set()
        assert release_start.wait(5)
        return original_start(*args)

    orchestrator.start_defect = record_start
    start_thread = threading.Thread(
        target=lambda: start_results.append(
            controller.start_defect(committed.session_id, "D-1")
        )
    )

    def close_controller():
        controller.close()
        close_returned.set()

    close_thread = threading.Thread(target=close_controller)
    start_thread.start()
    assert start_entered.wait(5)
    close_thread.start()
    returned_while_start_running = close_returned.wait(0.2)
    sessions_cleared = controller._candidate_sessions == {}
    token_cleared = "TOKEN-REMAINING" not in repr(controller._candidate_sessions)
    release_start.set()
    start_thread.join(5)
    close_thread.join(5)

    assert returned_while_start_running
    assert sessions_cleared and token_cleared
    assert start_results and isinstance(start_results[0], RunDetail)
    assert remaining.session_id != committed.session_id
    assert [call[0] for call in orchestrator.calls].count("start_defect") == 1


def test_query_from_controller_event_loop_is_rejected_without_deadlock():
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, Index())

    class ReentrantCandidates:
        async def list_candidates(self, *args, **kwargs):
            with pytest.raises(
                TuiControllerError, match="^candidate query is unavailable$"
            ):
                controller.query_defects("P", "I", "A", ())
            with pytest.raises(
                TuiControllerError, match="^candidate query is unavailable$"
            ):
                controller.close()
            return (candidate(),)

    orchestrator.defect_candidates = ReentrantCandidates()
    try:
        with pytest.raises(
            TuiControllerError, match="^candidate query is unavailable$"
        ):
            controller.query_defects("P", "I", "A", ())
    finally:
        controller.close()


def test_close_waits_for_inflight_query_then_closes_loop_and_thread():
    started = threading.Event()

    class SlowCandidates:
        async def list_candidates(self, *args, **kwargs):
            started.set()
            await asyncio.sleep(0.05)
            return (candidate(),)

    orchestrator = Orchestrator()
    orchestrator.defect_candidates = SlowCandidates()
    controller = TuiController(orchestrator, Index())
    errors = []
    worker = threading.Thread(
        target=lambda: _capture_query_error(controller, errors)
    )
    worker.start()
    assert started.wait(5)
    runtime = controller._async_runtime
    controller.close()
    worker.join(5)
    assert errors and str(errors[0]) == "candidate query is unavailable"
    assert not runtime._thread.is_alive()
    assert runtime._loop.is_closed()


def test_close_racing_loop_start_does_not_miss_stop(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original_new_event_loop = asyncio.new_event_loop

    def delayed_new_event_loop():
        entered.set()
        assert release.wait(5)
        return original_new_event_loop()

    monkeypatch.setattr(asyncio, "new_event_loop", delayed_new_event_loop)
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    query_errors = []
    close_errors = []
    query_thread = threading.Thread(
        target=lambda: _capture_query_error(controller, query_errors)
    )
    query_thread.start()
    assert entered.wait(5)
    close_thread = threading.Thread(
        target=lambda: _capture_close_error(controller, close_errors)
    )
    close_thread.start()
    release.set()
    close_thread.join(0.5)
    missed_stop = close_thread.is_alive()
    if missed_stop:
        assert controller._async_runtime._ready.wait(5)
        controller._async_runtime._loop.call_soon_threadsafe(
            controller._async_runtime._loop.stop
        )
    close_thread.join(5)
    query_thread.join(5)
    assert not missed_stop
    assert close_errors == []
    assert query_errors and isinstance(query_errors[0], TuiControllerError)


def test_runtime_construction_and_unexpected_stop_fail_with_fixed_error(monkeypatch):
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    monkeypatch.setattr(
        asyncio,
        "new_event_loop",
        lambda: (_ for _ in ()).throw(RuntimeError("TOKEN-INNER")),
    )
    with pytest.raises(TuiControllerError, match="^candidate query is unavailable$") as failed:
        controller.query_defects("P", "I", "A", ())
    assert "TOKEN-INNER" not in str(failed.value)
    failed_thread = controller._async_runtime._thread
    monkeypatch.undo()
    with pytest.raises(TuiControllerError, match="^candidate query is unavailable$"):
        controller.query_defects("P", "I", "A", ())
    assert controller._async_runtime._thread is failed_thread
    assert not failed_thread.is_alive()
    controller.close()

    orchestrator = Orchestrator([(candidate(),), (candidate(),)])
    controller = TuiController(orchestrator, Index())
    controller.query_defects("P", "I", "A", ())
    runtime = controller._async_runtime
    runtime._loop.call_soon_threadsafe(runtime._loop.stop)
    assert runtime._stopped.wait(5)
    with pytest.raises(TuiControllerError, match="^candidate query is unavailable$"):
        controller.query_defects("P", "I", "A", ())
    controller.close()


def test_loop_stop_before_task_creation_releases_waiter_without_coroutine_leak():
    orchestrator = Orchestrator([(candidate(),), (candidate(),)])
    controller = TuiController(orchestrator, Index())
    controller.query_defects("P", "I", "A", ())
    runtime = controller._async_runtime
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def stop_before_next_ready_batch():
        callback_entered.set()
        assert release_callback.wait(5)
        runtime._loop.stop()

    runtime._loop.call_soon_threadsafe(stop_before_next_ready_batch)
    assert callback_entered.wait(5)
    errors = []
    worker = threading.Thread(
        target=lambda: _capture_query_error(controller, errors)
    )
    worker.start()
    release_callback.set()
    assert runtime._stopped.wait(5)
    worker.join(0.5)
    stranded = worker.is_alive()
    if stranded:
        controller.close()
        worker.join(5)
    assert not stranded
    assert errors and type(errors[0]) is TuiControllerError
    assert str(errors[0]) == "candidate query is unavailable"
    assert runtime._futures == set()
    controller.close()


def test_thread_start_failure_is_fixed_and_close_remains_idempotent(monkeypatch):
    class FailedThread:
        def start(self):
            raise RuntimeError("TOKEN-INNER")

    monkeypatch.setattr(controller_module, "Thread", lambda **kwargs: FailedThread())
    controller = TuiController(Orchestrator([(candidate(),)]), Index())
    with pytest.raises(TuiControllerError, match="^candidate query is unavailable$") as failed:
        controller.query_defects("P", "I", "A", ())
    assert "TOKEN-INNER" not in str(failed.value)
    monkeypatch.undo()
    with pytest.raises(TuiControllerError, match="^candidate query is unavailable$"):
        controller.query_defects("P", "I", "A", ())
    assert controller._async_runtime._thread is None
    controller.close()
    controller.close()


def test_query_rejects_mixed_tokens_and_does_not_create_empty_or_failed_sessions():
    orchestrator = Orchestrator([
        (),
        (candidate("D-1", "one"), candidate("D-2", "two")),
        RuntimeError("TOKEN-INNER"),
    ])
    controller = TuiController(orchestrator, Index())
    empty = controller.query_defects("P", "I", "A", ())
    assert empty.items == ()
    with pytest.raises(TuiControllerError, match="candidate snapshot is invalid"):
        controller.start_defect(empty.session_id, "D-1")
    with pytest.raises(TuiControllerError, match="candidate snapshot is invalid"):
        controller.query_defects("P", "I", "A", ())
    with pytest.raises(TuiControllerError) as caught:
        controller.query_defects("P", "I", "A", ())
    assert "TOKEN-INNER" not in str(caught.value)


def test_start_defect_consumes_hidden_capability_even_when_orchestrator_fails():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    view = controller.query_defects("P", "I", "A", ())
    orchestrator.start_defect = lambda *args: (_ for _ in ()).throw(RuntimeError("TOKEN-INNER"))

    with pytest.raises(TuiControllerError) as caught:
        controller.start_defect(view.session_id, "D-1")


def test_start_defect_sanitizes_lower_layer_controller_error():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    view = controller.query_defects("P", "I", "A", ())
    orchestrator.start_defect = lambda *args: (_ for _ in ()).throw(
        TuiControllerError("TOKEN-INNER")
    )

    with pytest.raises(TuiControllerError) as caught:
        controller.start_defect(view.session_id, "D-1")
    assert str(caught.value) == "defect workflow could not be started"
    assert "TOKEN-INNER" not in str(caught.value)
    assert "TOKEN-INNER" not in str(caught.value)
    with pytest.raises(TuiControllerError, match="candidate snapshot is invalid"):
        controller.start_defect(view.session_id, "D-1")


def test_start_defect_classifies_expired_candidate_separately():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    view = controller.query_defects("P", "I", "A", ())
    orchestrator.start_defect = lambda *args: (_ for _ in ()).throw(
        InvalidWorkflowAction("TOKEN-STALE")
    )

    with pytest.raises(StaleCandidateError, match="^candidate snapshot is invalid$") as caught:
        controller.start_defect(view.session_id, "D-1")
    assert "TOKEN-STALE" not in str(caught.value)


def test_start_defect_rejects_malformed_identifiers_with_fixed_error():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    with pytest.raises(TuiControllerError, match="^candidate snapshot is invalid$"):
        controller.start_defect([], "D-1")
    view = controller.query_defects("P", "I", "A", ())
    with pytest.raises(TuiControllerError, match="^candidate snapshot is invalid$"):
        controller.start_defect(view.session_id, [])


def test_sessions_are_bounded_and_one_shot_under_concurrency():
    orchestrator = Orchestrator([(candidate(),), (candidate(),), (candidate(),)])
    controller = TuiController(orchestrator, Index(), max_candidate_sessions=2)
    first = controller.query_defects("P", "I", "A", ())
    second = controller.query_defects("P", "I", "A", ())
    third = controller.query_defects("P", "I", "A", ())
    with pytest.raises(TuiControllerError):
        controller.start_defect(first.session_id, "D-1")

    barrier = threading.Barrier(3)
    outcomes = []
    def invoke():
        barrier.wait()
        try:
            controller.start_defect(second.session_id, "D-1")
            outcomes.append("ok")
        except TuiControllerError:
            outcomes.append("error")
    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join()
    assert sorted(outcomes) == ["error", "ok"]
    assert third.session_id != second.session_id


def test_sync_adapters_return_only_views_and_forward_expected_versions():
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, Index())
    filters = RunFilter()
    assert controller.list_runs(filters, {"r": "busy"}) == (filters, {"r": "busy"})
    assert isinstance(controller.show("r"), RunDetail)
    assert isinstance(controller.start_requirement("REQ-1"), RunDetail)
    assert isinstance(controller.confirm_repository("r", "repo", 3), RunDetail)
    assert isinstance(controller.resume("r", 4), RunDetail)
    assert ("confirm", "r", "repo", 3) in orchestrator.calls
    assert ("resume", "r", 4) in orchestrator.calls
    assert not any(
        call[0] == "show" and call[2] is False
        for call in orchestrator.calls
    )


def test_cancel_asserts_authoritative_facts_then_forwards_bound_version():
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, Index())
    request = controller.prepare_action(orchestrator.run.run_id, "cancel")
    result = controller.cancel(request, "operator")
    assert isinstance(result, RunDetail)
    assert not any(
        call[0] == "show" and call[2] is False
        for call in orchestrator.calls
    )
    assert ("cancel", orchestrator.run.run_id, "operator", request.version) in orchestrator.calls

    drifted = orchestrator.run.validated_update(version=orchestrator.run.version + 1)
    orchestrator.run = drifted
    with pytest.raises(StaleTuiActionError, match="^workflow changed; review again$"):
        controller.cancel(request, "SECRET-ACTOR")


def test_request_authority_and_display_failures_are_unavailable_not_stale(monkeypatch):
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, Index())
    request = controller.prepare_action(orchestrator.run.run_id, "cancel")
    orchestrator.show = lambda run_id, *, read_only=False: (_ for _ in ()).throw(
        RuntimeError("TOKEN-INNER")
    )
    with pytest.raises(TuiControllerError, match="^workflow action is unavailable$") as caught:
        controller.cancel(request, "actor")
    assert type(caught.value) is TuiControllerError
    assert "TOKEN-INNER" not in str(caught.value)

    orchestrator.show = lambda run_id, *, read_only=False: orchestrator.run
    monkeypatch.setattr(
        DangerousActionRequest,
        "assert_current",
        lambda self, run: (_ for _ in ()).throw(
            TuiDisplayError("workflow display TOKEN-INNER")
        ),
    )
    with pytest.raises(TuiControllerError, match="^workflow action is unavailable$") as display:
        controller.cancel(request, "actor")
    assert type(display.value) is TuiControllerError
    assert "TOKEN-INNER" not in str(display.value)


def test_constructor_requires_strict_positive_capacity():
    for value in (0, -1, True, 1.5):
        with pytest.raises(TuiControllerError):
            TuiController(Orchestrator(), Index(), max_candidate_sessions=value)


def test_controller_deletes_local_task_through_authoritative_index(tmp_path):
    store = FileRunStore(tmp_path / "runs")
    run = store.create(WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1"))
    controller = TuiController(Orchestrator(), RunIndex(store))
    try:
        controller.delete_task(run.run_id)
        assert run.run_id not in store.list_run_ids()
        with pytest.raises(TuiControllerError, match="task could not be deleted safely"):
            controller.delete_task(run.run_id)
    finally:
        controller.close()


def test_controller_precheck_cannot_race_orchestrator_version_gate(tmp_path):
    store = FileRunStore(tmp_path / "runs")
    run = store.create(WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1"))
    side_effects = []
    flow = SimpleNamespace(execute=lambda current: side_effects.append(("flow", current)))
    publisher = SimpleNamespace(
        publish=lambda current: side_effects.append(("publish", current)),
        retry_comment=lambda current: side_effects.append(("retry", current)),
    )
    orchestrator = DeveloperWorkflowOrchestrator(
        store=store,
        requirement_flow=flow,
        defect_flow=flow,
        publisher=publisher,
        config=None,
        defect_candidates=None,
    )
    controller = TuiController(orchestrator, RunIndex(store))
    request = controller.prepare_action(run.run_id, "cancel")
    entered = threading.Event()
    release = threading.Event()
    original_lock = store.operation_lock

    @contextmanager
    def delayed_lock(run_id, purpose):
        entered.set()
        assert release.wait(5)
        with original_lock(run_id, purpose):
            yield

    store.operation_lock = delayed_lock
    errors = []
    worker = threading.Thread(
        target=lambda: _capture_stale(controller, request, errors)
    )
    worker.start()
    assert entered.wait(5)
    store.transition(
        run.run_id,
        run.version,
        target=WorkflowState.READING_ONES,
        reason="competing authoritative update",
    )
    release.set()
    worker.join(5)

    assert errors and isinstance(errors[0], StaleTuiActionError)
    assert str(errors[0]) == "workflow changed; review again"
    assert store.load(run.run_id).state.value == "READING_ONES"
    assert side_effects == []


def test_read_adapters_sanitize_lower_layer_errors():
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, SimpleNamespace(
        list=lambda *args: (_ for _ in ()).throw(OSError("TOKEN-INNER"))
    ))
    with pytest.raises(TuiControllerError) as listed:
        controller.list_runs(RunFilter())
    assert "TOKEN-INNER" not in str(listed.value)

    orchestrator.show = lambda run_id, *, read_only=False: (
        _ for _ in ()
    ).throw(OSError("TOKEN-INNER"))
    with pytest.raises(TuiControllerError) as shown:
        controller.show("run")
    assert "TOKEN-INNER" not in str(shown.value)


def test_generic_resume_cannot_bypass_publication_confirmation():
    orchestrator = Orchestrator()
    orchestrator.run = orchestrator.run.validated_update(
        state=WorkflowState.PUBLISHING, version=4
    )
    controller = TuiController(orchestrator, Index())
    with pytest.raises(TuiControllerError, match="workflow action is unavailable"):
        controller.resume(orchestrator.run.run_id, 4)
    assert not any(call[0] == "resume" for call in orchestrator.calls)


def test_atomic_stale_from_regular_versioned_commands_has_stale_type():
    orchestrator = Orchestrator()
    orchestrator.confirm_repository = lambda *args, **kwargs: (
        (_ for _ in ()).throw(InvalidWorkflowAction("workflow changed; review again"))
    )
    controller = TuiController(orchestrator, Index())
    with pytest.raises(StaleTuiActionError, match="^workflow changed; review again$"):
        controller.confirm_repository("run", "repo", 3)


def _capture_stale(controller, request, errors):
    try:
        controller.cancel(request, "operator")
    except Exception as exc:
        errors.append(exc)


def _capture_query_error(controller, errors):
    try:
        controller.query_defects("P", "I", "A", ())
    except Exception as exc:
        errors.append(exc)


def _capture_close_error(controller, errors):
    try:
        controller.close()
    except Exception as exc:
        errors.append(exc)
