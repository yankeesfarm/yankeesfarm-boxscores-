#!/usr/bin/env python3
"""
YankeesFarm analytics feed
---------------------------
Pulls live season stats for every Yankees affiliate from the MLB Stats API
and writes data.json in the shape analytics.html expects.

v4: REWRITTEN to fix a real bug -- a promoted/demoted player's stats only
ever reflected his CURRENT level, not his true combined season line (e.g.
a player who hit 18 HR at Hudson Valley before being promoted to Somerset
would show only his post-promotion Somerset home runs). v2/v3's approach
fetched each team's CURRENT active roster with a season-type stat hydrate
scoped to that team's own sportId -- a player no longer on a team's roster
was invisible to that team's fetch entirely, so there was nothing for any
downstream code to combine.

The fix: stop reinventing roster/stat fetching here and reuse the
EXACT proven logic already powering fetch_season_stats.py's season
leaderboard pipeline (lib/mlb_api.py), which is known-correct --
confirmed against real promoted players showing accurate combined season
totals on the live site:
  - get_active_roster() uses rosterType="fullSeason", which includes every
    player who was EVER on a team's roster this season, including players
    later promoted/demoted away. A promoted player therefore appears on
    BOTH his old and new team's roster pull.
  - get_player_stats_by_date_range() fetches the player's full game log
    for the season and filters to the requested team + date range IN CODE
    (never trusting the MLB API's own byDateRange/teamId filters, which
    silently failed in two earlier production attempts).

This script now calls those same two functions once per affiliate team,
and manually SUMS a player's raw counting stats across every team stint
he had this season (mirroring generate_leaderboard.py's combine_by_id()
principle, just done inline here since analytics.html has no separate
combine step downstream). A player's displayed "level" is his HIGHEST
level reached this season (standard prospect-media convention), not
necessarily his very last game -- see LEVEL_RANK below.

Bio fields (age, height/weight, bats/throws, hometown, origin, birthDate)
are no longer available "for free" from a hydrated roster response, since
get_active_roster() doesn't hydrate bio data. These are now fetched in a
single batched call to /people?personIds=... after all stats are combined,
chunked to keep URLs a reasonable length.

Run this from an environment that can reach statsapi.mlb.com (e.g. your
existing yankeesfarm-boxscores GitHub Action) -- NOT from a machine that
blocks that domain.

Usage:
    python fetch_farm_stats.py --season 2026 --out data.json

Requires: requests  (pip install requests)
"""

import argparse
import json
import os
import sys
import time
from datetime import date

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.mlb_api import get_active_roster, get_player_stats_by_date_range, get_team_schedule
from advanced_stats import (
    calc_xbh_rate,
    calc_babip,
    calc_woba,
    calc_fip_constant,
    calc_fip,
)

BASE = "https://statsapi.mlb.com/api/v1"
YANKEES_ORG_ID = 147

# Fallback FIP constant, same reasoning as the rest of the pipeline: used
# only when a level's own qualifying pitcher pool is too small to compute
# a real constant.
FALLBACK_FIP_CONSTANT = 3.10

# sportId -> our internal level code
SPORT_LEVELS = {
    11: "AAA",
    12: "AA",
    13: "A+",
    14: "A",
    16: "ROK",   # covers FCL + DSL -- split further below
}

# Used to pick a promoted/demoted player's DISPLAY level -- his highest
# level reached this season, standard prospect-media convention. DSL1/DSL2
# and ROK are treated as the same tier (both true rookie ball), so a player
# who spent time at both isn't arbitrarily ranked one above the other.
LEVEL_RANK = {"DSL1": 0, "DSL2": 0, "ROK": 1, "A": 2, "A+": 3, "AA": 4, "AAA": 5}

# Earliest plausible date to search from when hunting for a team's actual
# Opening Day -- same reasoning as fetch_season_stats.py's SEARCH_FROM.
SEARCH_FROM_MONTH_DAY = "-02-01"


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


