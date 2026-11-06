from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import server
from src.contracts import IssueTypeRef
from src.integrations.ones import OnesClient
from src.integrations.ones_api import OnesAsyncClient
from src.services.ones_gateway import OnesGateway, OnesGatewayError, OnesGatewayPayloadError, OnesGatewayTimeoutError


def metadata():
    fields = [
        ("field001", "标题", 2, True, None),
        ("field004", "负责人", 8, True, "$taskCreator"),
        ("field012", "优先级", 1, True, "normal"),
        ("field011", "迭代", 7, True, None),
        ("start", "开始日期", 5, True, None),
        ("field013", "结束日期", 5, True, None),
        ("field016", "描述", 20, False, None),
    ]
    return {
        "project": {
            "field_config": {"field_configs": [
                {"project_uuid": "project1", "issue_type_uuid": "tasktype", "field_uuid": ident,
                 "type": typ, "required": required, "generated": False, "default_value": default}
                for ident, _, typ, required, default in fields
            ]},
            "issue_type_config": {"issue_type_configs": [{"project_uuid": "project1", "issue_type_uuid": "tasktype"}]},
        },
        "team": {
            "task_field": {"fields": [{"uuid": ident, "name": name} for ident, name, *_ in fields]},
            "task_field_option": {"options": [{"uuid": "normal", "field_uuid": "field012", "value": "普通"}]},
        },
    }


def values():
    return [
        {"field_id": "field011", "value": "sprint1"},
        {"field_id": "start", "value": 1793894400, "date_value": "2026-11-06"},
        {"field_id": "field013", "value": 1793894400, "date_value": "2026-11-06"},
    ]


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(OnesGateway, "list_project_issue_types", AsyncMock(return_value=[IssueTypeRef(id="tasktype", component_type="task")]))
    monkeypatch.setattr(OnesGateway, "list_project_issue_types_sync", MagicMock(return_value=[IssueTypeRef(id="tasktype", component_type="task")]))
    client = MagicMock()
    client.fetch_task_creation_metadata = AsyncMock(return_value=metadata())
    client.fetch_current_user_identity = AsyncMock(return_value={"uuid": "creator1"})
    client.create_task = AsyncMock(side_effect=lambda task: {"tasks": [{"uuid": task["uuid"], "number": 12}], "bad_tasks": []})
    return OnesGateway(async_client=client)


async def test_field_discovery_and_creation_use_project_metadata(gateway):
    fields = await gateway.get_task_create_fields("project1", "tasktype")
    priority = next(field for field in fields if field["field_id"] == "field012")
    assert priority["required"] and priority["default_value"] == "normal"
    assert priority["options"] == [{"id": "normal", "name": "普通"}]
    result = await gateway.create_task("project1", "tasktype", "实现功能", field_values=values())
    task = gateway.async_client.create_task.await_args.args[0]
    assert result == {"task_id": task["uuid"], "number": 12}
    assert task["uuid"].startswith("creator1") and len(task["uuid"]) == len("creator1") + 8
    assert task["assign"] == "creator1" and task["watchers"] == ["creator1"]
    assert task["project_uuid"] == "project1" and task["issue_type_uuid"] == "tasktype"
    actual = {item["field_uuid"]: item for item in task["field_values"]}
    assert actual["field012"] == {"field_uuid": "field012", "type": 1, "value": "normal"}
    assert actual["field011"]["value"] == "sprint1"
    assert actual["start"]["date_value"] == "2026-11-06"
    assert actual["field001"]["value"] == "实现功能"
    gateway.async_client.create_task.assert_awaited_once()


@pytest.mark.parametrize("bad_values", [
    [],
    values() + [{"field_id": "unknown", "value": "x"}],
    values() + [{"field_id": "field001", "value": "x"}],
    values() + [{"field_id": "field011", "value": "again"}],
    [{"field_id": "field011", "value": "sprint1"}, {"field_id": "start", "value": 1793894400}],
])
async def test_invalid_fields_never_submit(gateway, bad_values):
    with pytest.raises(ValueError):
        await gateway.create_task("project1", "tasktype", "标题", field_values=bad_values)
    gateway.async_client.create_task.assert_not_awaited()


