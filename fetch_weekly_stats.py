#!/usr/bin/env python3
"""
Pull the past 7 days of hitting/pitching stats for all four Yankees
full-season affiliates from statsapi.mlb.com, filter out non-prospects,
and save a dated JSON snapshot. Designed to run weekly via GitHub Actions,
and to be re-run manually for a specific window with --start/--end.

Usage:
    python fetch_weekly_stats.py
    python fetch_weekly_stats.py --start 2026-07-28 --end 2026-08-03
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.affiliates import AFFILIATES
from lib.mlb_api import get_active_roster, get_player_stats_by_date_range
from lib.dedupe import dedupe_by_id

SEASON = 2026
OUTPUT_DIR = "data/weekly"

# The MLB Stats API returns rate stats (avg/obp/slg/ops/era/whip) as STRINGS
# (e.g. ".313", "3.45") -- not numbers. Everything in RATE_FIELDS gets cast
# to float on the way in so every file this pipeline writes has clean,
# consistent numeric types. inningsPitched stays a string ("6.1" = 6 1/3
# innings, NOT 6.1 decimal) since it's parsed with ip_to_outs() downstream.
HITTING_COUNT_FIELDS = [
    "atBats", "hits", "doubles", "triples", "homeRuns", "rbi",
    "baseOnBalls", "hitByPitch", "sacFlies", "stolenBases", "caughtStealing", "strikeOuts",
]
HITTING_RATE_FIELDS = ["avg", "obp", "slg", "ops"]
HITTING_FIELDS = HITTING_COUNT_FIELDS + HITTING_RATE_FIELDS

PITCHING_COUNT_FIELDS = [
    "strikeOuts", "baseOnBalls", "hits", "atBats", "homeRuns",
    "earnedRuns", "runs", "wins", "losses", "saves",
]
PITCHING_RATE_FIELDS = ["era", "whip", "avg"]
PITCHING_FIELDS = ["inningsPitched"] + PITCHING_COUNT_FIELDS + PITCHING_RATE_FIELDS


def _clean_row(stat, fields, count_fields, rate_fields):
    row = {}
    for fld in fields:
        raw = stat.get(fld)
        if fld in rate_fields:
            try:
                row[fld] = float(raw) if raw not in (None, "-", "") else 0.0
            except (TypeError, ValueError):
                row[fld] = 0.0
        elif fld in count_fields:
            try:
                row[fld] = int(raw) if raw not in (None, "-", "") else 0
            except (TypeError, ValueError):
                row[fld] = 0
        else:
            row[fld] = raw  # inningsPitched: leave as-is, parsed downstream
    return row


def load_excluded():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "excluded_players.json")
    with open(config_path) as f:
        raw = json.load(f)
    return {k for k in raw.keys() if not k.startswith("_")}


def fetch_team_week(affiliate_key, cfg, start_date, end_date, excluded):
    roster = get_active_roster(cfg["team_id"], SEASON)
    hitters, pitchers = [], []
    for entry in roster:
        person = entry["person"]
        pid = str(person["id"])
        if pid in excluded:
            continue
        name = person["fullName"]

        h_stat = get_player_stats_by_date_range(
            pid, "hitting", cfg["sport_id"], SEASON, start_date, end_date
        )
        if h_stat and int(h_stat.get("atBats", 0) or 0) > 0:
            row = {"id": pid, "name": name, "team": affiliate_key}
            row.update(_clean_row(h_stat, HITTING_FIELDS, HITTING_COUNT_FIELDS, HITTING_RATE_FIELDS))
            hitters.append(row)

        p_stat = get_player_stats_by_date_range(
            pid, "pitching", cfg["sport_id"], SEASON, start_date, end_date
        )
        ip_raw = p_stat.get("inningsPitched") if p_stat else None
        if p_stat and ip_raw and str(ip_raw) not in ("0", "0.0"):
            row = {"id": pid, "name": name, "team": affiliate_key}
            row.update(_clean_row(p_stat, PITCHING_FIELDS, PITCHING_COUNT_FIELDS, PITCHING_RATE_FIELDS))
            pitchers.append(row)

    return hitters, pitchers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="YYYY-MM-DD, defaults to 6 days before --end")
    parser.add_argument("--end", help="YYYY-MM-DD, defaults to yesterday")
    args = parser.parse_args()

    end_date = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start_date = date.fromisoformat(args.start) if args.start else end_date - timedelta(days=6)

    excluded = load_excluded()
    print(f"Loaded {len(excluded)} excluded (non-prospect) player IDs.")

    all_hitters, all_pitchers = [], []
    for key, cfg in AFFILIATES.items():
        print(f"Fetching {cfg['display_name']} ({start_date} to {end_date})...")
        hitters, pitchers = fetch_team_week(
            key, cfg, start_date.isoformat(), end_date.isoformat(), excluded
        )
        print(f"  {len(hitters)} hitters, {len(pitchers)} pitchers with activity.")
        all_hitters.extend(hitters)
        all_pitchers.extend(pitchers)

    all_hitters = dedupe_by_id(all_hitters, "hitter")
    all_pitchers = dedupe_by_id(all_pitchers, "pitcher")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, OUTPUT_DIR)
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"week_{end_date.isoformat()}.json")
    with open(out_path, "w") as f:
        json.dump({
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "hitters": all_hitters,
            "pitchers": all_pitchers,
        }, f, indent=2)

    print(f"\nSaved {len(all_hitters)} hitters and {len(all_pitchers)} pitchers to {out_path}")
    print("Next: run verify_data.py on this file BEFORE trusting the leaderboard.")


if __name__ == "__main__":
    main()
