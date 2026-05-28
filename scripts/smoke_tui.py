"""Headless smoke test for the Textual TUI.

Runs the app via Textual's test pilot (no real terminal), stubs out the
network bootstrap, drives a full task lifecycle through the bridge, and
exercises the draft-confirm send path plus the config modal. Catches
widget-API mistakes without needing a live broker or the codex binary.
"""

import asyncio
import os
import tempfile
from pathlib import Path

# Redirect HOME so save_lmheads_env writes into a throwaway dir, not the
# operator's real ~/.codex/lmheads.env.
_TMP_HOME = tempfile.mkdtemp(prefix="lmh-codex-test-home-")
os.environ["HOME"] = _TMP_HOME

from lmheads_codex.app import CodexApp  # noqa: E402
from lmheads_codex.bridge import Bridge  # noqa: E402
from lmheads_codex.config import Config, save_lmheads_env  # noqa: E402
from lmheads_codex.events import (  # noqa: E402
    AwaitingConfirm,
    CallerMessage,
    CodexFinished,
    CodexOutput,
    CodexStarted,
    TaskOpened,
)
from lmheads_codex.screens import ConfigScreen  # noqa: E402


def test_save_env_roundtrip() -> None:
    p = save_lmheads_env("lmh_abc", "https://lmheads.ai")
    body = p.read_text()
    assert "LMH_API_KEY=lmh_abc" in body, body
    assert "LMH_BASE_URL" not in body, "default base URL should be omitted"
    p2 = save_lmheads_env("lmh_xyz", "https://staging.example/")
    body2 = p2.read_text()
    assert "LMH_API_KEY=lmh_xyz" in body2
    assert "LMH_BASE_URL=https://staging.example" in body2, body2
    print("save_lmheads_env round-trip OK")


async def test_tui() -> None:
    cfg = Config(api_key="lmh_test", work_dir=Path("/tmp"))
    bridge = Bridge(auto=False)
    app = CodexApp(cfg=cfg, bridge=bridge, handler=object())

    # Don't hit the network in the headless test (on_mount would otherwise
    # run whoami via _bootstrap).
    app._bootstrap = lambda: None  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        tid = "abcd1234ef"
        bridge.emit(TaskOpened(task_id=tid, context_id="ctx1"))
        bridge.emit(CallerMessage(task_id=tid, text="please summarize repo [x]"))
        bridge.emit(CodexStarted(task_id=tid, resumed=False))
        bridge.emit(CodexOutput(task_id=tid, text="reading files"))
        bridge.emit(CodexOutput(task_id=tid, text="$ ls\n"))
        bridge.emit(CodexFinished(task_id=tid, final_text="Done: 3 files.", ok=True))
        await pilot.pause()

        assert tid in app.cards, "task card not created"
        assert app.active_id == tid, "task not auto-activated"

        # Simulate the executor awaiting confirmation.
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        bridge._pending[tid] = fut
        bridge.emit(
            AwaitingConfirm(
                task_id=tid,
                draft="Done: 3 files.",
                suggested_state="completed",
                require_human=False,
            )
        )
        await pilot.pause()
        assert bridge.is_awaiting(tid), "bridge should be awaiting confirm"

        app.action_toggle_state()
        assert app.cards[tid].reply_state == "input_required"
        app.action_toggle_state()
        assert app.cards[tid].reply_state == "completed"

        app.query_one("#reply").text = "Edited reply"
        app.action_send()
        await pilot.pause()

        assert fut.done(), "send did not resolve the confirm future"
        decision = fut.result()
        assert decision.text == "Edited reply", decision.text
        assert decision.state == "completed", decision.state

        # ── config modal ────────────────────────────────────────────
        app.cfg.api_key = ""  # pretend unconfigured so the save path runs
        app._open_config()
        await pilot.pause()
        assert isinstance(app.screen, ConfigScreen), type(app.screen)
        app.screen.query_one("#lmh_key").value = "lmh_new"
        app.screen.query_one("#lmh_base").value = "https://lmheads.ai"
        app.screen._save()
        await pilot.pause()

        assert app.cfg.api_key == "lmh_new", app.cfg.api_key
        env_file = Path(_TMP_HOME) / ".codex" / "lmheads.env"
        assert env_file.exists(), "config save did not write the env file"
        assert "LMH_API_KEY=lmh_new" in env_file.read_text()

    print("TUI smoke test passed")


async def main() -> None:
    test_save_env_roundtrip()
    await test_tui()


if __name__ == "__main__":
    asyncio.run(main())
