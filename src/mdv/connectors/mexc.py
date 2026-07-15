from __future__ import annotations

import httpx

from mdv.connectors.base import fetch_json, market_availability, session_status, utc_now
from mdv.models import MarketRecord, MarketSnapshot, TradingSchedule
from mdv.normalization import contract_direction, normalize_status


def mexc_market_schedule(market: dict, raw: dict) -> TradingSchedule | None:
    concepts = [str(value).lower() for value in raw.get("conceptPlate") or []]
    if market.get("market_type") != "FUTURE" or not any(
        "tradfi" in value or "stock" in value for value in concepts
    ):
        return None
    group = next(
        (value.rsplit("-", 1)[-1].upper() for value in concepts if "tradfi" in value),
        "TRADFI",
    )
    return TradingSchedule(
        session_status=session_status(str(market.get("status") or "UNKNOWN")),
        market_group=group,
    )


class MexcSpotConnector:
    source = "MEXC_SPOT"
    venue = "MEXC"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api.mexc.com/api/v3/exchangeInfo"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: dict, *, observed_at: str) -> MarketSnapshot:
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise ValueError("MEXC_SPOT: response has no symbols array")
        markets = []
        for row in symbols:
            raw_status = str(row.get("status") or "UNKNOWN").upper()
            venue_status = {"1": "ENABLED", "0": "DISABLED"}.get(raw_status, raw_status)
            allowed = row.get("isSpotTradingAllowed")
            active = venue_status in {"TRADING", "ENABLED"} and allowed is not False
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=self.product,
                    raw_symbol=str(row["symbol"]).upper(),
                    base_symbol=str(row["baseAsset"]).upper(),
                    quote_symbol=str(row["quoteAsset"]).upper(),
                    settle_symbol=None,
                    contract_type="SPOT",
                    status=normalize_status(venue_status),
                    active=active,
                    contract_multiplier=None,
                    raw=row,
                    venue_product=self.product,
                    venue_status=venue_status,
                )
            )
        snapshot = MarketSnapshot(self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets))
        snapshot.validate()
        return snapshot


class MexcFutureConnector:
    source = "MEXC_FUTURE"
    venue = "MEXC"
    market_type = "FUTURE"
    product = "PERP"
    url = "https://contract.mexc.com/api/v1/contract/detail"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: dict, *, observed_at: str) -> MarketSnapshot:
        if payload.get("success") is not True or not isinstance(payload.get("data"), list):
            raise ValueError("MEXC_FUTURE: unsuccessful or malformed response")
        markets = []
        for row in payload["data"]:
            state = int(row.get("state", -1))
            venue_status = {0: "ENABLED", 1: "DELIVERY", 2: "COMPLETED", 3: "OFFLINE", 4: "PAUSED"}.get(state, f"STATE_{state}")
            base_symbol = str(row["baseCoin"]).upper()
            quote_symbol = str(row["quoteCoin"]).upper()
            settle_symbol = str(row.get("settleCoin") or row.get("quoteCoin") or "").upper() or None
            schedule = mexc_market_schedule(
                {"market_type": self.market_type, "status": venue_status}, row
            )
            availability = market_availability(
                venue_status=venue_status,
                default_active=state == 0,
                trading_schedule=schedule,
            )
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=self.product,
                    raw_symbol=str(row["symbol"]).upper(),
                    base_symbol=base_symbol,
                    quote_symbol=quote_symbol,
                    settle_symbol=settle_symbol,
                    contract_type="PERP",
                    status=availability.status,
                    active=availability.active,
                    contract_multiplier=str(row.get("contractSize")) if row.get("contractSize") is not None else None,
                    raw=row,
                    max_market_order_size=(
                        str(row["maxVol"]) if row.get("maxVol") is not None else None
                    ),
                    venue_product=self.product,
                    venue_status=venue_status,
                    contract_direction=contract_direction(
                        market_type=self.market_type,
                        base_symbol=base_symbol,
                        quote_symbol=quote_symbol,
                        settle_symbol=settle_symbol,
                    ),
                    trading_schedule=availability.trading_schedule,
                )
            )
        snapshot = MarketSnapshot(self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets))
        snapshot.validate()
        return snapshot


def mexc_connectors() -> list[MexcSpotConnector | MexcFutureConnector]:
    return [MexcSpotConnector(), MexcFutureConnector()]
