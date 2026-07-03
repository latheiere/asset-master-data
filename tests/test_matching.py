from mdv.matching import evaluate_mexc_stock_alias, normalize_asset_symbol, normalize_venue_asset_symbol, score_symbol_groups


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


def test_mexc_stock_alias_requires_independent_metadata_evidence():
    accepted = evaluate_mexc_stock_alias(
        symbol="AMATSTOCK",
        venue="MEXC",
        market_type="FUTURE",
        raw={
            "displayNameEn": "AMAT_USDT PERPETUAL",
            "conceptPlate": ["mc-trade-zone-Stock", "mc-trade-zone-tradfi"],
            "indexOrigin": ["BINANCE_FUTURE", "BINANCETICKER"],
        },
        active_binance_future_symbols={"AMAT"},
        binance_equity_symbols={"AMAT"},
    )
    proposed = evaluate_mexc_stock_alias(
        symbol="NOTSTOCK",
        venue="MEXC",
        market_type="FUTURE",
        raw={},
        active_binance_future_symbols=set(),
        binance_equity_symbols=set(),
    )

    assert accepted is not None
    assert accepted.proposed_symbol == "AMAT"
    assert accepted.decision == "ACCEPTED"
    assert accepted.score == 1.0
    assert proposed is not None
    assert proposed.decision == "PROPOSED"
    assert proposed.score == 0.0
    assert normalize_venue_asset_symbol("AMATSTOCK", venue="MEXC", market_type="FUTURE").symbol == "AMATSTOCK"
