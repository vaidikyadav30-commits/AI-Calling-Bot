"""Upload the knowledge directory to Qdrant Cloud.

    uv run python scripts/ingest_knowledge.py             # sync changes
    uv run python scripts/ingest_knowledge.py --recreate  # rebuild from scratch
    uv run python scripts/ingest_knowledge.py --dry-run   # show what would be sent

Run this whenever the files under `knowledge/` change. Re-running is safe:
edited files are replaced, deleted files are pruned.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a plain script, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_caller.config import Settings, check_env
from ai_caller.integrations.rag.ingest import (
    build_splitter,
    chunk_file,
    discover,
    ingest,
)

REQUIRED = {
    "GEMINI_API_KEY": "Google Gemini API key - https://aistudio.google.com/app/apikey",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="drop and rebuild the collection (required after changing the embedding model)",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="keep chunks whose source file has been deleted",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be uploaded without contacting Qdrant",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    settings = Settings.from_env()

    if args.dry_run:
        files = discover(settings.rag.knowledge_dir)
        splitter = build_splitter(settings.rag)
        total = 0
        print(f"knowledge dir : {settings.rag.knowledge_dir}")
        print(f"collection    : {settings.rag.collection}")
        print(f"embedding     : {settings.embeddings.model}\n")
        for path in files:
            source = path.relative_to(settings.rag.knowledge_dir).as_posix()
            count = len(chunk_file(path, source, splitter))
            total += count
            print(f"  {source:<40} {count:3d} chunk(s)")
        print(f"\n{len(files)} file(s), {total} chunk(s) would be uploaded.")
        return 0

    check_env(REQUIRED)

    print(f"knowledge dir : {settings.rag.knowledge_dir}")
    print(f"collection    : {settings.rag.collection}")
    print(f"embedding     : {settings.embeddings.model}\n")

    report = ingest(
        settings.rag,
        settings.embeddings,
        recreate=args.recreate,
        prune=not args.no_prune,
    )

    print("\nDone.")
    print(report.summary())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
