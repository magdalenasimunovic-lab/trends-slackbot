#!/usr/bin/env python3
"""
Google Trends Slack Bot - GitHub Actions version
=================================================
Runs once per execution. GitHub Actions triggers it every hour via cron.
Fetches all trending topics (past 24 hours) across 9 markets, sorted by
search volume (highest relevance first).

Requires one GitHub secret:
  SLACK_WEBHOOK_URL  -- Slack Incoming Webhook URL

Markets: Brazil, USA, Mexico, Nigeria, Italy, Morocco, Spain, UK, Canada
"""

import json
import os
import re
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# -- Configuration -------------------------------------------------------------

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

MARKETS = {
    "Brazil":  "BR",
    "USA":     "US",
    "Mexico":  "MX",
    "Nigeria": "NG",
    "Italy":   "IT",
    "Morocco": "MA",
    "Spain":   "ES",
    "UK":      "GB",
    "Canada":  "CA",
}

TOP_N           = 200
CACHE_TTL_HOURS = 1    # re-report trends every hour
TRENDS_RSS_URL  = "https://trends.google.com/trending/rss?geo={geo}&hours=24"
TRENDS_PAGE_URL = "https://trends.google.com/trending?geo={geo}&hours=24"

HT_NS = "https://trends.google.com/trends/"  # namespace for ht:approx_traffic

RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

CACHE_FILE = Path("trends_cache.json")

# -- Logging -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# -- Helpers -------------------------------------------------------------------

def parse_traffic(raw: str) -> int:
    # Convert "1M+", "200K+", "50K+", "500+" to an integer for sorting
    raw = raw.strip().replace("+", "").replace(",", "")
    m = re.match(r"([\d.]+)([KMB]?)", raw, re.IGNORECASE)
    if not m:
        return 0
    val, suffix = float(m.group(1)), m.group(2).upper()
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(val * multipliers.get(suffix, 1))

# -- Cache helpers -------------------------------------------------------------

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.warning("Cache corrupted -- starting fresh.")
    return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

def get_seen_trends(cache: dict, country: str) -> set:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
    return {t for t, ts in cache.get(country, {}).items() if ts >= cutoff}

def update_cache(cache: dict, country: str, titles: list):
    now    = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
    existing = {t: ts for t, ts in cache.get(country, {}).items() if ts >= cutoff}
    for title in titles:
        if title not in existing:
            existing[title] = now
    cache[country] = existing

# -- Fetcher -------------------------------------------------------------------

def fetch_trending(geo: str, label: str) -> list:
    # Returns list of dicts: {"title": str, "traffic": str, "traffic_val": int}
    # sorted by traffic_val descending (most searched first)
    url = TRENDS_RSS_URL.format(geo=geo)
    try:
        resp = requests.get(url, headers=RSS_HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        trends = []
        for item in root.findall(".//item"):
            title_el   = item.find("title")
            traffic_el = item.find(f"{{{HT_NS}}}approx_traffic")

            if title_el is None or not title_el.text:
                continue

            title   = title_el.text.strip()
            traffic = traffic_el.text.strip() if traffic_el is not None and traffic_el.text else ""
            trends.append({
                "title":       title,
                "traffic":     traffic,
                "traffic_val": parse_traffic(traffic) if traffic else 0,
            })
            if len(trends) >= TOP_N:
                break

        # Sort highest search volume first
        trends.sort(key=lambda x: x["traffic_val"], reverse=True)
        log.info(f"  [{label}] fetched {len(trends)} trends")
        return trends
    except requests.HTTPError as e:
        log.warning(f"  [{label}] HTTP {e.response.status_code}")
    except Exception as e:
        log.warning(f"  [{label}] Failed: {e}")
    return []

# -- Slack ---------------------------------------------------------------------

def _trend_blocks(items: list, heading: str) -> list:
    # Split trends into chunked Slack section blocks (max 15 per block)
    # to stay within Slack 3000-char per-block and 50-block per-message limits.
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}}]
    for i in range(0, len(items), 15):
        chunk = items[i:i + 15]
        lines = []
        for item in chunk:
            label = f"*{item['title']}*"
            if item["traffic"]:
                label += f"  `{item['traffic']} searches`"
            lines.append(label)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        })
    return blocks


def build_payload(country: str, geo: str, trends: list) -> dict:
    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page_url = TRENDS_PAGE_URL.format(geo=geo)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Trending Now -- {country}", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{ts}*  |  {len(trends)} new trend(s)  |  Past 24h  |  Sorted by relevance"}]},
        {"type": "divider"},
    ]
    blocks.extend(_trend_blocks(trends, f"*Trending ({len(trends)})*"))
    blocks += [
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View on Google Trends", "emoji": True},
                "url": page_url,
                "style": "primary",
            }],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{page_url}|Google Trends -- {country}> - Trends Slack Bot"}],
        },
    ]
    return {"text": f"{len(trends)} new trend(s) in {country}", "blocks": blocks}


def send_to_slack(payload: dict):
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not set -- skipping Slack.")
        return
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("  Slack notification sent.")
    except requests.RequestException as e:
        log.error(f"  Slack failed: {e}")

# -- Main ----------------------------------------------------------------------

def main():
    log.info("-" * 50)
    log.info(f"Trend check -- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info(f"Filter: All trends | Past 24h | Sorted by relevance | {len(MARKETS)} markets")
    log.info("-" * 50)

    cache = load_cache()
    sent  = 0

    for country, geo in MARKETS.items():
        log.info(f"Checking {country} ...")
        trends = fetch_trending(geo, country)

        if not trends:
            time.sleep(3)
            continue

        seen       = get_seen_trends(cache, country)
        new_trends = [t for t in trends if t["title"] not in seen]

        if not new_trends:
            log.info(f"  -- Nothing new")
            update_cache(cache, country, [t["title"] for t in trends])
            time.sleep(3)
            continue

        log.info(f"  {len(new_trends)} new trend(s) -- sending to Slack")
        send_to_slack(build_payload(country, geo, new_trends))
        sent += 1

        update_cache(cache, country, [t["title"] for t in trends])
        time.sleep(3)

    save_cache(cache)
    log.info(f"Done. Sent {sent}/{len(MARKETS)} notifications.")


if __name__ == "__main__":
    main()
