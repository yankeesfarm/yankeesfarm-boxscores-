#!/usr/bin/env python3
"""
Push a generated leaderboard payload (from generate_leaderboard.py --json-out)
to a secured endpoint on the YankeesFarm Wix site, which writes it into a Wix
Data Collection for the site's Repeater/Table to display.
This is a PUBLISH step -- it is intentionally the last thing that runs, after
verify_data.py has already checked the source data. If this script exits
non-zero, the GitHub Actions workflow should treat that as a failed run (see
the "Push to website" step in the workflow) rather than silently leaving
stale data live on the site.
Auth: a shared secret, set as WIX_PUSH_SECRET in both GitHub Actions secrets
and the Wix Secrets Manager. The endpoint rejects any request that doesn't
present it. This is not a login system -- it's just enough to stop random
POST requests from overwriting your site's stat leaders.

Different periods can target different Wix HTTP functions (weekly/monthly
go to post_leaderboards; analytics goes to its own post_analytics function
with a different payload shape and its own collection). Use --endpoint-env
to point at the right one; it defaults to WIX_PUSH_ENDPOINT so existing
weekly/monthly calls are unaffected.

Usage:
    python push_to_wix.py --file data/weekly/week_2026-08-03_wix_payload.json --period weekly
    python push_to_wix.py --file data/monthly/2026-08_wix_payload.json --period monthly
    python push_to_wix.py --file data/analytics/data.json --period analytics --endpoint-env WIX_ANALYTICS_ENDPOINT
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
import requests
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 3
def push(endpoint_url, secret, payload, period):
    payload = dict(payload)
    payload["generatedAt"] = datetime.now(timezone.utc).isoformat()
    payload["period"] = period  # "weekly" (rolling display), "monthly" (official totals), or "analytics" (full 7-affiliate table)
    headers = {
        "Content-Type": "application/json",
        "X-YankeesFarm-Secret": secret,
    }
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(endpoint_url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            print(f"Attempt {attempt}/{MAX_RETRIES} failed (network error): {exc}")
            time.sleep(3 * attempt)
            continue
        if resp.status_code == 200:
            print(f"Pushed successfully: {resp.text[:300]}")
            return True
        # Auth/permission errors won't fix themselves on retry -- fail fast
        if resp.status_code in (401, 403):
            print(f"AUTH FAILURE ({resp.status_code}): check WIX_PUSH_SECRET matches on both "
                  f"sides. Response: {resp.text[:300]}")
            return False
        last_exc = RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
        print(f"Attempt {attempt}/{MAX_RETRIES} failed: {last_exc}")
        time.sleep(3 * attempt)
    print(f"Giving up after {MAX_RETRIES} attempts. Last error: {last_exc}")
    return False
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to a _wix_payload.json file")
    parser.add_argument("--period", required=True, choices=["weekly", "monthly", "analytics"])
    parser.add_argument("--endpoint-env", default="WIX_PUSH_ENDPOINT",
                         help="Name of the environment variable holding the target endpoint URL. "
                              "Defaults to WIX_PUSH_ENDPOINT (weekly/monthly). Pass "
                              "WIX_ANALYTICS_ENDPOINT for --period analytics.")
    args = parser.parse_args()
    endpoint_url = os.environ.get(args.endpoint_env)
    secret = os.environ.get("WIX_PUSH_SECRET")
    if not endpoint_url or not secret:
        print(f"ERROR: {args.endpoint_env} and WIX_PUSH_SECRET must be set as environment "
              "variables (GitHub Actions: repo Settings -> Secrets and variables -> Actions).")
        sys.exit(1)
    with open(args.file) as f:
        payload = json.load(f)
    ok = push(endpoint_url, secret, payload, args.period)
    if not ok:
        print("Push failed -- website was NOT updated. Previous data remains live.")
        sys.exit(1)
if __name__ == "__main__":
    main()
