"""Drive the OpenAI Codex CLI as a subprocess.

We shell out to ``codex exec --json`` rather than use an SDK: the only
mature Codex SDK is TypeScript, and it too just spawns this same binary
and reads the same JSONL stream. Staying in Python lets us reuse the
lmheads-python broker transport; the Codex side is this thin driver.

Two robustness choices matter here:

  * **The final reply comes from ``--output-last-message``**, a file
    Codex writes with the last assistant message. That contract is
    stable across Codex versions; the JSONL event schema is not.
  * **Streaming progress is best-effort.** We extract text/command
    markers from whatever event shapes we recognise so the operator
    sees activity, but nothing downstream depends on parsing them
    correctly — if a future Codex renames its events, we still get the
    right answer from the output file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger("lmheads_codex.codex")

# Keys under which Codex (across versions / event wrappers) stashes the
# session identifier we resume from. Searched recursively.
_SESSION_KEYS = ("session_id", "thread_id", "conversation_id", "rollout_id")


@dataclass
class CodexResult:
    final_text: str
    thread_id: str | None
    exit_code: int
    stderr: str
    error: str | None = None  # set when we couldn't even run codex

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code == 0


def _build_cmd(cfg: Config, *, prompt: str, thread_id: str | None, last_file: Path) -> list[str]:
    # `-C` is a global codex flag, so it goes before the `exec`
    # subcommand; everything else is exec-scoped and follows it (after
    # the optional `resume <id>` target).
    cmd: list[str] = [cfg.codex_bin, "-C", str(cfg.work_dir), "exec"]
    if thread_id:
        cmd += ["resume", thread_id]
    cmd += ["--json", "--skip-git-repo-check"]
    if cfg.codex_model:
        cmd += ["-m", cfg.codex_model]
    cmd += list(cfg.codex_flags)
    cmd += ["--output-last-message", str(last_file)]
    cmd += [prompt]
    return cmd


def _find_session_id(obj: Any) -> str | None:
    """Depth-first search for the first session-id-like value."""
    if isinstance(obj, dict):
        for k in _SESSION_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            found = _find_session_id(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_session_id(v)
            if found:
                return found
    return None


def _progress_text(event: dict) -> str | None:
    """Best-effort human-readable progress line for a JSONL event.

    Handles the common shapes across Codex versions:
      * legacy ``{"msg": {"type": ..., ...}}`` wrapper
      * ``agent_message`` / ``*_delta`` events
      * newer ``item.*`` events carrying an ``item`` payload
    Returns None for events we don't want to surface (heartbeats, token
    counts, the session-config event, etc.).
    """
    inner = event.get("msg") if isinstance(event.get("msg"), dict) else event
    etype = str(inner.get("type") or "")

    if etype.endswith("delta"):
        d = inner.get("delta")
        return d if isinstance(d, str) and d else None

    if etype in ("agent_message", "assistant_message") or etype.endswith("message"):
        for key in ("message", "text", "content"):
            v = inner.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return None

    item = inner.get("item")
    if isinstance(item, dict):
        itype = str(item.get("type") or "")
        if itype in ("assistant_message", "agent_message"):
            t = item.get("text")
            return t if isinstance(t, str) and t.strip() else None
        if "command" in itype or "exec" in itype:
            cmd = item.get("command") or item.get("cmd") or item.get("name") or ""
            return f"$ {cmd}".strip() if cmd else None
        return None

    if "command" in etype or "exec_command" in etype:
        cmd = inner.get("command") or inner.get("cmd") or ""
        return f"$ {cmd}".strip() if cmd else None

    return None


async def run_codex(
    cfg: Config,
    *,
    prompt: str,
    thread_id: str | None,
    on_output: Callable[[str], None],
) -> CodexResult:
    """Run one Codex turn and return its final assistant message.

    ``on_output`` is called with progress chunks as they stream so the
    TUI can show live activity. The authoritative reply is read from the
    ``--output-last-message`` file after the process exits.
    """
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    last_file = Path(
        tempfile.mkstemp(prefix="codex-last-", suffix=".txt", dir=cfg.state_dir)[1]
    )
    cmd = _build_cmd(cfg, prompt=prompt, thread_id=thread_id, last_file=last_file)
    log.info("running codex: %s", " ".join(cmd[:-1]) + " <prompt>")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2**20,  # JSONL lines (e.g. large diffs) can exceed the 64 KiB default
        )
    except FileNotFoundError:
        last_file.unlink(missing_ok=True)
        return CodexResult(
            final_text="",
            thread_id=thread_id,
            exit_code=127,
            stderr="",
            error=(
                f"codex binary not found ('{cfg.codex_bin}'). Install the "
                f"OpenAI Codex CLI and ensure it is on PATH, or set CODEX_BIN."
            ),
        )

    found_session: str | None = None
    saw_delta = False

    async def pump_stdout() -> None:
        nonlocal found_session, saw_delta
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            if found_session is None:
                found_session = _find_session_id(event)

            text = _progress_text(event)
            if text is None:
                continue
            inner = event.get("msg") if isinstance(event.get("msg"), dict) else event
            etype = str(inner.get("type") or "")
            if etype.endswith("delta"):
                saw_delta = True
                on_output(text)
            elif etype.endswith("message") or "message" in etype:
                # Full agent message: skip if we already streamed it as
                # deltas (avoids printing the assembled copy twice).
                if not saw_delta:
                    on_output(text)
                saw_delta = False
            else:
                on_output(text + "\n")

    stderr_buf = bytearray()

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            stderr_buf.extend(raw)

    try:
        await asyncio.gather(pump_stdout(), pump_stderr())
        exit_code = await proc.wait()
    except asyncio.CancelledError:
        proc.terminate()
        raise

    final_text = ""
    try:
        final_text = last_file.read_text().strip()
    except OSError:
        pass
    finally:
        last_file.unlink(missing_ok=True)

    stderr = stderr_buf.decode("utf-8", "replace").strip()
    error = None
    if exit_code != 0 and not final_text:
        error = stderr or f"codex exited with code {exit_code}"

    return CodexResult(
        final_text=final_text,
        thread_id=found_session or thread_id,
        exit_code=exit_code,
        stderr=stderr,
        error=error,
    )
