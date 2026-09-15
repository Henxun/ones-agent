from __future__ import annotations

from unittest.mock import Mock
import requests
from pydantic import ValidationError

import httpx
import pytest
import respx

from config.settings import OnesSettings
from src.contracts import CommentRecord
from src.integrations.ones import OnesClient, OnesPaginationError, OnesPayloadError
from src.integrations.ones_api import OnesAsyncClient
from src.services.ones_gateway import OnesGateway, OnesGatewayPayloadError


COMMENT_PATH = "/project/api/project/team/{team_id}/task/{item_id}/comment"
MESSAGES_PATH = "/project/api/project/team/{team_id}/task/{item_id}/messages"


def streamed(payload) -> Mock:
    import json
    body = json.dumps(payload).encode("utf-8")
    response = Mock(headers={"Content-Length": str(len(body))})
    response.raise_for_status.return_value = None
    response.iter_content.return_value = iter((body,))
    return response


def test_sync_list_comments_fails_closed_without_configured_endpoint() -> None:
    client = OnesClient(base_url="http://ones.test", email="", password="", team_id="team")
    with pytest.raises(RuntimeError, match="not configured"):
        client.list_comments("item")


def test_gateway_managed_clients_receive_explicit_comment_list_endpoint() -> None:
    settings = OnesSettings(
        base_url="http://ones.test", email="", password="", team_id="team",
        comment_list_path_template=COMMENT_PATH, _env_file=None,
    )
    gateway = OnesGateway(settings=settings)
    assert gateway._get_sync_client().comment_list_path_template == COMMENT_PATH


def test_sync_list_comments_uses_get_and_strict_cursor_pagination() -> None:
    client = OnesClient(base_url="http://ones.test", email="", password="", team_id="team", comment_list_path_template=COMMENT_PATH)
    first = streamed({"comments":[{"uuid":"c1","message":"one"}],"pageInfo":{"hasNextPage":True,"endCursor":"next"}})
    second = streamed({"comments":[{"uuid":"c2","message":"two"}],"pageInfo":{"hasNextPage":False,"endCursor":""}})
    client.session.get = Mock(side_effect=[first, second])

    result = client.list_comments("item", page_size=10)

    assert [entry["uuid"] for entry in result] == ["c1", "c2"]
    assert client.session.get.call_count == 2
    assert all(call.kwargs["params"]["limit"] == 10 for call in client.session.get.call_args_list)
    assert all(call.kwargs["timeout"] == 30.0 for call in client.session.get.call_args_list)


def test_sync_messages_endpoint_matches_browser_request_without_query_params() -> None:
    client = OnesClient(
        base_url="http://ones.test",
        email="",
        password="",
        team_id="team",
        comment_list_path_template=MESSAGES_PATH,
    )
    client.session.get = Mock(
        return_value=streamed(
            {"messages": [{"uuid": "m1", "message": "reopened feedback"}]}
        )
    )

    result = client.list_comments("item")

    assert result == [{"uuid": "m1", "message": "reopened feedback"}]
    assert client.session.get.call_args.args[0].endswith(
        "/project/api/project/team/team/task/item/messages"
    )
    assert client.session.get.call_args.kwargs["params"] is None


def test_sync_comment_pagination_rejects_nonadvancing_cursor() -> None:
    client = OnesClient(base_url="http://ones.test", email="", password="", team_id="team", comment_list_path_template=COMMENT_PATH)
    response = streamed({"comments":[],"pageInfo":{"hasNextPage":True,"endCursor":""}})
    client.session.get = Mock(return_value=response)
    with pytest.raises(OnesPaginationError):
        client.list_comments("item")


def test_sync_add_comment_rejects_non_text_before_post() -> None:
    client = OnesClient(base_url="http://ones.test", email="", password="", team_id="team")
    client.session.post = Mock()
    with pytest.raises(ValueError):
        client.add_comment("item", 123)  # type: ignore[arg-type]
    client.session.post.assert_not_called()


