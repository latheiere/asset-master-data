from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_contract_type, normalize_product, normalize_status


def _required(row: dict, name: str, *, source: str) -> str:
    value = str(row.get(name) or "").strip().upper()
    if not value:
        raise ValueError(f"{source}: record has no {name}")
    return value


def _xt_result(payload: object, *, source: str) -> object:
    if not isinstance(payload, dict) or payload.get("rc", payload.get("returnCode")) != 0:
        message = (
            payload.get("mc") or payload.get("msgInfo")
            if isinstance(payload, dict)
            else None
        )
        raise ValueError(f"{source}: unsuccessful response: {message or 'malformed payload'}")
    return payload.get("result")


def _asset_tags(row: dict, field: str, *, source: str) -> dict:
    raw = dict(row)
    values = row.get(field)
    if isinstance(values, list):
        tags = [
            {
                "provider": "XT",
                "tag": str(value).strip().upper(),
                "raw_tag": str(value).strip(),
                "source": source,
            }
            for value in values
            if str(value).strip()
        ]
        if tags:
            raw["_metadata"] = {"ASSET_TAGS": tags}
    return raw


class XtSpotConnector:
    source = "XT_SPOT"
    venue = "XT"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://sapi.xt.com/v4/public/symbol"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        result = _xt_result(payload, source=self.source)
        rows = result.get("symbols") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"{self.source}: response has no result.symbols array")
        markets = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: symbol is not an object")
            venue_status = str(row.get("state") or "UNKNOWN").strip().upper()
            active = venue_status == "ONLINE" and row.get("tradingEnabled") is True
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=self.product,
                    raw_symbol=_required(row, "symbol", source=self.source),
                    base_symbol=_required(row, "baseCurrency", source=self.source),
                    quote_symbol=_required(row, "quoteCurrency", source=self.source),
                    settle_symbol=None,
                    contract_type="SPOT",
                    status=normalize_status(venue_status),
                    active=active,
                    contract_multiplier=None,
                    raw=_asset_tags(row, "tags", source="XT_SPOT_SYMBOL"),
                    venue_product=str(row.get("type") or "SPOT").strip().upper(),
                    venue_status=venue_status,
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


