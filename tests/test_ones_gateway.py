from __future__ import annotations

import unittest
from unittest.mock import patch
import httpx
import requests
from structlog.testing import capture_logs

from config.settings import OnesSettings
from src.contracts import IdentityRef, WorkflowStatusRef
from src.integrations.ones import (
    GQL_FETCH_ISSUE_TYPES,
    OnesClient,
    OnesPaginationError as SyncOnesPaginationError,
)
from src.integrations.ones_api import (
    OnesAsyncClient,
    OnesPaginationError as AsyncOnesPaginationError,
)
from src.services.ones_gateway import (
    OnesGateway,
    OnesGatewayAuthError,
    OnesGatewayError,
    OnesGatewayNotFoundError,
    OnesGatewayPayloadError,
    OnesGatewayTimeoutError,
)


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeHTTPStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.response = FakeResponse(status_code)


class TestIssueTypeWebApiContract(unittest.IsolatedAsyncioTestCase):
    def test_sync_issue_types_use_the_existing_web_graphql_query(self):
        client = object.__new__(OnesClient)
        client._graphql = unittest.mock.MagicMock(
            return_value={"issueTypes": [{"uuid": "requirement-type"}]}
        )

        rows = client.fetch_issue_types()

        self.assertEqual(rows, [{"uuid": "requirement-type"}])
        client._graphql.assert_called_once_with(
            GQL_FETCH_ISSUE_TYPES, {}, t="issueTypes"
        )

    async def test_async_issue_types_use_the_existing_web_graphql_query(self):
        client = object.__new__(OnesAsyncClient)
        client._graphql = unittest.mock.AsyncMock(
            return_value={"issueTypes": [{"uuid": "requirement-type"}]}
        )

        rows = await client.fetch_issue_types()

        self.assertEqual(rows, [{"uuid": "requirement-type"}])
        client._graphql.assert_awaited_once_with(
            GQL_FETCH_ISSUE_TYPES, {}, t="issueTypes"
        )


class FakeAsyncClient:
    def __init__(self, mine_results: dict[str | None, list[dict]] | None = None, defects_results: dict[str | None, list[dict]] | None = None):
        self.mine_results = mine_results or {}
        self.defects_results = defects_results or {}
        self.calls: list[tuple[str, dict]] = []
        self.detail_calls: list[str] = []

    async def fetch_my_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_my_defects", kwargs))
        return list(self.mine_results.get(kwargs.get("project_id"), []))

    async def fetch_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_defects", kwargs))
        return list(self.defects_results.get(kwargs.get("project_id"), []))

    async def fetch_issue_detail(self, issue_id: str) -> dict:
        self.detail_calls.append(issue_id)
        return {"uuid": issue_id, "name": "fallback"}

    async def fetch_issue_types(self) -> list[dict]:
        return [
            {
                "id": "requirement-type",
                "name": "需求",
                "buildIn": True,
                "type": " Requirement ",
            },
            {
                "id": "task-type",
                "name": "任务",
                "buildIn": True,
                "type": "task",
            },
        ]

    async def fetch_issue_type_configs(self, project_id: str) -> list[dict]:
        return [
            {
                "project_uuid": project_id,
                "issue_type_uuid": "requirement-type",
                "name": "产品需求",
            },
            {
                "scope": project_id,
                "issue_type_uuid": "task-type",
                "name": "任务",
            },
        ]


class FakeSyncClient:
    def __init__(self, mine_results: dict[str | None, list[dict]] | None = None, defects_results: dict[str | None, list[dict]] | None = None):
        self.mine_results = mine_results or {}
        self.defects_results = defects_results or {}
        self.calls: list[tuple[str, dict]] = []
        self.detail_calls: list[str] = []

    def fetch_my_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_my_defects", kwargs))
        return list(self.mine_results.get(kwargs.get("project_id"), []))

    def fetch_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_defects", kwargs))
        return list(self.defects_results.get(kwargs.get("project_id"), []))

    def fetch_issue_detail(self, issue_id: str) -> dict:
        self.detail_calls.append(issue_id)
        return {"uuid": issue_id, "name": "fallback"}

    def fetch_issue_types(self) -> list[dict]:
        return [
            {"id": "requirement-type", "name": "需求", "buildIn": True, "type": "requirement"},
            {"id": "task-type", "name": "任务", "buildIn": True, "type": "task"},
        ]

    def fetch_issue_type_configs(self, project_id: str) -> list[dict]:
        return [
            {"project_uuid": project_id, "issue_type_uuid": "requirement-type"},
            {"project_uuid": project_id, "issue_type_uuid": "task-type"},
        ]


class RaisingAsyncClient(FakeAsyncClient):
    def __init__(self, *, exception: Exception):
        super().__init__()
        self.exception = exception

    async def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        raise self.exception

    async def fetch_defects(self, **kwargs) -> list[dict]:
        raise self.exception

    async def fetch_issue_detail(self, issue_id: str) -> dict:
        self.detail_calls.append(issue_id)
        raise self.exception


class RaisingSyncClient(FakeSyncClient):
    def __init__(self, *, exception: Exception):
        super().__init__()
        self.exception = exception

    def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        raise self.exception

    def fetch_defects(self, **kwargs) -> list[dict]:
        raise self.exception