def find_opening_day_and_games(team_id, sport_id, search_from, end_date):
    """Same approach as fetch_season_stats.py: ask the Stats API for this
    team's real schedule rather than guessing a generic season-start date,
    since levels start on different dates in a given year. Also returns
    the full completed-games list (not just dates) so compute_team_record()
    can derive a real W-L record from the same schedule call, instead of
    fetching it twice."""
    games = get_team_schedule(team_id, search_from, end_date, sport_id=sport_id)
    if not games:
        return None, []
    dates = [g["gameDate"][:10] for g in games if g.get("gameDate")]
    return (min(dates) if dates else None), games


def compute_team_record(games, team_id):
    """Derives W-L record and win% directly from this team's own completed
    schedule (the same schedule data get_team_schedule() already proven
    reliable for elsewhere in this pipeline), rather than a separate
    standings API call.

    NOTE: this intentionally does NOT compute "place in division" --
    that needs every other team in the division's record too, and the
    MLB Stats API's dedicated standings endpoint needs its MiLB
    league-ID mapping verified against a real response before trusting it
    (same class of issue the promoted-player cross-level bug was -- this
    environment can't reach statsapi.mlb.com to verify that shape).
    Flagged as a known follow-up rather than guessed at."""
    wins = losses = 0
    for g in games:
        teams = g.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})
        home_id = (home.get("team") or {}).get("id")
        away_id = (away.get("team") or {}).get("id")
        home_score = home.get("score")
        away_score = away.get("score")
        if home_score is None or away_score is None:
            continue
        if team_id == home_id:
            won = home_score > away_score
        elif team_id == away_id:
            won = away_score > home_score
        else:
            continue
        if won:
            wins += 1
        else:
            losses += 1
    total = wins + losses
    win_pct = round(wins / total, 3) if total else 0.0
    return {"wins": wins, "losses": losses, "winPct": win_pct}


def fetch_team_aggregate_stats(team_id, sport_id, season):
    """Team-wide hitting/pitching totals via the Stats API's own
    team-level stats endpoint -- NOT summed from individual player rows,
    since MLB tracks team totals directly and that avoids any risk of
    double-counting/undercounting from a promoted player's stats being
    split across two teams' rosters (the exact class of bug the
    cross-level hitter/pitcher combining fix elsewhere in this file deals
    with -- team-level totals don't have that problem at all, since
    they're the team's own tracked aggregate, not a sum of roster rows)."""
    hitting = get(f"{BASE}/teams/{team_id}/stats",
                   params={"stats": "season", "group": "hitting", "season": season, "sportId": sport_id})
    pitching = get(f"{BASE}/teams/{team_id}/stats",
                    params={"stats": "season", "group": "pitching", "season": season, "sportId": sport_id})

    def first_split_stat(payload):
        for block in payload.get("stats", []):
            splits = block.get("splits", [])
            if splits:
                return splits[0].get("stat", {})
        return {}

    h = first_split_stat(hitting)
    p = first_split_stat(pitching)
    return {
        "avg": float(h.get("avg") or 0),
        "obp": float(h.get("obp") or 0),
        "slg": float(h.get("slg") or 0),
        "ops": float(h.get("ops") or 0),
        "runs": h.get("runs", 0),
        "homeRuns": h.get("homeRuns", 0),
        "stolenBases": h.get("stolenBases", 0),
        "era": float(p.get("era") or 0),
        "whip": float(p.get("whip") or 0),
        "strikeOuts": p.get("strikeOuts", 0),
        "saves": p.get("saves", 0),
    }


def pct(n, d):
    return round(100 * n / d, 1) if d else 0.0


def _accumulate(bucket, stat, is_pitcher):
    """Sums one team-stint's already-summed stat dict into a player's
    running combined total across every team he's played for this season.
    inningsPitched needs special handling -- summed as whole outs, not as
    a float, since '.1'/'.2' represent thirds of an inning, not decimal
    tenths."""
    for k, v in stat.items():
        if k == "inningsPitched":
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            bucket[k] = bucket.get(k, 0) + v
    if is_pitcher:
        ip_str = stat.get("inningsPitched", "0.0")
        whole, _, frac = str(ip_str).partition(".")
        outs = (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)
        bucket["_outs"] = bucket.get("_outs", 0) + outs


