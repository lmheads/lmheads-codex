"""The A2A executor that drives Codex.

``lmheads_listen`` invokes :meth:`CodexExecutor.execute` once per inbound
caller message. Each call:

  1. Surfaces the caller message in the TUI.
  2. Posts ``working`` to the broker so the caller sees activity (the
     operator's confirm step can take a while).
  3. Runs one Codex turn (fresh, or resuming this task's thread).
  4. Asks the operator to confirm/edit the reply (unless ``--auto`` and
     Codex succeeded).
  5. Posts the final state + reply back to the broker.
"""

from __future__ import annotations

import logging
import uuid

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    Message,
    Part,
    Role,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

from .bridge import Bridge
from .codex import run_codex
from .config import Config
from .events import (
    COMPLETED,
    INPUT_REQUIRED,
    CallerMessage,
    CodexFinished,
    CodexOutput,
    CodexStarted,
    Decision,
    Notice,
    Replied,
    TaskFailed,
    TaskOpened,
)
from .threads import ThreadStore

log = logging.getLogger("lmheads_codex.executor")

_WIRE_TO_STATE = {
    COMPLETED: TaskState.TASK_STATE_COMPLETED,
    INPUT_REQUIRED: TaskState.TASK_STATE_INPUT_REQUIRED,
}


def _join_text(message: Message | None) -> str:
    if message is None:
        return ""
    return " ".join(p.text for p in message.parts if getattr(p, "text", None)).strip()


class CodexExecutor(AgentExecutor):
    def __init__(self, *, cfg: Config, bridge: Bridge, threads: ThreadStore) -> None:
        super().__init__()
        self.cfg = cfg
        self.bridge = bridge
        self.threads = threads

    async def execute(self, ctx: RequestContext, queue: EventQueue) -> None:
        task_id = ctx.task_id or ""
        context_id = ctx.context_id or ""
        incoming = _join_text(ctx.message) or "(empty message)"

        thread_id = self.threads.get(task_id)
        resumed = thread_id is not None
        if not resumed:
            self.bridge.emit(TaskOpened(task_id=task_id, context_id=context_id))
        self.bridge.emit(CallerMessage(task_id=task_id, text=incoming))

        await self._emit_state(queue, ctx, TaskState.TASK_STATE_WORKING, None)
        self.bridge.emit(CodexStarted(task_id=task_id, resumed=resumed))

        result = await run_codex(
            self.cfg,
            prompt=incoming,
            thread_id=thread_id,
            on_output=lambda t: self.bridge.emit(CodexOutput(task_id=task_id, text=t)),
        )
        if result.thread_id:
            self.threads.set(task_id, result.thread_id)

        self.bridge.emit(
            CodexFinished(task_id=task_id, final_text=result.final_text, ok=result.ok)
        )

        if result.ok and result.final_text:
            draft = result.final_text
            require_human = False
            suggested = COMPLETED
        else:
            err = result.error or result.stderr or "Codex produced no output."
            self.bridge.emit(TaskFailed(task_id=task_id, error=err))
            draft = ""  # operator composes the reply from scratch
            require_human = True
            suggested = INPUT_REQUIRED

        decision: Decision = await self.bridge.confirm(
            task_id=task_id,
            draft=draft,
            suggested_state=suggested,
            require_human=require_human,
        )

        state = _WIRE_TO_STATE.get(decision.state, TaskState.TASK_STATE_COMPLETED)
        await self._emit_state(queue, ctx, state, decision.text)
        self.bridge.emit(
            Replied(task_id=task_id, state=decision.state, text=decision.text)
        )

    async def cancel(self, ctx: RequestContext, queue: EventQueue) -> None:
        task_id = ctx.task_id or ""
        self.bridge.cancel_pending(task_id)
        self.bridge.emit(
            Notice(text=f"task {task_id[:8]} canceled by caller", level="warn")
        )

    async def _emit_state(
        self,
        queue: EventQueue,
        ctx: RequestContext,
        state: int,
        text: str | None,
    ) -> None:
        status = TaskStatus(state=state)
        if text:
            status = TaskStatus(
                state=state,
                message=Message(
                    message_id=uuid.uuid4().hex,
                    task_id=ctx.task_id or "",
                    context_id=ctx.context_id or "",
                    role=Role.ROLE_AGENT,
                    parts=[Part(text=text)],
                ),
            )
        await queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=ctx.task_id or "",
                context_id=ctx.context_id or "",
                status=status,
            )
        )
