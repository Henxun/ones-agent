from __future__ import annotations

from pathlib import Path

import pytest

import src.developer_workflow.credential_store as credential_store
from src.developer_workflow.credential_store import (
    CredentialStoreError,
    create_credential_store,
)
from src.developer_workflow.platform_support import (
    HostPaths,
    HostPlatformError,
    default_host_paths,
)


def test_macos_paths_follow_apple_user_library_conventions(tmp_path: Path) -> None:
    paths = default_host_paths(platform_name="darwin", home=tmp_path)

    assert paths.config_path == (
        tmp_path / "Library" / "Application Support" / "ones-dev" / "config.json"
    )
    assert paths.cache_root == (
        tmp_path / "Library" / "Caches" / "ones-dev" / "codex-runtime"
    )
    assert paths.credential_lock_root == (
        tmp_path
        / "Library"
        / "Application Support"
        / "ones-dev"
        / "credential-locks"
    )


def test_windows_paths_preserve_local_app_data_contract(tmp_path: Path) -> None:
    paths = default_host_paths(
        platform_name="win32", environ={"LOCALAPPDATA": str(tmp_path)}
    )

    assert paths == HostPaths(
        config_path=tmp_path / "ones-dev" / "config.json",
        cache_root=tmp_path / "ones-dev" / "codex-runtime",
        credential_lock_root=tmp_path / "ones-dev" / "credential-locks",
    )


@pytest.mark.parametrize(
    ("platform_name", "environ", "home"),
    [
        ("linux", {}, Path("/tmp")),
        ("win32", {}, Path("/tmp")),
        ("win32", {"LOCALAPPDATA": "relative"}, Path("/tmp")),
        ("darwin", {}, Path("relative")),
    ],
)
def test_paths_fail_closed_without_a_canonical_supported_root(
    platform_name: str, environ: dict[str, str], home: Path
) -> None:
    with pytest.raises(HostPlatformError):
        default_host_paths(
            platform_name=platform_name, environ=environ, home=home
        )


def test_credential_store_factory_uses_macos_lock_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[Path] = []

    class FakeMacOSCredentialStore:
        def __init__(self, *, lock_root: Path) -> None:
            captured.append(lock_root)

    import src.developer_workflow.macos_credentials as macos_store

    monkeypatch.setattr(macos_store, "MacOSCredentialStore", FakeMacOSCredentialStore)
    paths = default_host_paths(platform_name="darwin", home=tmp_path)

    store = create_credential_store(platform_name="darwin", paths=paths)

    assert isinstance(store, FakeMacOSCredentialStore)
    assert captured == [paths.credential_lock_root]


def test_credential_store_factory_sanitizes_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingWindowsStore:
        def __init__(self) -> None:
            raise RuntimeError("TOKEN-SECRET backend detail")

    monkeypatch.setattr(
        credential_store, "WindowsCredentialStore", FailingWindowsStore
    )
    with pytest.raises(CredentialStoreError) as captured:
        create_credential_store(platform_name="win32")
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "TOKEN-SECRET" not in repr(captured.value)
