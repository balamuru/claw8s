<p align="center">
  <img src="assets/banner.png" alt="claw8s" width="360"/>
</p>
<h1 align="center">claw8s</h1>
<p align="center">
  Autonomous Kubernetes monitoring and remediation agent powered by Claude.
</p>

<p align="center">
  <b>Detects</b> K8s incidents &rarr; <b>diagnoses</b> root cause &rarr; <b>acts</b> (with your approval) &rarr; <b>notifies</b> you via Telegram.
</p>

---


## Architecture

```
K8s Watch API
     │
     ▼
 KubernetesWatcher (background thread, debounced)
     │
     ▼
 asyncio incident queue
     │
     ▼
 Skills Dispatch (deterministic YAML runbooks)
     │   ├── crashloop_backoff.yaml
     │   └── oom_killed.yaml
     │
     ▼ (if inconclusive)
 Claw8sAgent (Claude open-ended loop)
     │   ├── get_pod_logs
     │   ├── describe_pod
     │   ├── list_pods
     │   ├── get_deployment_status
     │   ├── get_node_status
     │   ├── restart_deployment  ⚠️
     │   ├── scale_deployment    ⚠️
     │   ├── delete_pod          ⚠️
     │   └── cordon_node         ⚠️
     │
     ▼
 TelegramBot  ←→  You
     │
     ▼
 AuditLog (SQLite)
```

⚠️ = mutating action, requires Telegram approval if confidence < threshold

---

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `watcher.watch_all_namespaces` | `true` | Watch all namespaces |
| `watcher.debounce_seconds` | `120` | Cooldown between same-incident triggers |
| `watcher.trigger_reasons` | (list) | K8s event reasons that trigger the agent |
| `agent.model` | `claude-opus-4-5` | Anthropic model to use |
| `agent.auto_remediate_threshold` | `0.85` | Confidence below this → ask for approval |
| `agent.max_tool_calls` | `10` | Max tool calls per incident (safety cap) |
| `telegram.allowed_user_ids` | `[]` | Telegram user IDs allowed to control the bot |

---

## Quick Start

### 1. Install Dependencies

**Option A: Using uv (Recommended)**
```bash
# Install uv: https://docs.astral.sh/uv/
cd claw8s
uv venv && source .venv/bin/activate
uv pip install -e .
```

**Option B: Using pip**
```bash
cd claw8s
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 2. Set up Secrets
```bash
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN
```

### 3. Configure
```bash
cp config.yaml.example config.yaml
# Edit config.yaml — at minimum set your Telegram user ID
```

### 4. Run
```bash
python main.py --config config.yaml
# or if installed: claw8s --config config.yaml
```

### Getting a Telegram Bot Token
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. `/newbot` → follow prompts → copy the token
3. Message [@userinfobot](https://t.me/userinfobot) to find your user ID
4. Put both in your `.env` and `config.yaml`

---

## Soul & Skills

Claw8s uses a multi-stage memory architecture to ensure safety and efficiency.

### The Soul (Prompts)
The agent's identity and safety rules are stored in `prompts/`. These are injected as the system prompt and are never evicted from the model's context window.
- `prompts/soul.md`: Inviolable safety rules and core identity.
- `prompts/guidelines.md`: Soft behavioral defaults and investigation strategies.

### Skills (Runbooks)
Skills are deterministic, YAML-defined procedures for known incident types. They live in `skills/`.
- **Hybrid Execution**: Skills use a fast LLM (Haiku) for classification, but the procedural logic is fixed.
- **Fallback**: If a skill cannot resolve an incident, it hands off all findings to the main agent loop.

Example skill (`skills/crashloop_backoff.yaml`):
```yaml
name: crashloop_backoff
triggers: [CrashLoopBackOff]
steps:
  - id: get_logs
    tool: get_pod_logs
  - id: classify
    llm_classify:
      categories:
        oom: "Exit Code 137"
        bad_config: "Exit Code 1"
  - id: act
    switch: "{{ classify }}"
    cases:
      oom: { escalate: "Capacity issue detected." }
```

---

## Extending

### Adding a new tool

```python
# In tools/kubectl.py (or a new file in tools/)
from tools.registry import registry, ToolResult

@registry.tool(
    name="my_tool",
    description="What this tool does",
    parameters={
        "properties": {
            "namespace": {"type": "string"},
        },
        "required": ["namespace"],
    },
    is_destructive=False,  # True = will require approval if confidence is low
)
async def my_tool(namespace: str) -> ToolResult:
    # ... do something
    return ToolResult(success=True, output="done")
```

---

## Safety

- `kube-system` namespace is always protected from mutating actions
- Scale is capped at 0–20 replicas
- All actions are logged to SQLite with full reasoning chain
- Destructive actions below confidence threshold require your Telegram approval
- 5-minute approval timeout → auto-rejected

---

## File Structure

```
claw8s/
├── agent.py           ← Claude agentic loop
├── audit.py           ← SQLite audit log (async)
├── config.py          ← Config loading (env + yaml)
├── main.py            ← Entry point + wiring
├── watcher.py         ← K8s event watcher (debounced)
├── bot/
│   └── telegram.py    ← Telegram bot (alerts + approval)
├── prompts/           ← Markdown identity & safety rules
│   ├── soul.md
│   └── guidelines.md
├── skills/            ← YAML-defined runbooks
│   ├── _runner.py     ← Skill execution engine
│   └── *.yaml         ← Skill definitions
├── tools/
│   ├── registry.py    ← Tool decorator + dispatch
│   └── kubectl.py     ← K8s tools (read + mutate)
├── config.yaml.example
├── .env.example
└── pyproject.toml
```
