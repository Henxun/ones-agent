from __future__ import annotations

import sqlite3
from types import SimpleNamespace as NS
import pytest

from src.developer_workflow.schedules import (
    Schedule,
    ScheduleError,
    ScheduleRunItemStatus,
    ScheduleRunStatus,
    ScheduleRunTrigger,
    ScheduleStore,
    dispatch,
)
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


def test_run_ledger_opens_an_existing_store_without_rewriting_legacy_rows(tmp_path):
    root = tmp_path / "private"
    path = ScheduleStore(root).path
    existing = plan(enabled=True).model_copy(update={"version": 7, "next_due": 123})
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE schedule_run_items")
        db.execute("DROP TABLE schedule_runs")
        db.execute("DROP TABLE claims")
        db.execute("DROP TABLE schedules")
        db.execute("CREATE TABLE schedules (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE claims (scope TEXT, defect TEXT, action TEXT, "
            "run_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(scope, defect, action))"
        )
        db.execute(
            "INSERT INTO schedules VALUES (?,?)",
            (existing.id, existing.model_dump_json()),
        )
        db.execute(
            "INSERT INTO claims(scope,defect,action,run_id) VALUES (?,?,?,?)",
            (existing.workspace, "legacy-defect", existing.action.value, ""),
        )

    store = ScheduleStore(root)

    assert store.list() == (existing,)
    assert store.list_runs() == ()
    with sqlite3.connect(path) as db:
        claim = db.execute(
            "SELECT run_id,schedule_run_id,claimed_at,state FROM claims"
        ).fetchone()
    assert claim == ("", "", 0.0, "legacy_reserved")


