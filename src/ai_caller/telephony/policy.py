"""Guardrails on who may be called, from what, and how often.

Every one of these checks runs before a call is placed, in code rather than in
the prompt. A dialer that can be talked into ringing an emergency line is a
safety problem, not a prompting problem, and the LLM never sees these rules.

The checks are ordered cheapest-and-most-serious first: a malformed number is
rejected before an emergency number, which is rejected before any quota.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterable

from ai_caller.config import CallPolicySettings

logger = logging.getLogger("ai_caller.telephony.policy")

# E.164 allows at most 15 digits. The lower bound is deliberately loose: short
# national formats are rejected by the "must be international" rule instead, and
# a few countries do issue 8-digit numbers including their calling code.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# Characters people paste in from address books and web pages, all of
# which are noise in a phone number. The non-ASCII ones are deliberate: a
# number copied from a styled web page routinely arrives with a non-breaking
# space or an en dash instead of a hyphen, and stripping them here is what
# makes that paste work. Ruff's ambiguous-character rule is silenced for
# exactly that reason.
_PUNCTUATION = re.compile(r"[\s\-(). ‑–—]")  # noqa: RUF001

# Emergency and service short codes, worldwide. These are shorter than any valid
# E.164 number, so the format check already stops them; they are listed
# explicitly so the rejection is unambiguous in logs and in the UI, and so that
# a future change to the format rule cannot quietly let one through.
EMERGENCY_NUMBERS = frozenset(
    {
        "000",
        "100",
        "101",
        "102",
        "103",
        "106",
        "108",
        "110",
        "111",
        "112",
        "113",
        "115",
        "117",
        "118",
        "119",
        "120",
        "122",
        "123",
        "128",
        "133",
        "144",
        "155",
        "190",
        "191",
        "192",
        "193",
        "911",
        "912",
        "913",
        "915",
        "919",
        "988",
        "991",
        "992",
        "993",
        "995",
        "996",
        "997",
        "998",
        "999",
    }
)

# US/Canada N11 service codes and premium ranges, which are billed to us and
# are never a legitimate destination for an outbound AI call.
_BLOCKED_PREFIXES = ("+1900", "+1976", "+1411", "+1611", "+1711", "+1811")


class PolicyError(Exception):
    """A call was refused. ``code`` is stable and safe to branch on in a UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_number(raw: str, *, field: str = "number") -> str:
    """Return ``raw`` as E.164, or raise ``PolicyError``.

    Accepts the shapes people actually type — "+971 50 123 4567",
    "00971501234567", "(555) 010-1234" — and rejects anything that is not
    unambiguously an international number. National numbers are refused rather
    than guessed at: assuming a country code is how a test call ends up ringing
    a stranger.
    """
    if not raw or not raw.strip():
        raise PolicyError("number_missing", f"No {field} was provided.")

    cleaned = _PUNCTUATION.sub("", raw.strip())

    # "00" is the international access code in most of the world; LiveKit and
    # every carrier want "+".
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    bare = cleaned.lstrip("+")
    if bare in EMERGENCY_NUMBERS:
        raise PolicyError(
            "emergency_number",
            "Emergency and service numbers can never be dialled by the agent.",
        )

    if not cleaned.startswith("+"):
        raise PolicyError(
            "not_international",
            f"The {field} must be in international format, starting with a "
            f"country code (for example +971501234567). Got {raw!r}.",
        )

    if not _E164.match(cleaned):
        raise PolicyError(
            "invalid_number",
            f"{raw!r} is not a valid E.164 {field}.",
        )

    return cleaned


def country_code(number: str, known_codes: Iterable[str] = ()) -> str:
    """Best-effort calling code for an E.164 number.

    Calling codes are one to three digits and are not self-delimiting, so this
    matches the longest configured code first and otherwise falls back to a
    small table of common one- and two-digit codes. Only used for the country
    allowlist, where a wrong guess fails closed.
    """
    bare = number.lstrip("+")
    for code in sorted(known_codes, key=len, reverse=True):
        if code and bare.startswith(code):
            return code
    for length in (1, 2, 3):
        prefix = bare[:length]
        if prefix in _COMMON_CODES:
            return prefix
    return bare[:3]


