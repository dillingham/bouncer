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
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from . import config as config_mod
from .common import (L_FAIL, L_PASS, L_PENDING, L_SKIP, MIN_REVIEW_PROTOCOL, PREDICATE_TYPE, GitHub, GitHubError,
                     subject_name)
from .decide import brief, decide
from .render import (STALE_NOTES, clean, find_state, fmt_deadline, instructions, next_steps, parse_state,
                     plural, review_markdown, state_block, status_text)

LABELS = {  # name: (color, description)
    L_PENDING: ("fbca04", "Waiting for the author's bouncer review"),
    L_PASS: ("0e8a16", "Passed the bouncer review"),
    L_FAIL: ("b60205", "Bounced by the bouncer review"),
    L_SKIP: ("c5def5", "Skips the bouncer review"),
}
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Why a signed review didn't count (state "stale", see Gate._stale), as the state's "note" for the CLI.
STALE_STATE_NOTES = {"config": "config_changed", "protocol": "outdated"}
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _parse_time(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, ISO).replace(tzinfo=dt.timezone.utc)


def _snapshot(state: dict | None) -> str:
    return json.dumps(state, sort_keys=True)


@dataclass
class Found:
    ts: int
    predicate: dict
    run: str
    # From the signing certificate: the review.yml commit that ran, and its full workflow ref.
    signer_sha: str = ""
    signer_uri: str = ""


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
            found.append(Found(ts=ts, predicate=stmt["predicate"], run=cert.get("runInvocationURI", ""),
                               signer_sha=str(cert.get("buildSignerDigest") or "").lower(),
                               signer_uri=str(cert.get("buildSignerURI") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    found.sort(key=lambda f: f.ts)
    return found


class VerifyError(RuntimeError):
    """Couldn't tell whether a signed review exists: API, network, TUF or timeout trouble."""


# What `gh attestation verify` says when there is simply no valid review: none at all, none of
# the bouncer's type, or only ones that fail verification (e.g. signed by another workflow).
NO_REVIEW = ("no attestations found", "sigstore verification failed", "policy verification failed")


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
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise VerifyError("gh attestation verify timed out after 180 s") from None
    if res.returncode != 0:
        tail = " | ".join((res.stderr or "").strip().splitlines()[-3:])
        if any(s in (res.stderr or "").lower() for s in NO_REVIEW):
            print(f"  no verified review for {name}: {tail}")
            return []
        raise VerifyError(tail or f"gh attestation verify exited with {res.returncode}")
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        raise VerifyError(f"gh attestation verify printed something other than JSON: {res.stdout.strip()[:200]}") from None
    return parse_verify_output(data)


class Gate:
    def __init__(self, gh: GitHub, repo: str, cfg: config_mod.Config, verifier: Callable[[str, str], list[Found]],
                 now: dt.datetime | None = None, server: str = "https://github.com", log=print,
                 action_repo: str = "gh-bouncer/action", action_ref: str = ""):
        self.gh = gh
        self.repo = repo
        self.cfg = cfg
        self.verifier = verifier
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.server = server
        self.log = log
        # The bouncer action this gate runs as, and its ref ("" = unknown, e.g. a local path).
        self.action_repo = action_repo
        self.action_ref = action_ref
        self._signer_ok: dict[str, bool] = {}  # signer commit -> in the gate's history (per run)
        self._persisted: dict[int, str] = {}  # PR -> the state as last read or written by this run
        self._labels_ready = False

    # --- GitHub helpers ---------------------------------------------------
    def _ensure_labels(self) -> None:
        """Create the bouncer's labels, with descriptions, once per run. Existing labels without a
        description get one; a maintainer's own color or description is left alone."""
        if self._labels_ready:
            return
        have = {lb.get("name"): lb for lb in self.gh.paginate(f"/repos/{self.repo}/labels", limit=1000)}
        for name, (color, description) in LABELS.items():
            if name not in have:
                try:
                    self.gh.post(f"/repos/{self.repo}/labels", {"name": name, "color": color, "description": description})
                except GitHubError as e:
                    if e.status != 422:  # 422: created meanwhile
                        raise
            elif not have[name].get("description"):
                self.gh.patch(f"/repos/{self.repo}/labels/{urllib.parse.quote(name, safe='')}", {"description": description})
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

    def _draft(self, pr: dict, draft: bool) -> bool:
        """Convert to a draft, or mark ready for review. True if this call changed it."""
        mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        if bool(pr.get("draft")) == draft:
            return False
        try:
            self.gh.graphql(f"mutation($id:ID!){{{mutation}(input:{{pullRequestId:$id}}){{clientMutationId}}}}",
                            {"id": pr["node_id"]})
        except GitHubError as e:
            self.log(f"  could not {'convert to draft' if draft else 'mark ready'} (labels still apply): {e}")
            return False
        pr["draft"] = draft
        return True

    def _hold(self, pr: dict, state: dict) -> None:
        """Make the pull request a draft while its review is pending, noting that the gate did."""
        if self._draft(pr, True):
            state["drafted"] = True

    def _release(self, pr: dict, state: dict) -> None:
        """Undo the gate's own draft conversion. A draft the contributor made stays a draft."""
        if state.pop("drafted", False):
            self._draft(pr, False)

    def _comment(self, n: int, body: str) -> dict:
        return self.gh.post(f"/repos/{self.repo}/issues/{n}/comments", {"body": body[:65000]}) or {}

    def _save_state(self, n: int, sticky: dict | None, text: str, state: dict) -> dict:
        """Write the state comment: the text for people, then the state as JSON for the gate's next
        run and for `gh bouncer`. The fields the CLI reads are kept up to date here (the state
        contract in the README)."""
        state["left"] = max(0, self.cfg.max_attempts - int(state.get("fails", 0)))
        state["model"], state["effort"] = self.cfg.model, self.cfg.effort
        state.setdefault("report", "")
        if state.get("status") == "pending":
            state["deadline"] = self._deadline(state).strftime(ISO)
        else:
            state.pop("deadline", None)
            state.pop("note", None)
        if state.get("status") not in ("fail", "wrong_base"):
            state.pop("reasons", None)
        body = f"{text}\n\n{state_block(state)}"
        self._persisted[n] = _snapshot(state)
        if sticky:
            self.gh.patch(f"/repos/{self.repo}/issues/comments/{sticky['id']}", {"body": body})
            return sticky
        return self.gh.post(f"/repos/{self.repo}/issues/{n}/comments", {"body": body})

    def _unchanged(self, n: int, sticky: dict | None, head_sha: str) -> dict | None:
        """Re-read the state comment and the pull request right before acting on a review or the
        deadline. The sweep and per-PR runs are in different concurrency groups, and looking for a
        review can take minutes, so another run may have started a new round, applied this same
        review or closed the pull request meanwhile. Returns the fresh pull request, or None if
        anything changed, in which case that other run's result stands."""
        current = None
        if sticky:
            c = self.gh.get_or_none(f"/repos/{self.repo}/issues/comments/{sticky['id']}")
            current = parse_state([c] if c else [])[1]
        pr = self.gh.get(f"/repos/{self.repo}/pulls/{n}")
        if (_snapshot(current) != self._persisted.get(n) or pr.get("state") != "open"
                or pr["head"]["sha"] != head_sha):
            self.log(f"#{n}: another run changed it while this one was looking for a review; leaving it to that run")
            return None
        return pr

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
        if author.lower() in {u.lower() for u in self.cfg.exempt_users}:  # GitHub logins ignore case
            return "exempt user"
        if self.cfg.exempt_maintainers and assoc in TRUSTED_ASSOCIATIONS:
            return f"author is {assoc.lower()}"
        if self.cfg.exempt_prior_contributors and assoc == "CONTRIBUTOR":
            return "prior contributor"
        if head_repo.lower() == self.repo.lower():
            return "branch in this repository"
        return None

    def _target_branches(self, pr: dict) -> list[str]:
        """Base branches outside pull requests may target: checks.target_branches, else the default branch."""
        if self.cfg.target_branches:
            return self.cfg.target_branches
        default = ((pr.get("base") or {}).get("repo") or {}).get("default_branch")
        return [default or self.gh.get(f"/repos/{self.repo}")["default_branch"]]

    def _deadline(self, state: dict) -> dt.datetime:
        return _parse_time(state["requested_at"]) + dt.timedelta(hours=self.cfg.deadline_hours)

    def _stale(self, predicate: dict) -> str | None:
        """Why a signed review of the right commit doesn't count, or None if it does."""
        try:
            protocol = int(predicate.get("protocol") or 0)
        except (TypeError, ValueError):
            protocol = 0
        if protocol < MIN_REVIEW_PROTOCOL:
            return "protocol"  # made by an outdated version of the review
        if predicate.get("config_digest") != self.cfg.digest:
            return "config"  # made with other settings than the maintainers' current ones
        return None

    def _url(self, n: int) -> str:
        return f"{self.server}/{self.repo}/pull/{n}"

    def _gate_ref(self) -> str:
        if not self.action_ref:
            # Not run from a tagged download (e.g. a local path): the action's default branch.
            self.action_ref = self.gh.get(f"/repos/{self.action_repo}")["default_branch"]
        return self.action_ref

    def _signed_by_gate_history(self, f: Found) -> bool:
        """Whether the review was signed by a review.yml commit in the history of the gate's own ref.

        GitHub runs `uses: gh-bouncer/action/.github/workflows/review.yml@<sha>` even when <sha>
        only exists in a fork of gh-bouncer/action (forks share git objects), and the signature
        then names gh-bouncer/action's review.yml all the same, with the fork's code inside. Such
        an imposter commit is never an ancestor of (or equal to) the ref the gate runs at.
        Checked once per commit per run. If GitHub can't answer, that's a VerifyError.
        """
        sha = f.signer_sha
        expected = f"{self.server}/{self.action_repo}/.github/workflows/review.yml@".lower()
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not f.signer_uri.lower().startswith(expected):
            self.log(f"  ignoring a review with an unexpected signer: {f.signer_uri or '(none)'}")
            return False
        if sha not in self._signer_ok:
            ref = self._gate_ref()
            try:
                res = self.gh.get(f"/repos/{self.action_repo}/compare/{sha}...{urllib.parse.quote(ref, safe='/')}?per_page=1")
                status = (res or {}).get("status", "")
            except GitHubError as e:
                if e.status not in (404, 422):  # the commit isn't in the repository at all
                    raise VerifyError(f"couldn't compare {self.action_repo}@{sha[:12]} with {ref}: {e}") from None
                status = f"not found ({e.status})"
            # "ahead": the gate's ref is ahead of the signer commit, so the commit is in its history.
            self._signer_ok[sha] = status in ("ahead", "identical")
            if not self._signer_ok[sha]:
                self.log(f"  ignoring reviews signed by {self.action_repo}@{sha[:12]}: not in the history of "
                         f"{self.action_repo}@{ref} (compare: {status})")
        return self._signer_ok[sha]

    def _instructions(self, n: int, head_repo: str, state: dict) -> str:
        """The pending round's comment, with the note on why a signed review didn't count, if any."""
        return instructions(n, head_repo, self.repo, fmt_deadline(self._deadline(state)),
                            self.cfg.max_attempts - int(state.get("fails", 0)), self.cfg, self.server,
                            STALE_NOTES.get(state.get("stale"), ""))

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
                if (find_state(self.gh, self.repo, n)[1] or {}).get("drafted"):
                    self._draft(pr, False)
            self.log(f"#{n}: exempt ({why})")
            return "exempt"

        sticky, state = find_state(self.gh, self.repo, n)
        self._persisted[n] = _snapshot(state)
        head_sha = pr["head"]["sha"]
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
        # A draft the contributor made (not the gate's hold) is still being worked on: no review is
        # asked for and no deadline runs until it's marked ready for review.
        wip = bool(pr.get("draft")) and not (state or {}).get("drafted")

        # Deterministic pre-check, before a review is asked for or looked at.
        base = (pr.get("base") or {}).get("ref", "")
        allowed = self._target_branches(pr)
        if base not in allowed:
            return self._wrong_base(pr, labels, sticky, state, base, allowed)

        # Reopened by someone after a bounce, at the same commit.
        if state and state.get("sha") == head_sha and action == "reopened" and state.get("status") in ("fail", "exhausted"):
            if self._is_maintainer(sender or ""):
                state["status"] = "override"
                self._release(pr, state)
                self._save_state(n, sticky, status_text("override"), state)
                self._set_labels(n, labels, None)
                return "override"
            self._comment(n, status_text("reclosed", url=self._url(n)))
            self._close(n)
            return "reclosed"

        # A maintainer set the bounce aside. That holds for later commits too, whatever
        # rereview_after_pass says: a new round could only end in another bounce or, with no
        # attempts left, in closing a pull request the maintainer chose to keep.
        if state and state.get("status") == "override":
            if state.get("sha") != head_sha:
                state["sha"] = head_sha
                self._save_state(n, sticky, status_text("override"), state)
            return "override"

        # A PR that expired without a review gets a fresh round when it is reopened, even at the same commit.
        retry_expired = bool(state) and action == "reopened" and state.get("status") == "expired"
        # So does one that was bounced for its base branch, once it targets an allowed one.
        retarget = bool(state) and state.get("status") == "wrong_base"
        # And a draft once it's marked ready.
        was_draft = bool(state) and state.get("status") == "draft"
        if state is None or state.get("sha") != head_sha or retry_expired or retarget or was_draft:
            prev = state or {}
            if prev.get("status") == "pass" and not self.cfg.rereview_after_pass:
                prev["sha"] = head_sha
                self._save_state(n, sticky, status_text("kept_pass", report=prev.get("report", "")), prev)
                return "kept-pass"
            if not head_repo:
                return self._no_fork(n, sticky, prev, labels, head_sha)
            if wip:
                if not was_draft:
                    state = {"v": 1, "sha": head_sha, "status": "draft",
                             "rounds": int(prev.get("rounds", 0)), "fails": int(prev.get("fails", 0))}
                    self._save_state(n, sticky, status_text("draft"), state)
                    self._set_labels(n, labels, None)
                self.log(f"#{n}: draft, waiting until it's ready for review")
                return "draft"
            fails = int(prev.get("fails", 0))
            if fails >= self.cfg.max_attempts:
                state = {**prev, "sha": head_sha, "status": "exhausted"}
                self._save_state(n, sticky, status_text("exhausted" if self.cfg.close_on_fail else "exhausted_open",
                                                        allowed=plural(self.cfg.max_attempts, "review attempt")), state)
                self._set_labels(n, labels, L_FAIL)
                if self.cfg.close_on_fail:
                    self._close(n)
                return "exhausted"
            state = {
                "v": 1,
                "sha": head_sha,
                "requested_at": self.now.strftime(ISO),
                "status": "pending",
                "rounds": int(prev.get("rounds", 0)) + 1,
                "fails": fails,
                **({"drafted": True} if prev.get("drafted") else {}),
            }
            self._hold(pr, state)
            sticky = self._save_state(n, sticky, self._instructions(n, head_repo, state), state)
            self._set_labels(n, labels, L_PENDING)
            self.log(f"#{n}: review requested for {head_sha[:12]}")
        elif state.get("status") == "pending" and action == "ready_for_review" and state.get("drafted"):
            self._draft(pr, True)  # marked ready while the gate holds it: back to a draft until it passes
        elif state.get("status") == "pending" and head_repo and (
                (action == "ready_for_review")  # a draft the contributor made while it was pending
                or (action == "reopened" and self.now >= self._deadline(state))):
            # A fresh deadline rather than closing it on the spot: it's ready for review again, or
            # was closed while waiting (by the contributor, say) and reopened after the deadline.
            # Same commit, so same round.
            state["requested_at"] = self.now.strftime(ISO)
            self._hold(pr, state)
            self._save_state(n, sticky, self._instructions(n, head_repo, state), state)
            self._set_labels(n, labels, L_PENDING)
            self.log(f"#{n}: new deadline ({action})")

        if state.get("status") != "pending":
            return state.get("status", "")

        if not head_repo:
            return self._no_fork(n, sticky, state, labels, head_sha)
        try:
            found = self._signed_reviews(n, head_repo, head_sha)
        except VerifyError as e:
            # Not the same as no review: one may well exist. Try again next run, and don't let the
            # deadline close the pull request on an outage.
            self.log(f"::warning::#{n}: couldn't check for a signed review, trying again next run "
                     f"(not closing it at the deadline meanwhile): {e}")
            if state.get("note") != "verify_error" and self._unchanged(n, sticky, head_sha):
                state["note"] = "verify_error"  # for `gh bouncer`, which waits on the verdict
                self._save_state(n, sticky, self._instructions(n, head_repo, state), state)
            return "verify-error"
        valid = [f for f in found if not self._stale(f.predicate)]
        # Signed reviews of this commit that don't count. That's not a fail and uses no round:
        # the contributor is asked to run it again (said once per reason).
        stale = self._stale(found[-1].predicate) if found and not valid else None
        if stale:
            self.log(f"#{n}: signed review doesn't count ({stale})")
        expired = not valid and not wip and self.now >= self._deadline(state)
        # The state's note says why the latest signed review didn't count (cleared after an error).
        note = STALE_STATE_NOTES.get(stale or state.get("stale"))
        renote = (stale and state.get("stale") != stale) or state.get("note") != note
        if not (valid or expired or renote):
            self.log(f"#{n}: waiting for review")
            return "pending"

        fresh = self._unchanged(n, sticky, head_sha)
        if fresh is None:
            return "changed"
        pr, labels = fresh, {lb["name"] for lb in fresh.get("labels", [])}
        if valid:
            return self._apply(pr, labels, sticky, state, valid[0], attempts=len(valid))
        if renote:
            for key, value in (("stale", stale or state.get("stale")), ("note", note)):
                if value:
                    state[key] = value
                else:
                    state.pop(key, None)
            self._save_state(n, sticky, self._instructions(n, head_repo, state), state)

        if expired:
            state["status"] = "expired"
            state["fails"] = int(state.get("fails", 0)) + 1
            left = self.cfg.max_attempts - state["fails"]
            self._save_state(n, sticky, status_text("expired" if left > 0 else "expired_last", url=self._url(n),
                                                    deadline=fmt_deadline(self._deadline(state)),
                                                    left_text=plural(left, "review attempt")), state)
            self._set_labels(n, labels, L_FAIL)
            self._close(n)
            self.log(f"#{n}: expired")
            return "expired"
        return "pending"

    def _no_fork(self, n: int, sticky: dict | None, state: dict, labels: set[str], head_sha: str) -> str:
        """Close a pull request whose fork was deleted while it needs a review: none can be run
        or found for it. Only called for a new or pending round; a PR that already passed, or
        that a maintainer let through, is left alone."""
        state = {"v": 1, "rounds": 0, "fails": 0, **state, "sha": head_sha, "status": "no_fork"}
        self._save_state(n, sticky, status_text("no_fork"), state)
        self._set_labels(n, labels, None)
        self._close(n)
        self.log(f"#{n}: fork deleted")
        return "closed-no-fork"

    def _signed_reviews(self, n: int, head_repo: str, head_sha: str) -> list[Found]:
        """Verified reviews of exactly this PR and commit, signed by the bouncer's review.yml from
        the gate's own history, earliest first. Raises VerifyError if that can't be determined."""
        found = [
            f for f in self.verifier(head_repo, subject_name(self.repo, n, head_sha))
            if str(f.predicate.get("upstream", "")).lower() == self.repo.lower()
            and int(f.predicate.get("pr", -1)) == n
            and f.predicate.get("head_sha") == head_sha
            and str(f.predicate.get("head_repo", "")).lower() == head_repo.lower()
        ]
        return [f for f in found if self._signed_by_gate_history(f)]

    def _wrong_base(self, pr: dict, labels: set[str], sticky: dict | None, state: dict | None,
                    base: str, allowed: list[str]) -> str:
        """Bounce a PR into a branch outside checks.target_branches. No review is involved, so
        no round is used up."""
        n, head_sha = int(pr["number"]), pr["head"]["sha"]
        # Explain once per commit and base branch; later events only keep it bounced.
        told = bool(state) and state.get("status") == "wrong_base" and state.get("sha") == head_sha and state.get("base") == base
        if not told:
            names = ", ".join(f"`{b}`" for b in allowed)
            target = f"into {names}" if len(allowed) == 1 else f"into one of {names}"
            fix = ("Open a new pull request against the right branch (a closed pull request's base can't be changed)."
                   if self.cfg.close_on_fail else "Change the base branch, then comment `/bouncer check`.")
            prev = state or {}
            why = f"this pull request targets `{clean(base, 200)}`, but this project only takes outside pull requests {target}."
            state = {"v": 1, "sha": head_sha, "status": "wrong_base", "base": base,
                     "rounds": int(prev.get("rounds", 0)), "fails": int(prev.get("fails", 0)),
                     "reasons": [brief(f"Pre-check: {why}")],
                     **({"drafted": True} if prev.get("drafted") else {})}
            self._save_state(n, sticky, f"### 🚪 Bouncer\n\n⛔ **Bounced:** {why} {fix}", state)
        self._set_labels(n, labels, L_FAIL)
        if self.cfg.close_on_fail:
            self._close(n)
        self.log(f"#{n}: targets {base}, not {', '.join(allowed)}")
        return "wrong-base"

    def _apply(self, pr: dict, labels: set[str], sticky: dict | None, state: dict, f: Found, attempts: int) -> str:
        n = int(pr["number"])
        d = decide(f.predicate, self.cfg)
        bounced = d.outcome != "pass"
        fails = int(state.get("fails", 0)) + bounced
        steps = next_steps(self._url(n), self.cfg.max_attempts - fails, self.cfg.close_on_fail) if bounced else ""
        report = review_markdown(f.predicate, d, self.cfg, self.server, next_steps=steps)
        if attempts > 1:
            report += f"\n\n<sub>{attempts} reviews were run for this commit; only the first one counts.</sub>"
        state["report"] = self._comment(n, report).get("html_url", "")
        state.update(status=d.outcome, run=f.run, fails=fails)
        state.pop("stale", None)
        if not bounced:
            self._release(pr, state)
            self._save_state(n, sticky, status_text("pass", report=state["report"]), state)
            self._set_labels(n, labels, L_PASS)
        else:
            state["reasons"] = [brief(r) for r in d.reasons[:5]]
            kind = "fail_closed" if self.cfg.close_on_fail else "fail_open"
            self._save_state(n, sticky, status_text(kind, report=state["report"], steps=steps), state)
            self._set_labels(n, labels, L_FAIL)
            if self.cfg.close_on_fail:
                self._close(n)
        self.log(f"#{n}: {d.outcome}")
        return d.outcome

    def sweep(self) -> None:
        issues = self.gh.paginate(f"/repos/{self.repo}/issues?state=open&labels={urllib.parse.quote(L_PENDING)}", limit=500)
        for it in issues:
            if "pull_request" not in it:
                continue
            try:
                self.process(self.gh.get(f"/repos/{self.repo}/pulls/{it['number']}"))
            except Exception as e:  # noqa: BLE001 - one pull request mustn't stop the sweep for the rest
                self.log(f"::warning::#{it['number']}: {type(e).__name__}: {e}")

    def handle(self, event_name: str, payload: dict) -> None:
        if event_name in ("pull_request_target", "pull_request"):
            action = payload.get("action")
            if action not in ("opened", "reopened", "synchronize", "ready_for_review", "edited"):
                return
            # The payload is a snapshot from when the event fired, and the run may start much later
            # (queued behind other runs for this PR), so labels, draft state and head are read fresh.
            pr = self.gh.get(f"/repos/{self.repo}/pulls/{int(payload['pull_request']['number'])}")
            self.process(pr, action=action, sender=(payload.get("sender") or {}).get("login"))
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
    return config_mod.parse(config_mod.fetch_text(gh, repo))


def action_identity(action_path: str) -> tuple[str, str]:
    """owner/repo and ref of this action, from the runner's download path
    (/home/runner/work/_actions/OWNER/REPO/REF)."""
    marker = "/_actions/"
    norm = (action_path or "").replace("\\", "/")
    if marker not in norm:
        return "", ""
    parts = norm.split(marker, 1)[1].strip("/").split("/")
    if len(parts) < 3:
        return "", ""
    return f"{parts[0]}/{parts[1]}", "/".join(parts[2:])


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    bouncer_repo, bouncer_ref = action_identity(os.environ.get("BOUNCER_ACTION_PATH", ""))
    bouncer_repo = os.environ.get("BOUNCER_REPO") or bouncer_repo
    if not bouncer_repo:
        print("::error::Could not tell which bouncer action is running. Use it as `uses: gh-bouncer/action@v1`.")
        sys.exit(1)
    gh = GitHub()
    try:
        cfg = load_config(gh, repo)
    except config_mod.ConfigError as e:
        print(f"::error::.bouncer.yml is invalid: {e}")
        sys.exit(1)
    for w in cfg.warnings:
        print(f"::warning::.bouncer.yml: {w}")
    host = urllib.parse.urlparse(server).netloc
    signer = f"{host}/{bouncer_repo}/.github/workflows/review.yml"
    # Run from a local path there's no ref: use the action's default branch, as the signer check does.
    bouncer_ref = bouncer_ref or gh.get(f"/repos/{bouncer_repo}")["default_branch"]
    digest = None
    if cfg.pin_review_to_gate_version:
        digest = gh.get(f"/repos/{bouncer_repo}/commits/{urllib.parse.quote(bouncer_ref, safe='')}")["sha"]

    def verifier(head_repo: str, name: str) -> list[Found]:
        return gh_verifier(head_repo, name, signer, digest)

    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        payload = json.load(f)
    Gate(gh, repo, cfg, verifier, server=server, action_repo=bouncer_repo, action_ref=bouncer_ref).handle(
        os.environ["GITHUB_EVENT_NAME"], payload)


if __name__ == "__main__":
    main()
