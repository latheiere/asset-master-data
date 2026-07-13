# Security policy

## Supported versions

Security fixes are applied to the latest tagged release. Older immutable
runtime directories exist only for short rollback and are not a promise of
continued support.

## Reporting a vulnerability

Do not open a public issue containing credentials, entitlement files, database
content, exploit details, or host information. Contact the repository owner
through a private channel with the affected version/revision, impact, minimal
reproduction, and any proposed mitigation. Do not test against production or
venue endpoints without explicit authorization. There is no guaranteed response
SLA, so state any active exploitation or disclosure deadline clearly.

## Trust boundary

- The default listener is localhost. The application does not terminate TLS;
  use a trusted reverse proxy and set `auth.session_cookie_secure: true` before
  remote browser access.
- Every application route except the login page and empty favicon requires a
  valid Basic credential or signed browser session. `reader` accounts are
  read-only; manual-action mutation requires `operator`.
- Collection is not exposed over HTTP. It runs through an operator CLI, bundle
  import, or the systemd timer under a cross-process single-writer lease.
- Initial discovery uses public venue endpoints only. Never add exchange API
  keys, account cookies, balances, positions, or trading credentials here.
- A mapped ticker is evidence rather than verified legal/economic identity.
  Consumers must not treat a mapping, tag, or equity classification as proof of
  issuer, backing, redemption, or suitability.

## Secrets and authentication

Entitlements contain scrypt password hashes and the browser session-signing
secret. Keep the file outside source control with mode `0600`; use ignored
password input files when running `mdv entitlement`. A changed session secret
invalidates existing browser sessions. Fixed scrypt parameters, bounded worker
slots, and per-client failure throttling reduce resource exhaustion but do not
replace a network firewall or upstream rate limit.

Default runtime and predeploy archives are unencrypted and intentionally exclude
entitlements. Back up entitlements separately with authenticated encryption and
restrict off-host archive access. Never attach databases, backups, logs, or
entitlements to public reports. Restore accepts only regular files/directories,
enumerates at most 10,000 members incrementally, limits expansion to 1 GiB,
requires free-space reserve, and verifies the exact extracted tree it promotes.

## Production hardening

The provided systemd units restrict filesystem writes, privileges, tasks,
memory, collection duration, restart bursts, and timer concurrency. Production
deployment requires a clean, tagged `main`, locked dependencies, a verified
online SQLite backup before migration, an immutable release bundle, and a
readiness-checked cutover with rollback. Keep the host patched, monitor disk and
freshness, and run restore drills with the API, collector, and collection timer
stopped. Deployment preserves a deliberately disabled or inactive timer state.

Dependency vulnerabilities should be evaluated against both `requirements.lock`
and `requirements-dev.lock`. Regenerate locks through the project workflow,
review transitive changes, and run `make check`; do not bypass hashes in
production installation.
