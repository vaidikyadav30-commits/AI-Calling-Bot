# AGENTS.md

This is a LiveKit Agents project. LiveKit Agents is a Python SDK for building voice AI agents. This project is intended to be used with LiveKit Cloud. See @README.md for more about the rest of the LiveKit ecosystem.

The following is a guide for working with this project.

## Project structure

This Python project uses the `uv` package manager. You should always use `uv` to install dependencies, run the agent, and run tests.

All app-level code is in the `src/` directory. `src/agent.py` must remain the entrypoint (see the associated Dockerfile for how this is deployed); it is deliberately thin and delegates to the `ai_caller` package:

```
src/agent.py                    entrypoint — builds the AgentServer, nothing else
src/ai_caller/
  config.py                     all settings, read from the environment exactly once
  prompts.py                    agent instructions
  session.py                    voice pipeline + LiveKit job entrypoint
  webapp.py                     deployed web service: SPA + browser tokens + control plane
  agents/assistant.py           the Agent; knows nothing about specific integrations
  providers/                    one `build_*` factory per vendor
    llm.py  stt.py  tts.py  embeddings.py
  integrations/                 optional capabilities
    base.py                     the Integration contract
    __init__.py                 registry: build_integrations()
    rag/                        Qdrant + FastEmbed knowledge base
  telephony/                    phone calls, entirely through LiveKit
    models.py                   shared vocabulary (DialInfo, CallRecord, PhoneNumber)
    policy.py                   call guardrails, enforced in code
    numbers.py                  LiveKit Phone Numbers API client + inventory
    sip.py                      LiveKit SIP request building
    dial.py                     agent side: place or answer a call in a job
    calls.py                    control plane: place, list, monitor, hang up
    provision.py                dispatch rule + number routing
    api.py                      HTTP front end (FastAPI) for the control plane
scripts/ingest_knowledge.py     uploads knowledge/ to Qdrant
scripts/setup_telephony.py      rents a number and routes it to the agent
knowledge/                      .md and .txt files; `_`-prefixed names are skipped
frontend/                       React dialer + in-call UI (Vite dev server)
.railway/railway.ts             the two Railway services; see DEPLOY.md
```

Rules that keep this structure honest:

- **Never read `os.environ` outside `config.py`.** Settings are resolved once and passed down, which is what makes the providers and integrations testable.
- **Swapping a vendor means editing one file** under `providers/`. Nothing else names a provider.
- **Adding a capability means one module under `integrations/` plus a block in `integrations/__init__.py`.** Do not add integration-specific code to `assistant.py` or `session.py`.
- **Integrations must fail soft.** A broken integration is logged and skipped; it must never prevent a call from connecting.
- `config.py` must not load `.env.local` as an import side effect. Tests rely on `tests/conftest.py` doing it explicitly.
- **`assistant.py` takes `extra_instructions` and `extra_tools`, and never inspects why.** Anything that depends on *how* a session was reached (a phone call adds call-handling rules and an `end_call` tool; a browser session adds neither) is decided in `session.py` and passed in.
- **`telephony/api.py` is never imported by the agent.** The worker must not load FastAPI; `telephony/__init__.py` deliberately leaves it out. The same applies to `webapp.py`, which sits on top of it.

## Development and deployment are the same code

Locally, the Vite dev server serves the UI, mints room tokens at `/api/connection-details`, and proxies `/api/telephony` to the Python control plane (`frontend/vite.config.js`). There is no dev server in production, so `webapp.py` does those same three jobs in one process — same origin, same token shape, same agent dispatch embedded in the token. The frontend code does not know which one it is talking to, and the LiveKit API secret stays server-side in both.

`config.py` reads `.env.local` when it exists and the platform's environment variables when it does not, so one code path serves both. `$PORT` is the tell that we are on a PaaS: it sets the port *and* selects the `0.0.0.0` bind, because a container that listens on loopback receives nothing.

Two startup guards in `telephony/api.py` keep a deployed dialer from becoming somebody else's phone bill, and both exit non-zero rather than warning — on a PaaS a failed deploy is the correct outcome:

