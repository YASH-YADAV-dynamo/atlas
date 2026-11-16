# ADR-0004: Run HydraDB from the official image, pinned by commit

Status: accepted.

## Context
The requirement is that the graph database be the open-source engine `github.com/hydra-db/hydradb`,
self-hosted, not the hosted platform. The first plan was to build the engine from source with the
repository's Dockerfile. That build kept failing on the build machine (the Rust toolchain download inside
BuildKit), and build time was being spent on packaging rather than on the product.

## Decision
Run the engine from the project's own published image, pinned to an immutable tag:
`ghcr.io/hydra-db/hydradb:sha-9166374`. It is the open-source engine at the commit we had pinned for
the source build, and we run it ourselves in Docker Compose with a persistent volume and a token from the
environment. Nothing in Atlas talks to a hosted HydraDB service.

## Consequences
- The requirement is met: same open-source engine, self-hosted; only the way the binary is obtained changed.
- The tag is a commit hash, never `latest`. Upgrading the engine is a deliberate change of that one tag,
  followed by `make test-int` (contract and integration suites run against the engine).
- No Rust toolchain is needed to develop or deploy Atlas. To rebuild from source instead, build the
  hydradb repository's Dockerfile with `--target runtime` at the same commit; this is not part of CI.
- The HydraDB image is pulled from the upstream registry directly.
