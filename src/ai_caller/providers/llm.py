"""LLM provider.

DeepSeek speaks the OpenAI Chat Completions protocol, so the OpenAI plugin's
``with_deepseek`` helper is the supported path.
See https://docs.livekit.io/agents/models/llm/deepseek/
"""

from __future__ import annotations

from livekit.agents import llm as llm_api
from livekit.plugins import openai

from ai_caller.config import LLMSettings


def build_llm(settings: LLMSettings) -> llm_api.LLM:
    return openai.LLM.with_deepseek(
        model=settings.model,
        api_key=settings.api_key,
        temperature=settings.temperature,
    )
