---
name: merged-branch-cleanup
description: How to delete a branch after its PR squash-merges — remote and local — including the content-equivalence check that must pass before any local delete (`git branch -d` is not a containment test: it refuses shipped squash-merges and can pass on a stale upstream). Use after every merge, whenever you are about to run `git branch -D`, or when deciding whether a leftover local branch still holds unshipped work. The invariant (delete the merged branch; never force-delete without the check) lives in CLAUDE.md → Conventions; this skill carries the procedure.
---

# Deleting a merged branch

The invariants live in `CLAUDE.md` → Conventions: every merged branch is deleted, and a local branch is force-deleted only after the check below has passed. This skill is the procedure.

## Remote

This repository has GitHub's "Automatically delete head branches" setting on (`delete_branch_on_merge`), so GitHub deletes the remote branch when the PR merges. Do not rely on the setting or on any merge flag. Confirm the branch is gone:

```bash
git ls-remote --heads origin <branch>    # prints nothing once the branch is gone
```

If it is still there, delete it with `git push origin --delete <branch>`.

## Local

`git branch -d` is **not** the containment check. It tests ancestry against the branch's upstream ref, or against `HEAD` when there is no upstream. For a squash-merge it usually refuses a branch that is fully shipped, because the squash commit on `main` has a different hash from the branch's commits. It can also *succeed* on a branch `main` never got. A stale `refs/remotes/origin/<branch>` (it warns "merged to `refs/remotes/origin/<branch>`, but not yet merged to HEAD") or a stale local `main` is enough to make that happen.

So the order is:
1. Run the checks below.
2. Delete with `git branch -D <branch>`, only once one of them has passed.
3. Run `git fetch --prune` to clear the stale remote-tracking ref.

Find the squash commit from the PR, never by guessing from `main`'s tip:

```bash
git fetch origin main
SQUASH=$(gh pr view <branch-or-number> --json mergeCommit --jq .mergeCommit.oid)
```

Three checks, in order of how far `main` has moved. Compare against `origin/main`, never local `main`: the fetch updates `origin/main` only, and local `main` is usually behind on the fleet box.

1. **`main` has not moved past the branch:** `git diff origin/main..<branch>` prints nothing.
2. **`main` has moved, but not into the branch's files:** `git diff <branch> origin/main -- <files the branch touched>` prints nothing. List those files from the branch's own commits with **three** dots: `git diff --name-only origin/main...<branch>`. For `git diff`, three dots mean merge-base to branch, and two dots (`A..B`) just compare two trees. Once `main` has moved, the two-dot form mixes in every file `main` changed.
3. **A later commit on `main` touched the same files.** This is routine when a session ships several PRs against one file. Checks 1 and 2 then both show a difference, and that difference is `main` being *ahead*, not work being lost. Compare the two **change-sets** by patch ID, computed over zero-context diffs:

   ```bash
   git diff -U0 "$(git merge-base <branch> origin/main)" <branch> | git patch-id --verbatim
   git diff -U0 "$SQUASH^" "$SQUASH" | git patch-id --verbatim
   ```

   The two IDs match when the squash captured the branch exactly. Both flags are required:
   - **`-U0`** drops context lines. Without it, an edit `main` made within three lines of the branch's hunk changes one ID and not the other, so a fully shipped branch looks unshipped.
   - **`--verbatim`** keeps whitespace. `--stable` and the default mode discard it, so an unshipped indentation-only change matches, and in Python or YAML indentation changes what the code does.

   Patch IDs ignore line offsets and blob hashes. That is why this check survives a moving `main` and a plain `diff` of the two diffs does not.

**A misleading form:** diffing the branch against the squash commit directly (`git diff <branch> "$SQUASH"`) is *not* equivalent. It reports every commit `main` gained after that squash. A branch updated mid-flight also carries `main`'s commits from the moment it was updated, so both sides drift for reasons unrelated to the branch's own work.

If the IDs differ, do not delete the branch. Read the two diffs side by side and find the hunk that differs. A hunk on the branch that the squash lacks is unshipped work: ship it or account for it.
