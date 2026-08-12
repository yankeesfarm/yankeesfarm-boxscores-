#!/usr/bin/env python3
"""
YankeesFarm analytics feed
---------------------------
Pulls live season stats for every Yankees affiliate from the MLB Stats API
and writes data.json in the shape analytics.html expects.

Run this from an environment that can reach statsapi.mlb.com (e.g. your
existing yankeesfarm-boxscores GitHub Action) — NOT from a machine that
blocks that domain.

Usage:
    python fetch_farm_stats.py --season 2026 --out data.json

Requires: requests  (pip install requests)
"""

import argparse
import json
import time
import requests

BASE = "https://statsapi.mlb.com/api/v1"
YANKEES_ORG_ID = 147

# sportId -> our internal level code + display label
SPORT_LEVELS = {
    11: {"id": "AAA", "lvl": "AAA"},
    12: {"id": "AA",  "lvl": "AA"},
    13: {"id": "A+",  "lvl": "A+"},
    14: {"id": "A",   "lvl": "A"},
    16: {"id": "ROK", "lvl": "ROK"},   # covers FCL + DSL — split further below
}

HIT_FIELDS = ["avg", "obp", "slg", "ops", "homeRuns", "rbi", "stolenBases",
              "baseOnBalls", "strikeOuts", "plateAppearances", "atBats"]
PIT_FIELDS = ["wins", "losses", "era", "whip", "inningsPitched",
              "strikeOuts", "baseOnBalls", "battersFaced"]


def get(url, params=None):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def find_affiliate_teams(season):
    """Return list of {teamId, name, level_id} for every Yankees affiliate."""
    teams = []
    for sport_id, level in SPORT_LEVELS.items():
        data = get(f"{BASE}/teams", params={"sportId": sport_id, "season": season})
        for t in data.get("teams", []):
            if t.get("parentOrgId") == YANKEES_ORG_ID:
                # DSL Yankees org sometimes fields two DSL clubs (e.g. "DSL Yankees",
                # "DSL Bombers") both tagged sportId 16 with "DSL" in the league name —
                # split those out from the Rookie/FCL squad using the league name.
                level_id = level["id"]
                league_name = (t.get("league") or {}).get("name", "")
                if sport_id == 16 and "Dominican" in league_name:
                    # distinguish multiple DSL clubs by team name
                    level_id = "DSL2" if "bomber" in t["name"].lower() else "DSL1"
                teams.append({"teamId": t["id"], "name": t["name"], "level_id": level_id})
    return teams


def player_season_stats(person_id, season, group):
    data = get(f"{BASE}/people/{person_id}/stats",
               params={"stats": "season", "group": group, "season": season})
    for block in data.get("stats", []):
        for split in block.get("splits", []):
            return split.get("stat", {})
    return {}


def pct(n, d):
    return round(100 * n / d, 1) if d else 0.0


def build_hitter_row(person, stat, level_id):
    pa = stat.get("plateAppearances") or 0
    bb = stat.get("baseOnBalls") or 0
    so = stat.get("strikeOuts") or 0
    avg = float(stat.get("avg") or 0)
    slg = float(stat.get("slg") or 0)
    return {
        "name": person["fullName"],
        "pos": (person.get("primaryPosition") or {}).get("abbreviation", ""),
        "level": level_id,
        "avg": avg,
        "obp": float(stat.get("obp") or 0),
        "slg": slg,
        "ops": float(stat.get("ops") or 0),
        "hr": stat.get("homeRuns", 0),
        "rbi": stat.get("rbi", 0),
        "sb": stat.get("stolenBases", 0),
        "bbp": pct(bb, pa),
        "kp": pct(so, pa),
        "iso": round(slg - avg, 3),
        # Statcast fields — only populated for levels with tracking (see
        # enrich_with_statcast below). None means "not tracked at this level",
        # and the page renders that as a dash rather than a fabricated number.
        "maxev": None,
        "barrelp": None,
        "gbp": None,
        "fbp": None,
    }


STATCAST_LEVELS = {"AAA"}  # levels where Baseball Savant has park tracking


