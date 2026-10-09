"""Agent loop with a fake Anthropic client, and gate flows with a fake GitHub."""
import datetime as dt
import json
from types import SimpleNamespace as NS

import pytest

from bouncer import config
from bouncer.agent import Agent, ReviewFailed, Workspace
from bouncer.common import GitHubError
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


def test_agent_loop(tmp_path):
    (tmp_path / "head").mkdir()
    (tmp_path / "head/a.py").write_text("x = 1\n")
    ws = Workspace({"base": tmp_path / "head", "head": tmp_path / "head"})
    submit = {"summary": "ok", "rules": [{"id": "correct", "result": "pass", "confidence": 0.9, "reason": "fine", "evidence": []}],
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
    assert second["messages"][2]["content"][0]["content"] == "1: x = 1"
    assert msgs.calls[0]["output_config"] == {"effort": "high"}
    assert all(t["strict"] for t in msgs.calls[0]["tools"])
    assert "temperature" not in msgs.calls[0]


def test_agent_gives_up(tmp_path):
    ws = Workspace({"base": tmp_path, "head": tmp_path})
    msgs = FakeMessages([resp(NS(type="text", text="hmm"), stop="end_turn") for _ in range(10)])
    agent = Agent(NS(messages=msgs), "m", "low", 3, ws, gh=None, upstream="o/r", log=lambda *_: None)
    with pytest.raises(ReviewFailed):
        agent.run("sys", [], ["correct"])


# --- gate --------------------------------------------------------------------
class FakeGitHub:
    def __init__(self, maintainers=()):
        self.comments = {}
        self.labels = {}
        self.closed = set()
        self.graphql_calls = []
        self.maintainers = set(maintainers)
        self.next_id = 1

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
        return None

    def get(self, path, accept=None):
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
        self.graphql_calls.append(q.split("(")[1].split("{")[-1] if False else q)
        return {}

    def state(self, n):
        return parse_state(self.comments.get(n, []))[1]

    def bodies(self, n):
        return [c["body"] for c in self.comments.get(n, [])]


T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


def make_pr(n=7, sha="a" * 40, assoc="NONE", labels=(), draft=False, author="drive-by"):
    return {"number": n, "state": "open", "node_id": "PR_x", "draft": draft, "author_association": assoc,
            "user": {"login": author}, "labels": [{"name": x} for x in labels],
            "head": {"sha": sha, "repo": {"full_name": "fork/repo"}}}


def found(outcome, sha="a" * 40, n=7, ts=1):
    rules = [{"id": "correct", "result": "fail" if outcome == "fail" else "pass", "confidence": 0.95, "reason": "breaks x",
              "evidence": [{"root": "head", "path": "a.py", "line": 1, "quote": "x", "verified": True}]}]
    return Found(ts=ts, run="https://github.com/fork/repo/actions/runs/1", predicate={
        "upstream": "Up/Repo", "pr": n, "head_sha": sha, "head_repo": "fork/repo", "base_sha": "b" * 40,
        "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1, "turns": 1},
        "facts": {"linked_issues": [{"number": 1, "state": "open"}], "changed_files": [], "changed_lines": 1},
        "review": {"summary": "s", "rules": rules, "injection_detected": False, "injection_notes": ""}})


def gate(gh, verifier=lambda r, s: [], now=T0, cfg_text=""):
    return Gate(gh, "up/repo", config.parse(cfg_text), verifier, now=now, log=lambda *_: None)


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


def test_forged_state_comment_ignored():
    gh = FakeGitHub()
    gh.comments[7] = [{"id": 99, "user": {"login": "drive-by"},
                       "body": '<!-- bouncer:state {"sha":"' + "a" * 40 + '","status":"pass"} -->'}]
    assert gate(gh).process(make_pr(), action="opened") == "pending"


# --- review entry point, end to end with fakes --------------------------------
def test_review_run_writes_signed_payload(tmp_path, monkeypatch):
    import anthropic

    from bouncer import review as review_mod
    from bouncer.common import subject_digest, subject_name

    base, head, out = tmp_path / "base", tmp_path / "head", tmp_path / "out"
    for d in (base, head):
        (d / "src").mkdir(parents=True)
        (d / "src/a.py").write_text("def f():\n    return 1\n")
    (base / ".bouncer.yml").write_text("review: {model: claude-sonnet-5-5, effort: medium}\n")
    sha = "f" * 40
    pr = {"number": 5, "title": "Fix f", "body": "Fixes #1", "head": {"sha": sha}, "user": {"login": "x"}}

    class GH:
        def get(self, path, accept=None):
            if "diff" in (accept or ""):
                return "diff --git a/src/a.py b/src/a.py"
            return pr

        def get_or_none(self, path, accept=None):
            return {"state": "open", "title": "bug", "body": "f is wrong"}

    monkeypatch.setattr(review_mod, "GitHub", GH)
    monkeypatch.setattr(review_mod, "gather", lambda gh, up, p: {
        "author": "x", "author_association": "NONE", "author_created_at": None, "author_prs_24h": 1,
        "additions": 1, "deletions": 1, "changed_lines": 2, "changed_files": [{"path": "src/a.py", "status": "modified", "additions": 1, "deletions": 1}],
        "linked_issues": [{"number": 1, "state": "open", "title": "bug"}]})
    submit = {"summary": "Looks right.", "rules": [
        {"id": "correct", "result": "fail", "confidence": 0.9, "reason": "returns wrong value",
         "evidence": [{"root": "head", "path": "src/a.py", "line": 2, "quote": "return 1"}]}],
        "injection_detected": False, "injection_notes": ""}
    msgs = FakeMessages([resp(tool("t", "submit_review", submit))])
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: NS(messages=msgs, kw=kw))
    gh_out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fork/repo")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    review_mod.main(["run", "--pr", "5", "--upstream", "Up/Repo", "--head-repo", "fork/repo", "--base-sha", "b" * 40,
                     "--head-sha", sha, "--base-dir", str(base), "--head-dir", str(head), "--out-dir", str(out)])

    p = json.loads((out / "predicate.json").read_text())
    assert p["model"] == "claude-sonnet-5-5" and msgs.calls[0]["model"] == "claude-sonnet-5-5"
    assert msgs.calls[0]["output_config"] == {"effort": "medium"}
    assert p["review"]["rules"][[r["id"] for r in p["review"]["rules"]].index("correct")]["evidence"][0]["verified"] is True
    name = subject_name("Up/Repo", 5, sha)
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert outputs["subject_name"] == name and outputs["subject_digest"] == "sha256:" + subject_digest(name)
    assert outputs["verdict"] == "fail"
    assert "Bounced" in (out / "report.md").read_text()


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
