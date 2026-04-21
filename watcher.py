"""
claw8s.watcher
----------------
Watches Kubernetes events via the Watch API and feeds a debounced
asyncio Queue with normalized incident objects.

Key design decisions:
- We watch the core/v1 Events resource (not pod logs, not metrics).
  Events are the highest signal-to-noise source for operational issues.
- Debouncing: the same (namespace, object, reason) tuple won't re-trigger
  the agent more often than watcher_config.debounce_seconds.
- Runs in a thread (kubernetes watch is sync) bridged into asyncio.
"""

import asyncio
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from kubernetes import client, config as k8s_config, watch

from config import WatcherConfig
from audit import AuditEvent, now_iso

log = logging.getLogger(__name__)


@dataclass
class Incident:
    """A normalized, deduplicated K8s event worth investigating."""
    id: str
    timestamp: str
    namespace: str
    object_kind: str
    object_name: str
    reason: str
    message: str
    count: int
    raw: dict


class KubernetesWatcher:
    def __init__(self, cfg: WatcherConfig, queue: asyncio.Queue, kubeconfig_path: str = ""):
        self.cfg = cfg
        self.queue = queue
        self._debounce: dict[tuple, float] = {}  # key → last_triggered epoch
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Load kubeconfig
        if kubeconfig_path:
            k8s_config.load_kube_config(config_file=kubeconfig_path)
        else:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()

        self.v1 = client.CoreV1Api()
        self.start_time = datetime.now(timezone.utc)

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start the watcher in a background thread."""
        t = threading.Thread(target=self._watch_loop, args=(loop,), daemon=True)
        t.start()
        log.info("K8s watcher started")

    def stop(self):
        self._stop_event.set()

    def _watch_loop(self, loop: asyncio.AbstractEventLoop):
        w = watch.Watch()
        try:
            namespace = "" if self.cfg.watch_all_namespaces else None
            kwargs = {"timeout_seconds": 0}  # indefinite stream

            stream = (
                w.stream(self.v1.list_event_for_all_namespaces, **kwargs)
                if self.cfg.watch_all_namespaces
                else w.stream(self.v1.list_namespaced_event, namespace=self.cfg.namespaces[0], **kwargs)
            )

            for raw_event in stream:
                if self._stop_event.is_set():
                    break

                event_type = raw_event.get("type", "")
                obj = raw_event.get("object")
                if not obj or event_type not in ("ADDED", "MODIFIED"):
                    continue

                self._process_event(obj, loop)

        except Exception as e:
            log.error(f"Watcher error: {e}", exc_info=True)
        finally:
            w.stop()

    def _process_event(self, obj, loop: asyncio.AbstractEventLoop):
        try:
            # 1. Only process Warning events (ignore Normal events)
            if obj.type != "Warning":
                return

            # 2. Reason filter
            reason = obj.reason or ""
            if reason not in self.cfg.trigger_reasons:
                return

            # 3. Ignore old events from before the agent started
            event_time = obj.last_timestamp or obj.first_timestamp or datetime.now(timezone.utc)
            if event_time < self.start_time:
                return

            # 4. Initial silence window (skip events in the first 5 seconds of connection)
            if (datetime.now(timezone.utc) - self.start_time).total_seconds() < 5:
                return

            namespace = obj.metadata.namespace or "default"
            involved = obj.involved_object
            object_kind = involved.kind or "Unknown"
            object_name = involved.name or "unknown"
            message = obj.message or ""
            count = obj.count or 1

            # Debounce check
            debounce_key = (namespace, object_kind, object_name, reason)
            now = datetime.now(timezone.utc).timestamp()
            with self._lock:
                last = self._debounce.get(debounce_key, 0)
                if now - last < self.cfg.debounce_seconds:
                    return
                self._debounce[debounce_key] = now

            incident = Incident(
                id=str(uuid.uuid4()),
                timestamp=now_iso(),
                namespace=namespace,
                object_kind=object_kind,
                object_name=object_name,
                reason=reason,
                message=message,
                count=count,
                raw={
                    "namespace": namespace,
                    "kind": object_kind,
                    "name": object_name,
                    "reason": reason,
                    "message": message,
                    "count": count,
                },
            )

            log.info(f"Incident queued: [{reason}] {object_kind}/{object_name} in {namespace}")
            asyncio.run_coroutine_threadsafe(self.queue.put(incident), loop)

        except Exception as e:
            log.error(f"Error processing event: {e}", exc_info=True)
