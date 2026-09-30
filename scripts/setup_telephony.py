"""Give the agent a phone number, using LiveKit Phone Numbers.

    uv run python scripts/setup_telephony.py --list          # numbers you hold
    uv run python scripts/setup_telephony.py --search 415    # what's available
    uv run python scripts/setup_telephony.py --rent          # rent one (costs money)
    uv run python scripts/setup_telephony.py                 # route numbers to the agent
    uv run python scripts/setup_telephony.py --write-env     # ...and save the rule ID

No carrier account and no SIP trunk: LiveKit rents the number and routes the
call. Routing is idempotent, so re-run it after renting another number or
changing AGENT_NAME.

Every LiveKit plan includes one free US local number. Renting beyond that, or
renting toll-free, is billed — so ``--rent`` always asks first.

What gets built, and why, is documented in
``src/ai_caller/telephony/provision.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow running as a plain script, without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_caller.config import ENV_FILE, Settings, check_env
from ai_caller.telephony.models import PhoneNumber
from ai_caller.telephony.numbers import (
    PhoneNumberClient,
    PhoneNumberError,
    fetch_inventory,
)
from ai_caller.telephony.provision import ProvisionReport, provision

REQUIRED = {
    "LIVEKIT_URL": "LiveKit Cloud project URL (wss://...)",
    "LIVEKIT_API_KEY": "LiveKit Cloud API key",
    "LIVEKIT_API_SECRET": "LiveKit Cloud API secret",
}


def show(numbers: list[PhoneNumber], *, available: bool = False) -> None:
    """Print a number table. ``available`` ones have no routing to report."""
    if not numbers:
        print("  (none)")
        return

    for number in numbers:
        kind = number.number_type.value.replace("_", "-")
        where = number.label or number.country_code or "-"
        if available:
            print(f"  {number.number:<16} {kind:<10} {where}")
            continue

        routing = (
            f"routed ({len(number.dispatch_rule_ids)} rule)"
            if number.routed
            else "not routed"
        )
        print(
            f"  {number.number:<16} {kind:<10} {number.status.value:<9} "
            f"{routing:<18} {where}"
        )


async def list_numbers(settings: Settings) -> int:
    async with PhoneNumberClient(settings.telephony.livekit) as client:
        inventory = await fetch_inventory(
            client, default_caller_id=settings.telephony.caller_id
        )

    print(f"default from  : {inventory.default_caller_id or '(none)'}\n")
    print("Numbers held by this LiveKit project:")
    show(list(inventory.numbers))

    for warning in inventory.warnings:
        print(f"\n  ! {warning}")
    return 0


async def search_numbers(settings: Settings, area_code: str, limit: int) -> int:
    async with PhoneNumberClient(settings.telephony.livekit) as client:
        found = await client.search(area_code=area_code, limit=limit)

    label = f" in area code {area_code}" if area_code else ""
    print(f"Available US numbers{label}:")
    show(found, available=True)
    if found:
        print(
            "\nRent one with:\n"
            f"  uv run python scripts/setup_telephony.py --rent {found[0].number}"
        )
    return 0


async def rent_number(
    settings: Settings, number: str, area_code: str, *, assume_yes: bool
) -> int:
    """Rent a number, then route it to the agent in the same run."""
    telephony = settings.telephony

    async with PhoneNumberClient(telephony.livekit) as client:
        target = number
        if not target:
            found = await client.search(area_code=area_code, limit=1)
            if not found:
                print("No numbers are available to rent with those filters.")
                return 1
            target = found[0].number
            print(f"Selected {target} ({found[0].label or 'US'})")

        print(
            "\nRenting a phone number is billable. Every LiveKit plan includes "
            "one free\nUS local number; beyond that, and for toll-free, this "
            "costs money. A released\nnumber is still billed for the rest of "
            "the month."
        )
        if not assume_yes:
            answer = input(f"\nRent {target}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Cancelled. Nothing was rented.")
                return 0

        purchased = await client.purchase([target])
        for entry in purchased:
            print(f"\nRented {entry.number} (status: {entry.status.value})")

    print("\nNow routing it to the agent...")
    report = await provision(settings)
    render(report)
    return 1 if report.failed else 0


def render(report: ProvisionReport) -> None:
    print("\nSteps:")
    for step in report.steps:
        print(step)

    if report.notes:
        print("\nNotes:")
        for note in report.notes:
            print(f"  ! {note}")

    if report.env:
        print("\nAdd these to .env.local (or re-run with --write-env):\n")
        for key, value in report.env.items():
            print(f"  {key}={value}")


def write_env(env: dict[str, str], path: Path) -> list[str]:
    """Update the given keys in ``.env.local``, leaving everything else alone.

    Existing lines are rewritten in place rather than appended to, so re-running
    does not leave the file with two values for the same key — the last of which
    silently wins.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(env)
    changed: list[str] = []

    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            value = remaining.pop(key)
            if line != f"{key}={value}":
                lines[index] = f"{key}={value}"
                changed.append(key)

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Telephony - written by scripts/setup_telephony.py")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")
            changed.append(key)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


async def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()

    if args.list:
        return await list_numbers(settings)
    if args.search is not None:
        return await search_numbers(settings, args.search, args.limit)
    if args.rent is not None:
        return await rent_number(
            settings, args.rent, args.area_code, assume_yes=args.yes
        )

    report = await provision(settings, only_numbers=tuple(args.number or ()))
    render(report)

    if report.failed:
        print("\nSetup did not complete. Fix the failures above and re-run.")
        return 1

    if args.write_env and report.env:
        changed = write_env(report.env, ENV_FILE)
        print(f"\nUpdated {ENV_FILE}")
        if changed:
            print(f"  keys: {', '.join(changed)}")
        print("  Restart the agent and the control plane to pick them up.")
    elif report.env:
        print("\nRe-run with --write-env to save these automatically.")

    print("\nDone. Test it with:")
    print("  uv run python src/agent.py dev")
    print("  uv run python -m ai_caller.telephony.api")
    print("  ...then call your number.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the numbers this project holds, then exit",
    )
    parser.add_argument(
        "--search",
        nargs="?",
        const="",
        metavar="AREA_CODE",
        help="search available US numbers to rent, optionally by area code",
    )
    parser.add_argument(
        "--rent",
        nargs="?",
        const="",
        metavar="+E164",
        help="rent a number (asks first). With no number, rents the first match",
    )
    parser.add_argument(
        "--area-code",
        default="",
        metavar="CODE",
        help="area code to prefer when renting without an explicit number",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="how many search results to show"
    )
    parser.add_argument(
        "--number",
        action="append",
        metavar="+E164",
        help="only route this number (repeatable; default is every number)",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="write the resulting dispatch rule ID into .env.local",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="skip the rental confirmation"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    check_env(REQUIRED)
    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PhoneNumberError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        if exc.is_auth_error:
            print(
                "       Check LIVEKIT_API_KEY and LIVEKIT_API_SECRET in .env.local.",
                file=sys.stderr,
            )
        elif exc.is_unsupported:
            print(
                "       Phone Numbers may not be enabled for this project or plan.\n"
                "       See https://docs.livekit.io/telephony/start/phone-numbers",
                file=sys.stderr,
            )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
