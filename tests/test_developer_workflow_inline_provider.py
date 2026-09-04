from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
from textual.widgets import Input, Select, TabbedContent
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.provider_settings import ProviderSettingsPane
from src.developer_workflow.setup_models import SecretKind
from src.developer_workflow.setup_controller import SetupActionError
from test_developer_workflow_configuration_tabs import ConfigurationController
from test_developer_workflow_setup_controller import _controller, _candidate_for_store


@pytest.mark.parametrize("failed", [False, True])
async def test_inline_transition_keeps_navigation_mounted(failed):
    from types import SimpleNamespace
    from unittest.mock import Mock
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    app.read_inline_ones = AsyncMock(return_value={})
    app.read_inline_provider = AsyncMock(return_value={"provider": "github"})
    async with app.run_test() as pilot:
        dashboard = app.screen
        dashboard.action_show_settings()
        await pilot.pause()
        dashboard.query_one("#configuration-tabs", TabbedContent).active = "settings-provider"
        navigation = dashboard.query_one("#navigation")
        session = app.runtime_session
        app.runtime_session = SimpleNamespace(close=AsyncMock(), close_complete=True)
        editor = SimpleNamespace(load_active_public_draft=Mock(), aclose=AsyncMock(),
            save_and_activate=AsyncMock(return_value="new"), activate_existing=AsyncMock(return_value="old"))
        async def prepare(*args):
            assert navigation.is_attached and app.screen is dashboard
            assert dashboard.disabled
            if failed:
                raise RuntimeError("private")
        editor.prepare_inline_provider = prepare
        app._new_setup_controller = Mock(return_value=editor)
        async def finish(handle):
            assert navigation.is_attached and app.screen is dashboard
            app._bind_runtime_session(session)
        app._finish_setup = finish
        try:
            await app.save_inline_provider({}, {})
            assert app.screen is dashboard and app._dashboard is dashboard
            assert dashboard.query_one("#navigation") is navigation
            assert navigation.is_attached and not dashboard.disabled
            assert dashboard.query_one("#configuration-tabs", TabbedContent).active == "settings-provider"
        finally:
            app.runtime_session = session


@pytest.mark.parametrize("tab", ["settings-provider", "settings-ones"])
async def test_restored_configuration_is_visible_on_first_mount(tab, monkeypatch):
    from src.developer_workflow.tui.screens import DashboardScreen
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    app._restore_settings_tab = tab
    app._dashboard = app._build_dashboard(app.runtime_session)
    app.read_inline_ones = AsyncMock(return_value={})
    app.read_inline_provider = AsyncMock(return_value={"provider": "github"})
    mounted = []
    original = DashboardScreen.on_mount
    def observe(screen):
        original(screen)
        mounted.append((screen.query_one("#workspace-home").display,
                        screen.query_one("#settings-page").display,
                        screen.query_one("#configuration-tabs", TabbedContent).active))
    monkeypatch.setattr(DashboardScreen, "on_mount", observe)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert mounted == [(False, True, tab)]
        app.screen.query_one("#configuration-tabs", TabbedContent).active = "settings-provider"
        app.screen.action_next_tab()
        assert app.screen.query_one("#configuration-tabs", TabbedContent).active == "settings-nodes"


