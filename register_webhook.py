#!/usr/bin/env python3
"""
Register (or re-register) the Asana webhook for the ShoeCare board.

The webhook subscribes to two event types on the project:
  - task:changed:notes      — fires when Lovable adds the initial
    `Customer approval response: <link>` to a task description, so the bot
    can run the initial process_task pipeline.
  - story:added             — fires when Lovable posts a new comment to a
    task. The bot filters those for "Approval link:" text (Lovable's save
    signal) and runs sync_task on the parent task.

This script is safe to re-run — it deletes any existing webhook for the
same project + target URL before creating a new one.

Usage:
  1. Add SERVER_URL to .env  (e.g. https://your-app.onrender.com)
  2. python register_webhook.py
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

PAT         = os.getenv("ASANA_PAT")
PROJECT_GID = os.getenv("ASANA_PROJECT_GID", "1202289964354061")
SERVER_URL  = os.getenv("SERVER_URL", "").rstrip("/")

if not PAT:
    raise SystemExit("ERROR: ASANA_PAT is not set in your .env file.")
if not SERVER_URL:
    print("ERROR: Set SERVER_URL in your .env file.")
    print("  Example: SERVER_URL=https://your-app.onrender.com")
    raise SystemExit(1)

ASANA_BASE = "https://app.asana.com/api/1.0"
TARGET_URL = f"{SERVER_URL}/webhook"
HEADERS    = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}

FILTERS = [
    {"resource_type": "task",  "action": "changed", "fields": ["notes"]},
    {"resource_type": "story", "action": "added"},
]


def list_existing_webhooks() -> list[dict]:
    """Fetch all webhooks in the workspace targeting the same URL+project."""
    # First get the workspace from the project
    pr = requests.get(f"{ASANA_BASE}/projects/{PROJECT_GID}",
                      headers=HEADERS,
                      params={"opt_fields": "workspace.gid"},
                      timeout=15)
    pr.raise_for_status()
    workspace_gid = pr.json()["data"]["workspace"]["gid"]

    out = []
    offset = None
    while True:
        params = {"workspace": workspace_gid, "opt_fields": "target,resource.gid,active",
                  "limit": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(f"{ASANA_BASE}/webhooks", headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        body = r.json()
        for w in body.get("data", []):
            if w.get("target") == TARGET_URL and (w.get("resource") or {}).get("gid") == PROJECT_GID:
                out.append(w)
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break
    return out


def delete_webhook(gid: str) -> None:
    r = requests.delete(f"{ASANA_BASE}/webhooks/{gid}", headers=HEADERS, timeout=15)
    r.raise_for_status()


def create_webhook() -> dict:
    r = requests.post(
        f"{ASANA_BASE}/webhooks",
        headers=HEADERS,
        json={"data": {
            "resource": PROJECT_GID,
            "target":   TARGET_URL,
            "filters":  FILTERS,
        }},
        timeout=15,
    )
    if not r.ok:
        print(f"\nFailed to create webhook ({r.status_code}):")
        print(r.json())
        raise SystemExit(1)
    return r.json()["data"]


print(f"Registering webhook …")
print(f"  Project : {PROJECT_GID}")
print(f"  Target  : {TARGET_URL}")
print(f"  Filters : task:changed:notes, story:added")
print()

existing = list_existing_webhooks()
if existing:
    print(f"Found {len(existing)} existing webhook(s) for this project+target — deleting:")
    for w in existing:
        print(f"  delete {w['gid']} (active={w.get('active')})")
        delete_webhook(w["gid"])

new = create_webhook()
print()
print(f"Webhook registered:")
print(f"  GID    : {new['gid']}")
print(f"  Target : {new['target']}")
print(f"  Active : {new.get('active', True)}")