def enrich_with_statcast(hitters, season):
    """
    Best-effort: Baseball Savant (baseballsavant.mlb.com) has an undocumented
    CSV leaderboard endpoint that covers Triple-A parks with Statcast
    installed. It is NOT the same as statsapi.mlb.com and has no guaranteed
    schema, so treat this as optional and let it fail quietly per player.

    If your network can't reach baseballsavant.mlb.com, or a given player
    has too few batted-ball events tracked, the Statcast fields just stay
    None and the page shows a dash for that row — that's expected, not a bug.
    """
    import csv
    import io

    targets = [h for h in hitters if h["level"] in STATCAST_LEVELS]
    if not targets:
        return

    print(f"Attempting Statcast enrichment for {len(targets)} Triple-A hitters...")
    url = "https://baseballsavant.mlb.com/leaderboard/statcast"
    params = {
        "type": "batter",
        "year": season,
        "position": "",
        "team": "",
        "min": 1,
        "csv": "true",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:
        print(f"  Statcast fetch failed ({e}); leaving fields blank.")
        return

    by_name = {}
    for row in rows:
        full_name = f"{row.get('first_name','').strip()} {row.get('last_name','').strip()}".strip()
        by_name[full_name.lower()] = row

    for h in targets:
        row = by_name.get(h["name"].lower())
        if not row:
            continue
        try:
            h["maxev"] = round(float(row["max_hit_speed"]), 1) if row.get("max_hit_speed") else None
            h["barrelp"] = round(float(row["brl_percent"]), 1) if row.get("brl_percent") else None
            h["gbp"] = round(float(row["groundballs_percent"]), 1) if row.get("groundballs_percent") else None
            h["fbp"] = round(float(row["flyballs_percent"]), 1) if row.get("flyballs_percent") else None
        except (KeyError, ValueError):
            pass  # leave as None rather than guess at a differently-named column


def build_pitcher_row(person, stat, level_id):
    bf = stat.get("battersFaced") or 0
    so = stat.get("strikeOuts") or 0
    bb = stat.get("baseOnBalls") or 0
    return {
        "name": person["fullName"],
        "pos": (person.get("primaryPosition") or {}).get("abbreviation", "P"),
        "level": level_id,
        "w": stat.get("wins", 0),
        "l": stat.get("losses", 0),
        "era": float(stat.get("era") or 0),
        "whip": float(stat.get("whip") or 0),
        "ip": float(stat.get("inningsPitched") or 0),
        "k9": float(stat.get("strikeoutsPer9Inn") or 0),
        "bb9": float(stat.get("walksPer9Inn") or 0),
        "kp": pct(so, bf),
        "bbp": pct(bb, bf),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--out", default="data.json")
    args = ap.parse_args()

    print(f"Finding Yankees affiliate teams for {args.season}...")
    teams = find_affiliate_teams(args.season)
    for t in teams:
        print(f"  {t['level_id']:5s} {t['name']} (teamId={t['teamId']})")

    hitters, pitchers = [], []

    for t in teams:
        print(f"Fetching roster for {t['name']}...")
        roster = get(f"{BASE}/teams/{t['teamId']}/roster",
                      params={"rosterType": "active", "season": args.season})
        for entry in roster.get("roster", []):
            person = entry["person"]
            pos_type = (entry.get("position") or {}).get("type", "")
            is_pitcher = pos_type == "Pitcher"
            group = "pitching" if is_pitcher else "hitting"

            stat = player_season_stats(person["id"], args.season, group)
            if not stat:
                continue  # no stats logged yet (e.g. IL, just signed)

            if is_pitcher:
                pitchers.append(build_pitcher_row(person, stat, t["level_id"]))
            else:
                hitters.append(build_hitter_row(person, stat, t["level_id"]))

            time.sleep(0.05)  # be polite to the API

    enrich_with_statcast(hitters, args.season)

    payload = {
        "season": args.season,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hitters": hitters,
        "pitchers": pitchers,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\nWrote {len(hitters)} hitters and {len(pitchers)} pitchers to {args.out}")


if __name__ == "__main__":
    main()
