---
description: Run tests, sync the README, commit and push
argument-hint: [what changed]
---
1. Run `venv/bin/python -m pytest tests/unit -q`. If integration tests cover files changed since the last commit and minikube is up, run those too. If anything fails, stop and show the failure. Do not commit.
2. Run `git status --short` and `git diff HEAD --stat`. Update only the README sections those changes affect, plus the requirements checklist. Keep the README's structure.
3. Run `git add -A` and commit as `<area>: <what changed>` with a 1–3 line body. Summary hint: $ARGUMENTS
4. Run `git push` (`git push -u origin HEAD` if the branch has no upstream). If there is no remote, skip and say so.
5. Report the commit hash, the pytest summary line, and whether the push succeeded.