async def test_wrong_issue_type_does_not_fetch_or_submit(gateway):
    with pytest.raises(ValueError):
        await gateway.create_task("project1", "other", "标题", field_values=values())
    gateway.async_client.fetch_task_creation_metadata.assert_not_awaited()
    gateway.async_client.create_task.assert_not_awaited()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("lost"), httpx.ConnectError("lost")])
async def test_unknown_outcome_reports_task_id_and_never_retries(gateway, failure):
    gateway.async_client.create_task.side_effect = failure
    with pytest.raises(OnesGatewayError, match="outcome unknown, task_id=creator1"):
        await gateway.create_task("project1", "tasktype", "标题", field_values=values())
    gateway.async_client.create_task.assert_awaited_once()


async def test_rejected_and_malformed_create_responses_are_not_success(gateway):
    gateway.async_client.create_task.side_effect = None
    gateway.async_client.create_task.return_value = {"bad_tasks": [{"uuid": "x"}]}
    with pytest.raises(OnesGatewayError, match="rejected"):
        await gateway.create_task("project1", "tasktype", "标题", field_values=values())
    gateway.async_client.create_task.return_value = {"tasks": []}
    with pytest.raises(OnesGatewayPayloadError, match="outcome unknown, task_id=creator1"):
        await gateway.create_task("project1", "tasktype", "标题", field_values=values())


def test_sync_gateway_creates_same_payload(monkeypatch, gateway):
    client = MagicMock()
    client.fetch_task_creation_metadata.return_value = metadata()
    client.fetch_current_user_identity.return_value = {"uuid": "creator1"}
    client.create_task.side_effect = lambda task: {"tasks": [{"uuid": task["uuid"]}], "bad_tasks": []}
    result = OnesGateway(sync_client=client).create_task_sync("project1", "tasktype", "标题", "member1", values())
    task = client.create_task.call_args.args[0]
    assert result["task_id"] == task["uuid"] and task["assign"] == "member1"
    client.create_task.assert_called_once()


async def test_async_rest_endpoints_and_single_post():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = OnesAsyncClient()
    client._base_url = "https://ones.invalid"
    client._team_id = "team1"
    client._ready = True
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert await client.fetch_task_creation_metadata("project1") == {"project": {"ok": True}, "team": {"ok": True}}
        await client.create_task({"uuid": "task1"})
    finally:
        await client.close()
    assert str(requests[0].url).endswith("/project/project1/stamps/data?t=field_config,issue_type_config")
    assert str(requests[1].url).endswith("/team/team1/stamps/data?t=task_field,task_field_option")
    assert str(requests[2].url).endswith("/team/team1/tasks/add3")
    assert json.loads(requests[2].content) == {"tasks": [{"uuid": "task1"}]}


def test_sync_rest_endpoints_and_single_post():
    client = OnesClient(base_url="https://ones.invalid", email="", password="", team_id="team1")
    response = MagicMock()
    response.json.return_value = {"ok": True}
    client.session.post = MagicMock(return_value=response)
    assert client.fetch_task_creation_metadata("project1") == {"project": {"ok": True}, "team": {"ok": True}}
    client.create_task({"uuid": "task1"})
    calls = client.session.post.call_args_list
    assert calls[0].args[0].endswith("/project/project1/stamps/data?t=field_config,issue_type_config")
    assert calls[1].args[0].endswith("/team/team1/stamps/data?t=task_field,task_field_option")
    assert calls[2].args[0].endswith("/team/team1/tasks/add3")
    assert calls[2].kwargs == {"json": {"tasks": [{"uuid": "task1"}]}, "timeout": 30.0}


async def test_mcp_tools_forward_task_arguments_and_close_gateway(monkeypatch):
    gateway = MagicMock()
    gateway.get_task_create_fields = AsyncMock(return_value=[{"field_id": "field011"}])
    gateway.create_task = AsyncMock(return_value={"task_id": "task1"})
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=gateway)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(server, "OnesGateway", lambda: context)
    assert await server.get_task_create_fields("project1", "tasktype") == [{"field_id": "field011"}]
    assert await server.create_task("project1", "tasktype", "标题", "member1", values()) == {"task_id": "task1"}
    gateway.get_task_create_fields.assert_awaited_once_with("project1", "tasktype")
    gateway.create_task.assert_awaited_once_with("project1", "tasktype", "标题", "member1", values())
    assert context.__aexit__.await_count == 2
