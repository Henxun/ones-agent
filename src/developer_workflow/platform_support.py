"""Host-specific application paths for supported desktop platforms."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping


class HostPlatformError(RuntimeError):
    """主机平台无法提供安全的生产依赖。"""


@dataclass(frozen=True, slots=True)
class HostPaths:
    """主机平台上的应用私有路径，不负责创建目录。

    路径只包含非秘密配置、缓存和凭据写入锁；调用者仍须在写入前用
    ``prepare_private_directory`` 验证 owner、权限与符号链接边界。
    """

    config_path: Path
    cache_root: Path
    credential_lock_root: Path


def _system_account_home() -> Path:
    """返回当前有效用户的系统账户 home，不信任任务可控的 ``HOME``。"""

    try:
        import pwd

        value = pwd.getpwuid(os.geteuid()).pw_dir
        home = Path(value)
    except (KeyError, OSError, TypeError, ValueError):
        raise HostPlatformError("host platform paths are unavailable") from None
    if not home.is_absolute() or "\x00" in value:
        raise HostPlatformError("host platform paths are unavailable")
    return home


def default_host_paths(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> HostPaths:
    """返回 Windows 或 macOS 的规范默认路径。

    ``home`` 仅用于显式依赖注入；macOS 生产调用默认从系统账户数据库读取
    home，避免受任务环境中的 ``HOME`` 影响。输入平台不受支持、Windows
    缺少 ``LOCALAPPDATA``，或根路径不是绝对路径时均 fail-closed。
    """

    platform = platform_name or sys.platform
    environment = os.environ if environ is None else environ
    if platform == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        if not local_app_data or "\x00" in local_app_data:
            raise HostPlatformError("host platform paths are unavailable")
        base = Path(local_app_data)
        if not base.is_absolute():
            raise HostPlatformError("host platform paths are unavailable")
        app_root = base / "ones-dev"
        return HostPaths(
            config_path=app_root / "config.json",
            cache_root=app_root / "codex-runtime",
            credential_lock_root=app_root / "credential-locks",
        )
    if platform == "darwin":
        home_root = _system_account_home() if home is None else Path(home)
        if not home_root.is_absolute() or "\x00" in str(home_root):
            raise HostPlatformError("host platform paths are unavailable")
        support_root = home_root / "Library" / "Application Support" / "ones-dev"
        return HostPaths(
            config_path=support_root / "config.json",
            cache_root=home_root / "Library" / "Caches" / "ones-dev" / "codex-runtime",
            credential_lock_root=support_root / "credential-locks",
        )
    raise HostPlatformError("host platform is unsupported")


def user_data_directory() -> Path:
    """返回当前主机的应用支持目录，保留旧调用方兼容性。"""

    return default_host_paths().config_path.parent


__all__ = [
    "HostPaths",
    "HostPlatformError",
    "default_host_paths",
    "user_data_directory",
]
