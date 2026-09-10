"""Inline coding-agent discovery and selection settings."""

from __future__ import annotations

from dataclasses import replace

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, Select, Static

from ..coding_agents import (
    CodingAgentInstallation,
    CodingAgentProbeResult,
    discover_coding_agents,
)


class CodingAgentSettingsPane(VerticalScroll):
    DEFAULT_CSS = """
    CodingAgentSettingsPane { padding: 1 2; }
    #coding-agent-heading {
        text-style: bold;
        color: $text;
    }
    #coding-agent-description {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #coding-agent-summary, #coding-agent-catalog {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }
    #coding-agent-choice {
        width: 100%;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #coding-agent-choice-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #coding-agent-current {
        height: auto;
        margin-bottom: 1;
    }
    #coding-agent-actions {
        width: 100%;
        height: 4;
        margin-top: 1;
    }
    #coding-agent-actions Button {
        width: 20;
        margin-right: 1;
    }
    #coding-agent-notice {
        width: 100%;
        height: auto;
        min-height: 1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="coding-agent-settings-form")
        self._loaded = False
        self._busy = False
        self._catalog: tuple[CodingAgentInstallation, ...] = ()

    def compose(self) -> ComposeResult:
        yield Label("Coding Agent", id="coding-agent-heading")
        yield Static(
            "检测本机已安装的编码工具，并选择工作流实际使用的 Agent。"
            "启动检查不会验证账号登录；认证和安全执行能力仍会在任务启动时校验。"
            "未接入结构化输出协议的工具只展示，不会进入可选列表。",
            id="coding-agent-description",
            markup=False,
        )
        yield Static("正在检测本机环境…", id="coding-agent-summary", markup=False)
        yield Static("", id="coding-agent-catalog")
        with Vertical(id="coding-agent-choice"):
            yield Label("工作流执行引擎", id="coding-agent-choice-title")
            yield Static("当前生效：读取中", id="coding-agent-current", markup=False)
            yield Select([], allow_blank=True, id="coding-agent-select")
            with Horizontal(id="coding-agent-actions"):
                yield Button(
                    "保存并应用", id="coding-agent-save",
                    variant="primary", disabled=True,
                )
                yield Button(
                    "验证启动", id="coding-agent-verify", disabled=True
                )
                yield Button("重新检测", id="coding-agent-refresh")
            yield Static("", id="coding-agent-notice", markup=False)

    def _render_catalog(self) -> None:
        installed = sum(item.installed for item in self._catalog)
        usable = sum(item.usable for item in self._catalog)
        verified = sum(item.readiness_checked and item.launchable for item in self._catalog)
        summary = Text()
        summary.append("检测到 ", style="dim")
        summary.append(str(installed), style="bold cyan")
        summary.append(" 个工具   ·   已接入并可选择 ", style="dim")
        summary.append(str(usable), style="bold green")
        summary.append(" 个   ·   已验证 ", style="dim")
        summary.append(str(verified), style="bold cyan")
        summary.append(" 个", style="dim")
        self.query_one("#coding-agent-summary", Static).update(
            Panel(summary, border_style="cyan", padding=(0, 1))
        )

        table = Table(
            box=box.SIMPLE_HEAD,
            expand=True,
            padding=(0, 1),
            header_style="bold",
        )
        table.add_column("Agent", style="bold", min_width=14)
        table.add_column("安装", min_width=10)
        table.add_column("启动检查", min_width=20)
        table.add_column("工作流接入", min_width=22)
        table.add_column("可执行文件", ratio=2, overflow="fold")
        for item in self._catalog:
            if item.usable:
                status = Text("● 已安装", style="bold green")
                if item.launchable:
                    launch = Text(f"● 已验证 · {item.version}", style="cyan")
                elif item.readiness_checked:
                    launch = Text(f"● 验证失败 · {item.detail}", style="red")
                else:
                    launch = Text("尚未验证启动", style="yellow")
                capability = Text("可选择 · 认证待运行时校验", style="yellow")
            elif item.installed:
                status = Text("● 已安装", style="yellow")
                launch = Text(item.detail, style="yellow")
                capability = Text(
                    "已接入但不可选择" if item.supported else "尚未接入",
                    style="yellow",
                )
            else:
                status = Text("○ 未安装", style="dim")
                launch = Text("—", style="dim")
                capability = Text("—", style="dim")
            executable = str(item.executable) if item.executable else "—"
            table.add_row(
                item.label, status, launch, capability, Text(executable, style="dim")
            )
        self.query_one("#coding-agent-catalog", Static).update(
            Panel(
                table,
                title="[bold cyan]本机 Agent[/]",
                subtitle="[dim]自动检测仅检查 PATH，不会启动任何 Agent[/]",
                border_style="blue",
                padding=(0, 1),
            )
        )
        options = [(item.label, item.key) for item in self._catalog if item.usable]
        self.query_one("#coding-agent-select", Select).set_options(options)

    async def load(self, *, force: bool = False) -> None:
        if self._busy or (self._loaded and not force):
            return
        self._busy = True
        save = self.query_one("#coding-agent-save", Button)
        try:
            discover = getattr(self.app, "discover_inline_coding_agents", None)
            self._catalog = (
                await discover()
                if callable(discover)
                else discover_coding_agents()
            )
            self._render_catalog()
            current = await self.app.read_inline_coding_agent()
            available = {item.key for item in self._catalog if item.usable}
            labels = {item.key: item.label for item in self._catalog}
            select = self.query_one("#coding-agent-select", Select)
            if current in available:
                select.value = current
                save.disabled = False
                current_label = labels.get(current, current)
                self.query_one("#coding-agent-current", Static).update(
                    Text.assemble(
                        ("● ", "bold green"),
                        ("当前生效：", "dim"),
                        (current_label, "bold"),
                    )
                )
                message = (
                    "当前 Agent 已安装并接入工作流；尚未验证启动。"
                    "可主动验证，保存时也会执行相同的受限检查。"
                )
            else:
                select.clear()
                save.disabled = True
                self.query_one("#coding-agent-current", Static).update(
                    Text.assemble(
                        ("● ", "bold red"),
                        ("当前配置不可用：", "red"),
                        (labels.get(current, current), "bold red"),
                    )
                )
                message = "请安装受支持的原生 CLI，然后重新检测。"
            self.query_one("#coding-agent-notice", Static).update(message)
            self.query_one("#coding-agent-verify", Button).disabled = (
                not isinstance(select.value, str)
            )
            self._loaded = True
        except Exception:
            save.disabled = True
            self.query_one("#coding-agent-current", Static).update(
                Text("● 配置读取失败", style="bold red")
            )
            self.query_one("#coding-agent-notice", Static).update(
                "无法读取 Coding Agent 配置，请检查配置存储后重试。"
            )
        finally:
            self._busy = False

    @on(Button.Pressed, "#coding-agent-refresh")
    async def reload_catalog(self) -> None:
        await self.load(force=True)

    @on(Button.Pressed, "#coding-agent-save")
    def save(self) -> None:
        if self._busy or not self._loaded:
            return
        value = self.query_one("#coding-agent-select", Select).value
        if not isinstance(value, str):
            return
        self._busy = True
        self.app.run_worker(self._save(value), group="inline-coding-agent-save")

    @on(Button.Pressed, "#coding-agent-verify")
    def verify(self) -> None:
        if self._busy or not self._loaded:
            return
        value = self.query_one("#coding-agent-select", Select).value
        if not isinstance(value, str):
            return
        self._busy = True
        self.app.run_worker(
            self._verify(value), group="inline-coding-agent-verify"
        )

    def _record_readiness(
        self, key: str, result: CodingAgentProbeResult
    ) -> None:
        selected = self.query_one("#coding-agent-select", Select).value
        self._catalog = tuple(
            replace(
                item,
                launchable=result.launchable,
                version=result.version,
                detail=result.diagnostic,
                readiness_checked=True,
            )
            if item.key == key
            else item
            for item in self._catalog
        )
        self._render_catalog()
        if isinstance(selected, str):
            self.query_one("#coding-agent-select", Select).value = selected

    async def _verify(self, value: str) -> None:
        verify = self.query_one("#coding-agent-verify", Button)
        verify.disabled = True
        self.query_one("#coding-agent-notice", Static).update(
            "正在执行受限启动验证…"
        )
        try:
            result = await self.app.verify_inline_coding_agent(value)
            if self.is_attached:
                self._record_readiness(value, result)
                self.query_one("#coding-agent-notice", Static).update(
                    "启动验证通过；账号认证和执行权限仍将在任务运行时校验。"
                    if result.launchable
                    else f"启动验证失败：{result.diagnostic}"
                )
        except Exception:
            if self.is_attached:
                self.query_one("#coding-agent-notice", Static).update(
                    "启动验证失败；未保存命令输出，请检查安装路径后重试。"
                )
        finally:
            self._busy = False
            if self.is_attached:
                verify.disabled = False

    async def _save(self, value: str) -> None:
        self.query_one("#coding-agent-save", Button).disabled = True
        self.query_one("#coding-agent-notice", Static).update(
            "正在校验配置并切换工作流运行时…"
        )
        try:
            result = await self.app.save_inline_coding_agent(value)
            if self.is_attached and isinstance(result, CodingAgentProbeResult):
                self._record_readiness(value, result)
                self.query_one("#coding-agent-notice", Static).update(
                    "启动验证通过，配置已保存并应用。"
                )
        except Exception:
            if self.is_attached:
                self.query_one("#coding-agent-notice", Static).update(
                    "未应用配置；请确认 CLI 位于 PATH 中，且当前没有运行中的任务。"
                )
        finally:
            self._busy = False
            if self.is_attached:
                self.query_one("#coding-agent-save", Button).disabled = False


__all__ = ["CodingAgentSettingsPane"]
