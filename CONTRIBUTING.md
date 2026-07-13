# Contributing

Keep changes focused, auditable, and safe for a resource-constrained service.
Business and identity rules are repository-specific and should not be
generalized as part of infrastructure work.

## Development

```bash
make install
make check
```

Preserve documented HTTP contracts, migration compatibility, lifecycle
history, and runtime-data boundaries. Connector changes require recorded
success and malformed or partial fixtures.

## Versions and releases

Follow the [versioning and release contract](docs/VERSIONING.md). This service
chooses its own Semantic Version; it does not track sibling-service versions.
Never duplicate `project.version` in runtime code or combine it with the
deployed Git revision.

Production releases require a clean `main`, an annotated `vX.Y.Z` tag matching
`project.version` and `HEAD`, green CI, a verified backup, and post-deploy
service, authentication, and collection checks.

## Security

Never commit credentials, entitlement passwords, production configuration,
runtime databases, backups, or logs. Follow [SECURITY.md](SECURITY.md) when
reporting a vulnerability.
