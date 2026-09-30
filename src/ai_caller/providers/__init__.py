"""Model providers.

Each module exposes a single ``build_*`` factory taking its settings slice and
returning a plugin instance. Swapping a vendor means editing one file; nothing
else in the codebase names a provider.
"""

from ai_caller.providers.embeddings import build_embeddings
from ai_caller.providers.llm import build_llm
from ai_caller.providers.stt import build_stt
from ai_caller.providers.tts import build_tts

__all__ = ["build_embeddings", "build_llm", "build_stt", "build_tts"]