def test_timer_and_manual_run_claims_are_atomic_and_manual_preserves_schedule(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    saved = store.save(plan(enabled=True), expected_version=None, now=0)
    other = ScheduleStore(store.root)

    timer_claim = store.create_timer_run(3600, lease_seconds=60)

    assert timer_claim is not None
    timer_plan, timer_run = timer_claim
    assert timer_plan.next_due == 7200
    assert timer_run.trigger is ScheduleRunTrigger.TIMER
    assert timer_run.plan_snapshot == timer_plan
    assert other.create_timer_run(3600, lease_seconds=60) is None
    with pytest.raises(ScheduleError, match="已有运行中的批次"):
        other.create_manual_run(saved.id, expected_version=saved.version, now=3601)

    store.finish_run(
        timer_run.id,
        timer_run.lease_token,
        status=ScheduleRunStatus.SUCCESS,
        discovered_count=0,
        skipped_count=0,
        started_count=0,
        failed_count=0,
        message="没有候选缺陷",
        now=3602,
    )
    current = store.list()[0]
    paused = store.save(
        current.model_copy(update={"enabled": False}),
        expected_version=current.version,
        now=4000,
    )
    before = (paused.enabled, paused.next_due)

    manual_plan, manual_run = other.create_manual_run(
        paused.id,
        expected_version=paused.version,
        now=4001,
        lease_seconds=30,
        trigger=ScheduleRunTrigger.API,
    )

    assert manual_plan == paused
    assert manual_run.trigger is ScheduleRunTrigger.API
    after = other.list()[0]
    assert (after.enabled, after.next_due) == before
    with pytest.raises(ScheduleError, match="已变化"):
        other.create_manual_run(paused.id, expected_version=paused.version + 1, now=4002)


def test_run_history_and_items_survive_schedule_edit_and_delete(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    saved = store.save(plan(enabled=False), expected_version=None, now=0)
    _, run = store.create_manual_run(saved.id, expected_version=saved.version, now=10)
    item = store.record_run_item(
        run.id,
        run.lease_token,
        defect_id="defect-1",
        defect_name="无法连接设备",
        status=ScheduleRunItemStatus.STARTED,
        workflow_run_id="workflow-1",
        now=11,
    )
    edited = store.save(
        saved.model_copy(update={"name": "新的名称"}),
        expected_version=saved.version,
        now=12,
    )

    finished = store.finish_run(
        run.id,
        run.lease_token,
        status=ScheduleRunStatus.SUCCESS,
        discovered_count=1,
        skipped_count=0,
        started_count=1,
        failed_count=0,
        message="启动 1 项",
        now=13,
    )

    assert finished.status is ScheduleRunStatus.SUCCESS
    assert store.list()[0].last_result == edited.last_result
    assert store.list_run_items(run.id) == (item,)
    store.delete(edited.id, expected_version=edited.version)
    assert store.list() == ()
    assert store.get_run(run.id) == finished
    assert store.list_runs(saved.id) == (finished,)
    assert store.list_run_items(run.id) == (item,)
    assert finished.plan_snapshot.name == saved.name


def test_bounded_history_queries_are_workspace_scoped_and_paginated(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    first = store.save(plan(enabled=False), expected_version=None, now=0)
    other = store.save(
        plan(enabled=False).model_copy(update={"workspace": "other"}),
        expected_version=None,
        now=0,
    )
    first_runs = []
    for started_at in (10, 20, 30):
        _, run = store.create_manual_run(
            first.id, expected_version=first.version, now=started_at
        )
        store.record_run_item(
            run.id,
            run.lease_token,
            defect_id=f"defect-{started_at}",
            status=ScheduleRunItemStatus.STARTED,
            now=started_at + 1,
        )
        first_runs.append(
            store.finish_run(
                run.id,
                run.lease_token,
                status=ScheduleRunStatus.SUCCESS,
                discovered_count=1,
                skipped_count=0,
                started_count=1,
                failed_count=0,
                message="done",
                now=started_at + 2,
            )
        )
    _, other_run = store.create_manual_run(
        other.id, expected_version=other.version, now=40
    )

    assert store.query_runs("workspace", limit=1, offset=1) == (first_runs[1],)
    assert store.query_runs("workspace", schedule_id=first.id, limit=2) == (
        first_runs[2],
        first_runs[1],
    )
    with pytest.raises(ScheduleError, match="定时任务运行历史不可用"):
        store.query_runs("workspace", schedule_id=other.id)
    assert store.query_run_items("workspace", first_runs[0].id, limit=1)[0].defect_id == "defect-10"
    with pytest.raises(ScheduleError, match="运行批次不可用"):
        store.query_run_items("workspace", other_run.id)
    with pytest.raises(ScheduleError, match="运行历史查询范围无效"):
        store.query_runs("workspace", limit=101)
    with pytest.raises(ScheduleError, match="运行历史查询范围无效"):
        store.query_run_items("other", other_run.id, limit=201)


def test_run_reservation_and_reserved_item_are_atomic_across_processes(
    tmp_path, monkeypatch
):
    store = ScheduleStore(tmp_path / "private")
    saved = store.save(plan(enabled=True), expected_version=None, now=0)
    _, run = store.create_manual_run(saved.id, expected_version=saved.version, now=10)
    other = ScheduleStore(store.root)
    original = ScheduleStore._insert_reserved_run_item

    def interrupt_after_claim(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        ScheduleStore,
        "_insert_reserved_run_item",
        staticmethod(interrupt_after_claim),
    )
    with pytest.raises(KeyboardInterrupt):
        store.reserve_run_item(
            saved, "crash-window", run.id, run.lease_token, now=11
        )

    assert other.list_run_items(run.id) == ()
    assert other.reserve_defect(saved, "crash-window")
    monkeypatch.setattr(
        ScheduleStore, "_insert_reserved_run_item", staticmethod(original)
    )

    reserved = store.reserve_run_item(
        saved, "durable", run.id, run.lease_token, now=12
    )

    assert reserved is not None
    assert reserved.status is ScheduleRunItemStatus.RESERVED
    assert other.list_run_items(run.id) == (reserved,)
    assert not other.reserve_defect(saved, "durable")


def test_finish_run_updates_matching_plan_and_validates_counts(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    saved = store.save(plan(enabled=True), expected_version=None, now=0)
    _, run = store.create_manual_run(saved.id, expected_version=saved.version, now=10)

    with pytest.raises(ScheduleError, match="统计不一致"):
        store.finish_run(
            run.id,
            run.lease_token,
            status=ScheduleRunStatus.SUCCESS,
            discovered_count=0,
            skipped_count=0,
            started_count=1,
            failed_count=0,
            message="invalid",
            now=11,
        )

    finished = store.finish_run(
        run.id,
        run.lease_token,
        status=ScheduleRunStatus.FAILED,
        discovered_count=1,
        skipped_count=0,
        started_count=0,
        failed_count=1,
        message="仓库准备失败",
        error_stage="repository",
        now=12,
    )
    current = store.list()[0]
    assert finished.error_stage == "repository"
    assert finished.error_message == "仓库准备失败"
    assert current.last_result == "仓库准备失败"
    assert current.failures == 1


def test_lease_recovery_only_interrupts_expired_runs_and_preserves_claims(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    saved = store.save(plan(enabled=True), expected_version=None, now=0)
    assert store.reserve_defect(saved, "reserved-before-crash")
    _, expired = store.create_manual_run(
        saved.id, expected_version=saved.version, now=10, lease_seconds=5
    )
    store.record_run_item(
        expired.id,
        expired.lease_token,
        defect_id="reserved-before-crash",
        status=ScheduleRunItemStatus.RESERVED,
        now=11,
    )

    assert store.recover_expired_runs(now=14) == ()
    renewed = store.heartbeat_run(
        expired.id, expired.lease_token, now=14, lease_seconds=5
    )
    assert renewed.lease_until == 19
    assert store.recover_expired_runs(now=18) == ()

    with pytest.raises(ScheduleError, match="租约已失效"):
        store.heartbeat_run(
            expired.id, expired.lease_token, now=19, lease_seconds=5
        )
    with pytest.raises(ScheduleError, match="租约已失效"):
        store.record_run_item(
            expired.id,
            expired.lease_token,
            defect_id="write-at-expiry",
            status=ScheduleRunItemStatus.STARTED,
            now=19,
        )
    with pytest.raises(ScheduleError, match="租约已失效"):
        store.reserve_run_item(
            saved,
            "reserve-at-expiry",
            expired.id,
            expired.lease_token,
            now=19,
        )
    assert store.reserve_defect(saved, "reserve-at-expiry")
    with pytest.raises(ScheduleError, match="租约已失效"):
        store.finish_run(
            expired.id,
            expired.lease_token,
            status=ScheduleRunStatus.SUCCESS,
            discovered_count=0,
            skipped_count=0,
            started_count=0,
            failed_count=0,
            message="late finish",
            now=19,
        )
    assert store.get_run(expired.id).status is ScheduleRunStatus.RUNNING

    recovered = store.recover_expired_runs(now=20)

    assert len(recovered) == 1
    assert recovered[0].status is ScheduleRunStatus.INTERRUPTED
    assert recovered[0].error_stage == "lease"
    assert not store.reserve_defect(saved, "reserved-before-crash")
    with pytest.raises(ScheduleError, match="租约已失效"):
        store.heartbeat_run(expired.id, expired.lease_token, now=21)
    with pytest.raises(ScheduleError, match="租约已失效"):
        store.record_run_item(
            expired.id,
            expired.lease_token,
            defect_id="late-write",
            status=ScheduleRunItemStatus.STARTED,
            now=21,
        )


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
    first_run = store.list_runs()[0]
    assert first_run.status is ScheduleRunStatus.SUCCESS
    assert first_run.started_count == 1
    assert store.list_run_items(first_run.id)[0].workflow_run_id == "run"
    # Make the next interval due without losing the durable reservation.
    p = store.list()[0]
    store.save(p, expected_version=p.version, now=0)
    dispatch(controller)
    assert len(calls) == 2
    second_run = store.list_runs()[0]
    assert second_run.status is ScheduleRunStatus.SUCCESS
    assert second_run.skipped_count == 1
    assert store.list_run_items(second_run.id)[0].status is ScheduleRunItemStatus.SKIPPED


def test_dispatch_missing_mapping_fails_closed(tmp_path):
    store = ScheduleStore(tmp_path / "private")
    store.save(plan(enabled=True), expected_version=None, now=0)
    dispatch(NS(schedule_store=store, _closed=False, list_workspaces=lambda: []))
    assert store.list()[0].failures == 1
    assert store.list_runs()[0].status is ScheduleRunStatus.FAILED


def test_abrupt_dispatch_exit_leaves_recoverable_run_and_permanent_claim(tmp_path, monkeypatch):
    from src.developer_workflow import schedules

    store = ScheduleStore(tmp_path / "private")
    saved = store.save(plan(enabled=True), expected_version=None, now=0)
    monkeypatch.setattr(schedules.time, "time", lambda: 10_000)

    def interrupted_start(session, candidate):
        raise KeyboardInterrupt

    controller = NS(
        schedule_store=store,
        _closed=False,
        list_workspaces=lambda: [
            NS(key="workspace", project_id="project", iteration_id="iteration")
        ],
        list_runs=lambda filters: [],
        query_defects=lambda *args: NS(
            session_id="fresh", items=[NS(candidate_id="defect")]
        ),
        analyze_defect=interrupted_start,
        start_defect=None,
    )

    with pytest.raises(KeyboardInterrupt):
        dispatch(controller)

    run = store.list_runs()[0]
    assert run.status is ScheduleRunStatus.RUNNING
    assert store.list_run_items(run.id)[0].status is ScheduleRunItemStatus.RESERVED
    assert not store.reserve_defect(saved, "defect")
    recovered = store.recover_expired_runs(now=run.lease_until + 1)
    assert recovered[0].status is ScheduleRunStatus.INTERRUPTED
    assert not store.reserve_defect(saved, "defect")


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
