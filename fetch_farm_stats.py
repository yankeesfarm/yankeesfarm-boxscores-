#!/usr/bin/env python3
"""
YankeesFarm analytics feed
---------------------------
Pulls live season stats for every Yankees affiliate from the MLB Stats API
and writes data.json in the shape analytics.html expects.

v2: uses the "hydrate" parameter to attach each player's season stats
directly onto the roster response, instead of making a separate
/people/{id}/stats call per roster spot. This cuts the request count from
roughly one-per-player (150-200 calls) down to one-per-team (7 calls),
confirmed by inspecting how bronxpinstripes.com structures its own
roster+stats payload.

Run this from an environment that can reach statsapi.mlb.com (e.g. your
existing yankeesfarm-boxscores GitHub Action) -- NOT from a machine that
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

# sportId -> our internal level code
SPORT_LEVELS = {
    11: "AAA",
    12: "AA",
    13: "A+",
    14: "A",
    16: "ROK",   # covers FCL + DSL -- split further below
}


def get(url, params=None):
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def find_affiliate_teams(season):
    """Return list of {teamId, name, level_id, sportId} for every Yankees affiliate."""
    teams = []
    for sport_id, level_id in SPORT_LEVELS.items():
        data = get(f"{BASE}/teams", params={"sportId": sport_id, "season": season})
        for t in data.get("teams", []):
            if t.get("parentOrgId") == YANKEES_ORG_ID:
                # DSL Yankees org sometimes fields two DSL clubs (e.g. "DSL Yankees",
                # "DSL Bombers") both tagged sportId 16 with "Dominican" in the league
                # name -- split those out from the Rookie/FCL squad.
                this_level = level_id
                league_name = (t.get("league") or {}).get("name", "")
                if sport_id == 16 and "Dominican" in league_name:
                    this_level = "DSL2" if "bomber" in t["name"].lower() else "DSL1"
                teams.append({
                    "teamId": t["id"],
                    "name": t["name"],
                    "level_id": this_level,
                    "sportId": sport_id,
                })
    return teams


def fetch_hydrated_roster(team_id, season, sport_id):
    """
    One call per team: roster entries come back with each player's season
    hitting AND pitching stat blocks already attached via hydrate.

    sportId must be included INSIDE the hydrate stats sub-query, not just
    as a top-level param -- without it, the stats hydration defaults to
    Major League stats, silently returning nothing for anyone without MLB
    time. This is the same underlying issue as the original per-player
    /people/{id}/stats bug, just relocated into the hydrate string.
    """
    params = {
        "rosterType": "active",
        "hydrate": f"person(stats(type=season,group=[hitting,pitching],season={season},sportId={sport_id}))",
    }
    data = get(f"{BASE}/teams/{team_id}/roster", params=params)
    return data.get("roster", [])


def extract_stat_group(person, group_name):
    """
    Pull the season stat line for 'hitting' or 'pitching' out of a hydrated
    person object. If a player was traded/promoted mid-season, the stats
    endpoint returns multiple splits (one aggregate + one per team) -- the
    aggregate split is the one WITHOUT a 'team' key, so prefer that; fall
    back to the first split if no clean aggregate is present.
    """
    for block in person.get("stats", []):
        if block.get("group", {}).get("displayName") == group_name:
            splits = block.get("splits", [])
            if not splits:
                return None
            aggregate = next((s for s in splits if "team" not in s), None)
            chosen = aggregate or splits[0]
            return chosen.get("stat")
    return None


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
        "mlbId": person["id"],
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
        # Statcast fields -- not available from this endpoint; left null
        # rather than fabricated. See enrich_with_statcast() below.
        "maxev": None,
        "barrelp": None,
        "gbp": None,
        "fbp": None,
    }


def build_pitcher_row(person, stat, level_id):
    bf = stat.get("battersFaced") or 0
    so = stat.get("strikeOuts") or 0
    bb = stat.get("baseOnBalls") or 0
    return {
        "name": person["fullName"],
        "mlbId": person["id"],
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


STATCAST_LEVELS = {"AAA", "A"}  # Tampa Tarpons play at Steinbrenner Field, which has Statcast installed


def enrich_with_statcast(hitters, season):
    """
    Best-effort: Baseball Savant has an undocumented CSV leaderboard endpoint
    that covers Triple-A parks with Statcast installed. Not the same system
    as statsapi.mlb.com, no guaranteed schema -- treat as optional, fail
    quietly per player rather than raising.
    """
    import csv
    import io

    targets = [h for h in hitters if h["level"] in STATCAST_LEVELS]
    if not targets:
        return

    print(f"Attempting Statcast enrichment for {len(targets)} Triple-A hitters...")
    url = "https://baseballsavant.mlb.com/leaderboard/statcast"
    params = {"type": "batter", "year": season, "position": "", "team": "", "min": 1, "csv": "true"}
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
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--out", default="data.json")
    args = parser.parse_args()

    print(f"Finding Yankees affiliate teams for {args.season}...")
    teams = find_affiliate_teams(args.season)
    for t in teams:
        print(f"  {t['level_id']:5s} {t['name']} (teamId={t['teamId']})")

    hitters, pitchers = [], []

    for t in teams:
        print(f"Fetching hydrated roster for {t['name']}...")
        roster = fetch_hydrated_roster(t["teamId"], args.season, t["sportId"])
        for entry in roster:
            person = entry["person"]
            pos_type = (entry.get("position") or {}).get("type", "")
            is_pitcher = pos_type == "Pitcher"

            group = "pitching" if is_pitcher else "hitting"
            stat = extract_stat_group(person, group)
            if not stat:
                continue  # no stats logged yet this season

            if is_pitcher:
                pitchers.append(build_pitcher_row(person, stat, t["level_id"]))
            else:
                hitters.append(build_hitter_row(person, stat, t["level_id"]))

        time.sleep(0.1)  # one pause per team now, not per player

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
