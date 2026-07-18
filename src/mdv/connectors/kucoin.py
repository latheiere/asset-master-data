from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.contract_metadata import NORMALIZATION_VERSION, positive_decimal
from mdv.models import MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_status


def _data(payload: object, *, source: str) -> list:
    if not isinstance(payload, dict) or str(payload.get("code")) != "200000":
        raise ValueError(f"{source}: unsuccessful or malformed response")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"{source}: response has no data array")
    return rows


def _required(row: dict, name: str, *, source: str) -> str:
    value = str(row.get(name) or "").strip()
    if not value:
        raise ValueError(f"{source}: instrument has no {name}")
    return value


class KucoinSpotConnector:
    source = "KUCOIN_SPOT"
    venue = "KUCOIN"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api.kucoin.com/api/v2/symbols"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _data(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: symbol is not an object")
            active = row.get("enableTrading") is True
            raw = dict(row)
            if row.get("st") is True:
                raw["_metadata"] = {
                    "ASSET_TAGS": [
                        {
                            "provider": self.venue,
                            "tag": "ST",
                            "raw_tag": "st",
                            "source": "KUCOIN_SPOT_SYMBOL",
                        }
                    ]
                }
            venue_status = "ENABLED" if active else "DISABLED"
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    _required(row, "symbol", source=self.source),
                    _required(row, "baseCurrency", source=self.source).upper(),
                    _required(row, "quoteCurrency", source=self.source).upper(),
                    None,
                    "SPOT",
                    normalize_status(venue_status),
                    active,
                    None,
                    raw,
                    max_market_order_size=(
                        str(row["baseMaxSize"]) if row.get("baseMaxSize") is not None else None
                    ),
                    venue_product=self.product,
                    venue_status=venue_status,
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


class KucoinFutureConnector:
    source = "KUCOIN_FUTURE"
    venue = "KUCOIN"
    market_type = "FUTURE"
    product = "FUTURES"
    url = "https://api-futures.kucoin.com/api/v1/contracts/active"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _data(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: contract is not an object")
            venue_status = _required(row, "status", source=self.source).upper()
            # KuCoin can populate expireDate when it schedules a perpetual
            # delisting. The contract type, not that operational timestamp,
            # determines whether this is a delivery future.
            contract_type = self._contract_type(row)
            base_symbol = _required(row, "baseCurrency", source=self.source).upper()
            quote_symbol = _required(row, "quoteCurrency", source=self.source).upper()
            settle_symbol = _required(row, "settleCurrency", source=self.source).upper()
            direction = contract_direction(
                market_type=self.market_type,
                base_symbol=base_symbol,
                quote_symbol=quote_symbol,
                settle_symbol=settle_symbol,
            )
            raw_multiplier = row.get("multiplier")
            contract_multiplier, contract_metadata_reason = self._contract_multiplier(
                raw_multiplier,
                direction=direction,
            )
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
                    venue_status == "OPEN",
                    contract_multiplier,
                    dict(row),
                    expires_at=(
                        self._expires_at(row.get("expireDate"))
                        if contract_type == "DATED"
                        else None
                    ),
                    max_market_order_size=(
                        str(row["marketMaxOrderQty"])
                        if row.get("marketMaxOrderQty") is not None
                        else None
                    ),
                    venue_product=str(row.get("type") or self.product).upper(),
                    venue_status=venue_status,
                    contract_direction=direction,
                    contract_metadata_reason=contract_metadata_reason,
                    contract_metadata_source=(self.url if contract_metadata_reason else None),
                    contract_metadata_observed_at=(
                        observed_at if contract_metadata_reason else None
                    ),
                    contract_metadata_normalization_version=(
                        NORMALIZATION_VERSION if contract_metadata_reason else None
                    ),
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot

    def _contract_multiplier(
        self,
        value: object,
        *,
        direction: str | None,
    ) -> tuple[str | None, str | None]:
        if value in (None, ""):
            return None, None
        try:
            multiplier = Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return None, "SOURCE_RETURNED_INVALID_CONTRACT_MULTIPLIER"
        if not multiplier.is_finite() or multiplier == 0:
            return None, "SOURCE_RETURNED_NON_POSITIVE_CONTRACT_MULTIPLIER"
        if multiplier < 0:
            if direction != "INVERSE":
                return None, "SOURCE_RETURNED_UNEXPECTED_SIGNED_CONTRACT_MULTIPLIER"
            multiplier = abs(multiplier)
        return positive_decimal(multiplier), None

    def _contract_type(self, row: dict) -> str:
        contract_kind = str(row.get("type") or "").strip().upper()
        if contract_kind == "FFICSX":
            return "DATED"
        if contract_kind:
            return "PERP"
        raise ValueError(f"{self.source}: contract has no type")

    def _expires_at(self, value: object) -> str | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid expireDate {value!r}") from exc


def kucoin_connectors() -> list[KucoinSpotConnector | KucoinFutureConnector]:
    return [KucoinSpotConnector(), KucoinFutureConnector()]
