#!/usr/bin/env python3
"""
Pull TRUE season-to-date stats for all four affiliates, starting from each
team's actual Opening Day -- not from summing weekly snapshot files.

Why this exists: accumulate_monthly.py only ever sums the weekly JSON files
this pipeline itself has produced. Since the pipeline didn't start running
until August, that approach could only ever reflect "the season since we
started tracking it," not the real season back to Opening Day in
late March/early April. This script closes that gap by asking the MLB Stats
API directly for each player's full-season numbers, the same way a single
"season stats" page on MiLB.com would show them.

Opening Day is NOT hardcoded here. Each affiliate plays in a different level
(AAA/AA/High-A/Single-A) and those levels start on different dates in a given
year -- so this script asks the Stats API for each team's own schedule and
uses the date of that team's actual first completed game as the start of its
season window. That's the one number here I was not willing to guess or pull
from a news article -- it comes from the same authoritative source as
everything else in this pipeline.

Usage:
    python fetch_season_stats.py
    python fetch_season_stats.py --end 2026-08-10   # re-run for a past cutoff
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.affiliates import AFFILIATES, ROOKIE_AFFILIATES
from lib.mlb_api import get_active_roster, get_player_stats_by_date_range, get_team_schedule

SEASON = 2026
OUTPUT_DIR = "data/monthly"

# Earliest plausible date to search from when hunting for a team's actual
# Opening Day. Deliberately well before any real MiLB season starts, so a
# team's first completed game found in this window is genuinely game 1 --
# not an artifact of picking a search window that happens to clip the start
# of the season.
SEARCH_FROM = f"{SEASON}-02-01"

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
            row[fld] = raw
    return row


def recompute_hitting_rates(row):
    """Recompute avg/obp/slg/ops from the raw counting stats rather than
    trusting whatever the Stats API's own byDateRange rate-stat fields say.
    This matches accumulate_monthly.py's own rule (rate stats are always
    derived, never trusted pre-aggregated) -- and it turns out that rule
    matters here too: for an unusually wide, season-spanning date range,
    the API's own precomputed rate fields came back wrong (values like
    .054 for a full-season average), while the underlying counting stats
    (hits, at-bats, etc.) are simple sums and much more trustworthy."""
    ab = row["atBats"]
    h = row["hits"]
    bb = row["baseOnBalls"]
    hbp = row["hitByPitch"]
    sf = row["sacFlies"]
    doubles, triples, hr = row["doubles"], row["triples"], row["homeRuns"]
    singles = h - doubles - triples - hr
    tb = singles + 2 * doubles + 3 * triples + 4 * hr

    row["avg"] = round(h / ab, 3) if ab else 0.0
    pa_ob = ab + bb + hbp + sf
    row["obp"] = round((h + bb + hbp) / pa_ob, 3) if pa_ob else 0.0
    row["slg"] = round(tb / ab, 3) if ab else 0.0
    row["ops"] = round(row["obp"] + row["slg"], 3)
    return row


def recompute_pitching_rates(row, ip_outs):
    """Same principle as recompute_hitting_rates() -- never trust the API's
    own era/whip/so9/avg-against for a wide date range, always derive from
    the raw counting stats and innings-as-outs."""
    ip_decimal = ip_outs / 3
    row["era"] = round((row["earnedRuns"] * 9) / ip_decimal, 2) if ip_decimal else 0.0
    row["whip"] = round((row["hits"] + row["baseOnBalls"]) / ip_decimal, 2) if ip_decimal else 0.0
    row["so9"] = round((row["strikeOuts"] * 9) / ip_decimal, 2) if ip_decimal else 0.0
    row["avg"] = round(row["hits"] / row["atBats"], 3) if row["atBats"] else 0.0
    return row


def load_traded_away():
    """Players who have left the organization entirely via trade -- see
    config/traded_away_players.json for the full explanation. Excluded at
    every roster iteration in main(), for both the four full-season
    affiliates and the three rookie-level ones, so a traded player never
    appears on any leaderboard regardless of level."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "traded_away_players.json")
    with open(config_path) as f:
        raw = json.load(f)
    return {k for k in raw.keys() if not k.startswith("_")}


def find_opening_day(team_id, sport_id, end_date):
    """The date of this team's first completed game of the season, PLUS how
    many completed games it's played total -- found by asking the Stats
    API for its real schedule, not assumed from a generic 'MiLB season
    starts around late March' rule of thumb, since that varies by level and
    we have a source of truth available.

    The games-played count returned here is used downstream to compute the
    real "qualified for a rate title" threshold (3.1 PA / team game for
    hitters, 1 IP / team game for pitchers -- the same rule MLB itself
    uses), rather than a flat guessed minimum.

    Queries this team's own single sport_id rather than all levels at once
    -- MLB's API rejects date ranges over 45 days (this search spans
    several months) when multiple sportIds are requested together."""
    games = get_team_schedule(team_id, SEARCH_FROM, end_date, sport_id=sport_id)
    if not games:
        return None, 0
    dates = [g["gameDate"][:10] for g in games if g.get("gameDate")]
    return (min(dates) if dates else None), len(games)


