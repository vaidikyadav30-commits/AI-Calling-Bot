"""Knowledge-base retrieval, wired into the agent as an integration.

Two delivery paths, chosen with ``RAG_MODE``:

* ``auto`` — retrieve on every completed user turn and inject the excerpts into
  the turn context before generation. LiveKit recommends this for STT-LLM-TTS
  pipelines because it avoids the extra LLM round-trip a tool call costs, which
  is the dominant latency term in a voice call.
* ``tool`` — expose ``search_knowledge_base`` and let the LLM decide. More
  precise and cheaper per turn, but adds a round-trip when it fires.

See https://docs.livekit.io/agents/logic/external-data/
"""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import (
    ChatContext,
    ChatMessage,
    FunctionTool,
    RunContext,
    function_tool,
)

from ai_caller.config import EmbeddingSettings, RagSettings
from ai_caller.integrations.base import BaseIntegration
from ai_caller.integrations.rag.store import KnowledgeStore
from ai_caller.prompts import RAG_CONTEXT_TEMPLATE

logger = logging.getLogger("ai_caller.rag")


class RagIntegration(BaseIntegration):
    name = "rag"

    def __init__(self, store: KnowledgeStore, settings: RagSettings) -> None:
        self._store = store
        self._settings = settings

    @classmethod
    def create(cls, rag: RagSettings, embeddings: EmbeddingSettings) -> RagIntegration:
        return cls(KnowledgeStore.connect(rag, embeddings), rag)

    # ------------------------------------------------------------- internal

    async def _lookup(self, query: str) -> str:
        """Retrieve and format excerpts. Returns "" when nothing clears the bar.

        Wrapped in a timeout because this sits on the critical path of a live
        call: a slow Qdrant response should cost us context, not the turn.
        """
        query = (query or "").strip()
        if not query:
            return ""

        try:
            hits = await asyncio.wait_for(
                self._store.search(query), timeout=self._settings.timeout_s
            )
        except asyncio.TimeoutError:
            logger.warning(
                "knowledge lookup timed out after %.1fs", self._settings.timeout_s
            )
            return ""
        except Exception:
            logger.exception("knowledge lookup failed")
            return ""

        if not hits:
            logger.debug("no knowledge above threshold for %r", query[:80])
            return ""

        logger.debug("retrieved %d chunk(s) for %r", len(hits), query[:80])
        return "\n\n---\n\n".join(doc.page_content.strip() for doc, _ in hits)

    # ---------------------------------------------------------- turn context

    async def on_user_turn(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        if not self._settings.injects_context:
            return

        context = await self._lookup(new_message.text_content or "")
        if not context:
            return

        # Added as an assistant message: the model treats it as its own working
        # notes rather than as something the caller said.
        turn_ctx.add_message(
            role="assistant",
            content=RAG_CONTEXT_TEMPLATE.format(context=context),
        )

    # ----------------------------------------------------------------- tools

    def tools(self) -> list[FunctionTool]:
        if not self._settings.exposes_tool:
            return []

        @function_tool()
        async def search_knowledge_base(context: RunContext, query: str) -> str:
            """Look up information in the internal knowledge base.

            Use this for questions about this organization's specific details,
            such as pricing, policies, hours, procedures, or product facts.

            Args:
                query: What to look up, phrased as a search query.
            """
            result = await self._lookup(query)
            return result or "Nothing in the knowledge base covers that."

        return [search_knowledge_base]

    async def aclose(self) -> None:
        self._store.close()
