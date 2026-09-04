"""Owned resources for one activated TUI runtime."""

from __future__ import annotations

import asyncio
import _thread
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any

from ..runtime_bootstrap import RuntimeHandle
from .controller import TuiController
from .run_index import RunIndex
from .supervisor import RunTaskSupervisor, TaskEvent


class TuiRuntimeCloseError(RuntimeError):
    """A runtime session could not be completely closed."""


@dataclass(frozen=True, slots=True)
class _CloseOutcome:
    fatal: BaseException | None
    failed: bool


class TuiRuntimeSession:
    """Own the dashboard controller, task supervisor, index and runtime handle."""

    def __init__(
        self,
        *,
        handle: RuntimeHandle | Any | None,
        controller: TuiController | Any,
        run_index: RunIndex | Any,
        supervisor: RunTaskSupervisor | Any,
        event_sink: Callable[[TaskEvent], None],
        close_timeout: float = 5.0,
    ) -> None:
        if (
            isinstance(close_timeout, bool)
            or not isinstance(close_timeout, (int, float))
            or close_timeout <= 0
        ):
            raise ValueError("TUI runtime close timeout is invalid")
        self.handle = handle
        self.controller = controller
        self.run_index = run_index
        self.supervisor = supervisor
        self._event_sink: Callable[[TaskEvent], None] | None = event_sink
        self._close_timeout = float(close_timeout)
        self._close_task: asyncio.Task[_CloseOutcome] | None = None
        self._closed = False

    @property
    def close_complete(self) -> bool:
        task = self._close_task
        return task is not None and task.done() and not task.cancelled()

    @classmethod
    def from_handle(
        cls,
        handle: RuntimeHandle,
        max_concurrency: int,
        event_sink: Callable[[TaskEvent], None],
        *,
        publishing_enabled: bool = True,
    ) -> "TuiRuntimeSession":
        index = RunIndex(handle.orchestrator.store)
        from ..schedules import ScheduleStore
        controller = TuiController(
            handle.orchestrator,
            index,
            workflow_saver=getattr(handle, "workflow_saver", None),
            publishing_enabled=publishing_enabled,
        )
        session: TuiRuntimeSession

        controller.schedule_store = ScheduleStore(handle.orchestrator.config.run_root / ".schedules")

        def emit(event: TaskEvent) -> None:
            session.emit(event)

        supervisor = RunTaskSupervisor(max_concurrency, emit)
        session = cls(
            handle=handle,
            controller=controller,
            run_index=index,
            supervisor=supervisor,
            event_sink=event_sink,
        )
        return session

    @classmethod
    def from_controller(
        cls,
        controller: TuiController,
        max_concurrency: int,
        event_sink: Callable[[TaskEvent], None],
    ) -> "TuiRuntimeSession":
        session: TuiRuntimeSession

        def emit(event: TaskEvent) -> None:
            session.emit(event)

        supervisor = RunTaskSupervisor(max_concurrency, emit)
        session = cls(
            handle=None,
            controller=controller,
            run_index=getattr(controller, "_run_index", None),
            supervisor=supervisor,
            event_sink=event_sink,
        )
        return session

    def emit(self, event: TaskEvent) -> None:
        sink = self._event_sink
        if not self._closed and sink is not None:
            sink(event)

    async def close(self) -> None:
        """Stop UI work, controller runtime and shared services exactly once."""

        task = self._close_task
        if task is None:
            self._closed = True
            self._event_sink = None
            task = asyncio.create_task(self._drain_close())
            self._close_task = task
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(task), timeout=self._close_timeout
            )
        except asyncio.TimeoutError:
            raise TuiRuntimeCloseError("TUI runtime close failed") from None
        if outcome.fatal is not None:
            raise outcome.fatal
        if outcome.failed:
            raise TuiRuntimeCloseError("TUI runtime close failed") from None

    async def _drain_close(self) -> _CloseOutcome:
        fatal: BaseException | None = None
        failed = False
        try:
            async with asyncio.timeout(self._close_timeout):
                await self.supervisor.close()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                fatal = error
            else:
                failed = True

        done = Event()
        outcome: list[object] = []

        def worker() -> None:
            worker_fatal: BaseException | None = None
            worker_failed = False
            try:
                self.controller.close()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    worker_fatal = error
                else:
                    worker_failed = True
            if self.handle is not None:
                try:
                    self.handle.close()
                except BaseException as error:
                    if (
                        worker_fatal is None
                        and isinstance(error, (KeyboardInterrupt, SystemExit))
                    ):
                        worker_fatal = error
                    elif not isinstance(error, (KeyboardInterrupt, SystemExit)):
                        worker_failed = True
            outcome.extend((worker_fatal, worker_failed))
            done.set()

        start_failed = False
        try:
            Thread(
                target=worker, name="ones-dev-tui-runtime-close", daemon=True
            ).start()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                fatal = fatal or error
            else:
                start_failed = True
            try:
                _thread.start_new_thread(worker, ())
            except BaseException as fallback_error:
                if isinstance(fallback_error, (KeyboardInterrupt, SystemExit)):
                    fatal = fatal or fallback_error
                start_failed = True
        while not done.is_set():
            await asyncio.sleep(min(0.01, self._close_timeout))
        worker_fatal = outcome[0]
        result = _CloseOutcome(
            fatal=fatal or (
                worker_fatal if isinstance(worker_fatal, BaseException) else None
            ),
            failed=failed or start_failed or bool(outcome[1]),
        )
        self.handle = None
        self.controller = None
        self.run_index = None
        self.supervisor = None
        return result


__all__ = ["TuiRuntimeCloseError", "TuiRuntimeSession"]
