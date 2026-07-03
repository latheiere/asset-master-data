# Coding Agent Guide

This repository is an independent asset master-data service. It discovers
public exchange universes, records market lifecycle state, matches venue
symbols to canonical assets, and serves local HTML/JSON views.

Human-facing project behavior belongs in `README.md`. Contracts consumed by
external services belong in `docs/API.md`. Known implementation gaps and
acceptance criteria belong in `docs/TECHNICAL_DEBT.md`; do not present them as
implemented behavior.

## Boundaries

- Do not import code, configuration, databases, or runtime state from sibling
  projects or trading systems.
- Integration with consumers must use documented HTTP APIs, exported bundles,
  or explicit service hooks.
- Never put exchange credentials in this repository. Initial discovery uses
  public endpoints only.
- Runtime data belongs in `.data/` or the path selected by `database.path` in
  YAML configuration; it is not source code and must remain ignored by Git.
- A ticker is evidence, not a stable identity. Preserve matching method,
  confidence, version, and history.
- Never merge identities from a suffix such as `STOCK` alone. Require
  independent evidence and store every candidate decision and evidence JSON.
- Tags belong to canonical assets, are provider-scoped (`BINANCE:SEED`), and
  retain raw provider labels plus add/remove history.
- Preserve raw venue symbols and observations. Canonical changes update only
  versioned mappings; renamed or delisted venue records remain auditable.
- Never mark absent markets as missing from a partial or failed snapshot.
- Keep raw source payloads and observed timestamps so normalized data remains
  auditable.
- Keep `/mdv` asset-first: canonical asset, venue base symbols, then markets.
  Matching evidence belongs in audit data, not the primary operational table.
- Show active markets only in `/mdv`. Preserve inactive markets in lifecycle
  history and expose them only through explicit audit queries.
- Display unit-prefixed futures as venue symbol `1000BONK` and underlying unit
  `1000 BONK`; never render the multiplier twice.
- Normalize product duration as `SPOT`, `PERP`, or `DATED`. Keep quote,
  settlement asset, linear/inverse direction, and expiry cycle in separate
  fields. Raw exchange payload values and venue-native labels remain unchanged.
- Keep `venue_product` and `venue_status` for source semantics. Venue-specific
  URLs and routing must use `venue_product`, never overloaded normalized fields.
- Do not infer an expiry cycle when the source publishes only an exact expiry;
  keep `expiry_cycle` null and preserve `expires_at`.
- Synthetic lifecycle states such as `MISSING` are not venue statuses. Backfill
  normalization in migrations before collection to avoid false status events.
- Every data filter supports `=` and `!=`. Update raw-market queries, asset-view
  queries, metadata, URL canonicalization, and tests together.
- Keep latency-sensitive mapping APIs projection-free: one indexed read
  transaction, no `list_assets()`, no `raw_json`, and a synchronous/thread-pool
  route. Benchmark authenticated 1/10/100-symbol and concurrent requests.

## Extension architecture

- Add venues through the connector registry. Collection, CLI help, API venue
  validation, UI options, and trade-link dispatch must derive from it rather
  than repeat venue lists.
- Keep provider parsing and field names at the connector/evidence-adapter
  boundary. Generic matching, storage, and projection code consumes normalized
  evidence, never a required provider pair.
- Model aliases as candidates with independent evidence. Reference venues come
  from normalized provider evidence; do not require Binance, MEXC, or another
  fixed corroboration venue in the matching engine.
- Venue-specific product/status translation and URL routing are expected, but
  must be registered capabilities rather than conditionals spread across the
  service.
- New connectors require recorded success and malformed/partial fixtures,
  lifecycle safety tests, normalized-field tests, and registry-extension tests.

## Documentation ownership

- `README.md`: purpose, quick start, supported behavior, data guarantees,
  operation, and links to detailed contracts.
- `docs/API.md`: every external HTTP route, auth, filters, schemas, examples,
  errors, compatibility, and pagination semantics.
- `AGENTS.md`: coding-agent constraints, workflow, architecture invariants, and
  validation requirements. Do not move these instructions into README.
- Update `docs/TECHNICAL_DEBT.md` when a documented invariant is intentionally
  deferred. Remove items only after code, migration, tests, and docs agree.

## Workflow

1. Run `git status --short --branch`.
2. Read `README.md`, relevant modules, migrations, and tests.
3. Make focused changes with backward-compatible schema migrations.
4. Run `.venv/bin/python -m pytest -q`.
5. For connector changes, test recorded fixtures before optional live calls.
6. Update `README.md` for behavior, API, configuration, schema, or operations
   changes.
7. Do not create an origin, push, deploy, or expose the service publicly unless
   explicitly requested.
8. After an authorized production deploy, verify the deployed commit, schema,
   collection result, API service, refresh timer, and authentication boundary.

## Data safety

- Never delete or overwrite an existing SQLite database without explicit
  approval.
- Use transactions for complete snapshot application.
- Failed or malformed discovery must record an error and preserve the previous
  current view.
- Use SQLite backup APIs for live backups; do not copy only the main database
  file while WAL mode is active.
- Schema migrations must be monotonic and tested from an empty database and
  the previous schema.

## Local commands

```bash
make install
make test
make collect
make serve
```

Default UI:

```text
http://127.0.0.1:8090/mdv?TYPE=FUTURE
```

## Validation report

At task completion, report:

- Tests and live checks run
- Files changed
- Schema/API behavior changed
- Origin, push, and deployment status
- Any source endpoint failures or unresolved matching ambiguity
