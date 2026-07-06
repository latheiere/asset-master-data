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
    return value


class OkxConnector:
    venue = "OKX"
    base_url = "https://www.okx.com/api/v5/public/instruments"

    def __init__(self, *, inst_type: str):
        self.inst_type = inst_type.upper()
        self.market_type = "SPOT" if self.inst_type == "SPOT" else "FUTURE"
        self.product = self.inst_type
        self.source = {
            "SPOT": "OKX_SPOT",
            "SWAP": "OKX_SWAP_FUTURE",
            "FUTURES": "OKX_EXPIRY_FUTURE",
        }[self.inst_type]

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        url = f"{self.base_url}?{urlencode({'instType': self.inst_type})}"
        return self.parse(await fetch_json(client, url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            message = payload.get("msg") if isinstance(payload, dict) else None
            raise ValueError(f"{self.source}: unsuccessful response: {message or 'malformed payload'}")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError(f"{self.source}: response has no data array")
        markets = tuple(self._market(row) for row in rows)
        snapshot = MarketSnapshot(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            observed_at,
            markets,
        )
        snapshot.validate()
        return snapshot

    def _market(self, row: object) -> MarketRecord:
        if not isinstance(row, dict):
            raise ValueError(f"{self.source}: instrument is not an object")
        raw_symbol = _required(row, "instId", source=self.source)
        venue_status = str(row.get("state") or "UNKNOWN").strip().upper()
        if self.market_type == "SPOT":
            base_symbol = _required(row, "baseCcy", source=self.source).upper()
            quote_symbol = _required(row, "quoteCcy", source=self.source).upper()
            settle_symbol = None
            contract_type = "SPOT"
            expires_at = None
            expiry_cycle = None
        else:
            family = str(row.get("uly") or row.get("instFamily") or "").strip()
            parts = family.split("-")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValueError(f"{self.source}: instrument has no usable uly/instFamily")
            base_symbol, quote_symbol = parts[0].upper(), parts[1].upper()
            settle_symbol = _required(row, "settleCcy", source=self.source).upper()
            contract_type = "PERP" if self.inst_type == "SWAP" else "DATED"
            expires_at = self._expires_at(row.get("expTime"))
            expiry_cycle = self._expiry_cycle(row.get("alias"))
        return MarketRecord(
            source=self.source,
            venue=self.venue,
            market_type=self.market_type,
            product=contract_type,
            raw_symbol=raw_symbol,
            base_symbol=base_symbol,
            quote_symbol=quote_symbol,
            settle_symbol=settle_symbol,
            contract_type=contract_type,
            status=normalize_status(venue_status),
            active=venue_status == "LIVE",
            contract_multiplier=(str(row["ctVal"]) if row.get("ctVal") not in (None, "") else None),
            raw=dict(row),
            expires_at=expires_at,
            max_market_order_size=(
                str(row["maxMktSz"]) if row.get("maxMktSz") not in (None, "") else None
            ),
            venue_product=self.inst_type,
            venue_status=venue_status,
            contract_direction=contract_direction(
                market_type=self.market_type,
                base_symbol=base_symbol,
                quote_symbol=quote_symbol,
                settle_symbol=settle_symbol,
            ),
            expiry_cycle=expiry_cycle,
        )

    def _expires_at(self, value: object) -> str | None:
        if self.inst_type != "FUTURES" or value in (None, "", 0, "0"):
            return None
        try:
            return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid expTime {value!r}") from exc

    def _expiry_cycle(self, value: object) -> str | None:
        if self.inst_type != "FUTURES":
            return None
        return {
            "THIS_WEEK": "W",
            "NEXT_WEEK": "BW",
            "THIS_MONTH": "M",
            "NEXT_MONTH": "BM",
            "QUARTER": "Q",
            "NEXT_QUARTER": "BQ",
        }.get(str(value or "").strip().upper())


def okx_connectors() -> list[OkxConnector]:
    return [OkxConnector(inst_type=value) for value in ("SPOT", "SWAP", "FUTURES")]
