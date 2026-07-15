from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from mdv.models import FinancingSnapshot, MarketSnapshot, TradingSchedule
from mdv.normalization import normalize_status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Connector(Protocol):
    source: str
    venue: str
    market_type: str
    product: str

    async def fetch(
        self, client: httpx.AsyncClient
    ) -> MarketSnapshot | FinancingSnapshot: ...


@dataclass(frozen=True)
class MarketAvailability:
    """Generic lifecycle result after applying a provider's session policy."""

    status: str
    active: bool
    trading_schedule: TradingSchedule | None


def market_availability(
    *,
    venue_status: str,
    default_active: bool,
    trading_schedule: TradingSchedule | None = None,
    normalized_status: str | None = None,
) -> MarketAvailability:
    """Keep session-based markets listed while preserving terminal states."""
    status = normalize_status(normalized_status or venue_status)
    if trading_schedule is not None and trading_schedule.session_status == "CLOSED" and status == "CLOSED":
        status = "PAUSED"
    terminal = {"DELISTING", "DELIVERING", "SETTLING", "CLOSED", "MISSING"}
    active = False if status in terminal else (default_active or trading_schedule is not None)
    return MarketAvailability(status, active, trading_schedule)


def session_status(status: str) -> str:
    normalized = normalize_status(status)
    if normalized == "TRADING":
        return "OPEN"
    if normalized == "PAUSED":
        return "CLOSED"
    return "UNKNOWN"


def epoch_timestamp(value: object, *, milliseconds: bool) -> str | None:
    if value in (None, "", 0, "0", -1, "-1"):
        return None
    text = str(value).strip()
    if not text.lstrip("-").isdigit():
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.isoformat() if parsed.tzinfo is not None else None
        except ValueError:
            return None
    try:
        divisor = 1000 if milliseconds else 1
        return datetime.fromtimestamp(int(text) / divisor, timezone.utc).isoformat()
    except (OverflowError, TypeError, ValueError):
        return None


async def fetch_json(client: httpx.AsyncClient, url: str, *, attempts: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError) and not _transient_status(
                exc.response.status_code
            ):
                raise
            if attempt + 1 < attempts:
                await asyncio.sleep(_retry_delay(exc, attempt))
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last_error}")


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    attempts: int = 3,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError) and not _transient_status(
                exc.response.status_code
            ):
                raise
            if attempt + 1 < attempts:
                await asyncio.sleep(_retry_delay(exc, attempt))
    raise RuntimeError(f"POST {url} failed after {attempts} attempts: {last_error}")


def _transient_status(status_code: int) -> bool:
    return status_code in {408, 425, 429} or status_code >= 500


def _retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("retry-after", "").strip()
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
    return min(0.5 * (2**attempt) + random.uniform(0.0, 0.25), 10.0)
