"""Inline Git provider settings; secrets are never loaded into widgets."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static

from ..setup_models import SecretKind


class ProviderSettingsPane(VerticalScroll):
    DEFAULT_CSS = """
    ProviderSettingsPane { padding: 1 2; }
    ProviderSettingsPane Label { margin-top: 1; }
    ProviderSettingsPane Button { margin-top: 1; }
    """
    FIELDS = {"provider_host": "服务域名（不含 https://）", "provider_api_url": "API 地址",
              "git_author_name": "备用提交姓名（TUI 优先使用本机 Git）",
              "git_author_email": "备用提交邮箱（TUI 优先使用本机 Git）"}

    def __init__(self) -> None:
        super().__init__(id="provider-settings-form")
        self._loaded = False
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Label("GitHub / GitLab 连接配置")
        yield Static("提交和推送使用本机 Git 配置；平台令牌用于创建 PR/MR。令牌留空保留，更换服务端点必须重新填写。保存不会提交代码或创建 PR，发布仍需人工审批。", markup=False)
        yield Label("平台类型")
        yield Select([("GitHub", "github"), ("GitLab", "gitlab")], allow_blank=False, id="inline-provider-type")
        for key, label in self.FIELDS.items():
            yield Label(label)
            yield Input(id=f"inline-provider-{key}")
        yield Static("GitHub：github.com / https://api.github.com\nGitLab：gitlab.com / https://gitlab.com/api/v4；私有部署请填写实际地址。", markup=False)
        yield Label("平台令牌（不回显，留空保留）")
        yield Input(password=True, id="inline-provider-token")
        yield Button("保存并应用", id="inline-provider-save", variant="primary", disabled=True)
        yield Button("重新加载", id="inline-provider-reload")
        yield Static("", id="inline-provider-notice", markup=False)

    async def load(self, *, force: bool = False) -> None:
        if self._busy or (self._loaded and not force):
            return
        self._busy = True
        try:
            fields = await self.app.read_inline_provider()
            for key in self.FIELDS:
                self.query_one(f"#inline-provider-{key}", Input).value = fields.get(key, "")
            self.query_one("#inline-provider-type", Select).value = fields.get("provider", "github")
            self.query_one("#inline-provider-token", Input).value = ""
            self._loaded = True
            self.query_one("#inline-provider-save", Button).disabled = False
            self.query_one("#inline-provider-notice", Static).update("已加载当前配置；任务空闲时可保存并应用。")
        except Exception:
            self.query_one("#inline-provider-save", Button).disabled = True
            self.query_one("#inline-provider-notice", Static).update("配置读取失败，请检查配置存储后重新加载。")
        finally:
            self._busy = False

    @on(Button.Pressed, "#inline-provider-reload")
    async def reload(self) -> None:
        await self.load(force=True)

    @on(Button.Pressed, "#inline-provider-save")
    def save(self) -> None:
        if self._busy or not self._loaded:
            return
        self._busy = True
        self.app.run_worker(self._save(), group="inline-provider-save")

    async def _save(self) -> None:
        fields = {key: self.query_one(f"#inline-provider-{key}", Input).value.strip() for key in self.FIELDS}
        fields["provider"] = str(self.query_one("#inline-provider-type", Select).value)
        token = self.query_one("#inline-provider-token", Input)
        credentials = {SecretKind.PROVIDER_TOKEN: token.value}
        token.value = ""
        self.query_one("#inline-provider-save", Button).disabled = True
        self.query_one("#inline-provider-notice", Static).update("正在校验并保存配置…")
        try:
            await self.app.save_inline_provider(fields, credentials)
        except Exception:
            if self.is_attached:
                self.query_one("#inline-provider-notice", Static).update("配置未应用，请确认任务空闲、地址正确且令牌具有所需权限。")
        finally:
            credentials.clear()
            self._busy = False
            if self.is_attached:
                self.query_one("#inline-provider-save", Button).disabled = False
