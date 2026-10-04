#!/usr/bin/env python3
"""
Push prospect profile data (career seasons, splits, monthly trends) to the
YankeesFarm Wix site's updatePlayerStats endpoint.

This is the MISSING AUTOMATION PIECE between the existing leaderboard
pipeline and the individual prospect profile pages. It:

  1. Reads a prospect_registry.json mapping slug → personId + type
  2. For each player, fetches from the MLB Stats API:
     - yearByYear career stats (all seasons, all teams)
     - vsPlatoon splits for the current season
     - gameLog for current season → aggregated into monthly trends
  3. Formats data into the JSON shapes prospect.html's loadLiveStats expects
  4. POSTs to updatePlayerStats for each slug

Usage:
    python push_prospect_profiles.py
    python push_prospect_profiles.py --slug jasson-dominguez   # single player
    python push_prospect_profiles.py --dry-run                  # preview, don't push

Requires:
    - PROSPECT_PUSH_KEY env var (matches Wix secret "daily-stats-push-key")
    - PROSPECT_PUSH_ENDPOINT env var (e.g. https://www.yankeesfarmreport.com/_functions/updatePlayerStats)
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.mlb_api import _get, get_player_splits_by_hand

import requests

SEASON = 2026

# Level code mapping: MLB Stats API sport names → prospect.html level codes
SPORT_TO_LEVEL = {
    11: "AAA",
    12: "AA",
    13: "HIA",
    14: "LOA",
    16: "FCL",  # Default for rookies; refined by team below
}

# Team ID → level code + display label (for stints at specific teams)
TEAM_LABELS = {
    531: ("AAA", "SWB RailRiders (Triple-A)"),
    1956: ("AA", "Somerset Patriots (Double-A)"),
    537: ("HIA", "Hudson Valley Renegades (High-A)"),
    587: ("LOA", "Tampa Tarpons (Low-A)"),
    475: ("FCL", "FCL Yankees (Rookie)"),
    635: ("DSLY", "DSL Yankees (Rookie)"),
    634: ("DSLB", "DSL Bombers (Rookie)"),
}

HITTING_COUNT_FIELDS = [
    "gamesPlayed", "atBats", "runs", "hits", "doubles", "triples",
    "homeRuns", "rbi", "baseOnBalls", "strikeOuts", "stolenBases",
    "caughtStealing", "hitByPitch", "sacFlies", "plateAppearances",
]

PITCHING_COUNT_FIELDS = [
    "gamesPlayed", "gamesStarted", "wins", "losses", "saves",
    "hits", "earnedRuns", "baseOnBalls", "strikeOuts",
    "homeRuns", "hitByPitch",
]


def innings_to_outs(ip_str):
    """Convert '72.1' format innings to total outs (217)."""
    whole, _, frac = str(ip_str).partition(".")
    return (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)


def outs_to_innings_str(outs):
    """Convert total outs (217) back to '72.1' display format."""
    return f"{outs // 3}.{outs % 3}"


def get_year_by_year(person_id, group):
    """Fetch a player's career stats broken down by year and team.
    group: 'hitting' or 'pitching'."""
    data = _get(f"/people/{person_id}/stats", {
        "stats": "yearByYear",
        "group": group,
        "season": SEASON,
    })
    stats_list = data.get("stats", [])
    if not stats_list:
        return []
    return stats_list[0].get("splits", [])


def get_game_log(person_id, group, season):
    """Fetch a player's full game log for a season."""
    data = _get(f"/people/{person_id}/stats", {
        "stats": "gameLog",
        "group": group,
        "season": season,
    })
    stats_list = data.get("stats", [])
    if not stats_list:
        return []
    return stats_list[0].get("splits", [])


