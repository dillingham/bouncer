"""Markdown for PR comments. All model- and contributor-sourced text goes through clean()."""
from __future__ import annotations

import datetime as dt
import json
import re

from .common import inert_json, quote_path
from .config import Config
from .decide import Decision, kind, pct, rule_of

STATE_MARKER = "<!-- bouncer:state "
BOT_LOGIN = "github-actions[bot]"


def clean(text: str, limit: int = 1500) -> str:
    """Neutralize mentions, HTML comments/tags and references so model text can't ping people,
    hide content, or forge a bouncer state marker."""
    t = (text or "")[:limit]
    t = t.replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"@(?=[A-Za-z0-9])", "@​", t)
    t = re.sub(r"(?<![\w&])#(?=\d)", "#​", t)
    return t.strip()


def plural(k: int, word: str) -> str:
    return f"{k} {word}{'' if k == 1 else 's'}"


# GitHub refuses comments over 65,536 characters. The report stays well under that, leaving
# room for the gate's own note below it.
MAX_REPORT = 60_000


def _evidence_links(r: dict, blob: str, base_blob: str, most: int = 3) -> str:
    """Links to the first few pieces of verified evidence of a rule verdict (unverified quotes
    aren't shown)."""
    out = []
    for e in [e for e in r.get("evidence", []) if e.get("verified")][:most]:
        base = blob if e.get("root") == "head" else base_blob
        path = clean(e.get("path", ""), 300).replace("`", "'")
        out.append(f"[`{path}:{e.get('line')}`]({base}/{quote_path(e.get('path', ''))}#L{int(e.get('line') or 0)})")
    return " ".join(out)


def _fit(items: list[str], budget: int, more: str) -> list[str]:
    """The items (one line each) that fit in budget characters, then a `more` line ({n} = how
    many were left out) if any didn't. The result never takes more than budget characters."""
    out: list[str] = []
    used = 0
    for i, item in enumerate(items):
        rest = len(items) - i - 1
        # room for this item, and for the "more" line if anything after it might not fit
        reserve = len(more.format(n=rest)) + 1 if rest else 0
        if used + len(item) + 1 + reserve > budget:
            line = more.format(n=len(items) - i)
            return out + [line] if used + len(line) + 1 <= budget else out
        out.append(item)
        used += len(item) + 1
    return out


def review_markdown(p: dict, d: Decision, cfg: Config, server: str = "https://github.com", next_steps: str = "",
                    limit: int = MAX_REPORT) -> str:
    """The review report: verdict, summary, why it bounced (with evidence), next steps for the
    contributor, notes for the maintainer, and every Agent Rule, Required ones first.

    It always fits in `limit` characters with its footer and closed tags: the reasons get the
    room first, then the notes, then the rule list; whatever doesn't fit is counted instead."""
    review = p.get("review", {})
    blob = f"{server}/{p['head_repo']}/blob/{p['head_sha']}"
    base_blob = f"{server}/{p['upstream']}/blob/{p['base_sha']}"
    by_id = {r.get("id"): r for r in review.get("rules", [])}
    passed = d.outcome == "pass"

    why = []
    for reason in d.reasons:
        links = _evidence_links(by_id.get(rule_of(reason)) or {}, blob, base_blob)
        why.append(f"- {clean(reason)}" + (f" {links}" if links else ""))
    notes = [f"- {clean(f)}" for f in d.flags]
    failed = {rule_of(x) for x in d.reasons}
    counts = {"failed": 0, "noted": 0, "unsure": 0, "passed": 0}
    rows = []
    for rule in sorted(cfg.rules, key=lambda r: not r.hard):
        r = by_id.get(rule.id) or {}
        res = {"pass": "passed", "unsure": "unsure"}.get(r.get("result"), "failed" if rule.id in failed else "noted")
        counts[res] += 1
        mark = {"passed": "✅", "failed": "❌", "noted": "⚠️", "unsure": "❔"}[res]
        detail = f" · {pct(float(r.get('confidence', 0) or 0))}: {clean(r.get('reason', ''))}" if r else ": No verdict."
        links = _evidence_links(r, blob, base_blob)
        rows.append(f"- {mark} `{rule.id}` · {kind(rule.hard)}{detail}" + (f" {links}" if links else ""))
    u = p.get("usage", {})
    tokens_in = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    run_url = p.get("run_url", "")
    footer = (f"<sub>Reviewed `{p['head_sha'][:7]}` with {clean(p.get('model', ''), 60)} at "
              f"{clean(p.get('effort') or 'high', 10)} effort on the contributor's API key · {tokens_in:,} input and "
              f"{u.get('output_tokens', 0):,} output tokens · [signed run]({run_url})</sub>")

    def assemble(why: list[str], notes: list[str], rows: list[str]) -> list[str]:
        out = [f"### {'✅' if passed else '⛔'} Bouncer review: {'Passed' if passed else 'Bounced'}", ""]
        if review.get("summary"):
            out += ["\n".join(f"> {line}" for line in clean(review["summary"], 1000).splitlines()), ""]
        if d.reasons:
            out += ["**Why it was bounced**", ""] + why + [""]
        if next_steps:
            out += [next_steps, ""]
        if d.flags:
            # On a pass the maintainer is the reader; on a bounce, the contributor first.
            if passed:
                out += ["**For the maintainer** (these didn't fail the pull request)", ""] + notes + [""]
            else:
                out += [f"<details><summary>Also noted: {len(d.flags)}</summary>", ""] + notes + ["", "</details>", ""]
        summary = " · ".join(f"{v} {k}" for k, v in counts.items() if v)
        out += [f"<details><summary>Agent Rules: {summary}</summary>", ""] + rows + ["", "</details>", "", footer]
        return out

    budget = limit - len("\n".join(assemble([], [], [])))
    more = f"- …and {{n}} more, left out to fit GitHub's comment size limit. The [signed run]({run_url}) has the full review."
    fitted = []
    for items in (why, notes, rows):
        fitted.append(_fit(items, budget, more))
        budget -= sum(len(x) + 1 for x in fitted[-1])
    return "\n".join(assemble(*fitted))