- it refuses to bind beyond loopback without either `TELEPHONY_API_KEY` or `TELEPHONY_ALLOW_PUBLIC`, so public reachability is asked for rather than reached by accident; and
- when it *is* keylessly public **and** an outbound trunk exists, it refuses to start without `TELEPHONY_ALLOWED_NUMBERS`.

The outbound-trunk condition in the second guard is load-bearing: an inbound-only deployment cannot place a call at all, so refusing there would take down its dialer UI for a risk it does not have. `policy.py` still blocks emergency and premium ranges and rate-limits callers, but those bound the bill per minute; the allowlist is what bounds *who* can be reached. See `DEPLOY.md`.

## Telephony

Numbers are rented from **LiveKit Phone Numbers** and routed to the agent with a SIP dispatch rule. There is no carrier account, no inbound trunk, and no extra credentials — telephony is enabled by the same `LIVEKIT_*` values the agent already uses.

Known limits of the service, which the UI surfaces rather than hides: **US numbers only, inbound only**, no `TransferSIPParticipant`, and a released number is billed for the rest of the month. Outbound therefore needs a LiveKit *outbound trunk* pointed at a carrier (`LIVEKIT_SIP_OUTBOUND_TRUNK_ID`); without one, `can_dial_out` is false, the dialer explains why, and inbound is unaffected.

One agent serves browser visitors, inbound calls, and outbound calls. They differ only in how the other party arrives, which `session.py` resolves through `telephony/dial.py`:

| | who speaks first | participant | tools |
|---|---|---|---|
| web | the user | first to join | integrations only |
| inbound | the agent greets | first to join | `+ end_call` |
| outbound | the callee, or the agent after ~3s of silence | the dialled SIP identity | `+ end_call` |

An outbound call is placed by the **agent**, not the control plane: `calls.py` creates the room with the agent dispatched into it and the destination in the job metadata (`DialInfo`), then the agent dials with `wait_until_answered=True`. That ordering is what stops a callee from picking up to silence.

`DialInfo` is a wire format — it travels as dispatch metadata — so renaming a field breaks calls that are already queued.

**Guardrails live in `policy.py`, not in the prompt.** Emergency numbers, premium ranges, the country allowlist, concurrency and rate limits are all checked before a call is placed; a dialer that can be talked into ringing an emergency line is a safety problem, not a prompting problem. `PHONE_INSTRUCTIONS` in `prompts.py` covers what the model must *say* (disclosing that it is an AI, refusing to take card numbers, handling "stop calling"), which is a separate concern.

### How a number becomes reachable

Two things can route a call, and **only the first is required**:

1. a dispatch rule **matches** the number — a rule with no `inbound_numbers` matches every number on the project; and
2. the rule is optionally **pinned** to the number by ID (`UpdatePhoneNumber`).

Pinning is attempted, but its failure is not fatal: LiveKit's `UpdatePhoneNumber` currently rejects the call on some projects — reproducible with their own `lk number update` — while matching alone routes the call correctly. LiveKit signals which state a number is in through its status: `OFFLINE` means *no dispatch rule is associated with it*, `ACTIVE` means one is. That is why `PhoneNumber.routed` trusts the status rather than requiring a pinned ID, and why `inbound_status()` checks rule *scope* rather than only IDs. Changing either to demand an explicit binding would report a working setup as broken.

```console
uv run python scripts/setup_telephony.py --list          # numbers you hold
uv run python scripts/setup_telephony.py --search 415    # what's available
uv run python scripts/setup_telephony.py --rent          # rent one (asks first)
uv run python scripts/setup_telephony.py --write-env     # route them, save the rule ID
uv run python -m ai_caller.telephony.api                 # control plane on :8080
```

The Python server SDK does not wrap `PhoneNumberService` (only Go and the CLI do), so `numbers.py` calls its Twirp endpoints directly with a SIP-admin token. Routing is idempotent: the rule is found by name before being created, so re-running after renting another number is safe.

`tests/test_telephony.py` covers the whole path offline, using canned Twirp payloads copied from a live project — including the case where a matched number reports `ACTIVE` with an empty `sip_dispatch_rule_ids`.

Be sure to maintain code formatting. You can use the ruff formatter/linter as needed: `uv run ruff format` and `uv run ruff check`.

