from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Select, TabbedContent

from src.developer_workflow.claude_runner import ClaudeRunner
from src.developer_workflow.codex_runner import (
    CodexOutputError,
    CodexRunner,
    GuardedCodingAgentRunner,
)
from src.developer_workflow.coding_agent_runner import (
    CodingAgentOutputError,
    CodingAgentRunner,
)
from src.developer_workflow.coding_agents import (
    SUPPORTED_CODING_AGENT_KEYS,
    CodingAgentInstallation,
    coding_agent_definition,
    discover_coding_agents,
    validate_coding_agent_provider_keys,
)
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.coding_agent_settings import CodingAgentSettingsPane
from test_developer_workflow_configuration_tabs import ConfigurationController
from test_developer_workflow_setup_controller import _candidate_for_store, _controller


def test_discovery_reports_supported_and_detected_only_agents(tmp_path: Path) -> None:
    codex = tmp_path / "codex.exe"
    claude = tmp_path / "claude.exe"
    gemini = tmp_path / "gemini.exe"
    for path in (codex, claude, gemini):
        path.write_bytes(b"test")
    paths = {"codex": str(codex), "claude": str(claude), "gemini": str(gemini)}

    catalog = discover_coding_agents(paths.get)

    by_key = {item.key: item for item in catalog}
    assert by_key["codex"].usable
    assert by_key["claude"].usable
    assert by_key["gemini"].installed and not by_key["gemini"].usable
    assert not by_key["aider"].installed


def test_legacy_runtime_config_defaults_to_codex() -> None:
    from test_developer_workflow_setup_models import _public_config

    assert _public_config().coding_agent == "codex"


def test_runner_abstraction_is_provider_neutral_and_backwards_compatible(
    tmp_path: Path,
) -> None:
    codex = CodexRunner(tmp_path.resolve(), object())
    claude = ClaudeRunner(tmp_path.resolve(), object())

    assert isinstance(codex, GuardedCodingAgentRunner)
    assert isinstance(claude, GuardedCodingAgentRunner)
    assert isinstance(codex, CodingAgentRunner)
    assert isinstance(claude, CodingAgentRunner)
    assert not isinstance(claude, CodexRunner)
    assert CodexOutputError is CodingAgentOutputError


def test_supported_agent_registry_owns_selection_keys() -> None:
    assert SUPPORTED_CODING_AGENT_KEYS == {"codex", "claude"}
    assert coding_agent_definition("claude").command == "claude"
    validate_coding_agent_provider_keys({"codex": object(), "claude": object()})
    with pytest.raises(ValueError, match="incomplete"):
        validate_coding_agent_provider_keys({"codex": object()})


async def test_inline_agent_selection_preserves_existing_configuration(tmp_path: Path) -> None:
    controller, store, _, _ = _controller(tmp_path)
    active = _candidate_for_store(tmp_path, "a" * 32)
    store.document = store.document.validated_update(active=active)
    try:
        await controller.prepare_inline_coding_agent("claude")
        candidate, secrets = controller._build_candidate()
        assert candidate.runtime.coding_agent == "claude"
        assert candidate.runtime.ones_base_url == active.runtime.ones_base_url
        assert secrets.values
        assert store.commits == 0
    finally:
        await controller.aclose()


def test_claude_runner_uses_headless_schema_and_persists_session(tmp_path: Path) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"test")
    captured = {}
    structured = {
        "summary": "ok", "changed_files": [], "commands": [], "evidence": [],
        "review_findings": [], "risks": [], "unresolved_items": [],
    }

    def execute(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command, 0,
            json.dumps({
                "type": "result", "is_error": False,
                "session_id": "12345678-abcd-4000-8000-123456789abc",
                "structured_output": structured,
            }),
            "",
        )

    root = (tmp_path / "runs").resolve()
    runner = ClaudeRunner(
        root, object(), command_executor=execute,
        claude_command_resolver=lambda: executable,
        environment_provider=lambda: {"PATH": str(tmp_path), "SECRET_TOKEN": "hidden"},
    )
    output, removed = runner._invoke(
        run_id="run-1", prompt="analyze", cwd=tmp_path.resolve(),
        sandbox="workspace-write", timeout_seconds=10,
        skip_git_repo_check=False, additional_directories=(),
        output_schema=runner.schema_path,
    )

    assert json.loads(output) == structured and removed == ()
    assert captured["command"][1:3] == ("-p", "--output-format")
    assert "--json-schema" in captured["command"]
    assert captured["env"] == {"PATH": str(tmp_path)}
    assert (root / "run-1" / ".claude-session-id").read_text().strip().startswith("12345678")


async def test_configuration_agent_tab_lists_and_saves_available_agent(monkeypatch) -> None:
    catalog = (
        CodingAgentInstallation("codex", "Codex", True, True, True, Path("codex.exe"), "可用"),
        CodingAgentInstallation("claude", "Claude Code", True, True, True, Path("claude.exe"), "可用"),
        CodingAgentInstallation("gemini", "Gemini CLI", True, False, False, Path("gemini.exe"), "未接入"),
    )
    monkeypatch.setattr(
        "src.developer_workflow.tui.coding_agent_settings.discover_coding_agents",
        lambda: catalog,
    )
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    app.read_inline_coding_agent = AsyncMock(return_value="codex")
    app.save_inline_coding_agent = AsyncMock()
    async with app.run_test(size=(120, 40)) as pilot:
        dashboard = app.screen
        dashboard.action_show_settings()
        tabs = dashboard.query_one("#configuration-tabs", TabbedContent)
        tabs.active = "settings-agent"
        await pilot.pause()
        pane = dashboard.query_one(CodingAgentSettingsPane)
        select = pane.query_one("#coding-agent-select", Select)
        assert select.value == "codex"
        assert [item.label for item in pane._catalog] == [
            "Codex", "Claude Code", "Gemini CLI"
        ]
        select.value = "claude"
        pane.save()
        await pilot.pause()
        app.save_inline_coding_agent.assert_awaited_once_with("claude")
        assert not pane.query_one("#coding-agent-save", Button).disabled
