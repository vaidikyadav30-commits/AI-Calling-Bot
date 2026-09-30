"""LiveKit Phone Numbers: the inventory the dialer lists and manages.

LiveKit sells and routes the numbers directly, so there is no carrier account
and no SIP trunk to configure — a number is rented and then pointed at a
dispatch rule, which is what hands the call to the agent.

The Python server SDK does not wrap ``PhoneNumberService`` (only Go and the CLI
do), so this module speaks its Twirp endpoints itself:

    POST https://<project>.livekit.cloud/twirp/livekit.PhoneNumberService/<Method>
    Authorization: Bearer <token with SIP admin grant>

See https://docs.livekit.io/reference/telephony/phone-numbers-api

Known limits of the service, all of which the UI surfaces rather than hides:
US numbers only, inbound calls only, and a released number is still billed for
the remainder of the month.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import aiohttp
from livekit import api

from ai_caller.config import LiveKitSettings
from ai_caller.telephony.models import (
    NumberInventory,
    NumberStatus,
    NumberType,
    PhoneNumber,
)

logger = logging.getLogger("ai_caller.telephony.numbers")

SERVICE = "livekit.PhoneNumberService"

# The admin token is minted per client and only needs to outlive one request.
TOKEN_TTL = datetime.timedelta(minutes=10)


class PhoneNumberError(Exception):
    """A Phone Numbers API call failed."""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_unsupported(self) -> bool:
        """True when this project or plan does not offer Phone Numbers.

        Worth distinguishing: it means "not available to you", not "broken",
        and the dialer says so instead of showing a stack of red errors.
        """
        return self.status == 404 or self.code in ("not_found", "unimplemented")

    def __str__(self) -> str:
        return f"{self.message} ({self.code})" if self.code else self.message


def api_base(url: str) -> str:
    """Turn a LiveKit ``wss://`` project URL into its HTTPS API origin."""
    base = url.strip().rstrip("/")
    if base.startswith("wss://"):
        return "https://" + base[len("wss://") :]
    if base.startswith("ws://"):
        return "http://" + base[len("ws://") :]
    if base.startswith(("http://", "https://")):
        return base
    return f"https://{base}"


