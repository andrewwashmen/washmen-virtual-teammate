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
import time
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
SPA_BASE = "https://sc.washmen.com"

# Dubai is UTC+4 year-round (no DST). All operator-facing timestamps and the
# due-date calculation use this so what shows in Asana matches local clocks.
DUBAI_TZ = timezone(timedelta(hours=4))

# Hard cap on photo download size; Supabase webps are typically <500KB but we
# defend against runaway / hostile responses that could OOM the worker.
MAX_PHOTO_BYTES = 25 * 1024 * 1024

# Custom field GIDs
PRICE_FIELD_GID                    = "1202480206903933"
ASSESSMENT_FIELD_GID               = "1213817197288597"
ASSESSMENT_DONE_OPTION_GID         = "1213817197288598"
REASON_FOR_REJECTION_FIELD_GID     = "1202637830332848"  # multi_enum
INTERNAL_REJECTION_REASON_FIELD_GID = "1214569192694597"  # enum (single)

# Customer-facing rejection reasons → Asana option GIDs for the multi_enum
# "Reason for Rejection:" field. Used when source='customer' (the customer
# rejected via the Wizard). Matched case-insensitive with whitespace stripped.
#
# Lovable's customer-facing strings are shorter than Asana's option labels
# (e.g. "Changed my mind" vs the Asana option "Customer Changed their Mind"),
# so we register both forms — Asana label AND the Wizard's literal string —
# as keys that point to the same option GID. Add new aliases whenever an
# unrecognized-reason warning appears in the logs.
REJECTION_REASON_OPTIONS = {
    # "Pricing"
    "pricing":                                 "1202637830332849",
    # "Turn around time"
    "turn around time":                        "1202637830332850",
    # "Repair service not available"
    "repair service not available":            "1202637830332851",
    # "Replacement items not available"
    "replacement items not available":         "1202637830332852",
    # "Customer Is Not Happy"
    "customer is not happy":                   "1202650445593181",
    "not happy":                               "1202650445593181",
    # "Customer Changed their Mind"
    "customer changed their mind":             "1202675156630741",
    "changed my mind":                         "1202675156630741",
    "changed their mind":                      "1202675156630741",
    # "Sent wrong pair of Shoes/Bag"
    "sent wrong pair of shoes/bag":            "1202956846480528",
    "wrong item":                              "1202956846480528",
    # "Customer does not want any color change"
    "customer does not want any color change": "1203147939016011",
    "no color change":                         "1203147939016011",
    # "Donation"
    "donation":                                "1206664031863519",
    # "Transfer to Finery"
    "transfer to finery":                      "1207355056904088",
    # "Item not processed"
    "item not processed":                      "1207459611389499",
}

# Internal (facility-driven) rejection reasons → Asana option GID for the
# single-enum "Internal Rejection Reason" field. Used when source='facility'
# (a Washmen operator rejected on the customer's behalf — typically because
# the item couldn't be serviced). Matched case-insensitive with whitespace
# stripped. Unrecognized reasons log and skip; field stays empty.
INTERNAL_REJECTION_REASON_OPTIONS = {
    "transfer to finery":                          "1214569192694598",
    "weak material":                               "1214568232531707",
    "service not available":                       "1214568232531708",
    "structural integrity issues":                 "1214568232531709",
    "prior failed restoration/ modification risks": "1214568232531710",
    "prior failed restoration/modification risks":  "1214568232531710",  # without space
}

# Approval URLs from both the Lovable preview and the Washmen production domain
LINK_PATTERNS = [
    r"https://service-wizard-kit\.lovable\.app/approved/[a-f0-9\-]+",
    r"https://sc\.washmen\.com/approved/[a-f0-9\-]+",
]
SHOE_ID_RE = re.compile(r"/approved/([a-f0-9\-]{36})")

# Cache for the Supabase config (URL + anon key). Both are discovered from
# the SPA's JS bundle. The anon key is public — every browser visiting
# sc.washmen.com receives it — so caching it isn't a leak. RLS on the
# Supabase tables governs what data anon callers can read. Discovering the
# URL dynamically means a project migration on Washmen's side doesn't
# require a code change here.
_SUPABASE_BASE: Optional[str] = None
_ANON_KEY:      Optional[str] = None


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

