"""Persistent schedules: dispatch policy only, never a second repair engine."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from threading import RLock, local
import time
from typing import Iterator
import uuid
from functools import wraps

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from .contracts import DefectAction
from .private_paths import prepare_private_directory
from .setup_store import (_open_lock_nofollow, _protect_private_file,
                          _validate_regular_file, _try_lock, _unlock)


class ScheduleError(RuntimeError):
    pass


def serialized_mutation(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        store = getattr(self, "schedule_store", None)
        if store is None:
            return method(self, *args, **kwargs)
        with store.execution_gate():
            return method(self, *args, **kwargs)
    return call


def dispatch(controller) -> None:
    """Reuse the interactive workflow; never approve or publish a run."""
    store = controller.schedule_store
    if store is None or controller._closed:
        return
    with store.execution_gate():
        if controller._closed:
            return
        plan = store.claim_due(time.time())
        if plan is None:
            return
        started = 0
        try:
            workspaces = controller.list_workspaces()
            if not any((w.key, w.project_id, w.iteration_id) ==
                       (plan.workspace, plan.project, plan.iteration) for w in workspaces):
                raise ScheduleError("工作区映射已变化")
            from .tui.models import RunFilter
            existing = {r.work_item_id for r in controller.list_runs(RunFilter())}
            batch = controller.query_defects(plan.project, plan.iteration, plan.assignee, plan.status_ids)
            candidates = [c.candidate_id for c in batch.items if c.candidate_id not in existing]
            for candidate in candidates:
                if started >= plan.max_candidates:
                    break
                if controller._closed or not store.current(plan):
                    break
                fresh = controller.query_defects(plan.project, plan.iteration, plan.assignee, plan.status_ids)
                if candidate not in {c.candidate_id for c in fresh.items}:
                    continue
                if controller._closed or not store.current(plan):
                    break
                if not store.reserve_defect(plan, candidate):
                    continue
                start = controller.analyze_defect if plan.action == DefectAction.ANALYZE else controller.start_defect
                detail = start(fresh.session_id, candidate)
                store.record_run(plan, candidate, detail.summary.run_id)
                if not any(m.key == plan.workspace for m in detail.mapping_candidates):
                    raise ScheduleError("候选仓库映射不匹配，请在任务列表处理")
                controller.confirm_repository(detail.summary.run_id, plan.workspace, detail.summary.version)
                started += 1
            store.finish(plan, f"本轮启动 {started} 项；已有任务或占位的缺陷已跳过")
        except Exception:
            store.finish(plan, f"扫描或执行失败（已启动 {started} 项）；请检查连接、工作区及任务列表。连续三次失败自动暂停", failed=True)


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    version: int = Field(default=0, ge=0)
    workspace: str
    project: str
    iteration: str
    name: str = Field(min_length=1, max_length=80)
    action: DefectAction = DefectAction.ANALYZE
    interval_minutes: int = Field(default=60, ge=5, le=10080)
    assignee: str
    status_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    max_candidates: int = Field(default=3, ge=1, le=3)
    enabled: StrictBool = False
    next_due: float = 0
    last_result: str = "尚未运行"
    failures: int = 0

    @field_validator("workspace", "project", "iteration", "name", "assignee")
    @classmethod
    def clean_text(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid schedule field")
        return value.strip()

    @field_validator("status_ids")
    @classmethod
    def clean_statuses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not v or len(v) > 128 or not all(c.isalnum() or c in "_-" for c in v) for v in values):
            raise ValueError("invalid status selection")
        return tuple(dict.fromkeys(values))


class ScheduleStore:
    def __init__(self, root: Path) -> None:
        self.root = prepare_private_directory(root)
        self.path = self.root / "schedules.sqlite3"
        self._thread_gate = RLock()
        self._depth = local()
        fd, created = _open_lock_nofollow(self.path)
        try:
            if created:
                _protect_private_file(self.path)
            _validate_regular_file(self.path, descriptor=fd)
        finally:
            os.close(fd)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS claims (scope TEXT, defect TEXT, action TEXT, "
                       "run_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(scope, defect, action))")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        prepare_private_directory(self.root)
        _validate_regular_file(self.path)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def list(self, workspace: str | None = None) -> tuple[Schedule, ...]:
        with self._db() as db:
            items = tuple(Schedule.model_validate_json(row[0]) for row in db.execute(
                "SELECT payload FROM schedules ORDER BY id"))
        return tuple(s for s in items if workspace is None or s.workspace == workspace)

    def save(self, value: Schedule, *, expected_version: int | None,
             now: float | None = None) -> Schedule:
        now = time.time() if now is None else now
        with self._db() as db:
            row = db.execute("SELECT payload FROM schedules WHERE id=?", (value.id,)).fetchone()
            old = Schedule.model_validate_json(row[0]) if row else None
            if ((old is None) != (expected_version is None)
                    or old is not None and old.version != expected_version):
                raise ScheduleError("计划已变化，请刷新后重试")
            if old and (old.workspace, old.project, old.iteration) != (value.workspace, value.project, value.iteration):
                raise ScheduleError("不可更换计划所属工作区")
            saved = value.model_copy(update={"version": (old.version + 1) if old else 1,
                                            "next_due": now + value.interval_minutes * 60,
                                            "failures": 0,
                                            "last_result": old.last_result if old else "尚未运行"})
            db.execute("INSERT OR REPLACE INTO schedules VALUES (?,?)", (saved.id, saved.model_dump_json()))
            return saved

    def delete(self, plan_id: str, *, expected_version: int) -> None:
        """Remove only the schedule, retaining workflow records and dedupe claims."""
        with self._db() as db:
            row = db.execute("SELECT payload FROM schedules WHERE id=?", (plan_id,)).fetchone()
            if row is None or Schedule.model_validate_json(row[0]).version != expected_version:
                raise ScheduleError("计划已变化或已删除，请刷新后重试")
            db.execute("DELETE FROM schedules WHERE id=?", (plan_id,))

    def claim_due(self, now: float) -> Schedule | None:
        with self._db() as db:
            plans = [Schedule.model_validate_json(row[0]) for row in db.execute("SELECT payload FROM schedules")]
            due = sorted((s for s in plans if s.enabled and s.next_due <= now), key=lambda s: (s.next_due, s.id))
            if not due:
                return None
            plan = due[0].model_copy(update={"next_due": now + due[0].interval_minutes * 60,
                                             "last_result": "正在扫描/执行；中断后请核对已有任务"})
            db.execute("UPDATE schedules SET payload=? WHERE id=?", (plan.model_dump_json(), plan.id))
            return plan

    def current(self, plan: Schedule) -> bool:
        return any(s.id == plan.id and s.version == plan.version and s.enabled for s in self.list())

    def finish(self, plan: Schedule, message: str, *, failed: bool = False) -> None:
        with self._db() as db:
            row = db.execute("SELECT payload FROM schedules WHERE id=?", (plan.id,)).fetchone()
            if not row:
                return
            current = Schedule.model_validate_json(row[0])
            # An edit/pause wins over a stale worker's completion.
            if current.version != plan.version:
                return
            failures = current.failures + 1 if failed else 0
            value = current.model_copy(update={"last_result": message, "failures": failures,
                                               "enabled": current.enabled and failures < 3})
            db.execute("UPDATE schedules SET payload=? WHERE id=?", (value.model_dump_json(), value.id))

    def reserve_defect(self, plan: Schedule, defect: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT payload FROM schedules WHERE id=?", (plan.id,)).fetchone()
            if row is None:
                return False
            current = Schedule.model_validate_json(row[0])
            if not current.enabled or current.version != plan.version:
                return False
            cursor = db.execute("INSERT OR IGNORE INTO claims(scope,defect,action) VALUES (?,?,?)",
                                (plan.workspace, defect, plan.action.value))
            return cursor.rowcount == 1

    def record_run(self, plan: Schedule, defect: str, run_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE claims SET run_id=? WHERE scope=? AND defect=? AND action=?",
                       (run_id, plan.workspace, defect, plan.action.value))

    @contextmanager
    def execution_gate(self) -> Iterator[None]:
        """Serialize scheduled and manual controller mutations across TUI processes."""
        with self._thread_gate:
            depth = getattr(self._depth, "value", 0)
            if depth:
                self._depth.value += 1
                try:
                    yield
                finally:
                    self._depth.value -= 1
                return
            path = self.root / "execution.lock"
            fd, created = _open_lock_nofollow(path)
            locked = False
            try:
                if created:
                    os.write(fd, b"0")
                    _protect_private_file(path)
                _validate_regular_file(path, descriptor=fd)
                try:
                    _try_lock(fd)
                except OSError:
                    raise ScheduleError("另一个 TUI 正在执行任务，请稍后重试") from None
                locked = True
                self._depth.value = 1
                yield
            finally:
                self._depth.value = 0
                if locked:
                    _unlock(fd)
                os.close(fd)
