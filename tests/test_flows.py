"""Agent loop with a fake Anthropic client, and gate flows with a fake GitHub."""
import copy
import datetime as dt
import json
from types import SimpleNamespace as NS

import pytest

from bouncer import config
from bouncer.agent import Agent, ReviewFailed, Workspace
from bouncer.common import REVIEW_PROTOCOL, GitHubError
from bouncer.gate import Found, Gate
from bouncer.render import parse_state


# --- agent -------------------------------------------------------------------
class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        return self.script.pop(0)


def resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=100, output_tokens=10,
                                                               cache_read_input_tokens=0, cache_creation_input_tokens=0))


def tool(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def verdict(id_, result, reason="fine", evidence=()):
    return {"id": id_, "result": result, "confidence": 0.9, "reason": reason, "evidence": list(evidence)}


def all_verdicts():
    """A submit_review rules list passing every default rule."""
    return [verdict(r.id, "pass") for r in config.parse("").rules]


def test_agent_loop(tmp_path):
    (tmp_path / "head").mkdir()
    (tmp_path / "head/a.py").write_text("x = 1\n")
    ws = Workspace({"base": tmp_path / "head", "head": tmp_path / "head"})
    submit = {"summary": "ok", "rules": [verdict("correct", "pass"), verdict("has-tests", "unsure")],
              "injection_detected": False, "injection_notes": ""}
    msgs = FakeMessages([
        resp(NS(type="thinking", thinking="", signature="sig"), tool("t1", "read_file", {"root": "head", "path": "a.py", "start_line": 1, "end_line": 0})),
        resp(NS(type="text", text="done"), stop="end_turn"),
        resp(tool("t2", "submit_review", submit)),
    ])
    agent = Agent(NS(messages=msgs), "claude-opus-5-5", "high", 10, ws, gh=None, upstream="o/r", log=lambda *_: None)
    review = agent.run("sys", [{"type": "text", "text": "go"}], ["correct", "has-tests"])
    assert review["rules"][0]["result"] == "pass" and review["rules"][1]["result"] == "unsure"
    assert agent.usage.turns == 3 and agent.usage.input_tokens == 300
    second = msgs.calls[1]
    # assistant turn echoed with its thinking block intact, then the tool result
    assert second["messages"][1]["content"][0]["type"] == "thinking"
    assert second["messages"][2]["content"][0]["content"] == '<untrusted source="head:a.py">\n1: x = 1\n</untrusted>'
    assert msgs.calls[0]["output_config"] == {"effort": "high"}
    assert all(t["strict"] for t in msgs.calls[0]["tools"])
    assert "temperature" not in msgs.calls[0]


def test_agent_asks_once_for_missing_verdicts(tmp_path):
    ws = Workspace({"base": tmp_path, "head": tmp_path})
    partial = {"summary": "s", "rules": [verdict("correct", "pass")], "injection_detected": False, "injection_notes": ""}
    msgs = FakeMessages([resp(tool("s1", "submit_review", partial)), resp(tool("s2", "submit_review", partial))])
    logs = []
    agent = Agent(NS(messages=msgs), "m", "low", 5, ws, gh=None, upstream="o/r", log=logs.append)
    review = agent.run("sys", [], ["correct", "in-scope"])
    reply = msgs.calls[1]["messages"][-1]["content"][0]
    assert reply["tool_use_id"] == "s1" and "no verdict for in-scope" in reply["content"]
    # still incomplete the second time: accepted as is, and decide() fails the missing hard rule
    assert [r["id"] for r in review["rules"]] == ["correct"] and agent.usage.turns == 2
    assert logs == []  # submit_review is never logged


def test_agent_gives_up(tmp_path):
    ws = Workspace({"base": tmp_path, "head": tmp_path})
    msgs = FakeMessages([resp(NS(type="text", text="hmm"), stop="end_turn") for _ in range(10)])
    agent = Agent(NS(messages=msgs), "m", "low", 3, ws, gh=None, upstream="o/r", log=lambda *_: None)
    with pytest.raises(ReviewFailed):
        agent.run("sys", [], ["correct"])


# --- gate --------------------------------------------------------------------
SIGNER = "5" * 40  # a review.yml commit in the history of the gate's ref
IMPOSTER = "6" * 40  # one that only exists in a fork of gh-bouncer/action


class FakeGitHub:
    """GitHub as the gate sees it. The live pull request is the last one handed to the gate (see
    see()), with the labels, draft flag and open/closed state the gate itself left on it."""

    def __init__(self, maintainers=()):
        self.comments = {}
        self.labels = {}
        self.closed = set()
        self.drafts = {}
        self.prs = {}
        self.graphql_calls = []
        self.maintainers = set(maintainers)
        self.next_id = 1
        # compare API of the action repo: signer commit -> status against the gate's ref
        self.compare = {SIGNER: "ahead"}
        self.compare_calls = []

    def see(self, pr):
        """The pull request is now at this head/base and open (or closed). Labels and draft state
        come from the payload only the first time; after that the gate's own changes are the truth."""
        n = pr["number"]
        self.prs[n] = copy.deepcopy(pr)
        (self.closed.discard if pr.get("state") == "open" else self.closed.add)(n)
        self.labels.setdefault(n, {lb["name"] for lb in pr.get("labels", [])})
        self.drafts.setdefault(n, bool(pr.get("draft")))

    def pr(self, n):
        p = copy.deepcopy(self.prs[n])
        p.update(labels=[{"name": x} for x in sorted(self.labels.get(n, ()))], draft=self.drafts.get(n, False),
                 state="closed" if n in self.closed else "open")
        return p

    def _num(self, path, idx):
        return int(path.split("/")[idx])

    def paginate(self, path, limit=1000):
        if path.endswith("/comments"):
            return [dict(c) for c in self.comments.get(self._num(path, 5), [])]
        return []

    def get_or_none(self, path, accept=None):
        if "/collaborators/" in path:
            user = path.split("/")[5]
            return {"permission": "write" if user in self.maintainers else "read"}
        if "/issues/comments/" in path:
            cid = int(path.rsplit("/", 1)[1])
            return next((dict(c) for cs in self.comments.values() for c in cs if c["id"] == cid), None)
        return None

    def get(self, path, accept=None):
        if path.startswith("/repos/gh-bouncer/action/compare/"):
            self.compare_calls.append(path)
            status = self.compare.get(path.split("/compare/")[1].split("...")[0], "diverged")
            if isinstance(status, Exception):
                raise status
            return {"status": status}
        if path == "/repos/gh-bouncer/action":
            return {"default_branch": "main"}
        if path.startswith("/repos/up/repo/pulls/"):
            return self.pr(self._num(path, 5))
        raise AssertionError(path)

    def post(self, path, body):
        if path.endswith("/labels") and "/issues/" in path:
            self.labels.setdefault(self._num(path, 5), set()).update(body["labels"])
            return {}
        if path.endswith("/labels"):
            raise GitHubError(422, "exists")
        if path.endswith("/comments"):
            c = {"id": self.next_id, "user": {"login": "github-actions[bot]"}, "body": body["body"]}
            self.next_id += 1
            self.comments.setdefault(self._num(path, 5), []).append(c)
            return c
        raise AssertionError(path)

    def patch(self, path, body):
        if "/issues/comments/" in path:
            cid = int(path.rsplit("/", 1)[1])
            for cs in self.comments.values():
                for c in cs:
                    if c["id"] == cid:
                        c["body"] = body["body"]
            return {}
        if "/pulls/" in path and body.get("state") == "closed":
            self.closed.add(self._num(path, 5))
            return {}
        raise AssertionError(path)

    def delete(self, path):
        n = self._num(path, 5)
        self.labels.get(n, set()).discard(path.rsplit("/", 1)[1].replace("%3A", ":"))

    def graphql(self, q, v):
        self.graphql_calls.append(q)
        n = int(v["id"].split("_")[1])
        if "convertPullRequestToDraft" in q:
            self.drafts[n] = True
        elif "markPullRequestReadyForReview" in q:
            self.drafts[n] = False
        return {}

    def state(self, n):
        return parse_state(self.comments.get(n, []))[1]

    def bodies(self, n):
        return [c["body"] for c in self.comments.get(n, [])]


T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


def make_pr(n=7, sha="a" * 40, assoc="NONE", labels=(), draft=False, author="drive-by", base="main"):
    return {"number": n, "state": "open", "node_id": f"PR_{n}", "draft": draft, "author_association": assoc,
            "user": {"login": author}, "labels": [{"name": x} for x in labels],
            "head": {"sha": sha, "repo": {"full_name": "fork/repo"}},
            "base": {"ref": base, "repo": {"full_name": "up/repo", "default_branch": "main"}}}


def found(outcome, sha="a" * 40, n=7, ts=1, digest=None, protocol=REVIEW_PROTOCOL, signer=SIGNER):
    rules = [{"id": r.id, "result": "fail" if outcome == "fail" and r.id == "correct" else "pass", "confidence": 0.95,
              "reason": "breaks x", "evidence": [{"root": "head", "path": "a.py", "line": 1, "quote": "x", "verified": True}]}
             for r in config.parse("").rules]
    return Found(ts=ts, run="https://github.com/fork/repo/actions/runs/1", signer_sha=signer,
                 signer_uri="https://github.com/gh-bouncer/action/.github/workflows/review.yml@refs/tags/v1", predicate={
        "upstream": "Up/Repo", "pr": n, "head_sha": sha, "head_repo": "fork/repo", "base_sha": "b" * 40,
        "protocol": protocol, "config_digest": digest or config.parse("").digest,
        "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1, "turns": 1},
        "facts": {"linked_issues": [{"number": 1, "state": "open"}], "changed_files": [], "changed_lines": 1},
        "review": {"summary": "s", "rules": rules, "injection_detected": False, "injection_notes": ""}})


class TestGate(Gate):
    __test__ = False

    def process(self, pr, action=None, sender=None):
        if isinstance(self.gh, FakeGitHub):
            self.gh.see(pr)  # what the test hands the gate is the pull request's current head
        return super().process(pr, action=action, sender=sender)


def gate(gh, verifier=lambda r, s: [], now=T0, cfg_text="", action_ref="v1", log=lambda *_: None):
    return TestGate(gh, "up/repo", config.parse(cfg_text), verifier, now=now, log=log, action_ref=action_ref)


def test_new_pr_gets_instructions_then_passes():
    gh = FakeGitHub()
    assert gate(gh).process(make_pr(), action="opened") == "pending"
    st = gh.state(7)
    assert st["status"] == "pending" and st["rounds"] == 1
    assert "bouncer:pending" in gh.labels[7]
    assert "gh bouncer https://github.com/up/repo/pull/7" in gh.bodies(7)[0]
    assert any("convertPullRequestToDraft" in q for q in gh.graphql_calls)

    result = gate(gh, verifier=lambda r, s: [found("pass")], now=T0 + dt.timedelta(hours=1)).process(make_pr(labels=["bouncer:pending"], draft=True))
    assert result == "pass" and gh.state(7)["status"] == "pass"
    assert gh.labels[7] == {"bouncer:pass"} and 7 not in gh.closed
    assert any("Passed" in b for b in gh.bodies(7))
    assert any("markPullRequestReadyForReview" in q for q in gh.graphql_calls)


A, B = "a" * 40, "b" * 40


def test_queued_event_reads_the_pr_fresh_not_its_payload():
    gh = FakeGitHub()
    gate(gh).process(make_pr(sha=A), action="opened")
    # A push to B fires synchronize (payload: labels=[pending], draft) but its run is queued
    # behind a /bouncer check run that applies a pass for A.
    stale_payload = make_pr(sha=B, labels=["bouncer:pending"], draft=True)
    assert gate(gh, verifier=lambda r, s: [found("pass", sha=A)]).process(
        make_pr(sha=A, labels=["bouncer:pending"], draft=True)) == "pass"
    gh.see(make_pr(sha=B))  # the pull request is at B now
    before = len(gh.graphql_calls)
    gate(gh).handle("pull_request_target", {"action": "synchronize", "pull_request": stale_payload,
                                            "sender": {"login": "drive-by"}})
    assert gh.state(7)["sha"] == B and gh.state(7)["status"] == "pending"
    assert gh.labels[7] == {"bouncer:pending"}  # the pass label is gone from the unreviewed commit
    assert any("convertPullRequestToDraft" in q for q in gh.graphql_calls[before:])


def test_sweep_does_not_overwrite_a_round_started_meanwhile():
    gh = FakeGitHub()
    gate(gh).process(make_pr(sha=A), action="opened")

    def sweep_verifier(head_repo, name):
        # While the sweep (holding the PR at A) waits on `gh attestation verify`, a synchronize
        # run for B (a different concurrency group) starts a new round.
        gate(gh).process(make_pr(sha=B), action="synchronize")
        return [found("pass", sha=A)]

    assert gate(gh, verifier=sweep_verifier).process(make_pr(sha=A)) == "changed"
    st = gh.state(7)
    assert st["sha"] == B and st["status"] == "pending" and gh.labels[7] == {"bouncer:pending"}


def test_sweep_and_check_run_post_one_verdict():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")

    def sweep_verifier(head_repo, name):
        # gh bouncer's `/bouncer check` run (group bouncer-gate-7) overlaps the sweep (bouncer-gate-sweep).
        gate(gh, verifier=lambda r, s: [found("fail")]).process(make_pr())
        return [found("fail")]

    assert gate(gh, verifier=sweep_verifier).process(make_pr()) == "changed"
    assert len([b for b in gh.bodies(7) if b.startswith("### ⛔ Bouncer review")]) == 1
    assert gh.state(7)["fails"] == 1


def test_fail_closes_and_counts_round():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    assert gate(gh, verifier=lambda r, s: [found("fail")]).process(make_pr()) == "fail"
    assert 7 in gh.closed and gh.state(7)["fails"] == 1 and "bouncer:fail" in gh.labels[7]
    assert any("2 rounds left" in b for b in gh.bodies(7))


def test_only_matching_attestations_count():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    wrong = [found("pass", sha="c" * 40), found("pass", n=8)]
    assert gate(gh, verifier=lambda r, s: wrong).process(make_pr()) == "pending"


def test_review_signed_from_imposter_commit_does_not_count():
    # review.yml@<sha of a commit that only exists in a fork of gh-bouncer/action>: the signature
    # names gh-bouncer/action's review.yml, but the commit isn't in the history of the gate's ref.
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    logs = []
    imposter = found("pass", signer=IMPOSTER)
    g = gate(gh, verifier=lambda r, s: [imposter, found("fail", signer=IMPOSTER, ts=2)], log=logs.append)
    assert g.process(make_pr()) == "pending"
    st = gh.state(7)
    assert st["status"] == "pending" and st["fails"] == 0 and 7 not in gh.closed  # no valid review, not a fail
    assert any("not in the history of gh-bouncer/action@v1" in line for line in logs)
    assert gh.compare_calls == [f"/repos/gh-bouncer/action/compare/{IMPOSTER}...v1?per_page=1"]  # cached per commit
    # a real review after it counts, even though the imposter one came first
    assert gate(gh, verifier=lambda r, s: [imposter, found("fail", ts=2)]).process(make_pr()) == "fail"


@pytest.mark.parametrize("status,counts", [("ahead", True), ("identical", True), ("behind", False), ("diverged", False),
                                           (GitHubError(404, "Not Found"), False), (GitHubError(502, "Bad"), False)])
def test_signer_commit_must_be_in_gate_history(status, counts):
    gh = FakeGitHub()
    gh.compare[SIGNER] = status
    gate(gh).process(make_pr(), action="opened")
    assert gate(gh, verifier=lambda r, s: [found("pass")]).process(make_pr()) == ("pass" if counts else "pending")


def test_signer_uri_must_be_the_bouncer_review_workflow():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    other = found("pass")
    other.signer_uri = "https://github.com/fork/repo/.github/workflows/review.yml@refs/heads/main"
    assert gate(gh, verifier=lambda r, s: [other]).process(make_pr()) == "pending" and gh.compare_calls == []


def test_gate_without_a_ref_compares_against_the_default_branch():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    assert gate(gh, verifier=lambda r, s: [found("pass")], action_ref="").process(make_pr()) == "pass"
    assert gh.compare_calls == [f"/repos/gh-bouncer/action/compare/{SIGNER}...main?per_page=1"]


def test_review_with_other_settings_does_not_count():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    old = found("fail", digest="sha256:" + "0" * 64)
    assert gate(gh, verifier=lambda r, s: [old]).process(make_pr()) == "pending"
    st, body = gh.state(7), gh.bodies(7)[0]
    assert st["status"] == "pending" and st["fails"] == 0 and 7 not in gh.closed
    assert "changed the bouncer settings after it ran" in body and "gh bouncer https://github.com/up/repo/pull/7" in body
    assert len(gh.bodies(7)) == 1  # said in the instructions comment, not a new one
    # the run after the settings changed counts, even though the stale one was first
    assert gate(gh, verifier=lambda r, s: [old, found("pass", ts=2)]).process(make_pr()) == "pass"


def test_review_from_outdated_version_does_not_count():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    old = found("fail")
    del old.predicate["protocol"]  # written before protocols existed
    for stale in ([old], [found("fail", protocol=REVIEW_PROTOCOL - 1)], [found("fail", protocol="x")]):
        assert gate(gh, verifier=lambda r, s: stale).process(make_pr()) == "pending"
    st = gh.state(7)
    assert st["status"] == "pending" and st["fails"] == 0 and st["stale"] == "protocol"
    assert "outdated version of the bouncer review" in gh.bodies(7)[0] and len(gh.bodies(7)) == 1
    assert gate(gh, verifier=lambda r, s: [old, found("pass", ts=2)]).process(make_pr()) == "pass"


def test_settings_digest_ignores_gate_only_settings():
    gh = FakeGitHub()
    cfg = "gate: {deadline_hours: 72}\nchecks: {max_changed_lines: 500}"
    gate(gh, cfg_text=cfg).process(make_pr(), action="opened")
    assert gate(gh, verifier=lambda r, s: [found("pass")], cfg_text=cfg).process(make_pr()) == "pass"


def test_deadline_expires():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    assert gate(gh, now=T0 + dt.timedelta(hours=49)).process(make_pr()) == "expired"
    assert 7 in gh.closed


def test_exempt_authors_untouched():
    gh = FakeGitHub()
    assert gate(gh).process(make_pr(assoc="MEMBER")) == "exempt"
    assert gate(gh).process(make_pr(author="dependabot[bot]")) == "exempt"
    assert gate(gh).process(make_pr(labels=["bouncer:skip"])) == "exempt"
    assert gh.comments == {}


def test_reopen_same_commit():
    gh = FakeGitHub(maintainers={"maint"})
    gate(gh).process(make_pr(), action="opened")
    gate(gh, verifier=lambda r, s: [found("fail")]).process(make_pr())
    gh.closed.clear()
    assert gate(gh).process(make_pr(), action="reopened", sender="drive-by") == "reclosed"
    assert 7 in gh.closed
    assert gate(gh).process(make_pr(), action="reopened", sender="maint") == "override"


def test_new_commit_after_fail_starts_new_round_until_exhausted():
    gh = FakeGitHub()
    cfg = "gate: {max_attempts: 2}"
    gate(gh, cfg_text=cfg).process(make_pr(), action="opened")
    gate(gh, verifier=lambda r, s: [found("fail")], cfg_text=cfg).process(make_pr())
    assert gate(gh, cfg_text=cfg).process(make_pr(sha="d" * 40), action="reopened", sender="drive-by") == "pending"
    assert gh.state(7)["rounds"] == 2
    gate(gh, verifier=lambda r, s: [found("fail", sha="d" * 40)], cfg_text=cfg).process(make_pr(sha="d" * 40))
    assert gate(gh, cfg_text=cfg).process(make_pr(sha="e" * 40), action="reopened", sender="drive-by") == "exhausted"


def no_review_lookup(head_repo, name):
    raise AssertionError("looked for a review")


def test_wrong_base_branch_bounced_before_review():
    gh = FakeGitHub()
    assert gate(gh, verifier=no_review_lookup).process(make_pr(base="dev"), action="opened") == "wrong-base"
    assert 7 in gh.closed and gh.labels[7] == {"bouncer:fail"}
    body = gh.bodies(7)[0]
    assert "targets `dev`" in body and "into `main`" in body and "gh bouncer" not in body
    st = gh.state(7)
    assert st["status"] == "wrong_base" and st["fails"] == 0  # no review, no round used
    # reopened without fixing it: closed again, explained only once
    gh.closed.clear()
    assert gate(gh, verifier=no_review_lookup).process(make_pr(base="dev"), action="reopened", sender="drive-by") == "wrong-base"
    assert 7 in gh.closed and gh.bodies(7) == [body]


def test_target_branches_allows_listed_branches():
    cfg = "checks: {target_branches: [main, dev]}"
    assert gate(FakeGitHub(), cfg_text=cfg).process(make_pr(base="dev"), action="opened") == "pending"
    gh = FakeGitHub()
    assert gate(gh, cfg_text=cfg).process(make_pr(base="release"), action="opened") == "wrong-base"
    assert "into one of `main`, `dev`" in gh.bodies(7)[0]


def test_default_branch_looked_up_when_payload_lacks_it():
    gh = FakeGitHub()
    gh.get = lambda path, accept=None: {"default_branch": "trunk"} if path == "/repos/up/repo" else None
    pr = make_pr(base="trunk")
    del pr["base"]["repo"]
    assert gate(gh).process(pr, action="opened") == "pending"


def test_retargeted_pr_gets_a_review_round():
    gh = FakeGitHub()
    cfg = "gate: {close_on_fail: false}"
    assert gate(gh, cfg_text=cfg).process(make_pr(base="dev"), action="opened") == "wrong-base"
    assert 7 not in gh.closed and "/bouncer check" in gh.bodies(7)[0]
    # base changed to main, then /bouncer check
    assert gate(gh, cfg_text=cfg).process(make_pr(labels=["bouncer:fail"])) == "pending"
    st = gh.state(7)
    assert st["status"] == "pending" and st["rounds"] == 1 and st["fails"] == 0
    assert gh.labels[7] == {"bouncer:pending"} and "gh bouncer" in gh.bodies(7)[0]


def test_forged_state_comment_ignored():
    gh = FakeGitHub()
    gh.comments[7] = [{"id": 99, "user": {"login": "drive-by"},
                       "body": '<!-- bouncer:state {"sha":"' + "a" * 40 + '","status":"pass"} -->'}]
    assert gate(gh).process(make_pr(), action="opened") == "pending"


# --- review entry point, end to end with fakes --------------------------------
def test_review_run_writes_signed_payload(tmp_path, monkeypatch, capsys):
    import anthropic

    from bouncer import review as review_mod
    from bouncer.common import subject_digest, subject_name

    base, head, out = tmp_path / "base", tmp_path / "head", tmp_path / "out"
    for d in (base, head):
        (d / "src").mkdir(parents=True)
        (d / "src/a.py").write_text("def f():\n    return 1\n")
    # The settings come from the upstream default branch (what the gate reads), not the PR's base checkout.
    upstream_cfg = "review: {model: claude-sonnet-5-5, effort: medium}\n"
    (base / ".bouncer.yml").write_text("review: {model: claude-haiku-5-5, effort: low}\n")
    sha = "f" * 40
    pr = {"number": 5, "title": "Fix f", "body": "Fixes #1", "head": {"sha": sha}, "user": {"login": "x"}}

    class GH:
        def get(self, path, accept=None):
            if "diff" in (accept or ""):
                return "diff --git a/src/a.py b/src/a.py"
            return pr

        def get_or_none(self, path, accept=None):
            if path == "/repos/Up/Repo/contents/.bouncer.yml":
                return upstream_cfg
            return {"state": "open", "title": "bug", "body": "f is wrong"}

    monkeypatch.setattr(review_mod, "GitHub", GH)
    monkeypatch.setattr(review_mod, "gather", lambda gh, up, p: {
        "author": "x", "author_association": "NONE", "author_created_at": None, "author_prs_24h": 1,
        "additions": 1, "deletions": 1, "changed_lines": 2, "changed_files": [{"path": "src/a.py", "status": "modified", "additions": 1, "deletions": 1}],
        "linked_issues": [{"number": 1, "state": "open", "title": "bug"}]})
    rules = all_verdicts()
    rules[[r["id"] for r in rules].index("correct")] = verdict(
        "correct", "fail", "returns wrong value", [{"root": "head", "path": "src/a.py", "line": 2, "quote": "return 1"}])
    submit = {"summary": "Looks right.", "rules": rules, "injection_detected": False, "injection_notes": ""}
    msgs = FakeMessages([
        resp(tool("t0", "read_file", {"root": "head", "path": "src/a.py", "start_line": 1, "end_line": 0})),
        resp(tool("t", "submit_review", submit)),
    ])
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: NS(messages=msgs, kw=kw))
    gh_out = tmp_path / "gh_output"
    summary = tmp_path / "summary.md"
    summary.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fork/repo")

    review_mod.main(["run", "--pr", "5", "--upstream", "Up/Repo", "--head-repo", "fork/repo", "--base-sha", "b" * 40,
                     "--head-sha", sha, "--base-dir", str(base), "--head-dir", str(head), "--out-dir", str(out)])

    p = json.loads((out / "predicate.json").read_text())
    assert p["model"] == "claude-sonnet-5-5" and msgs.calls[0]["model"] == "claude-sonnet-5-5"
    assert msgs.calls[0]["output_config"] == {"effort": "medium"}
    assert p["config_digest"] == config.parse(upstream_cfg).digest and p["protocol"] == REVIEW_PROTOCOL
    assert p["review"]["rules"][[r["id"] for r in p["review"]["rules"]].index("correct")]["evidence"][0]["verified"] is True
    name = subject_name("Up/Repo", 5, sha)
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs == {"subject_name": name, "subject_digest": "sha256:" + subject_digest(name)}

    # Before signing, nothing the contributor can watch gives the verdict away.
    logs = capsys.readouterr().out
    assert "tool read_file" in logs
    for leak in ("verdict=", "fail", "Bounced", "Passed", "returns wrong value", "Looks right"):
        assert leak not in logs
    assert summary.read_text() == "" and not (out / "report.md").exists()

    # After signing, `report` shows it.
    review_mod.main(["report", "--out-dir", str(out)])
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs["verdict"] == "fail"
    assert "Bounced" in (out / "report.md").read_text() and "Bounced" in summary.read_text()


