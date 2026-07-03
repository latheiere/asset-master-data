from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import MarketRecord, MarketSnapshot
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


def _data(payload: object, *, source: str) -> list:
    if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
        message = payload.get("msg") if isinstance(payload, dict) else None
        raise ValueError(f"{source}: unsuccessful response: {message or 'malformed payload'}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"{source}: response has no data array")
    return data


class BitgetSpotConnector:
    source = "BITGET_SPOT"
    venue = "BITGET"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api.bitget.com/api/v2/spot/public/symbols"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _data(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: symbol is not an object")
            venue_status = str(row.get("status") or "UNKNOWN").strip().upper()
            tags = []
            if str(row.get("areaSymbol") or "").strip().lower() == "yes":
                tags.append(
                    {
                        "provider": self.venue,
                        "tag": "AREA",
                        "raw_tag": "Area",
                        "source": "BITGET_SPOT_SYMBOL",
                    }
                )
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=self.product,
                    raw_symbol=_required(row, "symbol", source=self.source),
                    base_symbol=_required(row, "baseCoin", source=self.source),
                    quote_symbol=_required(row, "quoteCoin", source=self.source),
                    settle_symbol=None,
                    contract_type="SPOT",
                    status=normalize_status(venue_status),
                    active=venue_status == "ONLINE",
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


class BitgetFutureConnector:
    venue = "BITGET"
    market_type = "FUTURE"
    url = "https://api.bitget.com/api/v2/mix/market/contracts"

    def __init__(self, *, product_type: str, product: str):
        self.product_type = product_type.upper()
        self.product = product
        prefix = self.product_type.removesuffix("-FUTURES")
        self.source = f"BITGET_{prefix}_FUTURE"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        url = f"{self.url}?{urlencode({'productType': self.product_type})}"
        return self.parse(await fetch_json(client, url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _data(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: contract is not an object")
            venue_status = str(row.get("symbolStatus") or "UNKNOWN").strip().upper()
            tags = []
            if str(row.get("isRwa") or "").strip().upper() == "YES":
                tags.append(
                    {
                        "provider": self.venue,
                        "tag": "RWA",
                        "raw_tag": "RWA",
                        "source": "BITGET_FUTURE_CONTRACT",
                    }
                )
            base_symbol = _required(row, "baseCoin", source=self.source)
            quote_symbol = _required(row, "quoteCoin", source=self.source)
            settle_symbol = base_symbol if self.product_type == "COIN-FUTURES" else quote_symbol
            contract_type = self._contract_type(row)
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=normalize_product(self.market_type, contract_type),
                    raw_symbol=_required(row, "symbol", source=self.source),
                    base_symbol=base_symbol,
                    quote_symbol=quote_symbol,
                    settle_symbol=settle_symbol,
                    contract_type=contract_type,
                    status=normalize_status(venue_status),
                    active=venue_status == "NORMAL",
                    contract_multiplier=None,
                    raw=_asset_tags(row, tags),
                    expires_at=self._expires_at(row),
                    max_market_order_size=(
                        str(row["maxMarketOrderQty"])
                        if row.get("maxMarketOrderQty") is not None
                        else None
                    ),
                    venue_product=self.product,
                    venue_status=venue_status,
                    contract_direction=contract_direction(
                        market_type=self.market_type,
                        base_symbol=base_symbol,
                        quote_symbol=quote_symbol,
                        settle_symbol=settle_symbol,
                    ),
                    expiry_cycle=self._expiry_cycle(row),
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
        symbol_type = str(row.get("symbolType") or "").strip().lower()
        if symbol_type == "perpetual":
            return "PERP"
        if symbol_type != "delivery":
            raise ValueError(f"{self.source}: unknown symbolType {symbol_type!r}")
        period = str(row.get("deliveryPeriod") or "").strip().lower()
        if period not in {"this_quarter", "next_quarter"}:
            raise ValueError(f"{self.source}: unknown deliveryPeriod {period!r}")
        return "DATED"

    def _expiry_cycle(self, row: dict) -> str | None:
        if str(row.get("symbolType") or "").strip().lower() != "delivery":
            return None
        return {"this_quarter": "Q", "next_quarter": "BQ"}.get(
            str(row.get("deliveryPeriod") or "").strip().lower()
        )

    def _expires_at(self, row: dict) -> str | None:
        if str(row.get("symbolType") or "").strip().lower() != "delivery":
            return None
        value = row.get("deliveryTime")
        try:
            return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid deliveryTime {value!r}") from exc


def bitget_connectors() -> list[BitgetSpotConnector | BitgetFutureConnector]:
    return [
        BitgetSpotConnector(),
        BitgetFutureConnector(product_type="USDT-FUTURES", product="USDT-M"),
        BitgetFutureConnector(product_type="USDC-FUTURES", product="USDC-M"),
        BitgetFutureConnector(product_type="COIN-FUTURES", product="COIN-M"),
    ]
