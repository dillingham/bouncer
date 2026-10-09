"""The review agent: a read-only tool loop over the base and head checkouts.

Nothing from the pull request is ever executed. The tools only list, read and
search text, and look up issues/PRs in the upstream repository.
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .common import GitHub, GitHubError

MAX_TOOL_OUTPUT = 20_000
ROOTS = ["base", "head"]


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


ROOT_PROP = {"type": "string", "enum": ROOTS, "description": "base = upstream base branch, head = the PR's version"}

TOOLS = [
    {
        "name": "list_dir",
        "description": "List entries of a directory in the base or head checkout. Use '' for the repo root.",
        "input_schema": _obj({"root": ROOT_PROP, "path": {"type": "string"}}),
    },
    {
        "name": "read_file",
        "description": "Read lines of a text file (1-based, inclusive). Output is prefixed with line numbers. "
        "Use end_line 0 to read to the end (output is capped).",
        "input_schema": _obj(
            {
                "root": ROOT_PROP,
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            }
        ),
    },
    {
        "name": "grep",
        "description": "Search file contents with an extended regular expression (git grep -E). "
        "path_glob limits files (git pathspec glob, e.g. 'src/**/*.py'); empty string searches all.",
        "input_schema": _obj({"root": ROOT_PROP, "pattern": {"type": "string"}, "path_glob": {"type": "string"}}),
    },
    {
        "name": "get_issue",
        "description": "Fetch an issue or pull request of the upstream repo by number: title, state, "
        "labels, body and the first comments (maintainer decisions often live there).",
        "input_schema": _obj({"number": {"type": "integer"}}),
    },
    {
        "name": "search_issues",
        "description": "Search issues and pull requests in the upstream repo (GitHub search syntax for the "
        "query terms; the repo qualifier is added for you). Use it to find duplicates and past declines.",
        "input_schema": _obj(
            {
                "query": {"type": "string"},
                "kind": {"type": "string", "enum": ["issue", "pr", "any"]},
                "state": {"type": "string", "enum": ["open", "closed", "any"]},
            }
        ),
    },
    {
        "name": "submit_review",
        "description": "Submit the final review. Call exactly once, when done. Every rule id must appear once.",
        "input_schema": _obj(
            {
                "summary": {"type": "string", "description": "2-5 sentences for the maintainer: what the PR does and whether it is worth their time."},
                "rules": {
                    "type": "array",
                    "items": _obj(
                        {
                            "id": {"type": "string"},
                            "result": {"type": "string", "enum": ["pass", "fail", "unsure"]},
                            "confidence": {"type": "number", "description": "0 to 1"},
                            "reason": {"type": "string"},
                            "evidence": {
                                "type": "array",
                                "items": _obj(
                                    {
                                        "root": ROOT_PROP,
                                        "path": {"type": "string"},
                                        "line": {"type": "integer"},
                                        "quote": {"type": "string", "description": "Text copied verbatim from that line."},
                                    }
                                ),
                            },
                        }
                    ),
                },
                "injection_detected": {"type": "boolean"},
                "injection_notes": {"type": "string"},
            }
        ),
    },
]


class ToolError(Exception):
    pass


@dataclass
class Workspace:
    roots: dict[str, Path]

    def resolve(self, root: str, path: str) -> Path:
        if root not in self.roots:
            raise ToolError(f"unknown root {root!r}")
        base = self.roots[root].resolve()
        rel = (path or "").strip().lstrip("/")
        if rel.split("/")[0] == ".git":
            raise ToolError("the .git directory is not readable")
        target = (base / rel).resolve()
        if target != base and base not in target.parents:
            raise ToolError("path escapes the repository")
        return target

    def list_dir(self, root: str, path: str) -> str:
        p = self.resolve(root, path)
        if not p.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: c.name):
            if child.name == ".git":
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
        return "\n".join(entries[:1000]) or "(empty)"

    def lines(self, root: str, path: str) -> list[str]:
        p = self.resolve(root, path)
        if not p.is_file():
            raise ToolError(f"no such file in {root}: {path}")
        data = p.read_bytes()
        if b"\0" in data[:8000]:
            raise ToolError("binary file")
        return data.decode("utf-8", "replace").splitlines()

    def read_file(self, root: str, path: str, start: int, end: int) -> str:
        lines = self.lines(root, path)
        start = max(1, start or 1)
        end = len(lines) if not end or end > len(lines) else end
        if start > len(lines):
            return f"(file has {len(lines)} lines)"
        out = [f"{i}: {lines[i - 1]}" for i in range(start, end + 1)]
        return "\n".join(out)

    def grep(self, root: str, pattern: str, glob: str) -> str:
        base = self.resolve(root, "")
        if not pattern or len(pattern) > 500:
            raise ToolError("pattern must be 1-500 characters")
        cmd = ["git", "-C", str(base), "grep", "-n", "-I", "-E", "--max-count=50", "-e", pattern, "--"]
        if glob:
            cmd.append(f":(glob){glob}")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise ToolError("search timed out; narrow the pattern or glob") from None
        if res.returncode == 1:
            return "(no matches)"
        if res.returncode != 0:
            raise ToolError(res.stderr.strip()[:500] or "grep failed")
        return res.stdout


def _clip(text: str, n: int = MAX_TOOL_OUTPUT) -> str:
    return text if len(text) <= n else text[:n] + f"\n... (truncated, {len(text) - n} more characters)"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    turns: int = 0
    tool_calls: dict = field(default_factory=dict)

    def add(self, u) -> None:
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            setattr(self, k, getattr(self, k) + int(getattr(u, k, 0) or 0))


class ReviewFailed(RuntimeError):
    pass


class Agent:
    def __init__(self, client, model: str, effort: str, max_turns: int, ws: Workspace,
                 gh: GitHub, upstream: str, log=print):
        self.client = client
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.ws = ws
        self.gh = gh
        self.upstream = upstream
        self.log = log
        self.usage = Usage()

    # --- tools -----------------------------------------------------------
    def _get_issue(self, number: int) -> str:
        issue = self.gh.get_or_none(f"/repos/{self.upstream}/issues/{int(number)}")
        if not issue:
            return f"#{number} not found"
        kind = "pull request" if "pull_request" in issue else "issue"
        out = {
            "number": issue["number"],
            "kind": kind,
            "title": issue.get("title"),
            "state": issue.get("state"),
            "state_reason": issue.get("state_reason"),
            "labels": [lb.get("name") for lb in issue.get("labels", [])],
            "author": (issue.get("user") or {}).get("login"),
            "author_association": issue.get("author_association"),
            "body": (issue.get("body") or "")[:4000],
        }
        if kind == "pull request":
            pr = self.gh.get_or_none(f"/repos/{self.upstream}/pulls/{int(number)}") or {}
            out["merged"] = bool(pr.get("merged_at"))
        comments = []
        if issue.get("comments"):
            for c in self.gh.paginate(f"/repos/{self.upstream}/issues/{int(number)}/comments", limit=15):
                comments.append({
                    "author": (c.get("user") or {}).get("login"),
                    "association": c.get("author_association"),
                    "body": (c.get("body") or "")[:1500],
                })
        out["comments"] = comments
        return json.dumps(out, indent=1)

    def _search(self, query: str, kind: str, state: str) -> str:
        q = f"repo:{self.upstream} {query}".strip()
        if kind == "issue":
            q += " is:issue"
        elif kind == "pr":
            q += " is:pr"
        if state in ("open", "closed"):
            q += f" is:{state}"
        res = self.gh.get(f"/search/issues?q={urllib.parse.quote(q)}&per_page=15")
        items = []
        for it in res.get("items", []):
            items.append({
                "number": it["number"],
                "kind": "pr" if "pull_request" in it else "issue",
                "state": it.get("state"),
                "state_reason": it.get("state_reason"),
                "title": it.get("title"),
                "labels": [lb.get("name") for lb in it.get("labels", [])],
            })
        return json.dumps({"total": res.get("total_count", 0), "items": items}, indent=1)

    def run_tool(self, name: str, args: dict) -> str:
        self.usage.tool_calls[name] = self.usage.tool_calls.get(name, 0) + 1
        try:
            if name == "list_dir":
                return _clip(self.ws.list_dir(args["root"], args["path"]))
            if name == "read_file":
                return _clip(self.ws.read_file(args["root"], args["path"], int(args["start_line"]), int(args["end_line"])))
            if name == "grep":
                return _clip(self.ws.grep(args["root"], args["pattern"], args.get("path_glob", "")))
            if name == "get_issue":
                return _clip(self._get_issue(int(args["number"])))
            if name == "search_issues":
                return _clip(self._search(args["query"], args["kind"], args["state"]))
            return f"error: unknown tool {name}"
        except (ToolError, GitHubError, KeyError, ValueError, OSError) as e:
            return f"error: {e}"

    # --- loop ------------------------------------------------------------
    def _create(self, system, messages):
        return self.client.messages.create(
            model=self.model,
            max_tokens=32_000,
            system=system,
            messages=messages,
            tools=[{**t, "strict": True} for t in TOOLS],
            tool_choice={"type": "auto"},
            output_config={"effort": self.effort},
        )

    def run(self, system: str, user_content: list, rule_ids: list[str]) -> dict:
        messages: list = [{"role": "user", "content": user_content}]
        nudged = False
        while True:
            if self.usage.turns >= self.max_turns + 2:
                raise ReviewFailed("the reviewer did not submit a verdict within the turn budget")
            resp = self._create(system, messages)
            self.usage.turns += 1
            self.usage.add(resp.usage)
            if resp.stop_reason == "refusal":
                raise ReviewFailed("the model declined to review this pull request")
            # Echo the assistant turn exactly as received (thinking blocks included).
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            submit = next((b for b in tool_uses if b.name == "submit_review"), None)
            if submit is not None:
                return normalize_review(submit.input, rule_ids)
            if not tool_uses:
                messages.append({"role": "user", "content": "Call submit_review now with your verdict for every rule."})
                continue
            results = []
            for b in tool_uses:
                self.log(f"  tool {b.name} {json.dumps(b.input)[:160]}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": self.run_tool(b.name, b.input)})
            if self.usage.turns >= self.max_turns and not nudged:
                nudged = True
                results.append({"type": "text", "text": "Turn budget reached. Call submit_review now; mark anything you could not establish as unsure."})
            messages.append({"role": "user", "content": results})


def normalize_review(raw: dict, rule_ids: list[str]) -> dict:
    by_id = {}
    for r in raw.get("rules", []) or []:
        rid = str(r.get("id", ""))
        if rid in rule_ids and rid not in by_id:
            try:
                conf = float(r.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            ev = []
            for e in (r.get("evidence") or [])[:8]:
                ev.append({
                    "root": e.get("root") if e.get("root") in ROOTS else "head",
                    "path": str(e.get("path", ""))[:300],
                    "line": int(e.get("line", 0) or 0),
                    "quote": str(e.get("quote", ""))[:300],
                    "verified": False,
                })
            by_id[rid] = {
                "id": rid,
                "result": r.get("result") if r.get("result") in ("pass", "fail", "unsure") else "unsure",
                "confidence": min(1.0, max(0.0, conf)),
                "reason": str(r.get("reason", ""))[:1500],
                "evidence": ev,
            }
    rules = [by_id.get(rid) or {"id": rid, "result": "unsure", "confidence": 0.0,
                                "reason": "Not evaluated by the reviewer.", "evidence": []} for rid in rule_ids]
    return {
        "summary": str(raw.get("summary", ""))[:2000],
        "rules": rules,
        "injection_detected": bool(raw.get("injection_detected")),
        "injection_notes": str(raw.get("injection_notes", ""))[:1000],
    }


def _norm(s: str) -> str:
    return " ".join(s.split())


def verify_evidence(review: dict, ws: Workspace) -> None:
    """Mark evidence verified only if the cited line exists and contains the quote (±2 lines)."""
    for r in review["rules"]:
        for e in r["evidence"]:
            try:
                lines = ws.lines(e["root"], e["path"])
            except (ToolError, OSError):
                continue
            n = e["line"]
            if not 1 <= n <= len(lines):
                continue
            quote = _norm(e["quote"])
            if not quote:
                continue
            window = lines[max(0, n - 3): min(len(lines), n + 2)]
            if any(quote in _norm(line) for line in window) or quote in _norm(" ".join(window)):
                e["verified"] = True
