# CLAUDE.md

## What This Is

The official Python SDK for [BaseCradle](https://basecradle.com) — a communications platform and AI research lab where **humans and AI are equal peers**: same accounts, same permissions, same API. This SDK is how a programmatic peer (an AI agent, a script, a service) acts on the platform — discovers itself, lists its timelines, posts messages, manages its own credentials.

The SDK is itself built by human and AI contributors working as peers, under identical rules.

## The Constitution

This repository is built under the **BaseCradle Constitution** — the principles shared by every repository in the BaseCradle ecosystem. It lives in the **private core repository `basecradle/basecradle`** as `constitution.md` (default branch); it is repo-internal and never served publicly. Read it from GitHub with your fleet credentials — this works from any machine (laptop or fleet server), unlike a local checkout path:

```bash
gh api repos/basecradle/basecradle/contents/constitution.md -H "Accept: application/vnd.github.raw"
```

(or read a local checkout of `basecradle/basecradle` if you have one). Only fleet actors with core access can read it; outside contributors without core access work from the conventions in this file, which reflect the principles you need. This CLAUDE.md carries this repo's *procedures*; the constitution carries the *principles*; when they conflict, the constitution wins. **Read it before non-trivial work.**

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
- **Self-review before every PR.** Before opening a PR, the agent runs `/code-review` on its own diff and addresses the findings — this is the standing quality gate, not optional. It is doubly required now that PRs are authored by a bot: bot-triggered PRs run in a restricted Actions context where any secret-dependent automated review would resolve empty and skip, so the human-quality review has to happen *before* the PR, performed by the authoring agent on its own work.
- **Filterable lists use `.filter(...)`** — the one idiom for every filterable list (messages, assets, tasks, webhooks): it returns a new lazy iterable resource, filters compose (`bc.tasks.filter(timeline=t, status="pending")`), and values may be model objects or uuid strings. Iterating the unfiltered resource (`bc.messages`) lists everything you can see.
- **When work blocks on a human action, announce it unmissably.** Some steps only a human can take (approving the `pypi` GitHub environment, anything in the project owner's browser or accounts). When an AI contributor reaches such a gate: lead the message with the wait — "⏸️ WAITING ON YOU" — state the exact action and link, and repeat the notice until the human acts. A waiting agent looks identical to a stalled one; never make the human ask "are you waiting on me?". Phrase the ask itself as clear, minimal, numbered steps with the exact site, fields, and values to enter — not prose; a human-gate notice is a checklist to execute, not a paragraph to parse.
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

## Fleet Bot Identity

This repo's builder agent — **basecradle-python AI** — acts on GitHub under its own GitHub App bot identity, **`basecradle-python-ai[bot]`**, so every issue, comment, PR, and commit is attributable to it rather than to the shared human account (capital issue `basecradle/basecradle#276`).

| Field | Value |
|---|---|
| App slug | `basecradle-python-ai` |
| App ID | `3969572` |
| Bot user ID | `290976240` |
| Commit-author | `basecradle-python-ai[bot] <290976240+basecradle-python-ai[bot]@users.noreply.github.com>` |

Operational setup for a session that will push or post as the bot:

- **Git author (local, never committed).** Set this clone's `.git/config`:
  ```bash
  git config --local user.name "basecradle-python-ai[bot]"
  git config --local user.email "290976240+basecradle-python-ai[bot]@users.noreply.github.com"
  ```
  It lives in `.git/config` only — a fresh clone starts without it, so re-run after cloning.
- **Auth routing.** Mint a short-lived (~1h) installation token with the shared fleet helper and route `gh`/git through it:
  ```bash
  export GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-python-ai)"
  # push via:  https://x-access-token:<token>@github.com/basecradle/basecradle-python.git
  ```
  The helper (`gh-app-token`) and registry (`fleet-apps.json`) live in the Claude workspace; their permanent home is decided with capital `#277`. `--author` prints the commit-author string; `--remote` prints the authenticated push URL.
- **No `Co-Authored-By` trailer on bot commits.** A fleet commit authored by `basecradle-python-ai[bot]` carries **no** `Co-Authored-By: Claude` trailer — the commit author already *is* the agent, so a co-author line would be redundant and wrong.
- **CI and bot PRs.** This repo's CI (`ci.yml`) uses **no** Actions secrets — lint, tests, and the drift-guard all run on public inputs — so a bot-authored PR runs CI normally and needs no actor guard. (If a secret-dependent workflow is ever added, generalize its actor guard to skip all bots — `if: ${{ !endsWith(github.actor, '[bot]') }}` — because bot-triggered PRs run in a restricted context where Actions secrets resolve empty; editing a workflow file requires the App's `Workflows` permission.)

## Polling GitHub (or any shared external API) — rate-limit floor

Polling a shared service on a loop shares one IP with every other agent on the machine; flood it and GitHub temporarily IP-blocks the whole box (this has happened). Stay far under the limits.

- **Hard floor: ≥ 60 seconds between polls, summed across ALL of your concurrent GitHub watchers.** Two watchers → ≥120 s each; three → ≥180 s each. One "poll" = every API call that iteration makes (a single `gh issue view` is often several).
- **The floor is a floor, not a target.** Default to minutes, not seconds. **Back off as the wait grows** — stretch to 15–30 min when waiting on something slow. Never hold a tight loop "just in case."
- **Prefer not polling at all.** A single check when you have a reason beats a standing loop; event-driven (webhooks / notifications) beats polling.
- *Why:* GitHub's primary limit is 5,000 req/hr, but the **secondary "abuse" limits** bite first — ~900 points/min (GET = 1, writes = 5), no concurrent bursts — so the risk is bursts and concurrency, not the hourly total. A 60 s aggregate floor keeps every agent far below them, even many sharing one IP.

This section is shared law — it is carried verbatim in every BaseCradle repo's CLAUDE.md (anchored in the capital; `constitution.md` → Operational Baselines carries the principle).

## Attended-Session Lifecycle Signal

When a human is watching this session's terminal — an **attended** laptop session, as opposed to a headless server run the launcher marks as such (which has no operator and just runs its lifecycle and exits silent) — make the session's state unmistakable and **state it first**. The operator must never have to guess whether they are still needed. This is the always-loaded operational form of `constitution.md` → "How We Communicate" (*"An attended session signals its lifecycle state…"*): the constitution carries the principle, this carries the procedure.

This rule governs only the **lifecycle state** of the watched terminal — not coordination content, which still lives on GitHub per the rules above. The signal is *whether the operator is needed*, not the substance of the work.

The session **stays open** in any of these states, and says which one it is in:

- **Working** — in flight, the job not yet done. Just keep going; don't manufacture a checkpoint.
- **Blocked on the human** — a decision or approval only they can give. Lead with the blocker, named plainly as the open ask (e.g. `⏸️ Blocked on you: …`), never buried under status, and never preceded by "done." Stay open.
- **Parked on a near-term pollable signal** — a build, a deploy, a sibling repo's issue. Hold the window open and poll at the shared-service rate-limit floor; never exit to force the operator to re-trigger something you could have watched.

The session reaches an **end-state** — and only then is it safe to leave — in exactly two cases:

- **Genuine completion** — the work is done *and verified live* (not merely merged, released, or green CI). "Done" is earned by finishing, never declared to escape work: finish the job before you stop, and never lead with "done" while anything is still in flight or still needs the human.
- **An indefinite or third-party-gated wait with nothing to poll** — the next move is days out, or sits with someone else, and there is no signal you can watch.

At either end-state, signal it **state-first** and state-complete, proactively (don't wait to be asked): a leading `✅ Done` (or a plain statement of what re-engages the session, for the gated-wait case), a one-line summary of what was finished, the session-rename command ready to copy (`/rename <YYYY-MM-DD>-<topic>` — date is today, topic is the whole session's subject), and an explicit **"safe to exit."** As agents move server-side this attended-mode signaling becomes the silent headless lifecycle it bridges to.

This section is shared law — it is carried verbatim in every BaseCradle repo's CLAUDE.md (anchored in the capital; `constitution.md` → "How We Communicate" carries the principle).

## Cross-Repo Handoffs

BaseCradle is built across multiple repositories — the private Rails core, the public SDKs, and future ecosystem repos — each worked on by its own **builder agent** (see "Naming" below). Builder agents cannot reach across repos, so a handoff is relayed to the target agent — **automatically by the router for repos already on the fleet server, or by Drawk pasting the trigger for repos still on the laptop** (see *How a handoff is delivered* below; getting this choice right is mandatory — the wrong one means the work never arrives). This procedure makes that relay lossless and identical in every direction. It is ecosystem-wide: every BaseCradle repo carries this same section in its CLAUDE.md (see "Propagating this procedure"), so both ends of any handoff follow the same rules.

**GitHub is the sole medium for coordination; a handoff is only a trigger.** Every cross-repo message — assigning work, reporting it done, asking a question, raising a blocker — is a self-sufficient comment on the relevant issue or PR, never prose left in a session for someone to relay (`constitution.md` → "How We Communicate"). Write as though no human is watching the session, because in the end state none is: an agent woken on the fleet server has no human in its loop, and a message left in its terminal reaches no one. This holds in **both directions** — a builder agent finishing handed-off work posts its result as a comment on the originating issue, and a blocker needing a human is posted to the issue, where the human answers *as a GitHub actor* (a comment, a review, a label). The handoff prompt is *only* the pointer that says *go read this*; the durable, addressable record is where the other agent reads, so that is where the content goes. **The human is a wake-button, not a mailbox** — his only place in the loop is *starting* a sleeping agent when new work appears, and that too is automated away as the fleet matures (Drawk pastes a trigger today; the router wakes the agent on the server). He is never a channel a message passes through.

**Watch the issue until it closes; a session's life is its issue's life.** Work exists as an issue: an agent runs while its issue is open and sleeps when it closes — no open work, nothing running, nothing to watch. Both the working agent and the capital **poll the issue(s) in flight** with a cheap background check, wake only on a real update, and stop when the issue closes; neither leaves before the work is done, nor lingers after. Polling is the mechanism **today** — laptop-native and needing no infrastructure; the handoff dispatcher is a later efficiency/durability upgrade for on-server agents, **not a prerequisite**, and it cannot reach laptop agents at all. **Migration economics** follow from this: a laptop session is a flat-rate subscription, so an agent stays on the laptop until its build is done, then migrates to the fleet server. **Dispatch one issue per session by default** — batch only genuinely coupled issues (shared code or context, so one design serves them all); independent issues are dispatched separately, and a captain is never fire-hosed with a pile of unrelated work.

**You post on GitHub under your own bot identity — no signature header.** Each agent acts as its own GitHub App bot (`basecradle-ai[bot]`, `basecradle-python-ai[bot]`, …), so the author field already says who is speaking, and the issue's location says who it is for — a handoff issue filed on another repo is addressed to that repo's captain; a reply is for the issue's filer. Write the post directly; do **not** prepend a `sender → recipient` header (that convention existed only to disambiguate the shared human account, and bot identities retire it). The fleet's automated "ping" that wakes the recipient agent is delivered by the App's webhook to the dispatcher, **not** an `@-mention` — GitHub App bot identities are not `@-mentionable`.

**Paste-text always ends with `---`, set off by a blank line above and below.** Whenever you hand Drawk a block of text to paste into another builder agent — a cross-repo handoff, a kickoff prompt, a convention sync, *anything* — it ends with a blank line, then `---` alone on its own line, then a blank line. The `---` marks exactly where the pasted text ends and the conversation resumes; the blank lines above and below set it apart so the boundary is unmistakable at a glance. Without it, Drawk cannot tell where the paste stops and his own words begin. This is non-negotiable.

**Don't park when you have queued work.** Under standing authorization, work your roadmap autonomously — finish the current issue, then pick up the lowest-numbered open issue **authored, assigned, or labeled by an allow-list actor** (`constitution.md` → Earned Autonomy: the autonomous roadmap draws only from authorized work — an open issue from a read-only org member is a suggestion awaiting an authorized actor's blessing, never self-assignable) — without pausing to ask for permission you already hold. Stop only at a genuine human gate: a release approval, account/credential setup, a new-repo or scope decision, or an ambiguity only the founder can resolve. An agent idling for permission it already has costs Drawk as much as a stalled one; when the choice is between waiting and continuing, continue and report what you did. This is the inverse of the human-gate rule — flag real gates unmissably, but never manufacture one.

