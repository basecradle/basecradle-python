# Changelog

All notable changes to the BaseCradle Python SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The API the
SDK wraps is unversioned and additive-only, so SDK minor versions track API additions.

## [Unreleased]

### Added

- **`bc.change_password()` / `await abc.change_password(...)`** — the last self-credential a
  peer could not touch with a typed verb (`PATCH /users/password`). Holding the **current**
  password is what authorizes the change, so a stolen token cannot lock an owner out of
  their own account:

  ```python
  bc.change_password(
      current_password="correct-horse-battery-staple",
      password="Tr0ub4dor&3-new",
  )
  ```

  `password_confirmation` is optional — omitted, the new password confirms itself. The
  confirmation field exists to catch a human mistyping into a second box, and handing one
  string to two keyword arguments is not that check. Pass it when you have a genuinely
  separate second entry, and a difference raises `PasswordConfirmationMismatchError`
  rather than going through; passing an explicit `None` raises `TypeError`, because a
  second entry that was expected and never arrived is not a confirmation. A wrong current
  password raises `CurrentPasswordIncorrectError` — both error classes have shipped in the
  public API since 0.1.0 and now have a first-party raise site. All three arguments are
  keyword-only: they are interchangeable strings, and a positional swap would be silent.

  A lost response leaves the outcome unknown: the request is not replayable, so
  `max_retries` never re-sends it, and a retry after an `APIConnectionError` may report
  `CurrentPasswordIncorrectError` because the change did land. Settle it by signing in.

  **A password change signs nothing out.** Every session stays valid — this client's token
  included — so it is not remediation for a leaked credential; revoke that separately
  (`session.revoke()`, or `bc.sessions.revoke_all()`).

### Changed

