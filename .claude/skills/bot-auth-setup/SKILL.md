---
name: bot-auth-setup
description: Per-session operational setup for acting on GitHub as the basecradle-python-ai[bot] identity — set the local git author, mint a short-lived installation token, route gh through it, and push with the token in the environment (never in a URL). Use at the start of any session that will push commits, open PRs, or post issue/PR comments as the bot. The identity facts (slug, App ID, bot user ID, commit-author, no-Co-Authored-By rule, CI-uses-no-secrets) live in CLAUDE.md → Fleet Bot Identity; this skill carries the setup steps.
---

# Bot Auth Setup — acting as `basecradle-python-ai[bot]`

The identity facts — the App slug/ID, bot user ID, commit-author string, the no-`Co-Authored-By` rule, and "CI uses no Actions secrets" — live in `CLAUDE.md` → "Fleet Bot Identity" and govern at all times. This skill is the per-session operational setup a session needs before it pushes or posts as the bot.

## 1. Git author (local, never committed)

Set this clone's `.git/config`:

```bash
git config --local user.name "basecradle-python-ai[bot]"
git config --local user.email "290976240+basecradle-python-ai[bot]@users.noreply.github.com"
```

It lives in `.git/config` only — a fresh clone starts without it, so re-run after cloning.

## 2. Mint the token and route `gh` through it

**On the fleet server** — where this agent runs — the box has its own helper on `PATH` at `/usr/local/bin/gh-app-token`. It reads this agent's provisioned `GH_APP_*` credentials (`GH_APP_ID`, `GH_APP_PEM_B64`, `GH_APP_SLUG`, `GH_APP_BOT_USER_ID`) from the environment and mints a short-lived (~1h) installation token:

```bash
export GH_TOKEN="$(gh-app-token)"      # bare invocation is the default --token mode
```

The installed helper's modes are **`--token`** (the default, so a bare call works), **`--author`** (prints the commit-author string), and **`--git-credential`** (git's credential-helper protocol — see §3). It takes a **mode flag, not a slug**: `gh-app-token basecradle-python-ai`, the laptop helper's calling convention, fails with `unknown mode`. With `GH_TOKEN` exported, `gh issue comment`, `gh pr`, etc. all go out as the bot; the token is short-lived, so re-mint if a session runs long.

**On a laptop**, the shared fleet helper takes the slug instead:

```bash
export GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-python-ai)"
```

That helper and its registry (`fleet-apps.json`) live in the Claude workspace; `--author` prints the commit-author string there too. Never wrap either minter in `2>/dev/null` — on a laptop that hides a "command not found" and lets `gh` fall through silently to the ambient `drawkkwast` login.

## 3. `git push` as the bot — the token rides the environment, never argv

**Never put the token in a URL** (`https://x-access-token:${GH_TOKEN}@github.com/…`): the shell expands it into `git`'s argv, and argv is readable by other accounts on a shared box (`/proc/<pid>/cmdline`, `ps`) for as long as the command runs (`basecradle-noc#694`, `basecradle#539`). **A token URL handed to `clone`/`pull`/`fetch` also outlives the command**: git records it verbatim in the reflog (`.git/logs`) *and* in `.git/FETCH_HEAD` — a secret at rest for the life of the clone. This clone held 16 such reflog entries between 2026-06-29 and 2026-09-03 before they were expired. (A `push` to a token URL updates no local ref, so it writes no reflog entry — it leaks through argv only.) The remote stays tokenless; git gets the token from a credential helper that reads `GH_TOKEN` out of the environment, which only the same uid can read — and reading a public repo needs no token at all. To clean an existing clone, expire the reflog **and** truncate `FETCH_HEAD`, which `reflog expire` does not touch:

```bash
git reflog expire --expire=now --expire-unreachable=now --all
: > .git/FETCH_HEAD
grep -rl x-access-token .git    # must return nothing
```

**On the fleet box** the NOC registers the minter as this agent's credential helper on every converge — in `~/.gitconfig`, scoped to `https://github.com` — so the recipe is just:

```bash
GH_TOKEN="$(gh-app-token)" git push origin <branch>
```

The helper answers only `https://github.com` and stores nothing: the token lives in the caller's environment and dies with it. If `GH_TOKEN` is unset it tells git to `quit`, so the failure is a clear message rather than a hung username prompt.

**On a laptop**, where that helper is not installed, pass one per command:

```bash
git -c 'credential.https://github.com.helper=' \
    -c 'credential.https://github.com.helper=!f() { if [ "$1" = get ]; then if [ -z "$GH_TOKEN" ]; then echo quit=1; else echo username=x-access-token; echo "password=$GH_TOKEN"; fi; fi; }; f' \
    push origin <branch>
```

Four details are load-bearing, each mirroring a guard the on-box helper enforces in code:

- **The single quotes** keep `$GH_TOKEN` literal in argv — the helper's own shell expands it from the environment.
- **The empty `…helper=` reset first** clears the inherited helper list. Without it the laptop's system `osxkeychain` helper is asked **first** (measured, git 2.55) — it can answer with the `drawkkwast` credential and, after a successful push, would **store** the bot token in the keychain.
- **Both entries are scoped to `https://github.com`**, so the token cannot reach another host. An unscoped `credential.helper` answers for *every* host git asks about — a fetch or push against any other origin gets handed the bot token (verified with `git credential fill`).
- **The `quit=1` branch** stops git when `GH_TOKEN` is unset. Without it the helper sends an empty password and the push fails with a generic GitHub auth error instead of naming the real problem.

(The `http.extraheader="AUTHORIZATION: bearer $TOKEN"` form **fails** — "invalid credentials" — for App installation tokens, and is argv besides.)
