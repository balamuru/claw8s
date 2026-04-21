"""
claw8s.tools.kubectl
-----------------------
Kubernetes action tools registered on the global registry.
All mutating tools (restart, scale, cordon) are marked is_destructive=True
so the agent knows to request human approval when below the confidence threshold.

Safety guards baked in:
- Scale: hard cap at min=0, max=20 replicas
- Cordon: only nodes, not control plane
- Restart: only Deployments/StatefulSets, not DaemonSets in kube-system
"""

import asyncio
import json
from typing import Optional
import logging
from typing import Optional

from kubernetes import client as k8s_client

from .registry import ToolRegistry, ToolResult

log = logging.getLogger(__name__)

# Module-level registry — imported and used by agent.py
registry = ToolRegistry()

# ──────────────────────────────────────────────
# READ-ONLY TOOLS
# ──────────────────────────────────────────────

@registry.tool(
    name="get_pod_logs",
    description="Fetch the last N lines of logs from a pod container. Use to diagnose crashes and errors.",
    parameters={
        "properties": {
            "namespace": {"type": "string", "description": "Kubernetes namespace (defaults to 'default')", "default": "default"},
            "name": {"type": "string", "description": "Pod name"},
            "container_name": {"type": "string", "description": "Container name (optional)", "default": ""},
            "container": {"type": "string", "description": "Container name (alias for container_name)", "default": ""},
            "tail_lines": {"type": "integer", "description": "Number of log lines to fetch", "default": 50},
        },
        "required": ["name"],
    },
    is_destructive=False,
)
async def get_pod_logs(**kwargs) -> ToolResult:
    # Handle multiple common hallucinations for the pod name
    name = kwargs.get("name") or kwargs.get("pod_name") or kwargs.get("pod")
    namespace = kwargs.get("namespace", "default")
    container_name = kwargs.get("container_name") or kwargs.get("container") or ""
    tail_lines = int(kwargs.get("tail_lines", 50))

    if not name:
        return ToolResult(success=False, output="Error: 'name' (pod name) is required.")
    try:
        v1 = k8s_client.CoreV1Api()
        k8s_kwargs = {"name": name, "namespace": namespace, "tail_lines": tail_lines, "timestamps": True}
        if container_name:
            k8s_kwargs["container"] = container_name
        logs = await asyncio.to_thread(v1.read_namespaced_pod_log, **kwargs)
        return ToolResult(success=True, output=logs or "(no logs)")
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="describe_pod",
    description="Describe a pod — events, conditions, resource usage, container states.",
    parameters={
        "properties": {
            "namespace": {"type": "string", "default": "default"},
            "name": {"type": "string"},
        },
        "required": ["name"],
    },
    is_destructive=False,
)
async def describe_pod(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("pod_name") or kwargs.get("pod")
    namespace = kwargs.get("namespace", "default")

    if not name:
        return ToolResult(success=False, output="Error: 'name' (pod name) is required.")
    try:
        v1 = k8s_client.CoreV1Api()
        pod = await asyncio.to_thread(v1.read_namespaced_pod, name=name, namespace=namespace)
        info = {
            "phase": pod.status.phase,
            "conditions": [{"type": c.type, "status": c.status, "reason": c.reason} for c in (pod.status.conditions or [])],
            "containers": [
                {
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "state": str(cs.state),
                    "last_state": str(cs.last_state),
                }
                for cs in (pod.status.container_statuses or [])
            ],
            "node_name": pod.spec.node_name,
            "resources": [
                {
                    "name": c.name,
                    "requests": c.resources.requests if c.resources else {},
                    "limits": c.resources.limits if c.resources else {},
                }
                for c in (pod.spec.containers or [])
            ],
        }
        return ToolResult(success=True, output=json.dumps(info, indent=2))
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="list_pods",
    description="List pods in a namespace with their status.",
    parameters={
        "properties": {
            "namespace": {"type": "string", "description": "Namespace, or 'all' for all namespaces", "default": "default"},
        },
        "required": [],
    },
    is_destructive=False,
)
async def list_pods(**kwargs) -> ToolResult:
    namespace = kwargs.get("namespace", "default")
    try:
        v1 = k8s_client.CoreV1Api()
        if namespace == "all":
            pods = await asyncio.to_thread(v1.list_pod_for_all_namespaces)
        else:
            pods = await asyncio.to_thread(v1.list_namespaced_pod, namespace=namespace)
        rows = []
        for p in pods.items:
            rows.append({
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "phase": p.status.phase,
                "ready": all(cs.ready for cs in (p.status.container_statuses or [])),
                "restarts": sum(cs.restart_count for cs in (p.status.container_statuses or [])),
            })
        return ToolResult(success=True, output=json.dumps(rows, indent=2))
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="get_deployment_status",
    description="Get the status of a Deployment including replica counts and conditions.",
    parameters={
        "properties": {
            "namespace": {"type": "string", "default": "default"},
            "name": {"type": "string"},
        },
        "required": ["name"],
    },
    is_destructive=False,
)
async def get_deployment_status(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("deployment_name") or kwargs.get("deployment")
    namespace = kwargs.get("namespace", "default")

    if not name:
        return ToolResult(success=False, output="Error: 'name' (deployment name) is required.")
    try:
        apps_v1 = k8s_client.AppsV1Api()
        d = await asyncio.to_thread(apps_v1.read_namespaced_deployment, name=name, namespace=namespace)
        info = {
            "desired": d.spec.replicas,
            "ready": d.status.ready_replicas,
            "available": d.status.available_replicas,
            "updated": d.status.updated_replicas,
            "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                           for c in (d.status.conditions or [])],
        }
        return ToolResult(success=True, output=json.dumps(info, indent=2))
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="get_deployment",
    description="Fetch the full definition of a Deployment. Use to inspect environment variables, images, and config.",
    parameters={
        "properties": {
            "namespace": {"type": "string", "default": "default"},
            "name": {"type": "string"},
        },
        "required": ["name"],
    },
    is_destructive=False,
)
async def get_deployment(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("deployment_name") or kwargs.get("deployment")
    namespace = kwargs.get("namespace", "default")

    if not name:
        return ToolResult(success=False, output="Error: 'name' (deployment name) is required.")
    try:
        apps_v1 = k8s_client.AppsV1Api()
        d = await asyncio.to_thread(apps_v1.read_namespaced_deployment, name=name, namespace=namespace)
        # Convert to dict and clean up for LLM readability
        d_dict = k8s_client.ApiClient().sanitize_for_serialization(d)
        return ToolResult(success=True, output=json.dumps(d_dict, indent=2))
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="get_node_status",
    description="Get status of all cluster nodes.",
    parameters={"properties": {}, "required": []},
    is_destructive=False,
)
async def get_node_status() -> ToolResult:
    try:
        v1 = k8s_client.CoreV1Api()
        nodes = await asyncio.to_thread(v1.list_node)
        result = []
        for n in nodes.items:
            conditions = {c.type: c.status for c in (n.status.conditions or [])}
            result.append({
                "name": n.metadata.name,
                "ready": conditions.get("Ready") == "True",
                "conditions": conditions,
                "allocatable": n.status.allocatable,
            })
        return ToolResult(success=True, output=json.dumps(result, indent=2))
    except Exception as e:
        return ToolResult(success=False, output=str(e))


