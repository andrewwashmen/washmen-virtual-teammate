#!/usr/bin/env python3
"""
Render scheduled job — polls eligible Asana tasks every 10 min and invokes
sync_task on each. sync_task itself is idempotent: it scrapes Supabase +
Jina, compares against the Snapshot key stored in the description, and
short-circuits when nothing changed.

Eligibility filter (cheap, runs on the listing payload):
  - Task is in the ShoeCare board
  - Not completed
  - `Customer approval response:` link in notes  (was processed at all)
  - `Approved by customer` marker             (don't sync rejected tasks)
  - `Snapshot key:` line                      (has a baseline to diff against)
  - modified_at within ACTIVITY_WINDOW        (stop polling stable tasks)

The activity-window filter is an upper bound, not a strict business rule:
the user said "stop polling 2 days after customer approved", and 5 days
since last modification is a safe over-approximation that doesn't require
parsing the approved-at timestamp out of the description.
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import process_task as pt
import sync_task   as st

load_dotenv()

WORKSPACE_GID = os.getenv("ASANA_WORKSPACE_GID", "41091308892039")
PROJECT_GID   = os.getenv("ASANA_PROJECT_GID",   "1202289964354061")

# Tasks not modified within this window are unlikely to receive Lovable
# changes — skip them to keep the polling load bounded. The user's window
# is "2 days after customer approved"; we use 5 days from last modification
# as a safe over-approximation that doesn't require parsing approved_at out
# of the description.
ACTIVITY_WINDOW = timedelta(days=5)


def list_eligible_tasks() -> list[str]:
    """List GIDs of tasks that look eligible for sync.

    Uses Asana's task search endpoint with server-side filters to keep the
    payload small (vs. listing every task in the project, which times out
    on boards with hundreds of tasks):

      - text=Snapshot key  → only tasks the bot has stamped (Phase 1+ tasks)
      - projects.any=...   → confined to the ShoeCare board
      - modified_at.after  → activity-window cutoff
      - completed=false    → ignore done tasks
      - limit=100          → max page size

    Client-side then re-checks Approved-marker + Snapshot-key presence to
    rule out false positives from the full-text search.
    """
    cutoff = (datetime.now(timezone.utc) - ACTIVITY_WINDOW).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    eligible: list[str] = []
    skipped: dict[str, int] = {"not_approved": 0, "no_snapshot_key": 0}

    offset: str | None = None
    while True:
        params: dict = {
            "text":              "Snapshot key",
            "projects.any":      PROJECT_GID,
            "modified_at.after": cutoff,
            "completed":         "false",
            "opt_fields":        "gid,notes,modified_at",
            "limit":             100,
        }
        if offset:
            params["offset"] = offset

        r = requests.get(
            f"{pt.ASANA_BASE}/workspaces/{WORKSPACE_GID}/tasks/search",
            headers=pt._asana_headers(),
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()

        for task in body.get("data", []):
            notes = task.get("notes") or ""
            if "Approved by customer" not in notes:
                skipped["not_approved"] += 1
                continue
            if "Snapshot key:" not in notes:
                skipped["no_snapshot_key"] += 1
                continue
            eligible.append(task["gid"])

        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break

    print(f"Listing summary: eligible={len(eligible)}, skipped={skipped}")
    return eligible


def main() -> None:
    started = datetime.now(timezone.utc)
    print(f"=== Polling started: {started.isoformat()} ===")

    try:
        eligible = list_eligible_tasks()
    except Exception as e:
        print(f"FATAL: failed to list tasks: {e}")
        sys.exit(1)

    successes = 0
    failures: list[tuple[str, str]] = []
    for task_id in eligible:
        try:
            st.sync_task(task_id, dry_run=False)
            successes += 1
        except Exception as e:
            print(f"  ERROR syncing {task_id}: {e}")
            failures.append((task_id, str(e)))

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\n=== Polling complete in {elapsed:.1f}s: "
          f"{successes}/{len(eligible)} synced, {len(failures)} failed ===")
    if failures:
        for tid, err in failures:
            print(f"  FAILED {tid}: {err}")


if __name__ == "__main__":
    main()
