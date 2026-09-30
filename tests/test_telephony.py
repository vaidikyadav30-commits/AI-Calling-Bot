"""Telephony tests: guardrails, dial requests, the control plane, and the API.

These run fully offline. The LiveKit Phone Numbers API is served from canned
Twirp payloads through the real client (so enum parsing and error mapping are
exercised too), and LiveKit's room and SIP services are stubs recording what
the service asked of them. No credentials, no carrier, no calls placed.

The payloads here are copies of real responses from a live project, including
the detail that a number matched by a catch-all dispatch rule reports
``PHONE_NUMBER_STATUS_ACTIVE`` with an *empty* ``sip_dispatch_rule_ids``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from livekit import api

from ai_caller.config import (
    CallPolicySettings,
    LiveKitSettings,
    Settings,
    TelephonySettings,
)
from ai_caller.telephony import dial as dial_module
from ai_caller.telephony.calls import TelephonyDisabledError, TelephonyService
from ai_caller.telephony.models import (
    CallStatus,
    DialInfo,
    Direction,
    NumberStatus,
    NumberType,
)
from ai_caller.telephony.numbers import (
    PhoneNumberClient,
    PhoneNumberError,
    api_base,
    fetch_inventory,
    parse_number,
    safe_fetch_inventory,
)
from ai_caller.telephony.policy import CallPolicy, PolicyError, normalize_number
from ai_caller.telephony.provision import provision
from ai_caller.telephony.sip import (
    DialError,
    OutboundTarget,
    build_participant_request,
    describe_sip_failure,
)

# ---------------------------------------------------------------------------
# Canned payloads, copied from a live project
# ---------------------------------------------------------------------------

NUMBER_ACTIVE = {
    "id": "PN_one",
    "e164_format": "+12402124128",
    "country_code": "US",
    "area_code": "240",
    "number_type": "PHONE_NUMBER_TYPE_LOCAL",
    "locality": "OAKLAND",
    "region": "MD",
    "capabilities": ["voice"],
    "status": "PHONE_NUMBER_STATUS_ACTIVE",
    "sip_dispatch_rule_id": "",
    "sip_dispatch_rule_ids": [],
}

NUMBER_OFFLINE = {
    "id": "PN_two",
    "e164_format": "+12402124129",
    "country_code": "US",
    "area_code": "240",
    "number_type": "PHONE_NUMBER_TYPE_TOLL_FREE",
    "capabilities": ["voice"],
    "status": "PHONE_NUMBER_STATUS_OFFLINE",
    "sip_dispatch_rule_ids": [],
}

NUMBER_RELEASED = {
    "id": "PN_three",
    "e164_format": "+12402124130",
    "status": "PHONE_NUMBER_STATUS_RELEASED",
    "sip_dispatch_rule_ids": [],
}

LIST_RESPONSE = {"items": [NUMBER_ACTIVE], "total_count": 1, "offline_count": 0}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeNumbers(PhoneNumberClient):
    """A ``PhoneNumberClient`` whose Twirp transport is a dict of responses.

    Subclassing rather than duck-typing keeps token minting, payload parsing,
    and error mapping under test; only the HTTP call itself is replaced.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        super().__init__(
            LiveKitSettings(
                url="wss://example.livekit.cloud",
                api_key="devkey",
                api_secret="secret-value-long-enough-for-hmac",
            )
        )
        self.responses = responses or {"ListPhoneNumbers": LIST_RESPONSE}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, **body: Any) -> dict[str, Any]:
        self.calls.append((method, body))
        result = self.responses.get(method, {})
        if isinstance(result, BaseException):
            raise result
        return result

    async def aclose(self) -> None:
        return None


@dataclass
class FakeRoom:
    name: str
    metadata: str = ""
    num_participants: int = 0
    creation_time: int = 1_700_000_000


@dataclass
class FakeParticipant:
    identity: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeRuleInfo:
    sip_dispatch_rule_id: str
    name: str = ""
    inbound_numbers: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)

    @property
    def room_config(self):
        agents = [type("Agent", (), {"agent_name": name})() for name in self.agents]
        return type("Config", (), {"agents": agents})()

    @property
    def rule(self):
        callee = type("Callee", (), {"room_prefix": "call-"})()
        individual = type("Individual", (), {"room_prefix": ""})()
        return type(
            "Rule",
            (),
            {"dispatch_rule_callee": callee, "dispatch_rule_individual": individual},
        )()


class FakeRoomService:
    def __init__(self, rooms: list[FakeRoom] | None = None) -> None:
        self.rooms = rooms or []
        self.participants: dict[str, list[FakeParticipant]] = {}
        self.created: list[Any] = []
        self.deleted: list[str] = []

    async def create_room(self, request):
        self.created.append(request)
        self.rooms.append(FakeRoom(name=request.name, metadata=request.metadata))
        return request

    async def list_rooms(self, _request):
        return type("Response", (), {"rooms": self.rooms})()

    async def list_participants(self, request):
        return type(
            "Response", (), {"participants": self.participants.get(request.room, [])}
        )()

    async def delete_room(self, request):
        self.deleted.append(request.room)
        return request


class FakeSipService:
    def __init__(self, rules: list[FakeRuleInfo] | None = None) -> None:
        self.rules = rules or []
        self.participants: list[Any] = []
        self.created_rules: list[Any] = []
        self.deleted_rules: list[str] = []
        self.error: BaseException | None = None

    async def create_sip_participant(self, request):
        self.participants.append(request)
        if self.error is not None:
            raise self.error
        return request

    async def list_dispatch_rule(self, _request):
        return type("Response", (), {"items": self.rules})()

    async def create_dispatch_rule(self, request):
        self.created_rules.append(request)
        rule = FakeRuleInfo(
            sip_dispatch_rule_id="SDR_new",
            name=request.name,
            inbound_numbers=list(request.inbound_numbers),
            agents=[agent.agent_name for agent in request.room_config.agents],
        )
        self.rules.append(rule)
        return rule

    async def delete_dispatch_rule(self, request):
        self.deleted_rules.append(request.sip_dispatch_rule_id)
        self.rules = [
            rule
            for rule in self.rules
            if rule.sip_dispatch_rule_id != request.sip_dispatch_rule_id
        ]
        return request


