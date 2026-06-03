"""Authenticate the Codex CLI on the operator's behalf.

Codex owns its own credential store (``~/.codex``); we never touch it
directly. We just invoke the binary's auth commands:

  * ``codex login`` — ChatGPT OAuth (device flow + PKCE, opens a
    browser). Interactive, so the TUI suspends and lets it own the
    terminal (see :func:`login_oauth`).
  * ``codex login --with-api-key`` — reads an OpenAI API key from stdin.
    Non-interactive, so we pipe it (see :func:`login_with_api_key`).
  * ``codex login status`` — reports the current auth state.

This keeps us decoupled from where/how Codex persists tokens.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from .config import Config


async def status(cfg: Config) -> str:
    """Best-effort human-readable Codex auth state."""
    try:
        proc = await asyncio.create_subprocess_exec(
            cfg.codex_bin,
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except FileNotFoundError:
        return f"codex binary not found ('{cfg.codex_bin}')"

    text = (out or b"").decode("utf-8", "replace").strip()
    if proc.returncode == 0 and text:
        return text
    # Older Codex builds may not have `login status`; fall back to the
    # presence of the credential file.
    if (Path.home() / ".codex" / "auth.json").exists():
        return "authenticated (~/.codex/auth.json present)"
    err_text = (err or b"").decode("utf-8", "replace").strip()
    return text or err_text or "not logged in"


async def login_with_api_key(cfg: Config, api_key: str) -> tuple[bool, str]:
    """Authenticate Codex with an OpenAI API key (piped over stdin)."""
    key = api_key.strip()
    if not key:
        return False, "no API key provided"
    try:
        proc = await asyncio.create_subprocess_exec(
            cfg.codex_bin,
            "login",
            "--with-api-key",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input=(key + "\n").encode())
    except FileNotFoundError:
        return False, f"codex binary not found ('{cfg.codex_bin}')"

    msg = (
        (out or b"").decode("utf-8", "replace").strip()
        or (err or b"").decode("utf-8", "replace").strip()
    )
    ok = proc.returncode == 0
    return ok, msg or ("logged in with API key" if ok else "codex login failed")


def login_oauth(cfg: Config) -> tuple[bool, str]:
    """Run ``codex login --device-auth`` synchronously with inherited stdio.

    Call this inside ``App.suspend()`` so Codex can print the device-code
    URL and block while polling — all on the real terminal. Blocking the
    event loop here is fine: the TUI is suspended and there's nothing to
    render until login returns.

    Device flow (not the default loopback flow) on purpose: it works the
    same on a desktop AND inside a container, with no need to publish the
    1455 OAuth callback port. The operator opens the printed URL on any
    browser they have handy, types the displayed code, and Codex polls
    until the grant lands.
    """
    try:
        result = subprocess.run([cfg.codex_bin, "login", "--device-auth"])  # noqa: S603
    except FileNotFoundError:
        return False, f"codex binary not found ('{cfg.codex_bin}')"
    ok = result.returncode == 0
    return ok, ("ChatGPT login complete" if ok else f"codex login exited {result.returncode}")
