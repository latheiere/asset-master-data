from __future__ import annotations

from urllib.parse import urlencode

import httpx

from mdv.connectors.base import fetch_json, required_text, strict_epoch_timestamp, utc_now
from mdv.contract_metadata import NORMALIZATION_VERSION, canonical_base_symbol
from mdv.models import MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_status


class DeribitConnector:
    venue = "DERIBIT"
    base_url = "https://www.deribit.com/api/v2/public/get_instruments"

    def __init__(self, *, kind: str):
        self.kind = kind
        self.market_type = "SPOT" if kind == "spot" else "FUTURE"
        self.product = "SPOT" if kind == "spot" else "FUTURES"
        self.source = f"DERIBIT_{self.market_type}"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        url = f"{self.base_url}?{urlencode({'currency': 'any', 'kind': self.kind})}"
        return self.parse(await fetch_json(client, url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise ValueError(f"{self.source}: unsuccessful or malformed response")
        markets = []
        for row in payload["result"]:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: instrument is not an object")
            if str(row.get("kind") or "").lower() != self.kind:
                raise ValueError(f"{self.source}: unexpected instrument kind")
            base_symbol = required_text(
                row, "base_currency", source=self.source, record_kind="instrument"
            ).upper()
            quote_symbol = required_text(
                row, "quote_currency", source=self.source, record_kind="instrument"
            ).upper()
            venue_status = required_text(
                row, "state", source=self.source, record_kind="instrument"
            ).upper()
            if self.market_type == "SPOT":
                contract_type = "SPOT"
                settle_symbol = None
                expires_at = None
                direction = None
                venue_product = "SPOT"
            else:
                contract_type = (
                    "PERP"
                    if str(row.get("settlement_period") or "").lower() == "perpetual"
                    else "DATED"
                )
                settle_symbol = required_text(
                    row,
                    "settlement_currency",
                    source=self.source,
                    record_kind="instrument",
                ).upper()
                expires_at = self._expires_at(row.get("expiration_timestamp"), contract_type)
                instrument_type = str(row.get("instrument_type") or "").upper()
                direction = {"LINEAR": "LINEAR", "REVERSED": "INVERSE"}.get(
                    instrument_type
                ) or contract_direction(
                    market_type=self.market_type,
                    base_symbol=base_symbol,
                    quote_symbol=quote_symbol,
                    settle_symbol=settle_symbol,
                )
                venue_product = instrument_type or self.product
            multiplier = (
                str(row["contract_size"])
                if self.market_type == "FUTURE" and row.get("contract_size") is not None
                else None
            )
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    contract_type,
                    required_text(
                        row,
                        "instrument_name",
                        source=self.source,
                        record_kind="instrument",
                    ).upper(),
                    base_symbol,
                    quote_symbol,
                    settle_symbol,
                    contract_type,
                    normalize_status(venue_status),
                    row.get("is_active") is True and venue_status == "OPEN",
                    multiplier,
                    dict(row),
                    expires_at=expires_at,
                    venue_product=venue_product,
                    venue_status=venue_status,
                    contract_direction=direction,
                    contract_multiplier_unit=(
                        "QUOTE" if multiplier is not None and direction == "INVERSE"
                        else ("VENUE_BASE" if multiplier is not None else None)
                    ),
                    contract_value_currency=(
                        quote_symbol
                        if multiplier is not None and direction == "INVERSE"
                        else (
                            canonical_base_symbol(
                                base_symbol,
                                venue=self.venue,
                                market_type=self.market_type,
                            )
                            if multiplier is not None
                            else None
                        )
                    ),
                    open_interest_unit=("CONTRACT" if multiplier is not None else None),
                    contract_metadata_source=(
                        self.base_url if multiplier is not None else None
                    ),
                    contract_metadata_observed_at=(
                        observed_at if multiplier is not None else None
                    ),
                    contract_metadata_normalization_version=(
                        NORMALIZATION_VERSION if multiplier is not None else None
                    ),
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot

    def _expires_at(self, value: object, contract_type: str) -> str | None:
        if contract_type != "DATED":
            return None
        return strict_epoch_timestamp(
            value,
            milliseconds=True,
            source=self.source,
            field="expiration_timestamp",
            allow_missing=True,
        )


def deribit_connectors() -> list[DeribitConnector]:
    return [DeribitConnector(kind=value) for value in ("spot", "future")]
