#!/usr/bin/env python3
"""
Change-detection sync for tasks already processed by process_task.py.

When invoked on a task, compares the current Supabase state against the
`Snapshot key:` line stored in the task description. If changed, syncs the
Asana task state to match (subtasks, attachments, description fields) and
posts a high-level summary comment listing what changed.

Manual invocation:
    python sync_task.py <task_id> [--dry-run]

Phase 3 will wire this into a Render scheduled job that polls eligible tasks
every 10 minutes.

Eligibility for sync (caller's job):
    - Description contains `Customer approval response:` link
    - Description contains `Approved by customer` (we don't sync rejected
      orders — customers can't change their decision via the same link)
    - approved_at + 2 days >= today (skip after the change window closes)
    - Description contains a `Snapshot key:` line (older tasks predating
      phase 1 are ineligible — no baseline to diff against)
"""

import os
import re
import sys
import html as _html
import requests
from datetime import datetime, timedelta, date, timezone
from typing import Optional

import process_task as pt


# ---------------------------------------------------------------------------
# Snapshot key parsing
# ---------------------------------------------------------------------------

_SNAPSHOT_LINE_RE = re.compile(r"^Snapshot key:\s+(.+)$", re.MULTILINE)
_SNAPSHOT_BODY_RE = re.compile(r"v(\d+)(?:\s*\|\s*dmg=(.+))?")
_INTERNAL_NOTES_RE = re.compile(r"\nInternal notes:\n((?:- [^\n]+\n?)+)")


def parse_internal_notes_from_description(notes: str) -> list[str]:
    """Extract the list of internal-notes bullets the bot last wrote."""
    m = _INTERNAL_NOTES_RE.search(notes)
    if not m:
        return []
    return [
        line[2:].strip()
        for line in m.group(1).strip().split("\n")
        if line.startswith("- ")
    ]


def parse_snapshot_key(notes: str) -> Optional[dict]:
    """Extract `{version, damages, raw}` from the Snapshot key line.

    Returns None if no Snapshot key line is present or if the body is
    malformed. `damages` is `{damage_id: photo_count}`.
    """
    m = _SNAPSHOT_LINE_RE.search(notes)
    if not m:
        return None
    raw = m.group(1).strip()
    body = _SNAPSHOT_BODY_RE.match(raw)
    if not body:
        return None
    version = int(body.group(1))
    damages: dict[str, int] = {}
    if body.group(2):
        for entry in body.group(2).split(","):
            e = entry.strip()
            if ":" not in e:
                continue
            d_id, count_s = e.rsplit(":", 1)
            try:
                damages[d_id.strip()] = int(count_s)
            except ValueError:
                continue
    return {"version": version, "damages": damages, "raw": raw}


# ---------------------------------------------------------------------------
# Asana DELETE helpers (process_task.py only POSTs/PUTs/GETs)
# ---------------------------------------------------------------------------

def _asana_delete(path: str) -> None:
    r = requests.delete(f"{pt.ASANA_BASE}{path}", headers=pt._asana_headers(), timeout=15)
    r.raise_for_status()


