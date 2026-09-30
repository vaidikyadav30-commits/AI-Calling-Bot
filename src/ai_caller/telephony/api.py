"""HTTP control plane for the dialer.

A thin translation of requests into ``TelephonyService`` calls: every decision,
guardrail, and side effect lives in the service, so this file has no business
logic to get out of step with the agent. Domain exceptions are mapped to status
codes once, in ``_register_error_handlers``, rather than per endpoint.

Run it with::

    uv run python -m ai_caller.telephony.api

The bind address defaults to loopback. Exposing it beyond localhost requires
``TELEPHONY_API_KEY`` — an open endpoint that places phone calls is somebody
else's phone bill.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai_caller.config import Settings, TelephonySettings
from ai_caller.telephony.calls import TelephonyDisabledError, TelephonyService
from ai_caller.telephony.numbers import PhoneNumberError
from ai_caller.telephony.policy import PolicyError
from ai_caller.telephony.sip import DialError

logger = logging.getLogger("ai_caller.telephony.api")

PREFIX = "/api/telephony"


class CallRequest(BaseModel):
    """Body of ``POST /api/telephony/calls``."""

    to: str = Field(..., description="Destination number in E.164, e.g. +971501234567")
    from_number: str | None = Field(
        default=None,
        alias="from",
        description="Caller ID. Defaults to TELEPHONY_CALLER_ID.",
    )
    context: dict[str, str] | None = Field(
        default=None,
        description="Extra context passed to the agent, e.g. {'name': 'Aisha'}.",
    )

    model_config = {"populate_by_name": True}


def create_app(
    settings: Settings | None = None, *, service: TelephonyService | None = None
) -> FastAPI:
    """Build the control-plane app.

    The service is created once and shared: it owns the LiveKit connection
    pools plus the rate-limit window, both of which have to be process-wide
    to mean anything. Passing one in is how the tests exercise the HTTP
    layer without a LiveKit project.
    """
    settings = settings or Settings.from_env()
    service = service or TelephonyService(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _log_readiness(settings)
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(
        title="AI Caller telephony",
        summary="List phone numbers and place AI-handled phone calls.",
        lifespan=lifespan,
    )
    _register_error_handlers(app)

    if settings.telephony.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.telephony.cors_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-API-Key"],
        )

    def require_api_key(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        """Shared-secret check, skipped entirely when no key is configured."""
        expected = settings.telephony.api_key
        if not expected:
            return
        if x_api_key != expected:
            raise HTTPException(status_code=401, detail={"error": "Invalid API key."})

    guarded = [Depends(require_api_key)]

    # ------------------------------------------------------------------ meta

    @app.get(f"{PREFIX}/health")
    async def health() -> dict[str, Any]:
        """Configuration state. Unauthenticated so the UI can explain itself."""
        telephony = settings.telephony
        return {
            "enabled": telephony.enabled,
            "canDialOut": telephony.can_dial_out,
            "agentName": settings.agent_name,
            "roomPrefix": telephony.room_prefix,
            "serverUrl": telephony.livekit.url,
            "authRequired": bool(telephony.api_key),
        }

    # --------------------------------------------------------------- numbers

    @app.get(f"{PREFIX}/numbers", dependencies=guarded)
    async def numbers(
        refresh: Annotated[bool, Query(description="Bypass the cache")] = False,
    ) -> dict[str, Any]:
        """Phone numbers this LiveKit project holds, and how they route."""
        inventory = await service.inventory(refresh=refresh)
        return inventory.to_dict()

    @app.get(f"{PREFIX}/inbound", dependencies=guarded)
    async def inbound() -> dict[str, Any]:
        """Whether inbound calls actually reach the agent, and through what."""
        return await service.inbound_status()

    # ----------------------------------------------------------------- calls

    @app.post(f"{PREFIX}/calls", status_code=201, dependencies=guarded)
    async def place_call(request: CallRequest) -> dict[str, Any]:
        """Start an outbound call and return a token for listening in."""
        placed = await service.place_call(
            request.to,
            from_number=request.from_number or "",
            context=request.context or {},
        )
        return placed.to_dict()

    @app.get(f"{PREFIX}/calls", dependencies=guarded)
    async def list_calls() -> dict[str, Any]:
        """Every call currently up, inbound and outbound."""
        calls = await service.list_calls()
        return {"calls": [call.to_dict() for call in calls]}

    @app.post(f"{PREFIX}/calls/{{room}}/monitor", dependencies=guarded)
    async def monitor(room: str) -> dict[str, Any]:
        """A fresh subscribe-only token for an already running call."""
        _assert_call_room(service, room)
        return {
            "room": room,
            "serverUrl": settings.telephony.livekit.url,
            "viewerToken": service.viewer_token(room),
        }

    @app.delete(f"{PREFIX}/calls/{{room}}", dependencies=guarded)
    async def hangup(room: str) -> dict[str, Any]:
        """End a call for everyone on it."""
        await service.hangup(room)
        return {"room": room, "status": "ended"}

    app.state.service = service
    return app


def _assert_call_room(service: TelephonyService, room: str) -> None:
    """Refuse to touch rooms that are not calls, e.g. browser test sessions."""
    if not room.startswith(f"{service.settings.room_prefix}-"):
        raise PolicyError("not_a_call", f"{room!r} is not a call room.")


def _register_error_handlers(app: FastAPI) -> None:
    """Map domain exceptions onto HTTP responses.

    Every handler returns the same shape — ``{"error", "code"}`` — so the
    frontend has exactly one error path to render.
    """

    def problem(status: int, message: str, code: str, **extra: Any) -> JSONResponse:
        return JSONResponse(
            status_code=status, content={"error": message, "code": code, **extra}
        )

    @app.exception_handler(PolicyError)
    async def _policy(_request: Request, exc: PolicyError) -> JSONResponse:
        # A refused call is the caller's mistake, not a server fault.
        logger.info("refused: %s (%s)", exc.message, exc.code)
        return problem(400, exc.message, exc.code)

    @app.exception_handler(DialError)
    async def _dial(_request: Request, exc: DialError) -> JSONResponse:
        misconfigured = exc.code in ("not_configured", "caller_id_missing")
        return problem(
            400 if misconfigured else 502,
            exc.message,
            exc.code,
            sipStatus=exc.sip_status,
        )

    @app.exception_handler(TelephonyDisabledError)
    async def _disabled(_request: Request, exc: TelephonyDisabledError) -> JSONResponse:
        return problem(503, str(exc), "telephony_disabled")

    @app.exception_handler(PhoneNumberError)
    async def _numbers(_request: Request, exc: PhoneNumberError) -> JSONResponse:
        # "Not available on this plan" is not a server fault, so it gets its
        # own code rather than being reported as a bad gateway.
        if exc.is_unsupported:
            return problem(501, str(exc), "numbers_unsupported")
        return problem(
            401 if exc.is_auth_error else 502, str(exc), "phone_number_error"
        )


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_bind_allowed(telephony: TelephonySettings) -> bool:
    """Whether this bind address is safe enough to serve on.

    Loopback is always fine. Anything wider hands the ability to place calls to
    whoever can reach the port, so it needs either a shared secret or an
    explicit statement that public reachability is the point — which is exactly
    the case on a PaaS, where the container is only ever addressed as 0.0.0.0.
    """
    if telephony.api_host in LOOPBACK_HOSTS:
        return True
    return bool(telephony.api_key or telephony.allow_public)


def is_reachable_by_anyone(telephony: TelephonySettings) -> bool:
    """True when no credential stands between the internet and the dialer."""
    return telephony.api_host not in LOOPBACK_HOSTS and not telephony.api_key


def unguarded_public_outbound(telephony: TelephonySettings) -> bool:
    """True when anyone who finds this URL could call any number they like.

    Three things have to line up for that to be possible, and all three are
    checked rather than assumed:

    - the endpoint is reachable without a key (a public PaaS deployment),
    - outbound is actually wired up, so a call can be placed at all, and
    - no destination allowlist narrows who can be reached.

    The middle condition is why this is not folded into ``is_bind_allowed``.
    An inbound-only deployment — no outbound trunk — has nothing to abuse, and
    refusing to serve the dialer UI in that case would be wrong.
    """
    if not is_reachable_by_anyone(telephony):
        return False
    if not telephony.can_dial_out:
        return False
    return not telephony.policy.allowed_numbers


def startup_refusal(telephony: TelephonySettings) -> str | None:
    """The reason this configuration must not serve, or None if it may.

    Returned rather than printed so the control plane and the web service share
    one set of rules and one wording; each entry point only decides where the
    text goes.
    """
    if not is_bind_allowed(telephony):
        return (
            f"refusing to listen on {telephony.api_host} without "
            "TELEPHONY_API_KEY set.\n"
            "       Anyone who could reach this port would be able to place "
            "calls billed to your LiveKit project.\n"
            "       Set TELEPHONY_API_KEY, or TELEPHONY_ALLOW_PUBLIC=true if "
            "the endpoint is meant to be reachable."
        )

    if unguarded_public_outbound(telephony):
        return (
            f"refusing to serve outbound calling on {telephony.api_host} with "
            "no destination allowlist.\n"
            "       This endpoint needs no credential and an outbound trunk is "
            "configured, so anyone who finds the URL could call any number at "
            "your expense.\n"
            "       Set TELEPHONY_ALLOWED_NUMBERS to the numbers you actually "
            "call (comma-separated, E.164):\n"
            "         TELEPHONY_ALLOWED_NUMBERS=+971501234567,+919876543210\n"
            "       Or set TELEPHONY_API_KEY to require a credential instead."
        )

    return None


def _log_readiness(settings: Settings) -> None:
    telephony = settings.telephony
    if is_reachable_by_anyone(telephony):
        # startup_refusal has already rejected the dangerous shape of this, so
        # what is left is either harmless or deliberately narrowed. Say which,
        # because "public" alone does not tell you whether it can cost money.
        allowed = telephony.policy.allowed_numbers
        if not telephony.can_dial_out:
            scope = "inbound and browser calls only, so no number can be dialled"
        else:
            scope = f"outbound limited to {len(allowed)} allowlisted number(s)"
        logger.warning(
            "serving on %s with no TELEPHONY_API_KEY: %s", telephony.api_host, scope
        )
    if not telephony.enabled:
        logger.warning("telephony disabled: LiveKit credentials are missing")
    elif not telephony.can_dial_out:
        logger.warning("inbound only: no LIVEKIT_SIP_OUTBOUND_TRUNK_ID configured")
    else:
        logger.info("telephony ready (agent=%s)", settings.agent_name)


def main() -> int:
    """Run the control plane with uvicorn."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    settings = Settings.from_env()
    telephony = settings.telephony

    host = telephony.api_host
    refusal = startup_refusal(telephony)
    if refusal:
        print(f"\nERROR: {refusal}\n", file=sys.stderr)
        return 1

    print(f"\n  Telephony control plane: http://{host}:{telephony.api_port}{PREFIX}")
    print(f"  Interactive docs:        http://{host}:{telephony.api_port}/docs\n")

    uvicorn.run(
        create_app(settings), host=host, port=telephony.api_port, log_level="info"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
