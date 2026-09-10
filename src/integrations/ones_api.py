"""ONES 异步 API 客户端 - httpx async + GraphQL + 评论/状态更新"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
import hashlib
import json
import os
import re
from typing import Any

import httpx
import structlog

from src.utils.encrypt import JSEncryptPython
from src.integrations.ones_common import quote_wiki_segment, validate_wiki_segment
from config.settings import OnesSettings

log = structlog.get_logger()


class OnesGraphQLResponseError(RuntimeError):
    """ONES GraphQL returned an error envelope with HTTP 200."""


class OnesPaginationError(RuntimeError):
    """ONES task pagination cannot advance without risking truncation."""


class OnesPayloadError(RuntimeError):
    """ONES returned JSON with an unexpected top-level shape."""


async def _stream_json_response(response: httpx.Response, limit: int) -> tuple[object, int]:
    buffer = bytearray()
    try:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                raise OnesPayloadError("ONES comment Content-Length is invalid") from None
            if declared < 0 or declared > limit:
                raise OnesPayloadError("ONES comment payload exceeds size limit")
        async for chunk in response.aiter_bytes():
            if len(buffer) + len(chunk) > limit:
                raise OnesPayloadError("ONES comment payload exceeds size limit")
            buffer.extend(chunk)
        size = len(buffer)
        try:
            return json.loads(bytes(buffer)), size
        except (ValueError, UnicodeError):
            raise OnesPayloadError("ONES comment response is malformed JSON") from None
    finally:
        buffer.clear()
        await response.aclose()


def _comment_path(template: str | None, team_id: str, item_id: str) -> str:
    if template is None:
        raise RuntimeError("ONES comment list endpoint is not configured")
    if (
        not template.startswith("/")
        or "?" in template
        or "#" in template
        or set(re.findall(r"\{([^{}]+)\}", template)) != {"team_id", "item_id"}
    ):
        raise ValueError("ONES comment path template is invalid")
    team = quote_wiki_segment(team_id, label="team")
    item = quote_wiki_segment(validate_wiki_segment(item_id, label="item"), label="item")
    return template.format(team_id=team, item_id=item)


def _comment_text(value: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("ONES comment text must be non-empty text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError("ONES comment text must be valid UTF-8") from None
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError("ONES comment text contains unsafe controls")
    return value


def _graphql_operation_name(query: str) -> str:
    first_line = (query or "").strip().splitlines()[0:1]
    if not first_line:
        return "unknown"
    line = first_line[0].strip()
    if line.startswith("query ") or line.startswith("mutation "):
        return line.split("(", 1)[0].split()[1]
    return line[:40] or "unknown"


def _defect_detail_summary(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "has_uuid": bool(detail.get("uuid")),
        "has_key": bool(detail.get("key")),
        "field_count": len(detail),
    }

RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0orxr+Larwt3bqq0yt5D
DgNlOh3D5kDSmidNbr3nHe/ktgr4sTWoVJAFtn2fgLB6e9zf571eeOJJ4hqp5Su2
RRTOhOojE98gEjBAi1fB7OPLR0d2TYzE/P9ahaOhT89noIGQz+Pu2n9wBK/7dg6A
MeJ51Edn4p4WlP+XKWyfH78T6v5hQ9snt5Vtz5wbpEOu+X414ENswIAhLCOCqBzj
khNqfJG/fNH/SjsjbmsqCdedirZAu8DYWBPv1x+vFn7hBOd2G40FnsWAAR8ekHgB
b+wB0DkHlDhIGK6QmbVZh4vKCcPk4QDrGY3rQPGrECGqmIi9BZK75sUeNTec6jp6
gQIDAQAB
-----END PUBLIC KEY-----"""

REDIRECT_URI = "http://aputureones.com:8088/auth/authorize/callback"
_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"

GQL_FETCH_TASKS = """{
  buckets(
    groupBy: $groupBy
    orderBy: $groupOrderBy
    pagination: $pagination
    filter: $groupFilter
  ) {
    key
    tasks(
      filterGroup: $filterGroup
      orderBy: $orderBy
      limit: $limit
    ) {
      key uuid name number createTime serverUpdateStamp
      deadline(unit: ONESDATE) path subTaskCount subTaskDoneCount
      status { uuid name category }
      issueType { uuid name }
      subIssueType { uuid name }
      project { uuid name }
      sprint { uuid name }
      parent { uuid name }
      assign { uuid name avatar }
      owner { uuid name avatar }
      priority { uuid value position }
      estimatedHours remainingManhour totalEstimatedHours totalRemainingHours
      issueTypeScope { uuid }
    }
    pageInfo { count totalCount hasNextPage endCursor }
  }
}"""

