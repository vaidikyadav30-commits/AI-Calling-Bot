"""The job entrypoint must survive being sent to a fresh process.

These are cheap structural checks, not evals: they build the server and
inspect what it registered, without connecting to LiveKit.
"""

from __future__ import annotations

import pickle

from ai_caller.config import LiveKitSettings, Settings, TelephonySettings
from ai_caller.session import create_server, run_session

SETTINGS = Settings(
    agent_name="test-agent",
    telephony=TelephonySettings(
        livekit=LiveKitSettings(
            url="wss://example.livekit.cloud",
            api_key="APItestkey",
            api_secret="a-secret-long-enough-to-sign-with",
        )
    ),
)


def registered(server) -> object:
    """The entrypoint the AgentServer will hand to each job process.

    Private attribute on purpose: it is the exact object that gets pickled,
    and checking anything else would not test the thing that broke.
    """
    return server._entrypoint_fnc


def test_entrypoint_is_picklable():
    """The bug this file exists for.

    On Linux the worker runs jobs in separate processes started with
    `forkserver`, which pickles the entrypoint to send it across. A function
    defined inside create_server() is `create_server.<locals>.entrypoint` and
    cannot be pickled, so every job dies with PicklingError before the agent
    joins the room and the caller hears silence.

    Windows hides this completely: livekit.agents falls back to a THREAD job
    executor there, which never pickles anything. So this regresses in local
    development without a single failing symptom, and only surfaces once
    deployed.
    """
    entrypoint = registered(create_server(SETTINGS))

    restored = pickle.loads(pickle.dumps(entrypoint))

    assert restored.func is run_session
    assert restored.keywords["settings"] == SETTINGS


def test_entrypoint_carries_the_settings_it_was_built_with():
    """A job process cannot inherit the server's objects, only what is sent.

    Binding the settings to the entrypoint is what keeps the worker and its
    job processes agreeing on configuration; re-reading the environment in the
    child would quietly diverge if the two ever differed.
    """
    entrypoint = registered(create_server(SETTINGS))

    assert entrypoint.keywords["settings"].agent_name == "test-agent"


def test_server_registers_under_the_configured_agent_name():
    """The dispatch name must match what the SIP rule and browser ask for.

    A mismatch is not an error anywhere: the room is created, nobody is
    dispatched, and the call connects to silence.
    """
    assert create_server(SETTINGS)._agent_name == "test-agent"


def test_idle_pool_is_set_explicitly_not_inferred_from_cpus():
    """LiveKit sizes the warm pool from the CPU count it can see.

    A container usually sees the host's cores, so a worker with 512 MB asks
    for 16 job processes at ~220 MB each and the kernel SIGKILLs them
    mid-call — visible only as `exit code -9`, with the caller hearing
    silence. The pool must therefore come from settings, never from the host.
    """
    from dataclasses import replace

    from ai_caller.config import WorkerSettings

    server = create_server(replace(SETTINGS, worker=WorkerSettings(idle_processes=2)))

    assert server._num_idle_processes == 2


def test_idle_pool_default_fits_a_small_container():
    """One warm process answers the next call without a ~2s import stall.

    Two would not fit beside the worker in 512 MB, and zero would make every
    caller wait for a cold start.
    """
    from ai_caller.config import WorkerSettings

    assert WorkerSettings().idle_processes == 1


def test_prewarm_is_module_level():
    """setup_fnc crosses the same process boundary as the entrypoint."""
    from ai_caller.session import prewarm

    assert pickle.loads(pickle.dumps(prewarm)) is prewarm
