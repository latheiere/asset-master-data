# Technical Debt Implementation Request

This request covers known gaps in the current `0.2.0` implementation relative to
the durable, provider-neutral service described in the README. It is not a list
of already implemented features.

## Goal

Make canonical identity, provider evidence, API contracts, and authorization
safe for long-lived external use without weakening raw-data auditability or
existing API compatibility.

## Required work

### 1. Replace ticker-derived asset identity

Current `asset_id` is a UUID derived from `canonical_symbol`. This makes ticker
changes create new identities and makes unrelated assets with the same ticker
indistinguishable. It conflicts with the rule that a ticker is evidence rather
than stable identity.

Implement durable opaque asset IDs and a versioned identifier model:

- Add canonical asset identity independent of symbol.
- Store canonical symbols and external identifiers as effective-dated claims.
- Allow multiple symbol claims and explicit rename/split/merge decisions.
- Preserve every previous market mapping and candidate decision.
- Never auto-merge on ticker, suffix, tag, or venue count alone.
- Add manual `ACCEPTED`/`REJECTED` review operations with actor, reason, and
  timestamp.
- Add at least one optional external-identity adapter behind a generic
  interface; collection must continue when enrichment is unavailable.

Migration must preserve existing asset IDs through an explicit legacy mapping,
not silently regenerate or collapse them.

### 2. Persist normalized provider evidence

Current generic projection still recognizes provider payload fields such as
`conceptPlate`, `indexOrigin`, `underlyingType`, and `symbolType` while reading
`raw_json`. This is backward-compatible, but provider semantics should stop at
the connector boundary.

Implement a normalized, versioned evidence envelope or tables for:

- Asset classifications such as `EQUITY`.
- Provider-scoped tags and raw labels.
- Alias hints: proposed symbol, rule, display match, reference venues, and raw
  source pointers.
- Trade-link routing capabilities.
- Evidence schema version and connector version.

Connectors must translate provider fields into this contract. Matching, store,
UI, and API code must consume only normalized evidence. Preserve the original
provider payload separately and unchanged. Backfill existing rows through a
monotonic migration or explicit rebuild command, with tests from schema 12.

### 3. Complete connector capability registration

The shared registry now owns connector factories, supported venues, and trade
URLs. Extend it so one registration describes all venue capabilities:

- Display name and stable venue key.
- Connector universes and normalized evidence adapter.
- Trade URL builder.
- Optional request headers, rate limits, retry policy, and pagination policy.
- Supported market types/products and health diagnostics.

Remove global HTTP behavior that exists for one source’s CDN. Adding a fixture
venue in tests must require one registration and no edits to collection, CLI,
resolution validation, templates, or database projection code.

### 4. Define and enforce external API schemas

Only batch mapping currently has explicit Pydantic response models. Add models
for assets, markets, logs, metadata, stats, health, and refresh responses.

- Keep `/api/v1` backward compatible.
- Specify nullability, numeric/string choices, pagination semantics, and all
  enums.
- Resolve the inconsistent `count` meaning: assets use pre-pagination total,
  markets use returned-row count. Add an explicit `total` and `count`, or
  version the correction.
- Decide whether `settle_symbol` is nullable for spot mapping targets; model and
  document one behavior.
- Add contract tests against `docs/API.md` examples and an OpenAPI snapshot.
- Add deterministic error bodies and stable error codes for query validation.
- Document compatibility/deprecation policy before adding `/api/v2`.

### 5. Separate read and mutation authorization

Any authenticated user can currently call `POST /api/v1/refresh`. Add explicit
roles or scopes:

- Read: health, assets, markets, mappings, metadata, stats, logs.
- Operator: refresh and future review/mutation endpoints.
- Browser session permissions must match the authenticated principal.
- Denied authenticated requests return 403; missing/invalid credentials remain
  401.
- Keep secrets out of SQLite, source control, logs, and API responses.

### 6. Add identity and migration regression coverage

Add tests that prove:

- Same ticker with conflicting independent identifiers stays separate.
- Rename preserves asset identity and mapping history.
- Split and merge decisions are explicit and reversible through new revisions.
- A suffix-only candidate never merges.
- Alias corroboration works with any declared reference venue and fails when
  reference evidence is absent, stale, ambiguous, or differently classified.
- Unit-prefixed symbols do not merge without an unprefixed counterpart.
- Empty, partial, malformed, and failed snapshots cannot mark markets missing.
- Migrations work from empty DB and schema 12 with representative history.
- All supported connectors parse recorded fixtures before optional live tests.

## Constraints

- Do not import sibling-project code, configuration, databases, or runtime
  state.
- Do not require exchange credentials for initial discovery.
- Do not overwrite or delete an existing SQLite database.
- Use monotonic, transactional migrations.
- Keep mapping resolution projection-free: one indexed read transaction, no
  `list_assets()`, no `raw_json`, synchronous/thread-pool route.
- Preserve authenticated 1/10/100-symbol and concurrent mapping benchmarks.
- Update README only for human-facing behavior; update `docs/API.md` for
  external contracts and `AGENTS.md` for coding-agent rules.

## Completion evidence

Provide:

- Migration plan, rollback limitations, and before/after schema summary.
- Tests from empty DB and schema 12 fixture.
- Full pytest result and mapping benchmark results.
- Recorded connector fixture results; list optional live endpoint failures.
- Files changed and API compatibility notes.
- Confirmation that no origin, push, or deployment occurred unless separately
  authorized.
- Remaining identity ambiguity requiring human review.
