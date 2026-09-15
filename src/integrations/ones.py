"""ONES API 客户端 - 账号密码登录 + GraphQL 缺陷获取"""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import json
import os
import re

import requests
import structlog

from config import (
    ONES_BASE_URL, ONES_EMAIL, ONES_PASSWORD,
    ONES_TEAM_ID, ONES_PROJECT_ID, ONES_ISSUE_TYPE_ID,
)
from src.utils.encrypt import JSEncryptPython
from src.integrations.ones_common import quote_wiki_segment, validate_wiki_segment

log = structlog.get_logger()


class OnesPaginationError(RuntimeError):
    """ONES task pagination cannot advance without risking truncation."""


class OnesPayloadError(RuntimeError):
    """ONES returned JSON with an unexpected top-level shape."""


def _stream_json_response(response: requests.Response, limit: int) -> tuple[object, int]:
    """Decode a bounded, fully received response while always releasing its socket."""

    buffer = bytearray()
    try:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if isinstance(length, (str, int)) and not isinstance(length, bool):
            try:
                declared = int(length)
            except ValueError:
                raise OnesPayloadError("ONES comment Content-Length is invalid") from None
            if declared < 0 or declared > limit:
                raise OnesPayloadError("ONES comment payload exceeds size limit")
        for chunk in response.iter_content(chunk_size=64 * 1024, decode_unicode=False):
            if not isinstance(chunk, (bytes, bytearray)):
                raise OnesPayloadError("ONES comment response chunk is invalid")
            if len(buffer) + len(chunk) > limit:
                raise OnesPayloadError("ONES comment payload exceeds size limit")
            buffer.extend(chunk)
        try:
            return json.loads(bytes(buffer)), len(buffer)
        except (ValueError, UnicodeError):
            raise OnesPayloadError("ONES comment response is malformed JSON") from None
    finally:
        buffer.clear()
        response.close()


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


