#!/usr/bin/env python3
"""
Washmen Service Wizard → Asana Automation (v2)

When a Service Wizard "Customer approval response" link is in a task's
description, this script reads the Final Approved Scope from the page and
updates the Asana task:

- Subtasks: one per approved service name (excludes "Removed by customer")
- Description: appends "Approved by customer" + "Approved by <Name> · <time>"
- Internal notes (if present): appended to description bottom
- Stains & damages (if present): comment with text + photos labeled "Stains & Damages"
- Service photos (if present under Final approved scope): one comment per service
- Price custom field: total approved price
- Due date: today + total TAT days
- Deduplication: skips if task already has a subtask matching any approved service
"""

import os
import re
import sys
import json
import html as _html
import requests
from datetime import datetime, timedelta, date
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

ASANA_PAT  = os.getenv("ASANA_PAT")
ASANA_BASE = "https://app.asana.com/api/1.0"
JINA_BASE  = "https://r.jina.ai"

# Custom field GID
PRICE_FIELD_GID = "1202480206903933"

# Both the Lovable preview domain and the Washmen production domain are supported
LINK_PATTERNS = [
    r"https://service-wizard-kit\.lovable\.app/approved/[a-f0-9\-]+",
    r"https://sc\.washmen\.com/approved/[a-f0-9\-]+",
]


# ---------------------------------------------------------------------------
# Asana API helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    if not ASANA_PAT:
        raise RuntimeError("ASANA_PAT is not set. Add it to your .env file.")
    return {"Authorization": f"Bearer {ASANA_PAT}"}


