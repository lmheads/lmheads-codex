# lmheads-codex — the Codex A2A bridge in a container, accessed over a
# WebSocket terminal in the browser (same shape as lmheads-claude-web).
# Personal use; one container, one user. The container hosts:
#   • the OpenAI Codex CLI (npm-installed)
#   • the lmheads-codex bridge (Python, this repo)
#   • tmux (so closing the browser doesn't kill the bridge — the
#     always-on-callee story)
#   • a tiny aiohttp server that PTYs the WebSocket into `tmux attach`
#
# BUILD CONTEXT IS THE PARENT DIRECTORY. The bridge needs lmheads>=0.3,
# which isn't on PyPI yet, so we vendor the sibling lmheads-python
# checkout. compose sets `context: ..` + `dockerfile: lmheads-codex/
# Dockerfile`; Dockerfile.dockerignore (BuildKit, sits next to this file)
# whitelists just the two repos so the context tar stays small. Once
# lmheads>=0.3 ships to PyPI this can collapse to a self-contained
# `pip install lmheads-codex` with the repo itself as context.
#
# Persistent state lives under /home/codex (bind-mounted): ~/.codex holds
# Codex's own auth AND lmheads.env, plus bash history. The project Codex
# works in is /home/codex/work (a second mount).

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# tmux for the persistent session, curl for installers, nodejs+npm for
# the Codex CLI, python3 for the bridge + web server, git because Codex
# wants it, locales for sane UTF-8 inside tmux/Textual.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git tmux unzip locales \
        nodejs npm \
        python3 python3-venv \
    && sed -i '/en_US.UTF-8/s/^# //' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# TERM must be set so `tmux attach` can resolve terminal capabilities in
# the PTY children spawned by server.py, and so Textual renders 256-color
# correctly over xterm.js. Without it: "open terminal failed".
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TERM=xterm-256color

# The OpenAI Codex CLI. Lands the `codex` binary on PATH.
RUN npm install -g @openai/codex

# Python runtime in a venv (isolated from system site-packages). aiohttp
# powers the web terminal; the two lmheads packages are installed from
# the vendored sources below.
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir aiohttp==3.10.10
ENV PATH="/opt/venv/bin:${PATH}"

# Install lmheads-python (0.3.x) FIRST so the `lmheads>=0.3` requirement
# of the bridge is already satisfied locally and pip never reaches for
# the older PyPI release.
COPY lmheads-python /src/lmheads-python
RUN pip install --no-cache-dir /src/lmheads-python

# Then the bridge itself (pulls textual + httpx; lmheads already present).
COPY lmheads-codex /src/lmheads-codex
RUN pip install --no-cache-dir /src/lmheads-codex

RUN chmod +x /src/lmheads-codex/entrypoint.sh

# Non-root user. Everything Codex does is sandboxed by the container
# boundary and the unprivileged uid. /home/codex/work is where Codex
# opens by default (bind-mount your project there).
RUN useradd -m -s /bin/bash codex \
    && mkdir -p /home/codex/work \
    && chown -R codex:codex /home/codex
USER codex
WORKDIR /home/codex/work

EXPOSE 7681
ENTRYPOINT ["/src/lmheads-codex/entrypoint.sh"]
