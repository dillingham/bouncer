"""Loading and validating the maintainer's .bouncer.yml.

The file always comes from the upstream repository (never from the PR), so the
contributor cannot change the rules, the model or the effort level.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import yaml

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_TURNS = {"low": 12, "medium": 20, "high": 35, "xhigh": 50, "max": 80}

DEFAULT_RULES = [
    {
        "id": "solves-linked-issue",
        "hard": True,
        "description": "The change actually resolves the linked issue as described there, "
        "not a superficial, partial or unrelated fix.",
    },
    {
        "id": "not-duplicate",
        "hard": True,
        "description": "It does not duplicate an open pull request, is not already fixed on the "
        "base branch, and does not redo something maintainers previously declined.",
    },
    {
        "id": "correct",
        "hard": True,
        "description": "No evident bugs, regressions, broken builds or nonsensical code "
        "(for example calls to APIs that do not exist in this codebase).",
    },
    {
        "id": "substantive",
        "hard": True,
        "description": "Not a trivial cosmetic change (typo-only, whitespace, renames, comment "
        "rewording, reformatting) unless the linked issue asks for exactly that.",
    },
    {
        "id": "in-scope",
        "hard": True,
        "description": "Serves the project's general users and stated scope, not a niche "
        "one-off use case or a feature the project does not want.",
    },
    {
        "id": "matches-style",
        "hard": False,
        "description": "Follows the conventions and structure of the surrounding code.",
    },
    {
        "id": "has-tests",
        "hard": False,
        "description": "Behavior changes come with tests, when the project has tests for that area.",
    },
]

_RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
_MODEL = re.compile(r"^claude-[a-z0-9.-]{1,60}$")


class ConfigError(ValueError):
    pass


@dataclass
class Rule:
    id: str
    description: str
    hard: bool = True


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_turns: int = DEFAULT_TURNS["high"]
    deadline_hours: int = 48
    max_attempts: int = 3
    close_on_fail: bool = True
    fail_confidence: float = 0.8
    pin_review_to_gate_version: bool = False
    exempt_users: list[str] = field(default_factory=lambda: ["dependabot[bot]", "renovate[bot]"])
    exempt_prior_contributors: bool = True
    exempt_maintainers: bool = True
    rereview_after_pass: bool = True
    require_linked_issue: bool = True
    max_changed_lines: int = 0
    max_author_prs_24h: int = 0
    forbidden_paths: list[str] = field(default_factory=lambda: [".github/**"])
    guidance: str = ""
    rules: list[Rule] = field(default_factory=lambda: [Rule(**r) for r in DEFAULT_RULES])
    digest: str = ""

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)


def _get(d: dict, key: str, typ, default):
    if key not in d or d[key] is None:
        return default
    val = d[key]
    if typ is float and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    if typ is int and isinstance(val, bool):
        raise ConfigError(f"{key} must be a number")
    if not isinstance(val, typ):
        raise ConfigError(f"{key} must be {typ.__name__}, got {type(val).__name__}")
    return val


def parse(text: str | None) -> Config:
    """Parse .bouncer.yml text. Missing file or empty text gives the defaults."""
    raw = text or ""
    data = yaml.safe_load(raw) if raw.strip() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(".bouncer.yml must be a mapping")
    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported .bouncer.yml version: {version}")

    review = data.get("review") or {}
    gate = data.get("gate") or {}
    checks = data.get("checks") or {}
    for name, section in (("review", review), ("gate", gate), ("checks", checks)):
        if not isinstance(section, dict):
            raise ConfigError(f"{name} must be a mapping")

    cfg = Config()
    cfg.model = _get(review, "model", str, cfg.model)
    if not _MODEL.match(cfg.model):
        raise ConfigError(f"review.model does not look like a Claude model id: {cfg.model!r}")
    cfg.effort = _get(review, "effort", str, cfg.effort)
    if cfg.effort not in EFFORTS:
        raise ConfigError(f"review.effort must be one of {', '.join(EFFORTS)}")
    cfg.max_turns = _get(review, "max_turns", int, DEFAULT_TURNS[cfg.effort])
    if not 3 <= cfg.max_turns <= 200:
        raise ConfigError("review.max_turns must be between 3 and 200")

    cfg.deadline_hours = _get(gate, "deadline_hours", int, cfg.deadline_hours)
    cfg.max_attempts = _get(gate, "max_attempts", int, cfg.max_attempts)
    cfg.close_on_fail = _get(gate, "close_on_fail", bool, cfg.close_on_fail)
    cfg.fail_confidence = _get(gate, "fail_confidence", float, cfg.fail_confidence)
    cfg.pin_review_to_gate_version = _get(
        gate, "pin_review_to_gate_version", bool, cfg.pin_review_to_gate_version
    )
    cfg.exempt_users = [str(u) for u in _get(gate, "exempt_users", list, cfg.exempt_users)]
    cfg.exempt_prior_contributors = _get(
        gate, "exempt_prior_contributors", bool, cfg.exempt_prior_contributors
    )
    cfg.exempt_maintainers = _get(gate, "exempt_maintainers", bool, cfg.exempt_maintainers)
    cfg.rereview_after_pass = _get(gate, "rereview_after_pass", bool, cfg.rereview_after_pass)
    if cfg.deadline_hours < 1 or cfg.max_attempts < 1:
        raise ConfigError("gate.deadline_hours and gate.max_attempts must be at least 1")
    if not 0.0 <= cfg.fail_confidence <= 1.0:
        raise ConfigError("gate.fail_confidence must be between 0 and 1")

    cfg.require_linked_issue = _get(checks, "require_linked_issue", bool, cfg.require_linked_issue)
    cfg.max_changed_lines = _get(checks, "max_changed_lines", int, cfg.max_changed_lines)
    cfg.max_author_prs_24h = _get(checks, "max_author_prs_24h", int, cfg.max_author_prs_24h)
    cfg.forbidden_paths = [str(p) for p in _get(checks, "forbidden_paths", list, cfg.forbidden_paths)]

    cfg.guidance = _get(data, "guidance", str, "").strip()[:8000]

    if "rules" in data and data["rules"] is not None:
        rules_raw = data["rules"]
        if not isinstance(rules_raw, list) or not rules_raw:
            raise ConfigError("rules must be a non-empty list")
        rules: list[Rule] = []
        for i, r in enumerate(rules_raw):
            if not isinstance(r, dict):
                raise ConfigError(f"rules[{i}] must be a mapping")
            rid = str(r.get("id", ""))
            if not _RULE_ID.match(rid):
                raise ConfigError(f"rules[{i}].id must be lowercase letters, digits and dashes")
            desc = r.get("description")
            if not isinstance(desc, str) or not desc.strip():
                raise ConfigError(f"rules[{i}].description is required")
            hard = r.get("hard", True)
            if not isinstance(hard, bool):
                raise ConfigError(f"rules[{i}].hard must be true or false")
            if any(x.id == rid for x in rules):
                raise ConfigError(f"duplicate rule id: {rid}")
            rules.append(Rule(id=rid, description=desc.strip()[:1000], hard=hard))
        if len(rules) > 30:
            raise ConfigError("at most 30 rules")
        cfg.rules = rules

    cfg.digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    return cfg
