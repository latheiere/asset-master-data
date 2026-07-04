from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
        if not self.markets:
            raise ValueError(f"{self.source} returned an empty market snapshot")
        ids = [market.market_id for market in self.markets]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.source} returned duplicate market symbols")
        if any(market.source != self.source for market in self.markets):
            raise ValueError(f"{self.source} returned a record for another source")


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
        if not self.records:
            raise ValueError(f"{self.source} returned an empty financing snapshot")
        ids = [record.financing_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.source} returned duplicate financing records")
        if any(record.source != self.source for record in self.records):
            raise ValueError(f"{self.source} returned a record for another source")
        if any(record.product != self.product for record in self.records):
            raise ValueError(f"{self.source} returned a record for another product")
