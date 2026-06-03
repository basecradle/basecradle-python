# Changelog

All notable changes to the BaseCradle Python SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The API the
SDK wraps is unversioned and additive-only, so SDK minor versions track API additions.

## [0.2.0] - 2026-06-03

The async release: the same SDK for async code, on one shared core.

### Added

- **`AsyncBaseCradle`** — `httpx.AsyncClient` transport, `async for` pagination, awaited
  verbs. Same models, same typed errors, same resources as the sync client.
- **Await-aware model verbs** — `Timeline`, `User`, `Session`, and `WebhookEndpoint` are the
  same classes in both worlds: verbs execute immediately on sync-attached objects and return
  coroutines (await them) on async-attached ones.
- **Parity as an invariant** — the test suite fails if a future resource ships in only one
  of the two clients.

### Changed

- The sync client is refactored onto the shared core. No behavior changes — every v0.1.0
  test passes unchanged.

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

[0.2.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.1.0
