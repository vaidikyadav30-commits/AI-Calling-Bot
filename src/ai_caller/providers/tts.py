"""Text-to-speech provider.

Swapping the voice vendor is a change to this file and nothing else. Two are
wired up, chosen by ``TTS_PROVIDER``:

* **ElevenLabs** (default) — see https://docs.livekit.io/agents/models/tts/elevenlabs/
* **Cartesia** — https://docs.livekit.io/agents/models/tts/cartesia/ — which
  reuses the ``CARTESIA_API_KEY`` the STT already needs, so switching costs
  nothing but a line in ``.env.local``.

Having the second one matters operationally: a TTS account that runs out of
credit takes the agent's voice with it, and the symptom is silence on the call
rather than an obvious error. Being able to move vendors in one line is the
difference between a minute of downtime and an evening of it.
"""

from __future__ import annotations

import logging

from livekit.agents import tts as tts_api
from livekit.plugins import cartesia, elevenlabs

from ai_caller.config import TTSSettings

logger = logging.getLogger("ai_caller.providers")


def build_tts(settings: TTSSettings) -> tts_api.TTS:
    if settings.uses_cartesia:
        logger.info(
            "tts: cartesia (model=%s, voice=%s)",
            settings.cartesia_model,
            settings.cartesia_voice,
        )
        return cartesia.TTS(
            model=settings.cartesia_model,
            voice=settings.cartesia_voice,
            api_key=settings.cartesia_api_key,
        )

    logger.info(
        "tts: elevenlabs (model=%s, voice=%s)", settings.model, settings.voice_id
    )
    return elevenlabs.TTS(
        model=settings.model,
        voice_id=settings.voice_id,
        api_key=settings.api_key,
    )
