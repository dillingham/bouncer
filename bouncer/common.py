"""Small shared pieces: the attestation subject, path globs, GitHub REST client."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

PREDICATE_TYPE = "https://github.com/pr-bouncer/bouncer/attestation/review/v1"
SCHEMA_VERSION = 1


def subject_name(upstream: str, pr: int, head_sha: str) -> str:
    """Deterministic subject both sides can compute.

    Every review run for the same PR at the same commit attests the same
    subject, so the gate can list all of them and only honor the earliest.
    """
    return f"bouncer-review/v1 {upstream.lower()}#{int(pr)}@{head_sha.lower()}"


def subject_digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def glob_to_regex(pattern: str) -> re.Pattern:
    """Path glob with ** (any depth), * (one segment) and ? support."""
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    """Minimal GitHub REST/GraphQL client using the workflow's token."""

    def __init__(self, token: str | None = None, api: str = "https://api.github.com"):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
        self.api = api.rstrip("/")

    def _request(self, method: str, path: str, body=None, accept="application/vnd.github+json"):
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bouncer",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    link = resp.headers.get("Link", "")
                    if accept.endswith("raw") or accept.endswith("diff"):
                        return raw.decode("utf-8", "replace"), link
                    return (json.loads(raw) if raw else None), link
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:500]
                if e.code in (502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(e.code, msg) from None
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise GitHubError(0, "unreachable")

    def get(self, path: str, accept="application/vnd.github+json"):
        return self._request("GET", path, accept=accept)[0]

    def get_or_none(self, path: str, accept="application/vnd.github+json"):
        try:
            return self.get(path, accept=accept)
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def paginate(self, path: str, limit: int = 1000) -> list:
        items: list = []
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}per_page=100"
        while url and len(items) < limit:
            page, link = self._request("GET", url)
            if isinstance(page, dict) and "items" in page:
                page = page["items"]
            items.extend(page or [])
            m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            url = m.group(1) if m else None
        return items[:limit]

    def post(self, path: str, body: dict):
        return self._request("POST", path, body)[0]

    def patch(self, path: str, body: dict):
        return self._request("PATCH", path, body)[0]

    def delete(self, path: str):
        return self._request("DELETE", path)[0]

    def graphql(self, query: str, variables: dict):
        res = self.post("/graphql", {"query": query, "variables": variables})
        if res and res.get("errors"):
            raise GitHubError(200, json.dumps(res["errors"])[:500])
        return (res or {}).get("data") or {}


def quote_path(p: str) -> str:
    return urllib.parse.quote(p, safe="/")
