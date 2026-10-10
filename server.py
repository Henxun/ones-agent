"""ONES Defect Agent - MCP Server（纯数据提供者 + 动作执行）

不包含 LLM 分析能力，由外接 Agent 自行分析缺陷。

接入方式:
  1. MCP 兼容 Agent (Claude Desktop, Cursor, Windsurf, etc.):
     配置 mcpServers: {"ones-defect": {"command": "uv", "args": ["run", "python", "server.py"]}}

  2. OpenAI Function Calling Agent (Codex, etc.):
     读取 tools.json 获取 function definitions，通过 HTTP API 调用

  3. 任意 Agent:
     读取 skill.md 获取工具说明和工作流指引
"""

from __future__ import annotations

from dataclasses import asdict

from fastmcp import FastMCP

from src.integrations.ones import OnesClient
from src.integrations.codebase import Codebase
from src.integrations.wechat import WeChatBot
from src.core.store import Store
from src.services.ones_gateway import OnesGateway
from config import CODEBASE_PATH, REPO_URL, REPO_BRANCH

_ones = OnesClient()
_store = Store()
_codebase = Codebase(path=CODEBASE_PATH or None, repo_url=REPO_URL or None, branch=REPO_BRANCH) if CODEBASE_PATH or REPO_URL else None
_bot = WeChatBot()

mcp = FastMCP("ONES Defect Agent")


# 新 ONES 能力统一复用网关；每次调用释放网络客户端。
@mcp.tool
async def list_project_issue_types(project_id: str) -> list[dict]:
    """获取项目启用的工作项类型；使用返回的精确 ID 查询需求。"""
    async with OnesGateway() as gateway:
        return [asdict(item) for item in await gateway.list_project_issue_types(project_id)]


@mcp.tool
async def list_iterations(project_id: str) -> list[dict]:
    """获取 ONES 项目的迭代列表。"""
    async with OnesGateway() as gateway:
        return await gateway.list_iterations(project_id)


@mcp.tool
async def list_team_members(uuids: list[str] | None = None) -> list[dict]:
    """获取团队成员，可按精确用户 UUID 筛选。"""
    async with OnesGateway() as gateway:
        return await gateway.list_team_members(uuids=uuids)


@mcp.tool
async def list_role_members(project_id: str) -> list[dict]:
    """获取 ONES 项目角色成员。"""
    async with OnesGateway() as gateway:
        return await gateway.list_role_members(project_id)


@mcp.tool
async def fetch_requirements(
    project_id: str,
    issue_type_id: str,
    sprint_id: str = "",
    assignee: str = "",
    status_ids: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """按项目和精确需求类型 ID 查询需求，最多返回 100 条。"""
    if not project_id.strip() or not issue_type_id.strip():
        raise ValueError("project_id 和 issue_type_id 必填")
    async with OnesGateway() as gateway:
        items = await gateway.list_requirements(
            project_id=project_id, issue_type_id=issue_type_id,
            sprint_id=sprint_id or None, assignee=assignee or None,
            status_ids=status_ids, limit=limit,
        )
        return [asdict(item) for item in items]


@mcp.tool
async def get_requirement_detail(issue_id: str) -> dict:
    """获取归一化需求详情，包含关联 Wiki 引用。"""
    async with OnesGateway() as gateway:
        return asdict(await gateway.get_normalized_requirement(issue_id))


@mcp.tool
async def list_comments(item_id: str, page_size: int = 200) -> list[dict[str, str]]:
    """读取工作项评论，不写入 ONES。"""
    async with OnesGateway() as gateway:
        return await gateway.list_comments(item_id, page_size=page_size)


@mcp.tool
async def get_wiki_page(url: str) -> dict:
    """读取配置的 ONES 团队 Wiki URL，返回正文、版本与内容摘要。"""
    async with OnesGateway() as gateway:
        return asdict(await gateway.get_wiki_snapshot(url))


# ── 缺陷数据 ──────────────────────────────────────────

@mcp.tool
def fetch_defects(limit: int = 50, project_id: str = "", mine: bool = False) -> list[dict]:
    """获取 ONES 项目缺陷列表。mine=True 时仅获取分配给当前用户的缺陷。"""
    if mine:
        return _ones.fetch_my_defects(limit=limit)
    return _ones.fetch_defects(project_id=project_id or None, limit=limit)


@mcp.tool
def fetch_my_defects() -> list[dict]:
    """获取当前登录用户被分配的缺陷列表"""
    return _ones.fetch_my_defects()


@mcp.tool
def check_new_defects(mine: bool = True) -> list[dict]:
    """增量检测：仅返回上次检查后新增的缺陷"""
    defects = _ones.fetch_my_defects() if mine else _ones.fetch_defects()
    new = _store.filter_new(defects)
    _store.update_check_time()
    return new


@mcp.tool
def get_defect_detail(issue_id: str) -> dict:
    """获取单个缺陷详情，用于深入分析"""
    return _ones.fetch_issue_detail(issue_id)


# ── 项目数据 ──────────────────────────────────────────

@mcp.tool
def list_projects(include_archived: bool = False) -> list[dict]:
    """获取当前团队可见项目列表"""
    return _ones.fetch_projects(include_archived=include_archived)


# ── 代码仓库 ──────────────────────────────────────────

@mcp.tool
def search_codebase(query: str = "", read_file: str = "", max_depth: int = 3) -> str:
    """搜索项目代码仓库。
    - query: 关键词搜索，返回匹配文件内容
    - read_file: 读取指定文件内容
    - 无参数时返回目录结构
    """
    if not _codebase:
        return "未配置代码仓库（CODEBASE_PATH 或 REPO_URL）"
    if read_file:
        content = _codebase.read_file(read_file)
        return content or f"文件不存在: {read_file}"
    if query:
        results = _codebase.search_keywords(query.split(), max_files=10)
        if not results:
            return f"未找到匹配 '{query}' 的文件"
        return "\n---\n".join(f"### {path}\n```\n{content[:4000]}\n```" for path, content in results.items())
    return _codebase.tree(max_depth=max_depth)


# ── 动作 ──────────────────────────────────────────────

@mcp.tool
def push_to_wechat(content: str) -> str:
    """发送 Markdown 消息到企业微信群"""
    _bot.send_markdown(content)
    return "已发送"


if __name__ == "__main__":
    mcp.run()
