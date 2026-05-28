#!/usr/bin/env bash
# Container entrypoint. Four jobs:
#   1. Drop empty optional env vars (compose passes "" for unset ones).
#   2. Sanity-check WEB_PASSWORD (fail fast).
#   3. Seed ~/.codex/lmheads.env from LMH_API_KEY if provided, so the
#      bridge boots already authenticated to lmheads.
#   4. Start the lmheads-codex bridge in a detached tmux session (so
#      closing the browser doesn't kill it), then exec the WebSocket
#      server in the foreground.

set -euo pipefail

# ── 0. Drop empty optional env vars ──────────────────────────────────
# `FOO: ${FOO:-}` in compose sets FOO="" when the host doesn't define it.
# Empty-string vars defeat downstream "is this set?" checks, so unset
# them and let file-based config / first-run flows take over.
for var in OPENAI_API_KEY LMH_API_KEY LMH_BASE_URL LMH_AUTO \
           CODEX_MODEL CODEX_FLAGS CODEX_BIN WORK_DIR; do
  if [[ -z "${!var:-}" ]]; then
    unset "$var"
  fi
done

# ── 1. Required env ──────────────────────────────────────────────────
: "${WEB_PASSWORD:?WEB_PASSWORD is required (set it in .env)}"

# Codex auth: either OPENAI_API_KEY (pay-as-you-go), or ChatGPT OAuth via
# the in-TUI config dialog (Ctrl+G → "Login with ChatGPT" — device flow,
# prints a URL you open in your own browser). Codex stores its creds in
# ~/.codex, which survives restarts via the bind mount. Only warn if
# neither is present so the operator knows to log in.
CODEX_AUTH="$HOME/.codex/auth.json"
if [[ -z "${OPENAI_API_KEY:-}" && ! -f "$CODEX_AUTH" ]]; then
  echo "[entrypoint] no OPENAI_API_KEY and no $CODEX_AUTH —"
  echo "[entrypoint]   authenticate Codex in the TUI: Ctrl+G → Login with ChatGPT."
fi

# ── 2. Seed lmheads config if a key was supplied ─────────────────────
LMH_DIR="$HOME/.codex"
LMH_ENV="$LMH_DIR/lmheads.env"
if [[ -n "${LMH_API_KEY:-}" ]]; then
  mkdir -p "$LMH_DIR"
  # Only write if missing / doesn't already pin the key, so a value set
  # via the TUI config dialog isn't clobbered on every restart.
  if [[ ! -f "$LMH_ENV" ]] || ! grep -q '^LMH_API_KEY=' "$LMH_ENV"; then
    {
      echo "LMH_API_KEY=$LMH_API_KEY"
      [[ -n "${LMH_BASE_URL:-}" && "$LMH_BASE_URL" != "https://lmheads.ai" ]] \
        && echo "LMH_BASE_URL=$LMH_BASE_URL"
    } > "$LMH_ENV"
    chmod 600 "$LMH_ENV"
    echo "[entrypoint] seeded $LMH_ENV from container env"
  fi
fi

# ── 3. tmux + web server ─────────────────────────────────────────────
TMUX_SESSION="${TMUX_SESSION:-codex}"

# Where Codex works. Defaults to /home/codex/work (the ./work mount).
WORK_DIR="${WORK_DIR:-$HOME/work}"
if [[ ! -d "$WORK_DIR" ]]; then
  echo "[entrypoint] WORK_DIR=$WORK_DIR doesn't exist, falling back to $HOME"
  WORK_DIR="$HOME"
fi

# Build the bridge invocation. The bridge reads LMH_*/CODEX_* straight
# from the environment; we only translate LMH_AUTO into the --auto flag
# and pin the work dir. `|| bash` drops to a shell if the bridge exits,
# so the web terminal stays usable for debugging.
BRIDGE_CMD="lmheads-codex -C $WORK_DIR"
case "${LMH_AUTO:-}" in
  yes|true|1)
    BRIDGE_CMD="$BRIDGE_CMD --auto"
    echo "[entrypoint] LMH_AUTO=yes — replies will be sent automatically"
    ;;
esac

if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  echo "[entrypoint] starting tmux session '$TMUX_SESSION' in $WORK_DIR"
  tmux new-session -d -s "$TMUX_SESSION" -c "$WORK_DIR" "$BRIDGE_CMD || bash"
fi

echo "[entrypoint] starting web terminal on :${WEB_PORT:-7681}"
exec python3 /src/lmheads-codex/server/server.py
