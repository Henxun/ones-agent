"""Phase 2 测试 - ONES 异步客户端 + Webhook + 任务队列"""

import hashlib
import hmac
import json

import pytest
import httpx
import respx
from structlog.testing import capture_logs

from config.settings import OnesSettings, AgentSettings
from src.integrations.ones_api import (
    GQL_FETCH_TASKS,
    OnesAsyncClient,
    OnesGraphQLResponseError,
    OnesPaginationError,
)
from src.core.queue import TaskQueue


# ── OnesAsyncClient 测试 ──────────────────────────────────


class TestOnesAsyncClientGraphQL:
    @pytest.mark.parametrize("bad_id", ["", ".", "..", "a/b", "a\\b", "a%b", "a?b", "a#b", "a b", "\nbad", "中文"])
    @respx.mock
    @pytest.mark.asyncio
    async def test_wiki_read_rejects_unsafe_segments_without_request(self, bad_id):
        settings = OnesSettings(base_url="https://ones.test", team_id="team-1", email="", password="", _env_file=None)
        client = OnesAsyncClient(settings)

        with pytest.raises(ValueError, match="Wiki"):
            await client.fetch_wiki_page("space-1", bad_id)
        assert not respx.calls
        await client.close()

    def test_fetch_tasks_query_includes_sprint_identity(self):
        assert "sprint { uuid name }" in GQL_FETCH_TASKS

    @respx.mock
    @pytest.mark.asyncio
    async def test_wiki_read_methods_reuse_async_client_and_use_exact_paths(self):
        settings = OnesSettings(base_url="https://ones.test", team_id="team-1", email="", password="", _env_file=None)
        client = OnesAsyncClient(settings)
        routes = [
            respx.get("https://ones.test/wiki/api/wiki/team/team-1/space/space-1/page/page-1").mock(
                return_value=httpx.Response(200, json={"content": "body"}),
            ),
            respx.get("https://ones.test/wiki/api/wiki/team/team-1/page/page-1/detail").mock(
                return_value=httpx.Response(200, json={"title": "Page"}),
            ),
            respx.get("https://ones.test/wiki/api/wiki/team/team-1/space/space-1/pages_with_history").mock(
                return_value=httpx.Response(200, json={"pages": []}),
            ),
        ]

        with capture_logs() as logs:
            assert await client.fetch_wiki_page("space-1", "page-1") == {"content": "body"}
            underlying = client._client
            assert await client.fetch_wiki_page_info("page-1") == {"title": "Page"}
            assert await client.fetch_wiki_pages_with_history("space-1") == {"pages": []}
        assert client._client is underlying
        assert all(route.called for route in routes)
        allowed = {"event", "log_level", "team_id", "space_id", "page_id", "status"}
        wiki_logs = [entry for entry in logs if entry.get("event") == "ones_wiki_read"]
        assert len(wiki_logs) == 3 and all(set(entry) <= allowed for entry in wiki_logs)
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_wiki_read_rejects_non_mapping_json(self):
        settings = OnesSettings(base_url="https://ones.test", team_id="team-1", email="", password="", _env_file=None)
        client = OnesAsyncClient(settings)
        respx.get("https://ones.test/wiki/api/wiki/team/team-1/page/page-1/detail").mock(
            return_value=httpx.Response(200, json=["not", "mapping"]),
        )

        from src.integrations.ones_api import OnesPayloadError
        with pytest.raises(OnesPayloadError, match="mapping"):
            await client.fetch_wiki_page_info("page-1")
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_graphql_http_200_errors_are_not_treated_as_success(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            return_value=httpx.Response(
                200,
                json={"data": None, "errors": [{"message": "invalid filter", "path": ["tasks"]}]},
            ),
        )

        with pytest.raises(OnesGraphQLResponseError, match="invalid filter"):
            await client._graphql("query Tasks { tasks { uuid } }", {})

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_defects(self):
        settings = OnesSettings(
            base_url="http://ones.test",
            email="",
            password="",
            team_id="team1",
            project_id="proj1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        gql_resp = {
            "data": {
                "buckets": [
                    {"key": "待处理", "tasks": [
                        {"uuid": "d1", "name": "Bug1", "status": {"name": "待处理"},
                         "priority": {"value": "高"}, "assign": {"name": "张三"},
                         "issueType": {"name": "缺陷"}, "project": {"name": "P"},
                         "createTime": 0, "deadline": None, "estimatedHours": 0,
                         "subTaskCount": 0, "subTaskDoneCount": 0},
                    ], "pageInfo": {"hasNextPage": False}}
                ]
            }
        }
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            return_value=httpx.Response(200, json=gql_resp)
        )

        result = await client.fetch_defects()
        assert len(result) == 1
        assert result[0]["uuid"] == "d1"
        assert result[0]["_status_group"] == "待处理"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_defects_forwards_status_in(self):
        settings = OnesSettings(
            base_url="http://ones.test",
            email="",
            password="",
            team_id="team1",
            project_id="proj1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        captured: dict[str, object] = {}

        def handler(request: httpx.Request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"data": {"buckets": []}})

        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(side_effect=handler)

        await client.fetch_defects(status_in=["status-uuid-1"])

        variables = captured["variables"]
        assert variables["filterGroup"][0]["status_in"] == ["status-uuid-1"]
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_defects_paginates_deduplicates_and_preserves_status(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        responses = iter([
            httpx.Response(200, json={"data": {"buckets": [{
                "key": "tasks",
                "tasks": [
                    {"uuid": "d1", "status": {"uuid": "s1", "category": "todo"}},
                    {"uuid": "d2", "status": {"uuid": "s2", "category": "doing"}},
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
            }]}}),
            httpx.Response(200, json={"data": {"buckets": [{
                "key": "tasks",
                "tasks": [
                    {"uuid": "d2", "status": {"uuid": "s2", "category": "doing"}},
                    {"uuid": "d3", "status": {"uuid": "s3", "category": "pending"}},
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
            }]}}),
        ])
        captured: list[dict] = []

        def handler(request: httpx.Request):
            captured.append(json.loads(request.content)["variables"])
            return next(responses)

        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(side_effect=handler)

        defects = await client.fetch_defects(limit=10, page_size=2)

        assert [item["uuid"] for item in defects] == ["d1", "d2", "d3"]
        assert defects[2]["status"]["category"] == "pending"
        assert [item["pagination"] for item in captured] == [
            {"limit": 2, "after": "", "preciseCount": True},
            {"limit": 2, "after": "cursor-1", "preciseCount": True},
        ]
        assert all(item["groupBy"] == {"tasks": {}} for item in captured)
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_defects_rejects_unstable_next_page_cursor(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            return_value=httpx.Response(200, json={"data": {"buckets": [{
                "key": "tasks",
                "tasks": [{"uuid": "d1", "status": {"uuid": "s1"}}],
                "pageInfo": {"hasNextPage": True, "endCursor": ""},
            }]}}),
        )

        with pytest.raises(OnesPaginationError, match="cursor"):
            await client.fetch_defects(limit=10, page_size=2)
        await client.close()

    @pytest.mark.parametrize(
        "bucket",
        [
            {"key": "tasks", "tasks": [{"uuid": "d1"}]},
            {"key": "tasks", "tasks": [{"uuid": "d1"}], "pageInfo": []},
            {"key": "tasks", "tasks": [{"uuid": "d1"}], "pageInfo": {"endCursor": "a"}},
            {
                "key": "tasks",
                "tasks": [{"uuid": "d1"}],
                "pageInfo": {"hasNextPage": "false", "endCursor": "a"},
            },
        ],
    )
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_defects_rejects_indeterminate_page_info(self, bucket):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            return_value=httpx.Response(200, json={"data": {"buckets": [bucket]}}),
        )

        with pytest.raises(OnesPaginationError, match="pageInfo|hasNextPage"):
            await client.fetch_defects(limit=10, page_size=2)
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_defects_rejects_cursor_cycles(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        responses = iter([
            httpx.Response(200, json={"data": {"buckets": [{
                "key": "tasks", "tasks": [{"uuid": uuid}],
                "pageInfo": {"hasNextPage": True, "endCursor": cursor},
            }]}})
            for uuid, cursor in (("d1", "a"), ("d2", "b"), ("d3", "a"))
        ])
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            side_effect=lambda request: next(responses),
        )

        with pytest.raises(OnesPaginationError, match="cursor"):
            await client.fetch_defects(limit=10, page_size=2)
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_projects(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        gql_resp = {
            "data": {
                "buckets": [
                    {"key": "active", "projects": [
                        {"uuid": "p1", "name": "Project1", "isArchive": False},
                    ]}
                ]
            }
        }
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            return_value=httpx.Response(200, json=gql_resp)
        )

        result = await client.fetch_projects()
        assert len(result) == 1
        assert result[0]["name"] == "Project1"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_add_comment(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        respx.post("http://ones.test/project/api/project/team/team1/task/item1/comment").mock(
            return_value=httpx.Response(200, json={"id": "c1"})
        )

        result = await client.add_comment("item1", "分析完成")
        assert result["id"] == "c1"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_status(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        respx.patch("http://ones.test/project/api/project/team/team1/task/item1").mock(
            return_value=httpx.Response(200, json={"uuid": "item1", "status": "done"})
        )

        result = await client.update_status("item1", "done")
        assert result["status"] == "done"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_team_members_uses_post(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        client._org_uuid = "org1"

        respx.post("http://ones.test/project/api/project/organization/org1/users?team_uuid=team1").mock(
            return_value=httpx.Response(200, json=[{"uuid": "u1", "name": "Alice"}])
        )

        result = await client.fetch_team_members()
        assert result == [{"uuid": "u1", "name": "Alice"}]
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_role_members_uses_project_endpoint(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        respx.get("http://ones.test/project/api/project/team/team1/project/proj1/role_members").mock(
            return_value=httpx.Response(200, json=[{"uuid": "role-member-1"}])
        )

        result = await client.fetch_role_members("proj1")
        assert result == [{"uuid": "role-member-1"}]
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_task_status_configs_uses_project_filter(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        route = respx.post("http://ones.test/project/api/project/team/team1/task_statuses").mock(
            return_value=httpx.Response(
                200,
                json={"task_status_configs": [{"project_uuid": "proj1", "status_uuid": "status-1"}]},
            ),
        )

        result = await client.fetch_task_status_configs(["proj1"])

        assert result == [{"project_uuid": "proj1", "status_uuid": "status-1"}]
        assert json.loads(route.calls[0].request.content) == {"project_uuids": ["proj1"]}
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_issue_type_configs_returns_only_requested_project(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        route = respx.post(
            "http://ones.test/project/api/project/team/team1/stamps/data"
            "?t=issue_type,issue_type_config,project"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "issue_type_config": {
                        "issue_type_configs": [
                            {"project_uuid": "proj1", "issue_type_uuid": "type-1"},
                            {"scope": "proj2", "issue_type_uuid": "type-2"},
                        ]
                    }
                },
            )
        )

        result = await client.fetch_issue_type_configs("proj1")

        assert result == [{"project_uuid": "proj1", "issue_type_uuid": "type-1"}]
        assert json.loads(route.calls[0].request.content) == {
            "issue_type": 0,
            "issue_type_config": 0,
            "project": 0,
        }
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_task_status_definitions_uses_get(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        respx.get("http://ones.test/project/api/project/team/team1/task_statuses").mock(
            return_value=httpx.Response(
                200,
                json={"task_statuses": [{"uuid": "status-1", "name": "待处理"}]},
            ),
        )

        result = await client.fetch_task_status_definitions()

        assert result == [{"uuid": "status-1", "name": "待处理"}]
        await client.close()


# ── Webhook 测试 ──────────────────────────────────────────


class TestWebhook:
    @pytest.mark.asyncio
    async def test_webhook_accepts_valid_payload(self):
        from httpx import ASGITransport, AsyncClient
        from main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/webhook/ones", json={
                "work_item_id": "item-123",
                "type": "defect",
                "status_change": "new",
            })
        assert resp.status_code == 200
        assert resp.json()["work_item_id"] == "item-123"
        assert resp.json()["status"] == "queued"

    @pytest.mark.asyncio
    async def test_webhook_rejects_missing_id(self):
        from httpx import ASGITransport, AsyncClient
        from main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/webhook/ones", json={"type": "defect"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_webhook_validates_signature(self, monkeypatch):
        monkeypatch.setenv("AGENT_WEBHOOK_SECRET", "test-secret")
        from config.settings import AgentSettings
        from main import app, settings

        settings.agent = AgentSettings(webhook_secret="test-secret", _env_file=None)

        from httpx import ASGITransport, AsyncClient

        body = json.dumps({"work_item_id": "item-1", "type": "defect"}).encode()
        sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/webhook/ones",
                content=body,
                headers={"Content-Type": "application/json", "X-ONES-Signature": sig},
            )
        assert resp.status_code == 200

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/webhook/ones",
                content=body,
                headers={"Content-Type": "application/json", "X-ONES-Signature": "bad"},
            )
        assert resp.status_code == 401

        settings.agent = AgentSettings(_env_file=None)


# ── TaskQueue 测试 ──────────────────────────────────────────


class TestTaskQueue:
    @pytest.mark.asyncio
    async def test_enqueue_and_process(self):
        results = []
        queue = TaskQueue(max_workers=1)

        async def task(n):
            results.append(n)

        queue.start()
        await queue.enqueue(task(1))
        await queue.enqueue(task(2))
        import asyncio
        await asyncio.sleep(0.5)
        assert 1 in results
        assert 2 in results
        await queue.stop()

    @pytest.mark.asyncio
    async def test_queue_handles_errors(self):
        results = []
        queue = TaskQueue(max_workers=1)

        async def bad_task():
            raise ValueError("boom")

        async def good_task():
            results.append("ok")

        queue.start()
        await queue.enqueue(bad_task())
        await queue.enqueue(good_task())
        import asyncio
        await asyncio.sleep(0.5)
        assert "ok" in results
        await queue.stop()
