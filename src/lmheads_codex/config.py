"""Runtime configuration — resolved once from CLI args + environment."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    api_key: str
    base_url: str = "https://lmheads.ai"

    # Directory Codex operates in (`codex exec -C <work_dir>`). Defaults
    # to the directory the bridge was launched from — "run the TUI from
    # your working directory" is the whole point.
    work_dir: Path = field(default_factory=Path.cwd)

    # When True, Codex's reply is sent to the caller automatically. When
    # False (the default), every reply is shown in the TUI and waits for
    # the operator to confirm/edit before it goes out. Even in auto mode
    # a failed Codex run still falls back to asking the operator.
    auto: bool = False

    codex_bin: str = "codex"
    codex_model: str | None = None
    # Flags appended to every `codex exec` call. `--full-auto` lets Codex
    # run commands / edit files without per-action approval prompts — the
    # bridge can't field those prompts in a headless exec, so we need a
    # non-interactive approval posture for the agent to do real work.
    codex_flags: list[str] = field(default_factory=lambda: ["--full-auto"])

    @property
    def state_dir(self) -> Path:
        return self.work_dir / ".lmheads-codex"

    @classmethod
    def from_env_and_args(
        cls,
        *,
        api_key: str | None,
        base_url: str | None,
        work_dir: str | None,
        auto: bool,
        model: str | None,
    ) -> Config:
        # A missing key is no longer fatal: the app mounts and opens the
        # config modal on first run so the operator can paste one in.
        key = api_key or os.environ.get("LMH_API_KEY", "")

        flags_env = os.environ.get("CODEX_FLAGS")
        flags = shlex.split(flags_env) if flags_env is not None else ["--full-auto"]

        wd = Path(work_dir).expanduser() if work_dir else Path.cwd()

        return cls(
            api_key=key,
            base_url=(base_url or os.environ.get("LMH_BASE_URL") or "https://lmheads.ai").rstrip("/"),
            work_dir=wd.resolve(),
            auto=auto,
            codex_bin=os.environ.get("CODEX_BIN", "codex"),
            codex_model=model or os.environ.get("CODEX_MODEL") or None,
            codex_flags=flags,
        )


def lmheads_env_path() -> Path:
    """User-global config file for the Codex bridge (``~/.codex/lmheads.env``).

    Co-located with Codex's own config dir so it's clearly the Codex
    companion, while staying out of the Claude plugin's namespace.
    """
    return Path.home() / ".codex" / "lmheads.env"


def save_lmheads_env(api_key: str, base_url: str) -> Path:
    """Persist the lmheads key (+ non-default base URL) for next launch.

    Writes :func:`lmheads_env_path` (``~/.codex/lmheads.env``) with 0600
    perms since it holds a credential. Returns the path written.
    """
    path = lmheads_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"LMH_API_KEY={api_key}"]
    base = (base_url or "").rstrip("/")
    if base and base != "https://lmheads.ai":
        lines.append(f"LMH_BASE_URL={base}")
    path.write_text("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader — populate os.environ from KEY=VALUE lines.

    Deliberately tiny (no python-dotenv dependency): handles ``KEY=value``,
    ``export KEY=value``, ``#`` comments, blank lines, and surrounding
    quotes. Existing environment variables win, so an explicit export in
    the shell always overrides the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
