#!/usr/bin/env python3
"""
One-time cleanup of bot artifacts created by the pre-PAT-cutover identity.

Why this exists:
    Before the cutover, the automation wrote to Asana under Andrew Villamor's
    PAT. After cutover, all writes are SC Bot. Asana restricts comment
    deletion to the author, so SC Bot can't delete Andrew's comments. That
    causes sync_task to crash at sync_stains (HTTP 403) when it tries to
    wipe-and-repost the Stains & Damages comment.

What this script does:
    Scans all processed ShoeCare tasks (those with `Snapshot key:` in
    description) and deletes bot-pattern artifacts authored by Andrew so
    future sync_task runs can wipe-and-repost cleanly under SC Bot.

What it deletes (only when author = Andrew Villamor):
    - Comments starting with "Stains & Damages"
    - Comments starting with "Updates synced"
    - Image attachments named "damage-*.{jpg,jpeg,webp,png}"

What it deliberately leaves alone:
    - Subtasks (SC Bot can already delete them)
    - Per-service photo attachments (sync_service_photos only deletes them
      in narrow cases; risk of touching operator uploads outweighs benefit)
    - Description content (managed via overwrite, no delete needed)
    - Approval link comments from Lovable
    - Anything not authored by Andrew

Auth:
    Reads ASANA_PAT from .env. To delete Andrew's content you must run this
    with Andrew's PAT (Asana restricts deletion to the author). Restore
    SC Bot's PAT in .env after the cleanup completes.

Usage:
    1. Put Andrew's PAT in .env (temporarily)
    2. Preview:  python cleanup_pre_pat_artifacts.py --dry-run
    3. Execute: python cleanup_pre_pat_artifacts.py
    4. Restore SC Bot's PAT in .env
"""

import os
import re
import sys
import requests
from dotenv import load_dotenv

import process_task as pt

load_dotenv()

WORKSPACE_GID = os.getenv("ASANA_WORKSPACE_GID", "41091308892039")
PROJECT_GID   = os.getenv("ASANA_PROJECT_GID",   "1202289964354061")
OLD_USER_GID  = "229819039084406"  # Andrew Villamor — pre-cutover bot identity

COMMENT_PATTERNS = [
    re.compile(r"^Stains? & Damages", re.IGNORECASE),
    re.compile(r"^Updates synced",     re.IGNORECASE),
]
ATTACHMENT_NAME_RE = re.compile(r"^damage-.+\.(jpg|jpeg|webp|png)$", re.IGNORECASE)


def list_processed_tasks() -> list[dict]:
    """All ShoeCare tasks with `Snapshot key:` in description."""
    found: list[dict] = []
    offset: str | None = None
    while True:
        params: dict = {
            "text":         "Snapshot key",
            "projects.any": PROJECT_GID,
            "opt_fields":   "gid,name,notes",
            "limit":        100,
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
        for t in body.get("data", []):
            # Server-side text search isn't anchored — re-check client-side.
            if "Snapshot key:" in (t.get("notes") or ""):
                found.append(t)
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break
    return found


def list_stories(task_id: str) -> list[dict]:
    out: list[dict] = []
    offset: str | None = None
    while True:
        params: dict = {
            "opt_fields": "gid,text,resource_subtype,created_by.gid",
            "limit":      100,
        }
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"{pt.ASANA_BASE}/tasks/{task_id}/stories",
            headers=pt._asana_headers(),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("data", []))
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break
    return out


def list_attachments(task_id: str) -> list[dict]:
    r = requests.get(
        f"{pt.ASANA_BASE}/tasks/{task_id}/attachments",
        headers=pt._asana_headers(),
        params={"opt_fields": "gid,name,created_by.gid"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def delete_resource(path: str, dry_run: bool) -> None:
    if dry_run:
        return
    r = requests.delete(f"{pt.ASANA_BASE}{path}", headers=pt._asana_headers(), timeout=15)
    r.raise_for_status()


def cleanup_task(task: dict, dry_run: bool) -> tuple[int, int, list[str]]:
    """Returns (n_comments_deleted, n_attachments_deleted, errors)."""
    task_id = task["gid"]
    n_c = 0
    n_a = 0
    errors: list[str] = []

    try:
        for s in list_stories(task_id):
            if s.get("resource_subtype") != "comment_added":
                continue
            if (s.get("created_by") or {}).get("gid") != OLD_USER_GID:
                continue
            text = s.get("text") or ""
            if not any(p.search(text) for p in COMMENT_PATTERNS):
                continue
            print(f"    {'[dry] ' if dry_run else ''}delete comment {s['gid']}: {text[:70]!r}")
            try:
                delete_resource(f"/stories/{s['gid']}", dry_run)
                n_c += 1
            except Exception as e:
                errors.append(f"comment {s['gid']}: {e}")
    except Exception as e:
        errors.append(f"list stories: {e}")

    try:
        for a in list_attachments(task_id):
            if (a.get("created_by") or {}).get("gid") != OLD_USER_GID:
                continue
            name = a.get("name") or ""
            if not ATTACHMENT_NAME_RE.match(name):
                continue
            print(f"    {'[dry] ' if dry_run else ''}delete attachment {a['gid']}: {name}")
            try:
                delete_resource(f"/attachments/{a['gid']}", dry_run)
                n_a += 1
            except Exception as e:
                errors.append(f"attachment {a['gid']}: {e}")
    except Exception as e:
        errors.append(f"list attachments: {e}")

    return n_c, n_a, errors


def main() -> None:
    dry_run = ("--dry-run" in sys.argv) or ("-n" in sys.argv)
    if dry_run:
        print("=== DRY RUN — no deletes will be performed ===\n")

    tasks = list_processed_tasks()
    print(f"Found {len(tasks)} processed task(s) in ShoeCare.\n")

    total_c = 0
    total_a = 0
    failed: list[tuple[str, list[str]]] = []
    for t in tasks:
        print(f"Task {t['gid']}: {(t.get('name') or '')[:80]}")
        n_c, n_a, errs = cleanup_task(t, dry_run)
        total_c += n_c
        total_a += n_a
        if errs:
            failed.append((t["gid"], errs))

    print()
    print(f"=== Summary {'[DRY RUN]' if dry_run else ''} ===")
    print(f"Tasks scanned:        {len(tasks)}")
    print(f"Comments deleted:     {total_c}")
    print(f"Attachments deleted:  {total_a}")
    print(f"Tasks with errors:    {len(failed)}")
    if failed:
        for tid, errs in failed:
            print(f"  {tid}:")
            for e in errs:
                print(f"    - {e}")


if __name__ == "__main__":
    main()