def build_seasons_hitter(splits):
    """Convert yearByYear splits into the seasons array format
    that prospect.html expects."""
    by_year = defaultdict(list)
    for split in splits:
        year = split.get("season")
        stat = split.get("stat", {})
        team_info = split.get("team", {})
        team_id = team_info.get("id")
        sport_id = (split.get("sport") or {}).get("id")

        if not year or not stat:
            continue
        # Skip MLB stats (sport_id 1) — only MiLB
        if sport_id == 1:
            continue

        ab = int(stat.get("atBats", 0) or 0)
        if ab == 0:
            continue

        level_code, level_label = TEAM_LABELS.get(team_id, (
            SPORT_TO_LEVEL.get(sport_id, "UNK"),
            f"{team_info.get('name', 'Unknown')} ({split.get('league', {}).get('name', '')})"
        ))

        h = int(stat.get("hits", 0) or 0)
        bb = int(stat.get("baseOnBalls", 0) or 0)
        hbp = int(stat.get("hitByPitch", 0) or 0)
        sf = int(stat.get("sacFlies", 0) or 0)
        doubles = int(stat.get("doubles", 0) or 0)
        triples = int(stat.get("triples", 0) or 0)
        hr = int(stat.get("homeRuns", 0) or 0)
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        pa = ab + bb + hbp + sf

        avg = round(h / ab, 3) if ab else 0
        obp = round((h + bb + hbp) / pa, 3) if pa else 0
        slg = round(tb / ab, 3) if ab else 0
        ops = round(obp + slg, 3)

        stint = {
            "level": level_code,
            "levelLabel": level_label,
            "G": int(stat.get("gamesPlayed", 0) or 0),
            "AB": ab,
            "R": int(stat.get("runs", 0) or 0),
            "H": h,
            "D": doubles,
            "T": triples,
            "HR": hr,
            "RBI": int(stat.get("rbi", 0) or 0),
            "BB": bb,
            "SO": int(stat.get("strikeOuts", 0) or 0),
            "SB": int(stat.get("stolenBases", 0) or 0),
            "AVG": avg,
            "OBP": obp,
            "SLG": slg,
            "OPS": ops,
            "WRC": None,
        }
        by_year[year].append(stint)

    seasons = []
    for year in sorted(by_year.keys()):
        seasons.append({"year": int(year), "stints": by_year[year]})
    return seasons


def build_seasons_pitcher(splits):
    """Convert yearByYear splits into the seasons array for pitchers."""
    by_year = defaultdict(list)
    for split in splits:
        year = split.get("season")
        stat = split.get("stat", {})
        team_info = split.get("team", {})
        team_id = team_info.get("id")
        sport_id = (split.get("sport") or {}).get("id")

        if not year or not stat:
            continue
        if sport_id == 1:
            continue

        ip_raw = stat.get("inningsPitched", "0")
        ip_outs = innings_to_outs(ip_raw)
        if ip_outs == 0:
            continue

        level_code, level_label = TEAM_LABELS.get(team_id, (
            SPORT_TO_LEVEL.get(sport_id, "UNK"),
            f"{team_info.get('name', 'Unknown')} ({split.get('league', {}).get('name', '')})"
        ))

        ip_decimal = ip_outs / 3
        h = int(stat.get("hits", 0) or 0)
        er = int(stat.get("earnedRuns", 0) or 0)
        bb = int(stat.get("baseOnBalls", 0) or 0)
        so = int(stat.get("strikeOuts", 0) or 0)

        era = round((er * 9) / ip_decimal, 2) if ip_decimal else 0
        whip = round((h + bb) / ip_decimal, 2) if ip_decimal else 0

        stint = {
            "level": level_code,
            "levelLabel": level_label,
            "G": int(stat.get("gamesPlayed", 0) or 0),
            "GS": int(stat.get("gamesStarted", 0) or 0),
            "W": int(stat.get("wins", 0) or 0),
            "L": int(stat.get("losses", 0) or 0),
            "SV": int(stat.get("saves", 0) or 0),
            "IP": float(ip_raw),
            "H": h,
            "ER": er,
            "BB": bb,
            "SO": so,
            "ERA": era,
            "WHIP": whip,
        }
        by_year[year].append(stint)

    seasons = []
    for year in sorted(by_year.keys()):
        seasons.append({"year": int(year), "stints": by_year[year]})
    return seasons


def build_splits_hitter(person_id, sport_ids):
    """Fetch and format vs-RHP / vs-LHP splits for a hitter.
    Queries each sport_id the player may have played at."""
    combined = {}
    for sid in sport_ids:
        raw = get_player_splits_by_hand(person_id, "hitting", sid, SEASON)
        for code in ("vr", "vl"):
            if code in raw and code not in combined:
                combined[code] = raw[code]
            elif code in raw:
                # Merge counting stats across levels
                for k, v in raw[code].items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        combined[code][k] = combined[code].get(k, 0) + v

    result = {}
    for code, key in [("vr", "vsRHP"), ("vl", "vsLHP")]:
        if code not in combined:
            continue
        s = combined[code]
        ab = int(s.get("atBats", 0) or 0)
        h = int(s.get("hits", 0) or 0)
        result[key] = [{
            "level": "2026",
            "AB": ab,
            "H": h,
            "D": int(s.get("doubles", 0) or 0),
            "HR": int(s.get("homeRuns", 0) or 0),
            "BB": int(s.get("baseOnBalls", 0) or 0),
            "SO": int(s.get("strikeOuts", 0) or 0),
            "AVG": round(h / ab, 3) if ab else 0,
        }]
    return result


