"""Private, atomic persistence for versioned bootstrap configuration."""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Iterator

from pydantic import ValidationError

from .config import DeveloperWorkflowConfig
from .credential_store import CredentialStore, CredentialStoreError
from .platform_support import HostPlatformError, default_host_paths
from .private_paths import (
    PrivatePathError,
    _ADMINISTRATORS_SID,
    _SYSTEM_SID,
    _current_user_sid,
    _is_link_or_reparse,
    _windows_descriptor,
    prepare_private_directory,
)
from .setup_models import ActiveSetup, RuntimeSecrets, SetupDocument

if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.MoveFileExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    _kernel32.MoveFileExW.restype = wintypes.BOOL
else:
    import fcntl


class SetupStoreError(RuntimeError):
    """A sanitized configuration persistence failure."""


_MAX_CONFIGURATION_BYTES = 1024 * 1024
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_READ_ATTEMPTS = 3
_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
logger = logging.getLogger(__name__)


class _Replaced(RuntimeError):
    pass


class _AtomicWriteError(RuntimeError):
    """Internal monotonic outcome from one atomic replacement attempt."""

    def __init__(self, *, replaced: bool) -> None:
        super().__init__("atomic configuration write failed")
        self.replaced = replaced


class SetupStore:
    """Serialize configuration pointer changes with credential generations."""

    def __init__(
        self,
        credentials: CredentialStore,
        *,
        config_path: Path | None = None,
        lock_timeout: float = 30.0,
        lock_poll_interval: float = 0.05,
    ) -> None:
        if lock_timeout < 0 or lock_poll_interval <= 0:
            raise ValueError("lock timing values must be positive")
        if config_path is None:
            try:
                config_path = default_host_paths().config_path
            except HostPlatformError:
                raise SetupStoreError("configuration path is unavailable") from None
        candidate = Path(config_path).absolute()
        if candidate.name != "config.json" or _has_unsafe_ancestor(candidate):
            raise SetupStoreError("configuration path is unsafe")
        try:
            self._directory = prepare_private_directory(candidate.parent)
        except (OSError, PrivatePathError):
            raise SetupStoreError("configuration path is unsafe") from None
        self._config_path = self._directory / "config.json"
        self._directory_identity = _identity(self._directory)
        self._credentials = credentials
        self._lock_timeout = lock_timeout
        self._lock_poll_interval = lock_poll_interval

    @property
    def config_path(self) -> Path:
        return self._config_path

    def load(self) -> SetupDocument:
        with self._locked():
            return self._load_unlocked()

    def load_or_empty(self, *, profile_id: str) -> SetupDocument:
        with self._locked():
            return self._load_or_empty_unlocked(profile_id)

    def commit(
        self, profile_id: str, candidate: ActiveSetup, secrets: RuntimeSecrets
    ) -> SetupDocument:
        with self._locked():
            current = self._load_or_empty_unlocked(profile_id)
            if current.profile_id != profile_id:
                raise SetupStoreError("configuration profile is invalid")
            if (
                type(candidate) is not ActiveSetup
                or type(secrets) is not RuntimeSecrets
                or set(candidate.credential_kinds) != set(secrets.values)
            ):
                raise SetupStoreError("configuration credentials are invalid")
            if current.activation_owner_generation is not None:
                raise SetupStoreError("configuration activation is already pending")
            referenced = {
                setup.generation
                for setup in (current.active, current.previous)
                if setup is not None
            }
            if candidate.generation in referenced:
                raise SetupStoreError("configuration generation is unavailable")
            try:
                fresh_generation = self._credentials.write_fresh_generation(
                    profile_id, candidate.generation, secrets
                )
            except CredentialStoreError:
                raise SetupStoreError("configuration generation is unavailable") from None
            if fresh_generation is not True:
                raise SetupStoreError("configuration generation is unavailable")
            document = current.validated_update(
                active=candidate,
                previous=current.active,
                activation_owner_generation=candidate.generation,
            )
            write_failure: _AtomicWriteError | None = None
            try:
                self._atomic_write(document)
            except _AtomicWriteError as error:
                write_failure = error
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise
                replaced = False
                try:
                    replaced = self._load_unlocked() == document
                except SetupStoreError:
                    pass
                write_failure = _AtomicWriteError(replaced=replaced)
            if write_failure is not None:
                if fresh_generation and not write_failure.replaced:
                    try:
                        self._credentials.delete_generation(
                            profile_id, candidate.generation
                        )
                    except CredentialStoreError:
                        pass
                raise SetupStoreError("configuration save failed")
            confirmation_failed = False
            try:
                loaded = self._load_unlocked()
            except SetupStoreError:
                confirmation_failed = True
            if confirmation_failed:
                raise SetupStoreError("configuration save failed")
            if loaded != document:
                raise SetupStoreError("configuration save failed")
            return loaded

    def read_active_secrets(self, document: SetupDocument) -> RuntimeSecrets:
        active = document.active
        if active is None:
            raise SetupStoreError("active configuration is unavailable")
        try:
            return self._credentials.read_generation(
                document.profile_id, active.generation, active.credential_kinds
            )
        except CredentialStoreError:
            raise SetupStoreError("active credentials are unavailable") from None

    def replace_active_workflow(
        self,
        profile_id: str,
        expected_generation: str,
        workflow: DeveloperWorkflowConfig,
    ) -> SetupDocument:
        """Atomically replace only the finalized active workflow configuration."""

        if type(workflow) is not DeveloperWorkflowConfig:
            raise SetupStoreError("configuration save failed")
        with self._locked():
            current = self._load_unlocked_for_profile(profile_id)
            active = current.active
            if (
                type(expected_generation) is not str
                or _GENERATION.fullmatch(expected_generation) is None
                or active is None
                or active.generation != expected_generation
                or current.activation_owner_generation is not None
            ):
                raise SetupStoreError("configuration generation is superseded")
            try:
                replacement = active.validated_update(workflow=workflow)
                document = current.validated_update(active=replacement)
            except (TypeError, ValueError, ValidationError):
                raise SetupStoreError("configuration save failed") from None
            return self._write_confirmed_monotonic(document)

    def restore_previous(
        self, profile_id: str, expected_generation: str
    ) -> SetupDocument:
        with self._locked():
            current = self._load_unlocked_for_profile(profile_id)
            self._require_active_generation(current, expected_generation)
            failed = current.active
            if failed is None:
                raise SetupStoreError("previous configuration is unavailable")
            restored = current.validated_update(
                active=current.previous,
                previous=None,
                activation_owner_generation=None,
            )
            loaded = self._write_confirmed_monotonic(restored)
            if failed is not None:
                try:
                    self._credentials.delete_generation(
                        profile_id, failed.generation
                    )
                except CredentialStoreError:
                    raise SetupStoreError("credential cleanup failed") from None
            return loaded

    def finalize_activation(
        self, profile_id: str, expected_generation: str
    ) -> SetupDocument:
        with self._locked():
            current = self._load_unlocked_for_profile(profile_id)
            self._require_active_generation(current, expected_generation)
            obsolete = current.previous
            finalized = current.validated_update(
                previous=None, activation_owner_generation=None
            )
            loaded = self._write_confirmed_monotonic(finalized)
            if obsolete is not None:
                try:
                    self._credentials.delete_generation(
                        profile_id, obsolete.generation
                    )
                except CredentialStoreError:
                    logger.warning("setup credential cleanup deferred")
            return loaded

    @staticmethod
    def _require_active_generation(
        document: SetupDocument, expected_generation: str
    ) -> None:
        if (
            type(expected_generation) is not str
            or _GENERATION.fullmatch(expected_generation) is None
            or document.active is None
            or document.active.generation != expected_generation
            or document.activation_owner_generation != expected_generation
        ):
            raise SetupStoreError("configuration generation is superseded")

    def orphan_generations(self) -> tuple[str, ...]:
        with self._locked():
            current = self._load_unlocked()
            referenced = {
                setup.generation
                for setup in (current.active, current.previous)
                if setup is not None
            }
            try:
                listed = self._credentials.list_generations(current.profile_id)
            except CredentialStoreError:
                raise SetupStoreError("credential enumeration failed") from None
            return tuple(
                generation
                for generation in sorted(set(listed))
                if _GENERATION.fullmatch(generation) and generation not in referenced
            )

    def cleanup_orphan_generations(self, generations: tuple[str, ...]) -> None:
        if (
            type(generations) is not tuple
            or not generations
            or len(set(generations)) != len(generations)
            or any(
                type(generation) is not str
                or _GENERATION.fullmatch(generation) is None
                for generation in generations
            )
        ):
            raise SetupStoreError("credential cleanup refused")
        with self._locked():
            current = self._load_unlocked()
            referenced = {
                setup.generation
                for setup in (current.active, current.previous)
                if setup is not None
            }
            try:
                listed = set(self._credentials.list_generations(current.profile_id))
            except CredentialStoreError:
                raise SetupStoreError("credential enumeration failed") from None
            if any(generation in referenced or generation not in listed for generation in generations):
                raise SetupStoreError("credential cleanup refused")
            for generation in generations:
                try:
                    self._credentials.delete_generation(
                        current.profile_id, generation
                    )
                except CredentialStoreError:
                    raise SetupStoreError("credential cleanup failed") from None

    def _load_or_empty_unlocked(self, profile_id: str) -> SetupDocument:
        if not self._config_path.exists():
            return SetupDocument(profile_id=profile_id)
        return self._load_unlocked_for_profile(profile_id)

    def _load_unlocked_for_profile(self, profile_id: str) -> SetupDocument:
        document = self._load_unlocked()
        if document.profile_id != profile_id:
            raise SetupStoreError("configuration profile is invalid")
        return document

    def _load_exact(self, expected: SetupDocument) -> SetupDocument:
        loaded = self._load_unlocked()
        if loaded != expected:
            raise SetupStoreError("configuration save failed")
        return loaded

    def _load_unlocked(self) -> SetupDocument:
        self._validate_directory()
        for attempt in range(_READ_ATTEMPTS):
            try:
                raw = _read_nofollow(self._config_path)
            except FileNotFoundError:
                raise SetupStoreError("configuration is unavailable") from None
            except _Replaced:
                if attempt + 1 < _READ_ATTEMPTS:
                    continue
                raise SetupStoreError("configuration path is unsafe") from None
            except ValueError:
                raise SetupStoreError("stored configuration is corrupted") from None
            except OSError:
                raise SetupStoreError("configuration path is unsafe") from None
            self._validate_directory()
            try:
                parsed = json.loads(
                    raw,
                    object_pairs_hook=_strict_object,
                    parse_constant=lambda _value: _invalid_json(),
                )
                return SetupDocument.model_validate(parsed)
            except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
                raise SetupStoreError("stored configuration is corrupted") from None
        raise SetupStoreError("configuration path is unsafe")

    def _write_or_fail(self, document: SetupDocument) -> None:
        failed = False
        try:
            self._atomic_write(document)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            failed = True
        if failed:
            raise SetupStoreError("configuration save failed")

    def _write_confirmed_monotonic(self, document: SetupDocument) -> SetupDocument:
        """Accept a post-replace failure only after confirming the exact pointer."""

        write_failed = False
        replaced = False
        try:
            self._atomic_write(document)
        except _AtomicWriteError as error:
            replaced = error.replaced
            write_failed = True
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            write_failed = True
        if write_failed and not replaced:
            try:
                replaced = self._load_unlocked() == document
            except SetupStoreError:
                pass
        if write_failed and not replaced:
            raise SetupStoreError("configuration save failed")
        return self._load_exact(document)

    def _atomic_write(self, document: SetupDocument) -> None:
        self._validate_directory()
        payload = json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
        if len(payload) > _MAX_CONFIGURATION_BYTES:
            raise OSError("configuration is too large")
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".config-", suffix=".tmp", dir=self._directory
        )
        temp_path = Path(raw_path)
        replaced = False
        failure: _AtomicWriteError | None = None
        try:
            os.chmod(temp_path, 0o600)
            _protect_private_file(temp_path)
            _validate_regular_file(temp_path, descriptor=descriptor)
            self._validate_directory()
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("configuration write failed")
                view = view[written:]
            _flush_file_descriptor(descriptor)
            os.close(descriptor)
            descriptor = -1
            _validate_regular_file(temp_path)
            self._validate_directory()
            _replace_atomic(temp_path, self._config_path)
            replaced = True
            _validate_regular_file(self._config_path)
            self._validate_directory()
            _fsync_directory(self._directory)
        except Exception:
            failure = _AtomicWriteError(replaced=replaced)
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temp_path.unlink()
            except OSError:
                pass
        if failure is not None:
            raise failure

    def _validate_directory(self) -> None:
        try:
            if _is_link_or_reparse(self._directory):
                raise OSError
            info = self._directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or _identity(self._directory) != self._directory_identity:
                raise OSError
            if os.name == "nt":
                # Existing directories are re-verified on every operation, not only construction.
                from .private_paths import _verify_windows

                _verify_windows(self._directory, _current_user_sid())
            elif info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError
        except (OSError, PrivatePathError):
            raise SetupStoreError("configuration path is unsafe") from None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._validate_directory()
        lock_path = self._directory / ".config.lock"
        descriptor = -1
        acquired = False
        try:
            if _is_link_or_reparse(lock_path):
                raise OSError("unsafe lock file")
            descriptor, created = _open_lock_nofollow(lock_path)
            if created:
                _protect_private_file(lock_path)
                _validate_regular_file(lock_path, descriptor=descriptor)
            else:
                _validate_existing_lock(
                    lock_path,
                    descriptor,
                    timeout=min(self._lock_timeout, 1.0),
                    poll_interval=self._lock_poll_interval,
                )
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            deadline = time.monotonic() + self._lock_timeout
            while not acquired:
                try:
                    _try_lock(descriptor)
                    acquired = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise SetupStoreError("configuration lock timed out") from exc
                    time.sleep(self._lock_poll_interval)
            self._validate_directory()
            _validate_regular_file(lock_path, descriptor=descriptor)
            yield
        except SetupStoreError:
            raise
        except OSError:
            raise SetupStoreError("configuration path is unsafe") from None
        finally:
            if acquired:
                try:
                    _unlock(descriptor)
                except OSError:
                    pass
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _has_unsafe_ancestor(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_link_or_reparse(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _invalid_json() -> None:
    raise ValueError("invalid JSON constant")


def _validate_regular_file(path: Path, *, descriptor: int | None = None) -> tuple[int, int]:
    if _is_link_or_reparse(path):
        raise OSError("unsafe file")
    info = path.stat(follow_symlinks=False)
    identity = (info.st_dev, info.st_ino)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("unsafe file")
    if descriptor is not None:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != identity
        ):
            raise OSError("unsafe file")
    if os.name == "nt":
        owner, entries, protected = _windows_descriptor(path)
        user = _current_user_sid()
        trusted = {user, _SYSTEM_SID, _ADMINISTRATORS_SID}
        full_control = 0x001F01FF
        if (
            owner != user
            or not protected
            or not entries
            or any(
                sid not in trusted
                or ace_type != 0
                or flags & 0x10
                or mask & full_control != full_control
                for sid, mask, flags, ace_type in entries
            )
        ):
            raise OSError("unsafe file")
    elif info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise OSError("unsafe file")
    return identity


def _read_nofollow(path: Path) -> str:
    expected = _validate_regular_file(path)
    if os.name == "nt":
        descriptor = _open_windows(path)
    else:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != expected or not stat.S_ISREG(opened.st_mode):
            raise _Replaced
        if opened.st_size > _MAX_CONFIGURATION_BYTES:
            raise ValueError("oversize")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_CONFIGURATION_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CONFIGURATION_BYTES:
                raise ValueError("oversize")
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino) != expected
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
        ):
            raise _Replaced
    finally:
        os.close(descriptor)
    if _validate_regular_file(path) != expected:
        raise _Replaced
    try:
        return b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("invalid UTF-8") from None


