from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from mdv.connectors.base import Connector
from mdv.connectors.binance import binance_connectors
from mdv.connectors.bitget import bitget_connectors
from mdv.connectors.bybit import bybit_connectors
from mdv.connectors.coinbase import coinbase_connectors, coinbase_financing_connectors
from mdv.connectors.gate import gate_connectors
from mdv.connectors.htx import htx_connectors
from mdv.connectors.hyperliquid import hyperliquid_connectors
from mdv.connectors.financing import (
    binance_financing_connectors,
    bitget_financing_connectors,
    bybit_financing_connectors,
    gate_financing_connectors,
    kucoin_financing_connectors,
)
from mdv.connectors.mexc import mexc_connectors
from mdv.connectors.kucoin import kucoin_connectors
from mdv.connectors.okx import okx_connectors
from mdv.connectors.whitebit import whitebit_connectors
from mdv.connectors.xt import xt_connectors, xt_financing_connectors
from mdv.matching import AliasHint, normalize_asset_symbol


ConnectorFactory = Callable[[], list[Connector]]
TradeUrlBuilder = Callable[[dict], str | None]


@dataclass(frozen=True)
class MarketMetadata:
    classifications: frozenset[str]
    tags: tuple[dict, ...]
    alias_hints: tuple[AliasHint, ...]


@dataclass(frozen=True)
class VenueIntegration:
    venue: str
    connector_factory: ConnectorFactory
    trade_url_builder: TradeUrlBuilder
    financing_factory: ConnectorFactory | None = None


def _encoded_market(market: dict) -> tuple[str, str, str, str, str]:
    return (
        quote(str(market.get("raw_symbol") or ""), safe="_-"),
        quote(str(market.get("base_symbol") or ""), safe="_-"),
        quote(str(market.get("quote_symbol") or ""), safe="_-"),
        quote(str(market.get("settle_symbol") or ""), safe="_-"),
        str(market.get("venue_product") or market.get("product") or "").upper(),
    )


def _binance_trade_url(market: dict) -> str | None:
    raw, base, quote_symbol, _, venue_product = _encoded_market(market)
    if market.get("market_type") == "FUTURE":
        section = "delivery" if venue_product == "COIN-M" else "futures"
        return f"https://www.binance.com/en/{section}/{raw}"
    if market.get("market_type") == "SPOT":
        return f"https://www.binance.com/en/trade/{base}_{quote_symbol}?type=spot"
    return None


def _bitget_trade_url(market: dict) -> str | None:
    raw, _, _, _, venue_product = _encoded_market(market)
    if market.get("market_type") == "FUTURE":
        section = {"USDT-M": "usdt", "USDC-M": "usdc", "COIN-M": "coin"}.get(
            venue_product
        )
        return f"https://www.bitget.com/futures/{section}/{raw}" if section else None
    if market.get("market_type") == "SPOT":
        return f"https://www.bitget.com/spot/{raw}"
    return None


def _bybit_trade_url(market: dict) -> str | None:
    raw, base, quote_symbol, settle, venue_product = _encoded_market(market)
    if market.get("market_type") == "FUTURE":
        section = "inverse" if venue_product == "INVERSE" else (
            "usdc" if settle.upper() == "USDC" else "usdt"
        )
        return f"https://www.bybit.com/trade/{section}/{raw}"
    if market.get("market_type") == "SPOT":
        return f"https://www.bybit.com/en/trade/spot/{base}/{quote_symbol}"
    return None


def _gate_trade_url(market: dict) -> str | None:
    raw, _, _, settle, venue_product = _encoded_market(market)
    if market.get("market_type") == "FUTURE":
        if venue_product.endswith("-DELIVERY"):
            return f"https://www.gate.com/en/futures-delivery/{settle.lower()}/{raw}"
        return f"https://www.gate.com/futures/{settle}/{raw}"
    if market.get("market_type") == "SPOT":
        return f"https://www.gate.com/trade/{raw}"
    return None


def _mexc_trade_url(market: dict) -> str | None:
    raw, base, quote_symbol, _, _ = _encoded_market(market)
    if market.get("market_type") == "FUTURE":
        return f"https://www.mexc.com/futures/{raw}"
    if market.get("market_type") == "SPOT":
        return f"https://www.mexc.com/exchange/{base}_{quote_symbol}"
    return None


