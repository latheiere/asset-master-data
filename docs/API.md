# HTTP API

This document defines the HTTP contract for services that consume Asset Master
Data. UI behavior and local setup belong in the project [README](../README.md).

## Base URL, authentication, and format

Default base URL:

```text
http://127.0.0.1:8090
```

All routes, including `/health`, require HTTP Basic Auth for service clients.
Browser session cookies are an HTML UI mechanism and should not be used by
service integrations.

```bash
curl --user "$ASSET_MASTER_USERNAME:$ASSET_MASTER_PASSWORD" \
  http://127.0.0.1:8090/api/v1/stats
```

JSON responses use `application/json`. Timestamps are ISO 8601 UTC strings.
Field names and enum values are case-sensitive in responses. Query parameter
names and enum inputs accept upper- or lowercase and are normalized to uppercase.

Versioned consumer routes use `/api/v1`. `/health` is intentionally unversioned.
FastAPI-generated OpenAPI is available at `/openapi.json` under the same
authentication boundary. This document is the maintained integration contract;
generated OpenAPI does not currently cover every dynamic response field.

## Error behavior

| Status | Meaning |
| --- | --- |
| `200` | Request completed; batch resolution may still contain per-symbol failures |
| `401` | Missing or invalid authentication |
| `422` | Invalid query value, request shape, enum, or conflicting filter |
| `500` | Unexpected server or storage failure |

Authentication failures include `WWW-Authenticate: Basic
realm="asset-master-data"`. Validation errors use FastAPI’s JSON `detail`
field. Clients must not treat HTTP 200 from mapping resolution as proof that
every symbol resolved; inspect every result status.

## Query conventions

Data filters support inclusion and exclusion:

```text
VENUE=BINANCE
VENUE!=MEXC
```

In URL parsing, `VENUE!=MEXC` is the key `VENUE!` with value `MEXC`. Values may
be repeated or comma-separated. Multiple included values normally mean “match
one”; `FUTURES` and `TAG` are asset-level requirements where all requested
values must be present. Inclusion and exclusion of the same futures venue is
invalid.

Common filters:

| Filter | Values and meaning |
| --- | --- |
| `TYPE` | `SPOT`, `FUTURE` |
| `PRODUCT` | `SPOT`, `PERP`, `DATED` |
| `CONTRACT` | Deprecated alias for `PRODUCT`; legacy `CQ`/`NQ` mean dated `Q`/`BQ` |
| `EXPIRY` | `W`, `BW`, `TW`, `M`, `BM`, `Q`, `BQ`, `TQ` |
| `DIRECTION` | `LINEAR`, `INVERSE`, `QUANTO` |
| `QUOTE` | Price-denomination asset, for example `USDT` |
| `SETTLE` | Futures settlement or margin asset |
| `FUTURES` | Required futures venue coverage at canonical-asset level |
| `STOCK` | `1` for equity-classified assets, `0` otherwise |
| `TAG` | Provider-scoped tag, for example `BINANCE:MONITORING` |
| `FINANCING` | Provider-scoped eligibility, for example `BINANCE:MARGIN` or `BYBIT:LOAN` |
| `VENUE` | Trading venue |
| `SYMBOL` | Canonical, venue-base, or raw symbol; `*` wildcard supported |
| `STATUS` | Normalized market status |
| `LIMIT` | Page size; defaults to 500 for market and asset endpoints |
| `OFFSET` | Zero-based result offset |

Asset filters are `TYPE`, `PRODUCT`, `CONTRACT`, `EXPIRY`, `DIRECTION`, `QUOTE`,
`SETTLE`, `FUTURES`, `STOCK`, `TAG`, `FINANCING`, `VENUE`, `SYMBOL`, `STATUS`,
`LIMIT`, and `OFFSET`. Multiple included `FINANCING` values require all named
capabilities. Raw-market filters are the same except they omit asset-level
`FUTURES`, `STOCK`, and `FINANCING`, and add `ACTIVE=true|false`. Asset
projections are always active-only. Use `GET /api/v1/metadata` to discover
accepted values from the current database instead of hardcoding venue or enum
lists.

## Endpoints

### `GET /health`

