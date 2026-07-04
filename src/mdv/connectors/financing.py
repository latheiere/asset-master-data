from __future__ import annotations

import asyncio
from collections import defaultdict

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import FinancingRecord, FinancingSnapshot


def _symbol(value: object, *, source: str, field: str) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError(f"{source}: record has no {field}")
    return symbol


def _bitget_data(payload: object, *, source: str) -> object:
    if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
        message = payload.get("msg") if isinstance(payload, dict) else None
        raise ValueError(f"{source}: unsuccessful response: {message or 'malformed payload'}")
    return payload.get("data")


class BinanceCrossMarginPublicConnector:
    source = "BINANCE_CROSS_MARGIN_PUBLIC"
    venue = "BINANCE"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"
    url = "https://api.binance.com/api/v3/exchangeInfo?showPermissionSets=true"

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list):
            raise ValueError(f"{self.source}: response has no symbols array")
        evidence: dict[str, list[dict]] = defaultdict(list)
        for row in symbols:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: symbol is not an object")
            permission_sets = row.get("permissionSets")
            permissions = row.get("permissions")
            has_margin = (
                isinstance(permission_sets, list)
                and any(
                    isinstance(group, list) and "MARGIN" in group
                    for group in permission_sets
                )
            ) or (isinstance(permissions, list) and "MARGIN" in permissions)
            if not has_margin:
                continue
            pair = _symbol(row.get("symbol"), source=self.source, field="symbol")
            for role, field in (("BASE", "baseAsset"), ("QUOTE", "quoteAsset")):
                asset = _symbol(row.get(field), source=self.source, field=field)
                evidence[asset].append({
                    "pair": pair,
                    "asset_role": role,
                    "pair_active": str(row.get("status") or "").upper() == "TRADING",
                    "raw": row,
                })
        records = tuple(
            FinancingRecord(
                self.source, self.venue, self.product, "BORROWABLE", asset,
                any(item["pair_active"] for item in items),
                "ENABLED", None, (), (), {},
                tuple(sorted({item["pair"] for item in items})),
                {
                    "evidence_granularity": "PAIR",
                    "pairs": items,
                },
            )
            for asset, items in sorted(evidence.items())
        )
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, records
        )
        snapshot.validate()
        return snapshot


class BybitCrossMarginConnector:
    source = "BYBIT_CROSS_MARGIN"
    venue = "BYBIT"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"
    url = "https://api.bybit.com/v5/spot-margin-trade/data"

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise ValueError(f"{self.source}: unsuccessful response")
        result = payload.get("result")
        groups = result.get("vipCoinList") if isinstance(result, dict) else None
        if not isinstance(groups, list) or not groups:
            raise ValueError(f"{self.source}: response has no vipCoinList array")
        by_asset: dict[str, list[dict]] = defaultdict(list)
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("list"), list):
                raise ValueError(f"{self.source}: malformed tier")
            tier = str(group.get("vipLevel") or "").strip()
            for row in group["list"]:
                if not isinstance(row, dict):
                    raise ValueError(f"{self.source}: coin is not an object")
                item = dict(row)
                item["vipLevel"] = tier
                by_asset[_symbol(row.get("currency"), source=self.source, field="currency")].append(item)
        records = []
        for asset, tiers in sorted(by_asset.items()):
            regular = next((row for row in tiers if row["vipLevel"] == "No VIP"), tiers[0])
            rates = tuple(
                {
                    "tier": row["vipLevel"],
                    "regular_user": row is regular,
                    "rate_type": "VARIABLE",
                    "rate_unit": "HOURLY",
                    "value": str(row.get("hourlyBorrowRate") or ""),
                }
                for row in tiers
                if str(row.get("hourlyBorrowRate") or "")
            )
            eligible = regular.get("borrowable") is True
            records.append(FinancingRecord(
                self.source, self.venue, self.product, "BORROWABLE", asset,
                eligible, "ENABLED" if eligible else "DISABLED", "No VIP",
                rates, (), {}, (), {"tiers": tiers},
            ))
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, tuple(records)
        )
        snapshot.validate()
        return snapshot