def fetch_team_season(affiliate_key, cfg, end_date, traded_away):
    opening_day, games_played = find_opening_day(cfg["team_id"], cfg["sport_id"], end_date)
    if not opening_day:
        print(f"  WARNING: could not find any completed games for "
              f"{cfg['display_name']} between {SEARCH_FROM} and {end_date}. "
              f"Skipping this team rather than guessing a start date.")
        return [], [], None, 0

    roster = get_active_roster(cfg["team_id"], SEASON)
    hitters, pitchers = [], []
    for entry in roster:
        person = entry["person"]
        pid = str(person["id"])
        if pid in traded_away:
            continue
        name = person["fullName"]

        h_stat = get_player_stats_by_date_range(
            pid, "hitting", cfg["sport_id"], SEASON, opening_day, end_date
        )
        if h_stat and int(h_stat.get("atBats", 0) or 0) > 0:
            row = {"id": pid, "name": name, "team": affiliate_key}
            row.update(_clean_row(h_stat, HITTING_FIELDS, HITTING_COUNT_FIELDS, HITTING_RATE_FIELDS))
            row = recompute_hitting_rates(row)
            hitters.append(row)

        p_stat = get_player_stats_by_date_range(
            pid, "pitching", cfg["sport_id"], SEASON, opening_day, end_date
        )
        ip_raw = p_stat.get("inningsPitched") if p_stat else None
        if p_stat and ip_raw and str(ip_raw) not in ("0", "0.0"):
            row = {"id": pid, "name": name, "team": affiliate_key}
            row.update(_clean_row(p_stat, PITCHING_FIELDS, PITCHING_COUNT_FIELDS, PITCHING_RATE_FIELDS))
            whole, _, frac = str(ip_raw).partition(".")
            ip_outs = (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)
            row = recompute_pitching_rates(row, ip_outs)
            pitchers.append(row)

    return hitters, pitchers, opening_day, games_played


