"""
claw8s.config
---------------
Loads configuration from environment variables and an optional config.yaml.
All secrets (API keys, bot token) live in env vars / .env only.
"""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass
class WatcherConfig:
    # K8s event types to watch
    namespaces: list[str] = field(default_factory=lambda: ["default"])
    watch_all_namespaces: bool = True
    # Event reasons that trigger the agent
    trigger_reasons: list[str] = field(default_factory=lambda: [
        "BackOff",
        "CrashLoopBackOff",
        "OOMKilling",
        "Failed",
        "FailedScheduling",
        "Unhealthy",
        "NodeNotReady",
        "EvictionThresholdMet",
        "FreeDiskSpaceFailed",
    ])
    # Seconds to wait before re-triggering on the same object
    debounce_seconds: int = 120


@dataclass
class AgentConfig:
    model: str = "claude-opus-4-5"
    max_tokens: int = 4096
    # Confidence threshold (0.0-1.0) below which agent asks for human approval
    auto_remediate_threshold: float = 0.85
    # Max consecutive tool calls per incident (safety limit)
    max_tool_calls: int = 10


@dataclass
class TelegramConfig:
    enabled: bool = True
    # Comma-separated list of allowed Telegram user IDs (leave empty = any)
    allowed_user_ids: list[int] = field(default_factory=list)


@dataclass
class AuditConfig:
    db_path: str = "claw8s_audit.db"


@dataclass
class Config:
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)

    # Secrets — always from env
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    kubeconfig_path: str = ""  # empty = in-cluster or default ~/.kube/config


def load_config(config_path: str = "config.yaml") -> Config:
    cfg = Config()

    # Load from YAML if it exists
    p = Path(config_path)
    if p.exists():
        with open(p) as f:
            raw = yaml.safe_load(f) or {}

        w = raw.get("watcher", {})
        cfg.watcher.namespaces = w.get("namespaces", cfg.watcher.namespaces)
        cfg.watcher.watch_all_namespaces = w.get("watch_all_namespaces", cfg.watcher.watch_all_namespaces)
        cfg.watcher.trigger_reasons = w.get("trigger_reasons", cfg.watcher.trigger_reasons)
        cfg.watcher.debounce_seconds = w.get("debounce_seconds", cfg.watcher.debounce_seconds)

        a = raw.get("agent", {})
        cfg.agent.model = a.get("model", cfg.agent.model)
        cfg.agent.max_tokens = a.get("max_tokens", cfg.agent.max_tokens)
        cfg.agent.auto_remediate_threshold = a.get("auto_remediate_threshold", cfg.agent.auto_remediate_threshold)
        cfg.agent.max_tool_calls = a.get("max_tool_calls", cfg.agent.max_tool_calls)

        t = raw.get("telegram", {})
        cfg.telegram.enabled = t.get("enabled", cfg.telegram.enabled)
        cfg.telegram.allowed_user_ids = [int(x) for x in t.get("allowed_user_ids", [])]

        au = raw.get("audit", {})
        cfg.audit.db_path = au.get("db_path", cfg.audit.db_path)

    # Secrets always from env (override YAML if set)
    cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    cfg.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cfg.kubeconfig_path = os.environ.get("KUBECONFIG", "")

    # Validate
    if not cfg.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required")
    if cfg.telegram.enabled and not cfg.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required when telegram.enabled=true")

    return cfg