class TestOnesGatewayAsync(unittest.IsolatedAsyncioTestCase):
    async def test_list_project_issue_types_uses_project_configs_and_names(self):
        gateway = OnesGateway(async_client=FakeAsyncClient())

        issue_types = await gateway.list_project_issue_types("project-1")

        self.assertEqual(
            [(item.id, item.name) for item in issue_types],
            [("requirement-type", "产品需求"), ("task-type", "任务")],
        )
        self.assertEqual(
            [(item.built_in, item.detail_type, item.component_type) for item in issue_types],
            [(True, "", "requirement"), (True, "", "task")],
        )

    async def test_project_issue_types_reject_missing_authoritative_component_type(self):
        client = FakeAsyncClient()
        client.fetch_issue_types = unittest.mock.AsyncMock(
            return_value=[{"id": "requirement-type", "name": "需求", "buildIn": True}]
        )
        gateway = OnesGateway(async_client=client)

        with self.assertRaises(OnesGatewayPayloadError):
            await gateway.list_project_issue_types("project-1")

    async def test_project_issue_types_accept_legacy_numeric_detail_type(self):
        client = FakeAsyncClient()
        client.fetch_issue_types = unittest.mock.AsyncMock(
            return_value=[
                {
                    "uuid": "requirement-type",
                    "name": "需求",
                    "builtIn": True,
                    "detailType": 1,
                },
                {
                    "uuid": "task-type",
                    "name": "任务",
                    "builtIn": True,
                    "detailType": 2,
                },
            ]
        )
        gateway = OnesGateway(async_client=client)

        issue_types = await gateway.list_project_issue_types("project-1")

        self.assertEqual(
            [(item.id, item.detail_type, item.component_type) for item in issue_types],
            [
                ("requirement-type", "1", "requirement"),
                ("task-type", "2", "task"),
            ],
        )

    async def test_list_defects_maps_async_pagination_failure_to_safe_payload_error(self):
        class RecordingPaginationClient(FakeAsyncClient):
            async def fetch_defects(self, **kwargs) -> list[dict]:
                self.calls.append(("fetch_defects", kwargs))
                raise AsyncOnesPaginationError(
                    "cursor failed; Authorization=Bearer secret-token; request=/graphql?credential=secret",
                )

        client = RecordingPaginationClient()
        gateway = OnesGateway(async_client=client)

        with self.assertRaises(OnesGatewayPayloadError) as captured:
            await gateway.list_defects(project_id="proj-1", limit=20, page_size=5)

        self.assertEqual(str(captured.exception), "Malformed ONES pagination during async ONES fetch_defects")
        self.assertNotIn("secret-token", str(captured.exception))
        self.assertEqual(client.calls[0][1]["limit"], 20)
        self.assertEqual(client.calls[0][1]["page_size"], 5)

    async def test_list_open_defects_passes_total_limit_and_page_size_to_real_client_path(self):
        class RecordingClient(FakeAsyncClient):
            async def fetch_task_status_configs(self, project_ids: list[str]) -> list[dict]:
                return [{
                    "project_uuid": "proj-1",
                    "issue_type_uuid": "defect-type",
                    "status_uuid": "status-todo",
                    "position": 0,
                }]

            async def fetch_task_status_definitions(self) -> list[dict]:
                return [{"uuid": "status-todo", "name": "Todo", "category": "todo"}]

            async def fetch_defects(self, **kwargs) -> list[dict]:
                self.calls.append(("fetch_defects", kwargs))
                records = [
                    {
                        "uuid": f"d-{index}",
                        "name": f"Defect {index}",
                        "project": {"uuid": "proj-1", "name": "Project"},
                        "status": {"uuid": "status-todo", "name": "Todo", "category": "todo"},
                        "issueType": {"uuid": "defect-type", "name": "Defect"},
                        "priority": {"uuid": "priority-1", "value": "Medium", "position": 1},
                        "assign": {"uuid": "user-1", "name": "User"},
                    }
                    for index in range(250)
                ]
                return records[: kwargs["limit"]]

        client = RecordingClient()
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_open_defects(
            project_id="proj-1",
            issue_type_id="defect-type",
            sprint_id="sprint-1",
            assignee="user-1",
        )

        self.assertEqual(len(defects), 250)
        self.assertEqual(
            client.calls,
            [("fetch_defects", {
                "project_id": "proj-1",
                "issue_type_id": "defect-type",
                "limit": 5000,
                "page_size": 200,
                "sprint_id": "sprint-1",
                "assign": "user-1",
                "status_in": ["status-todo"],
            })],
        )

    async def test_list_open_defects_uses_dynamic_open_statuses_and_complete_paging(self):
        gateway = OnesGateway()
        statuses = unittest.mock.AsyncMock(return_value=[
            WorkflowStatusRef(id="status-todo", category="to_do"),
            WorkflowStatusRef(id="status-doing", category="in_progress"),
            WorkflowStatusRef(id="status-done", category="done"),
        ])
        expected = [unittest.mock.sentinel.defect]
        normalized = unittest.mock.AsyncMock(return_value=expected)

        with patch.object(OnesGateway, "list_defect_statuses", statuses), patch.object(
            OnesGateway, "list_normalized_defects", normalized,
        ):
            result = await gateway.list_open_defects(
                project_id="proj-1",
                issue_type_id="defect-type",
                sprint_id="sprint-1",
                assignee="user-1",
            )

        self.assertEqual(result, expected)
        normalized.assert_awaited_once_with(
            project_id="proj-1",
            issue_type_id="defect-type",
            sprint_id="sprint-1",
            assignee="user-1",
            status_ids=["status-todo", "status-doing"],
            limit=5000,
            page_size=200,
        )

    async def test_list_open_defects_uses_only_verified_requested_status_ids(self):
        gateway = OnesGateway()
        statuses = unittest.mock.AsyncMock(return_value=[
            WorkflowStatusRef(id="CKA6U955", name="待处理", category="to_do"),
            WorkflowStatusRef(id="WwhszYN8", name="修复中", category="in_progress"),
            WorkflowStatusRef(id="done-id", name="已关闭", category="done"),
        ])
        normalized = unittest.mock.AsyncMock(return_value=[])

        with patch.object(OnesGateway, "list_defect_statuses", statuses), patch.object(
            OnesGateway, "list_normalized_defects", normalized,
        ):
            await gateway.list_open_defects(
                project_id="proj-1",
                issue_type_id="defect-type",
                sprint_id="sprint-1",
                assignee="user-1",
                status_ids=("CKA6U955", "WwhszYN8"),
            )

        normalized.assert_awaited_once_with(
            project_id="proj-1",
            issue_type_id="defect-type",
            sprint_id="sprint-1",
            assignee="user-1",
            status_ids=["CKA6U955", "WwhszYN8"],
            limit=5000,
            page_size=200,
        )

    async def test_list_open_defects_rejects_unknown_or_closed_requested_status_ids(self):
        gateway = OnesGateway()
        statuses = unittest.mock.AsyncMock(return_value=[
            WorkflowStatusRef(id="CKA6U955", name="待处理", category="to_do"),
            WorkflowStatusRef(id="done-id", name="已关闭", category="done"),
        ])

        with patch.object(OnesGateway, "list_defect_statuses", statuses):
            for requested in (("missing-id",), ("done-id",)):
                with self.subTest(requested=requested):
                    with self.assertRaises(OnesGatewayPayloadError):
                        await gateway.list_open_defects(
                            project_id="proj-1",
                            issue_type_id="defect-type",
                            sprint_id="sprint-1",
                            assignee="user-1",
                            status_ids=requested,
                        )

    async def test_list_open_defects_rejects_unknown_status_category(self):
        gateway = OnesGateway()
        statuses = unittest.mock.AsyncMock(return_value=[
            WorkflowStatusRef(id="status-unknown", category="waiting_for_magic"),
        ])

        with patch.object(OnesGateway, "list_defect_statuses", statuses):
            with self.assertRaises(OnesGatewayPayloadError):
                await gateway.list_open_defects(
                    project_id="proj-1",
                    issue_type_id="defect-type",
                    sprint_id="sprint-1",
                    assignee="user-1",
                )

    async def test_list_open_defects_rejects_missing_workflow_statuses(self):
        gateway = OnesGateway()
        statuses = unittest.mock.AsyncMock(return_value=[])

        with patch.object(OnesGateway, "list_defect_statuses", statuses):
            with self.assertRaises(OnesGatewayPayloadError):
                await gateway.list_open_defects(
                    project_id="proj-1",
                    issue_type_id="defect-type",
                    sprint_id="sprint-1",
                    assignee="user-1",
                )

    async def test_list_open_defects_does_not_issue_unfiltered_query_when_all_statuses_are_closed(self):
        gateway = OnesGateway()
        statuses = unittest.mock.AsyncMock(return_value=[
            WorkflowStatusRef(id="status-done", category="done"),
            WorkflowStatusRef(id="status-cancelled", category="cancelled"),
        ])
        normalized = unittest.mock.AsyncMock()

        with patch.object(OnesGateway, "list_defect_statuses", statuses), patch.object(
            OnesGateway, "list_normalized_defects", normalized,
        ):
            result = await gateway.list_open_defects(
                project_id="proj-1",
                issue_type_id="defect-type",
                sprint_id="sprint-1",
                assignee="user-1",
            )

        self.assertEqual(result, [])
        normalized.assert_not_awaited()

    async def test_close_closes_managed_sync_and_async_clients(self):
        async_client = unittest.mock.AsyncMock()
        sync_client = unittest.mock.MagicMock()
        gateway = OnesGateway()
        gateway._managed_async_client = async_client
        gateway._managed_sync_client = sync_client

        await gateway.close()

        async_client.close.assert_awaited_once_with()
        sync_client.close.assert_called_once_with()
        self.assertIsNone(gateway._managed_async_client)
        self.assertIsNone(gateway._managed_sync_client)

    async def test_get_defect_detail_accepts_ones_key_identity(self):
        class KeyDetailClient(FakeAsyncClient):
            async def fetch_issue_detail(self, issue_id: str) -> dict:
                self.detail_calls.append(issue_id)
                return {
                    "uuid": "uuid-1",
                    "key": "task-key-1",
                    "name": "full detail",
                    "description": "complete",
                }

        client = KeyDetailClient()
        gateway = OnesGateway(async_client=client)

        detail = await gateway.get_defect_detail("task-key-1")

        self.assertEqual(detail["uuid"], "uuid-1")
        self.assertEqual(detail["key"], "task-key-1")
        self.assertEqual(detail["description"], "complete")

    async def test_get_defect_detail_scoped_lookup_fetches_full_detail(self):
        class FullDetailClient(FakeAsyncClient):
            async def fetch_issue_detail(self, issue_id: str) -> dict:
                self.detail_calls.append(issue_id)
                return {
                    "uuid": "uuid-1",
                    "key": "task-key-1",
                    "name": "full detail",
                    "description": "complete",
                }

        client = FullDetailClient(
            defects_results={
                "proj-1": [{"uuid": "uuid-1", "key": "task-key-1", "name": "summary"}],
            },
        )
        gateway = OnesGateway(async_client=client)

        detail = await gateway.get_defect_detail("uuid-1", project_id="proj-1")

        self.assertEqual(detail["description"], "complete")
        self.assertEqual(client.detail_calls, ["task-key-1"])

    async def test_list_defect_statuses_returns_complete_workflow_order(self):
        class StatusClient(FakeAsyncClient):
            async def fetch_task_status_configs(self, project_ids: list[str]) -> list[dict]:
                self.calls.append(("fetch_task_status_configs", {"project_ids": project_ids}))
                return [
                    {
                        "project_uuid": "proj-1",
                        "issue_type_uuid": "defect-type",
                        "status_uuid": "status-rejected",
                        "position": 4,
                        "default": False,
                    },
                    {
                        "project_uuid": "proj-1",
                        "issue_type_uuid": "defect-type",
                        "status_uuid": "status-todo",
                        "position": 0,
                        "default": True,
                    },
                ]

            async def fetch_task_status_definitions(self) -> list[dict]:
                self.calls.append(("fetch_task_status_definitions", {}))
                return [
                    {
                        "uuid": "status-todo",
                        "name": "待处理",
                        "category": "to_do",
                        "built_in": True,
                        "detail_type": "normal",
                        "name_pinyin": "daichuli",
                    },
                    {
                        "uuid": "status-rejected",
                        "name": "已拒绝",
                        "category": "done",
                        "built_in": False,
                        "detail_type": "normal",
                        "name_pinyin": "yijujue",
                    },
                ]

        client = StatusClient()
        gateway = OnesGateway(async_client=client)

        statuses = await gateway.list_defect_statuses("proj-1", "defect-type")

        self.assertEqual([status.id for status in statuses], ["status-todo", "status-rejected"])
        self.assertEqual([status.position for status in statuses], [0, 4])
        self.assertTrue(statuses[0].default)
        self.assertEqual(statuses[1].name, "已拒绝")
        self.assertEqual(statuses[1].category, "done")

    async def test_list_defect_statuses_rejects_missing_definition(self):
        class MissingDefinitionClient(FakeAsyncClient):
            async def fetch_task_status_configs(self, project_ids: list[str]) -> list[dict]:
                return [{
                    "project_uuid": "proj-1",
                    "issue_type_uuid": "defect-type",
                    "status_uuid": "missing-status",
                    "position": 0,
                    "default": True,
                }]

            async def fetch_task_status_definitions(self) -> list[dict]:
                return []

        gateway = OnesGateway(async_client=MissingDefinitionClient())

        with self.assertRaises(OnesGatewayPayloadError):
            await gateway.list_defect_statuses("proj-1", "defect-type")

    async def test_list_current_user_defects_aggregates_projects_and_respects_total_limit(self):
        client = FakeAsyncClient(
            mine_results={
                "proj-1": [{"uuid": "d1"}, {"uuid": "d2"}],
                "proj-2": [{"uuid": "d2"}, {"uuid": "d3"}],
            },
        )
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_current_user_defects(
            project_ids=["proj-1", "proj-2"],
            limit=3,
            page_size=2,
        )

        self.assertEqual([defect["uuid"] for defect in defects], ["d1", "d2", "d3"])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_my_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 3, "page_size": 2},
                ),
                (
                    "fetch_my_defects",
                    {"project_id": "proj-2", "issue_type_id": None, "limit": 1, "page_size": 2},
                ),
            ],
        )

    async def test_list_defects_normalizes_current_user_token_to_mine_path(self):
        client = FakeAsyncClient(mine_results={"proj-1": [{"uuid": "d1"}]})
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_defects(
            project_id="proj-1",
            assign="$currentUser",
            limit=5,
        )

        self.assertEqual(defects, [{"uuid": "d1"}])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_my_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 5, "page_size": 5},
                ),
            ],
        )

    async def test_list_defects_forwards_sprint_id_on_async_path(self):
        client = FakeAsyncClient(defects_results={"proj-1": [{"uuid": "d1"}]})
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_defects(project_id="proj-1", sprint_id="sprint-1", limit=5)

        self.assertEqual(defects, [{"uuid": "d1"}])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 5, "page_size": 5, "sprint_id": "sprint-1"},
                ),
            ],
        )

    async def test_list_defects_forwards_status_uuid_on_async_path(self):
        client = FakeAsyncClient(defects_results={"proj-1": [{"uuid": "d1"}]})
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_defects(project_id="proj-1", status_ids=["status-uuid-1"], limit=5)

        self.assertEqual(defects, [{"uuid": "d1"}])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 5, "page_size": 5, "status_in": ["status-uuid-1"]},
                ),
            ],
        )

    async def test_get_defect_detail_uses_scoped_current_user_fetch_then_loads_full_detail(self):
        client = FakeAsyncClient(mine_results={"proj-1": [{"uuid": "d2", "name": "mine defect"}]})
        gateway = OnesGateway(async_client=client)

        defect = await gateway.get_defect_detail("d2", project_id="proj-1", mine=True, limit=10)

        self.assertEqual(defect, {"uuid": "d2", "name": "fallback"})
        self.assertEqual(client.detail_calls, ["d2"])

    async def test_list_projects_maps_request_auth_failures_to_gateway_auth_error(self):
        gateway = OnesGateway(async_client=RaisingAsyncClient(exception=FakeHTTPStatusError(401)))

        with self.assertRaises(OnesGatewayAuthError):
            await gateway.list_projects()

    async def test_list_projects_maps_managed_client_login_failures_to_gateway_auth_error(self):
        class FakeManagedAsyncClient:
            def __init__(self, settings):
                self.settings = settings

            async def _get_client(self):
                raise FakeHTTPStatusError(403)

        gateway = OnesGateway(settings=object())

        with patch("src.integrations.ones_api.OnesAsyncClient", FakeManagedAsyncClient):
            with self.assertRaises(OnesGatewayAuthError):
                await gateway.list_projects()

    async def test_list_defects_maps_timeout_to_gateway_timeout_error(self):
        gateway = OnesGateway(async_client=RaisingAsyncClient(exception=TimeoutError("slow upstream")))

        with self.assertRaises(OnesGatewayTimeoutError):
            await gateway.list_defects(project_id="proj-1")

    async def test_get_defect_detail_raises_not_found_when_scoped_and_fallback_resolution_are_empty(self):
        client = FakeAsyncClient(defects_results={"proj-1": []})

        async def empty_detail(issue_id: str) -> dict:
            client.detail_calls.append(issue_id)
            return {}

        client.fetch_issue_detail = empty_detail
        gateway = OnesGateway(async_client=client)

        with self.assertRaises(OnesGatewayNotFoundError):
            await gateway.get_defect_detail("missing-1", project_id="proj-1")

    async def test_normalize_defect_raises_payload_error_for_missing_required_fields(self):
        gateway = OnesGateway()

        with self.assertRaises(OnesGatewayPayloadError):
            gateway.normalize_defect({"uuid": "bug-1", "name": "Broken payload"})

    async def test_normalize_defect_canonicalizes_real_microsecond_update_stamp(self):
        gateway = OnesGateway()

        defect = gateway.normalize_defect(
            {
                "uuid": "bug-1",
                "name": "Broken export",
                "number": 7,
                "project": {"uuid": "proj-1", "name": "Project"},
                "status": {"uuid": "status-1", "name": "Doing", "category": "in_progress"},
                "issueType": {"uuid": "defect-type", "name": "Defect"},
                "priority": {"uuid": "priority-1", "value": "High", "position": 1},
                "parent": {"uuid": "parent-1", "name": None, "avatar": {}},
                "deadline": None,
                "createTime": 1787213579958320,
                "serverUpdateStamp": 1786351001326130,
            }
        )

        self.assertEqual(defect.created_at, "2026-08-20T08:12:59.958320Z")
        self.assertEqual(defect.updated_at, "2026-08-10T08:36:41.326130Z")
        self.assertEqual(defect.deadline, "")
        self.assertEqual(defect.parent, IdentityRef(id="parent-1", name="", avatar=""))


