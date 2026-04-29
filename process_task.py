#!/usr/bin/env python3
"""
Washmen Service Wizard → Asana Automation

For a given Asana task:
  1. Finds the Service Wizard link under "Customer response:" in the description
  2. Scrapes the page (handles React SPA via Jina AI Reader)
  3. Uploads all assessment photos as real Asana task attachments
  4. Adds a pinned summary comment with all text data
  5. Creates a subtask for each approved service
"""

import os
import re
import sys
import json
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

ASANA_PAT = os.getenv("ASANA_PAT")
ASANA_BASE = "https://app.asana.com/api/1.0"
JINA_BASE = "https://r.jina.ai"


# ---------------------------------------------------------------------------
# Asana API helpers
# ---------------------------------------------------------------------------

def _asana_headers() -> dict:
    if not ASANA_PAT:
        raise RuntimeError("ASANA_PAT is not set. Add it to your .env file.")
    return {"Authorization": f"Bearer {ASANA_PAT}"}


def get_task(task_id: str) -> dict:
    r = requests.get(f"{ASANA_BASE}/tasks/{task_id}", headers=_asana_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def upload_attachment(task_id: str, image_url: str, filename: str) -> dict:
    """Download an image and upload it as a native Asana task attachment."""
    img = requests.get(image_url, timeout=30)
    img.raise_for_status()
    content_type = img.headers.get("Content-Type", "image/jpeg")

    r = requests.post(
        f"{ASANA_BASE}/attachments",
        headers=_asana_headers(),
        files={"file": (filename, img.content, content_type)},
        data={"parent": task_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


def add_pinned_comment(task_id: str, html: str) -> dict:
    r = requests.post(
        f"{ASANA_BASE}/tasks/{task_id}/stories",
        headers={**_asana_headers(), "Content-Type": "application/json"},
        json={"data": {"html_text": html, "is_pinned": True}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def update_task(task_id: str, payload: dict) -> dict:
    """PUT a task — used for due date, custom fields, etc."""
    r = requests.put(
        f"{ASANA_BASE}/tasks/{task_id}",
        headers={**_asana_headers(), "Content-Type": "application/json"},
        json={"data": payload},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def create_subtask(task_id: str, name: str, notes: str) -> dict:
    r = requests.post(
        f"{ASANA_BASE}/tasks",
        headers={**_asana_headers(), "Content-Type": "application/json"},
        json={"data": {"name": name, "notes": notes, "parent": task_id}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Scraping & parsing
# ---------------------------------------------------------------------------

def extract_service_wizard_link(notes: str) -> Optional[str]:
    """Pull the lovable.app URL out of the task description."""
    match = re.search(
        r"https://service-wizard-kit\.lovable\.app/approved/[a-f0-9\-]+",
        notes,
    )
    return match.group(0) if match else None


def scrape_page(url: str) -> str:
    """Render the React SPA via Jina AI Reader and return markdown text."""
    r = requests.get(f"{JINA_BASE}/{url}", timeout=45)
    r.raise_for_status()
    return r.text


def _find(pattern: str, text: str, flags: int = re.IGNORECASE) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def parse_assessment(text: str) -> dict:
    """Parse Jina-rendered markdown into structured assessment data.

    The Service Wizard page renders as plain markdown with sections like:
      ## Sorter captured        → Brand, Type, Color, Sorted at
      ## Approved services ...  → Markdown table with service rows
      ## Order summary          → Order code, Customer
    """
    # Isolate the "## Sorter captured" section for reliable item details
    sorter_m = re.search(r"## Sorter captured(.+?)(?=##|$)", text, re.DOTALL | re.IGNORECASE)
    sorter = sorter_m.group(1) if sorter_m else ""

    data: dict = {
        # From the inline header summary line:  Order `ORD-1966` · Customer **Rami Shaar**
        "order_code": _find(r"Order\s+`([^`]+)`", text),
        "customer":   (
            _find(r"Customer\s+\*\*([^*]+)\*\*", text)          # bold inline form
            or _find(r"^Customer\s+([^\n]+)", text, re.MULTILINE)  # plain section form
        ),
        # From "## Sorter captured" — most reliable, already validated by sorter
        "brand":      _find(r"^Brand\s+([^\n]+)", sorter, re.MULTILINE | re.IGNORECASE),
        "item_type":  _find(r"^Type\s+([^\n]+)",  sorter, re.MULTILINE | re.IGNORECASE),
        "color":      _find(r"^Color\s+([^\n]+)", sorter, re.MULTILINE | re.IGNORECASE),
        "sorted_at":  _find(r"Sorted at\s+([^\n]+)", sorter, re.IGNORECASE),
        "status":     "Approved by facility" if "Approved by facility" in text else None,
        # Populated below from the approved services table
        "services":   [],
        "service":    None,   # first service name (convenience)
        "price":      None,   # first service price (convenience)
        "turnaround": None,   # first service TAT (convenience)
        "photo_urls": [],
    }

    # ── Approved services table ──────────────────────────────────────────────
    # Section heading: "## Approved services (operator scope)"
    # Row format: | **Premium Cleaning** | AED 145 | 3 (committed: 3) | — |
    approved_m = re.search(
        r"## Approved services.*?\n(.+?)(?=##|$)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if approved_m:
        for row in re.finditer(
            r"\|\s+\*\*(?!Total)([^*|]+)\*\*\s+\|\s+AED\s+([\d.]+)\s+\|\s+(\d+)[^|]*\|",
            approved_m.group(1),
        ):
            svc_name = row.group(1).strip()
            svc_price = float(row.group(2))
            tat_days = row.group(3).strip()
            data["services"].append({
                "name":       svc_name,
                "price":      svc_price,
                "turnaround": f"{tat_days} days",
            })

    if data["services"]:
        data["service"]    = data["services"][0]["name"]
        data["price"]      = data["services"][0]["price"]
        data["turnaround"] = data["services"][0]["turnaround"]

    # ── Photos ───────────────────────────────────────────────────────────────
    all_urls = re.findall(
        r"https://[a-z0-9]+\.supabase\.co/storage/v1/object/public/[^\s)\]\"']+",
        text,
    )
    data["photo_urls"] = list(dict.fromkeys(all_urls))  # deduplicate, preserve order

    return data


def photo_filename(url: str, index: int) -> str:
    """Generate a human-readable filename from the URL path."""
    if "/before/" in url:
        return "before.jpg"
    ext = url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
    return f"sorting_{index}.{ext}"


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_task(task_id: str):
    print(f"\n{'='*60}")
    print(f"Processing task: {task_id}")
    print(f"{'='*60}")

    # 1. Fetch task
    task = get_task(task_id)
    print(f"Task: {task['name']}")

    notes = task.get("notes", "")

    # 2. Find Service Wizard link
    link = extract_service_wizard_link(notes)
    if not link:
        print("No Service Wizard link found under 'Customer response:'. Skipping.")
        return

    print(f"Link found: {link}")

    # 3. Scrape
    print("Scraping page via Jina AI Reader...")
    raw = scrape_page(link)

    # 4. Parse
    data = parse_assessment(raw)
    print(f"Parsed: {json.dumps({k: v for k, v in data.items() if k != 'photo_urls'}, indent=2)}")
    print(f"Photos found: {len(data['photo_urls'])}")

    # 5. Upload photos as real Asana attachments
    sorting_index = 1
    for url in data["photo_urls"]:
        filename = photo_filename(url, sorting_index)
        if "sorting" in filename:
            sorting_index += 1
        print(f"  Uploading {filename} ...", end=" ", flush=True)
        try:
            att = upload_attachment(task_id, url, filename)
            print(f"OK ({att.get('name')})")
        except Exception as e:
            print(f"FAILED: {e}")

    # 6. Pinned summary comment (text data only — photos are attachments above)
    price_str = f"AED {data['price']:.2f}" if data["price"] else "N/A"
    comment_html = (
        "<body>"
        "<strong>Assessment Summary — Service Wizard</strong>"
        "<ul>"
        f"<li><strong>Service:</strong> {data['service'] or 'N/A'}</li>"
        f"<li><strong>Price:</strong> {price_str}</li>"
        f"<li><strong>Turnaround:</strong> {data['turnaround'] or 'N/A'}</li>"
        f"<li><strong>Brand:</strong> {data['brand'] or 'N/A'}</li>"
        f"<li><strong>Item Type:</strong> {data['item_type'] or 'N/A'}</li>"
        f"<li><strong>Color:</strong> {data['color'] or 'N/A'}</li>"
        f"<li><strong>Order Code:</strong> {data['order_code'] or 'N/A'}</li>"
        f"<li><strong>Customer:</strong> {data['customer'] or 'N/A'}</li>"
        f"<li><strong>Status:</strong> {data['status'] or 'N/A'}"
        f"{' — ' + data['sorted_at'] if data['sorted_at'] else ''}</li>"
        "</ul>"
        f"<em>{len(data['photo_urls'])} assessment photos uploaded as attachments.</em>"
        "</body>"
    )
    print("Adding pinned summary comment...")
    add_pinned_comment(task_id, comment_html)

    # 7. Update custom fields + due date
    custom_fields = {}
    if data["price"] is not None:
        custom_fields["1202480206903933"] = data["price"]        # Price (number)
    if data["color"]:
        custom_fields["1202289965454672"] = data["color"]        # Colour (text)
    if data["brand"]:
        custom_fields["1202289964354114"] = data["brand"]        # Brand (text)

    task_update: dict = {}
    if custom_fields:
        task_update["custom_fields"] = custom_fields

    # Due date = task created_at + turnaround days
    tat_match = re.match(r"(\d+)", data["turnaround"] or "")
    if tat_match:
        tat_days = int(tat_match.group(1))
        created_at = task.get("created_at", "")
        if created_at:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            due_date = (created_dt + timedelta(days=tat_days)).strftime("%Y-%m-%d")
            task_update["due_on"] = due_date
            print(f"Due date: {due_date} (created {created_dt.date()} + {tat_days} days)")

    if task_update:
        print(f"Updating task fields: {list(task_update.keys())}")
        update_task(task_id, task_update)

    # 8. One subtask per approved service
    for svc in data["services"]:
        svc_price_str = f"AED {svc['price']:.2f}"
        subtask_name = f"{svc['name']} — {svc_price_str} | TAT: {svc['turnaround']}"
        subtask_notes = (
            f"Approved service from Service Wizard assessment.\n\n"
            f"Service: {svc['name']}\n"
            f"Price: {svc_price_str}\n"
            f"Turnaround: {svc['turnaround']}\n"
            f"Approved by facility: {data['sorted_at'] or 'N/A'}\n"
            f"Source: {link}"
        )
        print(f"Creating subtask: {subtask_name}")
        sub = create_subtask(task_id, subtask_name, subtask_notes)
        print(f"  Subtask created: {sub['gid']}")

    print("\nDone!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process_task.py <task_id>")
        print("Example: python process_task.py 1214378804036117")
        sys.exit(1)

    process_task(sys.argv[1])
