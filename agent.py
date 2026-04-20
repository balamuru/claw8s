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
from typing import Optional, AsyncIterator

import anthropic

from config import AgentConfig
from watcher import Incident
from tools.registry import ToolRegistry, ToolResult
from audit import AuditLog, AuditAction, ActionStatus, now_iso

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Claw8s, an autonomous Kubernetes operations agent.

Your job is to investigate a Kubernetes incident, determine the root cause,
and take the safest appropriate action to remediate it.

Guidelines:
- Start by gathering information (logs, pod status, events) before acting.
- Only take mutating actions (restart, scale, delete) if you're confident they're needed.
- State your confidence (0.0–1.0) and reasoning before each mutating action.
- If unsure, prefer to alert the human rather than act.
- Be concise. The final summary should be 3–5 sentences max.
- Never touch kube-system resources autonomously.
- After acting, verify the action had the desired effect.

When you've finished your investigation and any remediation, provide a final
plain-English summary of: what the issue was, what you did (or recommend), and current status.
"""


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
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.registry = tool_registry
        self.audit = audit
        self.approval_callback = approval_callback  # async callable

    async def run(self, incident: Incident) -> AgentResult:
        log.info(f"Agent starting on incident {incident.id}: {incident.reason} / {incident.object_name}")

        messages = [
            {
                "role": "user",
                "content": self._incident_context(incident),
            }
        ]

        tools = self.registry.as_anthropic_tools()
        actions_taken = []
        tool_call_count = 0
        needs_human = False
        human_message = None

        while True:
            if tool_call_count >= self.cfg.max_tool_calls:
                log.warning(f"Reached max tool calls ({self.cfg.max_tool_calls}) for incident {incident.id}")
                needs_human = True
                human_message = f"Reached max tool call limit ({self.cfg.max_tool_calls}). Manual review needed."
                break

            response = await self.client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )

            # Add assistant response to history
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # Final text response — we're done
                final_text = next(
                    (b.text for b in response.content if hasattr(b, "text")), ""
                )
                return AgentResult(
                    incident_id=incident.id,
                    summary=final_text,
                    actions_taken=actions_taken,
                    needs_human=needs_human,
                    human_message=human_message,
                )

            if response.stop_reason != "tool_use":
                break

            # Process tool calls
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_call_count += 1
                tool_name = block.name
                tool_args = block.input
                tool_spec = self.registry.get_spec(tool_name)

                # Extract reasoning from the text block just before this tool call
                reasoning = next(
                    (b.text for b in response.content if hasattr(b, "text")), "No reasoning provided"
                )

                # Determine confidence — look for it in reasoning text or default
                confidence = self._extract_confidence(reasoning)

                # Check if this requires human approval
                approved = True
                if tool_spec and tool_spec.is_destructive:
                    if confidence < self.cfg.auto_remediate_threshold:
                        # Ask human
                        if self.approval_callback:
                            approved = await self.approval_callback(
                                incident.id, tool_name, tool_args, reasoning, confidence
                            )
                        else:
                            # No callback — skip destructive action, flag for human
                            approved = False
                            needs_human = True
                            human_message = f"Action `{tool_name}` on `{incident.object_name}` needs approval (confidence={confidence:.0%}).\n\nReasoning: {reasoning}"

                # Log the proposed action
                await self.audit.log_action(AuditAction(
                    incident_id=incident.id,
                    timestamp=now_iso(),
                    tool_name=tool_name,
                    tool_args=json.dumps(tool_args),
                    reasoning=reasoning,
                    confidence=confidence,
                    status=ActionStatus.APPROVED if approved else ActionStatus.REJECTED,
                ))

                if approved:
                    result: ToolResult = await self.registry.call(tool_name, tool_args)
                    status = ActionStatus.EXECUTED if result.success else ActionStatus.FAILED
                    await self.audit.update_action_result(incident.id, tool_name, status, result.output)

                    actions_taken.append({
                        "tool": tool_name,
                        "args": tool_args,
                        "success": result.success,
                        "output": result.output[:500],  # truncate for summary
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.output,
                    })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Action rejected (requires human approval). Confidence was {confidence:.0%}.",
                    })

            messages.append({"role": "user", "content": tool_results})

        # Fallback if loop exits unexpectedly
        return AgentResult(
            incident_id=incident.id,
            summary="Agent loop ended without a conclusion. Manual review recommended.",
            actions_taken=actions_taken,
            needs_human=True,
        )

    def _incident_context(self, incident: Incident) -> str:
        return (
            f"## New Incident\n\n"
            f"**Incident ID:** {incident.id}\n"
            f"**Time:** {incident.timestamp}\n"
            f"**Namespace:** {incident.namespace}\n"
            f"**Object:** {incident.object_kind}/{incident.object_name}\n"
            f"**Reason:** {incident.reason}\n"
            f"**Message:** {incident.message}\n"
            f"**Event count:** {incident.count}\n\n"
            f"Please investigate and remediate this incident."
        )

    def _extract_confidence(self, text: str) -> float:
        """Try to extract a confidence value from reasoning text (e.g. '0.9' or '90%')."""
        import re
        # Look for patterns like "confidence: 0.85" or "confidence: 85%"
        m = re.search(r"confidence[:\s]+([0-9.]+)%?", text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            return val / 100 if val > 1 else val
        return 0.75  # default if not found
