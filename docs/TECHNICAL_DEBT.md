# Technical debt

This file records architectural gaps, not already implemented behavior. Runtime
and operator behavior belongs in the README and `OPERATIONS_REFERENCE.md`; the
external contract belongs in `docs/API.md`.

## 1. Durable identity independent of ticker

`asset_id` is still derived from `canonical_symbol`. A ticker rename therefore
creates a new identity, while unrelated assets sharing a ticker cannot be
represented safely without manual conventions.

Target state:

- Opaque durable asset IDs independent of ticker text.
- Effective-dated symbol and external-identifier claims.
- Explicit, reversible rename/split/merge decisions with actor, reason, and
  timestamp.
- Migration preserving every legacy asset ID and mapping revision without
  silently collapsing identities.
- Optional enrichment adapters that fail open for collection and never make a
  suffix, tag, or venue count sufficient merge evidence.

## 2. Normalize all provider evidence at connector boundaries

Generic projection still interprets selected provider fields from stored
`raw_json` for classifications, tags, and alias hints. Raw payload preservation
is correct, but provider semantics should not leak past connectors.

Target state:

- A versioned normalized evidence envelope for classification, tag, alias,
  reference-venue, and routing evidence.
- Connector/evidence-adapter version recorded with each observation.
- Matching, storage, and UI consuming only normalized evidence.
- Monotonic backfill from existing raw payloads, retaining those payloads
  unchanged.

## 3. Complete connector capability registration

The registry owns connector factories, supported venues, and trade links, but
some HTTP behavior remains global.

Target state: one venue registration declares display metadata, universes,
normalized evidence adapter, trade-link builder, request headers, retry and
rate policy, pagination, supported products, and health diagnostics. A fixture
venue should require one registration and no edits to collection, CLI,
resolution validation, templates, or projection code.

## 4. Define response models for every API

Batch mapping has explicit Pydantic request/response models; assets, markets,
financing, logs, metadata, stats, and health still return dynamic dictionaries.

Target state:

- Explicit models for nullability, enum values, and numeric/string choices.
- Consistent `count`/`total` pagination semantics. Assets and financing currently
  report pre-pagination totals; markets report returned rows.
- Stable validation error codes and contract tests for documentation examples.
- An OpenAPI snapshot and a written compatibility/deprecation policy before an
  `/api/v2` is introduced.

## 5. Extend mutation audit and browser hardening

Reader/operator separation, bounded scrypt work, failed-auth throttling, and
403/401 behavior are implemented. Remaining work is to record the authenticated
actor on local manual-action revisions and add explicit anti-CSRF tokens if the
browser mutation surface is ever exposed beyond its current trusted-host
boundary. More granular scopes are only needed when additional mutations are
introduced.

## 6. Operational telemetry and encrypted recovery

Readiness exposes collection freshness, running work, active markets, and
database bytes; systemd uses host-survival memory/task limits and deployment
keeps bounded release and backup sets. Remaining work:

- Export structured metrics for collection duration, endpoint latency, retry
  counts, database/WAL growth, compaction deletions, and auth throttling.
- Alert before disk, readiness-age, or repeated partial-collection thresholds
  become operational failures.
- Automate encrypted off-host database and entitlement backups. Default local
  archives intentionally exclude entitlements and are not encrypted.
- Schedule recovery drills and record recovery-point/recovery-time evidence.
- If recovery must become one indivisible namespace switch across directories,
  add a staged-root/symlink protocol; current restore validates and stages all
  entries, preserves every original, atomically replaces each configured file,
  and rolls the full promotion set back on error.

## Required regression coverage for these items

Future work must retain the existing guarantees:

- Empty, partial, malformed, failed, overlapping, and out-of-order snapshots do
  not regress the current catalog.
- Recent changed raw evidence survives payload compaction; older change rows
  retain hashes/state until the configured hard row ceiling. Lifecycle/tag
  events, candidates, and mapping revisions are not pruned by audit compaction.
- Suffix-only aliases never merge without the documented independent evidence.
- Unit prefixes do not merge without an unprefixed counterpart.
- Migrations work from an empty database and the previously released schema.
- Recorded connector fixtures run before any optional live check.
- Mapping resolution remains projection-free: one indexed read transaction, no
  `list_assets()`, no `raw_json`, with authenticated 1/10/100-symbol and
  concurrent latency benchmarks preserved.

Completion reports must include migrations, API compatibility, tests and
benchmarks, changed files, unresolved ambiguity, and confirmation that no push,
deployment, or live collection occurred unless separately authorized.
