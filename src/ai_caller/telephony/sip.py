"""LiveKit SIP request building and error interpretation.

Kept apart from the control plane and from the agent so both sides construct
identical dial requests: the control plane can dial directly, and the agent
dials from inside the job, but a call placed either way gets the same timeouts,
attributes, and caller ID.

Outbound calls go through a **LiveKit outbound trunk**
(``LIVEKIT_SIP_OUTBOUND_TRUNK_ID``), which is where the carrier credentials
live. LiveKit Phone Numbers is inbound-only today, so a project with only a
rented LiveKit number has no outbound route and the dialer says so rather than
failing at dial time.

See https://docs.livekit.io/telephony/making-calls/outbound-trunk
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from google.protobuf.duration_pb2 import Duration
from livekit.protocol.sip import CreateSIPParticipantRequest

from ai_caller.config import TelephonySettings
from ai_caller.telephony.models import DialInfo, Direction

logger = logging.getLogger("ai_caller.telephony.sip")

# SIP status codes worth explaining to a human. Anything else is reported with
# its raw code; see https://docs.livekit.io/telephony/making-calls/outbound-calls
_SIP_REASONS = {
    403: (
        "forbidden",
        "The carrier refused the call. Check the caller ID and trunk credentials.",
    ),
    404: ("not_found", "The number could not be reached."),
    408: ("no_answer", "Nobody answered."),
    480: ("unavailable", "The number is unavailable."),
    486: ("busy", "The line was busy."),
    487: ("cancelled", "The call was cancelled before it was answered."),
    503: (
        "trunk_failure",
        "The SIP trunk is unavailable. Check the outbound trunk configuration.",
    ),
    603: ("declined", "The call was declined."),
}


class DialError(Exception):
    """An outbound call could not be placed or was not answered."""

    def __init__(self, code: str, message: str, *, sip_status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.sip_status = sip_status


def describe_sip_failure(exc: BaseException) -> DialError:
    """Turn a LiveKit ``SipCallError`` into something a UI can display.

    LiveKit surfaces the upstream carrier's SIP status, which is the only
    reliable way to tell "busy" from "no answer" from "your trunk is broken".
    """
    status = getattr(exc, "sip_status_code", None) or 0
    reason = getattr(exc, "sip_status", None) or ""

    code, explanation = _SIP_REASONS.get(
        status, ("dial_failed", "The call could not be completed.")
    )
    detail = f" ({status} {reason})" if status else ""
    return DialError(code, f"{explanation}{detail}", sip_status=status)


@dataclass(frozen=True)
class OutboundTarget:
    """A validated, ready-to-dial destination."""

    to_number: str
    from_number: str
    room: str
    call_id: str
    # Participant identity of the callee in the LiveKit room. Derived from the
    # call ID rather than the phone number so the number is not baked into logs
    # and traces any more than it already is.
    identity: str

    @property
    def display_name(self) -> str:
        return f"Caller {self.to_number}"


def participant_identity(call_id: str) -> str:
    return f"sip-{call_id}"


def build_participant_request(
    settings: TelephonySettings,
    target: OutboundTarget,
    *,
    wait_until_answered: bool = True,
) -> CreateSIPParticipantRequest:
    """Assemble the ``CreateSIPParticipant`` request for one outbound call.

    ``wait_until_answered`` makes LiveKit hold the request open until the callee
    picks up, which is what turns "no answer" and "busy" into an exception
    instead of a silent room with nobody in it.
    """
    policy = settings.policy

    request = CreateSIPParticipantRequest(
        sip_call_to=target.to_number,
        room_name=target.room,
        participant_identity=target.identity,
        participant_name=target.display_name,
        participant_metadata=DialInfo(
            direction=Direction.OUTBOUND,
            to_number=target.to_number,
            from_number=target.from_number,
            call_id=target.call_id,
        ).to_json(),
        # Mirrored onto the participant so the agent and the browser can both
        # read the call's identity without parsing metadata.
        participant_attributes={
            "call.direction": Direction.OUTBOUND.value,
            "call.id": target.call_id,
            "call.from": target.from_number,
        },
        wait_until_answered=wait_until_answered,
        # Noise suppression tuned for phone audio; harmless if unavailable.
        krisp_enabled=True,
    )

    if policy.ring_timeout_s > 0:
        request.ringing_timeout.CopyFrom(Duration(seconds=policy.ring_timeout_s))
    if policy.max_call_duration_s > 0:
        request.max_call_duration.CopyFrom(Duration(seconds=policy.max_call_duration_s))

    if not settings.outbound_trunk_id:
        raise DialError(
            "not_configured",
            "Outbound calling is not configured. LiveKit Phone Numbers is "
            "inbound-only today, so placing calls needs a LiveKit outbound "
            "trunk pointed at a carrier. Create one, then set "
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID.",
        )

    request.sip_trunk_id = settings.outbound_trunk_id
    # The trunk carries its own numbers list, but naming the caller ID
    # explicitly keeps the presented number under this module's control.
    if target.from_number:
        request.sip_number = target.from_number
    return request