class FakeLiveKit:
    def __init__(
        self,
        rooms: list[FakeRoom] | None = None,
        rules: list[FakeRuleInfo] | None = None,
    ) -> None:
        self.room = FakeRoomService(rooms)
        self.sip = FakeSipService(rules)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_settings(**telephony_kwargs: Any) -> Settings:
    defaults: dict[str, Any] = {
        "livekit": LiveKitSettings(
            url="wss://example.livekit.cloud",
            api_key="devkey",
            api_secret="secret-value-long-enough-for-hmac",
        ),
        "policy": CallPolicySettings(),
        "caller_id": "+12402124128",
    }
    defaults.update(telephony_kwargs)
    return Settings(agent_name="my-agent", telephony=TelephonySettings(**defaults))


@pytest.fixture
def numbers() -> FakeNumbers:
    return FakeNumbers()


@pytest.fixture
def service(numbers: FakeNumbers) -> TelephonyService:
    return TelephonyService(
        make_settings(outbound_trunk_id="ST_trunk"),
        lkapi=FakeLiveKit(),
        numbers=numbers,
    )


# ---------------------------------------------------------------------------
# Number normalization and guardrails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+12402124128", "+12402124128"),
        ("+1 240 212 4128", "+12402124128"),
        ("+971-50-123-4567", "+971501234567"),
        ("00971501234567", "+971501234567"),
        ("  +919876543210  ", "+919876543210"),
    ],
)
def test_normalize_accepts_real_world_formats(raw: str, expected: str) -> None:
    assert normalize_number(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "number_missing"),
        ("   ", "number_missing"),
        ("999", "emergency_number"),
        ("112", "emergency_number"),
        ("911", "emergency_number"),
        ("+911", "emergency_number"),
        ("0501234567", "not_international"),
        ("(555) 010-1234", "not_international"),
        ("+97150", "invalid_number"),
        ("+971abc1234567", "invalid_number"),
    ],
)
def test_normalize_rejects_bad_input(raw: str, code: str) -> None:
    """Emergency numbers and national formats must never be guessed at."""
    with pytest.raises(PolicyError) as excinfo:
        normalize_number(raw)
    assert excinfo.value.code == code


def test_emergency_numbers_are_refused_even_with_an_allowlist() -> None:
    """An allowlist is a narrowing tool; it can never re-enable 999."""
    policy = CallPolicy(CallPolicySettings(allowed_numbers=("999",)))
    with pytest.raises(PolicyError) as excinfo:
        policy.check_destination("999")
    assert excinfo.value.code == "emergency_number"


def test_premium_numbers_are_refused() -> None:
    policy = CallPolicy(CallPolicySettings())
    with pytest.raises(PolicyError) as excinfo:
        policy.check_destination("+19005551234")
    assert excinfo.value.code == "number_blocked"


def test_blocklist_and_allowlist() -> None:
    blocked = CallPolicy(CallPolicySettings(blocked_numbers=("+971501234567",)))
    with pytest.raises(PolicyError) as excinfo:
        blocked.check_destination("+971 50 123 4567")
    assert excinfo.value.code == "number_blocked"

    allowed = CallPolicy(CallPolicySettings(allowed_numbers=("+971501234567",)))
    assert allowed.check_destination("+971501234567") == "+971501234567"
    with pytest.raises(PolicyError) as excinfo:
        allowed.check_destination("+971509999999")
    assert excinfo.value.code == "number_not_allowed"


def test_country_allowlist() -> None:
    policy = CallPolicy(CallPolicySettings(allowed_countries=("971",)))
    assert policy.check_destination("+971501234567") == "+971501234567"

    with pytest.raises(PolicyError) as excinfo:
        policy.check_destination("+919876543210")
    assert excinfo.value.code == "country_not_allowed"


def test_caller_id_must_be_one_of_ours() -> None:
    policy = CallPolicy(CallPolicySettings())
    owned = ["+12402124128"]

    assert policy.check_caller_id("+12402124128", owned_numbers=owned) == (
        "+12402124128"
    )

    with pytest.raises(PolicyError) as excinfo:
        policy.check_caller_id("+12409999999", owned_numbers=owned)
    assert excinfo.value.code == "caller_id_not_owned"

    # With no inventory available the check is skipped rather than blocking.
    assert policy.check_caller_id("+12409999999", owned_numbers=[]) == "+12409999999"


def test_concurrency_limit() -> None:
    policy = CallPolicy(CallPolicySettings(max_concurrent_calls=2))
    policy.check_capacity(1)
    with pytest.raises(PolicyError) as excinfo:
        policy.check_capacity(2)
    assert excinfo.value.code == "too_many_calls"


def test_rate_limit_window_expires() -> None:
    """The limit is per rolling minute, not per process lifetime."""
    now = [1000.0]
    policy = CallPolicy(CallPolicySettings(calls_per_minute=2), clock=lambda: now[0])

    policy.check_capacity(0)
    policy.record_call()
    policy.check_capacity(0)
    policy.record_call()

    with pytest.raises(PolicyError) as excinfo:
        policy.check_capacity(0)
    assert excinfo.value.code == "rate_limited"

    now[0] += 61.0
    policy.check_capacity(0)  # window has rolled over


