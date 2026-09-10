import os
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_REUSED_BASETEMP_NAMES = frozenset(
    {".pytest-real", ".pytest-tmp", "final-review-pytest-temp", "pytest-temp"}
)

sys.path.insert(0, str(_PROJECT_ROOT))


def _is_unsafe_legacy_basetemp(
    value: str | os.PathLike[str], *, platform_name: str | None = None
) -> bool:
    """Return whether an explicit basetemp is a known reused checkout path.

    Keep this check lexical: an old Windows basetemp may itself be inaccessible
    after an ACL security test ran under another sandbox identity. New unique
    checkout-local paths remain supported for compatibility, though the system
    temporary directory is preferred.
    """

    if (platform_name or os.name) != "nt":
        return False

    candidate = os.path.normcase(os.path.abspath(os.fspath(value)))
    project_root = os.path.normcase(os.path.abspath(os.fspath(_PROJECT_ROOT)))
    try:
        is_workspace_local = os.path.commonpath((candidate, project_root)) == project_root
    except ValueError:
        # Paths on different Windows drives cannot share a common path.
        return False
    if not is_workspace_local:
        return False
    relative_parts = Path(os.path.relpath(candidate, project_root)).parts
    return any(
        part.casefold() in _LEGACY_REUSED_BASETEMP_NAMES for part in relative_parts
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Reject reusable checkout-local basetemps before pytest touches them."""

    basetemp = config.getoption("basetemp")
    if basetemp and _is_unsafe_legacy_basetemp(basetemp):
        raise pytest.UsageError(
            "this reused workspace-local --basetemp is unsupported: Windows ACL security "
            "tests can leave descendants inaccessible to a later test process. "
            "Omit --basetemp (recommended), or pass a new unique directory under "
            "the system temporary directory for each run."
        )
