---
name: release-to-pypi
description: Step-by-step procedure to cut a basecradle PyPI release — bump version + changelog, tag, verify the TestPyPI rehearsal, actuate the pypi env-gate, verify the live install, close the release issue, and post-release version bump. Use when preparing or running a release, cutting a tag, or verifying a published version. The invariants (captain's job ends at the version bump; the capital owns the publish actuation; the contractual workflow/environment names) live in CLAUDE.md → Releasing; this skill carries the ordered procedure.
---

# Releasing basecradle to PyPI

The invariants — the captain's release responsibility ends at the version bump + changelog, the **capital** owns the publish from the tag onward (`constitution.md` → Earned Autonomy → "Publishing is the capital's, not the founder's"), and the contractual names (`.github/workflows/release.yml`, environments `testpypi` and `pypi`) — live in `CLAUDE.md` → "Releasing" and govern at all times. This skill is the ordered procedure behind them.

The pipeline (`.github/workflows/release.yml`): pushing a `v*` tag → build → TestPyPI rehearsal → `pypi` env-gate → PyPI, all via OIDC Trusted Publishing (zero stored credentials). The `pypi` gate is **not** a human gate — the capital approves it via its operator credential (the reviewer identity named on the gate is the *credential the capital operates* via local `gh`, not the founder's action). The gate is a training wheel to retire toward bot-native auto-publish as the captain matures.

**Captain vs. capital.** The captain does **step 1 only**. From the tag onward (steps 2–7) the **capital** owns the publish: it tags, the pipeline runs, the capital approves the `pypi` env-gate, then verifies the live install and closes the release issue. A release is done not at PyPI but when the live `pip install` is verified — and that whole tail belongs to the capital, not the founder.

## The procedure, in order

1. **Release PR** (the captain's part): bump `src/basecradle/_version.py` from `X.Y.Z.dev0` to `X.Y.Z` and add the `CHANGELOG.md` entry (Keep a Changelog format). Merge on green CI. Do **not** put a closing keyword (`Closes #N`) on release PRs — see step 6.
2. **Tag**: on main after the merge — `git tag vX.Y.Z && git push origin vX.Y.Z`. This triggers the release workflow.
3. **Verify the rehearsal**: the TestPyPI publish is automatic. In a clean venv:
   `pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ basecradle==X.Y.Z`
   The extra index is required (httpx lives on real PyPI, not TestPyPI). Expect a minute or two of index-propagation lag — retry, don't panic.
4. **The publish gate**: the workflow waits on the `pypi` environment. The capital approves it via its operator credential; the founder is out of the publish loop.
5. **Verify the release**: clean venv, `pip install basecradle==X.Y.Z`, check import + `__version__` + both clients construct, and that https://pypi.org/project/basecradle/ renders. (The PyPI JSON API caches — pip resolving the new version is the real test.)
6. **Close the release issue manually** with the verification record. Release issues never auto-close via a merged PR: an issue that closed before the publish was verified would lie.
7. **Post-release version bump**: the first PR of the next cycle bumps `_version.py` to the next minor `.dev0` (after `0.2.0` ships, main becomes `0.3.0.dev0`) so dev builds are always distinguishable from releases.

## Versioning facts

`_version.py` is the single source of truth (hatchling reads it; `pyproject.toml` declares `dynamic = ["version"]`). Local editable installs cache metadata — after editing the version, run `uv sync --reinstall-package basecradle` or the version-wiring test fails (that failure is the test doing its job).
