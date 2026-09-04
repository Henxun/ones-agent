from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from src.developer_workflow import credential_store, platform_support
from src.developer_workflow import macos_credentials, macos_runtime


def test_macos_path_uses_os_account_not_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_support, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(platform_support.os, "geteuid", lambda: 501, raising=False)
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(
        getpwuid=lambda uid: SimpleNamespace(pw_dir=str(tmp_path / "real-user"))))
    monkeypatch.setenv("HOME", str(tmp_path / "untrusted"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows"))
    assert platform_support.user_data_directory() == (
        tmp_path / "real-user/Library/Application Support/ones-dev")


def test_macos_vault_factory_never_uses_windows_backend(monkeypatch):
    expected = object()
    monkeypatch.setattr(credential_store, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_credentials, "MacOSCredentialStore", lambda: expected)
    assert credential_store.create_credential_store() is expected


@pytest.mark.parametrize("machine,package,triple", [
    ("arm64", "arm64", "aarch64"), ("x86_64", "x64", "x86_64"),
])
def test_npm_native_discovery_layout(tmp_path, machine, package, triple):
    launcher = tmp_path / "node_modules/@openai/codex/bin/codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("must never execute this JavaScript", encoding="utf-8")
    candidates = macos_runtime._candidate_paths(launcher, machine)
    assert launcher.parent.parent / (
        f"node_modules/@openai/codex-darwin-{package}/vendor/{triple}-apple-darwin/bin/codex"
    ) in candidates


def test_unsupported_architecture_fails_before_execution(tmp_path):
    with pytest.raises(OSError, match="unsupported macOS architecture"):
        macos_runtime._candidate_paths(tmp_path / "codex", "unknown")


@pytest.mark.parametrize("inside_repository", [False, True])
def test_native_discovery_rejects_repository_payload_and_closes_handle(monkeypatch, tmp_path, inside_repository):
    path = tmp_path / "codex"
    path.write_bytes(b"fixture")
    closed = []
    identity = macos_runtime.NativeCodexIdentity(1, 2, 7, 3)
    adapter = SimpleNamespace(
        open_locked=lambda _: 42, final_path=lambda _: path,
        identity=lambda _: identity,
        verify_publisher=lambda *_: macos_runtime.OPENAI_MACOS_CODESIGN_PUBLISHER,
        close=closed.append,
    )
    monkeypatch.setattr(macos_runtime, "MacOSRuntimeAdapter", lambda: adapter)
    monkeypatch.setattr(macos_runtime.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(macos_runtime, "_inside_repository_by_identity", lambda *_: inside_repository)
    if inside_repository:
        with pytest.raises(OSError, match="native Codex payload is unavailable"):
            macos_runtime.discover_native_codex(which=lambda _: str(path), repository_roots=(tmp_path,))
    else:
        lease = macos_runtime.discover_native_codex(which=lambda _: str(path), repository_roots=())
        assert lease.identity == identity
        lease.close()
    assert closed == [42]


@pytest.mark.parametrize("returncode", [0, 1])
def test_signature_requires_apple_trust_and_openai_organization(monkeypatch, tmp_path, returncode):
    path = tmp_path / "codex"
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = (
            b"Identifier=codex\n"
            b"TeamIdentifier=2DC432GLL2\n"
            b"CodeDirectory flags=0x10000(runtime)\n"
        )
        return SimpleNamespace(returncode=returncode, stdout=output)

    monkeypatch.setattr(macos_runtime.subprocess, "run", run)
    if returncode:
        with pytest.raises(OSError, match="signature is not trusted"):
            macos_runtime._verify_macos_signature(path)
    else:
        macos_runtime._verify_macos_signature(path)
    argv, options = calls[0]
    assert argv[0] == "/usr/bin/codesign"
    assert "anchor apple generic" in argv[argv.index("--test-requirement") + 1]
    assert macos_runtime.OPENAI_MACOS_TEAM_IDENTIFIER in argv[
        argv.index("--test-requirement") + 1
    ]
    assert options.get("shell", False) is False and options["timeout"] == 5.0
    assert "DYLD_LIBRARY_PATH" not in options["env"]


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS filesystem test")
def test_native_cache_permissions_and_config_persistence(tmp_path):
    # Resolve pytest's /var -> /private/var alias before applying no-link rules.
    root = tmp_path.resolve() / "private-cache"
    adapter = macos_runtime.MacOSCacheRuntimeAdapter()
    adapter.prepare_private_directory(root)
    adapter.validate_private_directory(root)
    native = root / "codex-stage.tmp"
    native.write_bytes(b"\xcf\xfa\xed\xfe" + b"payload")
    adapter.protect_private_file(native)
    assert native.stat().st_mode & 0o777 == 0o700
    descriptor = adapter._runtime.open_locked(native)
    try:
        assert adapter._runtime.final_path(descriptor) == native
    finally:
        adapter._runtime.close(descriptor)
    native.chmod(0o777)
    with pytest.raises(OSError):
        adapter._runtime.open_locked(native)


def test_macos_git_uses_native_vault_and_preserves_explicit_credentials(monkeypatch):
    import os
    from src.developer_workflow import repository

    monkeypatch.setattr(repository, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(repository, "os", SimpleNamespace(
        name="posix", environ=dict(os.environ), devnull="/dev/null"))
    monkeypatch.setattr(repository, "_macos_ssh_command", lambda: "trusted-ssh-command")
    env = repository._isolated_git_environment()
    assert env["GIT_CONFIG_VALUE_3"] == "osxkeychain"
    assert env["GIT_SSH_COMMAND"] == "trusted-ssh-command"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    env = repository._isolated_git_environment({"GIT_ASKPASS": "/approved/askpass",
                                               "GIT_SSH_COMMAND": "/approved/ssh"})
    assert "GIT_CONFIG_VALUE_3" not in env
    assert env["GIT_SSH_COMMAND"] == "/approved/ssh"


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS filesystem test")
def test_macos_ssh_keeps_strict_trust_and_never_reads_keys(monkeypatch, tmp_path):
    import pwd
    import shlex
    from src.developer_workflow import repository

    root = tmp_path.resolve()
    directory = root / ".ssh"
    directory.mkdir(mode=0o700)
    (directory / "known_hosts").write_text("fixture", encoding="utf-8")
    key = directory / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setattr(pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(root)))
    args = shlex.split(repository._macos_ssh_command())
    assert "-oStrictHostKeyChecking=yes" in args
    assert args[args.index("-F") + 1] == "/dev/null"
    assert args[args.index("-i") + 1] == str(key)
    assert "-oIdentityAgent=none" in args


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS filesystem test")
def test_macos_ssh_rejects_other_writable_ssh_directory(monkeypatch, tmp_path):
    import pwd
    import shlex
    from src.developer_workflow import repository

    root = tmp_path.resolve()
    directory = root / ".ssh"
    directory.mkdir()
    directory.chmod(0o777)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text("fixture", encoding="utf-8")
    key = directory / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setattr(pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(root)))

    args = shlex.split(repository._macos_ssh_command())

    assert "-oUserKnownHostsFile=/dev/null" in args
    assert "-oIdentityFile=none" in args
    assert str(known_hosts) not in args
    assert str(key) not in args


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS persistence test")
def test_macos_setup_store_uses_posix_lock_and_persistence(tmp_path):
    from src.developer_workflow.setup_store import SetupStore

    # Empty-state persistence does not access secrets; native Keychain behavior is
    # covered independently by the ctypes backend tests.
    credentials = SimpleNamespace()
    store = SetupStore(credentials, config_path=tmp_path.resolve() / "config/config.json")
    empty = store.load_or_empty(profile_id="default")
    assert empty is not None
    assert store.config_path.parent.stat().st_mode & 0o777 == 0o700
