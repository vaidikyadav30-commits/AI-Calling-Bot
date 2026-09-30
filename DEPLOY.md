# Deploying to Railway

Two services, one repository, one image.

| Service | Start command | Public? |
|---|---|---|
| `agent` | `uv run python src/agent.py start` | no |
| `web` | `uv run python -m ai_caller.webapp` | yes — this is the URL you share |

Both build from the root `Dockerfile`, which compiles the React app in a Node
stage and copies `frontend/dist` into the final image. They differ only in the
start command, so the UI and the agent can never end up on different commits.

The `agent` service needs no domain. LiveKit Cloud dispatches jobs to the
worker over a connection the worker opens, so nothing ever calls *in* to it —
giving it a public URL would only expose a port that answers nothing useful.

## What the web service does

Locally, the Vite dev server serves the UI, mints room tokens at
`/api/connection-details`, and proxies `/api/telephony` to the Python control
plane. There is no dev server in production, so `ai_caller.webapp` does the
same three jobs in one process: same origin, same token shape, same agent
dispatch embedded in the token. The LiveKit API secret stays on the server in
both cases, and the frontend code is identical either way.

## 1. Push the repository

`.env.local` is gitignored and must stay that way — Railway gets the same
names as service variables instead.

```console
git add -A
git commit -m "Prepare for Railway"
git push
```

## 2. Create the services

In a new Railway project, add **two** services from this repository. Railway
detects the `Dockerfile` and uses it for both. Then, per service, set
**Settings → Deploy → Custom Start Command** to the command in the table above.

For the `web` service also set:

- **Settings → Networking → Generate Domain** (this is the dialer URL)
- **Settings → Deploy → Healthcheck Path**: `/api/telephony/health`
- **Healthcheck Timeout**: `300` — the image imports ~13s of LiveKit
  libraries before it serves anything, and a shorter timeout fails the first
  deploy for no reason

Leave `PORT` alone. Railway injects it, and `config.py` reads it as both the
port to bind and the signal to bind `0.0.0.0` instead of loopback.

Alternatively, codify all of the above with `.railway/railway.ts`
(`npm install railway`, then `railway link && railway config apply`).
`railway.json` will not work: Railway deprecated Config as Code and new
services cannot opt into it.

## 3. Set the variables

Set these on **both** services — the agent and the web service read the same
credentials. Nothing below belongs in git.

Required:

```
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
DEEPSEEK_API_KEY
CARTESIA_API_KEY
ELEVENLABS_API_KEY
AGENT_NAME=my-agent
```

`AGENT_NAME` must be identical on both services. The browser asks for an agent
by that name and the worker registers under it; a mismatch is a room the agent
never joins, which presents as silence rather than an error.

Optional, only if you use them:

```
GEMINI_API_KEY                   embeddings, required for the knowledge base
QDRANT_URL, QDRANT_API_KEY       knowledge base; without QDRANT_URL, RAG is off
LIVEKIT_SIP_DISPATCH_RULE_ID     inbound calls
LIVEKIT_SIP_OUTBOUND_TRUNK_ID    outbound calls; without it, inbound still works
TELEPHONY_CALLER_ID
TELEPHONY_ALLOWED_NUMBERS        the safest guardrail while testing
```

Web service only:

```
TELEPHONY_ALLOW_PUBLIC=true
TELEPHONY_ALLOWED_NUMBERS=+971501234567,+919876543210   # your test numbers
```

### How the public dialer is kept safe

The control plane can place phone calls billed to your LiveKit project. On
loopback that is fine; on a public URL it is somebody else's phone bill. Two
guards run at startup, both in `telephony/api.py`, and both **exit non-zero**
rather than warning — a failed Railway deploy is the correct outcome here.

**1. It will not bind publicly by accident.** Listening on anything other than
loopback requires either:

- `TELEPHONY_API_KEY` — every dialer endpoint then requires that key in an
  `X-API-Key` header. **The bundled frontend does not send one**, so the dialer
  UI stops working. Use this when you drive the API yourself.
- `TELEPHONY_ALLOW_PUBLIC=true` — you are stating that public reachability is
  the point, which on a PaaS it is.

**2. A public deployment will not place calls to arbitrary numbers.** If the
endpoint needs no credential *and* an outbound trunk is configured *and* there
is no allowlist, it refuses to start and tells you to set
`TELEPHONY_ALLOWED_NUMBERS`. So set it to the numbers you actually call:

```
TELEPHONY_ALLOWED_NUMBERS=+971501234567,+919876543210
```

A wrong digit then cannot reach a stranger, and neither can anyone who finds
your URL. Add a number to the list to start calling it; there is no way to
widen this from the UI or the prompt.

All three conditions are required, which is why an **inbound-only** deployment
needs no allowlist: with no `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` no call can be
placed at all, so there is nothing to abuse. The guard stays quiet until you
add a trunk, and fires the moment you do.

`policy.py` still blocks emergency and premium ranges unconditionally and rate
limits every caller, but those bound the bill per minute, not in total. The
allowlist is what bounds who can be reached.

`/api/connection-details` is unauthenticated on purpose in every case: it only
mints a token scoped to one freshly created room, so the most it can hand out
is a conversation with the agent — no phone call, no cost per minute.

If you would rather not expose the dialer at all, remove the generated domain
from the `web` service and reach it over Railway's private network. The guards
above are unaffected.

## 4. Telephony setup (optional)

Renting and routing a number is a one-off local step, not a deploy step:

```console
uv run python scripts/setup_telephony.py --rent
uv run python scripts/setup_telephony.py --write-env
```

That writes `LIVEKIT_SIP_DISPATCH_RULE_ID` into `.env.local`. Copy the value
into the Railway variables for both services.

## 5. Knowledge base (optional)

Ingestion runs against Qdrant Cloud from anywhere, so do it locally once and
the deployed agent reads the same collection:

```console
uv run python scripts/ingest_knowledge.py
```

`EMBEDDING_MODEL` must match between the ingest run and the deployed agent —
changing it changes the vector dimensions and requires re-ingesting with
`--recreate`.

## Verifying a deploy

```console
curl https://<your-web-service>.up.railway.app/api/telephony/health
```

`enabled: true` means the LiveKit credentials arrived. `canDialOut: false` just
means no outbound trunk is configured; inbound and browser calls are
unaffected.

Then open the URL and talk to the agent. If the page loads but the agent never
speaks, check the `agent` service logs for `registered worker` — the browser
and the worker must agree on `AGENT_NAME`.

## Running locally

Unchanged. `start.bat` still launches the agent, the control plane, and the
Vite dev server, and still reads `.env.local`:

```console
start.bat
```

To exercise the production path locally instead — one server, no Vite:

```console
cd frontend && npm run build && cd ..
uv run python -m ai_caller.webapp
```
