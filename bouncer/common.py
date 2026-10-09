"""Small shared pieces: the attestation subject, untrusted fences, path globs, GitHub REST client."""
from __future__ import annotations

import functools
import hashlib
import html
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

PREDICATE_TYPE = "https://github.com/gh-bouncer/action/attestation/review/v1"
L_PENDING, L_PASS, L_FAIL, L_SKIP = "bouncer:pending", "bouncer:pass", "bouncer:fail", "bouncer:skip"
SCHEMA_VERSION = 1

# What a signed review guarantees. The review writes REVIEW_PROTOCOL into its predicate and the
# gate ignores reviews below MIN_REVIEW_PROTOCOL. The predicate is written by the review code at
# whatever ref ran, so an older review.yml can't claim a newer protocol. Bump both when a fix to
# the review must not be bypassed by running an older version of it.
#   1 (no field): the original review
#   2: verdict hidden until signed, settings digest checked, skipped hard rules fail,
#      tool output fenced as untrusted
#   3: cut-off answers rejected, only upstream closing references count as linked issues,
#      renamed-from paths in the facts, .git blocked by resolved path
REVIEW_PROTOCOL = 3
MIN_REVIEW_PROTOCOL = 3


def subject_name(upstream: str, pr: int, head_sha: str) -> str:
    """Deterministic subject both sides can compute.

    Every review run for the same PR at the same commit attests the same
    subject, so the gate can list all of them and only honor the earliest.
    """
    return f"bouncer-review/v1 {upstream.lower()}#{int(pr)}@{head_sha.lower()}"


def subject_digest(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


@functools.cache
def _closing_tag() -> re.Pattern:
    """The < of a closing </untrusted> tag, also with spaces or Unicode format characters
    (zero-width space, soft hyphen, BOM, bidi controls...) anywhere in it: a tokenizer may drop
    those, or the model may not see them, and then the tag reads as a real one."""
    fmt = re.escape("".join(chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"))
    gap, hidden = f"[\\s{fmt}]*", f"[{fmt}]*"
    return re.compile(f"<(?={gap}/{gap}{hidden.join('untrusted')})", re.IGNORECASE)


def untrusted(text: str, **attrs: str) -> str:
    """Fence text from outside the maintainer team as data for the model:
    <untrusted source="head:src/a.py">...</untrusted>.

    A closing tag inside the text is escaped, so the text can't end the fence early and pass
    off what follows as trusted. The text is otherwise kept as it is, invisible characters
    included (a reviewer should see bidi tricks, say). Attribute values (paths chosen by the
    model) are escaped too.
    """
    body = _closing_tag().sub("&lt;", text)
    attr = "".join(f' {k}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())
    return f"<untrusted{attr}>\n{body}\n</untrusted>"


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


def _path_pattern(pattern: str) -> tuple[re.Pattern, bool]:
    """A forbidden_paths pattern, read like a .gitignore line: a slash at the start or in the
    middle anchors it to the repository root (`/vendor`, `.github/**`), a trailing slash matches
    directories only (`vendor/`), and a pattern with no other slash matches at any depth
    (`*.lock`, `vendor/`). Returns the regex and whether it only matches directories."""
    p = pattern.strip()
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    if "/" not in p:
        p = "**/" + p
    return glob_to_regex(p.lstrip("/")), dir_only


def path_matches(path: str, patterns: list[str]) -> bool:
    """Whether a changed file matches a forbidden_paths pattern, itself or through any of its
    directories (as in .gitignore, a matching directory covers everything inside it)."""
    parts = path.split("/")
    dirs = ["/".join(parts[:i]) for i in range(1, len(parts))]
    for pattern in patterns:
        rx, dir_only = _path_pattern(pattern)
        if any(rx.match(d) for d in dirs) or (not dir_only and rx.match(path)):
            return True
    return False


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
        # A request that fails with a 502/503/504 or a dropped connection may still have gone
        # through. Everything the gate sends is safe to repeat except creating a comment, which
        # would post it twice, so that is sent once.
        tries = 1 if method == "POST" and path.endswith("/comments") else 4
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            last = attempt == tries - 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    link = resp.headers.get("Link", "")
                    if accept.endswith("raw") or accept.endswith("diff"):
                        return raw.decode("utf-8", "replace"), link
                    return (json.loads(raw) if raw else None), link
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:500]
                if e.code in (502, 503, 504) and not last:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(e.code, msg) from None
            except (urllib.error.URLError, TimeoutError, ConnectionResetError, http.client.RemoteDisconnected):
                if not last:
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

    def page(self, path: str) -> tuple[list, dict]:
        """One page of a list endpoint, and its Link relations ({"next": url, "last": url, ...})."""
        items, link = self._request("GET", path)
        return items or [], {m.group(2): m.group(1) for m in re.finditer(r'<([^>]+)>;\s*rel="(\w+)"', link or "")}

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
