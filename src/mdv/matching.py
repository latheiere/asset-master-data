from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass


MATCHER_VERSION = "evidence-v1"
UNIT_PREFIXES = ("1000000", "10000", "1000")


@dataclass(frozen=True)
class NormalizedSymbol:
    symbol: str
    multiplier: int
    method: str


@dataclass(frozen=True)
class AliasCandidate:
    proposed_symbol: str
    rule: str
    decision: str
    score: float
    evidence: dict


def normalize_asset_symbol(symbol: str, *, allow_unit_prefix: bool) -> NormalizedSymbol:
    clean = "".join(character for character in (symbol or "").upper() if character.isalnum())
    if not clean:
        raise ValueError("asset symbol is empty after normalization")
    if allow_unit_prefix:
        for prefix in UNIT_PREFIXES:
            suffix = clean[len(prefix) :] if clean.startswith(prefix) else ""
            if suffix and suffix[0].isalpha() and len(suffix) >= 2:
                return NormalizedSymbol(suffix, int(prefix), "UNIT_PREFIX_SYMBOL")
    return NormalizedSymbol(clean, 1, "EXACT_SYMBOL")


def normalize_venue_asset_symbol(
    symbol: str,
    *,
    venue: str,
    market_type: str,
) -> NormalizedSymbol:
    """Normalize known unit conventions without changing raw market truth."""
    del venue, market_type
    return normalize_asset_symbol(symbol, allow_unit_prefix=True)


def evaluate_mexc_stock_alias(
    *,
    symbol: str,
    venue: str,
    market_type: str,
    raw: dict,
    active_binance_future_symbols: set[str],
    binance_equity_symbols: set[str],
) -> AliasCandidate | None:
    clean = normalize_asset_symbol(symbol, allow_unit_prefix=False).symbol
    if venue.upper() != "MEXC" or market_type.upper() != "FUTURE" or not clean.endswith("STOCK"):
        return None
    proposed = clean[:-5]
    if not proposed:
        return None

    display_name = str(raw.get("displayNameEn") or "").upper()
    concepts = [str(value) for value in (raw.get("conceptPlate") or [])]
    origins = [str(value).upper() for value in (raw.get("indexOrigin") or [])]
    checks = {
        "display_symbol_match": display_name.startswith(f"{proposed}_"),
        "stock_classification": any("stock" in value.lower() for value in concepts),
        "binance_index_origin": any(value in {"BINANCE_FUTURE", "BINANCETICKER"} for value in origins),
        "active_binance_future_exists": proposed in active_binance_future_symbols,
        "binance_equity_classification": proposed in binance_equity_symbols,
    }
    score = sum(checks.values()) / len(checks)
    evidence = {
        "raw_symbol": clean,
        "proposed_symbol": proposed,
        "display_name": display_name,
        "concepts": concepts,
        "index_origins": origins,
        "checks": checks,
    }
    return AliasCandidate(
        proposed_symbol=proposed,
        rule="MEXC_STOCK_METADATA",
        decision="ACCEPTED" if all(checks.values()) else "PROPOSED",
        score=score,
        evidence=evidence,
    )


def stable_asset_id(canonical_symbol: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://mdv.local/assets/{canonical_symbol}"))


def score_symbol_groups(rows: list[dict]) -> dict[str, tuple[str, float]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["normalized_symbol"]].append(row)

    result: dict[str, tuple[str, float]] = {}
    for symbol, members in grouped.items():
        venue_types: dict[str, set[str]] = defaultdict(set)
        venues = set()
        for member in members:
            venues.add(member["venue"])
            venue_types[member["venue"]].add(member["market_type"])
        same_venue_spot_future = any(types >= {"SPOT", "FUTURE"} for types in venue_types.values())
        if same_venue_spot_future:
            result[symbol] = ("SAME_VENUE_SPOT_FUTURE_SYMBOL", 0.97)
        elif len(venues) > 1:
            result[symbol] = ("CROSS_VENUE_SYMBOL", 0.85)
        else:
            result[symbol] = ("SYMBOL_ONLY", 0.65)
    return result