Authenticated liveness and database reachability check.

```json
{
  "status": "ok",
  "version": "0.10.0",
  "revision": "0123456789abcdef0123456789abcdef01234567",
  "markets": 12500
}
```

`version` is the installed release version. `revision` is the exact 40-character
Git commit injected by the deployment unit, or `unknown` outside a validated
deployment. The release version is independent of the `/api/v1` contract
version. `markets` includes inactive records.

### `GET /api/v1/assets`

Returns the active asset-first hierarchy. Supports all common filters except
`ACTIVE`.

```json
{
  "assets": [
    {
      "asset_id": "uuid",
      "canonical_symbol": "BTC",
      "venue_symbols": [{"venue": "BINANCE", "symbols": ["BTC"]}],
      "spot_venues": [{"venue": "BINANCE", "count": 3}],
      "perp_venues": [{"venue": "BINANCE", "count": 1}],
      "dated_venues": [{"venue": "BINANCE", "count": 1}],
      "margin_venues": [{"venue": "BINANCE", "count": 1}],
      "loan_venues": [{"venue": "BYBIT", "count": 1}],
      "future_venues": [
        {"venue": "BINANCE", "count": 2, "products": ["DATED", "PERP"]}
      ],
      "future_coverage": "ALL · 6/6",
      "future_coverage_kind": "all",
      "active_market_count": 6,
      "is_stock": false,
      "tags": [],
      "financing": [],
      "borrow_eligibility": [],
      "venues": []
    }
  ],
  "count": 1,
  "supported_future_venues": ["BINANCE", "BITFINEX", "BITGET", "BITMART", "BYBIT", "COINBASE", "DERIBIT", "GATE", "GEMINI", "HTX", "HYPERLIQUID", "KUCOIN", "MEXC", "OKX", "WHITEBIT", "XT"]
}
```

`venues` contains each venue’s base symbols plus `spot` and `futures` market
arrays. Market rows include normalized fields, source fields, timestamps,
`underlying_unit`, and `trade_url`; they do not include `raw_json`. `count` is
the filtered total before `LIMIT`/`OFFSET`.

`perp_venues` and `dated_venues` split active derivative coverage by normalized
duration. `margin_venues` and `loan_venues` split mapped borrow eligibility by
`CROSS_MARGIN` and `CRYPTO_LOAN`; their counts are eligible borrowable records.

`financing` contains compact, active, eligible financing records mapped through
an exact same-venue market symbol. `borrow_eligibility` is its `BORROWABLE`
subset; `COLLATERAL` records remain in `financing`. Compact rows include venue,
`CROSS_MARGIN` or `CRYPTO_LOAN`, asset role, regular-user rate when published,
rate count, terms, platform limits, pair symbols, and observation time. Raw
payloads and full rate tiers are omitted here to bound the asset response; use
the financing endpoint for the complete record. `FINANCING` filters asset
selection; financing metadata never changes active-market counts.

### `GET /api/v1/financing`

Returns public venue financing catalogs independently from spot and futures
markets. Defaults to current records, including eligible and disabled entries.

Filters:

| Filter | Values |
| --- | --- |
| `VENUE` | Provider venue key |
| `PRODUCT` | `CROSS_MARGIN` or `CRYPTO_LOAN` |
| `ROLE` | `BORROWABLE` or `COLLATERAL` |
| `SYMBOL` | Exact provider asset symbol |
| `ELIGIBLE` | `true` or `false` |
| `LIMIT` | Default/max 5000 |
| `OFFSET` | Zero-based offset |

```json
{
  "count": 1,
  "financing": [
    {
      "financing_id": "BYBIT_CRYPTO_LOAN:CRYPTO_LOAN:BORROWABLE:BTC",
      "source": "BYBIT_CRYPTO_LOAN",
      "venue": "BYBIT",
      "product": "CRYPTO_LOAN",
      "asset_role": "BORROWABLE",
      "raw_asset_symbol": "BTC",
      "eligible": true,
      "status": "ENABLED",
      "active": true,
      "regular_user_tier": "VIP0",
      "rates": [
        {
          "tier": "VIP0",
          "regular_user": true,
          "rate_type": "FLEXIBLE",
          "rate_unit": "APR",
          "value": "0.04"
        }
      ],
      "terms": [{"type": "FLEXIBLE", "enabled": true}],
      "limits": {"min_flexible": "0.001", "platform_max": "10"},
      "pair_symbols": [],
      "canonical_symbol": "BTC",
      "match_method": "SAME_VENUE_MARKET_SYMBOL",
      "raw": {"currency": "BTC"}
    }
  ]
}
```

