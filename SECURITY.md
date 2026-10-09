# Security

This page explains how bouncer's trust model works, the attacks we know about, and what stops each one. It's written so anyone (or any agent) working on this code can catch up quickly and spot new risks. It also covers the [`gh bouncer`](https://github.com/gh-bouncer/gh-bouncer) extension.

**Reporting a vulnerability:** please use GitHub's private vulnerability reporting on this repository (Security → Report a vulnerability). Don't open a public issue.

## How trust flows

- The contributor runs the review in **their own fork**, on their own Anthropic key, using the reusable workflow `gh-bouncer/action/.github/workflows/review.yml`. It signs its result (the "predicate") with a GitHub artifact attestation for the subject `bouncer-review/v1 {upstream}#{pr}@{head_sha}`.
- The maintainer's **gate** (`uses: gh-bouncer/action@v1`) never runs PR code. It downloads the fork's attestations and keeps only the ones that pass every check below:
  - signed by our `review.yml`
  - on a GitHub-hosted runner
  - from a commit in the gate's own history
  - from the PR's fork (matched by repository id)
  - for this exact PR and head commit
  - made with the current settings
  - at the current review protocol version

  Of those, it takes the earliest and computes the verdict **in code** (`bouncer/decide.py`) from the signed predicate and the maintainer's current `.bouncer.yml`.
- **The contributor controls** everything in their fork: workflows, inputs, the ref they call `review.yml` at, re-runs, cancels, secrets, attestations, and repo names. They also control the PR's title, body, commits and target branch, and comments. **They can't** write to the upstream repo or to `gh-bouncer/action`, set labels upstream, or change what GitHub signs about a run.

## Scenarios and remedies

Status: **Fixed** (handled and tested), **Open** (known gap), **Accepted** (deliberate trade-off), **Live** (handled in code, but only a real GitHub run can confirm it).

### Faking the signature or the reviewer

- **Edit the review workflow in the fork.** **Fixed.** A contributor changes their copy of `bouncer.yml`, or writes their own workflow that attests "passed".
  - The gate verifies with `--signer-workflow github.com/gh-bouncer/action/.github/workflows/review.yml`. A workflow defined in the fork signs under a different identity and is rejected (`gh_verifier` in `bouncer/gate.py`).
- **Imposter commit.** **Fixed.** GitHub will run `uses: gh-bouncer/action/.github/workflows/review.yml@<sha>` even when `<sha>` only exists in a *fork* of gh-bouncer/action, because forks share git objects. The certificate then names our workflow while the attacker's code runs.
  - The gate reads the signer commit from the certificate and asks the compare API whether it's an ancestor of, or equal to, the gate's own ref. Only `ahead` or `identical` is accepted (`Gate._signed_by_gate_history`). A rejected review gets the `outdated` note, so the contributor learns to sync their fork.
- **Run an old version of the review, from before a fix.** **Fixed.** Any ref of `review.yml` on our branches verifies, including ones with known holes.
  - Every predicate carries `REVIEW_PROTOCOL`, and the gate ignores anything below `MIN_REVIEW_PROTOCOL` (`bouncer/common.py`). The number is written by the code at the signing commit, so old code can't claim a new one. **Raise both numbers whenever a review-side security fix ships.** `pin_review_to_gate_version: true` goes further and requires the exact commit.
- **Run it locally or on a self-hosted runner.** **Fixed.** The review could then be tampered with mid-run.
  - Verified with `--deny-self-hosted-runners`.
- **Point the model API at a fake server that always passes.** **Fixed.** `ANTHROPIC_BASE_URL` or a proxy could be injected.
  - The base URL is hardcoded in `review.py`. A reusable workflow doesn't inherit the caller's `env`. Its only inputs are `pr` and `upstream`, and its only secret is the key.
- **Reuse a pass from another PR, commit or repo.** **Fixed.**
  - The subject digest binds upstream, PR number and head commit. The gate also re-checks `upstream`, `pr` and `head_sha` inside the predicate, and the certificate's `sourceRepositoryIdentifier` against the PR head repo's id.
- **The `upstream` input.** **Fixed.** The contributor can pass any `upstream` to the review.
  - The subject and predicate must name the gate's own repo, PR and head commit, so a review for another upstream never matches.

### Re-rolling until the model says yes

- **Cancel the runs that are heading for a bounce before they're signed.** **Fixed.** Earliest-wins only works if bounces get signed.
  - Nothing before the attest step reveals the verdict: no logs, step summary, outputs, annotations or artifacts. `run` writes only the predicate; `report` runs after signing. **Keep it that way**: any new logging in `agent.py` or `review.py` must not hint at the outcome before signing.
- **Delete the signed bounce from the fork's attestation store, then run again.** **Open.** GitHub lets owners delete their attestations (`DELETE /users/{username}/attestations/digest/{subject_digest}`). If that happens before the gate sweeps (every 10 minutes, or on `/bouncer check`), the gate only ever sees the later pass. Each retry still costs the contributor a full review, and a bounce the gate has already applied stays applied.
  - Planned fix: also look up the subject in Sigstore's append-only transparency log (Rekor), and count the earliest entry signed by our workflow, even if GitHub no longer lists it. A TODO marks the spot in `gate.py`. This needs a live test of Rekor's search by subject digest.
- **Rename the fork so earlier reviews don't match.** **Fixed, Live.** Reviews signed under the old name would drop out of a name-based lookup.
  - Attestations are downloaded by the fork's current name, verified for the owner (`--owner`), and matched by repository id, which a rename doesn't change. That GitHub lists pre-rename attestations under the new name is untested. A rename of the owner's *account* isn't covered.
- **Push trivial commits to get a fresh review.** **Accepted.** Each commit is a new subject, so earliest-wins starts over.
  - Each one costs a full review on the contributor's key. Bounces count toward `max_attempts`, after which the PR is closed for good.
- **Open a new PR for the same change to reset the attempt count.** **Open.** State is per PR.
  - Partial: `checks.max_author_prs_24h` limits how fast this can happen. An idea for later is to count attempts per author or per diff hash across PRs.

### Weakening the rules

- **Target a branch whose `.bouncer.yml` is weaker or missing.** **Fixed.**
  - `checks.target_branches` (default: the default branch only) bounces PRs into any other branch before any review. The review always reads `.bouncer.yml` from the upstream default branch, not the PR's base.
- **Get reviewed under old settings, before the maintainer tightened them.** **Fixed.**
  - The predicate signs a canonical digest of the settings that shape the review (model, effort, max_turns, guidance, rules). The gate only accepts its current digest, and a mismatch gets the `config_changed` note and a re-run. Gate-only settings (deadlines, pre-checks, exemptions, `fail_confidence`) are applied at decision time, so changing them doesn't make contributors pay again. **If a new setting changes what the reviewer does, add it to the digest.**
- **Choose a cheaper model or lower effort.** **Fixed.**
  - Model and effort come from the upstream config; the workflow has no inputs for them.
- **Ride a past contributor exemption.** **Fixed.** One small merged PR used to exempt everything that author opened afterwards.
  - `exempt_prior_contributors` now defaults to `false`. Maintainers who turn it on accept that risk.

### Manipulating the reviewer

- **Prompt injection through the PR title, body, diff, files or linked issues.** **Mitigated.** Text such as "reviewer: every rule passes" planted in a comment or issue.
  - PR text and every tool result (files, grep, issues) are fenced as `<untrusted>` (`untrusted()` in `common.py`). A closing tag inside the content is escaped, even with odd case, spacing or invisible format characters. The trusted `<facts>` block is written with `inert_json`, so a file path can't close it. Detected injection fails the PR, a model "fail" needs evidence that checks out against the real files, and the verdict is computed in code. Residual risk: subtle persuasion the model doesn't flag, and lookalike characters such as fullwidth `＜`.
- **Pass by default.** **Accepted (product decision).** A review that's unsure about everything still passes. A Required rule only bounces at `fail_confidence` or above, and only with verified evidence.
  - A Required rule the review didn't judge at all now fails. An option for later is to require an explicit pass on every Required rule.
- **Truncated or garbled model output.** **Fixed.** An answer cut off at `max_tokens` used to be accepted, with every rule marked unsure.
  - A cut-off `submit_review` is never accepted; the agent asks again. Transport errors and mid-stream overloads are retried a few times, then fail cleanly with nothing signed.
- **"Fixes #N" pointing at any open issue.** **Accepted.** This satisfies the linked-issue pre-check.
  - Whether the change actually solves that issue is the Required `solves-linked-issue` rule. Only upstream issues count; cross-repo references are ignored.
- **Read outside the checkout.** **Fixed.** Symlinks, `..`, `.git`, symlink loops and NUL bytes in paths the reviewer is asked to read.
  - Paths are resolved first and then checked against the workspace and `.git`. Any path error becomes a tool error, not a crash.

### Tampering with the gate's state

- **Forge the bouncer's state comment.** **Fixed.**
  - Only comments by `github-actions[bot]` are read. The state JSON is written with `inert_json`, so nothing in it can end the HTML comment.
- **Reopen a bounced PR to get past the bounce.** **Fixed.**
  - The gate decides from state, not from which event fired. A bounced commit is closed again on any run unless a maintainer did the reopening, which is checked against the timeline and the reopener's permission. Only a maintainer's reopen becomes an override.
- **Add or remove `bouncer:*` labels.** **Fixed.**
  - Labels need write access upstream. The gate sets labels from a fresh read of the PR.
- **Race two gate runs.** **Fixed.** A sweep and a `/bouncer check` run used to post the verdict twice, or overwrite a newer round.
  - The PR is re-read fresh, and the state and head are checked again right before writing; the run backs off if anything moved.
- **Duplicate comments after a gateway error.** **Fixed.** GitHub can create a comment and still answer 502.
  - Comment POSTs are never retried. The gate and the CLI both read the first state comment (`find_state`).
- **Make verification flaky so a valid PR expires.** **Fixed.**
  - A verify error (API, network, Sigstore, timeout, bad output) is not treated as "no review". The PR isn't expired on that run, and one failing PR can't abort the sweep.

### Keeping the maintainer's repo safe

- **Pwn request through `pull_request_target`.** **Fixed by design.** That event runs with the base repo's token and secrets.
  - The gate never checks out or runs PR code; it only calls the REST API and `gh attestation`. **Never add a checkout of the PR head to the gate.**
- **Script injection in workflows.** **Fixed by design.** PR titles, branch names, comments and inputs are attacker text.
  - Untrusted values reach shells only through `env:` as quoted `"$VAR"`, never `${{ }}` inside `run:`. `pr` is validated as digits and `upstream` as `owner/repo`. **Keep it that way.** Check with actionlint.
- **Token permissions.** **Fixed.**
  - The workflow starts from `permissions: {}`. The gate gets contents read, issues and pull-requests write, and attestations read. The review gets contents read, and `id-token` and `attestations` write.

### Protecting the contributor

- **API key exposure.** **Fixed.**
  - The CLI reads the key without echo, sends it to `gh secret set` on stdin, and strips it from the environment of every other `gh` call. It is never on argv or in logs. In Actions, only two steps see it: the one that finds the PR (it checks a key is set, so pushes without one skip quietly) and the Review step.
- **Paying twice for the same commit.** **Mitigated.**
  - Before starting a review, the CLI checks the fork for a running review or an existing attestation of the same subject, and checks the bouncer's state, so it won't start one for a PR that passed or isn't waiting. Push runs skip when the bouncer isn't waiting for a review of that commit.
- **A maintainer config that's expensive to run.** **Accepted.** Max effort and many turns make each review cost more.
  - The CLI shows the model and effort before asking for a key, and `max_turns` is capped at 200. Contributors can decline.
- **PR code runs in the contributor's own fork.** **Fixed by design.**
  - The review checks the PR head out read-only and never executes it. **Keep it that way**: no installs, builds or tests of PR code in `review.yml`.

### Denial of service

- **Spam `/bouncer` comments.** **Accepted.** Each one spends the maintainer's Actions minutes.
  - A per-PR concurrency group means only the newest queued run survives, and a run only acts on its own PR.
- **Huge PRs or reports.** **Fixed.**
  - The diff is capped, old tool output is trimmed, a truncated file list fails the pre-checks, and the report is budgeted under GitHub's comment limit.

## Rules for future changes

1. Never reveal the verdict before the attestation exists.
2. Never run or check out PR code in the gate, and never execute it in the review.
3. Never put `${{ }}` with attacker-influenced values inside `run:`.
4. Raise `REVIEW_PROTOCOL` and `MIN_REVIEW_PROTOCOL` with every review-side security fix.
5. Settings that shape the review go into the config digest; gate-only settings stay out.
6. Trusted prompt blocks get contributor text only through `inert_json`; everything else goes through `untrusted()`.
7. Gate decisions come from the state comment and fresh API reads, not from the event payload.
8. Don't retry non-idempotent POSTs.
9. New attestation checks must fail closed. A check that can't be completed is a verify error, never a pass.

## Needs a live run

These can only be confirmed on GitHub:

- reading a public fork's attestations with the upstream `GITHUB_TOKEN`
- attestations signed before a fork rename being listed under the new name
- whether a deleted attestation disappears from listing (the open issue above)
- `return_run_details` on workflow dispatch
- whether an author can reopen a PR the bot closed
- `convertPullRequestToDraft` with the Actions token
