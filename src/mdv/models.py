from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from mdv.contract_metadata import NORMALIZATION_VERSION


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
class TradingSchedule:
    """Provider-normalized metadata for markets that do not trade 24x7."""

    session_status: str
    description: str = "Follows the provider or underlying market session; not 24x7."
    market_group: str | None = None
    next_transition_at: str | None = None
    next_transition_status: str | None = None
    timezone: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("session_status", self.session_status),
                ("description", self.description),
                ("market_group", self.market_group),
                ("next_transition_at", self.next_transition_at),
                ("next_transition_status", self.next_transition_status),
                ("timezone", self.timezone),
            )
            if value is not None
        }


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
    trading_schedule: TradingSchedule | None = None
    contract_multiplier_unit: str | None = None
    contract_value_currency: str | None = None
    open_interest_unit: str | None = None
    contract_metadata_reason: str | None = None
    contract_metadata_source: str | None = None
    contract_metadata_observed_at: str | None = None
    contract_metadata_normalization_version: str | None = None

    @property
    def market_id(self) -> str:
        return f"{self.source}:{self.raw_symbol}"


@dataclass(frozen=True)
class MarketIngestIssue:
    raw_symbol: str
    error: str
    raw: dict[str, Any]


def _normalize_unsafe_contract_multiplier(
    market: MarketRecord,
    *,
    observed_at: str,
) -> MarketRecord:
    value = market.contract_multiplier
    if value is None:
        return market
    reason = None
    try:
        multiplier = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        reason = "SOURCE_RETURNED_INVALID_CONTRACT_MULTIPLIER"
    else:
        if not multiplier.is_finite() or multiplier <= 0:
            reason = "SOURCE_RETURNED_NON_POSITIVE_CONTRACT_MULTIPLIER"
    if reason is None:
        return market
    return replace(
        market,
        contract_multiplier=None,
        contract_metadata_reason=market.contract_metadata_reason or reason,
        contract_metadata_source=market.contract_metadata_source or market.source,
        contract_metadata_observed_at=(
            market.contract_metadata_observed_at or observed_at
        ),
        contract_metadata_normalization_version=(
            market.contract_metadata_normalization_version or NORMALIZATION_VERSION
        ),
    )


def _validate_market_record(
    market: MarketRecord,
    *,
    source: str,
    venue: str,
    market_type: str,
) -> None:
    if market.source != source:
        raise ValueError(f"{source} returned a record for another source")
    if market.venue != venue:
        raise ValueError(f"{source} returned a record for another venue")
    if market.market_type != market_type:
        raise ValueError(f"{source} returned a record for another market type")
    for field in (
        "product",
        "raw_symbol",
        "base_symbol",
        "quote_symbol",
        "contract_type",
        "status",
    ):
        _required_text(getattr(market, field), source=source, field=field)
    if not isinstance(market.active, bool) or not isinstance(market.raw, dict):
        raise ValueError(f"{source} returned malformed active/raw fields")
    if market.trading_schedule is not None:
        session_values = {"OPEN", "CLOSED", "UNKNOWN"}
        if market.trading_schedule.session_status not in session_values:
            raise ValueError(f"{source} returned an invalid session status")
        if (
            market.trading_schedule.next_transition_status is not None
            and market.trading_schedule.next_transition_status not in session_values
        ):
            raise ValueError(f"{source} returned an invalid next session status")
        if market.trading_schedule.next_transition_at is not None:
            _validate_observed_at(
                market.trading_schedule.next_transition_at,
                source=f"{source} trading schedule",
            )
        _required_text(
            market.trading_schedule.description,
            source=source,
            field="trading schedule description",
        )
    if market.market_type == "SPOT" and market.settle_symbol is not None:
        raise ValueError(f"{source} returned a settled spot market")
    if market.contract_multiplier is not None:
        multiplier = Decimal(market.contract_multiplier)
        if not multiplier.is_finite() or multiplier <= 0:
            raise ValueError(f"{source} returned an unsafe contract multiplier")
    if market.open_interest_unit not in {
        None,
        "CONTRACT",
        "BASE_ASSET",
        "QUOTE_ASSET",
    }:
        raise ValueError(f"{source} returned an invalid open-interest unit")
    for field in (
        "contract_multiplier_unit",
        "contract_value_currency",
        "contract_metadata_reason",
        "contract_metadata_source",
        "contract_metadata_normalization_version",
    ):
        value = getattr(market, field)
        if value is not None:
            _required_text(value, source=source, field=field)
    if market.contract_metadata_observed_at is not None:
        _validate_observed_at(
            market.contract_metadata_observed_at,
            source=f"{source} contract metadata",
        )


@dataclass(frozen=True)
class MarketSnapshot:
    source: str
    venue: str
    market_type: str
    product: str
    observed_at: str
    markets: tuple[MarketRecord, ...]
    issues: tuple[MarketIngestIssue, ...] = ()

    def __post_init__(self) -> None:
        valid_markets = []
        issues = list(self.issues)
        seen_ids: set[str] = set()
        for original in self.markets:
            market = _normalize_unsafe_contract_multiplier(
                original,
                observed_at=self.observed_at,
            )
            try:
                _validate_market_record(
                    market,
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                )
                if market.market_id in seen_ids:
                    raise ValueError(
                        f"{self.source} returned duplicate market symbol {market.raw_symbol}"
                    )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raw = market.raw if isinstance(market.raw, dict) else {"value": market.raw}
                issues.append(
                    MarketIngestIssue(
                        raw_symbol=str(market.raw_symbol or "<unknown>"),
                        error=f"{type(exc).__name__}: {exc}",
                        raw=raw,
                    )
                )
                continue
            seen_ids.add(market.market_id)
            valid_markets.append(market)
        object.__setattr__(self, "markets", tuple(valid_markets))
        object.__setattr__(self, "issues", tuple(issues))

    def validate(self) -> None:
        _required_text(self.source, source="snapshot", field="source")
        _required_text(self.venue, source=self.source, field="venue")
        if self.market_type not in {"SPOT", "FUTURE"}:
            raise ValueError(f"{self.source} returned an invalid market_type")
        _required_text(self.product, source=self.source, field="product")
        _validate_observed_at(self.observed_at, source=self.source)
        if not self.markets:
            raise ValueError(f"{self.source} returned an empty market snapshot")
        for issue in self.issues:
            _required_text(issue.raw_symbol, source=self.source, field="issue raw_symbol")
            _required_text(issue.error, source=self.source, field="issue error")
            if not isinstance(issue.raw, dict):
                raise ValueError(f"{self.source} returned a malformed symbol issue")


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
