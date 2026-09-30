"""Qdrant Cloud knowledge store.

Owns everything that talks to Qdrant, so the retrieval integration and the
ingest script share one definition of the collection, the embedder, and the
payload layout. Both use the same ``metadata.source`` convention, which is what
makes re-ingesting a changed file replace its chunks instead of duplicating them.
"""

from __future__ import annotations

import logging

from langchain_core.documents import Document
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from ai_caller.config import EmbeddingSettings, RagSettings
from ai_caller.providers.embeddings import build_embeddings

logger = logging.getLogger("ai_caller.rag")

# Payload key holding the originating file, relative to the knowledge directory.
SOURCE_KEY = "source"


def build_client(settings: RagSettings) -> QdrantClient:
    if not settings.url:
        # Fallback to local on-disk Qdrant if no URL is provided
        from ai_caller.config import PROJECT_ROOT

        local_path = str(PROJECT_ROOT / ".qdrant")
        return QdrantClient(path=local_path)
    return QdrantClient(url=settings.url, api_key=settings.api_key or None)


class KnowledgeStore:
    """Thin wrapper over a LangChain Qdrant vector store."""

    def __init__(
        self,
        client: QdrantClient,
        vector_store: QdrantVectorStore,
        settings: RagSettings,
    ) -> None:
        self._client = client
        self._store = vector_store
        self._settings = settings

    # ---------------------------------------------------------------- setup

    @classmethod
    def connect(
        cls,
        rag: RagSettings,
        embedding_settings: EmbeddingSettings,
        *,
        create: bool = False,
        recreate: bool = False,
    ) -> KnowledgeStore:
        """Open the collection.

        ``create`` makes the collection if it is missing (used by ingest);
        the agent connects without it so a typo in the name surfaces as a clear
        error instead of silently querying an empty collection.
        """
        embeddings = build_embeddings(embedding_settings)
        client = build_client(rag)

        if recreate and client.collection_exists(rag.collection):
            logger.info("dropping existing collection %r", rag.collection)
            client.delete_collection(rag.collection)

        if not client.collection_exists(rag.collection):
            if not (create or recreate):
                raise ValueError(
                    f"Qdrant collection {rag.collection!r} does not exist. "
                    "Run: uv run python scripts/ingest_knowledge.py"
                )
            # Probe the embedder for its dimensionality rather than hardcoding,
            # so switching models does not silently create a mismatched collection.
            dim = len(embeddings.embed_query("dimension probe"))
            logger.info("creating collection %r (dim=%d)", rag.collection, dim)
            client.create_collection(
                collection_name=rag.collection,
                vectors_config=models.VectorParams(
                    size=dim, distance=models.Distance.COSINE
                ),
            )
            # Needed for delete-by-source during re-ingestion.
            client.create_payload_index(
                collection_name=rag.collection,
                field_name=f"metadata.{SOURCE_KEY}",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

        store = QdrantVectorStore(
            client=client,
            collection_name=rag.collection,
            embedding=embeddings,
        )
        return cls(client, store, rag)

    # ------------------------------------------------------------ retrieval

    async def search(self, query: str) -> list[tuple[Document, float]]:
        """Top matches above the configured similarity floor."""
        hits = await self._store.asimilarity_search_with_score(
            query, k=self._settings.top_k
        )
        return [
            (doc, score)
            for doc, score in hits
            if score >= self._settings.score_threshold
        ]

    # ------------------------------------------------------------ ingestion

    def replace_source(self, source: str, documents: list[Document]) -> int:
        """Swap all chunks for one source file.

        Delete-then-insert keyed on the source path means editing a document
        updates the collection rather than layering a second copy on top of it.
        """
        self._client.delete(
            collection_name=self._settings.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key=f"metadata.{SOURCE_KEY}",
                            match=models.MatchValue(value=source),
                        )
                    ]
                )
            ),
            wait=True,
        )
        if documents:
            self._store.add_documents(documents)
        return len(documents)

    def known_sources(self) -> set[str]:
        """Every distinct source currently in the collection."""
        sources: set[str] = set()
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=self._settings.collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                metadata = payload.get("metadata") or {}
                if source := metadata.get(SOURCE_KEY):
                    sources.add(source)
            if offset is None:
                break
        return sources

    def delete_source(self, source: str) -> None:
        self.replace_source(source, [])

    def count(self) -> int:
        return self._client.count(self._settings.collection, exact=True).count

    def close(self) -> None:
        self._client.close()
