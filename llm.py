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

    async def continue_with_results(self, tool_results) -> LLMTurn:
        # Anthropic: tool results go in a user message as content blocks
        self._messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content}
                for tid, content in tool_results
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
        return LLMTurn(text=text, tool_calls=tool_calls, finished=finished)

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

    async def continue_with_results(self, tool_results) -> LLMTurn:
        # OpenAI: each tool result is a separate message with role "tool"
        for tool_call_id, content in tool_results:
            self._messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            })
        return await self._call()

    async def _call(self) -> LLMTurn:
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
        return LLMTurn(text=text, tool_calls=tool_calls, finished=finished)

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
