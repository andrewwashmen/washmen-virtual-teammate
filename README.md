# Washmen Virtual Teammate

Automation that keeps Asana tasks in sync with the Service Wizard (Lovable). When a customer approves an item, this bot creates subtasks, attaches photos, posts stains/damages comments, sets the price + due date, and writes status markers — automatically, in real time. When the customer or operator changes scope after approval, it syncs the diff.

## Architecture

Three services on Render, all sharing the same codebase:

| Service | Type | Purpose |
|---------|------|---------|
| `washmen-virtual-teammate` | web (always-on) | Receives Asana webhooks, dispatches to `process_task` or `sync_task` |
| `washmen-virtual-teammate-reconciler` | cron, hourly | Catches tasks that have a Lovable link in description but no `Approved by customer` marker (missed initial-callback events) |
| `washmen-virtual-teammate-sync-reconciler` | cron, every 15 min | Catches tasks already processed but whose Snapshot key drifted from Supabase (missed change-sync events) |

Errors from any path post to Slack with the task link and retry guidance.

## Trigger flow

```
Customer approves in Service Wizard
        ↓
Lovable writes "Approval link:" comment + description link to Asana
        ↓
Asana webhook fires to washmen-virtual-teammate web service
        ↓
process_task (initial) or sync_task (subsequent change)
        ↓
Subtasks, comments, photos, custom fields, description marker
```

## Auth: bot identity

All Asana writes are attributed to a dedicated bot identity, **not** a real person.

- **Identity:** "SC Bot" — Asana guest user with an external email (free on our Enterprise plan, no seat consumed)
- **PAT location:** stored in `ASANA_PAT` env var on each of the 3 Render services. Master copy in 1Password.
- **Lovable's writes** (`Approval link:` comments, description link writes) come from Lovable's own dedicated bot account, separate from SC Bot. Each side owns its own credentials.

### Rotating SC Bot's PAT

If the PAT is ever compromised or you want to rotate:

1. Sign in as SC Bot, generate a new PAT under *Profile Settings → Apps → Personal Access Tokens*
2. Update `ASANA_PAT` on all 3 Render services
3. Locally, update `.env` with the new PAT, run `python register_webhook.py` to re-register the webhook so its owner uses the new token
4. Revoke the old PAT in Asana

## Environment variables

| Variable | Where | Required | Purpose |
|----------|-------|----------|---------|
| `ASANA_PAT` | Render (all 3 services) + local `.env` | Yes | Auth for Asana API calls and webhook registration |
| `ASANA_PROJECT_GID` | Render + `.env` | Yes (default in code) | The ShoeCare project GID (`1202289964354061`) |
| `ASANA_WORKSPACE_GID` | Render + `.env` | No (default in code) | Workspace GID (`41091308892039`) |
| `SLACK_WEBHOOK_URL` | Render (all 3 services) | No | Where error alerts post; silently no-ops if unset |
| `SERVER_URL` | Local `.env` only | Yes for `register_webhook.py` | Public URL of the web service (e.g., `https://washmen-virtual-teammate.onrender.com`) |

`render.yaml` declares each service's required env vars. Secrets are set manually in the Render dashboard (`sync: false`).

## Operating

### Smoke test (manual run on a single task)

```bash
python process_task.py <task_gid>          # initial processing
python sync_task.py <task_gid> [--dry-run] # change sync
```

`process_task` is idempotent — it short-circuits if the description already has `Approved by customer` or `Rejected by customer`. Safe to re-run.

### Re-register the webhook

```bash
python register_webhook.py
```

Auto-deletes any existing webhook for the same project + target URL before creating a new one. Run after rotating the PAT, changing the Render URL, or whenever the webhook stops firing.

### Manually trigger a reconciler

```bash
python reconcile_missed.py    # hourly reconciler
python reconcile_changes.py   # 15-min sync reconciler
```

Both are idempotent. `reconcile_missed` finds tasks with a Lovable link but no Approved/Rejected marker. `reconcile_changes` finds approved tasks whose stored Snapshot key differs from Supabase.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Bot doesn't process a task with the link | Customer hasn't actually approved at the link page yet → no Supabase snapshot | Verify the link page shows "Approved" status |
| Webhook events stop firing | The PAT that registered the webhook was revoked, or the Render URL changed | Re-register: `python register_webhook.py` |
| Comments still show as a real person's name | Render didn't pick up the new env var | Verify `ASANA_PAT` value in Render dashboard, redeploy |
| `403 Forbidden` on custom field updates | Bot lacks Editor permission on ShoeCare project | Re-share project with SC Bot at Editor level |
| Slack alerts say "no snapshot" repeatedly | Lovable's writeback to Supabase is failing or delayed | Check with Lovable's team |
| Same task processed twice | Two deployments running against the same project | Confirm only one deployment is firing webhooks (cross-check `render.yaml` against any other repos) |

## Files

- `webhook_server.py` — Flask app, dispatches Asana events
- `process_task.py` — initial processing pipeline
- `sync_task.py` — change-detection sync
- `reconcile_missed.py` — hourly catch-up cron
- `reconcile_changes.py` — 15-min catch-up cron
- `notify.py` — Slack error alerts
- `register_webhook.py` — webhook registration utility
- `render.yaml` — Render Blueprint (services + env var declarations)
- `.env.example` — local env var template
