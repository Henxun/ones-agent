"""Read-only reconstruction of terminal UI run summaries."""

from __future__ import annotations

from collections.abc import Mapping

from ..state_store import FileRunStore, RunCorruptedError, RunNotFoundError
from .models import RunActivity, RunFilter, RunSummary, TuiDisplayError, WorkspaceSummary


class RunIndex:
    """Build display summaries from the authoritative file-backed store."""

    def __init__(self, store: FileRunStore) -> None:
        self._store = store

    def list(
        self,
        filters: RunFilter,
        activities: Mapping[str, RunActivity] | None = None,
        *,
        workspace: WorkspaceSummary | None = None,
    ) -> tuple[RunSummary, ...]:
        activity_by_id = activities or {}
        valid: list[RunSummary] = []
        corrupted: list[RunSummary] = []
        for run_id in self._store.list_run_ids():
            try:
                run = self._store.load(run_id, read_only=True)
            except RunCorruptedError:
                if workspace is not None:
                    continue
                item = RunSummary.corrupted_entry(run_id)
                if filters.matches(item):
                    corrupted.append(item)
                continue
            except RunNotFoundError:
                continue
            if workspace is not None:
                mapping = run.repository_group or run.repository
                if mapping is None or (
                    mapping.key != workspace.key
                    or mapping.project_id != workspace.project_id
                    or mapping.iteration_id != workspace.iteration_id
                ):
                    continue
            try:
                item = RunSummary.from_run(
                    run,
                    activity=activity_by_id.get(run_id, RunActivity.IDLE),
                )
            except TuiDisplayError:
                item = RunSummary.corrupted_entry(run_id)
                if filters.matches(item):
                    corrupted.append(item)
                continue
            if filters.matches_facts(
                state=run.state,
                workflow_type=run.type,
                run_id=run.run_id,
                work_item_id=run.work_item_id,
                updated_at=run.updated_at,
            ):
                valid.append(item)

        valid.sort(key=lambda item: item.run_id)
        valid.sort(key=lambda item: item.updated_at, reverse=True)
        corrupted.sort(key=lambda item: item.run_id)
        return (*valid, *corrupted)

    def delete(self, run_id: str) -> None:
        """Delete one authoritative task record from the backing store."""

        self._store.delete(run_id)
