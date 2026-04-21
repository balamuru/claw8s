"""
claw8s.llm
----------
Provider-agnostic LLM backend abstraction.

Supported providers:
  - anthropic   → Claude (claude-opus-4-5, claude-haiku-4-5, etc.)
  - openai      → OpenAI GPT, AND any OpenAI-compatible API:
                  Ollama (local), Groq, OpenRouter, Gemini, LiteLLM proxy, etc.

Usage:
  backend = get_backend(cfg)
  turn    = await backend.chat(system, user_message)
  while turn.tool_calls:
      results = [(tc.id, await execute(tc)) for tc in turn.tool_calls]
      turn = await backend.continue_with_results(results)
  print(turn.text)
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ── Shared data types ─────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMTurn:
    text: str | None          # assistant's text (may be None if only tool calls)
    tool_calls: list[ToolCall]
    finished: bool            # True = model is done, no more tool calls expected
    input_tokens: int = 0
    output_tokens: int = 0


# ── Abstract base ─────────────────────────────────────────────────────────────

class LLMBackend(ABC):
    """
    Each backend maintains its own message history internally so the agent
    doesn't need to know anything about provider-specific message formats.
    """

    @abstractmethod
    async def chat(
        self,
        system: str,
        tools: list[dict],   # provider-specific format (see get_tools())
        user_message: str,
        model: str,
        max_tokens: int,
    ) -> LLMTurn:
        """Start a new conversation with an initial user message."""
        ...

    @abstractmethod
    async def continue_with_results(
        self,
        tool_results: list[tuple[str, str]],  # [(tool_call_id, content), ...]
    ) -> LLMTurn:
        """Feed tool results back and get the next turn."""
        ...

    @abstractmethod
    def get_tools(self, registry) -> list[dict]:
        """Return tool definitions in this provider's expected format."""
        ...


# ── Anthropic backend ─────────────────────────────────────────────────────────

class AnthropicBackend(LLMBackend):
    def __init__(self, api_key: str):
        import anthropic as _anthropic
        self._client = _anthropic.AsyncAnthropic(api_key=api_key)
        self._messages: list[dict] = []
        self._system: str = ""
        self._tools: list[dict] = []
        self._model: str = ""
        self._max_tokens: int = 4096

    async def chat(self, system, tools, user_message, model, max_tokens) -> LLMTurn:
        self._system = system
        self._tools = tools
        self._model = model
        self._max_tokens = max_tokens
        self._messages = [{"role": "user", "content": user_message}]
        return await self._call()

    async def continue_with_results(self, tool_results: list[tuple[str, str, str]]) -> LLMTurn:
        # Anthropic: tool results go in a user message as content blocks
        # tool_results is [(id, name, content), ...]
        self._messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": str(content)}
                for tid, name, content in tool_results
            ],
        })
        return await self._call()

    async def _call(self) -> LLMTurn:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._system,
            tools=self._tools,
            messages=self._messages,
        )
        # Save assistant turn to history
        self._messages.append({"role": "assistant", "content": response.content})

        text = next((b.text for b in response.content if hasattr(b, "text")), None)
        tool_calls = [
            ToolCall(id=b.id, name=b.name, args=b.input)
            for b in response.content
            if getattr(b, "type", None) == "tool_use"
        ]
        finished = response.stop_reason in ("end_turn", "stop_sequence") and not tool_calls
        return LLMTurn(
            text=text, 
            tool_calls=tool_calls, 
            finished=finished,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens
        )

    def get_tools(self, registry) -> list[dict]:
        return registry.as_anthropic_tools()


# ── OpenAI-compatible backend ─────────────────────────────────────────────────

