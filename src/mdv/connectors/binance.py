from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from mdv.connectors.base import fetch_json, market_availability, session_status, utc_now
from mdv.models import MarketRecord, MarketSnapshot, TradingSchedule
from mdv.normalization import contract_direction, normalize_contract_type, normalize_product, normalize_status


def binance_market_schedule(market: dict, raw: dict) -> TradingSchedule | None:
    if market.get("market_type") != "FUTURE":
        return None
    subtypes = [str(value).upper() for value in raw.get("underlyingSubType") or []]
    if not (
        str(raw.get("contractType") or "").upper() == "TRADIFI_PERPETUAL"
        or str(raw.get("underlyingType") or "").upper() == "EQUITY"
        or "TRADFI" in subtypes
    ):
        return None
    provider_status = str(market.get("status") or "UNKNOWN")
    if normalize_status(provider_status) == "UNKNOWN":
        provider_status = str(market.get("venue_status") or provider_status)
    return TradingSchedule(
        session_status=session_status(provider_status),
        market_group=str(raw.get("underlyingType") or "TRADFI").upper(),
    )


class BinanceConnector:
    venue = "BINANCE"

    def __init__(self, *, source: str, market_type: str, product: str, url: str, metadata_url: str | None = None):
        self.source = source
        self.market_type = market_type
        self.product = product
        self.url = url
        self.metadata_url = metadata_url

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        if self.metadata_url:
            payload, metadata_payload = await asyncio.gather(
                fetch_json(client, self.url),
                fetch_json(client, self.metadata_url),
            )
        else:
            payload = await fetch_json(client, self.url)
            metadata_payload = None
        return self.parse(payload, metadata_payload=metadata_payload, observed_at=utc_now())

    def parse(self, payload: dict, *, observed_at: str, metadata_payload: dict | None = None) -> MarketSnapshot:
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise ValueError(f"{self.source}: response has no symbols array")
        metadata_by_symbol = {}
        if metadata_payload is not None:
            metadata_rows = metadata_payload.get("data")
            if not isinstance(metadata_rows, list):
                raise ValueError(f"{self.source}: metadata response has no data array")
            metadata_by_symbol = {
                str(row.get("s") or "").upper(): row
                for row in metadata_rows
                if isinstance(row, dict) and row.get("s")
            }
        markets = []
        for row in symbols:
            venue_status = str(row.get("status") or row.get("contractStatus") or "UNKNOWN").upper()
            is_spot = self.market_type == "SPOT"
            raw_contract_type = str(row.get("contractType") or "FUTURE").upper()
            contract_type = "SPOT" if is_spot else normalize_contract_type(raw_contract_type)
            settle_symbol = (
                None
                if is_spot
                else str(row.get("marginAsset") or row.get("quoteAsset") or "").upper() or None
            )
            metadata = metadata_by_symbol.get(str(row["symbol"]).upper())
            raw = dict(row)
            if metadata is not None:
                raw["_metadata"] = {"BINANCE_PRODUCT": metadata}
            schedule = binance_market_schedule(
                {"market_type": self.market_type, "status": venue_status}, raw
            )
            availability = market_availability(
                venue_status=venue_status,
                default_active=venue_status == "TRADING",
                trading_schedule=schedule,
            )
            market_lot_size = next(
                (
                    item
                    for item in (row.get("filters") or [])
                    if isinstance(item, dict) and item.get("filterType") == "MARKET_LOT_SIZE"
                ),
                None,
            )
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=normalize_product(self.market_type, contract_type),
                    raw_symbol=str(row["symbol"]).upper(),
                    base_symbol=str(row["baseAsset"]).upper(),
                    quote_symbol=str(row["quoteAsset"]).upper(),
                    settle_symbol=settle_symbol,
                    contract_type=contract_type,
                    status=availability.status,
                    active=availability.active,
                    contract_multiplier=str(row.get("contractSize")) if row.get("contractSize") is not None else None,
                    raw=raw,
                    max_market_order_size=(
                        str(market_lot_size["maxQty"])
                        if market_lot_size is not None and market_lot_size.get("maxQty") is not None
                        else None
                    ),
                    expires_at=self._expires_at(row.get("deliveryDate"), contract_type),
                    venue_product=self.product,
                    venue_status=venue_status,
                    contract_direction=contract_direction(
                        market_type=self.market_type,
                        base_symbol=str(row["baseAsset"]),
                        quote_symbol=str(row["quoteAsset"]),
                        settle_symbol=settle_symbol,
                    ),
                    expiry_cycle={
                        "CURRENT_MONTH": "M",
                        "NEXT_MONTH": "BM",
                        "CURRENT_QUARTER": "Q",
                        "NEXT_QUARTER": "BQ",
                    }.get(raw_contract_type),
                    trading_schedule=availability.trading_schedule,
                )
            )
        snapshot = MarketSnapshot(
            source=self.source,
            venue=self.venue,
            market_type=self.market_type,
            product=self.product,
            observed_at=observed_at,
            markets=tuple(markets),
        )
        snapshot.validate()
        return snapshot

    def _expires_at(self, value: object, contract_type: str) -> str | None:
        if contract_type != "DATED" or value in (None, "", 0, "0"):
            return None
        try:
            return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid deliveryDate {value!r}") from exc


def binance_connectors() -> list[BinanceConnector]:
    return [
        BinanceConnector(
            source="BINANCE_SPOT",
            market_type="SPOT",
            product="SPOT",
            url="https://api.binance.com/api/v3/exchangeInfo",
            metadata_url="https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true",
        ),
        BinanceConnector(
            source="BINANCE_USDM_FUTURE",
            market_type="FUTURE",
            product="USD-M",
            url="https://fapi.binance.com/fapi/v1/exchangeInfo",
        ),
        BinanceConnector(
            source="BINANCE_COINM_FUTURE",
            market_type="FUTURE",
            product="COIN-M",
            url="https://dapi.binance.com/dapi/v1/exchangeInfo",
        ),
    ]
