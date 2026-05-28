"""Async coupling between the executor and the TUI.

The executor pushes :mod:`~lmheads_codex.events` objects onto a queue the
app drains, and ``await``s :meth:`Bridge.confirm` when it needs the
operator to approve a reply. The app resolves that await via
:meth:`Bridge.resolve`. Everything lives on a single asyncio loop (the
one Textual runs), so these are plain futures — no thread hand-off.
"""

from __future__ import annotations

import asyncio

from .events import AwaitingConfirm, Decision


class Bridge:
    def __init__(self, *, auto: bool) -> None:
        self.auto = auto
        self.events: asyncio.Queue[object] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[Decision]] = {}

    def emit(self, event: object) -> None:
        self.events.put_nowait(event)

    async def confirm(
        self,
        *,
        task_id: str,
        draft: str,
        suggested_state: str,
        require_human: bool,
    ) -> Decision:
        """Block until the operator approves a reply (or auto-send it).

        In ``--auto`` mode a successful draft is sent immediately. A run
        that needs human attention (``require_human``) always waits for
        the operator, even in auto mode — that's the "couldn't reply on
        its own, so ask the user" path.
        """
        if self.auto and not require_human:
            return Decision(text=draft, state=suggested_state)

        fut: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self._pending[task_id] = fut
        self.emit(
            AwaitingConfirm(
                task_id=task_id,
                draft=draft,
                suggested_state=suggested_state,
                require_human=require_human,
            )
        )
        try:
            return await fut
        finally:
            self._pending.pop(task_id, None)

    def resolve(self, task_id: str, decision: Decision) -> bool:
        """Called by the UI when the operator submits a reply."""
        fut = self._pending.get(task_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def is_awaiting(self, task_id: str) -> bool:
        fut = self._pending.get(task_id)
        return fut is not None and not fut.done()

    def cancel_pending(self, task_id: str) -> None:
        fut = self._pending.pop(task_id, None)
        if fut is not None and not fut.done():
            fut.cancel()