# ---------------------------------------------------------------------------
# DialInfo: the job-metadata wire format
# ---------------------------------------------------------------------------


def test_dial_info_round_trip() -> None:
    dial = DialInfo(
        direction=Direction.OUTBOUND,
        to_number="+971501234567",
        from_number="+12402124128",
        call_id="abc123",
        context={"name": "Aisha"},
    )
    parsed = DialInfo.from_json(dial.to_json())

    assert parsed == dial
    assert parsed.is_outbound
    assert parsed.is_phone_call


def test_dial_info_accepts_the_livekit_example_key() -> None:
    """LiveKit's telephony examples use `phone_number`; honour it."""
    parsed = DialInfo.from_json('{"phone_number": "+971501234567"}')

    assert parsed.direction is Direction.OUTBOUND
    assert parsed.to_number == "+971501234567"


@pytest.mark.parametrize("raw", ["", None, "   ", "not json", "[]", "{}"])
def test_dial_info_falls_back_to_web(raw: str | None) -> None:
    """Bad metadata must degrade to a browser session, never raise."""
    parsed = DialInfo.from_json(raw)

    assert parsed.direction is Direction.WEB
    assert not parsed.is_phone_call


def test_dial_info_inbound() -> None:
    parsed = DialInfo.from_json('{"direction": "inbound"}')

    assert parsed.direction is Direction.INBOUND
    assert parsed.is_phone_call
    assert not parsed.is_outbound


# ---------------------------------------------------------------------------
# LiveKit Phone Numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("wss://x.livekit.cloud", "https://x.livekit.cloud"),
        ("ws://localhost:7880", "http://localhost:7880"),
        ("https://x.livekit.cloud/", "https://x.livekit.cloud"),
        ("x.livekit.cloud", "https://x.livekit.cloud"),
    ],
)
def test_api_base_derives_the_https_origin(url: str, expected: str) -> None:
    assert api_base(url) == expected


def test_parse_number_reads_protobuf_enum_names() -> None:
    """Enums arrive as full protobuf names over Twirp JSON."""
    number = parse_number(NUMBER_ACTIVE)

    assert number.number == "+12402124128"
    assert number.id == "PN_one"
    assert number.status is NumberStatus.ACTIVE
    assert number.number_type is NumberType.LOCAL
    assert number.label == "OAKLAND, MD"
    assert number.capabilities == ("voice",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PHONE_NUMBER_STATUS_OFFLINE", NumberStatus.OFFLINE),
        ("offline", NumberStatus.OFFLINE),
        ("OFFLINE", NumberStatus.OFFLINE),
        (4, NumberStatus.OFFLINE),
        ("nonsense", NumberStatus.UNSPECIFIED),
        (None, NumberStatus.UNSPECIFIED),
    ],
)
def test_parse_number_tolerates_every_enum_rendering(raw: Any, expected) -> None:
    """Serializers differ on enums; none of them should break the dialer."""
    assert parse_number({"status": raw}).status is expected


def test_active_number_counts_as_routed_without_an_explicit_binding() -> None:
    """The live API's actual behavior, and the crux of inbound readiness.

    A dispatch rule with no `inbound_numbers` matches every number, so LiveKit
    reports the number ACTIVE while `sip_dispatch_rule_ids` stays empty.
    Requiring an explicit binding here would report a working setup as broken.
    """
    number = parse_number(NUMBER_ACTIVE)

    assert number.status is NumberStatus.ACTIVE
    assert not number.explicitly_bound
    assert number.routed
    assert number.inbound_ready


def test_offline_number_is_held_but_not_routed() -> None:
    """OFFLINE means "no dispatch rule", not "broken" — it still needs routing."""
    number = parse_number(NUMBER_OFFLINE)

    assert number.held
    assert not number.routed
    assert not number.inbound_ready
    assert number.number_type is NumberType.TOLL_FREE


def test_released_number_is_not_held() -> None:
    number = parse_number(NUMBER_RELEASED)

    assert not number.held
    assert not number.inbound_ready


def test_parse_number_folds_in_the_deprecated_single_rule_id() -> None:
    number = parse_number({**NUMBER_ACTIVE, "sip_dispatch_rule_id": "SDR_legacy"})

    assert number.dispatch_rule_ids == ("SDR_legacy",)
    assert number.explicitly_bound


async def test_inventory_lists_numbers(numbers: FakeNumbers) -> None:
    inventory = await fetch_inventory(numbers)

    assert [n.number for n in inventory.numbers] == ["+12402124128"]
    assert inventory.default_caller_id == "+12402124128"
    assert inventory.routed_numbers
    assert inventory.warnings == ()


async def test_inventory_warns_when_nothing_is_routed() -> None:
    client = FakeNumbers({"ListPhoneNumbers": {"items": [NUMBER_OFFLINE]}})
    inventory = await fetch_inventory(client)

    assert not inventory.routed_numbers
    assert any("reach nobody" in w.lower() for w in inventory.warnings)


async def test_inventory_warns_when_there_are_no_numbers() -> None:
    client = FakeNumbers({"ListPhoneNumbers": {"items": []}})
    inventory = await fetch_inventory(client)

    assert inventory.numbers == ()
    assert any("free us local number" in w.lower() for w in inventory.warnings)


async def test_inventory_default_prefers_configuration(numbers: FakeNumbers) -> None:
    """A configured caller ID wins, so a misconfiguration stays visible."""
    inventory = await fetch_inventory(numbers, default_caller_id="+12409999999")
    assert inventory.default_caller_id == "+12409999999"


async def test_safe_fetch_never_raises() -> None:
    client = FakeNumbers(
        {"ListPhoneNumbers": PhoneNumberError("livekit down", status=503)}
    )
    inventory = await safe_fetch_inventory(client, default_caller_id="+12402124128")

    assert inventory.numbers == ()
    assert inventory.default_caller_id == "+12402124128"
    assert inventory.warnings


