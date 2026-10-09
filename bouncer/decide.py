"""Turn a signed review into pass/fail. Pure function, used by the gate.

The model never decides on its own. A PR fails only when:
  * a pre-check from .bouncer.yml fails (checked in code), or
  * the PR text tried to instruct the reviewer, or
  * the review has no verdict at all for a Required rule (skipping a rule must not pass it), or
  * a Required rule failed with confidence at or above the threshold AND at least
    one piece of evidence was verified against the actual files.
Everything else the model flags is reported to the maintainer as a note.

Reasons and notes about a rule start with "`rule-id` (Required|Advisory...)", which
rule_of() reads back for the report.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .common import path_matches
from .config import Config


@dataclass
class Decision:
    outcome: str  # "pass" | "fail"
    reasons: list[str] = field(default_factory=list)  # why it failed
    flags: list[str] = field(default_factory=list)  # concerns that did not fail it


def pct(x: float) -> str:
    return f"{round(x * 100)}%"


def kind(hard: bool) -> str:
    return "Required" if hard else "Advisory"


_RULE_LINE = re.compile(r"^`([^`]+)` \(([^)]*)\): ?(.*)$", re.S)


def rule_of(line: str) -> str | None:
    """The rule id a reason or note is about, or None (a pre-check or the injection reason)."""
    m = _RULE_LINE.match(line)
    return m.group(1) if m else None


def brief(line: str, limit: int = 200) -> str:
    """A reason as short plain text, for the state comment and the CLI: `rule-id: why`."""
    m = _RULE_LINE.match(line)
    text = f"{m.group(1)}: {m.group(3)}" if m else line
    text = " ".join(text.replace("`", "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def check_facts(facts: dict, cfg: Config) -> list[str]:
    out: list[str] = []
    if cfg.require_linked_issue:
        open_issues = [i for i in facts.get("linked_issues", []) if i.get("state") == "open"]
        if not open_issues:
            out.append("Pre-check: no open issue is linked. Say which issue this fixes in the description, "
                       "for example `Fixes #123`.")
    if cfg.max_changed_lines and facts.get("changed_lines", 0) > cfg.max_changed_lines:
        out.append(f"Pre-check: changes {facts['changed_lines']:,} lines; this project accepts at most "
                   f"{cfg.max_changed_lines:,} per pull request.")
    if cfg.forbidden_paths:
        touched = [p for f in facts.get("changed_files", []) for p in (f.get("previous_path"), f["path"])
                   if p and path_matches(p, cfg.forbidden_paths)]
        if touched:
            shown = ", ".join(f"`{p}`" for p in touched[:5])
            out.append(f"Pre-check: changes files this project doesn't take outside changes to: {shown}.")
    if facts.get("files_truncated") and (cfg.forbidden_paths or cfg.max_changed_lines):
        # GitHub lists at most 3,000 changed files, so the checks above can't see the rest.
        out.append("Pre-check: changes more files than GitHub lists (3,000), so the bouncer can't check them all.")
    prs = facts.get("author_prs_24h")
    if cfg.max_author_prs_24h and isinstance(prs, int) and prs > cfg.max_author_prs_24h:
        out.append(f"Pre-check: the author opened {prs} pull requests across GitHub in the last 24 hours; "
                   f"this project allows {cfg.max_author_prs_24h}.")
    return out


def decide(predicate: dict, cfg: Config) -> Decision:
    d = Decision(outcome="pass")
    d.reasons.extend(check_facts(predicate.get("facts", {}), cfg))

    review = predicate.get("review", {})
    if review.get("injection_detected"):
        note = (review.get("injection_notes") or "").strip()
        d.reasons.append("The pull request contains instructions aimed at the AI reviewer, which isn't allowed."
                         + (f" {note}" if note else ""))

    results = {r.get("id"): r for r in review.get("rules", [])}
    for rule in cfg.rules:
        r = results.get(rule.id)
        tag = f"`{rule.id}` ({kind(rule.hard)})"
        if not r:
            (d.reasons if rule.hard else d.flags).append(f"{tag}: the review didn't give a verdict on this rule.")
            continue
        reason = (r.get("reason") or "").strip()
        if r.get("result") == "unsure":
            d.flags.append(f"{tag}: Unsure. {reason}".strip())
            continue
        if r.get("result") != "fail":
            continue
        verified = [e for e in r.get("evidence", []) if e.get("verified")]
        conf = float(r.get("confidence", 0) or 0)
        if rule.hard and conf >= cfg.fail_confidence and verified:
            d.reasons.append(f"`{rule.id}` ({kind(rule.hard)}, {pct(conf)} confidence): {reason}")
        elif not rule.hard:
            d.flags.append(f"{tag}: {reason}")
        elif not verified:
            d.flags.append(f"{tag}: {reason} Didn't fail the pull request: no evidence the reviewer could verify.")
        else:
            d.flags.append(f"{tag}: {reason} Didn't fail the pull request: {pct(conf)} confidence, "
                           f"below the {pct(cfg.fail_confidence)} fail confidence.")

    if d.reasons:
        d.outcome = "fail"
    return d
