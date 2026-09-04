"""macOS native Codex discovery and private-cache security adapters."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys

from .codex_runtime import (
    CODEX_EXECUTABLE_NAME,
    OPENAI_MACOS_CODESIGN_PUBLISHER,
    OPENAI_MACOS_TEAM_IDENTIFIER,
    LockedNativeCodex,
    NativeCodexIdentity,
    _RuntimeAdapter,
    _WindowsCacheRuntimeAdapter,
    _current_repository_roots,
    _inside_repository_by_identity,
    _stat_identity,
)


MACOS_CODE_MODE_HOST_NAME = "codex-code-mode-host"
_MACOS_NATIVE_RELATIVE_PATHS = {
    "arm64": Path("node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"),
    "x86_64": Path("node_modules/@openai/codex-darwin-x64/vendor/x86_64-apple-darwin/bin/codex"),
}


def _macos_machine_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    raise OSError("native Codex payload is unavailable")


def _trusted_chain(path: Path) -> None:
    """Reject links, foreign owners, and unsafe writable source ancestors."""
    for item in (path, *path.parents):
        value = item.lstat()
        # Homebrew commonly keeps current-user-owned ancestors (for example
        # /opt/homebrew/lib) group-writable.  The source file is still opened
        # no-follow, locked, verified by descriptor identity and required to
        # carry the expected Apple signature before it is copied into the
        # private cache.  Treat same-UID ancestor writes as trusted-user
        # administration rather than rejecting the standard installation.
        # Other-write is never safe here: even a root-owned sticky directory
        # permits an unrelated local user to replace descendants before open.
        other_writable = bool(value.st_mode & 0o002)
        foreign_group_writable = (
            bool(value.st_mode & 0o020) and value.st_uid != os.geteuid()
        )
        if (stat.S_ISLNK(value.st_mode) or value.st_uid not in {0, os.geteuid()}
                or other_writable or foreign_group_writable):
            raise OSError("native runtime path is unsafe")


class MacOSRuntimeAdapter:
    """Descriptor-first Mach-O and code-signing validation for macOS."""

    def __init__(self, *, _verify_signature: Callable[[Path], None] | None = None,
                 _architecture: str | None = None) -> None:
        if sys.platform != "darwin":
            raise OSError("macOS native runtime is unavailable")
        self._verify_signature = _verify_signature or _verify_macos_signature
        self._architecture = _architecture or _macos_machine_architecture()

    def open_locked(self, path: Path) -> int:
        import fcntl

        _trusted_chain(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if not self.is_disk_regular_non_reparse(descriptor):
                raise OSError("native runtime is not executable")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def is_disk_regular_non_reparse(self, descriptor: int) -> bool:
        value = os.fstat(descriptor)
        return (stat.S_ISREG(value.st_mode) and value.st_nlink == 1
                and value.st_uid in {0, os.geteuid()} and not value.st_mode & 0o022
                and bool(value.st_mode & 0o111))

    def identity(self, descriptor: int) -> NativeCodexIdentity:
        value = os.fstat(descriptor)
        return NativeCodexIdentity(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)

    def final_path(self, descriptor: int) -> Path:
        import fcntl
        # F_GETPATH resolves the open descriptor rather than trusting a path string.
        raw = fcntl.fcntl(descriptor, getattr(fcntl, "F_GETPATH", 50), bytes(1024))
        encoded = raw.split(b"\x00", 1)[0]
        if not encoded:
            raise OSError("could not resolve Codex descriptor")
        return Path(os.fsdecode(encoded)).resolve(strict=True)

    def verify_publisher(self, descriptor: int, path: Path) -> str:
        initial = self.identity(descriptor)
        canonical = path.resolve(strict=True)
        if self.final_path(descriptor) != canonical:
            raise OSError("native Codex payload changed")
        self.rewind(descriptor)
        header = self.read(descriptor, 8)
        self.rewind(descriptor)
        expected_cpu = 0x0100000C if self._architecture == "arm64" else 0x01000007
        if (len(header) != 8 or int.from_bytes(header[:4], "little") != 0xFEEDFACF
                or int.from_bytes(header[4:8], "little") != expected_cpu):
            raise OSError("native Codex architecture is invalid")
        self._verify_signature(canonical)
        if (self.identity(descriptor) != initial
                or not self.same_file(canonical, self.final_path(descriptor))):
            raise OSError("native Codex payload changed")
        return OPENAI_MACOS_CODESIGN_PUBLISHER

    def read(self, descriptor: int, size: int) -> bytes:
        return os.read(descriptor, size)

    def rewind(self, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)

    def same_file(self, left: Path, right: Path) -> bool:
        return os.path.samefile(left, right)

    def repository_marker(self, root: Path) -> tuple[str, tuple[int, int, int, int]] | None:
        for name in (".git", ".hg", ".svn"):
            marker = root / name
            try:
                value = marker.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if marker.is_symlink():
                raise OSError("invalid repository marker")
            kind = "directory" if stat.S_ISDIR(value.st_mode) else "file"
            if kind == "file" and not stat.S_ISREG(value.st_mode):
                raise OSError("invalid repository marker")
            return kind, _stat_identity(value)
        return None

    def resolve_repository_path(self, path: Path) -> Path:
        before = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
        after = path.stat(follow_symlinks=False)
        if _stat_identity(before) != _stat_identity(after):
            raise OSError("unstable current repository identity")
        return resolved

    def repository_path_identity(self, path: Path) -> tuple[int, int, int, int]:
        value = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(value.st_mode):
            raise OSError("invalid repository parent")
        return _stat_identity(value)


def _verify_macos_signature(path: Path) -> None:
    if path.name not in {CODEX_EXECUTABLE_NAME, MACOS_CODE_MODE_HOST_NAME}:
        raise OSError("native Codex signature is not trusted")
    identifier = path.name
    requirement = ('=anchor apple generic and certificate leaf[subject.OU] = '
                   f'"{OPENAI_MACOS_TEAM_IDENTIFIER}" and identifier "{identifier}"')
    try:
        verify = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", "--verbose=0",
             "--test-requirement", requirement, str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5.0, check=False, shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        details = subprocess.run(
            ["/usr/bin/codesign", "-d", "--verbose=4", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=5.0, check=False, shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        raise OSError("native Codex signature is not trusted") from None
    text = details.stdout.decode("utf-8", "replace")[:16384]
    if (verify.returncode != 0 or details.returncode != 0
            or f"Identifier={identifier}\n" not in text
            or f"TeamIdentifier={OPENAI_MACOS_TEAM_IDENTIFIER}\n" not in text
            or "flags=0x10000(runtime)" not in text):
        raise OSError("native Codex signature is not trusted")


class MacOSCacheRuntimeAdapter(_WindowsCacheRuntimeAdapter):
    """Reuse orchestration while replacing every OS-specific trust primitive."""

    executable_name = CODEX_EXECUTABLE_NAME

    def __init__(self, *, _runtime_adapter: _RuntimeAdapter | None = None) -> None:
        if sys.platform != "darwin":
            raise OSError("macOS native runtime is unavailable")
        self._runtime = _runtime_adapter or MacOSRuntimeAdapter()

    def validate_cache_ancestor_chain(self, root: Path) -> None:
        current = root
        while not current.exists():
            if current.parent == current:
                raise OSError("private runtime ancestor is unsafe")
            current = current.parent
        while True:
            value = current.stat(follow_symlinks=False)
            writable = value.st_mode & 0o022
            sticky_root = value.st_uid == 0 and bool(value.st_mode & stat.S_ISVTX)
            if (current.is_symlink() or not stat.S_ISDIR(value.st_mode)
                    or value.st_uid not in {0, os.geteuid()}
                    or (writable and not sticky_root)):
                raise OSError("private runtime ancestor permissions are unsafe")
            if current.parent == current:
                return
            current = current.parent

    def prepare_private_directory(self, path: Path) -> Path:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700, follow_symlinks=False)
        self.validate_private_directory(path)
        return path.resolve(strict=True)

    def validate_private_directory(self, path: Path) -> None:
        value = path.stat(follow_symlinks=False)
        if (path.is_symlink() or not stat.S_ISDIR(value.st_mode)
                or value.st_uid != os.geteuid() or value.st_mode & 0o077):
            raise OSError("private runtime directory is unsafe")

    def protect_private_file(self, path: Path) -> None:
        executable = (path.name in {CODEX_EXECUTABLE_NAME, MACOS_CODE_MODE_HOST_NAME}
                      or path.name.startswith(("codex-", "code-mode-host-")))
        os.chmod(path, 0o700 if executable else 0o600, follow_symlinks=False)
        self.validate_private_file(path)

    def validate_private_file(self, path: Path) -> tuple[int, int]:
        value = path.stat(follow_symlinks=False)
        if (path.is_symlink() or not stat.S_ISREG(value.st_mode)
                or value.st_uid != os.geteuid() or value.st_mode & 0o077
                or value.st_nlink != 1):
            raise OSError("private runtime file is unsafe")
        return value.st_dev, value.st_ino

    def read_private_text(self, path: Path) -> str:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            return os.read(descriptor, 4097).decode("utf-8", "strict")
        finally:
            os.close(descriptor)

    def inspect_private_executable(self, path: Path) -> tuple[NativeCodexIdentity, str]:
        descriptor = self._runtime.open_locked(path)
        try:
            identity = self._runtime.identity(descriptor)
            final = self._runtime.final_path(descriptor)
            if not self._runtime.same_file(final, path):
                raise OSError("private Codex executable changed")
            publisher = self._runtime.verify_publisher(descriptor, final)
            if self._runtime.identity(descriptor) != identity:
                raise OSError("private Codex executable changed")
            return identity, publisher
        finally:
            self._runtime.close(descriptor)

    def fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _candidate_paths(locator: Path, machine: str) -> tuple[Path, ...]:
    """Locate native npm payloads without executing a JS or shell launcher."""
    architecture = {"arm64": "arm64", "aarch64": "arm64",
                    "x86_64": "x86_64", "amd64": "x86_64"}.get(machine.lower())
    if architecture is None:
        raise OSError("unsupported macOS architecture")
    resolved = locator.resolve(strict=True)
    candidates = [resolved]
    if resolved.name == "codex.js" and resolved.parent.name == "bin":
        candidates.append(resolved.parent.parent / _MACOS_NATIVE_RELATIVE_PATHS[architecture])
    return tuple(candidates)


def discover_native_codex(*, which: Callable[[str], str | None],
                          repository_roots: tuple[Path, ...] | None,
                          _adapter: _RuntimeAdapter | None = None) -> LockedNativeCodex:
    """Return a signed native macOS payload while retaining its descriptor."""
    raw = which("codex")
    if type(raw) is not str or not raw or "\x00" in raw:
        raise OSError("native Codex payload is unavailable")
    locator = Path(raw)
    if not locator.is_absolute() or locator.name != CODEX_EXECUTABLE_NAME:
        raise OSError("native Codex payload is unavailable")
    adapter = _adapter or MacOSRuntimeAdapter()
    roots = _current_repository_roots(adapter) if repository_roots is None else repository_roots
    for candidate in _candidate_paths(locator, _macos_machine_architecture()):
        descriptor: int | None = None
        try:
            descriptor = adapter.open_locked(candidate)
            physical = adapter.final_path(descriptor)
            identity = adapter.identity(descriptor)
            if (identity.size <= 0 or _inside_repository_by_identity(physical, roots, adapter)
                    or adapter.verify_publisher(descriptor, physical)
                    != OPENAI_MACOS_CODESIGN_PUBLISHER
                    or adapter.identity(descriptor) != identity):
                raise OSError("native Codex payload is unavailable")
            return LockedNativeCodex(descriptor, identity, identity.size,
                                     OPENAI_MACOS_CODESIGN_PUBLISHER, adapter)
        except (OSError, subprocess.TimeoutExpired):
            if descriptor is not None:
                adapter.close(descriptor)
    raise OSError("native Codex payload is unavailable")


__all__ = ["MACOS_CODE_MODE_HOST_NAME", "MacOSCacheRuntimeAdapter",
           "MacOSRuntimeAdapter", "discover_native_codex"]