async def test_unsupported_plan_is_explained_not_reported_as_broken() -> None:
    client = FakeNumbers(
        {
            "ListPhoneNumbers": PhoneNumberError(
                "not found", status=404, code="not_found"
            )
        }
    )
    inventory = await safe_fetch_inventory(client)

    assert any("not available" in w.lower() for w in inventory.warnings)


def test_phone_number_client_requires_credentials() -> None:
    with pytest.raises(PhoneNumberError):
        PhoneNumberClient(LiveKitSettings())


async def test_search_and_purchase_payloads(numbers: FakeNumbers) -> None:
    numbers.responses["SearchPhoneNumbers"] = {"items": [NUMBER_OFFLINE]}
    numbers.responses["PurchasePhoneNumber"] = {"phone_numbers": [NUMBER_OFFLINE]}

    found = await numbers.search(area_code="240", limit=5)
    assert [n.number for n in found] == ["+12402124129"]
    method, body = numbers.calls[-1]
    assert method == "SearchPhoneNumbers"
    assert body == {"country_code": "US", "limit": 5, "area_code": "240"}

    bought = await numbers.purchase(["+12402124129"], dispatch_rule_id="SDR_x")
    assert [n.number for n in bought] == ["+12402124129"]
    method, body = numbers.calls[-1]
    assert method == "PurchasePhoneNumber"
    assert body == {
        "phone_numbers": ["+12402124129"],
        "sip_dispatch_rule_id": "SDR_x",
    }


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


async def test_provision_creates_the_rule_and_routes_the_number(
    numbers: FakeNumbers,
) -> None:
    numbers.responses["UpdatePhoneNumber"] = {
        "phone_number": {**NUMBER_ACTIVE, "sip_dispatch_rule_ids": ["SDR_new"]}
    }
    livekit = FakeLiveKit()

    report = await provision(make_settings(), lkapi=livekit, client=numbers)

    assert not report.failed
    assert report.env["LIVEKIT_SIP_DISPATCH_RULE_ID"] == "SDR_new"

    (request,) = livekit.sip.created_rules
    assert request.name == "AI Caller inbound"
    assert [a.agent_name for a in request.room_config.agents] == ["my-agent"]
    # The rule must carry inbound metadata, or the agent would not know to greet.
    assert (
        DialInfo.from_json(request.room_config.agents[0].metadata).direction
        is Direction.INBOUND
    )
    # A callee rule keeps the caller's number out of room names.
    assert request.rule.dispatch_rule_callee.room_prefix == "call-"


async def test_provision_survives_pinning_being_unavailable(
    numbers: FakeNumbers,
) -> None:
    """The live failure mode: UpdatePhoneNumber is rejected by the server.

    Matching alone routes the call, so setup must report success rather than
    failing a configuration that works.
    """
    numbers.responses["UpdatePhoneNumber"] = PhoneNumberError(
        "Failed to update phone number", status=400, code="invalid_argument"
    )

    report = await provision(make_settings(), lkapi=FakeLiveKit(), client=numbers)

    assert not report.failed
    assert any("pinning unavailable" in step.detail for step in report.steps)


async def test_provision_is_idempotent(numbers: FakeNumbers) -> None:
    """A second run must not create a second rule."""
    existing = FakeRuleInfo(
        sip_dispatch_rule_id="SDR_existing",
        name="AI Caller inbound",
        agents=["my-agent"],
    )
    livekit = FakeLiveKit(rules=[existing])

    report = await provision(make_settings(), lkapi=livekit, client=numbers)

    assert livekit.sip.created_rules == []
    assert report.env["LIVEKIT_SIP_DISPATCH_RULE_ID"] == "SDR_existing"


async def test_provision_replaces_a_rule_pointing_at_the_wrong_agent(
    numbers: FakeNumbers,
) -> None:
    """Renaming AGENT_NAME must not leave calls going to a dead agent."""
    stale = FakeRuleInfo(
        sip_dispatch_rule_id="SDR_stale",
        name="AI Caller inbound",
        agents=["old-agent"],
    )
    livekit = FakeLiveKit(rules=[stale])

    await provision(make_settings(), lkapi=livekit, client=numbers)

    assert livekit.sip.deleted_rules == ["SDR_stale"]
    assert livekit.sip.created_rules


async def test_provision_notes_that_outbound_is_unavailable(
    numbers: FakeNumbers,
) -> None:
    report = await provision(make_settings(), lkapi=FakeLiveKit(), client=numbers)

    assert any("outbound" in note.lower() for note in report.notes)


async def test_provision_with_no_numbers_still_creates_the_rule() -> None:
    client = FakeNumbers({"ListPhoneNumbers": {"items": []}})
    livekit = FakeLiveKit()

    report = await provision(make_settings(), lkapi=livekit, client=client)

    assert livekit.sip.created_rules
    assert any("free us local number" in note.lower() for note in report.notes)


# ---------------------------------------------------------------------------
# SIP dial requests
# ---------------------------------------------------------------------------


def _target() -> OutboundTarget:
    return OutboundTarget(
        to_number="+971501234567",
        from_number="+12402124128",
        room="call-abc123",
        call_id="abc123",
        identity="sip-abc123",
    )


def test_request_uses_the_outbound_trunk() -> None:
    settings = make_settings(outbound_trunk_id="ST_stored")
    request = build_participant_request(settings.telephony, _target())

    assert request.sip_trunk_id == "ST_stored"
    assert request.sip_call_to == "+971501234567"
    assert request.room_name == "call-abc123"
    assert request.participant_identity == "sip-abc123"
    assert request.sip_number == "+12402124128"
    assert request.wait_until_answered