GQL_FETCH_PROJECTS = """{
  buckets(groupBy: $groupBy, orderBy: $orderBy, pagination: {limit: 50, after: "", preciseCount: true}) {
    key
    projects(limit: 10000, orderBy: $projectOrderBy, filterGroup: $projectFilterGroup) {
      uuid name icon status { uuid name category } isPin isArchive
      assign { uuid name avatar } owner { uuid name avatar }
      createTime planStartTime(unit: ONESDATE) planEndTime(unit: ONESDATE) type
    }
    pageInfo { count totalCount hasNextPage }
  }
}"""

GQL_FETCH_ISSUE_TYPES = """
query IssueTypes {
  issueTypes {
    uuid
    name
    builtIn
    detailType
  }
}
"""

GQL_FETCH_SPRINTS = """
query SPRINTS($filterGroup: [Filter!], $orderBy: SprintOrderBy) {
  list: sprints(filterGroup: $filterGroup, orderBy: $orderBy) {
    title: name
    uuid
    key
    project {
      name
      uuid
    }
    statusInfo: status {
      name
      uuid
      category
      key
      categoryValue
    }
    sprintStatusList
  }
}
"""

GQL_FETCH_TASK_DETAIL = """
query Task($key: Key) {
  task(key: $key) {
    key
    ...TaskHeader_task1
    ...Permission_Task1
    ...TaskAction_task1
    ...Permission_Task2
    ...TaskPrimaryFields_task1
    ...Permission_Task3
    ...TaskTabs_task1
    ...TaskDesc1
    ...TaskFieldList_task1
    ...TaskFieldList_task2
    ...WideTaskSide_task1
    ...Permission_Task4
  }
}

fragment TaskHeader_task1 on Task {
  uuid
  number
  name
  issueType {
    key
    uuid
    name
    builtIn
  }
  subIssueType {
    key
    uuid
    name
    builtIn
  }
  canView(attachPermission: {permissions: ["view_tasks"]})
  canEdit(attachPermission: {permissions: ["update_tasks"]})
  canDelete(attachPermission: {permissions: ["delete_tasks"]})
  canTransitTask(attachPermission: {permissions: ["transit_tasks"]})
  canUpdateWatchers(attachPermission: {permissions: ["update_task_watchers"]})
  parent {
    uuid
    number
    issueType {
      uuid
    }
    subIssueType {
      uuid
    }
    canView(attachPermission: {permissions: ["view_tasks"]})
    parent {
      uuid
      number
      issueType {
        uuid
      }
      subIssueType {
        uuid
      }
      canView(attachPermission: {permissions: ["view_tasks"]})
      parent {
        uuid
        number
        issueType {
          uuid
        }
        subIssueType {
          uuid
        }
        canView(attachPermission: {permissions: ["view_tasks"]})
        parent {
          uuid
          number
          issueType {
            uuid
          }
          subIssueType {
            uuid
          }
          canView(attachPermission: {permissions: ["view_tasks"]})
          parent {
            uuid
            number
            issueType {
              uuid
            }
            subIssueType {
              uuid
            }
            canView(attachPermission: {permissions: ["view_tasks"]})
            parent {
              uuid
              number
              issueType {
                uuid
              }
              subIssueType {
                uuid
              }
              canView(attachPermission: {permissions: ["view_tasks"]})
              parent {
                uuid
                number
                issueType {
                  uuid
                }
                subIssueType {
                  uuid
                }
                canView(attachPermission: {permissions: ["view_tasks"]})
                parent {
                  uuid
                  number
                  issueType {
                    uuid
                  }
                  subIssueType {
                    uuid
                  }
                  canView(attachPermission: {permissions: ["view_tasks"]})
                  parent {
                    uuid
                    number
                    issueType {
                      uuid
                    }
                    subIssueType {
                      uuid
                    }
                    canView(attachPermission: {permissions: ["view_tasks"]})
                    parent {
                      uuid
                      number
                      issueType {
                        uuid
                      }
                      subIssueType {
                        uuid
                      }
                      canView(attachPermission: {permissions: ["view_tasks"]})
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}

fragment Permission_Task1 on Task {
  project {
    uuid
  }
  issueTypeScope {
    uuid
  }
  issueType {
    uuid
    name
  }
  owner {
    uuid
    name
    namePinyin
    avatar
  }
  assign {
    uuid
    name
    namePinyin
    avatar
  }
  watchers {
    uuid
    name
    namePinyin
    avatar
  }
  hasEditPermission(attachPermission: {permissions: ["update_tasks"]})
}

fragment TaskAction_task1 on Task {
  key
  uuid
  project {
    uuid
    isSample
    isArchive
  }
  issueType {
    uuid
    detailType
  }
  canEdit(attachPermission: {permissions: ["update_tasks"]})
  uuid
  project {
    uuid
  }
  issueType {
    uuid
  }
  number
  summary: name
  project {
    uuid
  }
  path
  project {
    uuid
    isArchive
  }
  issueType {
    uuid
  }
  subTaskCount
  subTaskDoneCount
  sprint {
    name
    uuid
  }
  relatedWikiPages {
    uuid
    title
    referenceType
    ref_type: referenceType
    subReferenceType
    sub_ref_type: subReferenceType
    errorMessage
  }
  relatedWikiPagesCount
}

fragment Permission_Task2 on Task {
  project {
    uuid
  }
  issueTypeScope {
    uuid
  }
  issueType {
    uuid
    name
  }
  owner {
    uuid
    name
    namePinyin
    avatar
  }
  assign {
    uuid
    name
    namePinyin
    avatar
  }
  watchers {
    uuid
    name
    namePinyin
    avatar
  }
  hasEditPermission(attachPermission: {permissions: ["update_tasks"]})
  hasDeletePermission(attachPermission: {permissions: ["delete_tasks"]})
  hasUpdateStatusPermission(attachPermission: {permissions: ["transit_tasks"]})
  hasUpdateAllManHourPermission(attachPermission: {permissions: ["manage_task_record_manhours"]})
  hasUpdateOwnManHourPermission(attachPermission: {permissions: ["manage_task_own_record_manhours"]})
  hasChangeIssueTypePermission(attachPermission: {permissions: ["update_tasks_issue_type"]})
}

fragment TaskPrimaryFields_task1 on Task {
  uuid
  project {
    uuid
  }
  issueType {
    uuid
  }
  subIssueType {
    uuid
  }
  assign {
    uuid
    name
    namePinyin
    avatar
  }
  status {
    uuid
    name
    namePinyin
    category
    builtIn
    detailType
  }
}

fragment Permission_Task3 on Task {
  project {
    uuid
  }
  issueTypeScope {
    uuid
  }
  issueType {
    uuid
    name
  }
  owner {
    uuid
    name
    namePinyin
    avatar
  }
  assign {
    uuid
    name
    namePinyin
    avatar
  }
  watchers {
    uuid
    name
    namePinyin
    avatar
  }
  hasEditPermission(attachPermission: {permissions: ["update_tasks"]})
  hasUpdateStatusPermission(attachPermission: {permissions: ["transit_tasks"]})
}

fragment TaskTabs_task1 on Task {
  key
  relatedTasksCount
  allRelatedTasksCount: stubRelatedTasksCount
  relatedActivitiesCount
  attachmentCount
  relatedWikiPagesCount
}

fragment TaskDesc1 on Task {
  uuid
  description
  descriptionText
  desc_rich: description
}

fragment TaskFieldList_task1 on Task {
  uuid
  project {
    uuid
  }
  issueType {
    uuid
  }
  subIssueType {
    uuid
  }
  assign {
    uuid
    name
    namePinyin
    avatar
  }
  sprint {
    description
    name
    namePinyin
    uuid
    project {
      name
      sprintComponent {
        uuid
      }
      uuid
    }
    value: uuid
    label: name
  }
  _AaBYRMn4 {
    bgColor
    color
    defaultSelected
    position
    uuid
    value
  }
  _UxZQD5CR {
    bgColor
    color
    defaultSelected
    position
    uuid
    value
  }
  _BpyJ4tGk {
    bgColor
    color
    defaultSelected
    position
    uuid
    value
  }
  status {
    uuid
    name
    namePinyin
    category
    builtIn
    detailType
  }
  solution {
    bgColor
    color
    defaultSelected
    position
    uuid
    value
  }
  _8LQAbR5j {
    bgColor
    color
    defaultSelected
    position
    uuid
    value
  }
  _KK9u9662
  isOnlineDefect {
    bgColor
    color
    defaultSelected
    position
    uuid
    value
  }
  _8qMBnb1P
  _2r28N49C
  priority {
    bgColor
    color
    defaultSelected
    position
    uuid
    value
  }
}

fragment TaskFieldList_task2 on Task {
  uuid
  project {
    uuid
  }
  issueType {
    uuid
  }
  subIssueType {
    uuid
  }
  project {
    uuid
    name
    namePinyin
    isSample
    isArchive
    activityChart {
      uuid
    }
    value: uuid
    label: name
  }
  issueType {
    key
    uuid
    value: uuid
    name
    label: name
    builtIn
    namePinyin
    icon
    manhourStatisticMode
    detailType
    subIssueType
  }
  subIssueType {
    key
    uuid
    value: uuid
    name
    label: name
    builtIn
    namePinyin
    icon
    manhourStatisticMode
    detailType
    subIssueType
  }
  owner {
    uuid
    name
    namePinyin
    avatar
  }
  createTime
  serverUpdateStamp
}

fragment WideTaskSide_task1 on Task {
  owner {
    uuid
    name
    namePinyin
    avatar
  }
  assign {
    uuid
    name
    namePinyin
    avatar
  }
  watchers {
    uuid
    name
    namePinyin
    avatar
  }
  issueType {
    key
    uuid
    value: uuid
    name
    label: name
    builtIn
    namePinyin
    icon
    manhourStatisticMode
    detailType
    subIssueType
  }
  subIssueType {
    key
    uuid
    value: uuid
    name
    label: name
    builtIn
    namePinyin
    icon
    manhourStatisticMode
    detailType
    subIssueType
  }
}

fragment Permission_Task4 on Task {
  project {
    uuid
  }
  issueTypeScope {
    uuid
  }
  issueType {
    uuid
    name
  }
  owner {
    uuid
    name
    namePinyin
    avatar
  }
  assign {
    uuid
    name
    namePinyin
    avatar
  }
  watchers {
    uuid
    name
    namePinyin
    avatar
  }
  hasEditPermission(attachPermission: {permissions: ["update_tasks"]})
  hasUpdateWatchersPermission(attachPermission: {permissions: ["update_task_watchers"]})
}
"""


