"""Modal screens for the TUI — currently just the config dialog.

``ConfigScreen`` collects the lmheads API key + base URL and triggers
Codex authentication. It dismisses with ``{"api_key", "base_url"}`` on
save (the app persists + reconnects) or ``None`` on cancel. Codex auth
buttons act in place and don't dismiss — they're independent of saving
the lmheads config.
"""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from .codex_auth import login_with_api_key
from .codex_auth import status as codex_status
from .config import Config


class ConfigScreen(ModalScreen[dict | None]):
    CSS = """
    ConfigScreen { align: center middle; }
    #config-dialog {
        width: 78; height: auto; max-height: 90%;
        padding: 1 2; border: thick $primary; background: $surface;
    }
    #config-title { text-style: bold; padding-bottom: 1; }
    #config-dialog Label { padding-top: 1; color: $text-muted; }
    #codex_status { padding: 1 0; color: $text; }
    #config-buttons { padding-top: 1; height: auto; align-horizontal: right; }
    #config-buttons Button { margin-left: 2; }
    #apikey-row Button { margin-left: 2; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        has_key = bool(self.cfg.api_key)
        with Vertical(id="config-dialog"):
            yield Static("Configure lmheads-codex", id="config-title")

            yield Label("lmheads API key (lmh_…, agent-scoped)")
            yield Input(
                password=True,
                id="lmh_key",
                placeholder=(
                    "leave blank to keep current key" if has_key else "lmh_…"
                ),
            )
            yield Label("lmheads base URL")
            yield Input(id="lmh_base", value=self.cfg.base_url)

            yield Static("Codex auth: checking…", id="codex_status")
            with Horizontal(id="codex-row"):
                yield Button("Login with ChatGPT (browser)", id="oauth")
            yield Label("…or authenticate Codex with an OpenAI API key")
            with Horizontal(id="apikey-row"):
                yield Input(password=True, id="openai_key", placeholder="sk-…")
                yield Button("Use API key", id="apikey")

            with Horizontal(id="config-buttons"):
                yield Button("Save & connect", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#lmh_key", Input).focus()
        self._refresh_status()

    @work(exclusive=True, group="codex-status")
    async def _refresh_status(self) -> None:
        self.query_one("#codex_status", Static).update(Text("Codex auth: checking…"))
        s = await codex_status(self.cfg)
        self.query_one("#codex_status", Static).update(Text(f"Codex auth: {s}"))

    @on(Button.Pressed, "#oauth")
    def _login_oauth(self) -> None:
        # Import lazily and run under suspend so codex owns the terminal
        # for its device-flow URL / browser handoff.
        from .codex_auth import login_oauth

        with self.app.suspend():
            ok, msg = login_oauth(self.cfg)
        self.app.notify(msg, severity="information" if ok else "error")
        self._refresh_status()

    @on(Button.Pressed, "#apikey")
    def _login_api_key(self) -> None:
        key = self.query_one("#openai_key", Input).value.strip()
        if not key:
            self.app.notify("enter an OpenAI API key first", severity="warning")
            return
        self._do_api_key(key)

    @work(exclusive=True, group="codex-apikey")
    async def _do_api_key(self, key: str) -> None:
        ok, msg = await login_with_api_key(self.cfg, key)
        self.app.notify(msg, severity="information" if ok else "error")
        self.query_one("#openai_key", Input).value = ""
        self._refresh_status()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.dismiss(
            {
                "api_key": self.query_one("#lmh_key", Input).value.strip(),
                "base_url": self.query_one("#lmh_base", Input).value.strip(),
            }
        )

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
