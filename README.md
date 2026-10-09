# Bouncer action

The engine behind [`gh bouncer`](https://github.com/gh-bouncer/gh-bouncer): contributor-paid pull request reviews for GitHub. An outside pull request doesn't get a maintainer's attention until the contributor runs a thorough AI review of it **in their own fork, on their own API key**, against the maintainers' rules. Maintainers pay nothing. There is no server; everything runs in GitHub Actions.

```
gh extension install gh-bouncer/gh-bouncer
gh bouncer init          # maintainers: opens a PR that installs bouncer in your repo
gh bouncer <pr-url>      # contributors: runs the review for your pull request
```

## How it works

1. **Someone outside the team opens a PR.** The gate (in the maintainer's repo) labels it `bouncer:pending`, converts it to a draft, and comments with instructions and a deadline. A PR into a branch outside `checks.target_branches` (default: only the default branch) is bounced right away, without a review. A draft the contributor opened waits, with no deadline, until it's marked ready for review.
2. **The contributor runs `gh bouncer <pr-url>`.** It sets up their fork and stores their key as a fork secret the first time, then runs the review there. The review:
   - reads `.bouncer.yml` from the upstream default branch, the same copy the gate uses (model, effort, rules: all maintainer-controlled),
   - checks out the base branch and the PR head **read-only** (PR code is never executed),
   - runs an agent that reads the touched files in full, greps for callers and APIs, checks existing tests, searches past issues and PRs for duplicates and declines, and evaluates every rule with file/line evidence,
   - verifies every piece of evidence against the actual files (quotes that don't match are discarded),
   - signs the report with a GitHub artifact attestation, and only then shows the verdict in the run.
3. **The gate verifies the signature** (every 10 minutes, or right away on `/bouncer check`) and applies the verdict:
   - **passed**: `bouncer:pass`, marked ready for review (if the bouncer made it a draft), report posted for the maintainer.
   - **bounced**: report posted with the reasons and how to try again, `bouncer:fail`, PR closed (or left open with `close_on_fail: false`). The contributor can push fixes and reopen for another review attempt, up to `max_attempts`.
   - **no review by the deadline**: closed, which uses an attempt. If GitHub or Sigstore can't be reached to check for a review, the PR isn't closed on that run.

## Why the contributor can't fake a pass

| Attempt | What stops it |
|---|---|
| Edit the review workflow in their fork | The gate only accepts attestations signed by `gh-bouncer/action/.github/workflows/review.yml`, so a modified workflow signs with the wrong identity. |
| Run `review.yml` from a commit that only exists in a fork of `gh-bouncer/action` (GitHub resolves those, and the signature still names `gh-bouncer/action`) | The signing commit must be in the history of the gate's own version (the ref in `uses: gh-bouncer/action@v1`), checked with the compare API. |
| Run it on their own machine or a self-hosted runner | Verified with `--deny-self-hosted-runners`. |
| Pick a cheap model, lower effort, or soften the rules | Model, effort and rules come from the upstream `.bouncer.yml`; the workflow has no inputs for them. |
| Get reviewed under older or weaker settings | The review signs a digest of the settings it used, and the gate only accepts reviews made with its current settings. |
| Run an older version of the review, from before a fix | Each review signs the protocol version of the code that ran; the gate ignores reviews below the version it requires. |
| Point the API at a fake endpoint | The base URL is hardcoded. |
| Re-run until the model says yes | Every run for the same PR commit attests the same subject. The gate lists all of them and only honors the earliest. |
| Cancel runs heading for a bounce before they're signed | Nothing in the run (logs, summary, outputs) shows the verdict until the attestation exists. |
| Push commits to reroll | Each new commit needs a new review that costs their tokens again, and review attempts are capped. |
| Reuse a pass from another PR or commit | The repo, PR number and head commit are inside the signed payload and checked. |
| Prompt-inject the reviewer through the PR | PR text, and every file and issue the reviewer reads, is fenced as untrusted data that can't close its own fence; attempts are flagged and fail the PR; a model "fail" also needs verified evidence, a Required rule the review skips counts as failed, and the final call is made in code, not by the model. |
| Forge a bouncer state comment | Only comments by `github-actions[bot]` are read, and nothing in the state JSON can end its HTML comment. |

## Add it to a project (maintainers)

Run `gh bouncer init` in the project (or `gh bouncer init -R owner/repo`). It opens a pull request that adds:

- `.github/workflows/bouncer.yml`: one workflow with two jobs. In your repo the **gate** job runs `uses: gh-bouncer/action@v1`. In forks, which inherit the file, the **review** job runs the signed review on the contributor's key.
- `.bouncer.yml`, the Repo Config: model, effort, deadlines, pre-checks and Agent Rules. Edit the rules and `guidance` to match the project before merging; `guidance` (scope, things you never accept) is the strongest lever on verdict quality. Pre-checks are checked in code before the agent runs. A Required rule (`hard: true`) can bounce a PR; an Advisory one (`hard: false`) is only reported to you. `forbidden_paths` patterns are read like `.gitignore` lines. Unknown settings show up as warnings in the gate's run.

Members, collaborators, listed bots (`exempt_users`, any case), and any PR labeled `bouncer:skip` are exempt. Prior contributors are not by default (`exempt_prior_contributors`), since one merged PR would exempt everything its author opens afterwards. Reopening a bounced PR yourself overrides the verdict, for later commits too.

Reviews only count when signed by a `review.yml` commit in the history of the gate's own version. Keep the gate (`gh-bouncer/action@v1`) and the review (`review.yml@v1`) on the same line: pinning the gate to an older commit makes reviews from newer ones not count.

Prefer to do it by hand? Copy `templates/bouncer.yml` to `.github/workflows/bouncer.yml` and `templates/.bouncer.yml` to the repo root.

## Contributor experience

The bouncer's comment has one instruction: install the extension and run `gh bouncer <pr-url>`, with the deadline and review attempts left below it, and collapsed sections on how it works and what the review checks. `gh bouncer` turns on the review in their fork, stores their key as a fork secret (asking the first time), runs the review and reports back on the PR. After that, a push to the PR branch is reviewed automatically when the bouncer is waiting for a review of it. Other pushes (the PR passed, is a draft, is out of attempts or has `bouncer:skip`), pushes with no open PR, and forks without a key exit quietly, without using the key. The key never leaves their fork's secrets, and the report shows how many tokens their review used.

A bounce report ends with the reasons and how to try again. A review that fails before it's signed (a rejected key, no credits, an API outage, the turn budget running out) ends with an error that says what to do, and doesn't use up a review attempt.

If the maintainers change the review settings (model, effort, turns, guidance or rules) after a review ran, or the review was made with an outdated version of bouncer, it doesn't count: the bouncer's comment says so and asks the contributor to run `gh bouncer` again. That doesn't use up a review attempt either.

In a fork of a fork, the review looks for the PR in the fork's parent, then in the root of the fork network; the workflow's `upstream` input names the repository directly.

## State comment

The gate keeps one comment per pull request up to date (posted by `github-actions[bot]`, the only author it trusts). It ends with the state as JSON in `<!-- bouncer:state {...} -->`; `gh bouncer` reads it too. `<`, `>` and `&` inside the JSON are escaped as `\u003c`, `\u003e` and `\u0026`. Fields tools can rely on:

| Field | Type | Meaning |
|---|---|---|
| `status` | string | `pending`, `pass`, `fail`, `expired`, `exhausted`, `override`, `wrong_base`, `draft` or `no_fork` |
| `sha` | string | the head commit the status is about |
| `left` | int | review attempts left |
| `deadline` | string | while `pending`: when the PR is closed without a review, ISO 8601 UTC (`2026-10-11T12:00:00Z`) |
| `report` | string | URL of the latest review report comment, `""` if none (a new review request starts it over) |
| `reasons` | list of strings | only when bounced (`fail`, `wrong_base`): up to 5 short reasons, like `correct: Calls http.retry(), which doesn't exist.` |
| `model`, `effort` | string | the review settings in the current `.bouncer.yml` |
| `note` | string | only while `pending`, when the latest signed review didn't count or couldn't be checked: `config_changed`, `outdated` or `verify_error` |

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