def fetch_bios(person_ids):
    """Single batched call per chunk to /people?personIds=... for full bio
    data (birth info, draft year, height/weight, bats/throws) -- no longer
    available "for free" from a hydrated roster response, since
    get_active_roster() doesn't hydrate bio data the way the old
    fetch_hydrated_roster() did."""
    bios = {}
    ids = list(person_ids)
    chunk_size = 50
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        data = get(f"{BASE}/people", params={"personIds": ",".join(str(x) for x in chunk)})
        for person in data.get("people", []):
            bios[person["id"]] = person
    return bios


def bio_fields(person):
    """Bio fields read defensively (.get with a fallback) so a missing
    field just shows blank on the profile page rather than crashing the
    whole run."""
    if not person:
        return {"age": None, "heightWeight": None, "bats": None, "throws": None,
                "hometown": None, "origin": None, "birthDate": None}

    birth_city = person.get("birthCity")
    birth_state = person.get("birthStateProvince")
    birth_country = person.get("birthCountry")
    hometown_parts = [p for p in (birth_city, birth_state) if p]
    hometown = ", ".join(hometown_parts)
    if birth_country and birth_country not in ("USA",):
        hometown = f"{hometown}, {birth_country}" if hometown else birth_country

    draft_year = person.get("draftYear")
    origin = f"Draft ({draft_year})" if draft_year else "International Free Agent (IFA)"

    height = person.get("height")
    weight = person.get("weight")
    if height and weight:
        height_weight = f"{height} / {weight} lbs"
    elif height:
        height_weight = height
    elif weight:
        height_weight = f"{weight} lbs"
    else:
        height_weight = None

    return {
        "age": person.get("currentAge"),
        "heightWeight": height_weight,
        "bats": (person.get("batSide") or {}).get("description"),
        "throws": (person.get("pitchHand") or {}).get("description"),
        "hometown": hometown or None,
        "origin": origin,
        "birthDate": person.get("birthDate"),
    }


def finalize_hitter(pid, name, pos, level_id, totals, bio_person):
    ab = totals.get("atBats", 0)
    h = totals.get("hits", 0)
    doubles = totals.get("doubles", 0)
    triples = totals.get("triples", 0)
    hr = totals.get("homeRuns", 0)
    bb = totals.get("baseOnBalls", 0)
    hbp = totals.get("hitByPitch", 0)
    sf = totals.get("sacFlies", 0)
    so = totals.get("strikeOuts", 0)
    sb = totals.get("stolenBases", 0)
    rbi = totals.get("rbi", 0)
    pa = totals.get("plateAppearances", 0)
    ibb = totals.get("intentionalWalks", 0)

    singles = h - doubles - triples - hr
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    avg = round(h / ab, 3) if ab else 0.0
    obp_denom = ab + bb + hbp + sf
    # The raw "plateAppearances" field as fetched/summed from the MLB
    # Stats API is NOT reliably present -- this was confirmed live on the
    # season-leaderboard pipeline (identical data source): BB%/K%/K-BB%
    # came back completely empty while XBH%/ISO/BABIP/wOBA (which don't
    # depend on plateAppearances) worked fine. obp_denom (AB+BB+HBP+SF) is
    # the same value plate appearances should equal and is already
    # reliably computed here from raw counting stats, so use it instead of
    # trusting whatever combine_by_id()/totals summed from the raw fetch.
    if obp_denom:
        pa = obp_denom
    obp = round((h + bb + hbp) / obp_denom, 3) if obp_denom else 0.0
    slg = round(tb / ab, 3) if ab else 0.0
    ops = round(obp + slg, 3)
    bbp = pct(bb, pa)
    kp = pct(so, pa)

    adv_input = {
        "atBats": ab, "hits": h, "doubles": doubles, "triples": triples, "homeRuns": hr,
        "baseOnBalls": bb, "hitByPitch": hbp, "sacFlies": sf, "strikeOuts": so,
        "plateAppearances": pa, "intentionalWalks": ibb, "avg": avg, "slg": slg,
    }
    xbh_pct = calc_xbh_rate(adv_input)
    babip = calc_babip(adv_input)
    woba = calc_woba(adv_input)

    return {
        "name": name,
        "mlbId": pid,
        "pos": pos,
        "level": level_id,
        "avg": avg,
        "obp": obp,
        "slg": slg,
        "ops": ops,
        "hr": hr,
        "rbi": rbi,
        "sb": sb,
        "bbp": bbp,
        "kp": kp,
        "kbb": round(kp - bbp, 1),
        "iso": round(slg - avg, 3),
        "xbhp": round(xbh_pct * 100, 1) if xbh_pct is not None else None,
        "babip": babip,
        "woba": woba,
        "maxev": None,
        "barrelp": None,
        "gbp": None,
        "fbp": None,
        **bio_fields(bio_person),
    }