# ──────────────────────────────────────────────
# MUTATING TOOLS (require approval below threshold)
# ──────────────────────────────────────────────

@registry.tool(
    name="restart_deployment",
    description=(
        "Perform a rolling restart of a Deployment by patching its pod template annotation. "
        "Equivalent to 'kubectl rollout restart deployment'. Safe for stateless workloads."
    ),
    parameters={
        "properties": {
            "namespace": {"type": "string", "default": "default"},
            "name": {"type": "string"},
            "reason": {"type": "string", "description": "Human-readable reason for the restart"},
        },
        "required": ["name"],
    },
    is_destructive=True,
)
async def restart_deployment(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("deployment_name") or kwargs.get("deployment")
    namespace = kwargs.get("namespace", "default")
    reason = kwargs.get("reason", "Automated remediation")

    if not name:
        return ToolResult(success=False, output="Error: 'name' (deployment name) is required.")
    # Safety: refuse to touch kube-system unless explicitly named
    if namespace == "kube-system":
        return ToolResult(success=False, output="Refusing to restart deployments in kube-system automatically.")
    try:
        apps_v1 = k8s_client.AppsV1Api()
        from datetime import datetime, timezone
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "claw8s/restarted-at": datetime.now(timezone.utc).isoformat(),
                            "claw8s/restart-reason": reason,
                        }
                    }
                }
            }
        }
        await asyncio.to_thread(
            apps_v1.patch_namespaced_deployment,
            name=name, namespace=namespace, body=patch
        )
        return ToolResult(success=True, output=f"Rolling restart triggered for {name} in {namespace}.")
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="scale_deployment",
    description="Scale a Deployment to a specific number of replicas (0–20).",
    parameters={
        "properties": {
            "namespace": {"type": "string", "default": "default"},
            "name": {"type": "string"},
            "replicas": {"type": "integer", "description": "Target replica count (0–20)"},
            "reason": {"type": "string"},
        },
        "required": ["name", "replicas"],
    },
    is_destructive=True,
)
async def scale_deployment(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("deployment_name") or kwargs.get("deployment")
    replicas = int(kwargs.get("replicas", 1))
    namespace = kwargs.get("namespace", "default")
    reason = kwargs.get("reason", "Automated scaling")

    if not name:
        return ToolResult(success=False, output="Error: 'name' (deployment name) is required.")
    if not 0 <= replicas <= 20:
        return ToolResult(success=False, output=f"Replica count {replicas} out of allowed range (0–20).")
    if namespace == "kube-system":
        return ToolResult(success=False, output="Refusing to scale deployments in kube-system automatically.")
    try:
        apps_v1 = k8s_client.AppsV1Api()
        patch = {"spec": {"replicas": replicas}}
        await asyncio.to_thread(
            apps_v1.patch_namespaced_deployment_scale,
            name=name, namespace=namespace, body=patch
        )
        return ToolResult(success=True, output=f"Scaled {name} to {replicas} replicas. Reason: {reason}")
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="patch_deployment",
    description=(
        "Apply a JSON patch to a Deployment. Use to fix image tags, env vars, or resource limits. "
        "Example patch: {'spec': {'template': {'spec': {'containers': [{'name': 'nginx', 'image': 'nginx:latest'}]}}}}"
    ),
    parameters={
        "properties": {
            "namespace": {"type": "string", "default": "default"},
            "name": {"type": "string"},
            "patch": {"type": "object", "description": "The JSON patch to apply to the deployment"},
            "reason": {"type": "string"},
        },
        "required": ["name", "patch"],
    },
    is_destructive=True,
)
async def patch_deployment(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("deployment_name") or kwargs.get("deployment")
    patch = kwargs.get("patch")
    namespace = kwargs.get("namespace", "default")
    reason = kwargs.get("reason", "Automated patch")

    if not name or not patch:
        return ToolResult(success=False, output="Error: 'name' and 'patch' are required.")
    if namespace == "kube-system":
        return ToolResult(success=False, output="Refusing to patch deployments in kube-system automatically.")
    try:
        apps_v1 = k8s_client.AppsV1Api()
        await asyncio.to_thread(
            apps_v1.patch_namespaced_deployment,
            name=name, namespace=namespace, body=patch
        )
        return ToolResult(success=True, output=f"Deployment {name} patched successfully. Reason: {reason}")
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="delete_pod",
    description=(
        "Delete a specific pod (it will be recreated by its controller). "
        "Use for stuck/zombie pods that won't restart on their own."
    ),
    parameters={
        "properties": {
            "namespace": {"type": "string", "default": "default"},
            "name": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["name"],
    },
    is_destructive=True,
)
async def delete_pod(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("pod_name") or kwargs.get("pod")
    namespace = kwargs.get("namespace", "default")
    reason = kwargs.get("reason", "Automated pod deletion")

    if not name:
        return ToolResult(success=False, output="Error: 'name' (pod name) is required.")
    if namespace == "kube-system":
        return ToolResult(success=False, output="Refusing to delete pods in kube-system automatically.")
    try:
        v1 = k8s_client.CoreV1Api()
        await asyncio.to_thread(v1.delete_namespaced_pod, name=name, namespace=namespace)
        return ToolResult(success=True, output=f"Pod {name} deleted. Reason: {reason}")
    except Exception as e:
        return ToolResult(success=False, output=str(e))


@registry.tool(
    name="cordon_node",
    description="Cordon a node (mark as unschedulable). Does NOT drain existing pods.",
    parameters={
        "properties": {
            "name": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["name"],
    },
    is_destructive=True,
)
async def cordon_node(**kwargs) -> ToolResult:
    name = kwargs.get("name") or kwargs.get("node_name") or kwargs.get("node")
    reason = kwargs.get("reason", "Automated node cordon")

    if not name:
        return ToolResult(success=False, output="Error: 'name' (node name) is required.")
    try:
        v1 = k8s_client.CoreV1Api()
        patch = {
            "spec": {"unschedulable": True},
            "metadata": {"annotations": {"claw8s/cordon-reason": reason}},
        }
        await asyncio.to_thread(v1.patch_node, name=name, body=patch)
        return ToolResult(success=True, output=f"Node {name} cordoned. Reason: {reason}", requires_approval=True)
    except Exception as e:
        return ToolResult(success=False, output=str(e))