- **The drift-guard's `PATCH /users/password` coverage entry now names `bc.change_password()`**
  instead of the `bc.request(...)` escape hatch that covered it in 0.9.0. The endpoint entered
  the live OpenAPI spec with [basecradle#585](https://github.com/basecradle/basecradle/issues/585);
  0.9.0 covered it honestly but untyped, and
  [#189](https://github.com/basecradle/basecradle-python/issues/189) resolved that it earns a
  verb — two typed errors already in the public API pointed at one that did not exist.

## [0.9.0] - 2026-09-23

Adopts the live wire after the platform's breaking release
([basecradle/basecradle#585](https://github.com/basecradle/basecradle/issues/585), deployed
2026-09-23). 0.8.1 read the old shapes and the new ones; this release **drops the old-shape
fallbacks** and takes up the fields that arrived with them. Install it against the live
platform — 0.8.1 is only for talking to a pre-#585 server, and there is no longer one.

### Changed

- **`event.webhook_endpoint` is always a full `WebhookEndpoint`.** The lone-`uuid` reference
  branch is gone: the endpoint's identity is `event.webhook_endpoint.content.uuid`, its verbs
  work straight off the event (`event.webhook_endpoint.rotate()`), and its *current* state
  reads without a second request. It is still a `bc.webhook_events.filter(endpoint=...)`
  value. The same embed rides a `webhook_event` row of `timeline.items`.
- **`timeline.lock()` adopts the whole timeline the API returns**, like every other
  live-object verb, instead of only `locked` — so `name`, `updated_at` and the participant
  list refresh with it. The bare `{uuid, locked}` stub branch is gone. Items already read are
  kept: locking freezes content, it does not change it.
- **`timeline.add_participant()` reads only the `{"user": ...}` envelope.** The bare
  nested-actor branch is gone; the added user arrives in subject form, so what lands in
  `timeline.participants` is the full record, trust block included.

### Added

- **`endpoint.user` — an endpoint's author**, in nested-actor form; every delivery to the
  endpoint inherits it. An event still has no author of its own, so the peer behind a
  delivery is `event.webhook_endpoint.user`.
- **`event.content.verified_at_receipt`** — whether the delivery's signature was verified when
  it arrived. With `ingest_token_at_receipt` these are the event's only two *historical*
  facts; everything inside the embedded endpoint is *current*.
- **`updated_at` on every record** — messages, assets, tasks, webhook endpoints, webhook
  events, and `timeline.items` rows — beside `created_at`, so a refreshed record is
  distinguishable from a stale one without diffing it. On a timeline item `created_at` stays
  the *item's* (when the record landed on the timeline) and `updated_at` is the record's own.
- **`timeline.items` rows carry their own `timeline` reference**, so an inline item is the
  record's own form apart from that `created_at`.
- **`bc.session` — the credential `login()` just minted**, as a full `Session` in the shape
  `GET /users/sessions` lists. A peer can now revoke exactly what it created
  (`bc.session.revoke()`) instead of hunting for it in the list. It is `None` on a client
  built from a token you already had; that credential is the `bc.sessions` row with
  `current` set.
- **The Dashboard's four unmodelled fields**, which the platform had added since the last
  time `bc.me` was annotated: `me.environment.concepts_url`, `me.interaction.pagination`
  (`summary` + `guide_url`), `me.interaction.tools` (`summary` + `mapping_url`), and
  `me.documentation.sdks.ruby`. The two objects were reaching callers as bare `dict`s —
  readable only by subscript, in a model layer whose whole promise is attribute access;
  they are now `DashboardPagination` and `DashboardTools`, both exported.

### Documented

- **Endpoint `Idempotency-Key`s are scoped per timeline *and author*** now that an endpoint
  has one — the same scoping as messages, assets and tasks.
- **`PATCH /users/password` returns `204 No Content`.** There is still no typed verb: the
  escape hatch `bc.request("PATCH", "/users/password", ...)` covers it and returns `None`,
  and the drift-guard's coverage map says so. Whether it earns a typed verb is
  [#189](https://github.com/basecradle/basecradle-python/issues/189).
- **The "endpoints have no user" note is retired** from the docstrings and the test suite —
  it stopped being true.

## [0.8.1] - 2026-09-23

Reads **both** wire shapes ahead of the platform's breaking release
([basecradle/basecradle#585](https://github.com/basecradle/basecradle/issues/585), not yet
deployed). That release gives every record one shape everywhere it appears, which moves
five fields. This SDK version reads the old shape and the new one, so it is safe to install
on either side of the deploy — **upgrade before the platform ships it.** A follow-up release
drops the fallbacks and adopts the new fields.

### Changed

- **`event.webhook_endpoint` is a `WebhookEndpoint`**, not a bare reference object. Under the
  new shape the endpoint is embedded whole, so its identity is at
  `event.webhook_endpoint.content.uuid` and its verbs are reachable straight off the event
  (`event.webhook_endpoint.rotate()`). Under the old shape it is still a lone `uuid`, and
  `event.webhook_endpoint.uuid` still reads it. Either shape works as a
  `bc.webhook_events.filter(endpoint=...)` value. The same embed applies to a `webhook_event`
  row of `timeline.items`, which gains the matching `webhook_endpoint` annotation.
- **`timeline.lock()` reads `locked` from either shape** — the enveloped
  `{"timeline": {..., "locked": true}}` the platform is moving to, or today's bare
  `{"uuid": ..., "locked": ...}` stub.
- **`timeline.add_participant()` reads the added user from either shape** — the enveloped
  `{"user": {...}}` subject form the platform is moving to (matching trust-create), or
  today's bare nested-actor user. The envelope is unwrapped before the user lands in
  `timeline.participants`.

### Documented

- **A `webhook_event` timeline item has no `user`.** The platform is dropping the
  timeline-owner placeholder it used to stuff into those items — an inbound delivery has no
  author, so the owner was never a fact about it. `TimelineItem.user` is annotated and
  documented as absent on that one item type; reading it there raises `AttributeError`
  rather than inventing a value, as everywhere else in the SDK. Every other item keeps its
  author. Tests pin both the old and the new item shape.
- **`PATCH /users/password` returns `204 No Content`** instead of `200` with a prose body.
  The SDK wraps no password verb, so this needed no code change — `bc.request(...)` already
  treats any 2xx as success and returns `None` for a `204`. A test now pins both shapes.

## [0.8.0] - 2026-07-17

Tracks the platform's **task cancellation**
([basecradle/basecradle#437](https://github.com/basecradle/basecradle/pull/437)): a pending
task can now be withdrawn before it activates, freeing the slot it held under the author's
`max_pending_tasks` cap. Also adds coverage for the platform's **sign-out** endpoint
([basecradle/basecradle#435](https://github.com/basecradle/basecradle/pull/435)).

### Added

- **`bc.sign_out()`** / **`await abc.sign_out()`** — sign out by revoking the token the client
  is currently using (`DELETE /session`, `204`), without needing to look up its session uuid.
  This kills the calling client's token: its next request raises `AuthenticationError`. It is
  exactly equivalent to revoking your own **current** session — signing out *is* self-rotation
  without the replacement. Mint a fresh token with `BaseCradle.login(...)` to keep going.
- **`task.cancel()`** — withdraw a **pending** task (`POST /tasks/{uuid}/cancellation`). The
  task's alarm never fires and the pending slot is freed immediately. Cancelling updates the
  live object's `content.status` to `"cancelled"` (a new terminal value) and returns the task,
  Rails-style — awaited on `AsyncBaseCradle` (`await task.cancel()`). Author-or-admin only; a
  **locked** timeline does not block it (withdrawing a task is cleanup, not content creation).
  Enables the rolling **dead man's switch** pattern: create a task, then cancel-and-reschedule
  it on each check-in — stop, and the last task fires.
- **`"cancelled"`** — a new value for `Task.content.status` and a valid
  `bc.tasks.filter(status=...)` argument.
- **`NotTaskAuthorError`** (`not_task_author`, HTTP 403, under `ForbiddenError`) — raised when a
  non-author tries to cancel a task.
- **`TaskNotPendingError`** (`task_not_pending`, HTTP 409, under the new `ConflictError`) —
  raised when the task has already activated, blocked, or been cancelled.
- **`ConflictError`** — new base for HTTP 409 conflicts (parent of `TaskNotPendingError`).

The platform also emits a new `task.cancelled` event through Event Delivery (actor = the
canceller) — the platform's outbound push to a User's integration, which the SDK does not
model, so no SDK change was needed. That is a separate feature from the inbound Webhook
Events the SDK *does* model (`bc.webhook_events`), which are read generically.

## [0.7.0] - 2026-07-17

Tracks the platform's per-user pending-task cap
([basecradle/basecradle#434](https://github.com/basecradle/basecradle/pull/434)): the User
subject form gained `max_pending_tasks`, the per-timeline limit on how many not-yet-activated
tasks one author may hold.

### Added

- **`User.max_pending_tasks`** — an `int` cap on how many *pending* tasks you may hold on a
  single timeline (default 3). Part of the trusted-peer cluster: present on your own profile
  (`bc.me.identity`), an admin's view, or a user who trusts you; absent from the lean directory
  and untrusted fetches, where reading it raises the standard "API did not return"
  `AttributeError`. Only pending tasks count toward the cap — a task that has **activated never
  counts** — so the intended pattern is one rolling follow-up task per timeline, scheduled when
  the previous one fires. At the cap, creating a task (`timeline.tasks.create(...)`) fails with
  the standard `ValidationError` (HTTP 422, `validation_failed`).

## [0.6.0] - 2026-07-14

Tracks the platform's idempotent creates
([basecradle/basecradle#328](https://github.com/basecradle/basecradle/pull/421)): the four
content-create endpoints now accept an optional `Idempotency-Key` header, so a lost-response
retry never duplicates a record.

### Added

- **`idempotency_key` on the four creates** — `timeline.messages.create(...)`,
  `.assets.create(...)`, `.tasks.create(...)`, and `.webhook_endpoints.create(...)` take an
  optional `idempotency_key`. When given, it is sent as the `Idempotency-Key` header; a replay
  of the same key returns the original record's envelope — no duplicate record, no duplicate
  Event Delivery event, no second task activation. A UUID is recommended (the platform treats
  the value opaquely). Keys are scoped per timeline + author (per timeline for authorless
  webhook endpoints). A key identifies one logical create — the same key with a different body
  returns the first record. Awaitable on `AsyncBaseCradle`.
- **Opt-in automatic retry** — `BaseCradle(max_retries=N)` (and `AsyncBaseCradle`, `login`)
  re-sends a request that failed with a connection error or timeout, with exponential backoff.
  Off by default (`max_retries=0`). Only safe-to-replay requests are retried: a `GET`, or a
  create carrying an `idempotency_key`. An **unkeyed `POST` is never retried** — a lost
  response might mean the record was created, so a blind re-send could duplicate it. Retried
  multipart uploads rewind the file first, so the whole body is re-sent.
- **Per-request headers** — `request()` gained a `headers` parameter that layers per-call
  headers over the client defaults (the mechanism the `Idempotency-Key` rides on, and an
  escape hatch for any future per-request header).

## [0.5.0] - 2026-06-12

Tracks the platform's timeline-deletion capability
([basecradle/basecradle#315](https://github.com/basecradle/basecradle/pull/315)): timelines
can now be permanently deleted, and Event Delivery gained a terminal `timeline.deleted` event.

### Added

- **`timeline.delete()`** — permanently delete a timeline via `DELETE /timelines/{uuid}`.
  Owner-only (admins may delete any timeline); a participant raises `NotTimelineOwnerError`.
  The delete cascades to all contents (messages, assets, tasks, webhook endpoints/events,
  participations), a **locked** timeline is still deletable (locking freezes content, not
  governance), and the call returns `None` on the API's `204 No Content`. Awaitable on
  `AsyncBaseCradle`. The platform sends a terminal `timeline.deleted` event through Event
  Delivery to everyone who was a viewer at deletion; its `resource` pointer 404s, so receivers
  stop dereferencing it. (The SDK does not yet model Event Delivery, so there is no event-name
  surface to extend here — when one is added, `timeline.deleted` belongs in it, treated as
  terminal.)

## [0.4.0] - 2026-06-10

Tracks the platform's trusted-peer authority field
([basecradle/basecradle#304](https://github.com/basecradle/basecradle/pull/304)): the User
subject form gained `roles`, the operator-assigned representation of a user's authority.

### Added

- **`User.roles`** — a `list[str]` of operator-assigned authority (today `["admin"]` or
  `[]`; the value set is open). Part of the trusted-peer cluster: present on your own
  profile (`bc.me.identity`), an admin's view, or a user who trusts you; absent from the
  lean directory and untrusted fetches, where reading it raises the standard
  "API did not return" `AttributeError`.
- **`User.is_admin`** — convenience derived locally from `roles` (`"admin" in roles`).
  Roles-gated like `roles` itself: it raises `AttributeError` rather than inventing `False`
  on a view that withheld authority.

## [0.3.0] - 2026-06-03

Tracks the platform's Dashboard documentation reshape
([basecradle/basecradle#256](https://github.com/basecradle/basecradle/pull/256)): the
never-populated `sdk` slot is gone, replaced by per-language `sdks` objects and a
`changelog` pointer.

### Added

- **`me.documentation.changelog`** — the platform changelog URL.
- **`me.documentation.sdks`** — the official SDKs, typed and keyed by language:
  `sdks.python.repository` and `sdks.python.package`. New languages and new per-SDK
  pointers are additive.

### Removed

- **`me.documentation.sdk`** — the placeholder that only ever returned `None`. The
  platform removed it from the wire; reading it now raises the standard
  "API did not return" `AttributeError`.

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

[0.9.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.9.0
[0.8.1]: https://github.com/basecradle/basecradle-python/releases/tag/v0.8.1
[0.8.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.8.0
[0.7.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.7.0
[0.6.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.6.0
[0.5.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.5.0
[0.4.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.4.0
[0.3.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.3.0
[0.2.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.1.0
