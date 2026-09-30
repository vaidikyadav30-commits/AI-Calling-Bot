"""Load the knowledge directory into Qdrant.

Chunking is markdown-aware: the splitter prefers heading and paragraph
boundaries, so a chunk usually holds one coherent idea rather than a sentence
sliced mid-thought.

Re-running is idempotent. Each file's chunks are keyed by its relative path and
replaced wholesale, so editing a document updates the collection instead of
stacking a second copy beside the old one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ai_caller.config import EmbeddingSettings, RagSettings
from ai_caller.integrations.rag.store import SOURCE_KEY, KnowledgeStore

logger = logging.getLogger("ai_caller.rag.ingest")

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}


@dataclass
class IngestReport:
    files: int = 0
    chunks: int = 0
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    total_points: int = 0

    def summary(self) -> str:
        lines = [
            f"  files ingested : {self.files}",
            f"  chunks written : {self.chunks}",
            f"  points in db   : {self.total_points}",
        ]
        if self.removed:
            lines.append(f"  sources pruned : {', '.join(self.removed)}")
        if self.skipped:
            lines.append(f"  empty, skipped : {', '.join(self.skipped)}")
        return "\n".join(lines)


def is_ingestable(relative_path: Path) -> bool:
    """Whether a path (relative to the knowledge dir) should become agent knowledge.

    Names beginning with ``_`` or ``.`` are skipped, which gives the knowledge
    directory a place to keep notes and READMEs without the agent quoting its
    own documentation back at a caller.
    """
    if relative_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return False
    return not any(part.startswith(("_", ".")) for part in relative_path.parts)


def discover(knowledge_dir: Path) -> list[Path]:
    """Every ingestable file under the knowledge directory, in stable order."""
    if not knowledge_dir.exists():
        raise FileNotFoundError(
            f"Knowledge directory not found: {knowledge_dir}\n"
            "Create it and add .md or .txt files, or set KNOWLEDGE_DIR."
        )
    return sorted(
        path
        for path in knowledge_dir.rglob("*")
        if path.is_file() and is_ingestable(path.relative_to(knowledge_dir))
    )


def build_splitter(settings: RagSettings) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        # Ordered most- to least-preferred: split on structure before prose.
        separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""],
    )


def chunk_file(
    path: Path, source: str, splitter: RecursiveCharacterTextSplitter
) -> list[Document]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    return [
        Document(page_content=chunk, metadata={SOURCE_KEY: source, "chunk": index})
        for index, chunk in enumerate(splitter.split_text(text))
    ]


def ingest(
    rag: RagSettings,
    embeddings: EmbeddingSettings,
    *,
    recreate: bool = False,
    prune: bool = True,
) -> IngestReport:
    """Sync the knowledge directory into Qdrant.

    ``prune`` removes chunks whose source file no longer exists, so deleting a
    document also removes it from what the agent can say.
    """
    files = discover(rag.knowledge_dir)
    if not files:
        raise FileNotFoundError(
            f"No .md or .txt files found in {rag.knowledge_dir}. Add some and try again."
        )

    store = KnowledgeStore.connect(rag, embeddings, create=True, recreate=recreate)
    splitter = build_splitter(rag)
    report = IngestReport()

    try:
        seen: set[str] = set()
        for path in files:
            source = path.relative_to(rag.knowledge_dir).as_posix()
            seen.add(source)

            documents = chunk_file(path, source, splitter)
            if not documents:
                logger.warning("skipping empty file %s", source)
                report.skipped.append(source)
                continue

            written = store.replace_source(source, documents)
            logger.info("%-40s %3d chunk(s)", source, written)
            report.files += 1
            report.chunks += written

        if prune and not recreate:
            for stale in sorted(store.known_sources() - seen):
                logger.info("removing deleted source %s", stale)
                store.delete_source(stale)
                report.removed.append(stale)

        report.total_points = store.count()
    finally:
        store.close()

    return report
