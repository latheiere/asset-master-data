from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field


MATCHER_VERSION = "evidence-v2"
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


@dataclass(frozen=True)
class AliasHint:
    proposed_symbol: str
    rule: str
    display_symbol_match: bool
    classifications: frozenset[str]
    reference_venues: frozenset[str]
    source_evidence: dict = field(default_factory=dict)


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


def evaluate_alias_hint(
    *,
    hint: AliasHint,
    active_symbols_by_venue: dict[str, set[str]],
    classified_symbols_by_venue: dict[str, set[str]],
    required_classification: str,
) -> AliasCandidate:
    proposed = normalize_asset_symbol(
        hint.proposed_symbol, allow_unit_prefix=False
    ).symbol
    classification = required_classification.upper()
    reference_venues = sorted(hint.reference_venues)
    checks = {
        "display_symbol_match": hint.display_symbol_match,
        "asset_classification": classification in hint.classifications,
        "reference_venue_declared": bool(reference_venues),
        "active_reference_market_exists": any(
            proposed in active_symbols_by_venue.get(venue, set())
            for venue in reference_venues
        ),
        "reference_classification": any(
            proposed in classified_symbols_by_venue.get(venue, set())
            for venue in reference_venues
        ),
    }
    score = sum(checks.values()) / len(checks)
    evidence = {
        "proposed_symbol": proposed,
        "classifications": sorted(hint.classifications),
        "reference_venues": reference_venues,
        "source_evidence": hint.source_evidence,
        "checks": checks,
    }
    return AliasCandidate(
        proposed_symbol=proposed,
        rule=hint.rule,
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
