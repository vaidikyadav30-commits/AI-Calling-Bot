"""Entrypoint for the AI Caller voice agent.

Deliberately thin: the Dockerfile runs this file directly, so it stays a stable
target while the implementation lives in the `ai_caller` package.

    src/ai_caller/
      config.py        settings from .env.local
      prompts.py       agent instructions
      session.py       voice pipeline + LiveKit job entrypoint
      agents/          agent definitions
      providers/       swappable STT / LLM / TTS / embeddings
      integrations/    RAG today, further capabilities later
"""

import logging

from livekit.agents import cli

from ai_caller.config import Settings, check_env
from ai_caller.session import create_server

logger = logging.getLogger("agent")

# Module-level so `livekit.agents download-files` and the CLI can import it.
server = create_server(Settings.from_env())


if __name__ == "__main__":
    check_env()
    cli.run_app(server)