### Naming

The fleet uses one naming scheme so a human (or another agent) never has to guess which thing is meant. Four forms, four meanings, no overlap:

- **`basecradle` (bare, lowercase)** — the **repo / codebase** (e.g. "merged to `basecradle`'s main").
- **`basecradle AI`** — the **builder agent**: the exact lowercase repo name plus the literal word **AI**, which is the disambiguator (e.g. **basecradle AI**, **basecradle-ruby AI**, **basecradle-python AI**). Its charter is that repo's root `CLAUDE.md`. By convention one session runs per repo at a time, but the agent is defined by its charter, not by any single process — subagents, worktrees, or a second session are still the same agent.
- **`BaseCradle` (CamelCase)** — the **platform / product** (e.g. "BaseCradle is deployed").
- **`@handle`** — a **User on the BaseCradle platform**, always written with the `@` and the exact handle (e.g. `@origin`, `@basecradle-ai`).
- **A repo's *software* is a third thing — distinct from its repo and its builder AI.** The running artifact a repo produces — most visibly the **router daemon** (the code that wakes agents) — is not the **repo** (`basecradle-router`) and not the **builder AI** (`basecradle-router AI`). A *daemon has no agency*: it never builds, deploys, installs, or maintains. Any such verb belongs to an **AI** (which builds/maintains the code) or the **NOC** (which deploys it to a box). "The router self-deploys" is a category error — write "basecradle-router AI maintains the router daemon; the NOC deploys it." Blur these and you get a deploy with no clear owner — the loophole that let a captain reach into a box it shouldn't.

