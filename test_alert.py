#!/usr/bin/env python3
"""One-shot script to verify Slack alert wiring. Run once and delete.

Usage:
    python test_alert.py
"""
from dotenv import load_dotenv
load_dotenv()

import notify

if not notify._slack_webhook_url():
    print("SLACK_WEBHOOK_URL is not set in your .env file or environment.")
    raise SystemExit(1)

print("Sending test alert to Slack...")
notify.notify_error(
    "1234567890",
    RuntimeError("Slack alert test - wiring works if you see this in Slack"),
    notify.CTX_INITIAL,
)
print("Done. Check your Slack channel.")
