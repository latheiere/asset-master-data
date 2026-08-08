from __future__ import annotations

from urllib.parse import urlencode

import httpx

from mdv.connectors.base import (
    SingleEndpointConnector,
    fetch_json,
    required_text,
    strict_epoch_timestamp,
    utc_now,
)
from mdv.contract_metadata import NORMALIZATION_VERSION, canonical_base_symbol
from mdv.models import MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_status


CONTRACT_STATUSES = {
    0: ("DELISTING", "DELISTING"),
    1: ("LISTING", "TRADING"),
    2: ("PENDING_LISTING", "PRELAUNCH"),
    3: ("SUSPENSION", "PAUSED"),
    4: ("SUSPENDING_LISTING", "DELISTING"),
    5: ("IN_SETTLEMENT", "SETTLING"),
    6: ("DELIVERING", "DELIVERING"),
    7: ("SETTLEMENT_COMPLETED", "CLOSED"),
    8: ("DELIVERED", "CLOSED"),
    9: ("SUSPENDING_TRADE", "PAUSED"),
}


def _data(payload: object, *, source: str) -> list:
    if not isinstance(payload, dict):
        raise ValueError(f"{source}: malformed payload")
    if payload.get("status") not in (None, "ok") or payload.get("code") not in (None, 200):
        raise ValueError(f"{source}: unsuccessful response")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"{source}: response has no data array")
    return rows


class HtxSpotConnector(SingleEndpointConnector[MarketSnapshot]):
    source = "HTX_SPOT"
    venue = "HTX"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api.huobi.pro/v2/settings/common/symbols"

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _data(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: symbol is not an object")
            venue_status = required_text(
                row, "state", source=self.source, record_kind="instrument"
            ).upper()
            active = venue_status == "ONLINE" and row.get("te") is True
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    required_text(
                        row, "sc", source=self.source, record_kind="instrument"
                    ),
                    required_text(
                        row, "bcdn", source=self.source, record_kind="instrument"
                    ).upper(),
                    required_text(
                        row, "qcdn", source=self.source, record_kind="instrument"
                    ).upper(),
                    None,
                    "SPOT",
                    normalize_status(venue_status) if active else "CLOSED",
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


class HtxFutureConnector:
    venue = "HTX"
    market_type = "FUTURE"

    def __init__(
        self,
        *,
        source: str,
        product: str,
        url: str,
        linear: bool,
        dated: bool,
        business_type: str | None = None,
    ):
        self.source = source
        self.product = product
        self.url = url
        self.linear = linear
        self.dated = dated
        self.business_type = business_type

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        url = self.url
        if self.business_type:
            url = f"{url}?{urlencode({'business_type': self.business_type})}"
        return self.parse(await fetch_json(client, url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        markets = []
        for row in _data(payload, source=self.source):
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: contract is not an object")
            if self.business_type and row.get("business_type") not in (None, self.business_type):
                raise ValueError(f"{self.source}: unexpected business_type")
            try:
                code = int(row.get("contract_status"))
                venue_status, status = CONTRACT_STATUSES[code]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"{self.source}: unknown contract_status {row.get('contract_status')!r}"
                ) from exc
            base_symbol = required_text(
                row, "symbol", source=self.source, record_kind="instrument"
            ).upper()
            if self.linear:
                pair = required_text(
                    row, "pair", source=self.source, record_kind="instrument"
                ).split("-")
                if len(pair) != 2:
                    raise ValueError(f"{self.source}: invalid pair {row.get('pair')!r}")
                quote_symbol = pair[1].upper()
                settle_symbol = quote_symbol
            else:
                quote_symbol = "USD"
                settle_symbol = base_symbol
            raw_contract_type = str(row.get("contract_type") or "swap").strip().lower()
            contract_type = "DATED" if self.dated else "PERP"
            if self.dated and raw_contract_type == "swap":
                raise ValueError(f"{self.source}: dated contract has swap contract_type")
            multiplier = (
                str(row["contract_size"])
                if row.get("contract_size") is not None
                else None
            )
            direction = contract_direction(
                market_type=self.market_type,
                base_symbol=base_symbol,
                quote_symbol=quote_symbol,
                settle_symbol=settle_symbol,
            )
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    contract_type,
                    required_text(
                        row, "contract_code", source=self.source, record_kind="instrument"
                    ),
                    base_symbol,
                    quote_symbol,
                    settle_symbol,
                    contract_type,
                    status,
                    code == 1,
                    multiplier,
                    dict(row),
                    expires_at=self._expires_at(row.get("delivery_time")),
                    venue_product=self.product,
                    venue_status=venue_status,
                    contract_direction=direction,
                    expiry_cycle=(
                        {
                            "this_week": "W",
                            "next_week": "BW",
                            "quarter": "Q",
                            "next_quarter": "BQ",
                        }.get(raw_contract_type)
                        if self.dated
                        else None
                    ),
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
                    contract_metadata_source=(self.url if multiplier is not None else None),
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

    def _expires_at(self, value: object) -> str | None:
        if not self.dated:
            return None
        return strict_epoch_timestamp(
            value,
            milliseconds=True,
            source=self.source,
            field="delivery_time",
            allow_missing=True,
        )


def htx_connectors() -> list[HtxSpotConnector | HtxFutureConnector]:
    linear_url = "https://api.hbdm.com/linear-swap-api/v1/swap_contract_info"
    return [
        HtxSpotConnector(),
        HtxFutureConnector(
            source="HTX_USDT_SWAP_FUTURE",
            product="USDT-M SWAP",
            url=linear_url,
            linear=True,
            dated=False,
            business_type="swap",
        ),
        HtxFutureConnector(
            source="HTX_USDT_FUTURE",
            product="USDT-M FUTURES",
            url=linear_url,
            linear=True,
            dated=True,
            business_type="futures",
        ),
        HtxFutureConnector(
            source="HTX_COIN_SWAP_FUTURE",
            product="COIN-M SWAP",
            url="https://api.hbdm.com/swap-api/v1/swap_contract_info",
            linear=False,
            dated=False,
        ),
        HtxFutureConnector(
            source="HTX_COIN_FUTURE",
            product="COIN-M FUTURES",
            url="https://api.hbdm.com/api/v1/contract_contract_info",
            linear=False,
            dated=True,
        ),
    ]
