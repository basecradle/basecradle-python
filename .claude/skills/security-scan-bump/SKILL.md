---
name: security-scan-bump
description: The response procedure for when the daily dependency scan files its `[SECURITY] Dependency Advisories Against main (Daily Scan)` issue (or its `Could Not Reach a Verdict` issue) on this repository — ship a minimal `uv.lock` bump on its own branch, read the Critical and High advisories against the SDK, verify the scan is clean, close by hand. Use when that issue is open, when an advisory against a package in `uv.lock` turns up any other way, or when the scan's run is red. How the scan works lives in CLAUDE.md → "Responding to the daily dependency scan"; this skill carries the steps.
---

# Responding to the Daily Dependency Scan

`.github/workflows/dependency-security-scan.yml` runs `osv-scanner` against `uv.lock` on `main` every day (the schedule is the workflow's `cron`); the decisions are in `scripts/dependency_security_scan.py`. A finding arrives as one issue, `[SECURITY] Dependency Advisories Against main (Daily Scan)`, carrying `needs-human` when anything is rated High or Critical (it has paged `@origin`) and `needs-capital` otherwise. It is the capital's `security-scan-bump` procedure, carried for what applies to a Python SDK with no deploy.

## What the issue does on its own

- **It updates itself and never closes itself.** A new advisory is added with a comment and its label; one re-rated High or Critical after it was filed lower pages then; one that stops being reported is noted; a clean scan says so once. Close it by hand once the fix is on `main` and a scan run is clean.
- **Closing the issue dismisses nothing.** While an advisory stands, the next scan files a new issue, and pages again if it is High or Critical.
- **`Daily Dependency Security Scan Could Not Reach a Verdict`** is the other issue it can file (`needs-capital`): `osv-scanner` crashed, could not read `uv.lock`, or could not reach the OSV API (it exits 127 with an empty report, which is why the exit status is checked). Read the run log, fix it, and re-run the workflow by hand; the run that reaches a verdict closes that issue itself. Until then the clock is stopped. A run that dies before it can file anything (the App token will not mint, the `osv-scanner` download fails its checksum) is only a red run in the Actions tab, and the heartbeat pages when a day and its grace pass with no ping.

## Procedure

1. Note each advisory from the issue: package, installed version, advisory id, severity. Read the advisory for the minimum fixed version (`osv-scanner scan source --lockfile uv.lock --format json` lists the `fixed` events under each record's `affected[].ranges`).
2. `git checkout main && git pull origin main`, then `git checkout -b bump-<package>-cves`.
3. **Bump to the minimum version that resolves the advisory**, unless a larger upgrade is independently justified: `uv lock --upgrade-package <package>==<fixed version>` (repeat the flag for each package). This works for transitive packages too. A bare `--upgrade-package <package>` takes the newest release instead, which can drag in other packages: on 2026-11-07 it moved `anyio` three minors past the fix and forked `typing-extensions` into two versions (#316). **Read the lock diff line by line; every changed package is named in the PR body.**
   - **Lock with a uv at least as new as the one that last wrote `uv.lock`.** An older uv rewrites the lock's `revision` downwards, an unrelated change riding a security fix. `uvx --from uv uv lock …` runs a current uv when the system one is behind.
   - **An advisory in the SDK's own runtime dependency set that a user could still resolve** (the vulnerable version satisfies `pyproject.toml`'s `dependencies` floor) is a different fix: raise the floor, and that change ships in a release (→ `release-to-pypi`). Raise it on the issue so the capital sequences the release.
4. **Verify locally: the advisory is gone and nothing else broke.** Run the scanner from the release the workflow pins (version and checksum are in the workflow's `Install osv-scanner` step) against the branch, and against `main` as a control: `main` must still report the advisory and the branch must not (exit 0). `osv-scanner` queries the live OSV API, so there is no local database to go stale, but a clean result on the branch proves nothing until the same binary reports the advisory on `main`. Then `uv sync --locked`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.
5. **Open the bump PR.** Title: `Bump <package> <old> → <new> for <advisory-id>` (two: `for <id> and <id>`; many: `for N advisories`). The body is the advisory table (severity, package, advisory, fixed in), the changed packages, and the verification. It names the security issue as `Part of #<n>`, never with a closing keyword: the issue closes by hand after the scan is clean. Run `/code-review` on it, then arm auto-merge and confirm the arming took (CLAUDE.md → Conventions).
6. **After it merges, prove the scan clean against `main`.** Re-run the workflow (`Dependency Security Scan`, *Run workflow*, branch `main`) or wait for the next scheduled run. **Never dispatch it on the bump branch to check the fix:** a run on any branch writes to the same issue as `main`'s, so it would report the advisory resolved while `main` still carries it, and the next run on `main` would file it as new and page again. Step 4's local run is the branch check. That run comments "is clean" on the issue; then close the issue by hand with the run link.
7. **Read the Critical and High advisories against the SDK, on the security issue.** The bump is the fix either way, but which advisories had reach here decides whether there is anything else to do. Each advisory states its own preconditions; check them against how the SDK and its CI actually use the package. The lock governs this repository's CI and development environment, never a user's install, so state for each whether a user resolving `basecradle` today could still get the vulnerable version, and if so, step 3's floor raise applies.
8. **An open PR that went red on the same advisory** recovers once the bump is on `main`: bring it up to date with `gh pr update-branch <n>`. Dependabot's own PRs refuse `@dependabot rebase` from the App, so use the same command for them.

## Dismissing an advisory that does not apply

Add it to `osv-scanner.toml` at the repository root, beside `uv.lock` (`osv-scanner` reads it from the lockfile's directory, which is how the workflow finds it):

```toml
[[IgnoredVulns]]
id = "GHSA-xxxx-xxxx-xxxx"
ignoreUntil = 2027-01-31  # a date, so the dismissal is re-read rather than forgotten
reason = "Why this advisory has no reach here, and who decided."
```

Dismissing an advisory is a decision on reach, so it goes on the security issue with its reasoning before the PR that adds it. A High or Critical dismissal is the capital's call (`needs-capital`); never dismiss one on your own reading alone.
