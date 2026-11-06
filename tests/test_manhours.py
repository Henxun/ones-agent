from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import server
from src.integrations.ones import OnesClient
from src.integrations.ones_api import OnesAsyncClient
from src.integrations.ones_manhour import ADD_MANHOUR, LIST_MANHOURS, UPDATE_MANHOUR
from src.services.ones_gateway import OnesGateway, OnesGatewayAuthError, OnesGatewayPayloadError, OnesGatewayTimeoutError


@pytest.mark.parametrize(
    ("method", "query", "tag"),
    [("list_manhours", LIST_MANHOURS, "UserManhoursTimeSeries"),
     ("add_manhour", ADD_MANHOUR, "AddManhour"),
     ("update_manhour", UPDATE_MANHOUR, "UpdateManhour")],
)
async def test_sync_and_async_adapters_use_same_verified_operation(method, query, tag):
    sync = object.__new__(OnesClient)
    sync._graphql = MagicMock(return_value={"result": 1})
    async_client = object.__new__(OnesAsyncClient)
    async_client._graphql = AsyncMock(return_value={"result": 1})
    assert getattr(sync, method)({"x": 1}) == {"result": 1}
    assert await getattr(async_client, method)({"x": 1}) == {"result": 1}
    sync._graphql.assert_called_once_with(query, {"x": 1}, t=tag, timeout=30.0, strict_errors=True)
    async_client._graphql.assert_awaited_once_with(query, {"x": 1}, t=tag)


async def test_async_client_sends_verified_graphql_and_normalizes_response():
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={"data": {"addManhour": {"key": "manhour-m1"}}})

    client = OnesAsyncClient()
    client._base_url = "https://ones.invalid"
    client._team_id = "team1"
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client._ready = True
    try:
        result = await OnesGateway(async_client=client).add_manhour("u1", "t1", 1793926800, 0.01)
    finally:
        await client.close()
    assert result == {"key": "manhour-m1"}
    assert str(captured[0].url) == "https://ones.invalid/project/api/project/team/team1/items/graphql?t=AddManhour"
    body = json.loads(captured[0].content)
    assert body["query"] == ADD_MANHOUR
    assert body["variables"]["hours"] == 1000


def test_sync_partial_graphql_error_is_not_mistaken_for_success():
    client = OnesClient(base_url="https://ones.invalid", email="", password="", team_id="team1")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": {"addManhour": {"key": "manhour-m1"}}, "errors": [{"message": "failure"}]}
    client.session.post = MagicMock(return_value=response)
    with pytest.raises(OnesGatewayPayloadError):
        OnesGateway(sync_client=client).add_manhour_sync("u1", "t1", 1793926800, 1)
    assert client.session.post.call_count == 1
    assert client.session.post.call_args.kwargs["timeout"] == 30.0


def workhour_payload():
    return {"buckets": [{"startDate": 1793894400, "manhours": [{
        "key": "manhour-m1", "hours": 125000, "owner": {"uuid": "u1"},
        "task": {"key": "task-t1", "uuid": "t1", "name": "任务"},
        "startTime": 1793926800, "description": "实现功能", "type": "recorded",
        "canUpdateRecordedHour": True,
    }]}]}


async def test_list_normalizes_records_and_forwards_inclusive_date_range():
    client = MagicMock()
    client.list_manhours = AsyncMock(return_value=workhour_payload())
    gateway = OnesGateway(async_client=client)
    result = await gateway.list_manhours("u1", start_date="2026-11-01", end_date="2026-11-06", task_id="t1")
    assert result == [{"key": "manhour-m1", "owner_id": "u1", "task_id": "t1", "task_name": "任务",
                       "start_time": 1793926800, "hours": 1.25, "description": "实现功能", "can_update": True}]
    variables = client.list_manhours.await_args.args[0]
    assert variables["manhoursFilter"] == {
        "owner_in": ["u1"], "startDate_range": {"unit": "day", "from": "2026-11-01", "to": "2026-11-06"},
        "type_in": ["recorded"],
    }


def test_sync_list_defaults_to_this_week():
    client = MagicMock()
    client.list_manhours.return_value = workhour_payload()
    result = OnesGateway(sync_client=client).list_manhours_sync("u1")
    assert result[0]["hours"] == 1.25
    assert client.list_manhours.call_args.args[0]["manhoursFilter"]["startDate_range"] == {
        "unit": "day", "quick": "this_week"
    }


async def test_task_filter_excludes_other_work_items_after_member_query():
    client = MagicMock()
    client.list_manhours = AsyncMock(return_value=workhour_payload())
    gateway = OnesGateway(async_client=client)
    assert await gateway.list_manhours("u1", task_id="other") == []
    with pytest.raises(ValueError):
        await gateway.list_manhours("u1", task_id="task-t1")
    client.list_manhours.assert_awaited_once()


@pytest.mark.parametrize(("start", "end"), [("2026-11-01", ""), ("", "2026-11-01"),
                                             ("2026-11-07", "2026-11-01"), ("20261101", "2026-11-02")])
async def test_invalid_date_range_does_not_call_client(start, end):
    client = MagicMock()
    client.list_manhours = AsyncMock()
    with pytest.raises(ValueError):
        await OnesGateway(async_client=client).list_manhours("u1", start_date=start, end_date=end)
    client.list_manhours.assert_not_awaited()