def _coinbase_trade_url(market: dict) -> str | None:
    raw, _, _, _, _ = _encoded_market(market)
    if market.get("market_type") == "SPOT":
        return f"https://www.coinbase.com/advanced-trade/spot/{raw}"
    if market.get("market_type") == "FUTURE":
        return f"https://www.coinbase.com/advanced-trade/perpetuals/{raw}"
    return None


def _xt_trade_url(market: dict) -> str | None:
    raw, _, _, _, _ = _encoded_market(market)
    if market.get("market_type") == "FUTURE":
        return f"https://www.xt.com/en/futures/trade/{raw.lower()}"
    if market.get("market_type") == "SPOT":
        return f"https://www.xt.com/en/trade/{raw.lower()}"
    return None


def _okx_trade_url(market: dict) -> str | None:
    raw, _, _, _, venue_product = _encoded_market(market)
    section = {
        "SPOT": "trade-spot",
        "SWAP": "trade-swap",
        "FUTURES": "trade-futures",
    }.get(venue_product)
    return f"https://www.okx.com/{section}/{raw.lower()}" if section else None


def _hyperliquid_trade_url(market: dict) -> str | None:
    raw, _, _, _, _ = _encoded_market(market)
    return f"https://app.hyperliquid.xyz/trade/{raw}" if raw else None


def _htx_trade_url(market: dict) -> str | None:
    raw, base, quote_symbol, _, venue_product = _encoded_market(market)
    if market.get("market_type") == "SPOT":
        return f"https://www.htx.com/trade/{base.lower()}_{quote_symbol.lower()}?type=spot"
    if market.get("market_type") != "FUTURE":
        return None
    if venue_product.startswith("USDT-M"):
        contract_type = "futures" if venue_product.endswith("FUTURES") else "swap"
        return (
            "https://www.htx.com/futures/linear_swap/exchange"
            f"#contract_code={raw}&contract_type={contract_type}&type=cross"
        )
    section = "coin_future" if venue_product.endswith("FUTURES") else "coin_swap"
    return f"https://www.htx.com/futures/{section}/exchange#contract_code={raw}"


def _kucoin_trade_url(market: dict) -> str | None:
    raw, _, _, _, _ = _encoded_market(market)
    if market.get("market_type") == "FUTURE":
        return f"https://www.kucoin.com/futures/trade/{raw}"
    if market.get("market_type") == "SPOT":
        return f"https://www.kucoin.com/trade/{raw}"
    return None


def _whitebit_trade_url(market: dict) -> str | None:
    raw, _, _, _, _ = _encoded_market(market)
    return f"https://whitebit.com/trade/{raw}" if raw else None


INTEGRATIONS = {
    integration.venue: integration
    for integration in (
        VenueIntegration(
            "BINANCE", binance_connectors, _binance_trade_url,
            binance_financing_connectors,
        ),
        VenueIntegration(
            "BITGET", bitget_connectors, _bitget_trade_url,
            bitget_financing_connectors,
        ),
        VenueIntegration(
            "BYBIT", bybit_connectors, _bybit_trade_url,
            bybit_financing_connectors,
        ),
        VenueIntegration(
            "GATE", gate_connectors, _gate_trade_url,
            gate_financing_connectors,
        ),
        VenueIntegration(
            "COINBASE", coinbase_connectors, _coinbase_trade_url,
            coinbase_financing_connectors,
        ),
        VenueIntegration("MEXC", mexc_connectors, _mexc_trade_url),
        VenueIntegration("OKX", okx_connectors, _okx_trade_url),
        VenueIntegration(
            "HYPERLIQUID", hyperliquid_connectors, _hyperliquid_trade_url
        ),
        VenueIntegration("HTX", htx_connectors, _htx_trade_url),
        VenueIntegration(
            "KUCOIN", kucoin_connectors, _kucoin_trade_url, kucoin_financing_connectors,
        ),
        VenueIntegration("WHITEBIT", whitebit_connectors, _whitebit_trade_url),
        VenueIntegration("XT", xt_connectors, _xt_trade_url, xt_financing_connectors),
    )
}


