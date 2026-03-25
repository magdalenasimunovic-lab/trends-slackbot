#!/usr/bin/env python3
"""
Google Trends Slack Bot - GitHub Actions version
=================================================
Runs once per execution. GitHub Actions triggers it every hour via cron.

Filters applied in code (RSS feed ignores URL filter params):
  - Sports only  : trend's news articles come from known sports sources
  - Active only  : trend pubDate is within the last ACTIVE_HOURS hours
  - By relevance : sorted by search volume (ht:approx_traffic) descending

The "View on Google Trends" button links to the web page with all three
website filters pre-applied (category=17, status=active, hours=24).

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
from email.utils import parsedate_to_datetime
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
ACTIVE_HOURS    = 4    # mirror "Show active trends only" -- trends started within 4h

# RSS feed -- geo is the only param it actually respects
TRENDS_RSS_URL  = "https://trends.google.com/trending/rss?geo={geo}"

# Web page link uses the real UI filters: Sports + Active + 24h
TRENDS_PAGE_URL = (
    "https://trends.google.com/trending"
    "?geo={geo}&category=17&status=active&hours=24"
)

HT_NS = "https://trends.google.com/trends/"

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

# -- Sports detection ----------------------------------------------------------
# The RSS feed ignores &cat=s / &category=17, so we detect sports by checking
# whether the trend's news articles come from known sports outlets.

SPORTS_SOURCES = {
    "espn", "yahoo sports", "bbc sport", "sky sports", "the athletic",
    "bleacher report", "sports illustrated", "fox sports", "cbs sports",
    "nbc sports", "nfl.com", "nba.com", "mlb.com", "nhl.com",
    "goal", "goal.com", "sporting news", "90min", "marca", "as.com",
    "la gazzetta", "gazzetta dello sport", "corriere dello sport",
    "globo esporte", "lance!", "lance", "uol esporte",
    "talksport", "cafonline", "transfermarkt", "fotmob",
    "golf digest", "golf channel", "motorsport", "formula1",
    "tennis.com", "ufc.com", "boxingscene", "mmafighting",
    "mlssoccer", "bundesliga", "premierleague", "laliga",
    "fifa.com", "uefa.com", "olympics.com", "wta.com", "atptour.com",
    "lequipe", "equipe", "sport.es", "mundo deportivo",
    "sportbible", "givemesport", "eurosport", "sportstar",
    "cricket.com", "cricinfo", "espncricinfo",
    "nascar.com", "f1.com", "motorsportweek", "autosport",
    "draftnetwork", "tankathon", "rotowire", "fantasypros",
    "polymarket",
}

SPORTS_TITLE_KEYWORDS = {
    # Match/game indicators (covers all markets + Brazilian "x" format)
    " vs ", " vs. ", " v ", " x ",  # "Flamengo x Corinthians", "City vs United"
    "vs.", " fc ", " sc ", " cf ",   # club abbreviations
    # Leagues & competitions
    "nfl", "nba", "mlb", "nhl", "mls",
    "fifa", "uefa", "champions league", "premier league", "la liga",
    "serie a", "bundesliga", "ligue 1", "copa", "world cup", "euro ",
    "masters", "grand slam", "wimbledon", "roland garros", "us open",
    "super bowl", "world series", "stanley cup", "nba finals", "super league",
    "libertadores", "sudamericana", "brasileirao", "campeonato",
    # In-game / results
    "transfer", "draft", "lineup", "fixture", "standings", "matchday",
    "goal", "scored", "defeated", "wins", "loses", "draw", "final score",
    # Motor & other sports
    "formula 1", "f1 ", "motogp", "nascar", "grand prix",
    "olympics", "commonwealth games",
    "ufc ", "wwe ", "boxing", "bout", "knockout", "fight night",
}


def is_sports(title: str, sources: list) -> bool:
    # 1. Sports news source match
    sources_lower = {s.lower() for s in sources}
    if sources_lower & SPORTS_SOURCES:
        return True
    title_lower = title.lower()
    # 2. "vs" pattern in any form: "Team vs Team", "teamvsteam", "Team vs. Team"
    if re.search(r'\bvs\.?\b', title_lower):
        return True
    # 3. Brazilian/Portuguese match format: "Flamengo x Corinthians"
    if re.search(r'\b\w+\s+x\s+\w+', title_lower):
        return True
    # 4. Other sports keywords
    for kw in SPORTS_TITLE_KEYWORDS:
        if kw in title_lower:
            return True
    return False

# -- Logging -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# -- Helpers -------------------------------------------------------------------

def parse_traffic(raw: str) -> int:
    raw = raw.strip().replace("+", "").replace(",", "")
    m = re.match(r"([\d.]+)([KMB]?)", raw, re.IGNORECASE)
    if not m:
        return 0
    val, suffix = float(m.group(1)), m.group(2).upper()
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(val * multipliers.get(suffix, 1))


def is_active(pub_date_str: str) -> bool:
    if not pub_date_str:
        return True
    try:
        pub_dt = parsedate_to_datetime(pub_date_str)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ACTIVE_HOURS)
        return pub_dt >= cutoff
    except Exception:
        return True

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
    """
    Fetch all trends, apply sports + active filters in code, sort by relevance.
    Returns list of dicts: {title, traffic, traffic_val}
    """
    url = TRENDS_RSS_URL.format(geo=geo)
    try:
        resp = requests.get(url, headers=RSS_HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        all_items, skipped_sports, skipped_active = [], 0, 0

        for item in root.findall(".//item"):
            title_el   = item.find("title")
            traffic_el = item.find(f"{{{HT_NS}}}approx_traffic")
            pubdate_el = item.find("pubDate")

            if title_el is None or not title_el.text:
                continue

            title    = title_el.text.strip()
            traffic  = traffic_el.text.strip() if traffic_el is not None and traffic_el.text else ""
            pub_date = pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else ""

            # Collect news sources for sports detection
            sources = [
                src.text.strip()
                for src in item.findall(f".//{{{HT_NS}}}news_item_source")
                if src.text
            ]

            # Filter: sports only
            if not is_sports(title, sources):
                skipped_sports += 1
                continue

            # Filter: active trends only (started within ACTIVE_HOURS)
            if not is_active(pub_date):
                skipped_active += 1
                continue

            all_items.append({
                "title":       title,
                "traffic":     traffic,
                "traffic_val": parse_traffic(traffic) if traffic else 0,
            })

            if len(all_items) >= TOP_N:
                break

        # Sort by relevance (search volume) highest first
        all_items.sort(key=lambda x: x["traffic_val"], reverse=True)
        log.info(
            f"  [{label}] {len(all_items)} active sports trends "
            f"(skipped {skipped_sports} non-sports, {skipped_active} inactive)"
        )
        return all_items

    except requests.HTTPError as e:
        log.warning(f"  [{label}] HTTP {e.response.status_code}")
    except Exception as e:
        log.warning(f"  [{label}] Failed: {e}")
    return []

# -- Slack ---------------------------------------------------------------------

def _trend_blocks(items: list, heading: str) -> list:
    # Chunked Slack section blocks, max 15 per block to stay within limits.
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}}]
    for i in range(0, len(items), 15):
        chunk = items[i:i + 15]
        lines = []
        for item in chunk:
            line = f"*{item['title']}*"
            if item["traffic"]:
                line += f"  `{item['traffic']} searches`"
            lines.append(line)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        })
    return blocks


def build_payload(country: str, geo: str, trends: list) -> dict:
    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page_url = TRENDS_PAGE_URL.format(geo=geo)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Sports Trends -- {country}", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": (
                f"*{ts}*  |  {len(trends)} trend(s)  "
                f"|  Sports  |  Active only  |  By relevance"
            )}],
        },
        {"type": "divider"},
    ]
    blocks.extend(_trend_blocks(trends, f"*Trending Now ({len(trends)})*"))
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
            "elements": [{"type": "mrkdwn", "text": (
                f"<{page_url}|Google Trends -- {country}> - Trends Slack Bot"
            )}],
        },
    ]
    return {"text": f"{len(trends)} active sports trend(s) in {country}", "blocks": blocks}


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
    log.info("-" * 60)
    log.info(f"Trend check -- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info(f"Filters: Sports | Active (<{ACTIVE_HOURS}h) | By relevance | {len(MARKETS)} markets")
    log.info("-" * 60)

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
