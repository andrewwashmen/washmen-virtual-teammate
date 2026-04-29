#!/usr/bin/env python3
"""
Run this ONCE after deploying to Railway to register the Asana webhook.

Usage:
  1. Add SERVER_URL to your .env  (e.g. https://your-app.railway.app)
  2. python register_webhook.py
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

PAT         = os.getenv("ASANA_PAT")
PROJECT_GID = os.getenv("ASANA_PROJECT_GID", "1202289964354061")
SERVER_URL  = os.getenv("SERVER_URL", "").rstrip("/")

if not SERVER_URL:
    print("ERROR: Set SERVER_URL in your .env file.")
    print("  Example: SERVER_URL=https://your-app.railway.app")
    raise SystemExit(1)

headers = {
    "Authorization": f"Bearer {PAT}",
    "Content-Type": "application/json",
}

print(f"Registering webhook …")
print(f"  Project : {PROJECT_GID}")
print(f"  Target  : {SERVER_URL}/webhook")

r = requests.post(
    "https://app.asana.com/api/1.0/webhooks",
    headers=headers,
    json={
        "data": {
            "resource": PROJECT_GID,
            "target": f"{SERVER_URL}/webhook",
            "filters": [
                {
                    "resource_type": "task",
                    "action": "changed",
                    "fields": ["notes"],
                }
            ],
        }
    },
    timeout=15,
)

data = r.json()

if r.ok:
    print("\nWebhook registered successfully!")
    print(f"  Webhook GID : {data['data']['gid']}")
    print(f"  Target      : {data['data']['target']}")
    print(f"  Active      : {data['data'].get('active', True)}")
else:
    print(f"\nFailed ({r.status_code}):")
    print(data)