`count` is the pre-pagination total. `canonical_symbol` and mapping fields are
null when the financing symbol has no unique exact same-venue market mapping.
Rates are decimal values: `0.04` APR means 4%. `VENUE_NATIVE` means the provider
does not document a safe cross-venue rate unit; consumers must not compare it
as APR. Limits are public product limits only. They are not balances, current
inventory, credit decisions, or personalized maximum-borrow values.

### `GET /api/v1/markets`

Returns a flat market projection for audit and low-level consumers. Defaults to
active markets. Pass `ACTIVE=false` only when inactive history is required.

```json
{
  "count": 1,
  "markets": [
    {
      "market_id": "BINANCE_USDM_FUTURE:BTCUSDT",
      "venue": "BINANCE",
      "market_type": "FUTURE",
      "product": "PERP",
      "raw_symbol": "BTCUSDT",
      "base_symbol": "BTC",
      "quote_symbol": "USDT",
      "settle_symbol": "USDT",
      "status": "TRADING",
      "active": 1,
      "canonical_symbol": "BTC",
      "match_method": "SAME_VENUE_SPOT_FUTURE_SYMBOL",
      "match_confidence": 0.97,
      "matcher_version": "evidence-v4",
      "raw_json": "{...}"
    }
  ]
}
```

`count` is the number returned after pagination, unlike the asset endpoint’s
pre-pagination count.

### `POST /api/v1/mappings/resolve`

Resolves 1–100 unique source base symbols to one exact target market projection.
The implementation uses one indexed, read-only SQLite transaction and does not
construct the generic asset hierarchy.

Request:

```json
{
  "source": {
    "venue": "BINANCE",
    "symbol_type": "BASE",
    "symbols": ["BTC", "ETH"]
  },
  "target": {
    "venue": "GATE",
    "market_type": "FUTURE",
    "product": "PERP",
    "contract_type": "PERP",
    "quote_symbol": "USDT",
    "settle_symbol": "USDT",
    "status": "TRADING",
    "venue_product": "USDT-PERP",
    "contract_direction": "LINEAR"
  }
}
```

`source.symbol_type` currently accepts only `BASE`. `product` and
`contract_type` are both required and must contain the same normalized value.
`venue_product`, `contract_direction`, and `expiry_cycle` are optional exact
target filters. Extra fields are rejected.

Response:

```json
{
  "schema_version": "1",
  "snapshot_revision": "2026-07-03T14:41:10Z",
  "results": [
    {
      "source_symbol": "BTC",
      "status": "resolved",
      "asset_id": "uuid",
      "canonical_symbol": "BTC",
      "target": {
        "market_id": "GATE_USDT_FUTURE:BTC_USDT",
        "raw_symbol": "BTC_USDT",
        "base_symbol": "BTC",
        "last_seen_at": "2026-07-03T14:41:10+00:00"
      }
    }
  ]
}
```

Results preserve input order. Per-symbol statuses:

| Status | `error_code` | Meaning |
| --- | --- | --- |
| `resolved` | null | Exactly one fresh source asset and target market |
| `source_not_found` | `SOURCE_NOT_FOUND` | No active source mapping |
| `target_not_found` | `TARGET_NOT_FOUND` | Source resolved, target filter matched nothing |
| `ambiguous_source` | `MULTIPLE_SOURCE_ASSETS` | Source symbol maps to multiple assets |
| `ambiguous_target` | `MULTIPLE_TARGETS` | More than one target market matches |
| `stale` | `STALE_SNAPSHOT` | Source or target was not confirmed by its latest successful run |

Syntactically valid batches return HTTP 200 even when some results do not
resolve. `snapshot_revision` is the greatest `last_seen_at` among active markets
inside the read snapshot.

