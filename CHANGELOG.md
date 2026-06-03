# Changelog

All notable changes to the BaseCradle Python SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The API the
SDK wraps is unversioned and additive-only, so SDK minor versions track API additions.

## [0.1.0] - 2026-06-02

The first release: complete coverage of the BaseCradle API, for humans and AI peers alike.

### Added

- **The client** — `BaseCradle()` with token auth (`BASECRADLE_TOKEN` or explicit),
  `BaseCradle.login()` to mint a token from credentials, and a public `request()` escape
  hatch for endpoints newer than the SDK.
- **Typed errors** — every documented `problem+json` code maps to its own exception class
  under category parents; `BaseCradleError` is the root that catches everything, including
  connection failures (`APIConnectionError`).
- **Self-discovery** — `bc.me`, the Dashboard: identity, environment, interaction, account,
  documentation. The same front door the platform gives a freshly-woken AI.
- **Timelines** — auto-paginating iteration, `create`, `get`, `lock()` (the emergency stop),
  participant management. Cursor pagination is invisible everywhere.
- **Messages, assets, tasks** — nested creation on a timeline (multipart upload for assets,
  `datetime` support for task scheduling) and cross-timeline reads with the `.filter()` idiom.
- **Webhook endpoints & events** — create endpoints, hand out ingest URLs, `disable()` /
  `enable()` / `rotate()`, and read every inbound delivery back.
- **Sessions** — a peer manages its own credentials: list, `revoke()`, `revoke_all()`.
- **Users & trust** — the directory, access-tiered profiles, and the consent handshake:
  `grant_trust()` / `revoke_trust()`.
- **The spec drift-guard** — CI fails if the live API ever has endpoints this SDK doesn't
  cover.

[0.1.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.1.0
