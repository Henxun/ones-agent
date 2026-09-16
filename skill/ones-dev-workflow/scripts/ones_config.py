#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "keyring>=25.0",
# ]
# ///
"""Manage named ONES Skill profiles without storing secrets in project files."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


SERVICE = "ones-dev-workflow"
_PROFILE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_$-]{1,256}\Z")
_PUBLIC_FIELDS = {
    "base_url",
    "team_id",
    "email",
    "project_id",
    "issue_type_id",
    "auth_mode",
}


class ProfileError(RuntimeError):
    pass


def _profile_name(value: str) -> str:
    if _PROFILE.fullmatch(value) is None:
        raise ProfileError("profile name is invalid")
    return value


def _config_root() -> Path:
    configured = os.environ.get("ONES_SKILL_CONFIG_DIR", "").strip()
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise ProfileError("ONES_SKILL_CONFIG_DIR must be absolute")
        return root
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise ProfileError("LOCALAPPDATA is unavailable")
        return Path(local) / SERVICE
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".config") / SERVICE


def _profile_path(profile: str) -> Path:
    return _config_root() / "profiles" / f"{_profile_name(profile)}.json"


def _key(profile: str, kind: str) -> str:
    return f"{_profile_name(profile)}:{kind}"


def _keyring() -> Any:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        raise ProfileError("keyring dependency is unavailable; run with uv") from None
    return keyring, KeyringError


def _read_secret(profile: str, kind: str) -> str:
    keyring, keyring_error = _keyring()
    try:
        return str(keyring.get_password(SERVICE, _key(profile, kind)) or "")
    except keyring_error:
        raise ProfileError("system credential store is unavailable") from None


def _write_secret(profile: str, kind: str, value: str) -> None:
    keyring, keyring_error = _keyring()
    try:
        keyring.set_password(SERVICE, _key(profile, kind), value)
    except keyring_error:
        raise ProfileError("system credential store is unavailable") from None


def _delete_secret(profile: str, kind: str) -> None:
    keyring, keyring_error = _keyring()
    try:
        existing = keyring.get_password(SERVICE, _key(profile, kind))
        if existing is not None:
            keyring.delete_password(SERVICE, _key(profile, kind))
    except keyring_error:
        raise ProfileError("system credential store is unavailable") from None


def _validate_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProfileError("ONES base URL is invalid")
    return candidate


def _validate_identifier(value: str, label: str, *, required: bool) -> str:
    candidate = value.strip()
    if not candidate and not required:
        return ""
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise ProfileError(f"{label} is invalid")
    return candidate


def load_profile(profile: str, *, required: bool = False) -> dict[str, str]:
    path = _profile_path(profile)
    if not path.is_file():
        if required:
            raise ProfileError("ONES profile is not configured")
        return {}
    try:
        if path.stat().st_size > 64 * 1024:
            raise ProfileError("ONES profile is invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ProfileError("ONES profile is invalid") from None
    if (
        not isinstance(payload, Mapping)
        or payload.get("version") != 1
        or set(payload) - ({"version"} | _PUBLIC_FIELDS)
    ):
        raise ProfileError("ONES profile is invalid")
    result = {field: str(payload.get(field) or "") for field in _PUBLIC_FIELDS}
    result["base_url"] = _validate_url(result["base_url"])
    result["team_id"] = _validate_identifier(result["team_id"], "team id", required=True)
    result["project_id"] = _validate_identifier(result["project_id"], "project id", required=False)
    result["issue_type_id"] = _validate_identifier(result["issue_type_id"], "issue type id", required=False)
    if result["auth_mode"] not in {"account", "token"}:
        raise ProfileError("ONES profile authentication mode is invalid")
    if result["auth_mode"] == "account" and not result["email"].strip():
        raise ProfileError("ONES profile email is invalid")
    secret_kind = "password" if result["auth_mode"] == "account" else "token"
    result[secret_kind] = _read_secret(profile, secret_kind)
    if not result[secret_kind]:
        raise ProfileError("ONES profile credential is unavailable")
    return result


def _atomic_write(profile: str, payload: Mapping[str, str | int]) -> None:
    path = _profile_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        raise ProfileError("ONES profile could not be saved") from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _prompt(label: str, current: str = "") -> str:
    suffix = f" [{current}]" if current else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or current


def _configure(args: argparse.Namespace) -> dict[str, Any]:
    profile = _profile_name(args.profile)
    existing = load_profile(profile, required=False)
    if args.from_env:
        base_url = os.environ.get("ONES_BASE_URL", "")
        team_id = os.environ.get("ONES_TEAM_ID", "")
        email = os.environ.get("ONES_EMAIL", "")
        project_id = os.environ.get("ONES_PROJECT_ID", "")
        issue_type_id = os.environ.get("ONES_ISSUE_TYPE_ID", "")
        token = os.environ.get("ONES_API_TOKEN", "")
        password = os.environ.get("ONES_PASSWORD", "")
        auth_mode = "token" if token else "account"
        secret = token or password
    else:
        if not sys.stdin.isatty():
            raise ProfileError("interactive configuration requires a TTY; use --from-env")
        base_url = _prompt("ONES base URL", existing.get("base_url", ""))
        team_id = _prompt("Team ID", existing.get("team_id", ""))
        auth_mode = args.auth or existing.get("auth_mode", "account")
        email = "" if auth_mode == "token" else _prompt("Email", existing.get("email", ""))
        project_id = _prompt("Default project ID (optional)", existing.get("project_id", ""))
        issue_type_id = _prompt("Default issue type ID (optional)", existing.get("issue_type_id", ""))
        secret = getpass("API token: " if auth_mode == "token" else "Password: ")
    base_url = _validate_url(base_url)
    team_id = _validate_identifier(team_id, "team id", required=True)
    project_id = _validate_identifier(project_id, "project id", required=False)
    issue_type_id = _validate_identifier(issue_type_id, "issue type id", required=False)
    email = email.strip()
    if auth_mode not in {"account", "token"} or auth_mode == "account" and not email:
        raise ProfileError("authentication configuration is invalid")
    if not secret:
        raise ProfileError("credential is unavailable")

    secret_kind = "password" if auth_mode == "account" else "token"
    previous_secret = existing.get(secret_kind, "")
    _write_secret(profile, secret_kind, secret)
    try:
        _atomic_write(
            profile,
            {
                "version": 1,
                "base_url": base_url,
                "team_id": team_id,
                "email": email,
                "project_id": project_id,
                "issue_type_id": issue_type_id,
                "auth_mode": auth_mode,
            },
        )
    except BaseException:
        if previous_secret:
            _write_secret(profile, secret_kind, previous_secret)
        else:
            _delete_secret(profile, secret_kind)
        raise
    _delete_secret(profile, "token" if secret_kind == "password" else "password")
    return {"configured": True, "profile": profile, "auth_mode": auth_mode}


def _show(profile: str) -> dict[str, Any]:
    profile = _profile_name(profile)
    values = load_profile(profile, required=True)
    return {
        "profile": profile,
        "base_url": values["base_url"],
        "team_id": values["team_id"],
        "email": values["email"] if values["auth_mode"] == "account" else "",
        "project_id": values["project_id"],
        "issue_type_id": values["issue_type_id"],
        "auth_mode": values["auth_mode"],
        "has_credential": True,
    }


def _clear(profile: str, confirmed: bool) -> dict[str, Any]:
    profile = _profile_name(profile)
    if not confirmed:
        raise ProfileError("clear requires --yes")
    for kind in ("password", "token"):
        _delete_secret(profile, kind)
    path = _profile_path(profile)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        raise ProfileError("ONES profile could not be cleared") from None
    return {"cleared": True, "profile": profile}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage ONES Skill profiles")
    parser.add_argument("--profile", default="default")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--auth", choices=("account", "token"))
    configure.add_argument("--from-env", action="store_true")
    commands.add_parser("show")
    clear = commands.add_parser("clear")
    clear.add_argument("--yes", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "configure":
            result = _configure(args)
        elif args.command == "show":
            result = _show(args.profile)
        else:
            result = _clear(args.profile, args.yes)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ProfileError as error:
        print(
            json.dumps(
                {"ok": False, "error": "configuration", "message": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
