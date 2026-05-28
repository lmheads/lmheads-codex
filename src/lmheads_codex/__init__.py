"""lmheads-codex — run a local OpenAI Codex CLI as an lmheads A2A callee.

The package wires three pieces together:

  * ``lmheads.lmheads_listen`` (the lmheads-python SDK) owns the broker
    side — it subscribes to the agent's SSE channel and turns inbound
    A2A tasks into ``AgentExecutor`` calls.
  * :class:`lmheads_codex.executor.CodexExecutor` is that executor. For
    each inbound message it drives ``codex exec`` in the launch
    directory, one Codex thread per task.
  * :class:`lmheads_codex.app.CodexApp` is a Textual TUI that shows the
    live task list + conversation and runs the draft-and-confirm gate
    before any reply is sent back to the caller.

Run it with the ``lmheads-codex`` console script (see
:mod:`lmheads_codex.cli`).
"""

__version__ = "0.1.0"
