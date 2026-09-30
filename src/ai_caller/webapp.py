"""The deployed web service: dialer UI, browser tokens, telephony control plane.

In development the Vite dev server does three things (``frontend/vite.config.js``):
it serves the React app, mints connection details at ``/api/connection-details``,
and proxies ``/api/telephony`` to the FastAPI control plane. There is no dev
server in production, so this module does the same three things in one process.

Keeping them on one origin is not a convenience — it is what lets the browser
call ``/api/telephony`` with no CORS configuration and no credential of its own,
which is the same arrangement the dev proxy creates. The LiveKit API secret
never leaves the server in either environment.

Run it with::

    uv run python -m ai_caller.webapp

The agent worker never imports this module; the worker must not load FastAPI.
"""

from __future__ import annotations

import datetime
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from livekit import api

from ai_caller.config import LiveKitSettings, Settings, WebSettings
from ai_caller.telephony.api import PREFIX, create_app, startup_refusal

logger = logging.getLogger("ai_caller.webapp")

CONNECTION_DETAILS = "/api/connection-details"

# Paths the SPA fallback must never swallow. Without this, a typo in an API
# path would return the HTML page with status 200 and the frontend would fail
# on "unexpected token <" instead of a readable 404.
API_PREFIXES = ("/api/", "/docs", "/redoc", "/openapi.json")


def connection_details(livekit: LiveKitSettings, agent_name: str, ttl_minutes: int):
    """Mint a room, an identity, and a token that dispatches the agent.

    The agent dispatch rides along in the token's room configuration, so the
    room is created with the agent already requested. That removes the race
    where the browser joins and asks for an agent before the worker has
    registered, which presents as a room that nobody ever speaks in.
    """
    # A fresh room per conversation: no chance of colliding with an agent left
    # over from a previous session.
    room_name = f"ai-caller-{uuid.uuid4().hex[:8]}"
    identity = f"user-{uuid.uuid4().hex[:8]}"

    token = (
        api.AccessToken(livekit.api_key, livekit.api_secret)
        .with_identity(identity)
        .with_ttl(datetime.timedelta(minutes=ttl_minutes))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=agent_name)])
        )
    )

    return {
        "serverUrl": livekit.url,
        "roomName": room_name,
        "participantName": identity,
        "participantToken": token.to_jwt(),
    }


def create_web_app(settings: Settings | None = None, **kwargs: Any) -> FastAPI:
    """The telephony control plane, plus the browser endpoint and the UI.

    Built on top of ``telephony.create_app`` rather than beside it so the
    control plane keeps exactly one implementation, and so its error handlers
    and API-key dependency apply here unchanged.
    """
    settings = settings or Settings.from_env()
    app = create_app(settings, **kwargs)

    _register_connection_details(app, settings)
    _mount_ui(app, settings.web)
    return app


def _register_connection_details(app: FastAPI, settings: Settings) -> None:
    """``GET /api/connection-details`` — the browser's way into a room.

    Deliberately unauthenticated, like the dev server's version: it mints a
    token scoped to one freshly created room, so the worst it can hand out is
    a conversation with the agent. Placing phone calls stays behind the
    control plane's API-key check.
    """
    livekit = settings.telephony.livekit

    @app.get(CONNECTION_DETAILS)
    async def details() -> Any:
        if not livekit.configured:
            missing = ", ".join(
                name
                for name, value in (
                    ("LIVEKIT_URL", livekit.url),
                    ("LIVEKIT_API_KEY", livekit.api_key),
                    ("LIVEKIT_API_SECRET", livekit.api_secret),
                )
                if not value
            )
            logger.error("%s unavailable: missing %s", CONNECTION_DETAILS, missing)
            return JSONResponse(
                status_code=500,
                content={"error": f"Missing {missing}. Set it and restart the server."},
            )

        try:
            return connection_details(
                livekit, settings.agent_name, settings.web.token_ttl_minutes
            )
        except Exception as exc:  # a token failure must not read as a blank page
            logger.exception("%s failed", CONNECTION_DETAILS)
            return JSONResponse(
                status_code=500,
                content={"error": str(exc) or "Failed to mint a token."},
            )


def _mount_ui(app: FastAPI, web: WebSettings) -> None:
    """Serve the built React app, with unknown paths falling back to index.html.

    Absent in development — Vite serves the app from source there — so a
    missing build is logged and skipped rather than treated as an error. The
    API is fully usable without a UI in front of it.
    """
    if not web.has_ui:
        logger.info(
            "no frontend build at %s: serving the API only "
            "(run `npm run build` in frontend/ to attach the UI)",
            web.static_dir,
        )
        return

    index = web.static_dir / "index.html"
    # Vite emits hashed filenames under assets/, so StaticFiles can serve them
    # directly. The directory only exists once something has been bundled.
    assets = web.static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Any:
        if any(f"/{path}".startswith(prefix) for prefix in API_PREFIXES):
            return JSONResponse(status_code=404, content={"error": "Not found."})

        # Real files (favicon.svg, icons.svg, ...) are served as themselves;
        # everything else is a client-side route and gets the app shell.
        candidate = _safe_join(web.static_dir, path)
        if candidate is not None and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    logger.info("serving frontend from %s", web.static_dir)


def _safe_join(root: Path, path: str) -> Path | None:
    """Resolve ``path`` under ``root``, or None if it escapes.

    A request for ``../../.env.local`` must not be answered with the file it
    names, so containment is checked after resolution rather than by
    inspecting the string.
    """
    try:
        candidate = (root / path).resolve()
    except (OSError, ValueError):
        return None
    root = root.resolve()
    return candidate if candidate == root or root in candidate.parents else None


def main() -> int:
    """Run the web service with uvicorn."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    settings = Settings.from_env()
    telephony = settings.telephony

    # Same rules as the bare control plane: this service exposes every dialer
    # endpoint, so attaching a UI to it must not relax anything.
    refusal = startup_refusal(telephony)
    if refusal:
        print(f"\nERROR: {refusal}\n", file=sys.stderr)
        return 1

    where = f"http://{telephony.api_host}:{telephony.api_port}"
    print(f"\n  Dialer UI:               {where}/")
    print(f"  Telephony control plane: {where}{PREFIX}")
    print(f"  Interactive docs:        {where}/docs\n")

    uvicorn.run(
        create_web_app(settings),
        host=telephony.api_host,
        port=telephony.api_port,
        log_level="info",
        # Railway and most PaaS terminate TLS at their edge and forward the
        # original scheme and client IP in X-Forwarded-*. Without this uvicorn
        # reports every request as plain HTTP from the proxy's address.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
