# Asset Master Data

Local-first, auditable canonical asset and exchange-market metadata from public venue catalogs.

## Status and scope

The current release is `0.11.0`. The service discovers public spot, derivatives, margin, and loan catalogs; preserves raw observations and lifecycle history; builds evidence-backed canonical mappings; and serves authenticated HTML and JSON views. It is not a price feed, trading engine, or order router.

## Architecture

```text
public venue catalogs -> transactional SQLite history -> versioned identity mappings
                                                -> authenticated UI and HTTP API
```

- Venue failures are isolated and cannot invalidate a previous complete snapshot.
- Raw symbols and payload evidence remain auditable.
- Consumers integrate only through the documented API or exports.
- SQLite runs locally with WAL, migrations, and online backups.

## Quick start

Requirements: Python 3.11+; Python 3.13 is the development default.

```bash
make install
.venv/bin/python -m mdv.cli entitlement admin --password-file /secure/password-file
make collect
make run
```

Open <http://127.0.0.1:8090/mdv> and sign in. Runtime data is stored in `.data/`; generated entitlements and production configuration remain ignored by Git.

## Development

```bash
make test
make check           # tests, wheel smoke, diff validation
make package
```

Dependency declarations live in `pyproject.toml`. `requirements.lock` is the production lock and `requirements-dev.lock` is the development lock.

## Operations

```bash
make backup restore-check
make prod-status
make deploy-prod
```

Every production deployment is an immutable SemVer release tagged on `main`. Deployment validates the tag/version/revision contract, creates and verifies a live SQLite backup, installs locked dependencies, runs migrations, and restarts the hardened systemd service. Backups are mode `0600` but unencrypted; encrypt them before off-host transfer.

## Documentation

- [HTTP API contract](docs/API.md)
- [Detailed behavior and operations](OPERATIONS_REFERENCE.md)
- [Known technical debt](docs/TECHNICAL_DEBT.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

Licensed under the [Apache License 2.0](LICENSE).
