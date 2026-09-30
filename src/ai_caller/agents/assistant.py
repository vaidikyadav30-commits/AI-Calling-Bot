"""The voice assistant.

The agent knows nothing about any specific integration or transport. It collects
whatever tools the enabled integrations contribute and forwards each completed
user turn to them, so adding a capability never means editing this file.

``extra_instructions`` and ``extra_tools`` are the two seams for things that
depend on *how* the session was reached rather than on what it can do — a phone
call adds call-handling rules and the ability to hang up, a browser session adds
neither. The caller decides; this file stays the same either way.
"""

from __future__ import annotations

import logging

from livekit.agents import Agent, ChatContext, ChatMessage, llm

from ai_caller.config import Settings
from ai_caller.integrations import Integration, run_turn_hooks
from ai_caller.prompts import INSTRUCTIONS
from ai_caller.providers import build_llm

logger = logging.getLogger("ai_caller.agent")


def _tool_name(tool: object) -> str:
    """Readable name for a function tool or a toolset, for the startup log."""
    return str(getattr(tool, "name", None) or getattr(tool, "id", None) or tool)


class Assistant(Agent):
    def __init__(
        self,
        settings: Settings | None = None,
        integrations: list[Integration] | None = None,
        *,
        extra_instructions: str = "",
        extra_tools: list[llm.Tool | llm.Toolset] | None = None,
    ) -> None:
        settings = settings or Settings.from_env()
        self._integrations = integrations or []

        tools: list[llm.Tool | llm.Toolset] = [
            tool for integration in self._integrations for tool in integration.tools()
        ]
        tools.extend(extra_tools or [])
        if tools:
            logger.info("agent tools: %s", [_tool_name(tool) for tool in tools])

        super().__init__(
            llm=build_llm(settings.llm),
            instructions=INSTRUCTIONS + extra_instructions,
            tools=tools,
        )

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        """Give integrations a chance to add context before the LLM replies.

        Injecting here rather than through a tool call avoids an extra LLM
        round-trip, which is the single biggest latency win available in a
        voice pipeline.
        See https://docs.livekit.io/agents/logic/external-data/
        """
        await run_turn_hooks(self._integrations, turn_ctx, new_message)
