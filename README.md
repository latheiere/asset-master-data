# Asset Master Data

Licensed under the Apache License, Version 2.0. See `LICENSE`.

Independent, local-first service that discovers Binance, Bitget, Bybit, Gate.com,
and MEXC spot/futures markets, builds a canonical asset view, records market
status changes, and serves an HTML/JSON view.

No exchange credentials are required. Discovery uses public endpoints.

Python 3.11 or newer is required. The supplied Makefile defaults to
`python3.13`; override `PYTHON_BOOTSTRAP` when another supported interpreter is
preferred.

## Included universes

- Binance Spot
- Binance USD-M Futures
- Binance COIN-M Futures
- Bitget Spot
- Bitget USDT-M Futures
- Bitget USDC-M Futures
- Bitget Coin-M Futures
- Bybit Spot
- Bybit linear futures and perps
- Bybit inverse futures and perps
- Gate.com Spot
- Gate.com USDT perpetual futures
- Gate.com BTC perpetual futures
- Gate.com USDT delivery futures
- MEXC Spot
- MEXC perps

`TYPE=FUTURE` shows assets with at least one active futures contract.
`CONTRACT=PERP` selects perps. Binance delivery codes are `CQ` and `NQ`;
Bybit delivery contracts use `DATED`. Gate.com and Bitget delivery contracts
use `CQ` and `NQ`. All expose their UTC expiration as `expires_at` in API
projections.

## Start

```bash
make install
.venv/bin/python -m mdv.cli entitlement admin --password-file /secure/password-file
make collect
make serve
```

`config/entitlements.yaml` is generated with mode `0600`, contains scrypt
password hashes and a random session-signing secret, and is ignored by Git.
The password file is input only and may be removed after creating the user.
See `config/entitlements.example.yaml` for the non-secret schema.

Open:

```text
http://127.0.0.1:8090/mdv?TYPE=FUTURE
http://127.0.0.1:8090/mdv?CONTRACT=PERP&FUTURES=BINANCE,MEXC
http://127.0.0.1:8090/mdv?CONTRACT=PERP&FUTURES=BINANCE&FUTURES!=MEXC
http://127.0.0.1:8090/mdv?CONTRACT=PERP&STOCK=1
http://127.0.0.1:8090/mdv?TAG=BINANCE:MONITORING
http://127.0.0.1:8090/mdv?TYPE=SPOT&VENUE=BINANCE
http://127.0.0.1:8090/mdv?SYMBOL=BTC*
```

The server collects once on startup when the database is empty. Set
`server.refresh_on_startup: never` in `config/config.yaml` to disable that
behavior, or use `mdv serve --refresh` to force a refresh before serving.

All endpoints require authentication. API clients use HTTP Basic Auth. HTML
requests without credentials redirect to `/login`; a successful login stores a
signed, expiring, HttpOnly, SameSite cookie. The cookie does not contain the
password. Set `auth.session_cookie_secure: true` when HTML is exposed through
HTTPS.

## Configuration

Runtime configuration lives in `config/config.yaml`:

- `database.path`: SQLite database path.
- `server.host`, `server.port`, and `server.refresh_on_startup`: listener and
  startup collection behavior.
- `collection.http_timeout_seconds`: public exchange request timeout.
- `collection.schedule`: systemd `OnCalendar` expression, defaulting to daily
  at `00:00 UTC`.
- `auth.entitlements_path`, cookie name, session lifetime, and secure-cookie
  policy.

Use `mdv --config PATH ...` for another YAML file. Environment variables no
longer override runtime settings; systemd and interactive commands therefore
consume the same configuration.

## CLI

```bash
mdv --config config/config.yaml init
mdv --config config/config.yaml collect
mdv --config config/config.yaml collect --venue BINANCE
mdv --config config/config.yaml collect --venue BITGET
mdv --config config/config.yaml collect --venue BYBIT
mdv --config config/config.yaml collect --venue GATE
mdv --config config/config.yaml collect --venue MEXC
mdv --config config/config.yaml stats
mdv --config config/config.yaml serve --host 127.0.0.1 --port 8090
```

