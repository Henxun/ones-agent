from __future__ import annotations

import json

import pytest
from textual.widgets import Button, Input, ListView, Static, TabbedContent

from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.verification_settings import VerificationNodeDetails, VerificationNodesPane
from src.developer_workflow.verification import digest
from src.developer_workflow.verification_models import VerificationNode
from test_developer_workflow_tui_app import FakeController
from test_developer_workflow_verification_forms import press


class ConfigurationController(FakeController):
    def __init__(self):
        super().__init__()
        self.nodes = (VerificationNode(key="mac-lab", transport="local",
            capabilities=("os:macos", "arch:arm64")).model_dump(mode="json"),)
        self.saved = []
        self.fail_load = False
        self.fail_save = False

    def list_workspaces(self):
        return ()

    def verification_nodes(self):
        if self.fail_load:
            raise RuntimeError("SECRET-load-error")
        return self.nodes

    def verification_repositories(self):
        return ("app",)

    def save_verification_nodes(self, raw, expected_digest):
        if self.fail_save or expected_digest != digest(self.nodes):
            raise RuntimeError("SECRET-save-error")
        self.nodes = tuple(json.loads(raw))
        self.saved.append(self.nodes)


async def open_nodes(app, pilot):
    dashboard = app.screen
    dashboard.action_show_settings()
    await pilot.pause()
    tabs = dashboard.query_one("#configuration-tabs", TabbedContent)
    await pilot.click(tabs.get_tab("settings-nodes"))
    await pilot.pause()
    return dashboard


@pytest.mark.parametrize("size", [(140, 42), (80, 24)])
async def test_configuration_tabs_list_click_details_and_save(size):
    controller = ConfigurationController()
    app = DeveloperWorkflowTuiApp(controller, 3)
    async with app.run_test(size=size) as pilot:
        dashboard = await open_nodes(app, pilot)
        tabs = dashboard.query_one("#configuration-tabs", TabbedContent)
        assert tabs.active == "settings-nodes"
        assert len(dashboard.query_one("#configuration-node-list", ListView).children) == 1
        assert app.screen is dashboard  # Opening the module does not open an editor.
        assert not dashboard.query("#node-key")
        await pilot.click("#configuration-node-0")
        await pilot.pause()
        assert isinstance(app.screen, VerificationNodeDetails)
        app.screen.query_one("#node-key", Input).value = "renamed"
        await press(app, pilot, "#node-save")
        assert app.screen is dashboard
        assert controller.nodes[0]["key"] == "renamed"
        assert "renamed" in str(dashboard.query_one("#configuration-node-0 Static").render())
        assert tabs.active == "settings-nodes"
        await pilot.click(tabs.get_tab("settings-ones"))
        await pilot.pause()
        assert dashboard.query_one("#inline-ones-base_url", Input).visible
        assert not dashboard.query("#settings-ones #nav-runtime-setup")
        dashboard.action_next_tab()
        assert tabs.active == "settings-provider"
        dashboard.action_next_tab()
        assert tabs.active == "settings-agent"
        dashboard.action_next_tab()
        assert tabs.active == "settings-nodes"
        dashboard.action_next_tab()
        assert tabs.active == "settings-runtime"
        await pilot.pause()
        assert dashboard.query_one("#settings", Static).visible
        dashboard.action_previous_tab()
        assert tabs.active == "settings-nodes"


async def test_add_cancel_and_delete_confirmation_preserve_other_nodes():
    controller = ConfigurationController()
    app = DeveloperWorkflowTuiApp(controller, 3)
    async with app.run_test(size=(120, 42)) as pilot:
        dashboard = await open_nodes(app, pilot)
        await pilot.click("#configuration-node-add")
        await pilot.pause()
        app.screen.query_one("#node-key", Input).value = "discarded"
        await press(app, pilot, "#node-cancel")
        assert app.screen is dashboard and not controller.saved
        await press(app, pilot, "#configuration-node-add")
        app.screen.query_one("#node-key", Input).value = "windows"
        await press(app, pilot, "#node-save")
        assert [node["key"] for node in controller.nodes] == ["mac-lab", "windows"]
        await pilot.click("#configuration-node-0")
        await pilot.pause()
        await press(app, pilot, "#node-delete")
        assert len(controller.nodes) == 2
        await press(app, pilot, "#node-delete")
        assert app.screen is dashboard
        assert [node["key"] for node in controller.nodes] == ["windows"]


async def test_failed_save_retains_detail_inputs_and_cancel_does_not_mutate():
    controller = ConfigurationController()
    app = DeveloperWorkflowTuiApp(controller, 3)
    async with app.run_test(size=(120, 42)) as pilot:
        dashboard = await open_nodes(app, pilot)
        await pilot.click("#configuration-node-0")
        await pilot.pause()
        form = app.screen
        form.query_one("#node-key", Input).value = "my-draft"
        controller.fail_save = True
        await press(app, pilot, "#node-save")
        assert app.screen is form and not controller.saved
        assert form.query_one("#node-key", Input).value == "my-draft"
        error = str(form.query_one(".form-error", Static).render())
        assert "输入已保留" in error and "SECRET" not in error
        controller.fail_save = False
        await press(app, pilot, "#node-save")
        assert app.screen is dashboard and controller.nodes[0]["key"] == "my-draft"


async def test_load_failure_disables_add_and_refresh_recovers():
    controller = ConfigurationController()
    controller.fail_load = True
    app = DeveloperWorkflowTuiApp(controller, 3)
    async with app.run_test(size=(120, 42)) as pilot:
        dashboard = await open_nodes(app, pilot)
        assert dashboard.query_one("#configuration-node-add", Button).disabled
        assert not dashboard.query_one("#configuration-node-list", ListView).children
        error = str(dashboard.query_one("#configuration-node-status", Static).render())
        assert "加载失败" in error and "SECRET" not in error
        controller.fail_load = False
        await press(app, pilot, "#configuration-node-refresh")
        assert not dashboard.query_one("#configuration-node-add", Button).disabled
        assert len(dashboard.query_one("#configuration-node-list", ListView).children) == 1


async def test_concurrent_config_change_is_not_overwritten_by_old_detail():
    controller = ConfigurationController()
    app = DeveloperWorkflowTuiApp(controller, 3)
    async with app.run_test(size=(120, 42)) as pilot:
        dashboard = await open_nodes(app, pilot)
        await pilot.click("#configuration-node-0")
        await pilot.pause()
        form = app.screen
        form.query_one("#node-key", Input).value = "old-draft"
        controller.nodes = (VerificationNode(key="other-change", transport="local").model_dump(mode="json"),)
        await press(app, pilot, "#node-save")
        assert app.screen is form and controller.nodes[0]["key"] == "other-change"
        await press(app, pilot, "#node-cancel")
        assert dashboard.query_one(VerificationNodesPane).nodes == controller.nodes


async def test_node_list_keyboard_opens_details_not_hidden_task_and_reentry_refreshes():
    controller = ConfigurationController()
    app = DeveloperWorkflowTuiApp(controller, 3)
    async with app.run_test(size=(120, 42)) as pilot:
        dashboard = await open_nodes(app, pilot)
        listing = dashboard.query_one("#configuration-node-list", ListView)
        listing.focus()
        listing.index = 0
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, VerificationNodeDetails)
        await press(app, pilot, "#node-cancel")
        dashboard.action_show_runs()
        controller.nodes = ()
        dashboard.action_show_settings()
        await pilot.pause()
        assert not listing.children
        assert "暂无验证节点" in str(dashboard.query_one("#configuration-node-status", Static).render())
        assert not dashboard.query_one("#configuration-node-add", Button).disabled
