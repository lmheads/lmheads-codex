"""Console entry point: ``lmheads-codex``.

Resolves config from flags + environment (and a local ``.env``), checks
the API key is agent-scoped, builds the a2a-sdk handler around
:class:`~lmheads_codex.executor.CodexExecutor`, and launches the Textual
TUI which runs the broker consumer.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard

from . import __version__
from .app import CodexApp
from .bridge import Bridge
from .config import Config, lmheads_env_path, load_dotenv
from .executor import CodexExecutor
from .threads import ThreadStore


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lmheads-codex",
        description=(
            "Run a local OpenAI Codex CLI as an lmheads A2A callee. Launch it "
            "from the directory you want Codex to work in; inbound tasks on "
            "your agent appear in the TUI, one Codex thread per task."
        ),
    )
    p.add_argument("--api-key", help="agent-scoped lmh_… key (else $LMH_API_KEY)")
    p.add_argument("--base-url", help="lmheads server URL (else $LMH_BASE_URL)")
    p.add_argument(
        "-C", "--work-dir", help="directory Codex operates in (default: cwd)"
    )
    p.add_argument("-m", "--model", help="Codex model (else $CODEX_MODEL)")
    p.add_argument(
        "--auto",
        action="store_true",
        help="auto-send Codex replies; only stop for human input when Codex fails",
    )
    p.add_argument("--env-file", help="extra .env file to load")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _setup_logging(cfg: Config) -> None:
    # Textual owns the terminal, so logs must go to a file — a stderr
    # handler would scribble over the TUI. Tail .lmheads-codex/bridge.log
    # to debug.
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(cfg.state_dir / "bridge.log")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(os.environ.get("LMH_LOG_LEVEL", "INFO"))
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Load .env files before reading the environment. The local file wins
    # over the user-global one (load_dotenv never overwrites an existing
    # var), and a shell export still beats both.
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(lmheads_env_path())
    if args.env_file:
        load_dotenv(Path(args.env_file))

    # A missing key is non-fatal: the app opens the config modal on first
    # run. Identity resolution + listener start happen inside the app once
    # a key is present, so nothing here touches the network.
    cfg = Config.from_env_and_args(
        api_key=args.api_key,
        base_url=args.base_url,
        work_dir=args.work_dir,
        auto=args.auto,
        model=args.model,
    )

    _setup_logging(cfg)

    bridge = Bridge(auto=cfg.auto)
    threads = ThreadStore(cfg.state_dir / "threads.json")
    executor = CodexExecutor(cfg=cfg, bridge=bridge, threads=threads)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=AgentCard(
            name="codex-agent",  # cosmetic; broker owns the real identity
            description="Local OpenAI Codex bridged onto the lmheads A2A network.",
            version=__version__,
            capabilities=AgentCapabilities(streaming=True),
        ),
    )

    app = CodexApp(cfg=cfg, bridge=bridge, handler=handler)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