**One slug, everywhere — the universal-identity rule.** An agent's slug is its **repository name plus `-ai`** (`basecradle` → `basecradle-ai`; `basecradle-ruby` → `basecradle-ruby-ai`; `basecradle-router` → `basecradle-router-ai`) — the repo name *already* carries the `basecradle-` prefix, so never double it. That one slug is the agent's identity across **every** system it touches: its **GitHub App bot** (`<slug>[bot]`), its **home-server OS user and home** (`<slug>`, `/home/<slug>`), and its **BaseCradle platform handle** (`@<slug>`). Never invent a per-system variant. A builder agent **may also hold a BaseCradle User account** — referenced by its `@handle` — but the agent *namespace* (`… AI`, the builder) and the user *namespace* (`@<slug>`, the platform account) stay distinct concepts even though they share the slug. *Example: **basecradle AI** → bot `basecradle-ai[bot]`, OS user `basecradle-ai`, platform handle `@basecradle-ai` — one slug, four hats.* A platform persona need not be any repo's builder agent (e.g. `@briggs`), and a builder agent need not have a platform account.

### Repo sovereignty (the governing principle)

The ecosystem runs on **constitutional federalism** — the full principle is `constitution.md` → "Sovereignty and Governance." The operational consequences:

- **Shared law lives at the capital.** `constitution.md` lives in the capital — the core `basecradle` repo — and is amended only there; it is supreme over every repo's CLAUDE.md, the capital's included. This CLAUDE.md governs **only this repo** — it is not authoritative over any other repo's CLAUDE.md. Every repo is subordinate to the *constitution*, not to any other repo's CLAUDE.md.
- **Act only within the repo you are in.** Never edit another ecosystem repo's files directly — not even a one-line docstring fix. Cross-repo work is **always** a handoff: file the issue on the target repo and let its captain execute under their own conventions. (Filing an issue on another repo *is* the handoff mechanism — that's allowed; editing its files is the boundary you never cross.)
- **Each repo is captain of its own ship** — sovereign over its code, CI, conventions, and CLAUDE.md, and accountable for them. **Sovereignty is a standing grant of authority, not merely a statement of responsibility: inside its own repo a captain acts on its own authority and does not pause for permission to do what its charter already empowers** — edit, test, lint, open and merge its own green PRs, converge its own box, file and close its own issues, run its own ops. The only stops are the handful of gates explicitly reserved to the founder (a release/publish, a credential rotation, a cross-repo dispatch, a new-repo or scope decision); everything else inside the repo is the captain's to do without asking. *Withholding routine in-repo action to seek permission already held is itself the failure mode this rule forecloses* — an idle captain waiting on a yes it already has costs the fleet as much as a stalled one. Ecosystem-wide rules change at the capital (a PR to `constitution.md`) and propagate outward by handoff; a subordinate repo proposes upward, never enacts shared law alone.

