"""Full-screen Textual application for developer workflows."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from dataclasses import dataclass

from textual import on
from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Button

from .controller import TuiController
from ..setup_models import OnesProbePublicConfig
from .models import RunActivity
from .screens import DashboardScreen, HelpScreen, SettingsView
from .runtime_session import TuiRuntimeSession
from .setup_screens import (
    SetupImportContext,
    SetupRecoveryScreen,
    SetupRootScreen,
    SetupWizardScreen,
)
from .supervisor import TaskEvent


class TuiTaskMessage(Message):
    """Transport a safe supervisor event onto Textual's UI loop."""

    def __init__(self, event: TaskEvent) -> None:
        super().__init__()
        self.event = event


class _TransitionClosed(RuntimeError):
    """Internal control flow when UI shutdown has acquired lifecycle priority."""


class TuiAppCloseError(RuntimeError):
    """The bounded UI close wait expired without losing resource ownership."""


class _OwnedCloseCancelled(RuntimeError):
    """An owned close coroutine was externally cancelled before completion."""


@dataclass(frozen=True, slots=True)
class _AppCloseOutcome:
    fatal: BaseException | None = None
    complete: bool = True


class DeveloperWorkflowTuiApp(App[None]):
    """Keyboard-first, mouse-capable full-screen workflow console."""

    CSS_PATH = "tui.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("n", "new_run", "New"),
        Binding("/", "search", "Search"),
        Binding("f", "filter", "Filter"),
    ]

    def __init__(
        self,
        controller: TuiController | None = None,
        max_concurrency: int = 3,
        *,
        setup_controller: object | None = None,
        setup_controller_factory: Callable[[], object] | None = None,
        runtime_bootstrapper: object | None = None,
        setup_import: SetupImportContext | None = None,
        provider_type: str = "configured",
        sandbox_configured: bool = True,
        publishing_enabled: bool = True,
        poll_interval: float = 2.0,
        close_timeout: float = 5.0,
    ) -> None:
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or poll_interval <= 0
            or isinstance(close_timeout, bool)
            or not isinstance(close_timeout, (int, float))
            or close_timeout <= 0
        ):
            raise ValueError("TUI timing configuration is invalid")
        if type(publishing_enabled) is not bool:
            raise ValueError("publishing capability is invalid")
        super().__init__()
        if controller is None and (
            runtime_bootstrapper is None
            or (setup_controller is None and setup_controller_factory is None)
        ):
            raise ValueError("TUI bootstrap configuration is invalid")
        if controller is not None and setup_controller is not None:
            raise ValueError("TUI bootstrap configuration is invalid")
        self.setup_controller = setup_controller
        self._setup_controller_factory = setup_controller_factory
        if self.setup_controller is None and self._setup_controller_factory is not None:
            self.setup_controller = self._new_setup_controller()
        configured_builder = getattr(self.setup_controller, "_runtime_builder", None)
        if (
            runtime_bootstrapper is not None
            and configured_builder is not None
            and configured_builder is not runtime_bootstrapper
        ):
            raise ValueError("TUI bootstrap configuration is invalid")
        self._setup_import = setup_import
        self.runtime_session: TuiRuntimeSession | None = None
        self.controller: TuiController | None = None
        self.supervisor: object | None = None
        self.poll_interval = float(poll_interval)
        self._close_timeout = float(close_timeout)
        self._max_concurrency = max_concurrency
        self._publishing_enabled = publishing_enabled
        self.settings = SettingsView(
            max_concurrency=max_concurrency,
            provider_type=provider_type,
            sandbox_configured=sandbox_configured,
        )
        self.activities: dict[str, RunActivity] = {}
        self._accept_events = True
        self._ui_closed = False
        self._close_started = False
        self._close_complete = asyncio.Event()
        self._close_task: asyncio.Task[_AppCloseOutcome] | None = None
        self._closing_setup_controller: object | None = None
        self._transition_lock = asyncio.Lock()
        self._activation_handles: set[int] = set()
        self._active_handle_identity: int | None = None
        self._pending_runtime_handle: object | None = None
        self._poll_timer: Timer | None = None
        self._schedule_timer = None
        self._schedule_task = None
        self._reconfiguring = False
        app_ref = weakref.ref(self)

        def sink(event: TaskEvent) -> None:
            app = app_ref()
            if app is not None and app._accept_events:
                app.post_message(TuiTaskMessage(event))

        self._event_sink = sink
        self._dashboard: DashboardScreen | None = None
        self._setup_screen: SetupRootScreen | None = None
        if controller is not None:
            self.runtime_session = TuiRuntimeSession.from_controller(
                controller, max_concurrency, sink
            )
            self._bind_runtime_session(self.runtime_session)

    async def on_mount(self) -> None:
        async with self._transition_lock:
            await self._mount_initial_screen()

    async def _mount_initial_screen(self) -> None:
        if self._ui_closed:
            return
        if self.runtime_session is None:
            assert self.setup_controller is not None
            try:
                handle = await self.setup_controller.activate_existing()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                handle = None
            if handle is None:
                recovery = await asyncio.to_thread(
                    lambda: getattr(self.setup_controller, "recovery_state", None)
                )
                if getattr(recovery, "owner_generation", None):
                    await self._show_recovery(recovery)
                else:
                    await self._show_setup()
                return
            try:
                await self._close_setup_controller()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    await self._discard_runtime(handle=handle, session=None)
                    raise
                await self._recover_setup(handle=handle)
                return
            if self._ui_closed:
                await self._discard_runtime(handle=handle, session=None)
                return
            await self._replace_with_runtime(handle)
            return
        await self._mount_dashboard()

    def _build_runtime_session(self, handle: object) -> TuiRuntimeSession:
        return TuiRuntimeSession.from_handle(
            handle,
            self._max_concurrency,
            self._event_sink,
            publishing_enabled=self._publishing_enabled,  # type: ignore[arg-type]
        )

    def _new_setup_controller(self) -> object:
        factory = self._setup_controller_factory
        if factory is None:
            raise RuntimeError("TUI setup recovery is unavailable")
        try:
            controller = factory()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError("TUI setup recovery is unavailable") from None
        if controller is None:
            raise RuntimeError("TUI setup recovery is unavailable")
        return controller

    async def _show_setup(self) -> None:
        if self.setup_controller is None:
            self.setup_controller = self._new_setup_controller()
        try:
            screen = SetupWizardScreen(
                self.setup_controller,
                activation_callback=self._activate_setup_candidate,
                cancel_callback=self._cancel_setup,
                import_context=self._setup_import,
                import_discard_callback=self._discard_setup_import,
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            screen = SetupRootScreen(self.setup_controller)
        self._setup_screen = screen
        await self.push_screen(screen, self._setup_done)

    def _discard_setup_import(self) -> None:
        context = self._setup_import
        self._setup_import = None
        if context is not None:
            context.discard()

    async def _show_recovery(self, recovery_state: object) -> None:
        assert self.setup_controller is not None
        screen = SetupRecoveryScreen(
            self.setup_controller,
            owner_generation=getattr(recovery_state, "owner_generation", None),
            recovery_state=recovery_state,
            recovery_callback=self._run_recovery_action,
        )
        self._setup_screen = screen
        await self.push_screen(screen, self._recovery_done)

    async def _recovery_done(self, action: object | None) -> None:
        if action not in {"restore", "discard"}:
            return
        async with self._transition_lock:
            if self._ui_closed:
                return
            try:
                removed = await self._remove_setup_screen()
            except BaseException as error:
                await self._discard_setup_controller()
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                removed = False
            if not removed:
                await self._recover_recovery_removal_failure()
                return
            if self._ui_closed:
                await self._discard_setup_controller()
                return
            controller = self.setup_controller
            if controller is None:
                return
            handle = await controller.activate_existing()
            if handle is None:
                if action == "discard":
                    await self._show_setup()
                else:
                    await self._show_setup()
                return
            await self._finish_setup(handle)

    async def _run_recovery_action(
        self, action: str, owner_generation: str
    ) -> bool:
        """Serialize recovery mutation with activation, reconfiguration, and exit."""

        async with self._transition_lock:
            if self._ui_closed:
                return False
            controller = self.setup_controller
            if controller is None:
                return False
            try:
                if action == "cleanup":
                    generations = await controller.list_orphan_generations()
                    await controller.cleanup_orphans(generations)
                elif action == "restore":
                    await controller.restore_pending(owner_generation)
                elif action == "discard":
                    await controller.discard_pending(owner_generation)
                else:
                    return False
                return True
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                return False

    async def _recover_recovery_removal_failure(self) -> None:
        """Replace a stale recovery owner without activating or reading secrets."""

        await self._discard_setup_controller()
        if self._ui_closed:
            return
        try:
            replacement = self._new_setup_controller()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            self._ui_closed = True
            self._accept_events = False
            self.exit()
            return
        self.setup_controller = replacement
        screen = self._setup_screen
        if screen is not None:
            screen.controller = replacement
            try:
                if self.screen is screen:
                    return
            except BaseException:
                pass
        self._setup_screen = None
        recovery = await asyncio.to_thread(
            lambda: getattr(replacement, "recovery_state", None)
        )
        if getattr(recovery, "owner_generation", None):
            await self._show_recovery(recovery)
        else:
            await self._show_setup()

    async def _activate_setup_candidate(self) -> object | None:
        """Delegate the explicit review action to the setup controller boundary."""

        controller = self.setup_controller
        activate = getattr(controller, "save_and_activate", None)
        if controller is None or not callable(activate):
            return None
        try:
            return await activate()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return None

    async def _remove_setup_screen(self) -> bool:
        screen = self._setup_screen
        if screen is None:
            return True
        try:
            if screen.is_attached:
                await screen.remove()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return False
        self._setup_screen = None
        return True

    async def _remove_dashboard(self) -> None:
        dashboard = self._dashboard
        if dashboard is None:
            return
        dashboard.begin_teardown()
        try:
            if dashboard.is_attached:
                await dashboard.remove()
        finally:
            self._dashboard = None

    async def _close_setup_controller(self) -> None:
        controller = self.setup_controller
        if controller is None:
            return
        close = getattr(controller, "aclose", None)
        if callable(close):
            await close()
        else:
            controller.close()
        if self.setup_controller is controller:
            self.setup_controller = None

    async def _discard_setup_controller(self) -> None:
        controller = self.setup_controller
        self.setup_controller = None
        if controller is None:
            return
        try:
            close = getattr(controller, "aclose", None)
            if callable(close):
                await close()
            else:
                controller.close()
        except BaseException as error:
            pass

    def _build_dashboard(self, session: TuiRuntimeSession) -> DashboardScreen:
        return DashboardScreen(
            session.controller,
            session.supervisor,
            self.settings,
            publishing_enabled=self._publishing_enabled,
        )

    def _bind_runtime_session(self, session: TuiRuntimeSession) -> None:
        self.runtime_session = session
        self.controller = session.controller
        self.supervisor = session.supervisor
        self._dashboard = self._build_dashboard(session)

    async def _mount_dashboard(self) -> None:
        self._discard_setup_import()
        dashboard = self._dashboard
        if dashboard is None:
            raise RuntimeError("TUI runtime is unavailable")
        await self.push_screen(dashboard)
        if self._ui_closed:
            raise _TransitionClosed
        refresh_workspaces = getattr(dashboard, "refresh_workspaces", None)
        if callable(refresh_workspaces):
            await refresh_workspaces()
        if self._ui_closed:
            raise _TransitionClosed
        await dashboard.refresh_runs()
        if self._ui_closed:
            raise _TransitionClosed
        self._poll_timer = self.set_interval(
            self.poll_interval, self.refresh_runs
        )
        if self._schedule_timer is not None:
            self._schedule_timer.stop()
        self._schedule_timer = self.set_interval(10, self._dispatch_schedules)

    def _dispatch_schedules(self) -> None:
        if self._ui_closed or self._reconfiguring or self.runtime_session is None:
            return
        if self._schedule_task is not None and not self._schedule_task.done():
            return
        controller = self.runtime_session.controller
        if getattr(controller, "schedule_store", None) is None or self.runtime_session.supervisor.closed:
            return
        from ..schedules import dispatch
        self._schedule_task = self.runtime_session.supervisor.submit(
            "schedule-dispatch", "scheduled-scan", lambda: dispatch(controller))
        self._schedule_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def _replace_with_runtime(self, handle: object) -> bool:
        session: TuiRuntimeSession | None = None
        try:
            if self._ui_closed:
                raise _TransitionClosed
            session = self._build_runtime_session(handle)
            if self._ui_closed:
                raise _TransitionClosed
            self._bind_runtime_session(session)
            self._active_handle_identity = id(handle)
            if self._ui_closed:
                raise _TransitionClosed
            await self._mount_dashboard()
            return True
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                await self._discard_runtime(handle=handle, session=session)
                raise
            await self._recover_setup(handle=handle, session=session)
            return False

    async def _setup_done(self, handle: object | None) -> None:
        async with self._transition_lock:
            identity: int | None = None
            if handle is not None:
                identity = id(handle)
                if identity in self._activation_handles:
                    return
                self._activation_handles.add(identity)
            try:
                if handle is None or self._ui_closed or self.runtime_session is not None:
                    already_owned = (
                        handle is not None
                        and self.runtime_session is not None
                        and self._active_handle_identity == id(handle)
                    )
                    if handle is not None and not already_owned and (
                        self._ui_closed or self.runtime_session is not None
                    ):
                        try:
                            await asyncio.wait_for(
                                asyncio.to_thread(handle.close), timeout=5.0
                            )
                        except BaseException:
                            pass
                    return
                removed = False
                try:
                    removed = await self._remove_setup_screen()
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        await self._discard_runtime(handle=handle, session=None)
                        await self._discard_setup_controller()
                        raise
                if not removed:
                    await self._recover_setup_removal_failure(handle)
                    return
                if self._ui_closed:
                    await self._discard_runtime(handle=handle, session=None)
                    return
                await self._finish_setup(handle)
            finally:
                if identity is not None:
                    self._activation_handles.discard(identity)

    async def _finish_setup(self, handle: object) -> None:
        try:
            await self._close_setup_controller()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                await self._discard_runtime(handle=handle, session=None)
                raise
            await self._recover_setup(handle=handle)
            return
        if self._ui_closed:
            await self._discard_runtime(handle=handle, session=None)
            return
        await self._replace_with_runtime(handle)

    async def _recover_setup_removal_failure(self, handle: object) -> None:
        closed = False
        try:
            await asyncio.wait_for(asyncio.to_thread(handle.close), timeout=5.0)
            closed = bool(getattr(handle, "close_complete", True))
        except BaseException:
            closed = bool(getattr(handle, "close_complete", False))
        if not closed:
            self._pending_runtime_handle = handle
            return
        await self._discard_setup_controller()
        if self._ui_closed:
            return
        replacement = self._new_setup_controller()
        self.setup_controller = replacement
        screen = self._setup_screen
        if screen is None:
            await self._show_setup()
            return
        screen.controller = replacement
        try:
            current = self.screen is screen
        except BaseException:
            current = False
        if not current:
            await self.push_screen(screen, self._setup_done)

    async def _recover_setup(
        self,
        *,
        handle: object,
        session: TuiRuntimeSession | None = None,
    ) -> None:
        closed = await self._discard_runtime(handle=handle, session=session)
        if closed and not self._ui_closed:
            await self._discard_setup_controller()
            self.setup_controller = self._new_setup_controller()
            await self._show_setup()

    async def _discard_runtime(
        self,
        *,
        handle: object,
        session: TuiRuntimeSession | None,
    ) -> bool:
        dashboard = self._dashboard
        if dashboard is not None:
            try:
                dashboard.begin_teardown()
            except BaseException:
                pass
            if self.screen is dashboard:
                try:
                    self.pop_screen()
                except BaseException:
                    pass
            try:
                if dashboard.is_attached:
                    await dashboard.remove()
            except BaseException:
                pass
        if session is not None:
            try:
                await session.close()
            except BaseException:
                if not getattr(session, "close_complete", False):
                    self._notify_runtime_close_pending()
                    return False
        else:
            try:
                await asyncio.wait_for(asyncio.to_thread(handle.close), timeout=5.0)
            except BaseException:
                if not getattr(handle, "close_complete", False):
                    self._pending_runtime_handle = handle
                    self._notify_runtime_close_pending()
                    return False
        if session is not None and not getattr(session, "close_complete", True):
            return False
        if session is None and not getattr(handle, "close_complete", True):
            self._pending_runtime_handle = handle
            self._notify_runtime_close_pending()
            return False
        self.runtime_session = None
        self._active_handle_identity = None
        self.controller = None
        self.supervisor = None
        self._dashboard = None
        if self._pending_runtime_handle is handle:
            self._pending_runtime_handle = None
        return True

    def _notify_runtime_close_pending(self) -> None:
        try:
            self.notify("Runtime shutdown is still pending", severity="warning")
        except BaseException:
            pass

    @on(Button.Pressed, "#configure-runtime")
    async def _pressed_configure_runtime(self) -> None:
        await self._begin_reconfigure()

    @on(Button.Pressed, "#nav-runtime-setup")
    async def _pressed_runtime_setup_navigation(self) -> None:
        await self._begin_reconfigure()

    async def read_inline_ones(self) -> dict[str, str]:
        controller = self._new_setup_controller()
        try:
            await asyncio.to_thread(controller.load_active_public_draft)
            return {key: value for key, value in controller.runtime_public_fields.items()
                    if key in {"ones_base_url", "ones_team_id", "ones_issue_type_id"}}
        finally:
            await controller.aclose()

    async def save_inline_ones(self, fields, credentials) -> None:
        OnesProbePublicConfig(base_url=fields["ones_base_url"], team_id=fields["ones_team_id"], issue_type_id=fields["ones_issue_type_id"])
        if self.runtime_session is None:
            raise RuntimeError("editable runtime is unavailable")
        if any(activity is not RunActivity.IDLE for activity in self.activities.values()):
            raise RuntimeError("workflow is active")
        await self._begin_reconfigure(inline_fields=fields, inline_credentials=credentials)

    async def _begin_reconfigure(self, *, inline_fields=None, inline_credentials=None) -> None:
        """Close the stable runtime completely before constructing setup UI."""

        safe_exit = False
        async with self._transition_lock:
            if inline_fields is not None and any(activity is not RunActivity.IDLE for activity in self.activities.values()):
                raise RuntimeError("workflow is active")
            if self._ui_closed or self.runtime_session is None:
                return
            if self._schedule_timer is not None:
                self._schedule_timer.stop()
                self._schedule_timer = None
            if self._poll_timer is not None:
                self._poll_timer.stop()
                self._poll_timer = None
            dashboard = self._dashboard
            if dashboard is not None:
                dashboard.begin_teardown()
            close_failed = False
            try:
                await self.runtime_session.close()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                safe_exit = True
                close_failed = True
            close_complete = getattr(self.runtime_session, "close_complete", True)
            if not close_complete:
                # The runtime may still own resources, so retain the session for
                # the final close drain.  The dashboard itself is no longer a
                # safe surface after a close failure and must be removed before
                # the host exits; never leave a half-closed runtime visible.
                self._notify_runtime_close_pending()
                await self._remove_dashboard()
                self.activities.clear()
                return
            else:
                self.runtime_session = None
                self._active_handle_identity = None
                self.controller = None
                self.supervisor = None
                await self._remove_dashboard()
                self.activities.clear()
            if not safe_exit:
                try:
                    controller = self._new_setup_controller()
                    await asyncio.to_thread(controller.load_active_public_draft)
                    self.setup_controller = controller
                    self._reconfiguring = True
                    if inline_fields is None:
                        await self._show_setup()
                    else:
                        try:
                            await controller.prepare_inline_ones(inline_fields, inline_credentials or {})
                            handle = await controller.save_and_activate()
                            saved = True
                        except Exception:
                            handle = await controller.activate_existing()
                            saved = False
                        if handle is None:
                            await self._show_setup()
                        else:
                            await self._finish_setup(handle)
                            if self._dashboard is not None:
                                self._dashboard.action_show_settings()
                            self.notify("ONES 配置已保存并应用" if saved else "配置未应用，已恢复原配置；请检查必填项及连接后重试。", severity="information" if saved else "warning")
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    await self._discard_setup_controller()
                    safe_exit = True
        if safe_exit and not self._ui_closed:
            await self._close_ui()
            self.exit()

    async def _cancel_setup(self) -> None:
        """Cancel initial setup safely or reconstruct the prior stable runtime."""

        safe_exit = False
        async with self._transition_lock:
            if self._ui_closed:
                return
            try:
                removed = await self._remove_setup_screen()
            except BaseException as error:
                await self._discard_setup_controller()
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                removed = False
            if not removed:
                await self._recover_cancel_removal_failure()
                return
            if self._ui_closed:
                await self._discard_setup_controller()
                return
            controller = self.setup_controller
            if not self._reconfiguring or controller is None:
                await self._discard_setup_controller()
                safe_exit = True
            else:
                try:
                    handle = await controller.activate_existing()
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    handle = None
                if handle is None:
                    await self._discard_setup_controller()
                    safe_exit = True
                else:
                    try:
                        await self._close_setup_controller()
                    except BaseException as error:
                        await self._discard_runtime(handle=handle, session=None)
                        if isinstance(error, (KeyboardInterrupt, SystemExit)):
                            raise
                        safe_exit = True
                    if safe_exit:
                        self._reconfiguring = False
                        handle = None
                    if handle is None:
                        pass
                    else:
                        self._reconfiguring = False
                        if not await self._replace_with_runtime(handle):
                            safe_exit = True
        if safe_exit and not self._ui_closed:
            await self._close_ui()
            self.exit()

    async def _recover_cancel_removal_failure(self) -> None:
        """Replace a stale setup owner without reading credentials or building."""

        await self._discard_setup_controller()
        if self._ui_closed:
            return
        try:
            replacement = self._new_setup_controller()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            self._ui_closed = True
            self._accept_events = False
            self.exit()
            return
        self.setup_controller = replacement
        self._reconfiguring = True
        screen = self._setup_screen
        if screen is not None:
            screen.controller = replacement
            try:
                if self.screen is screen:
                    return
            except BaseException:
                pass
        self._setup_screen = None
        await self._show_setup()

    async def refresh_runs(self) -> None:
        """Refresh persisted runs without performing workflow mutations."""

        dashboard = self._dashboard
        if dashboard is None:
            return
        mount_generation = dashboard.mount_generation
        if (
            self._ui_closed
            or not dashboard.owns_refresh(mount_generation)
        ):
            return
        try:
            if self.screen is not dashboard:
                return
        except Exception:
            return
        await dashboard.refresh_runs(
            dict(self.activities),
            mount_generation=mount_generation,
        )
        if (
            self._ui_closed
            or self._dashboard is not dashboard
            or not dashboard.owns_refresh(mount_generation)
        ):
            return
        try:
            if self.screen is not dashboard:
                return
        except Exception:
            return

    async def on_tui_task_message(self, message: TuiTaskMessage) -> None:
        """Apply a validated worker event on Textual's UI loop."""

        if self._ui_closed:
            return
        self.activities[message.event.run_id] = message.event.activity
        await self.refresh_runs()

    async def action_quit(self) -> None:
        await self._close_ui()
        self.exit()

    def action_help(self) -> None:
        if self._dashboard is not None and self.screen is self._dashboard:
            self.push_screen(HelpScreen())

    def action_search(self) -> None:
        if self._dashboard is not None and self.screen is self._dashboard:
            self._dashboard.action_search()

    def action_filter(self) -> None:
        if self._dashboard is not None and self.screen is self._dashboard:
            self._dashboard.action_filter()

    async def on_unmount(self) -> None:
        await self._close_ui()

    async def _close_ui(self) -> None:
        try:
            await self._close_ui_impl()
        finally:
            self._discard_setup_import()

    async def _close_ui_impl(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._close_timeout
        while True:
            task = self._close_task
            if task is not None and task.cancelled():
                if self._close_task is task:
                    self._close_task = None
                continue
            if task is None:
                self._close_started = True
                self._ui_closed = True
                self._accept_events = False
                if self._schedule_timer is not None:
                    self._schedule_timer.stop()
                    self._schedule_timer = None
                if self._poll_timer is not None:
                    self._poll_timer.stop()
                    self._poll_timer = None
                task = asyncio.create_task(self._drain_close_ui())
                self._close_task = task
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TuiAppCloseError("TUI close timed out") from None
            timed_out = False
            try:
                outcome = await asyncio.wait_for(
                    asyncio.shield(task), timeout=remaining
                )
            except asyncio.TimeoutError:
                timed_out = True
            if timed_out:
                raise TuiAppCloseError("TUI close timed out") from None
            if outcome.complete:
                if outcome.fatal is not None:
                    raise outcome.fatal
                return
            if self._close_task is task:
                self._close_task = None

    @staticmethod
    async def _await_owned(awaitable) -> object:
        async def capture() -> tuple[object | None, BaseException | None]:
            try:
                return await awaitable, None
            except BaseException as error:
                return None, error

        task = asyncio.create_task(capture())
        while True:
            try:
                result, error = await asyncio.shield(task)
                if error is not None:
                    if isinstance(error, asyncio.CancelledError):
                        raise _OwnedCloseCancelled from None
                    raise error
                return result
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if task.done():
                    result, error = task.result()
                    if error is not None:
                        if isinstance(error, asyncio.CancelledError):
                            raise _OwnedCloseCancelled from None
                        raise error
                    return result

    async def _drain_close_ui(self) -> _AppCloseOutcome:
        acquired = False
        complete = False
        fatal: BaseException | None = None
        try:
            acquired = await self._acquire_transition_for_close(
                self._close_timeout
            )
            if not acquired:
                return _AppCloseOutcome(complete=False)
            if self._dashboard is not None:
                try:
                    self._dashboard.begin_teardown()
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        fatal = error
            if self.runtime_session is not None:
                runtime_incomplete = False
                try:
                    if (
                        self.supervisor is not None
                        and self.supervisor is not self.runtime_session.supervisor
                    ):
                        await self._await_owned(self.supervisor.close())
                    await self._await_owned(self.runtime_session.close())
                except _OwnedCloseCancelled:
                    runtime_incomplete = True
                except BaseException as error:
                    if fatal is None and isinstance(
                        error, (KeyboardInterrupt, SystemExit)
                    ):
                        fatal = error
                    if not getattr(self.runtime_session, "close_complete", True):
                        runtime_incomplete = True
                if runtime_incomplete:
                    return _AppCloseOutcome(fatal=fatal, complete=False)
                self.runtime_session = None
                self._active_handle_identity = None
            else:
                pending = self._pending_runtime_handle
                if pending is not None:
                    pending_incomplete = False
                    try:
                        await self._await_owned(asyncio.to_thread(pending.close))
                    except _OwnedCloseCancelled:
                        pending_incomplete = True
                    except BaseException as error:
                        if isinstance(error, (KeyboardInterrupt, SystemExit)):
                            fatal = error
                        if not getattr(pending, "close_complete", True):
                            pending_incomplete = True
                    if not getattr(pending, "close_complete", not pending_incomplete):
                        pending_incomplete = True
                    if pending_incomplete:
                        return _AppCloseOutcome(fatal=fatal, complete=False)
                    self._pending_runtime_handle = None
                controller = (
                    self.setup_controller or self._closing_setup_controller
                )
                self.setup_controller = None
                if controller is not None:
                    self._closing_setup_controller = controller
                    setup_incomplete = False
                    try:
                        close = getattr(controller, "aclose", None)
                        if callable(close):
                            await self._await_owned(close())
                        else:
                            controller.close()
                    except _OwnedCloseCancelled:
                        setup_incomplete = True
                    except BaseException as error:
                        if isinstance(error, (KeyboardInterrupt, SystemExit)):
                            fatal = error
                    finally:
                        if not setup_incomplete:
                            self._closing_setup_controller = None
                    if setup_incomplete:
                        return _AppCloseOutcome(fatal=fatal, complete=False)
            self.activities.clear()
            complete = True
            return _AppCloseOutcome(fatal=fatal)
        finally:
            if acquired:
                self._transition_lock.release()
            if acquired and complete:
                self._close_complete.set()

    async def _acquire_transition_for_close(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(
                    self._transition_lock.acquire(), timeout=remaining
                )
                return True
            except asyncio.TimeoutError:
                return False
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()


__all__ = ["DeveloperWorkflowTuiApp", "TuiAppCloseError", "TuiTaskMessage"]
