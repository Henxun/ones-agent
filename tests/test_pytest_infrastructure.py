from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest


class _Config:
    def __init__(self, basetemp: str | None) -> None:
        self.option = SimpleNamespace(basetemp=basetemp)

    def getoption(self, name: str) -> str | None:
        assert name == "basetemp"
        return self.option.basetemp


def test_known_reused_workspace_basetemp_is_rejected_before_path_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_on_path_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("the stale basetemp must not be accessed")

    monkeypatch.setattr(Path, "exists", fail_on_path_access)
    monkeypatch.setattr(Path, "resolve", fail_on_path_access)
    monkeypatch.setattr(conftest, "_is_unsafe_legacy_basetemp", lambda value: True)

    with pytest.raises(pytest.UsageError, match="reused workspace-local --basetemp"):
        conftest.pytest_configure(_Config("final-review-pytest-temp"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "legacy_path",
    (
        ".pytest-tmp",
        ".pytest-real",
        "pytest-temp",
        "pytest-temp/run-unique",
        "final-review-pytest-temp",
    ),
)
def test_known_reused_windows_basetemp_names_are_detected(legacy_path: str) -> None:
    assert conftest._is_unsafe_legacy_basetemp(legacy_path, platform_name="nt")


def test_external_unique_basetemp_remains_supported() -> None:
    drive = Path.cwd().drive
    if drive:
        external = Path(f"{drive}\\pytest-runs") / "run-unique"
    else:
        external = Path(os.sep) / "tmp" / "pytest-runs" / "run-unique"

    assert not conftest._is_unsafe_legacy_basetemp(external, platform_name="nt")
    conftest.pytest_configure(_Config(str(external)))  # type: ignore[arg-type]


def test_unique_workspace_basetemp_remains_supported() -> None:
    unique = Path(".pytest-tmp-run-5e741dee9eab4b2fba061211fba97599")

    assert not conftest._is_unsafe_legacy_basetemp(unique, platform_name="nt")
    conftest.pytest_configure(_Config(str(unique)))  # type: ignore[arg-type]


def test_default_pytest_temp_management_remains_supported(tmp_path: Path) -> None:
    assert tmp_path.is_dir()
    conftest.pytest_configure(_Config(None))  # type: ignore[arg-type]


def test_release_readiness_uses_a_unique_system_basetemp() -> None:
    release_guide = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "ones_dev_tui_release_readiness.md"
    ).read_text(encoding="utf-8")

    assert "[System.IO.Path]::GetTempPath()" in release_guide
    assert "[guid]::NewGuid().ToString(\"N\")" in release_guide
    assert '--basetemp="$pytestBase"' in release_guide
    assert '$PWD\\.pytest-tmp' not in release_guide