def _list_attachments(task_id: str) -> list[dict]:
    r = requests.get(
        f"{pt.ASANA_BASE}/tasks/{task_id}/attachments",
        headers=pt._asana_headers(),
        params={"opt_fields": "name"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def _list_stories(task_id: str) -> list[dict]:
    r = requests.get(
        f"{pt.ASANA_BASE}/tasks/{task_id}/stories",
        headers=pt._asana_headers(),
        params={"opt_fields": "text,resource_subtype"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Previous-snapshot fetch
# ---------------------------------------------------------------------------

def fetch_snapshot_by_version(shoe_id: str, version: int) -> Optional[dict]:
    """Fetch the historic snapshot row at a specific version for this shoe."""
    rows = pt._supabase_get("shoe_approval_snapshots", {
        "shoe_id": f"eq.{shoe_id}",
        "version": f"eq.{version}",
        "select":  "*",
    })
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def diff_snapshots(old_snap: dict, new_snap: dict) -> dict:
    """Compare two snapshots; return added/removed/updated service names + totals."""
    old_services = {(s.get("name") or ""): s for s in (old_snap.get("services") or [])}
    new_services = {(s.get("name") or ""): s for s in (new_snap.get("services") or [])}

    added   = sorted(new_services.keys() - old_services.keys() - {""})
    removed = sorted(old_services.keys() - new_services.keys() - {""})

    updated: list[str] = []
    for name in sorted((old_services.keys() & new_services.keys()) - {""}):
        old_s, new_s = old_services[name], new_services[name]
        if (
            old_s.get("price")    != new_s.get("price")
            or old_s.get("tat")    != new_s.get("tat")
            or old_s.get("final_commitment_days") != new_s.get("final_commitment_days")
            or set(old_s.get("photos") or []) != set(new_s.get("photos") or [])
        ):
            updated.append(name)

    return {
        "services_added":      added,
        "services_removed":    removed,
        "services_updated":    updated,
        "total_price_changed": old_snap.get("total_price") != new_snap.get("total_price"),
        "old_total_price":     old_snap.get("total_price"),
        "new_total_price":     new_snap.get("total_price"),
        "tat_changed":         old_snap.get("final_commitment_days") != new_snap.get("final_commitment_days"),
        "old_tat":             old_snap.get("final_commitment_days"),
        "new_tat":             new_snap.get("final_commitment_days"),
    }


def diff_damages(stored: dict[str, int], current: list[dict]) -> dict:
    """Compare stored {damage_id: photo_count} vs current damage rows.

    Per design Q10=B: detects add/remove + photo-count changes; doesn't
    detect note-text edits.
    """
    cur_by_id: dict[str, dict] = {}
    for d in current:
        photos = list(d.get("photo_urls") or [])
        if not photos and d.get("photo_url"):
            photos = [d["photo_url"]]
        photos = [p for p in photos if p]
        cur_by_id[d.get("id")] = {"note": d.get("note") or "", "photo_count": len(photos)}

    added_ids   = sorted(set(cur_by_id) - set(stored))
    removed_ids = sorted(set(stored)    - set(cur_by_id))
    updated_ids = sorted(d for d in (set(cur_by_id) & set(stored))
                         if stored[d] != cur_by_id[d]["photo_count"])

    return {
        "added":         [(d, cur_by_id[d]) for d in added_ids],
        "removed_count": len(removed_ids),
        "updated":       [(d, cur_by_id[d]) for d in updated_ids],
    }


def has_changes(snap_diff: dict, dmg_diff: dict) -> bool:
    return bool(
        snap_diff["services_added"]
        or snap_diff["services_removed"]
        or snap_diff["services_updated"]
        or snap_diff["total_price_changed"]
        or snap_diff["tat_changed"]
        or dmg_diff["added"]
        or dmg_diff["removed_count"]
        or dmg_diff["updated"]
    )


# ---------------------------------------------------------------------------
# Comment HTML (Q9=B: high-level summary, names of services but not per-field)
# ---------------------------------------------------------------------------

def _join_names(names: list[str]) -> str:
    return ", ".join(_html.escape(n) for n in names)


def _internal_notes_summary(prev: list[str], curr: list[str]) -> Optional[str]:
    """Human-readable description of how the internal-notes set changed."""
    if prev == curr:
        return None
    delta = len(curr) - len(prev)
    if delta > 0:
        return f"{delta} new note{'s' if delta != 1 else ''} added"
    if delta < 0:
        n = -delta
        return f"{n} note{'s' if n != 1 else ''} removed"
    return "edited"


def build_change_summary(snap_diff: dict, dmg_diff: dict, now_str: str,
                         due_was: Optional[date], due_now: Optional[date],
                         internal_summary: Optional[str] = None) -> str:
    # Use raw Unicode chars (·, →) — Asana doesn't render named HTML entities
    # like &middot; and &rarr;, they show as literal text.
    parts = [f"<body><strong>Updates synced · {_html.escape(now_str)}</strong><ul>"]

    if snap_diff["services_added"]:
        parts.append(f"<li><strong>Services added:</strong> {_join_names(snap_diff['services_added'])}</li>")
    if snap_diff["services_removed"]:
        parts.append(f"<li><strong>Services removed:</strong> {_join_names(snap_diff['services_removed'])}</li>")
    if snap_diff["services_updated"]:
        parts.append(f"<li><strong>Services updated:</strong> {_join_names(snap_diff['services_updated'])}</li>")

    if dmg_diff["added"]:
        notes = [d[1]["note"] or "(no note)" for d in dmg_diff["added"]]
        parts.append(f"<li><strong>Damages added:</strong> {_join_names(notes)}</li>")
    if dmg_diff["removed_count"]:
        n = dmg_diff["removed_count"]
        parts.append(f"<li><strong>Damages removed:</strong> {n} entr{'y' if n == 1 else 'ies'}</li>")
    if dmg_diff["updated"]:
        n = len(dmg_diff["updated"])
        parts.append(f"<li><strong>Damages updated:</strong> {n} entr{'y' if n == 1 else 'ies'} (photo changes)</li>")

    if snap_diff["total_price_changed"]:
        parts.append(
            f"<li><strong>Total:</strong> AED {snap_diff['old_total_price']} → AED {snap_diff['new_total_price']}</li>"
        )
    if snap_diff["tat_changed"]:
        msg = f"{snap_diff['old_tat']}d → {snap_diff['new_tat']}d"
        if due_was and due_now and due_was != due_now:
            msg += f" (due {due_was.isoformat()} → {due_now.isoformat()})"
        parts.append(f"<li><strong>TAT:</strong> {msg}</li>")

    if internal_summary:
        parts.append(f"<li><strong>Internal notes:</strong> {_html.escape(internal_summary)}</li>")

    parts.append("</ul></body>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# State syncs
# ---------------------------------------------------------------------------

def sync_subtasks(task_id: str, current_services: list[dict], dry_run: bool) -> None:
    """Add subtasks for newly approved services; delete subtasks for removed.

    Q14: changes happen pre-work, so deletion is safe (no progress lost).
    """
    existing = pt.list_subtasks(task_id)

    # Migration: subtasks created by older builds may carry the literal word
    # `Express` (e.g. `Premium Cleaning Express`). Current builds strip that
    # so without an in-place rename, the diff below would see the old name
    # as "removed" and lose subtask state on delete+recreate. Rename is
    # idempotent — clean names short-circuit through `_strip_express`'s
    # no-op path. We also update the in-memory `sub["name"]` so the
    # subsequent diff (and dry-run preview) sees the post-rename state.
    for sub in existing:
        cur_name = (sub.get("name") or "").strip()
        if not cur_name:
            continue
        clean, was_express = pt._strip_express(cur_name)
        if was_express and clean and clean != cur_name:
            print(f"  ~ rename subtask: {cur_name!r} -> {clean!r}")
            if not dry_run:
                pt.update_task(sub["gid"], {"name": clean})
            sub["name"] = clean

    by_name_lc = {(s.get("name") or "").strip().lower(): s for s in existing}
    current_names_lc = {s["name"].strip().lower() for s in current_services if s.get("name")}

    for svc in current_services:
        name = svc.get("name") or ""
        if name and name.strip().lower() not in by_name_lc:
            print(f"  + add subtask: {name}")
            if not dry_run:
                pt.create_subtask(task_id, name)

    for name_lc, sub in by_name_lc.items():
        if name_lc and name_lc not in current_names_lc:
            print(f"  - delete subtask: {sub.get('name')}")
            if not dry_run:
                _asana_delete(f"/tasks/{sub['gid']}")


def sync_service_photos(task_id: str, services: list[dict], dry_run: bool) -> None:
    """Diff-sync per-service photos against current Supabase URLs.

    Q3: only upload new photos, only delete removed/orphaned photos.
    Also migrates legacy positional names (`damage-1.jpg`) to stem-based.
    """
    attachments = _list_attachments(task_id)

    for svc in services:
        name = svc.get("name") or ""
        if not name:
            continue
        slug = pt._slugify(name)
        photos = svc.get("photos") or []

        expected = {f"{slug}-{pt._photo_stem(url)}.jpg" for url in photos}
        positional_re = re.compile(rf"^{re.escape(slug)}-\d+\.(jpg|jpeg)$", re.IGNORECASE)
        # Match this service's existing attachments (slug prefix + image ext)
        mine = [
            a for a in attachments
            if a["name"].lower().startswith(slug + "-")
            and a["name"].lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "webp", "png")
        ]
        mine_names = {a["name"]: a for a in mine}

        # Photos to upload: those whose stem-based filename isn't already attached
        to_upload = [u for u in photos if f"{slug}-{pt._photo_stem(u)}.jpg" not in mine_names]
        # Attachments to delete: legacy positional, OR stem-based not in current expected set
        to_delete = [
            a for a in mine
            if positional_re.match(a["name"]) or a["name"] not in expected
        ]

        if to_upload or to_delete:
            print(f"  service '{name}': +{len(to_upload)} photo(s) / -{len(to_delete)} photo(s)")

        if dry_run:
            continue

        for a in to_delete:
            _asana_delete(f"/attachments/{a['gid']}")
        if to_upload:
            pt._attach_photos(task_id, to_upload, name)


def sync_stains(task_id: str, damage_entries: list[dict], stains_photos: list[str],
                dry_run: bool) -> None:
    """Wipe-and-repost the Stains & Damages comment + damage attachments.

    Called only when damages have changed. Removes existing stains comment(s)
    and `damage-*.jpg` attachments, posts fresh ones from current state.
    """
    # Delete existing stains comments
    for s in _list_stories(task_id):
        if s.get("resource_subtype") != "comment_added":
            continue
        if "Stains & Damages" in (s.get("text") or ""):
            print(f"  - delete stains comment {s['gid']}")
            if not dry_run:
                _asana_delete(f"/stories/{s['gid']}")

    # Delete existing damage attachments (any naming variant)
    damage_re = re.compile(r"^damage-.+\.(jpg|jpeg|webp|png)$", re.IGNORECASE)
    for a in _list_attachments(task_id):
        if damage_re.match(a["name"]):
            print(f"  - delete damage attachment {a['name']}")
            if not dry_run:
                _asana_delete(f"/attachments/{a['gid']}")

    # Repost fresh
    if damage_entries or stains_photos:
        notes = [e["note"] for e in damage_entries if e["note"]]
        html_parts = ["<body><strong>Stains &amp; Damages</strong>"]
        if notes:
            html_parts.append("<ul>")
            for n in notes:
                html_parts.append(f"<li>{_html.escape(n)}</li>")
            html_parts.append("</ul>")
        html_parts.append("</body>")
        print(f"  + post stains comment ({len(notes)} entries) + attach {len(stains_photos)} photo(s)")
        if not dry_run:
            pt.add_comment(task_id, "".join(html_parts))
            if stains_photos:
                pt._attach_photos(task_id, stains_photos, "damage")


def _approved_date_dubai(snapshot: dict) -> Optional[date]:
    """Parse `approved_at` as Dubai-local date for due-date math (Q6=B)."""
    ts = snapshot.get("approved_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(pt.DUBAI_TZ).date()
    except (ValueError, TypeError):
        return None


def sync_description_and_fields(task_id: str, current_data: dict, current_snap: dict,
                                dry_run: bool) -> tuple[Optional[date], Optional[date]]:
    """Rewrite the bot-managed tail of the description + price + due_date.

    Returns (due_was, due_now) for the change summary comment. Q6=B applies
    when TAT changed: due_date = approved_date + new_TAT.
    """
    task = pt.get_task(task_id)
    notes = task.get("notes") or ""
    due_was: Optional[date] = None
    if task.get("due_on"):
        try:
            due_was = date.fromisoformat(task["due_on"])
        except ValueError:
            pass

    m = re.search(r"\n(Approved|Rejected) by customer\b", notes)
    if not m:
        print("  WARN: no marker line in description; skipping rewrite")
        return due_was, due_was

    head = notes[:m.start()].rstrip()
    addition_lines: list[str] = []
    if current_data["is_rejected"]:
        addition_lines.append("Rejected by customer")
        if current_data["rejection_reason"]:
            addition_lines.append(f"Reason: {current_data['rejection_reason']}")
        if current_data["rejector_name"] and current_data["rejected_at"]:
            addition_lines.append(f"Rejected by {current_data['rejector_name']} · {current_data['rejected_at']}")
    else:
        addition_lines.append("Approved by customer")
        if current_data["approver_name"] and current_data["approver_time"]:
            addition_lines.append(f"Approved by {current_data['approver_name']} · {current_data['approver_time']}")

    new_notes = head + "\n" + "\n".join(addition_lines)
    if current_data["internal_entries"]:
        new_notes += "\n\nInternal notes:\n" + "\n".join(f"- {e}" for e in current_data["internal_entries"])
    if current_data["sorter_suggested"]:
        new_notes += "\n\nSorter Suggested:\n" + ", ".join(current_data["sorter_suggested"]) + "."
    if current_data.get("snapshot_key"):
        new_notes += "\n\nSnapshot key: " + current_data["snapshot_key"]

    payload: dict = {"notes": new_notes}
    due_now: Optional[date] = None
    if current_data["is_rejected"]:
        payload["custom_fields"] = {pt.PRICE_FIELD_GID: None}
        payload["due_on"]        = None
        # Mirror the rejection reason into the right custom field. Same router
        # used by process_task — facility rejections go to Internal Rejection
        # Reason, customer rejections to Reason for Rejection.
        pt.apply_rejection_reason_field(payload, current_data)
    else:
        # Approved: clear any prior rejection-field values (in case this task
        # was previously rejected and the customer has now overturned), and
        # set price + due_on from the approved snapshot.
        payload["custom_fields"] = {
            pt.REASON_FOR_REJECTION_FIELD_GID:      [],
            pt.INTERNAL_REJECTION_REASON_FIELD_GID: None,
        }
        if current_data["total_price"] is not None:
            payload["custom_fields"][pt.PRICE_FIELD_GID] = current_data["total_price"]
        approved = _approved_date_dubai(current_snap)
        tat = current_data["total_tat"]
        if approved and tat:
            due_now = approved + timedelta(days=tat)
            payload["due_on"] = due_now.isoformat()

    # Merge in: snapshot-derived service field mappings (approved-derived
    # fields fully overwrite, sorter-derived preserve) AND description-derived
    # fields (Service Type enum + Brand/Colour/Size text), so a re-posted
    # Lovable link with changed scope, item type, or size lands in Asana.
    cf = payload.setdefault("custom_fields", {})
    cf.update(pt.compute_service_field_mappings(current_data, task.get("custom_fields")))
    cf.update(pt.derive_service_type_payload(notes))
    cf.update(pt.derive_description_text_payload(notes))

    print(f"  update description + custom fields"
          + (f" (due {due_was} -> {due_now})" if due_now and due_now != due_was else ""))
    if not dry_run:
        pt.update_task(task_id, payload)
    return due_was, due_now


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def sync_task(task_id: str, dry_run: bool = False) -> None:
    print(f"\n{'='*60}")
    print(f"Sync task: {task_id}{' [DRY RUN]' if dry_run else ''}")
    print(f"{'='*60}")

    task  = pt.get_task(task_id)
    notes = task.get("notes") or ""

    # Eligibility — sync handles both Approved and Rejected states; we react
    # to whichever marker the description currently has and detect transitions
    # via Supabase decision. Older tasks (no marker yet) are still skipped.
    if "Approved by customer" not in notes and "Rejected by customer" not in notes:
        print("Not yet processed (no Approved/Rejected marker). Skipping.")
        return
    link = pt.find_link(notes)
    if not link:
        print("No Service Wizard link in notes. Skipping.")
        return
    shoe_id = pt.find_shoe_id(link)
    if not shoe_id:
        print(f"Could not extract shoe_id from link {link!r}. Skipping.")
        return

    stored = parse_snapshot_key(notes)
    if stored is None:
        print("No `Snapshot key:` line in description (older task, no baseline). Skipping.")
        return

    # Fetch current state
    shoe_data       = pt.fetch_shoe_data(shoe_id)
    current_snap    = shoe_data["snapshot"]
    current_damages = shoe_data["damages"]
    if not current_snap:
        print("No current snapshot in Supabase. Skipping.")
        return

    current_key = pt.compute_snapshot_key(current_snap, current_damages)
    print(f"Stored key:  {stored['raw']}")
    print(f"Current key: {current_key}")
    snapshot_changed = current_key != stored["raw"]

    # Detect approved↔rejected flip independently of the snapshot key — defensive
    # against the case where Lovable changes decision without bumping version.
    old_was_rejected = "Rejected by customer" in notes
    new_is_rejected  = (current_snap.get("decision") or "") == "rejected"
    decision_changed = old_was_rejected != new_is_rejected
    if decision_changed:
        flip = "Rejected -> Approved" if old_was_rejected else "Approved -> Rejected"
        print(f"Decision flipped: {flip}")

    # Internal notes aren't part of the snapshot key (Supabase RLS hides the
    # table from anon, and we'd need a Jina scrape per cycle to hash them).
    # Instead, scrape now and compare against what's in the description.
    try:
        current_internal = pt.scrape_internal_notes(link)
    except Exception as e:
        print(f"  internal-notes scrape failed: {e} (continuing without)")
        current_internal = []
    prev_internal = parse_internal_notes_from_description(notes)
    internal_summary = _internal_notes_summary(prev_internal, current_internal)
    if internal_summary:
        print(f"Internal notes: {internal_summary} ({len(prev_internal)} -> {len(current_internal)})")

    if not snapshot_changed and not internal_summary and not decision_changed:
        print("No changes detected.")
        return

    # Compute service/damage diff only when the snapshot key changed
    if snapshot_changed:
        prev_snap = fetch_snapshot_by_version(shoe_id, stored["version"])
        if prev_snap is None:
            print(f"WARN: previous snapshot v{stored['version']} not found in Supabase; skipping.")
            return
        snap_diff = diff_snapshots(prev_snap, current_snap)
        dmg_diff  = diff_damages(stored["damages"], current_damages)
    else:
        snap_diff = {
            "services_added": [], "services_removed": [], "services_updated": [],
            "total_price_changed": False, "old_total_price": None, "new_total_price": None,
            "tat_changed": False, "old_tat": None, "new_tat": None,
        }
        dmg_diff = {"added": [], "removed_count": 0, "updated": []}

    current_data = pt.build_data(shoe_data, current_internal)

    # Apply state syncs. Rejected tasks have no subtasks and no per-service
    # photos (matching process_task's initial-rejection rules). Approved tasks
    # have both. We trigger this block on snapshot_changed OR decision_changed
    # so a pure flip without a version bump still re-syncs cleanly.
    if snapshot_changed or decision_changed:
        if current_data["is_rejected"]:
            # Wipe subtasks; per-service photos left as orphans (cheap to leave,
            # avoids re-uploading on a future flip back to approved).
            sync_subtasks(task_id, [], dry_run)
        else:
            sync_subtasks(task_id, current_data["approved_services"], dry_run)
            sync_service_photos(task_id, current_data["approved_services"], dry_run)
        if dmg_diff["added"] or dmg_diff["removed_count"] or dmg_diff["updated"]:
            sync_stains(task_id, current_data["damage_entries"], current_data["stains_photos"], dry_run)
    # Description rewrite always runs (picks up notes change, refreshes Snapshot key)
    due_was, due_now = sync_description_and_fields(task_id, current_data, current_snap, dry_run)

    # Post the change summary comment last
    now_str = datetime.now(pt.DUBAI_TZ).strftime("%d %b, %H:%M")
    summary = build_change_summary(snap_diff, dmg_diff, now_str, due_was, due_now, internal_summary)
    print("  + post change-summary comment")
    if not dry_run:
        pt.add_comment(task_id, summary)

    print("\nDone.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sync_task.py <task_id> [--dry-run]")
        sys.exit(1)
    task_id  = sys.argv[1]
    dry_run  = "--dry-run" in sys.argv[2:]
    sync_task(task_id, dry_run=dry_run)
