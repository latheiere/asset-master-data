# Asset Master Data

Asset Master Data is a local-first service that turns public exchange market
catalogs into one auditable view of canonical assets and their spot and futures
markets.

It answers questions such as:

- Which venues currently trade an asset?
- Which active perpetual or dated contracts exist for it?
- How did a venue symbol map to the canonical asset?
- When did a market appear, disappear, or change status?

The service collects public endpoints without exchange credentials, stores the
observations in SQLite, and serves authenticated HTML and JSON views. It is an
independent master-data service, not a trading engine, price feed, or order
routing service.

> Development disclosure: this repository has been developed primarily through
> Codex-assisted “vibe coding,” with human direction and test-based review. The
> current release is `0.1.0`; audit behavior, security, and operational controls
> before relying on it in a production or risk-sensitive system.

## What it provides

```text
public venue catalogs
        ↓
raw observations + lifecycle history
        ↓
versioned identity mappings
        ↓
canonical asset → venue base symbols → active markets
        ↓
authenticated HTML and JSON APIs
```

- Complete-snapshot collection with failure isolation per venue universe.
- Raw venue fields, observation timestamps, and inactive-market history.
- Normalized product, settlement, direction, expiry, status, and size fields.
- Evidence-based asset matching with candidate decisions and mapping revisions.
- Provider-scoped, versioned asset tags.
- Asset-first UI, collection log, metadata view, and consumer API.
- Local SQLite storage with WAL, foreign keys, transactions, and migrations.

## Quick start

Requirements: Python 3.11 or newer. The Makefile uses `python3.13` by default;
set `PYTHON_BOOTSTRAP` to another supported interpreter when needed.

```bash
make install
.venv/bin/python -m mdv.cli entitlement admin --password-file /secure/password-file
make collect
make serve
```

