#!/usr/bin/env python3
"""
Render scheduled job — reconciler for missed initial-callback webhooks.

Every hour, scans the ShoeCare project for tasks that have a Service Wizard
link in their notes but NO `Approved by customer` / `Rejected by customer`
marker. Those are tasks where the Asana webhook didn't reach our server
(or arrived during a Render restart, or hit a transient bug). For each
match, runs process_task to belatedly run the initial automation.

This is a safety net. Under normal operation it finds nothing — the live
webhook handles every event. It only catches up after Render restarts,
network blips between Asana and Render, or one-off failures.

Eligibility filter:
  - Task is in the ShoeCare board, not completed
  - Notes contain `Customer approval response:`   (Lovable wrote the link)
  - Notes do NOT contain `Approved by customer` or `Rejected by customer`
    (process_task hasn't yet stamped its marker)
  - modified_at within last 7 days                (don't reach back forever)

`process_task` has its own dedup, so running it on an already-processed
task is a safe no-op. The reconciler is purely additive — it doesn't
modify the live webhook flow.
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import process_task as pt

load_dotenv()

WORKSPACE_GID = os.getenv("ASANA_WORKSPACE_GID", "41091308892039")
PROJECT_GID   = os.getenv("ASANA_PROJECT_GID",   "1202289964354061")

# 7-day lookback. Lovable links older than that are unlikely to suddenly
# need processing — they would've been caught by an earlier reconciler run.
LOOKBACK_WINDOW = timedelta(days=7)


def list_unprocessed_tasks() -> list[str]:
    """Tasks with the Lovable link but no Approved/Rejected marker.

    Uses Asana's task search with `text=Customer approval response` for
    server-side narrowing — much faster than listing every task in the
    project and filtering client-side.
    """
    cutoff = (datetime.now(timezone.utc) - LOOKBACK_WINDOW).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    found: list[str] = []
    skipped: dict[str, int] = {"already_processed": 0, "no_link": 0}

    offset: str | None = None
    while True:
        params: dict = {
            "text":              "Customer approval response",
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
            # Re-check the link client-side; full-text search can match the
            # phrase appearing anywhere (e.g. inside the bot's own comments
            # quoted in description). We need it specifically on a link line.
            if "Customer approval response:" not in notes:
                skipped["no_link"] += 1
                continue
            if "Approved by customer" in notes or "Rejected by customer" in notes:
                skipped["already_processed"] += 1
                continue
            found.append(task["gid"])

        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break

    print(f"Listing summary: unprocessed={len(found)}, "
          f"already_processed={skipped['already_processed']}, "
          f"no_link={skipped['no_link']}")
    return found


def main() -> None:
    started = datetime.now(timezone.utc)
    print(f"=== Reconciler started: {started.isoformat()} ===")

    try:
        unprocessed = list_unprocessed_tasks()
    except Exception as e:
        print(f"FATAL: failed to list tasks: {e}")
        sys.exit(1)

    successes = 0
    failures: list[tuple[str, str]] = []
    for tid in unprocessed:
        try:
            pt.process_task(tid)
            successes += 1
        except Exception as e:
            print(f"  ERROR processing {tid}: {e}")
            failures.append((tid, str(e)))

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\n=== Reconciler complete in {elapsed:.1f}s: "
          f"{successes}/{len(unprocessed)} processed, {len(failures)} failed ===")
    if failures:
        for tid, err in failures:
            print(f"  FAILED {tid}: {err}")


if __name__ == "__main__":
    main()