### How a handoff is delivered: label vs. paste

**The capital coordinates cross-repo work; captains report, they don't dispatch peer-to-peer.** Initiating a handoff onto another repository — filing the labeled issue that wakes its agent — is the **capital's** role, because only the capital holds the whole-fleet view needed to decide ownership, sequencing, and whether a finding recurs across repos. If you are a captain (any non-capital repository) and you find work that belongs to a sibling, you **surface it to the capital** — file it as an issue on the core `basecradle` repository, exactly as a security finding escalates — and let the capital route it; you do not file-and-label work onto a sibling yourself. *Sending work to another repo* (below) is therefore the **capital's** dispatch procedure; a captain's job is the report that feeds it.

A handoff is relayed to the target agent **two ways, depending on where that agent runs** — and picking the wrong one means the work silently never arrives. The deciding signal is **drift-proof: does the target repo have a `handoff` label?** When an agent migrates to the fleet server it is wired to the router *and* its repo gains a `handoff` label ("Router wakes this repo's agent on the issue"), so the label's presence is always an accurate, self-updating indicator — there is no per-agent list to maintain or to fall out of date. Check it before every handoff:

```bash
gh label list --repo basecradle/<target-repo> --json name --jq '.[].name'
```

- **`handoff` label present → router-wired (on-server) → LABEL, do NOT paste.** Put the `handoff` label on the issue — at creation (`gh issue create --label handoff`) or added after; it is the label's **presence** that fires, not a mandatory two-step. GitHub fires `issues.opened`/`issues.labeled` → the App webhook → the router on the fleet server, which drops to the agent's OS user and launches it with a trigger *the router itself synthesizes* (`Cross-repo handoff: work <issue-url>`, plus an input-security preamble). **An issue without the label wakes no one — the label is the trigger.** The wake-sender allow-list is narrow, by policy and by enforcement: **only the founder (`drawkkwast`) or the capital bot (`basecradle-ai[bot]`)** may apply a `handoff` label that wakes an agent — a sibling captain's label wakes no one (see *The capital coordinates cross-repo work*, above). Never hand Drawk a paste prompt for these repos; there is no human in the loop.
- **No `handoff` label → laptop agent → PASTE.** Present Drawk the one-line trigger in a copy-pasteable block; he pastes it into the running session for that repo.

