#!/usr/bin/env python3
"""
Asana webhook server for the Service Wizard automation.

Listens for Asana task:changed events on the `notes` field. When the
asana-shooter worker (or anyone) appends a Service Wizard "Customer approval
response" link to a task description, this server detects it and runs the
full automation:

  - subtasks per approved service
  - "Approved by customer" / "Rejected by customer" line appended to description
  - internal notes appended to description (when present)
  - Stains & Damages comment with damage photos attached (when present)
  - Per-service comment with that service's photos attached (when present)
  - Price custom field set to total (cleared on rejection)
  - due date set to today + total TAT days (cleared on rejection)

Deduplication is handled inside process_task() — it skips if any existing
subtask name matches an approved service name on the page.
"""

import os
import threading
import logging
import requests
from collections import defaultdict
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from process_task import process_task, find_link, get_task, ASANA_BASE, _asana_headers
from sync_task    import sync_task
from notify       import notify_error, CTX_INITIAL, CTX_CHANGE

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Per-task in-flight guard
# ---------------------------------------------------------------------------
# Asana fires a follow-up webhook when process_task writes the description
# marker — that event would otherwise spawn a redundant thread, and if the
# bot's first run + the follow-up overlap by milliseconds, both threads race
# past dedup and post duplicate comments / attachments.
#
# Single-worker deployment means this set is authoritative for the whole
# server (multi-worker would need Redis-backed coordination, but volume
# doesn't justify that here).
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# In-process metrics
# ---------------------------------------------------------------------------
# Lightweight counters exposed via GET /metrics. Process-local — they reset
# whenever Render restarts the worker, and reflect only the web service's
# observed activity (reconcilers run in separate processes and report via
# their own log lines). Use as a quick health snapshot, not as an SLO source.

_METRICS_LOCK = threading.Lock()
_METRICS: dict = {
    "started_at":             datetime.now(timezone.utc).isoformat(),
    "events_received_total":  0,
    "events_by_action":       defaultdict(int),
    "tasks_processed_total":  0,
    "tasks_failed_total":     0,
    "syncs_processed_total":  0,
    "syncs_failed_total":     0,
    "last_event_at":          None,
    "last_error_at":          None,
    "last_error_context":     None,
    "last_error_message":     None,
}


