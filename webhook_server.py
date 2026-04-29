#!/usr/bin/env python3
"""
Asana webhook server for the Service Wizard automation.

Listens for Asana task:changed events. When an agent pastes a
Service Wizard link into a task description, this server detects
it and runs the full automation (photos, comment, custom fields,
subtask, due date) automatically.
"""

import os
import threading
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from process_task import process_task, extract_service_wizard_link, get_task

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

ASANA_BASE = "https://app.asana.com/api/1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asana_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('ASANA_PAT')}"}


def is_already_processed(task_id: str) -> bool:
    """Return True if our automation has already run on this task.

    We detect this by checking whether the task already has a 'before.jpg'
    attachment — the first thing the automation uploads.
    """
    try:
        r = requests.get(
            f"{ASANA_BASE}/tasks/{task_id}/attachments",
            headers=_asana_headers(),
            timeout=10,
        )
        r.raise_for_status()
        names = {a.get("name") for a in r.json().get("data", [])}
        return "before.jpg" in names
    except Exception as exc:
        log.warning("Could not check attachments for task %s: %s", task_id, exc)
        return False


def handle_task_change(task_id: str) -> None:
    """Background worker: run automation if conditions are met."""
    try:
        log.info("Checking task %s …", task_id)

        task = get_task(task_id)
        notes = task.get("notes", "")

        # 1. Does the description contain a Service Wizard link?
        link = extract_service_wizard_link(notes)
        if not link:
            log.info("Task %s — no Service Wizard link, skipping.", task_id)
            return

        # 2. Have we already processed this task?
        if is_already_processed(task_id):
            log.info("Task %s — already processed, skipping.", task_id)
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
    # On first registration Asana sends X-Hook-Secret and expects it echoed back
    hook_secret = request.headers.get("X-Hook-Secret")
    if hook_secret:
        log.info("Asana webhook handshake — responding with secret.")
        return jsonify({}), 200, {"X-Hook-Secret": hook_secret}

    # ── Event processing ─────────────────────────────────────────────────────
    payload = request.get_json(silent=True) or {}
    events = payload.get("events", [])

    for event in events:
        resource = event.get("resource", {})
        change   = event.get("change", {})

        if (
            resource.get("resource_type") == "task"
            and event.get("action") == "changed"
            and change.get("field") == "notes"
        ):
            task_id = resource.get("gid")
            if task_id:
                log.info("Notes updated on task %s — queuing …", task_id)
                threading.Thread(
                    target=handle_task_change,
                    args=(task_id,),
                    daemon=True,
                ).start()

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    """Railway uses this to verify the service is up."""
    return jsonify({"status": "healthy"}), 200


# ---------------------------------------------------------------------------
# Entry point (local dev only — Railway uses gunicorn via Procfile)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
