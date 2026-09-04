from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path

import pytest

import src.developer_workflow.codex_runner as runner
import src.developer_workflow.codex_runtime as runtime
import src.developer_workflow.macos_runtime as macos_runtime


pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="macOS runtime trust checks require Darwin",
)


def _macho(architecture: str, payload: bytes = b"payload") -> bytes:
    cpu = 0x0100000C if architecture == "arm64" else 0x01000007
    return (0xFEEDFACF).to_bytes(4, "little") + cpu.to_bytes(4, "little") + payload


def _npm_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path = tmp_path.resolve(strict=True)
    architecture = macos_runtime._macos_machine_architecture()
    package = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    entrypoint = package / "bin" / "codex.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    locator = tmp_path / "bin" / "codex"
    locator.parent.mkdir()
    locator.symlink_to(entrypoint)
    native = package / macos_runtime._MACOS_NATIVE_RELATIVE_PATHS[architecture]
    native.parent.mkdir(parents=True)
    native.write_bytes(_macho(architecture))
    native.chmod(0o700)
    companion = native.with_name(macos_runtime.MACOS_CODE_MODE_HOST_NAME)
    companion.write_bytes(_macho(architecture, b"companion"))
    companion.chmod(0o700)
    return locator, native, companion


def test_macos_discovery_binds_fixed_npm_native_payload(tmp_path: Path) -> None:
    locator, native, _ = _npm_layout(tmp_path)
    adapter = macos_runtime.MacOSRuntimeAdapter(_verify_signature=lambda path: None)

    locked = runtime.discover_locked_native_codex(
        which=lambda name: str(locator) if name == "codex" else None,
        repository_roots=(),
        _adapter=adapter,
    )
    try:
        assert adapter.final_path(locked.descriptor) == native.resolve(strict=True)
        assert locked.publisher == runtime.OPENAI_MACOS_CODESIGN_PUBLISHER
        assert locked.current_identity() == locked.identity
    finally:
        locked.close()


def test_macos_discovery_accepts_current_user_homebrew_style_ancestor(
    tmp_path: Path,
) -> None:
    locator, _, _ = _npm_layout(tmp_path)
    (tmp_path.resolve(strict=True) / "lib").chmod(0o770)
    adapter = macos_runtime.MacOSRuntimeAdapter(_verify_signature=lambda path: None)

    locked = runtime.discover_locked_native_codex(
        which=lambda name: str(locator) if name == "codex" else None,
        repository_roots=(),
        _adapter=adapter,
    )
    locked.close()


def test_macos_adapter_rejects_other_writable_ancestor(tmp_path: Path) -> None:
    _, native, _ = _npm_layout(tmp_path)
    (tmp_path.resolve(strict=True) / "lib").chmod(0o777)
    adapter = macos_runtime.MacOSRuntimeAdapter(_verify_signature=lambda path: None)

    with pytest.raises(OSError, match="native runtime path is unsafe"):
        adapter.open_locked(native)


def test_macos_preparer_stages_target_and_manifest_without_exe_suffix(
    tmp_path: Path,
) -> None:
    locator, native, companion = _npm_layout(tmp_path)
    source_adapter = macos_runtime.MacOSRuntimeAdapter(
        _verify_signature=lambda path: None,
    )
    cache_adapter = macos_runtime.MacOSCacheRuntimeAdapter(
        _runtime_adapter=source_adapter,
    )
    cache_adapter.smoke = (  # type: ignore[method-assign]
        lambda executable, *, environment, timeout: None
    )
    cache_root = tmp_path.resolve(strict=True) / "private" / "codex-runtime"
    preparer = runtime.CodexRuntimePreparer(
        cache_root=cache_root,
        discover=lambda: runtime.discover_locked_native_codex(
            which=lambda name: str(locator) if name == "codex" else None,
            repository_roots=(),
            _adapter=source_adapter,
        ),
        _cache_adapter=cache_adapter,
    )

    executable = preparer.prepare()

    digest = hashlib.sha256(native.read_bytes()).hexdigest()
    assert executable == (cache_root / digest / "codex").resolve(strict=True)
    assert executable.read_bytes() == native.read_bytes()
    assert (
        executable.with_name(macos_runtime.MACOS_CODE_MODE_HOST_NAME).read_bytes()
        == companion.read_bytes()
    )
    manifest = json.loads(executable.with_name("manifest.json").read_text())
    assert manifest["target"] == "codex"
    assert manifest["publisher"] == runtime.OPENAI_MACOS_CODESIGN_PUBLISHER
    assert os.stat(executable).st_mode & 0o077 == 0


def test_macos_production_trust_rejects_invalid_signature(tmp_path: Path) -> None:
    candidate = tmp_path / "codex"
    candidate.write_bytes(_macho(macos_runtime._macos_machine_architecture()))
    candidate.chmod(0o700)

    with pytest.raises(OSError, match="signature is not trusted"):
        macos_runtime._verify_macos_signature(candidate)


def test_macos_adapter_rejects_wrong_native_architecture(tmp_path: Path) -> None:
    architecture = macos_runtime._macos_machine_architecture()
    wrong = "x86_64" if architecture == "arm64" else "arm64"
    candidate = tmp_path / "codex"
    candidate.write_bytes(_macho(wrong))
    candidate.chmod(0o700)
    adapter = macos_runtime.MacOSRuntimeAdapter(_verify_signature=lambda path: None)
    descriptor = adapter.open_locked(candidate)
    try:
        with pytest.raises(OSError, match="architecture is invalid"):
            adapter.verify_publisher(descriptor, candidate)
    finally:
        adapter.close(descriptor)


def test_codex_command_name_gate_accepts_only_native_macos_binary() -> None:
    assert runner._is_native_codex_name("codex")
    assert not runner._is_native_codex_name("codex.js")
    assert not runner._is_native_codex_name("codex.sh")


def test_macos_execution_verifier_accepts_attested_explicit_cache_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, _, _ = _npm_layout(tmp_path)
    monkeypatch.setattr(macos_runtime, "_verify_macos_signature", lambda path: None)
    source_adapter = macos_runtime.MacOSRuntimeAdapter()
    cache_adapter = macos_runtime.MacOSCacheRuntimeAdapter(
        _runtime_adapter=source_adapter,
    )
    cache_adapter.smoke = (  # type: ignore[method-assign]
        lambda executable, *, environment, timeout: None
    )
    cache_root = tmp_path.resolve(strict=True) / "custom-cache" / "codex-runtime"
    prepared = runtime.CodexRuntimePreparer(
        cache_root=cache_root,
        discover=lambda: runtime.discover_locked_native_codex(
            which=lambda name: str(locator) if name == "codex" else None,
            repository_roots=(),
            _adapter=source_adapter,
        ),
        _cache_adapter=cache_adapter,
    ).prepare_verified()
    try:
        runtime.verify_locked_private_codex_for_execution(
            prepared._lease,
            cache_root=prepared._cache_root,
        )
        with pytest.raises(OSError, match="OS verification"):
            runtime.verify_locked_private_codex_for_execution(
                prepared._lease,
                cache_root=tmp_path.resolve(strict=True),
            )
    finally:
        prepared._lease.close()