def _protect_private_file(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    try:
        user_sid = _current_user_sid()
    except OSError:
        raise OSError("private file ACL is unavailable") from None
    completed = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{user_sid}:F",
            f"*{_SYSTEM_SID}:F",
            f"*{_ADMINISTRATORS_SID}:F",
            "/q",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError("private file ACL is unavailable")


def _open_windows(path: Path) -> int:
    return _open_windows_descriptor(
        path,
        desired_access=0x80000000,
        creation_disposition=3,
        os_flags=os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )


def _open_lock_nofollow(path: Path) -> tuple[int, bool]:
    if os.name == "nt":
        arguments = {
            "desired_access": 0x80000000 | 0x40000000,
            "os_flags": os.O_RDWR | getattr(os, "O_BINARY", 0),
        }
        try:
            return (
                _open_windows_descriptor(
                    path, creation_disposition=1, **arguments
                ),
                True,
            )
        except OSError as error:
            if getattr(error, "winerror", None) not in {80, 183}:
                raise
        return (
            _open_windows_descriptor(path, creation_disposition=3, **arguments),
            False,
        )
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600), True
    except FileExistsError:
        return os.open(path, flags), False


def _validate_existing_lock(
    path: Path,
    descriptor: int,
    *,
    timeout: float,
    poll_interval: float,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _validate_regular_file(path, descriptor=descriptor)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll_interval)


