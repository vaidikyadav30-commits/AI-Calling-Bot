# Knowledge base

Drop `.md` or `.txt` files in this folder. Everything here becomes searchable
context for the voice agent.

Upload after any change:

```console
uv run python scripts/ingest_knowledge.py
```

Re-running is safe. Edited files are replaced, deleted files are pruned, and
nothing is duplicated.

## Writing for a voice agent

The agent reads a few retrieved chunks and answers in one to three spoken
sentences, so structure matters more than volume.

- **One topic per heading.** Chunks are split on `##` and `###` boundaries
  first, so a heading per subject keeps each chunk self-contained.
- **Repeat the subject inside the section.** Write "Refunds take five business
  days," not "It takes five business days." A chunk is retrieved on its own,
  without the heading above it for context.
- **Prefer short prose over deep tables.** The agent has to say this out loud.
- **Write numbers the way they should sound**: "nine one two three" rather than
  a formatted phone number.

## Tuning

Set these in `.env.local` if the defaults don't fit:

| Variable | Default | Effect |
| --- | --- | --- |
| `RAG_TOP_K` | `4` | Chunks retrieved per turn |
| `RAG_SCORE_THRESHOLD` | `0.5` | Similarity floor; raise it if unrelated context leaks in |
| `RAG_CHUNK_SIZE` | `800` | Characters per chunk |
| `RAG_MODE` | `auto` | `auto`, `tool`, `both`, or `off` |

`RAG_CHUNK_SIZE` changes require `--recreate` to take effect on existing files.

## Files that are not knowledge

Files and folders whose name starts with `_` or `.` are skipped by the ingest
script. That is why this file is named `_README.md` — otherwise the agent would
answer questions about its own documentation.
