from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlencode

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_status


def _required(row: dict, name: str, *, source: str) -> str:
    value = str(row.get(name) or "").strip()
    if not value:
        raise ValueError(f"{source}: product has no {name}")
    return value


def _restricted(row: dict) -> bool:
    return any(row.get(field) is True for field in ("trading_disabled", "is_disabled", "view_only"))


def _active_status(venue_status: str, row: dict) -> tuple[bool, str]:
    active = venue_status in {"LIVE", "ONLINE", "STANDARD", "TRADING"} and not _restricted(row)
    normalized_status = "TRADING" if active else ("PAUSED" if _restricted(row) else venue_status)
    return active, normalize_status(normalized_status)


class CoinbaseSpotConnector:
    source = "COINBASE_SPOT"
    venue = "COINBASE"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api.exchange.coinbase.com/products"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")

        markets = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: product is not an object")
            venue_status = _required(row, "status", source=self.source).upper()
            active, status = _active_status(venue_status, row)
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    _required(row, "id", source=self.source),
                    _required(row, "base_currency", source=self.source).upper(),
                    _required(row, "quote_currency", source=self.source).upper(),
                    None,
                    "SPOT",
                    status,
                    active,
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


def coinbase_connectors() -> list[CoinbaseSpotConnector | CoinbasePerpetualConnector]:
    return [CoinbaseSpotConnector(), CoinbasePerpetualConnector()]


class CoinbasePerpetualConnector:
    source = "COINBASE_PERP_FUTURE"
    venue = "COINBASE"
    market_type = "FUTURE"
    product = "PERP"
    base_url = "https://api.coinbase.com/api/v3/brokerage/market/products"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        query = {
            "product_type": "FUTURE",
            "contract_expiry_type": "PERPETUAL",
            "limit": 250,
        }
        products = []
        for _ in range(100):
            payload = await fetch_json(client, f"{self.base_url}?{urlencode(query)}")
            products.extend(self._products(payload))
            pagination = payload.get("pagination") if isinstance(payload, dict) else None
            if not isinstance(pagination, dict):
                raise ValueError(f"{self.source}: response has no pagination object")
            if pagination.get("has_next") is not True:
                return self.parse({"products": products}, observed_at=utc_now())
            cursor = str(pagination.get("next_cursor") or "").strip()
            if not cursor:
                raise ValueError(f"{self.source}: pagination has no next_cursor")
            query["cursor"] = cursor
        raise ValueError(f"{self.source}: pagination exceeded 100 pages")

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        snapshot = MarketSnapshot(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            observed_at,
            tuple(self._market(row) for row in self._products(payload)),
        )
        snapshot.validate()
        return snapshot

    def _products(self, payload: object) -> list:
        products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(products, list):
            raise ValueError(f"{self.source}: response has no products array")
        return products

    def _market(self, row: object) -> MarketRecord:
        if not isinstance(row, dict):
            raise ValueError(f"{self.source}: product is not an object")
        details = row.get("future_product_details")
        if not isinstance(details, dict):
            raise ValueError(f"{self.source}: product has no future_product_details")
        if str(details.get("contract_expiry_type") or "").upper() != "PERPETUAL":
            raise ValueError(f"{self.source}: product is not perpetual")
        venue_status = _required(row, "status", source=self.source).upper()
        active, status = _active_status(venue_status, row)
        base_symbol = _required(details, "contract_root_unit", source=self.source).upper()
        quote_symbol = _required(row, "quote_currency_id", source=self.source).upper()
        return MarketRecord(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            _required(row, "product_id", source=self.source),
            base_symbol,
            quote_symbol,
            quote_symbol,
            "PERP",
            status,
            active,
            str(details["contract_size"]) if details.get("contract_size") not in (None, "") else None,
            dict(row),
            max_market_order_size=(
                str(row["base_max_size"]) if row.get("base_max_size") not in (None, "") else None
            ),
            venue_product=str(row.get("product_venue") or "ADVANCED_TRADE").upper(),
            venue_status=venue_status,
            contract_direction=contract_direction(
                market_type=self.market_type,
                base_symbol=base_symbol,
                quote_symbol=quote_symbol,
                settle_symbol=quote_symbol,
            ),
        )


class CoinbaseCrossMarginConnector:
    source = "COINBASE_CROSS_MARGIN"
    venue = "COINBASE"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"
    url = CoinbaseSpotConnector.url

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        evidence: dict[str, list[dict]] = defaultdict(list)
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: product is not an object")
            pair = _required(row, "id", source=self.source)
            venue_status = _required(row, "status", source=self.source).upper()
            active, _ = _active_status(venue_status, row)
            for role, field in (("BASE", "base_currency"), ("QUOTE", "quote_currency")):
                asset = _required(row, field, source=self.source).upper()
                evidence[asset].append(
                    {
                        "pair": pair,
                        "asset_role": role,
                        "pair_active": active,
                        "margin_enabled": row.get("margin_enabled") is True,
                        "raw": row,
                    }
                )
        records = tuple(
            FinancingRecord(
                self.source,
                self.venue,
                self.product,
                "BORROWABLE",
                asset,
                any(item["pair_active"] and item["margin_enabled"] for item in items),
                "ENABLED" if any(item["pair_active"] and item["margin_enabled"] for item in items) else "DISABLED",
                None,
                (),
                (),
                {},
                tuple(sorted({item["pair"] for item in items})),
                {"evidence_granularity": "PAIR", "pairs": items},
            )
            for asset, items in sorted(evidence.items())
        )
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, records
        )
        snapshot.validate()
        return snapshot


def coinbase_financing_connectors() -> list[CoinbaseCrossMarginConnector]:
    return [CoinbaseCrossMarginConnector()]
