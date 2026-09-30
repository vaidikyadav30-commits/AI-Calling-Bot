"""The deployed web service: browser tokens, the SPA, and the bind guard.

These run offline. The only LiveKit interaction is signing a JWT, which is
local — no project is contacted, so the assertions below hold with fake
credentials.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from ai_caller.config import (
    CallPolicySettings,
    LiveKitSettings,
    Settings,
    TelephonySettings,
    WebSettings,
)
from ai_caller.telephony.api import is_bind_allowed, startup_refusal
from ai_caller.webapp import connection_details, create_web_app

LIVEKIT = LiveKitSettings(
    url="wss://example.livekit.cloud",
    api_key="APItestkey",
    api_secret="a-secret-long-enough-to-sign-with",
)


def claims(token: str) -> dict:
    """The JWT payload, without verifying the signature."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


@pytest.fixture
def ui(tmp_path):
    """A stand-in for `npm run build` output."""
    (tmp_path / "index.html").write_text("<!doctype html><div id=root>", "utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", "utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log(1)", "utf-8")
    return tmp_path


def build_settings(*, livekit=LIVEKIT, static_dir=None, **telephony) -> Settings:
    return Settings(
        agent_name="my-agent",
        telephony=TelephonySettings(livekit=livekit, **telephony),
        web=WebSettings(static_dir=static_dir) if static_dir else WebSettings(),
    )


# --------------------------------------------------------------------- tokens


def test_connection_details_dispatches_the_agent():
    """The dispatch must ride in the token, as it does in the dev server.

    Without it the browser joins a room nobody is asked to serve, which
    presents as an agent that never speaks rather than as an error.
    """
    details = connection_details(LIVEKIT, "my-agent", 15)
    video = claims(details["participantToken"])

    assert video["roomConfig"]["agents"] == [{"agentName": "my-agent"}]
    assert video["video"]["room"] == details["roomName"]
    assert video["video"]["roomJoin"] is True


def test_connection_details_are_scoped_to_one_fresh_room():
    """Two visitors must never be handed the same room or identity."""
    first = connection_details(LIVEKIT, "my-agent", 15)
    second = connection_details(LIVEKIT, "my-agent", 15)

    assert first["roomName"] != second["roomName"]
    assert first["participantName"] != second["participantName"]
    assert first["serverUrl"] == LIVEKIT.url

    # Scoped to its own room: a token for one conversation cannot join another.
    assert claims(first["participantToken"])["video"]["room"] == first["roomName"]


def test_connection_details_endpoint_reports_missing_credentials():
    """A readable error beats a blank page when LiveKit is not configured."""
    client = TestClient(create_web_app(build_settings(livekit=LiveKitSettings())))
    response = client.get("/api/connection-details")

    assert response.status_code == 500
    assert "LIVEKIT_URL" in response.json()["error"]


# ------------------------------------------------------------------------- UI


def test_spa_is_served_with_client_side_routing(ui):
    client = TestClient(create_web_app(build_settings(static_dir=ui)))

    assert client.get("/").status_code == 200
    # A real file is served as itself...
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/assets/index-abc123.js").status_code == 200
    # ...and anything else is a client-side route, so it gets the app shell.
    assert "<div id=root>" in client.get("/anything/else").text


def test_api_paths_are_never_answered_with_html(ui):
    """The fallback must not swallow the API.

    A mistyped API path answered with index.html and status 200 surfaces in the
    browser as a JSON parse error, which says nothing about what went wrong.
    """
    client = TestClient(create_web_app(build_settings(static_dir=ui)))
    response = client.get("/api/telephony/nope")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_static_paths_cannot_escape_the_build_directory(ui):
    """`.env.local` sits two levels above the build; it must stay unreachable."""
    (ui.parent / "secret.txt").write_text("LIVEKIT_API_SECRET=hunter2", "utf-8")
    client = TestClient(create_web_app(build_settings(static_dir=ui)))

    response = client.get("/..%2Fsecret.txt")
    assert "hunter2" not in response.text


def test_api_works_without_a_frontend_build(tmp_path):
    """Development has no dist/; the control plane must still serve."""
    settings = build_settings(static_dir=tmp_path / "missing")
    client = TestClient(create_web_app(settings))

    assert client.get("/api/telephony/health").json()["enabled"] is True