def finalize_pitcher(pid, name, pos, level_id, totals):
    wins = totals.get("wins", 0)
    losses = totals.get("losses", 0)
    earned_runs = totals.get("earnedRuns", 0)
    hits = totals.get("hits", 0)
    bb = totals.get("baseOnBalls", 0)
    so = totals.get("strikeOuts", 0)
    bf = totals.get("battersFaced", 0)
    hr = totals.get("homeRuns", 0)
    hbp = totals.get("hitByPitch", 0)
    outs = totals.get("_outs", 0)
    true_ip = outs / 3 if outs else 0.0

    # Same fallback as finalize_hitter()'s plateAppearances fix: the raw
    # "battersFaced" field isn't reliably present from the API fetch,
    # which would otherwise silently zero out pitcher K%/BB%/K-BB%. Falls
    # back to the standard estimate -- outs recorded + hits + walks + HBP
    # -- only when the raw fetched value is missing/zero.
    if not bf:
        bf = outs + hits + bb + hbp

    era = round(9 * earned_runs / true_ip, 2) if true_ip else 0.0
    whip = round((bb + hits) / true_ip, 2) if true_ip else 0.0
    k9 = round(9 * so / true_ip, 1) if true_ip else 0.0
    bb9 = round(9 * bb / true_ip, 1) if true_ip else 0.0
    kp = pct(so, bf)
    bbp = pct(bb, bf)

    return {
        "name": name,
        "mlbId": pid,
        "pos": pos,
        "level": level_id,
        "w": wins,
        "l": losses,
        "era": era,
        "whip": whip,
        "ip": float(f"{outs // 3}.{outs % 3}") if outs else 0.0,
        "k9": k9,
        "bb9": bb9,
        "kp": kp,
        "bbp": bbp,
        "kbb": round(kp - bbp, 1),
        "fip": None,
        "_homeRuns": hr,
        "_hitByPitch": hbp,
        "_outs": outs,
        "_strikeOuts": so,
        "_baseOnBalls": bb,
    }


def apply_fip_by_level(pitchers):
    by_level = {}
    for row in pitchers:
        by_level.setdefault(row["level"], []).append(row)

    for level, rows in by_level.items():
        fip_inputs = []
        for row in rows:
            outs = row["_outs"]
            fip_inputs.append({
                "homeRuns": row["_homeRuns"],
                "baseOnBalls": row["_baseOnBalls"],
                "hitByPitch": row["_hitByPitch"],
                "strikeOuts": row["_strikeOuts"],
                "inningsPitched": f"{outs // 3}.{outs % 3}",
            })

        era_values = [r["era"] for r in rows if r.get("era")]
        league_era = sum(era_values) / len(era_values) if era_values else None
        fip_constant = calc_fip_constant(fip_inputs, league_era) if league_era else None
        if fip_constant is None:
            fip_constant = FALLBACK_FIP_CONSTANT
            print(f"  NOTE: using fallback FIP constant ({FALLBACK_FIP_CONSTANT}) for "
                  f"level '{level}' -- could not compute a real per-level constant.")

        for row, fip_input in zip(rows, fip_inputs):
            row["fip"] = calc_fip(fip_input, fip_constant)

    for row in pitchers:
        for key in ("_homeRuns", "_hitByPitch", "_outs", "_strikeOuts", "_baseOnBalls"):
            row.pop(key, None)

    return pitchers