def _bump(key: str, n: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[key] = _METRICS.get(key, 0) + n


def _bump_action(action: str) -> None:
    with _METRICS_LOCK:
        _METRICS["events_by_action"][action] += 1


def _record_error(context: str, exc: Exception) -> None:
    with _METRICS_LOCK:
        _METRICS["last_error_at"]      = datetime.now(timezone.utc).isoformat()
        _METRICS["last_error_context"] = context
        _METRICS["last_error_message"] = f"{type(exc).__name__}: {exc}"[:300]


def _stamp_event() -> None:
    with _METRICS_LOCK:
        _METRICS["last_event_at"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def handle_task_change(task_id: str) -> None:
    """Run automation if a Service Wizard link is in the description.

    Skipped (no-op) if another thread is already processing this task. The
    running thread will see the latest task state when it next refetches, so
    we don't lose any updates by skipping.
    """
    with _IN_FLIGHT_LOCK:
        if task_id in _IN_FLIGHT:
            log.info("Task %s — already in flight, skipping duplicate event.", task_id)
            return
        _IN_FLIGHT.add(task_id)

    task: dict | None = None
    try:
        log.info("Checking task %s …", task_id)

        task = get_task(task_id)
        notes = task.get("notes", "") or ""

        if not find_link(notes):
            log.info("Task %s — no Service Wizard link in description.", task_id)
            return

        log.info("Task %s — link found, running automation …", task_id)
        process_task(task_id)
        log.info("Task %s — automation complete.", task_id)
        _bump("tasks_processed_total")

    except Exception as exc:
        log.error("Error processing task %s: %s", task_id, exc, exc_info=True)
        notify_error(task_id, exc, CTX_INITIAL, task_name=(task or {}).get("name"))
        _bump("tasks_failed_total")
        _record_error(CTX_INITIAL, exc)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(task_id)


def handle_story_added(task_id: str, story_id: str) -> None:
    """Trigger sync_task when Lovable posts an `Approval link:` comment.

    Lovable adds an `Approval link: <url>` comment to the task each time the
    customer/operator saves a change in the Service Wizard. We treat that
    comment as a signal that Supabase has new data, and run sync_task to
    pick up the diff.

    Filters:
      - Story must be a comment (`resource_subtype == comment_added`) — handled
        by the dispatcher before calling this function
      - Comment text must contain `Approval link:` — distinguishes Lovable's
        save signal from operator chatter and avoids loops with the bot's own
        `Updates synced ...` and `Stains & Damages` comments
    """
    with _IN_FLIGHT_LOCK:
        if task_id in _IN_FLIGHT:
            log.info("Task %s — already in flight, skipping story trigger.", task_id)
            return
        _IN_FLIGHT.add(task_id)

    try:
        r = requests.get(
            f"{ASANA_BASE}/stories/{story_id}",
            headers=_asana_headers(),
            params={"opt_fields": "text"},
            timeout=15,
        )
        r.raise_for_status()
        text = ((r.json() or {}).get("data") or {}).get("text") or ""

        if "Approval link:" not in text:
            log.info("Task %s — comment isn't a Lovable signal, skipping.", task_id)
            return

        log.info("Task %s — Lovable signal detected, running sync_task …", task_id)
        sync_task(task_id, dry_run=False)
        log.info("Task %s — sync complete.", task_id)
        _bump("syncs_processed_total")

    except Exception as exc:
        log.error("Error syncing task %s after story event: %s", task_id, exc, exc_info=True)
        try:
            task_name = get_task(task_id).get("name")
        except Exception:
            task_name = None
        notify_error(task_id, exc, CTX_CHANGE, task_name=task_name)
        _bump("syncs_failed_total")
        _record_error(CTX_CHANGE, exc)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(task_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    # ── Asana handshake ──────────────────────────────────────────────────────
    # On first registration Asana sends X-Hook-Secret and expects it echoed back.
    hook_secret = request.headers.get("X-Hook-Secret")
    if hook_secret:
        log.info("Asana webhook handshake — responding with secret.")
        return jsonify({}), 200, {"X-Hook-Secret": hook_secret}

    # ── Event processing ─────────────────────────────────────────────────────
    payload = request.get_json(silent=True) or {}
    events  = payload.get("events", [])
    if events:
        _bump("events_received_total", len(events))
        _stamp_event()

    queued_tasks = set()
    queued_stories = set()
    for event in events:
        resource = event.get("resource", {})
        rtype    = resource.get("resource_type")
        action   = event.get("action")
        change   = event.get("change", {})

        # Initial Lovable post: link appears in the task notes → process_task
        if rtype == "task" and action == "changed" and change.get("field") == "notes":
            _bump_action("task_changed_notes")
            task_id = resource.get("gid")
            if task_id and task_id not in queued_tasks:
                queued_tasks.add(task_id)
                log.info("Notes updated on task %s — queuing process_task …", task_id)
                threading.Thread(
                    target=handle_task_change,
                    args=(task_id,),
                    daemon=True,
                ).start()

        # Subsequent Lovable saves: an `Approval link:` comment is added to
        # the task → sync_task to pick up the new snapshot/damages/notes
        elif rtype == "story" and action == "added" and resource.get("resource_subtype") == "comment_added":
            _bump_action("story_added")
            story_id = resource.get("gid")
            parent   = event.get("parent") or {}
            task_id  = parent.get("gid") if parent.get("resource_type") == "task" else None
            if task_id and story_id and (task_id, story_id) not in queued_stories:
                queued_stories.add((task_id, story_id))
                log.info("Comment %s added on task %s — queuing story trigger …", story_id, task_id)
                threading.Thread(
                    target=handle_story_added,
                    args=(task_id, story_id),
                    daemon=True,
                ).start()
        else:
            _bump_action("ignored")

    return jsonify({
        "status":  "ok",
        "tasks":   len(queued_tasks),
        "stories": len(queued_stories),
    }), 200


@app.route("/health", methods=["GET"])
def health():
    """Render and other PaaS hosts hit this to verify the service is up."""
    return jsonify({"status": "healthy"}), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    """Process-local counters for the web service. Resets on Render restart.

    Reflects only what this worker has observed — reconcilers run in separate
    processes (washmen-virtual-teammate-reconciler / -sync-reconciler) and
    log their own summaries. Use as a quick health snapshot, not an SLO source.
    """
    now = datetime.now(timezone.utc)
    with _METRICS_LOCK:
        started_at = _METRICS["started_at"]
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime_seconds = int((now - started_dt).total_seconds())
        except Exception:
            uptime_seconds = None
        snapshot = {
            "started_at":             started_at,
            "uptime_seconds":         uptime_seconds,
            "events_received_total":  _METRICS["events_received_total"],
            "events_by_action":       dict(_METRICS["events_by_action"]),
            "tasks_processed_total":  _METRICS["tasks_processed_total"],
            "tasks_failed_total":     _METRICS["tasks_failed_total"],
            "syncs_processed_total":  _METRICS["syncs_processed_total"],
            "syncs_failed_total":     _METRICS["syncs_failed_total"],
            "last_event_at":          _METRICS["last_event_at"],
            "last_error_at":          _METRICS["last_error_at"],
            "last_error_context":     _METRICS["last_error_context"],
            "last_error_message":     _METRICS["last_error_message"],
        }
    # Read this outside the metrics lock — it has its own.
    with _IN_FLIGHT_LOCK:
        snapshot["in_flight_count"] = len(_IN_FLIGHT)
    return jsonify(snapshot), 200


# ---------------------------------------------------------------------------
# Local dev entry — production uses gunicorn via Procfile / startCommand
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
