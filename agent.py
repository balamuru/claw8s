"""
claw8s.agent
--------------
The agentic loop. Takes an Incident, runs a multi-step tool-calling
conversation with Claude until it reaches a conclusion, then returns
a summary of what it found and what it did (or recommends).

Flow:
  1. Build system prompt with incident context
  2. Call Claude with tool definitions
  3. For each tool_use block: check if approval needed → execute → feed result back
  4. Repeat until Claude returns a final text response (stop_reason = end_turn)
  5. Return the final summary
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional
import anthropic

import llm
from config import AgentConfig
from watcher import Incident
from tools.registry import ToolRegistry, ToolResult
from audit import AuditLog, AuditAction, ActionStatus, now_iso
from prompts import load_system_prompt
from skills import skill_registry, SkillResult
from skills._runner import SkillRunner

log = logging.getLogger(__name__)

# System prompt is assembled from prompts/soul.md + prompts/guidelines.md
# at startup. Edit those files and restart — no code change needed.
SYSTEM_PROMPT = load_system_prompt()


@dataclass
class AgentResult:
    incident_id: str
    summary: str
    actions_taken: list[dict]
    needs_human: bool
    human_message: Optional[str] = None  # message to send to human if needs_human


class Claw8sAgent:
    def __init__(
        self,
        cfg: AgentConfig,
        api_key: str,
        tool_registry: ToolRegistry,
        audit: AuditLog,
        # Approval callback: called with (incident_id, tool_name, args, reasoning)
        # Should return True if approved, False if rejected.
        approval_callback=None,
    ):
        self.cfg = cfg
        self.backend = llm.get_backend(cfg.provider, api_key, cfg.base_url)
        self.registry = tool_registry
        self.audit = audit
        self.approval_callback = approval_callback  # async callable
        self.skill_runner = SkillRunner(tool_registry, api_key, audit, cfg.provider, cfg.base_url, cfg.model)

    async def run(self, incident: Incident) -> AgentResult:
        log.info(f"Agent starting run for incident {incident.id[:8]}")
        # ── Try skills first ──────────────────────────────────────────
        skill_findings = ""
        actions_taken = []
        skill_def = skill_registry.get(incident.reason)
        if skill_def:
            log.info(f"Matching skill found for '{incident.reason}': {skill_def.get('name')}")
            skill_res: SkillResult = await self.skill_runner.run(skill_def, incident)
            
            if not skill_res.inconclusive:
                log.info(f"Skill '{skill_def.get('name')}' resolved incident {incident.id[:8]}")
                return AgentResult(
                    incident_id=incident.id,
                    summary=skill_res.summary or skill_res.human_message,
                    actions_taken=skill_res.actions_taken,
                    needs_human=skill_res.needs_human,
                    human_message=skill_res.human_message
                )
            
            # Skill was inconclusive, carry findings into the agent loop
            skill_findings = skill_res.findings
            actions_taken = skill_res.actions_taken
            log.info(f"Skill '{skill_def.get('name')}' was inconclusive. Falling back to agent loop.")

        log.info(f"Constructing agent context and fetching history for {incident.id[:8]}")
        messages = [
            {
                "role": "user",
                "content": self._incident_context(incident, skill_findings),
            }
        ]

        tools = self.backend.get_tools(self.registry)
        tool_call_count = 0
        needs_human = False
        human_message = None

        # Fetch recent history for this object to prevent looping
        history = await self.audit.get_recent_object_actions(
            incident.namespace, incident.object_kind, incident.object_name
        )
        log.info(f"History fetched ({len(history)} items). Sending first chat turn to LLM.")

        # Start conversation
        turn = await self.backend.chat(
            system=SYSTEM_PROMPT,
            tools=tools,
            user_message=self._incident_context(incident, skill_findings, history),
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
        )
        log.info(f"First LLM turn complete. Text response: {bool(turn.text)}, Tool calls: {len(turn.tool_calls)}")

        while True:
            if turn.finished:
                return AgentResult(
                    incident_id=incident.id,
                    summary=turn.text or "",
                    actions_taken=actions_taken,
                    needs_human=needs_human,
                    human_message=human_message,
                )

            if tool_call_count >= self.cfg.max_tool_calls:
                log.warning(f"Reached max tool calls ({self.cfg.max_tool_calls}) for incident {incident.id}")
                needs_human = True
                human_message = f"Reached max tool call limit ({self.cfg.max_tool_calls}). Manual review needed."
                break

            # Process tool calls
            tool_results = []
            for tc in turn.tool_calls:
                tool_call_count += 1
                tool_spec = self.registry.get_spec(tc.name)

                # Extract reasoning from the assistant's text (if any) or use a default
                reasoning = turn.text or "No reasoning provided"
                confidence = self._extract_confidence(reasoning)

                # Check if this requires human approval
                approved = True
                if tool_spec and tool_spec.is_destructive:
                    if confidence < self.cfg.auto_remediate_threshold:
                        if self.approval_callback:
                            approved = await self.approval_callback(
                                incident.id, tc.name, tc.args, reasoning, confidence
                            )
                        else:
                            approved = False
                            needs_human = True
                            human_message = f"Action `{tc.name}` on `{incident.object_name}` needs approval (confidence={confidence:.0%}).\n\nReasoning: {reasoning}"

                # Log the action with tokens
                await self.audit.log_action(AuditAction(
                    incident_id=incident.id,
                    timestamp=now_iso(),
                    tool_name=tc.name,
                    tool_args=json.dumps(tc.args),
                    reasoning=reasoning,
                    confidence=confidence,
                    status=ActionStatus.APPROVED if approved else ActionStatus.REJECTED,
                    source="soul",
                    input_tokens=turn.input_tokens // len(turn.tool_calls) if turn.tool_calls else turn.input_tokens,
                    output_tokens=turn.output_tokens // len(turn.tool_calls) if turn.tool_calls else turn.output_tokens,
                ))

                if approved:
                    result: ToolResult = await self.registry.call(tc.name, tc.args, source="soul")
                    status = ActionStatus.EXECUTED if result.success else ActionStatus.FAILED
                    await self.audit.update_action_result(incident.id, tc.name, status, result.output)

                    actions_taken.append({
                        "tool": tc.name,
                        "args": tc.args,
                        "success": result.success,
                        "output": result.output[:500],
                    })
                    tool_results.append((tc.id, result.output))
                else:
                    tool_results.append((tc.id, f"Action rejected (requires human approval). Confidence was {confidence:.0%}. See reasoning: {reasoning}"))

            # Feed results back and get next turn
            turn = await self.backend.continue_with_results(tool_results)

        # Fallback if loop exits unexpectedly
        return AgentResult(
            incident_id=incident.id,
            summary="Agent loop ended without a conclusion. Manual review recommended.",
            actions_taken=actions_taken,
            needs_human=True,
        )

    def _incident_context(self, incident: Incident, skill_findings: str = "", history: list[dict] = None) -> str:
        ctx = (
            f"## New Incident\n\n"
            f"**Incident ID:** {incident.id}\n"
            f"**Time:** {incident.timestamp}\n"
            f"**Namespace:** {incident.namespace}\n"
            f"**Object:** {incident.object_kind}/{incident.object_name}\n"
            f"**Reason:** {incident.reason}\n"
            f"**Message:** {incident.message}\n"
            f"**Event count:** {incident.count}\n\n"
        )
        
        if skill_findings:
            ctx += (
                f"## Preliminary Skill Findings\n"
                f"A pre-defined skill was run for this incident type but was inconclusive. "
                f"Here is what was found:\n\n{skill_findings}\n\n"
            )

        if history:
            ctx += "## Recent Action History (Last 2 Hours)\n"
            ctx += "The following actions were already taken for this specific object. DO NOT repeat a failing action.\n\n"
            for h in history:
                ctx += f"- **{h['timestamp']}**: {h['tool']} ({h['status']}) - *{h['reasoning']}*\n"
                if h['result']:
                    ctx += f"  Result: {h['result']}\n"
            ctx += "\n"
            
        ctx += "Please investigate and remediate this incident."
        return ctx

    def _extract_confidence(self, text: str) -> float:
        """Try to extract a confidence value from reasoning text (e.g. '0.9' or '90%')."""
        import re
        # Look for patterns like "confidence: 0.85" or "confidence: 85%"
        # Refined regex to avoid capturing trailing punctuation like "0.70."
        m = re.search(r"confidence[:\s]+([0-9]*\.?[0-9]+)%?", text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                return val / 100 if val > 1 else val
            except ValueError:
                pass
        return 0.75  # default if not found