class TestOnesGatewaySync(unittest.TestCase):
    def test_list_project_issue_types_sync_matches_authoritative_component_types(self):
        issue_types = OnesGateway(sync_client=FakeSyncClient()).list_project_issue_types_sync(
            "project-1"
        )

        self.assertEqual(
            [(item.id, item.component_type) for item in issue_types],
            [("requirement-type", "requirement"), ("task-type", "task")],
        )

    def test_list_defects_sync_maps_sync_pagination_failure_to_safe_payload_error(self):
        class RecordingPaginationClient(FakeSyncClient):
            def fetch_defects(self, **kwargs) -> list[dict]:
                self.calls.append(("fetch_defects", kwargs))
                raise SyncOnesPaginationError(
                    "cursor failed; Authorization=Bearer secret-token; request=/graphql?credential=secret",
                )

        client = RecordingPaginationClient()
        gateway = OnesGateway(sync_client=client)

        with self.assertRaises(OnesGatewayPayloadError) as captured:
            gateway.list_defects_sync(project_id="proj-1", limit=20, page_size=5)

        self.assertEqual(str(captured.exception), "Malformed ONES pagination during sync ONES fetch_defects")
        self.assertNotIn("secret-token", str(captured.exception))
        self.assertEqual(client.calls[0][1]["limit"], 20)
        self.assertEqual(client.calls[0][1]["page_size"], 5)

    def test_list_defect_statuses_sync_returns_workflow_order(self):
        class StatusClient(FakeSyncClient):
            def fetch_task_status_configs(self, project_ids: list[str]) -> list[dict]:
                return [
                    {
                        "project_uuid": "proj-1",
                        "issue_type_uuid": "defect-type",
                        "status_uuid": "status-done",
                        "position": 5,
                        "default": False,
                    },
                    {
                        "project_uuid": "proj-1",
                        "issue_type_uuid": "defect-type",
                        "status_uuid": "status-todo",
                        "position": 0,
                        "default": True,
                    },
                ]

            def fetch_task_status_definitions(self) -> list[dict]:
                return [
                    {"uuid": "status-todo", "name": "待处理", "category": "to_do"},
                    {"uuid": "status-done", "name": "关闭", "category": "done"},
                ]

        gateway = OnesGateway(sync_client=StatusClient())

        statuses = gateway.list_defect_statuses_sync("proj-1", "defect-type")

        self.assertEqual([status.id for status in statuses], ["status-todo", "status-done"])

    def test_list_defects_sync_preserves_non_mine_filters_across_projects(self):
        client = FakeSyncClient(
            defects_results={
                "proj-1": [{"uuid": "d1"}],
                "proj-2": [{"uuid": "d2"}],
            },
        )
        gateway = OnesGateway(sync_client=client)

        defects = gateway.list_defects_sync(
            project_ids=["proj-1", "proj-2"],
            assign="user-1",
            sprint_id="sprint-1",
            limit=2,
            page_size=1,
        )

        self.assertEqual([defect["uuid"] for defect in defects], ["d1", "d2"])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_defects",
                    {
                        "project_id": "proj-1",
                        "issue_type_id": None,
                        "limit": 2,
                        "page_size": 1,
                        "sprint_id": "sprint-1",
                        "assign": "user-1",
                    },
                ),
                (
                    "fetch_defects",
                    {
                        "project_id": "proj-2",
                        "issue_type_id": None,
                        "limit": 1,
                        "page_size": 1,
                        "sprint_id": "sprint-1",
                        "assign": "user-1",
                    },
                ),
            ],
        )

    def test_list_defects_sync_forwards_status_uuid(self):
        client = FakeSyncClient(defects_results={"proj-1": [{"uuid": "d1"}]})
        gateway = OnesGateway(sync_client=client)

        defects = gateway.list_defects_sync(project_id="proj-1", status_ids=["status-uuid-1"], limit=2)

        self.assertEqual(defects, [{"uuid": "d1"}])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 2, "page_size": 2, "status_in": ["status-uuid-1"]},
                ),
            ],
        )

    def test_get_defect_detail_sync_uses_scoped_fetch_then_loads_full_detail(self):
        client = FakeSyncClient(mine_results={"proj-1": [{"uuid": "d3", "name": "scoped"}]})
        gateway = OnesGateway(sync_client=client)

        defect = gateway.get_defect_detail_sync("d3", project_id="proj-1", current_user=True, limit=4)

        self.assertEqual(defect, {"uuid": "d3", "name": "fallback"})
        self.assertEqual(client.detail_calls, ["d3"])

    def test_list_projects_sync_maps_request_auth_failures_to_gateway_auth_error(self):
        gateway = OnesGateway(sync_client=RaisingSyncClient(exception=FakeHTTPStatusError(401)))

        with self.assertRaises(OnesGatewayAuthError):
            gateway.list_projects_sync()