def test_sync_comment_limits_pages_payload_and_explicit_timeouts() -> None:
    client = OnesClient(
        base_url="http://ones.test", email="", password="", team_id="team",
        comment_list_path_template=COMMENT_PATH, comment_timeout_seconds=4.5,
        comment_max_pages=2, comment_max_payload_bytes=10,
    )
    huge = Mock(headers={"Content-Length":"11"}, content=b"x" * 11)
    huge.raise_for_status.return_value = None
    huge.json.side_effect = AssertionError("oversized response must not be decoded")
    client.session.get = Mock(return_value=huge)
    with pytest.raises(OnesPayloadError, match="size limit"):
        client.list_comments("item")
    assert client.session.get.call_args.kwargs["timeout"] == 4.5

    client.comment_max_payload_bytes = 1000
    pages = [
        streamed({"comments": [], "pageInfo": {"hasNextPage": True, "endCursor": cursor}})
        for cursor in ("a", "b")
    ]
    client.session.get = Mock(side_effect=pages)
    with pytest.raises(OnesPaginationError, match="limit"):
        client.list_comments("item")
    assert client.session.get.call_count == 2


def test_sync_comment_get_and_post_timeout_are_single_attempts() -> None:
    client = OnesClient(
        base_url="http://ones.test", email="", password="", team_id="team",
        comment_list_path_template=COMMENT_PATH, comment_timeout_seconds=1.25,
    )
    client.session.get = Mock(side_effect=requests.Timeout("slow"))
    with pytest.raises(requests.Timeout):
        client.list_comments("item")
    assert client.session.get.call_count == 1
    client.session.post = Mock(side_effect=requests.Timeout("uncertain"))
    with pytest.raises(requests.Timeout):
        client.add_comment("item", "hello")
    assert client.session.post.call_count == 1
    assert client.session.post.call_args.kwargs["timeout"] == 1.25


@pytest.mark.parametrize("method", ["get", "post"])
def test_sync_streaming_closes_before_chunked_decoded_body_is_materialized(method) -> None:
    class Response:
        headers = {}
        closed = False
        yielded = 0
        def raise_for_status(self): pass
        def iter_content(self, **kwargs):
            for chunk in (b"1234", b"5678", b"must-not-be-read"):
                self.yielded += 1
                yield chunk
        def close(self): self.closed = True

    response = Response()
    client = OnesClient(
        base_url="http://ones.test", email="", password="", team_id="team",
        comment_list_path_template=COMMENT_PATH, comment_max_payload_bytes=6,
    )
    setattr(client.session, method, Mock(return_value=response))
    with pytest.raises(OnesPayloadError, match="size limit"):
        client.list_comments("item") if method == "get" else client.add_comment("item", "text")
    assert response.closed is True
    assert response.yielded == 2
    call = getattr(client.session, method).call_args
    assert call.kwargs["stream"] is True