def state_block(state: dict) -> str:
    """The state as JSON in an HTML comment. <, > and & are escaped inside the JSON, so no value
    (a review reason, say) can end the comment early or forge another marker."""
    return f"{STATE_MARKER}{inert_json(state, separators=(',', ':'))} -->"


def parse_state(comments: list[dict]) -> tuple[dict | None, dict | None]:
    """Find the bouncer state comment. Only comments by the Actions bot count, so a
    contributor cannot forge state by pasting a marker into their own comment."""
    for c in comments:
        user = c.get("user") or {}
        if user.get("login") != BOT_LOGIN:
            continue
        body = c.get("body") or ""
        i = body.find(STATE_MARKER)
        if i < 0:
            continue
        j = body.find(" -->", i)
        try:
            return c, json.loads(body[i + len(STATE_MARKER): j])
        except (ValueError, TypeError):
            return c, None
    return None, None


def find_state(gh, repo: str, n: int) -> tuple[dict | None, dict | None]:
    """The bouncer state comment of a pull request, and its state (see parse_state).

    It's usually among the first comments, so the first page is read first. On a busy pull
    request it can be anywhere (say, the bouncer was installed long after it opened), so the
    rest is read from the newest page back, without a page limit: missing the comment would
    start a new review round, and post another state comment, on every run."""
    url = f"/repos/{repo}/issues/{n}/comments?per_page=100"
    items, links = gh.page(url)
    sticky, state = parse_state(items)
    m = re.search(r"[?&]page=(\d+)", links.get("last", ""))
    if sticky or not m:
        return sticky, state
    for p in range(int(m.group(1)), 1, -1):
        sticky, state = parse_state(gh.page(f"{url}&page={p}")[0])
        if sticky:
            return sticky, state
    return None, None


# Why a signed review of the current commit doesn't count (see Gate._stale), for the contributor.
STALE_NOTES = {
    "config": "Your signed review doesn't count: the maintainers changed the bouncer settings after it ran. "
              "Run the review again with the command below. This doesn't use up a review attempt.",
    "protocol": "Your signed review doesn't count: it was made with an outdated version of the bouncer review. "
                "Run the review again with the command below. If this note comes back, sync your fork's default "
                "branch with this repository first. This doesn't use up a review attempt.",
    "signer": "Your signed review doesn't count: it was made by a version of the bouncer review that this project's "
              "bouncer doesn't accept. Run the review again with the command below, which syncs your fork's default "
              "branch with this repository first. If this note comes back, let the maintainers know: their bouncer may "
              "be pinned to an older version than the review it runs. This doesn't use up a review attempt.",
}


def fmt_deadline(t: dt.datetime) -> str:
    """A deadline people can read at a glance: Sun Oct 11, 12:00 UTC."""
    return f"{t:%a %b} {t.day}, {t:%H:%M} UTC"


