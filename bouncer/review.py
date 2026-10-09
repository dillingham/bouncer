"""Contributor side. Runs inside the reusable review workflow, in the contributor's fork,
on the contributor's API key.

  python -m bouncer.review resolve --pr N [--upstream owner/repo]  -> GITHUB_OUTPUT: upstream, base_sha, head_sha, ...
  python -m bouncer.review run --pr N ...     -> out/predicate.json, subject outputs (no verdict)
  python -m bouncer.review report --out-dir   -> after signing: out/report.md, step summary, verdict output

`run` must not reveal the verdict anywhere the contributor can watch (logs, step summary,
outputs). Otherwise they could cancel every run heading for a bounce before the signing step
and retry until one passes, and earliest-wins would never see the bounces. `report` runs only
after the attestation exists.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from . import config as config_mod
from .agent import Agent, ReviewFailed, Workspace, stream_error_type, verify_evidence
from .common import (L_PASS, L_PENDING, L_SKIP, REVIEW_PROTOCOL, SCHEMA_VERSION, GitHub, GitHubError, subject_digest,
                     subject_name)
from .decide import decide
from .facts import gather
from .prompt import SYSTEM, build_user_content
from .render import find_state, review_markdown

MAX_DIFF = 150_000
CONFIG_COPY = "config.yml"  # the .bouncer.yml text the review used, for `report`


def _out(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in kv.items()]
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def fail(msg: str) -> None:
    """Stop the run with an error annotation (`gh bouncer` shows the first one it finds)."""
    print("::error::" + msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
    sys.exit(1)


# Said after every error before the signing step: a review that failed costs no attempt.
NOTHING_SIGNED = "Nothing was signed, so this doesn't use up a review attempt."


def review_failed_message(e: ReviewFailed, url: str, max_turns: int) -> str:
    """Why the review ended without a verdict, and what to do about it."""
    return {
        "turns": f"The review didn't reach a verdict within its turn budget ({max_turns} turns). "
                 f"Run gh bouncer {url} to try again. If it keeps happening, let the maintainers know.",
        "refusal": "The model declined to review this pull request. If you think that's a mistake, let the maintainers know.",
        "cut_off": f"The reviewer's answer was cut off at the output limit, twice. Run gh bouncer {url} to try again.",
        "context": "The review ran out of context window before reaching a verdict; this pull request may be too "
                   "large to review. Let the maintainers know.",
    }.get(e.kind, f"The review failed: {e}.") + f" {NOTHING_SIGNED}"


def api_error_message(e: Exception, url: str, model: str) -> str:
    """An Anthropic API error, after the client's own retries (and the agent's, for an answer cut
    off partway), as something the contributor can act on."""
    import anthropic
    import httpx2

    status = getattr(e, "status_code", None)
    detail = " ".join(str(getattr(e, "message", "") or e).split())[:200]
    streamed = stream_error_type(e)  # an error event that ended the answer partway
    if streamed:
        what = {"overloaded_error": "was overloaded", "api_error": "had an error"}.get(streamed, f"stopped with {streamed}")
        msg = (f"Anthropic's API {what} partway through an answer, even after asking again. Try again in a few "
               f"minutes: gh bouncer {url}")
    elif isinstance(e, httpx2.TransportError):
        msg = (f"The connection to Anthropic's API dropped partway through an answer, even after asking again. Try "
               f"again in a few minutes: gh bouncer {url}")
    elif isinstance(e, anthropic.AuthenticationError):
        msg = f"Anthropic rejected your API key (401). Save a working key with gh bouncer --set-key {url}."
    elif isinstance(e, anthropic.PermissionDeniedError):
        msg = (f"Your Anthropic API key isn't allowed to make this request (403): {detail} "
               "Check the key at https://console.anthropic.com/settings/keys.")
    elif isinstance(e, anthropic.RateLimitError):
        msg = f"Anthropic rate-limited your API key (429), even after retrying. Wait a few minutes, then run gh bouncer {url} again."
    elif isinstance(e, anthropic.BadRequestError) and "credit balance" in detail.lower():
        msg = ("Your Anthropic account is out of credits. Add credits at https://console.anthropic.com/settings/billing, "
               f"then run gh bouncer {url} again.")
    elif isinstance(e, anthropic.NotFoundError):
        msg = (f"Your Anthropic API key can't use {model}, the model this project reviews with (404). Check that "
               f"your key's workspace has access to it, then run gh bouncer {url} again.")
    elif isinstance(e, anthropic.APIStatusError) and status and status >= 500:
        what = "is overloaded" if status == 529 else "had an error"
        msg = f"Anthropic's API {what} ({status}), even after retrying. Try again in a few minutes: gh bouncer {url}"
    elif isinstance(e, anthropic.APIStatusError):
        msg = f"Anthropic's API refused the request ({status}): {detail}"
    else:  # connection errors and timeouts
        msg = f"Couldn't reach Anthropic's API (the connection failed or timed out). Try again in a few minutes: gh bouncer {url}"
    return f"{msg} {NOTHING_SIGNED}"


def skip(msg: str) -> None:
    print(f"::notice::{msg}")
    _out(skip="true")
    sys.exit(0)


def cmd_resolve(args, sleep=time.sleep) -> None:
    """Find the pull request to review.

    Manual runs fail loudly with instructions. Automatic runs (on push) exit quietly
    whenever there is nothing to do, so contributors never get failure emails from
    pushes that have no open pull request or no key configured, and aren't billed for
    reviews the bouncer isn't asking for.
    """
    gh = GitHub()
    me = os.environ["GITHUB_REPOSITORY"]
    auto = os.environ.get("GITHUB_EVENT_NAME") == "push"
    stop = skip if auto else fail

    wanted = (getattr(args, "upstream", "") or "").strip()
    if wanted and not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", wanted):
        fail(f"'{wanted}' is not a repository name. Use owner/repo, for example octo-org/widget.")
    repo = gh.get(f"/repos/{me}")
    parent = (repo.get("parent") or {}).get("full_name")
    if not repo.get("fork") or not parent:
        stop(f"{me} is not a fork. Run this workflow from your fork of the project.")
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        stop("No ANTHROPIC_API_KEY secret in this fork. Add it under Settings > Secrets and variables > Actions "
             "to run bouncer reviews.")

    # Where the pull request can be: the upstream input, else this fork's parent, then the root
    # of the fork network (for a fork of a fork, the parent is the intermediate fork).
    source = (repo.get("source") or {}).get("full_name")
    candidates = [wanted] if wanted else [parent] + ([source] if source and source.lower() != parent.lower() else [])
    pr_arg = (args.pr or "").strip().lstrip("#")
    branch = os.environ.get("GITHUB_REF_NAME", "")
    owner = me.split("/")[0]
    if pr_arg and not pr_arg.isdigit():
        fail(f"'{pr_arg}' is not a pull request number.")
    pr = parent = None
    for cand in candidates:
        if pr_arg:
            hit = gh.get_or_none(f"/repos/{cand}/pulls/{pr_arg}")
        else:
            head = urllib.parse.quote(f"{owner}:{branch}", safe=":")
            hit = (gh.get_or_none(f"/repos/{cand}/pulls?state=open&head={head}") or [None])[0]
        if hit and (pr is None or _head_repo(hit).lower() == me.lower()):
            pr, parent = hit, cand
            if _head_repo(hit).lower() == me.lower():
                break
    where = " or ".join(candidates)
    if pr is None and pr_arg:
        fail(f"Pull request #{pr_arg} was not found in {where}.")
    if pr is None:
        stop(f"No open pull request from {owner}:{branch} to {where}. Pick your pull request's branch "
             "under 'Use workflow from', or enter the pull request number.")
    n = pr["number"]
    if pr["state"] != "open":
        stop(f"{parent}#{n} is {pr['state']}. Reopen it first, then run the review.")
    head_repo = _head_repo(pr)
    if head_repo.lower() != me.lower():
        stop(f"{parent}#{n} comes from {head_repo or 'a deleted repository'}, not {me}. "
             "Run the review from the fork the pull request was opened from.")

    if auto:
        # GitHub updates the pull request a few seconds after a push.
        pushed = os.environ.get("GITHUB_SHA", "")
        for _ in range(6):
            if pr["head"]["sha"] == pushed:
                break
            sleep(10)
            pr = gh.get(f"/repos/{parent}/pulls/{n}")
        if pr["head"]["sha"] != pushed:  # checked after the last fetch too
            skip(f"{parent}#{n} hasn't picked up commit {pushed[:12]} yet. Push again or run the review manually.")
        if not _waiting_for_review(gh, parent, pr, sleep):
            # Nobody needs this review (passed, skipped, a draft, out of attempts...), so it isn't
            # billed to the contributor's key. A manual run still reviews.
            skip(f"{parent}#{n} isn't waiting for a bouncer review, so this push isn't reviewed and nothing is "
                 "billed to your key. To review it anyway, run gh bouncer.")

    _out(skip="false", pr=n, upstream=parent, head_repo=me, base_sha=pr["base"]["sha"], head_sha=pr["head"]["sha"])


def _waiting_for_review(gh: GitHub, upstream: str, pr: dict, sleep) -> bool:
    """Whether the bouncer is asking for a review of the pull request's current commit.

    The gate reacts to the same push, at about the time this run starts, so it gets up to two
    minutes to catch up: its state comment then names this commit, and says whether a review is
    pending. If it doesn't, the state for the earlier commit says whether the gate will ask for a
    review of this one (see _next_round). With no state at all (no gate upstream, say), the labels
    decide: bouncer:pending, and neither bouncer:skip nor bouncer:pass."""
    n, sha = pr["number"], pr["head"]["sha"]
    for attempt in range(13):
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if L_SKIP in labels:
            return False
        state = find_state(gh, upstream, n)[1] or {}
        if state.get("sha") == sha:
            return state.get("status") == "pending"
        if attempt < 12:
            sleep(10)
            pr = gh.get(f"/repos/{upstream}/pulls/{n}")
    waiting = _next_round(gh, upstream, pr, state) if state.get("sha") else None
    return waiting if waiting is not None else L_PENDING in labels and not labels & {L_SKIP, L_PASS}


def _next_round(gh: GitHub, upstream: str, pr: dict, state: dict) -> bool | None:
    """Whether the gate asks for a review of a new commit, going by its state for an earlier one
    (None for a status this version doesn't know). The labels can't tell: they still describe
    the earlier round, like bouncer:fail on a bounce left open, or bouncer:pass."""
    status, left = state.get("status"), state.get("left")
    if status in ("override", "exhausted", "wrong_base", "no_fork"):
        return False
    if status not in ("pending", "fail", "expired", "pass", "draft"):
        return None
    if pr.get("draft") and not state.get("drafted"):
        return False  # the contributor's own draft: no review until it's marked ready
    if status == "pass":
        try:
            text = config_mod.fetch_text(gh, upstream)
        except GitHubError:
            text = ""  # the default settings
        try:
            if not config_mod.parse(text).rereview_after_pass:
                return False
        except config_mod.ConfigError:
            return False  # the gate stops at an invalid .bouncer.yml too
    return not isinstance(left, int) or left > 0  # out of attempts, it isn't reviewed


def _head_repo(pr: dict) -> str:
    return ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")


def _find_contributing(base: Path) -> str:
    for rel in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING"):
        p = base / rel
        if p.is_file():
            return p.read_text("utf-8", "replace")[:8000]
    return ""


def _diff(gh: GitHub, upstream: str, pr: int) -> str:
    try:
        text = gh.get(f"/repos/{upstream}/pulls/{pr}", accept="application/vnd.github.diff")
    except GitHubError:
        files = gh.paginate(f"/repos/{upstream}/pulls/{pr}/files", limit=3000)
        text = "\n".join(f"--- {f['filename']}\n{f.get('patch') or '(no textual patch)'}" for f in files)
    if len(text) > MAX_DIFF:
        text = text[:MAX_DIFF] + "\n... (diff truncated; read the files in the head checkout for the rest)"
    return text


def _issues_text(gh: GitHub, upstream: str, linked: list[dict]) -> str:
    parts = []
    for i in linked[:5]:
        issue = gh.get_or_none(f"/repos/{upstream}/issues/{i['number']}") or {}
        parts.append(f"#{i['number']} [{issue.get('state')}] {issue.get('title', '')}\n{(issue.get('body') or '')[:5000]}")
    return "\n\n".join(parts)


def cmd_run(args) -> None:
    import anthropic
    import httpx2

    gh = GitHub()
    upstream, pr_n = args.upstream, int(args.pr)
    url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{upstream}/pull/{pr_n}"
    try:
        pr = gh.get(f"/repos/{upstream}/pulls/{pr_n}")
    except GitHubError as e:
        fail(f"Couldn't read the pull request from GitHub ({e}). Try again in a few minutes: gh bouncer {url} {NOTHING_SIGNED}")
    if pr["head"]["sha"] != args.head_sha:
        fail(f"The pull request got a new commit while the review was starting. Run gh bouncer {url} to review it. "
             f"{NOTHING_SIGNED}")

    base_dir, head_dir = Path(args.base_dir), Path(args.head_dir)
    # The copy the gate reads (the default branch), not the PR's base checkout: the gate only
    # accepts reviews made with its current settings, and GitHub doesn't move a PR's base sha
    # when the base branch moves, so a base-sha copy could stay stale however often it's re-run.
    cfg_text = config_mod.fetch_text(gh, upstream)
    try:
        cfg = config_mod.parse(cfg_text)
    except config_mod.ConfigError as e:
        fail(f"The maintainers' .bouncer.yml is invalid ({e}), so there's no review to run. Let them know. {NOTHING_SIGNED}")
    for w in cfg.warnings:
        print(f"::warning::The maintainers' .bouncer.yml: {w}")

    print(f"Reviewing {upstream}#{pr_n} at {args.head_sha[:12]} with {cfg.model} (effort {cfg.effort}, up to {cfg.max_turns} turns)")
    try:
        facts = gather(gh, upstream, pr)
        content = build_user_content(
            cfg, upstream, pr, facts,
            diff=_diff(gh, upstream, pr_n),
            contributing=_find_contributing(base_dir),
            issues_text=_issues_text(gh, upstream, facts["linked_issues"]),
        )
    except GitHubError as e:
        fail(f"Couldn't read the pull request from GitHub ({e}). Try again in a few minutes: gh bouncer {url} {NOTHING_SIGNED}")

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        fail(f"Your fork has no ANTHROPIC_API_KEY secret. Save one with gh bouncer --set-key {url}. {NOTHING_SIGNED}")
    # Base URL is pinned so nothing in the environment can redirect the review to another endpoint.
    client = anthropic.Anthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=4, timeout=600)
    ws = Workspace({"base": base_dir, "head": head_dir})
    agent = Agent(client, cfg.model, cfg.effort, cfg.max_turns, ws, gh, upstream)
    try:
        review = agent.run(SYSTEM, content, [r.id for r in cfg.rules])
    except ReviewFailed as e:
        fail(review_failed_message(e, url, cfg.max_turns))
    # Status, connection and timeout errors, after the client's retries, and network errors that
    # cut an answer off partway (the SDK lets those through), after the agent's.
    except (anthropic.APIError, httpx2.TransportError) as e:
        fail(api_error_message(e, url, cfg.model))
    verify_evidence(review, ws)

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    name = subject_name(upstream, pr_n, args.head_sha)
    predicate = {
        "schema": SCHEMA_VERSION,
        "protocol": REVIEW_PROTOCOL,
        "upstream": upstream,
        "pr": pr_n,
        "head_repo": args.head_repo,
        "head_sha": args.head_sha,
        "base_sha": args.base_sha,
        "subject": name,
        "config_digest": cfg.digest,
        "model": cfg.model,
        "effort": cfg.effort,
        "facts": facts,
        "review": review,
        "usage": {
            "input_tokens": agent.usage.input_tokens,
            "output_tokens": agent.usage.output_tokens,
            "cache_read_input_tokens": agent.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": agent.usage.cache_creation_input_tokens,
            "turns": agent.usage.turns,
            "tool_calls": agent.usage.tool_calls,
        },
        "bouncer_version": os.environ.get("BOUNCER_SHA", ""),
        "run_url": f"{server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "predicate.json").write_text(json.dumps(predicate, indent=1))
    (out / CONFIG_COPY).write_text(cfg_text)
    # No verdict here (see the module docstring): `report` shows it once the review is signed.
    print("Review written. It is signed next, and the verdict is shown after that.")
    _out(subject_name=name, subject_digest=f"sha256:{subject_digest(name)}")


def cmd_report(args) -> None:
    """Runs after the signing step: the verdict preview for the contributor."""
    out = Path(args.out_dir)
    predicate = json.loads((out / "predicate.json").read_text())
    cfg = config_mod.parse((out / CONFIG_COPY).read_text())
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    preview = decide(predicate, cfg)
    report = review_markdown(predicate, preview, cfg, server)
    (out / "report.md").write_text(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report)
    _out(verdict=preview.outcome)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="bouncer.review")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--pr", default="")
    r.add_argument("--upstream", default="")
    x = sub.add_parser("run")
    for a in ("--pr", "--upstream", "--head-repo", "--base-sha", "--head-sha", "--base-dir", "--head-dir", "--out-dir"):
        x.add_argument(a, required=True)
    sub.add_parser("report").add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    {"resolve": cmd_resolve, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