def build_splits_pitcher(person_id, sport_ids):
    """Fetch and format vs-LHB / vs-RHB splits for a pitcher."""
    combined = {}
    for sid in sport_ids:
        raw = get_player_splits_by_hand(person_id, "pitching", sid, SEASON)
        for code in ("vr", "vl"):
            if code in raw and code not in combined:
                combined[code] = raw[code]
            elif code in raw:
                for k, v in raw[code].items():
                    if k == "inningsPitched":
                        existing_outs = innings_to_outs(combined[code].get(k, "0"))
                        new_outs = innings_to_outs(v)
                        combined[code][k] = outs_to_innings_str(existing_outs + new_outs)
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        combined[code][k] = combined[code].get(k, 0) + v

    result = {}
    # For pitchers: vl = vs Left-Handed Batters, vr = vs Right-Handed Batters
    for code, key in [("vl", "vsLHB"), ("vr", "vsRHB")]:
        if code not in combined:
            continue
        s = combined[code]
        ip_raw = s.get("inningsPitched", "0")
        ip_outs = innings_to_outs(ip_raw)
        ab = int(s.get("atBats", 0) or 0)
        h = int(s.get("hits", 0) or 0)
        result[key] = [{
            "level": "2026",
            "IP": float(ip_raw),
            "H": h,
            "HR": int(s.get("homeRuns", 0) or 0),
            "HB": int(s.get("hitByPitch", 0) or 0),
            "BB": int(s.get("baseOnBalls", 0) or 0),
            "SO": int(s.get("strikeOuts", 0) or 0),
            "AVG": round(h / ab, 3) if ab else 0,
        }]
    return result


def build_monthly_trend_hitter(person_id):
    """Build monthly trend from game log for current season."""
    games = get_game_log(person_id, "hitting", SEASON)
    by_month = defaultdict(lambda: defaultdict(int))
    for g in games:
        game_date = g.get("date", "")[:10]
        if not game_date:
            continue
        month_key = game_date[5:7]  # "06" etc
        stat = g.get("stat", {})
        for field in ["atBats", "hits", "doubles", "triples", "homeRuns",
                       "stolenBases", "baseOnBalls", "strikeOuts",
                       "hitByPitch", "sacFlies"]:
            by_month[month_key][field] += int(stat.get(field, 0) or 0)

    MONTH_NAMES = {"03": "MAR", "04": "APR", "05": "MAY", "06": "JUN",
                   "07": "JUL", "08": "AUG", "09": "SEP", "10": "OCT"}
    trend = []
    for m in sorted(by_month.keys()):
        d = by_month[m]
        ab = d["atBats"]
        if ab == 0:
            continue
        pa = ab + d["baseOnBalls"] + d["hitByPitch"] + d["sacFlies"]
        trend.append({
            "month": MONTH_NAMES.get(m, m),
            "avg": round(d["hits"] / ab, 3) if ab else 0,
            "doubles": d["doubles"],
            "hr": d["homeRuns"],
            "sb": d["stolenBases"],
            "bb_pct": round((d["baseOnBalls"] / pa) * 100, 1) if pa else 0,
            "k_pct": round((d["strikeOuts"] / pa) * 100, 1) if pa else 0,
        })
    return trend


