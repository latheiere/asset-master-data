import base64
import asyncio
import threading

import httpx
import yaml
from fastapi.testclient import TestClient

from mdv import __version__
from mdv.auth import Entitlements, hash_password
from mdv.config import Settings
from mdv.db import SQLiteStore
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot
from mdv.web import create_app


def test_mdv_future_view_filters_and_renders_markets(tmp_path, monkeypatch):
    revision = "a" * 40
    monkeypatch.setenv("MDV_GIT_SHA", revision)
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    market = MarketRecord(
        source="MEXC_FUTURE",
        venue="MEXC",
        market_type="FUTURE",
        product="PERP",
        raw_symbol="BTC_USDT",
        base_symbol="BTC",
        quote_symbol="USDT",
        settle_symbol="USDT",
        contract_type="PERP",
        status="ENABLED",
        active=True,
        contract_multiplier="0.0001",
        raw={"symbol": "BTC_USDT", "state": 0},
        max_market_order_size="5000000",
    )
    store.apply_snapshot(
        MarketSnapshot(
            source=market.source,
            venue=market.venue,
            market_type=market.market_type,
            product=market.product,
            observed_at="2026-07-03T00:00:00+00:00",
            markets=(market,),
        )
    )
    store.apply_financing_snapshot(FinancingSnapshot(
        source="BYBIT_CRYPTO_LOAN",
        venue="BYBIT",
        product="CRYPTO_LOAN",
        observed_at="2026-07-05T00:00:00+00:00",
        records=(FinancingRecord(
            source="BYBIT_CRYPTO_LOAN", venue="BYBIT", product="CRYPTO_LOAN",
            asset_role="BORROWABLE", raw_asset_symbol="SOL", eligible=True,
            status="ENABLED", regular_user_tier="VIP0",
            rates=({"tier": "VIP0", "regular_user": True, "rate_type": "FLEXIBLE", "rate_unit": "APR", "value": "0.04"},),
            terms=({"type": "FLEXIBLE", "enabled": True},), limits={},
            pair_symbols=(), raw={"currency": "SOL"},
        ),),
    ))
    binance_market = MarketRecord(
        source="BINANCE_FUTURE",
        venue="BINANCE",
        market_type="FUTURE",
        product="USD-M",
        raw_symbol="ETHUSDT",
        base_symbol="ETH",
        quote_symbol="USDT",
        settle_symbol="USDT",
        contract_type="PERP",
        status="TRADING",
        active=True,
        contract_multiplier=None,
        raw={"symbol": "ETHUSDT", "status": "TRADING"},
    )
    store.apply_snapshot(
        MarketSnapshot(
            source=binance_market.source,
            venue=binance_market.venue,
            market_type=binance_market.market_type,
            product=binance_market.product,
            observed_at="2026-07-03T00:00:00+00:00",
            markets=(binance_market,),
        )
    )
    bybit_market = MarketRecord(
        source="BYBIT_LINEAR_FUTURE",
        venue="BYBIT",
        market_type="FUTURE",
        product="LINEAR",
        raw_symbol="SOLUSDT-25SEP26",
        base_symbol="SOL",
        quote_symbol="USDT",
        settle_symbol="USDT",
        contract_type="DATED",
        status="TRADING",
        active=True,
        contract_multiplier=None,
        raw={"symbol": "SOLUSDT-25SEP26", "contractType": "LinearFutures"},
        expires_at="2026-09-25T08:00:00+00:00",
    )
    store.apply_snapshot(
        MarketSnapshot(
            source=bybit_market.source,
            venue=bybit_market.venue,
            market_type=bybit_market.market_type,
            product=bybit_market.product,
            observed_at="2026-07-03T00:00:00+00:00",
            markets=(bybit_market,),
        )
    )
    tagged_market = MarketRecord(
        source="BINANCE_SPOT",
        venue="BINANCE",
        market_type="SPOT",
        product="SPOT",
        raw_symbol="WIFUSDT",
        base_symbol="WIF",
        quote_symbol="USDT",
        settle_symbol=None,
        contract_type="SPOT",
        status="TRADING",
        active=True,
        contract_multiplier=None,
        raw={
            "symbol": "WIFUSDT",
            "_metadata": {
                "BINANCE_PRODUCT": {
                    "s": "WIFUSDT",
                    "b": "WIF",
                    "tags": ["Monitoring", "Seed"],
                }
            },
        },
    )
    store.apply_snapshot(
        MarketSnapshot(
            source=tagged_market.source,
            venue=tagged_market.venue,
            market_type=tagged_market.market_type,
            product=tagged_market.product,
            observed_at="2026-07-03T00:00:00+00:00",
            markets=(tagged_market,),
        )
    )
    entitlements_path = tmp_path / "entitlements.yaml"
    entitlements_path.write_text(
        yaml.safe_dump(
            {
                "session_secret": "test-session-secret-that-is-at-least-32-characters",
                "users": {
                    "admin": {
                        "password_hash": hash_password("password"),
                        "role": "operator",
                    },
                    "reader": {
                        "password_hash": hash_password("read-password"),
                        "role": "reader",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        db_path=store.path,
        host="127.0.0.1",
        port=8090,
        http_timeout_seconds=1,
        collection_schedule="*-*-* 00:00:00 UTC",
        entitlements_path=entitlements_path,
        session_cookie_name="mdv_session",
        session_ttl_seconds=3600,
        session_cookie_secure=False,
    )

    with TestClient(create_app(settings=settings, store=store)) as client:
        unauthenticated_api = client.get("/api/v1/stats")
        favicon_response = client.get("/favicon.ico")
        login_redirect = client.get("/mdv", follow_redirects=False)
        root_redirect = client.get("/", follow_redirects=False)
        failed_login = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "next": "/mdv"},
        )
        successful_login = client.post(
            "/login",
            data={"username": "admin", "password": "password", "next": "/mdv"},
            follow_redirects=False,
        )
        cookie_access = client.get("/mdv")
        client.cookies.clear()
        client.headers["Authorization"] = "Basic " + base64.b64encode(
            b"admin:password"
        ).decode("ascii")
        authenticated_root = client.get("/", follow_redirects=False)
        health_response = client.get("/health")
        openapi_response = client.get("/openapi.json")
        canonical_redirect = client.get(
            "/mdv?TYPE=&CONTRACT=&STOCK=&FUTURES=&FUTURES%21=&VENUE=&PRODUCT=&TAG=BINANCE%3AMONITORING&SYMBOL=",
            follow_redirects=False,
        )
        futures_not_redirect = client.get(
            "/mdv?FUTURES%21=MEXC",
            follow_redirects=False,
        )
        financing_not_redirect = client.get(
            "/mdv?FINANCING%21=BYBIT%3ALOAN",
            follow_redirects=False,
        )
        response = client.get("/mdv?TYPE=FUTURE")
        asset_page = client.get("/asset?TYPE=FUTURE")
        asset_detail_page = client.get("/asset?SYMBOL=SOL")
        btc_detail_page = client.get("/asset?SYMBOL=BTC")
        asset_detail_api_response = client.get("/api/v1/assets?SYMBOL=SOL")
        btc_detail_api_response = client.get("/api/v1/assets?SYMBOL=BTC")
        coverage_page = client.get("/coverage?TYPE=FUTURE")
        api_response = client.get("/api/v1/markets?TYPE=FUTURE")
        asset_response = client.get("/api/v1/assets?TYPE=FUTURE")
        financing_response = client.get("/api/v1/financing?PRODUCT=CRYPTO_LOAN&SYMBOL=SOL")
        invalid_financing_response = client.get("/api/v1/financing?PRODUCT=MARGIN")
        financing_assets = client.get("/api/v1/assets?FINANCING=BYBIT:LOAN")
        financing_page = client.get("/mdv?FINANCING=BYBIT:LOAN")
        without_financing = client.get("/api/v1/assets?FINANCING!=BYBIT:LOAN")
        without_perpetuals = client.get("/api/v1/assets?PRODUCT!=PERP")
        without_spot = client.get("/api/v1/assets?TYPE!=SPOT")
        metadata_response = client.get("/api/v1/metadata")
        metadata_html_response = client.get("/metadata")
        logs_response = client.get("/logs")
        manual_actions_response = client.get("/manual-actions")
        manual_action_create_response = client.post(
            "/manual-actions",
            data={
                "action_type": "OTHER", "venue": "", "source_symbol": "",
                "target_symbol": "", "note": "operator note", "enabled": "on",
            },
            follow_redirects=False,
        )
        logs_api_response = client.get("/api/v1/logs")
        changed_only_logs_response = client.get("/logs?CHANGES_ONLY=1")
        changed_only_logs_api_response = client.get("/api/v1/logs?CHANGES_ONLY=1")
        mexc_logs_api_response = client.get("/api/v1/logs?VENUE=MEXC")
        filtered_logs_response = client.get(
            "/logs?ACTION=TAG_ADDED&TAG=BINANCE%3AMONITORING&DATE_FROM=2026-07-03&DATE_TO=2026-07-03"
        )
        filtered_logs_api_response = client.get(
            "/api/v1/logs?ACTION=TAG_ADDED&TAG=BINANCE%3AMONITORING&DATE_FROM=2026-07-03&DATE_TO=2026-07-03"
        )
        listing_logs_response = client.get(
            "/logs?ACTION=LISTING&VENUE=BINANCE&SYMBOL=WIF*&PRODUCT=SPOT"
        )
        listing_logs_api_response = client.get(
            "/api/v1/logs?ACTION=LISTING&VENUE=BINANCE&SYMBOL=WIF*&PRODUCT=SPOT"
        )
        invalid_logs_api_response = client.get("/api/v1/logs?ACTION=UNKNOWN")
        binance_only = client.get("/mdv?CONTRACT=PERP&FUTURES=BINANCE&FUTURES!=MEXC")
        monitoring = client.get("/mdv?TAG=BINANCE:MONITORING")
        monitoring_detail = client.get("/asset?SYMBOL=WIF")
        monitoring_detail_api = client.get("/api/v1/assets?SYMBOL=WIF")
        removed_refresh = client.post("/api/v1/refresh?VENUE=MEXC")
        reader_action = client.post(
            "/manual-actions",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(b"reader:read-password").decode("ascii")
            },
            data={
                "action_type": "OTHER",
                "venue": "",
                "source_symbol": "",
                "target_symbol": "",
                "note": "must not be saved",
                "enabled": "on",
            },
        )

    assert unauthenticated_api.status_code == 401
    assert unauthenticated_api.headers["www-authenticate"] == 'Basic realm="asset-master-data"'
    assert favicon_response.status_code == 204
    assert "max-age=86400" in favicon_response.headers["cache-control"]
    health = health_response.json()
    assert health["status"] == "ok"
    assert health["service"] == "asset-master-data"
    assert health["version"] == __version__
    assert health["revision"] == revision
    assert health["markets"] == 4
    assert health["readiness"]["ready"] is True
    assert health["readiness"]["active_markets"] == 4
    assert health["readiness"]["running_collections"] == 0
    assert health["readiness"]["collection_fresh"] is True
    openapi = openapi_response.json()
    assert openapi["info"]["version"] == __version__
    assert "/api/v1/refresh" not in openapi["paths"]
    assert login_redirect.status_code == 303
    assert login_redirect.headers["location"].startswith("/login?next=")
    assert root_redirect.status_code == 303
    assert root_redirect.headers["location"].startswith("/login?next=")
    assert authenticated_root.status_code == 307
    assert authenticated_root.headers["location"] == "/coverage"
    assert failed_login.status_code == 401
    assert successful_login.status_code == 303
    assert successful_login.headers["location"] == "/mdv"
    assert "HttpOnly" in successful_login.headers["set-cookie"]
    assert "SameSite=strict" in successful_login.headers["set-cookie"]
    assert cookie_access.status_code == 200
    assert canonical_redirect.status_code == 307
    assert canonical_redirect.headers["location"] == "/mdv?TAG=BINANCE%3AMONITORING"
    assert futures_not_redirect.status_code == 307
    assert futures_not_redirect.headers["location"] == "/mdv?FUTURES!=MEXC"
    assert financing_not_redirect.status_code == 307
    assert financing_not_redirect.headers["location"] == "/mdv?FINANCING!=BYBIT%3ALOAN"
    assert response.status_code == 200
    assert asset_page.status_code == 200
    assert asset_detail_page.status_code == 200
    assert btc_detail_page.status_code == 200
    assert asset_detail_api_response.status_code == 200
    assert btc_detail_api_response.status_code == 200
    assert coverage_page.status_code == 200
    assert "BTC_USDT" not in response.text
    assert "BTC_USDT" not in btc_detail_page.text
    assert "BTC_USDT" in btc_detail_api_response.text
    assert "MEXC" in response.text
    assert "Perps" in response.text
    assert "MEXC ONLY" in response.text
    assert "SAME_VENUE_SPOT_FUTURE_SYMBOL" not in response.text
    assert "https://www.mexc.com/futures/BTC_USDT" in btc_detail_api_response.text
    assert "https://www.bybit.com/trade/usdt/SOLUSDT-25SEP26" in asset_detail_api_response.text
    assert "2026-09-25T08:00:00+00:00" in asset_detail_api_response.text
    assert '"max_market_order_size":"5000000"' in btc_detail_api_response.text
    assert "Contract size:" not in asset_detail_page.text
    assert '<details class="asset" data-symbol="SOL">' in asset_detail_page.text
    assert 'loadAssetDetail(details)' in response.text
    assert '/api/v1/assets?SYMBOL=${encodeURIComponent(details.dataset.symbol)}&LIMIT=1' in response.text
    assert 'type="search" name="SYMBOL"' in response.text
    assert 'type="search" name="SYMBOL"' in coverage_page.text
    assert '<label>Tag<select name="TAG">' in response.text
    assert '<label>Tag<select name="TAG">' in coverage_page.text
    assert 'assetSymbol.addEventListener(\'keydown\'' in response.text
    assert 'assetSymbol.addEventListener(\'change\'' in response.text
    assert 'coverageSymbol.addEventListener(\'keydown\'' in coverage_page.text
    assert 'coverageSymbol.addEventListener(\'change\'' in coverage_page.text
    assert asset_detail_api_response.json()["assets"][0]["loan_venues"] == [
        {"venue": "BYBIT", "count": 1}
    ]
    assert "Refresh universes" not in response.text
    assert "market-toggle" not in response.text
    assert "Availability matrix" in coverage_page.text
    assert 'class="coverage-row"' in coverage_page.text
    assert 'role="button" aria-expanded="false" aria-controls="coverage-detail-1"' in coverage_page.text
    assert 'loadCoverageDetail(row, detail)' in coverage_page.text
    assert 'data-href="/asset?SYMBOL=' not in coverage_page.text
    assert "Margin" in coverage_page.text
    assert "Loans" in coverage_page.text
    assert api_response.json()["count"] == 3
    assert api_response.json()["markets"][2]["max_market_order_size"] == "5000000"
    assert asset_response.json()["count"] == 3
    sol_asset = next(
        asset for asset in asset_response.json()["assets"]
        if asset["canonical_symbol"] == "SOL"
    )
    assert sol_asset["borrow_eligibility"][0]["product"] == "CRYPTO_LOAN"
    assert sol_asset["loan_venues"] == [{"venue": "BYBIT", "count": 1}]
    assert sol_asset["margin_venues"] == []
    assert "raw" not in sol_asset["borrow_eligibility"][0]
    assert financing_response.status_code == 200
    assert financing_response.json()["count"] == 1
    assert financing_response.json()["financing"][0]["raw"]["currency"] == "SOL"
    assert invalid_financing_response.status_code == 422
    assert [
        asset["canonical_symbol"] for asset in financing_assets.json()["assets"]
    ] == ["SOL"]
    assert financing_page.status_code == 200
    assert '<option value="BYBIT:LOAN" selected>' in financing_page.text
    assert all(
        asset["canonical_symbol"] != "SOL"
        for asset in without_financing.json()["assets"]
    )
    assert {
        asset["canonical_symbol"] for asset in without_perpetuals.json()["assets"]
    } == {"SOL", "WIF"}
    assert without_spot.json()["count"] == 3
    assert metadata_response.status_code == 200
    assert metadata_response.json()["filters"]["VENUE"]["values"] == [
        "BINANCE",
        "BYBIT",
        "MEXC",
    ]
    assert metadata_response.json()["filters"]["TAG"]["values"] == [
        "BINANCE:MONITORING",
        "BINANCE:SEED",
    ]
    assert metadata_response.json()["filters"]["FINANCING"]["values"] == [
        "BYBIT:LOAN"
    ]
    assert metadata_response.json()["filters"]["FUTURES"]["operators"] == ["=", "!="]
    assert "FUTURES!" not in metadata_response.json()["filters"]
    assert metadata_html_response.status_code == 200
    assert "Filter Metadata" in metadata_html_response.text
    assert "BINANCE:MONITORING" in metadata_html_response.text
    assert "repeatable / comma-separated" in metadata_html_response.text
    assert "All data filters support" in metadata_html_response.text
    assert "Normalized instrument product" in metadata_html_response.text
    assert logs_response.status_code == 200
    assert "Collection Log" in logs_response.text
    assert manual_actions_response.status_code == 200
    assert "Manual asset actions" in manual_actions_response.text
    assert "mexc-tsemstock-to-tsem" in manual_actions_response.text
    assert manual_action_create_response.status_code == 303
    assert manual_action_create_response.headers["location"] == "/manual-actions"
    assert 'id="timezone-select"' in logs_response.text
    assert "mdv_timezone" in logs_response.text
    assert "Venues updated:" in logs_response.text
    assert 'name="ACTION"' in logs_response.text
    assert 'name="TAG"' in logs_response.text
    assert 'name="VENUE"' in logs_response.text
    assert 'name="SYMBOL"' in logs_response.text
    assert 'name="PRODUCT"' in logs_response.text
    assert 'name="DATE_FROM"' in logs_response.text
    assert 'name="DATE_TO"' in logs_response.text
    assert 'name="CHANGES_ONLY" value="1"' in logs_response.text
    assert 'type="search" name="SYMBOL"' in logs_response.text
    assert 'logFilters.requestSubmit()' in logs_response.text
    assert 'logSymbol.addEventListener(\'change\'' in logs_response.text
    assert 'Apply filters' not in logs_response.text
    assert 'class="app-nav"' in logs_response.text
    assert 'class="app-nav"' in metadata_html_response.text
    assert 'class="app-nav"' in manual_actions_response.text
    assert '<details class="venue-block" open>' in logs_response.text
    assert 'id="market-filter-group" class="filter-group" hidden' in logs_response.text
    assert 'id="tag-filter-group" class="filter-group" hidden' in logs_response.text
    assert logs_api_response.status_code == 200
    assert logs_api_response.json()["count"] == 5
    assert changed_only_logs_response.status_code == 200
    assert 'name="CHANGES_ONLY" value="1" checked' in changed_only_logs_response.text
    assert changed_only_logs_api_response.json()["count"] == 4
    assert {run["scope"] for run in logs_api_response.json()["runs"]} == {
        "BINANCE",
        "BYBIT",
        "MEXC",
    }
    assert mexc_logs_api_response.json()["count"] == 1
    assert mexc_logs_api_response.json()["runs"][0]["scope"] == "MEXC"
    assert filtered_logs_response.status_code == 200
    assert "WIF added Monitoring tag" in filtered_logs_response.text
    assert "WIF added Seed tag" not in filtered_logs_response.text
    assert filtered_logs_api_response.status_code == 200
    assert filtered_logs_api_response.json()["count"] == 1
    assert filtered_logs_api_response.json()["runs"][0]["change_count"] == 1
    assert listing_logs_response.status_code == 200
    assert 'id="market-filter-group" class="filter-group">' in listing_logs_response.text
    assert 'id="tag-filter-group" class="filter-group" hidden' in listing_logs_response.text
    assert "MARKET_DISCOVERED · SPOT · WIFUSDT" in listing_logs_response.text
    assert listing_logs_api_response.status_code == 200
    assert listing_logs_api_response.json()["count"] == 1
    assert listing_logs_api_response.json()["runs"][0]["venues"][0]["venue"] == "BINANCE"
    assert listing_logs_api_response.json()["runs"][0]["venues"][0]["changes"][0]["product"] == "SPOT"
    assert invalid_logs_api_response.status_code == 422
    assert "ETH" in binance_only.text
    assert "BTC_USDT" not in binance_only.text
    assert monitoring.status_code == 200
    assert "WIF" in monitoring.text
    assert "BINANCE · Monitoring" not in monitoring_detail.text
    assert any(
        tag["provider"] == "BINANCE" and tag["raw_tag"] == "Monitoring"
        for tag in monitoring_detail_api.json()["assets"][0]["tags"]
    )
    assert removed_refresh.status_code == 404
    assert reader_action.status_code == 403
    assert reader_action.json()["detail"] == "operator role required"


def test_basic_auth_failures_are_rate_limited_before_more_scrypt_work(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    settings = Settings(
        db_path=store.path,
        host="127.0.0.1",
        port=8090,
        http_timeout_seconds=1,
        collection_schedule="*-*-* 00:00:00 UTC",
        entitlements_path=tmp_path / "unused.yaml",
        session_cookie_name="mdv_session",
        session_ttl_seconds=3600,
        session_cookie_secure=False,
        auth_failed_attempt_limit=2,
        auth_failed_attempt_window_seconds=60,
    )
    entitlements = Entitlements(
        session_secret=b"test-session-secret-that-is-at-least-32-characters",
        users={"admin": hash_password("password")},
        roles={"admin": "operator"},
    )
    authorization = "Basic " + base64.b64encode(b"admin:wrong").decode("ascii")

    with TestClient(
        create_app(settings=settings, store=store, entitlements=entitlements)
    ) as client:
        responses = [
            client.get("/health", headers={"Authorization": authorization})
            for _ in range(3)
        ]

    assert [response.status_code for response in responses] == [401, 401, 429]
    assert responses[-1].headers["retry-after"] == "60"


def test_concurrent_auth_burst_does_not_queue_unbounded_scrypt_work(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    settings = Settings(
        db_path=store.path,
        host="127.0.0.1",
        port=8090,
        http_timeout_seconds=1,
        collection_schedule="*-*-* 00:00:00 UTC",
        entitlements_path=tmp_path / "unused.yaml",
        session_cookie_name="mdv_session",
        session_ttl_seconds=3600,
        session_cookie_secure=False,
        auth_max_concurrent_hashes=1,
        auth_failed_attempt_limit=10,
        auth_failed_attempt_window_seconds=60,
    )
    entitlements = Entitlements(
        session_secret=b"test-session-secret-that-is-at-least-32-characters",
        users={"admin": hash_password("password")},
        roles={"admin": "operator"},
    )
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_failure(_self, _username, _password):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return False

    monkeypatch.setattr(Entitlements, "authenticate", slow_failure)
    authorization = "Basic " + base64.b64encode(b"admin:wrong").decode("ascii")
    app = create_app(settings=settings, store=store, entitlements=entitlements)

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first = asyncio.create_task(
                client.get("/health", headers={"Authorization": authorization})
            )
            assert await asyncio.to_thread(started.wait, 1)
            burst = await asyncio.gather(
                *(
                    client.get(
                        "/health", headers={"Authorization": authorization}
                    )
                    for _ in range(8)
                )
            )
            release.set()
            return await first, burst

    first, burst = asyncio.run(exercise())

    assert first.status_code == 401
    assert {response.status_code for response in burst} == {429}
    assert calls == 1
