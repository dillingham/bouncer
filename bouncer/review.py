"""Contributor side. Runs inside the reusable review workflow, in the contributor's fork,
on the contributor's API key.

  python -m bouncer.review resolve --pr N     -> GITHUB_OUTPUT: upstream, base_sha, head_sha, ...
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
import sys
import time
import urllib.parse
from pathlib import Path

from . import config as config_mod
from .agent import Agent, ReviewFailed, Workspace, verify_evidence
from .common import SCHEMA_VERSION, GitHub, GitHubError, subject_digest, subject_name
from .decide import decide
from .facts import gather
from .prompt import SYSTEM, build_user_content
from .render import review_markdown

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
    print(f"::error::{msg}")
    sys.exit(1)


def skip(msg: str) -> None:
    print(f"::notice::{msg}")
    _out(skip="true")
    sys.exit(0)


def cmd_resolve(args, sleep=time.sleep) -> None:
    """Find the pull request to review.

    Manual runs fail loudly with instructions. Automatic runs (on push) exit quietly
    whenever there is nothing to do, so contributors never get failure emails from
    pushes that have no open pull request or no key configured.
    """
    gh = GitHub()
    me = os.environ["GITHUB_REPOSITORY"]
    auto = os.environ.get("GITHUB_EVENT_NAME") == "push"
    stop = skip if auto else fail

    repo = gh.get(f"/repos/{me}")
    parent = (repo.get("parent") or {}).get("full_name")
    if not repo.get("fork") or not parent:
        stop(f"{me} is not a fork. Run this workflow from your fork of the project.")
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        stop("No ANTHROPIC_API_KEY secret in this fork. Add it under Settings > Secrets and variables > Actions "
             "to run bouncer reviews.")

    pr_arg = (args.pr or "").strip().lstrip("#")
    branch = os.environ.get("GITHUB_REF_NAME", "")
    if pr_arg:
        if not pr_arg.isdigit():
            fail(f"'{pr_arg}' is not a pull request number.")
        pr = gh.get_or_none(f"/repos/{parent}/pulls/{pr_arg}")
        if not pr:
            fail(f"Pull request #{pr_arg} was not found in {parent}.")
    else:
        owner = me.split("/")[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe=":")
        prs = gh.get(f"/repos/{parent}/pulls?state=open&head={head}") or []
        if not prs:
            stop(f"No open pull request from {owner}:{branch} to {parent}. Pick your pull request's branch "
                 "under 'Use workflow from', or enter the pull request number.")
        pr = prs[0]
    n = pr["number"]
    if pr["state"] != "open":
        stop(f"{parent}#{n} is {pr['state']}. Reopen it first, then run the review.")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
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
        else:
            skip(f"{parent}#{n} hasn't picked up commit {pushed[:12]} yet. Push again or run the review manually.")

    _out(skip="false", pr=n, upstream=parent, head_repo=me, base_sha=pr["base"]["sha"], head_sha=pr["head"]["sha"])


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

    gh = GitHub()
    upstream, pr_n = args.upstream, int(args.pr)
    pr = gh.get(f"/repos/{upstream}/pulls/{pr_n}")
    if pr["head"]["sha"] != args.head_sha:
        fail("The pull request changed while the review was starting. Run the workflow again.")

    base_dir, head_dir = Path(args.base_dir), Path(args.head_dir)
    cfg_path = base_dir / ".bouncer.yml"
    cfg_text = cfg_path.read_text("utf-8") if cfg_path.is_file() else ""
    try:
        cfg = config_mod.parse(cfg_text)
    except config_mod.ConfigError as e:
        fail(f"The maintainers' .bouncer.yml is invalid: {e}")

    print(f"Reviewing {upstream}#{pr_n} at {args.head_sha[:12]} with {cfg.model} (effort {cfg.effort}, up to {cfg.max_turns} turns)")
    facts = gather(gh, upstream, pr)
    content = build_user_content(
        cfg, upstream, pr, facts,
        diff=_diff(gh, upstream, pr_n),
        contributing=_find_contributing(base_dir),
        issues_text=_issues_text(gh, upstream, facts["linked_issues"]),
    )

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        fail("ANTHROPIC_API_KEY is not set. Add it under Settings > Secrets and variables > Actions in your fork.")
    # Base URL is pinned so nothing in the environment can redirect the review to another endpoint.
    client = anthropic.Anthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=4, timeout=600)
    ws = Workspace({"base": base_dir, "head": head_dir})
    agent = Agent(client, cfg.model, cfg.effort, cfg.max_turns, ws, gh, upstream)
    try:
        review = agent.run(SYSTEM, content, [r.id for r in cfg.rules])
    except ReviewFailed as e:
        fail(str(e))
    except anthropic.AuthenticationError:
        fail("Anthropic rejected the API key. Check the ANTHROPIC_API_KEY secret in your fork.")
    except anthropic.APIStatusError as e:
        fail(f"Anthropic API error {e.status_code}: {str(e)[:300]}")
    verify_evidence(review, ws)

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    name = subject_name(upstream, pr_n, args.head_sha)
    predicate = {
        "schema": SCHEMA_VERSION,
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
    report = review_markdown(predicate, preview, server)
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
    x = sub.add_parser("run")
    for a in ("--pr", "--upstream", "--head-repo", "--base-sha", "--head-sha", "--base-dir", "--head-dir", "--out-dir"):
        x.add_argument(a, required=True)
    sub.add_parser("report").add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    {"resolve": cmd_resolve, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