Open [http://127.0.0.1:8090/mdv](http://127.0.0.1:8090/mdv) and sign in as
`admin`. The password file is input only and may be removed after user creation.

`config/entitlements.yaml` is generated with mode `0600`. It contains scrypt
password hashes and a random session-signing secret, and is ignored by Git.
`config/entitlements.example.yaml` documents its non-secret structure.

The server performs one collection on startup only when the database is empty.
Set `server.refresh_on_startup: never` to disable it, or run `mdv serve
--refresh` to force collection before serving.

## Supported universes

| Venue | Spot | Futures |
| --- | --- | --- |
| Binance | Spot | USD-M and COIN-M |
| Bitget | Spot | USDT-M, USDC-M, and Coin-M |
| Bybit | Spot | Linear and inverse perpetuals and dated futures |
| Gate.com | Spot | USDT/BTC perpetuals and USDT delivery futures |
| MEXC | Spot | Perpetuals |

Venue-specific parsing and trade-link rules live behind a shared connector
registry. Collection, API validation, and CLI help derive from that registry;
UI venue choices derive from collected metadata. A new venue does not require
parallel hardcoded lists.

## Data model and guarantees

The primary view is asset-first:

```text
canonical asset → venue base symbols → active spot/futures markets
```

Raw venue symbols and source fields are retained. Connector-added derived
metadata uses a reserved `_metadata` namespace; it does not replace venue
fields. Canonical changes update versioned mappings instead of rewriting market
history. Renamed and delisted markets remain available for audit.

Only complete successful snapshots can mark previously active markets missing.
A partial, empty, failed, or malformed response records an error and preserves
the previous current view. Snapshot application is transactional.

Normalized dimensions remain independent:

| Field | Meaning |
| --- | --- |
| `market_type` | `SPOT` or `FUTURE` |
| `product` | `SPOT`, `PERP`, or `DATED` |
| `contract_type` | Backward-compatible alias of `product` |
| `quote_symbol` | Price-denomination asset |
| `settle_symbol` | Futures settlement or margin asset; null for spot |
| `contract_direction` | `LINEAR`, `INVERSE`, or `QUANTO`; null for spot |
| `expiry_cycle` | Venue-published cycle: `W`, `BW`, `TW`, `M`, `BM`, `Q`, `BQ`, or `TQ` |
| `expires_at` | Exact UTC expiry timestamp for a dated contract |
| `status` | Normalized operational status |
| `active` | Whether the market belongs in the current operational view |
| `venue_product` | Venue-native universe or product classification |
| `venue_status` | Venue-native status label normalized only for case |
| `contract_multiplier` | Venue-reported contract value |
| `underlying_multiplier` | Canonical unit prefix, such as `1000` |
| `max_market_order_size` | Venue-reported maximum market-order quantity |

An exact expiry does not imply a cycle. `expiry_cycle` remains null unless the
source explicitly identifies one.

## Identity matching

A ticker is evidence, not stable identity. Every current mapping stores its
method, confidence, matcher version, and evidence; changed mappings create
revisions. Candidate records support `PROPOSED`, `ACCEPTED`, and `REJECTED`;
automatic rules currently emit proposed or accepted decisions.

Current evidence rules are:

- Same normalized symbol in spot and futures on one venue: high confidence.
- Same normalized symbol across venues: medium confidence.
- Isolated symbol: low confidence.
- Unit prefix, such as `1000SATS` → `SATS`: accepted only when an unprefixed
  counterpart exists in the observed universe.
- Provider alias hints, such as a `STOCK` suffix: accepted only when display
  symbol, asset classification, declared reference venue, active reference
  market, and reference classification all agree.

Alias corroboration is provider-neutral. It uses the venues declared by source
metadata rather than requiring a specific venue pair. A suffix alone never
merges identities.

Unit-prefixed contracts stay explicit: venue symbol `1000BONK`, canonical asset
`BONK`, underlying unit `1000 BONK`. The multiplier is never appended twice.

Durable identity beyond ticker evidence, external enrichment, and manual-review
workflow remain open work; see [Technical debt](docs/TECHNICAL_DEBT.md).

## Using the web UI

Useful starting views:

```text
http://127.0.0.1:8090/mdv?TYPE=FUTURE
http://127.0.0.1:8090/mdv?PRODUCT=PERP&FUTURES=BINANCE,MEXC
http://127.0.0.1:8090/mdv?PRODUCT=PERP&FUTURES=BINANCE&FUTURES!=MEXC
http://127.0.0.1:8090/mdv?PRODUCT=PERP&SETTLE=USDC
http://127.0.0.1:8090/mdv?TAG=BINANCE:MONITORING
http://127.0.0.1:8090/mdv?TYPE=SPOT&VENUE=BINANCE
http://127.0.0.1:8090/mdv?SYMBOL=BTC*
```

`/mdv` shows active markets only. Expand an asset to inspect venue symbols and
markets, including exact venue trading links. `/logs` shows collection outcomes
and lifecycle/tag changes. `/metadata` describes filter meanings and current
values.

Every data filter supports `=` and `!=`. Values may be repeated or
comma-separated. `SYMBOL` supports `*` wildcards. The Columns control changes
visibility and order, stored locally in a cookie.

## Authentication and network boundary

Every route, including `/health`, requires authentication. API clients use HTTP
Basic Auth. Browser requests redirect to `/login` and receive a signed,
expiring, HttpOnly, SameSite cookie after login; the cookie does not contain the
password.

The default listener is `127.0.0.1`. Keep it on a trusted host or network. Add
TLS and set `auth.session_cookie_secure: true` before browser access over HTTPS.

External-service API contracts, request/response examples, filters, and error
behavior are documented separately in [HTTP API](docs/API.md). Coding agents
must also follow [AGENTS.md](AGENTS.md).

## Configuration

Runtime configuration is `config/config.yaml`:

```yaml
database:
  path: .data/mdv.sqlite3

server:
  host: 127.0.0.1
  port: 8090
  refresh_on_startup: if-empty  # always | if-empty | never

collection:
  http_timeout_seconds: 20
  schedule: "*-*-* 00:00:00 UTC"  # systemd OnCalendar syntax

auth:
  entitlements_path: config/entitlements.yaml
  session_cookie_name: mdv_session
  session_ttl_seconds: 43200
  session_cookie_secure: false
```

Use `mdv --config PATH ...` for another YAML file. Runtime environment variables
do not override YAML, so interactive and systemd runs consume the same settings.
Runtime data defaults to `.data/` and remains ignored by Git.

## CLI and development

```bash
mdv --config config/config.yaml init
mdv --config config/config.yaml collect
mdv --config config/config.yaml collect --venue BINANCE
mdv --config config/config.yaml stats
mdv --config config/config.yaml serve --host 127.0.0.1 --port 8090
make test
```

Contributor and coding-agent constraints, migration rules, extension points,
and required validation reporting live in [AGENTS.md](AGENTS.md). Keep
human-facing setup and behavior here; keep external API contracts in
[docs/API.md](docs/API.md).

## Unix/systemd deployment

The deployment bundle installs:

- `asset-master-data.service`: localhost API, enabled for boot.
- `asset-master-refresh.service`: one-shot collection.
- `asset-master-refresh.timer`: collection schedule from configuration.

Prepare units without starting the API:

```bash
make install
.venv/bin/python -m mdv.cli entitlement USER --password-file /secure/password-file
bash deploy/systemd/install_systemd.sh
```

Complete dependency sync, migration, initial collection, and API start:

```bash
bash deploy/systemd/deploy.sh
```

Deployment does not overwrite configuration, entitlements, or an existing
SQLite database. Inspect it with:

```bash
systemctl status asset-master-data.service
systemctl list-timers asset-master-refresh.timer
journalctl -u asset-master-data -u asset-master-refresh --since today
```

Back up a live WAL database through SQLite’s backup API; do not copy only the
main database file.

## License

Apache License 2.0. See [LICENSE](LICENSE).
