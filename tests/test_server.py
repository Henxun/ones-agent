"""server.py 测试 - MCP tools（纯数据提供者）"""

from dataclasses import dataclass
import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import server
from server import (
    fetch_defects,
    fetch_my_defects,
    check_new_defects,
    get_defect_detail,
    list_projects,
    search_codebase,
    push_to_wechat,
)


MOCK_DEFECTS = [
    {"uuid": "d1", "name": "Bug1", "status": {"name": "待处理"}, "priority": {"value": "高"},
     "assign": {"name": "张三"}, "issueType": {"name": "缺陷"}, "project": {"name": "P"},
     "createTime": 0, "deadline": None, "estimatedHours": 0, "subTaskCount": 0, "subTaskDoneCount": 0},
]

MOCK_PROJECTS = [
    {"uuid": "p1", "name": "Project1", "isArchive": False},
]


@pytest.fixture(autouse=True)
def _mock_deps():
    """替换 server 模块的依赖为 mock"""
    mock_ones = MagicMock()
    mock_store = MagicMock()
    mock_codebase = MagicMock()
    mock_bot = MagicMock()

    orig = (server._ones, server._store, server._codebase, server._bot)
    server._ones, server._store, server._codebase, server._bot = mock_ones, mock_store, mock_codebase, mock_bot
    yield mock_ones, mock_store, mock_codebase, mock_bot
    server._ones, server._store, server._codebase, server._bot = orig


