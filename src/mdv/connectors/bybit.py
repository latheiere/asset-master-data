from __future__ import annotations

from urllib.parse import urlencode

import httpx

from mdv.connectors.base import (
    fetch_json,
    market_availability,
    required_text,
    session_status,
    strict_epoch_timestamp,
    utc_now,
)
from mdv.contract_metadata import (
    NORMALIZATION_VERSION,
    canonical_base_symbol,
    positive_decimal,
    with_contract_evidence,
)
from mdv.models import MarketRecord, MarketSnapshot, TradingSchedule
from mdv.normalization import contract_direction, normalize_contract_type, normalize_product


BYBIT_BASE_URL = "https://api.bytick.com"
BYBIT_OPEN_INTEREST_SPEC_URL = (
    "https://bybit-exchange.github.io/docs/v5/market/open-interest"
)


def bybit_market_schedule(market: dict, raw: dict) -> TradingSchedule | None:
    if (
        market.get("market_type") != "FUTURE"
        or str(raw.get("symbolType") or "").lower() != "stock"
    ):
        return None
    return TradingSchedule(
        session_status=session_status(str(market.get("status") or "UNKNOWN")),
        market_group="STOCK",
    )


class BybitConnector:
    venue = "BYBIT"
    url = f"{BYBIT_BASE_URL}/v5/market/instruments-info"

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
                base_symbol = required_text(
                    row, "baseCoin", source=self.source, record_kind="instrument"
                ).upper()
                quote_symbol = required_text(
                    row, "quoteCoin", source=self.source, record_kind="instrument"
                ).upper()
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
                schedule = bybit_market_schedule(
                    {"market_type": self.market_type, "status": venue_status}, row
                )
                availability = market_availability(
                    venue_status=venue_status,
                    default_active=venue_status == "TRADING",
                    trading_schedule=schedule,
                )
                direction = contract_direction(
                    market_type=self.market_type,
                    base_symbol=base_symbol,
                    quote_symbol=quote_symbol,
                    settle_symbol=settle_symbol,
                )
                contract_multiplier = None
                contract_multiplier_unit = None
                contract_value_currency = None
                open_interest_unit = None
                raw = row
                if self.market_type == "FUTURE":
                    if direction == "INVERSE":
                        contract_multiplier = "1"
                        contract_multiplier_unit = "QUOTE"
                        contract_value_currency = quote_symbol
                        open_interest_unit = "QUOTE_ASSET"
                    else:
                        contract_multiplier = "1"
                        contract_multiplier_unit = "VENUE_BASE"
                        contract_value_currency = canonical_base_symbol(
                            base_symbol,
                            venue=self.venue,
                            market_type=self.market_type,
                        )
                        open_interest_unit = "BASE_ASSET"
                    lot_size = row.get("lotSizeFilter")
                    raw = with_contract_evidence(
                        row,
                        {
                            "source": BYBIT_OPEN_INTEREST_SPEC_URL,
                            "normalization_version": NORMALIZATION_VERSION,
                            "open_interest_unit": open_interest_unit,
                            "quantity_increment": (
                                positive_decimal(lot_size.get("qtyStep"))
                                if isinstance(lot_size, dict)
                                else None
                            ),
                            "quantity_increment_is_not_contract_multiplier": True,
                        },
                    )
                markets.append(
                    MarketRecord(
                        source=self.source,
                        venue=self.venue,
                        market_type=self.market_type,
                        product=normalize_product(self.market_type, contract_type),
                        raw_symbol=required_text(
                            row, "symbol", source=self.source, record_kind="instrument"
                        ).upper(),
                        base_symbol=base_symbol,
                        quote_symbol=quote_symbol,
                        settle_symbol=settle_symbol,
                        contract_type=contract_type,
                        status=availability.status,
                        active=availability.active,
                        contract_multiplier=contract_multiplier,
                        raw=raw,
                        expires_at=self._expires_at(row.get("deliveryTime")),
                        max_market_order_size=max_market_order_size,
                        venue_product=self.product,
                        venue_status=venue_status,
                        contract_direction=direction,
                        trading_schedule=availability.trading_schedule,
                        contract_multiplier_unit=contract_multiplier_unit,
                        contract_value_currency=contract_value_currency,
                        open_interest_unit=open_interest_unit,
                        contract_metadata_source=(
                            BYBIT_OPEN_INTEREST_SPEC_URL
                            if self.market_type == "FUTURE"
                            else None
                        ),
                        contract_metadata_observed_at=(
                            observed_at if self.market_type == "FUTURE" else None
                        ),
                        contract_metadata_normalization_version=(
                            NORMALIZATION_VERSION
                            if self.market_type == "FUTURE"
                            else None
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

    def _contract_type(self, raw_contract_type: str) -> str:
        if self.market_type == "SPOT":
            return "SPOT"
        return normalize_contract_type(raw_contract_type, market_type=self.market_type)

    def _expires_at(self, raw_value: object) -> str | None:
        return strict_epoch_timestamp(
            raw_value,
            milliseconds=True,
            source=self.source,
            field="deliveryTime",
            allow_missing=True,
        )


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