def test_request_carries_the_policy_timeouts() -> None:
    settings = make_settings(
        outbound_trunk_id="ST_stored",
        policy=CallPolicySettings(ring_timeout_s=25, max_call_duration_s=600),
    )
    request = build_participant_request(settings.telephony, _target())

    assert request.ringing_timeout.seconds == 25
    assert request.max_call_duration.seconds == 600


def test_request_exposes_call_identity_as_attributes() -> None:
    settings = make_settings(outbound_trunk_id="ST_stored")
    request = build_participant_request(settings.telephony, _target())

    assert request.participant_attributes["call.direction"] == "outbound"
    assert request.participant_attributes["call.id"] == "abc123"
    assert DialInfo.from_json(request.participant_metadata).to_number == (
        "+971501234567"
    )


def test_request_without_an_outbound_trunk_is_refused() -> None:
    """LiveKit numbers cannot dial out, so this must fail clearly and early."""
    with pytest.raises(DialError) as excinfo:
        build_participant_request(make_settings().telephony, _target())

    assert excinfo.value.code == "not_configured"
    assert "inbound-only" in excinfo.value.message


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (486, "busy"),
        (603, "declined"),
        (408, "no_answer"),
        (480, "unavailable"),
        (503, "trunk_failure"),
        (403, "forbidden"),
        (599, "dial_failed"),
    ],
)
def test_sip_failures_are_explained(status: int, code: str) -> None:
    """ "Busy" and "no answer" must not both surface as "call failed"."""
    error = api.SipCallError(
        "sip_error",
        "failed",
        status=500,
        metadata={"sip_status_code": str(status), "sip_status": "reason"},
    )
    described = describe_sip_failure(error)

    assert described.code == code
    assert described.sip_status == status


# ---------------------------------------------------------------------------
# The control plane
# ---------------------------------------------------------------------------


async def test_place_call_creates_a_room_with_the_agent_dispatched(
    service: TelephonyService,
) -> None:
    placed = await service.place_call("+971 50 123 4567")

    assert placed.record.direction is Direction.OUTBOUND
    assert placed.record.to_number == "+971501234567"
    assert placed.record.from_number == "+12402124128"
    assert placed.record.status is CallStatus.DIALING
    assert placed.record.room.startswith("call-")
    assert placed.viewer_token

    # The room and the agent dispatch are one request: no window where the room
    # exists but no agent has been asked for.
    (request,) = service._lkapi.room.created  # type: ignore[union-attr]
    assert request.name == placed.record.room
    assert [agent.agent_name for agent in request.agents] == ["my-agent"]

    dispatched = DialInfo.from_json(request.agents[0].metadata)
    assert dispatched.direction is Direction.OUTBOUND
    assert dispatched.to_number == "+971501234567"
    assert dispatched.call_id == placed.record.call_id


async def test_place_call_passes_context_to_the_agent(
    service: TelephonyService,
) -> None:
    await service.place_call("+971501234567", context={"name": "Aisha"})

    (request,) = service._lkapi.room.created  # type: ignore[union-attr]
    assert DialInfo.from_json(request.agents[0].metadata).context["name"] == "Aisha"


async def test_place_call_refuses_an_emergency_number(
    service: TelephonyService,
) -> None:
    with pytest.raises(PolicyError) as excinfo:
        await service.place_call("999")

    assert excinfo.value.code == "emergency_number"
    assert not service._lkapi.room.created  # type: ignore[union-attr]


async def test_place_call_needs_livekit_credentials(numbers: FakeNumbers) -> None:
    settings = make_settings(livekit=LiveKitSettings())
    service = TelephonyService(settings, lkapi=FakeLiveKit(), numbers=numbers)

    with pytest.raises(TelephonyDisabledError):
        await service.place_call("+971501234567")


async def test_place_call_without_an_outbound_trunk(numbers: FakeNumbers) -> None:
    """Inbound-only is the default state, and must be explained not crashed."""
    service = TelephonyService(make_settings(), lkapi=FakeLiveKit(), numbers=numbers)

    with pytest.raises(DialError) as excinfo:
        await service.place_call("+971501234567")
    assert excinfo.value.code == "not_configured"


async def test_concurrency_limit_counts_live_rooms(numbers: FakeNumbers) -> None:
    rooms = [FakeRoom(name="call-one"), FakeRoom(name="call-two")]
    settings = make_settings(
        outbound_trunk_id="ST_trunk",
        policy=CallPolicySettings(max_concurrent_calls=2),
    )
    service = TelephonyService(settings, lkapi=FakeLiveKit(rooms), numbers=numbers)

    with pytest.raises(PolicyError) as excinfo:
        await service.place_call("+971501234567")
    assert excinfo.value.code == "too_many_calls"


async def test_list_calls_ignores_rooms_that_are_not_calls(
    numbers: FakeNumbers,
) -> None:
    rooms = [
        FakeRoom(name="ai-caller-web123"),  # browser session
        FakeRoom(
            name="call-abc",
            metadata=DialInfo(
                direction=Direction.OUTBOUND,
                to_number="+971501234567",
                from_number="+12402124128",
                call_id="abc",
            ).to_json(),
            num_participants=2,
        ),
    ]
    livekit = FakeLiveKit(rooms)
    livekit.room.participants["call-abc"] = [
        FakeParticipant(
            identity="sip-abc",
            attributes={
                "sip.callStatus": "active",
                "sip.phoneNumber": "+971501234567",
            },
        )
    ]
    service = TelephonyService(make_settings(), lkapi=livekit, numbers=numbers)

    calls = await service.list_calls()

    assert len(calls) == 1
    assert calls[0].room == "call-abc"
    assert calls[0].status is CallStatus.ACTIVE
    assert calls[0].direction is Direction.OUTBOUND
    assert calls[0].participants == 2


