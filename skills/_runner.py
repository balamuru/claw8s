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

import json
import logging
import re
from typing import Any

import llm

from tools.registry import ToolRegistry, ToolResult
from watcher import Incident
from audit import AuditLog, AuditAction, ActionStatus, now_iso
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
        audit: AuditLog,
        provider: str = "anthropic",
        base_url: str = "",
        model: str = "claude-haiku-4-5",
    ):
        self._tools = tools
        self._audit = audit
        self._backend = llm.get_backend(provider, api_key, base_url)
        # A fast, cheap model is ideal for the narrow classification task.
        self._model = model

    async def run(self, skill: dict, incident: Incident) -> SkillResult:
        name = skill.get("name", "unknown")
        log.info(f"[skill:{name}] Starting for incident {incident.id} ({incident.reason})")

        # Execution context — holds incident + results of previous steps
        ctx: dict[str, Any] = {"incident": incident, "_skill_name": name}
        actions_taken: list[dict] = []
        findings_parts: list[str] = []

        for step in skill.get("steps", []):
            step_id: str = step.get("id", f"step_{len(ctx)}")

            # ── Tool step ─────────────────────────────────────────────────
            if "tool" in step:
                result = await self._exec_tool(step, ctx, step_id, incident)
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
                    step["llm_classify"], ctx, name, incident
                )
                ctx[step_id] = classification
                findings_parts.append(f"### Classification: {classification}")
                log.info(f"[skill:{name}] step '{step_id}' → '{classification}'")

            # ── Switch step ───────────────────────────────────────────────
            elif "switch" in step:
                findings = "\n\n".join(findings_parts)
                return await self._exec_switch(
                    step, ctx, actions_taken, findings, name, incident
                )

        # Loop finished without a switch — skill couldn't reach a conclusion
        log.info(f"[skill:{name}] No switch step reached — inconclusive.")
        return SkillResult(
            inconclusive=True,
            findings="\n\n".join(findings_parts),
            actions_taken=actions_taken,
        )

    # ── Step executors ────────────────────────────────────────────────────────

    async def _exec_tool(self, step: dict, ctx: dict, step_id: str, incident: Incident, reasoning: str = "") -> ToolResult:
        tool_name = step["tool"]
        args = _render(step.get("args", {}), ctx)
        log.info(f"Skill tool call: {tool_name}({args})")

        # Log to audit
        action = AuditAction(
            incident_id=incident.id,
            timestamp=now_iso(),
            tool_name=tool_name,
            tool_args=json.dumps(args),
            reasoning=reasoning or f"Step '{step_id}' of skill '{ctx.get('_skill_name')}'",
            confidence=1.0,  # Skills are deterministic
            status=ActionStatus.APPROVED,
            source="skill",
        )
        await self._audit.log_action(action)

        result = await self._tools.call(tool_name, args, source=f"skill:{ctx.get('_skill_name')}")

        status = ActionStatus.EXECUTED if result.success else ActionStatus.FAILED
        await self._audit.update_action_result(incident.id, tool_name, status, result.output)

        return result

    async def _exec_classify(
        self, cfg: dict, ctx: dict, skill_name: str, incident: Incident
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
            f"You are an expert Kubernetes SRE. Your task is to classify an incident into one of the following categories based on the evidence provided.\n\n"
            f"CATEGORIES:\n{category_lines}\n\n"
            f"EVIDENCE:\n{evidence}\n\n"
            f"INSTRUCTIONS:\n"
            f"1. Analyze the evidence carefully.\n"
            f"2. Select the category key that best fits the root cause.\n"
            f"3. If no category fits perfectly, choose the closest one or 'unknown'.\n"
            f"4. Respond with ONLY the category key (e.g. 'oom'). Do not include markdown, quotes, or explanations.\n\n"
            f"RESPONSE:"
        )

        try:
            # We use a fresh chat for classification to keep it simple and stateless
            turn = await self._backend.chat(
                system="You are an incident classifier. Output ONLY the key of the chosen category.",
                tools=[],  # no tools for classification
                user_message=prompt,
                model=self._model,
                max_tokens=32,
            )
            
            # Log the classification action to audit for token tracking
            await self._audit.log_action(AuditAction(
                incident_id=incident.id,
                timestamp=now_iso(),
                tool_name="llm_classify",
                tool_args=json.dumps({"input_ids": input_ids}),
                reasoning=f"Classifying incident for skill '{skill_name}'",
                confidence=1.0,
                status=ActionStatus.EXECUTED,
                source=f"skill:{skill_name}",
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens
            ))

            raw = (turn.text or "").strip().lower()
            log.info(f"[skill:{skill_name}] LLM classify raw response: '{raw}'")
            
            if not raw:
                return "unknown"

            # Match to the closest valid key (fuzzy)
            for key in categories:
                if key in raw or raw in key:
                    return key
            
            log.warning(f"[skill:{skill_name}] Classify response matched no category.")
            return "unknown"
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
        incident: Incident,
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

        # ── Summary only (successful conclusion without action) ──────────
        if "summary" in case and "tool" not in case:
            summary = _render(case["summary"], ctx)
            return SkillResult(
                summary=summary,
                findings=findings,
                actions_taken=actions_taken,
            )

        # ── Execute a tool, then optionally verify ────────────────────────
        if "tool" in case:
            result = await self._exec_tool(case, ctx, "action", incident, reasoning=f"Remediation step in skill '{skill_name}'")
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
                verify_result = await self._exec_tool(
                    case["verify"], ctx, "verify", incident, reasoning=f"Verification for '{case['tool']}' in skill '{skill_name}'"
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
