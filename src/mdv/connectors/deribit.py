from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_status


def _required(row: dict, name: str, *, source: str) -> str:
    value = str(row.get(name) or "").strip()
    if not value:
        raise ValueError(f"{source}: instrument has no {name}")
    return value.upper()


class DeribitConnector:
    venue = "DERIBIT"
    base_url = "https://www.deribit.com/api/v2/public/get_instruments"

    def __init__(self, *, kind: str):
        self.kind = kind
        self.market_type = "SPOT" if kind == "spot" else "FUTURE"
        self.product = "SPOT" if kind == "spot" else "FUTURES"
        self.source = f"DERIBIT_{self.market_type}"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        url = f"{self.base_url}?{urlencode({'currency': 'any', 'kind': self.kind})}"
        return self.parse(await fetch_json(client, url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise ValueError(f"{self.source}: unsuccessful or malformed response")
        markets = []
        for row in payload["result"]:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: instrument is not an object")
            if str(row.get("kind") or "").lower() != self.kind:
                raise ValueError(f"{self.source}: unexpected instrument kind")
            base_symbol = _required(row, "base_currency", source=self.source)
            quote_symbol = _required(row, "quote_currency", source=self.source)
            venue_status = _required(row, "state", source=self.source)
            if self.market_type == "SPOT":
                contract_type = "SPOT"
                settle_symbol = None
                expires_at = None
                direction = None
                venue_product = "SPOT"
            else:
                contract_type = (
                    "PERP"
                    if str(row.get("settlement_period") or "").lower() == "perpetual"
                    else "DATED"
                )
                settle_symbol = _required(row, "settlement_currency", source=self.source)
                expires_at = self._expires_at(row.get("expiration_timestamp"), contract_type)
                instrument_type = str(row.get("instrument_type") or "").upper()
                direction = {"LINEAR": "LINEAR", "REVERSED": "INVERSE"}.get(
                    instrument_type
                ) or contract_direction(
                    market_type=self.market_type,
                    base_symbol=base_symbol,
                    quote_symbol=quote_symbol,
                    settle_symbol=settle_symbol,
                )
                venue_product = instrument_type or self.product
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    contract_type,
                    _required(row, "instrument_name", source=self.source),
                    base_symbol,
                    quote_symbol,
                    settle_symbol,
                    contract_type,
                    normalize_status(venue_status),
                    row.get("is_active") is True and venue_status == "OPEN",
                    str(row["contract_size"]) if row.get("contract_size") is not None else None,
                    dict(row),
                    expires_at=expires_at,
                    venue_product=venue_product,
                    venue_status=venue_status,
                    contract_direction=direction,
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot

    def _expires_at(self, value: object, contract_type: str) -> str | None:
        if contract_type != "DATED" or value in (None, "", 0, "0"):
            return None
        try:
            return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid expiration_timestamp {value!r}") from exc


def deribit_connectors() -> list[DeribitConnector]:
    return [DeribitConnector(kind=value) for value in ("spot", "future")]
