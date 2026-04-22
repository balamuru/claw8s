# Claw8s Skills Guide: Authoring Runbooks 🦅📖

Skills are the "Tier 1" deterministic brain of Claw8s. They allow you to define high-speed, cost-effective remediation paths for known problems without relying on open-ended LLM reasoning.

## 1. How Skills Work

1.  An incident is detected by the **Watcher**.
2.  The **Manager** attempts to match the incident's `reason` against all YAML files in the `skills/` directory.
3.  If a match is found, the **Skills Runner** executes the predefined steps.
4.  If the steps are completed but the issue persists, Claw8s escalates the incident to the **Soul (Agentic Loop)** for deeper reasoning.

## 2. YAML Structure

Create a new file in `skills/my_skill.yaml`:

```yaml
name: "Image Pull Specialist"
description: "Handles pods stuck in ImagePullBackOff or ErrImagePull"

# The reasons that trigger this skill (from K8s Events)
trigger_reasons:
  - "ErrImagePull"
  - "ImagePullBackOff"

# Sequential investigation steps
steps:
  - action: "get_pod_logs"
  - action: "get_deployment_status"
  - action: "analyze_image_path"
```

### Supported Step Actions
*   `get_pod_logs`: Fetches the last 50 lines of container logs.
*   `get_deployment_status`: Aggregates health, ready replicas, and restart counts.
*   `describe_object`: Runs the equivalent of `kubectl describe`.
*   `check_events`: Looks for related events in the same namespace.

## 3. The "Inconclusive" Escalation

The goal of a skill is to provide **context**. 
*   If a skill identifies the root cause (e.g., "The image tag `latest-v2` does not exist"), it passes this discovery to the Agent.
*   The Agent then takes the discovery and asks for your approval to fix it: *"The ImagePull skill found a typo in the tag. Should I patch it to `latest-v1`?"*

## 4. Why Use Skills?

*   **Speed**: Deterministic steps run in milliseconds.
*   **Cost**: Zero LLM tokens are used for the initial investigation.
*   **Predictability**: Ensures that your standard operational procedures (SOPs) are followed every time.
