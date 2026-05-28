"""Persistent map of A2A task id -> Codex thread (session) id.

One Codex thread per task: the first inbound message on a task starts a
fresh ``codex exec``; follow-ups (e.g. after an ``input_required`` round
trip) resume the same Codex session so the model keeps its context. The
map is persisted so a bridge restart doesn't orphan in-flight tasks.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("lmheads_codex.threads")


class ThreadStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                self._map = {str(k): str(v) for k, v in data.items() if v}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("could not read thread store %s: %s", self._path, e)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._map, indent=2))
        except OSError as e:
            log.warning("could not write thread store %s: %s", self._path, e)

    def get(self, task_id: str) -> str | None:
        return self._map.get(task_id)

    def set(self, task_id: str, thread_id: str) -> None:
        if not task_id or not thread_id:
            return
        if self._map.get(task_id) == thread_id:
            return
        self._map[task_id] = thread_id
        self._save()
