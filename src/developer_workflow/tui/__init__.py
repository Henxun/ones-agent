"""Stable display-model API for the developer workflow terminal UI."""

from .app import DeveloperWorkflowTuiApp
from .controller import (
    CandidateSessionView,
    StaleTuiActionError,
    TuiController,
    TuiControllerError,
)

from .models import (
    DangerousActionRequest,
    DefectChoice,
    HistoryView,
    PublicationView,
    RequirementChoice,
    RepositoryView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TestView,
    TuiDisplayError,
    WorkspaceRepositoryInput,
    WorkspaceSummary,
    run_detail_from_run,
    safe_tui_text,
)
from .run_index import RunIndex
from .runtime_session import TuiRuntimeCloseError, TuiRuntimeSession
from .setup_models import SetupStepView, build_setup_step_view
from .setup_screens import SetupRecoveryScreen, SetupRootScreen, SetupWizardScreen
from .supervisor import (
    RunTaskSupervisor,
    SupervisorClosedError,
    SupervisorLoopError,
    TaskEvent,
)


def run_tui(setup_controller: object, runtime_bootstrapper: object) -> None:
    """Run the two-stage terminal host without pre-constructing workflow services."""

    if not callable(setup_controller):
        raise TypeError("TUI bootstrap host is invalid")
    DeveloperWorkflowTuiApp(
        setup_controller_factory=setup_controller,
        runtime_bootstrapper=runtime_bootstrapper,
        setup_import=getattr(setup_controller, "import_context", None),
        # The current MVP runs Codex with danger-full-access on every host and
        # has no OS sandbox profile.  Keep the settings projection aligned
        # with the runtime assembly instead of inferring a capability by OS.
        sandbox_configured=False,
        publishing_enabled=False,
    ).run()

__all__ = [
    "CandidateSessionView",
    "DangerousActionRequest",
    "DeveloperWorkflowTuiApp",
    "DefectChoice",
    "HistoryView",
    "PublicationView",
    "RequirementChoice",
    "RepositoryView",
    "RunActivity",
    "RunDetail",
    "RunFilter",
    "RunIndex",
    "RunSummary",
    "RunTaskSupervisor",
    "SetupRootScreen",
    "SetupRecoveryScreen",
    "SetupStepView",
    "SetupWizardScreen",
    "SupervisorClosedError",
    "SupervisorLoopError",
    "TestView",
    "StaleTuiActionError",
    "TuiController",
    "TuiControllerError",
    "TuiDisplayError",
    "WorkspaceRepositoryInput",
    "WorkspaceSummary",
    "TaskEvent",
    "TuiRuntimeCloseError",
    "TuiRuntimeSession",
    "run_detail_from_run",
    "run_tui",
    "safe_tui_text",
    "build_setup_step_view",
]
