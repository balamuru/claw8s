"""
claw8s.skills
-------------
Skill registry. Discovers and loads all *.yaml skill definitions from
this directory at startup, mapping incident trigger reasons to skills.

A Skill is a deterministic, YAML-defined runbook for a known failure
pattern. The SkillRunner (in _runner.py) interprets and executes them.

Directory layout:
  skills/
    __init__.py              ← this file (SkillResult, SkillRegistry)
    _runner.py               ← execution engine
    crashloop_backoff.yaml   ← skill definition
    oom_killed.yaml
    node_not_ready.yaml
    ...                      ← add more without touching Python
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class SkillResult:
    """
    Returned by SkillRunner.run().

    Three possible outcomes:
      1. inconclusive=True  → skill couldn't resolve; fall through to agent loop
      2. needs_human=True   → skill identified the issue but a human must act
      3. neither            → skill resolved the incident autonomously
    """
    inconclusive: bool = False
    needs_human: bool = False
    human_message: str = ""
    summary: str = ""
    actions_taken: list[dict] = field(default_factory=list)
    # Skill findings to inject as context if falling through to agent loop
    findings: str = ""


# ── Registry ──────────────────────────────────────────────────────────────────

class SkillRegistry:
    """
    Loads all *.yaml files in the skills/ directory and maps their trigger
    reasons to skill definitions. Auto-discovers on instantiation.
    """

    def __init__(self):
        self._skills: dict[str, dict] = {}  # trigger_reason → skill definition
        self._reload()

    def _reload(self):
        self._skills.clear()
        loaded = 0
        for path in sorted(SKILLS_DIR.glob("*.yaml")):
            try:
                with open(path, encoding="utf-8") as f:
                    skill = yaml.safe_load(f)
                if not isinstance(skill, dict):
                    log.warning(f"Skipping {path}: not a valid YAML mapping.")
                    continue
                name = skill.get("name", path.stem)
                for trigger in skill.get("triggers", []):
                    self._skills[trigger] = skill
                    log.info(f"Skill '{name}' registered for trigger '{trigger}'")
                loaded += 1
            except Exception as e:
                log.error(f"Failed to load skill {path}: {e}")
        log.info(f"Skills loaded: {loaded} file(s), {len(self._skills)} trigger(s) registered.")

    def get(self, reason: str) -> dict | None:
        """Return the skill definition for this incident reason, or None."""
        return self._skills.get(reason)

    def all_triggers(self) -> list[str]:
        return list(self._skills.keys())

    def reload(self):
        """Hot-reload all skill files from disk. Useful in development."""
        self._reload()


# Module-level singleton — imported by agent.py
skill_registry = SkillRegistry()