class BybitCryptoLoanConnector:
    source = "BYBIT_CRYPTO_LOAN"
    venue = "BYBIT"
    market_type = "FINANCING"
    product = "CRYPTO_LOAN"
    loan_url = "https://api.bybit.com/v5/crypto-loan-common/loanable-data"
    collateral_url = "https://api.bybit.com/v5/crypto-loan/collateral-data"

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        loan, collateral = await asyncio.gather(
            fetch_json(client, self.loan_url),
            fetch_json(client, self.collateral_url),
        )
        return self.parse(loan, collateral, observed_at=utc_now())

    def parse(
        self, loan_payload: object, collateral_payload: object, *, observed_at: str
    ) -> FinancingSnapshot:
        loans = self._result_list(loan_payload, "list")
        collateral_groups = self._result_list(collateral_payload, "vipCoinList")
        records = []
        for row in loans:
            asset = _symbol(row.get("currency"), source=self.source, field="currency")
            rates = []
            flexible_rate = str(row.get("flexibleAnnualizedInterestRate") or "")
            if flexible_rate:
                rates.append({
                    "tier": str(row.get("vipLevel") or "VIP0"),
                    "regular_user": True,
                    "rate_type": "FLEXIBLE",
                    "rate_unit": "APR",
                    "value": flexible_rate,
                })
            for days in (7, 14, 30, 60, 90, 180):
                value = str(row.get(f"annualizedInterestRate{days}D") or "")
                if value:
                    rates.append({
                        "tier": str(row.get("vipLevel") or "VIP0"),
                        "regular_user": True,
                        "rate_type": "FIXED",
                        "rate_unit": "APR",
                        "term_days": days,
                        "value": value,
                    })
            eligible = row.get("flexibleBorrowable") is True or row.get("fixedBorrowable") is True
            records.append(FinancingRecord(
                self.source, self.venue, self.product, "BORROWABLE", asset,
                eligible, "ENABLED" if eligible else "DISABLED",
                str(row.get("vipLevel") or "VIP0"), tuple(rates),
                tuple(
                    {"type": name, "enabled": row.get(field) is True}
                    for name, field in (("FLEXIBLE", "flexibleBorrowable"), ("FIXED", "fixedBorrowable"))
                ),
                {
                    "min_flexible": row.get("minFlexibleBorrowingAmount"),
                    "min_fixed": row.get("minFixedBorrowingAmount"),
                    "platform_max": row.get("maxBorrowingAmount"),
                },
                (), dict(row),
            ))
        for group in collateral_groups:
            if not isinstance(group, dict) or not isinstance(group.get("list"), list):
                raise ValueError(f"{self.source}: malformed collateral tier")
            tier = str(group.get("vipLevel") or "VIP0")
            if tier not in {"VIP0", "No VIP"} and len(collateral_groups) > 1:
                continue
            for row in group["list"]:
                if not isinstance(row, dict):
                    raise ValueError(f"{self.source}: collateral coin is not an object")
                asset = _symbol(row.get("currency"), source=self.source, field="currency")
                records.append(FinancingRecord(
                    self.source, self.venue, self.product, "COLLATERAL", asset,
                    True, "ENABLED", tier, (), (), {}, (), dict(row),
                ))
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, tuple(records)
        )
        snapshot.validate()
        return snapshot

    def _result_list(self, payload: object, field: str) -> list:
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise ValueError(f"{self.source}: unsuccessful response")
        result = payload.get("result")
        value = result.get(field) if isinstance(result, dict) else None
        if not isinstance(value, list):
            raise ValueError(f"{self.source}: response has no result.{field} array")
        return value


class BitgetCrossMarginConnector:
    source = "BITGET_CROSS_MARGIN"
    venue = "BITGET"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"
    url = "https://api.bitget.com/api/v2/margin/currencies"

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        rows = _bitget_data(payload, source=self.source)
        if not isinstance(rows, list):
            raise ValueError(f"{self.source}: response has no data array")
        evidence: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: margin pair is not an object")
            if row.get("isCrossBorrowable") is not True:
                continue
            pair = _symbol(row.get("symbol"), source=self.source, field="symbol")
            for role, field in (("BASE", "baseCoin"), ("QUOTE", "quoteCoin")):
                asset = _symbol(row.get(field), source=self.source, field=field)
                evidence[asset].append({"pair": pair, "asset_role": role, "raw": row})
        records = tuple(
            FinancingRecord(
                self.source, self.venue, self.product, "BORROWABLE", asset,
                True, "ENABLED", None, (), (), {},
                tuple(sorted({item["pair"] for item in items})),
                {"evidence_granularity": "PAIR", "pairs": items},
            )
            for asset, items in sorted(evidence.items())
        )
        snapshot = FinancingSnapshot(self.source, self.venue, self.product, observed_at, records)
        snapshot.validate()
        return snapshot


