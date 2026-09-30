"""The contract every integration implements.

An integration is a self-contained capability bolted onto the agent — a
knowledge base, a CRM lookup, a calendar, an order system. Each one can
contribute in two ways:

* **Tools** the LLM may choose to call (precise, but costs an extra LLM
  round-trip, which is expensive in a voice call).
* **Turn context** injected before generation via ``on_user_turn``, which
  avoids that round-trip but runs on every turn.

Adding an integration means writing one module and registering it in
``integrations/__init__.py``. Nothing in the agent, session, or provider layers
needs to change.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from livekit.agents import ChatContext, ChatMessage, FunctionTool

logger = logging.getLogger("ai_caller.integrations")


@runtime_checkable
class Integration(Protocol):
    """A capability the agent can use."""

    name: str

    def tools(self) -> list[FunctionTool]:
        """Function tools to expose to the LLM. May be empty."""
        ...

    async def on_user_turn(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        """Optionally add context to `turn_ctx` before the LLM generates a reply.

        Runs on the critical path of the turn. Implementations must be fast and
        must not raise: a failure here should degrade the answer, never drop the
        call.
        """
        ...

    async def aclose(self) -> None:
        """Release any held resources."""
        ...


class BaseIntegration:
    """Convenience base with no-op defaults, so subclasses implement only what they use."""

    name: str = "integration"

    def tools(self) -> list[FunctionTool]:
        return []

    async def on_user_turn(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None


async def run_turn_hooks(
    integrations: list[Integration],
    turn_ctx: ChatContext,
    new_message: ChatMessage,
) -> None:
    """Fan a completed user turn out to every integration.

    Each hook is isolated: one integration failing or timing out must not stop
    the others, and must not prevent the agent from replying.
    """
    for integration in integrations:
        try:
            await integration.on_user_turn(turn_ctx, new_message)
        except Exception:
            logger.exception("integration %r failed on user turn", integration.name)


async def close_all(integrations: list[Integration]) -> None:
    for integration in integrations:
        try:
            await integration.aclose()
        except Exception:
            logger.exception("integration %r failed to close", integration.name)
