"""Discovery, trust validation, and locking for native Codex npm payloads."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


OPENAI_AUTHENTICODE_PUBLISHER = "OpenAI OpCo, LLC"
OPENAI_MACOS_TEAM_IDENTIFIER = "2DC432GLL2"
OPENAI_MACOS_CODESIGN_PUBLISHER = (
    f"OpenAI macOS TeamIdentifier={OPENAI_MACOS_TEAM_IDENTIFIER}"
)
NATIVE_CODEX_RELATIVE_PATH = Path(
    "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
    "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
)
CODEX_EXECUTABLE_NAME = "codex" if sys.platform == "darwin" else "codex.exe"
CODE_MODE_HOST_NAME = (
    "codex-code-mode-host" if sys.platform == "darwin"
    else "codex-code-mode-host.exe"
)


def _is_supported_runtime_target(name: str) -> bool:
    return name == "codex" or name.casefold() == "codex.exe"


def _expected_publisher_for_target(name: str) -> str:
    if name == "codex":
        return OPENAI_MACOS_CODESIGN_PUBLISHER
    if name.casefold() == "codex.exe":
        return OPENAI_AUTHENTICODE_PUBLISHER
    raise OSError("private Codex runtime target is invalid")


def _native_adapter_types() -> tuple[type, type]:
    """Resolve the exact native adapter pair without duplicating platform code."""

    if sys.platform == "darwin" and os.name != "nt":
        from .macos_runtime import MacOSCacheRuntimeAdapter, MacOSRuntimeAdapter

        return MacOSRuntimeAdapter, MacOSCacheRuntimeAdapter
    return _WindowsRuntimeAdapter, _WindowsCacheRuntimeAdapter


def _default_cache_adapter() -> _CacheRuntimeAdapter:
    return _native_adapter_types()[1]()


@dataclass(frozen=True, slots=True)
class NativeCodexIdentity:
    volume_serial: int
    file_index: int
    size: int
    mtime_ns: int


class _RuntimeAdapter(Protocol):
    def open_locked(self, path: Path) -> int: ...

    def is_disk_regular_non_reparse(self, descriptor: int) -> bool: ...

    def identity(self, descriptor: int) -> NativeCodexIdentity: ...

    def final_path(self, descriptor: int) -> Path: ...

    def verify_publisher(self, descriptor: int, path: Path) -> str: ...

    def read(self, descriptor: int, size: int) -> bytes: ...

    def rewind(self, descriptor: int) -> None: ...

    def close(self, descriptor: int) -> None: ...

    def same_file(self, left: Path, right: Path) -> bool: ...

    def repository_marker(
        self, root: Path,
    ) -> tuple[str, tuple[int, int, int, int]] | None: ...

    def resolve_repository_path(self, path: Path) -> Path: ...

    def repository_path_identity(self, path: Path) -> tuple[int, int, int, int]: ...


class _CacheRuntimeAdapter(Protocol):
    def validate_cache_ancestor_chain(self, root: Path) -> None: ...

    def prepare_private_directory(self, path: Path) -> Path: ...

    def validate_private_directory(self, path: Path) -> None: ...

    def protect_private_file(self, path: Path) -> None: ...

    def inspect_private_executable(
        self, path: Path,
    ) -> tuple[NativeCodexIdentity, str]: ...

    def validate_private_file(self, path: Path) -> tuple[int, int]: ...

    def read_private_text(self, path: Path) -> str: ...

    def fsync_directory(self, path: Path) -> None: ...

    def smoke(
        self, executable: Path, *, environment: dict[str, str], timeout: float,
    ) -> None: ...


@dataclass(slots=True, repr=False)
class LockedNativeCodex:
    descriptor: int = field(repr=False)
    identity: NativeCodexIdentity
    size: int
    publisher: str
    _adapter: _RuntimeAdapter = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    def read_chunk(self, size: int) -> bytes:
        if self._closed:
            raise ValueError("native Codex payload is closed")
        if type(size) is not int or size <= 0:
            raise ValueError("read size must be a positive integer")
        return self._adapter.read(self.descriptor, size)

    def rewind(self) -> None:
        if self._closed:
            raise ValueError("native Codex payload is closed")
        self._adapter.rewind(self.descriptor)

    def current_identity(self) -> NativeCodexIdentity:
        if self._closed:
            raise ValueError("native Codex payload is closed")
        return self._adapter.identity(self.descriptor)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._adapter.close(self.descriptor)


@dataclass(slots=True, repr=False)
class _StagingLease:
    path: Path
    created_ns: int
    file_identity: tuple[int, int, int, int]
    _stream: object = field(repr=False)
    _rename_safe: bool = field(default=False, repr=False)
    _closed: bool = field(default=False, repr=False)

    @classmethod
    def acquire(
        cls,
        root: Path,
        token: str,
        adapter: _CacheRuntimeAdapter,
        *,
        create: bool = True,
        created_ns: int | None = None,
        _before_lock: Callable[[], None] | None = None,
    ) -> _StagingLease:
        if not _is_stage_token(token):
            raise ValueError("private runtime staging token is invalid")
        path = root / f".lease-{token}"
        rename_safe = os.name == "nt"
        stream = _open_staging_lease(
            path, create=create, native_windows=rename_safe,
        )
        if create:
            if created_ns is None:
                created_ns = time.time_ns()
            if type(created_ns) is not int or created_ns < 0:
                stream.close()
                raise ValueError("private runtime staging time is invalid")
            record = _staging_lease_record(token, created_ns)
        else:
            if created_ns is not None:
                stream.close()
                raise ValueError("private runtime staging time is invalid")
            record = b""
        locked = False
        try:
            if create:
                _write_all(stream, record)
                stream.flush()
                os.fsync(stream.fileno())
                adapter.protect_private_file(path)
            adapter.validate_private_file(path)
            initial_identity = _staging_file_identity(path)
            if _descriptor_file_identity(stream) != initial_identity:
                raise OSError("private runtime staging lease changed")
            if _before_lock is not None:
                _before_lock()
            stream.seek(0)
            _lock_staging_descriptor(stream.fileno())
            locked = True
            stream.seek(0)
            raw = stream.read(512)
            parsed_created_ns = _parse_staging_lease_record(raw, token)
            if create and raw != record:
                raise OSError("private runtime staging lease is invalid")
            locked_identity = _staging_file_identity(path)
            if (
                locked_identity != initial_identity
                or _descriptor_file_identity(stream) != initial_identity
                or adapter.validate_private_file(path)
                != initial_identity[:2]
            ):
                raise OSError("private runtime staging lease changed")
            stream.seek(0)
            return cls(
                path=path,
                created_ns=parsed_created_ns,
                file_identity=initial_identity,
                _stream=stream,
                _rename_safe=rename_safe,
            )
        except BaseException:
            try:
                if locked:
                    stream.seek(0)
                    _unlock_staging_descriptor(stream.fileno())
            finally:
                stream.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.seek(0)  # type: ignore[attr-defined]
            _unlock_staging_descriptor(self._stream.fileno())  # type: ignore[attr-defined]
        finally:
            self._stream.close()  # type: ignore[attr-defined]


_RUNTIME_ATTESTATION_NONCE = object()
_RUNTIME_ATTESTATION_SECRET = secrets.token_bytes(32)
_PRIVATE_LEASE_SECRET = secrets.token_bytes(32)


def _private_lease_mac(lease: LockedPrivateCodex) -> bytes:
    snapshot = json.dumps(
        [
            "locked-private-codex-v1",
            str(lease.path),
            str(lease._cache_root),
            lease.sha256,
            *_identity_manifest(lease.identity).values(),
            *_identity_manifest(lease.source_identity).values(),
            id(lease._descriptor),
            id(lease._runtime_adapter),
            id(lease._cache_adapter),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    return hmac.digest(_PRIVATE_LEASE_SECRET, snapshot, "sha256")


@dataclass(slots=True, repr=False)
class LockedPrivateCodex:
    """A verified cache executable whose underlying file remains locked."""

    path: Path
    identity: NativeCodexIdentity
    sha256: str
    source_identity: NativeCodexIdentity
    _cache_root: Path = field(repr=False)
    _cache_adapter: _CacheRuntimeAdapter = field(repr=False)
    _descriptor: object = field(repr=False)
    _runtime_adapter: _RuntimeAdapter | None = field(repr=False, default=None)
    _seal: bytes = field(default=b"", repr=False)
    _closed: bool = field(default=False, repr=False)

    @classmethod
    def _acquire(
        cls,
        path: Path,
        sha256: str,
        cache_root: Path,
        adapter: _CacheRuntimeAdapter,
    ) -> LockedPrivateCodex:
        canonical = path.resolve(strict=True)
        canonical_root = cache_root.resolve(strict=True)
        adapter.validate_cache_ancestor_chain(canonical_root)
        adapter.validate_private_directory(canonical_root.parent)
        adapter.validate_private_directory(canonical_root)
        adapter.validate_private_directory(canonical.parent)
        adapter.validate_private_file(canonical)
        runtime = (
            adapter._runtime  # type: ignore[attr-defined]
            if type(adapter) is _native_adapter_types()[1]
            else None
        )
        descriptor: object
        if runtime is not None:
            descriptor = runtime.open_locked(canonical)
        else:
            descriptor = canonical.open("rb", buffering=0)
        lease: LockedPrivateCodex | None = None
        primary: BaseException | None = None
        try:
            if runtime is not None:
                if not runtime.is_disk_regular_non_reparse(descriptor):  # type: ignore[arg-type]
                    raise OSError("private Codex executable is unsafe")
                identity = runtime.identity(descriptor)  # type: ignore[arg-type]
                final_path = runtime.final_path(descriptor)  # type: ignore[arg-type]
                if not runtime.same_file(final_path, canonical):
                    raise OSError("private Codex executable changed")
                publisher = runtime.verify_publisher(  # type: ignore[arg-type]
                    descriptor, final_path,
                )
                runtime.rewind(descriptor)  # type: ignore[arg-type]
                digest = _hash_runtime_descriptor(runtime, descriptor)
                stable = runtime.identity(descriptor)  # type: ignore[arg-type]
            else:
                stream = descriptor
                metadata = os.fstat(stream.fileno())  # type: ignore[attr-defined]
                identity = NativeCodexIdentity(
                    metadata.st_dev, metadata.st_ino, metadata.st_size,
                    metadata.st_mtime_ns,
                )
                publisher = adapter.inspect_private_executable(canonical)[1]
                stream.seek(0)  # type: ignore[attr-defined]
                digest = _hash_stream(stream)
                after = os.fstat(stream.fileno())  # type: ignore[attr-defined]
                stable = NativeCodexIdentity(
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                )
            manifest_path = canonical.parent / "manifest.json"
            initial_manifest_identity = adapter.validate_private_file(
                manifest_path,
            )
            manifest = _read_strict_manifest(manifest_path, adapter)
            if (
                adapter.validate_private_file(manifest_path)
                != initial_manifest_identity
            ):
                raise OSError("manifest changed")
            source_identity = _source_identity_from_manifest(manifest)
            if (
                publisher != _expected_publisher_for_target(canonical.name)
                or stable != identity
                or digest != sha256
                or not _is_valid_manifest(manifest, sha256)
                or manifest["target"] != canonical.name
                or manifest["publisher"] != publisher
                or source_identity is None
                or identity.size != manifest["size"]
            ):
                raise OSError("private Codex runtime is unavailable")
            lease = cls(
                path=canonical,
                identity=identity,
                sha256=sha256,
                source_identity=source_identity,
                _cache_root=canonical_root,
                _cache_adapter=adapter,
                _descriptor=descriptor,
                _runtime_adapter=runtime,
            )
            lease._seal = _private_lease_mac(lease)
        except BaseException as error:
            primary = error
        if primary is not None:
            try:
                if runtime is not None:
                    runtime.close(descriptor)  # type: ignore[arg-type]
                else:
                    descriptor.close()  # type: ignore[attr-defined]
            except BaseException as cleanup:
                if _is_priority_failure(cleanup) and not _is_priority_failure(primary):
                    raise cleanup
            raise primary
        assert lease is not None
        return lease

    def verify(self) -> bool:
        try:
            if self._closed:
                return False
            if not hmac.compare_digest(self._seal, _private_lease_mac(self)):
                return False
            self._cache_adapter.validate_cache_ancestor_chain(self._cache_root)
            self._cache_adapter.validate_private_directory(self._cache_root.parent)
            self._cache_adapter.validate_private_directory(self._cache_root)
            self._cache_adapter.validate_private_directory(self.path.parent)
            self._cache_adapter.validate_private_file(self.path)
            manifest_path = self.path.parent / "manifest.json"
            initial_manifest_identity = self._cache_adapter.validate_private_file(
                manifest_path,
            )
            manifest = _read_strict_manifest(
                manifest_path, self._cache_adapter,
            )
            if (
                self._cache_adapter.validate_private_file(manifest_path)
                != initial_manifest_identity
            ):
                return False
            if (
                self.path.resolve(strict=True) != self.path
                or self.path.parent.parent != self._cache_root
                or self.path.parent.name != self.sha256
                or not _is_valid_manifest(manifest, self.sha256)
                or manifest["target"] != self.path.name
                or _source_identity_from_manifest(manifest) != self.source_identity
            ):
                return False
            if self._runtime_adapter is not None:
                runtime = self._runtime_adapter
                return (
                    runtime.is_disk_regular_non_reparse(self._descriptor)  # type: ignore[arg-type]
                    and runtime.identity(self._descriptor) == self.identity  # type: ignore[arg-type]
                    and runtime.same_file(
                        runtime.final_path(self._descriptor), self.path,  # type: ignore[arg-type]
                    )
                )
            metadata = os.fstat(self._descriptor.fileno())  # type: ignore[attr-defined]
            current = NativeCodexIdentity(
                metadata.st_dev, metadata.st_ino, metadata.st_size,
                metadata.st_mtime_ns,
            )
            path_metadata = self.path.stat(follow_symlinks=False)
            path_identity = NativeCodexIdentity(
                path_metadata.st_dev,
                path_metadata.st_ino,
                path_metadata.st_size,
                path_metadata.st_mtime_ns,
            )
            return current == self.identity == path_identity
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._runtime_adapter is not None:
            self._runtime_adapter.close(self._descriptor)  # type: ignore[arg-type]
        else:
            self._descriptor.close()  # type: ignore[attr-defined]


def verify_locked_private_codex_for_execution(
    lease: LockedPrivateCodex,
    *,
    cache_root: Path,
) -> None:
    """用新建的 OS 适配器复验锁定 runtime 与受证明的缓存根目录。

    ``cache_root`` 必须来自已通过 attestation 的命令；Windows 还会将其
    绑定到 ``LOCALAPPDATA`` 缺省根目录。任一身份、权限、签名或路径检查
    失败时抛出已脱敏的 ``OSError``，且不会启动子进程。
    """

    if (
        type(lease) is not LockedPrivateCodex
        or lease._closed
        or (os.name != "nt" and sys.platform != "darwin")
        or not isinstance(cache_root, Path)
        or "\x00" in os.fspath(cache_root)
        or not cache_root.is_absolute()
    ):
        raise OSError("private Codex OS verification failed")
    runtime_type, cache_type = _native_adapter_types()
    if (
        type(lease._runtime_adapter) is not runtime_type
        or type(lease._cache_adapter) is not cache_type
    ):
        raise OSError("private Codex OS verification failed")
    runtime: _RuntimeAdapter = runtime_type()
    cache: _CacheRuntimeAdapter = cache_type(_runtime_adapter=runtime)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if (
            type(local_app_data) is not str
            or not local_app_data
            or "\x00" in local_app_data
            or not Path(local_app_data).is_absolute()
        ):
            raise OSError("private Codex OS verification failed")
        platform_root: Path | None = (
            Path(local_app_data) / "ones-dev" / "codex-runtime"
        )
    elif sys.platform == "darwin":
        # macOS production wiring may provide a testable/custom host cache root.
        # It remains safe because the command attestation seals this value and
        # the verifier below binds it back to the locked lease before execution.
        platform_root = None
    else:
        raise OSError("private Codex OS verification failed")
    try:
        expected_root = cache_root.resolve(strict=True)
        path = lease.path.resolve(strict=True)
        root = lease._cache_root.resolve(strict=True)
        if (
            root != expected_root
            or (
                platform_root is not None
                and expected_root != platform_root.resolve(strict=True)
            )
            or path.parent.parent != root
            or path.parent.name != lease.sha256
            or not _is_supported_runtime_target(path.name)
            or not _is_sha256(lease.sha256)
        ):
            raise OSError("private Codex OS verification failed")
        cache.validate_cache_ancestor_chain(root)
        cache.validate_private_directory(root.parent)
        cache.validate_private_directory(root)
        cache.validate_private_directory(path.parent)
        cache.validate_private_file(path)
        quarantine = root / (
            f"{_QUARANTINE_PREFIX}{lease.sha256}{_QUARANTINE_SUFFIX}"
        )
        try:
            quarantine.lstat()
        except FileNotFoundError:
            pass
        else:
            raise OSError("private Codex OS verification failed")

        descriptor = lease._descriptor
        if not runtime.is_disk_regular_non_reparse(descriptor):  # type: ignore[arg-type]
            raise OSError("private Codex OS verification failed")
        initial = runtime.identity(descriptor)  # type: ignore[arg-type]
        final_path = runtime.final_path(descriptor)  # type: ignore[arg-type]
        if (
            initial != lease.identity
            or not runtime.same_file(final_path, path)
        ):
            raise OSError("private Codex OS verification failed")

        manifest_path = path.parent / "manifest.json"
        manifest_identity = cache.validate_private_file(manifest_path)
        manifest = _read_strict_manifest(manifest_path, cache)
        if (
            cache.validate_private_file(manifest_path) != manifest_identity
            or not _is_valid_manifest(manifest, lease.sha256)
            or manifest["target"] != path.name
            or _source_identity_from_manifest(manifest) != lease.source_identity
            or manifest["size"] != initial.size
        ):
            raise OSError("private Codex OS verification failed")

        runtime.rewind(descriptor)  # type: ignore[arg-type]
        if _hash_runtime_descriptor(runtime, descriptor) != lease.sha256:
            raise OSError("private Codex OS verification failed")
        if runtime.identity(descriptor) != initial:  # type: ignore[arg-type]
            raise OSError("private Codex OS verification failed")
        if (
            runtime.verify_publisher(descriptor, final_path)  # type: ignore[arg-type]
            != _expected_publisher_for_target(path.name)
            or runtime.identity(descriptor) != initial  # type: ignore[arg-type]
            or not runtime.same_file(
                runtime.final_path(descriptor), path,  # type: ignore[arg-type]
            )
        ):
            raise OSError("private Codex OS verification failed")
        cache.validate_private_file(path)
        try:
            quarantine.lstat()
        except FileNotFoundError:
            return
        raise OSError("private Codex OS verification failed")
    except (AttributeError, OSError, TypeError, ValueError):
        raise OSError("private Codex OS verification failed") from None


def _runtime_attestation_mac(
    path: Path,
    identity: NativeCodexIdentity,
    sha256: str,
    cache_root: Path,
) -> bytes:
    snapshot = json.dumps(
        [
            "prepared-codex-runtime-v1",
            str(cache_root),
            str(path),
            sha256,
            identity.volume_serial,
            identity.file_index,
            identity.size,
            identity.mtime_ns,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    return hmac.digest(_RUNTIME_ATTESTATION_SECRET, snapshot, "sha256")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _PreparedCodexRuntime:
    path: Path
    identity: NativeCodexIdentity
    sha256: str
    _cache_root: Path = field(repr=False)
    _lease: LockedPrivateCodex = field(repr=False)
    _seal: bytes = field(repr=False)
    _nonce: object = field(repr=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("verified Codex runtimes cannot be constructed directly")

    @classmethod
    def _issue(
        cls,
        path: Path,
        identity: NativeCodexIdentity,
        sha256: str,
        cache_root: Path,
        lease: LockedPrivateCodex,
        *,
        nonce: object,
    ) -> _PreparedCodexRuntime:
        if nonce is not _RUNTIME_ATTESTATION_NONCE:
            raise TypeError("invalid Codex runtime attestation")
        canonical = path.resolve(strict=True)
        canonical_root = cache_root.resolve(strict=True)
        if (
            not _is_supported_runtime_target(canonical.name)
            or not _is_sha256(sha256)
            or type(identity) is not NativeCodexIdentity
            or any(
                type(value) is not int
                for value in (
                    identity.volume_serial,
                    identity.file_index,
                    identity.size,
                    identity.mtime_ns,
                )
            )
            or identity.size < 0
            or type(lease) is not LockedPrivateCodex
            or not lease.verify()
            or lease.path != canonical
            or lease.identity != identity
            or lease.sha256 != sha256
            or lease._cache_root != canonical_root
            or canonical.parent.name != sha256
            or canonical.parent.parent != canonical_root
        ):
            raise ValueError("invalid Codex runtime attestation")
        seal = _runtime_attestation_mac(
            canonical, identity, sha256, canonical_root,
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "path", canonical)
        object.__setattr__(instance, "identity", identity)
        object.__setattr__(instance, "sha256", sha256)
        object.__setattr__(instance, "_cache_root", canonical_root)
        object.__setattr__(instance, "_lease", lease)
        object.__setattr__(instance, "_seal", seal)
        object.__setattr__(instance, "_nonce", nonce)
        return instance

    def _is_attested(self) -> bool:
        try:
            if (
                self._nonce is not _RUNTIME_ATTESTATION_NONCE
                or not isinstance(self.path, Path)
                or type(self.identity) is not NativeCodexIdentity
                or not _is_sha256(self.sha256)
                or not isinstance(self._cache_root, Path)
                or type(self._lease) is not LockedPrivateCodex
                or not self._lease.verify()
                or self._lease.path != self.path
                or self._lease.identity != self.identity
                or self._lease.sha256 != self.sha256
                or self._lease._cache_root != self._cache_root
                or type(self._seal) is not bytes
                or self.path.resolve(strict=True) != self.path
                or self._cache_root.resolve(strict=True) != self._cache_root
                or not _is_supported_runtime_target(self.path.name)
                or self.path.parent.name != self.sha256
                or self.path.parent.parent != self._cache_root
            ):
                return False
            expected = _runtime_attestation_mac(
                self.path, self.identity, self.sha256, self._cache_root,
            )
            return hmac.compare_digest(self._seal, expected)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def __copy__(self) -> _PreparedCodexRuntime:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _PreparedCodexRuntime:
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("verified Codex runtimes cannot be serialized")


_CACHE_SCHEMA_KEYS = frozenset(
    {
        "publisher", "schema_version", "sha256", "size", "source_identity",
        "target",
    }
)
_QUARANTINE_SCHEMA_KEYS = frozenset({"schema_version", "sha256"})
_QUARANTINE_PREFIX = ".quarantine-"
_QUARANTINE_SUFFIX = ".json"


class CodexRuntimePreparer:
    """Stage a verified native payload into a private content-addressed cache."""

    def __init__(
        self,
        *,
        cache_root: Path | None = None,
        discover: Callable[[], LockedNativeCodex] | None = None,
        _cache_adapter: _CacheRuntimeAdapter | None = None,
        chunk_size: int = 1024 * 1024,
        smoke_timeout: float = 5.0,
        _clock_ns: Callable[[], int] = time.time_ns,
        _lease_grace_ns: int = 30_000_000_000,
    ) -> None:
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk size must be a positive integer")
        if not isinstance(smoke_timeout, (int, float)) or smoke_timeout <= 0:
            raise ValueError("smoke timeout must be positive")
        if (
            not callable(_clock_ns)
            or type(_lease_grace_ns) is not int
            or _lease_grace_ns < 0
        ):
            raise ValueError("private runtime lease timing is invalid")
        if cache_root is None:
            if os.name == "nt":
                local_app_data = os.environ.get("LOCALAPPDATA")
                if not local_app_data:
                    raise OSError("private Codex runtime is unavailable")
                cache_root = Path(local_app_data) / "ones-dev" / "codex-runtime"
            elif sys.platform == "darwin":
                from .platform_support import default_host_paths

                cache_root = default_host_paths().cache_root
            else:
                raise OSError("private Codex runtime is unavailable")
        if "\x00" in os.fspath(cache_root):
            raise OSError("private Codex runtime is unavailable")
        self._cache_root = Path(cache_root)
        self._discover = discover
        self._cache_adapter = _cache_adapter
        self._chunk_size = chunk_size
        self._smoke_timeout = float(smoke_timeout)
        self._clock_ns = _clock_ns
        self._lease_grace_ns = _lease_grace_ns

    def _adapter(self) -> _CacheRuntimeAdapter:
        if self._cache_adapter is not None:
            return self._cache_adapter
        if os.name != "nt" and sys.platform != "darwin":
            raise OSError("private Codex runtime is unavailable")
        return _default_cache_adapter()

    @staticmethod
    def _target_name(adapter: _CacheRuntimeAdapter) -> str:
        # Test adapters written for the original Windows contract intentionally
        # retain codex.exe unless they explicitly opt into a platform target.
        value = getattr(adapter, "executable_name", "codex.exe")
        if type(value) is not str or not _is_supported_runtime_target(value):
            raise OSError("private Codex runtime is unavailable")
        return value

    def prepare(self) -> Path:
        adapter = self._adapter()
        root = self._prepare_root(adapter)
        self._cleanup_stale_owned_entries(root, adapter)
        quarantined, rebuildable = self._scan_quarantines(root, adapter)
        cached = self._valid_cached_executables(
            root, adapter, excluded=quarantined,
        )
        discovery = self._discover or discover_locked_native_codex
        try:
            locked = discovery()
        except BaseException as error:
            if _is_priority_failure(error) or not isinstance(error, OSError):
                raise
            if cached:
                return cached[sorted(cached)[-1]][0]
            raise OSError("private Codex runtime is unavailable") from None
        result: Path | None = None
        primary: BaseException | None = None
        primary_traceback = None
        try:
            try:
                identity_matches = [
                    (digest, path)
                    for digest, (path, source_identity) in cached.items()
                    if source_identity == locked.identity
                ]
                if (
                    identity_matches
                    and locked.current_identity() == locked.identity
                ):
                    result = sorted(identity_matches)[-1][1]
                else:
                    source_sha256 = self._hash_locked_source(locked)
                    matching = cached.get(source_sha256)
                    if (
                        source_sha256 in quarantined
                        and source_sha256 not in rebuildable
                    ):
                        raise OSError("private Codex runtime is quarantined")
                    result = (
                        matching[0]
                        if matching is not None
                        else self._stage_locked_source(
                            root,
                            locked,
                            source_sha256,
                            adapter,
                            quarantine=rebuildable.get(source_sha256),
                        )
                    )
                if type(locked) is LockedNativeCodex:
                    self._ensure_code_mode_host(result, locked, adapter)
            except BaseException as error:
                if _is_priority_failure(error) or not isinstance(error, OSError):
                    raise
                if cached:
                    result = cached[sorted(cached)[-1]][0]
                else:
                    raise OSError(
                        "private Codex runtime is unavailable"
                    ) from None
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        cleanup: BaseException | None = None
        cleanup_traceback = None
        try:
            locked.close()
        except BaseException as error:
            cleanup = error
            cleanup_traceback = error.__traceback__
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def _ensure_code_mode_host(
        self,
        executable: Path,
        locked: LockedNativeCodex,
        adapter: _CacheRuntimeAdapter,
    ) -> None:
        """Stage the signed companion required by current Codex desktop builds."""

        runtime = locked._adapter
        source_main = runtime.final_path(locked.descriptor)
        target_name = self._target_name(adapter)
        companion_name = (
            "codex-code-mode-host" if target_name == "codex"
            else "codex-code-mode-host.exe"
        )
        source = source_main.with_name(companion_name)
        descriptor = runtime.open_locked(source)
        temporary = executable.parent / f"code-mode-host-{uuid.uuid4().hex}.tmp"
        primary: BaseException | None = None
        try:
            if not runtime.is_disk_regular_non_reparse(descriptor):
                raise OSError("native Codex companion is unavailable")
            initial = runtime.identity(descriptor)
            final_path = runtime.final_path(descriptor)
            if (
                initial.size < 0
                or final_path.parent != source_main.parent
                or final_path.name.casefold() != companion_name.casefold()
                or not runtime.same_file(final_path, source)
                or runtime.verify_publisher(descriptor, final_path)
                != _expected_publisher_for_target(target_name)
            ):
                raise OSError("native Codex companion is unavailable")
            runtime.rewind(descriptor)
            digest = hashlib.sha256()
            copied = 0
            with temporary.open("xb", buffering=0) as destination:
                while True:
                    chunk = runtime.read(descriptor, self._chunk_size)
                    if not chunk:
                        break
                    _write_all(destination, chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if copied != initial.size or runtime.identity(descriptor) != initial:
                raise OSError("native Codex companion changed")

            target = executable.parent / companion_name
            expected_digest = digest.hexdigest()
            reuse = False
            try:
                target_identity, target_publisher = (
                    adapter.inspect_private_executable(target)
                )
                reuse = (
                    target_identity.size == copied
                    and target_publisher == _expected_publisher_for_target(
                        target_name,
                    )
                    and _hash_file(target) == expected_digest
                )
            except (FileNotFoundError, OSError, ValueError):
                reuse = False
            if not reuse:
                adapter.protect_private_file(temporary)
                os.replace(temporary, target)
                adapter.fsync_directory(executable.parent)
            target_identity, target_publisher = adapter.inspect_private_executable(
                target,
            )
            if (
                target_identity.size != copied
                or target_publisher != _expected_publisher_for_target(target_name)
                or _hash_file(target) != expected_digest
            ):
                raise OSError("private Codex companion is unavailable")
        except BaseException as error:
            primary = error
        finally:
            runtime.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if primary is not None:
            raise primary

    def prepare_verified(self) -> _PreparedCodexRuntime:
        path = self.prepare()
        adapter = self._adapter()
        sha256 = path.parent.name
        lease = LockedPrivateCodex._acquire(
            path, sha256, path.parent.parent, adapter,
        )
        try:
            return _PreparedCodexRuntime._issue(
                path,
                lease.identity,
                sha256,
                path.parent.parent,
                lease,
                nonce=_RUNTIME_ATTESTATION_NONCE,
            )
        except BaseException:
            lease.close()
            raise

    def _hash_locked_source(self, locked: LockedNativeCodex) -> str:
        digest = hashlib.sha256()
        copied = 0
        locked.rewind()
        while True:
            chunk = locked.read_chunk(self._chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
        if copied != locked.size or locked.current_identity() != locked.identity:
            raise OSError("native Codex payload changed")
        return digest.hexdigest()

    def _stage_locked_source(
        self,
        root: Path,
        locked: LockedNativeCodex,
        source_sha256: str,
        adapter: _CacheRuntimeAdapter,
        *,
        quarantine: Path | None = None,
    ) -> Path:
        staging_token = uuid.uuid4().hex
        staging = root / f".staging-{staging_token}"
        final_directory = root / source_sha256
        write_ahead = root / (
            f"{_QUARANTINE_PREFIX}{source_sha256}{_QUARANTINE_SUFFIX}"
        )
        published_final: Path | None = None
        write_ahead_started = False
        clear_started = False
        final_validated = False
        result: Path | None = None
        primary: BaseException | None = None
        primary_traceback = None
        staging_lease: _StagingLease | None = None
        try:
            staging_lease = _StagingLease.acquire(
                root, staging_token, adapter, created_ns=self._lease_now_ns(),
            )
            adapter.fsync_directory(root)
            adapter.prepare_private_directory(staging)
            temporary = staging / f"codex-{uuid.uuid4().hex}.tmp"
            digest = hashlib.sha256()
            copied = 0
            locked.rewind()
            with temporary.open("xb", buffering=0) as destination:
                while True:
                    chunk = locked.read_chunk(self._chunk_size)
                    if not chunk:
                        break
                    _write_all(destination, chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if copied != locked.size or locked.current_identity() != locked.identity:
                raise OSError("native Codex payload changed")
            adapter.protect_private_file(temporary)
            sha256 = digest.hexdigest()
            if sha256 != source_sha256:
                raise OSError("native Codex payload changed")
            target_name = self._target_name(adapter)
            executable = staging / target_name
            os.replace(temporary, executable)
            adapter.fsync_directory(staging)
            self._validate_executable(
                executable, sha256=sha256, size=copied, adapter=adapter,
            )
            manifest = {
                "publisher": _expected_publisher_for_target(target_name),
                "schema_version": 1,
                "sha256": sha256,
                "size": copied,
                "source_identity": _identity_manifest(locked.identity),
                "target": target_name,
            }
            manifest_temp = staging / f"manifest-{uuid.uuid4().hex}.tmp"
            with manifest_temp.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest, stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            adapter.protect_private_file(manifest_temp)
            os.replace(manifest_temp, staging / "manifest.json")
            adapter.fsync_directory(staging)
            self._validate_staging_ready(
                staging, sha256=sha256, size=copied, adapter=adapter,
            )
            adapter.smoke(
                executable,
                environment=_sanitized_smoke_environment(),
                timeout=self._smoke_timeout,
            )
            write_ahead_started = True
            self._publish_quarantine(root, sha256, adapter)
            if final_directory.exists():
                adapter.fsync_directory(root)
                try:
                    result = self._validate_cache_directory(
                        final_directory, sha256, adapter, smoke=False,
                    )
                except BaseException as error:
                    if _is_priority_failure(error) or not isinstance(
                        error, (OSError, ValueError, json.JSONDecodeError)
                    ):
                        raise
                    if quarantine is None:
                        raise
                    _remove_owned_tree(final_directory)
                    adapter.fsync_directory(root)
                    os.replace(staging, final_directory)
                    published_final = final_directory
                    if staging_lease._rename_safe:
                        staging_lease.path.unlink()
                        staging_lease.close()
                    else:
                        lease_path = staging_lease.path
                        staging_lease.close()
                        lease_path.unlink()
                    staging_lease = None
                    adapter.fsync_directory(root)
                    result = self._validate_cache_directory(
                        final_directory, sha256, adapter, smoke=False,
                    )
            else:
                os.replace(staging, final_directory)
                published_final = final_directory
                if staging_lease._rename_safe:
                    staging_lease.path.unlink()
                    staging_lease.close()
                else:
                    lease_path = staging_lease.path
                    staging_lease.close()
                    lease_path.unlink()
                staging_lease = None
                adapter.fsync_directory(root)
                result = self._validate_cache_directory(
                    final_directory, sha256, adapter, smoke=False,
                )
            final_validated = True
            try:
                initial_identity = adapter.validate_private_file(write_ahead)
            except FileNotFoundError:
                # A concurrent preparer may have durably validated this exact
                # content-addressed final and removed the shared marker.
                pass
            else:
                record = _read_strict_manifest(write_ahead, adapter)
                if (
                    adapter.validate_private_file(write_ahead) != initial_identity
                    or not _is_valid_quarantine(record, sha256)
                ):
                    raise OSError("invalid private runtime quarantine")
                clear_started = True
                write_ahead.unlink()
                adapter.fsync_directory(root)
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        quarantine_ready = False
        marker_missing_after_concurrent_commit = False
        marker_probe_error: BaseException | None = None
        marker_exists = True
        if write_ahead_started and primary is not None and not clear_started:
            try:
                marker_exists = write_ahead.exists()
            except BaseException as error:
                marker_probe_error = error
        concurrent_probe_error: BaseException | None = None
        if (
            write_ahead_started
            and primary is not None
            and not clear_started
            and marker_probe_error is None
            and not marker_exists
            and final_directory.exists()
        ):
            try:
                adapter.fsync_directory(root)
                self._validate_cache_directory(
                    final_directory, source_sha256, adapter, smoke=False,
                )
            except BaseException as error:
                concurrent_probe_error = error
            else:
                marker_missing_after_concurrent_commit = True
        if (
            write_ahead_started
            and primary is not None
            and not marker_missing_after_concurrent_commit
        ):
            try:
                self._publish_quarantine(root, source_sha256, adapter)
                quarantine_ready = True
            except BaseException as error:
                quarantine_error = error
            else:
                quarantine_error = None
        else:
            quarantine_error = None
        cleanup_paths = [staging]
        if (
            quarantine_ready
            and published_final is not None
            and primary is not None
            and not clear_started
        ):
            cleanup_paths.append(published_final)
        lease_cleanup: BaseException | None = None
        if staging_lease is not None:
            lease_path = staging_lease.path
            try:
                staging_lease.close()
                if (
                    lease_path.parent == root
                    and lease_path.name == f".lease-{staging_token}"
                ):
                    lease_path.unlink(missing_ok=True)
            except BaseException as error:
                lease_cleanup = error
        cleanup = _attempt_owned_cleanup(tuple(cleanup_paths))
        cleanup = _prefer_cleanup_error(cleanup, lease_cleanup)
        cleanup = _prefer_cleanup_error(cleanup, marker_probe_error)
        cleanup = _prefer_cleanup_error(cleanup, concurrent_probe_error)
        cleanup = _prefer_cleanup_error(cleanup, quarantine_error)
        if (
            published_final is not None
            and primary is not None
            and quarantine_ready
            and not clear_started
        ):
            if published_final.exists():
                retry_error = _attempt_owned_cleanup((published_final,))
                cleanup = _prefer_cleanup_error(cleanup, retry_error)
            try:
                adapter.fsync_directory(root)
            except BaseException as error:
                cleanup = _prefer_cleanup_error(cleanup, error)
            if published_final.exists():
                cleanup = _prefer_cleanup_error(
                    cleanup,
                    OSError("attempted private runtime could not be removed"),
                )
        cleanup_traceback = cleanup.__traceback__ if cleanup is not None else None
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def _prepare_root(self, adapter: _CacheRuntimeAdapter) -> Path:
        root = self._cache_root
        if not root.is_absolute():
            raise OSError("private Codex runtime is unavailable")
        parent = root.parent
        if parent.parent == parent:
            raise OSError("private Codex runtime is unavailable")
        adapter.validate_cache_ancestor_chain(root)
        adapter.prepare_private_directory(parent)
        prepared = adapter.prepare_private_directory(root)
        adapter.validate_cache_ancestor_chain(prepared)
        adapter.validate_private_directory(parent)
        adapter.validate_private_directory(prepared)
        return prepared

    def _valid_cached_executables(
        self,
        root: Path,
        adapter: _CacheRuntimeAdapter,
        *,
        excluded: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, tuple[Path, NativeCodexIdentity]]:
        valid: dict[str, tuple[Path, NativeCodexIdentity]] = {}
        try:
            candidates = tuple(root.iterdir())
        except OSError:
            raise OSError("private Codex runtime is unavailable") from None
        for directory in candidates:
            digest = directory.name
            if not (
                len(digest) == 64
                and all(character in "0123456789abcdef" for character in digest)
            ) or digest in excluded:
                continue
            try:
                path = self._validate_cache_directory(directory, digest, adapter)
                manifest_path = directory / "manifest.json"
                initial_manifest_identity = adapter.validate_private_file(
                    manifest_path,
                )
                manifest = _read_strict_manifest(
                    manifest_path, adapter,
                )
                if (
                    adapter.validate_private_file(manifest_path)
                    != initial_manifest_identity
                ):
                    raise OSError("manifest changed")
                source_identity = _source_identity_from_manifest(manifest)
                if source_identity is None:
                    raise OSError("invalid manifest source identity")
                valid[digest] = path, source_identity
            except BaseException as error:
                if _is_priority_failure(error) or not isinstance(
                    error, (OSError, ValueError, json.JSONDecodeError)
                ):
                    raise
        return valid

    def _scan_quarantines(
        self, root: Path, adapter: _CacheRuntimeAdapter,
    ) -> tuple[set[str], dict[str, Path]]:
        blocked: set[str] = set()
        valid: dict[str, Path] = {}
        for candidate in tuple(root.iterdir()):
            digest = _quarantine_digest(candidate.name)
            if digest is None:
                continue
            blocked.add(digest)
            try:
                initial_identity = adapter.validate_private_file(candidate)
                record = _read_strict_manifest(candidate, adapter)
                if (
                    adapter.validate_private_file(candidate) != initial_identity
                    or not _is_valid_quarantine(record, digest)
                ):
                    raise OSError("invalid private runtime quarantine")
                valid[digest] = candidate
            except BaseException as error:
                if _is_priority_failure(error) or not isinstance(
                    error, (OSError, ValueError, json.JSONDecodeError)
                ):
                    raise
        return blocked, valid

    def _publish_quarantine(
        self, root: Path, digest: str, adapter: _CacheRuntimeAdapter,
    ) -> Path:
        if not _is_sha256(digest):
            raise OSError("invalid private runtime quarantine")
        final = root / f"{_QUARANTINE_PREFIX}{digest}{_QUARANTINE_SUFFIX}"
        temporary = root / (
            f"{_QUARANTINE_PREFIX}{digest}-{uuid.uuid4().hex}.tmp"
        )
        result: Path | None = None
        primary: BaseException | None = None
        primary_traceback = None
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    {"schema_version": 1, "sha256": digest},
                    stream,
                    ensure_ascii=True,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            adapter.protect_private_file(temporary)
            os.replace(temporary, final)
            adapter.fsync_directory(root)
            initial_identity = adapter.validate_private_file(final)
            record = _read_strict_manifest(final, adapter)
            if (
                adapter.validate_private_file(final) != initial_identity
                or not _is_valid_quarantine(record, digest)
            ):
                raise OSError("invalid private runtime quarantine")
            result = final
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        cleanup: BaseException | None = None
        cleanup_traceback = None
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except BaseException as error:
            cleanup = error
            cleanup_traceback = error.__traceback__
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def _validate_staging_ready(
        self,
        directory: Path,
        *,
        sha256: str,
        size: int,
        adapter: _CacheRuntimeAdapter,
    ) -> None:
        adapter.validate_private_directory(directory)
        manifest_path = directory / "manifest.json"
        initial_identity = adapter.validate_private_file(manifest_path)
        manifest = _read_strict_manifest(manifest_path, adapter)
        if (
            adapter.validate_private_file(manifest_path) != initial_identity
            or not _is_valid_manifest(manifest, sha256)
            or manifest["target"] != self._target_name(adapter)
            or manifest["size"] != size
        ):
            raise OSError("private Codex runtime is unavailable")
        self._validate_executable(
            directory / self._target_name(adapter),
            sha256=sha256,
            size=size,
            adapter=adapter,
        )

    def _validate_cache_directory(
        self,
        directory: Path,
        digest: str,
        adapter: _CacheRuntimeAdapter,
        *,
        smoke: bool = True,
    ) -> Path:
        adapter.validate_private_directory(directory)
        manifest_path = directory / "manifest.json"
        initial_manifest_identity = adapter.validate_private_file(manifest_path)
        manifest = _read_strict_manifest(manifest_path, adapter)
        if adapter.validate_private_file(manifest_path) != initial_manifest_identity:
            raise OSError("manifest changed")
        if not _is_valid_manifest(manifest, digest):
            raise OSError("invalid manifest")
        if manifest["target"] != self._target_name(adapter):
            raise OSError("invalid manifest target")
        executable = directory / self._target_name(adapter)
        self._validate_executable(
            executable,
            sha256=digest,
            size=manifest["size"],
            adapter=adapter,
        )
        if smoke:
            adapter.smoke(
                executable,
                environment=_sanitized_smoke_environment(),
                timeout=self._smoke_timeout,
            )
        resolved = executable.resolve(strict=True)
        if not resolved.is_relative_to(self._cache_root.resolve(strict=True)):
            raise OSError("cache target escaped")
        return resolved

    def _lease_now_ns(self) -> int:
        value = self._clock_ns()
        if type(value) is not int or value < 0:
            raise OSError("private runtime lease clock is invalid")
        return value

    def _cleanup_stale_owned_entries(
        self, root: Path, adapter: _CacheRuntimeAdapter,
    ) -> None:
        entries = tuple(root.iterdir())
        leased_tokens: set[str] = set()
        for candidate in entries:
            name = candidate.name
            if not name.startswith(".lease-"):
                continue
            token = name[len(".lease-") :]
            if not _is_stage_token(token):
                continue
            leased_tokens.add(token)
            lease: _StagingLease | None = None
            try:
                adapter.validate_private_file(candidate)
                lease = _StagingLease.acquire(
                    root, token, adapter, create=False,
                )
            except BlockingIOError:
                continue
            except BaseException as error:
                if _is_priority_failure(error):
                    raise
                if not isinstance(error, (OSError, ValueError)):
                    raise
                continue
            stage = root / f".staging-{token}"
            try:
                now_ns = self._lease_now_ns()
                freshness_ns = max(
                    lease.created_ns, lease.file_identity[3],
                )
                if (
                    now_ns < freshness_ns
                    or now_ns - freshness_ns < self._lease_grace_ns
                ):
                    continue
                stage_safe = True
                try:
                    try:
                        stage.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        adapter.validate_private_directory(stage)
                        children = tuple(stage.iterdir())
                        if any(
                            not _is_owned_staging_child(child)
                            for child in children
                        ):
                            stage_safe = False
                        else:
                            for child in children:
                                adapter.validate_private_file(child)
                            cleanup = _attempt_owned_cleanup((stage,))
                            if cleanup is not None:
                                raise cleanup
                except BaseException as error:
                    stage_safe = False
                    if _is_priority_failure(error):
                        raise
                    if not isinstance(error, (OSError, ValueError)):
                        raise
                if not stage_safe:
                    continue
                if (
                    _staging_file_identity(candidate) != lease.file_identity
                    or _descriptor_file_identity(lease._stream)
                    != lease.file_identity
                    or adapter.validate_private_file(candidate)
                    != lease.file_identity[:2]
                ):
                    continue
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
                adapter.fsync_directory(root)
            finally:
                lease.close()

        for candidate in entries:
            name = candidate.name
            random_stage = (
                name.startswith(".staging-")
                and len(name) == len(".staging-") + 32
                and all(character in "0123456789abcdef" for character in name[9:])
            )
            deterministic_name = (
                len(name) == 64
                and all(character in "0123456789abcdef" for character in name)
            )
            if random_stage:
                token = name[len(".staging-") :]
                if token in leased_tokens:
                    continue
                try:
                    adapter.validate_private_directory(candidate)
                    children = tuple(candidate.iterdir())
                    if any(not _is_owned_staging_child(child) for child in children):
                        continue
                    for child in children:
                        adapter.validate_private_file(child)
                    cleanup = _attempt_owned_cleanup((candidate,))
                    if cleanup is not None:
                        raise cleanup
                    adapter.fsync_directory(root)
                except BaseException as error:
                    if _is_priority_failure(error):
                        raise
                    if not isinstance(error, (OSError, ValueError)):
                        raise
                continue
            if not deterministic_name:
                continue
            try:
                adapter.validate_private_directory(candidate)
                try:
                    (candidate / "manifest.json").lstat()
                except FileNotFoundError:
                    pass
                else:
                    continue
                children = tuple(candidate.iterdir())
                if any(not _is_owned_staging_child(child) for child in children):
                    continue
                for child in children:
                    adapter.validate_private_file(child)
                cleanup = _attempt_owned_cleanup((candidate,))
                if cleanup is not None:
                    raise cleanup
                adapter.fsync_directory(root)
            except BaseException as error:
                if _is_priority_failure(error):
                    raise
                if not isinstance(error, (OSError, ValueError)):
                    raise

    @staticmethod
    def _validate_executable(
        executable: Path,
        *,
        sha256: str,
        size: int,
        adapter: _CacheRuntimeAdapter,
    ) -> None:
        adapter.validate_private_file(executable)
        first_identity, publisher = adapter.inspect_private_executable(executable)
        if (
            publisher != _expected_publisher_for_target(executable.name)
            or first_identity.size != size
            or _hash_file(executable) != sha256
        ):
            raise OSError("private Codex runtime is unavailable")
        final_identity, final_publisher = adapter.inspect_private_executable(executable)
        if final_identity != first_identity or final_publisher != publisher:
            raise OSError("private Codex runtime is unavailable")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _hash_stream(stream: object) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)  # type: ignore[attr-defined]
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _hash_runtime_descriptor(
    adapter: _RuntimeAdapter, descriptor: object,
) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = adapter.read(descriptor, 1024 * 1024)  # type: ignore[arg-type]
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _write_all(stream: object, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)  # type: ignore[attr-defined]
        if type(written) is not int or written <= 0 or written > len(remaining):
            raise OSError("private Codex runtime write failed")
        remaining = remaining[written:]


def _open_staging_lease(
    path: Path, *, create: bool, native_windows: bool,
) -> object:
    if os.name != "nt" or not native_windows:
        return path.open("x+b" if create else "r+b", buffering=0)

    # Python's CRT open denies directory rename while the file is held.  The
    # lease must survive the same-volume staging-directory publish, so open it
    # with delete sharing and use the CRT descriptor only for byte locking.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x1 | 0x2 | 0x4,  # FILE_SHARE_READ | WRITE | DELETE
        None,
        1 if create else 3,  # CREATE_NEW | OPEN_EXISTING
        0x80,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if not create and error in {2, 3}:
            raise FileNotFoundError(error, os.strerror(error), path)
        if create and error == 80:
            raise FileExistsError(error, os.strerror(error), path)
        raise OSError(error, os.strerror(error), path)
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def _lock_staging_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError("private runtime staging is active") from error
        return
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        raise BlockingIOError("private runtime staging is active") from error


def _unlock_staging_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _is_stage_token(value: str) -> bool:
    return (
        type(value) is str
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _staging_lease_record(token: str, created_ns: int) -> bytes:
    return (
        json.dumps(
            {
                "created_ns": created_ns,
                "schema_version": 1,
                "token": token,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _parse_staging_lease_record(raw: bytes, token: str) -> int:
    if len(raw) > 512:
        raise OSError("private runtime staging lease is invalid")
    try:
        parsed = json.loads(
            raw.decode("ascii", "strict"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_invalid_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise OSError("private runtime staging lease is invalid") from None
    if (
        type(parsed) is not dict
        or frozenset(parsed) != frozenset(
            {"created_ns", "schema_version", "token"}
        )
        or type(parsed.get("schema_version")) is not int
        or parsed["schema_version"] != 1
        or type(parsed.get("created_ns")) is not int
        or parsed["created_ns"] < 0
        or type(parsed.get("token")) is not str
        or parsed["token"] != token
    ):
        raise OSError("private runtime staging lease is invalid")
    if raw != _staging_lease_record(token, parsed["created_ns"]):
        raise OSError("private runtime staging lease is invalid")
    return parsed["created_ns"]


def _staging_file_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = path.stat(follow_symlinks=False)
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or attributes & reparse
    ):
        raise OSError("private runtime staging lease is unsafe")
    return _stat_identity(metadata)


def _descriptor_file_identity(stream: object) -> tuple[int, int, int, int]:
    return _stat_identity(os.fstat(stream.fileno()))  # type: ignore[attr-defined]


def _is_owned_staging_child(path: Path) -> bool:
    name = path.name
    return (
        name in {".stage.lock", "codex", "codex.exe", "manifest.json"}
        or name.startswith("codex-") and name.endswith(".tmp")
        or name.startswith("manifest-") and name.endswith(".tmp")
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest member")
        result[key] = value
    return result


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _quarantine_digest(name: str) -> str | None:
    if not (
        name.startswith(_QUARANTINE_PREFIX)
        and name.endswith(_QUARANTINE_SUFFIX)
    ):
        return None
    digest = name[len(_QUARANTINE_PREFIX) : -len(_QUARANTINE_SUFFIX)]
    return digest if _is_sha256(digest) else None


def _invalid_json_constant(value: str) -> None:
    raise ValueError("invalid manifest constant")


def _read_strict_manifest(
    path: Path, adapter: _CacheRuntimeAdapter,
) -> dict[str, object]:
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_size > 4096:
        raise ValueError("manifest is oversized")
    raw = adapter.read_private_text(path)
    parsed = json.loads(
        raw,
        object_pairs_hook=_strict_json_object,
        parse_constant=_invalid_json_constant,
    )
    if type(parsed) is not dict:
        raise ValueError("manifest must be an object")
    return parsed


def _is_valid_manifest(manifest: dict[str, object], directory_name: str) -> bool:
    source_identity = _source_identity_from_manifest(manifest)
    target = manifest.get("target")
    publisher = manifest.get("publisher")
    return (
        frozenset(manifest) == _CACHE_SCHEMA_KEYS
        and type(manifest.get("schema_version")) is int
        and manifest["schema_version"] == 1
        and type(manifest.get("sha256")) is str
        and manifest["sha256"] == directory_name
        and type(publisher) is str
        and type(target) is str
        and _is_supported_runtime_target(target)
        and publisher == _expected_publisher_for_target(target)
        and type(manifest.get("size")) is int
        and manifest["size"] >= 0
        and source_identity is not None
        and source_identity.size == manifest["size"]
    )


def _identity_manifest(identity: NativeCodexIdentity) -> dict[str, int]:
    return {
        "file_index": identity.file_index,
        "mtime_ns": identity.mtime_ns,
        "size": identity.size,
        "volume_serial": identity.volume_serial,
    }


def _source_identity_from_manifest(
    manifest: dict[str, object],
) -> NativeCodexIdentity | None:
    value = manifest.get("source_identity")
    if type(value) is not dict or frozenset(value) != frozenset(
        {"file_index", "mtime_ns", "size", "volume_serial"}
    ):
        return None
    if any(type(value.get(key)) is not int for key in value):
        return None
    identity = NativeCodexIdentity(
        volume_serial=value["volume_serial"],
        file_index=value["file_index"],
        size=value["size"],
        mtime_ns=value["mtime_ns"],
    )
    return identity if identity.size >= 0 else None


def _is_valid_quarantine(record: dict[str, object], digest: str) -> bool:
    return (
        frozenset(record) == _QUARANTINE_SCHEMA_KEYS
        and type(record.get("schema_version")) is int
        and record["schema_version"] == 1
        and type(record.get("sha256")) is str
        and record["sha256"] == digest
    )


def _sanitized_smoke_environment() -> dict[str, str]:
    keep = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in keep and type(value) is str and "\x00" not in value
    }


def _remove_owned_tree(path: Path) -> None:
    if not path.name.startswith(".staging-") and not (
        len(path.name) == 64
        and all(character in "0123456789abcdef" for character in path.name)
    ):
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or attributes & reparse:
        if stat.S_ISDIR(metadata.st_mode):
            path.rmdir()
        else:
            path.unlink()
        return
    shutil.rmtree(path)


def _attempt_owned_cleanup(paths: tuple[Path, ...]) -> BaseException | None:
    first: BaseException | None = None
    priority: BaseException | None = None
    for path in paths:
        try:
            _remove_owned_tree(path)
        except BaseException as error:
            if first is None:
                first = error
            if priority is None and _is_priority_failure(error):
                priority = error
    return priority or first


def _prefer_cleanup_error(
    current: BaseException | None, candidate: BaseException | None,
) -> BaseException | None:
    if candidate is None:
        return current
    if current is None or (
        _is_priority_failure(candidate) and not _is_priority_failure(current)
    ):
        return candidate
    return current


def _final_path_has_fixed_layout(
    final_path: Path,
    locator_root: Path,
    adapter: _RuntimeAdapter,
) -> bool:
    relative_parts = NATIVE_CODEX_RELATIVE_PATH.parts
    if len(final_path.parts) <= len(relative_parts):
        return False
    if tuple(part.casefold() for part in final_path.parts[-len(relative_parts):]) != tuple(
        part.casefold() for part in relative_parts
    ):
        return False
    final_root = final_path.parents[len(relative_parts) - 1]
    return adapter.same_file(final_root, locator_root)


def _current_repository_chain_roots(adapter: _RuntimeAdapter) -> tuple[Path, ...]:
    lexical_cwd = Path.cwd()
    physical_cwd = adapter.resolve_repository_path(lexical_cwd)
    if not adapter.same_file(lexical_cwd, physical_cwd):
        raise OSError("unstable current repository identity")

    checked: list[
        tuple[
            Path,
            tuple[int, int, int, int],
            tuple[str, tuple[int, int, int, int]] | None,
        ]
    ] = []
    current = physical_cwd
    repository_roots: list[Path] = []
    while True:
        path_identity = adapter.repository_path_identity(current)
        marker = adapter.repository_marker(current)
        checked.append((current, path_identity, marker))
        if marker is not None:
            marker_kind, _ = marker
            if marker_kind not in {"directory", "file"}:
                raise OSError("invalid repository marker")
            repository_roots.append(current)
        if current.parent == current:
            break
        current = current.parent

    final_cwd = adapter.resolve_repository_path(Path.cwd())
    if not adapter.same_file(physical_cwd, final_cwd):
        raise OSError("unstable current repository identity")
    for path, initial_identity, initial_marker in reversed(checked):
        if (
            adapter.repository_path_identity(path) != initial_identity
            or adapter.repository_marker(path) != initial_marker
        ):
            raise OSError("unstable repository parent identity")
    return tuple(repository_roots) if repository_roots else (physical_cwd,)


def _current_repository_roots(adapter: _RuntimeAdapter) -> tuple[Path, ...]:
    worktree = Path(__file__).resolve(strict=True).parents[2]
    candidates = [worktree]
    if worktree.parent.name.casefold() == ".worktrees":
        candidates.append(worktree.parent.parent.resolve(strict=True))
    candidates.extend(_current_repository_chain_roots(adapter))
    roots: list[Path] = []
    for candidate in candidates:
        if not adapter.same_file(candidate, candidate):
            raise OSError("unstable repository root identity")
        if any(adapter.same_file(candidate, existing) for existing in roots):
            continue
        roots.append(candidate)
    return tuple(roots)


def _inside_repository_by_identity(
    path: Path,
    repository_roots: tuple[Path, ...],
    adapter: _RuntimeAdapter,
) -> bool:
    current = path
    while True:
        for root in repository_roots:
            if adapter.same_file(current, root):
                return True
        if current.parent == current:
            return False
        current = current.parent


def _is_priority_failure(error: BaseException) -> bool:
    return isinstance(error, MemoryError) or not isinstance(error, Exception)


def _is_expected_runtime_failure(error: BaseException) -> bool:
    return isinstance(error, OSError)


def _raise_native_codex_unavailable() -> None:
    raise OSError("native Codex payload is unavailable") from None


def _discover_locked_native_codex_raw(
    *,
    which: Callable[[str], str | None],
    repository_roots: tuple[Path, ...] | None,
    adapter_override: _RuntimeAdapter | None,
) -> LockedNativeCodex:
    raw_locator = which("codex.cmd")
    if (
        type(raw_locator) is not str
        or not raw_locator
        or "\x00" in raw_locator
    ):
        _raise_native_codex_unavailable()
    locator = Path(raw_locator)
    if not locator.is_absolute() or locator.name.casefold() != "codex.cmd":
        _raise_native_codex_unavailable()

    expected = locator.parent / NATIVE_CODEX_RELATIVE_PATH
    adapter = adapter_override if adapter_override is not None else _WindowsRuntimeAdapter()
    descriptor = adapter.open_locked(expected)
    primary_error: BaseException | None = None
    primary_traceback = None
    locked: LockedNativeCodex | None = None
    try:
        roots = (
            _current_repository_roots(adapter)
            if repository_roots is None
            else repository_roots
        )
        if not adapter.is_disk_regular_non_reparse(descriptor):
            raise OSError("native Codex payload is unavailable")
        initial_identity = adapter.identity(descriptor)
        if initial_identity.size < 0:
            raise OSError("native Codex payload is unavailable")
        final_path = adapter.final_path(descriptor)
        if (
            not _final_path_has_fixed_layout(final_path, locator.parent, adapter)
            or not adapter.same_file(final_path, expected)
            or _inside_repository_by_identity(final_path, roots, adapter)
        ):
            raise OSError("native Codex payload is unavailable")
        publisher = adapter.verify_publisher(descriptor, final_path)
        if publisher != OPENAI_AUTHENTICODE_PUBLISHER:
            raise OSError("native Codex payload is unavailable")
        final_identity = adapter.identity(descriptor)
        if final_identity != initial_identity:
            raise OSError("native Codex payload is unavailable")
        locked = LockedNativeCodex(
            descriptor=descriptor,
            identity=initial_identity,
            size=initial_identity.size,
            publisher=publisher,
            _adapter=adapter,
        )
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    if primary_error is None:
        assert locked is not None
        return locked

    cleanup_error: BaseException | None = None
    cleanup_traceback = None
    try:
        adapter.close(descriptor)
    except BaseException as error:
        cleanup_error = error
        cleanup_traceback = error.__traceback__

    if _is_priority_failure(primary_error):
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None and _is_priority_failure(cleanup_error):
        raise cleanup_error.with_traceback(cleanup_traceback)
    if not _is_expected_runtime_failure(primary_error):
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None:
        if not _is_expected_runtime_failure(cleanup_error):
            raise cleanup_error.with_traceback(cleanup_traceback)
    raise primary_error.with_traceback(primary_traceback)


def discover_locked_native_codex(
    *,
    which: Callable[[str], str | None] = shutil.which,
    repository_roots: tuple[Path, ...] | None = None,
    _adapter: _RuntimeAdapter | None = None,
) -> LockedNativeCodex:
    """Return a verified native payload while retaining its source handle lock."""

    failed = False
    try:
        if sys.platform == "darwin" and os.name != "nt":
            from .macos_runtime import discover_native_codex

            return discover_native_codex(
                which=which,
                repository_roots=repository_roots,
                _adapter=_adapter,
            )
        return _discover_locked_native_codex_raw(
            which=which,
            repository_roots=repository_roots,
            adapter_override=_adapter,
        )
    except BaseException as error:
        if _is_priority_failure(error) or not _is_expected_runtime_failure(error):
            raise
        failed = True
    if failed:
        del which, repository_roots, _adapter
        _raise_native_codex_unavailable()
    raise AssertionError("unreachable")



if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", _FILETIME),
            ("ftLastAccessTime", _FILETIME),
            ("ftLastWriteTime", _FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", ctypes.c_void_p),
        ]

    class _WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    class _CRYPT_PROVIDER_CERT(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pCert", ctypes.c_void_p),
            ("fCommercial", wintypes.BOOL),
            ("fTrustedRoot", wintypes.BOOL),
            ("fSelfSigned", wintypes.BOOL),
            ("fTestCert", wintypes.BOOL),
            ("dwRevokedReason", wintypes.DWORD),
            ("dwConfidence", wintypes.DWORD),
            ("dwError", wintypes.DWORD),
            ("pTrustListContext", ctypes.c_void_p),
            ("fTrustListSignerCert", wintypes.BOOL),
            ("pCtlContext", ctypes.c_void_p),
            ("dwCtlError", wintypes.DWORD),
            ("fIsCyclic", wintypes.BOOL),
            ("pChainElement", ctypes.c_void_p),
        ]

    class _CRYPT_PROVIDER_SGNR(ctypes.Structure):
        pass

    _CRYPT_PROVIDER_SGNR._fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("sftVerifyAsOf", _FILETIME),
        ("csCertChain", wintypes.DWORD),
        ("pasCertChain", ctypes.POINTER(_CRYPT_PROVIDER_CERT)),
        ("dwSignerType", wintypes.DWORD),
        ("psSigner", ctypes.c_void_p),
        ("dwError", wintypes.DWORD),
        ("csCounterSigners", wintypes.DWORD),
        ("pasCounterSigners", ctypes.POINTER(_CRYPT_PROVIDER_SGNR)),
        ("pChainContext", ctypes.c_void_p),
    ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileType.restype = wintypes.DWORD
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    _kernel32.SetFilePointerEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL

    _wintrust.WinVerifyTrust.argtypes = [
        wintypes.HWND, ctypes.POINTER(_GUID), ctypes.c_void_p
    ]
    _wintrust.WinVerifyTrust.restype = ctypes.c_long
    _wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    _wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    _wintrust.WTHelperGetProvSignerFromChain.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    _wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(
        _CRYPT_PROVIDER_SGNR
    )

    _crypt32.CertGetNameStringW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
    ]
    _crypt32.CertGetNameStringW.restype = wintypes.DWORD


def _raise_last_windows_error(operation: str) -> None:
    if os.name != "nt":
        raise OSError(f"{operation} is only available on Windows")
    error = ctypes.get_last_error()
    raise OSError(error, operation)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


class _WindowsRuntimeAdapter:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("native Codex payload is only available on Windows")

    def open_locked(self, path: Path) -> int:
        handle = _kernel32.CreateFileW(
            str(path), 0x80000000, 0x00000001, None, 3, 0x00200000, None
        )
        if handle == _INVALID_HANDLE_VALUE:
            _raise_last_windows_error("could not lock native Codex payload")
        return int(handle)

    def _information(self, descriptor: int) -> _BY_HANDLE_FILE_INFORMATION:
        information = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(descriptor), ctypes.byref(information)
        ):
            _raise_last_windows_error("could not inspect native Codex payload")
        return information

    def is_disk_regular_non_reparse(self, descriptor: int) -> bool:
        if _kernel32.GetFileType(wintypes.HANDLE(descriptor)) != 0x0001:
            return False
        attributes = self._information(descriptor).dwFileAttributes
        return not attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT)

    def identity(self, descriptor: int) -> NativeCodexIdentity:
        information = self._information(descriptor)
        file_index = (information.nFileIndexHigh << 32) | information.nFileIndexLow
        size = (information.nFileSizeHigh << 32) | information.nFileSizeLow
        mtime_100ns = (
            information.ftLastWriteTime.dwHighDateTime << 32
        ) | information.ftLastWriteTime.dwLowDateTime
        return NativeCodexIdentity(
            volume_serial=information.dwVolumeSerialNumber,
            file_index=file_index,
            size=size,
            mtime_ns=mtime_100ns * 100,
        )

    def final_path(self, descriptor: int) -> Path:
        handle = wintypes.HANDLE(descriptor)
        required = _kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not required:
            _raise_last_windows_error("could not resolve native Codex payload")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = _kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            _raise_last_windows_error("could not resolve native Codex payload")
        return Path(buffer.value)

    def verify_publisher(self, descriptor: int, path: Path) -> str:
        return _verify_wintrust_publisher(descriptor, path)

    def read(self, descriptor: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not _kernel32.ReadFile(
            wintypes.HANDLE(descriptor), buffer, size, ctypes.byref(read), None
        ):
            _raise_last_windows_error("could not read native Codex payload")
        return buffer.raw[: read.value]

    def rewind(self, descriptor: int) -> None:
        if not _kernel32.SetFilePointerEx(
            wintypes.HANDLE(descriptor), 0, None, 0
        ):
            _raise_last_windows_error("could not rewind native Codex payload")

    def close(self, descriptor: int) -> None:
        if not _kernel32.CloseHandle(wintypes.HANDLE(descriptor)):
            _raise_last_windows_error("could not close native Codex payload")

    def same_file(self, left: Path, right: Path) -> bool:
        before_left = left.stat(follow_symlinks=False)
        before_right = right.stat(follow_symlinks=False)
        same = os.path.samefile(left, right)
        after_left = left.stat(follow_symlinks=False)
        after_right = right.stat(follow_symlinks=False)
        for before, after in ((before_left, after_left), (before_right, after_right)):
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError("unstable native Codex path identity")
        return same

    def repository_marker(
        self, root: Path,
    ) -> tuple[str, tuple[int, int, int, int]] | None:
        marker = root / ".git"
        before_root = root.stat(follow_symlinks=False)
        try:
            before_marker = marker.stat(follow_symlinks=False)
        except FileNotFoundError:
            after_root = root.stat(follow_symlinks=False)
            try:
                marker.stat(follow_symlinks=False)
            except FileNotFoundError:
                if _stat_identity(before_root) != _stat_identity(after_root):
                    raise OSError("unstable repository marker identity")
                return None
            raise OSError("unstable repository marker identity") from None

        attributes = getattr(before_marker, "st_file_attributes", 0)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("invalid repository marker")
        if stat.S_ISDIR(before_marker.st_mode):
            marker_kind = "directory"
        elif stat.S_ISREG(before_marker.st_mode):
            marker_kind = "file"
        else:
            raise OSError("invalid repository marker")

        after_marker = marker.stat(follow_symlinks=False)
        after_root = root.stat(follow_symlinks=False)
        if (
            _stat_identity(before_root) != _stat_identity(after_root)
            or _stat_identity(before_marker) != _stat_identity(after_marker)
        ):
            raise OSError("unstable repository marker identity")
        return marker_kind, _stat_identity(after_marker)

    def resolve_repository_path(self, path: Path) -> Path:
        before = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
        if not self.same_file(path, resolved):
            raise OSError("unstable current repository identity")
        after = path.stat(follow_symlinks=False)
        if _stat_identity(before) != _stat_identity(after):
            raise OSError("unstable current repository identity")
        return resolved

    def repository_path_identity(self, path: Path) -> tuple[int, int, int, int]:
        value = path.stat(follow_symlinks=False)
        attributes = getattr(value, "st_file_attributes", 0)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT or not stat.S_ISDIR(value.st_mode):
            raise OSError("invalid repository parent")
        return _stat_identity(value)


def _certificate_publisher(certificate: object) -> str:
    oid = ctypes.c_char_p(b"2.5.4.10")
    required = _crypt32.CertGetNameStringW(certificate, 3, 0, oid, None, 0)
    if required <= 1:
        _raise_last_windows_error("could not read native Codex publisher")
    publisher = ctypes.create_unicode_buffer(required)
    if _crypt32.CertGetNameStringW(
        certificate, 3, 0, oid, publisher, required
    ) != required:
        _raise_last_windows_error("could not read native Codex publisher")
    return publisher.value


def _publisher_from_trust_state(state: object) -> str:
    provider = _wintrust.WTHelperProvDataFromStateData(state)
    if not provider:
        _raise_last_windows_error("could not inspect native Codex trust state")
    signer = _wintrust.WTHelperGetProvSignerFromChain(provider, 0, False, 0)
    if (
        not signer
        or signer.contents.csCertChain < 1
        or not signer.contents.pasCertChain
        or not signer.contents.pasCertChain[0].pCert
    ):
        _raise_last_windows_error("could not inspect native Codex signer")
    return _certificate_publisher(signer.contents.pasCertChain[0].pCert)


def _raise_signature_not_trusted() -> None:
    raise OSError("native Codex signature is not trusted") from None


def _verify_wintrust_publisher_raw(descriptor: int, path: Path) -> str:
    action = _GUID(
        0x00AAC56B, 0xCD44, 0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = _WINTRUST_FILE_INFO(
        ctypes.sizeof(_WINTRUST_FILE_INFO), str(path), wintypes.HANDLE(descriptor), None
    )
    trust_data = _WINTRUST_DATA()
    trust_data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
    trust_data.dwUIChoice = 2
    trust_data.fdwRevocationChecks = 0
    trust_data.dwUnionChoice = 1
    trust_data.pFile = ctypes.pointer(file_info)
    trust_data.dwStateAction = 1
    trust_data.dwProvFlags = 0x00001000
    publisher: str | None = None
    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        result = _wintrust.WinVerifyTrust(
            wintypes.HWND(_INVALID_HANDLE_VALUE), ctypes.byref(action), ctypes.byref(trust_data)
        )
        if result != 0:
            raise OSError(result & 0xFFFFFFFF, "native Codex signature is not trusted")
        publisher = _publisher_from_trust_state(trust_data.hWVTStateData)
        if type(publisher) is not str or not publisher:
            raise OSError("native Codex signature publisher is invalid")
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    cleanup_error: BaseException | None = None
    cleanup_traceback = None
    try:
        trust_data.dwStateAction = 2
        close_result = _wintrust.WinVerifyTrust(
            wintypes.HWND(_INVALID_HANDLE_VALUE), ctypes.byref(action), ctypes.byref(trust_data)
        )
        if close_result != 0:
            raise OSError(close_result & 0xFFFFFFFF, "could not close Codex trust state")
    except BaseException as error:
        cleanup_error = error
        cleanup_traceback = error.__traceback__

    if primary_error is not None:
        if _is_priority_failure(primary_error):
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_error is not None and _is_priority_failure(cleanup_error):
            raise cleanup_error.with_traceback(cleanup_traceback)
        if not _is_expected_runtime_failure(primary_error):
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_error is not None and not _is_expected_runtime_failure(cleanup_error):
            raise cleanup_error.with_traceback(cleanup_traceback)
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_traceback)
    assert publisher is not None
    return publisher


def _verify_wintrust_publisher(descriptor: int, path: Path) -> str:
    failed = False
    try:
        return _verify_wintrust_publisher_raw(descriptor, path)
    except BaseException as error:
        if _is_priority_failure(error) or not _is_expected_runtime_failure(error):
            raise
        failed = True
    if failed:
        del descriptor, path
        _raise_signature_not_trusted()
    raise AssertionError("unreachable")


_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_TRUSTED_INSTALLER_SID = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)
_ANCESTOR_REPLACEMENT_RIGHTS = (
    0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)


def _validate_windows_cache_ancestor_acl(
    *,
    owner: str,
    entries: tuple[tuple[str, int, int, int], ...],
    user_sid: str,
    dacl_protected: bool,
    trusted_installer_allowed: bool,
) -> None:
    if type(dacl_protected) is not bool or type(trusted_installer_allowed) is not bool:
        raise OSError("private runtime ancestor ACL is unsafe")
    trusted = {
        user_sid,
        _SYSTEM_SID,
        _ADMINISTRATORS_SID,
    }
    if trusted_installer_allowed:
        trusted.add(_TRUSTED_INSTALLER_SID)
    if owner not in trusted:
        raise OSError("private runtime ancestor owner is unsafe")
    for sid, mask, flags, ace_type in entries:
        if ace_type == 1:
            continue
        if ace_type != 0:
            raise OSError("private runtime ancestor ACE is unsafe")
        if flags & 0x08 or sid in trusted:
            continue
        if mask & _ANCESTOR_REPLACEMENT_RIGHTS:
            raise OSError("private runtime ancestor ACL is unsafe")


def _validate_windows_private_directory_acl(
    *,
    owner: str,
    entries: tuple[tuple[str, int, int, int], ...],
    user_sid: str,
    dacl_protected: bool,
) -> None:
    if type(dacl_protected) is not bool:
        raise OSError("private runtime directory ACL is unsafe")
    trusted = {user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    full_control = 0x001F01FF
    principals = {sid for sid, _, _, _ in entries}
    if (
        owner != user_sid
        or not dacl_protected
        or user_sid not in principals
        or not principals <= trusted
        or any(
            ace_type != 0
            or flags & 0x10
            or flags & 0x03 != 0x03
            or mask & full_control != full_control
            for _, mask, flags, ace_type in entries
        )
    ):
        raise OSError("private runtime directory ACL is unsafe")


def _windows_current_profile_directory() -> Path:
    if os.name != "nt":
        raise OSError("Windows profile directory is unavailable")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.DWORD()
        userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(length))
        if length.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(length.value)
        if not userenv.GetUserProfileDirectoryW(
            token, buffer, ctypes.byref(length),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value).resolve(strict=True)
    finally:
        kernel.CloseHandle(token)


def _trusted_installer_allowed_for_ancestor(current: Path, root: Path) -> bool:
    if current.parent == current:
        return True
    try:
        canonical_profile = _windows_current_profile_directory()
        standard_local = (canonical_profile / "AppData" / "Local").resolve(
            strict=True,
        )
        return (
            root.parent.parent.resolve(strict=True) == standard_local
            and (current == standard_local or current in standard_local.parents)
        )
    except OSError:
        return False


class _WindowsCacheRuntimeAdapter:
    def __init__(self, *, _runtime_adapter: _RuntimeAdapter | None = None) -> None:
        if os.name != "nt":
            raise OSError("private Codex runtime is only available on Windows")
        self._runtime = _runtime_adapter or _WindowsRuntimeAdapter()

    def validate_cache_ancestor_chain(self, root: Path) -> None:
        from .private_paths import (
            _current_user_sid,
            _is_link_or_reparse,
            _windows_descriptor,
        )

        user_sid = _current_user_sid()
        current = root.parent.parent
        while True:
            before = current.stat(follow_symlinks=False)
            if _is_link_or_reparse(current) or not stat.S_ISDIR(before.st_mode):
                raise OSError("private runtime ancestor is unsafe")
            canonical = current.resolve(strict=True)
            canonical_stat = canonical.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (
                canonical_stat.st_dev,
                canonical_stat.st_ino,
            ):
                raise OSError("private runtime ancestor is unstable")
            descriptor = _windows_descriptor(canonical)
            _validate_windows_cache_ancestor_acl(
                owner=descriptor[0],
                entries=descriptor[1],
                user_sid=user_sid,
                dacl_protected=descriptor[2],
                trusted_installer_allowed=(
                    _trusted_installer_allowed_for_ancestor(canonical, root)
                ),
            )
            after = current.stat(follow_symlinks=False)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or _windows_descriptor(canonical) != descriptor
            ):
                raise OSError("private runtime ancestor is unstable")
            if current.parent == current:
                return
            current = current.parent

    def prepare_private_directory(self, path: Path) -> Path:
        from .private_paths import PrivatePathError, prepare_private_directory

        try:
            return prepare_private_directory(path)
        except (OSError, PrivatePathError):
            raise OSError("private runtime directory is unsafe") from None

    def validate_private_directory(self, path: Path) -> None:
        from .private_paths import (
            PrivatePathError,
            _current_user_sid,
            _windows_descriptor,
            prepare_private_directory,
        )

        try:
            before = path.stat(follow_symlinks=False)
            prepared = prepare_private_directory(path)
            descriptor = _windows_descriptor(prepared)
            _validate_windows_private_directory_acl(
                owner=descriptor[0],
                entries=descriptor[1],
                user_sid=_current_user_sid(),
                dacl_protected=descriptor[2],
            )
            after = prepared.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or _windows_descriptor(prepared) != descriptor
            ):
                raise OSError("private runtime directory is unsafe")
        except (OSError, PrivatePathError):
            raise OSError("private runtime directory is unsafe") from None

    def protect_private_file(self, path: Path) -> None:
        from .setup_store import _protect_private_file

        _protect_private_file(path)

    def validate_private_file(self, path: Path) -> tuple[int, int]:
        from .setup_store import _validate_regular_file

        return _validate_regular_file(path)

    def read_private_text(self, path: Path) -> str:
        from .setup_store import _read_nofollow

        return _read_nofollow(path)

    def inspect_private_executable(
        self, path: Path,
    ) -> tuple[NativeCodexIdentity, str]:
        descriptor = self._runtime.open_locked(path)
        primary: BaseException | None = None
        primary_traceback = None
        result: tuple[NativeCodexIdentity, str] | None = None
        try:
            if not self._runtime.is_disk_regular_non_reparse(descriptor):
                raise OSError("private Codex executable is unsafe")
            initial = self._runtime.identity(descriptor)
            final_path = self._runtime.final_path(descriptor)
            if not self._runtime.same_file(final_path, path):
                raise OSError("private Codex executable changed")
            publisher = self._runtime.verify_publisher(descriptor, final_path)
            if publisher != OPENAI_AUTHENTICODE_PUBLISHER:
                raise OSError("private Codex publisher is invalid")
            if self._runtime.identity(descriptor) != initial:
                raise OSError("private Codex executable changed")
            result = initial, publisher
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        cleanup: BaseException | None = None
        cleanup_traceback = None
        try:
            self._runtime.close(descriptor)
        except BaseException as error:
            cleanup = error
            cleanup_traceback = error.__traceback__
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def fsync_directory(self, path: Path) -> None:
        handle = _kernel32.CreateFileW(
            str(path),
            0x40000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            _raise_last_windows_error("could not open private runtime directory")
        descriptor = int(handle)
        primary: BaseException | None = None
        try:
            if not _kernel32.FlushFileBuffers(wintypes.HANDLE(descriptor)):
                _raise_last_windows_error("could not flush private runtime directory")
        except BaseException as error:
            primary = error
        try:
            self._runtime.close(descriptor)
        except BaseException:
            if primary is None:
                raise
        if primary is not None:
            raise primary

    def smoke(
        self, executable: Path, *, environment: dict[str, str], timeout: float,
    ) -> None:
        _run_bounded_smoke(executable, environment=environment, timeout=timeout)


def _run_bounded_smoke(
    executable: Path, *, environment: dict[str, str], timeout: float,
) -> None:
    process, tree = _start_smoke_process(
        [str(executable), "--version"], executable.parent, environment,
    )
    output = bytearray()
    overflow = threading.Event()
    reader_errors: list[BaseException] = []

    def drain() -> None:
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                if len(output) + len(chunk) > 64 * 1024:
                    overflow.set()
                    return
                output.extend(chunk)
        except BaseException as error:
            reader_errors.append(error)

    reader: threading.Thread | None = None
    try:
        reader = threading.Thread(
            target=drain,
            daemon=True,
            name="ones-dev-codex-smoke-output",
        )
        reader.start()
    except BaseException as error:
        cleanup_errors = _cleanup_failed_smoke_start(process, reader, tree)
        selected = _select_smoke_failure(error, [], cleanup_errors)
        assert selected is not None
        raise selected
    assert reader is not None
    primary: BaseException | None = None
    primary_traceback = None
    returncode: int | None = None
    try:
        returncode = process.wait(timeout=timeout)
    except BaseException as error:
        primary = (
            OSError("private Codex runtime smoke test failed")
            if isinstance(error, subprocess.TimeoutExpired)
            else error
        )
        primary_traceback = primary.__traceback__

    if primary is None:
        try:
            reader.join(timeout=1.0)
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
    failed = (
        primary is not None
        or bool(reader_errors)
        or reader.is_alive()
        or overflow.is_set()
        or returncode != 0
    )
    cleanup_errors: list[BaseException] = []
    if failed:
        try:
            _terminate_smoke_process(process, tree)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            process.wait(timeout=1.0)
        except BaseException as error:
            cleanup_errors.append(error)
    try:
        if process.stdout is not None:
            process.stdout.close()
    except BaseException as error:
        cleanup_errors.append(error)
    try:
        reader.join(timeout=1.0)
    except BaseException as error:
        cleanup_errors.append(error)
    if reader.is_alive():
        cleanup_errors.append(OSError("private Codex smoke reader did not stop"))
    try:
        tree.close()
    except BaseException as error:
        cleanup_errors.append(error)

    operational = list(reader_errors)
    if failed and primary is None and not operational:
        operational.append(OSError("private Codex runtime smoke test failed"))
    selected = _select_smoke_failure(primary, operational, cleanup_errors)
    if selected is not None:
        if primary is selected and primary_traceback is not None:
            raise selected.with_traceback(primary_traceback)
        raise selected


def _select_smoke_failure(
    primary: BaseException | None,
    operational: list[BaseException],
    cleanup: list[BaseException],
) -> BaseException | None:
    if primary is not None and _is_priority_failure(primary):
        return primary
    for error in operational:
        if _is_priority_failure(error):
            return error
    for error in cleanup:
        if _is_priority_failure(error):
            return error
    if primary is not None:
        return primary
    if operational:
        return operational[0]
    if cleanup:
        return cleanup[0]
    return None


def _cleanup_failed_smoke_start(
    process: subprocess.Popen[bytes],
    reader: threading.Thread | None,
    tree: object,
) -> list[BaseException]:
    errors: list[BaseException] = []
    try:
        _terminate_smoke_process(process, tree)
    except BaseException as error:
        errors.append(error)
    try:
        process.wait(timeout=1.0)
    except BaseException as error:
        errors.append(error)
    try:
        if process.stdout is not None:
            process.stdout.close()
    except BaseException as error:
        errors.append(error)
    if reader is not None:
        try:
            reader.join(timeout=1.0)
        except BaseException as error:
            errors.append(error)
    try:
        tree.close()  # type: ignore[attr-defined]
    except BaseException as error:
        errors.append(error)
    return errors


def _start_smoke_process(
    command: list[str], cwd: Path, environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], object]:
    # Lazy import avoids a module cycle: the runner imports this preparation
    # layer, while smoke tests reuse its already-hardened process-tree launcher.
    from .codex_runner import _start_isolated_process

    return _start_isolated_process(
        command,
        cwd=cwd,
        env=environment,
        pipe_stdin=False,
        merge_stderr=True,
    )


def _terminate_smoke_process(
    process: subprocess.Popen[bytes], tree: object,
) -> None:
    from .codex_runner import _terminate

    _terminate(process, tree)  # type: ignore[arg-type]

__all__ = [
    "OPENAI_AUTHENTICODE_PUBLISHER",
    "OPENAI_MACOS_TEAM_IDENTIFIER",
    "OPENAI_MACOS_CODESIGN_PUBLISHER",
    "NATIVE_CODEX_RELATIVE_PATH",
    "LockedNativeCodex",
    "NativeCodexIdentity",
    "CodexRuntimePreparer",
    "discover_locked_native_codex",
]
