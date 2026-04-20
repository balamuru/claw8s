"""
claw8s.main
-------------
Entry point. Wires together:
  - KubernetesWatcher (background thread)
  - asyncio incident queue
  - Claw8sAgent (processes each incident)
  - TelegramBot (alerts out + approval in)
  - AuditLog (SQLite)

Run with:
  python -m claw8s.main
  python -m claw8s.main --config config.yaml
"""

import asyncio
import json
import logging
import signal
import sys
import argparse

from config import load_config
from audit import AuditLog, AuditEvent, now_iso
from watcher import KubernetesWatcher, Incident
from agent import Claw8sAgent, AgentResult
from tools.kubectl import registry as tool_registry
from bot.telegram import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("claw8s")


async def cluster_status_summary() -> str:
    """Quick cluster health snapshot for the /status command."""
    from kubernetes import client as k8s_client
    try:
        v1 = k8s_client.CoreV1Api()
        nodes = await asyncio.to_thread(v1.list_node)
        pods = await asyncio.to_thread(v1.list_pod_for_all_namespaces)

        total_nodes = len(nodes.items)
        ready_nodes = sum(
            1 for n in nodes.items
            if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
        )

        total_pods = len(pods.items)
        running_pods = sum(1 for p in pods.items if p.status.phase == "Running")
        failed_pods = sum(1 for p in pods.items if p.status.phase in ("Failed", "Unknown"))

        return (
            f"🖥️ Nodes: {ready_nodes}/{total_nodes} ready\n"
            f"🐳 Pods: {running_pods}/{total_pods} running, {failed_pods} failed"
        )
    except Exception as e:
        return f"Error getting status: {e}"


async def main(config_path: str = "config.yaml"):
    cfg = load_config(config_path)

    # ── Audit log ──────────────────────────────────────────────────
    audit = AuditLog(cfg.audit.db_path)
    await audit.connect()
    log.info(f"Audit log connected: {cfg.audit.db_path}")

    # ── Telegram bot ────────────────────────────────────────────────
    bot: TelegramBot | None = None
    if cfg.telegram.enabled:
        bot = TelegramBot(
            cfg=cfg.telegram,
            token=cfg.telegram_bot_token,
            audit=audit,
            cluster_status_fn=cluster_status_summary,
        )
        await bot.start()
        log.info("Telegram bot started")

    # ── Approval callback ───────────────────────────────────────────
    async def approval_callback(incident_id, tool_name, tool_args, reasoning, confidence) -> bool:
        if bot:
            return await bot.request_approval(incident_id, tool_name, tool_args, reasoning, confidence)
        return False  # no bot = never auto-approve destructive actions

    # ── Agent ───────────────────────────────────────────────────────
    agent = Claw8sAgent(
        cfg=cfg.agent,
        api_key=cfg.anthropic_api_key,
        tool_registry=tool_registry,
        audit=audit,
        approval_callback=approval_callback,
    )

    # ── Incident queue + watcher ────────────────────────────────────
    incident_queue: asyncio.Queue[Incident] = asyncio.Queue()
    loop = asyncio.get_event_loop()
    watcher = KubernetesWatcher(cfg.watcher, incident_queue, cfg.kubeconfig_path)
    watcher.start(loop)
    log.info("K8s watcher started — watching for incidents...")

    if bot:
        await bot.send_alert("🦅 *Claw8s is online* and watching your cluster.")

    # ── Main incident processing loop ───────────────────────────────
    async def process_incidents():
        while True:
            incident: Incident = await incident_queue.get()
            log.info(f"Processing incident: {incident.id} [{incident.reason}] {incident.object_name}")

            # Log the raw event
            await audit.log_event(AuditEvent(
                incident_id=incident.id,
                timestamp=incident.timestamp,
                namespace=incident.namespace,
                object_kind=incident.object_kind,
                object_name=incident.object_name,
                reason=incident.reason,
                message=incident.message,
                raw_event=json.dumps(incident.raw),
            ))

            # Alert: incident detected
            if bot:
                await bot.send_alert(
                    f"🚨 *Incident detected*\n\n"
                    f"`{incident.reason}` on `{incident.object_kind}/{incident.object_name}`\n"
                    f"Namespace: `{incident.namespace}`\n"
                    f"_{incident.message[:200]}_\n\n"
                    f"🔍 Investigating..."
                )

            # Run agent
            try:
                result: AgentResult = await agent.run(incident)
                log.info(f"Agent finished incident {incident.id}: needs_human={result.needs_human}")

                # Push result to Telegram
                if bot:
                    emoji = "⚠️" if result.needs_human else "✅"
                    actions_summary = ""
                    if result.actions_taken:
                        lines = [f"  • `{a['tool']}` → {'✓' if a['success'] else '✗'}" for a in result.actions_taken]
                        actions_summary = "\n*Actions:*\n" + "\n".join(lines) + "\n\n"

                    await bot.send_alert(
                        f"{emoji} *Incident resolved*\n\n"
                        f"ID: `{incident.id[:8]}`\n"
                        f"{actions_summary}"
                        f"*Summary:* {result.summary}"
                    )

                    if result.needs_human and result.human_message:
                        await bot.send_alert(f"👤 *Human attention needed:*\n\n{result.human_message}")

            except Exception as e:
                log.error(f"Agent failed on incident {incident.id}: {e}", exc_info=True)
                if bot:
                    await bot.send_alert(f"💥 *Agent error* on incident `{incident.id[:8]}`:\n`{e}`")

            incident_queue.task_done()

    # ── Graceful shutdown ───────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def handle_signal():
        log.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    processor_task = asyncio.create_task(process_incidents())

    await shutdown_event.wait()

    log.info("Shutting down...")
    processor_task.cancel()
    watcher.stop()
    if bot:
        await bot.stop()
    await audit.close()
    log.info("Claw8s stopped.")


def run():
    parser = argparse.ArgumentParser(description="Claw8s — autonomous K8s ops agent")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()
    asyncio.run(main(args.config))


if __name__ == "__main__":
    run()