# Enough to cover the common cases; the allowlist itself supplies the rest.
_COMMON_CODES = frozenset(
    {
        "1",
        "7",
        "20",
        "27",
        "31",
        "33",
        "34",
        "39",
        "44",
        "49",
        "52",
        "55",
        "61",
        "62",
        "65",
        "81",
        "82",
        "86",
        "91",
        "92",
        "234",
        "254",
        "353",
        "358",
        "380",
        "420",
        "966",
        "968",
        "971",
        "972",
        "973",
        "974",
        "977",
        "994",
        "998",
    }
)


class CallPolicy:
    """Stateful gate in front of the dialer.

    Holds the rate-limit window and therefore has to be shared by every caller
    of the control plane; ``TelephonyService`` owns the single instance.
    """

    def __init__(
        self,
        settings: CallPolicySettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._recent: deque[float] = deque()

    # ------------------------------------------------------------ destination

    def check_destination(self, raw_number: str) -> str:
        """Validate a destination and return it in E.164."""
        number = normalize_number(raw_number, field="destination number")
        settings = self._settings

        if number in settings.blocked_numbers:
            raise PolicyError(
                "number_blocked",
                f"{number} is on the blocked-numbers list.",
            )

        if any(number.startswith(prefix) for prefix in _BLOCKED_PREFIXES):
            raise PolicyError(
                "number_blocked",
                f"{number} is a premium or service number, which is never dialled.",
            )

        if settings.allowed_numbers and number not in settings.allowed_numbers:
            raise PolicyError(
                "number_not_allowed",
                f"{number} is not on TELEPHONY_ALLOWED_NUMBERS. While that "
                "allowlist is set, only those numbers can be called.",
            )

        if settings.allowed_countries:
            code = country_code(number, settings.allowed_countries)
            if code not in settings.allowed_countries:
                raise PolicyError(
                    "country_not_allowed",
                    f"Calls to +{code} are not enabled. Allowed country codes: "
                    + ", ".join(f"+{c}" for c in settings.allowed_countries)
                    + ".",
                )

        return number

    # -------------------------------------------------------------- caller ID

    def check_caller_id(
        self, raw_number: str, *, owned_numbers: Iterable[str] | None = None
    ) -> str:
        """Validate the number the call will appear to come from.

        A carrier rejects a caller ID you do not hold, so checking it here
        turns a confusing SIP 403 into a clear error. ``owned_numbers`` empty
        means the inventory was unavailable, and the check is skipped rather
        than blocking the call.
        """
        number = normalize_number(raw_number, field="caller ID")
        owned = set(owned_numbers or ())
        if owned and number not in owned:
            raise PolicyError(
                "caller_id_not_owned",
                f"{number} is not one of your numbers, so it cannot be used "
                "as the caller ID.",
            )
        return number

    # ---------------------------------------------------------------- volume

    def check_capacity(self, active_calls: int) -> None:
        """Concurrency and rate limits.

        Both exist to bound the damage from a loop somewhere upstream: an agent
        or script that retries forever costs money per attempt.
        """
        settings = self._settings

        if active_calls >= settings.max_concurrent_calls > 0:
            raise PolicyError(
                "too_many_calls",
                f"{active_calls} calls are already live and the limit is "
                f"{settings.max_concurrent_calls}.",
            )

        limit = settings.calls_per_minute
        if limit <= 0:
            return

        now = self._clock()
        cutoff = now - 60.0
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()

        if len(self._recent) >= limit:
            raise PolicyError(
                "rate_limited",
                f"More than {limit} calls were placed in the last minute. "
                "Wait a moment and try again.",
            )

    def record_call(self) -> None:
        """Count a call against the rate limit. Called once a dial is accepted."""
        self._recent.append(self._clock())
