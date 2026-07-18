from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from mdv.connectors.base import epoch_timestamp, fetch_json, market_availability, session_status, utc_now
from mdv.contract_metadata import NORMALIZATION_VERSION, positive_decimal, with_contract_evidence
from mdv.matching import normalize_venue_asset_symbol
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot, TradingSchedule
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


def coinbase_market_schedule(market: dict, raw: dict) -> TradingSchedule | None:
    details = raw.get("future_product_details")
    perpetual = details.get("perpetual_details") if isinstance(details, dict) else None
    if not (
        market.get("market_type") == "FUTURE"
        and isinstance(perpetual, dict)
        and str(perpetual.get("underlying_type") or "").upper() == "EQUITY"
    ):
        return None
    session = raw.get("fcm_trading_session_details")
    session = session if isinstance(session, dict) else {}
    session_state = str(session.get("session_state") or "").upper()
    if raw.get("trading_disabled") is True:
        current = "CLOSED"
    elif "UNDEFINED" not in session_state and session.get("is_session_open") is True:
        current = "OPEN"
    elif "UNDEFINED" not in session_state and session.get("is_session_open") is False:
        current = "CLOSED"
    else:
        current = session_status(str(market.get("status") or "UNKNOWN"))
    open_at = epoch_timestamp(session.get("open_time"), milliseconds=False)
    close_at = epoch_timestamp(session.get("close_time"), milliseconds=False)
    next_at = open_at if current == "CLOSED" else close_at
    return TradingSchedule(
        session_status=current,
        market_group="EQUITY",
        next_transition_at=next_at,
        next_transition_status=(
            "OPEN" if current == "CLOSED" and next_at
            else ("CLOSED" if current == "OPEN" and next_at else None)
        ),
    )


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
    international_url = "https://api.international.coinbase.com/api/v1/instruments"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        products, instruments = await asyncio.gather(
            self._fetch_products(client),
            fetch_json(client, self.international_url),
        )
        return self.parse(
            {"products": products},
            observed_at=utc_now(),
            instruments_payload=instruments,
        )

    async def _fetch_products(self, client: httpx.AsyncClient) -> list:
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
                return products
            cursor = str(pagination.get("next_cursor") or "").strip()
            if not cursor:
                raise ValueError(f"{self.source}: pagination has no next_cursor")
            query["cursor"] = cursor
        raise ValueError(f"{self.source}: pagination exceeded 100 pages")

    def parse(
        self,
        payload: object,
        *,
        observed_at: str,
        instruments_payload: object | None = None,
    ) -> MarketSnapshot:
        instruments = self._international_instruments(instruments_payload)
        snapshot = MarketSnapshot(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            observed_at,
            tuple(
                self._market(row, observed_at=observed_at, instruments=instruments)
                for row in self._products(payload)
            ),
        )
        snapshot.validate()
        return snapshot

    def _products(self, payload: object) -> list:
        products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(products, list):
            raise ValueError(f"{self.source}: response has no products array")
        return products

    def _international_instruments(
        self, payload: object | None
    ) -> tuple[dict[str, dict], dict[tuple[str, str], dict]]:
        if payload is None:
            return {}, {}
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: international response is not an array")
        by_symbol = {}
        by_assets = {}
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: international instrument is not an object")
            if str(row.get("type") or "").upper() != "PERP":
                continue
            symbol = _required(row, "symbol", source=self.source).upper()
            asset_key = (
                _required(row, "base_asset_name", source=self.source).upper(),
                _required(row, "quote_asset_name", source=self.source).upper(),
            )
            if symbol in by_symbol or asset_key in by_assets:
                raise ValueError(
                    f"{self.source}: duplicate international instrument {symbol}"
                )
            by_symbol[symbol] = row
            by_assets[asset_key] = row
        return by_symbol, by_assets

    def _market(
        self,
        row: object,
        *,
        observed_at: str,
        instruments: tuple[dict[str, dict], dict[tuple[str, str], dict]],
    ) -> MarketRecord:
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
        product_id = _required(row, "product_id", source=self.source)
        instrument = instruments[0].get(product_id.upper().removesuffix("-INTX"))
        if instrument is None:
            instrument = instruments[1].get((base_symbol, quote_symbol))
        normalized_root = normalize_venue_asset_symbol(
            base_symbol,
            venue=self.venue,
            market_type=self.market_type,
        )
        raw_brokerage_multiplier = positive_decimal(details.get("contract_size"))
        brokerage_multiplier = (
            positive_decimal(
                Decimal(raw_brokerage_multiplier) * normalized_root.multiplier
            )
            if raw_brokerage_multiplier is not None
            else None
        )
        international_multiplier = positive_decimal(
            instrument.get("base_asset_multiplier") if instrument else None
        )
        contract_metadata_reason = None
        if instrument is None:
            multiplier = brokerage_multiplier
            contract_metadata_reason = "COINBASE_INTX_INSTRUMENT_MISSING"
        elif international_multiplier is None:
            multiplier = None
            contract_metadata_reason = "COINBASE_INTX_MULTIPLIER_INVALID"
        elif (
            brokerage_multiplier is not None
            and brokerage_multiplier != international_multiplier
        ):
            multiplier = None
            contract_metadata_reason = "COINBASE_CONTRACT_MULTIPLIER_CONFLICT"
        else:
            multiplier = international_multiplier
        raw = with_contract_evidence(
            dict(row),
            {
                "source": self.international_url,
                "normalization_version": NORMALIZATION_VERSION,
                "brokerage_contract_size_raw": raw_brokerage_multiplier,
                "brokerage_contract_unit": base_symbol,
                "brokerage_contract_size": brokerage_multiplier,
                "international_instrument": instrument,
                "reason": contract_metadata_reason,
            },
        )
        schedule = coinbase_market_schedule(
            {"market_type": self.market_type, "status": status}, row
        )
        availability = market_availability(
            venue_status=venue_status,
            normalized_status=status,
            default_active=active,
            trading_schedule=schedule,
        )
        return MarketRecord(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            product_id,
            base_symbol,
            quote_symbol,
            quote_symbol,
            "PERP",
            availability.status,
            availability.active,
            multiplier,
            raw,
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
            trading_schedule=availability.trading_schedule,
            contract_multiplier_unit=(
                str(instrument["base_asset_name"]).upper()
                if multiplier is not None and instrument is not None
                else (normalized_root.symbol if multiplier is not None else None)
            ),
            contract_value_currency=(
                str(instrument["base_asset_name"]).upper()
                if multiplier is not None and instrument is not None
                else (normalized_root.symbol if multiplier is not None else None)
            ),
            open_interest_unit=("CONTRACT" if multiplier is not None else None),
            contract_metadata_reason=contract_metadata_reason,
            contract_metadata_source=self.international_url,
            contract_metadata_observed_at=observed_at,
            contract_metadata_normalization_version=NORMALIZATION_VERSION,
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
