"""Turn a signed review into pass/fail. Pure function, used by the gate.

The model never decides on its own. A PR fails only when:
  * a deterministic check from .bouncer.yml fails, or
  * the PR text tried to instruct the reviewer, or
  * a hard rule failed with confidence at or above the threshold AND at least
    one piece of evidence was verified against the actual files.
Everything else the model flags is reported to the maintainer as a soft flag.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .common import path_matches
from .config import Config


@dataclass
class Decision:
    outcome: str  # "pass" | "fail"
    reasons: list[str] = field(default_factory=list)  # why it failed
    flags: list[str] = field(default_factory=list)  # concerns that did not fail it


def check_facts(facts: dict, cfg: Config) -> list[str]:
    out: list[str] = []
    if cfg.require_linked_issue:
        open_issues = [i for i in facts.get("linked_issues", []) if i.get("state") == "open"]
        if not open_issues:
            out.append("No open issue is linked. Reference the issue this fixes (for example `Fixes #123`).")
    if cfg.max_changed_lines and facts.get("changed_lines", 0) > cfg.max_changed_lines:
        out.append(
            f"Changes {facts['changed_lines']} lines; this project accepts at most "
            f"{cfg.max_changed_lines} per pull request."
        )
    if cfg.forbidden_paths:
        touched = [f["path"] for f in facts.get("changed_files", []) if path_matches(f["path"], cfg.forbidden_paths)]
        if touched:
            shown = ", ".join(f"`{p}`" for p in touched[:5])
            out.append(f"Touches paths outside contributors' reach: {shown}.")
    prs = facts.get("author_prs_24h")
    if cfg.max_author_prs_24h and isinstance(prs, int) and prs > cfg.max_author_prs_24h:
        out.append(
            f"Author opened {prs} pull requests across GitHub in the last 24 hours "
            f"(limit {cfg.max_author_prs_24h})."
        )
    return out


def decide(predicate: dict, cfg: Config) -> Decision:
    d = Decision(outcome="pass")
    d.reasons.extend(check_facts(predicate.get("facts", {}), cfg))

    review = predicate.get("review", {})
    if review.get("injection_detected"):
        note = (review.get("injection_notes") or "").strip()
        d.reasons.append("The pull request contains text addressed to the reviewer." + (f" {note}" if note else ""))

    results = {r.get("id"): r for r in review.get("rules", [])}
    for rule in cfg.rules:
        r = results.get(rule.id)
        if not r or r.get("result") != "fail":
            if r and r.get("result") == "unsure":
                d.flags.append(f"`{rule.id}`: reviewer was unsure. {r.get('reason', '')}".strip())
            continue
        verified = [e for e in r.get("evidence", []) if e.get("verified")]
        conf = float(r.get("confidence", 0) or 0)
        line = f"`{rule.id}`: {r.get('reason', '').strip()}"
        if rule.hard and conf >= cfg.fail_confidence and verified:
            d.reasons.append(line)
        else:
            why = "soft rule" if not rule.hard else (
                "no verified evidence" if not verified else f"confidence {conf:.2f}"
            )
            d.flags.append(f"{line} ({why})")

    if d.reasons:
        d.outcome = "fail"
    return d