class TestFetchDefectsTool:
    def test_fetch_all_defects(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_defects.return_value = MOCK_DEFECTS

        result = fetch_defects(limit=10)

        mock_ones.fetch_defects.assert_called_once_with(project_id=None, limit=10)
        assert result == MOCK_DEFECTS

    def test_fetch_my_defects_flag(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS

        result = fetch_defects(mine=True)

        mock_ones.fetch_my_defects.assert_called_once_with(limit=50)
        assert result == MOCK_DEFECTS


class TestFetchMyDefectsTool:
    def test_fetch_my_defects(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS

        result = fetch_my_defects()

        mock_ones.fetch_my_defects.assert_called_once()
        assert result == MOCK_DEFECTS


class TestCheckNewDefectsTool:
    def test_check_new_filters_and_updates(self, _mock_deps):
        mock_ones, mock_store, _, _ = _mock_deps
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS
        mock_store.filter_new.return_value = MOCK_DEFECTS

        result = check_new_defects(mine=True)

        mock_store.filter_new.assert_called_once_with(MOCK_DEFECTS)
        mock_store.update_check_time.assert_called_once()
        assert result == MOCK_DEFECTS

    def test_check_new_not_mine(self, _mock_deps):
        mock_ones, mock_store, _, _ = _mock_deps
        mock_ones.fetch_defects.return_value = MOCK_DEFECTS
        mock_store.filter_new.return_value = []

        result = check_new_defects(mine=False)

        mock_ones.fetch_defects.assert_called_once()
        assert result == []


class TestGetDefectDetailTool:
    def test_get_defect_detail(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_issue_detail.return_value = MOCK_DEFECTS[0]

        result = get_defect_detail("d1")

        mock_ones.fetch_issue_detail.assert_called_once_with("d1")
        assert result == MOCK_DEFECTS[0]


class TestListProjectsTool:
    def test_list_projects(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_projects.return_value = MOCK_PROJECTS

        result = list_projects()

        mock_ones.fetch_projects.assert_called_once_with(include_archived=False)
        assert result == MOCK_PROJECTS

    def test_list_projects_include_archived(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_projects.return_value = MOCK_PROJECTS

        list_projects(include_archived=True)

        mock_ones.fetch_projects.assert_called_once_with(include_archived=True)


class TestSearchCodebaseTool:
    def test_search_no_codebase(self, _mock_deps):
        _, _, _, _ = _mock_deps
        server._codebase = None

        result = search_codebase()

        assert "未配置" in result

    def test_search_by_query(self, _mock_deps):
        _, _, mock_cb, _ = _mock_deps
        mock_cb.search_keywords.return_value = {"src/app.py": "print('hi')"}

        result = search_codebase(query="login")

        mock_cb.search_keywords.assert_called_once_with(["login"], max_files=10)
        assert "src/app.py" in result

    def test_search_read_file(self, _mock_deps):
        _, _, mock_cb, _ = _mock_deps
        mock_cb.read_file.return_value = "print('hello')"

        result = search_codebase(read_file="src/main.py")

        mock_cb.read_file.assert_called_once_with("src/main.py")
        assert "print" in result

    def test_search_tree(self, _mock_deps):
        _, _, mock_cb, _ = _mock_deps
        mock_cb.tree.return_value = "src/\n  main.py"

        result = search_codebase()

        mock_cb.tree.assert_called_once_with(max_depth=3)


class TestPushToWechatTool:
    def test_push_sends_markdown(self, _mock_deps):
        _, _, _, mock_bot = _mock_deps

        result = push_to_wechat("## Alert")

        mock_bot.send_markdown.assert_called_once_with("## Alert")
        assert "已发送" in result


@pytest.mark.parametrize(
    "tool,method,args,kwargs,normalized",
    [
        ("list_project_issue_types", "list_project_issue_types", ("p1",), {}, True),
        ("list_iterations", "list_iterations", ("p1",), {}, False),
        ("list_team_members", "list_team_members", (), {"uuids": ["u1"]}, False),
        ("list_role_members", "list_role_members", ("p1",), {}, False),
        ("list_comments", "list_comments", ("d1",), {"page_size": 200}, False),
        ("get_requirement_detail", "get_normalized_requirement", ("r1",), {}, True),
        ("get_wiki_page", "get_wiki_snapshot", ("https://ones/wiki/",), {}, True),
    ],
)
async def test_unified_ones_tools(monkeypatch, tool, method, args, kwargs, normalized):
    @dataclass
    class Record:
        id: str = "item1"

    gateway = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=gateway)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(server, "OnesGateway", lambda: context)
    value = Record() if normalized else {"id": "item1"}
    is_detail = tool.startswith("get_")
    getattr(gateway, method).return_value = None
    setattr(gateway, method, AsyncMock(return_value=value if is_detail else [value]))

    result = await getattr(server, tool)(*args, **kwargs)

    getattr(gateway, method).assert_awaited_once_with(*args, **kwargs)
    assert result == ({"id": "item1"} if is_detail else [{"id": "item1"}])
    context.__aexit__.assert_awaited_once()


async def test_requirement_filters_and_cleanup_on_failure(monkeypatch):
    gateway = MagicMock()
    gateway.list_requirements = AsyncMock(side_effect=ValueError("invalid limit"))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=gateway)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(server, "OnesGateway", lambda: context)
    with pytest.raises(ValueError, match="invalid limit"):
        await server.fetch_requirements("p1", "type1", limit=101)
    gateway.list_requirements.assert_awaited_once_with(
        project_id="p1", issue_type_id="type1", sprint_id=None,
        assignee=None, status_ids=None, limit=101,
    )
    context.__aexit__.assert_awaited_once()
    gateway.list_requirements.reset_mock()
    with pytest.raises(ValueError, match="必填"):
        await server.fetch_requirements("p1", " ")
    gateway.list_requirements.assert_not_awaited()


async def test_requirement_results_are_serializable(monkeypatch):
    from src.contracts import RequirementRecord

    gateway = MagicMock()
    gateway.list_requirements = AsyncMock(return_value=[RequirementRecord(requirement_id="r1")])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=gateway)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(server, "OnesGateway", lambda: context)

    result = await server.fetch_requirements(
        "p1", "type1", sprint_id="s1", assignee="u1", status_ids=["status1"], limit=10,
    )

    assert json.loads(json.dumps(result))[0]["requirement_id"] == "r1"
    gateway.list_requirements.assert_awaited_once_with(
        project_id="p1", issue_type_id="type1", sprint_id="s1",
        assignee="u1", status_ids=["status1"], limit=10,
    )
    context.__aexit__.assert_awaited_once()


def test_mcp_contract_names_parameters_and_documentation_match():
    root = Path(server.__file__).parent
    tree = ast.parse((root / "server.py").read_text(encoding="utf-8"))
    definitions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(ast.unparse(decorator) == "mcp.tool" for decorator in node.decorator_list)
    }
    schema = json.loads((root / "tools.json").read_text(encoding="utf-8"))
    functions = {item["function"]["name"]: item["function"] for item in schema["tools"]}
    assert definitions.keys() == functions.keys()
    document = (root / "skill.md").read_text(encoding="utf-8")
    for name, node in definitions.items():
        parameters = functions[name]["parameters"]
        assert set(parameters["properties"]) == {arg.arg for arg in node.args.args}
        required_count = len(node.args.args) - len(node.args.defaults)
        assert parameters["required"] == [arg.arg for arg in node.args.args[:required_count]]
        assert name in document
