#!/usr/bin/env python3
"""
Slack alerting for the Asana automation.

Silently no-ops if `SLACK_WEBHOOK_URL` is not set, so it's safe to wrap any
code path. Notification failures are caught and logged — they never break
the calling code path.

Configure once: create a Slack incoming webhook
(https://api.slack.com/messaging/webhooks), pick the channel, copy the URL,
and set `SLACK_WEBHOOK_URL` as an env var on every Render service that
runs automation code (web service + reconciler cron).
"""

import os
import logging
import requests
from typing import Optional

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# Phase 1 (initial callback). Lovable wrote the link, webhook fired,
# process_task failed. Reconciler may catch it next hour, or operator
# can re-trigger by editing the description (any notes change re-fires
# the webhook).
CTX_INITIAL = "Initial callback (process_task)"

# Phase 3 (change sync). Lovable posted an "Approval link:" comment
# signalling a saved change, sync_task ran and failed.
CTX_CHANGE  = "Change sync (sync_task)"

# Hourly reconciler errored on a specific task.
CTX_RECON   = "Reconciler"


def _task_url(task_id: str, project_gid: str = "1202289964354061") -> str:
    return f"https://app.asana.com/0/{project_gid}/{task_id}"


def _retry_hint(context: str, task_id: str) -> str:
    if context == CTX_CHANGE:
        return (
            "• Wait — Lovable posts a new `Approval link:` comment on every save, which re-fires the sync\n"
            f"• Manual: `python sync_task.py {task_id}` from the project directory"
        )
    # Initial callback or reconciler: same retry options
    return (
        "• Wait — the hourly reconciler will retry automatically\n"
        "• Or have the link re-edited in Asana (any notes change re-fires the webhook)\n"
        f"• Manual: `python process_task.py {task_id}` from the project directory"
    )


def notify_error(task_id: str, exc: BaseException, context: str,
                 task_name: Optional[str] = None) -> None:
    """Post an error alert to Slack with retry instructions."""
    if not SLACK_WEBHOOK_URL:
        return

    err_text = f"{type(exc).__name__}: {exc}"[:1500]
    title    = task_name or task_id

    payload = {
        "text": f"Asana automation error on task {task_id}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Asana automation error"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Task*\n<{_task_url(task_id)}|{title}>"},
                {"type": "mrkdwn", "text": f"*Context*\n{context}"},
            ]},
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*Error*\n```{err_text}```",
            }},
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*Retry*\n{_retry_hint(context, task_id)}",
            }},
        ],
    }

    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as e:
        log.error("Failed to post Slack notification for task %s: %s", task_id, e)
