#!/usr/bin/env python3
"""
Render scheduled job — reconciler for missed CHANGE-sync events.

Every 15 minutes, scans the ShoeCare project for tasks that have already
been initially processed (i.e., have a `Snapshot key:` line in the
description) and compares the stored key against Supabase's current
state. If they differ, runs sync_task to apply the change.

This is the safety net for the "Lovable changed Supabase but didn't post
the `Approval link:` trigger comment" case. Under normal operation it
finds nothing — Lovable's comment fires a webhook that syncs in real-
time. The reconciler only catches the gaps.

Pure additive — does not modify the live webhook flow, the initial-
callback reconciler, or any existing code path. Disabling = removing the
cron service from render.yaml.

Eligibility filter:
  - Task is in the ShoeCare board, not completed
  - Notes contain `Approved by customer` (skip rejected per design Q2)
  - Notes contain `Snapshot key:` line (baseline to compare against)
  - modified_at within last 7 days (don't reach back forever)

Cheap pre-check uses only Supabase (no Jina) — invokes sync_task only
when snapshot version or damage IDs/counts have changed. Pure internal-
note edits (no snapshot/damage change) won't fire this reconciler since
they're not part of the snapshot key — those still rely on Lovable's
real-time webhook trigger. That's an accepted trade-off to keep this
reconciler cheap (Jina rate limits otherwise).
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import process_task as pt
import sync_task   as st
from notify import notify_error, CTX_RECON

load_dotenv()

WORKSPACE_GID = os.getenv("ASANA_WORKSPACE_GID", "41091308892039")
PROJECT_GID   = os.getenv("ASANA_PROJECT_GID",   "1202289964354061")

LOOKBACK_WINDOW = timedelta(days=7)


def list_eligible_tasks() -> list[dict]:
    """Tasks that have been initially processed and might have stale state.

    Server-side narrowing via Asana's task search (`text=Snapshot key`)
    plus modified_at + completed filters. Client-side rechecks the marker
    text to weed out false positives from the full-text search.
    """
    cutoff = (datetime.now(timezone.utc) - LOOKBACK_WINDOW).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    eligible: list[dict] = []
    skipped: dict[str, int] = {"not_approved": 0, "no_snapshot_key": 0}

    offset: str | None = None
    while True:
        params: dict = {
            "text":              "Snapshot key",
            "projects.any":      PROJECT_GID,
            "modified_at.after": cutoff,
            "completed":         "false",
            "opt_fields":        "gid,name,notes,modified_at",
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
            eligible.append(task)

        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break

    print(f"Listing summary: eligible={len(eligible)}, skipped={skipped}")
    return eligible


def is_stale(task: dict) -> bool:
    """Cheap check: does the stored Snapshot key differ from Supabase's
    current state? Only Supabase queries — no Jina scrape.
    """
    notes = task.get("notes") or ""
    stored = st.parse_snapshot_key(notes)
    if not stored:
        return False

    link = pt.find_link(notes)
    if not link:
        return False
    shoe_id = pt.find_shoe_id(link)
    if not shoe_id:
        return False

    shoe_data = pt.fetch_shoe_data(shoe_id)
    if not shoe_data["snapshot"]:
        return False

    current_key = pt.compute_snapshot_key(shoe_data["snapshot"], shoe_data["damages"])
    return current_key != stored["raw"]


def main() -> None:
    started = datetime.now(timezone.utc)
    print(f"=== Sync reconciler started: {started.isoformat()} ===")

    try:
        eligible = list_eligible_tasks()
    except Exception as e:
        print(f"FATAL: failed to list tasks: {e}")
        sys.exit(1)

    stale: list[dict] = []
    for task in eligible:
        try:
            if is_stale(task):
                stale.append(task)
        except Exception as e:
            print(f"  pre-check ERROR for {task.get('gid')}: {e}")
            try:
                notify_error(task.get("gid"), e, CTX_RECON, task_name=task.get("name"))
            except Exception:
                pass

    print(f"Stale: {len(stale)}/{len(eligible)} tasks need sync")

    successes = 0
    failures: list[tuple[str, str]] = []
    for task in stale:
        tid = task["gid"]
        try:
            st.sync_task(tid, dry_run=False)
            successes += 1
        except Exception as e:
            print(f"  ERROR syncing {tid}: {e}")
            failures.append((tid, str(e)))
            try:
                notify_error(tid, e, CTX_RECON, task_name=task.get("name"))
            except Exception:
                pass

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\n=== Sync reconciler complete in {elapsed:.1f}s: "
          f"{successes}/{len(stale)} synced, {len(failures)} failed ===")
    if failures:
        for tid, err in failures:
            print(f"  FAILED {tid}: {err}")


if __name__ == "__main__":
    main()
