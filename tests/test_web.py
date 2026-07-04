import base64

import yaml
from fastapi.testclient import TestClient

from mdv import __version__
from mdv.auth import hash_password
from mdv.collection import CollectionResult
from mdv.config import Settings
from mdv.db import SQLiteStore
from mdv.models import MarketRecord, MarketSnapshot
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
                "users": {"admin": {"password_hash": hash_password("password")}},
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        db_path=store.path,
        host="127.0.0.1",
        port=8090,
        refresh_on_startup="never",
        http_timeout_seconds=1,
        collection_schedule="*-*-* 00:00:00 UTC",
        entitlements_path=entitlements_path,
        session_cookie_name="mdv_session",
        session_ttl_seconds=3600,
        session_cookie_secure=False,
    )

    class FakeCollectionService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def collect(self, *, venue=None):
            assert venue == "MEXC"
            return [
                CollectionResult(
                    source="MEXC_SPOT",
                    ok=True,
                    records=1,
                    run_id="ingest-run",
                    collection_run_id="collection-run",
                )
            ]

    monkeypatch.setattr("mdv.web.CollectionService", FakeCollectionService)

    with TestClient(create_app(settings=settings, store=store)) as client:
        unauthenticated_api = client.get("/api/v1/stats")
        favicon_response = client.get("/favicon.ico")
        login_redirect = client.get("/mdv", follow_redirects=False)
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
        response = client.get("/mdv?TYPE=FUTURE")
        api_response = client.get("/api/v1/markets?TYPE=FUTURE")
        asset_response = client.get("/api/v1/assets?TYPE=FUTURE")
        without_perpetuals = client.get("/api/v1/assets?PRODUCT!=PERP")
        without_spot = client.get("/api/v1/assets?TYPE!=SPOT")
        metadata_response = client.get("/api/v1/metadata")
        metadata_html_response = client.get("/metadata")
        logs_response = client.get("/logs")
        logs_api_response = client.get("/api/v1/logs")
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
        scoped_refresh = client.post("/api/v1/refresh?VENUE=MEXC")

    assert unauthenticated_api.status_code == 401
    assert unauthenticated_api.headers["www-authenticate"] == 'Basic realm="asset-master-data"'
    assert favicon_response.status_code == 204
    assert "max-age=86400" in favicon_response.headers["cache-control"]
    assert health_response.json() == {
        "status": "ok",
        "version": __version__,
        "revision": revision,
        "markets": 4,
    }
    assert openapi_response.json()["info"]["version"] == __version__
    assert login_redirect.status_code == 303
    assert login_redirect.headers["location"].startswith("/login?next=")
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
    assert response.status_code == 200
    assert "BTC_USDT" in response.text
    assert "MEXC" in response.text
    assert "Futures coverage" in response.text
    assert "MEXC ONLY" in response.text
    assert "SAME_VENUE_SPOT_FUTURE_SYMBOL" not in response.text
    assert "https://www.mexc.com/futures/BTC_USDT" in response.text
    assert "https://www.bybit.com/trade/usdt/SOLUSDT-25SEP26" in response.text
    assert "Expires: 2026-09-25T08:00:00+00:00" in response.text
    assert "Max market order size: 5000000" in response.text
    assert "Contract size:" not in response.text
    assert 'id="column-settings-toggle"' in response.text
    assert 'data-column-option="asset"' in response.text
    assert 'data-column-option="markets"' in response.text
    assert 'draggable="true"' in response.text
    assert 'tabindex="0"' in response.text
    assert "mdv_columns" in response.text
    assert 'data-details-cell' in response.text
    assert "Refresh universes" not in response.text
    assert "data-move" not in response.text
    assert api_response.json()["count"] == 3
    assert api_response.json()["markets"][2]["max_market_order_size"] == "5000000"
    assert asset_response.json()["count"] == 3
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
    assert 'id="market-filter-group" class="filter-group" hidden' in logs_response.text
    assert 'id="tag-filter-group" class="filter-group" hidden' in logs_response.text
    assert logs_api_response.status_code == 200
    assert logs_api_response.json()["count"] == 4
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
    assert "ETHUSDT" in binance_only.text
    assert "BTC_USDT" not in binance_only.text
    assert monitoring.status_code == 200
    assert "WIF" in monitoring.text
    assert "BINANCE · Monitoring" in monitoring.text
    assert scoped_refresh.status_code == 200
    assert scoped_refresh.json()["scope"] == "MEXC"
    assert scoped_refresh.json()["collection_run_id"] == "collection-run"
