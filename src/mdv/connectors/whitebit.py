from __future__ import annotations

import httpx

from mdv.connectors.base import fetch_json, market_availability, utc_now
from mdv.models import MarketRecord, MarketSnapshot, TradingSchedule


def whitebit_market_schedule(market: dict, raw: dict) -> TradingSchedule | None:
    if market.get("market_type") != "FUTURE" or raw.get("isTradFiFutures") is not True:
        return None
    current = (
        "UNKNOWN" if raw.get("delistedAt") not in (None, "")
        else ("OPEN" if raw.get("tradesEnabled") is True else "CLOSED")
    )
    return TradingSchedule(
        session_status=current,
        market_group="TRADFI",
    )


def _required(row: dict, name: str, *, source: str) -> str:
    value = str(row.get(name) or "").strip()
    if not value:
        raise ValueError(f"{source}: market has no {name}")
    return value


class WhitebitConnector:
    venue = "WHITEBIT"
    url = "https://whitebit.com/api/v4/public/markets"

    def __init__(self, *, market_type: str):
        self.market_type = market_type
        self.product = "SPOT" if market_type == "SPOT" else "PERP"
        self.source = f"WHITEBIT_{market_type}"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        wanted = {"spot"} if self.market_type == "SPOT" else {"futures", "tradfiFutures"}
        rows = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: market is not an object")
            if str(row.get("type") or "").strip() in wanted:
                rows.append(row)
        markets = []
        for row in rows:
            active = row.get("tradesEnabled") is True
            venue_status = "ENABLED" if active else (
                "DELISTED" if row.get("delistedAt") not in (None, "") else "DISABLED"
            )
            base_symbol = _required(row, "stock", source=self.source).upper()
            quote_symbol = _required(row, "money", source=self.source).upper()
            raw = dict(row)
            if row.get("isTradFiFutures") is True:
                raw["_metadata"] = {
                    "ASSET_TAGS": [
                        {
                            "provider": self.venue,
                            "tag": "TRADFI",
                            "raw_tag": "isTradFiFutures",
                            "source": "WHITEBIT_MARKET",
                        }
                    ]
                }
            schedule = whitebit_market_schedule(
                {"market_type": self.market_type, "status": venue_status}, raw
            )
            availability = market_availability(
                venue_status=venue_status,
                default_active=active,
                trading_schedule=schedule,
            )
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    _required(row, "name", source=self.source),
                    base_symbol,
                    quote_symbol,
                    None if self.market_type == "SPOT" else quote_symbol,
                    self.product,
                    availability.status,
                    availability.active,
                    None,
                    raw,
                    max_market_order_size=(
                        str(row["maxTotal"]) if row.get("maxTotal") is not None else None
                    ),
                    venue_product=str(row.get("type") or self.product).upper(),
                    venue_status=venue_status,
                    contract_direction=None if self.market_type == "SPOT" else "LINEAR",
                    trading_schedule=availability.trading_schedule,
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


def whitebit_connectors() -> list[WhitebitConnector]:
    return [WhitebitConnector(market_type=value) for value in ("SPOT", "FUTURE")]
