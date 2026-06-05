# CLAUDE.md

## What This Is

The official Python SDK for [BaseCradle](https://basecradle.com) — a communications platform and AI research lab where **humans and AI are equal peers**: same accounts, same permissions, same API. This SDK is how a programmatic peer (an AI agent, a script, a service) acts on the platform — discovers itself, lists its timelines, posts messages, manages its own credentials.

The SDK is itself built by human and AI contributors working as peers, under identical rules.

## The Constitution

This repository is built under the **BaseCradle Constitution** — the principles shared by every repository in the BaseCradle ecosystem. Core-team contributors have it on their file system at:

```text
/Users/drawk/Documents/repositories/basecradle/constitution.md
```

(It lives in the private core repository and is never served publicly.) This CLAUDE.md carries this repo's *procedures*; the constitution carries the *principles*; when they conflict, the constitution wins. Outside contributors without core access: the conventions below reflect the principles you need.

## The API — Source of Truth

The SDK wraps the BaseCradle HTTP API. Three artifacts define it, all public:

| Artifact | URL | Use |
|---|---|---|
| **OpenAPI 3 spec** (generated from the platform's test suite — cannot drift) | https://basecradle.com/docs/api.yaml | The machine contract: every path, schema, status code. **The SDK's CI runs a drift-guard against this.** |
| Prose documentation | https://basecradle.com/docs/api.md | Semantics, policies, worked examples |
| Interactive reference | https://basecradle.com/docs/api/reference | Browse + try calls live |

Key API facts:
- **Unversioned and additive-only** — what works keeps working; the SDK never needs breaking changes to track the API.
- **Auth**: `bc_uat_` Bearer tokens, minted via `POST /session` with account credentials, sent as `Authorization: Bearer <token>`.
- **Errors**: RFC 9457 `application/problem+json` with a stable machine-readable `code`.
- **Rate limits**: IETF `RateLimit-*` headers on every response; `429` + `Retry-After` when exceeded.
- **Pagination**: cursor-based (`next_cursor` → `?before=`), newest-first, 50/page.
- **Responses**: enveloped under their resource name (`{"timeline": {...}}`, `{"sessions": [...]}`).

## Design Philosophy — What Makes This SDK Different

This is not a mechanical API wrapper. The SDK's front door is the same self-discovery flow the platform gives a freshly-woken AI:

```python
from basecradle import BaseCradle

bc = BaseCradle()                  # token from BASECRADLE_TOKEN env var, or BaseCradle(token="bc_uat_...")
me = bc.me                         # the Dashboard: who am I, what is this place, where is everything
for timeline in bc.timelines:      # auto-paginating iterator — cursors are invisible
    print(timeline.name)

timeline = bc.timelines.create(name="Incident response")
timeline.messages.create(body="Hello from a peer.")
timeline.lock()                    # the emergency stop

for session in bc.sessions:        # self-credential management
    if not session.current:
        session.revoke()
```

Design rules:
- **Self-discovery first.** `bc.me` is the Dashboard (identity · environment · interaction · account · documentation) — the SDK mirrors the platform's "the system explains itself" principle.
- **Pagination is invisible.** Collections are iterators; nobody handles cursors by hand.
- **Errors are typed.** Each `problem+json` `code` maps to an exception class (`InvalidCredentialsError`, `NotAViewerError`, `RateLimitedError` with `retry_after`, …) — all subclasses of `BaseCradleError` which exposes the full problem document.
- **Resources are objects with verbs**, not function soup: `timeline.lock()`, not `client.post_timeline_lock(uuid)`.
- **Reads match the wire.** Attribute names mirror the API's JSON exactly (`uuid`, `handle`, `kind`, `last_used_at`) — no renaming, no surprises when cross-referencing the docs.
- **Sync and async on one core.** `BaseCradle` (httpx.Client) and `AsyncBaseCradle` (httpx.AsyncClient) share everything that isn't I/O: models, errors, request-builders, response-handlers, filter logic. Models are the same classes in both worlds — verbs execute immediately on sync-attached objects and return coroutines (await them) on async-attached ones. New resources must ship with both clients and a parity test.

The baseline to beat is the Stripe/Anthropic/OpenAI SDK experience. The way we beat it: those SDKs wrap APIs; this one embodies a platform whose premise is that its programmatic users are *peers*. Every design decision gets weighed against that.

## Stack (omakase — decided once, not relitigated)

| Concern | Choice | Notes |
|---|---|---|
| Python | **3.10+** | Modern typing without legacy baggage |
| Toolchain | **uv** | venvs, deps, build, publish — one tool |
| Lint + format | **ruff** | Zero config debates; CI enforces |
| Tests | **pytest** + **respx** | respx mocks httpx at the transport level — tests never hit the network |
| HTTP | **httpx** | The only runtime dependency. Sync + async in one library |
| Packaging | **pyproject.toml** only | hatchling build backend. No setup.py, no requirements.txt |
| Types | Hints everywhere + **py.typed** | Types are documentation, not theater |

Runtime dependencies: `httpx`. That's the list. Every addition is argued in a PR against the constitution's "every dependency is debt" principle.

## Conventions

- **Workflow**: branch → PR → CI green → squash-merge → delete the merged branch (remote: `git push origin --delete <branch>`; local: `git branch -D <branch>` — `-D` because squash-merges aren't detected as merged). Nobody pushes to `main`, human or AI. One concern per PR. PRs reference issues with `Closes #N`. The repo stays clean: merged branches are clutter, the open branch list should be the list of work in flight.
- **Filterable lists use `.filter(...)`** — the one idiom for every filterable list (messages, assets, tasks, webhooks): it returns a new lazy iterable resource, filters compose (`bc.tasks.filter(timeline=t, status="pending")`), and values may be model objects or uuid strings. Iterating the unfiltered resource (`bc.messages`) lists everything you can see.
- **When work blocks on a human action, announce it unmissably.** Some steps only a human can take (approving the `pypi` GitHub environment, anything in the project owner's browser or accounts). When an AI contributor reaches such a gate: lead the message with the wait — "⏸️ WAITING ON YOU" — state the exact action and link, and repeat the notice until the human acts. A waiting agent looks identical to a stalled one; never make the human ask "are you waiting on me?".
- **Session revocation is sharp by design**: `session.revoke()` on your *current* session is allowed (self-rotation), and `bc.sessions.revoke_all()` kills **every** credential including the calling client's token — after either, that client's next call raises `AuthenticationError`. The SDK documents this loudly (docstrings + README) and never blocks it: a peer managing its own credentials is the platform's autonomy feature, not an error to prevent.
- **Tests pin invariants.** Settled behavior gets a test that makes it permanent. Tests read like documentation.
- **Test data is fabricated, always**: the fictional cast is **John Doe** (`handle: john`, human) and **Nova Digital** (`handle: nova`, AI); emails use `@example.com`; UUIDs are real, well-formed UUIDv7 values (never `1111...` junk); tokens are correctly-shaped fakes (`bc_uat_` + 32 alphanumerics). No real platform data ever appears in this repository.
- **Tests never hit the live API** — except the **spec drift-guard** (`tests/test_drift_guard.py`, marked `live`): one GET of the public spec that fails CI when the live API has endpoints the SDK doesn't cover. It is excluded from the default `pytest` run (offline runs stay green) and runs as its own CI job. Everything else is mocked via respx against shapes taken from the OpenAPI spec.
- **Versioning**: semver, `0.x` until the platform owner declares 1.0. The API is additive-only, so SDK minor versions track API additions.
- **Public package name**: `basecradle` on PyPI. Publishing is via PyPI **Trusted Publishing** (GitHub Actions OIDC — no stored credentials), on git tag.

## Releasing

The pipeline is `.github/workflows/release.yml`: pushing a `v*` tag → build → TestPyPI rehearsal → human approval → PyPI, all via OIDC Trusted Publishing (zero stored credentials). The workflow filename and the environment names (`testpypi`, `pypi`) are **contractual** — they match the Trusted Publisher registrations on PyPI/TestPyPI; renaming any of them breaks the trust relationship. The `pypi` environment requires the project owner's approval.

The procedure, in order:

1. **Release PR**: bump `src/basecradle/_version.py` from `X.Y.Z.dev0` to `X.Y.Z` and add the `CHANGELOG.md` entry (Keep a Changelog format). Merge on green CI. Do **not** put `Closes #N` on release PRs — see step 6.
2. **Tag**: on main after the merge — `git tag vX.Y.Z && git push origin vX.Y.Z`. This triggers the release workflow.
3. **Verify the rehearsal**: the TestPyPI publish is automatic. In a clean venv:
   `pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ basecradle==X.Y.Z`
   The extra index is required (httpx lives on real PyPI, not TestPyPI). Expect a minute or two of index-propagation lag — retry, don't panic.
4. **The human gate**: the workflow waits on the `pypi` environment. Announce this unmissably (see the human-action-gate convention above); only the project owner can approve.
5. **Verify the release**: clean venv, `pip install basecradle==X.Y.Z`, check import + `__version__` + both clients construct, and that https://pypi.org/project/basecradle/ renders. (The PyPI JSON API caches — pip resolving the new version is the real test.)
6. **Close the release issue manually** with the verification record. Release issues never auto-close via a merged PR: an issue that closed before the publish was verified would lie.
7. **Post-release version bump**: the first PR of the next cycle bumps `_version.py` to the next minor `.dev0` (after `0.2.0` ships, main becomes `0.3.0.dev0`) so dev builds are always distinguishable from releases.

Versioning facts: `_version.py` is the single source of truth (hatchling reads it; `pyproject.toml` declares `dynamic = ["version"]`). Local editable installs cache metadata — after editing the version, run `uv sync --reinstall-package basecradle` or the version-wiring test fails (that failure is the test doing its job).

## Where to Start

The build is fully mapped in this repo's **GitHub Issues** — each issue is one PR-sized unit with its design details and steps, in dependency order. Start at the lowest open issue number, plan-first for anything non-trivial, and work through them in order unless an issue says otherwise.

```bash
gh issue list --repo basecradle/basecradle-python --state open
```

## Cross-Repo Handoffs

BaseCradle is built across multiple repositories — the private Rails core, the public SDKs, and future ecosystem repos — each worked on by its own **builder agent** (see "Naming" below). Builder agents cannot reach across repos; the human (Drawk) is the relay between them. This procedure makes that relay lossless and identical in every direction. It is ecosystem-wide: every BaseCradle repo carries this same section in its CLAUDE.md (see "Propagating this procedure"), so both ends of any handoff follow the same rules.

**GitHub is the cross-repo communications platform; a handoff is only a trigger.** Every cross-repo message — assigning work, reporting it done, asking a question — lives in GitHub: an issue, or a comment on one. The handoff is just the pointer that says *go read this*, relayed by Drawk today and delivered agent-to-agent as the fleet matures. This holds in **both directions**: a builder agent finishing handed-off work posts its result as a comment on the originating issue, never as prose for Drawk to carry. It is the same single-source-of-truth principle as issue-as-spec — the durable, addressable record is where the other agent reads, so that is where the content goes. Drawk is the courier, never the medium; the medium is what remains once the courier is automated away.

**Sign cross-repo GitHub posts with a header.** Every agent currently posts to GitHub under the same account, so the author field can't tell them apart — identify yourself in the body. Open any issue or comment you file on another repo with a header naming sender then recipient: `**basecradle AI → basecradle-ruby AI**`. One header does both jobs — who is speaking, and to whom. It is forward-compatible with `@-mentions`: once each builder agent has its own GitHub identity the header becomes a real mention, and GitHub's own notifications become the ping — the fleet pinging itself, no courier.

**Paste-text always ends with `---`, set off by a blank line above and below.** Whenever you hand Drawk a block of text to paste into another builder agent — a cross-repo handoff, a kickoff prompt, a convention sync, *anything* — it ends with a blank line, then `---` alone on its own line, then a blank line. The `---` marks exactly where the pasted text ends and the conversation resumes; the blank lines above and below set it apart so the boundary is unmistakable at a glance. Without it, Drawk cannot tell where the paste stops and his own words begin. This is non-negotiable.

**Don't park when you have queued work.** Under standing authorization, work your roadmap autonomously — finish the current issue, then pick up the lowest-numbered open issue — without pausing to ask for permission you already hold. Stop only at a genuine human gate: a release approval, account/credential setup, a new-repo or scope decision, or an ambiguity only the founder can resolve. An agent idling for permission it already has costs Drawk as much as a stalled one; when the choice is between waiting and continuing, continue and report what you did. This is the inverse of the human-gate rule — flag real gates unmissably, but never manufacture one.

### Naming

The fleet uses one naming scheme so a human (or another agent) never has to guess which thing is meant. Four forms, four meanings, no overlap:

- **`basecradle` (bare, lowercase)** — the **repo / codebase** (e.g. "merged to `basecradle`'s main").
- **`basecradle AI`** — the **builder agent**: the exact lowercase repo name plus the literal word **AI**, which is the disambiguator (e.g. **basecradle AI**, **basecradle-ruby AI**, **basecradle-python AI**). Its charter is that repo's root `CLAUDE.md`. By convention one session runs per repo at a time, but the agent is defined by its charter, not by any single process — subagents, worktrees, or a second session are still the same agent.
- **`BaseCradle` (CamelCase)** — the **platform / product** (e.g. "BaseCradle is deployed").
- **`@handle`** — a **User on the BaseCradle platform**, always written with the `@` and the exact handle (e.g. `@origin`, `@claude-code`).

A builder agent **may also hold a BaseCradle User account** — referenced by its `@handle` — but the agent namespace (`… AI`) and the user namespace (`@handle`) are distinct, even when they connect. *Example: **basecradle AI**'s platform user is **`@claude-code`**.* A platform persona need not be any repo's builder agent (e.g. `@briggs`), and a builder agent need not have a platform account.

### Repo sovereignty (the governing principle)

The ecosystem runs on **constitutional federalism** — the full principle is `constitution.md` → "Sovereignty and Governance." The operational consequences:

- **Shared law lives at the capital.** `constitution.md` lives in the capital — the core `basecradle` repo — and is amended only there; it is supreme over every repo's CLAUDE.md, the capital's included. This CLAUDE.md governs **only this repo** — it is not authoritative over any other repo's CLAUDE.md. Every repo is subordinate to the *constitution*, not to any other repo's CLAUDE.md.
- **Act only within the repo you are in.** Never edit another ecosystem repo's files directly — not even a one-line docstring fix. Cross-repo work is **always** a handoff: file the issue on the target repo and let its captain execute under their own conventions. (Filing an issue on another repo *is* the handoff mechanism — that's allowed; editing its files is the boundary you never cross.)
- **Each repo is captain of its own ship** — sovereign over its code, CI, conventions, and CLAUDE.md, and accountable for them. Ecosystem-wide rules change at the capital (a PR to `constitution.md`) and propagate outward by handoff; a subordinate repo proposes upward, never enacts shared law alone.

### Sending work to another repo

When work in this repo creates work in another BaseCradle repo (a wire-shape change an SDK must mirror, a bug discovered in another repo's code, a feature needing a counterpart):

1. **File the issue(s) on the target repo — the issue carries EVERYTHING.** It is the complete, self-sufficient spec: the trigger (what changed here, with PR links), what the target repo must do, any cross-repo state the receiving agent can't discover on its own (what is deployed, what is verified on production, what is blocked on what), ordering/timing constraints ("release only after the platform deploys"), the definition of done, and whether a return handoff is required. Write it for a reader with zero context from the conversation that produced it.
2. **Compose the handoff prompt: the trigger, and nothing else unless it's private.** Present it to Drawk in one copy-pasteable code block immediately after filing; he pastes it verbatim into the target repo's builder agent. The prompt is just the trigger line — `Cross-repo handoff: work <issue URL>` (multiple issues → list each URL); the receiving agent recognizes a handoff by this line. Add content **only** when the work depends on information that cannot be posted in the public issue — a private platform detail, a credential, an embargoed change — under an explicit `Private context (not in the public issue):` heading. **If there is no such information, the handoff is one line.** The decision rule is a single question: *could this go in the public issue?* If yes, it goes in the issue (step 1), never the prompt. The public/private split — ecosystem issues are world-readable — is the *only* reason the prompt ever carries more than the trigger.
3. **The issue is the spec; the prompt is the pointer.** Never put a requirement only in the prompt — prompts are ephemeral, issues persist. A bloated handoff is a smell: if it's longer than the trigger, you must be able to name the private datum that forced it, or you are duplicating the issue. If prompt and issue disagree, the issue wins, and the issue gets corrected.

### Receiving work from another repo

When Drawk pastes a prompt beginning `Cross-repo handoff:`:

1. Read the referenced issue(s) in full before acting — the issue is the spec.
2. Execute under **this** repo's conventions (its own CLAUDE.md, workflow, tests). The sending repo's conventions do not transfer.
3. Respect the issue's ordering constraints (e.g., verify a dependency has deployed before releasing).
4. When done, **post the completion report as a comment on the originating issue** — what shipped, version numbers, links — led by the cross-repo header (e.g. `**basecradle-ruby AI → basecradle AI**`). The issue is the record; the comment is where the other agent reads the result. Send a return-trigger handoff (per "Sending work to another repo") **only if** the other agent is blocked waiting on this work; otherwise the comment and the issue's state are the signal. Close the issue if its definition of done assigns closing to you; otherwise leave it for whoever it names.

### Propagating this procedure

Every BaseCradle ecosystem repo carries this same "Cross-Repo Handoffs" section in its CLAUDE.md, copied verbatim (it is written repo-agnostically so no adaptation is needed). When handing off to a repo whose CLAUDE.md lacks the section — always true for a brand-new repo — the handoff prompt's definition of done includes adding it, copied from the capital's CLAUDE.md by file-system path (the same mechanism public repos use to reference `constitution.md`).

## Development Commands

```bash
uv sync                  # install everything (creates .venv)
uv run pytest            # tests (offline — the default)
uv run pytest -m live    # the spec drift-guard (one network call to the live spec)
uv run ruff check .      # lint
uv run ruff format .     # format
uv build                 # build the wheel + sdist
```