Runtime data defaults to `.data/mdv.sqlite3`; change `database.path` in YAML
when needed. SQLite WAL mode, foreign keys, transactional snapshots, and
schema migrations are enabled.

## Matching policy

Raw venue markets and each observed payload remain unchanged. The current
master view is built through a separate, versioned mapping layer:

- Same symbol in spot and futures on one venue: high confidence.
- Same symbol across venues: medium confidence.
- Unit prefixes such as `1000SATS` map to `SATS` only when the unprefixed
  counterpart exists elsewhere in the observed universe.
- Venue conventions such as MEXC `AMATSTOCK` generate candidates. They are
  accepted as `AMAT` only when display ticker, stock classification, Binance
  index origin, and an active Binance futures counterpart all agree.
- Provider-scoped tags belong to canonical assets, for example
  `BINANCE:MONITORING`, `BINANCE:SEED`, `BINANCE:SOLANA`, and `BINANCE:MEME`.
  Binance tags use its public, keyless product metadata endpoint. Raw labels
  and add/remove history are retained. Gate.com `st_tag` is projected as
  `GATE:ST`; Bitget spot area membership and futures RWA classification are
  projected as `BITGET:AREA` and `BITGET:RWA`.
- Single isolated symbol: low confidence.

Ticker shape is evidence, not proof. Every mapping stores method, confidence,
matcher version, and evidence JSON. Candidate decisions are stored as
`PROPOSED`, `ACCEPTED`, or `REJECTED`; mapping revisions remain after decisions
change. Ambiguous candidates and rebrands can later add price-correlation,
external identity, or manual-review evidence without rewriting raw history.

CoinGecko currently provides a keyless public API suitable for low-volume
prototyping. It is the preferred first enrichment source. No external source is
required for initial collection.

## Master view

`/mdv` is asset-first rather than a flat market table:

```text
canonical asset -> venue base symbols -> active spot/futures markets
```

The main row shows spot venues, futures venues, and futures coverage. Coverage
uses the active futures venues currently stored, such as `ALL · 3/3` or
`BYBIT ONLY · 1/3`.
Expand a row to inspect venue symbols and active markets grouped by venue.
Each market symbol links directly to its exact Binance, Bitget, Bybit, Gate.com,
or MEXC spot/futures trading page. Futures details show the maximum market-order
size reported by Binance, Bitget, Bybit, Gate.com, or MEXC when that venue
publishes one. The value is refreshed with each successful snapshot and is also
exposed as `max_market_order_size` in market and asset JSON projections.

Use the `Columns` button at the left of the `/mdv` header to show, hide, or
reorder table columns. Drag-and-drop is supported, with `Alt+ArrowUp` and
`Alt+ArrowDown` available for keyboard reordering. The browser stores the
selection in the `mdv_columns` cookie for one year; `Reset defaults` restores
the original layout.

Submitted filter URLs omit empty fields. Futures exclusions use the readable
`FUTURES!=MEXC` form rather than percent-encoding the exclamation mark.

Inactive pairs and contracts never appear in the master view. They remain in
SQLite observations and lifecycle history for audit. Raw market API requests
also default to active markets; pass `ACTIVE=false` only when intentionally
querying inactive records.

Unit-prefixed futures remain explicit. For example, `1000BONK` maps to asset
`BONK`, keeps venue symbol `1000BONK`, and reports underlying unit `1000 BONK`.
The multiplier is not appended to the venue symbol.

## HTTP API

- `GET /mdv` HTML current asset view
- `GET /logs` HTML collection-run and change history
- `GET /metadata` HTML filter definitions and currently available values
- `GET /api/v1/assets` JSON asset hierarchy and venue coverage
- `GET /api/v1/markets` JSON raw active-market view
- `GET /api/v1/logs` JSON collection-run and change history
- `GET /api/v1/metadata` filter definitions and currently available values
- `GET /api/v1/stats` collection statistics
- `POST /api/v1/refresh` collect every venue
- `POST /api/v1/refresh?VENUE=BINANCE` collect one venue
- `GET /health` health check

Asset-view filters are `TYPE`, `CONTRACT`, `FUTURES`, `STOCK`, `TAG`,
`VENUE`, `PRODUCT`, `SYMBOL`, `LIMIT`, and `OFFSET`:

