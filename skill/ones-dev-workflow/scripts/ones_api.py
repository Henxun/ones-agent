#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "keyring>=25.0",
#   "pycryptodome>=3.23",
#   "requests>=2.31",
# ]
# ///
"""Standalone, read-only ONES account and work-item client."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from ones_config import ProfileError, load_profile

try:
    import requests
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA
except ImportError:
    print(
        json.dumps(
            {
                "ok": False,
                "error": "dependency",
                "message": "run this script with uv or install requests and pycryptodome",
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(2)


RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0orxr+Larwt3bqq0yt5D
DgNlOh3D5kDSmidNbr3nHe/ktgr4sTWoVJAFtn2fgLB6e9zf571eeOJJ4hqp5Su2
RRTOhOojE98gEjBAi1fB7OPLR0d2TYzE/P9ahaOhT89noIGQz+Pu2n9wBK/7dg6A
MeJ51Edn4p4WlP+XKWyfH78T6v5hQ9snt5Vtz5wbpEOu+X414ENswIAhLCOCqBzj
khNqfJG/fNH/SjsjbmsqCdedirZAu8DYWBPv1x+vFn7hBOd2G40FnsWAAR8ekHgB
b+wB0DkHlDhIGK6QmbVZh4vKCcPk4QDrGY3rQPGrECGqmIi9BZK75sUeNTec6jp6
gQIDAQAB
-----END PUBLIC KEY-----"""

_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
_ID = re.compile(r"[A-Za-z0-9_$-]{1,256}\Z")
_WIKI_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_MAX_BODY_BYTES = 10 * 1024 * 1024

GQL_PROJECTS = """{
  buckets(groupBy: $groupBy, orderBy: $orderBy, pagination: {limit: 50, after: "", preciseCount: true}) {
    key
    projects(limit: 10000, orderBy: $projectOrderBy, filterGroup: $projectFilterGroup) {
      uuid name isArchive type status { uuid name category } owner { uuid name }
    }
    pageInfo { count totalCount hasNextPage }
  }
}"""

GQL_TASKS = """{
  buckets(groupBy: $groupBy, orderBy: $groupOrderBy, pagination: $pagination, filter: $groupFilter) {
    key
    tasks(filterGroup: $filterGroup, orderBy: $orderBy, limit: $limit) {
      key uuid name number createTime serverUpdateStamp deadline(unit: ONESDATE) path
      status { uuid name category }
      issueType { uuid name }
      project { uuid name }
      sprint { uuid name }
      parent { uuid name }
      assign { uuid name avatar }
      owner { uuid name avatar }
      priority { uuid value position }
      severityLevel { uuid value position }
    }
    pageInfo { count totalCount hasNextPage endCursor }
  }
}"""

GQL_TASK_DETAIL = """query Task($key: Key) {
  task(key: $key) {
    key uuid number name path createTime serverUpdateStamp deadline(unit: ONESDATE)
    description descriptionText
    issueType { uuid name }
    project { uuid name }
    sprint { uuid name }
    status { uuid name category }
    priority { uuid value position }
    severityLevel { uuid value position }
    assign { uuid name avatar }
    owner { uuid name avatar }
    parent { uuid name }
    relatedWikiPages { uuid title referenceType subReferenceType errorMessage }
  }
}"""


class SkillError(RuntimeError):
    category = "api"


class ConfigurationError(SkillError):
    category = "configuration"


class AuthenticationError(SkillError):
    category = "authentication"


class TimeoutError(SkillError):
    category = "timeout"


class NotFoundError(SkillError):
    category = "not_found"


class PayloadError(SkillError):
    category = "payload"


