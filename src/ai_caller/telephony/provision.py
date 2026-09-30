"""Point LiveKit phone numbers at this agent.

Driven by ``scripts/setup_telephony.py``. With LiveKit Phone Numbers there is
no carrier account and no SIP trunk for inbound — the whole wiring is two
objects:

1. a **dispatch rule** that puts each caller in a room and dispatches this
   agent into it, and
2. an **assignment** of each phone number to that rule.

Without step 2 a call connects to a room with nobody in it, which the caller
experiences as silence. That is the failure this module exists to prevent.

Every step is idempotent: the rule is found by name before being created, and
a number already pointing at it is left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from livekit import api

from ai_caller.config import Settings
from ai_caller.telephony.models import DialInfo, Direction, PhoneNumber
from ai_caller.telephony.numbers import PhoneNumberClient, PhoneNumberError

logger = logging.getLogger("ai_caller.telephony.provision")

# Name used to find our own dispatch rule again on a re-run. Changing it
# orphans the existing rule rather than updating it.
DISPATCH_RULE_NAME = "AI Caller inbound"


@dataclass
class Step:
    """One provisioning action and what came of it."""

    name: str
    status: str  # "created" | "exists" | "updated" | "skipped" | "failed"
    detail: str = ""

    def __str__(self) -> str:
        mark = {
            "created": "+",
            "updated": "~",
            "exists": "=",
            "skipped": ".",
            "failed": "x",
        }.get(self.status, "?")
        suffix = f"  {self.detail}" if self.detail else ""
        return f"  [{mark}] {self.name}{suffix}"


@dataclass
class ProvisionReport:
    steps: list[Step] = field(default_factory=list)
    # Values the operator should put in .env.local.
    env: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> Step:
        step = Step(name, status, detail)
        self.steps.append(step)
        logger.info("%s", step)
        return step

    @property
    def failed(self) -> bool:
        return any(step.status == "failed" for step in self.steps)


async def ensure_dispatch_rule(
    lkapi: api.LiveKitAPI,
    report: ProvisionReport,
    *,
    room_prefix: str,
    agent_name: str,
) -> str:
    """Find or create the rule that hands inbound callers to the agent.

    Uses a *callee* rule: the room is named after the number that was dialled
    (ours) plus a random suffix, rather than after the caller's number. An
    individual rule would put the caller's phone number — personal data — into
    every room name, log line, and trace.
    """
    existing = await lkapi.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())

    for rule in existing.items:
        if rule.name != DISPATCH_RULE_NAME:
            continue

        dispatched = [agent.agent_name for agent in rule.room_config.agents]
        if agent_name in dispatched:
            report.add("Dispatch rule", "exists", rule.sip_dispatch_rule_id)
            return rule.sip_dispatch_rule_id

        # Recreate rather than patch: the rule is cheap, and a rule pointing at
        # the wrong agent is the failure where calls connect to silence.
        await lkapi.sip.delete_dispatch_rule(
            api.DeleteSIPDispatchRuleRequest(
                sip_dispatch_rule_id=rule.sip_dispatch_rule_id
            )
        )
        report.add(
            "Dispatch rule",
            "updated",
            f"replacing {rule.sip_dispatch_rule_id} (agent was "
            f"{', '.join(dispatched) or 'unset'})",
        )

    created = await lkapi.sip.create_dispatch_rule(
        api.CreateSIPDispatchRuleRequest(
            name=DISPATCH_RULE_NAME,
            rule=api.SIPDispatchRule(
                dispatch_rule_callee=api.SIPDispatchRuleCallee(
                    room_prefix=f"{room_prefix}-", randomize=True
                )
            ),
            room_config=api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=agent_name,
                        metadata=DialInfo(direction=Direction.INBOUND).to_json(),
                    )
                ]
            ),
        )
    )
    report.add("Dispatch rule", "created", created.sip_dispatch_rule_id)
    return created.sip_dispatch_rule_id


async def assign_numbers(
    client: PhoneNumberClient,
    report: ProvisionReport,
    *,
    numbers: list[PhoneNumber],
    dispatch_rule_id: str,
) -> list[PhoneNumber]:
    """Route each number to the rule. Returns those the agent will answer.

    Two things make a number reachable, and only the first is required:

    * the rule **matches** the number — a rule with no ``inbound_numbers``
      matches every number on the project, which is how the rule created above
      works; and
    * the rule is optionally **pinned** to the number by ID.

    Pinning is attempted because it is more explicit, but a failure is not
    fatal: LiveKit's ``UpdatePhoneNumber`` currently rejects the call on some
    projects (reproducible with ``lk number update``), and the match alone
    already routes the call. A number LiveKit reports as ``ACTIVE`` is, by its
    own definition, associated with a dispatch rule.
    """
    routed: list[PhoneNumber] = []

    for number in numbers:
        if dispatch_rule_id in number.dispatch_rule_ids:
            report.add(f"Number {number.number}", "exists", "pinned to this rule")
            routed.append(number)
            continue

        if not number.held:
            # Released or still provisioning: nothing to route.
            report.add(
                f"Number {number.number}",
                "skipped",
                f"status is {number.status.value}",
            )
            continue

        try:
            updated = await client.assign_dispatch_rule(
                number_id=number.id,
                number=number.number,
                dispatch_rule_id=dispatch_rule_id,
            )
            report.add(f"Number {number.number}", "updated", "pinned to the rule")
            routed.append(updated if updated.number else number)
            continue
        except PhoneNumberError as exc:
            logger.debug("could not pin %s: %s", number.number, exc)

        # Pinning failed. The rule still matches the number, so report what is
        # actually true rather than failing a setup that works.
        report.add(
            f"Number {number.number}",
            "exists" if number.routed else "skipped",
            "matched by the rule (pinning unavailable on this project)",
        )
        if number.routed:
            routed.append(number)
        else:
            report.notes.append(
                f"{number.number} is not reporting as routed yet. Re-run this "
                "in a moment; LiveKit updates the status shortly after the "
                "dispatch rule is created."
            )

    return routed


async def provision(
    settings: Settings,
    *,
    only_numbers: tuple[str, ...] = (),
    lkapi: api.LiveKitAPI | None = None,
    client: PhoneNumberClient | None = None,
) -> ProvisionReport:
    """Create the dispatch rule and route every number to it."""
    from ai_caller.telephony.numbers import fetch_inventory

    report = ProvisionReport()
    telephony = settings.telephony
    owns_lkapi = lkapi is None
    owns_client = client is None

    numbers_client = client or PhoneNumberClient(telephony.livekit)
    lk = lkapi or api.LiveKitAPI(
        url=telephony.livekit.url,
        api_key=telephony.livekit.api_key,
        api_secret=telephony.livekit.api_secret,
    )

    try:
        inventory = await fetch_inventory(
            numbers_client, default_caller_id=telephony.caller_id
        )
        selected = _select(list(inventory.numbers), only_numbers)

        rule_id = await ensure_dispatch_rule(
            lk,
            report,
            room_prefix=telephony.room_prefix,
            agent_name=settings.agent_name,
        )
        report.env["LIVEKIT_SIP_DISPATCH_RULE_ID"] = rule_id

        if not selected:
            report.add(
                "Phone numbers",
                "skipped",
                "no numbers to route",
            )
            report.notes.append(
                "This project holds no phone numbers yet. Every LiveKit plan "
                "includes one free US local number — rent it with "
                "`uv run python scripts/setup_telephony.py --rent`, then "
                "re-run this to route it."
            )
            return report

        assigned = await assign_numbers(
            numbers_client,
            report,
            numbers=selected,
            dispatch_rule_id=rule_id,
        )

        if assigned and not telephony.caller_id:
            report.env["TELEPHONY_CALLER_ID"] = assigned[0].number

        if not telephony.outbound_trunk_id:
            report.notes.append(
                "Inbound is configured. Outbound calling is not: LiveKit Phone "
                "Numbers is inbound-only today, so placing calls needs a "
                "LiveKit outbound trunk pointed at a carrier "
                "(LIVEKIT_SIP_OUTBOUND_TRUNK_ID)."
            )

        return report
    finally:
        if owns_client:
            await numbers_client.aclose()
        if owns_lkapi:
            await lk.aclose()


def _select(available: list[PhoneNumber], only: tuple[str, ...]) -> list[PhoneNumber]:
    """Narrow the inventory to the numbers this run should route."""
    if not only:
        return available

    wanted = {number.strip() for number in only if number.strip()}
    return [number for number in available if number.number in wanted]
