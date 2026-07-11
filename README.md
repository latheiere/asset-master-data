# Asset Master Data

Asset Master Data is a local-first service that turns public exchange catalogs
into one auditable view of canonical assets, spot and futures markets, and
financing eligibility.

It answers questions such as:

- Which venues currently trade an asset?
- Which active perpetual or dated contracts exist for it?
- Which assets are eligible for cross margin or crypto loans?
- How did a venue symbol map to the canonical asset?
- When did a market appear, disappear, or change status?

The service collects public endpoints without exchange credentials, stores the
observations in SQLite, and serves authenticated HTML and JSON views. It is an
independent master-data service, not a trading engine, price feed, or order
routing service.

> Development disclosure: this repository has been developed primarily through
> Codex-assisted “vibe coding,” with human direction and test-based review. The
> current release is `0.10.1`; audit behavior, security, and operational controls
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

- Complete-snapshot collection with failure isolation per venue universe, bounded
  fetch concurrency, and one derived-mapping rebuild after each refresh.
- Raw venue fields, observation timestamps, and inactive-market history.
- Normalized product, settlement, direction, expiry, status, and size fields.
- Evidence-based asset matching with candidate decisions and mapping revisions.
- Provider-scoped, versioned asset tags.
- Separate cross-margin and crypto-loan eligibility catalogs.
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

| Venue | Spot | Futures | Cross margin | Crypto loans |
| --- | --- | --- | --- | --- |
| Binance | Spot | USD-M and COIN-M | Margin-enabled pairs | — |
| BitMart | Spot | USDT/USDC- and coin-margined perpetuals and delivery futures | — | — |
| Bitget | Spot | USDT-M, USDC-M, and Coin-M | Pair eligibility | Loan and collateral assets |
| Bitfinex | Spot | Perpetuals | Public margin-pair eligibility | — |
| Bybit | Spot | Linear and inverse perpetuals and dated futures | Asset eligibility and rate tiers | Loan and collateral assets |
| Coinbase | Spot | USDC-settled perpetuals | Margin-enabled pairs | — |
| Deribit | Spot | Linear/inverse perpetuals and dated futures | — | — |
| Gate.com | Spot | USDT/BTC perpetuals and USDT delivery futures | Asset eligibility | Loan and collateral assets |
| Gemini | Spot | Linear and inverse perpetuals | — | — |
| HTX | Spot | USDT/coin-margined perpetuals and delivery futures | — | — |
| Hyperliquid | Spot | Core and HIP-3 perpetuals | — | — |
| KuCoin | Spot | Perpetuals and dated futures | Public currency catalog | — |
| MEXC | Spot | Perpetuals | — | — |
| OKX | Spot | Linear/inverse perpetuals and dated futures | — | — |
| WhiteBIT | Spot | Crypto and TradFi perpetuals | — | — |
| XT | Spot | Linear perpetuals and dated futures | Pair eligibility and rates | Loan and collateral assets |

Binance and Coinbase eligibility comes from public pair-level market flags;
Bitget, Bitfinex, and XT also publish pair-level evidence. Bybit, Gate, and KuCoin publish
asset-level margin catalogs. Gate and XT publish public loan and collateral
catalogs; XT additionally provides regular-user rates, terms, limits, and pledge
thresholds. Binance, BitMart, Coinbase, Deribit, Gemini, KuCoin, and MEXC do not expose credential-free
crypto-loan catalogs, so those universes are not collected. MEXC, HTX, and OKX
do not expose a complete public margin catalog; Hyperliquid and WhiteBIT do not
expose a complete public financing catalog.

Venue-specific parsing and trade-link rules live behind a shared connector
registry. Collection, API validation, and CLI help derive from that registry;
UI venue choices derive from collected metadata. A new venue does not require
parallel hardcoded lists.

## Data model and guarantees

The primary view is asset-first:

```text
canonical asset → venue base symbols → active spot/futures markets
```

Public financing catalogs are stored separately from trading markets:

```text
canonical asset → venue → CROSS_MARGIN or CRYPTO_LOAN → eligibility metadata
```

Cross-margin borrowing and collateralized crypto loans remain separate products.
Financing records retain asset roles (`BORROWABLE` or `COLLATERAL`), published
rates and tiers, terms, platform limits, source evidence, observations, and
lifecycle history. Regular-user rates are the preferred summary value.

Financing symbols map to canonical assets only through an existing exact
same-venue market symbol. Unmapped records remain available from the financing
API without creating or merging an asset. Account eligibility, personalized
limits, balances, positions, credit, and real-time inventory are out of scope.

Raw venue symbols and source fields are retained. Connector-added derived
metadata uses a reserved `_metadata` namespace; it does not replace venue
fields. Canonical changes update versioned mappings instead of rewriting market
history. Renamed and delisted markets remain available for audit.

Only complete successful snapshots can mark previously active markets or
financing records missing. A partial, empty, failed, or malformed response
records an error and preserves the previous current view. Each market and
financing catalog is applied transactionally and independently. Current raw
payloads remain available; observation history retains raw payloads only for
lifecycle, eligibility, or status transitions, while every observation keeps
its timestamp, normalized state, and content hash.

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
- Market and financing symbols ending in `STOCK` are an explicit venue
  convention: they map to the same canonical symbol without `STOCK`, while
  retaining the original venue symbol and mapping evidence.
