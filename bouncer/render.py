"""Markdown for PR comments. All model- and contributor-sourced text goes through clean()."""
from __future__ import annotations

import json
import re

from .common import quote_path
from .decide import Decision

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


def _cost_line(usage: dict) -> str:
    return (
        f"{usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0):,} input "
        f"and {usage.get('output_tokens', 0):,} output tokens over {usage.get('turns', 0)} turns"
    )


def review_markdown(p: dict, d: Decision, server: str = "https://github.com") -> str:
    icon = "✅" if d.outcome == "pass" else "⛔"
    title = "Passed" if d.outcome == "pass" else "Bounced"
    review = p.get("review", {})
    blob = f"{server}/{p['head_repo']}/blob/{p['head_sha']}"
    base_blob = f"{server}/{p['upstream']}/blob/{p['base_sha']}"
    out = [f"### {icon} Bouncer review: {title}", ""]
    if review.get("summary"):
        out += [clean(review["summary"], 2000), ""]
    if d.reasons:
        out += ["**Why it was bounced**", ""] + [f"- {clean(r)}" for r in d.reasons] + [""]
    if d.flags:
        out += ["**Notes for the maintainer**", ""] + [f"- {clean(f)}" for f in d.flags] + [""]

    out += ["<details><summary>Rule by rule</summary>", ""]
    for r in review.get("rules", []):
        mark = {"pass": "✅", "fail": "❌", "unsure": "❔"}.get(r.get("result"), "❔")
        out.append(f"- {mark} **{clean(r.get('id', ''), 60)}** ({r.get('confidence', 0):.2f}): {clean(r.get('reason', ''))}")
        for e in r.get("evidence", []):
            if not e.get("verified"):
                continue
            base = blob if e.get("root") == "head" else base_blob
            path = clean(e.get("path", ""), 300).replace("`", "'")
            out.append(f"  - [`{path}:{e.get('line')}`]({base}/{quote_path(e.get('path', ''))}#L{int(e.get('line') or 0)})")
    out += ["", "</details>", ""]
    usage = p.get("usage", {})
    out.append(
        f"<sub>Reviewed `{p['head_sha'][:12]}` with {clean(p.get('model', ''), 60)} on the contributor's API key "
        f"({_cost_line(usage)}). [Signed run]({p.get('run_url', '')}).</sub>"
    )
    return "\n".join(out)


def state_block(state: dict) -> str:
    return f"{STATE_MARKER}{json.dumps(state, separators=(',', ':'))} -->"


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


def instructions(pr: int, head_repo: str, upstream: str, deadline: str, attempts_left: int,
                 server: str = "https://github.com") -> str:
    fork = f"{server}/{head_repo}"
    return f"""### 🚪 Bouncer review required

This project reviews outside pull requests with an automated bouncer before a maintainer looks at them. **The review runs in your fork, on your own API key.** The maintainers pay nothing and pick it up once it passes.

1. In your fork, open [**Actions**]({fork}/actions) and enable workflows if GitHub asks.
2. Add your Anthropic API key as a repository secret named `ANTHROPIC_API_KEY` ([Settings → Secrets → Actions]({fork}/settings/secrets/actions)). A dedicated key with a spend limit is a good idea.
3. In [**Actions**]({fork}/actions), choose **Bouncer review** → **Run workflow**, and enter `{pr}`.
   If the workflow isn't listed, sync your fork's default branch with `{upstream}` so it picks up `.github/workflows/bouncer-review.yml`.
4. When the run finishes, comment `/bouncer check` here (or wait; this is checked about every 30 minutes).

Only the first review of each commit counts. Deadline: **{deadline}**, after which this pull request is closed. Review rounds left: {attempts_left}."""
