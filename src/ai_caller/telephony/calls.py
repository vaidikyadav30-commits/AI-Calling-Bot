"""The control plane: list numbers, place calls, watch them, hang them up.

One object, ``TelephonyService``, owns every side effect so the HTTP layer in
``api.py`` stays a thin translation of requests into method calls, and so the
same operations are available from a script or a test without a server running.

How an outbound call is actually placed: this service creates the room and
dispatches the agent into it with the destination in the job metadata, then
returns. The **agent** dials the callee from inside the job (see ``dial.py``).
That ordering matters — the agent is in the room and ready before the phone
starts ringing, so the callee never picks up to silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import time
import uuid
from dataclasses import dataclass

from livekit import api
from livekit.protocol.models import ParticipantInfo, Room

from ai_caller.config import Settings, TelephonySettings
from ai_caller.telephony.models import (
    CallRecord,
    CallStatus,
    DialInfo,
    Direction,
    NumberInventory,
    PhoneNumber,
)
from ai_caller.telephony.numbers import PhoneNumberClient, safe_fetch_inventory
from ai_caller.telephony.policy import CallPolicy, PolicyError
from ai_caller.telephony.sip import DialError, participant_identity

logger = logging.getLogger("ai_caller.telephony.calls")

# The number list changes when one is rented or released, which is rare.
# Caching it keeps the dialer instant without hiding real changes for long,
# and a manual refresh bypasses the cache.
INVENTORY_TTL_S = 60.0

# A room with nobody in it is a failed or finished call; let LiveKit reap it
# instead of leaving rooms behind for the active-call list to trip over.
ROOM_EMPTY_TIMEOUT_S = 60
ROOM_DEPARTURE_TIMEOUT_S = 10

# A phone call is the agent plus one caller, plus a little room for a human
# supervisor to listen in or take over.
ROOM_MAX_PARTICIPANTS = 4


class TelephonyDisabledError(Exception):
    """Raised when an operation needs configuration that is not present."""


@dataclass(frozen=True)
class PlacedCall:
    """Result of accepting an outbound call request."""

    record: CallRecord
    # Short-lived token letting the browser join the call room to listen and
    # read the transcript. Subscribe-only: a monitor cannot talk to the callee.
    viewer_token: str
    server_url: str

    def to_dict(self) -> dict[str, object]:
        return {
            **self.record.to_dict(),
            "viewerToken": self.viewer_token,
            "serverUrl": self.server_url,
        }


class TelephonyService:
    """Everything the dialer can do, independent of how it is invoked."""

    def __init__(
        self,
        settings: Settings,
        *,
        lkapi: api.LiveKitAPI | None = None,
        numbers: PhoneNumberClient | None = None,
        policy: CallPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._telephony = settings.telephony
        self._policy = policy or CallPolicy(settings.telephony.policy)

        self._lkapi = lkapi
        self._owns_lkapi = lkapi is None
        self._numbers = numbers
        self._owns_numbers = numbers is None

        self._inventory: NumberInventory | None = None
        self._inventory_at = 0.0
        self._inventory_lock = asyncio.Lock()

    # -------------------------------------------------------------- lifecycle

    @property
    def settings(self) -> TelephonySettings:
        return self._telephony

    @property
    def enabled(self) -> bool:
        return self._telephony.enabled

    async def aclose(self) -> None:
        if self._owns_lkapi and self._lkapi is not None:
            with contextlib.suppress(Exception):
                await self._lkapi.aclose()
            self._lkapi = None
        if self._owns_numbers and self._numbers is not None:
            with contextlib.suppress(Exception):
                await self._numbers.aclose()
            self._numbers = None

    def _lk(self) -> api.LiveKitAPI:
        livekit = self._telephony.livekit
        if not livekit.configured:
            raise TelephonyDisabledError(
                "LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set."
            )
        if self._lkapi is None:
            self._lkapi = api.LiveKitAPI(
                url=livekit.url,
                api_key=livekit.api_key,
                api_secret=livekit.api_secret,
            )
            self._owns_lkapi = True
        return self._lkapi

    def _nums(self) -> PhoneNumberClient:
        """Client for LiveKit's Phone Numbers API."""
        if self._numbers is None:
            if not self._telephony.livekit.configured:
                raise TelephonyDisabledError(
                    "LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set."
                )
            self._numbers = PhoneNumberClient(self._telephony.livekit)
            self._owns_numbers = True
        return self._numbers

    # --------------------------------------------------------------- numbers

    async def inventory(self, *, refresh: bool = False) -> NumberInventory:
        """LiveKit phone numbers and their routing, cached for a minute."""
        async with self._inventory_lock:
            fresh = (
                self._inventory is not None
                and time.monotonic() - self._inventory_at < INVENTORY_TTL_S
            )
            if fresh and not refresh and self._inventory is not None:
                return self._inventory

            inventory = await safe_fetch_inventory(
                self._nums(), default_caller_id=self._telephony.caller_id
            )
            self._inventory = inventory
            self._inventory_at = time.monotonic()
            return inventory

    # ------------------------------------------------------------- outbound

    async def place_call(
        self,
        to_number: str,
        *,
        from_number: str = "",
        context: dict[str, str] | None = None,
    ) -> PlacedCall:
        """Validate, then start an outbound call. Raises on refusal.

        Raises ``PolicyError`` when a guardrail refuses the call, ``DialError``
        when outbound is not configured, and ``TelephonyDisabledError`` when
        credentials are missing. None of those cost a carrier call.
        """
        if not self._telephony.enabled:
            raise TelephonyDisabledError(
                "Telephony is not configured. Add your LiveKit "
                "credentials to .env.local."
            )
        if not self._telephony.can_dial_out:
            raise DialError(
                "not_configured",
                "Outbound calling is not configured. LiveKit Phone Numbers is "
                "inbound-only today, so placing calls needs a LiveKit outbound "
                "trunk pointed at a carrier. Create one, then set "
                "LIVEKIT_SIP_OUTBOUND_TRUNK_ID.",
            )

        inventory = await self.inventory()
        caller_id = from_number or inventory.default_caller_id

        destination = self._policy.check_destination(to_number)
        # Numbers rented from LiveKit cannot present as caller ID on a
        # third-party outbound trunk, so the ownership check only applies when
        # the inventory actually contains the requested number.
        caller_id = self._policy.check_caller_id(caller_id)

        active = await self.list_calls()
        self._policy.check_capacity(len(active))

        call_id = uuid.uuid4().hex[:12]
        room = f"{self._telephony.room_prefix}-{call_id}"
        dial = DialInfo(
            direction=Direction.OUTBOUND,
            to_number=destination,
            from_number=caller_id,
            call_id=call_id,
            context=context or {},
        )

        await self._create_call_room(room, dial)
        self._policy.record_call()

        logger.info("outbound call %s: room=%s from=%s", call_id, room, caller_id)

        record = CallRecord(
            call_id=call_id,
            room=room,
            direction=Direction.OUTBOUND,
            to_number=destination,
            from_number=caller_id,
            status=CallStatus.DIALING,
            started_at=time.time(),
        )
        return PlacedCall(
            record=record,
            viewer_token=self.viewer_token(room),
            server_url=self._telephony.livekit.url,
        )

    async def _create_call_room(self, room: str, dial: DialInfo) -> None:
        """Create the room with the agent already dispatched into it.

        Doing both in one request removes the window where the room exists but
        no agent has been asked for, which is the failure that leaves a caller
        listening to silence.
        """
        await self._lk().room.create_room(
            api.CreateRoomRequest(
                name=room,
                metadata=dial.to_json(),
                empty_timeout=ROOM_EMPTY_TIMEOUT_S,
                departure_timeout=ROOM_DEPARTURE_TIMEOUT_S,
                max_participants=ROOM_MAX_PARTICIPANTS,
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=self._settings.agent_name,
                        metadata=dial.to_json(),
                    )
                ],
            )
        )

    # ---------------------------------------------------------- live calls

    async def list_calls(self) -> list[CallRecord]:
        """Calls currently up, inbound and outbound alike.

        Rooms are the source of truth rather than an in-process dictionary, so
        this stays correct across restarts of the control plane and includes
        inbound calls it never saw created.
        """
        try:
            response = await self._lk().room.list_rooms(api.ListRoomsRequest())
        except Exception as exc:
            logger.warning("could not list rooms: %s", exc)
            return []

        prefix = f"{self._telephony.room_prefix}-"
        rooms = [room for room in response.rooms if room.name.startswith(prefix)]
        if not rooms:
            return []

        records = await asyncio.gather(
            *(self._describe_room(room) for room in rooms),
            return_exceptions=True,
        )
        return [record for record in records if isinstance(record, CallRecord)]

    async def _describe_room(self, room: Room) -> CallRecord:
        dial = DialInfo.from_json(room.metadata)

        # Inbound rooms are created by the SIP dispatch rule, which knows
        # nothing about DialInfo — their numbers come from the SIP participant's
        # attributes instead.
        direction = dial.direction if dial.is_phone_call else Direction.INBOUND
        to_number = dial.to_number
        from_number = dial.from_number
        status = CallStatus.DIALING

        if room.num_participants:
            sip = await self._sip_participant(room.name)
            if sip is not None:
                status = _status_from_attributes(dict(sip.attributes))
                # LiveKit sets `sip.phoneNumber` to the far end of the call and
                # `sip.trunkPhoneNumber` to our own number on the trunk.
                far_end = sip.attributes.get("sip.phoneNumber", "")
                our_end = sip.attributes.get("sip.trunkPhoneNumber", "")
                if direction is Direction.INBOUND:
                    from_number = far_end or from_number
                    to_number = our_end or to_number
                else:
                    to_number = to_number or far_end
                    from_number = from_number or our_end

        return CallRecord(
            call_id=dial.call_id or room.name.rpartition("-")[2],
            room=room.name,
            direction=direction,
            to_number=to_number,
            from_number=from_number,
            status=status,
            started_at=float(room.creation_time),
            participants=room.num_participants,
        )

    async def _sip_participant(self, room: str) -> ParticipantInfo | None:
        """The phone participant in a room, if it has joined yet."""
        try:
            response = await self._lk().room.list_participants(
                api.ListParticipantsRequest(room=room)
            )
        except Exception as exc:
            logger.debug("could not list participants for %s: %s", room, exc)
            return None

        for participant in response.participants:
            if any(key.startswith("sip.") for key in participant.attributes):
                return participant
        return None

    async def hangup(self, room: str) -> None:
        """End a call for everyone by deleting its room.

        Deleting the room is what disconnects the SIP leg; shutting down only
        the agent would leave the caller holding a silent line.
        """
        if not room.startswith(f"{self._telephony.room_prefix}-"):
            raise PolicyError(
                "not_a_call",
                f"{room!r} is not a call room, so it will not be deleted.",
            )
        await self._lk().room.delete_room(api.DeleteRoomRequest(room=room))
        logger.info("hung up %s", room)

    # ------------------------------------------------------------- inbound

    async def inbound_status(self) -> dict[str, object]:
        """What inbound routing looks like right now.

        Both halves are reported, because either one missing means calls
        silently do not reach the agent: a dispatch rule must apply to the
        number, and that rule must dispatch *this* agent.
        """
        inventory = await self.inventory()
        routed = list(inventory.routed_numbers)

        rules: list[dict[str, object]] = []
        error = ""
        try:
            dispatch = await self._lk().sip.list_dispatch_rule(
                api.ListSIPDispatchRuleRequest()
            )
            rules = [
                {
                    "id": rule.sip_dispatch_rule_id,
                    "name": rule.name,
                    "agents": [agent.agent_name for agent in rule.room_config.agents],
                    # Empty means the rule matches every number on the project.
                    "inboundNumbers": list(rule.inbound_numbers),
                    "roomPrefix": (
                        rule.rule.dispatch_rule_callee.room_prefix
                        or rule.rule.dispatch_rule_individual.room_prefix
                    ),
                }
                for rule in dispatch.items
            ]
        except Exception as exc:
            logger.warning("could not read LiveKit SIP configuration: %s", exc)
            error = str(exc)

        agent_name = self._settings.agent_name
        ours = [
            rule
            for rule in rules
            if agent_name in rule["agents"]  # type: ignore[operator]
        ]

        # A number is answered when some rule that dispatches this agent
        # applies to it: pinned to it by ID, listed in the rule's inbound
        # numbers, or covered by a rule that matches every number.
        def answered(number: PhoneNumber) -> bool:
            for rule in ours:
                scope = rule["inboundNumbers"]
                if str(rule["id"]) in number.dispatch_rule_ids:
                    return True
                if not scope or number.number in scope:  # type: ignore[operator]
                    return True
            return False

        ready = any(answered(number) for number in routed)

        return {
            "ready": ready,
            "routedNumbers": [n.to_dict() for n in routed],
            "dispatchRules": rules,
            "agentName": agent_name,
            "error": error,
        }

    # --------------------------------------------------------------- tokens

    def viewer_token(self, room: str, *, ttl_minutes: int = 30) -> str:
        """A subscribe-only token for listening in on a call.

        Deliberately cannot publish: a monitor should never be able to talk
        over the agent, and `hidden` keeps the listener invisible to the caller.
        """
        livekit = self._telephony.livekit
        token = (
            api.AccessToken(livekit.api_key, livekit.api_secret)
            .with_identity(f"monitor-{uuid.uuid4().hex[:8]}")
            .with_name("Monitor")
            .with_ttl(datetime.timedelta(minutes=ttl_minutes))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room,
                    can_subscribe=True,
                    can_publish=False,
                    can_publish_data=False,
                    hidden=True,
                )
            )
        )
        return token.to_jwt()

    def call_token(self, room: str, identity: str, *, ttl_minutes: int = 30) -> str:
        """A full-duplex token, for taking over a call from the browser."""
        livekit = self._telephony.livekit
        token = (
            api.AccessToken(livekit.api_key, livekit.api_secret)
            .with_identity(identity)
            .with_ttl(datetime.timedelta(minutes=ttl_minutes))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room,
                    can_subscribe=True,
                    can_publish=True,
                    can_publish_data=True,
                )
            )
        )
        return token.to_jwt()


def _status_from_attributes(attributes: dict[str, str]) -> CallStatus:
    """Map LiveKit's ``sip.callStatus`` onto our own status enum."""
    raw = (attributes.get("sip.callStatus") or "").lower()
    if raw == "active":
        return CallStatus.ACTIVE
    if raw in ("ringing", "dialing"):
        return CallStatus.RINGING
    if raw in ("hangup", "disconnected"):
        return CallStatus.ENDED
    return CallStatus.DIALING


def build_service(settings: Settings | None = None) -> TelephonyService:
    """Factory used by the API server and the setup script."""
    return TelephonyService(settings or Settings.from_env())


__all__ = [
    "PlacedCall",
    "TelephonyDisabledError",
    "TelephonyService",
    "build_service",
    "participant_identity",
]