def instructions(pr: int, head_repo: str, upstream: str, deadline: str, attempts_left: int, cfg: Config,
                 server: str = "https://github.com", note: str = "") -> str:
    """The comment asking the contributor to run the review: the command first, the details
    (how it works, what it checks) collapsed below."""
    url = f"{server}/{upstream}/pull/{pr}"
    checks = []
    if cfg.require_linked_issue:
        checks.append("An open issue is linked, for example `Fixes #123` in the description.")
    if cfg.max_changed_lines:
        checks.append(f"At most {cfg.max_changed_lines:,} changed lines.")
    if cfg.forbidden_paths:
        checks.append("No changes to " + ", ".join(f"`{p}`" for p in cfg.forbidden_paths) + ".")
    if cfg.max_author_prs_24h:
        checks.append(f"At most {cfg.max_author_prs_24h} pull requests opened across GitHub in the last 24 hours.")
    lines = ["### 🚪 Bouncer review required", ""]
    if note:
        lines += [f"> ⚠️ {note}", ""]
    lines += [
        "Thanks for the pull request! Before a maintainer looks at it, this project asks outside contributors "
        "to run an AI review of their change. **It runs in your fork's GitHub Actions, on your own Anthropic API key**, "
        "so maintainers pay nothing.",
        "",
        "```",
        "gh extension install gh-bouncer/gh-bouncer",
        f"gh bouncer {url}",
        "```",
        "",
        f"**Deadline:** {deadline} · {plural(attempts_left, 'review attempt')} left",
        "",
        "<details><summary>How it works</summary>",
        "",
        f"- [`gh bouncer`](https://gh-bouncer.com) turns on Actions in your fork, asks for your Anthropic API key once "
        f"(saved only as an Actions secret in `{head_repo}`), runs the review there and reports back here.",
        f"- The review uses {cfg.model} at {cfg.effort} effort, billed to your key. "
        "Your code is checked out read only and never run.",
        "- GitHub signs the result. Only the first review of each commit counts.",
        "- Once your key is saved, every push to this pull request starts a new review automatically.",
        "- No review by the deadline closes the pull request. You can reopen it and run the review then.",
        "",
        "</details>",
        "",
        "<details><summary>What the review checks</summary>",
        "",
    ]
    if checks:
        lines += ["**Pre-checks**, checked in code before the review:", ""] + [f"- {c}" for c in checks] + [""]
    lines += ["**Agent Rules.** A Required rule can bounce the pull request; an Advisory one is only reported "
              "to the maintainers.", ""]
    lines += [f"- `{r.id}` ({kind(r.hard)}): {clean(r.description, 300)}" for r in sorted(cfg.rules, key=lambda r: not r.hard)]
    lines += ["", "</details>"]
    return "\n".join(lines)


DONT_FORCE_PUSH = ("Don't force-push while it's closed: GitHub won't reopen a pull request whose branch was "
                   "force-pushed.")
DISAGREE = "If you think the review got it wrong, say so in a comment."
MAINTAINER_CAN_REOPEN = "A maintainer can still reopen it if they'd like to take a look."


def next_steps(url: str, left: int, closed: bool = True) -> str:
    """What a contributor can do after a bounce, for the report and the state comment."""
    if left <= 0:
        return " ".join(["No review attempts left."] + ([MAINTAINER_CAN_REOPEN] if closed else []) + [DISAGREE])
    if closed:
        return (f"**To try again** ({plural(left, 'review attempt')} left): push your fixes as new commits, reopen this "
                f"pull request, then run `gh bouncer {url}`. {DONT_FORCE_PUSH} {DISAGREE}")
    return (f"**To try again** ({plural(left, 'review attempt')} left): push your fixes as new commits, then run "
            f"`gh bouncer {url}`. {DISAGREE}")


# What the state comment says, by situation (above the state block). {read} and {why} link the
# review report when there is one.
STATUS_TEXTS = {
    "pass": "✅ **Passed.** Ready for a maintainer.{read}",
    "kept_pass": "✅ **Passed** on an earlier commit. This project doesn't re-review later commits.{read}",
    "fail_closed": "⛔ **Bounced** and closed.{why}\n\n{steps}",
    "fail_open": "⛔ **Bounced.**{why} Left open for a maintainer to confirm.\n\n{steps}",
    "expired": "No signed review arrived by the deadline ({deadline}), so this pull request was closed. "
               "To try again, reopen it and run `gh bouncer {url}` ({left_text} left).",
    "expired_last": "No signed review arrived by the deadline ({deadline}), so this pull request was closed. "
                    f"It has no review attempts left. {MAINTAINER_CAN_REOPEN}",
    "exhausted": "This pull request has no review attempts left (this project allows {allowed}), so it was "
                 f"closed. {MAINTAINER_CAN_REOPEN}",
    "exhausted_open": "This pull request has no review attempts left (this project allows {allowed}), so new "
                      "commits aren't reviewed. Left open for a maintainer to decide.",
    "reclosed": "This commit was already reviewed and bounced, so the pull request was closed again. Push your "
                "fixes as new commits first, then reopen it and run `gh bouncer {url}`.",
    "override": "A maintainer reopened this pull request, so the bounce no longer applies. The bouncer won't "
                "review later commits either.",
    "no_fork": "This pull request's fork was deleted, so it can't be reviewed or merged. Closing it. "
               "To send this change again, open a new pull request from a fork.",
    "draft": "This pull request is a draft. Once it's marked ready for review, the bouncer asks for a review here.",
}


def status_text(kind: str, report: str = "", **kw) -> str:
    """A state comment text from STATUS_TEXTS, with the review report linked when there is one."""
    read = f" [Read the review]({report})" if report else ""
    why = f" [See why]({report})" if report else ""
    return "### 🚪 Bouncer\n\n" + STATUS_TEXTS[kind].format(read=read, why=why, **kw)