@pytest.mark.parametrize("base", ["https://git.example.test/api/v4", "https://git.example.test/gitlab/api/v4/"])
async def test_private_gitlab_probe_uses_authenticated_user_not_api_root(base):
    import httpx
    from src.developer_workflow.setup_validation import SetupValidator, ProviderProbeInput, ValidationStatus
    requests = []
    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200 if request.url.path.endswith("/user") else 404, json={"id": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        validator = SetupValidator._testing(provider_transport=transport)
        result = await validator.probe_provider(ProviderProbeInput(host="git.example.test", api_url=base))
    assert result.status is ValidationStatus.PASSED
    assert requests == [base.rstrip("/") + "/user"]


def test_validation_error_never_includes_unknown_category():
    from src.developer_workflow.setup_controller import InlineValidationError
    from src.developer_workflow.setup_validation import SetupStep
    assert "SECRET" not in str(InlineValidationError(SetupStep.PROVIDER, "SECRET"))
    assert "认证失败" in str(InlineValidationError(SetupStep.PROVIDER, "authentication"))


@pytest.mark.parametrize("size", [(140, 42), (80, 24)])
async def test_provider_tab_edits_inline_and_clears_token(size):
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    app.read_inline_provider = AsyncMock(return_value={"provider": "gitlab", "provider_host": "gitlab.example.test"})
    saved = []
    async def save(fields, credentials):
        saved.append((dict(fields), dict(credentials)))
    app.save_inline_provider = save
    async with app.run_test(size=size) as pilot:
        dashboard = app.screen
        dashboard.action_show_settings()
        await pilot.pause()
        dashboard.query_one("#configuration-tabs", TabbedContent).active = "settings-provider"
        assert not dashboard.query("#configure-runtime")
        assert dashboard.query_one("#inline-provider-type", Select).value == "gitlab"
        token = dashboard.query_one("#inline-provider-token", Input)
        assert token.password and token.value == ""
        token.value = "test-secret"
        dashboard.query_one(ProviderSettingsPane).save()
        await pilot.pause()
        assert saved[0][1][SecretKind.PROVIDER_TOKEN] == "test-secret"
        assert token.value == ""
        assert app.screen is dashboard


async def test_provider_endpoint_change_requires_new_token(tmp_path):
    controller, store, _, _ = _controller(tmp_path)
    store.document = store.document.validated_update(active=_candidate_for_store(tmp_path, "a" * 32))
    try:
        with pytest.raises(SetupActionError, match="explicit credentials"):
            await controller.prepare_inline_provider({"provider": "github", "provider_host": "github.com", "provider_api_url": "https://api.github.com", "git_author_name": "Test", "git_author_email": "test@example.test"}, {})
        assert store.commits == 0
    finally:
        await controller.aclose()


async def test_provider_edit_preserves_ones_and_updates_type(tmp_path):
    controller, store, _, _ = _controller(tmp_path)
    active = _candidate_for_store(tmp_path, "a" * 32)
    store.document = store.document.validated_update(active=active)
    try:
        await controller.prepare_inline_provider({"provider": "gitlab", "provider_host": active.runtime.provider_host, "provider_api_url": active.runtime.provider_api_url, "git_author_name": "Test", "git_author_email": "test@example.test"}, {})
        candidate, secrets = controller._build_candidate()
        assert candidate.workflow.publishing.provider.value == "gitlab"
        assert candidate.runtime.ones_base_url == active.runtime.ones_base_url
        assert secrets.require(SecretKind.ONES_PASSWORD) == "persisted-value"
        assert secrets.require(SecretKind.PROVIDER_TOKEN) == "persisted-value"
        assert store.commits == 0
    finally:
        await controller.aclose()


@pytest.mark.parametrize("module", ["provider", "ones"])
async def test_inline_save_passes_inputs_to_real_connection_validator(tmp_path, module):
    from src.developer_workflow.setup_validation import SetupValidator, SetupStep
    from test_developer_workflow_setup_validation import _OnesGateway, _Provider
    controller, store, _, _ = _controller(tmp_path)
    active = _candidate_for_store(tmp_path, "a" * 32)
    store.document = store.document.validated_update(active=active)
    gateway, transport = _OnesGateway(), _Provider()
    gateway.list_projects = AsyncMock(return_value=[])
    controller.STEPS = (SetupStep.ONES, SetupStep.PROVIDER, SetupStep.REVIEW)
    controller._validator = SetupValidator._testing(ones_gateway=gateway, provider_transport=transport)
    try:
        if module == "provider":
            await controller.prepare_inline_provider({"provider": "gitlab", "provider_host": active.runtime.provider_host,
                "provider_api_url": active.runtime.provider_api_url, "git_author_name": "Test", "git_author_email": "test@example.test"}, {})
        else:
            await controller.prepare_inline_ones({"ones_base_url": active.runtime.ones_base_url,
                "ones_team_id": active.runtime.ones_team_id, "ones_issue_type_id": active.runtime.ones_issue_type_id}, {})
        assert controller._review_confirmed
        assert ("get_team", active.runtime.ones_team_id) in gateway.calls
        assert transport.calls == [("GET", active.runtime.provider_api_url.rstrip("/") + "/user", 10.0)]
        assert store.commits == 0
    finally:
        await controller.aclose()
