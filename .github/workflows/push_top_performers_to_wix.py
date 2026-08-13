#!/usr/bin/env python3
"""
Push the day's Top Performers payload to yankeesfarmreport.com.

Mirrors the existing push_to_wix.py pattern used for the weekly
leaderboard: a plain HTTPS POST with a shared-secret header, hitting a
Velo HTTP function that writes into a Wix Data Collection.

IMPORTANT: match this to whatever your real push_to_wix.py already does.
If that script uses a different header name than X-Push-Secret, or a
different auth scheme, change WIX_PUSH_SECRET_HEADER below to match --
the two endpoints should use the same convention.

ENV VARS:
    WIX_PUSH_SECRET   -- shared secret (same one used for the leaderboard
                          push; stored as a GitHub Actions secret)

USAGE:
    python3 push_top_performers_to_wix.py            # yesterday
    python3 push_top_performers_to_wix.py 2026-07-31  # specific date
"""

import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

WIX_ENDPOINT = "https://www.yankeesfarmreport.com/_functions/top_performers"
WIX_PUSH_SECRET_HEADER = "X-Push-Secret"


def main():
    default_date = (date.today() - timedelta(days=1)).isoformat()
    game_date    = sys.argv[1] if len(sys.argv) > 1 else default_date

    payload_path = Path("output") / f"{game_date}_top_performers.json"
    if not payload_path.exists():
        print(f"ERROR: {payload_path} not found. Run top_performers.py for "
              f"{game_date} first.")
        sys.exit(1)

    secret = os.environ.get("WIX_PUSH_SECRET")
    if not secret:
        print("ERROR: WIX_PUSH_SECRET environment variable not set.")
        sys.exit(1)

    body = payload_path.read_bytes()
    req = urllib.request.Request(
        WIX_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            WIX_PUSH_SECRET_HEADER: secret,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"Wix responded {resp.status}: {resp.read().decode('utf-8')}")
    except Exception as e:
        print(f"ERROR pushing to Wix: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
