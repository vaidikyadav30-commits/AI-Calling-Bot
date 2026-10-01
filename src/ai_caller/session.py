"""Voice pipeline assembly and the LiveKit job entrypoint.

One entrypoint serves three kinds of session — a browser visitor, an inbound
phone call, and an outbound phone call — because they differ only in how the
other party arrives. The telephony module answers the questions that differ
(``ai_caller.telephony.dial``); everything below this line is shared.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time

from livekit.agents import (
    NOT_GIVEN,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    room_io,
)

from ai_caller.agents import Assistant
from ai_caller.config import Settings
from ai_caller.integrations import build_integrations, close_all
from ai_caller.prompts import PHONE_INSTRUCTIONS, opening_instructions
from ai_caller.providers import build_stt, build_tts
from ai_caller.telephony import (
    DialError,
    call_context,
    caller_note,
    dial_out,
    link_target,
    open_conversation,
    resolve_direction,
)
from ai_caller.telephony.dial import call_tools

logger = logging.getLogger("ai_caller.session")


def build_session(settings: Settings) -> AgentSession:
    """Cartesia STT -> DeepSeek -> ElevenLabs TTS, tuned for low turn latency."""
    return AgentSession(
        stt=build_stt(settings.stt),
        tts=build_tts(settings.tts),
        turn_handling=TurnHandlingOptions(
            # Cartesia's ink-2 is a turn-detecting model: it signals end-of-turn
            # itself, so the session can rely on STT endpointing directly.
            # See https://docs.livekit.io/agents/build/turns
            turn_detection="stt",
            # Adaptive interruptions tell a real interruption from a backchannel
            # like "mhm" or "right", so the agent talks through the latter.
            interruption={"mode": "adaptive"},
            # This delay is added on top of Cartesia's own endpoint signal, so
            # the default 0.5s is pure padding here. "dynamic" then adapts
            # within the range to how this speaker actually pauses.
            endpointing={"mode": "dynamic", "min_delay": 0.2, "max_delay": 2.0},
            # Start the LLM on the preflight transcript, before the turn is
            # confirmed, and run TTS preemptively too. preemptive_tts trades
            # some wasted synthesis on cancelled turns for a shorter gap before
            # the agent starts speaking.
            # See https://docs.livekit.io/agents/build/audio/#preemptive-generation
            preemptive_generation={"enabled": True, "preemptive_tts": True},
        ),
    )


def prewarm(proc: JobProcess) -> None:
    """Pay the expensive, shared setup before a call arrives.

    LiveKit keeps idle job processes ready and runs this in them, so whatever
    happens here is off the caller's critical path. Building the integrations
    once resolves DNS, completes the TLS handshake, and initializes the
    embedding client, which is the part of ``build_integrations`` that actually
    costs time on a cold process.

    The warmed objects are deliberately thrown away rather than shared: a
    session still builds its own, so a knowledge base that is down now can
    recover on the next call. This only warms the caches underneath them.
    """
    started = time.perf_counter()
    try:
        settings = Settings.from_env()
        build_integrations(settings)
    except Exception as exc:  # never let a cold cache stop the worker starting
        logger.warning("prewarm skipped: %s", exc)
        return

    proc.userdata["prewarmed"] = True
    logger.info("prewarm finished in %.2fs", time.perf_counter() - started)


async def run_session(ctx: JobContext, settings: Settings) -> None:
    """Serve one job: a browser visitor, an inbound call, or an outbound one.

    Deliberately module-level rather than nested inside ``create_server``. On
    Linux the worker runs each job in a separate process started with
    ``forkserver``, and the entrypoint is pickled to get it there — which a
    closure cannot survive (``Can't pickle local object``). Windows hides this
    entirely by running jobs in threads, so a nested entrypoint works in local
    development and fails on every call once deployed.
    """
    # Time every phase. On a phone call the caller hears silence until the
    # session starts, so when someone reports "it took a minute to answer",
    # these numbers say which phase to blame instead of guessing.
    started = time.perf_counter()

    def since() -> float:
        return time.perf_counter() - started

    # What was this job dispatched to do: answer a call, place one, or serve
    # a browser? Everything that follows keys off this one value.
    dial = call_context(ctx)

    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
        "direction": dial.direction.value,
        "call_id": dial.call_id,
    }

    if dial.is_phone_call:
        # Connect first and build everything else afterwards: an inbound
        # caller is already on the line, and every millisecond before this
        # is silence they are paying for.
        await ctx.connect()
        logger.info("connected in %.2fs", since())

    # A phone call whose metadata never arrived still has a SIP participant
    # in the room. Trusting that over the metadata is what stops a missing
    # dispatch-rule field from silently turning an inbound call into a
    # session that waits for the caller to speak first.
    dial = resolve_direction(ctx, dial)
    ctx.log_context_fields["direction"] = dial.direction.value

    # Built per session so a knowledge base that is down at start time can
    # recover on the next call without restarting the worker. Off the event
    # loop because the Qdrant client connects synchronously, which would
    # otherwise stall the room connection we just made.
    integrations = await asyncio.to_thread(build_integrations, settings)
    ctx.add_shutdown_callback(lambda: close_all(integrations))
    logger.info("integrations ready in %.2fs", since())

    caller = None
    if dial.is_outbound:
        try:
            caller = await dial_out(ctx, dial, settings.telephony)
        except DialError:
            # dial_out has already logged the reason and shut the job down.
            return

    session = build_session(settings)

    await session.start(
        agent=Assistant(
            settings,
            integrations,
            extra_instructions=PHONE_INSTRUCTIONS if dial.is_phone_call else "",
            extra_tools=call_tools(dial),
        ),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(),
            # On outbound, link to the callee explicitly so the session
            # cannot attach to a supervisor who joined to listen in.
            participant_identity=link_target(dial) or NOT_GIVEN,
        ),
    )
    logger.info("session started in %.2fs", since())

    if not dial.is_phone_call:
        # Join the room and connect to the user. Done after start() so
        # RoomIO is ready for the browser's pre-connect audio buffer.
        await ctx.connect()

    # Inbound: greet now. Outbound: only if the callee stays silent.
    await open_conversation(
        session,
        dial,
        opening_instructions(dial.is_outbound, caller_note(dial, caller)),
    )


def create_server(settings: Settings | None = None) -> AgentServer:
    """Build the AgentServer and register the session handler."""
    settings = settings or Settings.from_env()

    # Size the warm pool explicitly. LiveKit's default scales with the CPU
    # count it can see, and a container usually sees the host's — so a worker
    # given 512 MB cheerfully reserves several gigabytes of idle job
    # processes and the kernel kills them mid-call. See WorkerSettings.
    server = AgentServer(
        setup_fnc=prewarm,
        num_idle_processes=settings.worker.idle_processes,
        job_memory_limit_mb=settings.worker.job_memory_limit_mb,
        job_memory_warn_mb=settings.worker.job_memory_warn_mb,
    )

    # partial() rather than a closure: both the function and the bound
    # Settings pickle, so the whole entrypoint survives the trip to a job
    # process. Binding the settings here also keeps the worker and its job
    # processes on exactly one configuration instead of each re-reading the
    # environment and potentially disagreeing.
    server.rtc_session(
        functools.partial(run_session, settings=settings),
        agent_name=settings.agent_name,
    )
    return server
