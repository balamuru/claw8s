"""
claw8s.skills._runner
----------------------
Executes skill definitions loaded from YAML files.

Step types
----------
tool
    Call a registered tool with (optionally templated) args.
    Result stored in ctx[step_id].output / .success.

llm_classify
    Send gathered evidence to the LLM and ask it to pick one of N
    named categories. Result stored in ctx[step_id] as a plain string
    (the winning category key). Uses a fast model and a tiny prompt —
    the LLM's only job is to classify, not to plan.

switch
    Branch on the value of a context key (typically a classify result).
    Each case can:
      - call a tool (with optional verify step after)
      - escalate to human   →  needs_human=True
      - fall through        →  inconclusive=True

Template syntax
---------------
Use {{ variable }} in any string value.  Supported lookups:
  {{ incident.namespace }}      attribute on the Incident object
  {{ incident.object_name }}
  {{ step_id.output }}          .output of a previous tool step
  {{ step_id }}                 plain string result (e.g. classify)
"""

import logging
import re
from typing import Any

import llm

from tools.registry import ToolRegistry, ToolResult
from watcher import Incident
from . import SkillResult

log = logging.getLogger(__name__)


# ── Template rendering ────────────────────────────────────────────────────────

def _render(template: Any, ctx: dict) -> Any:
    """
    Recursively resolve {{ key.attr }} placeholders in strings, dicts, lists.
    Returns the input unchanged if it is not a string/dict/list.
    """
    if isinstance(template, str):
        def _replace(m: re.Match) -> str:
            parts = m.group(1).strip().split(".")
            val: Any = ctx
            for part in parts:
                if isinstance(val, dict):
                    val = val.get(part, "")
                else:
                    val = getattr(val, part, "")
            return str(val)
        return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", _replace, template)
    elif isinstance(template, dict):
        return {k: _render(v, ctx) for k, v in template.items()}
    elif isinstance(template, list):
        return [_render(item, ctx) for item in template]
    return template


# ── Runner ────────────────────────────────────────────────────────────────────

