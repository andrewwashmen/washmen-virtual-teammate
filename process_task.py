#!/usr/bin/env python3
"""
Washmen Service Wizard → Asana Automation (v3)

When a Service Wizard "Customer approval response" link is present in a task
description, this script reads the structured approval data from Supabase
(plus internal notes via Jina, since RLS hides that table from anon callers)
and updates the Asana task:

- Subtasks: one per approved service name
- Description: appends "Approved by customer" + "Approved by <Name> · <time>"
  (or the rejection equivalent)
- Internal notes (if present): appended to the description
- Stains & damages (if present): one comment with all entry notes listed and
  damage photos attached as files on the comment
- Service photos (if present): one comment per service with the service name
  as title and that service's photos attached as files on the comment
- Price custom field: total approved price (cleared on rejection)
- Due date: today + total TAT days (cleared on rejection)
- Deduplication: skips if the description already has a status marker, or if
  any existing subtask matches an approved service name
"""

import os
import re
import io
import sys
import json
import base64
import html as _html
import requests
from datetime import datetime, timedelta, date, timezone
from typing import Optional
from urllib.parse import urlparse
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

ASANA_PAT     = os.getenv("ASANA_PAT")
ASANA_BASE    = "https://app.asana.com/api/1.0"
JINA_BASE     = "https://r.jina.ai"
SUPABASE_BASE = "https://onjhhntixaxumtiildwa.supabase.co"
SUPABASE_HOST = urlparse(SUPABASE_BASE).hostname  # for SSRF check on photo URLs
SPA_BASE      = "https://sc.washmen.com"

# Hard cap on photo download size; Supabase webps are typically <500KB but we
# defend against runaway / hostile responses that could OOM the worker.
MAX_PHOTO_BYTES = 25 * 1024 * 1024

# Custom field GID
PRICE_FIELD_GID = "1202480206903933"

# Approval URLs from both the Lovable preview and the Washmen production domain
LINK_PATTERNS = [
    r"https://service-wizard-kit\.lovable\.app/approved/[a-f0-9\-]+",
    r"https://sc\.washmen\.com/approved/[a-f0-9\-]+",
]
SHOE_ID_RE = re.compile(r"/approved/([a-f0-9\-]{36})")

# Cache for the Supabase anon key. The key is public — every browser visiting
# sc.washmen.com receives it in the JS bundle — so this isn't a leak. RLS on
# the Supabase tables governs what data anon callers can actually read.
_ANON_KEY: Optional[str] = None


# ---------------------------------------------------------------------------
# Asana API
# ---------------------------------------------------------------------------

def _asana_headers() -> dict:
    if not ASANA_PAT:
        raise RuntimeError("ASANA_PAT is not set. Add it to your .env file.")
    return {"Authorization": f"Bearer {ASANA_PAT}"}