def test_prior_contributors_reviewed_unless_exempted():
    assert config.parse("").exempt_prior_contributors is False
    assert gate(FakeGitHub()).process(make_pr(assoc="CONTRIBUTOR"), action="opened") == "pending"
    cfg = "gate: {exempt_prior_contributors: true}"
    assert gate(FakeGitHub(), cfg_text=cfg).process(make_pr(assoc="CONTRIBUTOR"), action="opened") == "exempt"


def test_maintainers_can_be_reviewed_when_exemption_off():
    gh = FakeGitHub()
    assert gate(gh, cfg_text="gate: {exempt_maintainers: false}").process(make_pr(assoc="OWNER"), action="opened") == "pending"


def test_reopen_after_expiry_starts_new_round():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    gate(gh, now=T0 + dt.timedelta(hours=49)).process(make_pr())
    later = T0 + dt.timedelta(hours=50)
    assert gate(gh, now=later).process(make_pr(), action="reopened", sender="drive-by") == "pending"
    st = gh.state(7)
    assert st["rounds"] == 2 and st["fails"] == 1


def test_instructions_are_one_command():
    gh = FakeGitHub()
    gate(gh).process(make_pr(), action="opened")
    body = gh.bodies(7)[0]
    assert "gh extension install gh-bouncer/gh-bouncer" in body
    assert "gh bouncer https://github.com/up/repo/pull/7" in body


