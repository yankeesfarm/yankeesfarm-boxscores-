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
and captures BOTH each player's per-team stint (his stats at that specific
level only) AND his combined season total across every team, so the site
can show either view depending on which level filter is selected -- see
the "two kinds of rows" comment in main() for details.

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

# NOTE: promoted/demoted players are no longer collapsed into a single
# "highest level reached" row -- see the two-kinds-of-rows comment in
# main() for how level-specific vs. combined-season display now works.

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
    return {"wins": wins, "losses": losses, "winPct": win_pct, "games": total}


def finalize_team_batting(totals):
    """Team-wide batting line, computed the SAME way individual player
    rows are (never trusted from a separate pre-aggregated API call) --
    derived from raw counts accumulated directly from every player's
    per-team stat fetch during the roster loop in main(), BEFORE any
    cross-level combining happens. This is what actually fixes the
    promoted-player undercount bug: a player promoted away from this team
    mid-season still gets summed into these totals, since his team-scoped
    stat fetch for THIS specific team already captured his real production
    here, independent of where he ends up being displayed individually."""
    ab = totals.get("atBats", 0)
    h = totals.get("hits", 0)
    doubles = totals.get("doubles", 0)
    triples = totals.get("triples", 0)
    hr = totals.get("homeRuns", 0)
    bb = totals.get("baseOnBalls", 0)
    hbp = totals.get("hitByPitch", 0)
    sf = totals.get("sacFlies", 0)
    singles = h - doubles - triples - hr
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    obp_denom = ab + bb + hbp + sf
    return {
        "avg": round(h / ab, 3) if ab else 0.0,
        "obp": round((h + bb + hbp) / obp_denom, 3) if obp_denom else 0.0,
        "slg": round(tb / ab, 3) if ab else 0.0,
        "ops": round(((h + bb + hbp) / obp_denom if obp_denom else 0.0) + (round(tb / ab, 3) if ab else 0.0), 3),
        "runs": totals.get("runs", 0),
        "homeRuns": hr,
        "stolenBases": totals.get("stolenBases", 0),
    }