async def test_list_calls_reads_inbound_numbers_from_sip_attributes(
    numbers: FakeNumbers,
) -> None:
    """Inbound rooms are created by the dispatch rule and carry no metadata."""
    livekit = FakeLiveKit([FakeRoom(name="call-12402124128_x1", num_participants=2)])
    livekit.room.participants["call-12402124128_x1"] = [
        FakeParticipant(
            identity="sip-inbound",
            attributes={
                "sip.callStatus": "active",
                "sip.phoneNumber": "+919876543210",
                "sip.trunkPhoneNumber": "+12402124128",
            },
        )
    ]
    service = TelephonyService(make_settings(), lkapi=livekit, numbers=numbers)

    (call,) = await service.list_calls()

    assert call.direction is Direction.INBOUND
    assert call.from_number == "+919876543210"
    assert call.to_number == "+12402124128"


async def test_hangup_deletes_the_room(service: TelephonyService) -> None:
    await service.hangup("call-abc123")

    assert service._lkapi.room.deleted == ["call-abc123"]  # type: ignore[union-attr]


async def test_hangup_refuses_rooms_that_are_not_calls(
    service: TelephonyService,
) -> None:
    """A stray room name must not become a way to delete arbitrary rooms."""
    with pytest.raises(PolicyError) as excinfo:
        await service.hangup("ai-caller-web123")

    assert excinfo.value.code == "not_a_call"
    assert not service._lkapi.room.deleted  # type: ignore[union-attr]


async def test_viewer_token_cannot_publish(service: TelephonyService) -> None:
    token = service.viewer_token("call-abc123")
    claims = api.TokenVerifier("devkey", "secret-value-long-enough-for-hmac").verify(
        token
    )

    assert claims.video.room == "call-abc123"
    assert claims.video.can_subscribe
    assert not claims.video.can_publish
    assert claims.video.hidden


async def test_inventory_is_cached(service: TelephonyService) -> None:
    await service.inventory()
    first = len(service._numbers.calls)  # type: ignore[union-attr]

    await service.inventory()
    assert len(service._numbers.calls) == first  # type: ignore[union-attr]

    await service.inventory(refresh=True)
    assert len(service._numbers.calls) > first  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Inbound readiness
# ---------------------------------------------------------------------------


async def test_inbound_ready_with_a_catch_all_rule(numbers: FakeNumbers) -> None:
    """The shape a live project actually has: rule matches all numbers."""
    rules = [
        FakeRuleInfo(
            sip_dispatch_rule_id="SDR_all",
            name="AI Caller inbound",
            inbound_numbers=[],
            agents=["my-agent"],
        )
    ]
    service = TelephonyService(
        make_settings(), lkapi=FakeLiveKit(rules=rules), numbers=numbers
    )

    status = await service.inbound_status()

    assert status["ready"] is True
    assert status["agentName"] == "my-agent"


async def test_inbound_ready_with_a_number_scoped_rule(numbers: FakeNumbers) -> None:
    rules = [
        FakeRuleInfo(
            sip_dispatch_rule_id="SDR_scoped",
            name="AI Caller inbound",
            inbound_numbers=["+12402124128"],
            agents=["my-agent"],
        )
    ]
    service = TelephonyService(
        make_settings(), lkapi=FakeLiveKit(rules=rules), numbers=numbers
    )

    assert (await service.inbound_status())["ready"] is True


async def test_inbound_not_ready_when_the_rule_scopes_another_number(
    numbers: FakeNumbers,
) -> None:
    rules = [
        FakeRuleInfo(
            sip_dispatch_rule_id="SDR_other",
            name="AI Caller inbound",
            inbound_numbers=["+15550001111"],
            agents=["my-agent"],
        )
    ]
    service = TelephonyService(
        make_settings(), lkapi=FakeLiveKit(rules=rules), numbers=numbers
    )

    assert (await service.inbound_status())["ready"] is False


async def test_inbound_not_ready_when_the_rule_dispatches_another_agent(
    numbers: FakeNumbers,
) -> None:
    """A rule for a different agent is the silent-call failure mode."""
    rules = [
        FakeRuleInfo(
            sip_dispatch_rule_id="SDR_other",
            name="someone else",
            agents=["other-agent"],
        )
    ]
    service = TelephonyService(
        make_settings(), lkapi=FakeLiveKit(rules=rules), numbers=numbers
    )

    assert (await service.inbound_status())["ready"] is False


# ---------------------------------------------------------------------------
# The agent side
# ---------------------------------------------------------------------------


class FakeJob:
    def __init__(self, metadata: str) -> None:
        self.metadata = metadata


class FakeJobContext:
    def __init__(
        self,
        metadata: str = "",
        room: str = "call-abc123",
        participants: dict[str, Any] | None = None,
    ) -> None:
        self.job = FakeJob(metadata)
        self.room = type(
            "Room",
            (),
            {"name": room, "remote_participants": participants or {}},
        )()
        self.api = FakeLiveKit()
        self.log_context_fields: dict[str, Any] = {}
        self.shutdown_reasons: list[str] = []

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reasons.append(reason)

    async def wait_for_participant(self, *, identity: str | None = None):
        return FakeParticipant(identity=identity or "sip-abc123")


@dataclass
class FakeRemoteParticipant:
    identity: str
    kind: int = 0
    attributes: dict[str, str] = field(default_factory=dict)


