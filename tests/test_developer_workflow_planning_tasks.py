from __future__ import annotations

from pathlib import Path

import pytest

from src.developer_workflow.planning_tasks import (
    PlanningTask,
    PlanningTaskError,
    PlanningTaskPriority,
    PlanningTaskStatus,
    PlanningTaskStore,
    PlanningTaskType,
)


def test_planning_task_store_crud_and_workspace_scope(tmp_path: Path) -> None:
    store = PlanningTaskStore(tmp_path / "board")
    created = store.save(
        PlanningTask(workspace="camera", title="补充 macOS 验证"),
        expected_version=None,
        now=10,
    )

    assert created.version == 1
    assert created.created_at == 10
    assert created.task_type is PlanningTaskType.TASK
    assert store.list("other") == ()
    assert store.list("camera") == (created,)

    updated = store.save(
        created.model_copy(
            update={
                "status": PlanningTaskStatus.IN_PROGRESS,
                "priority": PlanningTaskPriority.HIGH,
                "task_type": PlanningTaskType.DEFECT,
            }
        ),
        expected_version=created.version,
        now=20,
    )
    assert updated.version == 2
    assert updated.created_at == 10
    assert updated.updated_at == 20
    assert updated.status is PlanningTaskStatus.IN_PROGRESS
    assert updated.task_type is PlanningTaskType.DEFECT

    with pytest.raises(PlanningTaskError, match="任务已变化"):
        store.save(created, expected_version=created.version, now=30)
    with pytest.raises(PlanningTaskError, match="任务已变化"):
        store.delete(
            updated.id, workspace="camera", expected_version=created.version
        )

    store.delete(updated.id, workspace="camera", expected_version=updated.version)
    assert store.list("camera") == ()


def test_planning_task_store_rejects_cross_workspace_mutation(
    tmp_path: Path,
) -> None:
    store = PlanningTaskStore(tmp_path / "board")
    created = store.save(
        PlanningTask(workspace="camera", title="任务"),
        expected_version=None,
        now=10,
    )

    with pytest.raises(PlanningTaskError, match="不可更换"):
        store.save(
            created.model_copy(update={"workspace": "desktop"}),
            expected_version=created.version,
            now=20,
        )
    with pytest.raises(PlanningTaskError, match="任务已变化"):
        store.delete(
            created.id, workspace="desktop", expected_version=created.version
        )