def test_action_identity_from_runner_path():
    from bouncer.gate import action_identity

    assert action_identity("/home/runner/work/_actions/gh-bouncer/action/v1") == ("gh-bouncer/action", "v1")
    assert action_identity("/home/runner/work/_actions/gh-bouncer/action/feature/x") == ("gh-bouncer/action", "feature/x")
    assert action_identity("/somewhere/else") == ("", "")


# --- resolve: manual vs automatic runs -------------------------------------------
class ResolveGH:
    def __init__(self, prs, pr_by_number=None, fork=True):
        self.prs, self.by_n, self.fork, self.calls = prs, pr_by_number or {}, fork, []

    def get(self, path, accept=None):
        self.calls.append(path)
        if path == "/repos/me/repo":
            return {"fork": self.fork, "parent": {"full_name": "up/repo"}}
        if "/pulls?" in path:
            return self.prs
        n = int(path.rsplit("/", 1)[1])
        return self.by_n[n].pop(0) if isinstance(self.by_n[n], list) else self.by_n[n]

    def get_or_none(self, path, accept=None):
        try:
            return self.get(path)
        except KeyError:
            return None


def _pr(n=3, sha="s1", state="open", head="me/repo"):
    return {"number": n, "state": state, "head": {"sha": sha, "repo": {"full_name": head}}, "base": {"sha": "b"}}