def get_task(task_id: str) -> dict:
    r = requests.get(f"{ASANA_BASE}/tasks/{task_id}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def list_subtasks(task_id: str) -> list:
    r = requests.get(
        f"{ASANA_BASE}/tasks/{task_id}/subtasks",
        headers=_headers(), params={"opt_fields": "name"}, timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def update_task(task_id: str, payload: dict) -> dict:
    r = requests.put(
        f"{ASANA_BASE}/tasks/{task_id}",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"data": payload}, timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def add_comment(task_id: str, html: str, pinned: bool = False) -> dict:
    r = requests.post(
        f"{ASANA_BASE}/tasks/{task_id}/stories",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"data": {"html_text": html, "is_pinned": pinned}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def create_subtask(task_id: str, name: str, notes: str = "") -> dict:
    r = requests.post(
        f"{ASANA_BASE}/tasks",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"data": {"name": name, "notes": notes, "parent": task_id}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Scraping & parsing
# ---------------------------------------------------------------------------

def find_link(notes: str) -> Optional[str]:
    for pattern in LINK_PATTERNS:
        m = re.search(pattern, notes)
        if m:
            return m.group(0)
    return None


def scrape_page(url: str) -> str:
    """Render the React SPA via Jina AI Reader (markdown mode, cache-bypassed)."""
    r = requests.get(
        f"{JINA_BASE}/{url}",
        headers={"X-No-Cache": "true", "X-Timeout": "60"},
        timeout=120,
    )
    r.raise_for_status()
    return r.text


def extract_service_name(cell_text: str) -> str:
    """Extract the clean service name from a Final Approved Scope cell.

    Cells render as `<Name> <Description>` in markdown — the visual line break
    between them is collapsed. Algorithm: walk forward through TitleCase words;
    the description starts at the first TitleCase word that's followed by a
    lowercase word. The service name is everything before that.

    Examples:
        "Premium Cleaning Some stains might..."  → "Premium Cleaning"
        "Icing Removed by customer"              → "Icing"
        "Sanitize & Deodorize A deep treatment"  → "Sanitize & Deodorize"
        "Leather Insole Replacement Fit and..."  → "Leather Insole Replacement"
    """
    cell = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", cell_text)        # strip ![alt](url)
    cell = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cell)          # collapse [txt](url)
    cell = re.sub(r"\*+", "", cell).strip()

    words = cell.split()
    if not words:
        return ""

    name_parts: list[str] = []
    for i, word in enumerate(words):
        # Word containing ':' is a description label (e.g. "Stitching:") — service
        # names never contain colons, so stop without including this word.
        if ":" in word:
            break

        plain = word.rstrip(".,;").strip()
        if not plain:
            continue

        if plain == "&":                # part of names like "Sanitize & Deodorize"
            name_parts.append(word)
            continue

        first = plain[0]
        if first.islower():             # description started before this word
            break

        # Current word is TitleCase. If the next word is lowercase, the description
        # starts at the next word — but the current word may itself be the first
        # description word (e.g. "Some stains..."), so skip it too.
        if i + 1 < len(words):
            next_plain = words[i + 1].rstrip(".,;:").strip()
            if next_plain and next_plain[0].islower():
                break

        name_parts.append(word)

    return " ".join(name_parts).strip()


def parse_page(text: str) -> dict:
    """Parse the rendered Service Wizard markdown into structured data."""
    data: dict = {
        "approver_name":     None,
        "approver_time":     None,
        "approver_type":     None,   # "Customer" or "Facility"
        "approved_services": [],     # [{name, tat_days, price, photo_url, removed}]
        "total_tat":         None,
        "total_price":       None,
        "stains_text":       None,
        "stains_photos":     [],
        "internal_notes":    None,
    }

    # Approver type: "Final approved scope(v1 · Customer)" or "(v2 · Facility)"
    m = re.search(
        r"Final approved scope\s*\(\s*v?\d*\s*·\s*(Customer|Facility)\s*\)",
        text, re.IGNORECASE,
    )
    if m:
        data["approver_type"] = m.group(1)

    # Approver line: "Approved by Haris Velijevic · 01 May, 10:50"
    m = re.search(r"Approved by\s+([^·\n]+?)\s*·\s*([^\n]+)", text)
    if m:
        data["approver_name"] = m.group(1).strip()
        data["approver_time"] = m.group(2).strip()

    # Final Approved Scope table — isolate the section
    section_m = re.search(
        r"Final approved scope.*?\n(.+?)(?=\nApproved by|\nQuote history|\n##|\Z)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if section_m:
        table = section_m.group(1)
        for row in re.finditer(
            r"^\|\s*(.+?)\s*\|\s*(?:(\d+)d|—|-)\s*\|\s*AED\s+([\d.]+)\s*\|",
            table, re.MULTILINE,
        ):
            cell      = row.group(1)
            tat_str   = row.group(2)
            price     = float(row.group(3))
            cell_lc   = cell.strip().lower().replace("*", "")

            if cell_lc == "service":
                continue   # header
            if cell_lc.startswith("---"):
                continue   # markdown separator
            if cell_lc.startswith("total"):
                data["total_price"] = price
                if tat_str:
                    data["total_tat"] = int(tat_str)
                continue

            photo_m   = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", cell)
            photo_url = photo_m.group(1) if photo_m else None

            svc_name = extract_service_name(cell)
            removed  = "removed by customer" in cell.lower()

            if svc_name:
                data["approved_services"].append({
                    "name":      svc_name,
                    "tat_days":  int(tat_str) if tat_str else None,
                    "price":     price,
                    "photo_url": photo_url,
                    "removed":   removed,
                })

    # Stains & damages
    stains_m = re.search(r"## Stains & damages\s*\n(.+?)(?=\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if stains_m:
        block = stains_m.group(1).strip()
        if block and "no stains or damages" not in block.lower():
            data["stains_photos"] = re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", block)
            text_only = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", block)
            text_only = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text_only)
            text_only = re.sub(r"\n{3,}", "\n\n", text_only).strip()
            if text_only:
                data["stains_text"] = text_only

    # Internal notes
    notes_m = re.search(r"## Internal notes\s*\n(.+?)(?=\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
    if notes_m:
        block = notes_m.group(1).strip()
        if block and "no internal notes" not in block.lower():
            data["internal_notes"] = block

    return data


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_task(task_id: str) -> None:
    print(f"\n{'='*60}")
    print(f"Processing task: {task_id}")
    print(f"{'='*60}")

    # 1. Fetch task
    task  = get_task(task_id)
    notes = task.get("notes", "") or ""
    print(f"Task: {task['name']}")

    # 2. Find link
    link = find_link(notes)
    if not link:
        print("No Service Wizard link found in description. Skipping.")
        return
    print(f"Link: {link}")

    # 3. Scrape & parse
    print("Scraping page via Jina AI Reader...")
    raw  = scrape_page(link)
    data = parse_page(raw)

    print(f"  Approver:    {data['approver_name']} ({data['approver_type']}) at {data['approver_time']}")
    print(f"  Services:    {len(data['approved_services'])}")
    for svc in data["approved_services"]:
        flag = " (REMOVED)" if svc["removed"] else ""
        photo = " [PHOTO]" if svc["photo_url"] else ""
        print(f"    - {svc['name']} · {svc['tat_days']}d · AED {svc['price']}{flag}{photo}")
    print(f"  Total:       AED {data['total_price']} · {data['total_tat']}d")
    print(f"  Stains:      text={'yes' if data['stains_text'] else 'no'}, photos={len(data['stains_photos'])}")
    print(f"  Internal:    {'yes' if data['internal_notes'] else 'no'}")

    # 4. Deduplication
    # Primary signal: description already contains "Approved by customer" — this
    # is robust because the line stays in the description even if subtasks get
    # deleted. Secondary signal: any existing subtask name matches an approved
    # service (catches edge cases where the marker was removed manually).
    approved_active = [s for s in data["approved_services"] if not s["removed"]]
    approved_names_lc = {s["name"].lower() for s in approved_active}

    if "Approved by customer" in notes:
        print("Already processed (description has 'Approved by customer'). Skipping.")
        return

    existing_subtasks = list_subtasks(task_id)
    existing_names_lc = {(s.get("name") or "").strip().lower() for s in existing_subtasks}
    overlap = existing_names_lc & approved_names_lc
    if overlap:
        print(f"Already processed (subtask match: {overlap}). Skipping.")
        return

    # 5. Build new description: append approval lines + internal notes
    addition_lines = ["Approved by customer"]   # literal, per spec
    if data["approver_name"] and data["approver_time"]:
        addition_lines.append(f"Approved by {data['approver_name']} · {data['approver_time']}")
    new_notes = notes.rstrip() + "\n" + "\n".join(addition_lines)
    if data["internal_notes"]:
        new_notes += "\n\nInternal notes:\n" + data["internal_notes"]

    # 6. Build the single PUT payload (description + custom fields + due date)
    task_update: dict = {"notes": new_notes}

    if data["total_price"] is not None:
        task_update["custom_fields"] = {PRICE_FIELD_GID: data["total_price"]}

    if data["total_tat"]:
        due_date = (date.today() + timedelta(days=data["total_tat"])).strftime("%Y-%m-%d")
        task_update["due_on"] = due_date
        print(f"  Due date:    {due_date} (today + {data['total_tat']} days)")

    print("Updating task (description, price, due date)...")
    update_task(task_id, task_update)

    # 7. Stains & damages comment
    # Asana comment HTML allows: <body>, <strong>, <em>, <u>, <s>, <code>, <ol>,
    # <ul>, <li>, <a>, <blockquote>, <pre>. <p> is NOT allowed — using <blockquote>.
    # All user-provided text is escaped so embedded < > & don't break parsing.
    if data["stains_text"] or data["stains_photos"]:
        print("Adding 'Stains & Damages' comment...")
        html_parts = ["<body><strong>Stains &amp; Damages</strong>"]
        if data["stains_text"]:
            text_safe = _html.escape(data["stains_text"])
            html_parts.append(f"<blockquote>{text_safe}</blockquote>")
        if data["stains_photos"]:
            html_parts.append("<ul>")
            for i, url in enumerate(data["stains_photos"], 1):
                url_safe = _html.escape(url, quote=True)
                html_parts.append(f'<li><a href="{url_safe}">Photo {i}</a></li>')
            html_parts.append("</ul>")
        html_parts.append("</body>")
        add_comment(task_id, "".join(html_parts))

    # 8. Per-service photo comments (one comment per service that has a photo)
    for svc in approved_active:
        if svc.get("photo_url"):
            print(f"Adding photo comment for: {svc['name']}")
            name_safe = _html.escape(svc["name"])
            url_safe  = _html.escape(svc["photo_url"], quote=True)
            html = (
                f"<body><strong>{name_safe}</strong>"
                f"<ul><li><a href=\"{url_safe}\">View photo</a></li></ul>"
                f"</body>"
            )
            add_comment(task_id, html)

    # 9. Subtasks — one per approved service (excluding removed)
    for svc in approved_active:
        print(f"Creating subtask: {svc['name']}")
        create_subtask(task_id, svc["name"])

    print("\nDone!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process_task.py <task_id>")
        sys.exit(1)
    process_task(sys.argv[1])