def default_connectors() -> list[Connector]:
    return [
        connector
        for integration in INTEGRATIONS.values()
        for connector in integration.connector_factory()
    ]


def default_collection_connectors() -> list[Connector]:
    return [
        connector
        for integration in INTEGRATIONS.values()
        for connector in (
            integration.connector_factory()
            + (
                integration.financing_factory()
                if integration.financing_factory is not None
                else []
            )
        )
    ]


def supported_venues() -> tuple[str, ...]:
    return tuple(sorted(INTEGRATIONS))


def market_trade_url(market: dict) -> str | None:
    integration = INTEGRATIONS.get(str(market.get("venue") or "").upper())
    return integration.trade_url_builder(market) if integration else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return []
    return [str(item) for item in value]


def market_metadata(market: dict, raw: dict) -> MarketMetadata:
    """Normalize provider evidence before generic matching and projection."""
    embedded = raw.get("_metadata") if isinstance(raw.get("_metadata"), dict) else {}
    classifications = {
        str(value).strip().upper()
        for value in _strings(embedded.get("ASSET_CLASSIFICATIONS"))
        if str(value).strip()
    }
    concepts = _strings(raw.get("conceptPlate"))
    future_details = raw.get("future_product_details")
    perpetual_details = (
        future_details.get("perpetual_details") if isinstance(future_details, dict) else None
    )
    if (
        any("stock" in value.lower() for value in concepts)
        or str(raw.get("underlyingType") or "").upper() == "EQUITY"
        or str(raw.get("contractType") or "").upper() == "TRADIFI_PERPETUAL"
        or str(raw.get("symbolType") or "").upper() == "STOCK"
        or (
            isinstance(perpetual_details, dict)
            and str(perpetual_details.get("underlying_type") or "").upper() == "EQUITY"
        )
    ):
        classifications.add("EQUITY")

    tags = [
        item for item in (embedded.get("ASSET_TAGS") or [])
        if isinstance(item, dict)
    ]
    product = embedded.get("BINANCE_PRODUCT")
    if isinstance(product, dict):
        tags.extend(
            {
                "provider": "BINANCE",
                "tag": raw_tag,
                "raw_tag": raw_tag,
                "source": "BINANCE_PRODUCT",
                "product_symbol": product.get("s"),
            }
            for raw_tag in product.get("tags") or []
        )

    aliases = []
    for item in embedded.get("IDENTITY_ALIASES") or []:
        if not isinstance(item, dict) or not item.get("proposed_symbol"):
            continue
        aliases.append(
            AliasHint(
                proposed_symbol=str(item["proposed_symbol"]),
                rule=str(item.get("rule") or "PROVIDER_ALIAS_METADATA"),
                display_symbol_match=bool(item.get("display_symbol_match")),
                classifications=frozenset(classifications),
                reference_venues=frozenset(
                    str(value).upper() for value in item.get("reference_venues") or []
                ),
                source_evidence=(
                    item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
                ),
            )
        )

    clean = normalize_asset_symbol(
        str(market.get("base_symbol") or ""), allow_unit_prefix=False
    ).symbol
    if not aliases and market.get("market_type") == "FUTURE" and clean.endswith("STOCK"):
        proposed = clean.removesuffix("STOCK")
        display_name = str(raw.get("displayNameEn") or "").upper()
        origins = [value.upper() for value in _strings(raw.get("indexOrigin"))]
        reference_venues = {
            venue
            for venue in supported_venues()
            if any(origin.startswith(venue) for origin in origins)
        }
        if proposed:
            aliases.append(
                AliasHint(
                    proposed_symbol=proposed,
                    rule="STOCK_SUFFIX_METADATA",
                    display_symbol_match=display_name.startswith(f"{proposed}_"),
                    classifications=frozenset(classifications),
                    reference_venues=frozenset(reference_venues),
                    source_evidence={
                        "raw_symbol": clean,
                        "display_name": display_name,
                        "concepts": concepts,
                        "index_origins": origins,
                    },
                )
            )

    return MarketMetadata(
        classifications=frozenset(classifications),
        tags=tuple(tags),
        alias_hints=tuple(aliases),
    )
