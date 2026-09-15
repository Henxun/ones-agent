"""Workspace-local planning tasks used by the operator task board."""

from __future__ import annotations

from contextlib import contextmanager
from enum import Enum
import os
from pathlib import Path
import sqlite3
from threading import RLock
import time
from typing import Iterator
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .private_paths import prepare_private_directory
from .setup_store import (
    _open_lock_nofollow,
    _protect_private_file,
    _validate_regular_file,
)


class PlanningTaskError(RuntimeError):
    """A planning task mutation could not be completed safely."""


class PlanningTaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class PlanningTaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlanningTaskType(str, Enum):
    TASK = "task"
    DEFECT = "defect"
    REQUIREMENT = "requirement"


class PlanningTask(BaseModel):
    """One manually managed, workspace-local planning card."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$"
    )
    version: int = Field(default=0, ge=0)
    workspace: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    task_type: PlanningTaskType = PlanningTaskType.TASK
    status: PlanningTaskStatus = PlanningTaskStatus.TODO
    priority: PlanningTaskPriority = PlanningTaskPriority.MEDIUM
    created_at: float = Field(default=0, ge=0)
    updated_at: float = Field(default=0, ge=0)

    @field_validator("workspace", "title")
    @classmethod
    def clean_single_line(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(
            ord(character) < 32 or ord(character) == 127
            for character in cleaned
        ):
            raise ValueError("invalid planning task field")
        return cleaned

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str) -> str:
        cleaned = value.strip()
        if any(
            (ord(character) < 32 and character not in "\n\t")
            or ord(character) == 127
            for character in cleaned
        ):
            raise ValueError("invalid planning task description")
        return cleaned


class PlanningTaskStore:
    """Private SQLite store with optimistic version checks."""

    def __init__(self, root: Path) -> None:
        self.root = prepare_private_directory(root)
        self.path = self.root / "planning_tasks.sqlite3"
        self._gate = RLock()
        descriptor, created = _open_lock_nofollow(self.path)
        try:
            if created:
                _protect_private_file(self.path)
            _validate_regular_file(self.path, descriptor=descriptor)
        finally:
            os.close(descriptor)
        with self._db() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS planning_tasks ("
                "id TEXT PRIMARY KEY, workspace TEXT NOT NULL, status TEXT NOT NULL, "
                "updated_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            database.execute(
                "CREATE INDEX IF NOT EXISTS planning_tasks_workspace_status "
                "ON planning_tasks(workspace, status, updated_at DESC, id)"
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._gate:
            prepare_private_directory(self.root)
            _validate_regular_file(self.path)
            database = sqlite3.connect(self.path, timeout=5)
            try:
                database.execute("PRAGMA synchronous=FULL")
                database.execute("BEGIN IMMEDIATE")
                yield database
                database.commit()
            except BaseException:
                database.rollback()
                raise
            finally:
                database.close()

    def list(self, workspace: str) -> tuple[PlanningTask, ...]:
        self._validate_workspace(workspace)
        with self._db() as database:
            rows = database.execute(
                "SELECT payload FROM planning_tasks WHERE workspace=? "
                "ORDER BY updated_at DESC,id",
                (workspace,),
            ).fetchall()
        try:
            return tuple(PlanningTask.model_validate_json(row[0]) for row in rows)
        except ValueError:
            raise PlanningTaskError("任务看板数据不可用") from None

    def save(
        self,
        task: PlanningTask,
        *,
        expected_version: int | None,
        now: float | None = None,
    ) -> PlanningTask:
        timestamp = time.time() if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or timestamp < 0:
            raise PlanningTaskError("任务时间无效")
        with self._db() as database:
            row = database.execute(
                "SELECT payload FROM planning_tasks WHERE id=?", (task.id,)
            ).fetchone()
            current = PlanningTask.model_validate_json(row[0]) if row else None
            if (
                (current is None) != (expected_version is None)
                or current is not None
                and current.version != expected_version
            ):
                raise PlanningTaskError("任务已变化，请刷新后重试")
            if current is not None and current.workspace != task.workspace:
                raise PlanningTaskError("不可更换任务所属工作区")
            saved = task.model_copy(
                update={
                    "version": current.version + 1 if current else 1,
                    "created_at": current.created_at if current else float(timestamp),
                    "updated_at": float(timestamp),
                }
            )
            database.execute(
                "INSERT OR REPLACE INTO planning_tasks"
                "(id,workspace,status,updated_at,payload) VALUES (?,?,?,?,?)",
                (
                    saved.id,
                    saved.workspace,
                    saved.status.value,
                    saved.updated_at,
                    saved.model_dump_json(),
                ),
            )
        return saved

    def delete(
        self, task_id: str, *, workspace: str, expected_version: int
    ) -> None:
        self._validate_workspace(workspace)
        with self._db() as database:
            row = database.execute(
                "SELECT payload FROM planning_tasks WHERE id=? AND workspace=?",
                (task_id, workspace),
            ).fetchone()
            current = PlanningTask.model_validate_json(row[0]) if row else None
            if current is None or current.version != expected_version:
                raise PlanningTaskError("任务已变化或已删除，请刷新后重试")
            database.execute(
                "DELETE FROM planning_tasks WHERE id=? AND workspace=?",
                (task_id, workspace),
            )

    @staticmethod
    def _validate_workspace(workspace: str) -> None:
        if (
            type(workspace) is not str
            or not workspace
            or len(workspace) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in workspace)
        ):
            raise PlanningTaskError("工作区无效")


__all__ = [
    "PlanningTask",
    "PlanningTaskError",
    "PlanningTaskPriority",
    "PlanningTaskStatus",
    "PlanningTaskStore",
    "PlanningTaskType",
]
