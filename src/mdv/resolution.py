from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mdv.normalization import (
    CONTRACT_DIRECTION_VALUES,
    EXPIRY_CYCLE_VALUES,
    PRODUCT_VALUES,
    STATUS_VALUES,
)


SUPPORTED_VENUES = {"BINANCE", "BITGET", "BYBIT", "GATE", "MEXC"}


def _uppercase(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized:
        raise ValueError("value must not be empty")
    return normalized


class MappingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str
    symbol_type: Literal["BASE"]
    symbols: list[str] = Field(min_length=1, max_length=100)

    @field_validator("venue", "symbol_type", mode="before")
    @classmethod
    def normalize_enum(cls, value: object) -> str:
        return _uppercase(value)

    @field_validator("venue")
    @classmethod
    def validate_venue(cls, value: str) -> str:
        if value not in SUPPORTED_VENUES:
            raise ValueError(f"unsupported venue: {value}")
        return value

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("symbols must be an array")
        normalized = []
        for symbol in value:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("symbols must contain non-empty strings")
            normalized.append(symbol.strip().upper())
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols must be unique")
        return normalized


class MappingTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str
    market_type: str
    product: str
    contract_type: str
    quote_symbol: str
    settle_symbol: str
    status: str
    venue_product: str | None = None
    contract_direction: str | None = None
    expiry_cycle: str | None = None

    @field_validator(
        "venue",
        "market_type",
        "product",
        "contract_type",
        "quote_symbol",
        "settle_symbol",
        "status",
        "venue_product",
        "contract_direction",
        "expiry_cycle",
        mode="before",
    )
    @classmethod
    def normalize_values(cls, value: object) -> str | None:
        return None if value is None else _uppercase(value)

    @model_validator(mode="after")
    def validate_dictionary(self) -> "MappingTarget":
        if self.venue not in SUPPORTED_VENUES:
            raise ValueError(f"unsupported venue: {self.venue}")
        if self.market_type not in {"SPOT", "FUTURE"}:
            raise ValueError(f"unsupported market_type: {self.market_type}")
        if self.product not in PRODUCT_VALUES:
            raise ValueError(f"unsupported product: {self.product}")
        if self.contract_type not in PRODUCT_VALUES:
            raise ValueError(f"unsupported contract_type: {self.contract_type}")
        if self.product != self.contract_type:
            raise ValueError("product and contract_type must use the same normalized value")
        if self.contract_direction not in (None, *CONTRACT_DIRECTION_VALUES):
            raise ValueError(f"unsupported contract_direction: {self.contract_direction}")
        if self.expiry_cycle not in (None, *EXPIRY_CYCLE_VALUES):
            raise ValueError(f"unsupported expiry_cycle: {self.expiry_cycle}")
        if self.status not in STATUS_VALUES:
            raise ValueError(f"unsupported status: {self.status}")
        if self.market_type == "SPOT" and self.product != "SPOT":
            raise ValueError("SPOT market_type requires SPOT product")
        if self.market_type == "FUTURE" and self.product == "SPOT":
            raise ValueError("FUTURE market_type requires PERP or DATED product")
        return self


class MappingResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: MappingSource
    target: MappingTarget


class ResolvedTarget(BaseModel):
    market_id: str
    raw_symbol: str
    base_symbol: str
    last_seen_at: str


class MappingResolution(BaseModel):
    source_symbol: str
    status: Literal[
        "resolved",
        "source_not_found",
        "target_not_found",
        "ambiguous_source",
        "ambiguous_target",
        "stale",
    ]
    error_code: str | None = None
    asset_id: str | None = None
    canonical_symbol: str | None = None
    target: ResolvedTarget | None = None


class MappingResolveResponse(BaseModel):
    schema_version: Literal["1"] = "1"
    snapshot_revision: str
    results: list[MappingResolution]