async def test_add_and_update_forward_exact_fixed_point_values():
    client = MagicMock()
    client.add_manhour = AsyncMock(return_value={"addManhour": {"key": "manhour-m1"}})
    client.update_manhour = AsyncMock(return_value={"updateManhour": {"key": "manhour-m1"}})
    gateway = OnesGateway(async_client=client)
    assert await gateway.add_manhour("u1", "t1", 1793926800, 0.01, "说明") == {"key": "manhour-m1"}
    assert await gateway.update_manhour("manhour-m1", "u1", "t1", 1793926800, 1.25, "更新") == {"key": "manhour-m1"}
    added = client.add_manhour.await_args.args[0]
    changed = client.update_manhour.await_args.args[0]
    assert added == {"mode": "simple", "owner": "u1", "task": "t1", "type": "recorded",
                     "start_time": 1793926800, "hours": 1000, "description": "说明"}
    assert changed == {**added, "hours": 125000, "description": "更新", "key": "manhour-m1"}


def test_sync_mutations_return_keys_and_preserve_description():
    client = MagicMock()
    client.add_manhour.return_value = {"addManhour": {"key": "manhour-m1"}}
    client.update_manhour.return_value = {"updateManhour": {"key": "manhour-m1"}}
    gateway = OnesGateway(sync_client=client)
    assert gateway.add_manhour_sync("u1", "t1", 1793926800, 0.01)["key"] == "manhour-m1"
    assert gateway.update_manhour_sync("manhour-m1", "u1", "t1", 1793926800, 0.02, "") == {"key": "manhour-m1"}
    assert client.update_manhour.call_args.args[0]["description"] == ""


@pytest.mark.parametrize("hours", [0, -1, 10000, 0.000001, float("nan"), float("inf"), True, "1"])
async def test_bad_hours_never_submit_a_write(hours):
    client = MagicMock()
    client.add_manhour = AsyncMock()
    with pytest.raises(ValueError):
        await OnesGateway(async_client=client).add_manhour("u1", "t1", 1793926800, hours)
    client.add_manhour.assert_not_awaited()


@pytest.mark.parametrize("args", [
    ("m1", "u1", "t1", 1793926800, 1, ""),
    ("manhour-m1", "u1", "task-t1", 1793926800, 1, ""),
    ("manhour-m1", "u1", "t1", 1793926800000, 1, ""),
])
async def test_bad_update_identity_or_time_does_not_submit(args):
    client = MagicMock()
    client.update_manhour = AsyncMock()
    with pytest.raises(ValueError):
        await OnesGateway(async_client=client).update_manhour(*args)
    client.update_manhour.assert_not_awaited()


async def test_malformed_response_and_timeout_are_not_reported_as_success_or_retried():
    client = MagicMock()
    client.add_manhour = AsyncMock(return_value={"errors": [{"message": "failure"}]})
    gateway = OnesGateway(async_client=client)
    with pytest.raises(OnesGatewayPayloadError, match="outcome may be unknown"):
        await gateway.add_manhour("u1", "t1", 1793926800, 1)
    client.add_manhour.assert_awaited_once()
    client.add_manhour.reset_mock()
    client.add_manhour.side_effect = httpx.ReadTimeout("timeout")
    with pytest.raises(OnesGatewayTimeoutError):
        await gateway.add_manhour("u1", "t1", 1793926800, 1)
    client.add_manhour.assert_awaited_once()


async def test_permission_denied_and_mismatched_update_key():
    client = MagicMock()
    request = httpx.Request("POST", "https://ones.invalid")
    response = httpx.Response(403, request=request)
    client.update_manhour = AsyncMock(side_effect=httpx.HTTPStatusError("forbidden", request=request, response=response))
    gateway = OnesGateway(async_client=client)
    with pytest.raises(OnesGatewayAuthError):
        await gateway.update_manhour("manhour-m1", "u1", "t1", 1793926800, 1, "")
    client.update_manhour.side_effect = None
    client.update_manhour.return_value = {"updateManhour": {"key": "manhour-other"}}
    with pytest.raises(OnesGatewayPayloadError, match="outcome may be unknown"):
        await gateway.update_manhour("manhour-m1", "u1", "t1", 1793926800, 1, "")


async def test_mcp_tools_delegate_and_close_gateway(monkeypatch):
    gateway = MagicMock()
    gateway.list_manhours = AsyncMock(return_value=[{"key": "manhour-m1"}])
    gateway.add_manhour = AsyncMock(return_value={"key": "manhour-m1"})
    gateway.update_manhour = AsyncMock(return_value={"key": "manhour-m1"})
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=gateway)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(server, "OnesGateway", lambda: context)
    assert await server.list_manhours("u1") == [{"key": "manhour-m1"}]
    assert await server.add_manhour("u1", "t1", 1793926800, 1) == {"key": "manhour-m1"}
    assert await server.update_manhour("manhour-m1", "u1", "t1", 1793926800, 1, "desc") == {"key": "manhour-m1"}
    gateway.list_manhours.assert_awaited_once_with("u1", start_date="", end_date="", task_id="")
    gateway.add_manhour.assert_awaited_once_with("u1", "t1", 1793926800, 1, "")
    gateway.update_manhour.assert_awaited_once_with("manhour-m1", "u1", "t1", 1793926800, 1, "desc")
    assert context.__aexit__.await_count == 3
