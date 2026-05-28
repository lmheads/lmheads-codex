"""Tiny aiohttp WebSocket terminal for the lmheads-codex bridge.

Wire shape:

  Browser (xterm.js) ─── WebSocket /ws ──► server.py
                                              │
                                              ▼
                                    PTY (pty.fork) ──► tmux attach -t codex
                                                              │
                                                              ▼
                                                     running `lmheads-codex`

Each WS connection forks a fresh PTY and runs `tmux attach`. Tmux holds
the *real* bridge session — disconnecting (closing the browser tab) just
kills this attachment; the bridge keeps running and the next connection
reattaches and repaints.

Auth: a single shared `WEB_PASSWORD` checked on the WS handshake.
Single-user; no sessions, no cookies. The first client message must be
`{"type":"auth","password":"…"}`.

After auth, two binary streams flow simultaneously:
  • PTY → WS as binary frames (raw terminal bytes, ANSI escapes intact)
  • WS → PTY as binary frames (keystrokes)
A JSON control channel rides on text frames for resize:
  `{"type":"resize","cols":N,"rows":M}` triggers TIOCSWINSZ on the PTY.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import termios
from pathlib import Path

from aiohttp import WSMsgType, web

PASSWORD = os.environ.get("WEB_PASSWORD", "")
PORT = int(os.environ.get("WEB_PORT", "7681"))
TMUX_SESSION = os.environ.get("TMUX_SESSION", "codex")
STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger("codex-web")


# ── HTTP routes ─────────────────────────────────────────────────────


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    peer = request.remote or "?"
    log.info("ws connect from %s", peer)

    # First message must be auth. Reject anything else.
    try:
        msg = await ws.receive(timeout=10)
    except asyncio.TimeoutError:
        await ws.close(code=1008, message=b"auth timeout")
        return ws

    if msg.type != WSMsgType.TEXT:
        await ws.close(code=1008, message=b"auth required")
        return ws

    try:
        auth = json.loads(msg.data)
    except json.JSONDecodeError:
        await ws.close(code=1008, message=b"bad auth")
        return ws

    if auth.get("type") != "auth" or auth.get("password") != PASSWORD:
        log.warning("ws auth failed from %s", peer)
        await ws.send_json({"type": "error", "message": "auth failed"})
        await ws.close(code=1008)
        return ws

    await ws.send_json({"type": "ready"})
    log.info("ws auth ok from %s", peer)

    # Fork a PTY running `tmux attach`. Missing-session is an error we let
    # surface so the client sees it in the terminal.
    pid, fd = pty.fork()
    if pid == 0:
        # Child process. exec replaces this with tmux.
        try:
            os.execvp("tmux", ["tmux", "attach-session", "-t", TMUX_SESSION])
        except FileNotFoundError:
            os.write(2, b"tmux not found in container\n")
            os._exit(127)

    log.info("spawned pty pid=%d fd=%d for %s", pid, fd, peer)

    # Non-blocking PTY so the event loop doesn't pin a thread.
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    closed = asyncio.Event()

    def on_pty_readable() -> None:
        try:
            data = os.read(fd, 8192)
        except BlockingIOError:
            return
        except OSError:
            closed.set()
            return
        if not data:
            closed.set()
            return
        asyncio.create_task(_safe_send_bytes(ws, data))

    loop.add_reader(fd, on_pty_readable)

    async def ws_to_pty() -> None:
        async for ws_msg in ws:
            if ws_msg.type == WSMsgType.BINARY:
                try:
                    os.write(fd, ws_msg.data)
                except OSError:
                    return
            elif ws_msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(ws_msg.data)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "resize":
                    cols = int(payload.get("cols") or 0)
                    rows = int(payload.get("rows") or 0)
                    if cols > 0 and rows > 0:
                        try:
                            winsize = struct.pack("HHHH", rows, cols, 0, 0)
                            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                        except OSError:
                            pass
            elif ws_msg.type in (
                WSMsgType.CLOSE,
                WSMsgType.CLOSING,
                WSMsgType.CLOSED,
                WSMsgType.ERROR,
            ):
                return

    async def wait_pty_close() -> None:
        await closed.wait()

    done, pending = await asyncio.wait(
        [asyncio.create_task(ws_to_pty()), asyncio.create_task(wait_pty_close())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()

    # Cleanup. tmux attach exits with SIGHUP; the underlying tmux session
    # keeps running detached.
    try:
        loop.remove_reader(fd)
    except ValueError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        pass

    log.info("ws closed for %s (pid=%d)", peer, pid)
    return ws


async def _safe_send_bytes(ws: web.WebSocketResponse, data: bytes) -> None:
    """Send bytes, swallowing the post-close error aiohttp raises if the
    client vanished mid-frame."""
    if ws.closed:
        return
    try:
        await ws.send_bytes(data)
    except (ConnectionResetError, RuntimeError):
        pass


# ── App init ────────────────────────────────────────────────────────


def make_app() -> web.Application:
    if not PASSWORD:
        raise RuntimeError(
            "WEB_PASSWORD env var is required — set it in your .env / "
            "compose file. Single-user shared password by design."
        )
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC_DIR)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    web.run_app(make_app(), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
