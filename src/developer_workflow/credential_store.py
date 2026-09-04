"""Windows Credential Manager storage for developer-workflow secrets."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import hashlib
import os
import re
import sys
from collections.abc import Callable, Iterator
from threading import RLock
from typing import TYPE_CHECKING, ContextManager, Protocol, TypeVar, cast, runtime_checkable
import unicodedata

from .setup_models import RuntimeSecrets, SecretKind

if TYPE_CHECKING:
    from .platform_support import HostPaths


CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BLOB_SIZE = 2560
_TARGET_PREFIX = "ones-dev"
_GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}")
_PROFILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ERROR_MESSAGE = "credential operation failed"
_GENERATION_WRITE_LOCK = RLock()
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 0x102
_WAIT_FAILED = 0xFFFFFFFF
_MUTEX_TIMEOUT_MILLISECONDS = 30_000

_T = TypeVar("_T")
_GenerationLockFactory = Callable[[str, str], ContextManager[None]]


class CredentialStoreError(RuntimeError):
    """Raised without backend, target, or credential details."""


@dataclass(slots=True, repr=False)
class _CredentialRecord:
    target: str
    credential_type: int
    blob: bytearray = field(repr=False)


class _CredentialBackend(Protocol):
    def write_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> None: ...

    def read_generic(self, target: str) -> _CredentialRecord | None: ...

    def delete_generic(self, target: str) -> bool: ...

    def list_generic_targets(self, prefix: str) -> tuple[str, ...]: ...


@runtime_checkable
class CredentialStore(Protocol):
    def write(
        self, profile_id: str, generation: str, kind: SecretKind, value: str
    ) -> None: ...

    def read(self, profile_id: str, generation: str, kind: SecretKind) -> str: ...

    def delete(self, profile_id: str, generation: str, kind: SecretKind) -> None: ...

    def write_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> None: ...

    def write_fresh_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool: ...

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets: ...

    def delete_generation(self, profile_id: str, generation: str) -> None: ...

    def list_generations(self, profile_id: str) -> tuple[str, ...]: ...


def _fail() -> CredentialStoreError:
    return CredentialStoreError(_ERROR_MESSAGE)


def _validate_profile(profile_id: str) -> str:
    if type(profile_id) is not str or _PROFILE_PATTERN.fullmatch(profile_id) is None:
        raise _fail()
    return profile_id


def _validate_generation(generation: str) -> str:
    if type(generation) is not str or _GENERATION_PATTERN.fullmatch(generation) is None:
        raise _fail()
    return generation


def _validate_kind(kind: SecretKind) -> SecretKind:
    if type(kind) is not SecretKind:
        raise _fail()
    return kind


def credential_target(
    profile_id: str, generation: str, kind: SecretKind
) -> str:
    """Derive a credential target exclusively from canonical bounded segments."""

    profile = _validate_profile(profile_id)
    canonical_generation = _validate_generation(generation)
    canonical_kind = _validate_kind(kind)
    return f"{_TARGET_PREFIX}/{profile}/{canonical_generation}/{canonical_kind.value}"


def _validate_secret(value: str) -> bytearray:
    if type(value) is not str or not value:
        raise _fail()
    forbidden_categories = {"Cc", "Cf", "Cs", "Zl", "Zp"}
    if any(
        unicodedata.category(character) in forbidden_categories for character in value
    ):
        raise _fail()
    try:
        raw = bytearray(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise _fail() from None
    if not raw or len(raw) > _MAX_CREDENTIAL_BLOB_SIZE:
        _zero(raw)
        raise _fail()
    return raw


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _safe_backend_call(operation: Callable[[], _T]) -> _T:
    failed = False
    result: _T | None = None
    try:
        result = operation()
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        failed = True
    if failed:
        # Raising outside the handler prevents the sensitive backend exception from
        # surviving in either __cause__ or __context__.
        raise _fail()
    return cast(_T, result)


def _mutex_cleanup_succeeded(operation: Callable[[], object]) -> bool:
    failed = False
    result = False
    try:
        result = bool(operation())
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        failed = True
    return not failed and result


def _in_process_generation_lock(
    profile_id: str, generation: str
) -> ContextManager[None]:
    del profile_id, generation
    return nullcontext()


def _validated_generation_write(
    profile_id: str, generation: str, secrets: RuntimeSecrets
) -> tuple[tuple[tuple[SecretKind, str], ...], str, str, str]:
    if type(secrets) is not RuntimeSecrets:
        raise _fail()
    entries = tuple(secrets.values.items())
    if not entries:
        raise _fail()
    for kind, value in entries:
        credential_target(profile_id, generation, kind)
        probe = _validate_secret(value)
        _zero(probe)
    profile = _validate_profile(profile_id)
    canonical_generation = _validate_generation(generation)
    prefix = f"{_TARGET_PREFIX}/{profile}/{canonical_generation}/"
    return entries, profile, canonical_generation, prefix


def create_credential_store(
    *, platform_name: str | None = None, paths: HostPaths | None = None
) -> CredentialStore:
    """选择当前主机的原生凭据库，绝不回退到明文存储。

    macOS 使用 ``paths`` 中显式注入的 owner-only 锁目录；生产调用省略该
    参数时由平台路径真源解析系统账户 home。平台不受支持时 fail-closed。
    """

    platform = platform_name or sys.platform
    if platform == "darwin":
        from .macos_credentials import MacOSCredentialStore

        if paths is None:
            return _safe_backend_call(MacOSCredentialStore)
        return _safe_backend_call(
            lambda: MacOSCredentialStore(lock_root=paths.credential_lock_root)
        )
    if platform == "win32":
        return _safe_backend_call(WindowsCredentialStore)
    raise _fail()


class WindowsCredentialStore:
    """Store secrets under app-owned Windows generic-credential targets."""

    __slots__ = ("_backend", "_lock_factory")

    def __init__(
        self,
        backend: _CredentialBackend | None = None,
        *,
        lock_factory: _GenerationLockFactory | None = None,
    ) -> None:
        if backend is None:
            self._backend = cast(
                _CredentialBackend,
                _safe_backend_call(_CtypesWindowsCredentialBackend),
            )
            self._lock_factory = lock_factory or _windows_generation_lock
        else:
            self._backend = backend
            self._lock_factory = lock_factory or _in_process_generation_lock

    def write(
        self, profile_id: str, generation: str, kind: SecretKind, value: str
    ) -> None:
        target = credential_target(profile_id, generation, kind)
        raw = _validate_secret(value)
        try:
            _safe_backend_call(
                lambda: self._backend.write_generic(
                    target, raw, persist="local_machine"
                )
            )
        finally:
            _zero(raw)

    def read(self, profile_id: str, generation: str, kind: SecretKind) -> str:
        target = credential_target(profile_id, generation, kind)
        record = _safe_backend_call(lambda: self._backend.read_generic(target))
        if record is None or type(record) is not _CredentialRecord:
            raise _fail()
        raw = record.blob
        try:
            if (
                type(record.target) is not str
                or record.target != target
                or type(record.credential_type) is not int
                or record.credential_type != CRED_TYPE_GENERIC
                or type(raw) is not bytearray
                or not raw
                or len(raw) > _MAX_CREDENTIAL_BLOB_SIZE
                or 0 in raw
            ):
                raise _fail()
            try:
                value = raw.decode("utf-8", errors="strict")
            except UnicodeError:
                raise _fail() from None
            # Apply the same Unicode policy to untrusted data returned by the OS.
            validated = _validate_secret(value)
            try:
                return value
            finally:
                _zero(validated)
        finally:
            if type(raw) is bytearray:
                _zero(raw)

    def delete(self, profile_id: str, generation: str, kind: SecretKind) -> None:
        target = credential_target(profile_id, generation, kind)
        _safe_backend_call(lambda: self._backend.delete_generic(target))

    def write_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> None:
        if type(secrets) is not RuntimeSecrets:
            raise _fail()
        # Validate every target before the first mutation.
        entries = tuple(secrets.values.items())
        if not entries:
            raise _fail()
        for kind, value in entries:
            credential_target(profile_id, generation, kind)
            probe = _validate_secret(value)
            _zero(probe)
        profile = _validate_profile(profile_id)
        canonical_generation = _validate_generation(generation)
        prefix = f"{_TARGET_PREFIX}/{profile}/{canonical_generation}/"
        expected_targets = {
            credential_target(profile, canonical_generation, kind): (kind, value)
            for kind, value in entries
        }
        with _GENERATION_WRITE_LOCK:
            with self._lock_factory(profile, canonical_generation):
                targets = _safe_backend_call(
                    lambda: self._backend.list_generic_targets(prefix)
                )
                if type(targets) is not tuple:
                    raise _fail()
                if targets:
                    if len(targets) != len(expected_targets) or set(targets) != set(
                        expected_targets
                    ):
                        raise _fail()
                    for kind, value in expected_targets.values():
                        if self.read(profile, canonical_generation, kind) != value:
                            raise _fail()
                    return

                written: list[SecretKind] = []
                try:
                    for kind, value in entries:
                        self.write(profile, canonical_generation, kind, value)
                        written.append(kind)
                except BaseException:
                    for kind in written:
                        try:
                            self.delete(profile, canonical_generation, kind)
                        except CredentialStoreError:
                            pass
                    raise

    def write_fresh_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool:
        entries, profile, canonical_generation, prefix = _validated_generation_write(
            profile_id, generation, secrets
        )
        with _GENERATION_WRITE_LOCK:
            with self._lock_factory(profile, canonical_generation):
                targets = _safe_backend_call(
                    lambda: self._backend.list_generic_targets(prefix)
                )
                if type(targets) is not tuple or targets:
                    raise _fail()
                written: list[SecretKind] = []
                try:
                    for kind, value in entries:
                        self.write(profile, canonical_generation, kind, value)
                        written.append(kind)
                except BaseException:
                    for kind in written:
                        try:
                            self.delete(profile, canonical_generation, kind)
                        except CredentialStoreError:
                            pass
                    raise
        return True

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets:
        if type(kinds) is not tuple or not kinds:
            raise _fail()
        if any(type(kind) is not SecretKind for kind in kinds) or len(set(kinds)) != len(
            kinds
        ):
            raise _fail()
        values: dict[SecretKind, str] = {}
        for kind in kinds:
            values[kind] = self.read(profile_id, generation, kind)
        return RuntimeSecrets(values)

    def delete_generation(self, profile_id: str, generation: str) -> None:
        profile = _validate_profile(profile_id)
        canonical_generation = _validate_generation(generation)
        prefix = f"{_TARGET_PREFIX}/{profile}/{canonical_generation}/"
        with _GENERATION_WRITE_LOCK:
            with self._lock_factory(profile, canonical_generation):
                targets = _safe_backend_call(
                    lambda: self._backend.list_generic_targets(prefix)
                )
                if type(targets) is not tuple:
                    raise _fail()
                for target in targets:
                    parsed = _parse_target(target, expected_profile=profile)
                    if parsed is not None and parsed[0] == canonical_generation:
                        _safe_backend_call(
                            lambda target=target: self._backend.delete_generic(target)
                        )

    def list_generations(self, profile_id: str) -> tuple[str, ...]:
        profile = _validate_profile(profile_id)
        prefix = f"{_TARGET_PREFIX}/{profile}/"
        targets = _safe_backend_call(lambda: self._backend.list_generic_targets(prefix))
        if type(targets) is not tuple:
            raise _fail()
        generations: set[str] = set()
        for target in targets:
            parsed = _parse_target(target, expected_profile=profile)
            if parsed is not None:
                generations.add(parsed[0])
        return tuple(sorted(generations))


def _parse_target(
    target: object, *, expected_profile: str
) -> tuple[str, SecretKind] | None:
    if type(target) is not str:
        return None
    parts = target.split("/")
    if len(parts) != 4 or parts[0] != _TARGET_PREFIX or parts[1] != expected_profile:
        return None
    if _GENERATION_PATTERN.fullmatch(parts[2]) is None:
        return None
    try:
        kind = SecretKind(parts[3])
    except ValueError:
        return None
    return parts[2], kind


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


@contextmanager
def _windows_generation_lock(
    profile_id: str, generation: str
) -> Iterator[None]:
    if os.name != "nt":
        raise _fail()
    digest = hashlib.sha256(
        f"{profile_id}\x00{generation}".encode("ascii", errors="strict")
    ).hexdigest()
    name = f"Local\\ONESDevCredentialGeneration-{digest}"

    def load_kernel32() -> object:
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    kernel32 = _safe_backend_call(load_kernel32)
    handle = _safe_backend_call(lambda: kernel32.CreateMutexW(None, False, name))
    if not handle:
        raise _fail()
    acquired = False
    release_failed = False
    close_failed = False
    try:
        wait_result = int(
            _safe_backend_call(
                lambda: kernel32.WaitForSingleObject(
                    handle, _MUTEX_TIMEOUT_MILLISECONDS
                )
            )
        )
        if wait_result in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            # An abandoned mutex transfers ownership. The caller always performs
            # a strict enumerate/read recheck while holding it before mutation.
            acquired = True
        elif wait_result in {_WAIT_TIMEOUT, _WAIT_FAILED}:
            raise _fail()
        else:
            raise _fail()
        yield
    finally:
        try:
            if acquired and not _mutex_cleanup_succeeded(
                lambda: kernel32.ReleaseMutex(handle)
            ):
                release_failed = True
        finally:
            if not _mutex_cleanup_succeeded(lambda: kernel32.CloseHandle(handle)):
                close_failed = True
    if release_failed or close_failed:
        raise _fail()


class _CtypesWindowsCredentialBackend:
    __slots__ = ("_advapi32",)

    def __init__(self) -> None:
        if os.name != "nt":
            raise _fail()
        try:
            advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)  # type: ignore[attr-defined]
            advapi32.CredWriteW.argtypes = [
                ctypes.POINTER(_CREDENTIALW),
                wintypes.DWORD,
            ]
            advapi32.CredWriteW.restype = wintypes.BOOL
            advapi32.CredReadW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(_PCREDENTIALW),
            ]
            advapi32.CredReadW.restype = wintypes.BOOL
            advapi32.CredDeleteW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            advapi32.CredDeleteW.restype = wintypes.BOOL
            advapi32.CredEnumerateW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(ctypes.POINTER(_PCREDENTIALW)),
            ]
            advapi32.CredEnumerateW.restype = wintypes.BOOL
            advapi32.CredFree.argtypes = [ctypes.c_void_p]
            advapi32.CredFree.restype = None
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise _fail() from None
        self._advapi32 = advapi32

    def write_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> None:
        if persist != "local_machine" or not value:
            raise OSError("invalid credential input")
        buffer = (ctypes.c_ubyte * len(value)).from_buffer(value)
        credential = _CREDENTIALW()
        credential.Type = CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def read_generic(self, target: str) -> _CredentialRecord | None:
        pointer = _PCREDENTIALW()
        if not self._advapi32.CredReadW(
            target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize)
            if size < 0 or size > _MAX_CREDENTIAL_BLOB_SIZE:
                raise OSError("invalid credential blob")
            if size and not credential.CredentialBlob:
                raise OSError("invalid credential blob")
            blob = bytearray(ctypes.string_at(credential.CredentialBlob, size))
            return _CredentialRecord(
                target=credential.TargetName,
                credential_type=int(credential.Type),
                blob=blob,
            )
        finally:
            try:
                credential = pointer.contents
                size = min(int(credential.CredentialBlobSize), _MAX_CREDENTIAL_BLOB_SIZE)
                if size > 0 and credential.CredentialBlob:
                    ctypes.memset(credential.CredentialBlob, 0, size)
            finally:
                self._advapi32.CredFree(pointer)

    def delete_generic(self, target: str) -> bool:
        if self._advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return False
        raise ctypes.WinError(error)

    def list_generic_targets(self, prefix: str) -> tuple[str, ...]:
        count = wintypes.DWORD()
        credentials = ctypes.POINTER(_PCREDENTIALW)()
        if not self._advapi32.CredEnumerateW(
            prefix + "*", 0, ctypes.byref(count), ctypes.byref(credentials)
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return ()
            raise ctypes.WinError(error)
        try:
            targets: list[str] = []
            for index in range(count.value):
                credential = credentials[index].contents
                if int(credential.Type) == CRED_TYPE_GENERIC:
                    targets.append(credential.TargetName)
            return tuple(targets)
        finally:
            self._advapi32.CredFree(credentials)


__all__ = [
    "CredentialStore",
    "CredentialStoreError",
    "WindowsCredentialStore",
    "credential_target",
]
