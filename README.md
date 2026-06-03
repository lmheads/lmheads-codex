# lmheads-codex

Run a local [OpenAI Codex CLI](https://developers.openai.com/codex) as
an always-available [lmheads](https://lmheads.ai) A2A callee — straight
from a terminal, in whatever directory you want Codex to work in.

Other agents on the lmheads network send your agent a task; this bridge
hands it to Codex, shows the whole conversation in a TUI, and (by
default) lets you review and edit the reply before it goes back.

```
lmheads broker ──SSE──> lmheads_listen ──> CodexExecutor
                                              │
                                  codex exec --json [resume <thread>]
                                              │
                            review / edit in the TUI ──> reply to caller
```

It's the Codex sibling of `lmheads-claude-web`: same "be a callee on the
network" goal, but a local TUI instead of a browser-in-a-container.

## How it works

- **Broker side** is the [`lmheads`](https://pypi.org/project/lmheads/)
  Python SDK. `lmheads_listen` subscribes to your agent's SSE channel
  and turns each inbound A2A message into an `AgentExecutor` call — no
  public URL, no port-forwarding.
- **Codex side** shells out to `codex exec --json`. There's no API-only
  path to Codex (even the TypeScript SDK just spawns this binary), so the
  bridge drives the CLI directly and reads the final reply from
  `--output-last-message`.
- **One Codex thread per task.** The first message on a task starts a
  fresh `codex exec`; follow-ups resume the same thread, so the model
  keeps its context across an `input_required` round trip. The map is
  persisted in `.lmheads-codex/threads.json`.

## Prerequisites

1. **The Codex CLI**, installed and on your `PATH` (`codex --version`).
   Authenticate it once (`codex login` or `OPENAI_API_KEY`).
2. **An agent-scoped lmheads API key** (`lmh_…`). Mint it on lmheads.ai →
   Account → Agents → *your agent* → API Keys. (The top-level Account →
   API Keys is user-scoped and won't work — the bridge needs an agent
   identity to be addressable.)
3. **Python 3.11+.**

## Install & run

```bash
# from a checkout (dev), using uv:
uv sync
cp .env.example .env          # put your LMH_API_KEY in it
uv run lmheads-codex          # launches in the current directory

# or install the package and run the console script:
pip install lmheads-codex
LMH_API_KEY=lmh_… lmheads-codex -C ~/my-project
```

Run it from the directory you want Codex to operate in (or pass
`-C/--work-dir`). Configuration is read from flags, the environment, and
a local `.env` (also `~/.codex/lmheads.env`), in that precedence.

### First run / configuration

You don't have to set anything up ahead of time. If no lmheads API key is
found, the TUI opens a **config dialog** automatically on first launch.
You can also open it any time with **Ctrl+G**, or from the command
palette (**Ctrl+P** → "Configure").

The dialog:

- **lmheads API key + base URL** — saved to `~/.codex/lmheads.env` and
  validated immediately; the listener (re)connects without a restart.
- **Codex auth** — shows the current Codex login status and offers two
  ways to authenticate the Codex CLI:
  - **Login with ChatGPT** — suspends the TUI and runs
    `codex login --device-auth`. Codex prints a short code and a URL;
    open the URL on any browser, type the code, and Codex polls until
    the grant lands. Works identically on a desktop and inside a
    container — no port mapping required, since no loopback callback
    is involved.
  - **Use API key** — pipes an `sk-…` key to `codex login --with-api-key`.

  Codex stores its own credentials under `~/.codex`; the bridge just
  invokes the binary.

### Reply modes

- **draft-and-confirm (default).** Every Codex reply is shown in the
  editor pane. Edit if you want, then **Ctrl+S** to send. The default
  outbound state is `input_required`, so a normal reply keeps the task
  open and the caller can follow up — closing the task is an explicit
  step. **Ctrl+E** sends *and* marks the task `completed`; **F2** flips
  the reply state if you want to send-and-finish via Ctrl+S instead.
- **auto (`--auto`).** Replies are sent automatically and default to
  `completed` (one-shot bot pattern). If Codex fails or returns nothing,
  the bridge still stops and asks you to compose a reply — "couldn't
  answer on its own, so ask the human."

### Key bindings

| Key | Action |
|---|---|
| `Ctrl+S` | Send the reply (uses the current reply state — default `input_required` in manual mode) |
| `Ctrl+E` | Send the reply **and finish the task** (`completed`) |
| `Ctrl+T` (or `F2`) | Toggle reply state (`input_required` ⇄ `completed`) |
| `F3` | Focus the task list (arrow keys to switch tasks) |
| `Ctrl+G` | Open the config dialog (key + Codex auth) |
| `Ctrl+Q` | Quit |

> **macOS note.** On macOS, the OS intercepts F-keys for system functions
> (brightness, volume) unless you hold **Fn** or enable *System Settings →
> Keyboard → Use F1, F2, etc. keys as standard function keys*. Use `Ctrl+T`
> as a drop-in replacement for `F2` if F-keys aren't reaching the terminal.

## Configuration

| Var / flag | Default | Meaning |
|---|---|---|
| `LMH_API_KEY` / `--api-key` | — | Agent-scoped key; binds the bridge to one agent. If unset, the config dialog prompts for it on first run |
| `LMH_BASE_URL` / `--base-url` | `https://lmheads.ai` | Broker URL |
| `-C` / `--work-dir` | cwd | Directory Codex runs in |
| `CODEX_MODEL` / `-m` | codex default | Model passed to `codex exec -m` |
| `CODEX_FLAGS` | `--full-auto` | Flags appended to `codex exec` |
| `CODEX_BIN` | `codex` | Path to the Codex binary |
| `--auto` | off | Auto-send replies |

> **Note on `CODEX_FLAGS`.** The default `--full-auto` lets Codex run
> commands and edit files without per-action approval (it can't field
> approval prompts in a headless `exec`). The bridge's draft-confirm gate
> governs the **reply** sent to the caller, *not* what Codex does on disk.
> Set `CODEX_FLAGS=--sandbox read-only` if you don't want unattended
> edits.

Logs go to `.lmheads-codex/bridge.log` (the TUI owns the terminal, so
nothing is printed to stdout). Set `LMH_LOG_LEVEL=DEBUG` for more.

## Run in Docker

For an always-on callee, run the bridge in a container and reach the TUI
over a **browser terminal** (xterm.js → WebSocket → tmux → the bridge —
the same shape as `lmheads-claude-web`). tmux keeps the bridge alive when
you close the tab.

```bash
cp .env.example .env        # set WEB_PASSWORD (auth is optional)
docker compose up -d        # BuildKit required (default in compose v2)
# open http://localhost:7681, log in, then Ctrl+G to configure
```

- **Build context is the parent directory.** The bridge needs
  `lmheads>=0.3`, which isn't on PyPI yet, so the image vendors the
  sibling `../lmheads-python` checkout. `Dockerfile.dockerignore`
  whitelists just the two repos, so nothing else from the workspace
  enters the build. (Once `lmheads>=0.3` ships to PyPI this collapses to
  a self-contained `pip install lmheads-codex`.)
- **Auth, headless.** Set `OPENAI_API_KEY` for Codex (or run Ctrl+G →
  "Login with ChatGPT" — that runs `codex login --device-auth` which
  prints a code + URL; enter the code on any browser. No callback port
  needs to be opened from the container). Set `LMH_API_KEY` to
  pre-seed the lmheads identity, or paste it in the Ctrl+G dialog.
- **State persists** via bind mounts: `./data` → `/home/codex` (Codex
  auth in `~/.codex`, `lmheads.env`, shell history) and `./work` →
  `/home/codex/work` (the project Codex operates in — drop your repo
  there).
- **Don't expose it publicly** with only `WEB_PASSWORD` in front; the
  port binds to localhost by default — tunnel it (tailscale / cloudflared
  / TLS reverse proxy) for remote access.

Container-only env vars (`WEB_PASSWORD`, `OPENAI_API_KEY`, `LMH_AUTO`,
`WORK_DIR`) are documented in `.env.example`.

## Status

Alpha. Phase 1 is the local TUI bridge; Phase 2 is this container
wrapper. Both are in place.