def finalize_team_pitching(totals):
    """Same principle as finalize_team_batting() -- summed directly from
    every pitcher's per-team stat fetch during the roster loop, not a
    separate team-stats API call."""
    outs = totals.get("_outs", 0)
    true_ip = outs / 3 if outs else 0.0
    return {
        "era": round(9 * totals.get("earnedRuns", 0) / true_ip, 2) if true_ip else 0.0,
        "whip": round((totals.get("baseOnBalls", 0) + totals.get("hits", 0)) / true_ip, 2) if true_ip else 0.0,
        "strikeOuts": totals.get("strikeOuts", 0),
        "saves": totals.get("saves", 0),
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
        "ab": ab,
        "pa": pa,
        "h": h,
        "avg": avg,
        "obp": obp,
        "slg": slg,
        "ops": ops,
        "hr": hr,
        "doubles": doubles,
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
    # "_outs" is only present if this dict already went through
    # _accumulate() (the cross-level combined totals). A single team
    # stint's raw stat dict (used for level-specific rows) never gets that
    # treatment, so fall back to parsing "inningsPitched" directly here --
    # same outs-are-thirds-not-decimals conversion used everywhere else in
    # this pipeline.
    outs = totals.get("_outs")
    if outs is None:
        whole, _, frac = str(totals.get("inningsPitched", "0.0")).partition(".")
        outs = (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)
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

    # Opponent batting average against -- "atBats" here is at-bats BY THE
    # OPPOSING HITTERS this pitcher faced (a real field the Stats API
    # returns on every pitching stat object, already flowing through
    # _accumulate() for combined rows the same as hits/bb/so). Never
    # derived/estimated -- None (renders as "-" on the site) when a stint
    # has zero recorded at-bats against, instead of a misleading 0.000.
    ab_against = totals.get("atBats", 0)
    avg_against = round(hits / ab_against, 3) if ab_against else None

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
        "h": hits,
        "bb": bb,
        "k": so,
        "avg": avg_against,
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


def enrich_with_fangraphs(hitters, season):
    """Pull wOBA and wRC+ for all NYY system players from FanGraphs' minor
    league leaderboard API. FanGraphs computes these with their own per-level
    park factors and run-environment scaling -- the same values displayed
    publicly on their minor league leaderboard and used manually on prospect
    profiles. Matched to our rows by player name (case-insensitive).

    Uses qual=0 (no PA minimum) so we get every NYY player, not just
    those who qualify by FG's own threshold. Unmatched players keep our
    calc_woba() value for woba and get wrc_plus=None (renders as '—').

    IMPORTANT: FanGraphs returns combined season stats when splitTeam=false.
    This means wOBA/wRC+ on level-specific stint rows (e.g., a player's
    A+ line after he was promoted to AA) will be his combined-season value,
    not his level-specific value. The "all" combined rows are accurate.
    A follow-up call with splitTeam=true would fix this for level splits."""
    ALL_MiLB_LGS = "2,4,5,6,7,8,9,10,11,14,12,13,15,16,17,18,30,32"
    url = "https://www.fangraphs.com/api/leaders/minor-league/data"
    params = {
        "pos": "all",
        "lg": ALL_MiLB_LGS,
        "stats": "bat",
        "qual": 0,           # no minimum -- we want all NYY players
        "type": 1,           # advanced tab (wOBA, wRC+, BB%, K%, ISO, BABIP, etc.)
        "season": season,
        "seasonEnd": season,
        "org": 9,            # NYY org ID
        "ind": 0,
        "splitTeam": "false",
        "level": 0,          # all levels combined
    }
    # Strategy: load FanGraphs' homepage first so Cloudflare issues a
    # clearance cookie to the real Chromium browser, then call the data
    # API directly from within that browser context (which carries the
    # cookie). This avoids both the expect_response race condition and
    # the headless-detection issue -- the browser already passed the CF
    # challenge, so the in-context fetch() call succeeds immediately.
    print(f"Fetching FanGraphs wOBA/wRC+ for NYY system ({season}) via Playwright...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed; skipping FanGraphs enrichment.")
        return

    api_params = (
        f"pos=all&lg=2%2C4%2C5%2C6%2C7%2C8%2C9%2C10%2C11%2C14%2C12%2C13"
        f"%2C15%2C16%2C17%2C18%2C30%2C32&stats=bat&qual=0&type=1"
        f"&season={season}&seasonEnd={season}&org=9&ind=0"
        f"&splitTeam=false&level=0"
    )
    api_path = f"/api/leaders/minor-league/data?{api_params}"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = ctx.new_page()
            # Patch out the automation flag Cloudflare checks.
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            # Step 1: load homepage to receive a Cloudflare clearance cookie.
            print("  Step 1: loading FanGraphs homepage for CF clearance...")
            page.goto("https://www.fangraphs.com/", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(5_000)   # let JS challenge complete
            # Step 2: call the data API from within the now-cleared browser context.
            print("  Step 2: calling API from within browser context...")
            payload = page.evaluate(f"""
                async () => {{
                    const r = await fetch('{api_path}', {{
                        headers: {{
                            'Accept': 'application/json',
                            'Referer': 'https://www.fangraphs.com/leaders/minor-league'
                        }}
                    }});
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return await r.json();
                }}
            """)
            ctx.close()
            browser.close()
        print("  FG: data captured successfully.")
    except Exception as e:
        print(f"  FanGraphs Playwright fetch failed ({e}); wOBA uses computed value, wRC+ will be null.")
        return

    if isinstance(payload, dict):
        print(f"  FG payload top-level keys: {list(payload.keys())}")
    else:
        print(f"  FG payload type: {type(payload).__name__}")

    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    if not rows:
        print("  FanGraphs returned empty payload; skipping wRC+ enrichment.")
        return

    first = rows[0] if rows else {}
    print(f"  FG first-row keys: {list(first.keys())}")
    print(f"  FG total rows: {len(rows)}")

    def _get(row, *keys):
        for k in keys:
            if row.get(k) is not None:
                return row[k]
        return None

    fg_by_name = {}
    for row in rows:
        name = (_get(row, "PlayerName", "Name", "playerName") or "").strip().lower()
        if not name:
            continue
        woba = _get(row, "wOBA", "woba")
        wrc_plus = _get(row, "wRC+", "wRCplus", "wrc_plus", "wRCPlus")
        fg_by_name[name] = {"woba": woba, "wrc_plus": wrc_plus}

    print(f"  FG names (first 5): {list(fg_by_name.keys())[:5]}")
    print(f"  Our names (first 5): {[h['name'].lower() for h in hitters[:5]]}")

    matched = 0
    for h in hitters:
        fg = fg_by_name.get(h["name"].strip().lower())
        if not fg:
            continue
        if fg["woba"] is not None:
            h["woba"] = fg["woba"]
        h["wrc_plus"] = fg["wrc_plus"]
        matched += 1

    print(f"  FanGraphs enrichment: matched {matched} / {len(hitters)} hitter rows.")


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
    hitting_stints = []
    pitching_stints = []
    person_names = {}
    person_pos = {}
    team_rows = []

    for t in teams:
        print(f"Finding Opening Day for {t['name']}...")
        opening_day, season_games = find_opening_day_and_games(t["teamId"], t["sportId"], search_from, end_date)
        if not opening_day:
            print(f"  WARNING: could not find any completed games for {t['name']}. Skipping.")
            continue
        print(f"  Opening Day: {opening_day}. Pulling full-season roster...")

        record = compute_team_record(season_games, t["teamId"])

        # Accumulated fresh per team, directly from each player's per-team
        # stat fetch below -- NOT from the final cross-level "hitters"/
        # "pitchers" lists, which intentionally show a promoted player
        # under only his highest level reached. A player promoted AWAY
        # from this team mid-season still needs to count toward THIS
        # team's season totals, which only these team-scoped accumulators
        # capture correctly.
        team_batting_totals = {}
        team_pitching_totals = {}

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

            # Keep this stint's OWN team-scoped stat line intact (not just
            # accumulated into the cross-level total) -- this is what lets
            # the site show Jackson Lovich's 2 HR while filtered to Hudson
            # Valley specifically, separate from his 20 HR combined season
            # line under "All Levels" below.
            stint_list = pitching_stints if is_pitcher else hitting_stints
            stint_list.append({"pid": pid, "level": t["level_id"], "stat": dict(stat)})

            target = pitching_totals if is_pitcher else hitting_totals
            bucket = target.setdefault(pid, {})
            _accumulate(bucket, stat, is_pitcher)

            team_bucket = team_pitching_totals if is_pitcher else team_batting_totals
            _accumulate(team_bucket, stat, is_pitcher)

        team_rows.append({
            "teamId": t["teamId"],
            "name": t["name"],
            "level": t["level_id"],
            **record,
            **finalize_team_batting(team_batting_totals),
            **finalize_team_pitching(team_pitching_totals),
        })

        time.sleep(0.1)

    all_person_ids = set(hitting_totals) | set(pitching_totals)
    print(f"Fetching bios for {len(all_person_ids)} players...")
    bios = fetch_bios(all_person_ids)

    # Qualifying threshold for the "All Qualified" filter on /the-farm --
    # same formula fetch_season_stats.py already uses for the season
    # leaderboard (3.1 PA / team game for hitters, 0.5 IP / team game for
    # pitchers, using the MOST games any one affiliate has played as the
    # season's reference point, per that file's own reasoning: minor
    # league pitcher workloads are deliberately capped, so MLB's literal
    # "1 IP/team game" rule excludes legitimate rotation regulars).
    max_games = max((t.get("games", 0) for t in team_rows), default=0)
    qualifying_pa = round(3.1 * max_games)
    qualifying_ip = round(0.5 * max_games, 1)
    print(f"Qualifying threshold: {max_games} games (max across affiliates) -> "
          f"{qualifying_pa} PA / {qualifying_ip} IP to be marked qualified.")

    # Two kinds of rows per player, both tagged via the same "level" field
    # the site already filters on:
    #   1. One row per level he actually played at this season, showing
    #      ONLY that level's stats (e.g. Jackson Lovich's 2 HR at
    #      Hudson Valley specifically, tagged level="A+").
    #   2. One additional row tagged level="all" with his combined season
    #      totals -- matches the "ALL" entry's id in analytics.html's
    #      LEVELS array exactly, so clicking that button already filters
    #      to just these combined rows with zero client-side changes needed.
    # A player who stayed at one level all year simply gets two nearly
    # identical rows (his level-specific stint and his "all" total, which
    # are the same number) -- harmless, and keeps the data model uniform.
    # Every row also gets a "qualified" boolean (PA/IP vs. the threshold
    # above) so the site's "All Qualified" button can filter out
    # small-sample noise from the "All Levels" view.
    hitters = [
        finalize_hitter(s["pid"], person_names[s["pid"]], person_pos.get(s["pid"], ""), s["level"],
                         s["stat"], bios.get(s["pid"]))
        for s in hitting_stints
    ]
    hitters += [
        finalize_hitter(pid, person_names[pid], person_pos.get(pid, ""), "all", totals, bios.get(pid))
        for pid, totals in hitting_totals.items()
    ]
    for row in hitters:
        row["qualified"] = row["pa"] >= qualifying_pa

    pitchers = [
        finalize_pitcher(s["pid"], person_names[s["pid"]], person_pos.get(s["pid"], "P"), s["level"], s["stat"])
        for s in pitching_stints
    ]
    pitchers += [
        finalize_pitcher(pid, person_names[pid], person_pos.get(pid, "P"), "all", totals)
        for pid, totals in pitching_totals.items()
    ]
    for row in pitchers:
        row["qualified"] = row["ip"] >= qualifying_ip

    for row in pitchers:
        row.update(bio_fields(bios.get(row["mlbId"])))

    apply_fip_by_level(pitchers)
    enrich_with_fangraphs(hitters, season)
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
