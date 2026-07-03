from mdv.matching import (
    AliasHint,
    evaluate_alias_hint,
    normalize_asset_symbol,
    normalize_venue_asset_symbol,
    score_symbol_groups,
)


def test_normalize_futures_unit_prefix_without_breaking_numeric_asset_name():
    assert normalize_asset_symbol("1000PEPE", allow_unit_prefix=True).symbol == "PEPE"
    assert normalize_asset_symbol("1000PEPE", allow_unit_prefix=True).multiplier == 1000
    assert normalize_asset_symbol("1INCH", allow_unit_prefix=True).symbol == "1INCH"
    assert normalize_asset_symbol("1000PEPE", allow_unit_prefix=False).symbol == "1000PEPE"
    assert normalize_asset_symbol("币安人生", allow_unit_prefix=True).symbol == "币安人生"


def test_same_venue_spot_future_has_stronger_score_than_cross_venue_only():
    scores = score_symbol_groups(
        [
            {"normalized_symbol": "BTC", "venue": "BINANCE", "market_type": "SPOT"},
            {"normalized_symbol": "BTC", "venue": "BINANCE", "market_type": "FUTURE"},
            {"normalized_symbol": "ABC", "venue": "BINANCE", "market_type": "SPOT"},
            {"normalized_symbol": "ABC", "venue": "MEXC", "market_type": "SPOT"},
        ]
    )
    assert scores["BTC"] == ("SAME_VENUE_SPOT_FUTURE_SYMBOL", 0.97)
    assert scores["ABC"] == ("CROSS_VENUE_SYMBOL", 0.85)


def test_alias_hint_requires_independent_reference_evidence():
    hint = AliasHint(
        proposed_symbol="AMAT",
        rule="STOCK_SUFFIX_METADATA",
        display_symbol_match=True,
        classifications=frozenset({"EQUITY"}),
        reference_venues=frozenset({"REFERENCE_A"}),
    )
    accepted = evaluate_alias_hint(
        hint=hint,
        active_symbols_by_venue={"REFERENCE_A": {"AMAT"}},
        classified_symbols_by_venue={"REFERENCE_A": {"AMAT"}},
        required_classification="EQUITY",
    )
    proposed = evaluate_alias_hint(
        hint=hint,
        active_symbols_by_venue={},
        classified_symbols_by_venue={},
        required_classification="EQUITY",
    )

    assert accepted.proposed_symbol == "AMAT"
    assert accepted.decision == "ACCEPTED"
    assert accepted.score == 1.0
    assert proposed.decision == "PROPOSED"
    assert proposed.score == 0.6
    assert normalize_venue_asset_symbol("AMATSTOCK", venue="MEXC", market_type="FUTURE").symbol == "AMATSTOCK"