STATCAST_LEVELS = {"AAA", "A"}  # Tampa Tarpons play at Steinbrenner Field, which has Statcast installed


def enrich_with_statcast(hitters, season):
    import csv
    import io

    targets = [h for h in hitters if h["level"] in STATCAST_LEVELS]
    if not targets:
        return

    print(f"Attempting Statcast enrichment for {len(targets)} hitters...")
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

    season = args.season
    end_date = date.today().isoformat()
    search_from = f"{season}{SEARCH_FROM_MONTH_DAY}"

    print(f"Finding Yankees affiliate teams for {season}...")
    teams = find_affiliate_teams(season)
    for t in teams:
        print(f"  {t['level_id']:5s} {t['name']} (teamId={t['teamId']})")

    hitting_totals = {}
    pitching_totals = {}
    person_names = {}
    person_pos = {}
    person_levels_touched = {}
    team_rows = []

    for t in teams:
        print(f"Finding Opening Day for {t['name']}...")
        opening_day, season_games = find_opening_day_and_games(t["teamId"], t["sportId"], search_from, end_date)
        if not opening_day:
            print(f"  WARNING: could not find any completed games for {t['name']}. Skipping.")
            continue
        print(f"  Opening Day: {opening_day}. Pulling full-season roster...")

        record = compute_team_record(season_games, t["teamId"])
        team_agg = fetch_team_aggregate_stats(t["teamId"], t["sportId"], season)
        team_rows.append({
            "teamId": t["teamId"],
            "name": t["name"],
            "level": t["level_id"],
            **record,
            **team_agg,
        })

        roster = get_active_roster(t["teamId"], season)  # rosterType=fullSeason
        for entry in roster:
            person = entry["person"]
            pid = person["id"]
            pos_type = (entry.get("position") or {}).get("type", "")
            is_pitcher = pos_type == "Pitcher"
            pos_abbrev = (entry.get("position") or {}).get("abbreviation", "P" if is_pitcher else "")

            group = "pitching" if is_pitcher else "hitting"
            stat = get_player_stats_by_date_range(
                pid, group, t["sportId"], season, opening_day, end_date, team_id=t["teamId"]
            )
            if not stat:
                continue

            person_names[pid] = person.get("fullName")
            person_pos[pid] = pos_abbrev
            person_levels_touched.setdefault(pid, []).append(t["level_id"])

            target = pitching_totals if is_pitcher else hitting_totals
            bucket = target.setdefault(pid, {})
            _accumulate(bucket, stat, is_pitcher)

        time.sleep(0.1)

    def display_level(pid):
        levels = person_levels_touched.get(pid, [])
        if not levels:
            return "ROK"
        return max(levels, key=lambda lv: LEVEL_RANK.get(lv, 0))

    all_person_ids = set(hitting_totals) | set(pitching_totals)
    print(f"Fetching bios for {len(all_person_ids)} players...")
    bios = fetch_bios(all_person_ids)

    hitters = [
        finalize_hitter(pid, person_names[pid], person_pos.get(pid, ""), display_level(pid),
                         totals, bios.get(pid))
        for pid, totals in hitting_totals.items()
    ]
    pitchers = [
        finalize_pitcher(pid, person_names[pid], person_pos.get(pid, "P"), display_level(pid), totals)
        for pid, totals in pitching_totals.items()
    ]

    for row in pitchers:
        row.update(bio_fields(bios.get(row["mlbId"])))

    apply_fip_by_level(pitchers)
    enrich_with_statcast(hitters, season)

    payload = {
        "season": season,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hitters": hitters,
        "pitchers": pitchers,
        "teams": team_rows,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\nWrote {len(hitters)} hitters and {len(pitchers)} pitchers to {args.out}")


if __name__ == "__main__":
    main()
