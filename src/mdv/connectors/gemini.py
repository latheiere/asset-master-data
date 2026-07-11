from __future__ import annotations

import asyncio

import httpx

from mdv.connectors.base import fetch_json, utc_now
from mdv.models import MarketRecord, MarketSnapshot
from mdv.normalization import contract_direction, normalize_status


SYMBOLS_URL = "https://api.gemini.com/v1/symbols"
QUOTE_SUFFIXES = (
    "RLUSD", "GUSD", "USDC", "USDT", "SGD", "GBP", "EUR", "USD", "BTC", "FIL", "ETH", "SOL",
)


def _split_symbol(symbol: object, *, source: str) -> tuple[str, str]:
    value = str(symbol or "").strip().upper()
    if not value:
        raise ValueError(f"{source}: market has no symbol")
    value = value.removesuffix("PERP")
    for quote in QUOTE_SUFFIXES:
        if value.endswith(quote) and len(value) > len(quote):
            return value.removesuffix(quote), quote
    raise ValueError(f"{source}: cannot derive base/quote from {symbol!r}")


class GeminiSpotConnector:
    source = "GEMINI_SPOT"
    venue = "GEMINI"
    market_type = "SPOT"
    product = "SPOT"
    url = SYMBOLS_URL

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        return self.parse(await fetch_json(client, self.url), observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        markets = []
        for symbol in payload:
            if not isinstance(symbol, str):
                raise ValueError(f"{self.source}: symbol is not a string")
            if symbol.upper().endswith("PERP"):
                continue
            base_symbol, quote_symbol = _split_symbol(symbol, source=self.source)
            # Gemini's list endpoint is explicitly the available trading universe;
            # detailed status is only available via one request per symbol.
            markets.append(
                MarketRecord(
                    self.source, self.venue, self.market_type, self.product,
                    symbol.upper(), base_symbol, quote_symbol, None, "SPOT", "TRADING",
                    True, None, {"symbol": symbol, "source": "GEMINI_SYMBOLS"},
                    venue_product=self.product, venue_status="AVAILABLE",
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


class GeminiFutureConnector:
    source = "GEMINI_FUTURE"
    venue = "GEMINI"
    market_type = "FUTURE"
    product = "PERP"
    url = SYMBOLS_URL

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        symbols = await fetch_json(client, self.url)
        if not isinstance(symbols, list):
            raise ValueError(f"{self.source}: response is not an array")
        futures = [symbol for symbol in symbols if isinstance(symbol, str) and symbol.upper().endswith("PERP")]
        if not futures:
            raise ValueError(f"{self.source}: response has no perpetual symbols")
        semaphore = asyncio.Semaphore(8)

        async def details(symbol: str) -> object:
            async with semaphore:
                return await fetch_json(client, f"https://api.gemini.com/v1/symbols/details/{symbol}")

        payload = await asyncio.gather(*(details(symbol) for symbol in futures))
        return self.parse(payload, observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        markets = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: contract is not an object")
            if str(row.get("product_type") or "").lower() != "swap":
                raise ValueError(f"{self.source}: unexpected product_type")
            raw_symbol = str(row.get("symbol") or "").strip().upper()
            base_symbol = str(row.get("base_currency") or "").strip().upper()
            quote_symbol = str(row.get("quote_currency") or "").strip().upper()
            settle_symbol = str(row.get("contract_price_currency") or "").strip().upper()
            venue_status = str(row.get("status") or "").strip().upper()
            contract_style = str(row.get("contract_type") or "").strip().upper()
            if not all((raw_symbol, base_symbol, quote_symbol, settle_symbol)):
                raise ValueError(f"{self.source}: contract has missing required fields")
            if contract_style not in {"LINEAR", "INVERSE"}:
                raise ValueError(f"{self.source}: unknown contract_type {contract_style!r}")
            markets.append(
                MarketRecord(
                    self.source, self.venue, self.market_type, "PERP", raw_symbol,
                    base_symbol, quote_symbol, settle_symbol, "PERP",
                    normalize_status(venue_status), venue_status == "OPEN", None, dict(row),
                    venue_product=contract_style, venue_status=venue_status,
                    contract_direction=contract_direction(
                        market_type=self.market_type, base_symbol=base_symbol,
                        quote_symbol=quote_symbol, settle_symbol=settle_symbol,
                    ),
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


def gemini_connectors() -> list[GeminiSpotConnector | GeminiFutureConnector]:
    return [GeminiSpotConnector(), GeminiFutureConnector()]
