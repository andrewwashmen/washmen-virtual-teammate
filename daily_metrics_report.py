#!/usr/bin/env python3
"""
Render scheduled job — daily metrics summary to Slack.

Once a day, fetches the web service's `/metrics` endpoint and posts a
formatted summary to the same Slack channel that receives error alerts
(via `SLACK_WEBHOOK_URL`).

Caveat: `/metrics` counters are process-local to the web service and reset
on every Render restart. Numbers reported here cover the window from the
worker's most recent restart up to the moment this job runs — not a strict
24-hour window. Reconciler activity is NOT included; reconcilers run as
separate processes and log their own summaries.

Required env vars:
  - METRICS_URL          full URL to the running /metrics endpoint
                         (e.g. https://washmen-virtual-teammate.onrender.com/metrics)
  - SLACK_WEBHOOK_URL    incoming webhook for the alerts channel
"""

import os
import sys
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)


def _fetch_metrics(url: str) -> dict:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def _format_uptime(seconds) -> str:
    if not isinstance(seconds, int) or seconds < 0:
        return "unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _build_payload(m: dict) -> dict:
    by_action = m.get("events_by_action") or {}
    actions_text = (
        "\n".join(f"• `{k}`: {v}" for k, v in sorted(by_action.items()))
        if by_action else "_none_"
    )

    last_error_lines = []
    if m.get("last_error_at"):
        last_error_lines.append(f"*At:* {m['last_error_at']}")
        if m.get("last_error_context"):
            last_error_lines.append(f"*Context:* {m['last_error_context']}")
        if m.get("last_error_message"):
            last_error_lines.append(f"*Message:* `{m['last_error_message']}`")
    last_error_text = "\n".join(last_error_lines) if last_error_lines else "_none since last restart_"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "text": f"Daily metrics — {today}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"Daily metrics — {today}"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Worker uptime*\n{_format_uptime(m.get('uptime_seconds'))}"},
                {"type": "mrkdwn", "text": f"*In flight*\n{m.get('in_flight_count', 0)}"},
                {"type": "mrkdwn", "text": f"*Events received*\n{m.get('events_received_total', 0)}"},
                {"type": "mrkdwn", "text": f"*Last event*\n{m.get('last_event_at') or '_none_'}"},
                {"type": "mrkdwn", "text": f"*Tasks processed*\n{m.get('tasks_processed_total', 0)}  (failed: {m.get('tasks_failed_total', 0)})"},
                {"type": "mrkdwn", "text": f"*Syncs processed*\n{m.get('syncs_processed_total', 0)}  (failed: {m.get('syncs_failed_total', 0)})"},
            ]},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Events by action*\n{actions_text}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Last error*\n{last_error_text}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": "Counters reset on Render restart. Reconciler activity not included."},
            ]},
        ],
    }


def _post_to_slack(webhook: str, payload: dict) -> None:
    r = requests.post(webhook, json=payload, timeout=10)
    r.raise_for_status()


def main() -> int:
    metrics_url = os.getenv("METRICS_URL")
    webhook     = os.getenv("SLACK_WEBHOOK_URL")

    if not metrics_url:
        log.error("METRICS_URL is not set.")
        return 1
    if not webhook:
        log.error("SLACK_WEBHOOK_URL is not set.")
        return 1

    log.info("Fetching metrics from %s", metrics_url)
    try:
        metrics = _fetch_metrics(metrics_url)
    except Exception as exc:
        # If /metrics is unreachable we still want a Slack note explaining
        # the silence — otherwise the team only notices the missing summary.
        log.error("Failed to fetch metrics from %s: %s", metrics_url, exc)
        try:
            _post_to_slack(webhook, {
                "text": f"Daily metrics — fetch failed: {type(exc).__name__}: {exc}"
            })
        except Exception as slack_exc:
            log.error("Also failed to notify Slack: %s", slack_exc)
        return 1

    log.info("Posting daily metrics summary to Slack …")
    try:
        _post_to_slack(webhook, _build_payload(metrics))
    except Exception as exc:
        log.error("Failed to post daily metrics summary to Slack: %s", exc)
        return 1

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