def build_monthly_trend_pitcher(person_id):
    """Build monthly trend from game log for current season."""
    games = get_game_log(person_id, "pitching", SEASON)
    by_month = defaultdict(lambda: {"ip_outs": 0, **{k: 0 for k in
        ["hits", "earnedRuns", "baseOnBalls", "strikeOuts"]}})
    for g in games:
        game_date = g.get("date", "")[:10]
        if not game_date:
            continue
        month_key = game_date[5:7]
        stat = g.get("stat", {})
        ip_raw = stat.get("inningsPitched", "0")
        by_month[month_key]["ip_outs"] += innings_to_outs(ip_raw)
        for field in ["hits", "earnedRuns", "baseOnBalls", "strikeOuts"]:
            by_month[month_key][field] += int(stat.get(field, 0) or 0)

    MONTH_NAMES = {"03": "MAR", "04": "APR", "05": "MAY", "06": "JUN",
                   "07": "JUL", "08": "AUG", "09": "SEP", "10": "OCT"}
    trend = []
    for m in sorted(by_month.keys()):
        d = by_month[m]
        ip_outs = d["ip_outs"]
        if ip_outs == 0:
            continue
        ip_dec = ip_outs / 3
        trend.append({
            "month": MONTH_NAMES.get(m, m),
            "era": round((d["earnedRuns"] * 9) / ip_dec, 2),
            "whip": round((d["hits"] + d["baseOnBalls"]) / ip_dec, 2),
            "k9": round((d["strikeOuts"] * 9) / ip_dec, 2),
            "bb9": round((d["baseOnBalls"] * 9) / ip_dec, 2),
        })
    return trend


def push_to_wix(endpoint, key, slug, seasons, splits, monthly_trend):
    """POST to updatePlayerStats."""
    payload = {
        "key": key,
        "slug": slug,
        "seasons": seasons,
        "splits": splits,
        "monthlyTrend": monthly_trend,
    }
    try:
        resp = requests.post(endpoint, json=payload, timeout=20)
        if resp.status_code == 200:
            return True
        print(f"    PUSH FAILED ({resp.status_code}): {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"    PUSH ERROR: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Process only this one player slug")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and format but don't push")
    parser.add_argument("--registry", default="config/prospect_registry.json",
                        help="Path to slug→personId mapping file")
    args = parser.parse_args()

    endpoint = os.environ.get("PROSPECT_PUSH_ENDPOINT")
    push_key = os.environ.get("PROSPECT_PUSH_KEY")
    if not args.dry_run and (not endpoint or not push_key):
        print("ERROR: PROSPECT_PUSH_ENDPOINT and PROSPECT_PUSH_KEY env vars required (or use --dry-run)")
        sys.exit(1)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    reg_path = os.path.join(script_dir, args.registry)
    with open(reg_path) as f:
        registry = json.load(f)

    # All sport IDs for split queries (covers all levels)
    all_sport_ids = [11, 12, 13, 14, 16]

    players = registry.items()
    if args.slug:
        if args.slug not in registry:
            print(f"ERROR: {args.slug} not in registry")
            sys.exit(1)
        players = [(args.slug, registry[args.slug])]

    total = len(list(players))
    success, skipped, failed = 0, 0, 0

    # Re-iterate since we consumed the generator
    players = [(args.slug, registry[args.slug])] if args.slug else list(registry.items())

    for i, (slug, info) in enumerate(players, 1):
        pid = info.get("personId")
        ptype = info.get("type")

        if not pid:
            print(f"[{i}/{total}] {slug}: SKIP (no personId)")
            skipped += 1
            continue

        print(f"[{i}/{total}] {slug} (pid={pid}, {ptype})...")

        try:
            # 1. Career seasons
            if ptype == "hitter":
                raw_splits = get_year_by_year(pid, "hitting")
                seasons = build_seasons_hitter(raw_splits)
            else:
                raw_splits = get_year_by_year(pid, "pitching")
                seasons = build_seasons_pitcher(raw_splits)

            # 2. Splits (current season)
            if ptype == "hitter":
                splits = build_splits_hitter(pid, all_sport_ids)
            else:
                splits = build_splits_pitcher(pid, all_sport_ids)

            # 3. Monthly trend (current season)
            if ptype == "hitter":
                monthly = build_monthly_trend_hitter(pid)
            else:
                monthly = build_monthly_trend_pitcher(pid)

            print(f"    {len(seasons)} seasons, {len(splits)} split sides, {len(monthly)} months")

            if args.dry_run:
                # Save to local file for inspection
                out_dir = os.path.join(script_dir, "data", "profiles")
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, f"{slug}.json"), "w") as f:
                    json.dump({"slug": slug, "seasons": seasons, "splits": splits, "monthlyTrend": monthly}, f, indent=2)
                success += 1
            else:
                ok = push_to_wix(endpoint, push_key, slug, seasons, splits, monthly)
                if ok:
                    success += 1
                else:
                    failed += 1

            # Rate limiting: 0.5s between players
            time.sleep(0.5)

        except Exception as e:
            print(f"    ERROR: {e}")
            failed += 1

    print(f"\nDone: {success} pushed, {skipped} skipped (no personId), {failed} failed")


if __name__ == "__main__":
    main()
