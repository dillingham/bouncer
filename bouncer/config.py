"""Loading and validating the maintainer's .bouncer.yml.

The file always comes from the upstream repository (never from the PR), so the
contributor cannot change the rules, the model or the effort level.
"""
from __future__ import annotations

import difflib
import hashlib
import json
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

# Every setting, by section ("" = top level). Anything else gets a warning: a typo would
# otherwise silently fall back to the default.
KNOWN = {
    "": ("version", "review", "gate", "checks", "guidance", "rules"),
    "review": ("model", "effort", "max_turns"),
    "gate": ("deadline_hours", "max_attempts", "close_on_fail", "fail_confidence", "pin_review_to_gate_version",
             "exempt_users", "exempt_prior_contributors", "exempt_maintainers", "rereview_after_pass"),
    "checks": ("target_branches", "require_linked_issue", "max_changed_lines", "max_author_prs_24h", "forbidden_paths"),
    "rules": ("id", "hard", "description"),
}

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
    # Off by default: one merged PR would exempt everything its author opens afterwards.
    exempt_prior_contributors: bool = False
    exempt_maintainers: bool = True
    rereview_after_pass: bool = True
    # Base branches outside pull requests may target. Empty = the repository's default branch only.
    target_branches: list[str] = field(default_factory=list)
    require_linked_issue: bool = True
    max_changed_lines: int = 0
    max_author_prs_24h: int = 0
    forbidden_paths: list[str] = field(default_factory=lambda: [".github/**"])
    guidance: str = ""
    rules: list[Rule] = field(default_factory=lambda: [Rule(**r) for r in DEFAULT_RULES])
    # Problems that don't stop the bouncer, such as unknown settings. Shown as warnings.
    warnings: list[str] = field(default_factory=list)

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    @property
    def digest(self) -> str:
        """Digest of the settings that shape the review itself: model, effort, turns, guidance, rules.

        The review signs it and the gate only accepts reviews whose digest matches its current
        config. It is computed from the parsed values, so comments, formatting, key order and line
        endings don't change it. The gate applies everything else (deadlines, checks, exemptions,
        fail_confidence) from its current config when it decides, so changing those doesn't
        invalidate reviews. A setting that starts to affect the review must be added here.
        """
        review = {
            "model": self.model,
            "effort": self.effort,
            "max_turns": self.max_turns,
            "guidance": self.guidance,
            "rules": [{"id": r.id, "hard": r.hard, "description": r.description} for r in self.rules],
        }
        canonical = json.dumps(review, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


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


def _unknown(section: str, d: dict) -> list[str]:
    out = []
    for key in d:
        if key not in KNOWN[section]:
            name = f"{section}.{key}" if section and section != "rules" else str(key)
            near = difflib.get_close_matches(str(key), KNOWN[section], n=1)
            out.append(f"unknown setting {name}, ignored" + (f" (did you mean {near[0]}?)" if near else ""))
    return out


def parse(text: str | None) -> Config:
    """Parse .bouncer.yml text. Missing file or empty text gives the defaults."""
    raw = text or ""
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ConfigError(f"not valid YAML: {getattr(e, 'problem', None) or e}{where}") from None
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
    cfg.warnings = _unknown("", data) + _unknown("review", review) + _unknown("gate", gate) + _unknown("checks", checks)
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
    cfg.exempt_users = [str(u) for u in _get(gate, "exempt_users", list, cfg.exempt_users)]  # compared ignoring case
    cfg.exempt_prior_contributors = _get(
        gate, "exempt_prior_contributors", bool, cfg.exempt_prior_contributors
    )
    cfg.exempt_maintainers = _get(gate, "exempt_maintainers", bool, cfg.exempt_maintainers)
    cfg.rereview_after_pass = _get(gate, "rereview_after_pass", bool, cfg.rereview_after_pass)
    if cfg.deadline_hours < 1 or cfg.max_attempts < 1:
        raise ConfigError("gate.deadline_hours and gate.max_attempts must be at least 1")
    if not 0.0 <= cfg.fail_confidence <= 1.0:
        raise ConfigError("gate.fail_confidence must be between 0 and 1")

    branches = _get(checks, "target_branches", list, cfg.target_branches)
    if not all(isinstance(b, str) and b.strip() for b in branches):
        raise ConfigError("checks.target_branches must be a list of branch names")
    cfg.target_branches = [b.strip() for b in branches]
    cfg.require_linked_issue = _get(checks, "require_linked_issue", bool, cfg.require_linked_issue)
    cfg.max_changed_lines = _get(checks, "max_changed_lines", int, cfg.max_changed_lines)
    cfg.max_author_prs_24h = _get(checks, "max_author_prs_24h", int, cfg.max_author_prs_24h)
    if cfg.max_changed_lines < 0 or cfg.max_author_prs_24h < 0:
        raise ConfigError("checks.max_changed_lines and checks.max_author_prs_24h must be 0 (no limit) or more")
    cfg.forbidden_paths = [str(p).strip() for p in _get(checks, "forbidden_paths", list, cfg.forbidden_paths)]
    for p in cfg.forbidden_paths:
        # Patterns are read like .gitignore lines (see common.path_matches), minus negation.
        if not p.strip("/"):
            raise ConfigError("checks.forbidden_paths has an empty pattern")
        if p.startswith("!"):
            raise ConfigError(f"checks.forbidden_paths: negated patterns like {p!r} aren't supported")

    cfg.guidance = _get(data, "guidance", str, "").strip()[:8000]

    if "rules" in data and data["rules"] is not None:
        rules_raw = data["rules"]
        if not isinstance(rules_raw, list) or not rules_raw:
            raise ConfigError("rules must be a non-empty list")
        rules: list[Rule] = []
        for i, r in enumerate(rules_raw):
            if not isinstance(r, dict):
                raise ConfigError(f"rules[{i}] must be a mapping")
            cfg.warnings += [f"rules[{i}]: {w}" for w in _unknown("rules", r)]
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

    return cfg


def fetch_text(gh, repo: str) -> str:
    """.bouncer.yml from the repository's default branch ("" if there is none). Both the review
    and the gate read this copy, so the review's signed config digest can match the gate's."""
    return gh.get_or_none(f"/repos/{repo}/contents/.bouncer.yml", accept="application/vnd.github.raw") or ""
