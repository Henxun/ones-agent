"""Persistent schedules: dispatch policy only, never a second repair engine."""

from __future__ import annotations

from contextlib import contextmanager
from enum import Enum
import math
import os
from pathlib import Path
import re
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


_RUN_LEASE_SECONDS = 3600
_MAX_RUN_QUERY_LIMIT = 100
_MAX_RUN_ITEM_QUERY_LIMIT = 200
_MAX_QUERY_OFFSET = 10_000


class ScheduleError(RuntimeError):
    pass


class ScheduleRunTrigger(str, Enum):
    TIMER = "timer"
    MANUAL = "manual"
    API = "api"


class ScheduleRunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ScheduleRunItemStatus(str, Enum):
    SKIPPED = "skipped"
    RESERVED = "reserved"
    STARTED = "started"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


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
        now = time.time()
        store.recover_expired_runs(now)
        claimed = store.create_timer_run(now, lease_seconds=_RUN_LEASE_SECONDS)
        if claimed is None:
            return
        plan, schedule_run = claimed
        started = 0
        discovered = 0
        skipped = 0
        failed = 0
        active_candidate = ""
        candidate_reserved = False
        try:
            store.heartbeat_run(
                schedule_run.id,
                schedule_run.lease_token,
                lease_seconds=_RUN_LEASE_SECONDS,
            )
            workspaces = controller.list_workspaces()
            if not any((w.key, w.project_id, w.iteration_id) ==
                       (plan.workspace, plan.project, plan.iteration) for w in workspaces):
                raise ScheduleError("工作区映射已变化")
            from .tui.models import RunFilter
            existing = {r.work_item_id for r in controller.list_runs(RunFilter())}
            batch = controller.query_defects(plan.project, plan.iteration, plan.assignee, plan.status_ids)
            candidates = [c.candidate_id for c in batch.items]
            discovered = len(candidates)
            for candidate in candidates:
                active_candidate = candidate
                candidate_reserved = False
                if candidate in existing:
                    skipped += 1
                    store.record_run_item(
                        schedule_run.id,
                        schedule_run.lease_token,
                        defect_id=candidate,
                        status=ScheduleRunItemStatus.SKIPPED,
                        reason="已有工作流任务",
                    )
                    active_candidate = ""
                    continue
                if started >= plan.max_candidates:
                    skipped += 1
                    store.record_run_item(
                        schedule_run.id,
                        schedule_run.lease_token,
                        defect_id=candidate,
                        status=ScheduleRunItemStatus.SKIPPED,
                        reason="达到本轮处理上限",
                    )
                    active_candidate = ""
                    continue
                if controller._closed or not store.current(plan):
                    skipped += 1
                    store.record_run_item(
                        schedule_run.id,
                        schedule_run.lease_token,
                        defect_id=candidate,
                        status=ScheduleRunItemStatus.SKIPPED,
                        reason="调度已关闭、暂停或修改",
                    )
                    active_candidate = ""
                    continue
                store.heartbeat_run(
                    schedule_run.id,
                    schedule_run.lease_token,
                    lease_seconds=_RUN_LEASE_SECONDS,
                )
                fresh = controller.query_defects(plan.project, plan.iteration, plan.assignee, plan.status_ids)
                if candidate not in {c.candidate_id for c in fresh.items}:
                    skipped += 1
                    store.record_run_item(
                        schedule_run.id,
                        schedule_run.lease_token,
                        defect_id=candidate,
                        status=ScheduleRunItemStatus.SKIPPED,
                        reason="缺陷已不在最新候选快照中",
                    )
                    active_candidate = ""
                    continue
                if controller._closed or not store.current(plan):
                    skipped += 1
                    store.record_run_item(
                        schedule_run.id,
                        schedule_run.lease_token,
                        defect_id=candidate,
                        status=ScheduleRunItemStatus.SKIPPED,
                        reason="调度已关闭、暂停或修改",
                    )
                    active_candidate = ""
                    continue
                reserved_item = store.reserve_run_item(
                    plan,
                    candidate,
                    schedule_run.id,
                    schedule_run.lease_token,
                    now=time.time(),
                )
                if reserved_item is None:
                    skipped += 1
                    store.record_run_item(
                        schedule_run.id,
                        schedule_run.lease_token,
                        defect_id=candidate,
                        status=ScheduleRunItemStatus.SKIPPED,
                        reason="缺陷已被同工作区同动作占位",
                    )
                    active_candidate = ""
                    continue
                candidate_reserved = True
                start = controller.analyze_defect if plan.action == DefectAction.ANALYZE else controller.start_defect
                detail = start(fresh.session_id, candidate)
                store.record_run(
                    plan,
                    candidate,
                    detail.summary.run_id,
                    schedule_run_id=schedule_run.id,
                )
                store.record_run_item(
                    schedule_run.id,
                    schedule_run.lease_token,
                    defect_id=candidate,
                    status=ScheduleRunItemStatus.STARTED,
                    workflow_run_id=detail.summary.run_id,
                    reason="工作流任务已创建",
                )
                if not any(m.key == plan.workspace for m in detail.mapping_candidates):
                    raise ScheduleError("候选仓库映射不匹配，请在任务列表处理")
                controller.confirm_repository(detail.summary.run_id, plan.workspace, detail.summary.version)
                started += 1
                active_candidate = ""
                candidate_reserved = False
            store.finish_run(
                schedule_run.id,
                schedule_run.lease_token,
                status=ScheduleRunStatus.SUCCESS,
                discovered_count=discovered,
                skipped_count=skipped,
                started_count=started,
                failed_count=0,
                message=f"本轮启动 {started} 项；已有任务或占位的缺陷已跳过",
            )
        except Exception:
            failed = 1 if active_candidate else 0
            if active_candidate:
                store.record_run_item(
                    schedule_run.id,
                    schedule_run.lease_token,
                    defect_id=active_candidate,
                    status=(
                        ScheduleRunItemStatus.NEEDS_ATTENTION
                        if candidate_reserved
                        else ScheduleRunItemStatus.FAILED
                    ),
                    reason=(
                        "执行中断且占位已保留，请核对任务列表"
                        if candidate_reserved
                        else "扫描或执行失败"
                    ),
                )
            store.finish_run(
                schedule_run.id,
                schedule_run.lease_token,
                status=ScheduleRunStatus.FAILED,
                discovered_count=max(discovered, failed),
                skipped_count=skipped,
                started_count=started,
                failed_count=failed,
                message=(
                    f"扫描或执行失败（已启动 {started} 项）；请检查连接、工作区及任务列表。"
                    "连续三次失败自动暂停"
                ),
                error_stage="dispatch",
            )


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


