"""Deterministic facts about a PR, computed by bouncer code (not the model).

They are signed into the attestation together with the review, and the gate
applies the maintainer's hard checks to them.
"""
from __future__ import annotations

import datetime as dt
import re

from .common import GitHub, GitHubError

_KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"


def linked_issue_numbers(body: str, upstream: str) -> list[int]:
    """Issues referenced with a closing keyword (Fixes #12, closes owner/repo#12, or a URL)."""
    body = body or ""
    up = re.escape(upstream)
    patterns = [
        rf"(?i)\b{_KEYWORDS}\s*:?\s+#(\d+)\b",
        rf"(?i)\b{_KEYWORDS}\s*:?\s+{up}#(\d+)\b",
        rf"(?i)\b{_KEYWORDS}\s*:?\s+https://github\.com/{up}/issues/(\d+)\b",
    ]
    found: list[int] = []
    for p in patterns:
        for m in re.finditer(p, body):
            n = int(m.group(1))
            if n not in found:
                found.append(n)
    return found[:10]


def _closing_refs_graphql(gh: GitHub, upstream: str, pr: int) -> list[int]:
    owner, name = upstream.split("/", 1)
    q = """query($o:String!,$n:String!,$pr:Int!){repository(owner:$o,name:$n){
      pullRequest(number:$pr){closingIssuesReferences(first:10){nodes{number}}}}}"""
    try:
        data = gh.graphql(q, {"o": owner, "n": name, "pr": pr})
        nodes = data["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        return [int(n["number"]) for n in nodes]
    except (GitHubError, KeyError, TypeError):
        return []


def gather(gh: GitHub, upstream: str, pr_data: dict, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    pr = int(pr_data["number"])
    author = pr_data["user"]["login"]

    files = gh.paginate(f"/repos/{upstream}/pulls/{pr}/files", limit=3000)
    changed = [
        {
            "path": f["filename"],
            "status": f.get("status", ""),
            "additions": int(f.get("additions", 0)),
            "deletions": int(f.get("deletions", 0)),
        }
        for f in files
    ]

    numbers = linked_issue_numbers(pr_data.get("body") or "", upstream)
    for n in _closing_refs_graphql(gh, upstream, pr):
        if n not in numbers:
            numbers.append(n)
    linked = []
    for n in numbers[:10]:
        issue = gh.get_or_none(f"/repos/{upstream}/issues/{n}")
        if not issue or "pull_request" in issue:
            continue
        linked.append({"number": n, "state": issue.get("state", ""), "title": issue.get("title", "")[:200]})

    user = gh.get_or_none(f"/users/{author}") or {}
    since = (now - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prs_24h = None
    try:
        res = gh.get(f"/search/issues?q=is:pr+author:{author}+created:>={since}&per_page=1")
        prs_24h = int(res.get("total_count", 0))
    except GitHubError:
        pass

    return {
        "author": author,
        "author_created_at": user.get("created_at"),
        "author_prs_24h": prs_24h,
        "author_association": pr_data.get("author_association", ""),
        "title": (pr_data.get("title") or "")[:300],
        "changed_files": changed,
        "files_truncated": len(files) >= 3000,
        "additions": sum(f["additions"] for f in changed),
        "deletions": sum(f["deletions"] for f in changed),
        "changed_lines": sum(f["additions"] + f["deletions"] for f in changed),
        "linked_issues": linked,
    }