# ---------------------------------------------------------------- bind guard


@pytest.mark.parametrize(
    ("host", "api_key", "allow_public", "expected"),
    [
        ("127.0.0.1", "", False, True),  # loopback needs no ceremony
        ("0.0.0.0", "", False, False),  # the accidental exposure this prevents
        ("0.0.0.0", "secret", False, True),  # a shared secret makes it safe
        ("0.0.0.0", "", True, True),  # or an explicit "yes, publish it"
    ],
)
def test_bind_guard(host, api_key, allow_public, expected):
    telephony = TelephonySettings(
        livekit=LIVEKIT, api_host=host, api_key=api_key, allow_public=allow_public
    )
    assert is_bind_allowed(telephony) is expected


# ------------------------------------------------- public outbound allowlist

TRUNK = "ST_outbound"


def public(**kwargs) -> TelephonySettings:
    """A keyless, publicly bound control plane — what a PaaS deploy looks like."""
    return TelephonySettings(
        livekit=LIVEKIT, api_host="0.0.0.0", allow_public=True, **kwargs
    )


def test_public_outbound_needs_an_allowlist():
    """The shape this guard exists for: open URL, working trunk, any number.

    Nothing else in the stack stops this. policy.py blocks emergency and
    premium ranges and rate-limits the caller, so the bill is bounded per
    minute, not in total.
    """
    refusal = startup_refusal(public(outbound_trunk_id=TRUNK))

    assert refusal is not None
    assert "TELEPHONY_ALLOWED_NUMBERS" in refusal


def test_an_allowlist_makes_public_outbound_servable():
    telephony = public(
        outbound_trunk_id=TRUNK,
        policy=CallPolicySettings(allowed_numbers=("+971501234567",)),
    )
    assert startup_refusal(telephony) is None


def test_an_api_key_makes_public_outbound_servable():
    """A credential is the other way to narrow who can dial."""
    telephony = TelephonySettings(
        livekit=LIVEKIT,
        api_host="0.0.0.0",
        api_key="secret",
        outbound_trunk_id=TRUNK,
    )
    assert startup_refusal(telephony) is None


def test_inbound_only_deployment_needs_no_allowlist():
    """No outbound trunk means no call can be placed, so there is nothing to abuse.

    Refusing here would take down the dialer UI of every inbound-only
    deployment for a risk that does not exist in it.
    """
    assert public().can_dial_out is False
    assert startup_refusal(public()) is None


def test_loopback_needs_no_allowlist():
    """Local development must not have to configure any of this."""
    telephony = TelephonySettings(livekit=LIVEKIT, outbound_trunk_id=TRUNK)
    assert startup_refusal(telephony) is None


def test_public_bind_without_permission_is_still_refused():
    """The original guard must keep firing, and name itself in the message."""
    refusal = startup_refusal(TelephonySettings(livekit=LIVEKIT, api_host="0.0.0.0"))

    assert refusal is not None
    assert "TELEPHONY_ALLOW_PUBLIC" in refusal


# ----------------------------------------------------------- platform binding


def test_platform_port_drives_the_default_bind(monkeypatch):
    """Railway assigns $PORT and only routes to 0.0.0.0.

    A service that ignores either never passes its healthcheck, so $PORT has to
    move both values — while explicit TELEPHONY_* settings still win.
    """
    monkeypatch.setenv("PORT", "4567")
    monkeypatch.delenv("TELEPHONY_API_HOST", raising=False)
    monkeypatch.delenv("TELEPHONY_API_PORT", raising=False)

    telephony = TelephonySettings.from_env()
    assert (telephony.api_host, telephony.api_port) == ("0.0.0.0", 4567)

    monkeypatch.setenv("TELEPHONY_API_HOST", "127.0.0.1")
    assert TelephonySettings.from_env().api_host == "127.0.0.1"


def test_local_defaults_are_unchanged_without_a_platform_port(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("TELEPHONY_API_HOST", raising=False)
    monkeypatch.delenv("TELEPHONY_API_PORT", raising=False)

    telephony = TelephonySettings.from_env()
    assert (telephony.api_host, telephony.api_port) == ("127.0.0.1", 8080)
