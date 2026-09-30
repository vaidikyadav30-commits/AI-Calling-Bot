"""The agent side of a phone call.

``session.py`` asks this module three questions and nothing more:

1. What kind of call is this? — ``call_context``
2. If it is outbound, please dial it. — ``dial_out``
3. Who should the session listen to, and who speaks first? — ``link_target``
   and ``open_conversation``

Keeping it here rather than in ``session.py`` means the pipeline file does not
grow a telephony branch for every feature, and it means the dial path can be
tested without a LiveKit worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from livekit import api, rtc
from livekit.agents import AgentSession, JobContext, llm
from livekit.agents.beta.tools import EndCallTool

from ai_caller.config import TelephonySettings
from ai_caller.telephony.models import DialInfo, Direction
from ai_caller.telephony.sip import (
    DialError,
    OutboundTarget,
    build_participant_request,
    describe_sip_failure,
    participant_identity,
)

logger = logging.getLogger("ai_caller.telephony.dial")

# How long to wait for an outbound callee to say something before the agent
# opens the conversation itself. People usually answer with "hello?", and
# replying to that sounds far better than talking over it — but a silent pickup
# (or a hands-free car) must not leave both sides waiting.
OUTBOUND_OPENING_DELAY_S = 3.0

# Ceiling on waiting for the SIP participant to appear in the room after the
# carrier reports the call answered. Normally milliseconds.
PARTICIPANT_JOIN_TIMEOUT_S = 20.0


def call_context(ctx: JobContext) -> DialInfo:
    """What this job was dispatched to do.

    Falls back to a web session for anything unrecognized, so the agent keeps
    working for browser callers no matter what metadata arrives.
    """
    metadata = ""
    # A job without metadata is normal (browser sessions); a job context that
    # cannot produce it at all is not, but must still not break the call.
    with contextlib.suppress(Exception):
        metadata = ctx.job.metadata or ""

    dial = DialInfo.from_json(metadata)
    if dial.is_phone_call:
        logger.info(
            "%s call %s (to=%s from=%s)",
            dial.direction.value,
            dial.call_id or "-",
            dial.to_number or "-",
            dial.from_number or "-",
        )
    return dial


def resolve_direction(ctx: JobContext, dial: DialInfo) -> DialInfo:
    """Correct the direction using who is actually in the room.

    Job metadata is the primary signal, but it is not the only truth available,
    and it is the fragile one: it depends on the dispatch rule carrying the
    right ``room_config`` metadata all the way through. A SIP participant in
    the room is proof that this is a phone call no matter what the metadata
    said, and a call mistaken for a web session is the worst failure mode here
    — the agent stays silent, waiting for a caller who is waiting for it.

    Call this after ``ctx.connect()``; before that the room has no participants.
    """
    if dial.is_phone_call:
        return dial

    participant = _sip_participant(ctx)
    if participant is None:
        return dial

    attributes = dict(participant.attributes)
    logger.info(
        "job metadata said %r but a SIP participant is present; "
        "handling this as an inbound call",
        dial.direction.value,
    )
    return DialInfo(
        direction=Direction.INBOUND,
        to_number=attributes.get("sip.trunkPhoneNumber", ""),
        from_number=attributes.get("sip.phoneNumber", ""),
        call_id=attributes.get("call.id", "") or dial.call_id,
        context=dial.context,
    )


def _sip_participant(ctx: JobContext) -> rtc.RemoteParticipant | None:
    """The phone leg in the room, if one has joined."""
    try:
        participants = list(ctx.room.remote_participants.values())
    except Exception:  # pragma: no cover - only in odd worker states
        return None

    for participant in participants:
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return participant
        if any(key.startswith("sip.") for key in participant.attributes):
            return participant
    return None


async def dial_out(
    ctx: JobContext, dial: DialInfo, settings: TelephonySettings
) -> rtc.RemoteParticipant:
    """Place the outbound call and wait for the callee to answer.

    Blocks until the carrier reports the call answered and the SIP participant
    has joined the room, so the caller of this function can start the session
    knowing there is somebody to talk to.

    Raises ``DialError`` and shuts the job down on any failure, since a job that
    keeps running after a failed dial holds a worker slot for nothing.
    """
    target = OutboundTarget(
        to_number=dial.to_number,
        from_number=dial.from_number,
        room=ctx.room.name,
        call_id=dial.call_id,
        identity=participant_identity(dial.call_id or "out"),
    )
    request = build_participant_request(settings, target)

    logger.info("dialling %s from %s", target.to_number, target.from_number or "-")
    try:
        await ctx.api.sip.create_sip_participant(request)
    except api.SipCallError as exc:
        failure = describe_sip_failure(exc)
        logger.warning(
            "call %s failed: %s (sip %s)",
            dial.call_id or "-",
            failure.message,
            failure.sip_status or "-",
        )
        ctx.shutdown(reason=f"dial failed: {failure.code}")
        raise failure from exc
    except Exception as exc:
        logger.exception("could not place the outbound call")
        ctx.shutdown(reason="dial failed")
        raise DialError("dial_failed", str(exc)) from exc

    logger.info("call %s answered", dial.call_id or "-")

    try:
        return await asyncio.wait_for(
            ctx.wait_for_participant(identity=target.identity),
            timeout=PARTICIPANT_JOIN_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        # The carrier said the call was answered but the media leg never
        # arrived: nothing to talk to, so give the worker back.
        logger.warning("callee never joined room %s", ctx.room.name)
        ctx.shutdown(reason="callee never joined")
        raise DialError(
            "no_media", "The call was answered but no audio path was established."
        ) from exc


def call_tools(dial: DialInfo) -> list[llm.Toolset]:
    """Tools that only make sense on a phone call.

    ``EndCallTool`` deletes the room, which is what actually drops the SIP leg.
    Without it a finished call leaves the caller listening to silence until they
    hang up themselves — and on a browser session there is no call to end, so
    the tool is not offered there at all.
    """
    if not dial.is_phone_call:
        return []

    return [
        EndCallTool(
            delete_room=True,
            # Stops the model from ending the call inside its own greeting,
            # which it will otherwise occasionally do on a silent pickup.
            ignore_on_enter=True,
        )
    ]


def link_target(dial: DialInfo) -> str | None:
    """Which participant identity the session should attach to.

    Outbound calls have a known identity, so naming it removes any chance of
    the session linking to a monitor that joined first. Inbound and web
    sessions link to whoever is there, which is the default.
    """
    if dial.is_outbound and dial.call_id:
        return participant_identity(dial.call_id)
    return None


def caller_note(dial: DialInfo, participant: rtc.RemoteParticipant | None) -> str:
    """One line of call context for the agent's opening instructions.

    Deliberately terse: it is prepended to a greeting prompt, and anything
    longer tempts the model into reading the phone number back to the caller.
    """
    if not dial.is_phone_call:
        return ""

    attributes = dict(participant.attributes) if participant is not None else {}
    if dial.direction is Direction.INBOUND:
        dialled = attributes.get("sip.trunkPhoneNumber") or dial.to_number
        return (
            f"This is an inbound phone call on {dialled}."
            if dialled
            else "This is an inbound phone call."
        )

    who = dial.context.get("name", "")
    target = f" to {who}" if who else ""
    return f"This is an outbound phone call{target} that you placed."


async def open_conversation(
    session: AgentSession,
    dial: DialInfo,
    instructions: str,
    *,
    delay_s: float = OUTBOUND_OPENING_DELAY_S,
) -> None:
    """Decide who speaks first, and make it happen.

    * Inbound: the agent greets immediately — the caller dialled us and is
      waiting to hear something.
    * Outbound: the callee almost always speaks first ("hello?"), so the agent
      holds back and only opens the conversation if they stay silent.
    * Web: unchanged from before telephony existed; the user starts.
    """
    if dial.direction is Direction.INBOUND:
        session.generate_reply(instructions=instructions)
        return

    if not dial.is_outbound:
        return

    spoke = asyncio.Event()

    def _on_user_input(_event: object) -> None:
        spoke.set()

    session.on("user_input_transcribed", _on_user_input)
    try:
        await asyncio.wait_for(spoke.wait(), timeout=delay_s)
        logger.debug("callee spoke first; replying rather than greeting")
    except asyncio.TimeoutError:
        logger.debug("callee silent after %.1fs; opening the conversation", delay_s)
        session.generate_reply(instructions=instructions)
    finally:
        session.off("user_input_transcribed", _on_user_input)
