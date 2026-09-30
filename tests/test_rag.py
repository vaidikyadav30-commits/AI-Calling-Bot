"""RAG tests against an in-memory Qdrant.

These run offline: no Qdrant Cloud credentials and no LLM calls. FastEmbed runs
locally, so retrieval quality here reflects the real thing — only the network
hop to Qdrant Cloud is substituted.
"""

from __future__ import annotations

import pytest
from livekit.agents import ChatContext, ChatMessage
from qdrant_client import QdrantClient

from ai_caller.config import EmbeddingSettings, RagSettings
from ai_caller.integrations.rag import RagIntegration
from ai_caller.integrations.rag.ingest import ingest
from ai_caller.integrations.rag.store import KnowledgeStore

FAQ = """\
# Company handbook

## Refund policy

Refunds are available within fourteen days of purchase. Approved refunds take
five to seven business days to reach the original payment method.

## Support hours

Support runs Monday through Friday, nine in the morning to six in the evening
India Standard Time. Support is closed on weekends.
"""

SHIPPING = """\
## Delivery times

Standard delivery arrives in three to five business days. Express delivery
arrives the next working day when ordered before noon.
"""


@pytest.fixture(scope="module")
def embedding_settings() -> EmbeddingSettings:
    # Read the key rather than defaulting it away: embeddings moved from
    # FastEmbed (local, keyless) to Gemini, so a bare EmbeddingSettings() now
    # fails every test in this module instead of running offline.
    settings = EmbeddingSettings.from_env()
    if not settings.api_key:
        pytest.skip("GEMINI_API_KEY is not set, so embeddings cannot be built.")
    return settings


@pytest.fixture
def knowledge_dir(tmp_path):
    (tmp_path / "handbook.md").write_text(FAQ, encoding="utf-8")
    (tmp_path / "shipping.md").write_text(SHIPPING, encoding="utf-8")
    return tmp_path


@pytest.fixture
def rag_settings(knowledge_dir) -> RagSettings:
    return RagSettings(
        url="http://in-memory",  # only needs to be truthy; the client is patched
        collection="test_knowledge",
        knowledge_dir=knowledge_dir,
        top_k=3,
        # The production default. Pinning it here rather than inheriting means
        # a threshold change has to face these tests, which are the only thing
        # that checks it still separates a real match from small talk.
        score_threshold=RagSettings.score_threshold,
    )


@pytest.fixture
def shared_qdrant(monkeypatch):
    """One in-memory client shared by ingest and retrieval.

    Each QdrantClient(":memory:") owns a separate store, so ingesting and then
    querying through different clients would query an empty collection.
    """
    client = QdrantClient(location=":memory:")
    monkeypatch.setattr(client, "close", lambda *a, **k: None)
    monkeypatch.setattr(
        "ai_caller.integrations.rag.store.build_client",
        lambda settings: client,
    )
    return client


def _ingest(rag, embeddings, **kwargs):
    return ingest(rag, embeddings, **kwargs)


def test_ingest_writes_chunks(shared_qdrant, rag_settings, embedding_settings):
    report = _ingest(rag_settings, embedding_settings)

    assert report.files == 2
    assert report.chunks > 0
    assert report.total_points == report.chunks


def test_reingest_is_idempotent(shared_qdrant, rag_settings, embedding_settings):
    """Re-running must replace each file's chunks, not stack a second copy."""
    first = _ingest(rag_settings, embedding_settings)
    second = _ingest(rag_settings, embedding_settings)

    assert second.total_points == first.total_points


@pytest.mark.asyncio
async def test_edited_file_replaces_old_chunks(
    shared_qdrant, rag_settings, embedding_settings, knowledge_dir
):
    _ingest(rag_settings, embedding_settings)

    (knowledge_dir / "handbook.md").write_text(
        "## Refund policy\n\nRefunds are available within thirty days of purchase.\n",
        encoding="utf-8",
    )
    _ingest(rag_settings, embedding_settings)

    store = KnowledgeStore.connect(rag_settings, embedding_settings)
    hits = await store.search("how long do I have to request a refund?")
    text = " ".join(doc.page_content for doc, _ in hits)

    assert "thirty days" in text
    assert "fourteen days" not in text


