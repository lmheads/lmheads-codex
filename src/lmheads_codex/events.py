"""Messages passed from the executor to the TUI, and the operator's reply.

The executor (which runs inside ``lmheads_listen``) and the Textual app
never call each other directly — they communicate through
:class:`~lmheads_codex.bridge.Bridge`, which carries these immutable
event objects one way and :class:`Decision` objects back. Keeping them
as plain dataclasses (no Textual imports) means the executor stays
testable and UI-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

# Wire-level A2A states the operator can choose for an outbound reply.
COMPLETED = "completed"
INPUT_REQUIRED = "input_required"


@dataclass
class TaskOpened:
    task_id: str
    context_id: str


@dataclass
class CallerMessage:
    task_id: str
    text: str


@dataclass
class CodexStarted:
    task_id: str
    resumed: bool


@dataclass
class CodexOutput:
    task_id: str
    text: str


@dataclass
class CodexFinished:
    task_id: str
    final_text: str
    ok: bool


@dataclass
class AwaitingConfirm:
    task_id: str
    draft: str
    suggested_state: str
    # True when Codex failed or returned nothing: even in --auto mode the
    # bridge stops here and asks the operator to compose the reply.
    require_human: bool


@dataclass
class Replied:
    task_id: str
    state: str
    text: str


@dataclass
class TaskFailed:
    task_id: str
    error: str


@dataclass
class Notice:
    text: str
    level: str = "info"  # info | warn | error


@dataclass
class Decision:
    """The operator's verdict on a drafted reply."""

    text: str
    state: str  # COMPLETED | INPUT_REQUIRED