def test_resolve_direction_rescues_a_call_with_no_metadata() -> None:
    """The worst failure mode: a phone call mistaken for a web session.

    The agent would then wait for the caller, while the caller waits for the
    agent. A SIP participant in the room proves it is a call regardless of
    what the dispatch metadata did or did not carry.
    """
    ctx = FakeJobContext(
        metadata="",
        participants={
            "sip_x": FakeRemoteParticipant(
                identity="sip_x",
                kind=3,  # rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                attributes={
                    "sip.phoneNumber": "+919876543210",
                    "sip.trunkPhoneNumber": "+12402124128",
                },
            )
        },
    )

    resolved = dial_module.resolve_direction(
        ctx,  # type: ignore[arg-type]
        DialInfo(),
    )

    assert resolved.direction is Direction.INBOUND
    assert resolved.from_number == "+919876543210"
    assert resolved.to_number == "+12402124128"


def test_resolve_direction_detects_sip_by_attributes_too() -> None:
    """Kind is the strong signal, but attributes alone are enough."""
    ctx = FakeJobContext(
        participants={
            "x": FakeRemoteParticipant(
                identity="x", kind=0, attributes={"sip.callStatus": "active"}
            )
        }
    )

    assert (
        dial_module.resolve_direction(ctx, DialInfo()).direction  # type: ignore[arg-type]
        is Direction.INBOUND
    )


def test_resolve_direction_leaves_web_sessions_alone() -> None:
    """A browser participant must not be mistaken for a caller."""
    ctx = FakeJobContext(
        participants={
            "user": FakeRemoteParticipant(identity="user-123", kind=1, attributes={})
        }
    )

    assert (
        dial_module.resolve_direction(ctx, DialInfo()).direction  # type: ignore[arg-type]
        is Direction.WEB
    )


def test_resolve_direction_trusts_explicit_metadata() -> None:
    """An outbound call must never be downgraded by what is in the room."""
    dial = DialInfo(
        direction=Direction.OUTBOUND, to_number="+971501234567", call_id="abc"
    )
    ctx = FakeJobContext(
        participants={
            "sip": FakeRemoteParticipant(identity="sip-abc", kind=3, attributes={})
        }
    )

    assert dial_module.resolve_direction(ctx, dial) is dial  # type: ignore[arg-type]


def test_call_context_reads_the_job_metadata() -> None:
    ctx = FakeJobContext(
        DialInfo(
            direction=Direction.OUTBOUND, to_number="+971501234567", call_id="abc"
        ).to_json()
    )
    dial = dial_module.call_context(ctx)  # type: ignore[arg-type]

    assert dial.is_outbound
    assert dial.to_number == "+971501234567"


def test_call_tools_only_on_phone_calls() -> None:
    """A browser session has no call to hang up, so no end_call tool."""
    phone = dial_module.call_tools(DialInfo(direction=Direction.INBOUND))
    web = dial_module.call_tools(DialInfo(direction=Direction.WEB))

    assert len(phone) == 1
    assert phone[0].id == "end_call"
    assert web == []


def test_link_target_only_for_outbound() -> None:
    outbound = DialInfo(direction=Direction.OUTBOUND, call_id="abc")
    assert dial_module.link_target(outbound) == "sip-abc"
    assert dial_module.link_target(DialInfo(direction=Direction.INBOUND)) is None
    assert dial_module.link_target(DialInfo()) is None


async def test_dial_out_explains_a_rejected_call() -> None:
    ctx = FakeJobContext()
    ctx.api.sip.error = api.SipCallError(
        "sip_error",
        "failed",
        status=500,
        metadata={"sip_status_code": "486", "sip_status": "Busy Here"},
    )
    dial = DialInfo(
        direction=Direction.OUTBOUND,
        to_number="+971501234567",
        from_number="+12402124128",
        call_id="abc",
    )

    with pytest.raises(DialError) as excinfo:
        await dial_module.dial_out(
            ctx,  # type: ignore[arg-type]
            dial,
            make_settings(outbound_trunk_id="ST_stored").telephony,
        )

    assert excinfo.value.code == "busy"
    # The job must be released, not left holding a worker slot.
    assert ctx.shutdown_reasons


async def test_dial_out_returns_the_callee() -> None:
    ctx = FakeJobContext()
    dial = DialInfo(
        direction=Direction.OUTBOUND,
        to_number="+971501234567",
        from_number="+12402124128",
        call_id="abc",
    )

    participant = await dial_module.dial_out(
        ctx,  # type: ignore[arg-type]
        dial,
        make_settings(outbound_trunk_id="ST_stored").telephony,
    )

    assert participant.identity == "sip-abc"
    (request,) = ctx.api.sip.participants
    assert request.sip_call_to == "+971501234567"
    assert request.room_name == "call-abc123"
    assert not ctx.shutdown_reasons


