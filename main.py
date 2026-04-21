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
from datetime import datetime, timezone, date
from typing import Optional
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
    print(f"[BOOT] main({config_path}) entry")
    cfg = load_config(config_path)
    print(f"[BOOT] Config loaded: {config_path}")

    # ── Audit log ──────────────────────────────────────────────────
    print(f"[BOOT] Connecting to Audit DB: {cfg.audit.db_path}")
    audit = AuditLog(cfg.audit.db_path)
    await audit.connect()
    log.info(f"Audit log connected: {cfg.audit.db_path}")
    print(f"[BOOT] Audit DB connected.")

    # Run initial purge
    if cfg.audit.retention_days > 0:
        await audit.purge_old_records(cfg.audit.retention_days)

    # ── Telegram bot ────────────────────────────────────────────────
    bot: TelegramBot | None = None
    if cfg.telegram.enabled:
        print("[BOOT] Initializing Telegram bot...")
        bot = TelegramBot(
            cfg=cfg.telegram,
            token=cfg.telegram_bot_token,
            audit=audit,
            cluster_status_fn=cluster_status_summary,
        )
        print("[BOOT] Spawning Telegram bot task...")
        async def start_with_logging():
            try:
                await bot.start()
                print("[BOOT] Telegram bot background task completed successfully.")
            except Exception as e:
                print(f"❌ [BOOT] FATAL: Telegram bot failed to start: {e}")
                log.error(f"Telegram bot startup failed: {e}", exc_info=True)
        
        asyncio.create_task(start_with_logging())
        print("[BOOT] Telegram bot task spawned.")

    # ── Approval callback ───────────────────────────────────────────
    async def approval_callback(incident_id, tool_name, tool_args, reasoning, confidence) -> bool:
        if bot:
            return await bot.request_approval(incident_id, tool_name, tool_args, reasoning, confidence)
        return False  # no bot = never auto-approve destructive actions

    # ── Agent ───────────────────────────────────────────────────────
    print("[BOOT] Initializing Agent...")
    agent = Claw8sAgent(
        cfg=cfg.agent,
        api_key=cfg.llm_api_key,
        tool_registry=tool_registry,
        audit=audit,
        approval_callback=approval_callback,
    )

    # ── Incident queue + watcher ────────────────────────────────────
    print("[BOOT] Starting K8s Watcher...")
    incident_queue: asyncio.Queue[Incident] = asyncio.Queue()
    loop = asyncio.get_event_loop()
    watcher = KubernetesWatcher(cfg.watcher, incident_queue, cfg.kubeconfig_path)
    watcher.start(loop)
    log.info("K8s watcher started — watching for incidents...")
    print("[BOOT] K8s Watcher active.")

    def escape(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ── Main incident processing loop ───────────────────────────────
    async def process_incidents():
        log.info("[SYSTEM] Incident processor thread started and listening.")
        while True:
            incident: Incident = await incident_queue.get()
            log.info(f"--- [DEQUEUE] Processing Incident: {incident.id[:8]} ({incident.reason}) ---")
            
            # Spawn a task so investigations can run concurrently
            asyncio.create_task(handle_incident(incident))

    async def handle_incident(incident: Incident):
        # Log the raw event
        log.info(f"Pre-flight: Persisting incident {incident.id[:8]} to audit database...")
        
        def json_serial(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")

        try:
            await audit.log_event(AuditEvent(
                incident_id=incident.id,
                timestamp=incident.timestamp,
                namespace=incident.namespace,
                object_kind=incident.object_kind,
                object_name=incident.object_name,
                reason=incident.reason,
                message=incident.message,
                raw_event=json.dumps(incident.raw, default=json_serial),
            ))
        except Exception as e:
            log.error(f"Audit Database Error: {e}")
            
        log.info(f"Handoff: Triggering agent reasoning for {incident.id[:8]}...")

        # Alert: incident detected
        if bot:
            await bot.send_alert(
                f"🚨 <b>Incident detected</b>\n\n"
                f"<code>{incident.reason}</code> on <code>{incident.object_kind}/{incident.object_name}</code>\n"
                f"Namespace: <code>{incident.namespace}</code>\n"
                f"<i>{escape(incident.message[:200])}</i>\n\n"
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
                    lines = [f"  • <code>{a['tool']}</code> → {'✓' if a['success'] else '✗'}" for a in result.actions_taken]
                    actions_summary = "\n<b>Actions:</b>\n" + "\n".join(lines) + "\n\n"

                await bot.send_alert(
                    f"{emoji} <b>Incident resolved</b>\n\n"
                    f"ID: <code>{incident.id[:8]}</code>\n"
                    f"{actions_summary}"
                    f"<b>Summary:</b> {result.summary}"
                )

                if result.needs_human and result.human_message:
                    await bot.send_alert(f"👤 <b>Human attention needed:</b>\n\n{result.human_message}")

        except Exception as e:
            log.error(f"Agent failed on incident {incident.id}: {e}", exc_info=True)
            if bot:
                await bot.send_alert(f"💥 <b>Agent error</b> on incident <code>{incident.id[:8]}</code>:\n<code>{e}</code>")
        finally:
            incident_queue.task_done()

    # ── Graceful shutdown ───────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def handle_signal():
        log.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # ── Start Background Tasks ──────────────────────────────────────
    log.info("[SYSTEM] Starting incident processor...")
    processor_task = asyncio.create_task(process_incidents())
    
    if bot:
        log.info("[SYSTEM] Sending online alert (background)...")
        asyncio.create_task(bot.send_alert("🦅 <b>Claw8s is online</b> and watching your cluster."))

    # ── Dashboard Server ───────────────────────────────────────────
    import uvicorn
    from dashboard.api import app as dashboard_app
    
    log.info("[SYSTEM] Initializing dashboard server...")
    config = uvicorn.Config(dashboard_app, host="0.0.0.0", port=9090, log_level="error")
    server = uvicorn.Server(config)
    
    # Run dashboard in background task
    dashboard_task = asyncio.create_task(server.serve())
    log.info("Dashboard started on http://localhost:9090")

    log.info("[SYSTEM] Boot sequence complete. Monitoring active.")
    await shutdown_event.wait()

    log.info("Shutting down...")
    dashboard_task.cancel()
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
