# Whitepaper: Autonomous Kubernetes Remediation with Claw8s 🦅🛡️

## Executive Summary

Claw8s is a next-generation autonomous agent designed to provide "Steady-State" remediation for Kubernetes clusters. By combining deterministic **Skills** (YAML runbooks) with a high-reasoning **Soul** (Agentic LLM loop), Claw8s bridges the gap between static scripts and manual SRE intervention. This paper outlines the architectural evolution, core features, and critical lessons learned during the stabilization of the Claw8s pipeline.

---

## 1. Architectural Philosophy: The Hybrid Model

The core innovation of Claw8s is its **Multi-Tier Reasoning** architecture.

### Tier 1: Deterministic Skills (The Junior SRE)
Skills are pre-defined, YAML-based runbooks. They are fast, cost-effective, and safe. They handle the "known unknowns" (e.g., standard OOMKilled events).

### Tier 2: The Agentic Soul (The Senior SRE)
When a Skill is inconclusive, the **Soul** takes over. Guided by an inviolable set of ethical and operational rules (`soul.md`), the Soul uses open-ended tool calling to diagnose novel failures, analyze logs, and perform complex multi-turn remediations.

---

## 2. System Architecture & Component Layout

Claw8s is built as a highly decoupled, event-driven system. It consists of four primary "Organs" that interact asynchronously:

### 📡 The Watcher (Sensory Cortex)
The Watcher is the system's primary input. It maintains a live stream of the Kubernetes Event API while simultaneously running a **Proactive Stale Pod Scanner**. This dual-path approach ensures that Claw8s detects both sudden spikes (Events) and slow, silent failures (Pods stuck in `Pending` for 7+ minutes).

### 🧰 The Registry (Toolbelt)
All agent actions are encapsulated in a centralized **Tool Registry**. Each tool (e.g., `scale_deployment`, `get_pod_logs`) is strictly typed and metadata-enriched. This allows the system to distinguish between "Read-Only" diagnostics and "Destructive" mutations, triggering the appropriate human approval flows.

### 🏛️ The Auditor (Memory Bank)
Claw8s never forgets. Every incident, reasoning turn, and tool execution is persisted in a **Relational Audit Database** (SQLite). This data is then injected back into the Agent's context as "Short-Term Memory," preventing the agent from repeating failing actions or entering infinite restart loops.

### 📱 The Bot (Mobile Console)
The Telegram interface serves as the system's human bridge. It provides a real-time stream of cluster health and allows for "Commander" overrides. Through the **Interactive Approval Flow**, SREs can approve, reject, or reconfirm actions directly from their mobile devices.

---

## 3. Core Features & Capabilities

### ⚡ Concurrent Boot Architecture
To ensure high availability, Claw8s implements a non-blocking startup sequence. External integrations (like the Telegram Bot) are spawned as independent background tasks, preventing the core monitoring loop from hanging on slow API handshakes.

### 🔍 Smart Reconfirm (Live Probes)
The remediation loop features a "Trust but Verify" mandate. The Telegram **Reconfirm** button triggers live Kubernetes health probes (e.g., checking `ready_replicas`) to validate manual fixes before automated actions are taken, preventing redundant or destructive operations.

### 🛡️ Steady-State Verification
Claw8s enforces a **Stability Mandate**. The agent is prohibited from declaring an incident "Resolved" until it verifies that the resource is not only `Ready` but also **Stable** (zero increasing restart counts over a verification window).

### 📊 Namespace-Aware Observability
The system provides granular, filtered status reports that group pod health by namespace. This reduces "noise" from system components while highlighting unhealthy resources cluster-wide with live heartbeat timestamps.

---

## 4. Retrospective: Lessons Learned & "Special Scenarios"

During the stabilization exercise, several critical engineering traps were identified and resolved:

### 🏮 The "Silent Boot" Trap
**Scenario**: The application would appear "stuck" on startup without logs.
**Lesson**: Synchronous API calls to external services (Telegram/LLMs) during the boot sequence can block the entire event loop.
**Fix**: Decoupled initialization into concurrent `asyncio` tasks.

### ✂️ The 64-Byte Callback Limit
**Scenario**: The bot would crash with `Button_data_invalid` when handling long incident IDs.
**Lesson**: Telegram has a strict 64-byte limit for button callback data. 
**Fix**: Implemented callback hashing and internal memory mapping to keep button payloads lean.

### 🛑 The "Premature Victory" Bug
**Scenario**: The agent would declare an incident "Resolved" while pods were still in a crash loop.
**Lesson**: `Ready: True` is a point-in-time check, not a health verdict.
**Fix**: Injected `total_restart_count` into tool outputs and added a "Stability Mandate" to the core prompt.

### 🧪 Gemini Protocol Sanitization
**Scenario**: Multi-turn reasoning would fail with a `400 Bad Request` after several turns.
**Lesson**: The Gemini API (via OpenAI adapter) is hyper-sensitive to empty `name` fields in the conversation history.
**Fix**: Implemented a "Nuclear Sanitizer" that force-scrubs every history message before it reaches the API.

---

## 5. Conclusion: The Path Forward

Claw8s demonstrates that autonomous remediation is most effective when it is **Paranoid**. By treating "Success" as a state to be proven through continuous verification rather than a simple command return, we create a system that SREs can truly trust.

**The future of Claw8s lies in:**
1.  **Stateful Memory**: Moving from a 2-hour window to multi-day anomaly detection.
2.  **Chaos Integration**: Self-testing the "Soul" by intentionally breaking pods and measuring resolution accuracy.
3.  **Multi-Cluster Fleet Control**: Centralized management via a single unified "SRE Soul."

---
*Document Version: 1.1.0*
*Last Updated: 2026-04-21*