- Delivery-managed manual actions can map a reviewed venue symbol, rename a
  canonical asset, or retain another review note. They are packaged as tracked
  data and reconciled into every installation; local UI edits override the
  delivered row without being overwritten on upgrade.

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
http://127.0.0.1:8090/coverage?TYPE=FUTURE
http://127.0.0.1:8090/asset?SYMBOL=BTC*
http://127.0.0.1:8090/mdv?TYPE=FUTURE
http://127.0.0.1:8090/mdv?PRODUCT=PERP&FUTURES=BINANCE,MEXC
http://127.0.0.1:8090/mdv?PRODUCT=PERP&FUTURES=BINANCE&FUTURES!=MEXC
http://127.0.0.1:8090/mdv?PRODUCT=PERP&SETTLE=USDC
http://127.0.0.1:8090/mdv?TAG=BINANCE:MONITORING
http://127.0.0.1:8090/mdv?FINANCING=BINANCE:MARGIN
http://127.0.0.1:8090/mdv?TYPE=SPOT&VENUE=BINANCE
http://127.0.0.1:8090/mdv?SYMBOL=BTC*
```

`/coverage` is the compact comparison view: it separates spot, perpetual,
dated, margin, and loan availability. `/asset` is the asset explorer, with
click-to-expand venue detail, native symbols, markets, and trading links.
`/mdv` remains a compatible alias for `/asset`. Full rates, tiers, terms,
limits, collateral roles, raw evidence, and unmapped records stay in the JSON
API.

`/logs` shows collection outcomes and lifecycle/tag changes in 10-run pages.
It supports action, provider-scoped tag, venue, symbol, product, and inclusive
UTC date filters. `/metadata` describes filter meanings and current values.

Every data filter supports `=` and `!=`. Values may be repeated or
comma-separated. `SYMBOL` supports `*` wildcards. The primary filters submit on
selection; uncommon dimensions are grouped under Advanced filters.

`/manual-actions` provides CRUD for reviewed mapping, name-change, and other
asset actions. `MAP_SYMBOL` applies to an exact venue base symbol; `RENAME_ASSET`
applies globally after normal matching; `OTHER` is an auditable note only.
Bundled actions live in `src/mdv/manual_asset_actions.json` and are reconciled
on migration. A local edit or delete takes precedence for that installation.

Collection logs distinguish a first observed market (`MARKET_DISCOVERED`) from
a later listing for an asset that already had a market of that type on the venue
(`MARKET_LISTED`). Both are returned by `ACTION=LISTING`.

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

Use `mdv --config PATH ...` for another YAML file. Environment variables do not
override behavioral YAML settings, so interactive and systemd runs consume the
same configuration. Systemd injects `MDV_GIT_SHA` only as deployment identity.
Runtime data defaults to `.data/` and remains ignored by Git.

## CLI and development

```bash
mdv --config config/config.yaml init
mdv --config config/config.yaml collect
mdv --config config/config.yaml collect --venue BINANCE
mdv --config config/config.yaml collect --exclude-venue XT
mdv --config config/config.yaml bundle-export --venue XT --output xt-bundle.json
mdv --config config/config.yaml bundle-import xt-bundle.json
mdv --config config/config.yaml stats
mdv --config config/config.yaml serve --host 127.0.0.1 --port 8090
make test
```

Collection bundles provide a transport-neutral path for venues that are not
reachable from the database host. Export fetches every registered universe for
one venue without touching a database. Import checks the bundle format,
checksum, registry metadata, complete source set, and each snapshot before
applying successful universes transactionally. Failed universes are recorded
without marking their previous records missing. `--output -` writes a bundle to
standard output for an external SSH or file-transfer wrapper.

The Makefile accepts an ignored `Makefile.local` override of `COLLECT_COMMAND`
for host-specific collection orchestration. Remote host names and transfer
policy remain outside the package and tracked repository.

Contributor and coding-agent constraints, migration rules, extension points,
and required validation reporting live in [AGENTS.md](AGENTS.md). Keep
human-facing setup and behavior here; keep external API contracts in
[docs/API.md](docs/API.md).

## Releases and versioning

Releases use Semantic Versioning. `project.version` in `pyproject.toml` is the
only editable version source; runtime code, OpenAPI, and `mdv --version` read the
installed package metadata. `/health` reports both the release version and the
exact deployed Git revision.

Version commits use an annotated immutable `vX.Y.Z` tag. Production deployment
accepts only `main` HEAD when that tag matches `project.version`:

```bash
# Replace X.Y.Z after updating project.version, release notes, and tests:
git commit -m "chore(release): prepare X.Y.Z"
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push origin main
git push origin vX.Y.Z
make deploy-prod
# Run only when deployment changes the collected universe or parsing behavior.
make collect-prod
```

Do not bump for every commit. Bump once per release: patch for compatible fixes,
minor for compatible features, and major for incompatible public API changes.
Before `1.0.0`, use a minor bump for feature or compatibility-breaking releases.

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

Complete dependency sync, migration, and API start:

```bash
bash deploy/systemd/deploy.sh
```

The deploy script refuses dirty worktrees, untagged commits, mismatched versions,
lightweight tags, and tags that do not identify current `main` HEAD.

Deployment does not force a collection. Run `make collect-prod` only when the
release changes collection behavior, such as adding a venue or a financing
catalog; scheduled collection continues through `asset-master-refresh.timer`.

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