class ScheduleRun(BaseModel):
    """One durable attempt to scan and dispatch a schedule snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    schedule_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    schedule_version: int = Field(ge=0)
    trigger: ScheduleRunTrigger
    status: ScheduleRunStatus = ScheduleRunStatus.RUNNING
    plan_snapshot: Schedule
    lease_token: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    started_at: float = Field(ge=0)
    heartbeat_at: float = Field(ge=0)
    lease_until: float = Field(ge=0)
    finished_at: float | None = Field(default=None, ge=0)
    discovered_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    started_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    error_stage: str = Field(default="", max_length=80)
    error_message: str = Field(default="", max_length=2048)


class ScheduleRunItem(BaseModel):
    """A bounded, non-secret result for one candidate in a scheduled run."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    schedule_run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    ordinal: int = Field(ge=1)
    defect_id: str = Field(min_length=1, max_length=256)
    defect_name: str = Field(default="", max_length=512)
    action: DefectAction
    status: ScheduleRunItemStatus
    workflow_run_id: str = Field(default="", max_length=128)
    reason: str = Field(default="", max_length=2048)
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)

    @field_validator("defect_id", "defect_name", "workflow_run_id", "reason")
    @classmethod
    def clean_run_text(cls, value: str) -> str:
        if any(ord(c) < 32 and c not in "\t" or ord(c) == 127 for c in value):
            raise ValueError("invalid schedule run field")
        return value.strip()


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
            self._ensure_claim_columns(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS schedule_runs ("
                "id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, workspace TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "started_at REAL NOT NULL, lease_until REAL NOT NULL, payload TEXT NOT NULL)"
            )
            self._ensure_run_columns(db)
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS schedule_runs_one_active "
                "ON schedule_runs(schedule_id) WHERE status = 'running'"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS schedule_runs_schedule_started "
                "ON schedule_runs(schedule_id, started_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS schedule_runs_workspace_started "
                "ON schedule_runs(workspace, schedule_id, started_at DESC, id DESC)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS schedule_run_items ("
                "id TEXT PRIMARY KEY, schedule_run_id TEXT NOT NULL, defect_id TEXT NOT NULL DEFAULT '', "
                "ordinal INTEGER NOT NULL, "
                "payload TEXT NOT NULL, UNIQUE(schedule_run_id, ordinal))"
            )
            self._ensure_run_item_columns(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS schedule_run_items_run "
                "ON schedule_run_items(schedule_run_id, ordinal)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS schedule_run_items_candidate "
                "ON schedule_run_items(schedule_run_id, defect_id) WHERE defect_id <> ''"
            )

    @staticmethod
    def _ensure_claim_columns(db: sqlite3.Connection) -> None:
        existing = {row[1] for row in db.execute("PRAGMA table_info(claims)")}
        additions = {
            "schedule_run_id": "TEXT NOT NULL DEFAULT ''",
            "claimed_at": "REAL NOT NULL DEFAULT 0",
            "state": "TEXT NOT NULL DEFAULT 'legacy_reserved'",
        }
        for name, declaration in additions.items():
            if name not in existing:
                db.execute(f"ALTER TABLE claims ADD COLUMN {name} {declaration}")

    @staticmethod
    def _ensure_run_item_columns(db: sqlite3.Connection) -> None:
        existing = {row[1] for row in db.execute("PRAGMA table_info(schedule_run_items)")}
        if "defect_id" not in existing:
            db.execute(
                "ALTER TABLE schedule_run_items ADD COLUMN defect_id TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _ensure_run_columns(db: sqlite3.Connection) -> None:
        existing = {row[1] for row in db.execute("PRAGMA table_info(schedule_runs)")}
        if "workspace" not in existing:
            db.execute(
                "ALTER TABLE schedule_runs ADD COLUMN workspace TEXT NOT NULL DEFAULT ''"
            )
        rows = db.execute(
            "SELECT id,payload FROM schedule_runs WHERE workspace=''"
        ).fetchall()
        for run_id, payload in rows:
            try:
                workspace = ScheduleRun.model_validate_json(
                    payload
                ).plan_snapshot.workspace
            except ValueError:
                continue
            db.execute(
                "UPDATE schedule_runs SET workspace=? WHERE id=? AND workspace=''",
                (workspace, run_id),
            )

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

    @staticmethod
    def _validate_clock(now: float, lease_seconds: float) -> tuple[float, float]:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ScheduleError("运行租约参数无效")
        return float(now), float(lease_seconds)

    @staticmethod
    def _run_from_row(row: sqlite3.Row | tuple | None) -> ScheduleRun | None:
        if row is None:
            return None
        return ScheduleRun.model_validate_json(row[-1])

    def _insert_run(
        self,
        db: sqlite3.Connection,
        plan: Schedule,
        trigger: ScheduleRunTrigger,
        now: float,
        lease_seconds: float,
    ) -> ScheduleRun:
        run = ScheduleRun(
            schedule_id=plan.id,
            schedule_version=plan.version,
            trigger=trigger,
            plan_snapshot=plan,
            started_at=now,
            heartbeat_at=now,
            lease_until=now + lease_seconds,
        )
        try:
            db.execute(
                "INSERT INTO schedule_runs("
                "id,schedule_id,workspace,status,started_at,lease_until,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.schedule_id,
                    run.plan_snapshot.workspace,
                    run.status.value,
                    run.started_at,
                    run.lease_until,
                    run.model_dump_json(),
                ),
            )
        except sqlite3.IntegrityError:
            raise ScheduleError("该定时任务已有运行中的批次") from None
        return run

    def create_timer_run(
        self, now: float, *, lease_seconds: float = 300
    ) -> tuple[Schedule, ScheduleRun] | None:
        """Atomically advance one due plan and open its durable run ledger."""

        now, lease_seconds = self._validate_clock(now, lease_seconds)
        with self._db() as db:
            plans = sorted(
                (
                    Schedule.model_validate_json(row[0])
                    for row in db.execute("SELECT payload FROM schedules")
                ),
                key=lambda item: (item.next_due, item.id),
            )
            for current in plans:
                if not current.enabled or current.next_due > now:
                    continue
                active = db.execute(
                    "SELECT 1 FROM schedule_runs WHERE schedule_id=? AND status='running'",
                    (current.id,),
                ).fetchone()
                if active:
                    continue
                plan = current.model_copy(
                    update={
                        "next_due": now + current.interval_minutes * 60,
                        "last_result": "正在扫描/执行；中断后请核对已有任务",
                    }
                )
                run = self._insert_run(
                    db, plan, ScheduleRunTrigger.TIMER, now, lease_seconds
                )
                db.execute(
                    "UPDATE schedules SET payload=? WHERE id=?",
                    (plan.model_dump_json(), plan.id),
                )
                return plan, run
        return None

    def create_manual_run(
        self,
        schedule_id: str,
        *,
        expected_version: int,
        now: float | None = None,
        lease_seconds: float = 300,
        trigger: ScheduleRunTrigger = ScheduleRunTrigger.MANUAL,
    ) -> tuple[Schedule, ScheduleRun]:
        """Open an explicit one-off run without changing enablement or next due time."""

        if not isinstance(trigger, ScheduleRunTrigger) or trigger is ScheduleRunTrigger.TIMER:
            raise ScheduleError("立即执行来源无效")
        now, lease_seconds = self._validate_clock(
            time.time() if now is None else now, lease_seconds
        )
        with self._db() as db:
            row = db.execute(
                "SELECT payload FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
            plan = Schedule.model_validate_json(row[0]) if row else None
            if plan is None or plan.version != expected_version:
                raise ScheduleError("计划已变化或已删除，请刷新后重试")
            return plan, self._insert_run(db, plan, trigger, now, lease_seconds)

    def get_run(self, run_id: str) -> ScheduleRun | None:
        with self._db() as db:
            row = db.execute(
                "SELECT payload FROM schedule_runs WHERE id=?", (run_id,)
            ).fetchone()
        return self._run_from_row(row)

    def list_runs(self, schedule_id: str | None = None) -> tuple[ScheduleRun, ...]:
        with self._db() as db:
            if schedule_id is None:
                rows = db.execute(
                    "SELECT payload FROM schedule_runs ORDER BY started_at DESC,id DESC"
                ).fetchall()
            else:
                owner = db.execute(
                    "SELECT workspace FROM schedule_runs WHERE schedule_id=? LIMIT 1",
                    (schedule_id,),
                ).fetchone()
                if owner is None:
                    schedule_row = db.execute(
                        "SELECT payload FROM schedules WHERE id=?", (schedule_id,)
                    ).fetchone()
                    if schedule_row is not None:
                        owner = (
                            Schedule.model_validate_json(
                                schedule_row[0]
                            ).workspace,
                        )
                if owner is not None and owner[0] != workspace:
                    raise ScheduleError("定时任务运行历史不可用")
                rows = db.execute(
                    "SELECT payload FROM schedule_runs WHERE schedule_id=? "
                    "ORDER BY started_at DESC,id DESC",
                    (schedule_id,),
                ).fetchall()
        return tuple(ScheduleRun.model_validate_json(row[0]) for row in rows)

    @staticmethod
    def _validate_query_page(limit: int, offset: int, *, maximum: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > maximum
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or offset > _MAX_QUERY_OFFSET
        ):
            raise ScheduleError("运行历史查询范围无效")

    @staticmethod
    def _validate_history_scope(
        workspace: str, schedule_id: str | None = None
    ) -> None:
        if (
            type(workspace) is not str
            or not workspace
            or len(workspace) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in workspace)
            or (
                schedule_id is not None
                and re.fullmatch(r"[a-f0-9]{32}", schedule_id) is None
            )
        ):
            raise ScheduleError("运行历史查询范围无效")

    def query_runs(
        self,
        workspace: str,
        *,
        schedule_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ScheduleRun, ...]:
        """Read one bounded page without crossing the caller's workspace."""

        self._validate_history_scope(workspace, schedule_id)
        self._validate_query_page(limit, offset, maximum=_MAX_RUN_QUERY_LIMIT)
        with self._db() as db:
            if schedule_id is None:
                rows = db.execute(
                    "SELECT payload FROM schedule_runs WHERE workspace=? "
                    "ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?",
                    (workspace, limit, offset),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload FROM schedule_runs "
                    "WHERE workspace=? AND schedule_id=? "
                    "ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?",
                    (workspace, schedule_id, limit, offset),
                ).fetchall()
        return tuple(ScheduleRun.model_validate_json(row[0]) for row in rows)

    def heartbeat_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        now: float | None = None,
        lease_seconds: float = 300,
    ) -> ScheduleRun:
        now, lease_seconds = self._validate_clock(
            time.time() if now is None else now, lease_seconds
        )
        with self._db() as db:
            run = self._owned_running_run(db, run_id, lease_token, now=now)
            updated = self._replace_run(
                run, heartbeat_at=now, lease_until=now + lease_seconds
            )
            self._update_run(db, updated)
            return updated

    @staticmethod
    def _owned_running_run(
        db: sqlite3.Connection,
        run_id: str,
        lease_token: str,
        *,
        now: float,
    ) -> ScheduleRun:
        row = db.execute(
            "SELECT payload FROM schedule_runs WHERE id=?", (run_id,)
        ).fetchone()
        run = ScheduleRun.model_validate_json(row[0]) if row else None
        if (
            run is None
            or run.status is not ScheduleRunStatus.RUNNING
            or run.lease_token != lease_token
            or run.lease_until <= now
        ):
            raise ScheduleError("运行租约已失效")
        return run

    @staticmethod
    def _update_run(db: sqlite3.Connection, run: ScheduleRun) -> None:
        db.execute(
            "UPDATE schedule_runs SET status=?,lease_until=?,payload=? WHERE id=?",
            (run.status.value, run.lease_until, run.model_dump_json(), run.id),
        )

    @staticmethod
    def _replace_run(run: ScheduleRun, **updates) -> ScheduleRun:
        try:
            return ScheduleRun.model_validate(
                {**run.model_dump(mode="python"), **updates}
            )
        except ValueError:
            raise ScheduleError("运行台账内容无效") from None

    def record_run_item(
        self,
        run_id: str,
        lease_token: str,
        *,
        defect_id: str,
        status: ScheduleRunItemStatus,
        defect_name: str = "",
        workflow_run_id: str = "",
        reason: str = "",
        now: float | None = None,
    ) -> ScheduleRunItem:
        timestamp, _ = self._validate_clock(
            time.time() if now is None else now, 1
        )
        if not isinstance(status, ScheduleRunItemStatus):
            raise ScheduleError("运行明细状态无效")
        with self._db() as db:
            run = self._owned_running_run(
                db, run_id, lease_token, now=timestamp
            )
            existing = db.execute(
                "SELECT payload FROM schedule_run_items "
                "WHERE schedule_run_id=? AND defect_id=?",
                (run_id, defect_id),
            ).fetchone()
            if existing:
                current = ScheduleRunItem.model_validate_json(existing[0])
                try:
                    item = ScheduleRunItem.model_validate(
                        {
                            **current.model_dump(mode="python"),
                            "defect_name": defect_name,
                            "status": status,
                            "workflow_run_id": workflow_run_id,
                            "reason": reason,
                            "updated_at": timestamp,
                        }
                    )
                except ValueError:
                    raise ScheduleError("运行明细内容无效") from None
                db.execute(
                    "UPDATE schedule_run_items SET payload=? WHERE id=?",
                    (item.model_dump_json(), item.id),
                )
                return item
            row = db.execute(
                "SELECT COALESCE(MAX(ordinal),0) FROM schedule_run_items "
                "WHERE schedule_run_id=?",
                (run_id,),
            ).fetchone()
            item = ScheduleRunItem(
                schedule_run_id=run_id,
                ordinal=int(row[0]) + 1,
                defect_id=defect_id,
                defect_name=defect_name,
                action=run.plan_snapshot.action,
                status=status,
                workflow_run_id=workflow_run_id,
                reason=reason,
                created_at=timestamp,
                updated_at=timestamp,
            )
            db.execute(
                "INSERT INTO schedule_run_items(id,schedule_run_id,defect_id,ordinal,payload) "
                "VALUES (?,?,?,?,?)",
                (
                    item.id,
                    item.schedule_run_id,
                    item.defect_id,
                    item.ordinal,
                    item.model_dump_json(),
                ),
            )
            return item

    def list_run_items(self, run_id: str) -> tuple[ScheduleRunItem, ...]:
        with self._db() as db:
            rows = db.execute(
                "SELECT payload FROM schedule_run_items WHERE schedule_run_id=? "
                "ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        return tuple(ScheduleRunItem.model_validate_json(row[0]) for row in rows)

    def query_run_items(
        self,
        workspace: str,
        run_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ScheduleRunItem, ...]:
        """Read bounded run details after authorizing the parent workspace."""

        self._validate_history_scope(workspace)
        if type(run_id) is not str or re.fullmatch(r"[a-f0-9]{32}", run_id) is None:
            raise ScheduleError("运行批次不可用")
        self._validate_query_page(limit, offset, maximum=_MAX_RUN_ITEM_QUERY_LIMIT)
        with self._db() as db:
            authorized = db.execute(
                "SELECT 1 FROM schedule_runs WHERE id=? AND workspace=?",
                (run_id, workspace),
            ).fetchone()
            if authorized is None:
                raise ScheduleError("运行批次不可用")
            rows = db.execute(
                "SELECT payload FROM schedule_run_items WHERE schedule_run_id=? "
                "ORDER BY ordinal LIMIT ? OFFSET ?",
                (run_id, limit, offset),
            ).fetchall()
        return tuple(ScheduleRunItem.model_validate_json(row[0]) for row in rows)

    def finish_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        status: ScheduleRunStatus,
        discovered_count: int,
        skipped_count: int,
        started_count: int,
        failed_count: int,
        message: str,
        error_stage: str = "",
        now: float | None = None,
    ) -> ScheduleRun:
        if not isinstance(status, ScheduleRunStatus) or status not in {
            ScheduleRunStatus.SUCCESS,
            ScheduleRunStatus.PARTIAL,
            ScheduleRunStatus.FAILED,
        }:
            raise ScheduleError("运行完成状态无效")
        timestamp, _ = self._validate_clock(time.time() if now is None else now, 1)
        counts = (discovered_count, skipped_count, started_count, failed_count)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise ScheduleError("运行统计无效")
        if skipped_count + started_count + failed_count > discovered_count:
            raise ScheduleError("运行统计不一致")
        with self._db() as db:
            run = self._owned_running_run(
                db, run_id, lease_token, now=timestamp
            )
            updated = self._replace_run(
                run,
                status=status,
                finished_at=timestamp,
                heartbeat_at=timestamp,
                lease_until=timestamp,
                discovered_count=discovered_count,
                skipped_count=skipped_count,
                started_count=started_count,
                failed_count=failed_count,
                error_stage=error_stage,
                error_message=(
                    message
                    if status in {ScheduleRunStatus.PARTIAL, ScheduleRunStatus.FAILED}
                    else ""
                ),
            )
            self._update_run(db, updated)
            row = db.execute(
                "SELECT payload FROM schedules WHERE id=?", (run.schedule_id,)
            ).fetchone()
            current = Schedule.model_validate_json(row[0]) if row else None
            if current is not None and current.version == run.schedule_version:
                failures = current.failures + 1 if status is ScheduleRunStatus.FAILED else 0
                plan = current.model_copy(
                    update={
                        "last_result": message,
                        "failures": failures,
                        "enabled": current.enabled and failures < 3,
                    }
                )
                db.execute(
                    "UPDATE schedules SET payload=? WHERE id=?",
                    (plan.model_dump_json(), plan.id),
                )
            return updated

    def recover_expired_runs(
        self, now: float | None = None
    ) -> tuple[ScheduleRun, ...]:
        """Close abandoned leases without releasing or rewriting dedupe claims."""

        timestamp, _ = self._validate_clock(time.time() if now is None else now, 1)
        recovered: list[ScheduleRun] = []
        with self._db() as db:
            rows = db.execute(
                "SELECT payload FROM schedule_runs WHERE status='running' "
                "AND lease_until<=? ORDER BY started_at,id",
                (timestamp,),
            ).fetchall()
            for row in rows:
                run = ScheduleRun.model_validate_json(row[0])
                updated = self._replace_run(
                    run,
                    status=ScheduleRunStatus.INTERRUPTED,
                    finished_at=timestamp,
                    heartbeat_at=timestamp,
                    lease_until=timestamp,
                    error_stage="lease",
                    error_message="运行租约过期；请核对任务列表和占位记录",
                )
                self._update_run(db, updated)
                recovered.append(updated)
        return tuple(recovered)

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

    def reserve_defect(
        self,
        plan: Schedule,
        defect: str,
        *,
        schedule_run_id: str = "",
        now: float | None = None,
    ) -> bool:
        with self._db() as db:
            row = db.execute("SELECT payload FROM schedules WHERE id=?", (plan.id,)).fetchone()
            if row is None:
                return False
            current = Schedule.model_validate_json(row[0])
            if not current.enabled or current.version != plan.version:
                return False
            claimed_at = time.time() if now is None else now
            state = "reserved" if schedule_run_id else "legacy_reserved"
            cursor = db.execute(
                "INSERT OR IGNORE INTO claims("
                "scope,defect,action,schedule_run_id,claimed_at,state) "
                "VALUES (?,?,?,?,?,?)",
                (
                    plan.workspace,
                    defect,
                    plan.action.value,
                    schedule_run_id,
                    claimed_at,
                    state,
                ),
            )
            return cursor.rowcount == 1

    def reserve_run_item(
        self,
        plan: Schedule,
        defect: str,
        run_id: str,
        lease_token: str,
        *,
        defect_name: str = "",
        now: float | None = None,
    ) -> ScheduleRunItem | None:
        """Atomically create a permanent claim and its RESERVED ledger item."""

        timestamp, _ = self._validate_clock(
            time.time() if now is None else now, 1
        )
        with self._db() as db:
            run = self._owned_running_run(
                db, run_id, lease_token, now=timestamp
            )
            row = db.execute(
                "SELECT payload FROM schedules WHERE id=?", (plan.id,)
            ).fetchone()
            current = Schedule.model_validate_json(row[0]) if row else None
            if (
                current is None
                or not current.enabled
                or current.version != plan.version
                or run.schedule_id != plan.id
                or run.schedule_version != plan.version
                or run.plan_snapshot.workspace != plan.workspace
                or run.plan_snapshot.action is not plan.action
            ):
                return None
            cursor = db.execute(
                "INSERT OR IGNORE INTO claims("
                "scope,defect,action,schedule_run_id,claimed_at,state) "
                "VALUES (?,?,?,?,?,'reserved')",
                (
                    plan.workspace,
                    defect,
                    plan.action.value,
                    run.id,
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return self._insert_reserved_run_item(
                db,
                run,
                defect=defect,
                defect_name=defect_name,
                timestamp=timestamp,
            )

    @staticmethod
    def _insert_reserved_run_item(
        db: sqlite3.Connection,
        run: ScheduleRun,
        *,
        defect: str,
        defect_name: str,
        timestamp: float,
    ) -> ScheduleRunItem:
        row = db.execute(
            "SELECT COALESCE(MAX(ordinal),0) FROM schedule_run_items "
            "WHERE schedule_run_id=?",
            (run.id,),
        ).fetchone()
        item = ScheduleRunItem(
            schedule_run_id=run.id,
            ordinal=int(row[0]) + 1,
            defect_id=defect,
            defect_name=defect_name,
            action=run.plan_snapshot.action,
            status=ScheduleRunItemStatus.RESERVED,
            reason="已保留永久去重占位",
            created_at=timestamp,
            updated_at=timestamp,
        )
        db.execute(
            "INSERT INTO schedule_run_items("
            "id,schedule_run_id,defect_id,ordinal,payload) VALUES (?,?,?,?,?)",
            (
                item.id,
                item.schedule_run_id,
                item.defect_id,
                item.ordinal,
                item.model_dump_json(),
            ),
        )
        return item

    def record_run(
        self,
        plan: Schedule,
        defect: str,
        run_id: str,
        *,
        schedule_run_id: str = "",
    ) -> None:
        with self._db() as db:
            if schedule_run_id:
                db.execute(
                    "UPDATE claims SET run_id=?,state='started' "
                    "WHERE scope=? AND defect=? AND action=? AND schedule_run_id=?",
                    (
                        run_id,
                        plan.workspace,
                        defect,
                        plan.action.value,
                        schedule_run_id,
                    ),
                )
            else:
                db.execute(
                    "UPDATE claims SET run_id=? WHERE scope=? AND defect=? AND action=?",
                    (run_id, plan.workspace, defect, plan.action.value),
                )

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