def get_task(task_id: str) -> dict:
    r = requests.get(f"{ASANA_BASE}/tasks/{task_id}", headers=_asana_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def list_subtasks(task_id: str) -> list:
    r = requests.get(
        f"{ASANA_BASE}/tasks/{task_id}/subtasks",
        headers=_asana_headers(), params={"opt_fields": "name"}, timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def update_task(task_id: str, payload: dict) -> dict:
    r = requests.put(
        f"{ASANA_BASE}/tasks/{task_id}",
        headers={**_asana_headers(), "Content-Type": "application/json"},
        json={"data": payload}, timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def add_comment(task_id: str, html: str, pinned: bool = False) -> dict:
    r = requests.post(
        f"{ASANA_BASE}/tasks/{task_id}/stories",
        headers={**_asana_headers(), "Content-Type": "application/json"},
        json={"data": {"html_text": html, "is_pinned": pinned}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def create_subtask(task_id: str, name: str, notes: str = "") -> dict:
    r = requests.post(
        f"{ASANA_BASE}/tasks",
        headers={**_asana_headers(), "Content-Type": "application/json"},
        json={"data": {"name": name, "notes": notes, "parent": task_id}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["data"]


def upload_attachment(task_id: str, filename: str, content: bytes,
                      content_type: str = "image/webp") -> dict:
    """POST a file as an Asana attachment on a task.

    Asana's attachment API only accepts task / project_brief parents — stories
    (comments) cannot be attachment parents (returns 400 "Not the correct type"),
    so all photos are attached at the task level. The filename slug is what
    correlates each photo with its comment (e.g. `premium-cleaning-1.webp`).
    """
    r = requests.post(
        f"{ASANA_BASE}/attachments",
        headers=_asana_headers(),                       # no Content-Type — multipart auto
        data={"parent": task_id},
        files={"file": (filename, content, content_type)},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Supabase API
# ---------------------------------------------------------------------------

def _get_anon_key() -> str:
    """Extract the public Supabase anon key from the SPA's JS bundle (cached).

    The bundle may embed multiple JWTs in the future (third-party SDKs, etc.),
    so pick by `role=anon` rather than first-match.
    """
    global _ANON_KEY
    if _ANON_KEY:
        return _ANON_KEY

    html = requests.get(SPA_BASE, timeout=15).text
    js_match = re.search(r'src="(/assets/index-[^"]+\.js)"', html)
    if not js_match:
        raise RuntimeError("Could not locate JS bundle in Service Wizard SPA HTML")
    js = requests.get(SPA_BASE + js_match.group(1), timeout=30).text

    for jwt in re.findall(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", js):
        try:
            payload_b64 = jwt.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        except (ValueError, json.JSONDecodeError):
            continue
        if payload.get("role") == "anon":
            _ANON_KEY = jwt
            return _ANON_KEY
    raise RuntimeError("Could not find a role=anon JWT in the JS bundle")


def _supabase_get(table: str, params: dict) -> list:
    key = _get_anon_key()
    r = requests.get(
        f"{SUPABASE_BASE}/rest/v1/{table}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, list):
        raise RuntimeError(f"Unexpected Supabase response on {table}: {body!r}")
    return body


def fetch_shoe_data(shoe_id: str) -> dict:
    """Fetch the current approval snapshot + damage entries from Supabase.

    Returns `{"snapshot": dict | None, "damages": list[dict]}`.
    """
    snaps = _supabase_get("shoe_approval_snapshots", {
        "shoe_id":    f"eq.{shoe_id}",
        "is_current": "eq.true",
        "select":     "*",
    })
    damages = _supabase_get("shoe_damages", {
        "shoe_id": f"eq.{shoe_id}",
        "select":  "*",
        "order":   "created_at.asc",
    })
    return {"snapshot": snaps[0] if snaps else None, "damages": damages}


def download_image(url: str) -> tuple[bytes, str]:
    """Download an image from the Supabase storage host; returns (bytes, ctype).

    SSRF guards: the URL must be HTTPS and resolve to the Supabase project we
    sourced the photo references from. The size is capped at MAX_PHOTO_BYTES
    (streamed read with running counter) so a runaway response can't OOM the
    worker even if Content-Length is missing or lies.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing non-HTTPS image URL: {url!r}")
    if parsed.hostname != SUPABASE_HOST:
        raise ValueError(f"refusing image URL from unexpected host: {parsed.hostname!r}")

    with requests.get(url, timeout=30, stream=True) as r:
        r.raise_for_status()
        declared = r.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_PHOTO_BYTES:
            raise ValueError(f"image declared size {declared} bytes exceeds cap")
        ctype = r.headers.get("Content-Type", "image/webp")
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > MAX_PHOTO_BYTES:
                raise ValueError(f"image exceeded cap of {MAX_PHOTO_BYTES} bytes")
            chunks.append(chunk)
    return b"".join(chunks), ctype


# ---------------------------------------------------------------------------
# Jina scrape — ONLY used to recover internal notes, which RLS hides from
# anonymous Supabase callers.
# ---------------------------------------------------------------------------

def find_link(notes: str) -> Optional[str]:
    for pattern in LINK_PATTERNS:
        m = re.search(pattern, notes)
        if m:
            return m.group(0)
    return None


def find_shoe_id(text: str) -> Optional[str]:
    m = SHOE_ID_RE.search(text)
    return m.group(1) if m else None


def scrape_internal_notes(url: str) -> list[str]:
    """Render the SPA via Jina, extract entries under `## Internal notes`.

    Failures (network, parse) return [] rather than raising — internal notes
    are an enrichment, not load-bearing.
    """
    r = requests.get(
        f"{JINA_BASE}/{url}",
        headers={"X-No-Cache": "true", "X-Timeout": "60"},
        timeout=120,
    )
    r.raise_for_status()
    text = r.text

    notes_m = re.search(
        r"## Internal notes\s*\n(.+?)(?=\n##|\nSection\s+\d+\s*\n|\Z)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if not notes_m:
        return []
    block = notes_m.group(1).strip()
    if not block or "no internal notes" in block.lower():
        return []
    return _clean_entries(block)


def _clean_entries(block: str) -> list[str]:
    """Parse a section that contains numbered entries with metadata.

    Each entry on the page renders as:
        1.   email@example.com 01 May, 14:46
        <blank line>
        <actual content, possibly multiple lines>
    Returns content strings only (numbering / email / timestamp stripped).
    """
    if not block:
        return []
    cleaned = re.sub(r"^Section\s+\d+\s*$", "", block, flags=re.MULTILINE)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return []
    pattern = r"^\d+\.\s+\S+@\S+\s+[^\n]+\n+(.+?)(?=^\d+\.\s+\S+@|\Z)"
    matches = re.findall(pattern, cleaned, flags=re.MULTILINE | re.DOTALL)
    if matches:
        return [re.sub(r"\s+", " ", m).strip() for m in matches if m.strip()]
    flat = re.sub(r"\s+", " ", cleaned).strip()
    return [flat] if flat else []


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

def _format_supabase_timestamp(ts: Optional[str]) -> Optional[str]:
    """Format Supabase ISO 8601 timestamp to match the SPA's `01 May, 14:48`.

    The SPA renders times in UTC without timezone conversion, so we do too.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%d %b, %H:%M")
    except (ValueError, TypeError):
        return ts


def build_data(shoe_data: dict, internal_entries: list[str]) -> dict:
    """Assemble the unified data dict from Supabase rows + Jina internal notes."""
    snapshot = shoe_data.get("snapshot")
    damages  = shoe_data.get("damages") or []

    if not snapshot:
        return {"has_snapshot": False}

    is_rejected = (snapshot.get("decision") or "") == "rejected"

    # snapshot.services is the FINAL set the customer agreed to (for approved
    # orders) or the proposed set the customer rejected (for rejected orders).
    # Services the customer removed from scope simply aren't present in this
    # array, so there's no per-row removed flag.
    services = []
    for s in snapshot.get("services") or []:
        services.append({
            "name":     s.get("name") or "",
            "tat_days": s.get("final_commitment_days") or s.get("tat"),
            "price":    float(s.get("price") or 0),
            "photos":   list(s.get("photos") or []),
        })

    # Damage entries: we surface both a structured per-entry view (for logs)
    # and a flattened, deduplicated photo bucket (for the comment).
    damage_entries: list[dict] = []
    stains_photos: list[str] = []
    seen: set[str] = set()
    for d in damages:
        photos = list(d.get("photo_urls") or [])
        if not photos and d.get("photo_url"):
            photos = [d["photo_url"]]
        damage_entries.append({"note": d.get("note") or "", "photos": photos})
        for p in photos:
            if p and p not in seen:
                seen.add(p)
                stains_photos.append(p)

    approver_label = snapshot.get("approved_by_label")
    approved_at    = snapshot.get("approved_at")
    source_label   = (snapshot.get("source") or "").capitalize() or None

    # Sorter-suggested = operator's snapshot at send time. The SPA only
    # populates `operator_services_at_send` when the customer mutated the
    # scope, so when empty, the final `services` list IS the operator's
    # recommendation (page mirrors this fallback).
    sorter_source     = snapshot.get("operator_services_at_send") or snapshot.get("services") or []
    sorter_suggested  = [s.get("name") for s in sorter_source if s.get("name")]

    return {
        "has_snapshot":      True,
        "is_rejected":       is_rejected,
        "rejection_reason":  snapshot.get("rejection_reason") if is_rejected else None,
        "rejector_name":     approver_label                             if is_rejected else None,
        "rejected_at":       _format_supabase_timestamp(approved_at)    if is_rejected else None,
        "approver_name":     approver_label                             if not is_rejected else None,
        "approver_time":     _format_supabase_timestamp(approved_at)    if not is_rejected else None,
        "approver_type":     source_label                               if not is_rejected else None,
        "approved_services": services,
        "total_tat":         snapshot.get("final_commitment_days"),
        "total_price":       snapshot.get("total_price"),
        "stains_entries":    [e["note"] for e in damage_entries if e["note"]],
        "stains_photos":     stains_photos,
        "damage_entries":    damage_entries,
        "sorter_suggested":  sorter_suggested,
        "internal_entries":  internal_entries,
    }


# ---------------------------------------------------------------------------
# Photo upload helpers
# ---------------------------------------------------------------------------

def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "photo"


def _to_jpeg(content: bytes, quality: int = 88) -> bytes:
    """Transcode any image bytes (webp / png / etc.) to JPEG for Asana.

    Asana's activity feed renders inline previews for JPEG/PNG but shows .webp
    as a generic file box, so we standardise on JPEG. Alpha channels are
    flattened onto white since JPEG doesn't support transparency.
    """
    img = Image.open(io.BytesIO(content))
    if img.mode not in ("RGB", "L"):
        if img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _attach_photos(task_id: str, photos: list[str], name_prefix: str) -> int:
    """Download each photo URL and upload to the Asana task as JPEG attachments.

    Photos are transcoded to JPEG so Asana renders them inline in the activity
    feed (webp uploads show as generic download boxes). Filenames use a slug
    of `name_prefix` so files in the task's attachments tab correlate visually
    with the comment they pair with (`premium-cleaning-1.jpg`, `damage-1.jpg`).

    Returns the count of successful uploads; failures are logged but don't
    abort the rest — partial coverage beats no coverage.
    """
    slug = _slugify(name_prefix)
    success = 0
    for i, url in enumerate(photos, 1):
        filename = f"{slug}-{i}.jpg"
        try:
            content, _ = download_image(url)
            jpeg = _to_jpeg(content)
            upload_attachment(task_id, filename, jpeg, "image/jpeg")
            success += 1
        except Exception as e:
            print(f"    failed to attach {filename}: {e}")
    return success


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

    # 2. Find link & extract shoe_id
    link = find_link(notes)
    if not link:
        print("No Service Wizard link found in description. Skipping.")
        return
    shoe_id = find_shoe_id(link)
    if not shoe_id:
        print(f"Could not extract shoe_id from link: {link}. Skipping.")
        return
    print(f"Link: {link}")
    print(f"Shoe: {shoe_id}")

    # 3. Fetch from Supabase
    print("Fetching shoe data from Supabase...")
    shoe_data = fetch_shoe_data(shoe_id)
    if not shoe_data["snapshot"]:
        print("No approval snapshot found yet. Skipping.")
        return

    # 4. Scrape internal notes from page (RLS blocks Supabase anon reads)
    print("Scraping internal notes via Jina...")
    try:
        internal_entries = scrape_internal_notes(link)
    except Exception as e:
        print(f"  internal-notes scrape failed: {e} (continuing without)")
        internal_entries = []

    data = build_data(shoe_data, internal_entries)

    if data["is_rejected"]:
        print(f"  Status:      REJECTED by {data['rejector_name']} at {data['rejected_at']}")
        print(f"  Reason:      {data['rejection_reason']}")
    else:
        print(f"  Approver:    {data['approver_name']} ({data['approver_type']}) at {data['approver_time']}")
        print(f"  Services:    {len(data['approved_services'])}")
        for svc in data["approved_services"]:
            ph = f" [{len(svc['photos'])} photo(s)]" if svc["photos"] else ""
            print(f"    - {svc['name']} · {svc['tat_days']}d · AED {svc['price']}{ph}")
        print(f"  Total:       AED {data['total_price']} · {data['total_tat']}d")
    print(f"  Stains:      entries={len(data['stains_entries'])}, photos={len(data['stains_photos'])}")
    print(f"  Internal:    entries={len(data['internal_entries'])}")

    # 5. Deduplication
    if "Approved by customer" in notes or "Rejected by customer" in notes:
        print("Already processed (description has status marker). Skipping.")
        return

    approved_services = data["approved_services"]
    approved_names_lc = {s["name"].lower() for s in approved_services}
    if approved_names_lc:
        existing = list_subtasks(task_id)
        existing_lc = {(s.get("name") or "").strip().lower() for s in existing}
        overlap = existing_lc & approved_names_lc
        if overlap:
            print(f"Already processed (subtask match: {overlap}). Skipping.")
            return

    # ── Side effects ─────────────────────────────────────────────────────────
    # The description marker (`Approved by customer` / `Rejected by customer`)
    # is the dedup commit-point — it MUST be the last write. If we write it
    # first and then crash mid-way, every subsequent run short-circuits and the
    # task is permanently stranded with missing comments / photos / subtasks.
    # Order: stains → per-service photo comments → subtasks → update_task.

    # 6. Stains & Damages comment with attached photos
    # Asana allows: <body>, <strong>, <em>, <u>, <s>, <code>, <ol>, <ul>, <li>,
    # <a>, <blockquote>, <pre>. <p> is NOT allowed.
    if data["stains_entries"] or data["stains_photos"]:
        print(f"Adding 'Stains & Damages' comment ({len(data['stains_photos'])} photos)...")
        html_parts = ["<body><strong>Stains &amp; Damages</strong>"]
        if data["stains_entries"]:
            html_parts.append("<ul>")
            for entry in data["stains_entries"]:
                html_parts.append(f"<li>{_html.escape(entry)}</li>")
            html_parts.append("</ul>")
        html_parts.append("</body>")
        add_comment(task_id, "".join(html_parts))
        if data["stains_photos"]:
            n = _attach_photos(task_id, data["stains_photos"], "damage")
            print(f"  attached {n}/{len(data['stains_photos'])} damage photos")

    # 7. Per-service photo comments + subtasks (only for approved orders)
    if not data["is_rejected"]:
        for svc in approved_services:
            if svc["photos"]:
                print(f"Adding photo comment for: {svc['name']} ({len(svc['photos'])} photo(s))")
                add_comment(task_id, f"<body><strong>{_html.escape(svc['name'])}</strong></body>")
                n = _attach_photos(task_id, svc["photos"], svc["name"])
                print(f"  attached {n}/{len(svc['photos'])} photos")

        for svc in approved_services:
            try:
                print(f"Creating subtask: {svc['name']}")
                create_subtask(task_id, svc["name"])
            except Exception as e:
                # Single subtask failure shouldn't strand the rest. The
                # description marker hasn't been written yet, so an operator-
                # driven retry (after clearing partial subtasks) will resume.
                print(f"  failed to create subtask {svc['name']!r}: {e}")

    # 8. Build new description + commit (LAST write)
    addition_lines: list[str] = []
    if data["is_rejected"]:
        addition_lines.append("Rejected by customer")
        if data["rejection_reason"]:
            addition_lines.append(f"Reason: {data['rejection_reason']}")
        if data["rejector_name"] and data["rejected_at"]:
            addition_lines.append(f"Rejected by {data['rejector_name']} · {data['rejected_at']}")
    else:
        addition_lines.append("Approved by customer")
        if data["approver_name"] and data["approver_time"]:
            addition_lines.append(f"Approved by {data['approver_name']} · {data['approver_time']}")

    new_notes = notes.rstrip() + "\n" + "\n".join(addition_lines)
    if data["internal_entries"]:
        new_notes += "\n\nInternal notes:\n" + "\n".join(f"- {e}" for e in data["internal_entries"])
    if data["sorter_suggested"]:
        new_notes += "\n\nSorter Suggested:\n" + ", ".join(data["sorter_suggested"]) + "."

    # Rejected orders: clear price + due date (no agreed work — leftover values
    # from prior runs would be misleading). Approved orders: set both.
    task_update: dict = {"notes": new_notes}
    if data["is_rejected"]:
        task_update["custom_fields"] = {PRICE_FIELD_GID: None}
        task_update["due_on"]        = None
    else:
        if data["total_price"] is not None:
            task_update["custom_fields"] = {PRICE_FIELD_GID: data["total_price"]}
        if data["total_tat"]:
            due_date = (date.today() + timedelta(days=data["total_tat"])).strftime("%Y-%m-%d")
            task_update["due_on"] = due_date
            print(f"  Due date:    {due_date} (today + {data['total_tat']} days)")

    print("Updating task (description"
          + (", clear price + due date" if data["is_rejected"] else ", price, due date")
          + ")...")
    update_task(task_id, task_update)

    print("\nDone!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process_task.py <task_id>")
        sys.exit(1)
    process_task(sys.argv[1])
