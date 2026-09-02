#!/usr/bin/env python3
"""
YankeesFarm Top Performers of the Night
------------------------------------------------------------
Reads the JSON produced by dsl_fcl_boxscores.py for a given date and pulls
out the players who had a standout game, formatted for the nightly
dashboard on yankeesfarmreport.com.

Also computes a single "Player of the Day" (hitter) and "Pitcher of the
Day" from among the players who already qualified, using a points system.

USAGE:
    python3 top_performers.py                # yesterday
    python3 top_performers.py 2026-07-31      # specific date

OUTPUT:
    ./output/YYYY-MM-DD_top_performers.json
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from dsl_fcl_boxscores import get_season_totals, LEVEL_LABELS

HEADSHOT_URL = "https://midfield.mlbstatic.com/v1/people/{id}/spots/120"

MIN_HITTER_HITS   = 2
MIN_HITTER_XBH    = 1

MIN_PITCHER_OUTS      = 1 * 3   # 1.0 IP -- floor, must pitch at least this much
UP_TO_4_INN_OUTS       = 4 * 3   # 4.0 IP -- top of the 1.0-4.0 IP bucket (inclusive)
LONG_OUTING_OUTS       = 5 * 3   # 5.0 IP -- boundary where the "long" bucket begins
SIX_INN_OUTS           = 6 * 3   # 6.0 IP -- splits the "long" bucket in two

MIN_SO_UNDER_5        = 3       # 1.0 up to 4.2 IP: min strikeouts
MAX_RUNS_1_TO_4_INN   = 1       # 1.0-4.0 IP inclusive: max runs allowed
MAX_RUNS_4_1_TO_4_2   = 2       # 4.1-4.2 IP exactly: max runs allowed

MAX_LONG_RUNS       = 2        # 5.0+ IP: max runs allowed (unchanged for both sub-tiers below)
MIN_SO_LONG         = 4        # 5.0-5.2 IP: min strikeouts
MIN_SO_VERY_LONG    = 5        # 6.0+ IP: min strikeouts

MIN_SO_DOMINANT     = 10
MAX_R_DOMINANT      = 4
MAX_BB_DOMINANT     = 4

WALK_TIER_1_INN_OUTS      = 1 * 3
WALK_TIER_1_1_1_2_OUTS    = 1 * 3 + 2
WALK_TIER_2_TO_4_INN_OUTS = 4 * 3
WALK_TIER_4_1_5_2_OUTS    = 5 * 3 + 2

MAX_BB_1_INN        = 1
MAX_BB_1_1_TO_1_2   = 2
MAX_BB_2_TO_4_INN   = 2
MAX_BB_4_1_TO_5_2   = 3
MAX_BB_6_PLUS       = 4


def ip_to_outs(ip_str) -> int:
    """Convert MLB's '6.1' / '6.2' innings-pitched notation to total outs.
    '.1' = 1 out, '.2' = 2 outs (NOT decimal thirds)."""
    if not ip_str:
        return 0
    ip_str = str(ip_str)
    if "." in ip_str:
        whole, frac = ip_str.split(".")
    else:
        whole, frac = ip_str, "0"
    whole = int(whole) if whole else 0
    frac = int(frac) if frac in ("0", "1", "2") else 0
    return whole * 3 + frac


def max_walks_allowed(outs: int) -> int:
    if outs == WALK_TIER_1_INN_OUTS:
        return MAX_BB_1_INN
    if outs <= WALK_TIER_1_1_1_2_OUTS:
        return MAX_BB_1_1_TO_1_2
    if outs <= WALK_TIER_2_TO_4_INN_OUTS:
        return MAX_BB_2_TO_4_INN
    if outs <= WALK_TIER_4_1_5_2_OUTS:
        return MAX_BB_4_1_TO_5_2
    return MAX_BB_6_PLUS


def headshot(player_id: int) -> str:
    return HEADSHOT_URL.format(id=player_id)


def format_hitter_line(row: dict, season: dict) -> str:
    ab = row["ab"]
    h  = row["h"]
    doubles = row.get("doubles", 0)
    triples = row.get("triples", 0)
    hr      = row.get("homeRuns", 0)
    rbi     = row.get("rbi", 0)
    sb      = row.get("stolenBases", 0)

    parts = []
    if doubles:
        parts.append(
            f"Double ({season.get('doubles', '?')})" if doubles == 1
            else f"{doubles} Doubles ({season.get('doubles', '?')})"
        )
    if triples:
        parts.append(
            f"Triple ({season.get('triples', '?')})" if triples == 1
            else f"{triples} Triples ({season.get('triples', '?')})"
        )
    if hr:
        parts.append(
            f"HR ({season.get('homeRuns', '?')})" if hr == 1
            else f"{hr} HR ({season.get('homeRuns', '?')})"
        )
    if rbi:
        parts.append(
            f"RBI ({season.get('rbi', '?')})" if rbi == 1
            else f"{rbi} RBI ({season.get('rbi', '?')})"
        )
    if sb:
        parts.append(
            f"SB ({season.get('stolenBases', '?')})" if sb == 1
            else f"{sb} SB ({season.get('stolenBases', '?')})"
        )

    line = f"{h} for {ab}"
    if parts:
        line += ": " + ", ".join(parts)
    return line


def format_pitcher_line(row: dict) -> str:
    ip = row.get("ip", "0.0")
    r  = row.get("r", 0)
    so = row.get("so", 0)
    return f"{ip} IP, {r} R, {so} K"


def qualifying_hitters(row: dict) -> bool:
    xbh = row.get("doubles", 0) + row.get("triples", 0) + row.get("homeRuns", 0)
    return row.get("h", 0) >= MIN_HITTER_HITS and xbh >= MIN_HITTER_XBH


def qualifying_pitcher(row: dict) -> bool:
    outs = ip_to_outs(row.get("ip"))
    r    = row.get("r", 0)
    so   = row.get("so", 0)
    bb   = row.get("bb", 0)

    if outs < MIN_PITCHER_OUTS:
        return False

    if so >= MIN_SO_DOMINANT and r <= MAX_R_DOMINANT and bb <= MAX_BB_DOMINANT:
        return True

    if bb > max_walks_allowed(outs):
        return False

    if outs >= SIX_INN_OUTS:
        return r <= MAX_LONG_RUNS and so >= MIN_SO_VERY_LONG

    if outs >= LONG_OUTING_OUTS:
        return r <= MAX_LONG_RUNS and so >= MIN_SO_LONG

    if outs <= UP_TO_4_INN_OUTS:
        return r <= MAX_RUNS_1_TO_4_INN and so >= MIN_SO_UNDER_5

    return r <= MAX_RUNS_4_1_TO_4_2 and so >= MIN_SO_UNDER_5


# ---------------------------------------------------------------------------
# Player of the Day / Pitcher of the Day scoring
# ---------------------------------------------------------------------------
# Applied only to players who already qualified for the dashboard above --
# this crowns a winner from that pool, it doesn't change who qualifies.

HIT_PTS       = 1
DOUBLE_PTS    = 2
TRIPLE_PTS    = 3
HR_PTS        = 4
RBI_PTS       = 0.5
SB_PTS        = 0.5
BB_PTS        = 0.25
K_PTS_HITTER  = -0.25

HR_BONUS_2 = 3   # exactly 2 HR
HR_BONUS_3 = 5   # 3+ HR (does not stack with the 2-HR bonus)

RBI_BONUS_3    = 1   # exactly 3 RBI
RBI_BONUS_4    = 2   # exactly 4 RBI (does not stack with the 3-RBI bonus)
RBI_BONUS_EACH_AFTER_4 = 1   # each RBI beyond the 4th, on top of RBI_BONUS_4


def hitter_points(row: dict) -> float:
    h       = row.get("h", 0)
    doubles = row.get("doubles", 0)
    triples = row.get("triples", 0)
    hr      = row.get("homeRuns", 0)
    rbi     = row.get("rbi", 0)
    sb      = row.get("stolenBases", 0)
    bb      = row.get("bb", 0)
    so      = row.get("so", 0)

    points = (
        h * HIT_PTS
        + doubles * DOUBLE_PTS
        + triples * TRIPLE_PTS
        + hr * HR_PTS
        + rbi * RBI_PTS
        + sb * SB_PTS
        + bb * BB_PTS
        + so * K_PTS_HITTER
    )

    if hr >= 3:
        points += HR_BONUS_3
    elif hr == 2:
        points += HR_BONUS_2

    if rbi >= 5:
        points += RBI_BONUS_4 + (rbi - 4) * RBI_BONUS_EACH_AFTER_4
    elif rbi == 4:
        points += RBI_BONUS_4
    elif rbi == 3:
        points += RBI_BONUS_3

    return round(points, 2)


INNING_PTS       = 0.25
HITS_ALLOWED_TIER_5 = 5   # allowing <=3 hits
HITS_ALLOWED_TIER_3 = 3   # allowing 4-5 hits
RUN_PTS          = -1
BB_PTS_PITCHER   = -0.25
K_PTS_PITCHER    = 1

PER_INNING_AFTER_5 = 1

NO_HITTER_9_BONUS  = 10   # 9.0+ IP, 0 hits allowed
SHUTOUT_7_BONUS    = 5    # 7.0+ IP, 0 runs
SHUTOUT_5_BONUS    = 3    # 5.0+ IP, 0 runs -- confirmed 3 points
ER_1_OR_2_BONUS    = 2    # 5.0+ IP, 1-2 earned runs
TEN_K_BONUS        = 3    # 10+ strikeouts (stacks with the above)

RP_1INN_0R_3K_BONUS       = 5   # RP, exactly 1.0 IP, 0 runs, 3+ K
RP_1_1_TO_2_0H_0R_BONUS   = 5   # RP, 1.1-2.0 IP, 0 hits AND 0 runs


def pitcher_points(row: dict) -> float:
    outs = ip_to_outs(row.get("ip"))
    innings = outs / 3.0
    role = row.get("role", "")
    h  = row.get("h", 0)
    r  = row.get("r", 0)
    er = row.get("er", r)  # fall back to r if er isn't present
    bb = row.get("bb", 0)
    so = row.get("so", 0)

    points = (
        innings * INNING_PTS
        + r * RUN_PTS
        + bb * BB_PTS_PITCHER
        + so * K_PTS_PITCHER
    )

    if h <= HITS_ALLOWED_TIER_3:
        points += HITS_ALLOWED_TIER_5
    elif h <= HITS_ALLOWED_TIER_5:
        points += HITS_ALLOWED_TIER_3

    if innings > 5:
        points += (innings - 5) * PER_INNING_AFTER_5

    if innings >= 9 and h == 0:
        points += NO_HITTER_9_BONUS
    elif innings >= 7 and r == 0:
        points += SHUTOUT_7_BONUS
    elif innings >= 5 and r == 0:
        points += SHUTOUT_5_BONUS
    elif innings >= 5 and er in (1, 2):
        points += ER_1_OR_2_BONUS

    if so >= 10:
        points += TEN_K_BONUS

    # Relief-pitcher-specific bonuses. Both are disjoint innings ranges
    # (exactly 1.0 IP vs 1.1-2.0 IP) so there's no overlap to worry about.
    if role == "RP":
        if outs == MIN_PITCHER_OUTS and r == 0 and so >= 3:
            points += RP_1INN_0R_3K_BONUS
        elif MIN_PITCHER_OUTS < outs <= 6 and h == 0 and r == 0:
            points += RP_1_1_TO_2_0H_0R_BONUS

    return round(points, 2)


def build_top_performers(records: list, season: str) -> dict:
    hitters = []
    pitchers = []

    for g in records:
        if g["status"] not in ("Final", "Game Over", "Completed Early"):
            continue

        side = g["yankees_side"]
        team_name = g["home_team"] if side == "home" else g["away_team"]
        batting  = g[f"{side}_batting"]
        pitching = g[f"{side}_pitching"]
        league   = g["league"]
        sport_id = g["sport_id"]

        for row in batting:
            if not qualifying_hitters(row):
                continue
            season_totals = get_season_totals(row["id"], season, sport_id)
            hitters.append({
                "playerId": row["id"],
                "name":     row["name"],
                "level":    league,
                "team":     team_name,
                "position": row.get("position", ""),
                "bio":      f"{league} | {row.get('position', '')}".strip(" |"),
                "photoUrl": headshot(row["id"]),
                "statline": format_hitter_line(row, season_totals),
                "points":   hitter_points(row),
            })

        for row in pitching:
            if not qualifying_pitcher(row):
                continue
            pitchers.append({
                "playerId": row["id"],
                "name":     row["name"],
                "level":    league,
                "team":     team_name,
                "role":     row.get("role", ""),
                "bio":      f"{league} | {row.get('role', '')}".strip(" |"),
                "photoUrl": headshot(row["id"]),
                "statline": format_pitcher_line(row),
                "points":   pitcher_points(row),
            })

    # SPs listed before RPs. Python's sort is stable, so within each
    # group players stay in the order they were found (i.e. game order),
    # only the SP/RP grouping itself is reordered.
    pitchers.sort(key=lambda p: 0 if p["role"] == "SP" else 1)

    player_of_the_day = max(hitters, key=lambda p: p["points"]) if hitters else None
    pitcher_of_the_day = max(pitchers, key=lambda p: p["points"]) if pitchers else None

    return {
        "hitters": hitters,
        "pitchers": pitchers,
        "playerOfTheDay": player_of_the_day,
        "pitcherOfTheDay": pitcher_of_the_day,
    }


def main():
    default_date = (date.today() - timedelta(days=1)).isoformat()
    game_date    = sys.argv[1] if len(sys.argv) > 1 else default_date
    season = game_date.split("-")[0]

    boxscore_path = Path("output") / f"{game_date}_yankees_boxscores.json"
    if not boxscore_path.exists():
        print(f"ERROR: {boxscore_path} not found. Run dsl_fcl_boxscores.py for "
              f"{game_date} first (this script reads its output).")
        sys.exit(1)

    records = json.loads(boxscore_path.read_text())
    payload = build_top_performers(records, season)
    payload["date"] = game_date
    payload["generatedAt"] = datetime_now_iso()

    out_path = Path("output") / f"{game_date}_top_performers.json"
    out_path.write_text(json.dumps(payload, indent=2))

    print(f"Hitters: {len(payload['hitters'])}  Pitchers: {len(payload['pitchers'])}")
    if payload["playerOfTheDay"]:
        print(f"Player of the Day: {payload['playerOfTheDay']['name']} ({payload['playerOfTheDay']['points']} pts)")
    if payload["pitcherOfTheDay"]:
        print(f"Pitcher of the Day: {payload['pitcherOfTheDay']['name']} ({payload['pitcherOfTheDay']['points']} pts)")
    print(f"Wrote {out_path}")


def datetime_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
