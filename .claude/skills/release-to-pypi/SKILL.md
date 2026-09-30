---
name: release-to-pypi
description: Step-by-step procedure to cut a basecradle PyPI release — bump version + changelog, tag, verify the TestPyPI rehearsal, actuate the pypi env-gate, verify the live install, close the release issue, and post-release version bump. Use when preparing or running a release, cutting a tag, or verifying a published version. The invariants (captain's job ends at the version bump; the capital owns the publish actuation; the contractual workflow/environment names) live in CLAUDE.md → Releasing; this skill carries the ordered procedure.
---

# Releasing basecradle to PyPI

The invariants — the captain's release responsibility ends at the version bump + changelog, the **capital** owns the publish from the tag onward (`constitution.md` → Earned Autonomy → "Publishing is the capital's, not the founder's"), and the contractual names (`.github/workflows/release.yml`, environments `testpypi` and `pypi`) — live in `CLAUDE.md` → "Releasing" and govern at all times. This skill is the ordered procedure behind them.

The pipeline (`.github/workflows/release.yml`): pushing a `v*` tag → build → TestPyPI rehearsal → `pypi` env-gate → PyPI, all via OIDC Trusted Publishing (zero stored credentials). The `pypi` gate is **not** a human gate — the capital approves it via its operator credential (the reviewer identity named on the gate is the *credential the capital operates* via local `gh`, not the founder's action). The gate is a training wheel to retire toward bot-native auto-publish as the captain matures.

**Captain vs. capital.** The captain does **step 1 only**, and **step 1 is not finished until the handoff is posted** (step 1b). From the tag onward (steps 2–6) the **capital** owns the publish: it tags, the pipeline runs, the capital approves the `pypi` env-gate, then verifies the live install and closes the release issue. A release is done not at PyPI but when the live `pip install` is verified — and that whole tail belongs to the capital, not the founder. **Step 7 is the captain's again**, and belongs to no release's tail: it is an ordinary in-repo PR opened in the *next* cycle, which only the captain can make (the capital never reaches into this repo — `CLAUDE.md` → repo sovereignty).

## The procedure, in order

1. **Release PR** (the captain's part): bump `src/basecradle/_version.py` from `X.Y.Z.dev0` to `X.Y.Z`, and make **three** edits to `CHANGELOG.md` — all three are CI-enforced since #229, because they are hand-typed copies of the same version and a mismatch would announce a release that was never built:

   - rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, in **exactly** that shape (`tests/test_version.py` reads it literally: four-digit year, zero-padded month and day, a plain hyphen — not an en dash),
   - leave a fresh `## [Unreleased]` section above it, or the next cycle's changes have nowhere to go (after 0.11.0 it was renamed away and seven PRs merged into the gap),
   - add the link definition at the bottom: `[X.Y.Z]: https://github.com/basecradle/basecradle-python/releases/tag/vX.Y.Z` — the label and the tag in the URL must be the same version.

   Merge on green CI. Do **not** put a closing keyword (`Closes #N`) on release PRs — see step 6.
1b. **Hand off to the capital — the step that ends the captain's turn.** The merge wakes nobody, and steps 2–6 are not yours, so a release turn that ends at the merge leaves the publish with no owner (`CLAUDE.md` → Conventions, "arm auto-merge — never end a turn parked on CI"). Post a comment on the release issue naming the version, the merge commit, and that `_version.py` now reads `X.Y.Z`, and apply **`needs-capital`** — the capital's inbox is the org-wide `needs-capital` query, and the label is what puts the ball in its court. Leave the issue **open**: step 6 is the capital's close. That comment is the last thing the captain owes a release.
2. **Tag**: on main after the merge — **never type the tag. Derive it from the version the tree builds, so the tag cannot name a version other than the one that gets published** (#214, #216). **Cut it before anything else merges:** since #218 the tag must sit on main's tip, and step 7's `.dev0` bump moves the tip past the release commit — after which the shape check below and #218's guard exclude each other and nothing is taggable until a fresh release commit lands. Tag first, bump after. Run it from the repo root, as one block:

   ```bash
   (
     set -euo pipefail

     git switch main
     git pull --ff-only
     [ -z "$(git status --porcelain)" ] || { echo "tree is not clean — refusing to tag" >&2; exit 1; }

     out="$(mktemp -d)"
     trap 'rm -rf "$out"' EXIT
     uv build --wheel --out-dir "$out"
     version="$(basename "$out"/*.whl | cut -d- -f2)"

     if [[ ! $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
       echo "this tree builds '$version', not a plain X.Y.Z release — refusing to tag." >&2
       echo "a .dev0 means main's tip is not the release commit: either step 1 has not" >&2
       echo "merged yet, or step 7 already bumped past it. Since #218 the tag must BE" >&2
       echo "main's tip, so land a fresh release commit rather than tagging history." >&2
       exit 1
     fi

     git tag "v$version"
     git push origin "v$version"
   )
   ```

   **Why the wheel's filename and not `_version.py`:** `basename …*.whl | cut -d- -f2` is the *same expression* `release.yml`'s guard applies to the wheel it is about to publish (#214), so the tag and its backstop read one artifact one way. Neither candidate #216 named fits: `uv version --short` refuses a `dynamic = ["version"]` project, and `hatch` (the CLI) is not a declared dependency here — only `hatchling`, the backend. Reading `_version.py` by hand is the third option and the trap: it means re-implementing hatchling's version regex, which is case-insensitive, accepts `VERSION`, either quote and any spacing, and **strips a leading `v`** — so `__version__ = "v0.12.0"` builds `0.12.0` while a plain `sed` tags `vv0.12.0` (measured: the two disagree on five of seven realistic version lines). It is blind to PEP 440 normalization besides, which the filename has already applied.

   Every other line closes a way the tag could still lie, and each is load-bearing:

   - **`set -euo pipefail`, and one command per line.** The `&&` chains this step used to carry *defeat* `set -e`: a failure in a `&&` list is exempt unless it is the last command, so `git switch main && git pull --ff-only` continued past a failed switch and tagged whatever `HEAD` was, and `git tag … && git push …` exited **0** when the tag already existed — pushing nothing and reporting success. Split, both halt.
   - **A `( … )` subshell, not a `bash <<'EOF'` heredoc.** The block is indented inside this list, and a heredoc terminator only closes at column 0 — pasted with its indentation it does not terminate, and bash runs the delimiter as a command and swallows the rest of the block. A subshell is indentation-proof, keeps `set -e` and the `trap` out of the caller's shell, and makes the refusals' `exit 1` exit the block rather than the operator's session. It is bash, for `[[ … =~ … ]]` and `pipefail`.
   - **`git status --porcelain`, not `git diff --quiet`.** `git diff` does not see *untracked* files, and hatchling packages untracked-but-unignored files under `src/basecradle/` — so the wheel built here would not be the wheel CI builds from the tag, which is the whole point of building it. (`dist/` is gitignored, so this does not trip on ordinary build output.)
   - **`out="$(mktemp -d)"` + `trap` + `uv build --wheel --out-dir "$out"`.** The directory is captured in a variable rather than substituted inline, because the `trap` and the glob on the next line both need the path. Building into a fresh directory makes exactly one wheel matchable, so the glob cannot pick up a stale or foreign one — `basename` given two operands treats the second as a *suffix* rather than erroring, so a second wheel in a shared `dist/` would be silently accepted. It also touches nothing the clone's owner put there (`CLAUDE.md` → "Clean only what you created"), removes only its own directory, and skips the sdist nothing here reads.
   - **An anchored `^[0-9]+\.[0-9]+\.[0-9]+$`.** A character-class test (`*[!0-9.]*`) accepts `1.2.3.4`, `0.12`, `0`, and `...`; anchoring refuses them. What it most matters for is main's steady state *between* releases, `X.Y.Z.dev0`: that is the one mismatch #214's guard **cannot** catch — tag and build agree — so it would sail through and put a dev build on PyPI under a filename that can never be replaced. This procedure tags plain releases only; this repo has never shipped a pre-release, and adding one is a capital call, not a local edit.

   Pushing the tag triggers the release workflow. Two guards in its `build` job stand between a bad tag and PyPI, and both fail **before** either publish job runs, so nothing is burned — the cost is a deleted tag:

   - **The tag must sit on main's tip** (#218) — catches the right version on the wrong commit, which the version guard cannot see, because such a tag *agrees* with the tree it builds.
   - **The tag must name the version being built** (#214) — catches a tag that disagrees with both built artifacts, the sdist included.

   Recover by deleting the tag, then re-cutting it with the block above. Both halves matter, and they are **two commands, not a `&&` chain** — a failed local delete must not skip the remote, which is the half people forget:

   ```bash
   git tag -d "vX.Y.Z"
   git push origin ":refs/tags/vX.Y.Z"
   ```
3. **Verify the rehearsal**: the TestPyPI publish is automatic. Build the clean venv as a throwaway in your own `~/scratch`, and delete it the moment the check passes (see "Verification venvs" below):
   `uv venv --seed --clear ~/scratch/verify-basecradle && source ~/scratch/verify-basecradle/bin/activate`
   Both flags earn their place: `--seed` puts `pip` *in* the venv (without it the next line runs the ambient pip, or none at all), and `--clear` replaces whatever is at the slot (without it `uv venv` refuses and leaves a stale venv from a failed run in place — which the check would then silently reuse, and "clean venv" would be a lie). Then:
   `pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ basecradle==X.Y.Z`
   The extra index is required (httpx lives on real PyPI, not TestPyPI). Expect a minute or two of index-propagation lag — retry, don't panic.
4. **The publish gate**: the workflow waits on the `pypi` environment. The capital approves it via its operator credential; the founder is out of the publish loop.
5. **Verify the release**: a fresh throwaway venv in the same slot, built the same way, `pip install basecradle==X.Y.Z`, check import + `__version__` + both clients construct, and that https://pypi.org/project/basecradle/ renders. (The PyPI JSON API caches — pip resolving the new version is the real test.)
6. **Close the release issue manually** with the verification record. Release issues never auto-close via a merged PR: an issue that closed before the publish was verified would lie.
7. **Post-release version bump** (the captain, next cycle): **not before the tag is cut** — it moves main's tip off the release commit, and #218's guard requires the tag to be main's tip. The first PR of the next cycle bumps `_version.py` to the next minor `.dev0` (after `0.2.0` ships, main becomes `0.3.0.dev0`) so dev builds are always distinguishable from releases. If the next cycle opens with no other work queued, the bump *is* that PR — don't wait for a feature to carry it, which is how it gets skipped.

## Verification venvs

Steps 3 and 5 each build a venv that is not the deliverable, so it gets a home and an end like anything else a release leaves behind (`CLAUDE.md` → "Whatever creates, cleans up"). The home is **one** fixed-name slot in the runner's own `~/scratch` — `~/scratch/verify-basecradle`, overwritten (`--clear`) every release. Deliberately no version in the path: a fresh name per run is the pattern that rule forbids, and it would let a step that died before its cleanup strand a `verify-v0.2.0` no later release ever reclaims. The end is `rm -rf ~/scratch/verify-basecradle` the moment that step's check passes — both venvs are gone before step 6 closes the release issue. The `~/scratch` sweeper (`~/.claude/CLAUDE.md` → Your Home) is the backstop, not the plan.

## Versioning facts

`_version.py` is the single source of truth (hatchling reads it; `pyproject.toml` declares `dynamic = ["version"]`). Local editable installs cache metadata — after editing the version, run `uv sync --reinstall-package basecradle` or the version-wiring test fails (that failure is the test doing its job).
