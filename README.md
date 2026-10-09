# Bouncer action

The engine behind [`gh bouncer`](https://github.com/gh-bouncer/gh-bouncer): contributor-paid pull request reviews for GitHub. An outside pull request doesn't get a maintainer's attention until the contributor runs a thorough AI review of it **in their own fork, on their own API key**, against the maintainers' rules. Maintainers pay nothing. There is no server; everything runs in GitHub Actions.

```
gh extension install gh-bouncer/gh-bouncer
gh bouncer init          # maintainers: opens a PR that installs bouncer in your repo
gh bouncer <pr-url>      # contributors: runs the review for your pull request
```

## How it works

1. **Someone outside the team opens a PR.** The gate (in the maintainer's repo) labels it `bouncer:pending`, converts it to a draft, and comments with instructions and a deadline.
2. **The contributor runs `gh bouncer <pr-url>`.** It sets up their fork and stores their key as a fork secret the first time, then runs the review there. The review:
   - reads `.bouncer.yml` from the upstream base branch (model, effort, rules: all maintainer-controlled),
   - checks out the base branch and the PR head **read-only** (PR code is never executed),
   - runs an agent that reads the touched files in full, greps for callers and APIs, checks existing tests, searches past issues and PRs for duplicates and declines, and evaluates every rule with file/line evidence,
   - verifies every piece of evidence against the actual files (quotes that don't match are discarded),
   - signs the report with a GitHub artifact attestation, and only then shows the verdict in the run.
3. **The gate verifies the signature** (every 10 minutes, or right away on `/bouncer check`) and applies the verdict:
   - **pass**: `bouncer:pass`, marked ready for review, report posted for the maintainer.
   - **fail**: report posted with reasons, `bouncer:fail`, PR closed. The contributor can push fixes and reopen for another round, up to `max_attempts`.
   - **no review by the deadline**: closed.

## Why the contributor can't fake a pass

| Attempt | What stops it |
|---|---|
| Edit the review workflow in their fork | The gate only accepts attestations signed by `gh-bouncer/action/.github/workflows/review.yml`, so a modified workflow signs with the wrong identity. |
| Run it on their own machine or a self-hosted runner | Verified with `--deny-self-hosted-runners`. |
| Pick a cheap model, lower effort, or soften the rules | Model, effort and rules come from the upstream `.bouncer.yml`; the workflow has no inputs for them. |
| Point the API at a fake endpoint | The base URL is hardcoded. |
| Re-run until the model says yes | Every run for the same PR commit attests the same subject. The gate lists all of them and only honors the earliest. |
| Cancel runs heading for a bounce before they're signed | Nothing in the run (logs, summary, outputs) shows the verdict until the attestation exists. |
| Push commits to reroll | Each new commit is a new round that costs their tokens again, and bounced rounds are capped. |
| Reuse a pass from another PR or commit | The repo, PR number and head commit are inside the signed payload and checked. |
| Prompt-inject the reviewer through the PR | PR text is fenced as untrusted data; attempts are flagged and fail the PR; a model "fail" also needs verified evidence and the final call is made in code, not by the model. |
| Forge a bouncer state comment | Only comments by `github-actions[bot]` are read. |

## Add it to a project (maintainers)

Run `gh bouncer init` in the project (or `gh bouncer init -R owner/repo`). It opens a pull request that adds:

- `.github/workflows/bouncer.yml`: one workflow with two jobs. In your repo the **gate** job runs `uses: gh-bouncer/action@v1`. In forks, which inherit the file, the **review** job runs the signed review on the contributor's key.
- `.bouncer.yml`: model, effort, deadlines and rules. Edit the rules and `guidance` to match the project before merging; `guidance` (scope, things you never accept) is the strongest lever on verdict quality.

Members, collaborators, prior contributors (configurable), listed bots, and any PR labeled `bouncer:skip` are exempt. Reopening a bounced PR yourself overrides the verdict.

Prefer to do it by hand? Copy `templates/bouncer.yml` to `.github/workflows/bouncer.yml` and `templates/.bouncer.yml` to the repo root.

## Contributor experience

The gate's comment has one instruction: install the extension and run `gh bouncer <pr-url>`. It turns on the review in their fork, stores their key as a fork secret (asking the first time), runs the review and reports back on the PR. After that, every push to the PR branch is reviewed automatically; pushes with no open PR, or forks without a key, exit quietly. The key never leaves their fork's secrets, and the report shows how many tokens their review used.

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
action.yml                     the gate, used as gh-bouncer/action@v1 in the maintainer's repo
.github/workflows/review.yml   reusable: the signed review, run from contributor forks
bouncer/                       Python: config, facts, agent, decide, gate, render
templates/                     what gh bouncer init installs
tests/                         pytest suite (fakes for GitHub and Anthropic; real gh verify output)
```

Run the tests: `pip install -r requirements.txt pytest && python -m pytest -q`.