class TestOnesGatewayWikiAndRequirements(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = OnesSettings(
            base_url="https://ONES.Test:443/root/",
            team_id="team-1",
            email="",
            password="",
            _env_file=None,
        )

    def test_parse_wiki_url_accepts_same_origin_default_port_and_canonical_path(self):
        gateway = OnesGateway(settings=self.settings)

        parsed = gateway.parse_wiki_url(
            "https://ones.test/root/wiki/#/team/team-1/space/space-1/page/page-1",
        )

        self.assertEqual(parsed.team_id, "team-1")
        self.assertEqual(parsed.space_id, "space-1")
        self.assertEqual(parsed.page_id, "page-1")
        self.assertEqual(
            parsed.source_url,
            "https://ones.test/root/wiki/#/team/team-1/space/space-1/page/page-1",
        )

    def test_parse_wiki_url_rejects_untrusted_or_ambiguous_urls(self):
        gateway = OnesGateway(settings=self.settings)
        rejected = [
            "https://evil.test/root/wiki/#/team/team-1/space/s/page/p",
            "https://ones.test/root/wiki/#/team/other/space/s/page/p",
            "https://ones.test/root/wiki/#/team/team-1/space/s/../page/p",
            "https://ones.test/root/wiki/?team=team-1&space=s&page=p",
            "https://ones.test/root/wiki/?ignored=evil#/team/team-1/space/s/page/p",
            "https://ones.test/root/wiki/#/team/team-1/space/s/page/p?ignored=evil",
            "https://ones.test/root/wiki/#/team/team-1/space/s/page/p/extra",
            "https://ones.test/root/wiki/#/team/team-1/space/s/page/p#extra",
            "https://ones.test/root/wiki/#/team/team-1/space//page/p",
            "https://ones.test/root/wiki/#/team/team-1/space/a\\b/page/p",
            "https://ones.test/root/wiki/#/team/team-1/space/a b/page/p",
            "https://ones.test/root/wiki/#/team/team-1/space/a%25b/page/p",
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ValueError):
                gateway.parse_wiki_url(url)

    async def test_wiki_snapshot_by_ids_rejects_unsafe_segment_before_client_call(self):
        calls = 0

        class Client:
            async def fetch_wiki_page(self, space_id, page_id):
                nonlocal calls
                calls += 1
                return {"content": "body"}

        gateway = OnesGateway(settings=self.settings, async_client=Client())
        for bad_id in ("..", "a\\b", "a b", "a%b"):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                await gateway.get_wiki_snapshot_by_ids(bad_id, "page")
        self.assertEqual(calls, 0)

    async def test_wiki_snapshot_normalizes_text_and_hash_stably(self):
        class Client:
            async def fetch_wiki_page(self, space_id, page_id):
                return {"content": "line 1  \r\nline 2\t\r\n"}

            async def fetch_wiki_page_info(self, page_id):
                return {"title": "Design", "version": 7, "updated_at": "2026-08-10T00:00:00Z"}

        gateway = OnesGateway(settings=self.settings, async_client=Client())
        snapshot = await gateway.get_wiki_snapshot(
            "https://ones.test/root/wiki/#/team/team-1/space/space-1/page/page-1",
        )

        self.assertEqual(snapshot.normalized_content, "line 1\nline 2\n")
        self.assertEqual(snapshot.content_sha256, "9060554863a62b9db5f726216876654e561896071d2e6480f2048b70e0fdadb9")
        self.assertEqual((snapshot.title, snapshot.version, snapshot.updated_at), ("Design", "7", "2026-08-10T00:00:00Z"))

    async def test_wiki_snapshot_accepts_lan_created_time_metadata(self):
        class Client:
            async def fetch_wiki_page(self, space_id, page_id):
                return {"content": "body"}

            async def fetch_wiki_page_info(self, page_id):
                return {"title": "LAN page", "version": 7, "CreatedTime": 1782979542}

        snapshot = await OnesGateway(
            settings=self.settings, async_client=Client()
        ).get_wiki_snapshot_by_ids("space-1", "page-1")

        self.assertEqual(snapshot.updated_at, "2026-07-02T08:05:42Z")

    def test_wiki_timestamp_normalization_is_strict_and_canonical(self):
        valid = {
            "2026-08-10T08:00:00+08:00": "2026-08-10T00:00:00Z",
            "2026-08-10T00:00:00.123Z": "2026-08-10T00:00:00.123000Z",
            1700000000: "2023-11-14T22:13:20Z",
            1700000000123: "2023-11-14T22:13:20.123000Z",
        }
        for raw, expected in valid.items():
            with self.subTest(raw=raw):
                self.assertEqual(OnesGateway._normalize_wiki_timestamp(raw), expected)
        for raw in (
            True, False, 1.5, "now", "2026-08-10T00:00:00", " 2026-08-10T00:00:00Z",
            0, 999999999999999999999,
        ):
            with self.subTest(raw=raw), self.assertRaises(OnesGatewayPayloadError):
                OnesGateway._normalize_wiki_timestamp(raw)

    def test_wiki_snapshot_sync_serializes_structured_content_stably(self):
        class Client:
            def fetch_wiki_page(self, space_id, page_id):
                return {"page": {"content": {"z": "中文", "a": [2, 1]}}}

            def fetch_wiki_page_info(self, page_id):
                return {"name": "Structured", "revision": "r2", "serverUpdateStamp": 1700000000}

        gateway = OnesGateway(settings=self.settings, sync_client=Client())
        snapshot = gateway.get_wiki_snapshot_sync(
            "https://ones.test/root/wiki/#/team/team-1/space/space-1/page/page-2",
        )

        self.assertEqual(snapshot.normalized_content, '{"a":[2,1],"z":"中文"}')
        self.assertEqual(snapshot.title, "Structured")

    def test_wiki_snapshot_rejects_missing_or_malformed_envelopes_and_metadata(self):
        valid_body = {"content": "body"}
        valid_detail = {"title": "Title", "version": 1, "updated_at": "2026-08-10T00:00:00Z"}
        invalid_pairs = [
            ({}, valid_detail),
            ({"page": {}}, valid_detail),
            ({"data": {}}, valid_detail),
            ({"content": "body", "page": []}, valid_detail),
            ({"content": "body", "data": "bad"}, valid_detail),
            (valid_body, {"title": "Title", "version": 1, "updated_at": "2026-08-10T00:00:00Z", "page": []}),
            (valid_body, {"title": "Title", "version": 1, "updated_at": "2026-08-10T00:00:00Z", "data": "bad"}),
            (valid_body, {"version": 1, "updated_at": "2026-08-10T00:00:00Z"}),
            (valid_body, {"title": "Title", "updated_at": "2026-08-10T00:00:00Z"}),
            (valid_body, {"title": "Title", "version": 1}),
            ({"content": "  \r\n\t"}, valid_detail),
            ({"content": 42}, valid_detail),
            (valid_body, {"title": {}, "version": 1, "updated_at": "2026-08-10T00:00:00Z"}),
            (valid_body, {"title": "Title", "version": [], "updated_at": "2026-08-10T00:00:00Z"}),
            (valid_body, {"title": "Title", "version": 1, "updated_at": {"bad": True}}),
            (valid_body, {"title": "Title", "version": True, "updated_at": "2026-08-10T00:00:00Z"}),
            (valid_body, {"title": "Title", "version": 1, "updated_at": False}),
            ({"content": {"value": float("nan")}}, valid_detail),
            ({"content": {"value": float("inf")}}, valid_detail),
            ({"content": "bad\ud800text"}, valid_detail),
            ({"content": {"text": "bad\ud800text"}}, valid_detail),
            (valid_body, {"title": "bad\ud800title", "version": 1, "updated_at": "2026-08-10T00:00:00Z"}),
            (valid_body, {"title": "Title", "version": "bad\ud800version", "updated_at": "2026-08-10T00:00:00Z"}),
        ]

        for body, detail in invalid_pairs:
            with self.subTest(body=body, detail=detail), self.assertRaises(OnesGatewayPayloadError):
                OnesGateway(settings=self.settings)._build_wiki_snapshot(
                    "space", "page", body, detail, source_url="https://ones.test/wiki",
                )

    def test_structured_wiki_serialization_error_has_no_sensitive_chain(self):
        sentinel = "Cookie=session Authorization=Bearer-secret body=private"

        class SensitiveObject:
            def __repr__(self):
                return sentinel

        with self.assertRaises(OnesGatewayPayloadError) as captured:
            OnesGateway(settings=self.settings)._build_wiki_snapshot(
                "space",
                "page",
                {"content": {"node": SensitiveObject()}},
                {"title": "Title", "version": 1, "updated_at": "2026-08-10T00:00:00Z"},
                source_url="https://ones.test/wiki",
            )

        error = captured.exception
        exposed = " ".join((str(error), repr(error), repr(error.__cause__), repr(error.__context__)))
        self.assertNotIn("Bearer-secret", exposed)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    async def test_normalized_requirement_extracts_only_provable_wiki_urls(self):
        detail = {
            "uuid": "req-1", "number": 42, "name": "Build it",
            "project": {"uuid": "proj-1", "name": "P"},
            "sprint": {"uuid": "iter-1", "name": "I"},
            "assign": {"uuid": "user-1", "name": "U"},
            "status": {"uuid": "status-1", "name": "Doing", "category": "doing"},
            "description": "See https://ones.test/root/wiki/#/team/team-1/space/space-1/page/page-1",
            "relatedWikiPages": [
                {"uuid": "page-1", "title": "Spec"},
                {"uuid": "page-without-space", "title": "Do not guess"},
                {
                    "uuid": "page-2",
                    "title": "Linked",
                    "url": "https://ones.test/root/wiki/#/team/team-1/space/space-2/page/page-2",
                },
            ],
        }

        class Client:
            async def fetch_issue_detail(self, issue_id):
                return detail

        record = await OnesGateway(settings=self.settings, async_client=Client()).get_normalized_requirement("req-1")

        self.assertEqual(record.requirement_id, "req-1")
        self.assertEqual((record.project.id, record.iteration.id, record.assignee.id, record.status.id), ("proj-1", "iter-1", "user-1", "status-1"))
        self.assertEqual(len(record.wiki_refs), 2)
        self.assertEqual((record.wiki_refs[0].page_id, record.wiki_refs[0].space_id, record.wiki_refs[0].title), ("page-1", "space-1", "Spec"))
        self.assertEqual((record.wiki_refs[1].page_id, record.wiki_refs[1].space_id, record.wiki_refs[1].title), ("page-2", "space-2", "Linked"))

    async def test_wiki_errors_map_and_retry_only_transient_failures(self):
        attempts = 0

        class TransientClient:
            async def fetch_wiki_page(self, space_id, page_id):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise FakeHTTPStatusError(503)
                return {"content": "ok"}

            async def fetch_wiki_page_info(self, page_id):
                return {"title": "Recovered", "version": 1, "updated_at": "2026-08-10T00:00:00Z"}

        gateway = OnesGateway(settings=self.settings, async_client=TransientClient(), retry_backoff=lambda attempt: 0)
        snapshot = await gateway.get_wiki_snapshot("https://ones.test/root/wiki/#/team/team-1/space/s/page/p")
        self.assertEqual(attempts, 3)
        self.assertEqual(snapshot.title, "Recovered")

        for status, error in ((401, OnesGatewayAuthError), (403, OnesGatewayAuthError), (404, OnesGatewayNotFoundError)):
            class FailingClient:
                async def fetch_wiki_page(self, space_id, page_id):
                    raise FakeHTTPStatusError(status)
            with self.subTest(status=status), self.assertRaises(error):
                await OnesGateway(settings=self.settings, async_client=FailingClient(), retry_backoff=lambda attempt: 0).get_wiki_snapshot(
                    "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
                )

    async def test_async_wiki_retry_counts_use_exact_httpx_types_and_statuses(self):
        request = httpx.Request("GET", "https://ones.test/wiki")
        cases = [
            (lambda: httpx.ReadError("sentinel", request=request), 3, OnesGatewayError),
            (lambda: httpx.ProxyError("sentinel", request=request), 3, OnesGatewayError),
            (lambda: httpx.ReadTimeout("sentinel", request=request), 3, OnesGatewayTimeoutError),
            (lambda: httpx.ConnectTimeout("sentinel", request=request), 3, OnesGatewayTimeoutError),
            (lambda: TimeoutError("sentinel"), 1, OnesGatewayTimeoutError),
            (lambda: FakeHTTPStatusError(429), 3, OnesGatewayError),
            (lambda: FakeHTTPStatusError(500), 3, OnesGatewayError),
            (lambda: FakeHTTPStatusError(600), 1, OnesGatewayError),
            (lambda: FakeHTTPStatusError(401), 1, OnesGatewayAuthError),
            (lambda: FakeHTTPStatusError(403), 1, OnesGatewayAuthError),
            (lambda: FakeHTTPStatusError(404), 1, OnesGatewayNotFoundError),
        ]
        for exception_factory, expected_attempts, error_type in cases:
            attempts = 0

            class Client:
                async def fetch_wiki_page(self, space_id, page_id):
                    nonlocal attempts
                    attempts += 1
                    raise exception_factory()

            with self.subTest(exception=exception_factory().__class__.__name__), self.assertRaises(error_type):
                await OnesGateway(settings=self.settings, async_client=Client(), retry_backoff=lambda attempt: 0).get_wiki_snapshot(
                    "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
                )
            self.assertEqual(attempts, expected_attempts)

    def test_sync_wiki_retry_counts_use_exact_requests_types(self):
        cases = [
            (lambda: requests.exceptions.SSLError("sentinel"), 3, OnesGatewayError),
            (lambda: requests.exceptions.ProxyError("sentinel"), 3, OnesGatewayError),
            (lambda: requests.exceptions.ReadTimeout("sentinel"), 1, OnesGatewayTimeoutError),
            (lambda: TimeoutError("sentinel"), 1, OnesGatewayTimeoutError),
            (lambda: FakeHTTPStatusError(600), 1, OnesGatewayError),
        ]
        for exception_factory, expected_attempts, error_type in cases:
            attempts = 0

            class Client:
                def fetch_wiki_page(self, space_id, page_id):
                    nonlocal attempts
                    attempts += 1
                    raise exception_factory()

            with self.subTest(exception=exception_factory().__class__.__name__), self.assertRaises(error_type):
                OnesGateway(settings=self.settings, sync_client=Client(), retry_backoff=lambda attempt: 0).get_wiki_snapshot_sync(
                    "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
                )
            self.assertEqual(attempts, expected_attempts)

    async def test_wiki_timeout_and_non_mapping_errors_are_stable_and_sanitized(self):
        class TimeoutClient:
            async def fetch_wiki_page(self, space_id, page_id):
                raise TimeoutError("Cookie=session-secret Authorization=Bearer token password=p")

        with self.assertRaises(OnesGatewayTimeoutError) as captured:
            await OnesGateway(settings=self.settings, async_client=TimeoutClient()).get_wiki_snapshot(
                "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
            )
        message = str(captured.exception)
        self.assertNotIn("session-secret", message)
        self.assertNotIn("Bearer token", message)

        class PayloadClient:
            async def fetch_wiki_page(self, space_id, page_id):
                return ["bad"]

        with self.assertRaises(OnesGatewayPayloadError):
            await OnesGateway(settings=self.settings, async_client=PayloadClient()).get_wiki_snapshot(
                "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
            )

    async def test_async_wiki_error_breaks_sensitive_exception_chain_and_logs(self):
        sentinel = "Cookie=session Authorization=Bearer-secret email=user@example password=p body=private"
        request = httpx.Request("GET", "https://ones.test/wiki")

        class Client:
            async def fetch_wiki_page(self, space_id, page_id):
                raise httpx.ReadError(sentinel, request=request)

        with capture_logs() as logs:
            with self.assertRaises(OnesGatewayError) as captured:
                await OnesGateway(settings=self.settings, async_client=Client(), retry_backoff=lambda attempt: 0).get_wiki_snapshot(
                    "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
                )

        error = captured.exception
        exposed = " ".join((str(error), repr(error), repr(error.__cause__), repr(error.__context__), repr(logs)))
        for secret in ("session", "Bearer-secret", "user@example", "password=p", "body=private"):
            self.assertNotIn(secret, exposed)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    async def test_managed_wiki_client_init_error_is_sanitized_before_logging(self):
        sentinel = "Cookie=session Authorization=Bearer-secret email=user@example password=p body=private"

        class FailingManagedClient:
            def __init__(self, settings):
                raise ValueError(sentinel)

        with patch("src.integrations.ones_api.OnesAsyncClient", FailingManagedClient):
            with capture_logs() as logs:
                with self.assertRaises(OnesGatewayPayloadError) as captured:
                    await OnesGateway(settings=self.settings).get_wiki_snapshot(
                        "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
                    )

        error = captured.exception
        exposed = " ".join((str(error), repr(error), repr(error.__cause__), repr(error.__context__), repr(logs)))
        for secret in ("session", "Bearer-secret", "user@example", "password=p", "body=private"):
            self.assertNotIn(secret, exposed)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertTrue(all(not ({"exc", "message", "body", "headers"} & set(entry)) for entry in logs))

    def test_sync_wiki_payload_error_breaks_sensitive_exception_chain_and_logs(self):
        sentinel = "Cookie=session Authorization=Bearer-secret email=user@example password=p body=private"

        class Client:
            def fetch_wiki_page(self, space_id, page_id):
                raise ValueError(sentinel)

        with capture_logs() as logs:
            with self.assertRaises(OnesGatewayPayloadError) as captured:
                OnesGateway(settings=self.settings, sync_client=Client()).get_wiki_snapshot_sync(
                    "https://ones.test/root/wiki/#/team/team-1/space/s/page/p",
                )

        error = captured.exception
        exposed = " ".join((str(error), repr(error), repr(error.__cause__), repr(error.__context__), repr(logs)))
        for secret in ("session", "Bearer-secret", "user@example", "password=p", "body=private"):
            self.assertNotIn(secret, exposed)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_normalized_requirement_sync_matches_async_shape(self):
        class Client:
            def fetch_issue_detail(self, issue_id):
                return {
                    "uuid": issue_id,
                    "title": "Requirement",
                    "project": {"uuid": "project"},
                    "sprint": {"uuid": "iteration"},
                    "status": {"uuid": "status"},
                    "description": "",
                    "relatedWikiPages": [],
                }

        record = OnesGateway(settings=self.settings, sync_client=Client()).get_normalized_requirement_sync("req-sync")
        self.assertEqual((record.requirement_id, record.title, record.source), ("req-sync", "Requirement", "ones"))

    def test_requirement_rejects_missing_or_malformed_mappings(self):
        base = {
            "uuid": "req", "title": "Requirement",
            "project": {"uuid": "project"},
            "status": {"uuid": "status"},
            "relatedWikiPages": [],
        }
        invalid_overrides = [
            {"project": None},
            {"project": []},
            {"status": None},
            {"status": "bad"},
            {"sprint": "bad"},
            {"assign": 42},
            {"relatedWikiPages": "bad"},
            {"relatedWikiPages": ["bad"]},
            {"uuid": ["req"]},
            {"title": {"text": "bad"}},
            {"project": {}},
            {"project": {"uuid": []}},
            {"project": {"uuid": "project", "name": {"bad": True}}},
            {"status": {}},
            {"status": {"uuid": {"bad": True}}},
            {"status": {"uuid": "status", "category": []}},
            {"sprint": {}},
            {"sprint": {"name": "missing uuid"}},
            {"assign": {}},
            {"assign": {"uuid": False}},
            {"number": True},
            {"number": []},
            {"description": 42},
            {"description": {"value": float("nan")}},
            {"description": {"value": float("inf")}},
            {"description": "bad\ud800text"},
            {"description": {"text": "bad\ud800text"}},
            {"uuid": "bad\ud800uuid"},
            {"project": {"uuid": "bad\ud800uuid"}},
            {"status": {"uuid": "status", "name": "bad\ud800name"}},
        ]

        gateway = OnesGateway(settings=self.settings)
        for override in invalid_overrides:
            payload = {**base, **override}
            with self.subTest(override=override), self.assertRaises(OnesGatewayPayloadError):
                gateway.normalize_requirement(payload)

    def test_requirement_normalizes_structured_description_as_canonical_json(self):
        gateway = OnesGateway(settings=self.settings)
        payload = {
            "uuid": 7,
            "number": 3.5,
            "title": " Requirement ",
            "project": {"uuid": 1, "name": " Project "},
            "status": {"uuid": 2, "name": " Status ", "category": " doing "},
            "issueType": {
                "uuid": "requirement-type",
                "name": " Requirement ",
                "builtIn": True,
                "detailType": "requirement",
            },
            "description": {"z": "中文", "a": [2, 1]},
            "relatedWikiPages": [],
        }

        record = gateway.normalize_requirement(payload)

        self.assertEqual(record.requirement_id, "7")
        self.assertEqual(record.number, "3.5")
        self.assertEqual(record.title, "Requirement")
        self.assertEqual((record.project.id, record.project.name), ("1", "Project"))
        self.assertEqual((record.status.id, record.status.name, record.status.category), ("2", "Status", "doing"))
        self.assertEqual(
            (
                record.issue_type.id,
                record.issue_type.name,
                record.issue_type.built_in,
                record.issue_type.detail_type,
            ),
            ("requirement-type", "Requirement", True, "requirement"),
        )
        self.assertEqual(record.description, '{"a":[2,1],"z":"中文"}')
