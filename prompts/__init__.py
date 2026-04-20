"""
claw8s.prompts
--------------
Loads prompt files from the `prompts/` directory and assembles the
system prompt from ordered tiers.

Tier 0 — soul.md        : Identity + inviolable rules. NEVER evicted from
                           context (passed as `system=` not in messages).
Tier 1 — guidelines.md  : Soft behavioral defaults. Loaded at startup,
                           injected once into the system prompt.

Files are loaded once and cached. Edit the .md files and restart to
pick up changes — no code deploy needed.
"""

import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Directory containing all prompt markdown files, relative to this file.
PROMPTS_DIR = Path(__file__).parent


def _load(filename: str) -> str:
    """Load a prompt file, returning its content stripped of leading/trailing whitespace."""
    path = PROMPTS_DIR / filename
    if not path.exists():
        log.warning(f"Prompt file not found: {path}. Skipping.")
        return ""
    text = path.read_text(encoding="utf-8").strip()
    log.debug(f"Loaded prompt file: {path} ({len(text)} chars)")
    return text


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """
    Assemble the full system prompt from ordered tiers.

    Returns a single string ready to pass as `system=` to the LLM.
    The result is cached after first load — restart the process to reload.
    """
    parts = []

    soul = _load("soul.md")
    if soul:
        parts.append(soul)

    guidelines = _load("guidelines.md")
    if guidelines:
        parts.append(guidelines)

    if not parts:
        raise RuntimeError(
            "No prompt files found in prompts/. "
            "At minimum, prompts/soul.md must exist."
        )

    assembled = "\n\n---\n\n".join(parts)
    log.info(f"System prompt assembled: {len(assembled)} chars from {len(parts)} file(s).")
    return assembled


def reload_prompts() -> str:
    """
    Force a reload of all prompt files, bypassing the cache.
    Useful for hot-reloading in development without restarting.
    """
    load_system_prompt.cache_clear()
    return load_system_prompt()