## Knowledge base (RAG)

Retrieval is optional: with no `QDRANT_URL`, the agent runs without it. Embeddings use FastEmbed locally (no API key, 384 dims), so the ingest script and the agent must always use the same `EMBEDDING_MODEL` — changing it requires re-ingesting with `--recreate`.

Context reaches the LLM via `on_user_turn_completed` by default (`RAG_MODE=auto`) rather than a tool call, because that avoids an extra LLM round-trip on every turn. Set `RAG_MODE=tool` to switch to tool-calling. See https://docs.livekit.io/agents/logic/external-data/

```console
uv run python scripts/ingest_knowledge.py --dry-run   # preview chunking
uv run python scripts/ingest_knowledge.py             # sync to Qdrant
```

`tests/test_rag.py` runs the whole retrieval path against an in-memory Qdrant, so it needs no cloud credentials.

## LiveKit Documentation

LiveKit Agents is a fast-evolving project, and the documentation is updated frequently. You should always refer to the latest documentation when working with this project. For your convenience, LiveKit offers both a CLI and an MCP server that can be used to browse and search its documentation. If the developer has not yet installed the CLI, you should recommend that they install it.

### LiveKit CLI

The [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/) `lk docs` subcommand gives full access to LiveKit documentation from the terminal. Requires CLI version 2.15.0+. Check with `lk --version`.

Install or update the CLI:

- **macOS:** `brew install livekit-cli` (update: `brew update && brew upgrade livekit-cli`)
- **Linux:** `curl -sSL https://get.livekit.io/cli | bash`
- **Windows:** `winget install LiveKit.LiveKitCLI` (update: `winget upgrade LiveKit.LiveKitCLI`)

Run `lk docs --help` to see all available commands. Key commands: `lk docs overview`, `lk docs search`, `lk docs get-page`, `lk docs code-search`, `lk docs changelog`, `lk docs submit-feedback`. Run `lk docs <command> --help` before using a command for the first time, as the help text includes advice on usage and interpreting results. Prefer browsing (`overview`, `get-page`) over search, and prefer `search` over `code-search`, as docs pages are more up-to-date and provide better context than raw code.

### LiveKit Docs MCP Server

For deeper IDE integration, LiveKit also offers an [MCP server](https://docs.livekit.io/reference/developer-tools/docs-mcp/) with the same documentation access. It uses Streamable HTTP transport at `https://docs.livekit.io/mcp`. Refer to your IDE's documentation for how to add it.

### Submit docs feedback to LiveKit

If you use the LiveKit CLI or MCP Server to search or browse documentation, also submit constructive feedback to LiveKit with `lk docs submit-feedback` or the MCP `submit_docs_feedback` tool. Add this to your to-do list when planning work, to ensure you file feedback after completing your other tasks. Useful feedback includes incomplete documentation, broken examples, missing steps or any other documentation gap or issue.

## Handoffs and tasks ("workflows")

Voice AI agents are highly sensitive to excessive latency. For this reason, it's important to design complex agents in a structured manner that minimizes the amount of irrelevant context and unnecessary tools included in requests to the LLM. LiveKit Agents supports handoffs (one agent hands control to another) and tasks (tightly-scoped prompts to achieve a specific outcome) to support building reliable workflows. You should make use of these features, instead of writing long instruction prompts that cover multiple phases of a conversation.  Refer to the [documentation](https://docs.livekit.io/agents/build/workflows/) for more information.

## Testing

When possible, add tests for agent behavior. Read the [documentation](https://docs.livekit.io/agents/start/testing/), and refer to existing tests in the `tests/` directory.  Run tests with `uv run pytest`.

Important: When modifying core agent behavior such as instructions, tool descriptions, and tasks/workflows/handoffs, never just guess what will work. Always use test-driven development (TDD) and begin by writing tests for the desired behavior. For instance, if you're planning to add a new tool, write one or more tests for the tool's behavior, then iterate on the tool until the tests pass correctly. This will ensure you are able to produce a working, reliable agent for the user.

## LiveKit CLI

Beyond documentation access, the LiveKit CLI (`lk`) supports other tasks such as managing SIP trunks for telephony-based agents. Run `lk --help` to explore available commands.
