from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from mdv.models import MarketSnapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Connector(Protocol):
    source: str
    venue: str
    market_type: str
    product: str

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot: ...


async def fetch_json(client: httpx.AsyncClient, url: str, *, attempts: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last_error}")
