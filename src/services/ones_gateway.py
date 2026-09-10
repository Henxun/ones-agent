"""Unified backend-facing ONES gateway.

This service provides one canonical interface for ONES project and defect
access while preserving the existing sync and async client implementations
behind an adapter boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math
import re
import time
from typing import TYPE_CHECKING, Callable, Iterable, Sequence
from urllib.parse import unquote, urlsplit

import httpx
import requests
import structlog

from src.contracts import (
    DefectRecord,
    IdentityRef,
    IssueTypeRef,
    PriorityRef,
    ProjectRef,
    RequirementRecord,
    StatusRef,
    WikiPageRef,
    WikiPageSnapshot,
    WorkflowStatusRef,
)
from src.integrations.ones_common import quote_wiki_segment, validate_wiki_segment


log = structlog.get_logger()

if TYPE_CHECKING:
    from config.settings import OnesSettings
    from src.integrations.ones import OnesClient
    from src.integrations.ones_api import OnesAsyncClient


_DEFAULT_DEFECT_LIMIT = 1000
_CURRENT_USER_TOKEN = "$currentUser"
_OPEN_STATUS_CATEGORIES = frozenset({"open", "todo", "to_do", "doing", "in_progress", "pending"})
_LEGACY_ISSUE_TYPE_COMPONENTS = {
    "0": "custom",
    "1": "requirement",
    "2": "task",
    "3": "defect",
    "4": "sub_task",
    "5": "sub_requirement",
    "6": "ticket",
    "7": "user_story",
    "8": "sub_check_item",
    "9": "release",
    "10": "epic",
}
_CLOSED_STATUS_CATEGORIES = frozenset({"done", "completed", "cancelled", "discarded", "closed"})


class OnesGatewayError(Exception):
    """Base exception for stable gateway-level ONES failures."""


class OnesGatewayAuthError(OnesGatewayError):
    """Raised when ONES authentication or authorization fails."""


class OnesGatewayTimeoutError(OnesGatewayError):
    """Raised when ONES does not respond before the gateway timeout."""


class OnesGatewayPayloadError(OnesGatewayError):
    """Raised when ONES returns a structurally invalid payload."""


class OnesGatewayNotFoundError(OnesGatewayError):
    """Raised when a requested ONES entity cannot be resolved."""


@dataclass(frozen=True, slots=True)
class _DefectQuery:
    project_ids: tuple[str, ...]
    issue_type_id: str | None
    sprint_id: str | None
    assignee: str | None
    status_ids: tuple[str, ...]
    mine: bool
    limit: int
    page_size: int


@dataclass(slots=True)
class OnesGateway:
    """Canonical backend-facing interface for ONES access.

    The active runtime can use the async methods directly. Legacy sync callers
    can use the sync wrappers without importing the underlying client modules.
    """

    settings: OnesSettings | None = None
    async_client: OnesAsyncClient | None = None
    sync_client: OnesClient | None = None
    retry_backoff: Callable[[int], float] = field(default=lambda attempt: 0.1 * (2 ** (attempt - 1)), repr=False)
    _managed_async_client: OnesAsyncClient | None = field(default=None, init=False, repr=False)
    _managed_sync_client: OnesClient | None = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> OnesGateway:
        await self._get_async_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def list_projects(self, include_archived: bool = False) -> list[dict]:
        return await self._call_async("fetch_projects", include_archived=include_archived)

    async def list_project_issue_types(self, project_id: str) -> list[IssueTypeRef]:
        """Return the work-item types enabled for one ONES project."""

        definitions = await self._call_async("fetch_issue_types")
        configs = await self._call_async("fetch_issue_type_configs", project_id)
        return self._build_project_issue_types(definitions, configs, project_id)

    def list_project_issue_types_sync(self, project_id: str) -> list[IssueTypeRef]:
        definitions = self._call_sync("fetch_issue_types")
        configs = self._call_sync("fetch_issue_type_configs", project_id)
        return self._build_project_issue_types(definitions, configs, project_id)

    @classmethod
    def _build_project_issue_types(
        cls,
        definitions: object,
        configs: object,
        project_id: str,
    ) -> list[IssueTypeRef]:
        if (
            type(project_id) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project_id) is None
            or not isinstance(definitions, list)
            or not isinstance(configs, list)
        ):
            raise OnesGatewayPayloadError("Malformed ONES project issue type metadata")
        definitions_by_id: dict[str, IssueTypeRef] = {}
        for item in definitions:
            if not isinstance(item, dict):
                raise OnesGatewayPayloadError("Malformed ONES issue type definition")
            identity = item.get("id", item.get("uuid"))
            name = item.get("name")
            raw_component_type = item.get("type")
            component_type = (
                raw_component_type.strip().casefold()
                if type(raw_component_type) is str
                else ""
            )
            raw_detail_type = item.get("detailType", "")
            if not component_type and (
                isinstance(raw_detail_type, (str, int))
                and not isinstance(raw_detail_type, bool)
            ):
                component_type = _LEGACY_ISSUE_TYPE_COMPONENTS.get(
                    str(raw_detail_type).strip(), ""
                )
            if (
                type(identity) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity) is None
                or type(name) is not str
                or not name.strip()
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", component_type) is None
            ):
                raise OnesGatewayPayloadError("Malformed ONES issue type definition")
            raw_built_in = item.get("buildIn", item.get("builtIn"))
            if type(raw_built_in) is not bool:
                raise OnesGatewayPayloadError("Malformed ONES issue type definition")
            definition = IssueTypeRef(
                id=identity,
                name=cls._ensure_utf8(name.strip(), context="issue type name"),
                built_in=raw_built_in,
                detail_type=str(raw_detail_type).strip(),
                component_type=component_type,
            )
            existing = definitions_by_id.get(identity)
            if existing is not None and existing != definition:
                raise OnesGatewayPayloadError(
                    "Conflicting ONES issue type definitions"
                )
            definitions_by_id[identity] = definition

        result: list[IssueTypeRef] = []
        seen: set[str] = set()
        for item in configs:
            if not isinstance(item, dict):
                raise OnesGatewayPayloadError("Malformed ONES issue type config")
            scope = item.get("project_uuid") or item.get("scope")
            identity = item.get("issue_type_uuid")
            if scope != project_id:
                raise OnesGatewayPayloadError("Malformed ONES issue type config scope")
            if type(identity) is not str or identity not in definitions_by_id:
                raise OnesGatewayPayloadError("Malformed ONES issue type config identity")
            if identity in seen:
                continue
            seen.add(identity)
            configured_name = item.get("name")
            name = (
                cls._ensure_utf8(configured_name.strip(), context="issue type config name")
                if type(configured_name) is str and configured_name.strip()
                else definitions_by_id[identity].name
            )
            definition = definitions_by_id[identity]
            result.append(
                IssueTypeRef(
                    id=identity,
                    name=name,
                    built_in=definition.built_in,
                    detail_type=definition.detail_type,
                    component_type=definition.component_type,
                )
            )
        return result

    def parse_wiki_url(self, url: str) -> WikiPageRef:
        settings = self.settings
        if settings is None:
            from config.settings import OnesSettings
            settings = OnesSettings()
        base = urlsplit(settings.base_url.rstrip("/"))
        candidate = urlsplit(str(url).strip())
        if self._origin(candidate) != self._origin(base):
            raise ValueError("Wiki URL must use the configured ONES origin")

        base_path = base.path.rstrip("/")
        expected_path = f"{base_path}/wiki/"
        if candidate.path.rstrip("/") != expected_path.rstrip("/") or candidate.query:
            raise ValueError("Wiki URL must use the authorized ONES Wiki page path")
        fragment_path = candidate.fragment
        decoded = unquote(fragment_path)
        if decoded != fragment_path or ".." in decoded.split("/"):
            raise ValueError("Wiki URL contains an unsafe encoded or traversal path")
        match = re.fullmatch(r"/?team/([^/?#]+)/space/([^/?#]+)/page/([^/?#]+)", decoded)
        if match is None or any(not value.strip() for value in match.groups()):
            raise ValueError("Wiki URL is missing team, space, or page identity")
        team_id, space_id, page_id = match.groups()
        team_id = validate_wiki_segment(team_id, label="team")
        space_id = validate_wiki_segment(space_id, label="space")
        page_id = validate_wiki_segment(page_id, label="page")
        if team_id != settings.team_id:
            raise ValueError("Wiki URL team does not match the configured ONES team")
        canonical_origin = self._canonical_origin(base)
        source_url = (
            f"{canonical_origin}{base_path}/wiki/#/team/{quote_wiki_segment(team_id, label='team')}"
            f"/space/{quote_wiki_segment(space_id, label='space')}/page/{quote_wiki_segment(page_id, label='page')}"
        )
        return WikiPageRef(team_id=team_id, space_id=space_id, page_id=page_id, source_url=source_url)

    async def get_wiki_snapshot(self, url: str) -> WikiPageSnapshot:
        ref = self.parse_wiki_url(url)
        return await self.get_wiki_snapshot_by_ids(ref.space_id, ref.page_id, source_url=ref.source_url)

    async def get_wiki_snapshot_by_ids(
        self,
        space_id: str,
        page_id: str,
        *,
        source_url: str | None = None,
    ) -> WikiPageSnapshot:
        settings = self.settings
        if settings is None:
            from config.settings import OnesSettings
            settings = OnesSettings()
        validate_wiki_segment(settings.team_id, label="team")
        space_id = validate_wiki_segment(space_id, label="space")
        page_id = validate_wiki_segment(page_id, label="page")
        body = await self._call_wiki_async("fetch_wiki_page", space_id, page_id)
        detail = await self._call_wiki_async("fetch_wiki_page_info", page_id)
        return self._build_wiki_snapshot(space_id, page_id, body, detail, source_url=source_url)

    def get_wiki_snapshot_sync(self, url: str) -> WikiPageSnapshot:
        ref = self.parse_wiki_url(url)
        return self.get_wiki_snapshot_by_ids_sync(ref.space_id, ref.page_id, source_url=ref.source_url)

    def get_wiki_snapshot_by_ids_sync(
        self,
        space_id: str,
        page_id: str,
        *,
        source_url: str | None = None,
    ) -> WikiPageSnapshot:
        settings = self.settings
        if settings is None:
            from config.settings import OnesSettings
            settings = OnesSettings()
        validate_wiki_segment(settings.team_id, label="team")
        space_id = validate_wiki_segment(space_id, label="space")
        page_id = validate_wiki_segment(page_id, label="page")
        body = self._call_wiki_sync("fetch_wiki_page", space_id, page_id)
        detail = self._call_wiki_sync("fetch_wiki_page_info", page_id)
        return self._build_wiki_snapshot(space_id, page_id, body, detail, source_url=source_url)

    async def get_normalized_requirement(self, issue_id: str) -> RequirementRecord:
        detail = await self._call_async("fetch_issue_detail", issue_id)
        return self.normalize_requirement(self._resolve_issue_detail(detail, issue_id))

    def get_normalized_requirement_sync(self, issue_id: str) -> RequirementRecord:
        detail = self._call_sync("fetch_issue_detail", issue_id)
        return self.normalize_requirement(self._resolve_issue_detail(detail, issue_id))

    async def list_comments(self, item_id: str, *, page_size: int = 200) -> list[dict[str, str]]:
        payload = await self._call_async("list_comments", self._comment_item_id(item_id), page_size=page_size)
        return self._normalize_comments(payload)

    def list_comments_sync(self, item_id: str, *, page_size: int = 200) -> list[dict[str, str]]:
        payload = self._call_sync("list_comments", self._comment_item_id(item_id), page_size=page_size)
        return self._normalize_comments(payload)

    async def add_comment(self, item_id: str, text: str) -> dict[str, str]:
        item_id = self._comment_item_id(item_id)
        text = self._comment_text(text)
        payload = await self._call_async("add_comment", item_id, text)
        return self._normalize_created_comment(payload, text)

    def add_comment_sync(self, item_id: str, text: str) -> dict[str, str]:
        item_id = self._comment_item_id(item_id)
        text = self._comment_text(text)
        payload = self._call_sync("add_comment", item_id, text)
        return self._normalize_created_comment(payload, text)

    @classmethod
    def _comment_item_id(cls, value: str) -> str:
        if type(value) is not str or not value.strip():
            raise OnesGatewayPayloadError("ONES comment item id is invalid")
        value = cls._ensure_utf8(value.strip(), context="comment item id")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise OnesGatewayPayloadError("ONES comment item id is invalid")
        return value

    @classmethod
    def _comment_text(cls, value: str) -> str:
        if type(value) is not str or not value.strip():
            raise OnesGatewayPayloadError("ONES comment text is invalid")
        value = cls._ensure_utf8(value, context="comment text")
        if any(ord(character) < 32 and character not in "\n\t" for character in value):
            raise OnesGatewayPayloadError("ONES comment text is invalid")
        return value

    @classmethod
    def _normalize_comments(cls, payload: object) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            raise OnesGatewayPayloadError("Malformed ONES comments payload: expected list")
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in payload:
            if not isinstance(entry, dict):
                raise OnesGatewayPayloadError("Malformed ONES comment: expected mapping")
            identity = entry.get("id", entry.get("uuid"))
            text = entry.get("text", entry.get("message", entry.get("content")))
            if type(identity) is not str or not identity.strip() or type(text) is not str:
                raise OnesGatewayPayloadError("Malformed ONES comment fields")
            identity = cls._ensure_utf8(identity.strip(), context="comment id")
            text = cls._ensure_utf8(text, context="comment text")
            if any(ord(character) < 32 or ord(character) == 127 for character in identity):
                raise OnesGatewayPayloadError("Malformed ONES comment identity")
            if any(ord(character) < 32 and character not in "\n\t" for character in text):
                raise OnesGatewayPayloadError("Malformed ONES comment text")
            if identity in seen:
                continue
            seen.add(identity)
            result.append({"id": identity, "text": text})
        return result

    @classmethod
    def _normalize_created_comment(cls, payload: object, text: str) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise OnesGatewayPayloadError("Malformed ONES add comment payload")
        identity = payload.get("id", payload.get("uuid", payload.get("key")))
        if type(identity) is not str or not identity.strip():
            raise OnesGatewayPayloadError("Malformed ONES add comment identity")
        identity = cls._ensure_utf8(identity.strip(), context="comment id")
        if any(ord(character) < 32 or ord(character) == 127 for character in identity):
            raise OnesGatewayPayloadError("Malformed ONES add comment identity")
        returned_text = payload.get("text", payload.get("message", payload.get("content", text)))
        if type(returned_text) is not str or returned_text != text:
            raise OnesGatewayPayloadError("Malformed ONES add comment text")
        return {"id": identity, "text": text}

    async def list_defect_statuses(
        self,
        project_id: str,
        issue_type_id: str,
    ) -> list[WorkflowStatusRef]:
        configs = await self._call_async("fetch_task_status_configs", [project_id])
        definitions = await self._call_async("fetch_task_status_definitions")
        return self._build_workflow_statuses(configs, definitions, project_id, issue_type_id)

    async def list_open_defects(
        self,
        project_id: str,
        issue_type_id: str,
        sprint_id: str | None,
        assignee: str | None,
        *,
        limit: int = 5000,
        page_size: int = 200,
        status_ids: Sequence[str] | None = None,
    ) -> list[DefectRecord]:
        statuses = await self.list_defect_statuses(project_id, issue_type_id)
        if not statuses:
            raise OnesGatewayPayloadError("ONES defect workflow has no statuses")

        open_status_ids: list[str] = []
        for status in statuses:
            category = str(status.category or "").strip().lower()
            if not category:
                raise OnesGatewayPayloadError(f"ONES workflow status {status.id or '<empty>'} has no category")
            if category in _OPEN_STATUS_CATEGORIES:
                open_status_ids.append(status.id)
            elif category not in _CLOSED_STATUS_CATEGORIES:
                raise OnesGatewayPayloadError(
                    f"ONES workflow status {status.id or '<empty>'} has unknown category: {category}",
                )

        selected_status_ids = open_status_ids
        if status_ids is not None:
            requested = list(status_ids)
            if (
                not requested
                or len(requested) != len(set(requested))
                or any(
                    type(status_id) is not str
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", status_id) is None
                    for status_id in requested
                )
                or not set(requested).issubset(open_status_ids)
            ):
                raise OnesGatewayPayloadError(
                    "Requested ONES defect statuses are not configured open states"
                )
            selected_status_ids = requested

        if not selected_status_ids:
            return []

        return await self.list_normalized_defects(
            project_id=project_id,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            status_ids=selected_status_ids,
            limit=limit,
            page_size=page_size,
        )

    def list_defect_statuses_sync(
        self,
        project_id: str,
        issue_type_id: str,
    ) -> list[WorkflowStatusRef]:
        configs = self._call_sync("fetch_task_status_configs", [project_id])
        definitions = self._call_sync("fetch_task_status_definitions")
        return self._build_workflow_statuses(configs, definitions, project_id, issue_type_id)

    @classmethod
    def _build_workflow_statuses(
        cls,
        configs: list[dict],
        definitions: list[dict],
        project_id: str,
        issue_type_id: str,
    ) -> list[WorkflowStatusRef]:
        definitions_by_id = {
            str(item.get("uuid") or ""): item
            for item in definitions
            if isinstance(item, dict) and item.get("uuid")
        }
        selected = [
            item
            for item in configs
            if isinstance(item, dict)
            and item.get("project_uuid") == project_id
            and item.get("issue_type_uuid") == issue_type_id
        ]
        selected.sort(key=lambda item: int(item.get("position", 0)))
        return [cls._normalize_workflow_status(item, definitions_by_id) for item in selected]

    async def list_defects(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        status_ids: Iterable[str] | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            status_ids=status_ids,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        return await self._list_defects_async(query)

    async def list_requirements(
        self,
        *,
        project_id: str | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        status_ids: Iterable[str] | None = None,
        limit: int = 50,
        page_size: int | None = None,
    ) -> list[RequirementRecord]:
        """List normalized ONES requirements for a read-only TUI picker.

        The list endpoint returns compact task rows, while the requirement
        workflow needs the canonical detail shape. Re-read each bounded row
        through the same gateway before exposing it to callers.
        """

        if type(limit) is not int or limit <= 0 or limit > 100:
            raise ValueError("requirement limit is invalid")
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=None,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=None,
            status_ids=status_ids,
            current_user=None,
            mine=False,
            limit=limit,
            page_size=page_size,
        )
        rows = await self._list_defects_async(query)
        requirements: list[RequirementRecord] = []
        for row in rows[:limit]:
            identity = str(row.get("uuid") or row.get("key") or "").strip()
            if not identity:
                raise OnesGatewayPayloadError("requirement list item has no identity")
            detail = await self._call_async("fetch_issue_detail", identity)
            requirements.append(self.normalize_requirement(detail))
        return requirements

    async def list_current_user_defects(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        return await self.list_defects(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            current_user=True,
            limit=limit,
            page_size=page_size,
        )

    async def get_defect_detail(
        self,
        issue_id: str,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        status_ids: Iterable[str] | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> dict:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            status_ids=status_ids,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        if self._uses_scoped_fetch(query):
            defect = await self._find_defect_async(issue_id, query)
            if defect:
                matched = self._validate_issue_payload(defect, issue_id, context="scoped defect lookup")
                detail_id = str(matched.get("key") or matched["uuid"])
                detail = await self._call_async("fetch_issue_detail", detail_id)
                return self._resolve_issue_detail(detail, issue_id)

        detail = await self._call_async("fetch_issue_detail", issue_id)
        return self._resolve_issue_detail(detail, issue_id)

    async def list_project_refs(self, include_archived: bool = False) -> list[ProjectRef]:
        projects = await self.list_projects(include_archived=include_archived)
        return self.normalize_projects(projects)

    async def list_team_members(self, uuids: list[str] | None = None) -> list[dict]:
        return await self._call_async("fetch_team_members", uuids=uuids)

    async def get_current_user_id(self) -> str:
        payload = await self._call_async("fetch_current_user_identity")
        identity = payload.get("uuid") if isinstance(payload, dict) else None
        if type(identity) is not str or not identity.strip():
            raise OnesGatewayPayloadError("Malformed ONES current user identity")
        return self._required_scalar(identity, context="current user uuid")

    async def list_role_members(self, project_id: str) -> list[dict]:
        return await self._call_async("fetch_role_members", project_id)

    async def list_normalized_defects(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        status_ids: Iterable[str] | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[DefectRecord]:
        defects = await self.list_defects(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            status_ids=status_ids,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        return self.normalize_defects(defects)

    async def get_normalized_defect(self, issue_id: str, **kwargs) -> DefectRecord:
        defect = await self.get_defect_detail(issue_id, **kwargs)
        return self.normalize_defect(defect)

    async def list_iterations(self, project_id: str) -> list[dict]:
        return await self._call_async("fetch_sprints", project_id)

    def list_projects_sync(self, include_archived: bool = False) -> list[dict]:
        return self._call_sync("fetch_projects", include_archived=include_archived)

    def list_defects_sync(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        status_ids: Iterable[str] | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            status_ids=status_ids,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        return self._list_defects_sync_internal(query)

    def list_current_user_defects_sync(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        return self.list_defects_sync(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            current_user=True,
            limit=limit,
            page_size=page_size,
        )

    def get_defect_detail_sync(
        self,
        issue_id: str,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        status_ids: Iterable[str] | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> dict:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            status_ids=status_ids,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        if self._uses_scoped_fetch(query):
            defect = self._find_defect_sync(issue_id, query)
            if defect:
                matched = self._validate_issue_payload(defect, issue_id, context="scoped defect lookup")
                detail_id = str(matched.get("key") or matched["uuid"])
                detail = self._call_sync("fetch_issue_detail", detail_id)
                return self._resolve_issue_detail(detail, issue_id)

        detail = self._call_sync("fetch_issue_detail", issue_id)
        return self._resolve_issue_detail(detail, issue_id)

    def get_normalized_defect_sync(self, issue_id: str, **kwargs) -> DefectRecord:
        return self.normalize_defect(self.get_defect_detail_sync(issue_id, **kwargs))

    def list_project_refs_sync(self, include_archived: bool = False) -> list[ProjectRef]:
        projects = self.list_projects_sync(include_archived=include_archived)
        return self.normalize_projects(projects)

    def list_team_members_sync(self, uuids: list[str] | None = None) -> list[dict]:
        return self._call_sync("fetch_team_members", uuids=uuids)

    def list_role_members_sync(self, project_id: str) -> list[dict]:
        return self._call_sync("fetch_role_members", project_id)

    async def close(self) -> None:
        if self._managed_async_client is not None:
            await self._managed_async_client.close()
            self._managed_async_client = None
        if self._managed_sync_client is not None:
            self._managed_sync_client.close()
            self._managed_sync_client = None

    async def _list_defects_async(self, query: _DefectQuery) -> list[dict]:
        client = await self._get_async_client()
        return await self._collect_defects_async(client, query)

    async def _find_defect_async(self, issue_id: str, query: _DefectQuery) -> dict:
        defects = await self._list_defects_async(query)
        return self._select_issue(defects, issue_id)

    def _list_defects_sync_internal(self, query: _DefectQuery) -> list[dict]:
        client = self._get_sync_client()
        return self._collect_defects_sync(client, query)

    def _find_defect_sync(self, issue_id: str, query: _DefectQuery) -> dict:
        defects = self._list_defects_sync_internal(query)
        return self._select_issue(defects, issue_id)

    def normalize_defects(self, defects: list[dict]) -> list[DefectRecord]:
        return [self.normalize_defect(defect) for defect in defects]

    @staticmethod
    def _normalize_workflow_status(
        config: dict,
        definitions_by_id: dict[str, dict],
    ) -> WorkflowStatusRef:
        status_id = str(config.get("status_uuid") or "").strip()
        definition = definitions_by_id.get(status_id)
        if not status_id or definition is None:
            raise OnesGatewayPayloadError(
                f"Malformed ONES workflow status: missing definition for {status_id or '<empty>'}",
            )
        name = str(definition.get("name") or "").strip()
        category = str(definition.get("category") or "").strip()
        if not name or not category:
            raise OnesGatewayPayloadError(
                f"Malformed ONES workflow status definition: {status_id}",
            )
        return WorkflowStatusRef(
            id=status_id,
            name=name,
            category=category,
            position=int(config.get("position", 0)),
            default=bool(config.get("default", False)),
            built_in=bool(definition.get("built_in", False)),
            detail_type=str(definition.get("detail_type") or ""),
            name_pinyin=str(definition.get("name_pinyin") or ""),
        )

    def normalize_projects(self, projects: list[dict]) -> list[ProjectRef]:
        return [self.normalize_project(project) for project in projects]

    def normalize_project(self, project: dict) -> ProjectRef:
        project = self._require_mapping(project, context="project payload")
        project_id = self._require_field(project, "uuid", context="project payload")
        return ProjectRef(
            id=project_id,
            name=project.get("name", ""),
        )

    def normalize_defect(self, defect: dict) -> DefectRecord:
        defect = self._require_mapping(defect, context="defect payload")
        defect_id = self._require_field(defect, "uuid", context="defect payload")
        title = self._require_field(defect, "name", context=f"defect payload {defect_id}")
        project = self._require_nested_mapping(defect, "project", context=f"defect payload {defect_id}")
        status = self._require_nested_mapping(defect, "status", context=f"defect payload {defect_id}")
        issue_type = self._require_nested_mapping(defect, "issueType", context=f"defect payload {defect_id}")
        priority = self._require_nested_mapping(defect, "priority", context=f"defect payload {defect_id}")
        updated_at_raw = defect.get("serverUpdateStamp", "")
        updated_at = (
            self._normalize_timestamp(updated_at_raw, context="defect updated_at")
            if updated_at_raw not in (None, "")
            else ""
        )
        created_at_raw = defect.get("createTime", "")
        created_at = (
            self._normalize_timestamp(created_at_raw, context="defect created_at")
            if created_at_raw not in (None, "")
            else ""
        )

        return DefectRecord(
            defect_id=defect_id,
            title=title,
            number=str(defect.get("number", "") or ""),
            project=ProjectRef(
                id=project.get("uuid", ""),
                name=project.get("name", ""),
            ),
            status=StatusRef(
                id=status.get("uuid", ""),
                name=status.get("name", ""),
                category=status.get("category", ""),
            ),
            issue_type=IssueTypeRef(
                id=issue_type.get("uuid", ""),
                name=issue_type.get("name", ""),
            ),
            priority=PriorityRef(
                id=priority.get("uuid", ""),
                value=self._priority_value(priority),
                position=priority.get("position"),
            ),
            assignee=self._identity_ref(defect.get("assign")),
            owner=self._identity_ref(defect.get("owner")),
            parent=self._identity_ref(defect.get("parent")),
            path=defect.get("path", ""),
            description=defect.get("description", ""),
            deadline=self._optional_scalar(
                defect.get("deadline"), context="defect deadline"
            ),
            created_at=created_at,
            updated_at=updated_at,
            source="ones",
            raw=defect,
        )

    def normalize_requirement(self, detail: dict) -> RequirementRecord:
        detail = self._require_mapping(detail, context="requirement payload")
        requirement_id = self._required_scalar(detail.get("uuid"), context="requirement uuid")
        title = self._first_string_value(detail, keys=("name", "title"), context="requirement title")
        project = self._require_nested_mapping(detail, "project", context=f"requirement payload {requirement_id}")
        iteration = self._optional_mapping(detail, "sprint", context=f"requirement payload {requirement_id}")
        assignee = self._optional_mapping(detail, "assign", context=f"requirement payload {requirement_id}")
        status = self._require_nested_mapping(detail, "status", context=f"requirement payload {requirement_id}")
        issue_type = self._optional_mapping(
            detail, "issueType", context=f"requirement payload {requirement_id}"
        )
        project_id = self._required_scalar(project.get("uuid"), context="requirement project uuid")
        status_id = self._required_scalar(status.get("uuid"), context="requirement status uuid")
        issue_type_id = ""
        issue_type_built_in = False
        issue_type_detail_type = ""
        if detail.get("issueType") is not None:
            issue_type_id = self._required_scalar(
                issue_type.get("uuid"), context="requirement issue type uuid"
            )
            raw_built_in = issue_type.get("builtIn", False)
            raw_detail_type = issue_type.get("detailType", "")
            if raw_detail_type is None:
                raw_detail_type = ""
            if type(raw_built_in) is not bool or (
                not isinstance(raw_detail_type, (str, int))
                or isinstance(raw_detail_type, bool)
            ):
                raise OnesGatewayPayloadError(
                    "Malformed ONES requirement payload: issue type metadata"
                )
            issue_type_built_in = raw_built_in
            issue_type_detail_type = str(raw_detail_type).strip()
        iteration_id = ""
        if detail.get("sprint") is not None:
            iteration_id = self._required_scalar(iteration.get("uuid"), context="requirement sprint uuid")
        assignee_id = ""
        if detail.get("assign") is not None:
            assignee_id = self._required_scalar(assignee.get("uuid"), context="requirement assignee uuid")
        number = self._optional_scalar(detail.get("number"), context="requirement number")
        description = self._normalize_requirement_description(detail.get("description"))
        related = detail.get("relatedWikiPages")
        if related is None:
            related = []
        if not isinstance(related, list):
            raise OnesGatewayPayloadError("Malformed ONES requirement payload: relatedWikiPages must be a list")
        if any(not isinstance(item, dict) for item in related):
            raise OnesGatewayPayloadError("Malformed ONES requirement payload: relatedWikiPages entries must be mappings")
        related_by_page: dict[str, dict] = {}
        for item in related:
            raw_page_id = item.get("uuid") if item.get("uuid") is not None else item.get("page_id")
            page_identity = self._optional_scalar(raw_page_id, context="related Wiki page uuid")
            if page_identity:
                related_by_page[page_identity] = item
        wiki_refs: list[WikiPageRef] = []
        seen: set[tuple[str, str]] = set()
        for raw_url in re.findall(r"https?://[^\s<>'\"]+", description):
            try:
                ref = self.parse_wiki_url(raw_url.rstrip(".,;:)"))
            except ValueError:
                continue
            key = (ref.space_id, ref.page_id)
            if key in seen:
                continue
            seen.add(key)
            related_item = related_by_page.get(ref.page_id, {})
            ref.title = self._optional_display_value(related_item, keys=("title", "name"), context="related Wiki title")
            wiki_refs.append(ref)
        for item in related:
            if not isinstance(item, dict):
                continue
            raw_urls = [self._optional_string(item.get(field_name), context=f"related Wiki {field_name}") for field_name in ("url", "source_url", "link", "web_url")]
            for raw_url in raw_urls:
                if not raw_url:
                    continue
                try:
                    ref = self.parse_wiki_url(raw_url)
                except ValueError:
                    continue
                key = (ref.space_id, ref.page_id)
                if key in seen:
                    continue
                seen.add(key)
                ref.title = self._optional_display_value(item, keys=("title", "name"), context="related Wiki title")
                wiki_refs.append(ref)

        return RequirementRecord(
            requirement_id=requirement_id,
            number=number,
            title=title,
            project=ProjectRef(id=project_id, name=self._optional_display_value(project, keys=("name",), context="requirement project name")),
            iteration=ProjectRef(id=iteration_id, name=self._optional_display_value(iteration, keys=("name",), context="requirement sprint name")),
            assignee=(
                IdentityRef(
                    id=assignee_id,
                    name=self._optional_display_value(assignee, keys=("name",), context="requirement assignee name"),
                    avatar=self._optional_display_value(assignee, keys=("avatar",), context="requirement assignee avatar"),
                )
                if assignee_id
                else None
            ),
            status=StatusRef(
                id=status_id,
                name=self._optional_display_value(status, keys=("name",), context="requirement status name"),
                category=self._optional_display_value(status, keys=("category",), context="requirement status category"),
            ),
            issue_type=IssueTypeRef(
                id=issue_type_id,
                name=self._optional_display_value(
                    issue_type,
                    keys=("name",),
                    context="requirement issue type name",
                ),
                built_in=issue_type_built_in,
                detail_type=issue_type_detail_type,
            ),
            description=description,
            wiki_refs=wiki_refs,
            source="ones",
        )

    async def _call_wiki_async(self, method_name: str, *args) -> dict:
        init_error: OnesGatewayError | None = None
        try:
            client = await self._get_async_client()
        except Exception as exc:
            init_error = self._map_wiki_exception(exc, context=f"async ONES {method_name}")
        if init_error is not None:
            raise init_error from None
        method = getattr(client, method_name)
        for attempt in range(1, 4):
            safe_error: OnesGatewayError | None = None
            retryable = False
            try:
                payload = await method(*args)
                return self._require_mapping(payload, context=method_name)
            except Exception as exc:
                safe_error = self._map_wiki_exception(exc, context=f"async ONES {method_name}")
                retryable = self._is_retryable(exc)
            if attempt == 3 or not retryable:
                raise safe_error from None
            if safe_error is not None:
                delay = max(0.0, float(self.retry_backoff(attempt)))
                if delay:
                    await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _call_wiki_sync(self, method_name: str, *args) -> dict:
        init_error: OnesGatewayError | None = None
        try:
            client = self._get_sync_client()
        except Exception as exc:
            init_error = self._map_wiki_exception(exc, context=f"sync ONES {method_name}")
        if init_error is not None:
            raise init_error from None
        method = getattr(client, method_name)
        for attempt in range(1, 4):
            safe_error: OnesGatewayError | None = None
            retryable = False
            try:
                payload = method(*args)
                return self._require_mapping(payload, context=method_name)
            except Exception as exc:
                safe_error = self._map_wiki_exception(exc, context=f"sync ONES {method_name}")
                retryable = self._is_retryable(exc)
            if attempt == 3 or not retryable:
                raise safe_error from None
            if safe_error is not None:
                delay = max(0.0, float(self.retry_backoff(attempt)))
                if delay:
                    time.sleep(delay)
        raise AssertionError("unreachable")

    def _build_wiki_snapshot(
        self,
        space_id: str,
        page_id: str,
        body: dict,
        detail: dict,
        *,
        source_url: str | None,
    ) -> WikiPageSnapshot:
        body = self._require_mapping(body, context="Wiki page body")
        detail = self._require_mapping(detail, context="Wiki page detail")
        nested_page = self._envelope_mapping(body, "page", context="Wiki page body")
        nested_data = self._envelope_mapping(body, "data", context="Wiki page body")
        detail_page = self._envelope_mapping(detail, "page", context="Wiki page detail")
        detail_data = self._envelope_mapping(detail, "data", context="Wiki page detail")
        content = self._first_present(body, nested_page, nested_data, key="content")
        if content is None:
            raise OnesGatewayPayloadError("Malformed ONES Wiki page body: missing content")
        normalized = self._normalize_wiki_content(content)
        if not normalized:
            raise OnesGatewayPayloadError("Malformed ONES Wiki page body: empty content")
        settings = self.settings
        if settings is None:
            from config.settings import OnesSettings
            settings = OnesSettings()
        if source_url is None:
            base = urlsplit(settings.base_url.rstrip("/"))
            source_url = (
                f"{self._canonical_origin(base)}{base.path.rstrip('/')}/wiki/#/team/{settings.team_id}"
                f"/space/{space_id}/page/{page_id}"
            )
        metadata_payloads = (detail, detail_page, detail_data, nested_page, nested_data, body)
        title = self._first_string_value(*metadata_payloads, keys=("title", "name"), context="Wiki title")
        version = self._first_scalar_value(
            *metadata_payloads, keys=("version", "revision"), context="Wiki version",
        )
        updated_at_raw = self._first_raw_value(
            *metadata_payloads,
            keys=("updated_at", "updateTime", "serverUpdateStamp", "CreatedTime"),
            context="Wiki updated_at",
        )
        updated_at = self._normalize_wiki_timestamp(updated_at_raw)
        return WikiPageSnapshot(
            team_id=settings.team_id,
            space_id=space_id,
            page_id=page_id,
            title=title,
            version=version,
            updated_at=updated_at,
            normalized_content=normalized,
            content_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            source_url=source_url,
        )

    @staticmethod
    def _normalize_wiki_content(content: object) -> str:
        if isinstance(content, str):
            lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            normalized = "\n".join(line.rstrip() for line in lines).rstrip("\n")
            result = f"{normalized}\n" if normalized else ""
            return OnesGateway._ensure_utf8(result, context="Wiki content")
        if not isinstance(content, (dict, list)):
            raise OnesGatewayPayloadError("Malformed ONES Wiki content: expected text or structured JSON")
        serialization_failed = False
        try:
            normalized = json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            serialization_failed = True
        if serialization_failed:
            raise OnesGatewayPayloadError("Malformed ONES Wiki content: not JSON serializable") from None
        return OnesGateway._ensure_utf8(normalized, context="Wiki content")

    @staticmethod
    def _optional_mapping(payload: dict, key: str, *, context: str) -> dict:
        value = payload.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: invalid '{key}'")
        return value

    @staticmethod
    def _envelope_mapping(payload: dict, key: str, *, context: str) -> dict:
        if key not in payload:
            return {}
        value = payload[key]
        if not isinstance(value, dict):
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: invalid '{key}' envelope")
        return value

    @staticmethod
    def _first_present(*payloads: dict, key: str) -> object | None:
        for payload in payloads:
            if key in payload:
                return payload[key]
        return None

    @staticmethod
    def _first_string_value(*payloads: dict, keys: tuple[str, ...], context: str) -> str:
        for payload in payloads:
            for key in keys:
                if key not in payload or payload[key] is None:
                    continue
                value = payload[key]
                if not isinstance(value, str):
                    raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: expected string")
                normalized = value.strip()
                if normalized:
                    return OnesGateway._ensure_utf8(normalized, context=context)
        raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: missing non-empty string")

    @staticmethod
    def _first_scalar_value(*payloads: dict, keys: tuple[str, ...], context: str) -> str:
        for payload in payloads:
            for key in keys:
                if key not in payload or payload[key] is None:
                    continue
                value = OnesGateway._required_scalar(payload[key], context=context)
                if value:
                    return value
        raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: missing non-empty scalar")

    @staticmethod
    def _first_raw_value(*payloads: dict, keys: tuple[str, ...], context: str) -> object:
        for payload in payloads:
            for key in keys:
                if key in payload and payload[key] is not None:
                    return payload[key]
        raise OnesGatewayPayloadError(
            f"Malformed ONES payload for {context}: missing non-empty scalar"
        )

    @staticmethod
    def _normalize_wiki_timestamp(value: object) -> str:
        return OnesGateway._normalize_timestamp(value, context="Wiki updated_at")

    @staticmethod
    def _normalize_timestamp(value: object, *, context: str) -> str:
        lower = datetime(2000, 1, 1, tzinfo=UTC)
        upper = datetime(3000, 1, 1, tzinfo=UTC)
        parsed: datetime
        if type(value) is int:
            if value >= 100_000_000_000_000:
                seconds = value / 1_000_000
            elif value >= 100_000_000_000:
                seconds = value / 1_000
            else:
                seconds = value
            try:
                parsed = datetime.fromtimestamp(seconds, tz=UTC)
            except (OverflowError, OSError, ValueError):
                raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}") from None
        elif type(value) is str and value and value == value.strip():
            candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}") from None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}")
            parsed = parsed.astimezone(UTC)
        else:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}")
        if parsed < lower or parsed >= upper:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}")
        return parsed.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _required_scalar(value: object, *, context: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: expected scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: expected finite scalar")
        normalized = str(value).strip()
        if not normalized:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: empty scalar")
        return OnesGateway._ensure_utf8(normalized, context=context)

    @staticmethod
    def _optional_scalar(value: object, *, context: str) -> str:
        if value is None:
            return ""
        return OnesGateway._required_scalar(value, context=context)

    @staticmethod
    def _optional_string(value: object, *, context: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: expected string")
        return OnesGateway._ensure_utf8(value.strip(), context=context)

    @staticmethod
    def _optional_display_value(payload: dict, *, keys: tuple[str, ...], context: str) -> str:
        for key in keys:
            if key in payload and payload[key] is not None:
                return OnesGateway._optional_scalar(payload[key], context=context)
        return ""

    @staticmethod
    def _normalize_requirement_description(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            normalized = "\n".join(line.rstrip() for line in lines).rstrip("\n")
            return OnesGateway._ensure_utf8(normalized, context="requirement description")
        if not isinstance(value, (dict, list)):
            raise OnesGatewayPayloadError("Malformed ONES requirement description: expected text or structured JSON")
        serialization_failed = False
        try:
            normalized = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            serialization_failed = True
        if serialization_failed:
            raise OnesGatewayPayloadError("Malformed ONES requirement description: not JSON serializable") from None
        return OnesGateway._ensure_utf8(normalized, context="requirement description")

    @staticmethod
    def _ensure_utf8(value: str, *, context: str) -> str:
        encoding_failed = False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            encoding_failed = True
        if encoding_failed:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: invalid UTF-8 text") from None
        return value

    async def _call_async(self, method_name: str, *args, **kwargs):
        try:
            client = await self._get_async_client()
            method = getattr(client, method_name)
            return await method(*args, **kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context=f"async ONES {method_name}") from exc

    def _call_sync(self, method_name: str, *args, **kwargs):
        try:
            client = self._get_sync_client()
            method = getattr(client, method_name)
            return method(*args, **kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context=f"sync ONES {method_name}") from exc

    async def _collect_defects_async(self, client, query: _DefectQuery) -> list[dict]:
        if query.limit <= 0:
            return []

        project_ids = query.project_ids or (None,)
        defects: list[dict] = []
        seen_issue_ids: set[str] = set()
        remaining = query.limit

        for project_id in project_ids:
            fetched = await self._fetch_defects_async(
                client,
                query,
                project_id=project_id,
                limit=remaining,
            )
            self._extend_unique_defects(defects, fetched, seen_issue_ids)
            remaining = query.limit - len(defects)
            if remaining <= 0:
                break

        return defects[:query.limit]

    def _collect_defects_sync(self, client, query: _DefectQuery) -> list[dict]:
        if query.limit <= 0:
            return []

        project_ids = query.project_ids or (None,)
        defects: list[dict] = []
        seen_issue_ids: set[str] = set()
        remaining = query.limit

        for project_id in project_ids:
            fetched = self._fetch_defects_sync(
                client,
                query,
                project_id=project_id,
                limit=remaining,
            )
            self._extend_unique_defects(defects, fetched, seen_issue_ids)
            remaining = query.limit - len(defects)
            if remaining <= 0:
                break

        return defects[:query.limit]

    async def _fetch_defects_async(
        self,
        client,
        query: _DefectQuery,
        *,
        project_id: str | None,
        limit: int,
    ) -> list[dict]:
        kwargs = {
            "project_id": project_id,
            "issue_type_id": query.issue_type_id,
            "limit": limit,
            "page_size": query.page_size,
        }
        if query.sprint_id:
            kwargs["sprint_id"] = query.sprint_id
        if query.mine:
            try:
                return await client.fetch_my_defects(**kwargs)
            except Exception as exc:
                raise self._map_exception(exc, context="async ONES fetch_my_defects") from exc

        if query.assignee:
            kwargs["assign"] = query.assignee
        if query.status_ids:
            kwargs["status_in"] = list(query.status_ids)
        try:
            return await client.fetch_defects(**kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context="async ONES fetch_defects") from exc

    def _fetch_defects_sync(
        self,
        client,
        query: _DefectQuery,
        *,
        project_id: str | None,
        limit: int,
    ) -> list[dict]:
        kwargs = {
            "project_id": project_id,
            "issue_type_id": query.issue_type_id,
            "limit": limit,
            "page_size": query.page_size,
        }
        if query.sprint_id:
            kwargs["sprint_id"] = query.sprint_id
        if query.mine:
            try:
                return client.fetch_my_defects(**kwargs)
            except Exception as exc:
                raise self._map_exception(exc, context="sync ONES fetch_my_defects") from exc

        if query.assignee:
            kwargs["assign"] = query.assignee
        if query.status_ids:
            kwargs["status_in"] = list(query.status_ids)
        try:
            return client.fetch_defects(**kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context="sync ONES fetch_defects") from exc

    async def _get_async_client(self) -> OnesAsyncClient:
        if self.async_client is not None:
            return self.async_client

        try:
            log.info(
                "ones_gateway_async_client_init",
                has_settings=bool(self.settings),
                managed_client_cached=self._managed_async_client is not None,
            )
            from config.settings import OnesSettings
            from src.integrations.ones_api import OnesAsyncClient

            if self._managed_async_client is None:
                managed_settings = self.settings or OnesSettings()
                self._managed_async_client = OnesAsyncClient(managed_settings)
                await self._managed_async_client._get_client()
            return self._managed_async_client
        except Exception as exc:
            log.error(
                "ones_gateway_async_client_init_failed",
                context="async ONES client initialization",
            )
            raise self._map_exception(exc, context="async ONES client initialization") from exc

    def _get_sync_client(self) -> OnesClient:
        if self.sync_client is not None:
            return self.sync_client

        try:
            log.info(
                "ones_gateway_sync_client_init",
                managed_client_cached=self._managed_sync_client is not None,
            )
            from src.integrations.ones import OnesClient

            if self._managed_sync_client is None:
                from config.settings import OnesSettings
                managed_settings = self.settings or OnesSettings()
                self._managed_sync_client = OnesClient(
                    base_url=managed_settings.base_url,
                    email=managed_settings.email,
                    password=managed_settings.password,
                    team_id=managed_settings.team_id,
                    project_id=managed_settings.project_id,
                    issue_type_id=managed_settings.issue_type_id,
                    comment_list_path_template=managed_settings.comment_list_path_template,
                    comment_timeout_seconds=managed_settings.comment_timeout_seconds,
                    comment_max_pages=managed_settings.comment_max_pages,
                    comment_max_comments=managed_settings.comment_max_comments,
                    comment_max_payload_bytes=managed_settings.comment_max_payload_bytes,
                )
            return self._managed_sync_client
        except Exception as exc:
            log.error(
                "ones_gateway_sync_client_init_failed",
                context="sync ONES client initialization",
            )
            raise self._map_exception(exc, context="sync ONES client initialization") from exc

    @staticmethod
    def _build_defect_query(
        *,
        project_id: str | None,
        project_ids: Iterable[str] | None,
        issue_type_id: str | None,
        sprint_id: str | None,
        assignee: str | None,
        assign: str | None,
        status_ids: Iterable[str] | None,
        current_user: bool | None,
        mine: bool,
        limit: int,
        page_size: int | None,
    ) -> _DefectQuery:
        normalized_project_ids = OnesGateway._normalize_project_ids(project_id, project_ids)
        normalized_assignee = assignee if assignee is not None else assign
        normalized_status_ids = tuple(str(value).strip() for value in (status_ids or []) if str(value).strip())
        use_current_user = bool(current_user) or mine or normalized_assignee == _CURRENT_USER_TOKEN
        if use_current_user:
            normalized_assignee = None

        normalized_limit = OnesGateway._normalize_limit(limit)
        normalized_page_size = OnesGateway._normalize_limit(
            page_size if page_size is not None else normalized_limit,
        )
        if normalized_page_size <= 0 and normalized_limit > 0:
            normalized_page_size = normalized_limit

        return _DefectQuery(
            project_ids=normalized_project_ids,
            issue_type_id=issue_type_id or None,
            sprint_id=sprint_id or None,
            assignee=normalized_assignee or None,
            status_ids=normalized_status_ids,
            mine=use_current_user,
            limit=normalized_limit,
            page_size=normalized_page_size,
        )

    @staticmethod
    def _normalize_project_ids(project_id: str | None, project_ids: Iterable[str] | None) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()

        for raw_value in (project_id, *(project_ids or [])):
            value = (raw_value or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)

        return tuple(normalized)

    @staticmethod
    def _normalize_limit(value: int | None) -> int:
        if value is None:
            return _DEFAULT_DEFECT_LIMIT
        if value <= 0:
            return 0
        return value

    @staticmethod
    def _uses_scoped_fetch(query: _DefectQuery) -> bool:
        return bool(
            query.mine
            or query.project_ids
            or query.issue_type_id
            or query.sprint_id
            or query.assignee
        )

    @staticmethod
    def _extend_unique_defects(defects: list[dict], fetched: list[dict], seen_issue_ids: set[str]) -> None:
        for defect in fetched:
            issue_id = defect.get("uuid")
            if issue_id and issue_id in seen_issue_ids:
                continue
            if issue_id:
                seen_issue_ids.add(issue_id)
            defects.append(defect)

    @staticmethod
    def _select_issue(defects: list[dict], issue_id: str) -> dict:
        for defect in defects:
            if not isinstance(defect, dict):
                raise OnesGatewayPayloadError(
                    f"Malformed ONES payload in defect lookup: expected mapping entries while resolving {issue_id}",
                )
            if defect.get("uuid") is None:
                raise OnesGatewayPayloadError(
                    f"Malformed ONES payload in defect lookup: missing 'uuid' while resolving {issue_id}",
                )
            if OnesGateway._matches_issue(defect, issue_id):
                return defect

        return {}

    @staticmethod
    def _resolve_issue_detail(detail: dict, issue_id: str) -> dict:
        if not detail:
            raise OnesGatewayNotFoundError(f"ONES issue not found: {issue_id}")

        return OnesGateway._validate_issue_payload(detail, issue_id, context="issue detail")

    @staticmethod
    def _validate_issue_payload(defect: dict, issue_id: str, *, context: str) -> dict:
        defect = OnesGateway._require_mapping(defect, context=context)
        OnesGateway._require_field(defect, "uuid", context=context)
        if not OnesGateway._matches_issue(defect, issue_id):
            raise OnesGatewayNotFoundError(f"ONES issue not found: {issue_id}")
        return defect

    @staticmethod
    def _matches_issue(defect: dict, issue_id: str) -> bool:
        identities = {
            str(defect.get("uuid") or "").strip(),
            str(defect.get("key") or "").strip(),
        }
        identities.discard("")
        return issue_id in identities

    @staticmethod
    def _require_mapping(payload: object, *, context: str) -> dict:
        if not isinstance(payload, dict):
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: expected mapping")
        return payload

    @staticmethod
    def _require_nested_mapping(payload: dict, field_name: str, *, context: str) -> dict:
        nested = payload.get(field_name)
        if not isinstance(nested, dict):
            raise OnesGatewayPayloadError(
                f"Malformed ONES payload for {context}: missing or invalid '{field_name}'",
            )
        return nested

    @staticmethod
    def _require_field(payload: dict, field_name: str, *, context: str) -> str:
        value = payload.get(field_name)
        if value is None:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: missing '{field_name}'")
        normalized = str(value).strip()
        if not normalized:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: empty '{field_name}'")
        return normalized

    @staticmethod
    def _map_exception(exc: Exception, *, context: str) -> OnesGatewayError:
        if isinstance(exc, OnesGatewayError):
            return exc

        from src.integrations.ones import OnesPaginationError as SyncOnesPaginationError
        from src.integrations.ones_api import OnesPaginationError as AsyncOnesPaginationError

        if isinstance(exc, (SyncOnesPaginationError, AsyncOnesPaginationError)):
            return OnesGatewayPayloadError(f"Malformed ONES pagination during {context}")

        if OnesGateway._is_timeout_error(exc):
            return OnesGatewayTimeoutError(f"ONES upstream timeout during {context}")

        if OnesGateway._is_auth_error(exc):
            return OnesGatewayAuthError(f"ONES authentication failed during {context}")

        if OnesGateway._status_code(exc) == 404:
            return OnesGatewayNotFoundError(f"ONES entity not found during {context}")

        if isinstance(exc, (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError)):
            return OnesGatewayPayloadError(f"Malformed ONES payload during {context}")

        if exc.__class__.__name__ == "OnesPayloadError":
            return OnesGatewayPayloadError(f"Malformed ONES payload during {context}")

        return OnesGatewayError(f"ONES gateway request failed during {context}")

    @staticmethod
    def _map_wiki_exception(exc: Exception, *, context: str) -> OnesGatewayError:
        mapped = OnesGateway._map_exception(exc, context=context)
        error_type = type(mapped)
        if isinstance(mapped, OnesGatewayAuthError):
            message = f"ONES authentication failed during {context}"
        elif isinstance(mapped, OnesGatewayTimeoutError):
            message = f"ONES upstream timeout during {context}"
        elif isinstance(mapped, OnesGatewayNotFoundError):
            message = f"ONES entity not found during {context}"
        elif isinstance(mapped, OnesGatewayPayloadError):
            message = f"Malformed ONES payload during {context}"
        else:
            error_type = OnesGatewayError
            message = f"ONES gateway request failed during {context}"
        return error_type(message)

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        return isinstance(exc, (TimeoutError, httpx.TimeoutException, requests.exceptions.Timeout))

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        return OnesGateway._status_code(exc) in {401, 403}

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        return status_code

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        status_code = OnesGateway._status_code(exc)
        if status_code == 429 or (status_code is not None and 500 <= status_code < 600):
            return True
        if isinstance(exc, (httpx.TransportError, requests.exceptions.ConnectionError)):
            return True
        if OnesGateway._is_timeout_error(exc):
            return False
        return False

    @staticmethod
    def _origin(parsed) -> tuple[str, str, int | None]:
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else None
        return scheme, hostname, port

    @staticmethod
    def _canonical_origin(parsed) -> str:
        scheme, hostname, port = OnesGateway._origin(parsed)
        default = 443 if scheme == "https" else 80 if scheme == "http" else None
        suffix = "" if port == default else f":{port}"
        return f"{scheme}://{hostname}{suffix}"

    @staticmethod
    def _identity_ref(payload: dict | None) -> IdentityRef | None:
        if not payload:
            return None

        def optional_identity_value(value: object) -> str:
            if value is None or isinstance(value, bool):
                return ""
            if isinstance(value, (str, int, float)):
                return str(value)
            return ""

        return IdentityRef(
            id=optional_identity_value(payload.get("uuid")),
            name=optional_identity_value(payload.get("name")),
            avatar=optional_identity_value(payload.get("avatar")),
        )

    @staticmethod
    def _priority_value(priority: dict) -> str:
        return str(priority.get("value", "") or priority.get("name", ""))


__all__ = [
    "OnesGateway",
    "OnesGatewayAuthError",
    "OnesGatewayError",
    "OnesGatewayNotFoundError",
    "OnesGatewayPayloadError",
    "OnesGatewayTimeoutError",
]
