#!/usr/bin/env python3
"""
push_prospect_awards.py -- Trophy Case feed.

For every prospect in config/prospect_registry.json, pulls official MLB/MiLB
awards from the MLB Stats API (/people/{id}/awards) and sends them to the
existing updatePlayerStats endpoint as `awards` (only that field -- the
handler leaves seasons/splits/monthly untouched).  YankeesFarm's own content
is never included.

Env: WIX_STATS_PUSH_KEY (same key fetch_daily_stats.py uses).
"""
import json, os, sys, time
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_prospect_stats import fetch_awards

ENDPOINT = "https://www.yankeesfarmreport.com/_functions/updatePlayerStats"
KEY = os.environ.get("WIX_STATS_PUSH_KEY")


def main():
    if not KEY:
        raise SystemExit("WIX_STATS_PUSH_KEY environment variable not set")
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "config", "prospect_registry.json")) as f:
        registry = json.load(f)
    ok = skipped = failed = 0
    for slug, info in registry.items():
        awards = fetch_awards(info["personId"])
        if awards is None:       # API call failed: keep what the profile already has
            skipped += 1
            continue
        try:
            r = requests.post(ENDPOINT, json={"key": KEY, "slug": slug, "awards": awards}, timeout=20)
            r.raise_for_status()
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  x {slug}: {e}", file=sys.stderr)
        time.sleep(0.05)
    print(f"Awards pushed for {ok} players ({skipped} skipped, {failed} failed).")
    if failed > len(registry) * 0.2:
        sys.exit(1)


if __name__ == "__main__":
    main()
