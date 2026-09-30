"""Integration registry.

``build_integrations`` decides which capabilities are active for a session based
on configuration. To add a new one — a CRM, a calendar, an order lookup —
implement ``BaseIntegration`` in a submodule and add a block here. The agent,
session, and provider layers stay untouched.

Every integration is optional by construction: if one cannot start, it is logged
and skipped so the voice pipeline still runs.
"""

from __future__ import annotations

import logging

from ai_caller.config import Settings
from ai_caller.integrations.base import (
    BaseIntegration,
    Integration,
    close_all,
    run_turn_hooks,
)

logger = logging.getLogger("ai_caller.integrations")

__all__ = [
    "BaseIntegration",
    "Integration",
    "build_integrations",
    "close_all",
    "run_turn_hooks",
]


def build_integrations(settings: Settings) -> list[Integration]:
    """Instantiate every enabled integration."""
    integrations: list[Integration] = []

    # --- Knowledge base (Qdrant + FastEmbed) ------------------------------
    if settings.rag.enabled:
        try:
            # Imported here so the agent does not load the ONNX runtime, Qdrant
            # client, or model weights when RAG is switched off.
            from ai_caller.integrations.rag import RagIntegration

            integrations.append(
                RagIntegration.create(settings.rag, settings.embeddings)
            )
            logger.info(
                "rag enabled (collection=%r, mode=%s)",
                settings.rag.collection,
                settings.rag.mode,
            )
        except Exception as exc:
            # A misconfigured knowledge base must not take the phone line down.
            logger.warning("rag disabled: %s", exc)
    else:
        logger.info("rag disabled (QDRANT_URL unset or RAG_MODE=off)")

    # --- Add further integrations here ------------------------------------
    # if settings.calendar.enabled:
    #     integrations.append(CalendarIntegration.create(settings.calendar))

    return integrations
