
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
TRENDS_RSS_URL  = "https://trends
