"""Telephony: inbound and outbound phone calls, entirely through LiveKit.

Numbers are rented from LiveKit Phone Numbers and routed to the agent with a
SIP dispatch rule, so there is no carrier account and no inbound trunk to
configure. Outbound calling needs a LiveKit outbound trunk pointed at a
carrier, because Phone Numbers is inbound-only today.

The module is layered so that each side imports only what it needs, and so a
missing dependency in one layer cannot break another:

    models.py       the shared vocabulary (DialInfo, CallRecord, PhoneNumber)
    policy.py       guardrails: number validation, allow/deny, rate limits
    numbers.py      LiveKit Phone Numbers API client and the dialer inventory
    sip.py          LiveKit SIP request building and error interpretation
    dial.py         the agent side: place or answer a call inside a job
    calls.py        the control plane: place, list, monitor, hang up
    provision.py    dispatch rule + number routing (setup script)
    api.py          HTTP front end for the control plane (needs fastapi)

``api.py`` is deliberately *not* imported here: the agent worker has no reason
to load fastapi, and importing this package must stay cheap for it.
"""

from ai_caller.telephony.dial import (
    call_context,
    caller_note,
    dial_out,
    link_target,
    open_conversation,
    resolve_direction,
)
from ai_caller.telephony.models import (
    CallRecord,
    CallStatus,
    DialInfo,
    Direction,
    NumberInventory,
    NumberStatus,
    NumberType,
    PhoneNumber,
)
from ai_caller.telephony.policy import CallPolicy, PolicyError, normalize_number
from ai_caller.telephony.sip import DialError

__all__ = [
    "CallPolicy",
    "CallRecord",
    "CallStatus",
    "DialError",
    "DialInfo",
    "Direction",
    "NumberInventory",
    "NumberStatus",
    "NumberType",
    "PhoneNumber",
    "PolicyError",
    "call_context",
    "caller_note",
    "dial_out",
    "link_target",
    "normalize_number",
    "open_conversation",
    "resolve_direction",
]
