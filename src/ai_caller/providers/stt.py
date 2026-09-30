"""Speech-to-text provider.

Cartesia's ink-2 (the plugin default for English) is a turn-detecting model: it
emits end-of-turn signals itself, which is what allows the session to use STT
endpointing instead of a separate turn detector.
See https://docs.livekit.io/agents/models/stt/cartesia/
"""

from __future__ import annotations

from livekit.agents import stt as stt_api
from livekit.plugins import cartesia

from ai_caller.config import STTSettings


def build_stt(settings: STTSettings) -> stt_api.STT:
    kwargs: dict[str, object] = {"api_key": settings.api_key}
    if settings.model:
        kwargs["model"] = settings.model
    return cartesia.STT(**kwargs)
