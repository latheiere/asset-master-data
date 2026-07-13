# Versioning and Release Contract

Each top-level service package selects its own release number. Release numbers
are not synchronized between packages or services, but every top-level service
package follows this contract.

## Version format

- Use final [Semantic Versioning](https://semver.org/) in strict `X.Y.Z` form.
- Store `X.Y.Z` without a `v` prefix in `project.version` in
  `pyproject.toml`.
- Use `vX.Y.Z` only for the corresponding Git release tag.
- Do not use a shared suite version or copy another package's number.
- Increment the major, minor, or patch component according to that repository's
  own compatibility guarantees.

## Single source of truth

`project.version` is the only editable application-version literal. Runtime
modules obtain it through `importlib.metadata.version(<distribution-name>)`.
CLI output, OpenAPI metadata, health output, bundles, and other consumers must
derive from that runtime value rather than defining another version.

A source-tree fallback such as `0+unknown` is allowed only when distribution
metadata is unavailable. Installed editable and wheel environments must always
report the strict `X.Y.Z` release.

The deployed Git revision is a separate 40-character commit SHA. Never append
it to, or substitute it for, the package version.

## Required verification

Tests and package smoke checks must prove:

1. `project.version` is strict `X.Y.Z`.
2. Editable-install metadata equals the runtime `__version__`.
3. A built wheel installs independently and reports the same `X.Y.Z`.
4. Health, CLI, OpenAPI, or bundle version fields derive from package metadata
   where those surfaces exist.
5. Version and Git revision remain separate fields.

## Release procedure

Ordinary development commits do not require a version change. At a release
boundary:

1. Choose the next version for that repository.
2. Update only `project.version` and the README release disclosure.
3. Run `make check` and inspect the built wheel.
4. Commit the complete release state.
5. Create an annotated, immutable `vX.Y.Z` tag on that commit.
6. Push the commit and tag, then verify CI.
7. Deploy only repositories with an existing remote-installation contract.
8. Verify installed metadata, runtime health/version, and deployed Git SHA.

Never move or reuse a release tag. A local-only package follows the same
metadata, test, wheel, and tag rules but does not gain a remote deployment
surface merely because it has a release.