def _encrypt_password(password: str) -> str:
    rsa = JSEncryptPython()
    rsa.setPublicKey(RSA_PUBLIC_KEY)
    return rsa.encrypt(password)


def _code_verifier(length: int = 43) -> str:
    return "".join(_CHARS[os.urandom(1)[0] % len(_CHARS)] for _ in range(length))


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _is_invalid_item_key_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    body = str(getattr(response, "text", "") or "")
    return "InvalidParameter.Item.Key.InvalidFormat" in body


class OnesAsyncClient:
    """ONES 异步 API 客户端

    用法:
        async with OnesAsyncClient(settings) as client:
            defects = await client.fetch_defects()
            await client.add_comment("item-id", "分析完成")
            await client.update_status("item-id", "done")
    """

    def __init__(
        self,
        settings: OnesSettings | None = None,
        *,
        comment_list_path_template: str | None = None,
    ):
        self._settings = settings or OnesSettings()
        self._base_url = self._settings.base_url.rstrip("/")
        self._team_id = self._settings.team_id
        self._project_id = self._settings.project_id
        self._issue_type_id = self._settings.issue_type_id
        self._token: str | None = None
        self._org_uuid: str = ""
        self._org_user_uuid: str = ""
        self._client: httpx.AsyncClient | None = None
        self._init_lock = asyncio.Lock()
        self._reauth_lock = asyncio.Lock()
        self._auth_generation = 0
        self._ready = False
        self._comment_list_path_template = (
            comment_list_path_template
            if comment_list_path_template is not None
            else getattr(self._settings, "comment_list_path_template", None)
        )
        self._comment_timeout_seconds = self._settings.comment_timeout_seconds
        self._comment_max_pages = self._settings.comment_max_pages
        self._comment_max_comments = self._settings.comment_max_comments
        self._comment_max_payload_bytes = self._settings.comment_max_payload_bytes

    def _oauth_redirect_uri(self) -> str:
        return f"{self._base_url}/auth/authorize/callback"

    async def _get_client(self) -> httpx.AsyncClient:
        if not self._ready:
            async with self._init_lock:
                if not self._ready:
                    if self._client is None:
                        log.info(
                            "ones_async_client_init",
                            base_url=self._base_url,
                            team_id=self._team_id,
                            has_email=bool(self._settings.email),
                            has_password=bool(self._settings.password),
                        )
                        self._client = httpx.AsyncClient(verify=False, timeout=30.0)
                    if self._settings.email and self._settings.password:
                        await self._login(self._client)
                        self._auth_generation += 1
                    self._ready = True
        return self._client

    async def __aenter__(self) -> OnesAsyncClient:
        await self._get_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _login(self, client: httpx.AsyncClient | None = None) -> None:
        if client is None:
            client = self._client
        if client is None:
            raise RuntimeError("ONES HTTP client must be initialized before login")
        log.info(
            "ones_login_start",
            base_url=self._base_url,
            team_id=self._team_id,
            has_email=bool(self._settings.email),
            has_password=bool(self._settings.password),
        )

        resp = await client.post(
            f"{self._base_url}/identity/api/login",
            json={"email": self._settings.email, "password": _encrypt_password(self._settings.password)},
        )
        resp.raise_for_status()
        org_user = resp.json()["org_users"][0]
        org_uuid = org_user["org_uuid"]
        self._org_uuid = org_uuid
        org_user_uuid = org_user["org_user"]["org_user_uuid"]
        self._org_user_uuid = str(org_user_uuid)

        verifier = _code_verifier()
        challenge = _code_challenge(verifier)
        resp = await client.post(
            f"{self._base_url}/identity/authorize",
            data={
                "client_id": "ones.v1",
                "scope": f"openid offline_access ones:org:{org_uuid}:{org_user_uuid}",
                "response_type": "code",
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "redirect_uri": self._oauth_redirect_uri(),
                "state": f"org_uuid={org_uuid}",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        request_id = resp.headers["Location"].split("id=")[1].split("&")[0]

        await client.get(f"{self._base_url}/identity/api/auth_request/{request_id}")
        await client.get(f"{self._base_url}/identity/api/org_users")
        await client.post(
            f"{self._base_url}/identity/api/auth_request/finalize",
            content=json.dumps({
                "auth_request_id": request_id,
                "region_uuid": "default",
                "org_uuid": org_uuid,
                "org_user_uuid": org_user_uuid,
            }, allow_nan=False),
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )

        resp = await client.get(
            f"{self._base_url}/identity/authorize/callback",
            params={"id": request_id, "lang": "zh"},
            follow_redirects=False,
        )
        code = resp.text.split("code=")[1].split("&")[0]

        resp = await client.post(
            f"{self._base_url}/identity/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "ones.v1",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self._oauth_redirect_uri(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {self._token}"
        log.info("ones_login_success", base_url=self._base_url, team_id=self._team_id)

    async def _reauthenticate_if_stale(
        self,
        client: httpx.AsyncClient,
        observed_generation: int,
    ) -> None:
        async with self._reauth_lock:
            if self._auth_generation != observed_generation:
                return
            await self._login(client)
            self._auth_generation += 1

    async def _graphql(self, query: str, variables: dict, t: str = "group-task-data") -> dict:
        client = await self._get_client()
        operation_name = _graphql_operation_name(query)
        log.info(
            "ones_graphql_request",
            base_url=self._base_url,
            team_id=self._team_id,
            t=t,
            operation=operation_name,
            variable_keys=sorted(list(variables.keys())),
        )
        observed_generation = self._auth_generation
        for attempt in range(2):
            resp = await client.post(
                f"{self._base_url}/project/api/project/team/{self._team_id}/items/graphql?t={t}",
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json;charset=UTF-8"},
            )
            try:
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                if (
                    attempt == 0
                    and exc.response.status_code == 401
                    and self._settings.email
                    and self._settings.password
                ):
                    await self._reauthenticate_if_stale(client, observed_generation)
                    continue
                body = (exc.response.text or "").strip()
                log.error(
                    "ones_graphql_request_failed",
                    base_url=self._base_url,
                    team_id=self._team_id,
                    t=t,
                    operation=operation_name,
                    status_code=getattr(exc.response, "status_code", None),
                    response_body=body[:1000],
                )
                if body:
                    raise httpx.HTTPStatusError(
                        f"{exc} | response body: {body}",
                        request=exc.request,
                        response=exc.response,
                    ) from exc
                raise
        result = resp.json()
        if isinstance(result, dict) and result.get("errors"):
            errors = result["errors"]
            messages = [
                str(error.get("message") or "GraphQL error")
                for error in errors
                if isinstance(error, dict)
            ]
            log.error(
                "ones_graphql_response_errors",
                base_url=self._base_url,
                team_id=self._team_id,
                t=t,
                operation=operation_name,
                error_count=len(errors) if isinstance(errors, list) else 1,
            )
            raise OnesGraphQLResponseError("; ".join(messages) or "ONES GraphQL response contains errors")
        log.info(
            "ones_graphql_request_succeeded",
            base_url=self._base_url,
            team_id=self._team_id,
            t=t,
            operation=operation_name,
            response_keys=sorted(list(result.keys())) if isinstance(result, dict) else [],
        )
        return result.get("data", result)

    # ── 数据获取 ──────────────────────────────────────────

    async def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        filters: list[dict] = [{"visibleInProject_equal": True}]
        if not include_archived:
            filters.append({"isArchive_equal": False})
        data = await self._graphql(GQL_FETCH_PROJECTS, {
            "groupBy": {"projects": {}},
            "orderBy": {},
            "projectOrderBy": {"isPin": "DESC", "namePinyin": "ASC", "createTime": "DESC"},
            "projectFilterGroup": [filters],
        }, t="projects-group-list-for-project-view")
        projects = []
        for bucket in data.get("buckets", []):
            for p in bucket.get("projects", []):
                p["_group"] = bucket.get("key", "")
                projects.append(p)
        return projects

    async def fetch_defects(
        self,
        project_id: str | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assign: str | None = None,
        status_in: list[str] | None = None,
        limit: int = 1000,
        page_size: int = 200,
    ) -> list[dict]:
        if limit <= 0:
            return []
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        pid = project_id or self._project_id
        itid = issue_type_id or self._issue_type_id
        filter_ = {}
        if pid:
            filter_["project_in"] = [pid]
        if itid:
            filter_["issueType_in"] = [itid]
        if sprint_id:
            filter_["sprint_in"] = [sprint_id]
        if assign:
            filter_["assign_in"] = [assign]
        if status_in:
            filter_["status_in"] = status_in

        tasks: list[dict] = []
        seen_task_ids: set[str] = set()
        cursor = ""
        seen_cursors = {cursor}
        while len(tasks) < limit:
            request_limit = min(page_size, limit - len(tasks))
            data = await self._graphql(GQL_FETCH_TASKS, {
                "groupBy": {"tasks": {}},
                "groupOrderBy": {},
                "orderBy": {"position": "ASC", "createTime": "DESC"},
                "filterGroup": [filter_] if filter_ else [],
                "pagination": {"limit": request_limit, "after": cursor, "preciseCount": True},
                "limit": request_limit,
            })
            buckets = data.get("buckets", [])
            for bucket in buckets:
                for task in bucket.get("tasks", []):
                    task_id = str(task.get("uuid") or "")
                    if task_id and task_id in seen_task_ids:
                        continue
                    if task_id:
                        seen_task_ids.add(task_id)
                    task["_status_group"] = bucket.get("key", "")
                    tasks.append(task)
                    if len(tasks) >= limit:
                        break
                if len(tasks) >= limit:
                    break

            page_infos = []
            for bucket in buckets:
                page_info = bucket.get("pageInfo")
                if not isinstance(page_info, Mapping):
                    raise OnesPaginationError("ONES pagination bucket has invalid pageInfo")
                if not isinstance(page_info.get("hasNextPage"), bool):
                    raise OnesPaginationError("ONES pagination pageInfo has invalid hasNextPage")
                page_infos.append(page_info)
            if not any(info["hasNextPage"] for info in page_infos):
                break
            next_cursors = {
                str(info.get("endCursor") or "").strip()
                for info in page_infos
                if info["hasNextPage"]
            }
            if len(next_cursors) != 1:
                raise OnesPaginationError("ONES pagination has no stable cursor")
            next_cursor = next(iter(next_cursors))
            if not next_cursor or next_cursor in seen_cursors:
                raise OnesPaginationError("ONES pagination cursor did not advance")
            cursor = next_cursor
            seen_cursors.add(cursor)

        return tasks[:limit]

    async def fetch_issue_detail(self, issue_id: str) -> dict:
        log.info("ones_fetch_issue_detail", issue_id=issue_id, t="Task")
        try:
            data = await self._graphql(GQL_FETCH_TASK_DETAIL, {"key": issue_id}, t="Task")
            detail = data.get("task") if isinstance(data, dict) else {}
            if isinstance(detail, dict) and detail:
                log.info("ones_fetch_issue_detail_result", issue_id=issue_id, **_defect_detail_summary(detail))
                return detail
        except Exception as exc:
            if not _is_invalid_item_key_error(exc):
                raise
            log.warning("ones_fetch_issue_detail_invalid_key_fallback", issue_id=issue_id, t="Task")

        tasks = await self.fetch_defects()
        matched = next(
            (
                task
                for task in tasks
                if isinstance(task, dict) and (task.get("uuid") == issue_id or task.get("key") == issue_id)
            ),
            {},
        )
        if not matched:
            log.warning("ones_fetch_issue_detail_list_lookup_missed", issue_id=issue_id, t="Task")
            return {}

        matched_key = str(matched.get("key") or issue_id).strip()
        log.info("ones_fetch_issue_detail_resolved_key", issue_id=issue_id, resolved_key=matched_key, t="Task")
        data = await self._graphql(GQL_FETCH_TASK_DETAIL, {"key": matched_key}, t="Task")
        detail = data.get("task") if isinstance(data, dict) else {}
        if isinstance(detail, dict) and detail:
            log.info("ones_fetch_issue_detail_result", issue_id=issue_id, resolved_key=matched_key, **_defect_detail_summary(detail))
        return detail if isinstance(detail, dict) else {}

    async def fetch_sprints(self, project_id: str) -> list[dict]:
        project_id = (project_id or "").strip()
        if not project_id:
            return []

        data = await self._graphql(
            GQL_FETCH_SPRINTS,
            {
                "filterGroup": [{"project_in": [project_id], "visibleInProject_equal": True}],
                "orderBy": {"namePinyin": "ASC", "createTime": "ASC"},
            },
            t=f"sprint_select_{project_id}",
        )
        return list(data.get("list", []))

    async def fetch_task_status_configs(self, project_ids: list[str]) -> list[dict[str, Any]]:
        client = await self._get_client()
        response = await client.post(
            f"{self._base_url}/project/api/project/team/{self._team_id}/task_statuses",
            json={"project_uuids": list(project_ids)},
        )
        response.raise_for_status()
        data = response.json()
        return list(data.get("task_status_configs", [])) if isinstance(data, dict) else []

    async def fetch_issue_types(self) -> list[dict[str, Any]]:
        data = await self._graphql(GQL_FETCH_ISSUE_TYPES, {}, t="issueTypes")
        return list(data.get("issueTypes", [])) if isinstance(data, dict) else []

    async def fetch_issue_type_configs(self, project_id: str) -> list[dict[str, Any]]:
        project_id = (project_id or "").strip()
        if not project_id:
            return []
        client = await self._get_client()
        response = await client.post(
            f"{self._base_url}/project/api/project/team/{self._team_id}/stamps/data"
            "?t=issue_type,issue_type_config,project",
            json={"issue_type": 0, "issue_type_config": 0, "project": 0},
        )
        response.raise_for_status()
        data = response.json()
        configs = (
            data.get("issue_type_config", {}).get("issue_type_configs", [])
            if isinstance(data, dict)
            and isinstance(data.get("issue_type_config"), dict)
            else []
        )
        return [
            item
            for item in configs
            if isinstance(item, dict)
            and (item.get("project_uuid") == project_id or item.get("scope") == project_id)
        ]

    async def fetch_task_status_definitions(self) -> list[dict[str, Any]]:
        client = await self._get_client()
        response = await client.get(
            f"{self._base_url}/project/api/project/team/{self._team_id}/task_statuses",
        )
        response.raise_for_status()
        data = response.json()
        return list(data.get("task_statuses", [])) if isinstance(data, dict) else []

    async def _fetch_wiki_mapping(self, path: str, *, space_id: str = "", page_id: str = "") -> dict:
        client = await self._get_client()
        response = await client.get(f"{self._base_url}{path}")
        response.raise_for_status()
        payload = response.json()
        log.info(
            "ones_wiki_read",
            team_id=self._team_id,
            space_id=space_id,
            page_id=page_id,
            status=response.status_code,
        )
        if not isinstance(payload, Mapping):
            raise OnesPayloadError("ONES Wiki payload must be a JSON mapping")
        return dict(payload)

    async def fetch_wiki_page(self, space_id: str, page_id: str) -> dict:
        team_segment = quote_wiki_segment(self._team_id, label="team")
        space_id = validate_wiki_segment(space_id, label="space")
        page_id = validate_wiki_segment(page_id, label="page")
        return await self._fetch_wiki_mapping(
            f"/wiki/api/wiki/team/{team_segment}/space/{quote_wiki_segment(space_id, label='space')}/page/{quote_wiki_segment(page_id, label='page')}",
            space_id=space_id,
            page_id=page_id,
        )

    async def fetch_wiki_page_info(self, page_id: str) -> dict:
        team_segment = quote_wiki_segment(self._team_id, label="team")
        page_id = validate_wiki_segment(page_id, label="page")
        return await self._fetch_wiki_mapping(
            f"/wiki/api/wiki/team/{team_segment}/page/{quote_wiki_segment(page_id, label='page')}/detail",
            page_id=page_id,
        )

    async def fetch_wiki_pages_with_history(self, space_id: str) -> dict:
        team_segment = quote_wiki_segment(self._team_id, label="team")
        space_id = validate_wiki_segment(space_id, label="space")
        return await self._fetch_wiki_mapping(
            f"/wiki/api/wiki/team/{team_segment}/space/{quote_wiki_segment(space_id, label='space')}/pages_with_history",
            space_id=space_id,
        )

    async def fetch_role_members(self, project_id: str) -> list[dict[str, Any]]:
        project_id = (project_id or "").strip()
        if not project_id:
            return []

        client = await self._get_client()
        resp = await client.get(
            f"{self._base_url}/project/api/project/team/{self._team_id}/project/{project_id}/role_members",
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("role_members"), list):
                return data["role_members"]
            if isinstance(data.get("data"), list):
                return data["data"]
        return []

    async def fetch_team_members(self, uuids: list[str] | None = None) -> list[dict[str, Any]]:
        client = await self._get_client()
        org_uuid = self._org_uuid.strip()
        if not org_uuid:
            return []

        resp = await client.request(
            "POST",
            f"{self._base_url}/project/api/project/organization/{org_uuid}/users",
            params={"team_uuid": self._team_id},
            json={"uuids": list(uuids or [])},
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("users"), list):
                return data["users"]
            if isinstance(data.get("data"), list):
                return data["data"]
        return []

    async def fetch_current_user_identity(self) -> dict[str, str]:
        await self._get_client()
        if not self._org_user_uuid:
            return {}
        return {"uuid": self._org_user_uuid}

    async def fetch_my_defects(self, **kwargs) -> list[dict]:
        return await self.fetch_defects(assign="$currentUser", **kwargs)

    # ── 评论 & 状态 ────────────────────────────────────────

    async def list_comments(self, item_id: str, *, page_size: int = 200) -> list[dict]:
        if page_size <= 0 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        path = _comment_path(self._comment_list_path_template, self._team_id, item_id)
        client = await self._get_client()
        cursor = ""
        seen = {cursor}
        comments: list[dict] = []
        total_bytes = 0
        pages = 0
        while True:
            pages += 1
            if pages > self._comment_max_pages:
                raise OnesPaginationError("ONES comment pagination limit exceeded")
            async with client.stream(
                "GET", f"{self._base_url}{path}",
                params={"limit": page_size, "after": cursor},
                timeout=self._comment_timeout_seconds,
            ) as response:
                payload, actual_size = await _stream_json_response(
                    response, self._comment_max_payload_bytes - total_bytes
                )
            total_bytes += actual_size
            if not isinstance(payload, Mapping) or not isinstance(payload.get("comments"), list):
                raise OnesPayloadError("ONES comments payload must contain a list")
            if any(not isinstance(item, Mapping) for item in payload["comments"]):
                raise OnesPayloadError("ONES comment entries must be mappings")
            comments.extend(dict(item) for item in payload["comments"])
            if len(comments) > self._comment_max_comments:
                raise OnesPayloadError("ONES comments count exceeds limit")
            page_info = payload.get("pageInfo")
            if not isinstance(page_info, Mapping) or not isinstance(page_info.get("hasNextPage"), bool):
                raise OnesPaginationError("ONES comment pagination has invalid pageInfo")
            if not page_info["hasNextPage"]:
                return comments
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise OnesPaginationError("ONES comment pagination cursor did not advance")
            cursor = next_cursor
            seen.add(cursor)

    async def add_comment(self, item_id: str, text: str) -> dict:
        text = _comment_text(text)
        path = _comment_path(
            "/project/api/project/team/{team_id}/task/{item_id}/comment",
            self._team_id,
            item_id,
        )
        client = await self._get_client()
        async with client.stream(
            "POST", f"{self._base_url}{path}", json={"content": text},
            timeout=self._comment_timeout_seconds,
        ) as response:
            payload, _ = await _stream_json_response(
                response, self._comment_max_payload_bytes
            )
        log.info("ones_add_comment", item_id=item_id)
        if not isinstance(payload, Mapping):
            raise OnesPayloadError("ONES add comment payload must be a mapping")
        return dict(payload)

    async def update_status(self, item_id: str, status: str) -> dict:
        client = await self._get_client()
        resp = await client.patch(
            f"{self._base_url}/project/api/project/team/{self._team_id}/task/{item_id}",
            json={"status": status},
        )
        resp.raise_for_status()
        log.info("ones_update_status", item_id=item_id, status=status)
        return resp.json()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._ready = False
