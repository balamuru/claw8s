# Claw8s Configuration Guide 🦅⚙️

Claw8s is highly configurable via `config.yaml`. This guide explains the primary settings and how to bridge various LLM providers.

## 1. LLM Provider Setup (`agent`)

Claw8s supports **Anthropic** (Native) and **OpenAI** (and any OpenAI-compatible API).

### 🏛️ Anthropic (Claude)
The recommended provider for the most reliable agentic reasoning.
```yaml
agent:
  provider: anthropic
  model: claude-3-5-sonnet  # or claude-3-opus-latest
  max_tokens: 4096
```

### 🌉 OpenAI / General Compatibility
Use this for GPT-4 or any provider that implements the OpenAI Chat Completions API.
```yaml
agent:
  provider: openai
  model: gpt-4o
```

### ♊ Google Gemini (via OpenAI Bridge)
Gemini is supported through its OpenAI-compatible endpoint. Note: Claw8s includes an "Iron Shield" sanitizer to handle Gemini's strict protocol requirements.
```yaml
agent:
  provider: openai
  model: gemini-1.5-flash
  base_url: "https://generativelanguage.googleapis.com/v1beta/openai/"
```

### 🦙 Ollama (Local LLM)
Run Claw8s entirely on-prem with no external API calls. Ensure you are running a model with **Tool Calling** support (e.g., `llama3.1`).
```yaml
agent:
  provider: openai
  model: llama3.1
  base_url: "http://localhost:11434/v1/"
```

---

## 2. Autonomy & Safety (`agent`)

*   **`auto_remediate_threshold` (0.0 to 1.0)**:
    *   **0.0**: Fully autonomous. The agent will execute all actions without asking.
    *   **0.85 (Default)**: Balanced. The agent only executes actions it is very confident in. Low-confidence actions trigger a Telegram approval request.
    *   **1.0**: Human-in-the-loop only. Every mutation requires a click in Telegram.
*   **`max_tool_calls`**: A safety cap on the number of reasoning turns the agent can take per incident. This prevents infinite loops or excessive API usage.

---

## 3. Monitoring Scope (`watcher`)

*   **`watch_all_namespaces`**: Set to `true` to monitor the entire cluster.
*   **`debounce_seconds`**: Cooldown period (per object+reason) to prevent alert storms during rapid restart loops.
*   **`trigger_reasons`**: A list of K8s Event reasons that Claw8s should react to. Common defaults include `BackOff`, `Unhealthy`, and `OOMKilling`.

---

## 4. Telegram Integration (`telegram`)

*   **`allowed_user_ids`**: An explicit list of Telegram User IDs permitted to interact with the bot. **Leave empty `[]` only in private, secure environments.**
*   **`primary_chat_id`**: The ID where real-time alerts will be pushed.
