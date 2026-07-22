# Asset Master Data

Local-first, auditable canonical asset and exchange-market metadata from public venue catalogs.

## Status and scope

The current release is `0.14.3`. The service discovers public spot, derivatives, margin, and loan catalogs; preserves raw observations and lifecycle history; builds evidence-backed canonical mappings; and serves authenticated HTML and JSON views. It is not a price feed, trading engine, or order router.

## Architecture

```text
public venue catalogs -> transactional SQLite history -> versioned identity mappings
                                                -> authenticated UI and HTTP API
```

- Venue failures are isolated and cannot invalidate a previous complete snapshot.
  Invalid per-symbol records are quarantined with raw evidence while valid sibling
  symbols are applied; partial snapshots never infer removals from unseen symbols.
- One cross-process writer lease and snapshot-time ordering prevent overlapping or
  older collections from regressing the current catalog.
- Raw symbols, lifecycle events, and mapping revisions remain auditable.
  Unchanged rows expire, old change payloads compact to hashes/state, and
  retained change rows have a hard per-table ceiling exposed in health data.
- Provider-classified TradFi, RWA, stock, forex, index, commodity, and similar
  session-based markets remain active while their venue or underlying market is
  closed. Market details expose normalized trading-session metadata and the next
  provider-published transition when available. Routine `TRADING`/`PAUSED`
  session flips are not lifecycle changes; a terminal status or disappearance
  from a complete snapshot still is.
- Consumers integrate only through the documented API or exports.
- Derivative projections publish auditable contract-multiplier and native
  open-interest units. Conflicting or incomplete venue specifications remain
  null with an explicit reason; quantity/tick increments are never substituted
  for contract value.
- SQLite runs locally with WAL, migrations, and online backups.

## Quick start

Requirements: Python 3.11+; Python 3.13 is the development default.

```bash
make install
.venv/bin/python -m mdv.cli entitlement admin \
  --password-file /secure/password-file --role operator
make collect
make run
```

Open <http://127.0.0.1:8090/mdv> and sign in. Runtime data is stored in `.data/`.
Non-secret `config/config.yaml` is version-controlled and snapshotted per
release; generated entitlements and runtime data remain ignored by Git.

Serving the UI/API never contacts venues or starts collection. Run collection
through `mdv collect`, `make collect`, or the systemd timer. Operators can edit
manual mapping actions; reader accounts are read-only.

For an installed wheel outside a checkout, `mdv init-config` creates an
XDG-compatible configuration under `~/.config/asset-master-data/` and selects
`~/.local/share/asset-master-data/` for data. Use `mdv doctor --require-ready`
to validate configuration, private entitlement permissions, database state,
and collection freshness. Config, entitlement, and bundle writes use private,
randomly named temporary files followed by an fsync and atomic promotion;
symlink destinations are rejected.

## Development

```bash
make test
make check           # tests, wheel smoke, diff validation
make package
```

Dependency declarations live in `pyproject.toml`. `requirements.lock` is the
production lock and `requirements-dev.lock` is the development lock. CI covers
every advertised Python version. A lock refreshed with Python 3.13 must still
contain transitive packages required by Python 3.11; declare such portability
dependencies directly when environment-specific resolution would omit them.

## Operations

```bash
make backup restore-check
sudo systemctl disable --now asset-master-refresh.timer
sudo systemctl stop asset-master-data.service asset-master-refresh.service
make restore
make prod-status
make deploy-prod
```

`make backup` requires the configured database and configuration, uses SQLite's
online backup API, rejects symlinked inputs, and self-verifies a mode-`0600`
archive before atomic promotion. `make restore` verifies all
hashes and SQLite integrity before atomically replacing the configured files;
the API, collector, and collection timer must all be stopped first. Archive
member count, expanded size (1 GiB), and available extraction/staging space are
bounded. Entitlements are intentionally excluded because the archive is
unencrypted—back them up separately with encryption.

Every production deployment is an immutable SemVer release tagged on `main`.
Deployment validates the tag/version/revision contract, creates and verifies a
required live backup before migration, atomically switches a versioned runtime,
and automatically restores the prior runtime on failed readiness. Each release
contains its wheel environment, non-secret configuration snapshot, installer,
and systemd templates, so rollback does not depend on the newly pulled checkout.
The predeploy archive backs up the active database, retains the exact active
configuration as non-restorable evidence, and records its release/revision in
the manifest. Collection is quiesced before migration and remains off through
readiness; the timer's prior enabled and active state is restored after either
a successful cutover or rollback, preserving an operator pause.
The first immutable deploy builds the pre-pull revision as its real rollback
target. After success
it retains at most current + rollback + one extra release and the newest
predeploy archive for each of two distinct source revisions. Systemd bounds
memory, tasks, collection duration, retry
concurrency, and restart behavior for limited hosts.

## Documentation

- [HTTP API contract](docs/API.md)
- [Detailed behavior and operations](OPERATIONS_REFERENCE.md)
- [Versioning and release contract](docs/VERSIONING.md)
- [Known technical debt](docs/TECHNICAL_DEBT.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

Licensed under the [Apache License 2.0](LICENSE).