def run_resolve(monkeypatch, tmp_path, gh, event, pr_arg="", key="k", sha="s1", ref="feature"):
    from bouncer import review as review_mod

    monkeypatch.setattr(review_mod, "GitHub", lambda: gh)
    out = tmp_path / "out"
    out.write_text("")
    for k, v in {"GITHUB_OUTPUT": str(out), "GITHUB_REPOSITORY": "me/repo", "GITHUB_EVENT_NAME": event,
                 "ANTHROPIC_API_KEY": key, "GITHUB_SHA": sha, "GITHUB_REF_NAME": ref}.items():
        monkeypatch.setenv(k, v)
    code = 0
    try:
        review_mod.cmd_resolve(NS(pr=pr_arg), sleep=lambda s: None)
    except SystemExit as e:
        code = e.code
    return code, dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def test_resolve_push_without_key_or_pr_is_quiet(monkeypatch, tmp_path):
    assert run_resolve(monkeypatch, tmp_path, ResolveGH([_pr()]), "push", key="") == (0, {"skip": "true"})
    assert run_resolve(monkeypatch, tmp_path, ResolveGH([]), "push") == (0, {"skip": "true"})
    # manual runs explain the problem instead
    assert run_resolve(monkeypatch, tmp_path, ResolveGH([]), "workflow_dispatch")[0] == 1
    assert run_resolve(monkeypatch, tmp_path, ResolveGH([_pr()]), "workflow_dispatch", key="")[0] == 1


def test_resolve_finds_pr_from_branch(monkeypatch, tmp_path):
    gh = ResolveGH([_pr(n=9)])
    code, out = run_resolve(monkeypatch, tmp_path, gh, "workflow_dispatch")
    assert code == 0 and out["pr"] == "9" and out["skip"] == "false" and out["upstream"] == "up/repo"
    assert any("head=me:feature" in c for c in gh.calls)


def test_resolve_push_waits_for_pr_to_catch_up(monkeypatch, tmp_path):
    gh = ResolveGH([_pr(sha="old")], {3: [_pr(sha="old"), _pr(sha="new")]})
    code, out = run_resolve(monkeypatch, tmp_path, gh, "push", sha="new")
    assert code == 0 and out["head_sha"] == "new"
    gh = ResolveGH([_pr(sha="old")], {3: _pr(sha="old")})
    assert run_resolve(monkeypatch, tmp_path, gh, "push", sha="new")[1] == {"skip": "true"}
