from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
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
    CodingAgentProbeResult,
    coding_agent_definition,
    discover_coding_agents,
    probe_coding_agent,
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

    def probe(key: str, executable: Path) -> CodingAgentProbeResult:
        assert executable.exists()
        return CodingAgentProbeResult(
            launchable=True, version="1.2.3", diagnostic="已验证可启动"
        )

    catalog = discover_coding_agents(paths.get, probe)

    by_key = {item.key: item for item in catalog}
    assert by_key["codex"].usable
    assert by_key["claude"].usable
    assert by_key["claude"].launchable
    assert by_key["claude"].version == "1.2.3"
    assert by_key["gemini"].installed and not by_key["gemini"].usable
    assert not by_key["aider"].installed


def test_probe_extracts_only_version_and_uses_bounded_safe_process(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"test")
    captured: dict[str, object] = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            "2.7.1 (Claude Code)\n",
            "",
        )

    result = probe_coding_agent("claude", executable, run=run)

    assert result == CodingAgentProbeResult(
        launchable=True, version="2.7.1", diagnostic="已验证可启动"
    )
    assert captured["command"] == [str(executable.resolve()), "--version"]
    assert captured["timeout"] == 5.0
    assert captured["max_output_bytes"] == 16 * 1024
    assert "secret-token" not in result.diagnostic
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "CODEX_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.parametrize(
    "output",
    [
        "Claude Code 2.7.1",
        "2.7.1 (Claude Code)\nsecret-token-must-not-be-returned",
        "10.0.0.1",
        "2.7.1 8.9.0 (Claude Code)",
        "2.7.1 (Claude Code) secret-token",
        "codex-cli 1.2.3",
    ],
)
def test_claude_probe_rejects_noncanonical_or_extra_output(
    tmp_path: Path, output: str
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"test")
    completed = subprocess.CompletedProcess([], 0, output, "")

    result = probe_coding_agent(
        "claude", executable, run=lambda *_args, **_kwargs: completed
    )

    assert not result.launchable
    assert result.version == ""
    assert result.diagnostic == "未返回可识别的版本号"


def test_codex_probe_uses_configured_runtime_resolver_and_anchored_product_line(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"test")

    class Command:
        def argv(self, *arguments: str) -> list[str]:
            return [str(executable), *arguments]

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "src.developer_workflow.codex_runner.resolve_codex_command",
        lambda: (_ for _ in ()).throw(OSError("wrong default cache")),
    )
    completed = subprocess.CompletedProcess([], 0, "codex-cli 1.2.3\n", "")

    result = probe_coding_agent(
        "codex",
        executable,
        run=lambda *_args, **_kwargs: completed,
        codex_command_resolver=Command,
    )

    assert result.launchable
    assert result.version == "1.2.3"


def test_codex_probe_fails_closed_when_default_cache_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(
        "src.developer_workflow.codex_runner.resolve_codex_command",
        lambda: (_ for _ in ()).throw(OSError("private path detail")),
    )

    result = probe_coding_agent(
        "codex",
        executable,
        run=lambda *_args, **_kwargs: pytest.fail("process must not start"),
    )

    assert not result.launchable
    assert result.diagnostic == "无法安全启动命令"
    assert "private path" not in result.diagnostic


