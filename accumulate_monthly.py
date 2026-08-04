#!/usr/bin/env python3
"""
Merge every weekly snapshot whose end_date falls in the given month into
month-to-date cumulative totals.

CRITICAL ACCURACY POINT: rate stats (AVG/OBP/SLG/OPS/ERA/WHIP/AVG-against/
SO9) are ALWAYS recomputed here from summed counting stats (hits, at-bats,
innings, strikeouts, etc.) -- never averaged week-to-week. Averaging four
weekly batting averages together is NOT the same number as (total hits /
total at-bats), and the difference compounds the more unevenly playing time
is distributed across the month. This script only ever sums counting stats,
then derives rates once at the end, so the monthly number is a mathematically
exact rollup of the underlying games -- not an approximation of one.

Usage:
    python accumulate_monthly.py --month 2026-07
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.dedupe import dedupe_by_id

WEEKLY_DIR = "data/weekly"
MONTHLY_DIR = "data/monthly"

HITTING_COUNT_FIELDS = [
    "atBats", "hits", "doubles", "triples", "homeRuns", "rbi",
    "baseOnBalls", "hitByPitch", "sacFlies", "stolenBases", "caughtStealing", "strikeOuts",
]
PITCHING_COUNT_FIELDS = [
    "strikeOuts", "baseOnBalls", "hits", "atBats", "homeRuns", "earnedRuns",
    "runs", "wins", "losses", "saves",
]


def ip_to_outs(ip_val):
    """Convert MLB's '87.2' innings notation (the .2 means 2/3 of an inning,
    NOT .2 decimal innings) into whole outs, so it can be summed safely."""
    if ip_val is None:
        return 0
    whole, _, frac = str(ip_val).partition(".")
    whole = int(whole) if whole else 0
    frac = int(frac) if frac else 0
    return whole * 3 + frac


def outs_to_ip(outs):
    return f"{outs // 3}.{outs % 3}"


def load_weeks_in_month(script_dir, year_month):
    files = sorted(glob.glob(os.path.join(script_dir, WEEKLY_DIR, "week_*.json")))
    weeks = []
    for path in files:
        with open(path) as f:
            data = json.load(f)
        if data["end_date"].startswith(year_month):
            weeks.append(data)
    return weeks


def accumulate_hitters(weeks):
    totals = {}
    for wk in weeks:
        # defensive dedupe -- protects against a duplicate slipping through
        # even if fetch_weekly_stats.py's own dedupe didn't catch it (e.g.
        # a hand-edited or manually backfilled weekly file)
        week_hitters = dedupe_by_id(wk["hitters"], f"hitter ({wk['end_date']})")
        for row in week_hitters:
            pid = row["id"]
            if pid not in totals:
                totals[pid] = {"id": pid, "name": row["name"], "team": row["team"]}
                for f in HITTING_COUNT_FIELDS:
                    totals[pid][f] = 0
            for f in HITTING_COUNT_FIELDS:
                totals[pid][f] += int(row.get(f) or 0)
            totals[pid]["team"] = row["team"]  # most recently seen team wins

    for t in totals.values():
        ab = t["atBats"]
        h = t["hits"]
        bb = t["baseOnBalls"]
        hbp = t["hitByPitch"]
        sf = t["sacFlies"]
        doubles, triples, hr = t["doubles"], t["triples"], t["homeRuns"]
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr

        t["avg"] = round(h / ab, 3) if ab else 0.0
        pa_ob = ab + bb + hbp + sf
        t["obp"] = round((h + bb + hbp) / pa_ob, 3) if pa_ob else 0.0
        t["slg"] = round(tb / ab, 3) if ab else 0.0
        t["ops"] = round(t["obp"] + t["slg"], 3)
    return list(totals.values())


def accumulate_pitchers(weeks):
    totals = {}
    for wk in weeks:
        week_pitchers = dedupe_by_id(wk["pitchers"], f"pitcher ({wk['end_date']})")
        for row in week_pitchers:
            pid = row["id"]
            if pid not in totals:
                totals[pid] = {"id": pid, "name": row["name"], "team": row["team"], "outs": 0}
                for f in PITCHING_COUNT_FIELDS:
                    totals[pid][f] = 0
            totals[pid]["outs"] += ip_to_outs(row.get("inningsPitched"))
            for f in PITCHING_COUNT_FIELDS:
                totals[pid][f] += int(row.get(f) or 0)
            totals[pid]["team"] = row["team"]

    for t in totals.values():
        outs = t["outs"]
        ip_decimal = outs / 3
        t["inningsPitched"] = outs_to_ip(outs)
        t["era"] = round((t["earnedRuns"] * 9) / ip_decimal, 2) if ip_decimal else 0.0
        t["whip"] = round((t["hits"] + t["baseOnBalls"]) / ip_decimal, 2) if ip_decimal else 0.0
        t["so9"] = round((t["strikeOuts"] * 9) / ip_decimal, 2) if ip_decimal else 0.0
        t["avg"] = round(t["hits"] / t["atBats"], 3) if t["atBats"] else 0.0
    return list(totals.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True, help="YYYY-MM")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    weeks = load_weeks_in_month(script_dir, args.month)
    if not weeks:
        print(f"No weekly snapshots found for {args.month} yet -- run fetch_weekly_stats.py first.")
        return

    hitters = accumulate_hitters(weeks)
    pitchers = accumulate_pitchers(weeks)

    output_dir = os.path.join(script_dir, MONTHLY_DIR)
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{args.month}.json")
    with open(out_path, "w") as f:
        json.dump({
            "month": args.month,
            "weeks_included": [w["end_date"] for w in weeks],
            "hitters": hitters,
            "pitchers": pitchers,
        }, f, indent=2)

    print(f"Accumulated {len(weeks)} weekly snapshot(s) -> {out_path}")
    print(f"Weeks included: {[w['end_date'] for w in weeks]}")
    print("Double-check that list covers every week of the month with no gaps "
          "before treating this as the final monthly total.")


if __name__ == "__main__":
    main()
