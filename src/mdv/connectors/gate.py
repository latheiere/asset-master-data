from __future__ import annotations

from datetime import datetime, timezone

import httpx

from mdv.connectors.base import fetch_json, market_availability, session_status, utc_now
from mdv.models import MarketRecord, MarketSnapshot, TradingSchedule
from mdv.normalization import contract_direction, normalize_product, normalize_status


def _required(row: dict, name: str, *, source: str) -> str:
    value = str(row.get(name) or "").strip().upper()
    if not value:
        raise ValueError(f"{source}: instrument has no {name}")
    return value


def _asset_tags(row: dict, tags: list[dict]) -> dict:
    raw = dict(row)
    if tags:
        raw["_metadata"] = {"ASSET_TAGS": tags}
    return raw


def gate_market_schedule(market: dict, raw: dict) -> TradingSchedule | None:
    market_group = str(raw.get("contract_type") or "").strip().upper()
    if market.get("market_type") != "FUTURE" or market_group not in {
        "STOCKS", "FOREX", "INDICES", "COMMODITIES", "METALS",
    }:
        return None
    return TradingSchedule(
        session_status=session_status(str(market.get("status") or "UNKNOWN")),
        market_group=market_group,
    )


class GateSpotConnector:
    source = "GATE_SPOT"
    venue = "GATE"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api.gateio.ws/api/v4/spot/currency_pairs"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        markets = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: currency pair is not an object")
            venue_status = str(row.get("trade_status") or "UNKNOWN").strip().upper()
            tags = []
            if row.get("st_tag") is True:
                tags.append(
                    {
                        "provider": self.venue,
                        "tag": "ST",
                        "raw_tag": "ST",
                        "source": "GATE_SPOT_CURRENCY_PAIR",
                    }
                )
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=self.product,
                    raw_symbol=_required(row, "id", source=self.source),
                    base_symbol=_required(row, "base", source=self.source),
                    quote_symbol=_required(row, "quote", source=self.source),
                    settle_symbol=None,
                    contract_type="SPOT",
                    status=normalize_status(venue_status),
                    active=venue_status == "TRADABLE",
                    contract_multiplier=None,
                    raw=_asset_tags(row, tags),
                    venue_product=self.product,
                    venue_status=venue_status,
                )
            )
        snapshot = MarketSnapshot(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            observed_at,
            tuple(markets),
        )
        snapshot.validate()
        return snapshot


class GateFutureConnector:
    venue = "GATE"
    market_type = "FUTURE"

    def __init__(self, *, settle: str, delivery: bool = False):
        self.settle = settle.upper()
        self.delivery = delivery
        kind = "DELIVERY" if delivery else "FUTURE"
        self.source = f"GATE_{self.settle}_{kind}"
        self.product = f"{self.settle}-{'DELIVERY' if delivery else 'PERP'}"
        endpoint = "delivery" if delivery else "futures"
        self.url = f"https://api.gateio.ws/api/v4/{endpoint}/{self.settle.lower()}/contracts"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        markets = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: contract is not an object")
            raw_symbol = _required(row, "name", source=self.source)
            underlying = _required(row, "underlying", source=self.source) if self.delivery else raw_symbol
            try:
                base_symbol, quote_symbol = underlying.rsplit("_", 1)
            except ValueError as exc:
                raise ValueError(f"{self.source}: invalid underlying {underlying!r}") from exc
            in_delisting = row.get("in_delisting") is True
            raw_status = str(row.get("status") or "").strip().upper()
            venue_status = "DELISTING" if in_delisting else (raw_status or "TRADING")
            schedule = gate_market_schedule(
                {"market_type": self.market_type, "status": venue_status}, row
            )
            availability = market_availability(
                venue_status=venue_status,
                default_active=venue_status == "TRADING" and not in_delisting,
                trading_schedule=schedule,
            )
            contract_type = self._contract_type(row)
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=normalize_product(self.market_type, contract_type),
                    raw_symbol=raw_symbol,
                    base_symbol=base_symbol,
                    quote_symbol=quote_symbol,
                    settle_symbol=self.settle,
                    contract_type=contract_type,
                    status=availability.status,
                    active=availability.active,
                    contract_multiplier=(
                        str(row["quanto_multiplier"])
                        if row.get("quanto_multiplier") is not None
                        else None
                    ),
                    raw=row,
                    expires_at=self._expires_at(row.get("expire_time")),
                    max_market_order_size=(
                        str(row["market_order_size_max"])
                        if not self.delivery and row.get("market_order_size_max") is not None
                        else None
                    ),
                    venue_product=self.product,
                    venue_status=venue_status,
                    contract_direction=contract_direction(
                        market_type=self.market_type,
                        base_symbol=base_symbol,
                        quote_symbol=quote_symbol,
                        settle_symbol=self.settle,
                    ),
                    expiry_cycle=self._expiry_cycle(row),
                    trading_schedule=availability.trading_schedule,
                )
            )
        snapshot = MarketSnapshot(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            observed_at,
            tuple(markets),
        )
        snapshot.validate()
        return snapshot

    def _contract_type(self, row: dict) -> str:
        if not self.delivery:
            return "PERP"
        self._expiry_cycle(row)
        return "DATED"

    def _expiry_cycle(self, row: dict) -> str | None:
        if not self.delivery:
            return None
        cycle = str(row.get("cycle") or "").strip().upper()
        codes = {
            "WEEKLY": "W",
            "BI-WEEKLY": "BW",
            "QUARTERLY": "Q",
            "BI-QUARTERLY": "BQ",
        }
        try:
            return codes[cycle]
        except KeyError as exc:
            raise ValueError(f"{self.source}: unknown delivery cycle {cycle!r}") from exc

    def _expires_at(self, value: object) -> str | None:
        if not self.delivery:
            return None
        try:
            return datetime.fromtimestamp(int(str(value)), timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid expire_time {value!r}") from exc


def gate_connectors() -> list[GateSpotConnector | GateFutureConnector]:
    return [
        GateSpotConnector(),
        GateFutureConnector(settle="USDT"),
        GateFutureConnector(settle="BTC"),
        GateFutureConnector(settle="USDT", delivery=True),
    ]