@pytest.mark.parametrize(
    ("outcome", "diagnostic"),
    [
        (subprocess.CompletedProcess([], 1, "Bearer private-value", ""), "版本探测命令执行失败"),
        (subprocess.CompletedProcess([], 0, "unexpected private-value", ""), "未返回可识别的版本号"),
    ],
)
def test_probe_failure_diagnostic_is_categorical_and_redacted(
    tmp_path: Path,
    outcome: subprocess.CompletedProcess[str],
    diagnostic: str,
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"test")

    result = probe_coding_agent(
        "claude", executable, run=lambda *_args, **_kwargs: outcome
    )

    assert not result.launchable
    assert result.version == ""
    assert result.diagnostic == diagnostic
    assert "private-value" not in result.diagnostic


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        (subprocess.TimeoutExpired(["claude"], 5), "版本探测超时"),
        (CodingAgentOutputError("private output limit detail"), "无法安全启动命令"),
    ],
)
def test_probe_redacts_bounded_executor_failures(
    tmp_path: Path,
    failure: Exception,
    diagnostic: str,
) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"test")

    def fail(*_args, **_kwargs):
        raise failure

    result = probe_coding_agent("claude", executable, run=fail)

    assert not result.launchable
    assert result.diagnostic == diagnostic
    assert "private" not in result.diagnostic


def test_discovery_keeps_installed_but_unlaunchable_agent_unselectable(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "claude.exe"
    claude.write_bytes(b"test")

    catalog = discover_coding_agents(
        lambda command: str(claude) if command == "claude" else None,
        lambda _key, _path: CodingAgentProbeResult(
            False, diagnostic="版本探测命令执行失败"
        ),
    )
    item = next(item for item in catalog if item.key == "claude")

    assert item.installed
    assert not item.launchable
    assert not item.usable
    assert item.detail == "版本探测命令执行失败"


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
        CodingAgentInstallation(
            "codex", "Codex", True, True, True, Path("codex.exe"),
            "可用", True, "1.4.0",
        ),
        CodingAgentInstallation(
            "claude", "Claude Code", True, True, True, Path("claude.exe"),
            "可用", True, "2.7.1",
        ),
        CodingAgentInstallation(
            "gemini", "Gemini CLI", True, False, False, Path("gemini.exe"),
            "未接入",
        ),
        CodingAgentInstallation(
            "aider", "Aider", True, True, False, Path("aider.exe"),
            "版本探测命令执行失败",
        ),
    )
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    app.discover_inline_coding_agents = AsyncMock(return_value=catalog)
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
            "Codex", "Claude Code", "Gemini CLI", "Aider"
        ]
        assert tuple(
            value for _, value in select._options if isinstance(value, str)
        ) == ("codex", "claude")
        select.value = "claude"
        pane.save()
        await pilot.pause()
        app.save_inline_coding_agent.assert_awaited_once_with("claude")
        assert not pane.query_one("#coding-agent-save", Button).disabled


async def test_app_discovery_uses_active_runtime_codex_preparer(monkeypatch) -> None:
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    prepared = object()

    def prepare_verified():
        return prepared

    app._runtime_bootstrapper = SimpleNamespace(
        codex_runtime_preparer=SimpleNamespace(prepare_verified=prepare_verified)
    )
    resolved = object()

    def resolve_codex_command(*, _prepare):
        assert _prepare is prepare_verified
        assert _prepare() is prepared
        return resolved

    monkeypatch.setattr(
        "src.developer_workflow.codex_runner.resolve_codex_command",
        resolve_codex_command,
    )

    def fake_probe(key, executable, *, codex_command_resolver=None):
        assert key == "codex"
        assert executable == Path("codex.cmd")
        assert codex_command_resolver is not None
        assert codex_command_resolver() is resolved
        return CodingAgentProbeResult(True, "1.2.3", "已验证可启动")

    monkeypatch.setattr(
        "src.developer_workflow.tui.app.probe_coding_agent", fake_probe
    )

    def fake_discovery(*, probe):
        result = probe("codex", Path("codex.cmd"))
        return (
            CodingAgentInstallation(
                "codex", "Codex", True, True, result.launchable,
                Path("codex.cmd"), result.diagnostic, result.launchable,
                result.version,
            ),
        )

    monkeypatch.setattr(
        "src.developer_workflow.tui.app.discover_coding_agents", fake_discovery
    )

    catalog = await app.discover_inline_coding_agents()

    assert catalog[0].launchable
    assert catalog[0].version == "1.2.3"
