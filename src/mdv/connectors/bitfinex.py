from __future__ import annotations

import asyncio

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.contract_metadata import NORMALIZATION_VERSION, positive_decimal, with_contract_evidence
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot


CONFIG_URL = (
    "https://api-pub.bitfinex.com/v2/conf/"
    "pub:list:pair:exchange,pub:list:pair:futures,pub:list:currency"
)
MARGIN_CONFIG_URL = (
    "https://api-pub.bitfinex.com/v2/conf/"
    "pub:list:pair:margin,pub:list:currency"
)
FUTURES_INFO_URL = "https://api-pub.bitfinex.com/v2/conf/pub:info:pair:futures"
DERIVATIVES_STATUS_URL = "https://api-pub.bitfinex.com/v2/status/deriv?keys=ALL"
PRODUCT_DESCRIPTION_URL = "https://www.bitfinex.com/legal/derivative/product/"


def _lists(payload: object, *, source: str) -> tuple[list, list, tuple[str, ...]]:
    if not isinstance(payload, list) or len(payload) != 3:
        raise ValueError(f"{source}: malformed configuration response")
    spot, futures, currencies = payload
    if not all(isinstance(value, list) for value in (spot, futures, currencies)):
        raise ValueError(f"{source}: configuration response has no pair arrays")
    currency_codes = tuple(
        sorted(
            (str(value).strip().upper() for value in currencies if str(value).strip()),
            key=len,
            reverse=True,
        )
    )
    if not currency_codes:
        raise ValueError(f"{source}: configuration response has no currencies")
    return spot, futures, currency_codes


def _spot_symbols(raw_symbol: object, currencies: tuple[str, ...], *, source: str) -> tuple[str, str]:
    raw = str(raw_symbol or "").strip().upper()
    if not raw:
        raise ValueError(f"{source}: pair is empty")
    if ":" in raw:
        base, quote = raw.split(":", 1)
        if base and quote:
            return base, quote
    for quote in currencies:
        if raw.endswith(quote) and len(raw) > len(quote):
            return raw.removesuffix(quote), quote
    raise ValueError(f"{source}: cannot derive base/quote from {raw!r}")


def _future_symbols(raw_symbol: object, *, source: str) -> tuple[str, str]:
    raw = str(raw_symbol or "").strip().upper()
    parts = raw.split(":", 1)
    if len(parts) != 2 or not all(part.endswith("F0") for part in parts):
        raise ValueError(f"{source}: invalid derivative symbol {raw!r}")
    base = parts[0].removesuffix("F0")
    settle = parts[1].removesuffix("F0")
    if not base or not settle:
        raise ValueError(f"{source}: invalid derivative symbol {raw!r}")
    return base, settle