class FakeSession:
    """Records opening turns instead of speaking them."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self._handlers: dict[str, list[Any]] = {}

    def generate_reply(self, *, instructions: str = "", **_kwargs: Any) -> None:
        self.replies.append(instructions)

    def on(self, event: str, callback: Any) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Any) -> None:
        self._handlers.get(event, []).remove(callback)

    def emit(self, event: str) -> None:
        for callback in list(self._handlers.get(event, [])):
            callback(object())


async def test_inbound_calls_are_greeted_immediately() -> None:
    session = FakeSession()

    await dial_module.open_conversation(
        session,  # type: ignore[arg-type]
        DialInfo(direction=Direction.INBOUND),
        "Answer the phone.",
    )

    assert session.replies == ["Answer the phone."]


async def test_web_sessions_are_not_greeted() -> None:
    """Unchanged from before telephony: the browser user speaks first."""
    session = FakeSession()

    await dial_module.open_conversation(
        session,  # type: ignore[arg-type]
        DialInfo(),
        "Say hello.",
    )

    assert session.replies == []


async def test_outbound_waits_for_the_callee_to_speak() -> None:
    """Somebody who answers with "hello?" should be replied to, not talked over."""
    session = FakeSession()
    dial = DialInfo(direction=Direction.OUTBOUND, to_number="+971501234567")

    task = asyncio.create_task(
        dial_module.open_conversation(
            session,  # type: ignore[arg-type]
            dial,
            "Introduce yourself.",
            delay_s=5.0,
        )
    )
    await asyncio.sleep(0)
    session.emit("user_input_transcribed")
    await task

    assert session.replies == []


async def test_outbound_opens_the_conversation_on_silence() -> None:
    """A silent pickup must not leave both sides waiting."""
    session = FakeSession()
    dial = DialInfo(direction=Direction.OUTBOUND, to_number="+971501234567")

    await dial_module.open_conversation(
        session,  # type: ignore[arg-type]
        dial,
        "Introduce yourself.",
        delay_s=0.01,
    )

    assert session.replies == ["Introduce yourself."]


def test_caller_note_describes_the_call() -> None:
    inbound = dial_module.caller_note(
        DialInfo(direction=Direction.INBOUND, to_number="+12402124128"), None
    )
    assert "inbound" in inbound.lower()

    outbound = dial_module.caller_note(
        DialInfo(direction=Direction.OUTBOUND, context={"name": "Aisha"}), None
    )
    assert "Aisha" in outbound

    assert dial_module.caller_note(DialInfo(), None) == ""


# ---------------------------------------------------------------------------
# The HTTP control plane
# ---------------------------------------------------------------------------


@pytest.fixture
def client(service: TelephonyService):
    from fastapi.testclient import TestClient

    from ai_caller.telephony.api import create_app

    app = create_app(make_settings(outbound_trunk_id="ST_trunk"), service=service)
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_configuration(client) -> None:
    body = client.get("/api/telephony/health").json()

    assert body["enabled"]
    assert body["canDialOut"]
    assert body["agentName"] == "my-agent"
    assert body["roomPrefix"] == "call"


def test_health_reports_inbound_only_without_a_trunk(
    service: TelephonyService,
) -> None:
    from fastapi.testclient import TestClient

    from ai_caller.telephony.api import create_app

    with TestClient(create_app(make_settings(), service=service)) as inbound_only:
        body = inbound_only.get("/api/telephony/health").json()

    assert body["enabled"]
    assert body["canDialOut"] is False


def test_numbers_endpoint(client) -> None:
    body = client.get("/api/telephony/numbers").json()

    assert body["defaultCallerId"] == "+12402124128"
    (number,) = body["numbers"]
    assert number["number"] == "+12402124128"
    assert number["label"] == "OAKLAND, MD"
    assert number["inboundReady"] is True
    assert number["explicitlyBound"] is False


def test_inbound_endpoint(client) -> None:
    body = client.get("/api/telephony/inbound").json()

    assert "ready" in body
    assert body["agentName"] == "my-agent"


def test_place_call_endpoint(client) -> None:
    response = client.post("/api/telephony/calls", json={"to": "+971501234567"})

    assert response.status_code == 201
    body = response.json()
    assert body["toNumber"] == "+971501234567"
    assert body["room"].startswith("call-")
    assert body["viewerToken"]


def test_place_call_endpoint_reports_refusals(client) -> None:
    response = client.post("/api/telephony/calls", json={"to": "999"})

    assert response.status_code == 400
    assert response.json()["code"] == "emergency_number"


def test_place_call_endpoint_validates_its_body(client) -> None:
    assert client.post("/api/telephony/calls", json={}).status_code == 422


def test_list_and_hangup_endpoints(client, service: TelephonyService) -> None:
    client.post("/api/telephony/calls", json={"to": "+971501234567"})
    calls = client.get("/api/telephony/calls").json()["calls"]

    assert len(calls) == 1
    room = calls[0]["room"]

    response = client.delete(f"/api/telephony/calls/{room}")
    assert response.status_code == 200
    assert service._lkapi.room.deleted == [room]  # type: ignore[union-attr]


def test_hangup_endpoint_refuses_other_rooms(client) -> None:
    response = client.delete("/api/telephony/calls/ai-caller-web123")

    assert response.status_code == 400
    assert response.json()["code"] == "not_a_call"


def test_monitor_endpoint_returns_a_viewer_token(client) -> None:
    body = client.post("/api/telephony/calls/call-abc123/monitor").json()

    assert body["room"] == "call-abc123"
    assert body["viewerToken"]


def test_api_key_is_enforced_when_configured(service: TelephonyService) -> None:
    from fastapi.testclient import TestClient

    from ai_caller.telephony.api import create_app

    app = create_app(make_settings(api_key="s3cret"), service=service)
    with TestClient(app) as guarded:
        # Health stays open so the UI can explain why it is locked.
        assert guarded.get("/api/telephony/health").status_code == 200
        assert guarded.get("/api/telephony/numbers").status_code == 401
        assert (
            guarded.get(
                "/api/telephony/numbers", headers={"X-API-Key": "s3cret"}
            ).status_code
            == 200
        )


def test_numbers_endpoint_degrades_when_livekit_is_down() -> None:
    from fastapi.testclient import TestClient

    from ai_caller.telephony.api import create_app

    broken = FakeNumbers(
        {"ListPhoneNumbers": PhoneNumberError("livekit down", status=503)}
    )
    service = TelephonyService(make_settings(), lkapi=FakeLiveKit(), numbers=broken)

    with TestClient(create_app(make_settings(), service=service)) as test_client:
        body = test_client.get("/api/telephony/numbers").json()

    assert body["numbers"] == []
    assert body["warnings"]


def test_settings_defaults_are_inbound_only() -> None:
    """The out-of-the-box state: inbound works, outbound is off."""
    telephony = make_settings().telephony

    assert telephony.enabled
    assert not telephony.can_dial_out
    assert replace(telephony, outbound_trunk_id="ST_x").can_dial_out