def test_default_gateway_sync_client_uses_environment_comment_limits(monkeypatch) -> None:
    monkeypatch.setenv("ONES_EMAIL", "")
    monkeypatch.setenv("ONES_PASSWORD", "")
    monkeypatch.setenv("ONES_COMMENT_LIST_PATH_TEMPLATE", COMMENT_PATH)
    monkeypatch.setenv("ONES_COMMENT_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("ONES_COMMENT_MAX_PAGES", "3")
    monkeypatch.setenv("ONES_COMMENT_MAX_COMMENTS", "9")
    monkeypatch.setenv("ONES_COMMENT_MAX_PAYLOAD_BYTES", "1234")
    client = OnesGateway()._get_sync_client()
    assert client.comment_list_path_template == COMMENT_PATH
    assert client.comment_timeout_seconds == 7.5
    assert client.comment_max_pages == 3
    assert client.comment_max_comments == 9
    assert client.comment_max_payload_bytes == 1234


@pytest.mark.parametrize(
    "field",
    ["comment_timeout_seconds", "comment_max_pages", "comment_max_comments", "comment_max_payload_bytes"],
)
def test_comment_resource_limits_must_be_positive(field) -> None:
    with pytest.raises(ValidationError):
        OnesSettings(**{field: 0}, _env_file=None)


@pytest.mark.asyncio
async def test_async_add_comment_rejects_invalid_utf8_before_client_init() -> None:
    settings = OnesSettings(base_url="http://ones.test",email="",password="",team_id="team",_env_file=None)
    client = OnesAsyncClient(settings)
    with pytest.raises(ValueError):
        await client.add_comment("item", "\ud800")
    assert client._client is None


@respx.mock
@pytest.mark.asyncio
async def test_async_list_comments_uses_only_get() -> None:
    settings = OnesSettings(base_url="http://ones.test",email="",password="",team_id="team",_env_file=None)
    client = OnesAsyncClient(settings, comment_list_path_template=COMMENT_PATH)
    route = respx.get("http://ones.test/project/api/project/team/team/task/item/comment", params={"limit":200,"after":""}).mock(return_value=httpx.Response(200,json={"comments":[{"uuid":"c1","message":"one"}],"pageInfo":{"hasNextPage":False,"endCursor":""}}))
    result = await client.list_comments("item")
    assert route.called and result[0]["uuid"] == "c1"
    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_async_messages_endpoint_accepts_top_level_browser_payload() -> None:
    settings = OnesSettings(
        base_url="http://ones.test", email="", password="", team_id="team", _env_file=None
    )
    client = OnesAsyncClient(settings, comment_list_path_template=MESSAGES_PATH)
    route = respx.get(
        "http://ones.test/project/api/project/team/team/task/item/messages"
    ).mock(return_value=httpx.Response(200, json=[{"uuid": "m1", "message": "note"}]))

    result = await client.list_comments("item")

    assert route.called
    assert result == [{"uuid": "m1", "message": "note"}]
    await client.close()


@respx.mock
@pytest.mark.asyncio
async def test_async_comment_page_and_payload_limits_fail_closed() -> None:
    settings = OnesSettings(
        base_url="http://ones.test", email="", password="", team_id="team",
        comment_list_path_template=COMMENT_PATH, comment_max_pages=1,
        comment_max_payload_bytes=10_000, _env_file=None,
    )
    client = OnesAsyncClient(settings)
    route = respx.get(
        "http://ones.test/project/api/project/team/team/task/item/comment"
    ).mock(return_value=httpx.Response(200, json={
        "comments": [], "pageInfo": {"hasNextPage": True, "endCursor": "next"}
    }))
    with pytest.raises(Exception, match="pagination limit"):
        await client.list_comments("item")
    assert route.call_count == 1
    await client.close()

    tiny = settings.model_copy(update={"comment_max_payload_bytes": 1})
    client = OnesAsyncClient(tiny)
    respx.get(
        "http://ones.test/project/api/project/team/team/task/item/comment"
    ).mock(return_value=httpx.Response(200, content=b"{}", headers={"Content-Type":"application/json"}))
    with pytest.raises(Exception, match="size limit"):
        await client.list_comments("item")
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_async_streaming_acloses_before_chunked_body_is_materialized(method) -> None:
    class Stream(httpx.AsyncByteStream):
        closed = False
        yielded = 0
        async def __aiter__(self):
            for chunk in (b"1234", b"5678", b"must-not-be-read"):
                self.yielded += 1
                yield chunk
        async def aclose(self): self.closed = True

    stream = Stream()
    def handler(request):
        return httpx.Response(200, stream=stream)
    settings = OnesSettings(
        base_url="http://ones.test", email="", password="", team_id="team",
        comment_list_path_template=COMMENT_PATH, comment_max_payload_bytes=6,
        _env_file=None,
    )
    client = OnesAsyncClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._ready = True
    with pytest.raises(Exception, match="size limit"):
        await (client.list_comments("item") if method == "get" else client.add_comment("item", "text"))
    assert stream.closed is True
    assert stream.yielded == 2
    await client.close()

@pytest.mark.asyncio
async def test_gateway_normalizes_comments_and_add_response() -> None:
    class Client:
        async def list_comments(self, item_id, **kwargs):
            return [{"uuid":"c1","message":"hello"}]
        async def add_comment(self, item_id, text):
            return {"id":"c2"}
    gateway = OnesGateway(async_client=Client())
    assert await gateway.list_comments("item") == [{"id":"c1","text":"hello"}]
    assert await gateway.add_comment("item", "world") == {"id":"c2","text":"world"}


@pytest.mark.asyncio
async def test_gateway_rejects_malformed_comment_payload() -> None:
    class Client:
        async def list_comments(self, item_id, **kwargs):
            return [{"uuid":"c1","message":123}]
    with pytest.raises(OnesGatewayPayloadError):
        await OnesGateway(async_client=Client()).list_comments("item")


def test_gateway_builds_typed_defect_comment_snapshot() -> None:
    class Client:
        def list_comments(self, item_id, **kwargs):
            assert item_id == "item"
            return [
                {
                    "message_uuid": "m1",
                    "content": {"text": "please recheck the offline path"},
                    "sender": {"uuid": "u1", "name": "Reviewer"},
                    "createTime": 1789444886,
                },
                {"uuid": "system-event"},
            ]

    comments = OnesGateway(sync_client=Client()).list_defect_comments_sync("item")

    assert comments == [
        CommentRecord(
            id="m1",
            text="please recheck the offline path",
            author_id="u1",
            author_name="Reviewer",
            created_at="1789444886",
        )
    ]
