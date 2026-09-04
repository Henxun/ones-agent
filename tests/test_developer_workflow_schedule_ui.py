from __future__ import annotations

from types import SimpleNamespace as NS
import pytest
from textual.app import App
from textual.widgets import Button, Input, Select, SelectionList, Static
from src.developer_workflow.schedules import Schedule, ScheduleStore
from src.developer_workflow.tui.models import DefectFilterOptions, FilterChoice, WorkspaceSummary
from src.developer_workflow.tui.schedule_settings import SchedulePane, ScheduleEditor
from src.developer_workflow.tui.schedule_settings import ScheduleDeleteConfirmation


class Supervisor:
    async def run_readonly(self, action, call, *args):
        return call(*args)


class ScheduleApp(App):
    def __init__(self, store):
        super().__init__()
        self.controller = NS(schedule_store=store, load_defect_filter_options=lambda project:
            DefectFilterOptions((), (FilterChoice("user", "成员", True),),
                                (FilterChoice("open", "待处理", True),)))

    def compose(self):
        yield SchedulePane(self.controller, Supervisor(), WorkspaceSummary("group", "project", "iteration", ("repo",)))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (140, 42)])
async def test_schedule_cards_are_separated_and_actions_grouped(tmp_path, size):
    store = ScheduleStore(tmp_path / "private")
    for enabled in (True, False):
        store.save(Schedule(workspace="group", project="project", iteration="iteration",
                            name="定时扫描 [不作为标记解析]", assignee="user", status_ids=("open",),
                            enabled=enabled, last_result="较长的结果说明" * 12), expected_version=None)
    app = ScheduleApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        cards = list(app.query(".schedule-card"))
        assert len(cards) == 2
        assert cards[1].region.y > cards[0].region.bottom
        assert len(app.query(".schedule-card.paused")) == 1
        for card in cards:
            assert len(card.query(Button)) == 3
            assert card.query_one(".schedule-card-title", Static).markup is False
            assert card.query_one(".schedule-card-actions").region.right <= card.region.right
            assert card.query_one(".schedule-card-actions").region.bottom < card.region.bottom


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (140, 42)])
async def test_create_edit_toggle_schedule(tmp_path, size):
    store = ScheduleStore(tmp_path / "private")
    app = ScheduleApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.query_one("#schedule-add", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, ScheduleEditor)
        for _ in range(20):
            if not app.screen.query_one("#schedule-save", Button).disabled:
                break
            await pilot.pause(0.05)
        assert app.screen.query_one("#schedule-assignee", Select).value == "user"
        assert app.screen.query_one(SelectionList).selected == ["open"]
        app.screen.query_one("#schedule-save", Button).press()
        await pilot.pause()
        p = store.list()[0]
        assert not p.enabled
        for _ in range(30):
            if app.query(f"#toggle-{p.id}"):
                break
            await pilot.pause(0.05)
        app.query_one(f"#toggle-{p.id}", Button).press()
        await pilot.pause()
        assert store.list()[0].enabled
        for _ in range(30):
            if app.query(f"#edit-{p.id}") and not app.query(".schedule-card.paused"):
                break
            await pilot.pause(0.05)
        await pilot.pause(0.1)
        app.query_one(f"#edit-{p.id}", Button).press()
        await pilot.pause()
        for _ in range(20):
            if not app.screen.query_one("#schedule-save", Button).disabled:
                break
            await pilot.pause(0.05)
        app.screen.query_one("#schedule-name", Input).value = "已编辑"
        app.screen.query_one("#schedule-action", Select).value = "analyze_and_repair"
        app.screen.query_one("#schedule-enable", Button).press()
        await pilot.pause()
        saved = store.list()[0]
        assert saved.name == "已编辑"
        assert saved.action.value == "analyze_and_repair"
        assert saved.version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (140, 42)])
async def test_delete_requires_confirmation(tmp_path, size):
    store = ScheduleStore(tmp_path / "private")
    p = store.save(Schedule(workspace="group", project="project", iteration="iteration",
                           name="删除测试", assignee="user", status_ids=("open",), enabled=True),
                   expected_version=None)
    app = ScheduleApp(store)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.query_one(f"#delete-{p.id}", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, ScheduleDeleteConfirmation)
        assert store.list() == (p,)
        await pilot.press("escape")
        await pilot.pause()
        assert store.list() == (p,)
        app.query_one(f"#delete-{p.id}", Button).press()
        await pilot.pause()
        assert app.screen.query_one("#schedule-delete-confirm", Button).region.bottom <= size[1]
        app.screen.query_one("#schedule-delete-confirm", Button).press()
        for _ in range(30):
            await pilot.pause(0.05)
            if not store.list() and not app.query(f"#delete-{p.id}"):
                break
        assert store.list() == ()
        assert not app.query(f"#delete-{p.id}")
