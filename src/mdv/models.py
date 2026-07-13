from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _validate_observed_at(value: str, *, source: str) -> None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{source} returned an invalid observed_at timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{source} returned an observed_at timestamp without a timezone")


def _required_text(value: object, *, source: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source} returned a record without {field}")


@dataclass(frozen=True)
class MarketRecord:
    source: str
    venue: str
    market_type: str
    product: str
    raw_symbol: str
    base_symbol: str
    quote_symbol: str
    settle_symbol: str | None
    contract_type: str
    status: str
    active: bool
    contract_multiplier: str | None
    raw: dict[str, Any]
    expires_at: str | None = None
    max_market_order_size: str | None = None
    venue_product: str | None = None
    venue_status: str | None = None
    contract_direction: str | None = None
    expiry_cycle: str | None = None

    @property
    def market_id(self) -> str:
        return f"{self.source}:{self.raw_symbol}"


@dataclass(frozen=True)
class MarketSnapshot:
    source: str
    venue: str
    market_type: str
    product: str
    observed_at: str
    markets: tuple[MarketRecord, ...]

    def validate(self) -> None:
        _required_text(self.source, source="snapshot", field="source")
        _required_text(self.venue, source=self.source, field="venue")
        if self.market_type not in {"SPOT", "FUTURE"}:
            raise ValueError(f"{self.source} returned an invalid market_type")
        _required_text(self.product, source=self.source, field="product")
        _validate_observed_at(self.observed_at, source=self.source)
        if not self.markets:
            raise ValueError(f"{self.source} returned an empty market snapshot")
        ids = [market.market_id for market in self.markets]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.source} returned duplicate market symbols")
        if any(market.source != self.source for market in self.markets):
            raise ValueError(f"{self.source} returned a record for another source")
        if any(market.venue != self.venue for market in self.markets):
            raise ValueError(f"{self.source} returned a record for another venue")
        if any(market.market_type != self.market_type for market in self.markets):
            raise ValueError(f"{self.source} returned a record for another market type")
        for market in self.markets:
            for field in (
                "product",
                "raw_symbol",
                "base_symbol",
                "quote_symbol",
                "contract_type",
                "status",
            ):
                _required_text(getattr(market, field), source=self.source, field=field)
            if not isinstance(market.active, bool) or not isinstance(market.raw, dict):
                raise ValueError(f"{self.source} returned malformed active/raw fields")
            if market.market_type == "SPOT" and market.settle_symbol is not None:
                raise ValueError(f"{self.source} returned a settled spot market")


@dataclass(frozen=True)
class FinancingRecord:
    source: str
    venue: str
    product: str
    asset_role: str
    raw_asset_symbol: str
    eligible: bool
    status: str
    regular_user_tier: str | None
    rates: tuple[dict[str, Any], ...]
    terms: tuple[dict[str, Any], ...]
    limits: dict[str, Any]
    pair_symbols: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def financing_id(self) -> str:
        return ":".join(
            (self.source, self.product, self.asset_role, self.raw_asset_symbol)
        )


@dataclass(frozen=True)
class FinancingSnapshot:
    source: str
    venue: str
    product: str
    observed_at: str
    records: tuple[FinancingRecord, ...]

    def validate(self) -> None:
        _required_text(self.source, source="snapshot", field="source")
        _required_text(self.venue, source=self.source, field="venue")
        if self.product not in {"CROSS_MARGIN", "CRYPTO_LOAN"}:
            raise ValueError(f"{self.source} returned an invalid financing product")
        _validate_observed_at(self.observed_at, source=self.source)
        if not self.records:
            raise ValueError(f"{self.source} returned an empty financing snapshot")
        ids = [record.financing_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.source} returned duplicate financing records")
        if any(record.source != self.source for record in self.records):
            raise ValueError(f"{self.source} returned a record for another source")
        if any(record.venue != self.venue for record in self.records):
            raise ValueError(f"{self.source} returned a record for another venue")
        if any(record.product != self.product for record in self.records):
            raise ValueError(f"{self.source} returned a record for another product")
        for record in self.records:
            if record.asset_role not in {"BORROWABLE", "COLLATERAL"}:
                raise ValueError(f"{self.source} returned an invalid financing asset role")
            _required_text(
                record.raw_asset_symbol, source=self.source, field="raw_asset_symbol"
            )
            _required_text(record.status, source=self.source, field="status")
            if not isinstance(record.eligible, bool) or not isinstance(record.raw, dict):
                raise ValueError(f"{self.source} returned malformed eligible/raw fields")