class BitfinexConnector:
    venue = "BITFINEX"
    url = CONFIG_URL

    def __init__(self, *, market_type: str):
        self.market_type = market_type
        self.product = "SPOT" if market_type == "SPOT" else "PERP"
        self.source = f"BITFINEX_{market_type}"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        if self.market_type == "SPOT":
            return self.parse(await fetch_json(client, self.url), observed_at=utc_now())
        config, product_info, status = await asyncio.gather(
            fetch_json(client, self.url),
            fetch_json(client, FUTURES_INFO_URL),
            fetch_json(client, DERIVATIVES_STATUS_URL),
        )
        return self.parse(
            config,
            observed_at=utc_now(),
            product_info_payload=product_info,
            status_payload=status,
        )

    def parse(
        self,
        payload: object,
        *,
        observed_at: str,
        product_info_payload: object | None = None,
        status_payload: object | None = None,
    ) -> MarketSnapshot:
        spot, futures, currencies = _lists(payload, source=self.source)
        rows = spot if self.market_type == "SPOT" else futures
        product_info = (
            self._product_info(product_info_payload)
            if self.market_type == "FUTURE" and product_info_payload is not None
            else {}
        )
        statuses = (
            self._derivative_status(status_payload)
            if self.market_type == "FUTURE" and status_payload is not None
            else {}
        )
        markets = []
        for row in rows:
            if not isinstance(row, str):
                raise ValueError(f"{self.source}: pair is not a string")
            raw_symbol = row.upper()
            if self.market_type == "SPOT":
                base_symbol, quote_symbol = _spot_symbols(row, currencies, source=self.source)
                settle_symbol = None
                contract_type = "SPOT"
                direction = None
                venue_product = "EXCHANGE"
            else:
                base_symbol, settle_symbol = _future_symbols(row, source=self.source)
                quote_symbol = settle_symbol
                contract_type = "PERP"
                direction = "INVERSE" if settle_symbol == base_symbol else "LINEAR"
                venue_product = "F0"
            contract_multiplier = None
            contract_multiplier_unit = None
            contract_value_currency = None
            open_interest_unit = None
            contract_metadata_reason = None
            raw = {"symbol": row, "source": "BITFINEX_VALID_PAIRS"}
            if self.market_type == "FUTURE":
                info = product_info.get(raw_symbol)
                status_row = statuses.get(raw_symbol)
                if info is None:
                    contract_metadata_reason = "BITFINEX_PRODUCT_SPEC_MISSING"
                elif status_row is None:
                    contract_metadata_reason = "BITFINEX_DERIVATIVES_STATUS_MISSING"
                else:
                    # Bitfinex product descriptions define order/position size in
                    # units of each named underlying; the status API publishes OI
                    # as contracts. Resolve per symbol only after both official
                    # catalogs identify the same instrument.
                    contract_multiplier = "1"
                    contract_multiplier_unit = base_symbol
                    contract_value_currency = base_symbol
                    open_interest_unit = "CONTRACT"
                raw = with_contract_evidence(
                    raw,
                    {
                        "source": PRODUCT_DESCRIPTION_URL,
                        "product_info_source": FUTURES_INFO_URL,
                        "status_source": DERIVATIVES_STATUS_URL,
                        "normalization_version": NORMALIZATION_VERSION,
                        "product_info": info,
                        "status": status_row,
                        "minimum_order_size": (
                            positive_decimal(info[3])
                            if isinstance(info, list) and len(info) > 3
                            else None
                        ),
                        "reason": contract_metadata_reason,
                    },
                )
            # Bitfinex publishes this as its valid-pairs universe, which is the
            # complete tradable catalog but does not carry a per-pair status field.
            markets.append(
                MarketRecord(
                    self.source, self.venue, self.market_type, self.product,
                    raw_symbol, base_symbol, quote_symbol, settle_symbol, contract_type,
                    "TRADING", True, contract_multiplier,
                    raw,
                    venue_product=venue_product, venue_status="VALID", contract_direction=direction,
                    contract_multiplier_unit=contract_multiplier_unit,
                    contract_value_currency=contract_value_currency,
                    open_interest_unit=open_interest_unit,
                    contract_metadata_reason=contract_metadata_reason,
                    contract_metadata_source=(
                        PRODUCT_DESCRIPTION_URL if self.market_type == "FUTURE" else None
                    ),
                    contract_metadata_observed_at=(
                        observed_at if self.market_type == "FUTURE" else None
                    ),
                    contract_metadata_normalization_version=(
                        NORMALIZATION_VERSION if self.market_type == "FUTURE" else None
                    ),
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot

    def _product_info(self, payload: object) -> dict[str, list]:
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], list):
            raise ValueError(f"{self.source}: malformed derivative product info")
        result = {}
        for row in payload[0]:
            if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], list):
                raise ValueError(f"{self.source}: malformed derivative product row")
            symbol = str(row[0] or "").strip().upper()
            if not symbol or symbol in result:
                raise ValueError(f"{self.source}: invalid derivative product symbol")
            result[symbol] = row[1]
        return result

    def _derivative_status(self, payload: object) -> dict[str, list]:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: malformed derivative status response")
        result = {}
        for row in payload:
            if not isinstance(row, list) or len(row) <= 18:
                raise ValueError(f"{self.source}: malformed derivative status row")
            symbol = str(row[0] or "").strip().upper().removeprefix("T")
            if not symbol or symbol in result:
                raise ValueError(f"{self.source}: invalid derivative status symbol")
            result[symbol] = row
        return result


class BitfinexCrossMarginConnector:
    source = "BITFINEX_CROSS_MARGIN"
    venue = "BITFINEX"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"
    url = MARGIN_CONFIG_URL

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError(f"{self.source}: malformed configuration response")
        pairs, currencies = payload
        if not isinstance(pairs, list) or not isinstance(currencies, list):
            raise ValueError(f"{self.source}: response has no pair/currency arrays")
        currency_codes = tuple(
            sorted(
                (str(value).strip().upper() for value in currencies if str(value).strip()),
                key=len,
                reverse=True,
            )
        )
        if not currency_codes:
            raise ValueError(f"{self.source}: response has no currencies")
        evidence: dict[str, list[dict]] = {}
        for pair in pairs:
            if not isinstance(pair, str):
                raise ValueError(f"{self.source}: pair is not a string")
            base_symbol, quote_symbol = _spot_symbols(
                pair, currency_codes, source=self.source
            )
            for role, asset in (("BASE", base_symbol), ("QUOTE", quote_symbol)):
                evidence.setdefault(asset, []).append(
                    {"pair": pair.upper(), "asset_role": role, "raw": pair}
                )
        records = tuple(
            FinancingRecord(
                self.source, self.venue, self.product, "BORROWABLE", asset,
                True, "ENABLED", None, (), (), {},
                tuple(sorted({item["pair"] for item in pairs})),
                {"evidence_granularity": "PAIR", "pairs": pairs},
            )
            for asset, pairs in sorted(evidence.items())
        )
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, records
        )
        snapshot.validate()
        return snapshot


def bitfinex_connectors() -> list[BitfinexConnector]:
    return [BitfinexConnector(market_type=value) for value in ("SPOT", "FUTURE")]


def bitfinex_financing_connectors() -> list[BitfinexCrossMarginConnector]:
    return [BitfinexCrossMarginConnector()]
