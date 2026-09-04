"""Safe local Git mirrors and isolated workflow worktrees."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

if os.name == "nt":  # pragma: no cover - exercised by Windows repository tests
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _repo_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _repo_kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _repo_kernel32.CreateFileW.restype = wintypes.HANDLE
    _repo_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _repo_kernel32.CloseHandle.restype = wintypes.BOOL
    _repo_kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    _repo_kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

from .contracts import (
    ApprovalPackage,
    PreparedWorktree,
    RepositoryMapping,
    RepositorySnapshot,
    WorkflowRun,
    WorkflowType,
    validate_git_ref_name,
)


class RepositoryError(RuntimeError):
    """Base error for local repository operations."""


class RepositoryBoundaryError(RepositoryError):
    """A path would cross a configured repository boundary."""


class RepositoryIdentityError(RepositoryError):
    """A mirror or worktree does not have the expected Git identity."""


class RemoteBaseChangedError(RepositoryIdentityError):
    """The target branch moved since this task's baseline was recorded."""


class RepositoryCommandError(RepositoryError):
    """A Git subprocess failed without exposing its potentially sensitive output."""


class BaseBranchNotFound(RepositoryError):
    """The configured remote base branch does not exist."""


class BranchAlreadyExists(RepositoryError):
    """The requested local or remote branch already exists."""


class HeadChangedError(RepositoryError):
    """The isolated worktree HEAD moved away from its expected commit."""


class TargetExists(RepositoryBoundaryError):
    """The requested worktree target has already been reserved."""


class SnapshotTooLargeError(RepositoryError):
    """A repository snapshot exceeded a configured safe size limit."""


class MirrorOriginMismatch(RepositoryIdentityError):
    """An existing mirror origin does not safely match its mapping."""


@dataclass(frozen=True, slots=True)
class _WorktreeRegistration:
    branch: str
    head: str


CommandRunner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[bytes]]


def _path_from_open_descriptor(descriptor: int) -> Path:
    """Resolve a POSIX file path from the already-open descriptor.

    Unsupported POSIX hosts fail closed rather than falling back to the caller's
    pathname, because that pathname can be replaced after ``os.open`` returns.
    """

    if sys.platform == "darwin":
        import fcntl

        # Darwin's F_GETPATH writes MAXPATHLEN bytes for the object referenced by
        # the descriptor. Using this handle-bound result is essential: resolving
        # the original pathname here would reintroduce a symlink-swap race.
        command = getattr(fcntl, "F_GETPATH", 50)
        try:
            raw_path = fcntl.fcntl(descriptor, command, b"\0" * 1024)
        except OSError as error:
            raise RepositoryBoundaryError(
                "changed file handle could not be verified"
            ) from error
        value = raw_path.split(b"\0", 1)[0]
        if not value:
            raise RepositoryBoundaryError(
                "changed file handle could not be verified"
            )
        try:
            path = Path(os.fsdecode(value))
        except UnicodeError as error:
            raise RepositoryBoundaryError(
                "changed file handle could not be verified"
            ) from error
        if not path.is_absolute():
            raise RepositoryBoundaryError(
                "changed file handle could not be verified"
            )
        return path

    if sys.platform.startswith("linux"):
        descriptor_path = Path(f"/proc/self/fd/{descriptor}")
        try:
            return descriptor_path.resolve(strict=True)
        except OSError as error:
            raise RepositoryBoundaryError(
                "changed file handle could not be verified"
            ) from error

    raise RepositoryBoundaryError("changed file handle could not be verified")


def _open_readonly_nofollow(path: Path, *, worktree: Path | None = None) -> int:
    """Open a regular file without ever traversing a final reparse point."""

    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise RepositoryBoundaryError(
                "changed file could not be safely opened"
            ) from error
        if worktree is not None:
            try:
                actual = _path_from_open_descriptor(descriptor)
                root = worktree.resolve(strict=True)
            except (OSError, RepositoryBoundaryError) as error:
                os.close(descriptor)
                raise RepositoryBoundaryError(
                    "changed file handle could not be verified"
                ) from error
            if not _is_within(actual, root):
                os.close(descriptor)
                raise RepositoryBoundaryError(
                    "changed file handle resolves outside worktree"
                )
        return descriptor

    handle = _repo_kernel32.CreateFileW(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # Share read/write/delete.
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise RepositoryBoundaryError("changed file could not be safely opened")
    if worktree is not None:
        buffer = ctypes.create_unicode_buffer(32768)
        length = _repo_kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), 0
        )
        if not length or length >= len(buffer):
            _repo_kernel32.CloseHandle(handle)
            raise RepositoryBoundaryError(
                "changed file handle could not be verified"
            )
        final_text = buffer.value
        if final_text.startswith("\\\\?\\UNC\\"):
            final_text = "\\\\" + final_text[8:]
        elif final_text.startswith("\\\\?\\"):
            final_text = final_text[4:]
        try:
            actual = Path(final_text).resolve(strict=True)
            root = worktree.resolve(strict=True)
        except OSError as error:
            _repo_kernel32.CloseHandle(handle)
            raise RepositoryBoundaryError(
                "changed file handle could not be verified"
            ) from error
        if not _is_within(actual, root):
            _repo_kernel32.CloseHandle(handle)
            raise RepositoryBoundaryError(
                "changed file handle resolves outside worktree"
            )
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError as error:
        _repo_kernel32.CloseHandle(handle)
        raise RepositoryBoundaryError(
            "changed file could not be safely opened"
        ) from error
    opened = os.fstat(descriptor)
    if getattr(opened, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    ):
        os.close(descriptor)
        raise RepositoryBoundaryError("changed file could not be safely opened")
    return descriptor


_GIT_CREDENTIAL_ENV_ALLOWLIST = frozenset(
    {"GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "SSH_ASKPASS", "SSH_AUTH_SOCK"}
)
_GIT_IDENTITY_ENV_ALLOWLIST = frozenset(
    {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}
)


def validate_git_identity_environment(
    identity: Mapping[str, str],
) -> dict[str, str]:
    """Validate the complete explicit Git identity without invoking Git."""

    if not isinstance(identity, Mapping):
        raise RepositoryBoundaryError("explicit Git environment is invalid")
    identity_values = dict(identity)
    if any(key not in _GIT_IDENTITY_ENV_ALLOWLIST for key in identity_values):
        raise RepositoryBoundaryError(
            "Git identity environment contains a forbidden key"
        )
    if identity_values and set(identity_values) != _GIT_IDENTITY_ENV_ALLOWLIST:
        raise RepositoryBoundaryError("Git identity environment is incomplete")
    if any(
        type(value) is not str
        or not value.strip()
        or len(value) > 320
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in identity_values.values()
    ):
        raise RepositoryBoundaryError(
            "Git identity environment contains an invalid value"
        )
    try:
        for value in identity_values.values():
            value.encode("utf-8", "strict")
    except UnicodeError:
        raise RepositoryBoundaryError(
            "Git identity environment contains invalid UTF-8"
        ) from None
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        value = identity_values.get(key)
        if value is not None and re.fullmatch(
            r"[^@\s<>]+@[^@\s<>]+", value
        ) is None:
            raise RepositoryBoundaryError("Git identity email is invalid")
    return identity_values


def _windows_ssh_command() -> str | None:
    """Use native profile trust/key files, never ambient HOME or ssh config."""
    if os.name != "nt":
        return None
    import ctypes

    profile = ctypes.create_unicode_buffer(32768)
    # CSIDL_PROFILE: resolve the signed-in user's profile independently of the
    # environment handed to a task. No secret file contents enter this process.
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x28, None, 0, profile) != 0:
        return None
    directory = Path(profile.value) / ".ssh"
    if not directory.is_dir() or _is_link_or_reparse(directory):
        return None
    known_hosts = directory / "known_hosts"
    if not known_hosts.is_file() or _is_link_or_reparse(known_hosts):
        return None
    args = ["ssh", "-F", "none", "-oBatchMode=yes", "-oIdentitiesOnly=yes",
            "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no",
            "-oUserKnownHostsFile=" + known_hosts.as_posix()]
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        key = directory / name
        if key.is_file() and not _is_link_or_reparse(key):
            args.extend(("-i", key.as_posix()))
    return shlex.join(args)