def _discover_supabase_config() -> tuple[str, str]:
    """Discover Supabase base URL + anon key from the SPA's JS bundle (cached).

    The bundle may embed multiple JWTs (third-party SDKs, etc.), so we pick
    by `role=anon` rather than first-match. The Supabase URL likewise is
    found by regex match against the bundle, so a project migration on
    Washmen's side doesn't require a code change here.
    """
    global _SUPABASE_BASE, _ANON_KEY
    if _SUPABASE_BASE and _ANON_KEY:
        return _SUPABASE_BASE, _ANON_KEY

    html = requests.get(SPA_BASE, timeout=15).text
    js_match = re.search(r'src="(/assets/index-[^"]+\.js)"', html)
    if not js_match:
        raise RuntimeError("Could not locate JS bundle in Service Wizard SPA HTML")
    js = requests.get(SPA_BASE + js_match.group(1), timeout=30).text

    sb_match = re.search(r"https://[a-z0-9]+\.supabase\.co", js)
    if not sb_match:
        raise RuntimeError("Could not find Supabase URL in JS bundle")
    base = sb_match.group(0)

    anon_key = None
    for jwt in re.findall(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", js):
        try:
            payload_b64 = jwt.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        except (ValueError, json.JSONDecodeError):
            continue
        if payload.get("role") == "anon":
            anon_key = jwt
            break
    if not anon_key:
        raise RuntimeError("Could not find a role=anon JWT in the JS bundle")

    _SUPABASE_BASE, _ANON_KEY = base, anon_key
    return base, anon_key


def _get_anon_key() -> str:
    return _discover_supabase_config()[1]


def _get_supabase_base() -> str:
    return _discover_supabase_config()[0]


def _supabase_get(table: str, params: dict) -> list:
    base, key = _discover_supabase_config()
    r = requests.get(
        f"{base}/rest/v1/{table}",
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
    # Allow any Supabase project host. After Washmen's project migration,
    # some pre-existing rows in the new project still reference photo URLs
    # on the old project's storage host — pinning to the *current* project
    # only would reject those legitimate URLs.
    host = parsed.hostname or ""
    if not host.endswith(".supabase.co"):
        raise ValueError(f"refusing image URL from unexpected host: {host!r}")

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
    """Format Supabase ISO 8601 timestamp as Dubai local time `01 May, 18:48`."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(DUBAI_TZ).strftime("%d %b, %H:%M")
    except (ValueError, TypeError):
        return ts


def compute_snapshot_key(snapshot: dict, damages: list[dict]) -> str:
    """Stable signature used by the change-detection poller.

    Encodes:
      - snapshot version (incrementing per-shoe; lets us refetch the prior
        snapshot from Supabase for diffing on change)
      - per-damage `<id>:<photo_count>` (damages don't version in Supabase,
        so we keep enough state to diff add/remove + photo-count changes)

    Format: `v<N> | dmg=<uuid>:<count>,<uuid>:<count>` (no `dmg=` part if
    there are no damages).
    """
    version = snapshot.get("version") or 0
    sigs: list[str] = []
    for d in sorted(damages, key=lambda x: str(x.get("id") or "")):
        photos = list(d.get("photo_urls") or [])
        if not photos and d.get("photo_url"):
            photos = [d["photo_url"]]
        photos = [p for p in photos if p]
        sigs.append(f"{d.get('id')}:{len(photos)}")
    if not sigs:
        return f"v{version}"
    return f"v{version} | dmg=" + ",".join(sigs)


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
        # Raw lowercase source used to route rejection reason to the right
        # custom field (`facility` → Internal Rejection Reason, else → Reason
        # for Rejection). Set for both approved and rejected so callers don't
        # have to special-case.
        "rejector_source":   (snapshot.get("source") or "").lower()     if is_rejected else None,
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
        "snapshot_key":      compute_snapshot_key(snapshot, damages),
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


def _photo_stem(url: str) -> str:
    """Derive a stable, human-readable identifier from the source photo URL.

    Supabase storage URLs end in `<timestamp>-<random>.<ext>`; we strip the
    extension and any query string to get a stem like `1777646782293-zx1swb`.
    The resulting filename `<service>-<stem>.jpg` lets the change-detection
    poller match Asana attachments to source URLs deterministically.
    """
    basename = url.rsplit("/", 1)[-1].split("?")[0]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return re.sub(r"[^a-zA-Z0-9_-]", "", stem) or "photo"


def _attach_photos(task_id: str, photos: list[str], name_prefix: str) -> int:
    """Download each photo URL and upload to the Asana task as JPEG attachments.

    Photos are transcoded to JPEG so Asana renders them inline in the activity
    feed (webp uploads show as generic download boxes). Filenames use a slug
    of `name_prefix` plus a stable stem from the source URL, e.g.
    `damage-1777646782293-zx1swb.jpg`. The stem-based naming lets the polling
    sync compare existing Asana attachments to current Supabase URLs without
    re-uploading anything that hasn't changed.

    Returns the count of successful uploads; failures are logged but don't
    abort the rest — partial coverage beats no coverage.
    """
    slug = _slugify(name_prefix)
    success = 0
    for url in photos:
        filename = f"{slug}-{_photo_stem(url)}.jpg"
        try:
            content, _ = download_image(url)
            jpeg = _to_jpeg(content)
            upload_attachment(task_id, filename, jpeg, "image/jpeg")
            success += 1
        except Exception as e:
            print(f"    failed to attach {filename}: {e}")
    return success


# ---------------------------------------------------------------------------
# Rejection-reason field routing
# ---------------------------------------------------------------------------

def apply_rejection_reason_field(task_update: dict, data: dict) -> None:
    """Set the appropriate rejection-reason custom field on a task_update payload.

    Routes based on `data["rejector_source"]`:
      - "facility" → single-enum "Internal Rejection Reason" (us rejecting on
        the customer's behalf — typically because the item can't be serviced)
      - anything else (including "customer") → multi-enum "Reason for Rejection:"
        (the customer rejected via the Wizard)

    Mutates `task_update["custom_fields"]` in place. Unrecognized reason strings
    are logged and skipped — the field stays empty rather than guessing wrong.
    """
    rj = (data.get("rejection_reason") or "").strip().lower()
    if not rj:
        return

    custom_fields = task_update.setdefault("custom_fields", {})

    if data.get("rejector_source") == "facility":
        opt = INTERNAL_REJECTION_REASON_OPTIONS.get(rj)
        if opt:
            custom_fields[INTERNAL_REJECTION_REASON_FIELD_GID] = opt
            print(f"  Internal Rejection Reason: {data['rejection_reason']}")
        else:
            print(f"  Internal Rejection Reason: unrecognized "
                  f"{data['rejection_reason']!r}; field left empty")
    else:
        opt = REJECTION_REASON_OPTIONS.get(rj)
        if opt:
            custom_fields[REASON_FOR_REJECTION_FIELD_GID] = [opt]
            print(f"  Reason for Rejection: {data['rejection_reason']}")
        else:
            print(f"  Reason for Rejection: unrecognized "
                  f"{data['rejection_reason']!r}; field left empty")


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

    # Mark Assessment "Done" as soon as we see the Lovable link. Runs before
    # the Supabase fetch so it executes even when the snapshot isn't visible
    # yet (Lovable write-order race). Failure here is non-fatal — the rest of
    # the pipeline continues and a future process_task run will retry.
    try:
        update_task(task_id, {"custom_fields": {ASSESSMENT_FIELD_GID: ASSESSMENT_DONE_OPTION_GID}})
        print("  Assessment: marked Done")
    except Exception as e:
        print(f"  Assessment: update failed ({e}); continuing")

    # 3. Fetch from Supabase
    print("Fetching shoe data from Supabase...")
    shoe_data = fetch_shoe_data(shoe_id)
    if not shoe_data["snapshot"]:
        # Race: Lovable can write the Asana link before its Supabase snapshot
        # is visible to anon callers. Wait briefly and retry once.
        print("  No snapshot yet — retrying in 5s (Lovable write-order race) ...")
        time.sleep(5)
        shoe_data = fetch_shoe_data(shoe_id)
    if not shoe_data["snapshot"]:
        print("No approval snapshot found yet. Skipping (reconciler will catch up).")
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
    if data.get("snapshot_key"):
        # Used by the change-detection poller to know what was processed last.
        # Visible to operators as a debug aid; safe to ignore in normal use.
        new_notes += "\n\nSnapshot key: " + data["snapshot_key"]

    # Rejected orders: clear price + due date (no agreed work — leftover values
    # from prior runs would be misleading). Approved orders: set both.
    task_update: dict = {"notes": new_notes}
    if data["is_rejected"]:
        task_update["custom_fields"] = {PRICE_FIELD_GID: None}
        task_update["due_on"]        = None
        apply_rejection_reason_field(task_update, data)
    else:
        if data["total_price"] is not None:
            task_update["custom_fields"] = {PRICE_FIELD_GID: data["total_price"]}
        if data["total_tat"]:
            today_dubai = datetime.now(DUBAI_TZ).date()
            due_date = (today_dubai + timedelta(days=data["total_tat"])).strftime("%Y-%m-%d")
            task_update["due_on"] = due_date
            print(f"  Due date:    {due_date} (today + {data['total_tat']} days, Dubai)")

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
