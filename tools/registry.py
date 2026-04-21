"""
claw8s.tools.registry
-----------------------
Tool registration and dispatch. Tools are plain async functions decorated
with @registry.tool(). The LLM agent calls tools by name with JSON args.

Each tool returns a ToolResult with a success flag, output text, and an
optional confidence hint to help the agent decide if it needs human approval.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

log = logging.getLogger(__name__)


@dataclass
class ToolResult:
    success: bool
    output: str
    # Suggested confidence that this action is the right one (0.0 - 1.0).
    # Tools can set this; the agent may also override it from its reasoning.
    confidence: float = 1.0
    # If True, the agent MUST get human approval before executing this action.
    requires_approval: bool = False


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema object for the parameters
    fn: Callable[..., Coroutine]
    # If True, this tool makes a real mutating change to the cluster.
    # Used to decide whether to require approval at the UI layer.
    is_destructive: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict,
        is_destructive: bool = False,
    ):
        """Decorator to register a tool function."""
        def decorator(fn: Callable):
            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                parameters=parameters,
                fn=fn,
                is_destructive=is_destructive,
            )
            return fn
        return decorator

    def get_spec(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def all_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def as_anthropic_tools(self) -> list[dict]:
        """Return tool definitions in Anthropic Claude tool-use format."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": {
                    "type": "object",
                    **spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def as_openai_tools(self) -> list[dict]:
        """Return tool definitions in OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    async def call(self, name: str, args: dict, source: str = "agent") -> ToolResult:
        spec = self._tools.get(name)
        if not spec:
            return ToolResult(success=False, output=f"Unknown tool: {name}")
        try:
            log.info(f"[{source}] Calling tool '{name}' with args: {args}")
            result = await spec.fn(**args)
            return result
        except Exception as e:
            log.error(f"Tool '{name}' raised: {e}", exc_info=True)
            return ToolResult(success=False, output=f"Tool error: {e}")