def _open_windows_descriptor(
    path: Path,
    *,
    desired_access: int,
    creation_disposition: int,
    os_flags: int,
) -> int:
    handle = _kernel32.CreateFileW(  # type: ignore[name-defined]
        str(path),
        desired_access,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        creation_disposition,
        0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = _kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)  # type: ignore[name-defined]
        if not length or length >= len(buffer):
            raise OSError("unsafe file")
        final = buffer.value
        if final.startswith("\\\\?\\UNC\\"):
            final = "\\\\" + final[8:]
        elif final.startswith("\\\\?\\"):
            final = final[4:]
        if os.path.normcase(os.path.abspath(final)) != os.path.normcase(os.path.abspath(path)):
            raise OSError("unsafe file")
        return msvcrt.open_osfhandle(  # type: ignore[name-defined]
            int(handle), os_flags
        )
    except BaseException:
        _kernel32.CloseHandle(handle)  # type: ignore[name-defined]
        raise


def _try_lock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # type: ignore[name-defined]
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[name-defined]


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)  # type: ignore[name-defined]
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[name-defined]


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # MoveFileExW(MOVEFILE_WRITE_THROUGH) is the Windows durability boundary.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _flush_file_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        handle = msvcrt.get_osfhandle(descriptor)  # type: ignore[name-defined]
        if handle == -1 or not _kernel32.FlushFileBuffers(handle):  # type: ignore[name-defined]
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.fsync(descriptor)


def _move_file_ex(source: str, destination: str, flags: int) -> None:
    if os.name != "nt":
        raise OSError("Windows replacement API is unavailable")
    if not _kernel32.MoveFileExW(source, destination, flags):  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_atomic(source: Path, destination: Path) -> None:
    if os.name == "nt":
        _move_file_ex(
            str(source),
            str(destination),
            _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
        )
        return
    os.replace(source, destination)


__all__ = ["SetupStore", "SetupStoreError"]
