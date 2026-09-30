"""Embedding provider.

Uses Gemini for embeddings.
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from ai_caller.config import EmbeddingSettings


def build_embeddings(settings: EmbeddingSettings) -> Embeddings:
    """Return the shared embedder."""
    if not settings.api_key:
        raise ValueError("GEMINI_API_KEY is required for embeddings.")

    return GoogleGenerativeAIEmbeddings(
        model=settings.model,
        google_api_key=settings.api_key,
    )
