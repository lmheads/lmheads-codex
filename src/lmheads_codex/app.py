"""Textual TUI: live task list + conversation, with the draft-confirm gate.

The app owns the asyncio loop. On mount it starts two workers:

  * ``lmheads_listen`` — the broker consumer (from lmheads-python). Its
    executor (``CodexExecutor``) pushes UI events onto the bridge queue.
  * an event-drain loop that turns those events into widget updates.

Both run on the app loop, so widget mutation is direct (no thread
hand-off). The operator reviews each Codex reply in the bottom editor
and sends it with Ctrl+S; F2 flips the outbound state between
``completed`` and ``input_required``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from a2a.server.request_handlers import DefaultRequestHandler
from lmheads import lmheads_listen, whoami
from lmheads.discover import NotAgentScopedError
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static, TextArea
from textual.worker import Worker

from .bridge import Bridge
from .config import Config, save_lmheads_env
from .events import (
    COMPLETED,
    INPUT_REQUIRED,
    AwaitingConfirm,
    CallerMessage,
    CodexFinished,
    CodexOutput,
    CodexStarted,
    Decision,
    Notice,
    Replied,
    TaskFailed,
    TaskOpened,
)
from .screens import ConfigScreen

log = logging.getLogger("lmheads_codex.app")


@dataclass
class TaskCard:
    task_id: str
    context_id: str
    status: str = "submitted"
    lines: list[str] = field(default_factory=list)  # rendered transcript (markup)
    awaiting: AwaitingConfirm | None = None
    reply_state: str = COMPLETED  # operator's chosen outbound state
    codex_tail: str = ""  # transient live working text

    @property
    def short(self) -> str:
        return self.task_id[:8] or "?"


class CodexApp(App):
    CSS = """
    #body { height: 1fr; }
    #tasks { width: 32; border-right: solid $panel; }
    #right { width: 1fr; }
    #transcript { height: 1fr; border: round $panel; padding: 0 1; }
    #status { height: auto; padding: 0 1; color: $text-muted; }
    #reply { height: 8; border: round $panel; }
    """

    BINDINGS = [
        ("ctrl+s", "send", "Send reply"),
        ("f2", "toggle_state", "Toggle reply state"),
        ("f3", "focus_tasks", "Task list"),
        ("ctrl+g", "configure", "Configure"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        cfg: Config,
        bridge: Bridge,
        handler: DefaultRequestHandler,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.bridge = bridge
        self.handler = handler
        self.agent_name = ""
        self.cards: dict[str, TaskCard] = {}
        self.active_id: str | None = None
        self._listener: Worker | None = None
        self._config_open = False

    def get_system_commands(self, screen: Screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Configure",
            "Set the lmheads API key and authenticate Codex",
            self.action_configure,
        )

    # ── layout ───────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield RichLog(id="transcript", wrap=True, markup=True, highlight=False)
                yield Static("", id="status")
                yield TextArea(id="reply")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "lmheads-codex"
        self._update_subtitle()

        table = self.query_one("#tasks", DataTable)
        table.add_column("Task", key="task")
        table.add_column("Status", key="status")

        reply = self.query_one("#reply", TextArea)
        reply.read_only = True  # editable only while a task awaits a reply

        self._render_status()
        self._write(
            "[dim]Waiting for inbound tasks. "
            "When a reply is drafted: edit below, F2 toggles state, Ctrl+S sends.[/]"
        )

        # @work-decorated: calling it schedules the worker on the app
        # loop. (Don't also pass it to run_worker — that double-starts.)
        self._consume_events()
        # _bootstrap branches on whether a key is configured: connect, or
        # open the config modal on first run.
        self._bootstrap()

    # ── connection lifecycle ─────────────────────────────────────────

    @work(name="bootstrap", group="boot", exclusive=True)
    async def _bootstrap(self) -> None:
        """Validate the key, show identity, and (re)start the listener."""
        if not self.cfg.api_key:
            self._open_config(first_run=True)
            return
        try:
            ident = await whoami(self.cfg.api_key, base_url=self.cfg.base_url)
        except NotAgentScopedError as e:
            self._notice(str(e), "error")
            self._open_config()
            return
        except Exception as e:  # noqa: BLE001 — surface, don't crash the UI
            self._notice(f"could not reach lmheads at {self.cfg.base_url}: {e}", "error")
            self._open_config()
            return
        self.agent_name = ident.agent_name
        self._update_subtitle()
        self._notice(
            f"connected as {ident.agent_name or '?'} ({(ident.agent_id or '')[:8]})"
        )
        self._start_listener()

    def _start_listener(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
        self._listener = self._listen()

    @work(name="lmheads-listen", group="listen", exclusive=False)
    async def _listen(self) -> None:
        try:
            await lmheads_listen(
                self.handler,
                api_key=self.cfg.api_key,
                base_url=self.cfg.base_url,
            )
        except NotAgentScopedError as e:
            self._notice(str(e), "error")
        except Exception as e:  # noqa: BLE001 — surface, don't crash the UI
            self._notice(f"listener stopped: {e}", "error")

    @work(name="ui-events", group="bridge", exclusive=False)
    async def _consume_events(self) -> None:
        while True:
            evt = await self.bridge.events.get()
            try:
                self._handle(evt)
            except Exception:  # noqa: BLE001
                log.exception("error handling UI event %r", evt)

    # ── event handling ───────────────────────────────────────────────

    def _handle(self, evt: object) -> None:
        if isinstance(evt, TaskOpened):
            self._open_task(evt)
        elif isinstance(evt, CallerMessage):
            self._append(evt.task_id, f"[bold cyan]caller[/]  {_esc(evt.text)}")
        elif isinstance(evt, CodexStarted):
            verb = "resuming thread" if evt.resumed else "new thread"
            self._set_status_field(evt.task_id, "working")
            self._append(evt.task_id, f"[dim]— codex working ({verb})…[/]")
        elif isinstance(evt, CodexOutput):
            self._codex_output(evt)
        elif isinstance(evt, CodexFinished):
            if evt.ok and evt.final_text:
                self._append(evt.task_id, f"[bold green]codex →[/] {_esc(evt.final_text)}")
        elif isinstance(evt, TaskFailed):
            self._set_status_field(evt.task_id, "error")
            self._append(evt.task_id, f"[bold red]codex error[/] {_esc(evt.error)}")
        elif isinstance(evt, AwaitingConfirm):
            self._await_confirm(evt)
        elif isinstance(evt, Replied):
            label = "completed" if evt.state == COMPLETED else evt.state
            self._set_status_field(evt.task_id, label)
            self._append(
                evt.task_id, f"[bold]you → caller[/] [dim]({label})[/]  {_esc(evt.text)}"
            )
            card = self.cards.get(evt.task_id)
            if card:
                card.awaiting = None
            self._reset_editor()
        elif isinstance(evt, Notice):
            self._notice(evt.text, evt.level)

    def _open_task(self, evt: TaskOpened) -> None:
        if evt.task_id in self.cards:
            return
        card = TaskCard(task_id=evt.task_id, context_id=evt.context_id)
        self.cards[evt.task_id] = card
        table = self.query_one("#tasks", DataTable)
        table.add_row(card.short, card.status, key=evt.task_id)
        if self.active_id is None:
            self._set_active(evt.task_id)

    def _codex_output(self, evt: CodexOutput) -> None:
        card = self.cards.get(evt.task_id)
        if card is None:
            return
        # Discrete step markers (command runs) arrive newline-terminated;
        # token-level streaming does not. Markers go in the transcript;
        # streaming tail just updates the status line.
        if evt.text.endswith("\n"):
            self._append(evt.task_id, f"[dim]{_esc(evt.text.rstrip())}[/]")
        else:
            card.codex_tail = (card.codex_tail + evt.text)[-400:]
            if evt.task_id == self.active_id:
                self._render_status()

    def _await_confirm(self, evt: AwaitingConfirm) -> None:
        card = self.cards.get(evt.task_id)
        if card is None:
            return
        card.awaiting = evt
        card.reply_state = evt.suggested_state
        self._set_status_field(evt.task_id, "awaiting ★")
        if evt.require_human:
            self._append(
                evt.task_id,
                "[bold yellow]needs you[/] — Codex couldn't reply; compose one below.",
            )
        # Pull focus to whichever task just became actionable so the
        # operator isn't typing into a stale draft.
        self._set_active(evt.task_id)
        self._load_editor(card)

    # ── widget helpers ───────────────────────────────────────────────

    def _append(self, task_id: str, line: str) -> None:
        card = self.cards.get(task_id)
        if card is None:
            return
        card.lines.append(line)
        if task_id == self.active_id:
            self._write(line)

    def _write(self, line: str) -> None:
        self.query_one("#transcript", RichLog).write(line)

    def _set_status_field(self, task_id: str, status: str) -> None:
        card = self.cards.get(task_id)
        if card is None:
            return
        card.status = status
        table = self.query_one("#tasks", DataTable)
        try:
            table.update_cell(task_id, "status", status)
        except Exception:  # noqa: BLE001 — row may have been removed
            pass
        if task_id == self.active_id:
            self._render_status()

    def _set_active(self, task_id: str) -> None:
        if task_id not in self.cards:
            return
        self.active_id = task_id
        card = self.cards[task_id]
        rich_log = self.query_one("#transcript", RichLog)
        rich_log.clear()
        for line in card.lines:
            rich_log.write(line)
        self._render_status()
        if card.awaiting is not None:
            self._load_editor(card)
        else:
            self._reset_editor()

    def _load_editor(self, card: TaskCard) -> None:
        if card.task_id != self.active_id:
            return
        reply = self.query_one("#reply", TextArea)
        reply.read_only = False
        reply.text = card.awaiting.draft if card.awaiting else ""
        reply.focus()
        self._render_status()

    def _reset_editor(self) -> None:
        reply = self.query_one("#reply", TextArea)
        reply.text = ""
        reply.read_only = True
        self._render_status()

    def _render_status(self) -> None:
        card = self.cards.get(self.active_id) if self.active_id else None
        if card and card.awaiting is not None:
            msg = (
                f"[reverse] AWAITING REPLY [/] task {card.short} · "
                f"state=[b]{card.reply_state}[/] · "
                f"Ctrl+S send · F2 toggle state"
            )
        elif card and card.status == "working":
            tail = card.codex_tail.replace("\n", " ")[-120:]
            msg = f"task {card.short} · codex working… [dim]{_esc(tail)}[/]"
        else:
            mode = "auto" if self.cfg.auto else "draft-confirm"
            msg = (
                f"mode=[b]{mode}[/] · agent=[b]{self.agent_name or '?'}[/] · "
                f"cwd={self.cfg.work_dir}"
            )
        self.query_one("#status", Static).update(Text.from_markup(msg))

    def _notice(self, text: str, level: str = "info") -> None:
        color = {"warn": "yellow", "error": "red"}.get(level, "blue")
        self._write(f"[{color}]·[/] {_esc(text)}")

    def _update_subtitle(self) -> None:
        mode = "auto" if self.cfg.auto else "draft-confirm"
        who = self.agent_name or "not connected"
        self.sub_title = f"{who} · {mode} · {self.cfg.work_dir}"

    # ── configuration ────────────────────────────────────────────────

    def _open_config(self, *, first_run: bool = False) -> None:
        if self._config_open:
            return
        self._config_open = True
        if first_run:
            self._notice("no lmheads API key — opening configuration", "warn")
        self.push_screen(ConfigScreen(self.cfg), self._on_config_result)

    def _on_config_result(self, result: dict | None) -> None:
        self._config_open = False
        if not result:
            if not self.cfg.api_key:
                self._notice("not configured — press Ctrl+G to configure", "warn")
            return
        if result.get("api_key"):
            self.cfg.api_key = result["api_key"]
        if result.get("base_url"):
            self.cfg.base_url = result["base_url"].rstrip("/")
        if not self.cfg.api_key:
            self._notice("no API key entered — press Ctrl+G to try again", "warn")
            return
        try:
            path = save_lmheads_env(self.cfg.api_key, self.cfg.base_url)
            self._notice(f"saved config to {path}; reconnecting…")
        except OSError as e:
            self._notice(f"could not save config: {e}", "error")
        self._bootstrap()

    # ── actions ──────────────────────────────────────────────────────

    def action_configure(self) -> None:
        self._open_config()

    def action_send(self) -> None:
        tid = self.active_id
        if not tid or not self.bridge.is_awaiting(tid):
            self._notice("no reply is expected for the selected task", "warn")
            return
        card = self.cards[tid]
        reply = self.query_one("#reply", TextArea)
        text = reply.text.strip()
        if not text:
            self._notice("reply is empty — type a message before sending", "warn")
            return
        self.bridge.resolve(tid, Decision(text=text, state=card.reply_state))
        self._set_status_field(tid, "sending…")
        self._reset_editor()

    def action_toggle_state(self) -> None:
        tid = self.active_id
        if not tid:
            return
        card = self.cards.get(tid)
        if card is None or card.awaiting is None:
            return
        card.reply_state = (
            INPUT_REQUIRED if card.reply_state == COMPLETED else COMPLETED
        )
        self._render_status()

    def action_focus_tasks(self) -> None:
        self.query_one("#tasks", DataTable).focus()

    @on(DataTable.RowHighlighted)
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        if key and key != self.active_id:
            self._set_active(key)


def _esc(text: str) -> str:
    """Escape Rich markup so caller/Codex text can't inject tags."""
    return text.replace("[", r"\[")
