# webook-monitor

Monitors [webook.com/en/events](https://webook.com/en/events) and sends a
Telegram alert whenever a new event appears. Runs every 15 minutes on
GitHub Actions — no laptop required.

## How it works

1. `monitor.py` discovers events by walking webook's official sitemap
   (`https://webook.com/sitemap.xml` → `sitemap_events_*.xml`). If the
   sitemap is unreachable it falls back to scraping the events page HTML.
2. It diffs the current event slugs against `state/known_events.json`.
3. For each newly seen slug, a Telegram message is sent.
4. The updated state is committed back to the repo so the next run knows
   what's already been seen.

The very first run saves a baseline and sends no notifications.

## Running locally

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="<your bot token>"
export TELEGRAM_CHAT_ID="<your chat id>"
python3 monitor.py
```

## Secrets (GitHub)

Add these in **Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Also enable **Settings → Actions → General → Workflow permissions →
Read and write permissions** so the workflow can commit the state file.

## Manually trigger

```bash
gh workflow run monitor.yml
gh run list --workflow=monitor.yml
```