def test_deleted_file_is_pruned(
    shared_qdrant, rag_settings, embedding_settings, knowledge_dir
):
    _ingest(rag_settings, embedding_settings)

    (knowledge_dir / "shipping.md").unlink()
    report = _ingest(rag_settings, embedding_settings)

    assert "shipping.md" in report.removed

    store = KnowledgeStore.connect(rag_settings, embedding_settings)
    assert "shipping.md" not in store.known_sources()


@pytest.mark.asyncio
async def test_retrieval_finds_relevant_chunk(
    shared_qdrant, rag_settings, embedding_settings
):
    _ingest(rag_settings, embedding_settings)
    store = KnowledgeStore.connect(rag_settings, embedding_settings)

    hits = await store.search("when is your support team available?")

    assert hits, "expected at least one chunk above the score threshold"
    assert "Monday through Friday" in " ".join(doc.page_content for doc, _ in hits)


@pytest.mark.asyncio
async def test_context_is_injected_into_turn(
    shared_qdrant, rag_settings, embedding_settings
):
    """The agent-facing behavior: a relevant question gains background context."""
    _ingest(rag_settings, embedding_settings)
    integration = RagIntegration.create(rag_settings, embedding_settings)

    turn_ctx = ChatContext()
    message = ChatMessage(
        type="message", role="user", content=["what is your refund policy?"]
    )
    await integration.on_user_turn(turn_ctx, message)

    injected = " ".join(
        item.text_content or "" for item in turn_ctx.items if item.type == "message"
    )
    assert "fourteen days" in injected


@pytest.mark.asyncio
async def test_unrelated_question_injects_nothing(
    shared_qdrant, rag_settings, embedding_settings
):
    """The score threshold must keep small talk free of irrelevant documents."""
    _ingest(rag_settings, embedding_settings)
    integration = RagIntegration.create(rag_settings, embedding_settings)

    turn_ctx = ChatContext()
    message = ChatMessage(
        type="message", role="user", content=["tell me a joke about penguins"]
    )
    await integration.on_user_turn(turn_ctx, message)

    assert len(turn_ctx.items) == 0


@pytest.mark.asyncio
async def test_lookup_failure_does_not_raise(
    shared_qdrant, rag_settings, embedding_settings
):
    """A broken knowledge base degrades the answer; it must never drop the call."""
    _ingest(rag_settings, embedding_settings)
    integration = RagIntegration.create(rag_settings, embedding_settings)

    async def boom(_query: str):
        raise RuntimeError("qdrant is down")

    integration._store.search = boom

    turn_ctx = ChatContext()
    message = ChatMessage(
        type="message", role="user", content=["what is your refund policy?"]
    )
    await integration.on_user_turn(turn_ctx, message)  # must not raise

    assert len(turn_ctx.items) == 0


def test_underscore_files_are_not_ingested(
    shared_qdrant, rag_settings, embedding_settings, knowledge_dir
):
    """Notes and READMEs in the knowledge folder must stay out of the index."""
    (knowledge_dir / "_README.md").write_text(
        "## Internal note\n\nSet RAG_TOP_K to change how many chunks are retrieved.\n",
        encoding="utf-8",
    )
    report = _ingest(rag_settings, embedding_settings)

    assert report.files == 2  # handbook.md and shipping.md only

    store = KnowledgeStore.connect(rag_settings, embedding_settings)
    assert "_README.md" not in store.known_sources()


def test_tool_mode_exposes_search_tool(shared_qdrant, rag_settings, embedding_settings):
    _ingest(rag_settings, embedding_settings)

    auto = RagIntegration.create(rag_settings, embedding_settings)
    assert auto.tools() == []

    tool_mode = RagIntegration.create(
        RagSettings(**{**rag_settings.__dict__, "mode": "tool"}), embedding_settings
    )
    assert len(tool_mode.tools()) == 1
