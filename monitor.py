#!/usr/bin/env python3
"""
webook.com event monitor.

Discovers all events listed on webook.com by walking the official sitemap
index (https://webook.com/sitemap.xml) and its `sitemap_events_*.xml`
children. Compares the current set of event slugs against the previously
saved state and sends a Telegram notification for each newly added event.
On the very first run a baseline is saved and no notifications are sent.

If the sitemap is unavailable, falls back to parsing event links out of
the rendered HTML and the Next.js `__NEXT_DATA__` blob.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

SITEMAP_INDEX = "https://webook.com/sitemap.xml"
EVENTS_PAGE = "https://webook.com/en/events"
STATE_FILE = Path("state/known_events.json")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
         "image": "http://www.google.com/schemas/sitemap-image/1.1"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Match /en/events/<slug>, but exclude sub-pages like /book, /seats, /tickets.
EVENT_URL_RE = re.compile(
    r"^https?://webook\.com/en/events/([A-Za-z0-9][A-Za-z0-9_\-]*)/?$"
)


def http_get(url: str, *, retries: int = 3, timeout: int = 30) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            # webook's sitemap is served with a declared ISO-8859-1 header but
            # actually contains UTF-8 bytes — force the correct decoding.
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


# ---------------- Sitemap parsing (primary source) ---------------- #

def _humanize_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _list_event_sitemaps(index_xml: str) -> list[str]:
    root = ET.fromstring(index_xml)
    out: list[str] = []
    for sm in root.findall("sm:sitemap", SM_NS):
        loc = sm.findtext("sm:loc", default="", namespaces=SM_NS).strip()
        if "sitemap_events_" in loc and loc.endswith(".xml"):
            out.append(loc)
    return out


def _parse_event_sitemap(xml_text: str) -> dict[str, dict]:
    """Return {slug: {"slug", "title", "url"}} from one sitemap_events_*.xml."""
    found: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return found
    for url_el in root.findall("sm:url", SM_NS):
        loc = (url_el.findtext("sm:loc", default="", namespaces=SM_NS) or "").strip()
        m = EVENT_URL_RE.match(loc)
        if not m:
            continue
        slug = m.group(1)
        if not slug:
            continue
        title = ""
        img = url_el.find("image:image", SM_NS)
        if img is not None:
            title = (img.findtext("image:title", default="", namespaces=SM_NS) or "").strip()
        title = re.sub(r"\s+", " ", title)
        if not title:
            title = _humanize_slug(slug)
        found[slug] = {"slug": slug, "title": title, "url": loc.rstrip("/")}
    return found


def collect_events_from_sitemap() -> dict[str, dict]:
    index = http_get(SITEMAP_INDEX).text
    sitemaps = _list_event_sitemaps(index)
    print(f"  sitemap index lists {len(sitemaps)} event sitemap files")
    all_events: dict[str, dict] = {}
    for url in sitemaps:
        try:
            xml = http_get(url).text
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: failed to fetch {url}: {e}", file=sys.stderr)
            continue
        chunk = _parse_event_sitemap(xml)
        all_events.update(chunk)
    return all_events


# ---------------- HTML fallback ---------------- #

def _walk_for_events(node, out: dict):
    if isinstance(node, dict):
        slug = node.get("slug") or node.get("event_slug")
        title = (
            node.get("title")
            or node.get("name")
            or node.get("event_name")
            or node.get("title_en")
        )
        if slug and title and isinstance(slug, str) and isinstance(title, str):
            t = node.get("type") or node.get("__typename") or ""
            if "category" not in str(t).lower() and "venue" not in str(t).lower():
                out.setdefault(slug.strip("/"), {
                    "slug": slug.strip("/"),
                    "title": title.strip(),
                    "url": f"https://webook.com/en/events/{slug.strip('/')}",
                })
        for v in node.values():
            _walk_for_events(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_for_events(v, out)


def collect_events_from_html() -> dict[str, dict]:
    html = http_get(EVENTS_PAGE).text
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}

    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            _walk_for_events(json.loads(tag.string), out)
        except json.JSONDecodeError:
            pass

    href_re = re.compile(r"^/(?:en|ar)/events/([A-Za-z0-9][A-Za-z0-9_\-]*)/?$")
    for a in soup.find_all("a", href=True):
        m = href_re.match(a["href"])
        if not m:
            continue
        slug = m.group(1)
        title = a.get_text(strip=True) or a.get("title", "") or _humanize_slug(slug)
        out.setdefault(slug, {
            "slug": slug,
            "title": title,
            "url": f"https://webook.com/en/events/{slug}",
        })
    return out


def collect_events() -> dict[str, dict]:
    try:
        events = collect_events_from_sitemap()
        if events:
            return events
        print("  sitemap returned no events, falling back to HTML scrape")
    except Exception as e:  # noqa: BLE001
        print(f"  sitemap fetch failed ({e}), falling back to HTML scrape")
    return collect_events_from_html()


# ---------------- State + Telegram ---------------- #

def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(events: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        TELEGRAM_API.format(token=token),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def _chunks(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.", file=sys.stderr)
        return 2

    print("Collecting events from webook.com ...")
    try:
        current = collect_events()
    except Exception as e:  # noqa: BLE001
        print(f"Failed to collect events: {e}", file=sys.stderr)
        return 1
    print(f"Total events discovered: {len(current)}")

    if not current:
        print("No events found — aborting without touching state.", file=sys.stderr)
        return 1

    previous = load_state()
    first_run = not previous

    if first_run:
        save_state(current)
        print("First run: baseline saved, no notifications sent.")
        return 0

    new_slugs = sorted(set(current.keys()) - set(previous.keys()))
    print(f"New events since last run: {len(new_slugs)}")

    # Cap notification storms (e.g. if sitemap changes drastically) to avoid
    # spamming the chat. Anything beyond the cap is still saved to state.
    NOTIFY_CAP = 25
    to_notify = new_slugs[:NOTIFY_CAP]
    if len(new_slugs) > NOTIFY_CAP:
        print(f"Capping notifications at {NOTIFY_CAP} (rest are silently added to state)")

    for slug in to_notify:
        ev = current[slug]
        message = (
            "\U0001F389 <b>New event on webook.com</b>\n\n"
            f"<b>{ev['title']}</b>\n{ev['url']}"
        )
        try:
            send_telegram(token, chat_id, message)
            print(f"  notified: {slug}")
            time.sleep(0.5)  # polite spacing
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED to notify for {slug}: {e}", file=sys.stderr)

    merged = dict(previous)
    merged.update(current)
    save_state(merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