def _macos_ssh_command() -> str:
    """Reuse existing account trust/keys, without importing SSH configuration."""
    import pwd
    from .private_paths import _has_link_or_reparse_ancestor

    directory = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ssh"

    def safe_directory(path: Path) -> bool:
        try:
            info = path.lstat()
            return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                    and not info.st_mode & 0o022
                    and not _has_link_or_reparse_ancestor(path))
        except OSError:
            return False

    def safe(path: Path, *, private: bool = False) -> bool:
        try:
            info = path.lstat()
            return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and not info.st_mode & (0o077 if private else 0o022)
                    and not _has_link_or_reparse_ancestor(path))
        except OSError:
            return False

    args = ["/usr/bin/ssh", "-F", "/dev/null", "-oBatchMode=yes", "-oIdentitiesOnly=yes",
            "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no", "-oIdentityAgent=none",
            "-oGlobalKnownHostsFile=/dev/null"]
    # An untrusted ~/.ssh lets another local user replace its children, so do
    # not expose any path below it to ssh.
    if not safe_directory(directory):
        args.extend(("-oUserKnownHostsFile=/dev/null", "-oIdentityFile=none"))
        return shlex.join(args)

    known_hosts = directory / "known_hosts"
    args.append("-oUserKnownHostsFile=" + (
        str(known_hosts) if safe(known_hosts) else "/dev/null"
    ))
    keys = [directory / name for name in ("id_ed25519", "id_ecdsa", "id_rsa")]
    for key in keys:
        if safe(key, private=True):
            args.extend(("-i", str(key)))
    if "-i" not in args:
        args.extend(("-oIdentityFile=none",))
    return shlex.join(args)