class XtFutureConnector:
    source = "XT_FUTURE"
    venue = "XT"
    market_type = "FUTURE"
    product = "U_BASED"
    url = "https://fapi.xt.com/future/market/v3/public/symbol/list"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        result = _xt_result(payload, source=self.source)
        rows = result.get("symbols") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"{self.source}: response has no result.symbols array")
        markets = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: contract is not an object")
            raw_contract_type = str(row.get("contractType") or "").strip().upper()
            contract_type = normalize_contract_type(raw_contract_type, market_type="FUTURE")
            base_symbol = _required(row, "baseCoin", source=self.source)
            quote_symbol = _required(row, "quoteCoin", source=self.source)
            completed = row.get("deliveryCompletion") is True
            trade_enabled = row.get("tradeSwitch") is True
            open_enabled = row.get("openSwitch") is True
            if completed:
                venue_status = "DELIVERED"
            elif row.get("inPreMarket") is True:
                venue_status = "PRELAUNCH"
            elif trade_enabled and open_enabled:
                venue_status = "TRADING"
            elif trade_enabled:
                venue_status = "CLOSE_ONLY"
            else:
                venue_status = "PAUSED"
            expires_at = self._expires_at(row.get("deliveryDate"), contract_type)
            markets.append(
                MarketRecord(
                    source=self.source,
                    venue=self.venue,
                    market_type=self.market_type,
                    product=normalize_product(self.market_type, contract_type),
                    raw_symbol=_required(row, "symbol", source=self.source),
                    base_symbol=base_symbol,
                    quote_symbol=quote_symbol,
                    settle_symbol=quote_symbol,
                    contract_type=contract_type,
                    status=normalize_status(venue_status),
                    active=trade_enabled and not completed,
                    contract_multiplier=(
                        str(row["contractSize"])
                        if row.get("contractSize") is not None
                        else None
                    ),
                    raw=_asset_tags(row, "labels", source="XT_FUTURE_SYMBOL"),
                    expires_at=expires_at,
                    max_market_order_size=(
                        str(row["maxMarketOrderQty"])
                        if row.get("maxMarketOrderQty") is not None
                        else None
                    ),
                    venue_product=str(row.get("productType") or self.product)
                    .strip()
                    .upper(),
                    venue_status=venue_status,
                    contract_direction=contract_direction(
                        market_type=self.market_type,
                        base_symbol=base_symbol,
                        quote_symbol=quote_symbol,
                        settle_symbol=quote_symbol,
                    ),
                    expiry_cycle={"CURRENT_QUARTER": "Q", "NEXT_QUARTER": "BQ"}.get(
                        raw_contract_type
                    ),
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot

    def _expires_at(self, value: object, contract_type: str) -> str | None:
        if contract_type != "DATED" or value in (None, "", 0, "0"):
            return None
        try:
            return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{self.source}: invalid deliveryDate {value!r}") from exc


class XtCrossMarginConnector:
    source = "XT_CROSS_MARGIN"
    venue = "XT"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"
    url = "https://sapi.xt.com/v4/public/lever/symbol"

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        rows = _xt_result(payload, source=self.source)
        if not isinstance(rows, list):
            raise ValueError(f"{self.source}: response result is not an array")
        evidence: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: margin pair is not an object")
            pair = _required(row, "symbol", source=self.source)
            for role, asset_field, limit_field in (
                ("BUY", "buyCurrency", "maxLoanAmountBuy"),
                ("SELL", "sellCurrency", "maxLoanAmountSell"),
            ):
                asset = _required(row, asset_field, source=self.source)
                evidence[asset].append(
                    {
                        "pair": pair,
                        "asset_role": role,
                        "max_loan_amount": row.get(limit_field),
                        "daily_interest_rate": row.get("dailyInterestRate"),
                        "raw": row,
                    }
                )
        records = []
        for asset, items in sorted(evidence.items()):
            rates = tuple(
                {
                    "tier": "REGULAR",
                    "regular_user": True,
                    "rate_type": "VARIABLE",
                    "rate_unit": "DAILY",
                    "pair": item["pair"],
                    "value": str(item["daily_interest_rate"]),
                }
                for item in items
                if item["daily_interest_rate"] not in (None, "")
            )
            records.append(
                FinancingRecord(
                    self.source,
                    self.venue,
                    self.product,
                    "BORROWABLE",
                    asset,
                    True,
                    "ENABLED",
                    "REGULAR",
                    rates,
                    (),
                    {
                        "pairs": [
                            {
                                "pair": item["pair"],
                                "max_loan_amount": item["max_loan_amount"],
                            }
                            for item in items
                        ]
                    },
                    tuple(sorted({item["pair"] for item in items})),
                    {"evidence_granularity": "PAIR", "pairs": items},
                )
            )
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, tuple(records)
        )
        snapshot.validate()
        return snapshot


class XtCryptoLoanConnector:
    source = "XT_CRYPTO_LOAN"
    venue = "XT"
    market_type = "FINANCING"
    product = "CRYPTO_LOAN"
    loan_url = "https://sapi.xt.com/v4/public/finance/loan/product/loan-currency"
    collateral_url = "https://sapi.xt.com/v4/public/finance/loan/product/pledge-currency"
    page_limit = 100

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        loan_pages, collateral_pages = await asyncio.gather(
            self._fetch_pages(client, self.loan_url),
            self._fetch_pages(client, self.collateral_url),
        )
        return self.parse_pages(loan_pages, collateral_pages, observed_at=utc_now())

    async def _fetch_pages(self, client: httpx.AsyncClient, url: str) -> list[object]:
        pages = []
        for page in range(1, 101):
            payload = await fetch_json(
                client, f"{url}?{urlencode({'page': page, 'limit': self.page_limit})}"
            )
            pages.append(payload)
            result = _xt_result(payload, source=self.source)
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise ValueError(f"{self.source}: response has no result.items array")
            if result.get("hasNext") is not True:
                return pages
        raise ValueError(f"{self.source}: pagination exceeded 100 pages")

    def parse_pages(
        self,
        loan_pages: list[object],
        collateral_pages: list[object],
        *,
        observed_at: str,
    ) -> FinancingSnapshot:
        loans = self._items(loan_pages)
        collateral = self._items(collateral_pages)
        records = []
        for row in loans:
            asset = _required(row, "currency", source=self.source)
            rates = []
            terms = []
            for label, days, available_field, rate_field in (
                ("DEMAND", None, "demandAprAvailable", "demandApr"),
                ("FIXED", 7, "sevenDayAprAvailable", "sevenDayApr"),
                ("FIXED", 30, "thirtyDayAprAvailable", "thirtyDayApr"),
                ("FIXED", 90, "ninetyDayAprAvailable", "ninetyDayApr"),
            ):
                enabled = row.get(available_field) is True
                terms.append({"type": label, "term_days": days, "enabled": enabled})
                if enabled and row.get(rate_field) not in (None, ""):
                    rate = {
                        "tier": "REGULAR",
                        "regular_user": True,
                        "rate_type": label,
                        "rate_unit": "APR_PERCENT",
                        "value": str(row[rate_field]),
                    }
                    if days is not None:
                        rate["term_days"] = days
                    rates.append(rate)
            eligible = any(term["enabled"] for term in terms)
            records.append(
                FinancingRecord(
                    self.source,
                    self.venue,
                    self.product,
                    "BORROWABLE",
                    asset,
                    eligible,
                    "ENABLED" if eligible else "DISABLED",
                    "REGULAR",
                    tuple(rates),
                    tuple(terms),
                    {
                        "min": row.get("singleMin"),
                        "individual_cap": row.get("individualCap"),
                    },
                    (),
                    dict(row),
                )
            )
        for row in collateral:
            cap = row.get("individualCap")
            eligible = cap is None or float(cap) > 0
            records.append(
                FinancingRecord(
                    self.source,
                    self.venue,
                    self.product,
                    "COLLATERAL",
                    _required(row, "currency", source=self.source),
                    eligible,
                    "ENABLED" if eligible else "DISABLED",
                    "REGULAR",
                    (),
                    (),
                    {
                        "individual_cap": cap,
                        "initial_pledge_rate": row.get("initialPledgeRate"),
                        "warning_pledge_rate": row.get("earlyWarningPledgeRate"),
                        "liquidation_pledge_rate": row.get("forceLiquidatePledgeRate"),
                    },
                    (),
                    dict(row),
                )
            )
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, tuple(records)
        )
        snapshot.validate()
        return snapshot

    def _items(self, pages: list[object]) -> list[dict]:
        if not pages:
            raise ValueError(f"{self.source}: response has no pages")
        items = []
        for payload in pages:
            result = _xt_result(payload, source=self.source)
            rows = result.get("items") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise ValueError(f"{self.source}: response has no result.items array")
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"{self.source}: product is not an object")
            items.extend(rows)
        return items


def xt_connectors() -> list[XtSpotConnector | XtFutureConnector]:
    return [XtSpotConnector(), XtFutureConnector()]


def xt_financing_connectors() -> list[XtCrossMarginConnector | XtCryptoLoanConnector]:
    return [XtCrossMarginConnector(), XtCryptoLoanConnector()]
