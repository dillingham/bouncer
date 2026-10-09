# Bouncer

Contributor-paid pull request reviews for GitHub. An outside pull request doesn't get a maintainer's attention until the contributor runs a thorough AI review of it **in their own fork, on their own API key**, against the maintainers' rules. The maintainer pays nothing, and so do you: there is no server. Everything runs in GitHub Actions.

## How it works

1. **Someone outside the team opens a PR.** The gate (in the maintainer's repo) labels it `bouncer:pending`, converts it to a draft, and comments with instructions and a deadline.
2. **The contributor runs "Bouncer review" from their fork's Actions tab**, with their `ANTHROPIC_API_KEY` secret. The workflow:
   - reads `.bouncer.yml` from the upstream base branch (model, effort, rules: all maintainer-controlled),
   - checks out the base branch and the PR head **read-only** (PR code is never executed),
   - runs an agent that reads the touched files in full, greps for callers and APIs, checks existing tests, searches past issues and PRs for duplicates and declines, and evaluates every rule with file/line evidence,
   - verifies every piece of evidence against the actual files (quotes that don't match are discarded),
   - signs the report with a GitHub artifact attestation.
3. **The gate verifies the signature** (on `/bouncer check` or every 30 minutes) and applies the verdict:
   - **pass**: `bouncer:pass`, marked ready for review, report posted for the maintainer.
   - **fail**: report posted with reasons, `bouncer:fail`, PR closed. The contributor can push fixes and reopen for another round, up to `max_attempts`.
   - **no review by the deadline**: closed.

## Why the contributor can't fake a pass

| Attempt | What stops it |
|---|---|
| Edit the review workflow in their fork | The gate only accepts attestations signed by `dillingham/bouncer/.github/workflows/review.yml`, so a modified workflow signs with the wrong identity. |
| Run it on their own machine or a self-hosted runner | Verified with `--deny-self-hosted-runners`. |
| Pick a cheap model, lower effort, or soften the rules | Model, effort and rules come from the upstream `.bouncer.yml`; the workflow has no inputs for them. |
| Point the API at a fake endpoint | The base URL is hardcoded. |
| Re-run until the model says yes | Every run for the same PR commit attests the same subject. The gate lists all of them and only honors the earliest. |
| Push commits to reroll | Each new commit is a new round that costs their tokens again, and bounced rounds are capped. |
| Reuse a pass from another PR or commit | The repo, PR number and head commit are inside the signed payload and checked. |
| Prompt-inject the reviewer through the PR | PR text is fenced as untrusted data; attempts are flagged and fail the PR; a model "fail" also needs verified evidence and the final call is made in code, not by the model. |
| Forge a bouncer state comment | Only comments by `github-actions[bot]` are read. |

## Add it to a project (maintainers)

1. Copy `templates/.github/workflows/bouncer-gate.yml` and `bouncer-review.yml` into the project's `.github/workflows/`, and `templates/.bouncer.yml` to the repo root. Edit the rules and `guidance` to match the project.
2. Merge to the default branch. New forks inherit `bouncer-review.yml`; it only runs in forks.
3. Optional: add `guidance` about scope and the things you never accept. That text is the strongest lever on verdict quality.

Members, collaborators, prior contributors (configurable), listed bots, and any PR labeled `bouncer:skip` are exempt. Reopening a bounced PR yourself overrides the verdict.

## Contributor experience

The gate's comment walks them through it: enable Actions in the fork, add `ANTHROPIC_API_KEY`, run **Bouncer review** with the PR number, then comment `/bouncer check`. The key stays a secret in their own fork. The report shows how many tokens their review used.

## Before trusting it on a busy repo

These need a live run to confirm; none can be exercised offline:

- **Reading the fork's attestations with the upstream token.** The gate fetches attestations from the contributor's public fork with the upstream repo's `GITHUB_TOKEN`. Public repos should allow this; test it with a throwaway fork first.
- **Draft conversion.** GitHub may refuse `convertPullRequestToDraft` for the Actions token. It's best-effort; the `bouncer:pending` label is the real gate. Filter your PR list with `-label:bouncer:pending`.
- **Deleted attestations.** If a fork owner can delete an attestation from their repo, earliest-wins weakens. Entries still exist in the public Sigstore transparency log; checking it is a follow-up.
- **Notifications** still fire when a PR opens; the gate controls what reaches review, not the inbox.
- **Scheduled sweeps** pause after 60 days without repo activity (GitHub policy). `/bouncer check` still works.
- **Anthropic only** for now. Other providers would be another client in `bouncer/agent.py`.

## Layout

```
.github/workflows/review.yml   reusable: the review, run from contributor forks
.github/workflows/gate.yml     reusable: the gate, run in the maintainer's repo
bouncer/                       Python: config, facts, agent, decide, gate, render
templates/                     what maintainers copy into their project
tests/                         pytest suite (fakes for GitHub and Anthropic; real gh verify output)
```

Run the tests: `pip install -r requirements.txt pytest && python -m pytest -q`.
