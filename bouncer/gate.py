"""Maintainer side. Runs in the upstream repo on pull_request_target / issue_comment /
schedule. It never checks out or executes pull request code; it only reads the PR via the
API, verifies signed reviews, and labels, comments on, drafts or closes the PR.

  python -m bouncer.gate
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from . import config as config_mod
from .common import PREDICATE_TYPE, GitHub, GitHubError, subject_name
from .decide import decide
from .render import instructions, parse_state, review_markdown, state_block

L_PENDING, L_PASS, L_FAIL, L_SKIP = "bouncer:pending", "bouncer:pass", "bouncer:fail", "bouncer:skip"
LABEL_COLORS = {L_PENDING: "fbca04", L_PASS: "0e8a16", L_FAIL: "b60205", L_SKIP: "c5def5"}
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _parse_time(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, ISO).replace(tzinfo=dt.timezone.utc)


def _fmt_deadline(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class Found:
    ts: int
    predicate: dict
    run: str


def parse_verify_output(data: list) -> list[Found]:
    """Parse `gh attestation verify --format json` output (only verified attestations appear)."""
    found: list[Found] = []
    for item in data or []:
        try:
            bundle = item["attestation"]["bundle"]
            stmt = None
            payload = (bundle.get("dsseEnvelope") or {}).get("payload")
            if payload:
                stmt = json.loads(base64.b64decode(payload))
            if not stmt:
                stmt = item["verificationResult"]["statement"]
            if stmt.get("predicateType") != PREDICATE_TYPE:
                continue
            ts = None
            for e in (bundle.get("verificationMaterial") or {}).get("tlogEntries") or []:
                if e.get("integratedTime") is not None:
                    ts = int(e["integratedTime"])
                    break
            if ts is None:
                stamps = item["verificationResult"].get("verifiedTimestamps") or []
                ts = min(int(_parse_time(s["timestamp"]).timestamp()) for s in stamps)
            cert = item["verificationResult"]["signature"]["certificate"]
            found.append(Found(ts=ts, predicate=stmt["predicate"], run=cert.get("runInvocationURI", "")))
        except (KeyError, TypeError, ValueError):
            continue
    found.sort(key=lambda f: f.ts)
    return found


def gh_verifier(head_repo: str, name: str, signer_workflow: str, signer_digest: str | None) -> list[Found]:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "subject")
        with open(path, "wb") as f:
            f.write(name.encode())
        cmd = [
            "gh", "attestation", "verify", path,
            "--repo", head_repo,
            "--signer-workflow", signer_workflow,
            "--deny-self-hosted-runners",
            "--predicate-type", PREDICATE_TYPE,
            "--limit", "300",
            "--format", "json",
        ]
        if signer_digest:
            cmd += ["--signer-digest", signer_digest]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()[-3:]
        print(f"  no verified review for {name}: {' | '.join(tail)}")
        return []
    return parse_verify_output(json.loads(res.stdout or "[]"))


class Gate:
    def __init__(self, gh: GitHub, repo: str, cfg: config_mod.Config, verifier: Callable[[str, str], list[Found]],
                 now: dt.datetime | None = None, server: str = "https://github.com", log=print):
        self.gh = gh
        self.repo = repo
        self.cfg = cfg
        self.verifier = verifier
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.server = server
        self.log = log
        self._labels_ready = False

    # --- GitHub helpers ---------------------------------------------------
    def _ensure_labels(self) -> None:
        if self._labels_ready:
            return
        for name, color in LABEL_COLORS.items():
            try:
                self.gh.post(f"/repos/{self.repo}/labels", {"name": name, "color": color})
            except GitHubError as e:
                if e.status != 422:
                    raise
        self._labels_ready = True

    def _add_label(self, n: int, name: str) -> None:
        self._ensure_labels()
        self.gh.post(f"/repos/{self.repo}/issues/{n}/labels", {"labels": [name]})

    def _remove_label(self, n: int, name: str, current: set[str]) -> None:
        if name in current:
            try:
                self.gh.delete(f"/repos/{self.repo}/issues/{n}/labels/{urllib.parse.quote(name, safe='')}")
            except GitHubError as e:
                if e.status != 404:
                    raise
            current.discard(name)

    def _set_labels(self, n: int, current: set[str], add: str | None) -> None:
        for lb in (L_PENDING, L_PASS, L_FAIL):
            if lb != add:
                self._remove_label(n, lb, current)
        if add and add not in current:
            self._add_label(n, add)
            current.add(add)

    def _draft(self, pr: dict, draft: bool) -> None:
        mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        if bool(pr.get("draft")) == draft:
            return
        try:
            self.gh.graphql(f"mutation($id:ID!){{{mutation}(input:{{pullRequestId:$id}}){{clientMutationId}}}}",
                            {"id": pr["node_id"]})
        except GitHubError as e:
            self.log(f"  could not {'convert to draft' if draft else 'mark ready'} (labels still apply): {e}")

    def _comment(self, n: int, body: str) -> None:
        self.gh.post(f"/repos/{self.repo}/issues/{n}/comments", {"body": body[:65000]})

    def _save_state(self, n: int, sticky: dict | None, text: str, state: dict) -> dict:
        body = f"{text}\n\n{state_block(state)}"
        if sticky:
            self.gh.patch(f"/repos/{self.repo}/issues/comments/{sticky['id']}", {"body": body})
            return sticky
        return self.gh.post(f"/repos/{self.repo}/issues/{n}/comments", {"body": body})

    def _close(self, n: int) -> None:
        self.gh.patch(f"/repos/{self.repo}/pulls/{n}", {"state": "closed"})

    def _is_maintainer(self, login: str) -> bool:
        if not login:
            return False
        res = self.gh.get_or_none(f"/repos/{self.repo}/collaborators/{urllib.parse.quote(login)}/permission")
        return bool(res) and res.get("permission") in ("admin", "maintain", "write")

    # --- policy -------------------------------------------------------------
    def exempt(self, pr: dict) -> str | None:
        author = (pr.get("user") or {}).get("login", "")
        assoc = pr.get("author_association", "")
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
        labels = {lb["name"] for lb in pr.get("labels", [])}
        if L_SKIP in labels:
            return "skip label"
        if author in self.cfg.exempt_users:
            return "exempt user"
        if assoc in TRUSTED_ASSOCIATIONS:
            return f"author is {assoc.lower()}"
        if self.cfg.exempt_prior_contributors and assoc == "CONTRIBUTOR":
            return "prior contributor"
        if head_repo.lower() == self.repo.lower():
            return "branch in this repository"
        return None

    def _deadline(self, state: dict) -> dt.datetime:
        return _parse_time(state["requested_at"]) + dt.timedelta(hours=self.cfg.deadline_hours)

    # --- main flow ------------------------------------------------------------
    def process(self, pr: dict, action: str | None = None, sender: str | None = None) -> str:
        n = int(pr["number"])
        if pr.get("state") != "open":
            return "closed"
        labels = {lb["name"] for lb in pr.get("labels", [])}
        why = self.exempt(pr)
        if why:
            if L_PENDING in labels:
                self._set_labels(n, labels, None)
                self._draft(pr, False)
            self.log(f"#{n}: exempt ({why})")
            return "exempt"

        comments = self.gh.paginate(f"/repos/{self.repo}/issues/{n}/comments", limit=500)
        sticky, state = parse_state(comments)
        head_sha = pr["head"]["sha"]
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
        if not head_repo:
            self._comment(n, "### 🚪 Bouncer\n\nThe source repository of this pull request was deleted, so it can't be reviewed. Closing.")
            self._close(n)
            return "closed-no-fork"

        # Reopened by someone after a bounce, at the same commit.
        if state and state.get("sha") == head_sha and action == "reopened" and state.get("status") in ("fail", "expired", "exhausted"):
            if self._is_maintainer(sender or ""):
                state["status"] = "override"
                self._save_state(n, sticky, "### 🚪 Bouncer\n\nReopened by a maintainer, so the bouncer verdict is set aside.", state)
                self._set_labels(n, labels, None)
                self._draft(pr, False)
                return "override"
            self._comment(n, "### 🚪 Bouncer\n\nThis commit was already bounced. Push changes and reopen to request a new review.")
            self._close(n)
            return "reclosed"

        if state is None or state.get("sha") != head_sha:
            prev = state or {}
            if prev.get("status") in ("pass", "override") and not self.cfg.rereview_after_pass:
                prev["sha"] = head_sha
                self._save_state(n, sticky, "### 🚪 Bouncer\n\nAlready passed; later commits are not re-reviewed.", prev)
                return "kept-pass"
            fails = int(prev.get("fails", 0))
            if fails >= self.cfg.max_attempts:
                state = {**prev, "sha": head_sha, "status": "exhausted"}
                self._save_state(n, sticky, f"### 🚪 Bouncer\n\nThis pull request has used all {self.cfg.max_attempts} review rounds. Closing.", state)
                self._set_labels(n, labels, L_FAIL)
                self._close(n)
                return "exhausted"
            state = {
                "v": 1,
                "sha": head_sha,
                "requested_at": self.now.strftime(ISO),
                "status": "pending",
                "rounds": int(prev.get("rounds", 0)) + 1,
                "fails": fails,
            }
            text = instructions(n, head_repo, self.repo, _fmt_deadline(self._deadline(state)),
                                self.cfg.max_attempts - fails, self.server)
            sticky = self._save_state(n, sticky, text, state)
            self._set_labels(n, labels, L_PENDING)
            self._draft(pr, True)
            self.log(f"#{n}: review requested for {head_sha[:12]}")
        elif state.get("status") == "pending" and action == "ready_for_review":
            self._draft(pr, True)

        if state.get("status") != "pending":
            return state.get("status", "")

        name = subject_name(self.repo, n, head_sha)
        found = self.verifier(head_repo, name)
        valid = [
            f for f in found
            if str(f.predicate.get("upstream", "")).lower() == self.repo.lower()
            and int(f.predicate.get("pr", -1)) == n
            and f.predicate.get("head_sha") == head_sha
            and str(f.predicate.get("head_repo", "")).lower() == head_repo.lower()
        ]
        if valid:
            return self._apply(pr, labels, sticky, state, valid[0], attempts=len(valid))

        if self.now >= self._deadline(state):
            state["status"] = "expired"
            state["fails"] = int(state.get("fails", 0)) + 1
            self._save_state(n, sticky, "### 🚪 Bouncer\n\nNo signed review arrived before the deadline. Closing. "
                             "Run the review in your fork, then reopen this pull request.", state)
            self._set_labels(n, labels, L_FAIL)
            self._close(n)
            self.log(f"#{n}: expired")
            return "expired"
        self.log(f"#{n}: waiting for review")
        return "pending"

    def _apply(self, pr: dict, labels: set[str], sticky: dict | None, state: dict, f: Found, attempts: int) -> str:
        n = int(pr["number"])
        d = decide(f.predicate, self.cfg)
        report = review_markdown(f.predicate, d, self.server)
        if attempts > 1:
            report += f"\n\n<sub>{attempts} reviews were run for this commit; only the first one counts.</sub>"
        self._comment(n, report)
        state["status"] = d.outcome
        state["run"] = f.run
        if d.outcome == "pass":
            self._save_state(n, sticky, "### 🚪 Bouncer\n\n✅ Passed. Ready for a maintainer.", state)
            self._set_labels(n, labels, L_PASS)
            self._draft(pr, False)
        else:
            state["fails"] = int(state.get("fails", 0)) + 1
            left = self.cfg.max_attempts - state["fails"]
            if self.cfg.close_on_fail:
                more = (f"Push fixes and reopen this pull request for another review ({left} round{'s' if left != 1 else ''} left)."
                        if left > 0 else "No review rounds left.")
                self._save_state(n, sticky, f"### 🚪 Bouncer\n\n⛔ Bounced. {more}", state)
                self._set_labels(n, labels, L_FAIL)
                self._close(n)
            else:
                self._save_state(n, sticky, "### 🚪 Bouncer\n\n⛔ Bounced. Left open for a maintainer to confirm.", state)
                self._set_labels(n, labels, L_FAIL)
        self.log(f"#{n}: {d.outcome}")
        return d.outcome

    def sweep(self) -> None:
        issues = self.gh.paginate(f"/repos/{self.repo}/issues?state=open&labels={urllib.parse.quote(L_PENDING)}", limit=500)
        for it in issues:
            if "pull_request" not in it:
                continue
            pr = self.gh.get(f"/repos/{self.repo}/pulls/{it['number']}")
            try:
                self.process(pr)
            except GitHubError as e:
                self.log(f"#{it['number']}: {e}")

    def handle(self, event_name: str, payload: dict) -> None:
        if event_name in ("pull_request_target", "pull_request"):
            action = payload.get("action")
            if action not in ("opened", "reopened", "synchronize", "ready_for_review"):
                return
            self.process(payload["pull_request"], action=action, sender=(payload.get("sender") or {}).get("login"))
        elif event_name == "issue_comment":
            issue = payload.get("issue") or {}
            body = ((payload.get("comment") or {}).get("body") or "").strip()
            if "pull_request" in issue and body.startswith("/bouncer"):
                pr = self.gh.get(f"/repos/{self.repo}/pulls/{issue['number']}")
                self.process(pr)
        elif event_name == "workflow_dispatch" and (payload.get("inputs") or {}).get("pr"):
            pr = self.gh.get(f"/repos/{self.repo}/pulls/{int(payload['inputs']['pr'])}")
            self.process(pr)
        else:
            self.sweep()


def load_config(gh: GitHub, repo: str) -> config_mod.Config:
    text = gh.get_or_none(f"/repos/{repo}/contents/.bouncer.yml", accept="application/vnd.github.raw")
    return config_mod.parse(text or "")


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    bouncer_repo = os.environ["BOUNCER_REPO"]
    bouncer_sha = os.environ.get("BOUNCER_SHA", "")
    gh = GitHub()
    try:
        cfg = load_config(gh, repo)
    except config_mod.ConfigError as e:
        print(f"::error::.bouncer.yml is invalid: {e}")
        sys.exit(1)
    host = urllib.parse.urlparse(server).netloc
    signer = f"{host}/{bouncer_repo}/.github/workflows/review.yml"
    digest = bouncer_sha if cfg.pin_review_to_gate_version else None

    def verifier(head_repo: str, name: str) -> list[Found]:
        return gh_verifier(head_repo, name, signer, digest)

    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        payload = json.load(f)
    Gate(gh, repo, cfg, verifier, server=server).handle(os.environ["GITHUB_EVENT_NAME"], payload)


if __name__ == "__main__":
    main()