def _isolated_git_environment(
    credential_environment: Mapping[str, str] | None = None,
    *,
    controlled_home: Path | None = None,
    controlled_temp: Path | None = None,
) -> dict[str, str]:
    system_allowlist = {"PATH", "SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR"}
    environment = {
        key: value for key, value in os.environ.items() if key in system_allowlist
    }
    home = str(controlled_home or (Path(tempfile.gettempdir()) / "ones-agent-empty-git-home"))
    temp = str(controlled_temp or (Path(tempfile.gettempdir()) / "ones-agent-git-temp"))
    supplied = dict(credential_environment or {})
    if any(key not in _GIT_CREDENTIAL_ENV_ALLOWLIST for key in supplied):
        raise RepositoryBoundaryError("Git credential environment contains a forbidden key")
    if any(
        type(value) is not str
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in supplied.values()
    ):
        raise RepositoryBoundaryError("Git credential environment contains an invalid value")
    try:
        for value in supplied.values():
            value.encode("utf-8", "strict")
    except UnicodeError:
        raise RepositoryBoundaryError("Git credential environment contains invalid UTF-8") from None
    environment.update(supplied)
    environment.update(
        {
            "HOME": home,
            "USERPROFILE": home,
            "APPDATA": home,
            "LOCALAPPDATA": home,
            "TMP": temp,
            "TEMP": temp,
            "TMPDIR": temp,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            # Ignore both user and system SSH configuration. In particular, a
            # system-wide Include or ProxyCommand must not regain execution in
            # a supposedly isolated Git subprocess.
            "GIT_SSH_COMMAND": (
                "ssh -F none -oProxyCommand=none -oBatchMode=yes "
                "-oIdentitiesOnly=yes -oStrictHostKeyChecking=yes "
                "-oUpdateHostKeys=no"
            ),
        }
    )
    environment.update(supplied)
    # Retain config isolation: never inherit arbitrary credential helper shell
    # commands from user/system/repository configuration. Git for Windows ships
    # GCM, whose OS vault is available even with the controlled HOME. Explicit
    # askpass credentials take precedence over that narrow native fallback.
    environment.update({
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "credential.interactive",
        "GIT_CONFIG_VALUE_2": "false",
    })
    if os.name == "nt" and "GIT_ASKPASS" not in supplied:
        environment.update({
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_3": "credential.helper",
            "GIT_CONFIG_VALUE_3": "manager",
        })
    elif sys.platform == "darwin" and "GIT_ASKPASS" not in supplied:
        environment.update({
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_3": "credential.helper",
            "GIT_CONFIG_VALUE_3": "osxkeychain",
        })
    if "GIT_SSH" in supplied and "GIT_SSH_COMMAND" not in supplied:
        # Git gives SSH_COMMAND precedence; the built-in default must not hide
        # an explicitly configured SSH executable.
        environment.pop("GIT_SSH_COMMAND", None)
    if os.name == "nt" and not any(name in supplied for name in ("GIT_SSH", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK", "SSH_ASKPASS")):
        native_ssh = _windows_ssh_command()
        if native_ssh:
            environment["GIT_SSH_COMMAND"] = native_ssh
    elif sys.platform == "darwin" and not any(name in supplied for name in ("GIT_SSH", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK", "SSH_ASKPASS")):
        environment["GIT_SSH_COMMAND"] = _macos_ssh_command()
    return environment


def _default_command_runner(
    command: Sequence[str], cwd: Path | None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=_isolated_git_environment(),
        shell=False,
        capture_output=True,
        check=False,
    )


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _canonical_commit_oid(value: str) -> str:
    if len(value) not in {40, 64} or not re.fullmatch(r"[0-9a-f]+", value):
        raise RepositoryIdentityError("commit identity is not a canonical full object ID")
    return value


def _path_identity(path: Path) -> tuple[int, int, int, int]:
    return _path_identity_from_stat(path.lstat())


def _path_identity_from_stat(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        getattr(metadata, "st_file_attributes", 0),
    )


def _safe_ascii_slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", ascii_value)).strip("-").lower()


def build_branch_name(workflow_type: WorkflowType | str, work_item_id: str, title: str) -> str:
    """Build a bounded, safe branch name for a requirement or defect workflow."""

    raw_type = workflow_type.value if isinstance(workflow_type, WorkflowType) else workflow_type
    if raw_type == WorkflowType.REQUIREMENT.value:
        prefix = "requirement"
    elif raw_type in {WorkflowType.DEFECT.value, "bugfix"}:
        prefix = "bugfix"
    else:
        raise ValueError("workflow_type must be requirement, defect, or bugfix")
    if (
        not work_item_id
        or work_item_id != work_item_id.strip()
        or work_item_id.startswith("-")
        or any(character in work_item_id for character in "/\\:")
    ):
        raise ValueError("work_item_id is not a safe branch segment")
    safe_id = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", work_item_id)).strip("-")
    if not safe_id or safe_id in {".", ".."} or safe_id.casefold().endswith(".lock"):
        raise ValueError("work_item_id is not a safe branch segment")
    slug = _safe_ascii_slug(title) or "change"
    fixed_length = len(prefix) + 1 + len(safe_id) + 1
    available = 120 - fixed_length
    if available < 1:
        raise ValueError("work_item_id is too long")
    slug = slug[:available].rstrip("-") or "change"[:available]
    branch = f"{prefix}/{safe_id}-{slug}"
    validate_git_ref_name(branch)
    return branch


def build_run_branch_name(
    workflow_type: WorkflowType | str,
    work_item_id: str,
    title: str,
    run_id: str,
    *,
    repository_key: str | None = None,
) -> str:
    """Build a work branch unique to one persisted workflow run."""

    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", run_id or ""):
        raise ValueError("run_id is not a safe branch segment")
    if repository_key is not None and not re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", repository_key
    ):
        raise ValueError("repository_key is not a safe branch segment")
    suffix = f"-{run_id}"
    if repository_key is not None:
        suffix += f"-{repository_key}"
    base = build_branch_name(workflow_type, work_item_id, title)
    available = 120 - len(suffix)
    if available < 4:
        raise ValueError("run-scoped branch suffix is too long")
    base = base[:available].rstrip("-.")
    if not base or base.endswith("/"):
        raise ValueError("run-scoped branch could not be constructed safely")
    branch = f"{base}{suffix}"
    validate_git_ref_name(branch)
    return branch


@dataclass(slots=True)
class WorktreeRepository:
    mirror_root: Path
    worktree_root: Path
    command_runner: CommandRunner = field(default=_default_command_runner, repr=False)
    credential_env_provider: Callable[[], Mapping[str, str]] = field(
        default=lambda: {}, repr=False
    )
    identity_env_provider: Callable[[], Mapping[str, str]] = field(
        default=lambda: {}, repr=False
    )
    _controlled_git_home: Path = field(init=False, repr=False)
    _controlled_git_temp: Path = field(init=False, repr=False)
    max_patch_bytes: int = 10 * 1024 * 1024
    max_untracked_file_bytes: int = 50 * 1024 * 1024
    max_snapshot_bytes: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        for value in (
            self.max_patch_bytes,
            self.max_untracked_file_bytes,
            self.max_snapshot_bytes,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("snapshot size limits must be positive integers")
        self.mirror_root = self._prepare_root(Path(self.mirror_root), "mirror_root")
        self.worktree_root = self._prepare_root(Path(self.worktree_root), "worktree_root")
        self._controlled_git_home = self._prepare_root(
            self.mirror_root / ".git-home", "controlled_git_home"
        )
        self._controlled_git_temp = self._prepare_root(
            self.mirror_root / ".git-temp", "controlled_git_temp"
        )
        if self.command_runner is _default_command_runner:
            self.command_runner = self._run_default_command

    def _git_environment(self) -> dict[str, str]:
        try:
            supplied = self.credential_env_provider()
            identity = self.identity_env_provider()
        except Exception:
            raise RepositoryBoundaryError("explicit Git environment is unavailable") from None
        if not isinstance(supplied, Mapping):
            raise RepositoryBoundaryError("explicit Git environment is invalid")
        identity_values = validate_git_identity_environment(identity)
        environment = _isolated_git_environment(
            supplied,
            controlled_home=self._controlled_git_home,
            controlled_temp=self._controlled_git_temp,
        )
        environment.update(identity_values)
        return environment

    def _run_default_command(
        self, command: Sequence[str], cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            list(command), cwd=cwd, env=self._git_environment(), shell=False,
            capture_output=True, check=False,
        )

    @staticmethod
    def _prepare_root(path: Path, name: str) -> Path:
        if path.exists() and _is_link_or_reparse(path):
            raise RepositoryBoundaryError(f"{name} must not be a link or reparse point")
        if path.exists() and not path.is_dir():
            raise RepositoryBoundaryError(f"{name} must be a directory")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RepositoryBoundaryError(f"{name} could not be created") from error
        if _is_link_or_reparse(path):
            raise RepositoryBoundaryError(f"{name} must not be a link or reparse point")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise RepositoryBoundaryError(f"{name} must be a directory")
        return resolved

    def _execute(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        operation: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = self.command_runner(tuple(command), cwd)
        except (OSError, subprocess.SubprocessError) as error:
            raise RepositoryCommandError(f"{operation} could not be started") from error
        if check and completed.returncode != 0:
            raise RepositoryCommandError(
                f"{operation} failed with exit code {completed.returncode}"
            )
        return completed

    def _output(
        self, command: Sequence[str], *, cwd: Path | None = None, operation: str
    ) -> bytes:
        return self._execute(command, cwd=cwd, operation=operation).stdout.strip()

    def content_sha256(self, prepared: PreparedWorktree, repository_path: str) -> str:
        """Hash one regular repository-relative file without following links."""
        RepositorySnapshot._validate_repository_path(repository_path)
        root = prepared.path.resolve(strict=True)
        target = root / repository_path
        descriptor = _open_readonly_nofollow(target, worktree=root)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                while chunk := stream.read(64 * 1024):
                    total += len(chunk)
                    if total > self.max_untracked_file_bytes:
                        raise SnapshotTooLargeError("content hash input exceeds configured limit")
                    digest.update(chunk)
        except OSError as error:
            raise RepositoryBoundaryError("repository content could not be hashed") from error
        return digest.hexdigest()

    @staticmethod
    def _safe_repo_name(value: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]+", value)
            or value in {".", ".."}
            or value.casefold().endswith(".lock")
            or value.startswith("-")
        ):
            raise RepositoryBoundaryError("repo_name is not a safe path segment")
        return value

    @staticmethod
    def _safe_run_id(value: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]+", value or "")
            or value in {".", ".."}
            or value.startswith("-")
            or value.casefold().endswith(".lock")
        ):
            raise RepositoryBoundaryError("run_id is not a safe path segment")
        return value

    def _mirror_path(self, mapping: RepositoryMapping) -> Path:
        name = self._safe_repo_name(mapping.repo_name)
        candidate = (self.mirror_root / f"{name}.git").resolve(strict=False)
        if not _is_within(candidate, self.mirror_root):
            raise RepositoryBoundaryError("mirror path escapes mirror_root")
        return candidate

    def _target_path(self, run_id: str, repository_key: str | None = None) -> Path:
        run_root = (self.worktree_root / self._safe_run_id(run_id)).resolve(strict=False)
        candidate = run_root
        if repository_key is not None:
            candidate = (run_root / self._safe_repo_name(repository_key)).resolve(strict=False)
        if not _is_within(candidate, self.worktree_root):
            raise RepositoryBoundaryError("worktree path escapes worktree_root")
        return candidate

    def _prepare_group_parent(self, run_id: str) -> Path:
        parent = self._target_path(run_id)
        try:
            parent.mkdir(exist_ok=True)
        except OSError as error:
            raise RepositoryBoundaryError("group worktree parent could not be created") from error
        if _is_link_or_reparse(parent) or not parent.is_dir():
            raise RepositoryBoundaryError("group worktree parent is not a real directory")
        if not _is_within(parent.resolve(strict=True), self.worktree_root):
            raise RepositoryBoundaryError("group worktree parent escapes worktree_root")
        return parent.resolve(strict=True)

    @staticmethod
    def _normalized_url(value: str) -> tuple[str, str]:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise MirrorOriginMismatch("mirror origin is not a safe URL")
        local = Path(value)
        if local.is_absolute():
            return "local", os.path.normcase(str(local.resolve(strict=False)))
        scp = re.fullmatch(r"([^@\s]+@)?([^:\s]+):(.+)", value)
        if scp:
            user = scp.group(1) or ""
            return "remote", f"{user}{scp.group(2).lower()}:{scp.group(3)}"
        try:
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment:
                raise MirrorOriginMismatch("mirror origin is not a safe URL")
            if parsed.scheme.lower() in {"http", "https"} and (
                parsed.username is not None or parsed.password is not None
            ):
                raise MirrorOriginMismatch("mirror origin is not a safe URL")
            if parsed.password is not None:
                raise MirrorOriginMismatch("mirror origin is not a safe URL")
            hostname = parsed.hostname
            if not parsed.scheme or hostname is None:
                raise MirrorOriginMismatch("mirror origin is not a safe URL")
            host = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
            user = f"{parsed.username}@" if parsed.username is not None else ""
            port = f":{parsed.port}" if parsed.port is not None else ""
        except (TypeError, ValueError) as error:
            if isinstance(error, MirrorOriginMismatch):
                raise
            raise MirrorOriginMismatch("mirror origin is not a safe URL") from error
        normalized = urlunsplit(
            (parsed.scheme.lower(), f"{user}{host}{port}", parsed.path, "", "")
        )
        return "remote", normalized

    def _local_source_facts(
        self, mapping: RepositoryMapping
    ) -> tuple[Path, str, bytes, tuple[int, int, int, int]] | None:
        if mapping.source_path is None:
            return None
        try:
            source = Path(mapping.source_path).resolve(strict=True)
        except OSError as error:
            raise RepositoryBoundaryError("local source repository does not exist") from error
        if _is_link_or_reparse(source) or not source.is_dir():
            raise RepositoryBoundaryError("local source repository is not a real directory")
        trusted_source = f"safe.directory={source}"
        origin = self._output(
            [
                "git", "-c", trusted_source, "-C", str(source),
                "remote", "get-url", "origin",
            ],
            operation="local source origin validation",
        ).decode("utf-8", "surrogateescape")
        if self._normalized_url(origin) != self._normalized_url(mapping.repo_url):
            raise MirrorOriginMismatch("local source origin does not match mapping")
        head = self._output(
            [
                "git", "-c", trusted_source, "-C", str(source),
                "rev-parse", "--verify", "HEAD^{commit}",
            ],
            operation="local source HEAD validation",
        ).decode("ascii")
        status = self._execute(
            [
                "git", "-c", trusted_source, "-C", str(source),
                "status", "--porcelain=v1", "-z",
                "--untracked-files=all",
            ],
            operation="local source status validation",
        ).stdout
        return source, _canonical_commit_oid(head), status, _path_identity(source)

    def _ensure_mirror(self, mapping: RepositoryMapping) -> Path:
        source_facts = self._local_source_facts(mapping)
        mirror = self._mirror_path(mapping)
        if mirror.exists():
            if _is_link_or_reparse(mirror) or not mirror.is_dir():
                raise RepositoryBoundaryError("mirror must be a real directory")
            bare_result = self._execute(
                ["git", "--git-dir", str(mirror), "rev-parse", "--is-bare-repository"],
                operation="mirror validation",
                check=False,
            )
            if bare_result.returncode != 0 or bare_result.stdout.strip() != b"true":
                raise RepositoryIdentityError("existing mirror is not a bare repository")
            origin = self._output(
                ["git", "--git-dir", str(mirror), "remote", "get-url", "origin"],
                operation="mirror origin validation",
            ).decode("utf-8", "surrogateescape")
            try:
                origin_identity = self._normalized_url(origin)
                mapping_identity = self._normalized_url(mapping.repo_url)
            except MirrorOriginMismatch:
                raise
            if origin_identity != mapping_identity:
                raise MirrorOriginMismatch("existing mirror origin does not match mapping")
        else:
            clone_source = mapping.repo_url if source_facts is None else str(source_facts[0])
            clone_command = ["git"]
            if source_facts is not None:
                clone_command.extend(
                    [
                        "-c", f"safe.directory={source_facts[0]}",
                        "-c", f"safe.directory={source_facts[0] / '.git'}",
                    ]
                )
            clone_command.extend(
                ["clone", "--bare", clone_source, str(mirror)]
            )
            self._execute(
                clone_command,
                operation="mirror clone",
            )
            if source_facts is not None:
                self._execute(
                    [
                        "git", "--git-dir", str(mirror), "remote", "set-url", "origin",
                        mapping.repo_url,
                    ],
                    operation="mirror authoritative origin configuration",
                )
        self._execute(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "remote.origin.fetch",
                "+refs/heads/*:refs/remotes/origin/*",
            ],
            operation="mirror fetch configuration",
        )
        fetch_command = ["git"]
        fetch_source = "origin"
        fetch_refspec: list[str] = []
        if source_facts is not None:
            source = source_facts[0]
            fetch_command.extend(
                [
                    "-c", f"safe.directory={source}",
                    "-c", f"safe.directory={source / '.git'}",
                ]
            )
            fetch_source = str(source)
            fetch_refspec.append(
                "+refs/heads/*:refs/remotes/origin/*"
            )
        fetch_command.extend(
            [
                "--git-dir", str(mirror), "fetch", "--prune",
                fetch_source, *fetch_refspec,
            ]
        )
        self._execute(fetch_command, operation="mirror fetch")
        if source_facts is not None and self._local_source_facts(mapping) != source_facts:
            raise RepositoryIdentityError("local source repository changed during mirror setup")
        return mirror

    def _ref_exists(self, mirror: Path, ref: str) -> bool:
        result = self._execute(
            ["git", "--git-dir", str(mirror), "show-ref", "--verify", "--quiet", ref],
            operation="reference check",
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise RepositoryCommandError(
                f"reference check failed with exit code {result.returncode}"
            )
        return result.returncode == 0

    @staticmethod
    def _validate_work_branch(branch: str) -> str:
        try:
            validate_git_ref_name(branch)
        except ValueError as error:
            raise RepositoryBoundaryError("branch is not a safe Git ref") from error
        if not branch.startswith(("requirement/", "bugfix/")):
            raise RepositoryBoundaryError("branch must use requirement/ or bugfix/")
        return branch

    def prepare(
        self,
        run_id: str,
        mapping: RepositoryMapping,
        branch: str,
        *,
        repository_key: str | None = None,
    ) -> PreparedWorktree:
        branch = self._validate_work_branch(branch)
        if repository_key is not None:
            self._prepare_group_parent(run_id)
        target = self._target_path(run_id, repository_key)
        mirror = self._ensure_mirror(mapping)
        base_ref = f"refs/remotes/origin/{mapping.base_branch}"
        if not self._ref_exists(mirror, base_ref):
            raise BaseBranchNotFound("configured remote base branch was not found")
        if self._ref_exists(mirror, f"refs/remotes/origin/{branch}"):
            raise BranchAlreadyExists("requested branch already exists")
        base_commit = self._output(
            [
                "git",
                "--git-dir",
                str(mirror),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{base_ref}^{{commit}}",
            ],
            operation="base commit resolution",
        ).decode("ascii")
        base_commit = _canonical_commit_oid(base_commit)
        local_branch_ref = f"refs/heads/{branch}"
        reuse_branch = False
        if self._ref_exists(mirror, local_branch_ref):
            existing_oid = self._output(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{local_branch_ref}^{{commit}}",
                ],
                operation="existing branch identity resolution",
            ).decode("ascii")
            registrations = self._registered_worktrees(mirror)
            if (
                _canonical_commit_oid(existing_oid) != base_commit
                or registrations is None
                or any(
                    registration.branch == local_branch_ref
                    for registration in registrations.values()
                )
            ):
                raise BranchAlreadyExists("requested branch already exists")
            reuse_branch = True
        try:
            os.mkdir(target)
        except FileExistsError as error:
            raise TargetExists("worktree target is already reserved") from error
        except OSError as error:
            raise RepositoryBoundaryError("worktree target could not be reserved") from error
        reservation_identity = _path_identity(target)
        if _is_link_or_reparse(target):
            raise RepositoryBoundaryError("worktree reservation must not be a reparse point")
        reservation_owned = True
        branch_created = False
        created_oid: str | None = None
        worktree_added = False
        try:
            if not reuse_branch:
                self._execute(
                    [
                        "git",
                        "--git-dir",
                        str(mirror),
                        "branch",
                        "--end-of-options",
                        branch,
                        base_commit,
                    ],
                    operation="branch creation",
                )
                branch_created = True
                created_oid = base_commit
            self._execute(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "worktree",
                    "add",
                    "--",
                    str(target),
                    branch,
                ],
                operation="worktree creation",
            )
            worktree_added = True
            head = self._head(target)
            status = self._status(target)
            if head != base_commit or status:
                raise RepositoryIdentityError("new worktree is not clean at the resolved base")
        except Exception:
            self._cleanup_failed_worktree(
                mirror,
                target,
                branch,
                reservation_owned,
                reservation_identity,
                branch_created,
                created_oid,
                worktree_added,
            )
            raise
        return PreparedWorktree(
            path=target,
            branch=branch,
            base_commit=base_commit,
            head_commit=head,
            mirror_path=mirror,
        )

    def recover(
        self,
        run_id: str,
        mapping: RepositoryMapping,
        branch: str,
        *,
        repository_key: str | None = None,
    ) -> PreparedWorktree | None:
        """Recover only an exact, complete worktree left by this deterministic run."""

        branch = self._validate_work_branch(branch)
        target = self._target_path(run_id, repository_key)
        if not target.exists():
            return None
        if _is_link_or_reparse(target) or not target.is_dir():
            raise RepositoryIdentityError("worktree recovery target is not a real directory")
        mirror_candidate = self._mirror_path(mapping)
        if not mirror_candidate.exists():
            raise RepositoryIdentityError("worktree recovery mirror is missing")
        mirror = self._ensure_mirror(mapping)
        if self._ref_exists(mirror, f"refs/remotes/origin/{branch}"):
            raise BranchAlreadyExists(
                "requested branch appeared remotely before worktree recovery"
            )
        base_ref = f"refs/remotes/origin/{mapping.base_branch}"
        local_branch_ref = f"refs/heads/{branch}"
        if not self._ref_exists(mirror, base_ref) or not self._ref_exists(
            mirror, local_branch_ref
        ):
            raise RepositoryIdentityError("worktree recovery refs are incomplete")
        base_commit = _canonical_commit_oid(
            self._output(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{base_ref}^{{commit}}",
                ],
                operation="recovery base identity resolution",
            ).decode("ascii")
        )
        branch_commit = _canonical_commit_oid(
            self._output(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{local_branch_ref}^{{commit}}",
                ],
                operation="recovery branch identity resolution",
            ).decode("ascii")
        )
        registrations = self._registered_worktrees(mirror)
        registration = (
            registrations.get(target.resolve(strict=True))
            if registrations is not None
            else None
        )
        head = self._head(target)
        if (
            registration is None
            or registration.branch != local_branch_ref
            or _canonical_commit_oid(registration.head) != head
            or head != branch_commit
            or branch_commit != base_commit
            or self._status(target)
        ):
            raise RepositoryIdentityError(
                "worktree recovery identity does not match the expected clean base"
            )
        return PreparedWorktree(
            path=target,
            branch=branch,
            base_commit=base_commit,
            head_commit=head,
            mirror_path=mirror,
        )

    def resolve_repository_path(
        self,
        prepared: PreparedWorktree,
        mapping: RepositoryMapping,
        repository_path: str,
    ) -> Path:
        """Resolve one configured repository path without weakening snapshot gates."""

        RepositorySnapshot._validate_repository_path(repository_path)
        worktree, _ = self._validate_identity(prepared, mapping)
        if not self._allowed(repository_path, mapping.allowed_paths):
            raise RepositoryBoundaryError("repository path is outside allowed_paths")
        return self._validate_changed_path(worktree, repository_path)

    def _cleanup_failed_worktree(
        self,
        mirror: Path,
        target: Path,
        branch: str,
        reservation_owned: bool,
        reservation_identity: tuple[int, int, int, int],
        branch_created: bool,
        created_oid: str | None,
        worktree_added: bool,
    ) -> None:
        if not reservation_owned or not _is_within(
            target.resolve(strict=False), self.worktree_root
        ):
            return
        try:
            identity_matches = (
                target.exists()
                and not _is_link_or_reparse(target)
                and _path_identity(target) == reservation_identity
            )
        except OSError:
            identity_matches = False
        registered = self._registered_worktrees(mirror)
        if identity_matches and registered is not None:
            registration = registered.get(target.resolve(strict=False))
            if (
                worktree_added
                and registration is not None
                and registration.branch == f"refs/heads/{branch}"
            ):
                self._execute(
                    [
                        "git",
                        "--git-dir",
                        str(mirror),
                        "worktree",
                        "remove",
                        "--force",
                        str(target),
                    ],
                    operation="failed worktree cleanup",
                    check=False,
                )
            elif registration is None and not any(target.iterdir()):
                try:
                    target.rmdir()
                except OSError:
                    pass
        # Branch refs are intentionally retained. External Git can attach a worktree
        # after any observation, so deletion cannot be made safe without cooperation.

    def _registered_worktrees(
        self, mirror: Path
    ) -> dict[Path, _WorktreeRegistration] | None:
        payload = self._execute(
            ["git", "--git-dir", str(mirror), "worktree", "list", "--porcelain", "-z"],
            operation="worktree registration check",
            check=False,
        )
        if payload.returncode != 0:
            return None
        records: dict[Path, _WorktreeRegistration] = {}
        current_path: Path | None = None
        current_branch = ""
        current_head = ""
        for item in payload.stdout.split(b"\0"):
            if item.startswith(b"worktree "):
                if current_path is not None:
                    records[current_path] = _WorktreeRegistration(
                        branch=current_branch, head=current_head
                    )
                raw = item[len(b"worktree ") :].decode("utf-8", "surrogateescape")
                current_path = Path(raw).resolve(strict=False)
                current_branch = ""
                current_head = ""
            elif item.startswith(b"branch "):
                current_branch = item[len(b"branch ") :].decode(
                    "utf-8", "surrogateescape"
                )
            elif item.startswith(b"HEAD "):
                current_head = item[len(b"HEAD ") :].decode("ascii", "strict")
        if current_path is not None:
            records[current_path] = _WorktreeRegistration(
                branch=current_branch, head=current_head
            )
        return records

    def _head(self, worktree: Path) -> str:
        oid = self._output(
            [
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--verify",
                "--end-of-options",
                "HEAD^{commit}",
            ],
            operation="HEAD resolution",
        ).decode("ascii")
        return _canonical_commit_oid(oid)

    def _status(self, worktree: Path) -> bytes:
        return self._execute(
            [
                "git",
                "-C",
                str(worktree),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            operation="worktree status",
        ).stdout

    def _validate_identity(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping | None = None
    ) -> tuple[Path, Path]:
        try:
            worktree = Path(prepared.path).resolve(strict=True)
            mirror = Path(prepared.mirror_path).resolve(strict=True)
        except OSError as error:
            raise RepositoryIdentityError(
                "prepared worktree paths do not exist"
            ) from error
        self._validate_work_branch(prepared.branch)
        _canonical_commit_oid(prepared.base_commit)
        _canonical_commit_oid(prepared.head_commit)
        expected_mirror = self._mirror_path(mapping) if mapping is not None else mirror
        if (
            not _is_within(worktree, self.worktree_root)
            or not _is_within(mirror, self.mirror_root)
            or mirror != expected_mirror
            or _is_link_or_reparse(worktree)
            or _is_link_or_reparse(mirror)
        ):
            raise RepositoryIdentityError("prepared worktree does not match repository roots")
        try:
            common_raw = self._output(
                ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
                operation="worktree identity validation",
            ).decode("utf-8", "surrogateescape")
        except RepositoryCommandError as error:
            raise RepositoryIdentityError("worktree Git identity is invalid") from error
        common = Path(common_raw)
        if not common.is_absolute():
            common = worktree / common
        if common.resolve(strict=True) != mirror:
            raise RepositoryIdentityError("worktree does not belong to the expected mirror")
        registrations = self._registered_worktrees(mirror)
        registration = registrations.get(worktree) if registrations is not None else None
        expected_branch = f"refs/heads/{prepared.branch}"
        if registration is None or registration.branch != expected_branch:
            raise RepositoryIdentityError("worktree registration identity does not match")
        registration_head = _canonical_commit_oid(registration.head)
        symbolic = self._execute(
            ["git", "-C", str(worktree), "symbolic-ref", "--quiet", "HEAD"],
            operation="worktree symbolic branch validation",
            check=False,
        )
        if symbolic.returncode != 0 or symbolic.stdout.strip().decode(
            "utf-8", "surrogateescape"
        ) != expected_branch:
            raise RepositoryIdentityError("worktree symbolic branch identity does not match")
        if self._head(worktree) != registration_head:
            raise RepositoryIdentityError("worktree registered HEAD identity does not match")
        return worktree, mirror

    @staticmethod
    def _safe_relative_path(raw: bytes, *, windows: bool | None = None) -> str:
        value = raw.decode("utf-8", "surrogateescape")
        is_windows = windows if windows is not None else os.name == "nt"
        if is_windows:
            value = value.replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or path.parts[0].casefold() == ".git"
        ):
            raise RepositoryBoundaryError("Git reported an unsafe repository path")
        return path.as_posix()

    def _parse_status(self, payload: bytes) -> tuple[set[str], set[str]]:
        entries = payload.split(b"\0")
        changed: set[str] = set()
        untracked: set[str] = set()
        index = 0
        while index < len(entries) and entries[index]:
            entry = entries[index]
            if len(entry) < 4 or entry[2:3] != b" ":
                raise RepositoryIdentityError("Git returned an invalid status record")
            code = entry[:2]
            path = self._safe_relative_path(entry[3:])
            changed.add(path)
            if code == b"??":
                untracked.add(path)
            if b"R" in code or b"C" in code:
                index += 1
                if index >= len(entries) or not entries[index]:
                    raise RepositoryIdentityError("Git returned an incomplete rename record")
                changed.add(self._safe_relative_path(entries[index]))
            index += 1
        return changed, untracked

    def _validate_changed_path(self, worktree: Path, relative: str) -> Path:
        candidate = worktree.joinpath(*PurePosixPath(relative).parts)
        current = worktree
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if _is_link_or_reparse(current):
                    resolved = current.resolve(strict=False)
                    if not _is_within(resolved, worktree):
                        raise RepositoryBoundaryError(
                            "changed link or reparse point escapes worktree"
                        )
        resolved = candidate.resolve(strict=False)
        if not _is_within(resolved, worktree):
            raise RepositoryBoundaryError("changed path escapes worktree")
        return candidate

    @staticmethod
    def _allowed(relative: str, prefixes: tuple[str, ...]) -> bool:
        return not prefixes or any(
            relative == prefix or relative.startswith(f"{prefix}/") for prefix in prefixes
        )

    def snapshot(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping
    ) -> RepositorySnapshot:
        worktree, _ = self._validate_identity(prepared, mapping)
        base_commit = _canonical_commit_oid(prepared.base_commit)
        expected_head = _canonical_commit_oid(prepared.head_commit)
        self._validate_mirror_commit(Path(prepared.mirror_path), base_commit)
        self._validate_mirror_commit(Path(prepared.mirror_path), expected_head)
        head = self._head(worktree)
        self._validate_mirror_commit(Path(prepared.mirror_path), head)
        status = self._status(worktree)
        changed, untracked = self._parse_status(status)
        for relative in changed:
            self._validate_changed_path(worktree, relative)
            if not self._allowed(relative, mapping.allowed_paths):
                raise RepositoryBoundaryError("changed path is outside allowed_paths")
        patch_bytes = self._read_patch(worktree, base_commit)
        snapshot_size = len(patch_bytes)
        if snapshot_size > self.max_snapshot_bytes:
            raise SnapshotTooLargeError("repository snapshot exceeds configured size limit")
        untracked_hashes: dict[str, str] = {}
        untracked_types: dict[str, str] = {}
        for relative in sorted(untracked):
            path = self._validate_changed_path(worktree, relative)
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_size > self.max_untracked_file_bytes:
                    raise SnapshotTooLargeError(
                        "untracked file exceeds configured size limit"
                    )
                digest, content_size = self._hash_untracked_file(path, metadata)
                kind = "file"
            elif stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                target_bytes = os.fsencode(target)
                content_size = len(target_bytes)
                if content_size > self.max_untracked_file_bytes:
                    raise SnapshotTooLargeError(
                        "untracked link exceeds configured size limit"
                    )
                digest = hashlib.sha256(target_bytes).hexdigest()
                kind = "symlink"
            else:
                raise RepositoryBoundaryError("untracked path has an unsupported file type")
            untracked_hashes[relative] = digest
            untracked_types[relative] = kind
            snapshot_size += content_size
            if snapshot_size > self.max_snapshot_bytes:
                raise SnapshotTooLargeError(
                    "repository snapshot exceeds configured size limit"
                )
        aggregate = hashlib.sha256()
        aggregate.update(patch_bytes)
        for relative in sorted(untracked_hashes):
            aggregate.update(b"\0untracked\0")
            aggregate.update(relative.encode("utf-8", "surrogateescape"))
            aggregate.update(b"\0")
            aggregate.update(untracked_types[relative].encode("ascii"))
            aggregate.update(b"\0")
            aggregate.update(untracked_hashes[relative].encode("ascii"))
        return RepositorySnapshot(
            head_commit=head,
            diff_sha256=aggregate.hexdigest(),
            changed_files=tuple(sorted(changed)),
            patch=patch_bytes.decode("utf-8", "surrogateescape"),
            untracked_hashes=untracked_hashes,
            is_clean=not changed,
        )

    def contains_sensitive_content(
        self,
        prepared: PreparedWorktree,
        mapping: RepositoryMapping,
        secrets: tuple[str, ...],
    ) -> bool:
        """Safely scan changed regular files and link targets for exact secrets."""

        needles = tuple(
            secret.encode("utf-8", "strict") for secret in secrets if secret
        )
        if not needles:
            return False
        worktree, _ = self._validate_identity(prepared, mapping)
        initial_status = self._status(worktree)
        changed, _ = self._parse_status(initial_status)
        total_size = 0
        for relative in sorted(changed):
            path = self._validate_changed_path(worktree, relative)
            if not self._allowed(relative, mapping.allowed_paths):
                raise RepositoryBoundaryError("changed path is outside allowed_paths")
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue  # Deleted side of a delete or rename has no live content.
            except OSError as error:
                raise RepositoryBoundaryError(
                    "changed file could not be inspected"
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    content = os.fsencode(os.readlink(path))
                except OSError as error:
                    raise RepositoryBoundaryError(
                        "changed link could not be safely read"
                    ) from error
                size = len(content)
                if size > self.max_untracked_file_bytes:
                    raise SnapshotTooLargeError(
                        "changed link exceeds configured size limit"
                    )
                found = any(needle in content for needle in needles)
            elif stat.S_ISREG(metadata.st_mode):
                if _is_link_or_reparse(path):
                    raise RepositoryBoundaryError(
                        "changed regular file is a reparse point"
                    )
                if metadata.st_size > self.max_untracked_file_bytes:
                    raise SnapshotTooLargeError(
                        "changed file exceeds configured size limit"
                    )
                found, size = self._scan_regular_file_for_needles(
                    path, metadata, needles, worktree
                )
            else:
                raise RepositoryBoundaryError(
                    "changed path has an unsupported file type"
                )
            total_size += size
            if total_size > self.max_snapshot_bytes:
                raise SnapshotTooLargeError(
                    "changed file scan exceeds configured size limit"
                )
            if found:
                return True
        if self._status(worktree) != initial_status:
            raise RepositoryBoundaryError("changed file set changed during scan")
        return False

    def _scan_regular_file_for_needles(
        self,
        path: Path,
        initial: os.stat_result,
        needles: tuple[bytes, ...],
        worktree: Path,
    ) -> tuple[bool, int]:
        descriptor = _open_readonly_nofollow(path, worktree=worktree)
        size = 0
        found = False
        tail = b""
        overlap = max(len(needle) for needle in needles) - 1
        try:
            opened = os.fstat(descriptor)
            if (
                _path_identity_from_stat(opened) != _path_identity_from_stat(initial)
                or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(initial.st_mode)
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise RepositoryBoundaryError("changed file identity changed")
            while chunk := os.read(descriptor, 64 * 1024):
                size += len(chunk)
                if size > self.max_untracked_file_bytes:
                    raise SnapshotTooLargeError(
                        "changed file exceeds configured size limit"
                    )
                candidate = tail + chunk
                if any(needle in candidate for needle in needles):
                    found = True
                tail = candidate[-overlap:] if overlap > 0 else b""
            final_opened = os.fstat(descriptor)
            if (
                _path_identity_from_stat(final_opened)
                != _path_identity_from_stat(opened)
                or final_opened.st_size != opened.st_size
                or final_opened.st_mtime_ns != opened.st_mtime_ns
            ):
                raise RepositoryBoundaryError("changed file identity changed")
        finally:
            os.close(descriptor)
        try:
            final_path = path.lstat()
            if (
                _path_identity_from_stat(final_path)
                != _path_identity_from_stat(initial)
                or final_path.st_size != initial.st_size
                or final_path.st_mtime_ns != initial.st_mtime_ns
            ):
                raise RepositoryBoundaryError("changed file identity changed")
        except OSError as error:
            raise RepositoryBoundaryError("changed file identity changed") from error
        return found, size

    def _read_patch(self, worktree: Path, base_commit: str) -> bytes:
        command = [
            "git",
            "-C",
            str(worktree),
            "diff",
            "--binary",
            "--full-index",
            "--end-of-options",
            base_commit,
            "--",
        ]
        try:
            process = subprocess.Popen(
                command,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._git_environment(),
            )
        except OSError as error:
            raise RepositoryCommandError("worktree diff could not be started") from error
        def drain_stderr() -> None:
            assert process.stderr is not None
            while process.stderr.read(8192):
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        chunks: list[bytes] = []
        size = 0
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(64 * 1024):
                size += len(chunk)
                if size > min(self.max_patch_bytes, self.max_snapshot_bytes):
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise SnapshotTooLargeError("patch exceeds configured size limit")
                chunks.append(chunk)
            return_code = process.wait()
        finally:
            process.stdout.close()
            stderr_thread.join(timeout=2)
            if process.stderr is not None:
                process.stderr.close()
        if return_code != 0:
            raise RepositoryCommandError(
                f"worktree diff failed with exit code {return_code}"
            )
        return b"".join(chunks)

    def _hash_untracked_file(
        self, path: Path, initial: os.stat_result
    ) -> tuple[str, int]:
        if _is_link_or_reparse(path):
            raise RepositoryBoundaryError("untracked regular file is a reparse point")
        descriptor = _open_readonly_nofollow(path)
        digest = hashlib.sha256()
        size = 0
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != initial.st_dev
                or opened.st_ino != initial.st_ino
                or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(initial.st_mode)
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise RepositoryBoundaryError("untracked file identity changed")
            while chunk := os.read(descriptor, 64 * 1024):
                size += len(chunk)
                if size > self.max_untracked_file_bytes:
                    raise SnapshotTooLargeError(
                        "untracked file exceeds configured size limit"
                    )
                digest.update(chunk)
            final_opened = os.fstat(descriptor)
            if (
                _path_identity_from_stat(final_opened) != _path_identity_from_stat(opened)
                or final_opened.st_size != opened.st_size
                or final_opened.st_mtime_ns != opened.st_mtime_ns
            ):
                raise RepositoryBoundaryError("untracked file identity changed")
        finally:
            os.close(descriptor)
        try:
            final_path = path.lstat()
            if (
                _path_identity_from_stat(final_path) != _path_identity_from_stat(initial)
                or final_path.st_size != initial.st_size
                or final_path.st_mtime_ns != initial.st_mtime_ns
            ):
                raise RepositoryBoundaryError("untracked file identity changed")
        except OSError as error:
            raise RepositoryBoundaryError("untracked file identity changed") from error
        return digest.hexdigest(), size

    def assert_head_unchanged(
        self, prepared: PreparedWorktree, expected: str | None = None
    ) -> None:
        expected_oid = _canonical_commit_oid(expected or prepared.head_commit)
        worktree, mirror = self._validate_identity(prepared)
        self._validate_mirror_commit(mirror, expected_oid)
        actual = self._head(worktree)
        self._validate_mirror_commit(mirror, actual)
        if actual != expected_oid:
            raise HeadChangedError("worktree HEAD changed")

    def remote_base_oid(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping
    ) -> str:
        """Read the exact remote base ref without fetching or mutating local Git state."""

        worktree, _ = self._validate_identity(prepared, mapping)
        branch = validate_git_ref_name(mapping.base_branch)
        reference = f"refs/heads/{branch}"
        result = self._execute(
            ["git", "-C", str(worktree), "ls-remote", "--refs", "origin", reference],
            operation="remote base branch inspection",
        )
        records = result.stdout.strip().splitlines()
        if len(records) != 1:
            raise RepositoryIdentityError("remote base branch identity is unavailable or ambiguous")
        fields = records[0].split()
        if len(fields) != 2 or fields[1] != reference.encode("ascii"):
            raise RepositoryIdentityError("remote base branch response is invalid")
        return _canonical_commit_oid(fields[0].decode("ascii"))

    def assert_remote_base_unchanged(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping
    ) -> None:
        if self.remote_base_oid(prepared, mapping) != prepared.base_commit:
            raise RemoteBaseChangedError("remote base branch changed after approval")

    def _publication_context(
        self, run: WorkflowRun
    ) -> tuple[PreparedWorktree, RepositoryMapping, RepositorySnapshot, Path]:
        prepared = run.prepared_worktree
        mapping = run.repository
        tested = run.tested_snapshot
        if prepared is None or mapping is None or tested is None or run.approval is None:
            raise RepositoryIdentityError("publication repository evidence is incomplete")
        worktree, _ = self._validate_identity(prepared, mapping)
        return prepared, mapping, tested, worktree

    def prepare_commit_intent(self, run: WorkflowRun, approval: ApprovalPackage) -> str:
        """Stage exactly approved paths and return the deterministic tree object id."""

        prepared, mapping, tested, worktree = self._publication_context(run)
        self.assert_remote_base_unchanged(prepared, mapping)
        current = self.snapshot(prepared, mapping)
        if current.model_dump(mode="json") != tested.model_dump(mode="json"):
            raise RepositoryIdentityError("repository snapshot changed after testing")
        if (
            approval.repository != mapping
            or approval.head_commit != prepared.head_commit
            or approval.diff_hash != tested.diff_sha256
            or approval.changed_files != tested.changed_files
            or approval.branch != prepared.branch
            or not approval.commit_message.strip()
            or "\x00" in approval.commit_message
        ):
            raise RepositoryIdentityError("approval does not match the tested repository snapshot")
        descriptor, temporary_index = tempfile.mkstemp(
            prefix="ones-dev-index-", dir=prepared.mirror_path
        )
        os.close(descriptor)
        os.unlink(temporary_index)
        environment = self._git_environment()
        environment["GIT_INDEX_FILE"] = temporary_index
        try:
            for command, operation in (
                (["git", "-C", str(worktree), "read-tree", prepared.head_commit], "approved temporary index initialization"),
                (["git", "-C", str(worktree), "add", "-A", "--", *tested.changed_files], "approved temporary path staging"),
            ):
                completed = subprocess.run(
                    command, cwd=worktree, env=environment, shell=False,
                    capture_output=True, check=False,
                )
                if completed.returncode != 0:
                    raise RepositoryCommandError(
                        f"{operation} failed with exit code {completed.returncode}"
                    )
            completed = subprocess.run(
                ["git", "-C", str(worktree), "write-tree"],
                cwd=worktree, env=environment, shell=False,
                capture_output=True, check=False,
            )
            if completed.returncode != 0:
                raise RepositoryCommandError(
                    f"approved tree creation failed with exit code {completed.returncode}"
                )
            return _canonical_commit_oid(completed.stdout.strip().decode("ascii"))
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            if isinstance(error, RepositoryError):
                raise
            raise RepositoryCommandError("approved tree could not be created") from error
        finally:
            try:
                os.unlink(temporary_index)
            except FileNotFoundError:
                pass

    def find_approved_commit(self, run: WorkflowRun) -> str | None:
        prepared, _, _, worktree = self._publication_context(run)
        publication = run.publication
        head = self._head(worktree)
        if head == publication.expected_parent:
            return None
        parent = self._output(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}^"],
            operation="published commit parent validation",
        ).decode("ascii")
        tree = self._output(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "--end-of-options", f"{head}^{{tree}}"],
            operation="published commit tree validation",
        ).decode("ascii")
        message = self._output(
            ["git", "-C", str(worktree), "show", "-s", "--format=%B", "--end-of-options", head],
            operation="published commit message validation",
        ).decode("utf-8", "strict")
        if (
            _canonical_commit_oid(parent) != publication.expected_parent
            or _canonical_commit_oid(tree) != publication.expected_tree
            or message != publication.commit_message
            or prepared.branch != publication.remote_branch
        ):
            raise RepositoryIdentityError("worktree HEAD is not the approved publication commit")
        return head

    def commit_approved(self, run: WorkflowRun) -> str:
        prepared, mapping, tested, worktree = self._publication_context(run)
        publication = run.publication
        if self.find_approved_commit(run) is not None:
            raise RepositoryIdentityError("approved commit already exists")
        current = self.snapshot(prepared, mapping)
        if current.model_dump(mode="json") != tested.model_dump(mode="json"):
            raise RepositoryIdentityError("repository snapshot changed before commit")
        tree = self.prepare_commit_intent(run, run.approval)
        if tree != publication.expected_tree:
            raise RepositoryIdentityError("approved snapshot does not match intended tree")
        command = [
            "git", "-C", str(worktree), "commit-tree", publication.expected_tree,
            "-p", publication.expected_parent,
        ]
        try:
            completed = subprocess.run(
                command,
                input=(publication.commit_message + "\n").encode("utf-8", "strict"),
                cwd=worktree,
                env=self._git_environment(),
                shell=False,
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            raise RepositoryCommandError("approved commit could not be created") from error
        if completed.returncode != 0:
            raise RepositoryCommandError(
                f"approved commit failed with exit code {completed.returncode}"
            )
        commit = _canonical_commit_oid(completed.stdout.strip().decode("ascii"))
        self._execute(
            [
                "git", "-C", str(worktree), "update-ref",
                f"refs/heads/{prepared.branch}", commit, publication.expected_parent,
            ],
            operation="approved branch update",
        )
        self._execute(
            ["git", "-C", str(worktree), "reset", "--hard", commit],
            operation="approved worktree synchronization",
        )
        if self.find_approved_commit(run) != commit:
            raise RepositoryIdentityError("approved commit fact could not be verified")
        return commit

    def remote_branch_oid(self, run: WorkflowRun) -> str | None:
        _, _, _, worktree = self._publication_context(run)
        branch = validate_git_ref_name(run.publication.remote_branch)
        result = self._execute(
            ["git", "-C", str(worktree), "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            operation="remote branch inspection",
        )
        output = result.stdout.strip()
        if not output:
            return None
        records = output.splitlines()
        if len(records) != 1:
            raise RepositoryIdentityError("remote branch identity is ambiguous")
        fields = records[0].split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{branch}".encode("ascii"):
            raise RepositoryIdentityError("remote branch response is invalid")
        return _canonical_commit_oid(fields[0].decode("ascii"))

    def push_approved(self, run: WorkflowRun) -> None:
        _, _, _, worktree = self._publication_context(run)
        publication = run.publication
        commit = _canonical_commit_oid(publication.commit_hash)
        branch = validate_git_ref_name(publication.remote_branch)
        remote = self.remote_branch_oid(run)
        if remote is not None:
            if remote != commit:
                raise RepositoryIdentityError("remote branch points to a different commit")
            return
        self._execute(
            ["git", "-C", str(worktree), "push", "origin", f"{commit}:refs/heads/{branch}"],
            operation="approved push",
        )

    def _validate_mirror_commit(self, mirror: Path, oid: str) -> None:
        canonical = _canonical_commit_oid(oid)
        result = self._execute(
            [
                "git",
                "--git-dir",
                str(mirror),
                "cat-file",
                "-e",
                "--",
                f"{canonical}^{{commit}}",
            ],
            operation="commit identity validation",
            check=False,
        )
        if result.returncode != 0:
            raise RepositoryIdentityError("commit identity is not present in the mirror")


__all__ = [
    "BaseBranchNotFound",
    "BranchAlreadyExists",
    "HeadChangedError",
    "MirrorOriginMismatch",
    "RepositoryBoundaryError",
    "RepositoryCommandError",
    "RepositoryError",
    "RepositoryIdentityError",
    "SnapshotTooLargeError",
    "TargetExists",
    "WorktreeRepository",
    "build_branch_name", "build_run_branch_name",
]