def _json_config(config_path: str | None) -> dict[str, Any]:
    environment_path = os.environ.get("ONES_CONFIG_FILE", "").strip()
    explicit = config_path is not None or bool(environment_path)
    if config_path is not None or environment_path:
        path = Path(config_path or environment_path).expanduser()
    else:
        skill_directory = Path(__file__).resolve().parents[1]
        candidates = (
            skill_directory / "ones.config.json",
            Path.cwd() / "ones.config.json",
        )
        path = next((candidate for candidate in candidates if candidate.exists()), candidates[-1])
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        if explicit:
            raise ConfigurationError("ONES JSON configuration was not found")
        return {}
    if not path.is_file() or path.is_symlink():
        raise ConfigurationError("ONES JSON configuration is invalid")
    try:
        if path.stat().st_size > 64 * 1024:
            raise ConfigurationError("ONES JSON configuration is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError("ONES JSON configuration is invalid") from None
    if not isinstance(payload, Mapping):
        raise ConfigurationError("ONES JSON configuration must be an object")
    allowed = {
        "base_url",
        "team_id",
        "project_id",
        "issue_types",
        "auth",
        "timeout_seconds",
        "ONES_BASE_URL",
        "ONES_TEAM_ID",
        "ONES_PROJECT_ID",
        "ONES_ISSUE_TYPE_ID",
        "ONES_DEFECT_ISSUE_TYPE_ID",
        "ONES_REQUIREMENT_ISSUE_TYPE_ID",
        "ONES_EMAIL",
        "ONES_PASSWORD",
        "ONES_API_TOKEN",
        "ONES_TIMEOUT_SECONDS",
    }
    if set(payload) - allowed:
        raise ConfigurationError("ONES JSON configuration contains unknown fields")
    issue_types = payload.get("issue_types") or {}
    auth = payload.get("auth") or {}
    if not isinstance(issue_types, Mapping) or set(issue_types) - {"defect", "requirement"}:
        raise ConfigurationError("ONES issue_types configuration is invalid")
    if not isinstance(auth, Mapping) or set(auth) - {
        "mode",
        "email",
        "password",
        "password_env",
        "token",
        "token_env",
    }:
        raise ConfigurationError("ONES auth configuration is invalid")
    mode = str(
        auth.get("mode")
        or ("token" if payload.get("ONES_API_TOKEN") else "account")
    ).strip()
    if mode not in {"account", "token"}:
        raise ConfigurationError("ONES auth mode is invalid")

    def secret(direct_name: str, environment_name: str) -> str:
        direct = str(auth.get(direct_name) or "")
        reference = str(auth.get(environment_name) or "").strip()
        if direct and reference:
            raise ConfigurationError(
                f"ONES auth must not set both {direct_name} and {environment_name}"
            )
        if reference:
            if _ENVIRONMENT_NAME.fullmatch(reference) is None:
                raise ConfigurationError(f"ONES {environment_name} is invalid")
            return os.environ.get(reference, "")
        return direct

    generic_issue_type = str(payload.get("ONES_ISSUE_TYPE_ID") or "").strip()
    return {
        "_config_path": str(path.resolve()),
        "base_url": str(
            payload.get("base_url") or payload.get("ONES_BASE_URL") or ""
        ).strip(),
        "team_id": str(
            payload.get("team_id") or payload.get("ONES_TEAM_ID") or ""
        ).strip(),
        "project_id": str(
            payload.get("project_id") or payload.get("ONES_PROJECT_ID") or ""
        ).strip(),
        "defect_issue_type_id": str(
            issue_types.get("defect")
            or payload.get("ONES_DEFECT_ISSUE_TYPE_ID")
            or generic_issue_type
            or ""
        ).strip(),
        "requirement_issue_type_id": str(
            issue_types.get("requirement")
            or payload.get("ONES_REQUIREMENT_ISSUE_TYPE_ID")
            or generic_issue_type
            or ""
        ).strip(),
        "auth_mode": mode,
        "email": str(auth.get("email") or payload.get("ONES_EMAIL") or "").strip(),
        "password": (
            secret("password", "password_env")
            or str(payload.get("ONES_PASSWORD") or "")
            if mode == "account"
            else ""
        ),
        "token": (
            secret("token", "token_env")
            or str(payload.get("ONES_API_TOKEN") or "").strip()
            if mode == "token"
            else ""
        ),
        "timeout_seconds": payload.get(
            "timeout_seconds", payload.get("ONES_TIMEOUT_SECONDS", 30)
        ),
    }


@dataclass(frozen=True, slots=True)
class Config:
    base_url: str
    team_id: str
    email: str
    password: str
    api_token: str
    project_id: str
    defect_issue_type_id: str
    requirement_issue_type_id: str
    timeout: float
    configuration_source: str
    credential_source: str

    @classmethod
    def from_sources(cls, profile: str, config_path: str | None) -> "Config":
        local = _json_config(config_path)
        environment_base = os.environ.get("ONES_BASE_URL", "").strip()
        environment_team = os.environ.get("ONES_TEAM_ID", "").strip()
        environment_email = os.environ.get("ONES_EMAIL", "").strip()
        environment_password = os.environ.get("ONES_PASSWORD", "")
        environment_token = os.environ.get("ONES_API_TOKEN", "").strip()
        environment_auth_complete = bool(
            environment_token or environment_email and environment_password
        )
        local_auth_complete = bool(
            local.get("token") or local.get("email") and local.get("password")
        )
        sources_complete = bool(
            (environment_base or local.get("base_url"))
            and (environment_team or local.get("team_id"))
            and (environment_auth_complete or local_auth_complete)
        )
        if sources_complete:
            saved: dict[str, str] = {}
        else:
            try:
                saved = load_profile(profile, required=False)
            except ProfileError as error:
                raise ConfigurationError(str(error)) from None

        def value(
            environment_name: str,
            local_name: str,
            profile_name: str,
        ) -> str:
            environment_value = os.environ.get(environment_name, "").strip()
            return (
                environment_value
                or str(local.get(local_name) or "").strip()
                or saved.get(profile_name, "").strip()
            )

        def issue_type_value(environment_name: str, local_name: str) -> str:
            return (
                os.environ.get(environment_name, "").strip()
                or os.environ.get("ONES_ISSUE_TYPE_ID", "").strip()
                or str(local.get(local_name) or "").strip()
                or saved.get("issue_type_id", "").strip()
            )

        base_url = value("ONES_BASE_URL", "base_url", "base_url").rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError("ONES_BASE_URL is missing or invalid")
        config_hint = str(local.get("_config_path") or "named profile/environment")
        team_id = _identifier(
            value("ONES_TEAM_ID", "team_id", "team_id"),
            f"team_id/ONES_TEAM_ID (source: {config_hint})",
        )
        api_token = ""
        email = ""
        password = ""
        if environment_token:
            api_token = environment_token
            credential_source = "environment:ONES_API_TOKEN"
        elif environment_email and environment_password:
            email = environment_email
            password = environment_password
            credential_source = "environment:ONES_EMAIL+ONES_PASSWORD"
        elif local.get("auth_mode") == "token" and local.get("token"):
            api_token = str(local["token"])
            credential_source = f"json:{config_hint}"
        elif (
            local.get("auth_mode") == "account"
            and local.get("email")
            and local.get("password")
        ):
            email = str(local["email"])
            password = str(local["password"])
            credential_source = f"json:{config_hint}"
        elif saved.get("auth_mode") == "token" and saved.get("token"):
            api_token = saved.get("token", "")
            credential_source = f"profile:{profile}"
        else:
            email = saved.get("email", "")
            password = saved.get("password", "")
            credential_source = f"profile:{profile}"
        if not api_token and (not email or not password):
            raise ConfigurationError(
                f"credentials are missing or incomplete (config: {config_hint})"
            )
        if password and password != password.strip():
            raise ConfigurationError(
                f"password contains leading or trailing whitespace (config: {config_hint})"
            )
        try:
            timeout = float(
                os.environ.get(
                    "ONES_TIMEOUT_SECONDS",
                    str(local.get("timeout_seconds", 30)),
                )
            )
        except ValueError:
            raise ConfigurationError("ONES_TIMEOUT_SECONDS is invalid") from None
        if not 0 < timeout <= 300:
            raise ConfigurationError("ONES_TIMEOUT_SECONDS is invalid")
        return cls(
            base_url=base_url,
            team_id=team_id,
            email=email,
            password=password,
            api_token=api_token,
            project_id=value("ONES_PROJECT_ID", "project_id", "project_id"),
            defect_issue_type_id=issue_type_value(
                "ONES_DEFECT_ISSUE_TYPE_ID", "defect_issue_type_id"
            ),
            requirement_issue_type_id=issue_type_value(
                "ONES_REQUIREMENT_ISSUE_TYPE_ID", "requirement_issue_type_id"
            ),
            timeout=timeout,
            configuration_source=config_hint,
            credential_source=credential_source,
        )


def _identifier(value: str | None, label: str) -> str:
    candidate = str(value or "").strip()
    if _ID.fullmatch(candidate) is None:
        raise ConfigurationError(f"{label} is missing or invalid")
    return candidate


def _optional_identifier(value: str | None, label: str) -> str | None:
    return None if value is None or not value.strip() else _identifier(value, label)


def _wiki_identifier(value: str | None, label: str) -> str:
    candidate = str(value or "").strip()
    if _WIKI_ID.fullmatch(candidate) is None:
        raise ConfigurationError(f"ONES Wiki {label} identifier is missing or invalid")
    return candidate


def _encrypt_password(password: str, public_key: str = RSA_PUBLIC_KEY) -> str:
    try:
        key = RSA.import_key(public_key)
        if key.has_private():
            raise ValueError("private keys are not accepted")
        encrypted = PKCS1_v1_5.new(key).encrypt(password.encode("utf-8"))
    except (TypeError, ValueError, IndexError):
        raise PayloadError("ONES encryption certificate is invalid") from None
    return base64.b64encode(encrypted).decode("ascii")


def _safe_upstream_error_code(response: requests.Response) -> str:
    try:
        if len(response.content) > 64 * 1024:
            return ""
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    code = payload.get("errcode") or payload.get("code")
    candidate = str(code or "").strip()
    return candidate if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", candidate) else ""


def _code_verifier(length: int = 43) -> str:
    random_bytes = os.urandom(length)
    return "".join(_CHARS[value % len(_CHARS)] for value in random_bytes)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PayloadError(f"ONES returned an invalid {label}")
    return dict(value)


def _reference(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    identity = str(value.get("uuid") or "").strip()
    name = str(value.get("name") or value.get("value") or "").strip()
    if not identity and not name:
        return None
    result = {"id": identity, "name": name}
    category = str(value.get("category") or "").strip()
    if category:
        result["category"] = category
    return result


def _normalize_task(task: Mapping[str, Any], *, include_description: bool) -> dict[str, Any]:
    description: Any = task.get("descriptionText") or task.get("description") or ""
    if not isinstance(description, str):
        description = json.dumps(description, ensure_ascii=False, sort_keys=True)
    wiki_refs = []
    for value in task.get("relatedWikiPages") or []:
        if isinstance(value, Mapping):
            wiki_refs.append(
                {
                    "id": str(value.get("uuid") or ""),
                    "title": str(value.get("title") or ""),
                    "reference_type": str(value.get("referenceType") or ""),
                    "sub_reference_type": str(value.get("subReferenceType") or ""),
                }
            )
    result: dict[str, Any] = {
        "id": str(task.get("uuid") or ""),
        "key": str(task.get("key") or ""),
        "number": str(task.get("number") or ""),
        "title": str(task.get("name") or ""),
        "project": _reference(task.get("project")),
        "iteration": _reference(task.get("sprint")),
        "status": _reference(task.get("status")),
        "issue_type": _reference(task.get("issueType")),
        "priority": _reference(task.get("priority")),
        "severity": _reference(task.get("severityLevel")),
        "assignee": _reference(task.get("assign")),
        "owner": _reference(task.get("owner")),
        "parent": _reference(task.get("parent")),
        "path": str(task.get("path") or ""),
        "deadline": str(task.get("deadline") or ""),
        "created_at": str(task.get("createTime") or ""),
        "updated_at": str(task.get("serverUpdateStamp") or ""),
    }
    if include_description:
        result["description"] = description
        result["wiki_refs"] = wiki_refs
    return result


class OnesClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.account_id = ""
        if config.api_token:
            self.session.headers["Authorization"] = f"Bearer {config.api_token}"
        else:
            self._login()

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                f"{self.config.base_url}{path}",
                timeout=self.config.timeout,
                **kwargs,
            )
        except requests.Timeout:
            raise TimeoutError("ONES request timed out") from None
        except requests.exceptions.SSLError:
            raise SkillError("ONES TLS certificate validation failed") from None
        except requests.exceptions.ConnectionError:
            raise SkillError("ONES connection failed") from None
        except requests.RequestException:
            raise SkillError("ONES request failed") from None
        if response.status_code in {401, 403}:
            status = response.status_code
            upstream_code = _safe_upstream_error_code(response)
            response.close()
            suffix = f"; code={upstream_code}" if upstream_code else ""
            raise AuthenticationError(
                f"ONES rejected authentication or authorization (HTTP {status}{suffix})"
            )
        if response.status_code == 404:
            response.close()
            raise NotFoundError("ONES resource was not found")
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise SkillError(f"ONES returned HTTP {status}")
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > _MAX_BODY_BYTES:
            response.close()
            raise PayloadError("ONES response exceeded the size limit")
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        try:
            if len(response.content) > _MAX_BODY_BYTES:
                raise PayloadError("ONES response exceeded the size limit")
            return _mapping(response.json(), "JSON response")
        except ValueError:
            raise PayloadError("ONES returned malformed JSON") from None
        finally:
            response.close()

    def _login(self) -> None:
        bootstrap = self._request("GET", "/identity/login")
        bootstrap.close()
        public_key = RSA_PUBLIC_KEY
        try:
            certificate = self._json("POST", "/identity/api/encryption_cert")
        except NotFoundError:
            certificate = {}
        dynamic_key = certificate.get("public_key")
        if dynamic_key is not None:
            if not isinstance(dynamic_key, str) or not dynamic_key.strip():
                raise PayloadError("ONES encryption certificate is invalid")
            public_key = dynamic_key
        try:
            payload = self._json(
                "POST",
                "/identity/api/login",
                json={
                    "email": self.config.email,
                    "password": _encrypt_password(self.config.password, public_key),
                },
            )
        except TimeoutError:
            raise
        except AuthenticationError as error:
            raise AuthenticationError(
                f"ONES account login failed at identity/api/login: {error}"
            ) from None
        except SkillError as error:
            raise SkillError(
                f"ONES login transport/API failed at identity/api/login: {error}"
            ) from None
        org_users = payload.get("org_users")
        if not isinstance(org_users, list) or not org_users:
            raise AuthenticationError("ONES account has no available organization")
        selected = _mapping(org_users[0], "organization account")
        org_uuid = _identifier(str(selected.get("org_uuid") or ""), "organization id")
        org_user = _mapping(selected.get("org_user"), "organization user")
        org_user_uuid = _identifier(
            str(org_user.get("org_user_uuid") or ""), "account id"
        )
        self.account_id = org_user_uuid

        verifier = _code_verifier()
        response = self._request(
            "POST",
            "/identity/authorize",
            data={
                "client_id": "ones.v1",
                "scope": f"openid offline_access ones:org:{org_uuid}:{org_user_uuid}",
                "response_type": "code",
                "code_challenge_method": "S256",
                "code_challenge": _code_challenge(verifier),
                "redirect_uri": f"{self.config.base_url}/auth/authorize/callback",
                "state": f"org_uuid={org_uuid}",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )
        try:
            location = response.headers.get("Location", "")
        finally:
            response.close()
        request_id = parse_qs(urlsplit(location).query).get("id", [""])[0]
        request_id = _identifier(request_id, "authorization request id")

        for method, path in (
            ("GET", f"/identity/api/auth_request/{request_id}"),
            ("GET", "/identity/api/org_users"),
        ):
            response = self._request(method, path)
            response.close()
        response = self._request(
            "POST",
            "/identity/api/auth_request/finalize",
            data=json.dumps(
                {
                    "auth_request_id": request_id,
                    "region_uuid": "default",
                    "org_uuid": org_uuid,
                    "org_user_uuid": org_user_uuid,
                },
                allow_nan=False,
            ),
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        response.close()
        response = self._request(
            "GET",
            "/identity/authorize/callback",
            params={"id": request_id, "lang": "zh"},
            allow_redirects=False,
        )
        try:
            source = response.headers.get("Location", "") + " " + response.text
        finally:
            response.close()
        match = re.search(r"(?:[?&]|\b)code=([^&\s]+)", source)
        if match is None:
            raise AuthenticationError("ONES authorization code was unavailable")
        token = self._json(
            "POST",
            "/identity/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "ones.v1",
                "code": match.group(1),
                "code_verifier": verifier,
                "redirect_uri": f"{self.config.base_url}/auth/authorize/callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthenticationError("ONES access token was unavailable")
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _graphql(self, query: str, variables: dict[str, Any], *, tag: str) -> dict[str, Any]:
        payload = self._json(
            "POST",
            f"/project/api/project/team/{self.config.team_id}/items/graphql?t={tag}",
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        if payload.get("errors"):
            raise SkillError("ONES GraphQL request failed")
        data = payload.get("data", payload)
        return _mapping(data, "GraphQL response")

    def projects(self, include_archived: bool) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = [{"visibleInProject_equal": True}]
        if not include_archived:
            filters.append({"isArchive_equal": False})
        data = self._graphql(
            GQL_PROJECTS,
            {
                "groupBy": {"projects": {}},
                "orderBy": {},
                "projectOrderBy": {
                    "isPin": "DESC",
                    "namePinyin": "ASC",
                    "createTime": "DESC",
                },
                "projectFilterGroup": [filters],
            },
            tag="projects-group-list-for-project-view",
        )
        result = []
        for bucket in data.get("buckets") or []:
            if not isinstance(bucket, Mapping):
                raise PayloadError("ONES returned an invalid project bucket")
            for project in bucket.get("projects") or []:
                value = _mapping(project, "project")
                result.append(
                    {
                        "id": str(value.get("uuid") or ""),
                        "name": str(value.get("name") or ""),
                        "archived": bool(value.get("isArchive")),
                        "type": str(value.get("type") or ""),
                        "status": _reference(value.get("status")),
                        "owner": _reference(value.get("owner")),
                    }
                )
        return result

    def fetch_wiki_page_content(self, page_id: str) -> dict[str, Any]:
        team = quote(_wiki_identifier(self.config.team_id, "team"), safe="")
        page = quote(_wiki_identifier(page_id, "page"), safe="")
        return self._json(
            "GET",
            f"/wiki/api/wiki/team/{team}/online_page/{page}/content",
        )

    def tasks(
        self,
        *,
        project_id: str,
        issue_type_id: str,
        iteration_id: str | None,
        assignee_id: str | None,
        statuses: Iterable[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {
            "project_in": [project_id],
            "issueType_in": [issue_type_id],
        }
        if iteration_id:
            filters["sprint_in"] = [iteration_id]
        if assignee_id:
            filters["assign_in"] = [assignee_id]
        status_values = list(statuses)
        if status_values:
            filters["status_in"] = status_values

        tasks: list[dict[str, Any]] = []
        seen_tasks: set[str] = set()
        cursor = ""
        seen_cursors = {cursor}
        while len(tasks) < limit:
            page_size = min(200, limit - len(tasks))
            data = self._graphql(
                GQL_TASKS,
                {
                    "groupBy": {"tasks": {}},
                    "groupOrderBy": {},
                    "groupFilter": [],
                    "orderBy": {"position": "ASC", "createTime": "DESC"},
                    "filterGroup": [filters],
                    "pagination": {
                        "limit": page_size,
                        "after": cursor,
                        "preciseCount": True,
                    },
                    "limit": page_size,
                },
                tag="group-task-data",
            )
            buckets = data.get("buckets") or []
            next_cursors: set[str] = set()
            has_next = False
            for bucket in buckets:
                bucket = _mapping(bucket, "task bucket")
                for task in bucket.get("tasks") or []:
                    task = _mapping(task, "task")
                    identity = str(task.get("uuid") or task.get("key") or "")
                    if identity and identity not in seen_tasks:
                        seen_tasks.add(identity)
                        tasks.append(_normalize_task(task, include_description=False))
                page_info = _mapping(bucket.get("pageInfo"), "task page info")
                if not isinstance(page_info.get("hasNextPage"), bool):
                    raise PayloadError("ONES pagination is invalid")
                if page_info["hasNextPage"]:
                    has_next = True
                    next_cursors.add(str(page_info.get("endCursor") or "").strip())
            if not has_next:
                break
            if len(next_cursors) != 1:
                raise PayloadError("ONES pagination has no stable cursor")
            cursor = next(iter(next_cursors))
            if not cursor or cursor in seen_cursors:
                raise PayloadError("ONES pagination did not advance")
            seen_cursors.add(cursor)
        return tasks[:limit]

    def task_detail(
        self,
        item_id: str,
        *,
        project_id: str | None,
        issue_type_id: str | None,
    ) -> dict[str, Any]:
        try:
            data = self._graphql(GQL_TASK_DETAIL, {"key": item_id}, tag="Task")
            task = data.get("task")
            if isinstance(task, Mapping) and task:
                return _normalize_task(task, include_description=True)
        except SkillError as error:
            if type(error) is not SkillError or not project_id or not issue_type_id:
                raise
        if not project_id or not issue_type_id:
            raise NotFoundError(
                "item was not resolved; use its key or provide project and issue-type IDs"
            )
        matches = self.tasks(
            project_id=project_id,
            issue_type_id=issue_type_id,
            iteration_id=None,
            assignee_id=None,
            statuses=(),
            limit=5000,
        )
        match = next(
            (
                value
                for value in matches
                if value.get("id") == item_id or value.get("key") == item_id
            ),
            None,
        )
        if match is None or not match.get("key"):
            raise NotFoundError("ONES item was not found in the authorized scope")
        data = self._graphql(GQL_TASK_DETAIL, {"key": match["key"]}, tag="Task")
        task = data.get("task")
        if not isinstance(task, Mapping) or not task:
            raise NotFoundError("ONES item detail was not found")
        return _normalize_task(task, include_description=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone read-only ONES client")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config-check")
    commands.add_parser("auth-check")
    projects = commands.add_parser("projects")
    projects.add_argument("--include-archived", action="store_true")
    wiki_content = commands.add_parser("wiki-content")
    wiki_content.add_argument("page_id")

    for name in ("defect", "requirement"):
        detail = commands.add_parser(name)
        detail.add_argument("item_id")
        detail.add_argument("--project")
        detail.add_argument("--issue-type")

    for name in ("defects", "requirements"):
        listing = commands.add_parser(name)
        listing.add_argument("--project")
        listing.add_argument("--issue-type")
        listing.add_argument("--iteration")
        assignee = listing.add_mutually_exclusive_group()
        assignee.add_argument("--assignee")
        assignee.add_argument("--mine", action="store_true")
        listing.add_argument("--status", action="append", default=[])
        listing.add_argument("--limit", type=int, default=100)
    return parser


def _output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _run(args: argparse.Namespace, config: Config) -> None:
    if args.command == "config-check":
        _output(
            {
                "configuration_valid": True,
                "configuration_source": config.configuration_source,
                "credential_source": config.credential_source,
                "auth_mode": "token" if config.api_token else "account",
                "endpoint_origin": (
                    f"{urlsplit(config.base_url).scheme}://"
                    f"{urlsplit(config.base_url).netloc}"
                ),
                "project_configured": bool(config.project_id),
                "defect_issue_type_configured": bool(config.defect_issue_type_id),
                "requirement_issue_type_configured": bool(
                    config.requirement_issue_type_id
                ),
            }
        )
        return
    client = OnesClient(config)
    try:
        if args.command == "auth-check":
            projects = client.projects(False)
            _output(
                {
                    "authenticated": True,
                    "mode": "token" if config.api_token else "account",
                    "account_id": client.account_id or None,
                    "visible_project_count": len(projects),
                }
            )
        elif args.command == "projects":
            _output(client.projects(args.include_archived))
        elif args.command == "wiki-content":
            _output(client.fetch_wiki_page_content(args.page_id))
        elif args.command in {"defect", "requirement"}:
            default_issue_type = (
                config.defect_issue_type_id
                if args.command == "defect"
                else config.requirement_issue_type_id
            )
            _output(
                client.task_detail(
                    _identifier(args.item_id, "item id"),
                    project_id=_optional_identifier(args.project, "project id"),
                    issue_type_id=_optional_identifier(
                        args.issue_type or default_issue_type,
                        "issue type id",
                    ),
                )
            )
        else:
            if not 1 <= args.limit <= 5000:
                raise ConfigurationError("limit must be between 1 and 5000")
            project = _identifier(
                args.project or config.project_id,
                "project id",
            )
            default_issue_type = (
                config.defect_issue_type_id
                if args.command == "defects"
                else config.requirement_issue_type_id
            )
            issue_type = _identifier(args.issue_type or default_issue_type, "issue type id")
            assignee = "$currentUser" if args.mine else _optional_identifier(
                args.assignee, "assignee id"
            )
            _output(
                client.tasks(
                    project_id=project,
                    issue_type_id=issue_type,
                    iteration_id=_optional_identifier(args.iteration, "iteration id"),
                    assignee_id=assignee,
                    statuses=(
                        _identifier(value, "status id") for value in args.status
                    ),
                    limit=args.limit,
                )
            )
    finally:
        client.close()


def main(argv: Sequence[str] | None = None) -> int:
    config: Config | None = None
    try:
        args = _parser().parse_args(argv)
        config = Config.from_sources(args.profile, args.config)
        _run(args, config)
        return 0
    except SkillError as error:
        result: dict[str, Any] = {
            "ok": False,
            "error": error.category,
            "message": str(error),
        }
        if config is not None:
            result["configuration_source"] = config.configuration_source
            result["credential_source"] = config.credential_source
        print(
            json.dumps(result, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
