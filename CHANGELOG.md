# Changelog

All notable changes to the BaseCradle Python SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The API the
SDK wraps is unversioned and additive-only, so SDK minor versions track API additions.

## [Unreleased]

### Added

- **Contact messages and notes: the platform's first admin-only surface** (#262, after
  [basecradle#663](https://github.com/basecradle/basecradle/pull/663)). Six operations, on
  both clients:

  ```python
  for message in bc.contact_messages.filter(status="received"):  # GET /contact_messages
      message.add_note(body="Looks genuine. Replied by email.")  # POST …/notes → Note
      message.set_status("closed")                               # PATCH …/status
  bc.contact_messages.get(uuid)                                  # GET /contact_messages/{uuid}
  for note in bc.notes: ...                                      # GET /notes
  bc.notes.get(uuid)                                             # GET /notes/{uuid}
  ```

  `ContactMessage` and `Note` are flat top-level records, read wire-exact. A message's
  `user` is a `User`, or `None` for a visitor without an account. Its `notes` are `Note`
  objects embedded in full, and `data` is the wire's own `dict` of vendor slots, left
  unmodelled on purpose. `set_status()` adopts the whole record the API returns.
  `add_note()` returns the new `Note` and appends it to `notes`. A note never changes once
  written.
- **`NotAnAdminError`** (`not_an_admin`, HTTP 403, under `ForbiddenError`). Every contact
  message and note operation raises it for anyone who is not an admin.
- **`bc.me.admin`**, the Dashboard's sixth section, typed as `DashboardAdmin`
  (`contact_messages_url`, `notes_url`, `guide_url`). It is present only for an admin. For
  anyone else, reading `bc.me.admin` raises `AttributeError`, like any field the API did
  not return.
- **`RequestHeaders`** is the class behind every stored request's headers: lookup folds
  case, and `repr()` prints names, never values. It is generic over the value type. A
  contact message's `headers` is `RequestHeaders[str | None]`, because the platform can
  record a header with a `null` value. `WebhookEventHeaders` is now
  `RequestHeaders[str]`, with the same lookup, repr and `copy()` as before. The one
  visible difference is the wording of a missing header's `KeyError`, which now says
  "on this request" where it said "on this delivery".
- **Model fields annotated `Model | None` now wrap**, so a nested record that may be `null`
  comes back as its model when present and `None` when not. `ContactMessage.user` is the
  first such field.

### Fixed

- **`BinaryPayloadError`** (`binary_payload`, HTTP 415, #264). The platform documents this
  webhook-ingest code beside `payload_too_large`, but it had no typed class, so it fell
  back to a bare `BaseCradleError`. As of this release, all 20 documented codes map to
  their own classes. A code the platform documents later still reads as a bare
  `BaseCradleError` until the SDK maps it.

### Security

- **`repr()` of a webhook delivery's headers no longer prints their values** (#246).
  `WebhookEventHeaders` is a `dict` subclass, so it inherited `dict.__repr__` and rendered
  every header in full — while every other model in the SDK prints field *names* and never
  values. It now does the same:

  ```python
  repr(event.content.headers)
  # before: {'Host': 'basecradle.com', 'Authorization': 'Bearer …', 'X-Api-Key': '…'}
  # now:    <WebhookEventHeaders ['Authorization', 'Content-Type', 'Host', 'X-Api-Key']>
  ```

  **Whose secret was this?** Not yours — these are an inbound delivery's own request
  headers, so this was never your `bc_uat_` token (that was #242, fixed in 0.13.0). It is
  the *sender's*: anyone authenticating their POST to your ingest URL puts their
  credential in those headers, and the platform stores and returns them verbatim. One
  `log.debug("%r", event.content.headers)` while debugging a delivery put it wherever your
  logs go.

  **Do you need to do anything?** Only if you rendered a delivery's headers — `repr()`,
  `str()`, an f-string, `pprint`, or `%r` logging — into somewhere you do not fully
  control, *and* your senders authenticate to your ingest URL. If so, those senders'
  credentials were exposed and they are the ones who have to rotate; nothing of yours was.
  Reading a header you asked for by name was never a leak and still is not.

  **Reads are untouched and stay wire-exact.** `headers["X-GitHub-Delivery"]`, `get()`,
  `in`, iteration, `keys()`, `==` and `copy()` all behave exactly as before, case-folding
  included.

  **What this closes** is the accidental path through the object's own rendering:
  `repr()`, `str()`, an f-string, `pprint`, and `%r`/`%s` logging.

  **What it does not.** Everything that *asks* for the pairs still returns them in full,
  deliberately — it is still a real `dict`, and a caller reaching for one of these is
  asking for the headers:

  ```python
  headers["X-Api-Key"], headers.get(...), headers.items(), headers.values()
  json.dumps(headers), dict(headers), {**headers}, headers | other, pickle.dumps(headers)
  ```

  One accidental path also stays open and is worth knowing about: **a failing `pytest`
  assertion comparing headers prints the differing pairs in full**, because pytest's dict
  comparison reads the mapping directly and never consults `__repr__`. If you assert on a
  delivery's headers in CI, compare the names (`sorted(headers)`) or a single value, not
  the whole mapping.

  No denylist of "sensitive" header names is involved: every name is shown, no value is.

## [0.13.0] - 2026-09-30

### Security

- **Nothing generic emits the bearer token any more.** A sweep of every way the SDK's
  objects can be rendered, walked, or serialized (#242) found the credential coming out of
  three of them. The client holds a `bc_uat_` token and every resource and model holds the
  client, so all three object kinds were affected.

  **Do you need to rotate your token?** Only if something in your code, your logs or your
  crash reports did one of the things in the first column. The exposure is to wherever
  *your* output went — nothing was ever sent to BaseCradle or to a third party by the SDK.

  | This emitted the token before 0.13.0 | This never did |
  |---|---|
  | `vars(client)` / `client.__dict__`, and anything printing them | `repr(client)`, `str(client)`, `f"{client}"`, `format(client)`, `pprint(client)` |
  | `json.dump`/`json.dumps` with `default=vars` — on a client, a resource **or** a record | `logging` with `%r`, `%s` or an f-string of a client, resource or record |
  | `client.__reduce__()` / `__reduce_ex__()`, and `copy.copy(client)` | `repr()` of any model — `ApiObject` prints field *names*, never values |
  | a crash reporter expanding frame locals through `__dict__` (Sentry, `rich`, `cgitb`, IPython's `%debug`) with a client in scope | a caught `BaseCradleError` — it holds only the problem document, never the request or response |
  | | tracebacks (`traceback.format_exc()`), and `pickle.dumps(client)`, which failed before reaching the wire |

  If you only ever read `client.token` yourself, or logged these objects with `%r`, nothing
  leaked and there is nothing to rotate. If a `vars()`-based dump or a crash report with a
  client in scope went anywhere you do not fully control, rotate:
  `session.revoke()` for one credential, `bc.sessions.revoke_all()` for all of them.
  Changing your password does **not** revoke anything.

  The detail, surface by surface:

  - **`vars(client)` and `client.__dict__` carried the token**, because it was a plain
    instance attribute. Two consequences were worse than they look. `json.dump(obj, fp,
    default=vars)` **writes the credential to the stream and only then raises** — the
    `ValueError: Circular reference detected` that stops `json.dumps` discards its partial
    result, but a dump to a file, socket or log stream has already flushed it, and this
    held for a resource and a model too, through the client they hold. And any crash
    reporter that expands frame locals one level through `__dict__` — Sentry, `rich`,
    `cgitb`, IPython's `%debug` — put the token in the report for any traceback with a
    client in scope. The token now lives in a `__slots__` slot on the client core, which
    is absent from `__dict__`, so neither walk reaches it.
  - **`__reduce__` handed out the whole instance dict.** `pickle.dumps(client)` failed only
    by accident — `httpx` happens to hold an unpicklable lock — *after* the protocol hook
    had already returned the credential, and `copy.copy(client)` succeeded outright,
    yielding a second live client on the same token.
  - **`repr(client)` was silent about the credential** rather than explicit. Silence cannot
    be told from "this object holds no secret", which is exactly the distinction you need
    when reading a log for a leak.

  Everything else measured already held: `repr`/`str`/`format`/`pprint`/`%r`-logging of a
  client, resource or model never emitted it, `ApiObject.__repr__` shows field *names* and
  never values (so a webhook endpoint's `ingest_url` was never printed either), `ApiObject`
  offers no `to_dict` and is not iterable, `__eq__`/`__hash__` read wire data only, and
  `BaseCradleError` keeps only the parsed problem document — no `httpx` request or response
  — so no caught error reaches the `Authorization` header. Those are now pinned by tests
  rather than resting on nobody having looked.

  One thing the sweep found and deliberately did **not** change: `WebhookEventHeaders` is a
  `dict` subclass, so unlike every `ApiObject` it renders and JSON-serializes its values
  directly. Those are an inbound delivery's own request headers, never this client's
  `Authorization` — but a sender that authenticates its POST puts *its* secret there, and
  `log.debug("%r", event.content.headers)` prints it. Redacting would break the SDK's
  wire-exactness rule ("reads match the wire"), so this is raised for a decision rather
  than settled here.

  **Since decided** (#246): `repr()` elides the values and shows the header names, which
  is not a read and so leaves wire-exactness intact. See the entry at the top of this
  file. Everything in the paragraph above is still true of 0.13.0 itself.

  **What you may notice:**

  - `repr(client)` gained a field: `<BaseCradle base_url='https://basecradle.com'
    token=[REDACTED]>`. The redaction is deliberately explicit.
  - **Resources have real reprs.** `<MessagesResource path='/messages' filters={}>`,
    `<MessagesResource path='/messages' filters={'timeline': '019e...'}>`,
    `<TimelineMessages timeline='019e...'>`, and `<TimelinesResource>` for the three that
    carry nothing but the client — in place of `<... object at 0x7f...>`.
  - **Pickling or copying a client or a resource now raises `BaseCradleError`** naming the
    risk, at every pickle protocol and through `copy.copy`, `copy.deepcopy` and `copyreg`.
    A client cannot be serialized because it authenticates with your token; a resource
    cannot because it is a live handle whose records are fetched lazily, so the message
    points at iterating it explicitly instead. `copy.copy(client)` previously returned a
    working clone and `pickle.dumps` previously raised `TypeError`.
  - **Deep-copying anything that holds a client refuses too**, with the same error: a
    `deepcopy` of a resource or of an attached record (`Timeline`, `Message`, …) recurses
    into the client and lands on its refusal. `copy.copy` of a *record* still works — a
    shallow copy never touches the client, so it was never a route out with the
    credential.
  - **`client.token` is unchanged** — it reads and assigns exactly as before. `login()`
    still documents it as the one place a minted credential can be read, and that is still
    true; only where it is *kept* moved. As before, assigning it does not re-authenticate
    an existing client: the transport's `Authorization` header is built once, at
    construction.
  - **`vars(client)` no longer lists `base_url`, `start_here`, `session`, `_timeout` or
    `_max_retries`** either — they share the slots declaration with the token, because a
    class cannot assign a name its `__slots__` omits. Every one is unchanged as an
    attribute (`client.base_url`, `client.session`, …) and `base_url` is in the repr; only
    a `vars()`-based diagnostic dump sees less.

  **Two stated limits, rather than quiet ones.** Neither is the #242 class of bug — a
  *generic* serializer emitting the credential unasked — and both are pinned by tests so
  they stay documented:

  - **An exhaustive attribute walker still reaches it.** A client must hold its credential
    to authenticate, and it hands it to `httpx` as a default header, so a serializer that
    recursively walks every attribute of every object it reaches — tolerating cycles,
    following private names — arrives at `httpx`'s own header storage: measured at depth 8
    from a client and depth 10 from a resource or a record. Nothing has to be *named* to
    get there. (`httpx` redacts `authorization` in its own `Headers.__repr__`, so only a
    walker that bypasses reprs sees the value. The `json.dump(…, default=vars)` idiom above
    is closed; combining a `TypeError`-tolerant `default` with `sort_keys=True` is deep
    enough to reach it.) Closing this would mean authenticating per request instead of once
    per client — a design change, not a fix, so it is raised rather than taken.
  - **A caller-registered reducer bypasses the refusal.** `copyreg.dispatch_table` and
    `Pickler.reducer_override` are consulted *before* `__reduce__`. That is a caller
    overriding on purpose, not a hole to plug.

### Changed

- **Iteration and `get()` are now typed by the record they yield, not `Any`.** The
  resource core is generic over its model, so a type checker reads
  `for message in bc.messages` as `Message`, `bc.tasks.filter(status="pending")` as
  `Task`, and `bc.assets.get(uuid)` as `Asset` — all of which were `Any`. `py.typed`
  promises these annotations are real; now they are specific enough to be worth having.
  Two consequences for anyone running a type checker against the SDK:

  - **Mistakes that used to pass now fail**, which is the point:
    `message: Message = bc.assets.get(uuid)` is an error rather than silently accepted.
    If such an assignment exists in your code, it was always wrong about the runtime
    object.
  - **`ItemsResource` and `AsyncItemsResource` are generic**, so a bare annotation of
    either (`def count(r: ItemsResource) -> int`) is now reported under
    `mypy --strict`'s `disallow_any_generics`. Write `ItemsResource[Message]`, or
    `ItemsResource[Any]` if the resource genuinely is not known.

  No runtime behaviour changes — the same objects come back from the same calls.

### Fixed

- **A malformed `problem+json` body no longer crashes the SDK.** `exception_from_response`
  looked the wire's `code` up in its registry without checking it was a string, so a body
  whose `code` was a list or an object raised `TypeError: unhashable type` out of the
  lookup — and the caller got a bare `TypeError` with no status, no code and no problem
  document, instead of the `BaseCradleError` this path promises ("a new error code must
  never crash the SDK"). A non-string `code` is now simply not a code the SDK knows, which
  is the same answer it already gave for a code added after this release. Everything that
  worked before is unchanged, `{"code": null}` included: `detail` is still the message and
  the problem document is still attached. Surfaced by `mypy --strict` (#229), which had
  been reporting the unnarrowed lookup all along.

## [0.12.0] - 2026-09-30

### Changed

- **A `timeline.items` row now hands back the record's own content class.** `item.content`
  is a `MessageContent`, `AssetContent`, `WebhookEventContent` or `TaskContent` by
  `item.type`, where it was the generic `ApiObject` on that path before. One record has one
  shape: the same webhook event read through `bc.webhook_events` and through
  `timeline.items` now answers the same way for the same field, so 0.11.0's case-folding
  `headers` reaches both — `item.content.headers["X-GitHub-Delivery"]` folds case exactly
  as `event.content.headers` does, where it previously raised `KeyError`. Every future
  enrichment of a content class reaches both paths for the same reason. Reads still match
  the wire; nothing is renamed or hidden. Both SDKs changed in lockstep
  (python#210, ruby#189).

  Three observable consequences of the same change, called out rather than left to be
  discovered:

  - **`repr(item.content)`** changes from `<ApiObject [...]>` to `<MessageContent [...]>`
    (and the other three) — it now names the class you actually have.
  - **Equality across the two read paths is no longer ruled out by type.**
    `ApiObject.__eq__` is `same type and same wire data`, so `item.content` and the same
    record's own `content` fetched directly could never compare equal before, whatever the
    API returned; now they compare equal whenever the two responses carry the same fields,
    which is what the spec describes. Code relying on the old unconditional inequality to
    tell the paths apart should branch on `item.type` instead. (The *item* is still a
    `TimelineItem` and still does not equal the record itself — only the contents match.)
  - **On a `webhook_event` row, `item.content.headers` is now a `WebhookEventHeaders`
    copy rather than the wire dict itself.** Previously that path handed back the very dict
    inside the item's data, so `item.content.headers is item._data["content"]["headers"]`
    held and writing to it mutated the item. It is now the same detached, case-folding
    object the `bc.webhook_events` path has returned since 0.11.0 — a fresh one per access.
    `==` against a plain dict is unaffected; identity comparisons and in-place writes on
    that path are. Neither was ever supported (models are read-only views), but the
    behavior did change.

  An item `type` this release does not know still reads as the generic wire-exact
  `ApiObject` rather than raising: the API is additive-only, so an item type added after
  this release keeps reading.

## [0.11.0] - 2026-09-30

### Added

- **`WebhookEventHeaders`** — the type `WebhookEventContent.headers` returns, exported from
  the package root like every other model. A `dict` of exactly the pairs the wire carried
  whose lookup folds case; see *Changed* below for the behavior.

### Changed

- **`WebhookEventContent.headers` now looks header names up case-insensitively.** Header
  names are case-insensitive by RFC, the platform rewrites them to canonical Title-Case per
  segment (`X-Github-Delivery`, not `X-GitHub-Delivery`), and its docs tell consumers to
  match case-insensitively — so a case-sensitive headers object was a defect in the object.
  `headers["X-GitHub-Delivery"]` (GitHub's own published spelling),
  `headers["x-github-delivery"]` and the wire's own `headers["X-Github-Delivery"]` now all
  read the same header; `in` and `.get()` fold case the same way. **Reads still match the
  wire:** the value is a `dict` of exactly the pairs the API returned, so iterating,
  `keys()` and `==` read the platform's spelling and nothing is renamed. A header that was
  genuinely not delivered stays *absent* rather than becoming `None` — subscripting raises
  `KeyError` naming the headers that did arrive, and `.get()` returns its default. Both
  SDKs changed in lockstep (python#199, ruby#173).

## [0.10.0] - 2026-09-23

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

[0.13.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.13.0
[0.12.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.12.0
[0.11.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.11.0
[0.10.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.10.0
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
