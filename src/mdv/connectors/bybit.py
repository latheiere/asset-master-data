from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_contract_type, normalize_product, normalize_status


class BybitConnector:
    venue = "BYBIT"
    url = "https://api.bybit.com/v5/market/instruments-info"

    def __init__(
        self,
        *,
        source: str,
        category: str,
        market_type: str,
        product: str,
    ):
        self.source = source
        self.category = category
        self.market_type = market_type
        self.product = product

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        payloads = []
        cursor = ""
        seen_cursors = set()
        while True:
            params = {"category": self.category, "status": "Trading"}
            if self.category != "spot":
                params["limit"] = "1000"
                if cursor:
                    params["cursor"] = cursor
            payload = await fetch_json(client, f"{self.url}?{urlencode(params)}")
            payloads.append(payload)
            result = self._result(payload)
            next_cursor = str(result.get("nextPageCursor") or "")
            if self.category == "spot" or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise ValueError(f"{self.source}: repeated pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return self.parse_pages(payloads, observed_at=utc_now())

    def parse(self, payload: dict, *, observed_at: str) -> MarketSnapshot:
        return self.parse_pages([payload], observed_at=observed_at)

    def parse_pages(self, payloads: list[dict], *, observed_at: str) -> MarketSnapshot:
        markets = []
        for payload in payloads:
            result = self._result(payload)
            for row in result["list"]:
                if not isinstance(row, dict):
                    raise ValueError(f"{self.source}: instrument is not an object")
                raw_contract_type = str(row.get("contractType") or "")
                if self.market_type == "FUTURE":
                    known_contract_types = {
                        "LinearPerpetual",
                        "InversePerpetual",
                        "LinearFutures",
                        "InverseFutures",
                    }
                    if raw_contract_type not in known_contract_types:
                        raise ValueError(
                            f"{self.source}: unknown contractType {raw_contract_type!r}"
                        )
                venue_status = str(row.get("status") or "UNKNOWN").upper()
                base_symbol = self._required(row, "baseCoin")
                quote_symbol = self._required(row, "quoteCoin")
                settle_symbol = None
                if self.market_type == "FUTURE":
                    fallback_settle = base_symbol if self.category == "inverse" else quote_symbol
                    settle_symbol = str(row.get("settleCoin") or fallback_settle).upper()
                contract_type = self._contract_type(raw_contract_type)
                lot_size_filter = row.get("lotSizeFilter")
                max_market_order_size = None
                if self.market_type == "FUTURE" and isinstance(lot_size_filter, dict):
                    value = lot_size_filter.get("maxMktOrderQty")
                    if value is not None:
                        max_market_order_size = str(value)
                markets.append(
                    MarketRecord(
                        source=self.source,
                        venue=self.venue,
                        market_type=self.market_type,
                        product=normalize_product(self.market_type, contract_type),
                        raw_symbol=self._required(row, "symbol"),
                        base_symbol=base_symbol,
                        quote_symbol=quote_symbol,
                        settle_symbol=settle_symbol,
                        contract_type=contract_type,
                        status=normalize_status(venue_status),
                        active=venue_status == "TRADING",
                        contract_multiplier=None,
                        raw=row,
                        expires_at=self._expires_at(row.get("deliveryTime")),
                        max_market_order_size=max_market_order_size,
                        venue_product=self.product,
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
            self.source,
            self.venue,
            self.market_type,
            self.product,
            observed_at,
            tuple(markets),
        )
        snapshot.validate()
        return snapshot

    def _result(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            message = payload.get("retMsg") if isinstance(payload, dict) else None
            raise ValueError(f"{self.source}: unsuccessful response: {message or 'malformed payload'}")
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise ValueError(f"{self.source}: response has no result.list array")
        if str(result.get("category") or "").lower() != self.category:
            raise ValueError(f"{self.source}: response category does not match {self.category}")
        return result

    def _required(self, row: dict, name: str) -> str:
        value = str(row.get(name) or "").strip().upper()
        if not value:
            raise ValueError(f"{self.source}: instrument has no {name}")
        return value

    def _contract_type(self, raw_contract_type: str) -> str:
        if self.market_type == "SPOT":
            return "SPOT"
        return normalize_contract_type(raw_contract_type, market_type=self.market_type)

    def _expires_at(self, raw_value: object) -> str | None:
        if raw_value in (None, "", "0", 0):
            return None
        try:
            milliseconds = int(str(raw_value))
            return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid deliveryTime {raw_value!r}") from exc


def bybit_connectors() -> list[BybitConnector]:
    return [
        BybitConnector(
            source="BYBIT_SPOT",
            category="spot",
            market_type="SPOT",
            product="SPOT",
        ),
        BybitConnector(
            source="BYBIT_LINEAR_FUTURE",
            category="linear",
            market_type="FUTURE",
            product="LINEAR",
        ),
        BybitConnector(
            source="BYBIT_INVERSE_FUTURE",
            category="inverse",
            market_type="FUTURE",
            product="INVERSE",
        ),
    ]
