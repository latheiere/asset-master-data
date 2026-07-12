from __future__ import annotations

import asyncio
from collections.abc import Iterable

import httpx

from mdv.connectors.base import post_json, utc_now
from mdv.models import MarketRecord, MarketSnapshot


INFO_URL = "https://api.hyperliquid.xyz/info"
EVM_RPC_URL = "https://rpc.hyperliquid.xyz/evm"
ERC20_NAME_SELECTOR = "0x06fdde03"
ERC20_SYMBOL_SELECTOR = "0x95d89b41"
EVM_METADATA_BATCH_SIZE = 50


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


def _decode_abi_string(value: object) -> str | None:
    """Decode a standard ABI dynamic string without trusting contract output."""
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        encoded = bytes.fromhex(value[2:])
    except ValueError:
        return None
    if len(encoded) < 64:
        return None
    offset = int.from_bytes(encoded[:32], "big")
    if offset + 32 > len(encoded):
        return None
    length = int.from_bytes(encoded[offset : offset + 32], "big")
    end = offset + 32 + length
    if not length or length > 128 or end > len(encoded):
        return None
    try:
        result = encoded[offset + 32 : end].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return result if result and result.isprintable() else None


def _valid_erc20_symbol(value: object) -> str | None:
    if not isinstance(value, str) or not (1 <= len(value) <= 32):
        return None
    if not any(character.isalnum() for character in value):
        return None
    if not all(character.isalnum() or character in ".-_" for character in value):
        return None
    return value


def _evm_contract_address(token: dict) -> str | None:
    contract = token.get("evmContract")
    address = contract.get("address") if isinstance(contract, dict) else None
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        return None
    try:
        int(address[2:], 16)
    except ValueError:
        return None
    return address.lower()


async def _erc20_metadata(
    client: httpx.AsyncClient, tokens: Iterable[dict]
) -> dict[int, dict]:
    """Best-effort ERC-20 enrichment for linked HyperCore spot tokens.

    This optional data must never invalidate a complete Hyperliquid snapshot:
    HyperCore metadata remains the authoritative discovery payload.
    """
    contracts = [
        (int(token["index"]), address)
        for token in tokens
        if isinstance(token.get("index"), int)
        if (address := _evm_contract_address(token)) is not None
    ]
    metadata: dict[int, dict] = {}
    for offset in range(0, len(contracts), EVM_METADATA_BATCH_SIZE):
        batch = contracts[offset : offset + EVM_METADATA_BATCH_SIZE]
        requests = [
            {
                "jsonrpc": "2.0",
                "id": f"{index}:name",
                "method": "eth_call",
                "params": [{"to": address, "data": ERC20_NAME_SELECTOR}, "latest"],
            }
            for index, address in batch
        ] + [
            {
                "jsonrpc": "2.0",
                "id": f"{index}:symbol",
                "method": "eth_call",
                "params": [{"to": address, "data": ERC20_SYMBOL_SELECTOR}, "latest"],
            }
            for index, address in batch
        ]
        try:
            response = await client.post(EVM_RPC_URL, json=requests)
            response.raise_for_status()
            replies = response.json()
        except (httpx.HTTPError, ValueError):
            continue
        if not isinstance(replies, list):
            continue
        values: dict[str, str] = {}
        for reply in replies:
            if not isinstance(reply, dict) or not isinstance(reply.get("id"), str):
                continue
            decoded = _decode_abi_string(reply.get("result"))
            if decoded is not None:
                values[reply["id"]] = decoded
        for index, address in batch:
            symbol = _valid_erc20_symbol(values.get(f"{index}:symbol"))
            if symbol is None:
                continue
            metadata[index] = {
                "contract_address": address,
                "name": values.get(f"{index}:name"),
                "symbol": symbol,
            }
    return metadata


class HyperliquidSpotConnector:
    source = "HYPERLIQUID_SPOT"
    venue = "HYPERLIQUID"
    market_type = "SPOT"
    product = "SPOT"

    async def fetch(self, client: httpx.AsyncClient) -> MarketSnapshot:
        payload = await post_json(client, INFO_URL, {"type": "spotMetaAndAssetCtxs"})
        meta = _spot_meta(payload, source=self.source)
        tokens = _tokens(meta, source=self.source)
        base_token_indexes = {
            row["tokens"][0]
            for row in meta["universe"]
            if isinstance(row, dict)
            and isinstance(row.get("tokens"), list)
            and len(row["tokens"]) == 2
            and isinstance(row["tokens"][0], int)
        }
        evm_metadata = await _erc20_metadata(
            client,
            (tokens[index] for index in base_token_indexes if index in tokens),
        )
        return self.parse(
            payload, observed_at=utc_now(), evm_metadata=evm_metadata
        )

    def parse(
        self,
        payload: object,
        *,
        observed_at: str,
        evm_metadata: dict[int, dict] | None = None,
    ) -> MarketSnapshot:
        meta = _spot_meta(payload, source=self.source)
        tokens = _tokens(meta, source=self.source)
        evm_metadata = evm_metadata or {}
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
            token_metadata = evm_metadata.get(int(base["index"]))
            base_symbol = str(base["name"])
            raw = dict(row)
            if token_metadata is not None:
                # A linked ERC-20 symbol is a provider-published identity claim,
                # not an alias inferred from the market ticker.
                base_symbol = str(token_metadata["symbol"])
                raw["_metadata"] = {
                    "HYPERLIQUID_EVM_TOKEN": {
                        "core_token_index": base["index"],
                        "core_token_name": base["name"],
                        **token_metadata,
                    }
                }
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    raw_symbol,
                    base_symbol,
                    str(quote["name"]).upper(),
                    None,
                    "SPOT",
                    "TRADING" if active else "DELISTING",
                    active,
                    None,
                    raw,
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
