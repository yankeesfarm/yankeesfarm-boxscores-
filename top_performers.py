#!/usr/bin/env python3
"""
YankeesFarm Top Performers of the Night
------------------------------------------------------------
Reads the JSON produced by dsl_fcl_boxscores.py for a given date and pulls
out the players who had a standout game, formatted for the nightly
dashboard on yankeesfarmreport.com.

Filter rules:
  Hitters:   2+ hits, at least 1 of which is a 2B/3B/HR
  Starters:  5+ IP, 2 or fewer runs allowed, 3+ strikeouts
  Relievers: 1 or 0 runs allowed

Run this AFTER dsl_fcl_boxscores.py in the same workflow step, since it
reads that script's JSON output rather than re-hitting the MLB API for
box scores. It does make one API call per qualifying hitter to pull
season-to-date totals (same call dsl_fcl_boxscores.py already makes for
the recap .txt), so keep this script in the same job/runner.

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

# Reuses the season-totals lookup and level labels already built and
# tested in the main box score script -- keeping this as the single
# source of truth rather than re-implementing it here.
from dsl_fcl_boxscores import get_season_totals, LEVEL_LABELS

HEADSHOT_URL = "https://midfield.mlbstatic.com/v1/people/{id}/spots/120"

MIN_HITTER_HITS   = 2
MIN_HITTER_TB     = 4    # 2+ hits normally need 4+ total bases...
MIN_HITTER_RBI_EXCEPTION = 2  # ...unless under 4 TB but 2+ RBI

MIN_PITCHER_OUTS   = 1 * 3   # 1.0 IP -- floor, must pitch at least this much
MID_OUTING_OUTS     = 2 * 3   # 2.0 IP -- boundary where the "short" bucket ends
LONG_OUTING_OUTS    = 5 * 3   # 5.0 IP -- boundary where the "long" bucket begins

MAX_SHORT_RUNS      = 1        # 1.0-1.2 IP: max runs allowed
MIN_SO_SHORT        = 3        # 1.0-1.2 IP: min strikeouts

MIN_SO_MID          = 3        # 2.0-4.2 IP: min strikeouts (no run cap)

MAX_LONG_RUNS       = 2        # 5.0+ IP: max runs allowed
MIN_SO_LONG         = 4        # 5.0+ IP: min strikeouts


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


def headshot(player_id: int) -> str:
    return HEADSHOT_URL.format(id=player_id)


def format_hitter_line(row: dict, season: dict) -> str:
    """'3 for 4: Double (5), HR (12), 3 RBI (44), SB (8)' -- extra-base
    hits, HR, RBI, and SB only, each with the season-to-date total in
    parens. Matches Carlos's template exactly."""
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
    h = row.get("h", 0)
    if h < MIN_HITTER_HITS:
        return False

    doubles = row.get("doubles", 0)
    triples = row.get("triples", 0)
    hr      = row.get("homeRuns", 0)
    singles = h - doubles - triples - hr
    total_bases = singles * 1 + doubles * 2 + triples * 3 + hr * 4

    # Normal bar: 2+ hits AND 4+ total bases.
    if total_bases >= MIN_HITTER_TB:
        return True
    # Exception: 2+ hits with fewer than 4 total bases still qualifies
    # if he drove in at least 2 runs.
    return row.get("rbi", 0) >= MIN_HITTER_RBI_EXCEPTION


def qualifying_pitcher(row: dict) -> bool:
    outs = ip_to_outs(row.get("ip"))
    r    = row.get("r", 0)
    so   = row.get("so", 0)

    # Floor for everyone: must have recorded at least a full inning
    # (3 outs) to be listed at all.
    if outs < MIN_PITCHER_OUTS:
        return False

    # 5.0+ IP: tougher strikeout bar, runs allowed cap unchanged.
    if outs >= LONG_OUTING_OUTS:
        return r <= MAX_LONG_RUNS and so >= MIN_SO_LONG

    # 1.0-1.2 IP: runs allowed cap unchanged, flat strikeout minimum
    # (previously split 3K/0R vs 5K/1R -- now a single flat number).
    if outs < MID_OUTING_OUTS:
        return r <= MAX_SHORT_RUNS and so >= MIN_SO_SHORT

    # 2.0 up to 4.2 IP: no runs-allowed cap (unchanged from before),
    # just needs the strikeout minimum.
    return so >= MIN_SO_MID


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
            })

    # SPs listed before RPs. Python's sort is stable, so within each
    # group players stay in the order they were found (i.e. game order),
    # only the SP/RP grouping itself is reordered.
    pitchers.sort(key=lambda p: 0 if p["role"] == "SP" else 1)

    return {"hitters": hitters, "pitchers": pitchers}


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
    print(f"Wrote {out_path}")


def datetime_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
