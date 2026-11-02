---
paths:
  - "charts/**"
  - "images/**"
  - "install/**"
  - "gates/**"
  - ".github/workflows/**"
  - "scripts/gate_check.py"
  - "scripts/init_env.py"
---
# The gate runs a PR needs (ADR-039, ADR-050)

ADR-050 §4's flow, made concrete. Do this before the first push to `gate/*`, and again whenever
the diff or the PR branch changes.

1. **Runs count only under the PR branch's own task.** gate-check reads
   `gate/<level>/<task>`, where `<task>` is the PR's head branch without its first segment;
   `python3 scripts/gate_check.py --gate-branch <branch>` prints the branches. A run on the same
   commit under another task does not count. If the work moves to another branch (its PR merged
   before the last commits reached it), push the head again under the new task.
2. **Work out the gates from the whole diff,** `git diff --name-only origin/main...HEAD`,
   against the lists in `scripts/gate_check.py`. Every file counts, a comment-only change too:
   - `PLATFORM_DIRS` or a gate workflow: **dry-run** and **smoke**;
   - `install/`, `scripts/init_env.py`, or a file added to or removed from `charts/`: also
     **smoke-fresh** (ADR-050 §3). gate-check does not read it; the PR names its run under
     *Verified*, or names it under *NOT VERIFIED*.
3. **Push one head to every gate it needs, at once.** The runs go side by side. A later commit
   that changes more than `*.md` needs all of them again.
4. **Retry what did not pass.**
   - A run that failed: find the cause, fix it, push the new head to every gate again. At most
     two self-corrections, then stop and report. "Flake" is not a cause.
   - A run that was missed, or died before any test ran (checkout, install, runner loss): push
     the same head to that gate.
5. **Push the PR branch only once every gate passed,** with each run's link under *Verified*.
   A red gate-check is not the way to find out what was missing.
