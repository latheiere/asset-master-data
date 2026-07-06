from __future__ import annotations

import asyncio

import httpx

from mdv.connectors.base import post_json, utc_now
from mdv.models import MarketRecord, MarketSnapshot


INFO_URL = "https://api.hyperliquid.xyz/info"


def _spot_meta(payload: object, *, source: str) -> dict:
    if (
        not isinstance(payload, list)
        or not payload
        or not isinstance(payload[0], dict)
    ):
        raise ValueError(f"{source}: response has no spot metadata object")
    meta = payload[0]
    if not isinstance(meta.get("tokens"), list) or not isinstance(meta.get("universe"), list):
        raise ValueError(f"{source}: spot metadata has no tokens/universe arrays")
    return meta


def _tokens(meta: dict, *, source: str) -> dict[int, dict]:
    result = {}
    for row in meta["tokens"]:
        if not isinstance(row, dict) or not isinstance(row.get("index"), int) or not row.get("name"):
            raise ValueError(f"{source}: token metadata is malformed")
        result[row["index"]] = row
    return result


class HyperliquidSpotConnector:
    source = "HYPERLIQUID_SPOT"
    venue = "HYPERLIQUID"
    market_type = "SPOT"
    product = "SPOT"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        payload = await post_json(client, INFO_URL, {"type": "spotMetaAndAssetCtxs"})
        return self.parse(payload, observed_at=utc_now())

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        meta = _spot_meta(payload, source=self.source)
        tokens = _tokens(meta, source=self.source)
        markets = []
        for row in meta["universe"]:
            if not isinstance(row, dict) or not isinstance(row.get("tokens"), list):
                raise ValueError(f"{self.source}: market metadata is malformed")
            indexes = row["tokens"]
            if len(indexes) != 2 or any(not isinstance(value, int) for value in indexes):
                raise ValueError(f"{self.source}: market has invalid token indexes")
            try:
                base, quote = (tokens[index] for index in indexes)
            except KeyError as exc:
                raise ValueError(f"{self.source}: market references an unknown token") from exc
            raw_symbol = str(row.get("name") or "").strip()
            if not raw_symbol:
                raise ValueError(f"{self.source}: market has no name")
            active = row.get("isDelisted") is not True
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    raw_symbol,
                    str(base["name"]).upper(),
                    str(quote["name"]).upper(),
                    None,
                    "SPOT",
                    "TRADING" if active else "DELISTING",
                    active,
                    None,
                    dict(row),
                    venue_product=self.product,
                    venue_status="TRADING" if active else "DELISTED",
                )
            )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


class HyperliquidPerpConnector:
    source = "HYPERLIQUID_PERP_FUTURE"
    venue = "HYPERLIQUID"
    market_type = "FUTURE"
    product = "PERP"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        perps, spot = await asyncio.gather(
            post_json(client, INFO_URL, {"type": "allPerpMetas"}),
            post_json(client, INFO_URL, {"type": "spotMetaAndAssetCtxs"}),
        )
        return self.parse(perps, spot_payload=spot, observed_at=utc_now())

    def parse(
        self,
        payload: object,
        *,
        spot_payload: object,
        observed_at: str,
    ) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")
        tokens = _tokens(_spot_meta(spot_payload, source=self.source), source=self.source)
        markets = []
        for dex_index, meta in enumerate(payload):
            if not isinstance(meta, dict) or not isinstance(meta.get("universe"), list):
                raise ValueError(f"{self.source}: perp metadata is malformed")
            collateral_index = meta.get("collateralToken")
            if not isinstance(collateral_index, int) or collateral_index not in tokens:
                raise ValueError(f"{self.source}: perp metadata has invalid collateralToken")
            quote_symbol = str(tokens[collateral_index]["name"]).upper()
            for row in meta["universe"]:
                if not isinstance(row, dict):
                    raise ValueError(f"{self.source}: perp market is not an object")
                raw_symbol = str(row.get("name") or "").strip()
                if not raw_symbol:
                    raise ValueError(f"{self.source}: perp market has no name")
                dex_name, separator, base_name = raw_symbol.partition(":")
                if not separator:
                    dex_name, base_name = "HYPERLIQUID", raw_symbol
                if not base_name:
                    raise ValueError(f"{self.source}: perp market has no base symbol")
                active = row.get("isDelisted") is not True
                raw = dict(row)
                raw["_metadata"] = {
                    "HYPERLIQUID_DEX": dex_name,
                    "HYPERLIQUID_DEX_INDEX": dex_index,
                    "HYPERLIQUID_COLLATERAL_TOKEN_INDEX": collateral_index,
                }
                markets.append(
                    MarketRecord(
                        self.source,
                        self.venue,
                        self.market_type,
                        self.product,
                        raw_symbol,
                        base_name.upper(),
                        quote_symbol,
                        quote_symbol,
                        "PERP",
                        "TRADING" if active else "DELISTING",
                        active,
                        None,
                        raw,
                        venue_product=("PERP" if dex_name == "HYPERLIQUID" else f"HIP-3:{dex_name}"),
                        venue_status="TRADING" if active else "DELISTED",
                        contract_direction="LINEAR",
                    )
                )
        snapshot = MarketSnapshot(
            self.source, self.venue, self.market_type, self.product, observed_at, tuple(markets)
        )
        snapshot.validate()
        return snapshot


def hyperliquid_connectors() -> list[HyperliquidSpotConnector | HyperliquidPerpConnector]:
    return [HyperliquidSpotConnector(), HyperliquidPerpConnector()]
