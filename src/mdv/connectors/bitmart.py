from __future__ import annotations

from datetime import datetime, timezone

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_status


def _rows(payload: object, *, source: str) -> list:
    if not isinstance(payload, dict) or str(payload.get("code")) != "1000":
        raise ValueError(f"{source}: unsuccessful or malformed response")
    data = payload.get("data")
    rows = data.get("symbols") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{source}: response has no data.symbols array")
    return rows


def _required(row: dict, name: str, *, source: str) -> str:
    value = str(row.get(name) or "").strip()
    if not value:
        raise ValueError(f"{source}: market has no {name}")
    return value.upper()


class BitmartSpotConnector:
    source = "BITMART_SPOT"
    venue = "BITMART"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api-cloud.bitmart.com/spot/v1/symbols/details"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _rows(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: market is not an object")
            venue_status = _required(row, "trade_status", source=self.source)
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    _required(row, "symbol", source=self.source),
                    _required(row, "base_currency", source=self.source),
                    _required(row, "quote_currency", source=self.source),
                    None,
                    "SPOT",
                    normalize_status(venue_status),
                    venue_status == "TRADING",
                    None,
                    dict(row),
                    venue_product=self.product,
                    venue_status=venue_status,
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


class BitmartFutureConnector:
    source = "BITMART_FUTURE"
    venue = "BITMART"
    market_type = "FUTURE"
    product = "FUTURES"
    url = "https://api-cloud-v2.bitmart.com/contract/public/details"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _rows(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: contract is not an object")
            contract_type = self._contract_type(row)
            base_symbol = _required(row, "base_currency", source=self.source)
            quote_symbol = _required(row, "quote_currency", source=self.source)
            venue_product, settle_symbol = self._margin_product(
                base_symbol=base_symbol, quote_symbol=quote_symbol
            )
            venue_status = _required(row, "status", source=self.source)
            raw = dict(row)
            if isinstance(row.get("tradfi_info"), dict):
                raw["_metadata"] = {
                    "ASSET_TAGS": [
                        {
                            "provider": self.venue,
                            "tag": "TRADFI",
                            "raw_tag": "tradfi_info",
                            "source": "BITMART_FUTURE_CONTRACT",
                        }
                    ]
                }
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    contract_type,
                    _required(row, "symbol", source=self.source),
                    base_symbol,
                    quote_symbol,
                    settle_symbol,
                    contract_type,
                    normalize_status(venue_status),
                    venue_status == "TRADING",
                    (
                        str(row["contract_size"])
                        if row.get("contract_size") not in (None, "")
                        else None
                    ),
                    raw,
                    expires_at=self._expires_at(row.get("expire_timestamp"), contract_type),
                    max_market_order_size=(
                        str(row["market_max_volume"])
                        if row.get("market_max_volume") not in (None, "")
                        else None
                    ),
                    venue_product=venue_product,
                    venue_status=venue_status,
                    contract_direction=contract_direction(
                        market_type=self.market_type,
                        base_symbol=base_symbol,
                        quote_symbol=quote_symbol,
                        settle_symbol=settle_symbol,
                    ),
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot

    def _contract_type(self, row: dict) -> str:
        try:
            product_type = int(row.get("product_type"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{self.source}: invalid product_type {row.get('product_type')!r}"
            ) from exc
        if product_type == 1:
            return "PERP"
        if product_type == 2:
            return "DATED"
        raise ValueError(f"{self.source}: unknown product_type {product_type!r}")

    def _margin_product(self, *, base_symbol: str, quote_symbol: str) -> tuple[str, str]:
        # BitMart documents USD-denominated contracts as COIN-M; USDⓈ contracts
        # use their quote asset as collateral. The details endpoint has no separate
        # settlement field, so preserve that documented product distinction here.
        if quote_symbol == "USD":
            return "COIN-M", base_symbol
        if quote_symbol == "USDC":
            return "USDC-M", quote_symbol
        return "USDT-M", quote_symbol

    def _expires_at(self, value: object, contract_type: str) -> str | None:
        if contract_type != "DATED" or value in (None, "", 0, "0"):
            return None
        try:
            return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid expire_timestamp {value!r}") from exc


def bitmart_connectors() -> list[BitmartSpotConnector | BitmartFutureConnector]:
    return [BitmartSpotConnector(), BitmartFutureConnector()]
