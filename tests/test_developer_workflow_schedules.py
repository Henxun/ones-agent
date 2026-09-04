from __future__ import annotations

from types import SimpleNamespace as NS
import pytest

from src.developer_workflow.schedules import Schedule, ScheduleStore, ScheduleError, dispatch
from src.developer_workflow.contracts import DefectAction


def plan(**values):
    return Schedule(workspace="workspace", project="project", iteration="iteration", name="扫描",
                    assignee="member", status_ids=("open",), **values)


def test_store_edit_pause_coalesce_and_dedupe(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    p = store.save(plan(enabled=True), expected_version=None, now=100)
    assert store.claim_due(101) is None
    claimed = store.claim_due(10000)
    assert claimed.next_due == 13600
    assert store.claim_due(10000) is None
    assert store.reserve_defect(p, "defect")
    assert not ScheduleStore(store.root).reserve_defect(p, "defect")
    paused = store.save(p.model_copy(update={"enabled": False}), expected_version=p.version, now=200)
    assert not store.current(p)
    store.finish(p, "stale")
    assert store.list()[0].last_result != "stale"
    with pytest.raises(ScheduleError):
        store.save(p, expected_version=p.version)
    assert store.claim_due(99999) is None
    resumed = store.save(paused.model_copy(update={"enabled": True}), expected_version=paused.version, now=300)
    for i in range(3):
        store.finish(resumed, "失败", failed=True)
    assert not store.list()[0].enabled


@pytest.mark.parametrize("action", list(DefectAction))
def test_dispatch_reuses_workflow_and_never_approves(tmp_path, action):
    store = ScheduleStore(tmp_path / "private")
    store.save(plan(enabled=True, action=action), expected_version=None, now=0)
    calls = []
    def start(session, candidate):
        calls.append((action.value, candidate))
        return NS(summary=NS(run_id="run", version=1), mapping_candidates=[NS(key="workspace")])
    controller = NS(schedule_store=store, _closed=False,
        list_workspaces=lambda: [NS(key="workspace", project_id="project", iteration_id="iteration")],
        list_runs=lambda filters: [],
        query_defects=lambda *args: NS(session_id="fresh", items=[NS(candidate_id="defect")]),
        analyze_defect=start if action == DefectAction.ANALYZE else None,
        start_defect=start if action == DefectAction.ANALYZE_AND_REPAIR else None,
        confirm_repository=lambda *args: calls.append(("confirm", args)))
    dispatch(controller)
    assert calls == [(action.value, "defect"), ("confirm", ("run", "workspace", 1))]
    # Make the next interval due without losing the durable reservation.
    p = store.list()[0]
    store.save(p, expected_version=p.version, now=0)
    dispatch(controller)
    assert len(calls) == 2


def test_dispatch_missing_mapping_fails_closed(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    store.save(plan(enabled=True), expected_version=None, now=0)
    dispatch(NS(schedule_store=store, _closed=False, list_workspaces=lambda: []))
    assert store.list()[0].failures == 1


def test_delete_retains_claims_and_rejects_stale_edits(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    p = store.save(plan(enabled=True), expected_version=None, now=0)
    store.reserve_defect(p, "defect")
    store.record_run(p, "defect", "existing-run")
    with pytest.raises(ScheduleError):
        store.delete(p.id, expected_version=p.version + 1)
    assert store.list() == (p,)
    store.delete(p.id, expected_version=p.version)
    assert ScheduleStore(store.root).list() == ()
    assert store.claim_due(99999) is None
    assert not store.current(p)
    assert not store.reserve_defect(p, "new-defect-after-deletion")
    store.finish(p, "late completion")
    assert store.list() == ()
    with pytest.raises(ScheduleError):
        store.save(p, expected_version=p.version)
    with pytest.raises(ScheduleError):
        store.delete(p.id, expected_version=p.version)
    replacement = store.save(plan(enabled=True), expected_version=None)
    assert not store.reserve_defect(replacement, "defect")


def test_delete_during_scan_stops_new_tasks(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    p = store.save(plan(enabled=True), expected_version=None, now=0)
    def query(*args):
        store.delete(p.id, expected_version=p.version)
        return NS(items=[NS(candidate_id="defect")])
    dispatch(NS(schedule_store=store, _closed=False,
        list_workspaces=lambda: [NS(key="workspace", project_id="project", iteration_id="iteration")],
        list_runs=lambda filters: [], query_defects=query))
    assert store.list() == ()


def test_pause_while_querying_prevents_start(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    p = store.save(plan(enabled=True), expected_version=None, now=0)
    def query(*args):
        store.save(p.model_copy(update={"enabled": False}), expected_version=p.version)
        return NS(items=[NS(candidate_id="defect")])
    controller = NS(schedule_store=store, _closed=False,
        list_workspaces=lambda: [NS(key="workspace", project_id="project", iteration_id="iteration")],
        list_runs=lambda filters: [], query_defects=query)
    dispatch(controller)
    assert not store.list()[0].enabled
    assert store.list()[0].failures == 0


def test_execution_lock_is_reentrant_and_excludes_other_stores(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    other = ScheduleStore(store.root)
    with store.execution_gate():
        with store.execution_gate():
            with pytest.raises(ScheduleError):
                with other.execution_gate():
                    pytest.fail("overlapping mutation")


@pytest.mark.asyncio
async def test_timer_does_not_queue_overlapping_dispatch(monkeypatch):
    import asyncio
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.supervisor import RunTaskSupervisor
    from src.developer_workflow import schedules
    calls = []
    monkeypatch.setattr(schedules, "dispatch", lambda controller: calls.append(controller))
    supervisor = RunTaskSupervisor(1, lambda event: None)
    controller = NS(schedule_store=object())
    app = NS(_ui_closed=False, _reconfiguring=False, _schedule_task=None,
             runtime_session=NS(controller=controller, supervisor=supervisor))
    DeveloperWorkflowTuiApp._dispatch_schedules(app)
    first = app._schedule_task
    DeveloperWorkflowTuiApp._dispatch_schedules(app)
    assert app._schedule_task is first
    await first
    assert calls == [controller]
    app._ui_closed = True
    DeveloperWorkflowTuiApp._dispatch_schedules(app)
    assert app._schedule_task is first
    await supervisor.close()


@pytest.mark.parametrize("values", [{"interval_minutes": 0}, {"max_candidates": 4}, {"name": "\n"}])
def test_invalid_limits(values):
    base = plan().model_dump()
    base.update(values)
    with pytest.raises(ValueError):
        Schedule.model_validate(base)