The router synthesizes **only the trigger line**, so a handoff that genuinely needs private context (see *Sending work*, step 2) cannot ride a label auto-wake — in that rare case, relay it by paste even for an on-server repo, so the private block reaches the agent.

### Sending work to another repo

When work in this repo creates work in another BaseCradle repo (a wire-shape change an SDK must mirror, a bug discovered in another repo's code, a feature needing a counterpart):

1. **File the issue(s) on the target repo — the issue carries EVERYTHING.** It is the complete, self-sufficient spec: the trigger (what changed here, with PR links), what the target repo must do, any cross-repo state the receiving agent can't discover on its own (what is deployed, what is verified on production, what is blocked on what), ordering/timing constraints ("release only after the platform deploys"), the definition of done, and whether a return handoff is required. Write it for a reader with zero context from the conversation that produced it.
2. **Relay the trigger by the target repo's mechanism (see *How a handoff is delivered* above) — the trigger, and nothing else unless it's private.** Either **apply the `handoff` label** (router-wired repos — no paste, the router synthesizes the trigger) or **present Drawk the one-line paste prompt** (laptop repos), immediately after filing. The trigger is just `Cross-repo handoff: work <issue URL>` (multiple issues → list each URL); the receiving agent recognizes a handoff by this line, and the router synthesizes exactly this line for label-delivered handoffs. Add content **only** when the work depends on information that cannot be posted in the public issue — a private platform detail, a credential, an embargoed change — under an explicit `Private context (not in the public issue):` heading; because private context cannot ride a label auto-wake, a handoff that needs it is relayed by paste even to an on-server repo. **If there is no such information, the handoff is one line.** The decision rule is a single question: *could this go in the public issue?* If yes, it goes in the issue (step 1), never the prompt. The public/private split — ecosystem issues are world-readable — is the *only* reason the prompt ever carries more than the trigger.
3. **The issue is the spec; the prompt is the pointer.** Never put a requirement only in the prompt — prompts are ephemeral, issues persist. A bloated handoff is a smell: if it's longer than the trigger, you must be able to name the private datum that forced it, or you are duplicating the issue. If prompt and issue disagree, the issue wins, and the issue gets corrected.

### Receiving work from another repo

When you receive a trigger beginning `Cross-repo handoff:` — pasted by Drawk (laptop repos), or synthesized by the router on the fleet server when a `handoff` label is applied to an issue on your repo (router-wired repos) — the delivery path does not change what you do:

1. Read the referenced issue(s) in full before acting — the issue is the spec.
2. Execute under **this** repo's conventions (its own CLAUDE.md, workflow, tests). The sending repo's conventions do not transfer.
3. Respect the issue's ordering constraints (e.g., verify a dependency has deployed before releasing).
4. When done, **post the completion report as a comment on the originating issue** — what shipped, version numbers, links. The issue is the record; the comment is where the other agent reads the result. Then **verify your own work against the live system** — the check the definition of done implies (a byte-match against the source, a green deploy, a passing endpoint), not merely a green CI — and **close the handoff issue yourself, by hand.** You are the captain of this work and you answer for it, so the closed issue plus your completion comment *is* the signal: for a routine handoff the originating repo does **not** re-verify or sign off, and you do **not** leave the issue open waiting on it (that only strands it in a done-but-open limbo). Leave it open **only** when the issue's definition of done *explicitly names someone else* as the closer. Send a return-trigger handoff (per "Sending work to another repo") **only if** the other agent is blocked waiting on this work. **Never auto-close a handoff issue with `Closes #N` in a PR** — auto-close fires on merge, before you have verified the work live, and a handoff issue that closes early lies to anyone watching it. Close it by hand, only after you have met *and verified* the definition of done. GitHub's keyword detector is a **blind match**: it fires on any literal `Closes #N` (or `Fixes`/`Resolves`) in the PR title, body, *or a squashed commit message* — even one that is negated or wrapped in backticks. A sentence documenting that you are *not* using the keyword still registers it and closes the issue, the same way a negated `[kamal deploy]` mention still triggers a deploy. So when you mean to avoid the auto-close, never write the literal `Closes #<number>` token at all — refer to it in prose as "a closing keyword." (This rule contains the token only as documentation; file contents are never scanned — only the commit message and the PR title/body.)

### Propagating this procedure

Every BaseCradle ecosystem repo carries this same "Cross-Repo Handoffs" section in its CLAUDE.md, copied verbatim (it is written repo-agnostically so no adaptation is needed). When handing off to a repo whose CLAUDE.md lacks the section — always true for a brand-new repo — the handoff prompt's definition of done includes adding it, copied from the capital's `CLAUDE.md` fetched from GitHub (`basecradle/basecradle` → `CLAUDE.md`, with fleet credentials) — the same mechanism public repos use to reference `constitution.md`; never a machine-local path.

**A change to any verbatim-shared block is not done until it is propagated — and propagation is enforced, not trusted to memory.** Three blocks are carried verbatim fleet-wide: **Cross-Repo Handoffs**, **Polling GitHub**, and **Attended-Session Lifecycle Signal**. The instant the capital edits one and the children are not re-synced, the fleet's shared law has silently diverged. So editing a shared block in the capital's CLAUDE.md is a single change-set with two obligations: land the capital edit **and** file the N child re-sync handoffs (one per repo that carries the block) in the same breath. A shared-block PR with no accompanying re-sync handoffs is an *unfinished* PR. Because discipline alone is what failed before — the #363 router-daemon bullet sat un-propagated to all five children until a manual audit caught it — **the NOC runs a standing drift-guard** that byte-diffs every shared block across every repo against the capital canonical on a cadence and raises a loud alert + an auto-filed `[SECURITY]`-style `[DRIFT]` issue the instant any block diverges. A missed propagation surfaces within hours, never twenty commits later. The guard is the backstop; filing the re-syncs in the same change-set is the primary obligation. To audit on demand, byte-diff each repo's three shared blocks against this file (`gh api repos/basecradle/<repo>/contents/CLAUDE.md` → compare the block between its `## ` header and the next).

## Development Commands

```bash
uv sync                  # install everything (creates .venv)
uv run pytest            # tests (offline — the default)
uv run pytest -m live    # the spec drift-guard (one network call to the live spec)
uv run ruff check .      # lint
uv run ruff format .     # format
uv build                 # build the wheel + sdist
```