class OpenAIBackend(LLMBackend):
    """
    Works with any OpenAI-compatible API:
      - OpenAI (GPT-4o, GPT-4-turbo, …)
      - Ollama  → base_url="http://localhost:11434/v1", api_key="ollama"
      - Groq    → base_url="https://api.groq.com/openai/v1"
      - OpenRouter → base_url="https://openrouter.ai/api/v1"
      - Gemini  → base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
      - Any LiteLLM proxy
    """

    def __init__(self, api_key: str, base_url: str = ""):
        import openai as _openai
        kwargs = {"api_key": api_key or "none"}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = _openai.AsyncOpenAI(**kwargs)
        self._messages: list[dict] = []
        self._system: str = ""
        self._tools: list[dict] = []
        self._model: str = ""
        self._max_tokens: int = 4096

    async def chat(self, system, tools, user_message, model, max_tokens) -> LLMTurn:
        self._system = system
        self._tools = tools
        self._model = model
        self._max_tokens = max_tokens
        self._messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]
        return await self._call()

    async def continue_with_results(self, tool_results: list[tuple[str, str, str]]) -> LLMTurn:
        # OpenAI: each tool result is a separate message with role "tool"
        for tool_call_id, name, content in tool_results:
            # SAFETY: Gemini fails if name is empty. If name is missing, use a fallback.
            safe_name = name if name and str(name).strip() else "remediation_tool"
            
            self._messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": safe_name,
                "content": str(content),
            })
        
        # DEBUG: log the turn history count and last message name
        log.info(f"Continuing conversation (turn {len(self._messages)}). Last tool: {name}")
        return await self._call()

    async def _call(self) -> LLMTurn:
        # IRON SHIELD SANITIZER: Gemini/OpenAI adapter is extremely strict.
        # We must ensure every message is a dict and has all required protocol fields.
        sanitized = []
        for m in self._messages:
            # Ensure m is a dict
            if hasattr(m, "model_dump"):
                m_dict = m.model_dump(exclude_none=True) # Don't exclude unset, Gemini might want them
            else:
                m_dict = dict(m)

            role = m_dict.get("role")
            if role == "tool":
                if not m_dict.get("name") or not str(m_dict.get("name")).strip():
                    m_dict["name"] = "remediation_tool"
            elif role == "assistant":
                tcs = m_dict.get("tool_calls")
                if tcs:
                    for tc in tcs:
                        if "function" not in tc:
                            tc["function"] = {"name": "remediation_tool", "arguments": "{}"}
                        else:
                            f = tc["function"]
                            if not f.get("name") or not str(f.get("name")).strip():
                                f["name"] = "remediation_tool"
                            if f.get("arguments") is None:
                                f["arguments"] = "{}"
            sanitized.append(m_dict)

        # Update internal history with sanitized versions to prevent drift
        self._messages = sanitized

        kwargs = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=self._messages,
        )
        if self._tools:
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        # Save assistant turn to history
        self._messages.append(msg.model_dump(exclude_unset=True))

        text = msg.content
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))

        finish = response.choices[0].finish_reason
        finished = finish in ("stop", None) and not tool_calls
        
        input_tokens = getattr(response.usage, "prompt_tokens", 0)
        output_tokens = getattr(response.usage, "completion_tokens", 0)

        return LLMTurn(
            text=text, 
            tool_calls=tool_calls, 
            finished=finished,
            input_tokens=input_tokens,
            output_tokens=output_tokens
        )

    def get_tools(self, registry) -> list[dict]:
        return registry.as_openai_tools()


# ── Factory ───────────────────────────────────────────────────────────────────

def get_backend(provider: str, api_key: str, base_url: str = "") -> LLMBackend:
    """
    Instantiate the right backend from config.

    provider: "anthropic" | "openai"
    api_key:  your API key for the chosen provider
    base_url: optional — override the API endpoint (for Ollama, Groq, etc.)
    """
    if provider == "anthropic":
        return AnthropicBackend(api_key=api_key)
    elif provider == "openai":
        return OpenAIBackend(api_key=api_key, base_url=base_url)
    else:
        raise ValueError(
            f"Unknown LLM provider: '{provider}'. "
            f"Valid options: 'anthropic', 'openai'"
        )