class PhoneNumberClient:
    """Twirp client for ``livekit.PhoneNumberService``.

    Usable as an async context manager. A borrowed session is not closed, so
    the control plane can share one connection pool across every request.
    """

    def __init__(
        self,
        settings: LiveKitSettings,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if not settings.configured:
            raise PhoneNumberError(
                "LiveKit is not configured: set LIVEKIT_URL, LIVEKIT_API_KEY "
                "and LIVEKIT_API_SECRET."
            )
        self._settings = settings
        self._base = api_base(settings.url)
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> PhoneNumberClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    def _token(self) -> str:
        """A short-lived token carrying the SIP admin grant these APIs require."""
        return (
            api.AccessToken(self._settings.api_key, self._settings.api_secret)
            .with_identity("ai-caller-telephony")
            .with_ttl(TOKEN_TTL)
            .with_sip_grants(api.SIPGrants(admin=True))
            .to_jwt()
        )

    async def call(self, method: str, **body: Any) -> dict[str, Any]:
        """Invoke one Twirp method. Raises ``PhoneNumberError`` on failure."""
        session = await self._get_session()
        url = f"{self._base}/twirp/{SERVICE}/{method}"

        try:
            async with session.post(
                url,
                json=body or {},
                headers={"Authorization": f"Bearer {self._token()}"},
            ) as response:
                payload = await _decode(response)
                if response.status >= 400:
                    raise _error_from(payload, response.status, method)
                return payload
        except aiohttp.ClientError as exc:
            raise PhoneNumberError(f"Could not reach LiveKit: {exc}") from exc

    # ----------------------------------------------------------------- reads

    async def list_numbers(self, *, limit: int = 50) -> list[PhoneNumber]:
        """Numbers this project holds, newest inventory first."""
        body = await self.call("ListPhoneNumbers", limit=limit)
        items = body.get("items") or []
        return [parse_number(item) for item in items if isinstance(item, dict)]

    async def search(
        self, *, country_code: str = "US", area_code: str = "", limit: int = 20
    ) -> list[PhoneNumber]:
        """Numbers available to rent. Only US inventory exists today."""
        request: dict[str, Any] = {"country_code": country_code, "limit": limit}
        if area_code:
            request["area_code"] = area_code
        body = await self.call("SearchPhoneNumbers", **request)
        items = body.get("items") or []
        return [parse_number(item) for item in items if isinstance(item, dict)]

    # ---------------------------------------------------------------- writes

    async def purchase(
        self, numbers: list[str], *, dispatch_rule_id: str = ""
    ) -> list[PhoneNumber]:
        """Rent numbers. This spends money — callers must confirm first.

        Passing ``dispatch_rule_id`` assigns the rule at purchase time, so the
        number is never briefly live with nothing to answer it.
        """
        request: dict[str, Any] = {"phone_numbers": numbers}
        if dispatch_rule_id:
            request["sip_dispatch_rule_id"] = dispatch_rule_id

        body = await self.call("PurchasePhoneNumber", **request)
        purchased = body.get("phone_numbers") or []
        return [parse_number(item) for item in purchased if isinstance(item, dict)]

    async def assign_dispatch_rule(
        self, *, number_id: str = "", number: str = "", dispatch_rule_id: str
    ) -> PhoneNumber:
        """Point a number at a dispatch rule — the step that makes it answer."""
        request: dict[str, Any] = {"sip_dispatch_rule_id": dispatch_rule_id}
        if number_id:
            request["id"] = number_id
        elif number:
            request["phone_number"] = number
        else:
            raise PhoneNumberError("Either a number ID or a number is required.")

        body = await self.call("UpdatePhoneNumber", **request)
        return parse_number(body.get("phone_number") or {})

    async def release(self, numbers: list[str]) -> None:
        """Give numbers back. Still billed for the rest of the month."""
        await self.call("ReleasePhoneNumbers", phone_numbers=numbers)


# ------------------------------------------------------------------ inventory


async def fetch_inventory(
    client: PhoneNumberClient, *, default_caller_id: str = ""
) -> NumberInventory:
    """Everything the dialer needs to populate its number list."""
    numbers = await client.list_numbers()
    inventory = NumberInventory(
        numbers=tuple(numbers),
        default_caller_id=_pick_default(numbers, default_caller_id),
        warnings=tuple(_warnings(numbers)),
    )
    logger.info(
        "livekit inventory: %d number(s), %d routed",
        len(inventory.numbers),
        len(inventory.routed_numbers),
    )
    return inventory


async def safe_fetch_inventory(
    client: PhoneNumberClient, *, default_caller_id: str = ""
) -> NumberInventory:
    """``fetch_inventory`` that never raises.

    Used where a LiveKit hiccup should degrade the dialer rather than break it.
    """
    try:
        return await fetch_inventory(client, default_caller_id=default_caller_id)
    except (PhoneNumberError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("phone number list unavailable: %s", exc)
        note = (
            "Phone Numbers is not available on this LiveKit project or plan."
            if isinstance(exc, PhoneNumberError) and exc.is_unsupported
            else f"Phone number list unavailable: {exc}"
        )
        return NumberInventory(default_caller_id=default_caller_id, warnings=(note,))


def parse_number(record: dict[str, Any]) -> PhoneNumber:
    """Build a ``PhoneNumber`` from a Twirp payload.

    Tolerant on purpose: protobuf JSON may render enums as their full names
    ("PHONE_NUMBER_STATUS_ACTIVE"), as short strings ("active"), or as integers
    depending on the serializer, and different endpoints return different
    subsets of the fields.
    """
    rule_ids = record.get("sip_dispatch_rule_ids")
    if not isinstance(rule_ids, list):
        rule_ids = []
    legacy_rule = record.get("sip_dispatch_rule_id")
    if legacy_rule and legacy_rule not in rule_ids:
        rule_ids = [*rule_ids, legacy_rule]

    capabilities = record.get("capabilities")
    capability_list = (
        tuple(str(item).lower() for item in capabilities)
        if isinstance(capabilities, list)
        else ()
    )

    return PhoneNumber(
        id=str(record.get("id") or ""),
        number=str(record.get("e164_format") or record.get("phone_number") or ""),
        country_code=str(record.get("country_code") or ""),
        area_code=str(record.get("area_code") or ""),
        locality=str(record.get("locality") or ""),
        region=str(record.get("region") or ""),
        number_type=_parse_enum(
            record.get("number_type"), NumberType, "PHONE_NUMBER_TYPE_"
        ),
        status=_parse_enum(record.get("status"), NumberStatus, "PHONE_NUMBER_STATUS_"),
        capabilities=capability_list,
        dispatch_rule_ids=tuple(str(rule) for rule in rule_ids if rule),
    )


def _parse_enum(raw: Any, enum: type, prefix: str) -> Any:
    """Map a protobuf enum in any of its JSON renderings onto our own enum."""
    members = list(enum)
    if isinstance(raw, bool):
        return members[0]
    if isinstance(raw, int):
        return members[raw] if 0 <= raw < len(members) else members[0]

    text = str(raw or "").upper()
    if text.startswith(prefix):
        text = text[len(prefix) :]
    text = text.replace("-", "_")
    for member in members:
        if member.name == text or member.value.upper() == text:
            return member
    return members[0]


def _pick_default(numbers: list[PhoneNumber], configured: str) -> str:
    """The number to preselect. A configured value wins, even if unknown.

    Surfacing a misconfiguration beats silently correcting it.
    """
    if configured:
        return configured
    for number in numbers:
        if number.held:
            return number.number
    return ""


def _warnings(numbers: list[PhoneNumber]) -> list[str]:
    warnings: list[str] = []
    held = [number for number in numbers if number.held]

    if not numbers:
        warnings.append(
            "This LiveKit project holds no phone numbers. Every plan includes "
            "one free US local number: rent it with "
            "`uv run python scripts/setup_telephony.py --rent`."
        )
    elif not held:
        warnings.append(
            "Every number on this project has been released. Rent one with "
            "`uv run python scripts/setup_telephony.py --rent`."
        )
    elif not any(number.routed for number in held):
        warnings.append(
            "No number is routed to the agent yet, so inbound calls would "
            "reach nobody. Run `uv run python scripts/setup_telephony.py` to "
            "create the dispatch rule and assign it."
        )

    pending = [number for number in numbers if number.status is NumberStatus.PENDING]
    if pending:
        warnings.append(
            f"{len(pending)} number(s) are still being provisioned by LiveKit."
        )

    return warnings


async def _decode(response: aiohttp.ClientResponse) -> dict[str, Any]:
    text = await response.text()
    if not text:
        return {}
    try:
        import json

        parsed = json.loads(text)
    except ValueError:
        return {"msg": text.strip()[:500]}
    return parsed if isinstance(parsed, dict) else {"items": parsed}


def _error_from(body: dict[str, Any], status: int, method: str) -> PhoneNumberError:
    # Twirp errors are {"code": "...", "msg": "..."}.
    message = str(body.get("msg") or body.get("message") or f"{method} failed.")
    code = str(body.get("code") or "")
    logger.debug("phone number api %s -> %s %s", method, status, message)
    return PhoneNumberError(message, status=status, code=code)