### `GET /api/v1/logs`

Returns durable collection invocations newest-first.

Parameters:

- `VENUE`: restrict collection runs, and matching changes when a change filter is active.
- `ACTION`: `LISTING`, `REMOVAL`, `TAG_ADDED`, or `TAG_REMOVED`.
- `TAG`: provider-scoped tag such as `BINANCE:MONITORING`; restricts results to
  tag changes for that tag. It cannot be combined with listing/removal actions
  or `SYMBOL`.
- `SYMBOL`: canonical, venue-base, or raw market symbol for lifecycle changes;
  `*` wildcard supported. It cannot be combined with tag actions.
- `PRODUCT`: normalized lifecycle market type: `PERP`, `DATED`, or `SPOT`. It
  cannot be combined with tag actions.
- `DATE_FROM` and `DATE_TO`: inclusive UTC change dates in `YYYY-MM-DD` format.
- `CHANGES_ONLY`: `1`, `true`, `yes`, or `on` restricts results to runs with at
  least one recorded market lifecycle or tag change.
- `LIMIT` (default 10, maximum 500) and `OFFSET`: paginate matching collection runs.

When `CHANGES_ONLY` or any change filter is present, only runs containing a matching change and
only matching changes and venue sections inside those runs are returned. Venue
updates with no matching change are omitted. `count` is the total number of
matching runs before pagination.

```json
{
  "count": 1,
  "runs": [
    {
      "collection_run_id": "uuid",
      "scope": "ALL",
      "status": "SUCCEEDED",
      "requested_venues": ["BINANCE", "MEXC"],
      "change_count": 2,
      "venues": [
        {
          "venue": "BINANCE",
          "status": "SUCCEEDED",
          "record_count": 3000,
          "change_count": 1,
          "changes": [],
          "universes": []
        }
      ]
    }
  ],
  "filter_options": {
    "actions": ["LISTING", "REMOVAL", "TAG_ADDED", "TAG_REMOVED"],
    "tags": ["BINANCE:MONITORING"],
    "venues": ["BINANCE", "MEXC"],
    "products": ["PERP", "DATED", "SPOT"]
  }
}
```

Universe rows contain source, market type, venue product, timing, completion,
record count, and error. Changes contain lifecycle or tag event details.
`MARKET_DISCOVERED` means no earlier same-venue market for that asset and market
type was recorded. `MARKET_LISTED` means a new instrument was listed after such
a market existed; both match `ACTION=LISTING`. A log entry is not evidence that
the asset class itself is new.
Filter options include historical tags, including tags no longer active, and
venues represented in collection history.

### `GET /api/v1/metadata`

Returns machine-readable filter definitions, operators, descriptions, enum
meanings, and values available from the current active universe.

```json
{
  "filters": {
    "VENUE": {
      "kind": "enum",
      "values": ["BINANCE", "MEXC"],
      "operators": ["=", "!="],
      "multiple": true,
      "description": "Trading venue."
    }
  }
}
```

### `GET /api/v1/stats`

Returns total and active market counts, per-universe counts and last-seen times,
and the most recent ingest result for each source.

### `POST /api/v1/refresh`

Starts collection and waits for it to finish.

```text
POST /api/v1/refresh
POST /api/v1/refresh?VENUE=MEXC
```

The first form collects every registered venue; the second collects every
universe registered for one venue. Unknown venues return 422. The response
contains `collection_run_id`, normalized `scope`, aggregate `ok`, and one result
per universe with source, success, record count, ingest run ID, and optional
error.

This is a mutating and potentially slow endpoint. Current authentication has no
read/write roles; protect credentials and network access accordingly.

## Consumer configuration example

```yaml
asset_master:
  base_url: http://127.0.0.1:8090
  timeout_seconds: 1.0
```

Store credentials only in the consumer’s secret manager or ignored environment
file:

```text
ASSET_MASTER_USERNAME=service-user
ASSET_MASTER_PASSWORD=generated-password
```

Use a longer timeout for `/api/v1/refresh` than for read endpoints. Consumers
that require a coherent batch mapping should retain `snapshot_revision` with
their result set.