def fetch_rookie_team_season_hitting_only(affiliate_key, cfg, end_date, traded_away):
    """Same idea as fetch_team_season(), but hitting stats only for the
    DSL/FCL rookie-level affiliates -- these hitters only ever feed the six
    counting-stat categories (see ROOKIE_LEVEL_INCLUDED_FIELDS in
    generate_leaderboard.py), so there's no reason to fetch pitching stats
    here, or to skip a team just because its own Opening Day search failed
    independently of the four main affiliates' searches."""
    opening_day, _games_played = find_opening_day(cfg["team_id"], cfg["sport_id"], end_date)
    if not opening_day:
        print(f"  WARNING: could not find any completed games for "
              f"{cfg['display_name']} between {SEARCH_FROM} and {end_date}. "
              f"Skipping this team rather than guessing a start date.")
        return [], None

    roster = get_active_roster(cfg["team_id"], SEASON)
    hitters = []
    for entry in roster:
        person = entry["person"]
        pid = str(person["id"])
        if pid in traded_away:
            continue
        name = person["fullName"]

        h_stat = get_player_stats_by_date_range(
            pid, "hitting", cfg["sport_id"], SEASON, opening_day, end_date
        )
        if h_stat and int(h_stat.get("atBats", 0) or 0) > 0:
            row = {"id": pid, "name": name, "team": affiliate_key}
            row.update(_clean_row(h_stat, HITTING_FIELDS, HITTING_COUNT_FIELDS, HITTING_RATE_FIELDS))
            row = recompute_hitting_rates(row)
            hitters.append(row)

    return hitters, opening_day


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", help="YYYY-MM-DD, defaults to yesterday")
    args = parser.parse_args()

    end_date = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    end_date_str = end_date.isoformat()

    traded_away = load_traded_away()
    print(f"Loaded {len(traded_away)} traded-away player IDs to exclude entirely.")

    all_hitters, all_pitchers = [], []
    opening_days = {}
    team_games_played = {}
    any_team_failed = False

    for key, cfg in AFFILIATES.items():
        print(f"Finding real Opening Day for {cfg['display_name']}...")
        hitters, pitchers, opening_day, games_played = fetch_team_season(key, cfg, end_date_str, traded_away)
        if opening_day is None:
            any_team_failed = True
            continue
        print(f"  Opening Day: {opening_day}. Pulling {opening_day} -> {end_date_str}...")
        print(f"  {games_played} completed games played. {len(hitters)} hitters, "
              f"{len(pitchers)} pitchers with season activity.")
        opening_days[key] = opening_day
        team_games_played[key] = games_played
        all_hitters.extend(hitters)
        all_pitchers.extend(pitchers)

    # "Qualified" for a rate-stat title (AVG/OBP/SLG/OPS/ERA/WHIP). Hitters
    # use MLB's own literal rule -- 3.1 plate appearances per team game --
    # which works fine here since a regular position player really does
    # appear in most of his team's games.
    #
    # Pitchers do NOT use MLB's literal "1 IP per team game" rule. That
    # standard assumes an MLB workhorse starter throwing ~180-220 innings a
    # year. It does not fit a player-development system that deliberately
    # limits pitching prospects' workloads (six-man rotations, strict
    # innings caps) -- confirmed by checking this org's actual innings
    # totals mid-season: with the top team having played 112 games, literal
    # "1 IP/game" (112 IP) let through exactly ONE pitcher organization-wide,
    # while the #2-through-#10 most-worked pitchers (all legitimate,
    # full-time rotation arms) were sitting at 84-93 innings and got
    # excluded. 0.5 IP per team game is used instead, which comfortably
    # includes every real rotation regular without lowering the bar so far
    # that a September call-up's 8 innings would qualify.
    #
    # Since the four affiliates have played slightly different numbers of
    # games (each started on its own real Opening Day, with its own
    # rainouts/makeups), both thresholds use the MOST games any one of them
    # has played as the season's reference point, so the bar is never
    # stricter than what's actually achievable by a full-season player at
    # any level.
    max_games = max(team_games_played.values()) if team_games_played else 0
    qualifying_pa = round(3.1 * max_games)
    qualifying_ip = round(0.5 * max_games, 1)
    print(f"\nQualifying threshold: {max_games} games (max across affiliates) -> "
          f"{qualifying_pa} PA / {qualifying_ip} IP to qualify for a rate-stat leaderboard.")

    # NOTE: hitters and pitchers intentionally NOT deduped here anymore --
    # a player who played multiple affiliate levels this season shows up
    # once per level, each row representing his real stats AT THAT LEVEL,
    # not a duplicate. generate_leaderboard.py's combine_by_id() sums these
    # correctly at read time, for both hitters and pitchers. See
    # lib/dedupe.py for the full explanation.

    all_rookie_hitters = []
    rookie_opening_days = {}
    for key, cfg in ROOKIE_AFFILIATES.items():
        print(f"Finding real Opening Day for {cfg['display_name']}...")
        rookie_hitters, opening_day = fetch_rookie_team_season_hitting_only(key, cfg, end_date_str, traded_away)
        if opening_day is None:
            continue
        print(f"  Opening Day: {opening_day}. Pulling {opening_day} -> {end_date_str}...")
        print(f"  {len(rookie_hitters)} hitters with season activity.")
        rookie_opening_days[key] = opening_day
        all_rookie_hitters.extend(rookie_hitters)
    opening_days.update(rookie_opening_days)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, OUTPUT_DIR)
    os.makedirs(output_dir, exist_ok=True)

    # Kept as YYYY-MM to match the file naming generate_leaderboard.py and
    # push_to_wix.py already expect -- this is genuinely season-to-date data,
    # not calendar-month data, despite the filename shape.
    month_label = end_date.strftime("%Y-%m")
    out_path = os.path.join(output_dir, f"{month_label}.json")
    with open(out_path, "w") as f:
        json.dump({
            "month": month_label,
            "season_to_date": True,
            "opening_days_used": opening_days,
            "through_date": end_date_str,
            "team_games_played": team_games_played,
            "qualifying_pa": qualifying_pa,
            "qualifying_ip": qualifying_ip,
            "hitters": all_hitters,
            "pitchers": all_pitchers,
            "rookie_hitters": all_rookie_hitters,
        }, f, indent=2)

    print(f"\nSaved {len(all_hitters)} hitters, {len(all_pitchers)} pitchers, and "
          f"{len(all_rookie_hitters)} rookie-level (DSL/FCL) hitters to {out_path}")
    print(f"Opening Days used per team: {opening_days}")
    if any_team_failed:
        print("WARNING: at least one team's Opening Day could not be found -- "
              "that team's players are missing from this file entirely. "
              "Check the warnings above before trusting this as complete.")
        sys.exit(1)
    print("Next: run verify_data.py on this file BEFORE trusting the leaderboard.")


if __name__ == "__main__":
    main()
