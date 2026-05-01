#!/usr/bin/env python3
"""
Asana webhook server for the Service Wizard automation.

Listens for Asana task:changed events on the `notes` field. When the
asana-shooter worker (or anyone) appends a Service Wizard "Customer approval
response" link to a task description, this server detects it and runs the
full automation:

  - subtasks per approved service
  - "Approved by customer" + approver line appended to description
  - internal notes appended to description (when present)
  - Stains & Damages comment (when present)
  - per-service photo comments (when present)
  - Price custom field set to total
  - due date set to today + total TAT days

Deduplication is handled inside process_task() — it skips if any existing
subtask name matches an approved service name on the page.
"""

import os
import threading
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from process_task import process_task, find_link, get_task

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def handle_task_change(task_id: str) -> None:
    """Run automation if a Service Wizard link is in the description.
    process_task() handles its own dedup (existing-subtask check)."""
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

    except Exception as exc:
        log.error("Error processing task %s: %s", task_id, exc, exc_info=True)


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

    queued = set()
    for event in events:
        resource = event.get("resource", {})
        change   = event.get("change", {})

        if (
            resource.get("resource_type") == "task"
            and event.get("action") == "changed"
            and change.get("field") == "notes"
        ):
            task_id = resource.get("gid")
            if task_id and task_id not in queued:
                queued.add(task_id)
                log.info("Notes updated on task %s — queuing …", task_id)
                threading.Thread(
                    target=handle_task_change,
                    args=(task_id,),
                    daemon=True,
                ).start()

    return jsonify({"status": "ok", "queued": len(queued)}), 200


@app.route("/health", methods=["GET"])
def health():
    """Render and other PaaS hosts hit this to verify the service is up."""
    return jsonify({"status": "healthy"}), 200


# ---------------------------------------------------------------------------
# Local dev entry — production uses gunicorn via Procfile / startCommand
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
