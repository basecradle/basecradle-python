---
name: bot-auth-setup
description: Per-session operational setup for acting on GitHub as the basecradle-python-ai[bot] identity — set the local git author, mint a short-lived installation token, and route gh/git through it (laptop helper vs. fleet-server GH_APP_* minting). Use at the start of any session that will push commits, open PRs, or post issue/PR comments as the bot. The identity facts (slug, App ID, bot user ID, commit-author, no-Co-Authored-By rule, CI-uses-no-secrets) live in CLAUDE.md → Fleet Bot Identity; this skill carries the setup steps.
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

## 2. Auth routing — mint a short-lived installation token, route gh/git through it

**On the laptop**, use the shared fleet helper:

```bash
export GH_TOKEN="$(~/Documents/claude-workspace/2026-06-05-fleet-identity/gh-app-token basecradle-python-ai)"
# push via:  https://x-access-token:<token>@github.com/basecradle/basecradle-python.git
```

The helper (`gh-app-token`) and registry (`fleet-apps.json`) live in the Claude workspace on the laptop; `--author` prints the commit-author string, `--remote` the authenticated push URL.

**On the fleet server** there is no shared helper. Each agent's own provisioned credentials — the `GH_APP_*` env vars in its environment (`GH_APP_ID`, `GH_APP_PEM_B64`, `GH_APP_SLUG`, `GH_APP_BOT_USER_ID`) — serve this role, and the agent mints its own installation token from them: base64-decode the PEM, build an RS256 JWT (`iss` = App ID), `GET /app/installations` → take `[0].id`, `POST /app/installations/{id}/access_tokens` → the `ghs_` token. See the "Mint GH token on fleet server" memory for the exact steps.