- `CONTRACT=PERP`, `CONTRACT=DATED`, `CONTRACT=CQ`, and `CONTRACT=NQ` select
  normalized contract codes.
- `FUTURES=BINANCE,MEXC` requires futures on both venues.
- `FUTURES=BINANCE` requires Binance and permits other venues.
- `FUTURES=BINANCE&FUTURES!=MEXC` requires Binance and excludes MEXC.
- `STOCK=1` keeps stock-classified assets; `STOCK=0` excludes them.
- `TAG=BINANCE:MONITORING` requires that active tag on the canonical asset.
  Repeat `TAG` or use commas to require multiple tags.

Lowercase query names are accepted too. `SYMBOL` searches canonical assets,
venue base symbols, and raw market symbols and supports `*` wildcards. The raw
market API additionally supports `STATUS` and `ACTIVE`.

The default listener is localhost-only. Authentication is mandatory on every
route, including `/health`. Add TLS before binding it to a public interface.

`GET /metadata` and `GET /api/v1/metadata` describe every query filter as HTML
and JSON, respectively. Enum entries include values available from the current
active universe; text and integer entries include their wildcard or range
constraints. `FUTURES` is marked as a multi-value filter supporting `=` and
`!=`; `TAG` is also marked as multi-value.

## Collection log

Every collection invocation is stored as a durable parent run before network
discovery starts. The parent records whether the request covered all venues or
one venue, plus start/completion times, aggregate status, universe counts,
record counts, and errors. Each existing per-universe `ingest_runs` row links
to its parent. Migration `005_collection_runs.sql` preserves earlier ingest
runs as standalone legacy parent runs.

Migration `006_group_legacy_collection_runs.sql` groups adjacent legacy
per-universe rows from the same invocation into one parent run. This removes
duplicate timestamp entries while retaining each venue and universe result.

`/logs` presents runs newest-first in a commit-log-style timeline. Changes are
grouped by venue and include market listings, removals, activation and status
changes, and provider-scoped tag additions/removals. Successful snapshots with
no lifecycle or tag differences are shown explicitly as `No changes`. Failed
universes and source errors remain visible in expandable run details.

The log timezone selector formats run and change timestamps in the browser.
Its IANA timezone is stored for one year in the `mdv_timezone` cookie. Raw JSON
timestamps remain ISO 8601 values from SQLite. `GET /api/v1/logs` accepts
`VENUE`, `LIMIT`, and `OFFSET`.

## Unix/systemd installation

The deployment bundle installs one long-running API and one scheduled
collection job:

- `asset-master-data.service`: localhost API, enabled for boot.
- `asset-master-refresh.service`: one-shot collection.
- `asset-master-refresh.timer`: schedule rendered from
  `collection.schedule`; enabled and started by the installer.

Prepare the units without starting the API:

```bash
make install
.venv/bin/python -m mdv.cli entitlement USER --password-file /secure/password-file
bash deploy/systemd/install_systemd.sh
```

The installer validates the configured systemd calendar, protects the local
data directory, enables the API for the next boot, starts the collection timer,
and deliberately leaves the API stopped. To perform a complete deployment,
including dependency sync, migration, initial collection, and API start:

```bash
bash deploy/systemd/deploy.sh
```

The deployment script does not overwrite `config/config.yaml`,
`config/entitlements.yaml`, or an existing SQLite database. Re-running it is
safe. Inspect operations with:

```bash
systemctl status asset-master-data.service
systemctl list-timers asset-master-refresh.timer
journalctl -u asset-master-data -u asset-master-refresh --since today
```

## Consumer configuration

For the crypto bot on the same host, use the loopback listener:

```yaml
asset_master:
  base_url: http://127.0.0.1:8090
  timeout_seconds: 1.0
```

Set the matching plaintext credentials only in the bot's ignored `.env`:

```text
ASSET_MASTER_USERNAME=tradier
ASSET_MASTER_PASSWORD=generated-password
```

The service-side `tradier` entry remains a one-way scrypt hash in the ignored
entitlements file. Do not place either plaintext password or generated
entitlements in Git.
