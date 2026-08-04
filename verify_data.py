#!/usr/bin/env python3
"""
Sanity checks on a weekly snapshot BEFORE you post anything derived from it.

These checks catch the most common pipeline failure modes -- a missed game,
a mid-week promotion silently attributed to the wrong team, or a stat pull
that quietly returned nothing for a player who should have data. They do
NOT guarantee the numbers are perfect. Treat a clean run of this script as
"safe to generate a draft," not "safe to post without looking at it."

Usage:
    python verify_data.py --file data/weekly/week_2026-08-03.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.affiliates import AFFILIATES
from lib.mlb_api import get_team_schedule


def check_game_counts(data):
    print("--- Game count check ---")
    for key, cfg in AFFILIATES.items():
        try:
            games = get_team_schedule(cfg["team_id"], data["start_date"], data["end_date"])
            print(f"  {cfg['display_name']}: {len(games)} completed games "
                  f"({data['start_date']} to {data['end_date']})")
        except Exception as exc:
            print(f"  {cfg['display_name']}: COULD NOT VERIFY schedule -- {exc}")
    print("  -> Cross-check these counts against the real schedule "
          "(milb.com/<team>/schedule) before trusting stats below them. "
          "A team playing 6 games but showing stat totals that look like "
          "3 games' worth is your signal something was missed.")


def check_duplicates(data):
    print("\n--- Duplicate player check (promotions mid-week) ---")
    for label, rows in [("hitters", data["hitters"]), ("pitchers", data["pitchers"])]:
        ids = [r["id"] for r in rows]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            print(f"  WARNING: {label} has {len(dupes)} duplicate player ID(s) "
                  f"(likely a mid-week promotion/demotion -- stats were "
                  f"identical from both team lookups and only the first "
                  f"encountered 'team' label was kept): {dupes}")
        else:
            print(f"  No duplicate {label} entries.")


def check_zero_rows(data):
    print("\n--- Empty-stat check ---")
    zero_hitters = [r for r in data["hitters"] if not r.get("atBats")]
    zero_pitchers = [r for r in data["pitchers"] if not r.get("inningsPitched")]
    if zero_hitters or zero_pitchers:
        print(f"  WARNING: {len(zero_hitters)} hitter row(s) and "
              f"{len(zero_pitchers)} pitcher row(s) have zero recorded "
              f"activity -- these should have been filtered upstream. "
              f"Investigate before publishing.")
        for r in zero_hitters[:5]:
            print(f"    hitter: {r['name']} ({r['team']})")
        for r in zero_pitchers[:5]:
            print(f"    pitcher: {r['name']} ({r['team']})")
    else:
        print("  No zero-activity rows found.")


def check_team_coverage(data):
    print("\n--- Team coverage check ---")
    seen_teams = {r["team"] for r in data["hitters"]} | {r["team"] for r in data["pitchers"]}
    missing = set(AFFILIATES.keys()) - seen_teams
    if missing:
        print(f"  WARNING: no hitters or pitchers at all came back for: {sorted(missing)}. "
              f"That almost certainly means a fetch failure for that team, "
              f"not that literally nobody played -- investigate before trusting this file.")
    else:
        print("  All four affiliates have at least some data in this snapshot.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    args = parser.parse_args()

    with open(args.file) as f:
        data = json.load(f)

    check_game_counts(data)
    check_duplicates(data)
    check_zero_rows(data)
    check_team_coverage(data)

    print("\nThese are automated sniff tests, not a guarantee of accuracy. "
          "Before posting monthly totals to Instagram, manually spot-check "
          "your top 2-3 names in each category against MiLB.com directly.")


if __name__ == "__main__":
    main()
