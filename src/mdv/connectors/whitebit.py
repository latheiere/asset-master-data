from __future__ import annotations

import asyncio

import httpx

from mdv.connectors.base import fetch_json, market_availability, utc_now
from mdv.contract_metadata import NORMALIZATION_VERSION, with_contract_evidence
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
    futures_url = "https://whitebit.com/api/v4/public/futures"

    def __init__(self, *, market_type: str):
        self.market_type = market_type
        self.product = "SPOT" if market_type == "SPOT" else "PERP"
        self.source = f"WHITEBIT_{market_type}"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        if self.market_type == "SPOT":
            return self.parse(await fetch_json(client, self.url), observed_at=utc_now())
        markets, futures = await asyncio.gather(
            fetch_json(client, self.url),
            fetch_json(client, self.futures_url),
        )
        return self.parse(markets, futures_payload=futures, observed_at=utc_now())

    def parse(
        self,
        payload: object,
        *,
        observed_at: str,
        futures_payload: object | None = None,
    ) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        wanted = {"spot"} if self.market_type == "SPOT" else {"futures", "tradfiFutures"}
        rows = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: market is not an object")
            if str(row.get("type") or "").strip() in wanted:
                rows.append(row)
        futures_by_symbol = (
            self._futures_by_symbol(futures_payload)
            if self.market_type == "FUTURE" and futures_payload is not None
            else {}
        )
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
            contract_multiplier = None
            contract_multiplier_unit = None
            contract_value_currency = None
            open_interest_unit = None
            contract_metadata_reason = None
            if self.market_type == "FUTURE":
                raw_symbol = _required(row, "name", source=self.source)
                future_spec = futures_by_symbol.get(raw_symbol)
                if future_spec is None:
                    contract_metadata_reason = "WHITEBIT_FUTURES_SPEC_MISSING"
                elif (
                    str(future_spec.get("stock_currency") or "").upper() != base_symbol
                    or str(future_spec.get("money_currency") or "").upper() != quote_symbol
                ):
                    contract_metadata_reason = "WHITEBIT_FUTURES_SPEC_CONFLICT"
                else:
                    contract_multiplier = "1"
                    contract_multiplier_unit = base_symbol
                    contract_value_currency = base_symbol
                    open_interest_unit = "BASE_ASSET"
                raw = with_contract_evidence(
                    raw,
                    {
                        "source": self.futures_url,
                        "normalization_version": NORMALIZATION_VERSION,
                        "futures": future_spec,
                        "reason": contract_metadata_reason,
                    },
                )
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
                    contract_multiplier,
                    raw,
                    max_market_order_size=(
                        str(row["maxTotal"]) if row.get("maxTotal") is not None else None
                    ),
                    venue_product=str(row.get("type") or self.product).upper(),
                    venue_status=venue_status,
                    contract_direction=None if self.market_type == "SPOT" else "LINEAR",
                    trading_schedule=availability.trading_schedule,
                    contract_multiplier_unit=contract_multiplier_unit,
                    contract_value_currency=contract_value_currency,
                    open_interest_unit=open_interest_unit,
                    contract_metadata_reason=contract_metadata_reason,
                    contract_metadata_source=(
                        self.futures_url if self.market_type == "FUTURE" else None
                    ),
                    contract_metadata_observed_at=(
                        observed_at if self.market_type == "FUTURE" else None
                    ),
                    contract_metadata_normalization_version=(
                        NORMALIZATION_VERSION if self.market_type == "FUTURE" else None
                    ),
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot

    def _futures_by_symbol(self, payload: object) -> dict[str, dict]:
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError(f"{self.source}: unsuccessful futures response")
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise ValueError(f"{self.source}: futures response has no result array")
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: futures instrument is not an object")
            symbol = _required(row, "ticker_id", source=self.source)
            if symbol in result:
                raise ValueError(f"{self.source}: duplicate futures instrument {symbol}")
            result[symbol] = row
        return result


def whitebit_connectors() -> list[WhitebitConnector]:
    return [WhitebitConnector(market_type=value) for value in ("SPOT", "FUTURE")]
