"""Shared vocabulary for the telephony module.

These types are the contract between the three sides of a phone call:

* the **control plane** (``api.py``) which a human or another service drives,
* the **agent job** (``dial.py``) which places or answers the call, and
* the **browser** which lists numbers and monitors calls.

``DialInfo`` in particular is serialized into the agent dispatch metadata, so
its field names are a wire format: renaming one breaks calls that are already
queued.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class Direction(str, Enum):
    """Which way the call was set up.

    ``WEB`` covers browser sessions, which are not phone calls at all but share
    the same agent and therefore the same job metadata.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"
    WEB = "web"


class CallStatus(str, Enum):
    DIALING = "dialing"
    RINGING = "ringing"
    ACTIVE = "active"
    ENDED = "ended"
    FAILED = "failed"


class NumberStatus(str, Enum):
    """Lifecycle of a rented number, mirroring ``PhoneNumberStatus``.

    ``OFFLINE`` is the one worth knowing: it does not mean broken, it means the
    number has no dispatch rule attached. A freshly rented number sits there
    until it is routed, and assigning a rule is what turns it ``ACTIVE``.
    """

    UNSPECIFIED = "unspecified"
    ACTIVE = "active"
    PENDING = "pending"
    RELEASED = "released"
    OFFLINE = "offline"


class NumberType(str, Enum):
    UNKNOWN = "unknown"
    MOBILE = "mobile"
    LOCAL = "local"
    TOLL_FREE = "toll_free"


@dataclass(frozen=True)
class PhoneNumber:
    """A LiveKit phone number, as the UI needs to see it."""

    number: str
    # LiveKit's own identifier, needed to assign a dispatch rule.
    id: str = ""
    country_code: str = ""
    area_code: str = ""
    locality: str = ""
    region: str = ""
    number_type: NumberType = NumberType.UNKNOWN
    status: NumberStatus = NumberStatus.ACTIVE
    capabilities: tuple[str, ...] = ()
    # Dispatch rules this number is pointed at. Empty means an inbound call
    # would arrive with nothing to answer it, which is what "not routed" means.
    dispatch_rule_ids: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        """True when this project still holds the number and can route it.

        ``OFFLINE`` counts: an unrouted number is exactly the one that needs a
        dispatch rule assigned, so treating it as unusable would skip it.
        """
        return self.status in (
            NumberStatus.ACTIVE,
            NumberStatus.OFFLINE,
            NumberStatus.UNSPECIFIED,
        )

    @property
    def explicitly_bound(self) -> bool:
        """True when a dispatch rule is pinned to this number by ID."""
        return bool(self.dispatch_rule_ids)

    @property
    def routed(self) -> bool:
        """True when some dispatch rule will pick up calls to this number.

        A rule does not have to be pinned to the number: a rule with no
        ``inbound_numbers`` matches every number on the project. LiveKit
        reports that distinction through the status — ``OFFLINE`` means "no
        dispatch rule is associated with this number" — so status is the
        authority here, with an explicit binding as the other way to be sure.
        """
        return self.explicitly_bound or self.status is NumberStatus.ACTIVE

    @property
    def inbound_ready(self) -> bool:
        """True when a call to this number would reach an agent."""
        return self.held and self.routed

    @property
    def label(self) -> str:
        """Human location for the number, e.g. "San Francisco, CA"."""
        return ", ".join(part for part in (self.locality, self.region) if part)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "id": self.id,
            "countryCode": self.country_code,
            "areaCode": self.area_code,
            "locality": self.locality,
            "region": self.region,
            "label": self.label,
            "numberType": self.number_type.value,
            "status": self.status.value,
            "capabilities": list(self.capabilities),
            "dispatchRuleIds": list(self.dispatch_rule_ids),
            "held": self.held,
            "routed": self.routed,
            "explicitlyBound": self.explicitly_bound,
            "inboundReady": self.inbound_ready,
        }