def _defect_detail_summary(detail: dict) -> dict:
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
      key
      uuid
      name
      number
      createTime
      serverUpdateStamp
      deadline(unit: ONESDATE)
      path
      subTaskCount
      subTaskDoneCount
      status { uuid name category }
      issueType { uuid name }
      subIssueType { uuid name }
      project { uuid name }
      sprint { uuid name }
      parent { uuid name }
      assign { uuid name avatar }
      owner { uuid name avatar }
      priority { uuid value position }
      estimatedHours
      remainingManhour
      totalEstimatedHours
      totalRemainingHours
      issueTypeScope { uuid }
    }
    pageInfo {
      count totalCount hasNextPage endCursor
    }
  }
}"""

GQL_FETCH_PROJECTS = """{
  buckets(groupBy: $groupBy, orderBy: $orderBy, pagination: {limit: 50, after: "", preciseCount: true}) {
    key
    projects(limit: 10000, orderBy: $projectOrderBy, filterGroup: $projectFilterGroup) {
      uuid
      name
      icon
      status { uuid name category }
      isPin
      isArchive
      assign { uuid name avatar }
      owner { uuid name avatar }
      createTime
      planStartTime(unit: ONESDATE)
      planEndTime(unit: ONESDATE)
      type
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


class OnesClient:
    def __init__(
        self,
        base_url: str = ONES_BASE_URL,
        email: str = ONES_EMAIL,
        password: str = ONES_PASSWORD,
        team_id: str = ONES_TEAM_ID,
        project_id: str = ONES_PROJECT_ID,
        issue_type_id: str = ONES_ISSUE_TYPE_ID,
        comment_list_path_template: str | None = None,
        comment_timeout_seconds: float = 30.0,
        comment_max_pages: int = 50,
        comment_max_comments: int = 10_000,
        comment_max_payload_bytes: int = 10 * 1024 * 1024,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.team_id = team_id
        self.project_id = project_id
        self.issue_type_id = issue_type_id
        self.comment_list_path_template = comment_list_path_template
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
            for value in (comment_timeout_seconds, comment_max_pages, comment_max_comments, comment_max_payload_bytes)
        ):
            raise ValueError("comment resource limits must be positive")
        self.comment_timeout_seconds = float(comment_timeout_seconds)
        self.comment_max_pages = int(comment_max_pages)
        self.comment_max_comments = int(comment_max_comments)
        self.comment_max_payload_bytes = int(comment_max_payload_bytes)

        if email and password:
            self._login(email, password)

    def _oauth_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/authorize/callback"

    def _login(self, email: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base_url}/identity/api/login",
            json={"email": email, "password": _encrypt_password(password)},
        )
        resp.raise_for_status()
        org_user = resp.json()["org_users"][0]
        org_uuid = org_user["org_uuid"]
        org_user_uuid = org_user["org_user"]["org_user_uuid"]

        verifier = _code_verifier()
        challenge = _code_challenge(verifier)
        resp = self.session.post(
            f"{self.base_url}/identity/authorize",
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
            allow_redirects=False,
        )
        request_id = resp.headers["Location"].split("id=")[1].split("&")[0]

        self.session.get(f"{self.base_url}/identity/api/auth_request/{request_id}")
        self.session.get(f"{self.base_url}/identity/api/org_users")
        self.session.post(
            f"{self.base_url}/identity/api/auth_request/finalize",
            data=json.dumps({
                "auth_request_id": request_id,
                "region_uuid": "default",
                "org_uuid": org_uuid,
                "org_user_uuid": org_user_uuid,
            }, allow_nan=False),
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )

        resp = self.session.get(
            f"{self.base_url}/identity/authorize/callback",
            params={"id": request_id, "lang": "zh"},
            allow_redirects=False,
        )
        code = resp.text.split("code=")[1].split("&")[0]

        resp = self.session.post(
            f"{self.base_url}/identity/oauth/token",
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
        self.session.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def _graphql_url(self, t: str = "group-task-data") -> str:
        return f"{self.base_url}/project/api/project/team/{self.team_id}/items/graphql?t={t}"

    def _graphql(self, query: str, variables: dict, t: str = "group-task-data") -> dict:
        resp = self.session.post(
            self._graphql_url(t),
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("data", result)

    def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        filters: list[dict] = [{"visibleInProject_equal": True}]
        if not include_archived:
            filters.append({"isArchive_equal": False})

        data = self._graphql(GQL_FETCH_PROJECTS, {
            "groupBy": {"projects": {}},
            "orderBy": {},
            "projectOrderBy": {"isPin": "DESC", "namePinyin": "ASC", "createTime": "DESC"},
            "projectFilterGroup": [filters],
        }, t="projects-group-list-for-project-view")

        projects: list[dict] = []
        for bucket in data.get("buckets", []):
            for p in bucket.get("projects", []):
                p["_group"] = bucket.get("key", "")
                projects.append(p)
        return projects

    def fetch_task_status_configs(self, project_ids: list[str]) -> list[dict]:
        response = self.session.post(
            f"{self.base_url}/project/api/project/team/{self.team_id}/task_statuses",
            json={"project_uuids": list(project_ids)},
        )
        response.raise_for_status()
        data = response.json()
        return list(data.get("task_status_configs", [])) if isinstance(data, dict) else []

    def fetch_issue_types(self) -> list[dict]:
        data = self._graphql(GQL_FETCH_ISSUE_TYPES, {}, t="issueTypes")
        rows = data.get("issueTypes", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise OnesPayloadError("ONES issue type response is malformed")
        return rows

    def fetch_issue_type_configs(self, project_id: str) -> list[dict]:
        project_id = (project_id or "").strip()
        if not project_id:
            return []
        response = self.session.post(
            f"{self.base_url}/project/api/project/team/{self.team_id}/stamps/data"
            "?t=issue_type,issue_type_config,project",
            json={"issue_type": 0, "issue_type_config": 0, "project": 0},
            timeout=30,
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

    def fetch_task_status_definitions(self) -> list[dict]:
        response = self.session.get(
            f"{self.base_url}/project/api/project/team/{self.team_id}/task_statuses",
        )
        response.raise_for_status()
        data = response.json()
        return list(data.get("task_statuses", [])) if isinstance(data, dict) else []

    def _fetch_wiki_mapping(self, path: str, *, space_id: str = "", page_id: str = "") -> dict:
        response = self.session.get(f"{self.base_url}{path}")
        response.raise_for_status()
        payload = response.json()
        log.info(
            "ones_wiki_read",
            team_id=self.team_id,
            space_id=space_id,
            page_id=page_id,
            status=getattr(response, "status_code", None),
        )
        if not isinstance(payload, Mapping):
            raise OnesPayloadError("ONES Wiki payload must be a JSON mapping")
        return dict(payload)

    def fetch_wiki_page(self, space_id: str, page_id: str) -> dict:
        team_segment = quote_wiki_segment(self.team_id, label="team")
        space_id = validate_wiki_segment(space_id, label="space")
        page_id = validate_wiki_segment(page_id, label="page")
        return self._fetch_wiki_mapping(
            f"/wiki/api/wiki/team/{team_segment}/space/{quote_wiki_segment(space_id, label='space')}/page/{quote_wiki_segment(page_id, label='page')}",
            space_id=space_id,
            page_id=page_id,
        )

    def fetch_wiki_page_info(self, page_id: str) -> dict:
        team_segment = quote_wiki_segment(self.team_id, label="team")
        page_id = validate_wiki_segment(page_id, label="page")
        return self._fetch_wiki_mapping(
            f"/wiki/api/wiki/team/{team_segment}/page/{quote_wiki_segment(page_id, label='page')}/detail",
            page_id=page_id,
        )

    def fetch_wiki_pages_with_history(self, space_id: str) -> dict:
        team_segment = quote_wiki_segment(self.team_id, label="team")
        space_id = validate_wiki_segment(space_id, label="space")
        return self._fetch_wiki_mapping(
            f"/wiki/api/wiki/team/{team_segment}/space/{quote_wiki_segment(space_id, label='space')}/pages_with_history",
            space_id=space_id,
        )

    def fetch_defects(
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
        pid = project_id or self.project_id
        itid = issue_type_id or self.issue_type_id

        filter = {}
        if pid:
            filter["project_in"] = [pid]
        if itid:
            filter["issueType_in"] = [itid]
        if sprint_id:
            filter["sprint_in"] = [sprint_id]
        if assign:
            filter["assign_in"] = [assign]
        if status_in:
            filter["status_in"] = status_in

        tasks: list[dict] = []
        seen_task_ids: set[str] = set()
        cursor = ""
        seen_cursors = {cursor}
        while len(tasks) < limit:
            request_limit = min(page_size, limit - len(tasks))
            data = self._graphql(GQL_FETCH_TASKS, {
                "groupBy": {"tasks": {}},
                "groupOrderBy": {},
                "orderBy": {"position": "ASC", "createTime": "DESC"},
                "filterGroup": [filter] if filter else [],
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

    def fetch_issue_detail(self, issue_id: str) -> dict:
        try:
            data = self._graphql(GQL_FETCH_TASK_DETAIL, {"key": issue_id}, t="Task")
            detail = data.get("task") if isinstance(data, dict) else {}
            if isinstance(detail, dict) and detail:
                log.info("ones_fetch_issue_detail_result", issue_id=issue_id, **_defect_detail_summary(detail))
                return detail
        except Exception as exc:
            if not _is_invalid_item_key_error(exc):
                raise
            log.warning("ones_fetch_issue_detail_invalid_key_fallback", issue_id=issue_id, t="Task")

        tasks = self.fetch_defects()
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
        data = self._graphql(GQL_FETCH_TASK_DETAIL, {"key": matched_key}, t="Task")
        detail = data.get("task") if isinstance(data, dict) else {}
        if isinstance(detail, dict) and detail:
            log.info("ones_fetch_issue_detail_result", issue_id=issue_id, resolved_key=matched_key, **_defect_detail_summary(detail))
        return detail if isinstance(detail, dict) else {}

    def fetch_my_defects(self, **kwargs) -> list[dict]:
        return self.fetch_defects(assign="$currentUser", **kwargs)

    def fetch_all_defects(self) -> list[dict]:
        return self.fetch_defects(limit=5000)

    def list_comments(self, item_id: str, *, page_size: int = 200) -> list[dict]:
        if page_size <= 0 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        path = _comment_path(self.comment_list_path_template, self.team_id, item_id)
        messages_endpoint = path.endswith("/messages")
        cursor = ""
        seen = {cursor}
        comments: list[dict] = []
        total_bytes = 0
        pages = 0
        while True:
            pages += 1
            if pages > self.comment_max_pages:
                raise OnesPaginationError("ONES comment pagination limit exceeded")
            response = self.session.get(
                f"{self.base_url}{path}",
                params=None if messages_endpoint else {"limit": page_size, "after": cursor},
                timeout=self.comment_timeout_seconds,
                stream=True,
            )
            remaining = self.comment_max_payload_bytes - total_bytes
            payload, actual_size = _stream_json_response(response, remaining)
            total_bytes += actual_size
            entries = (
                payload
                if isinstance(payload, list)
                else payload.get("messages")
                if messages_endpoint and isinstance(payload, Mapping)
                else payload.get("comments")
                if isinstance(payload, Mapping)
                else None
            )
            if not isinstance(entries, list):
                raise OnesPayloadError("ONES comments payload must contain a list")
            if any(not isinstance(item, Mapping) for item in entries):
                raise OnesPayloadError("ONES comment entries must be mappings")
            comments.extend(dict(item) for item in entries)
            if len(comments) > self.comment_max_comments:
                raise OnesPayloadError("ONES comments count exceeds limit")
            if messages_endpoint:
                return comments
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

    def add_comment(self, item_id: str, text: str) -> dict:
        text = _comment_text(text)
        path = _comment_path(
            "/project/api/project/team/{team_id}/task/{item_id}/comment",
            self.team_id,
            item_id,
        )
        response = self.session.post(
            f"{self.base_url}{path}", json={"content": text},
            timeout=self.comment_timeout_seconds,
            stream=True,
        )
        payload, _ = _stream_json_response(response, self.comment_max_payload_bytes)
        if not isinstance(payload, Mapping):
            raise OnesPayloadError("ONES add comment payload must be a mapping")
        log.info("ones_add_comment", item_id=item_id)
        return dict(payload)

    def close(self) -> None:
        self.session.close()
