from __future__ import annotations

import logging
from pathlib import Path
import sys
from tempfile import TemporaryFile
from unittest.mock import AsyncMock, MagicMock

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from src.services.ones_gateway import OnesGateway, OnesGatewayNotFoundError, OnesGatewayPayloadError


UUID = "HSiyR3b7ypVof4CM"
KEY = f"task-{UUID}"


def requirement_detail() -> dict:
    return {
        "uuid": UUID, "key": KEY, "number": 42, "name": "需求",
        "project": {"uuid": "project1", "name": "项目"},
        "status": {"uuid": "status1", "name": "进行中"},
        "assign": {"uuid": "user1", "name": "负责人", "avatar": ""},
        "description": "需求描述",
    }


async def test_requirement_list_uses_web_key_and_accepts_empty_avatar():
    client = MagicMock()
    client.fetch_defects = AsyncMock(return_value=[{"uuid": UUID, "key": KEY}])
    client.fetch_issue_detail = AsyncMock(return_value=requirement_detail())
    gateway = OnesGateway(async_client=client)
    rows = await gateway.list_requirements(project_id="project1", issue_type_id="type1", limit=3)
    assert len(rows) == 1 and rows[0].requirement_id == UUID
    assert rows[0].assignee.avatar == ""
    client.fetch_issue_detail.assert_awaited_once_with(KEY)


async def test_requirement_list_rejects_detail_for_another_item():
    client = MagicMock()
    client.fetch_defects = AsyncMock(return_value=[{"uuid": UUID, "key": KEY}])
    client.fetch_issue_detail = AsyncMock(return_value={**requirement_detail(), "uuid": "different", "key": "task-different"})
    with pytest.raises(OnesGatewayNotFoundError):
        await OnesGateway(async_client=client).list_requirements(project_id="project1", issue_type_id="type1")


@pytest.mark.parametrize("issue_id", [UUID, KEY])
async def test_requirement_detail_accepts_uuid_and_graphql_key(issue_id):
    client = MagicMock()
    client.fetch_issue_detail = AsyncMock(return_value=requirement_detail())
    result = await OnesGateway(async_client=client).get_normalized_requirement(issue_id)
    assert result.requirement_id == UUID and result.assignee.avatar == ""
    client.fetch_issue_detail.assert_awaited_once_with(KEY)


def test_sync_requirement_detail_accepts_uuid():
    client = MagicMock()
    client.fetch_issue_detail.return_value = requirement_detail()
    result = OnesGateway(sync_client=client).get_normalized_requirement_sync(UUID)
    assert result.requirement_id == UUID and result.assignee.avatar == ""
    client.fetch_issue_detail.assert_called_once_with(KEY)


def test_requirement_avatar_rejects_non_string():
    payload = requirement_detail()
    payload["assign"]["avatar"] = {"unexpected": True}
    with pytest.raises(OnesGatewayPayloadError, match="assignee avatar"):
        OnesGateway().normalize_requirement(payload)


async def test_stdio_server_does_not_write_logs_into_jsonrpc_stdout(caplog):
    repo = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable, args=[str(repo / "server.py")], cwd=str(repo),
        env={"ONES_EMAIL": "", "ONES_PASSWORD": "", "ONES_BASE_URL": "http://127.0.0.1:1", "PYTHONUTF8": "1"},
    )
    with caplog.at_level(logging.ERROR), TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("fetch_requirements", {
                    "project_id": "p1", "issue_type_id": "type1", "limit": 0,
                })
                assert result.isError
        stderr.seek(0)
        assert "ones_gateway_async_client_init" in stderr.read()
    assert "Failed to parse JSONRPC message from server" not in caplog.text