@dataclass(frozen=True)
class NumberInventory:
    """Everything the dialer UI needs to populate its number pickers."""

    numbers: tuple[PhoneNumber, ...] = ()
    default_caller_id: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def held_numbers(self) -> tuple[PhoneNumber, ...]:
        """Numbers this project holds, routed or not."""
        return tuple(n for n in self.numbers if n.held)

    @property
    def routed_numbers(self) -> tuple[PhoneNumber, ...]:
        """Numbers that actually reach the agent."""
        return tuple(n for n in self.numbers if n.inbound_ready)

    def to_dict(self) -> dict[str, Any]:
        return {
            "numbers": [n.to_dict() for n in self.numbers],
            "defaultCallerId": self.default_caller_id,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DialInfo:
    """Instructions handed to the agent job through dispatch metadata.

    The agent decides what to do from this alone: an outbound call places a SIP
    participant, an inbound call greets whoever is already on the line, and a
    web session behaves exactly as it did before telephony existed.
    """

    direction: Direction = Direction.WEB
    # Destination for outbound; the caller's number for inbound (LiveKit fills
    # that in as a participant attribute, so it is informational here).
    to_number: str = ""
    # Caller ID presented on outbound, or the number that was dialled inbound.
    from_number: str = ""
    call_id: str = ""
    # Free-form context for the agent, e.g. the name of the person being called.
    context: dict[str, str] = field(default_factory=dict)

    @property
    def is_outbound(self) -> bool:
        return self.direction is Direction.OUTBOUND

    @property
    def is_phone_call(self) -> bool:
        return self.direction in (Direction.INBOUND, Direction.OUTBOUND)

    def to_json(self) -> str:
        payload: dict[str, Any] = {"direction": self.direction.value}
        if self.to_number:
            # "phone_number" mirrors the key used across LiveKit's telephony
            # examples, so an agent written against those docs still works.
            payload["phone_number"] = self.to_number
            payload["to_number"] = self.to_number
        if self.from_number:
            payload["from_number"] = self.from_number
        if self.call_id:
            payload["call_id"] = self.call_id
        if self.context:
            payload["context"] = self.context
        return json.dumps(payload)

    @staticmethod
    def from_json(raw: str | None) -> DialInfo:
        """Parse dispatch metadata. Anything unparseable means a web session.

        Never raises: a malformed metadata blob must not stop the agent from
        answering, it should only fall back to the default behavior.
        """
        if not raw or not raw.strip():
            return DialInfo()

        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return DialInfo()
        if not isinstance(data, dict):
            return DialInfo()

        to_number = str(data.get("to_number") or data.get("phone_number") or "")
        raw_direction = str(data.get("direction") or "").lower()
        try:
            direction = Direction(raw_direction)
        except ValueError:
            # No explicit direction: a number to dial implies outbound.
            direction = Direction.OUTBOUND if to_number else Direction.WEB

        context = data.get("context")
        return DialInfo(
            direction=direction,
            to_number=to_number,
            from_number=str(data.get("from_number") or ""),
            call_id=str(data.get("call_id") or ""),
            context={str(k): str(v) for k, v in context.items()}
            if isinstance(context, dict)
            else {},
        )

    def with_call_id(self, call_id: str) -> DialInfo:
        return replace(self, call_id=call_id)


@dataclass(frozen=True)
class CallRecord:
    """A call the control plane knows about, for listing and monitoring."""

    call_id: str
    room: str
    direction: Direction
    to_number: str = ""
    from_number: str = ""
    status: CallStatus = CallStatus.DIALING
    # Unix seconds. Set from the LiveKit room creation time where available so
    # the UI agrees with the server on how long a call has been running.
    started_at: float = 0.0
    participants: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "callId": self.call_id,
            "room": self.room,
            "direction": self.direction.value,
            "toNumber": self.to_number,
            "fromNumber": self.from_number,
            "status": self.status.value,
            "startedAt": self.started_at,
            "participants": self.participants,
            "error": self.error,
        }