class BitgetCryptoLoanConnector:
    source = "BITGET_CRYPTO_LOAN"
    venue = "BITGET"
    market_type = "FINANCING"
    product = "CRYPTO_LOAN"
    url = "https://api.bitget.com/api/v2/earn/loan/public/coinInfos"

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        data = _bitget_data(payload, source=self.source)
        if not isinstance(data, dict):
            raise ValueError(f"{self.source}: response has no data object")
        loans = data.get("loanInfos")
        collateral = data.get("pledgeInfos")
        if not isinstance(loans, list) or not isinstance(collateral, list):
            raise ValueError(f"{self.source}: response has no loanInfos/pledgeInfos arrays")
        records = []
        for row in loans:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: loan coin is not an object")
            rates = tuple(
                {
                    "tier": "REGULAR",
                    "regular_user": True,
                    "rate_type": "FIXED",
                    "rate_unit": "APR",
                    "term_days": days,
                    "value": str(row[field]),
                }
                for days, field in ((7, "rate7D"), (30, "rate30D"))
                if row.get(field) not in (None, "")
            )
            records.append(FinancingRecord(
                self.source, self.venue, self.product, "BORROWABLE",
                _symbol(row.get("coin"), source=self.source, field="coin"),
                True, "ENABLED", "REGULAR", rates,
                tuple({"type": "FIXED", "term_days": days} for days in (7, 30)),
                {"min": row.get("min"), "platform_max": row.get("max")},
                (), dict(row),
            ))
        for row in collateral:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: collateral coin is not an object")
            records.append(FinancingRecord(
                self.source, self.venue, self.product, "COLLATERAL",
                _symbol(row.get("coin"), source=self.source, field="coin"),
                True, "ENABLED", "REGULAR", (), (), {}, (), dict(row),
            ))
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, tuple(records)
        )
        snapshot.validate()
        return snapshot


class GateCrossMarginConnector:
    source = "GATE_CROSS_MARGIN"
    venue = "GATE"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"
    url = "https://api.gateio.ws/api/v4/margin/cross/currencies"

    async def fetch(self, client: httpx.AsyncClient) -> FinancingSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> FinancingSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        records = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: currency is not an object")
            eligible = row.get("loanable") is True and row.get("status") == 1
            records.append(FinancingRecord(
                self.source, self.venue, self.product, "BORROWABLE",
                _symbol(row.get("name"), source=self.source, field="name"),
                eligible, "ENABLED" if eligible else "DISABLED", None,
                tuple({
                    "tier": "REGULAR",
                    "regular_user": True,
                    "rate_type": "VARIABLE",
                    "rate_unit": "VENUE_NATIVE",
                    "value": str(row["rate"]),
                } for _ in (0,) if row.get("rate") not in (None, "")),
                (),
                {
                    "min": row.get("min_borrow_amount"),
                    "platform_max_usdt": row.get("total_max_borrow_amount"),
                },
                (), dict(row),
            ))
        snapshot = FinancingSnapshot(
            self.source, self.venue, self.product, observed_at, tuple(records)
        )
        snapshot.validate()
        return snapshot


def bybit_financing_connectors() -> list:
    return [BybitCrossMarginConnector(), BybitCryptoLoanConnector()]


def binance_financing_connectors() -> list:
    return [BinanceCrossMarginPublicConnector()]


def bitget_financing_connectors() -> list:
    return [BitgetCrossMarginConnector(), BitgetCryptoLoanConnector()]


def gate_financing_connectors() -> list:
    return [GateCrossMarginConnector()]


def financing_connectors() -> list:
    return [
        *binance_financing_connectors(),
        *bybit_financing_connectors(),
        *bitget_financing_connectors(),
        *gate_financing_connectors(),
    ]
