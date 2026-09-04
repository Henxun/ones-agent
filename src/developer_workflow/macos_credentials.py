"""macOS Keychain storage for developer-workflow secrets."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import ctypes.util
import hashlib
import os
from pathlib import Path
import stat
import sys
import time
from typing import Iterator, Protocol, cast

from .credential_store import (
    CRED_TYPE_GENERIC,
    CredentialStoreError,
    WindowsCredentialStore,
    _GENERATION_WRITE_LOCK,
    _CredentialBackend,
    _CredentialRecord,
    _fail,
    _safe_backend_call,
    _validate_secret,
    _validated_generation_write,
    _zero,
    credential_target,
)
from .private_paths import prepare_private_directory
from .platform_support import default_host_paths
from .setup_models import RuntimeSecrets, SecretKind

if os.name == "posix":
    import fcntl


_ERR_SEC_SUCCESS = 0
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_ITEM_NOT_FOUND = -25300
_UTF8_ENCODING = 0x08000100
_MAX_KEYCHAIN_RESULT_BYTES = 2560
_LOCK_TIMEOUT_SECONDS = 30.0


class _MacOSCredentialBackend(_CredentialBackend, Protocol):
    def write_fresh_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> bool: ...


class MacOSCredentialStore(WindowsCredentialStore):
    """用 macOS Keychain 保存 secrets，并保留 generation 原子回滚语义。

    默认后端只通过 Security.framework 原生 API 传递 secret，不启动子进程，
    因而 secret 不会进入 argv。跨进程 generation 写入由 owner-only 文件锁串行化。
    """

    def __init__(
        self,
        backend: _CredentialBackend | None = None,
        *,
        lock_root: Path | None = None,
    ) -> None:
        if backend is not None:
            super().__init__(backend)
            return
        if sys.platform != "darwin":
            raise _fail()
        if lock_root is None:
            # 生产路径必须来自系统账户数据库，不能继承任务可控的 HOME。
            lock_root = default_host_paths().credential_lock_root
        root = _prepare_lock_root(lock_root)
        backend_instance = cast(
            _CredentialBackend, _safe_backend_call(_SecurityFrameworkBackend)
        )
        super().__init__(
            backend_instance,
            lock_factory=lambda profile, generation: _macos_generation_lock(
                root, profile, generation
            ),
        )

    def write_fresh_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool:
        """仅以 Keychain add 写入一个全新的 generation。

        与普通 ``write`` 的 add-or-update 不同，本操作遇到 duplicate 必须失败；
        这样即使有不遵守本进程锁的写入者，也不会覆盖既有 secret。
        """

        entries, profile, canonical_generation, prefix = _validated_generation_write(
            profile_id, generation, secrets
        )
        backend = cast(_MacOSCredentialBackend, self._backend)
        with _GENERATION_WRITE_LOCK:
            with self._lock_factory(profile, canonical_generation):
                targets = _safe_backend_call(
                    lambda: backend.list_generic_targets(prefix)
                )
                if type(targets) is not tuple or targets:
                    raise _fail()
                written: list[SecretKind] = []
                try:
                    for kind, value in entries:
                        target = credential_target(profile, canonical_generation, kind)
                        raw = _validate_secret(value)
                        try:
                            def create_fresh(
                                target: str = target, raw: bytearray = raw
                            ) -> bool:
                                return backend.write_fresh_generic(
                                    target, raw, persist="local_machine"
                                )

                            created = _safe_backend_call(create_fresh)
                        finally:
                            _zero(raw)
                        if created is not True:
                            raise _fail()
                        written.append(kind)
                except BaseException:
                    for kind in written:
                        try:
                            self.delete(profile, canonical_generation, kind)
                        except CredentialStoreError:
                            pass
                    raise
        return True


def _prepare_lock_root(path: Path) -> Path:
    try:
        candidate = Path(path).absolute()
        # ``mkdir(parents=True, mode=0o700)`` only applies ``mode`` to the
        # final component.  Prepare the shared application-support root first
        # so creating ``credential-locks`` cannot leave ``ones-dev`` at the
        # process umask default (commonly 0755), which would then be rejected
        # by SetupStore's owner-only boundary.
        prepare_private_directory(candidate.parent)
        return prepare_private_directory(candidate)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise _fail() from None


@contextmanager
def _macos_generation_lock(
    root: Path, profile_id: str, generation: str
) -> Iterator[None]:
    """用不包含 profile 明文的私有文件锁保护一个 generation。"""

    digest = hashlib.sha256(
        f"{profile_id}\x00{generation}".encode("ascii", errors="strict")
    ).hexdigest()
    descriptor = -1
    acquired = False
    cleanup_failed = False
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(root / f"generation-{digest}.lock", flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise OSError("unsafe credential lock")
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("credential lock timed out")
                time.sleep(0.05)
        yield
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise _fail() from None
    finally:
        if descriptor >= 0:
            try:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                cleanup_failed = True
            try:
                os.close(descriptor)
            except OSError:
                cleanup_failed = True
    if cleanup_failed:
        raise _fail()


class _SecurityFrameworkBackend:
    """Minimal ctypes binding for generic-password Keychain items."""

    __slots__ = ("_cf", "_security", "_constants", "_callback_refs")

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("Security.framework is unavailable")
        security_path = ctypes.util.find_library("Security")
        core_foundation_path = ctypes.util.find_library("CoreFoundation")
        if not security_path or not core_foundation_path:
            raise OSError("Security.framework is unavailable")
        self._security = ctypes.CDLL(security_path)
        self._cf = ctypes.CDLL(core_foundation_path)
        self._configure_functions()
        names = (
            "kSecClass",
            "kSecClassGenericPassword",
            "kSecAttrService",
            "kSecAttrAccount",
            "kSecValueData",
            "kSecReturnData",
            "kSecReturnAttributes",
            "kSecMatchLimit",
            "kSecMatchLimitOne",
            "kSecMatchLimitAll",
        )
        self._constants = {
            name: ctypes.c_void_p.in_dll(self._security, name).value for name in names
        }
        self._constants["kCFBooleanTrue"] = ctypes.c_void_p.in_dll(
            self._cf, "kCFBooleanTrue"
        ).value
        self._callback_refs = (
            ctypes.addressof(
                ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryKeyCallBacks")
            ),
            ctypes.addressof(
                ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryValueCallBacks")
            ),
        )
        if any(value is None for value in self._constants.values()):
            raise OSError("Security.framework constants are unavailable")

    def _configure_functions(self) -> None:
        self._security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecItemAdd.restype = ctypes.c_int32
        self._security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecItemUpdate.restype = ctypes.c_int32
        self._security.SecItemCopyMatching.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecItemCopyMatching.restype = ctypes.c_int32
        self._security.SecItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecItemDelete.restype = ctypes.c_int32
        self._cf.CFStringCreateWithBytes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        self._cf.CFStringCreateWithBytes.restype = ctypes.c_void_p
        self._cf.CFDataCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
        ]
        self._cf.CFDataCreate.restype = ctypes.c_void_p
        self._cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        self._cf.CFGetTypeID.restype = ctypes.c_ulong
        self._cf.CFArrayGetTypeID.argtypes = []
        self._cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        self._cf.CFDataGetTypeID.argtypes = []
        self._cf.CFDataGetTypeID.restype = ctypes.c_ulong
        self._cf.CFDictionaryGetTypeID.argtypes = []
        self._cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
        self._cf.CFStringGetTypeID.argtypes = []
        self._cf.CFStringGetTypeID.restype = ctypes.c_ulong
        self._cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetLength.restype = ctypes.c_long
        self._cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
        self._cf.CFDictionaryCreateMutable.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._cf.CFDictionaryCreateMutable.restype = ctypes.c_void_p
        self._cf.CFDictionarySetValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._cf.CFDictionarySetValue.restype = None
        self._cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        self._cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self._cf.CFArrayGetCount.restype = ctypes.c_long
        self._cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self._cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self._cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFStringGetLength.restype = ctypes.c_long
        self._cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self._cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None

    def _dictionary(self) -> int:
        dictionary = self._cf.CFDictionaryCreateMutable(
            None, 0, self._callback_refs[0], self._callback_refs[1]
        )
        if not dictionary:
            raise OSError("Keychain query allocation failed")
        return int(dictionary)

    def _set(self, dictionary: int, key: str, value: int) -> None:
        self._cf.CFDictionarySetValue(
            dictionary, self._constants[key], value
        )

    def _string(self, value: str) -> int:
        raw = bytearray(value.encode("utf-8", errors="strict"))
        try:
            buffer = (ctypes.c_ubyte * len(raw)).from_buffer(raw)
            result = self._cf.CFStringCreateWithBytes(
                None, buffer, len(raw), _UTF8_ENCODING, False
            )
            if not result:
                raise OSError("Keychain string allocation failed")
            return int(result)
        finally:
            for index in range(len(raw)):
                raw[index] = 0

    def _data(self, value: bytearray) -> int:
        buffer = (ctypes.c_ubyte * len(value)).from_buffer(value)
        result = self._cf.CFDataCreate(None, buffer, len(value))
        if not result:
            raise OSError("Keychain data allocation failed")
        return int(result)

    def _base_query(self, target: str | None = None) -> tuple[int, list[int]]:
        owned: list[int] = []
        try:
            query = self._dictionary()
            owned.append(query)
            self._set(
                query, "kSecClass", self._constants["kSecClassGenericPassword"]
            )
            service = self._string("ones-dev.credentials")
            owned.append(service)
            self._set(query, "kSecAttrService", service)
            if target is not None:
                account = self._string(target)
                owned.append(account)
                self._set(query, "kSecAttrAccount", account)
            return query, owned
        except BaseException:
            self._release_all(owned)
            raise

    def _release_all(self, values: list[int]) -> None:
        for value in reversed(values):
            self._cf.CFRelease(value)

    def write_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> None:
        if persist != "local_machine" or not value:
            raise OSError("invalid credential input")
        query, owned = self._base_query(target)
        try:
            data = self._data(value)
            owned.append(data)
            self._set(query, "kSecValueData", data)
            status = int(self._security.SecItemAdd(query, None))
            if status == _ERR_SEC_DUPLICATE_ITEM:
                update_query, update_owned = self._base_query(target)
                try:
                    attributes = self._dictionary()
                    update_owned.append(attributes)
                    self._set(attributes, "kSecValueData", data)
                    status = int(self._security.SecItemUpdate(update_query, attributes))
                finally:
                    self._release_all(update_owned)
            if status != _ERR_SEC_SUCCESS:
                raise OSError("Keychain write failed")
        finally:
            self._release_all(owned)

    def write_fresh_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> bool:
        if persist != "local_machine" or not value:
            raise OSError("invalid credential input")
        query, owned = self._base_query(target)
        try:
            data = self._data(value)
            owned.append(data)
            self._set(query, "kSecValueData", data)
            status = int(self._security.SecItemAdd(query, None))
            if status == _ERR_SEC_DUPLICATE_ITEM:
                return False
            if status != _ERR_SEC_SUCCESS:
                raise OSError("Keychain write failed")
            return True
        finally:
            self._release_all(owned)

    def read_generic(self, target: str) -> _CredentialRecord | None:
        query, owned = self._base_query(target)
        result = ctypes.c_void_p()
        try:
            self._set(query, "kSecReturnData", self._constants["kCFBooleanTrue"])
            self._set(query, "kSecMatchLimit", self._constants["kSecMatchLimitOne"])
            status = int(
                self._security.SecItemCopyMatching(query, ctypes.byref(result))
            )
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                return None
            if status != _ERR_SEC_SUCCESS or not result.value:
                raise OSError("Keychain read failed")
            if self._cf.CFGetTypeID(result.value) != self._cf.CFDataGetTypeID():
                raise OSError("Keychain value is invalid")
            size = int(self._cf.CFDataGetLength(result.value))
            if size <= 0 or size > _MAX_KEYCHAIN_RESULT_BYTES:
                raise OSError("Keychain value is invalid")
            pointer = self._cf.CFDataGetBytePtr(result.value)
            if not pointer:
                raise OSError("Keychain value is invalid")
            blob = bytearray(size)
            destination = (ctypes.c_ubyte * size).from_buffer(blob)
            ctypes.memmove(destination, pointer, size)
            return _CredentialRecord(
                target=target,
                credential_type=CRED_TYPE_GENERIC,
                blob=blob,
            )
        finally:
            if result.value:
                self._cf.CFRelease(result.value)
            self._release_all(owned)

    def delete_generic(self, target: str) -> bool:
        query, owned = self._base_query(target)
        try:
            status = int(self._security.SecItemDelete(query))
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                return False
            if status != _ERR_SEC_SUCCESS:
                raise OSError("Keychain delete failed")
            return True
        finally:
            self._release_all(owned)

    def list_generic_targets(self, prefix: str) -> tuple[str, ...]:
        query, owned = self._base_query()
        result = ctypes.c_void_p()
        try:
            self._set(
                query, "kSecReturnAttributes", self._constants["kCFBooleanTrue"]
            )
            self._set(query, "kSecMatchLimit", self._constants["kSecMatchLimitAll"])
            status = int(
                self._security.SecItemCopyMatching(query, ctypes.byref(result))
            )
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                return ()
            if status != _ERR_SEC_SUCCESS or not result.value:
                raise OSError("Keychain enumeration failed")
            if self._cf.CFGetTypeID(result.value) != self._cf.CFArrayGetTypeID():
                raise OSError("Keychain enumeration is invalid")
            targets: list[str] = []
            count = int(self._cf.CFArrayGetCount(result.value))
            if count < 0 or count > 10_000:
                raise OSError("Keychain enumeration is invalid")
            for index in range(count):
                attributes = self._cf.CFArrayGetValueAtIndex(result.value, index)
                if (
                    not attributes
                    or self._cf.CFGetTypeID(attributes)
                    != self._cf.CFDictionaryGetTypeID()
                ):
                    raise OSError("Keychain enumeration is invalid")
                account = self._cf.CFDictionaryGetValue(
                    attributes, self._constants["kSecAttrAccount"]
                )
                if not account:
                    continue
                if self._cf.CFGetTypeID(account) != self._cf.CFStringGetTypeID():
                    raise OSError("Keychain account is invalid")
                target = self._decode_string(account)
                if target.startswith(prefix):
                    targets.append(target)
            return tuple(targets)
        finally:
            if result.value:
                self._cf.CFRelease(result.value)
            self._release_all(owned)

    def _decode_string(self, value: int) -> str:
        length = int(self._cf.CFStringGetLength(value))
        maximum = int(
            self._cf.CFStringGetMaximumSizeForEncoding(length, _UTF8_ENCODING)
        )
        if length < 0 or maximum < 0 or maximum > 4096:
            raise OSError("Keychain account is invalid")
        buffer = ctypes.create_string_buffer(maximum + 1)
        if not self._cf.CFStringGetCString(
            value, buffer, len(buffer), _UTF8_ENCODING
        ):
            raise OSError("Keychain account is invalid")
        return buffer.value.decode("utf-8", errors="strict")


__all__ = ["MacOSCredentialStore"]
