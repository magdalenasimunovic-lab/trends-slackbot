#!/usr/bin/env python3
"""
Google Trends Slack Bot — GitHub Actions version
=================================================
Runs once per execution. GitHub Actions triggers it every hour via cron.
The trends cache (trends_cache.json) is committed back to the repo after
each run so it persists between executions.

Markets: Brazil, USA, Mexico, Nigeria, Italy, Morocco, Spain, UK, Canada
"""

import json
import os
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Configuration ──────────────────────────────────────────────────────────────

# Set in GitHub → Settings → Secrets and variables → Actions → New secret
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

MARKETS = {
    "🇧🇷 Brazil":   "BR",
    "🇺🇸 USA":      "US",
    "🇲🇽 Mexico":   "MX",
    "🇳🇬 Nigeria":  "NG",
    "🇮🇹 Italy":    "IT",
    "🇲🇦 Morocco":  "MA",
    "🇪🇸 Spain":    "ES",
    "🇬🇧 UK":       "GB",
    "🇨🇦 Canada":   "CA",
}

TOP_N             = 200   # fetch everything the RSS feed provides
CACHE_TTL_HOURS   = 24
TRENDS_RSS_URL    = "https://trends.google.com/trending/rss?geo={geo}"
TRENDS_PAGE_URL   = "https://trends.google.com/trending?geo={geo}"

RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

SPORTS_KEYWORDS = [
    "sport", "football", "soccer", "basketball", "tennis", "cricket", "rugby",
    "formula 1", "f1", "grand prix", "nba", "nfl", "mlb", "nhl",
    "ufc", "mma", "boxing", "wwe", "wrestling", "golf", "athletics",
    "fifa", "champions league", "premier league", "la liga", "bundesliga",
    "serie a", "ligue 1", "world cup", "olympics", "olympic", "paralympic",
    "wimbledon", "us open", "french open", "australian open", "grand slam",
    "super bowl", "world series", "stanley cup",
    "match", "game", "tournament", "championship", "league", "cup", "final",
    "score", "goal", "player", "coach", "transfer", "signing", "fixture",
    "real madrid", "barcelona", "manchester", "liverpool", "chelsea",
    "arsenal", "juventus", "milan", "inter", "psg", "ajax",
    "quarterback", "touchdown", "home run", "slam dunk", "hat trick",
    "futebol", "jogo", "copa", "campeonato", "partido", "gol", "liga",
    "selecao", "seleção", "atletico", "atlético", "boca", "river",
    "calcio", "partita", "campionato", "gara",
    "championnat",
    "كرة", "مباراة",
]

CACHE_FILE = Path("trends_cache.json")

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Cache helpers ──────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.warning("Cache corrupted — starting fresh.")
    return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

def get_seen_trends(cache: dict, country: str) -> set:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
    return {t for t, ts in cache.get(country, {}).items() if ts >= cutoff}

def update_cache(cache: dict, country: str, trends: list):
    now    = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
    existing = {t: ts for t, ts in cache.get(country, {}).items() if ts >= cutoff}
    for trend in trends:
        if trend not in existing:
            existing[trend] = now
    cache[country] = existing

# ── Sports detection ───────────────────────────────────────────────────────────

def is_sports_trend(title: str) -> bool:
    tl = title.lower()
    return any(kw in tl for kw in SPORTS_KEYWORDS)

# ── Fetcher ────────────────────────────────────────────────────────────────────

def fetch_trending(geo: str, label: str) -> list:
    url = TRENDS_RSS_URL.format(geo=geo)
    try:
        resp = requests.get(url, headers=RSS_HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        trends = []
        for item in root.findall(".//item"):
            el = item.find("title")
            if el is not None and el.text:
                trends.append(el.text.strip())
            if len(trends) >= TOP_N:
                break
        log.info(f"  [{label}] ✓ {len(trends)} trends fetched")
        return trends
    except requests.HTTPError as e:
        log.warning(f"  [{label}] HTTP {e.response.status_code}")
    except Exception as e:
        log.warning(f"  [{label}] Failed: {e}")
    return []

# ── Slack ──────────────────────────────────────────────────────────────────────

def _trend_blocks(items: list, fmt: str, heading: str) -> list:
    """
    Return a list of Slack section blocks for a set of trend titles.
    Items are chunked into groups of 15 to stay within Slack's 3 000-char
    per-block limit and 50-block per-message limit.
    fmt   — format string applied to each title, e.g. "🏆 *{}*" or "• {}"
    heading — bold label shown above the first chunk, e.g. "*⚽ Sports (3)*"
    """
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}}]
    for i in range(0, len(items), 15):
        chunk = items[i:i + 15]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(fmt.format(t) for t in chunk)},
        })
    return blocks


def build_payload(country: str, geo: str, new_trends: list, sports: list) -> dict:
    ts        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    other     = [t for t in new_trends if t not in sports]
    page_url  = TRENDS_PAGE_URL.format(geo=geo)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 New Trends — {country}", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🕐 *{ts}*  |  {len(new_trends)} new trend(s)"}]},
        {"type": "divider"},
    ]

    if sports:
        blocks.extend(_trend_blocks(sports, "🏆 *{}*", f"*⚽ Sports & Games ({len(sports)})*"))
        if other:
            blocks.append({"type": "divider"})

    if other:
        blocks.extend(_trend_blocks(other, "• {}", f"*📈 Other Trending Topics ({len(other)})*"))

    blocks += [
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View on Google Trends →", "emoji": True},
                "url": page_url,
                "style": "primary",
            }],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{page_url}|Google Trends — {country}> · Trends Slack Bot"}],
        },
    ]

    summary = f"📊 {len(new_trends)} new trend(s) in {country}"
    if sports:
        summary += f" — ⚽ {len(sports)} sports"
    return {"text": summary, "blocks": blocks}


def send_to_slack(payload: dict):
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not set — skipping Slack.")
        return
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("  ✅ Slack notification sent.")
    except requests.RequestException as e:
        log.error(f"  ❌ Slack failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("━" * 50)
    log.info(f"🔍 Trend check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info("━" * 50)

    cache = load_cache()
    sent  = 0

    for country, geo in MARKETS.items():
        log.info(f"Checking {country} ...")
        trends = fetch_trending(geo, country)

        if not trends:
            time.sleep(3)
            continue

        seen       = get_seen_trends(cache, country)
        new_trends = [t for t in trends if t not in seen]
        sports     = [t for t in new_trends if is_sports_trend(t)]

        if new_trends:
            log.info(f"  ✨ {len(new_trends)} new — {len(sports)} sports")
            send_to_slack(build_payload(country, geo, new_trends, sports))
            sent += 1
        else:
            log.info(f"  — Nothing new")

        update_cache(cache, country, trends)
        time.sleep(3)

    save_cache(cache)
    log.info(f"✅ Done. Sent {sent}/{len(MARKETS)} notifications.")


if __name__ == "__main__":
    main()