class SkillRunner:
    """
    Interprets and executes a skill definition (dict loaded from YAML).
    One SkillRunner instance is created per Claw8sAgent and reused across
    incidents (it is stateless between calls to run()).
    """

    def __init__(
        self,
        tools: ToolRegistry,
        api_key: str,
        provider: str = "anthropic",
        base_url: str = "",
        model: str = "claude-haiku-4-5",
    ):
        self._tools = tools
        self._backend = llm.get_backend(provider, api_key, base_url)
        # A fast, cheap model is ideal for the narrow classification task.
        self._model = model

    async def run(self, skill: dict, incident: Incident) -> SkillResult:
        name = skill.get("name", "unknown")
        log.info(f"[skill:{name}] Starting for incident {incident.id} ({incident.reason})")

        # Execution context — holds incident + results of previous steps
        ctx: dict[str, Any] = {"incident": incident}
        actions_taken: list[dict] = []
        findings_parts: list[str] = []

        for step in skill.get("steps", []):
            step_id: str = step.get("id", f"step_{len(ctx)}")

            # ── Tool step ─────────────────────────────────────────────────
            if "tool" in step:
                result = await self._exec_tool(step, ctx, step_id)
                ctx[step_id] = result
                if result.success:
                    findings_parts.append(f"### {step_id}\n{result.output[:800]}")
                    actions_taken.append({
                        "tool": step["tool"],
                        "args": _render(step.get("args", {}), ctx),
                        "output": result.output[:300],
                        "success": result.success,
                    })
                else:
                    log.warning(f"[skill:{name}] Tool '{step['tool']}' failed: {result.output}")

            # ── LLM classify step ─────────────────────────────────────────
            elif "llm_classify" in step:
                classification = await self._exec_classify(
                    step["llm_classify"], ctx, name
                )
                ctx[step_id] = classification
                findings_parts.append(f"### Classification: {classification}")
                log.info(f"[skill:{name}] step '{step_id}' → '{classification}'")

            # ── Switch step ───────────────────────────────────────────────
            elif "switch" in step:
                findings = "\n\n".join(findings_parts)
                return await self._exec_switch(
                    step, ctx, actions_taken, findings, name
                )

        # Loop finished without a switch — skill couldn't reach a conclusion
        log.info(f"[skill:{name}] No switch step reached — inconclusive.")
        return SkillResult(
            inconclusive=True,
            findings="\n\n".join(findings_parts),
            actions_taken=actions_taken,
        )

    # ── Step executors ────────────────────────────────────────────────────────

    async def _exec_tool(self, step: dict, ctx: dict, step_id: str) -> ToolResult:
        tool_name = step["tool"]
        args = _render(step.get("args", {}), ctx)
        log.info(f"Skill tool call: {tool_name}({args})")
        return await self._tools.call(tool_name, args)

    async def _exec_classify(
        self, cfg: dict, ctx: dict, skill_name: str
    ) -> str:
        """
        Narrow LLM call: given evidence from previous steps, pick a category.
        Returns the winning category key as a plain string.
        """
        categories: dict = cfg.get("categories", {})
        input_ids: list = cfg.get("inputs", [])

        # Gather evidence from referenced step results
        evidence_parts = []
        for inp in input_ids:
            step_result = ctx.get(inp)
            if step_result is None:
                continue
            # ToolResult has .output; classify results are plain strings
            text = step_result.output if hasattr(step_result, "output") else str(step_result)
            evidence_parts.append(f"### {inp}\n{text[:1500]}")

        evidence = "\n\n".join(evidence_parts) or "(no evidence gathered)"

        category_lines = "\n".join(
            f"  {key}: {desc}" for key, desc in categories.items()
        )
        prompt = (
            f"You are classifying a Kubernetes incident. "
            f"Choose EXACTLY ONE category from the list below.\n\n"
            f"Categories:\n{category_lines}\n\n"
            f"Evidence:\n{evidence}\n\n"
            f"Respond with ONLY the category key (e.g. 'oom'). No explanation."
        )

        try:
            # We use a fresh chat for classification to keep it simple and stateless
            turn = await self._backend.chat(
                system="You are an incident classifier.",
                tools=[],  # no tools for classification
                user_message=prompt,
                model=self._model,
                max_tokens=16,
            )
            raw = (turn.text or "").strip().lower()
            # Match to the closest valid key
            for key in categories:
                if key in raw:
                    return key
            log.warning(f"[skill:{skill_name}] Classify response '{raw}' matched no category.")
        except Exception as e:
            log.error(f"[skill:{skill_name}] LLM classify failed: {e}")

        return "unknown"

    async def _exec_switch(
        self,
        step: dict,
        ctx: dict,
        actions_taken: list,
        findings: str,
        skill_name: str,
    ) -> SkillResult:
        # Resolve the switch key (e.g. "{{ classify }}" → "oom")
        switch_key: str = _render(step["switch"], ctx)
        cases: dict = step.get("cases", {})
        case = cases.get(switch_key) or cases.get("unknown") or {}

        log.info(f"[skill:{skill_name}] switch on '{switch_key}' → case: {list(case.keys())}")

        if not case:
            return SkillResult(
                inconclusive=True, findings=findings, actions_taken=actions_taken
            )

        # ── Escalate to human ─────────────────────────────────────────────
        if "escalate" in case:
            message = _render(case["escalate"], ctx)
            return SkillResult(
                needs_human=True,
                human_message=message,
                findings=findings,
                actions_taken=actions_taken,
            )

        # ── Fall through to open-ended agent ──────────────────────────────
        if case.get("inconclusive"):
            return SkillResult(
                inconclusive=True, findings=findings, actions_taken=actions_taken
            )

        # ── Execute a tool, then optionally verify ────────────────────────
        if "tool" in case:
            result = await self._exec_tool(case, ctx, "action")
            actions_taken.append({
                "tool": case["tool"],
                "args": _render(case.get("args", {}), ctx),
                "output": result.output[:300],
                "success": result.success,
            })

            if not result.success:
                return SkillResult(
                    needs_human=True,
                    human_message=(
                        f"Skill action `{case['tool']}` failed: {result.output}\n\n"
                        f"Manual intervention required."
                    ),
                    findings=findings,
                    actions_taken=actions_taken,
                )

            ctx["action"] = result

            # Optional verification step
            if "verify" in case:
                verify_result = await self._tools.call(
                    case["verify"]["tool"],
                    _render(case["verify"].get("args", {}), ctx),
                )
                ctx["verify"] = verify_result
                findings += f"\n\n### Verification\n{verify_result.output[:500]}"

            summary = _render(
                case.get(
                    "summary",
                    f"Skill executed `{case['tool']}` successfully.",
                ),
                ctx,
            )
            return SkillResult(
                summary=summary,
                findings=findings,
                actions_taken=actions_taken,
            )

        # Unrecognised case structure — fall through
        return SkillResult(
            inconclusive=True, findings=findings, actions_taken=actions_taken
        )
