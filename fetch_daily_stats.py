"""
fetch_daily_stats.py

Runs daily (via GitHub Actions cron, same pattern as your other scheduled
scripts). Pulls real, current season totals for every affiliate and pushes
each player's numbers to the ProspectStats Wix Data Collection via the
post_updatePlayerStats endpoint.

This REPLACES the need to manually update prospect.html's hardcoded stats --
once this runs, the live site pulls from this collection automatically
(see loadLiveStats() in prospect.html).

v2 (this version): ADDS coverage for the three rookie-level affiliates --
FCL Yankees, DSL NYY Yankees, and DSL NYY Bombers -- which previously had
ZERO players tracked. The original design required every rookie-level
prospect to be manually added to roster_map.json with a working milb.com
team-stats scrape, and rookie-level milb.com pages don't reliably serve
real <table> HTML (see fetch_team_table()'s own docstring warning below),
so nobody had ever gotten wired up. Real-world symptom: Juan Torres
(DSL NYY Yankees) had literally no record in ProspectStats at all, and
his profile page was showing stale/placeholder content instead of his
real .385/.470/.644 season line.

FIX: for these three rookie teams only, this now reuses lib/mlb_api.py's
already-proven get_active_roster() + get_player_stats_by_date_range()
functions -- the EXACT approach fetch_farm_stats.py already uses
successfully for DSL/FCL players in the Top Performers/analytics
pipeline. Rosters are discovered dynamically every run instead of
hand-maintained, which also handles short-season roster churn (players
get assigned/reassigned between these three teams constantly -- Juan
Torres himself moved from DSL NYY Bombers to DSL NYY Yankees mid-season).
This also correctly separates DSL NYY Yankees vs. DSL NYY Bombers (and
FCL Yankees), which all three share sportId 16 -- via team_id-scoped
per-game filtering. See get_player_stats_by_date_range()'s own docstring
in lib/mlb_api.py for why that per-game approach was the only reliable
way to split them (aggregate teamId filters silently failed in prior
production attempts).

Full-season affiliates (SWB, Somerset, Hudson Valley, Tampa) are
UNCHANGED -- they still use the original milb.com scrape via
roster_map.json. roster_map.json itself does not need any edits for
this update; only this script changed.

USAGE:
    python fetch_daily_stats.py

REQUIRES:
    - roster_map.json: slug -> { team_slug, stat_type: "hitting"|"pitching" }
      (unchanged, still used only for the 4 full-season teams)
    - WIX_STATS_PUSH_KEY environment variable (must match the
      "daily-stats-push-key" secret set in Wix Secrets Manager)
    - lib/mlb_api.py (already in this repo, used by fetch_farm_stats.py)

NOTE (unchanged from v1): milb.com team stat pages return ALL players in
that team's TOP-LEVEL game log for the CURRENT team only. A player who
changed levels mid-season will only show their current-team totals via
that scrape path -- this script does not attempt to reconstruct
multi-level season splits for the 4 scraped teams. The new rookie-level
path below does NOT have this limitation, since it's driven by the
Stats API's actual game log.
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.mlb_api import get_active_roster, get_player_stats_by_date_range, get_team_schedule

MILB_BASE = "https://www.milb.com/{team}/stats/"
PUSH_ENDPOINT = "https://www.yankeesfarmreport.com/_functions/updatePlayerStats"
PUSH_KEY = os.environ.get("WIX_STATS_PUSH_KEY")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; YankeesFarmBot/1.0)"}

SEASON = 2026
SEARCH_FROM_MONTH_DAY = "-02-01"  # same reasoning as fetch_farm_stats.py's SEARCH_FROM

# Rookie-level teams fetched dynamically via the MLB Stats API instead of
# milb.com scraping. IDs confirmed against Carlos's own notes / the Stats
# API: all three share sportId 16, disambiguated via team_id filtering in
# get_player_stats_by_date_range().
ROOKIE_TEAMS = [
    {"teamId": 475, "sportId": 16, "levelCode": "FCL", "levelLabel": "FCL Yankees (Rookie)"},
    {"teamId": 635, "sportId": 16, "levelCode": "DSL", "levelLabel": "DSL NYY Yankees (Rookie)"},
    {"teamId": 634, "sportId": 16, "levelCode": "DSL", "levelLabel": "DSL NYY Bombers (Rookie)"},
]

# team_id/sport_id for the 4 full-season affiliates, used only for the
# season-total computation (get_season_total_raw()) -- NOT for the milb.com
# scrape above, which is unchanged and keyed by team slug. These match
# config/affiliates.py exactly (verified live against milb.com's own
# team_info payload on 2026-09-04) -- do not use 588/589 for Hudson
# Valley/Somerset if you've seen those numbers anywhere else; they're wrong.
FULL_SEASON_TEAM_IDS = {
    "tampa": {"team_id": 587, "sport_id": 14},
    "hudson-valley": {"team_id": 537, "sport_id": 13},
    "somerset": {"team_id": 1956, "sport_id": 12},
    "scranton-wb": {"team_id": 531, "sport_id": 11},
}


# ---------------------------------------------------------------------------
# EXISTING PATH (v1, unchanged): milb.com scrape for the 4 full-season teams
# ---------------------------------------------------------------------------

def fetch_team_table(team_slug, stat_type):
    """
    Fetches the unsplit (full-season) stats table for a team.
    stat_type is "hitting" or "pitching".
    Returns a dict keyed by MLB person ID (int) -> row dict of stats.

    IMPORTANT -- VERIFY THIS BEFORE TRUSTING THE CRON JOB:
    This assumes milb.com serves the populated table in the raw HTML
    response to a plain GET request. Many modern sites only do this via
    client-side JavaScript after the page loads, in which case `requests`
    would get an empty shell and this function would silently return {}.
    Run `python fetch_daily_stats.py --debug-dump tampa hitting` once and
    inspect debug_tampa_hitting.html yourself before relying on this daily.

    NOTE: this is why rookie-level teams (FCL/DSL) are NOT fetched this
    way -- their milb.com pages are more likely to hit exactly this
    failure mode, so v2 routes them through the Stats API instead (see
    fetch_rookie_team_players() below).
    """
    from bs4 import BeautifulSoup

    url = MILB_BASE.format(team=team_slug)
    if stat_type == "pitching":
        url += "pitching"
    params = {"playerPool": "ALL"}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    print(f"[DEBUG] {team_slug}/{stat_type}: found {len(tables)} <table> element(s)")
    if not tables:
        print(f"[WARN] No <table> found for {team_slug}/{stat_type} -- "
              f"page may use div-based grid markup instead of real <table> tags.")
        return {}
    table = tables[0]

    thead = table.find("thead")
    header_cells = [th.get_text(strip=True) for th in thead.find_all("th")] if thead else []
    print(f"[DEBUG] Headers found (informational only, not used for mapping): {header_cells}")

    tbody = table.find("tbody")
    if tbody is None:
        print(f"[WARN] Table found but no <tbody>.")
        return {}
    body_rows = tbody.find_all("tr")
    print(f"[DEBUG] Found {len(body_rows)} <tr> rows in tbody")

    # Confirmed via manual inspection of real decoded row data (Aug 2026) -- the PLAYER
    # column has no <td> of its own (name lives only in the row's <a> link), so these
    # lists are one shorter than the visible header count and start at TEAM.
    HITTING_COLS = ["TEAM","G","AB","R","H","2B","3B","HR","RBI","BB","SO","SB","CS","AVG","OBP","SLG","OPS"]
    PITCHING_COLS = ["TEAM","W","L","ERA","G","GS","CG","SHO","SV","SVO","IP","H","R","ER","HR","HB","BB","SO","WHIP","AVG"]
    expected_cols = PITCHING_COLS if stat_type == "pitching" else HITTING_COLS

    rows_out = {}
    for i, row in enumerate(body_rows):
        cells = row.find_all("td")
        link = row.find("a", href=True)
        cell_texts = [c.get_text(strip=True) for c in cells]
        if i == 0:
            print(f"[DEBUG] Row 0 cell values: {cell_texts}")
            print(f"[DEBUG] Row 0: {len(cells)} cells (expected {len(expected_cols)}), "
                  f"link href={link['href'] if link else None}")
        if len(cells) != len(expected_cols):
            continue
        id_match = re.search(r"/player/(\d+)", link["href"]) if link else None
        if not id_match:
            continue
        mlb_id = int(id_match.group(1))
        row_data = dict(zip(expected_cols, cell_texts))
        rows_out[mlb_id] = normalize_row(row_data, stat_type)
    print(f"[DEBUG] Successfully parsed {len(rows_out)} player rows")
    return rows_out


def normalize_row(row, stat_type):
    """Converts milb.com's column labels/string values into the numeric fields build_stint expects."""
    def num(key, cast=int):
        val = row.get(key, "0").replace(",", "")
        try:
            return cast(val)
        except ValueError:
            return 0.0 if cast is float else 0

    if stat_type == "hitting":
        return {
            "G": num("G"), "AB": num("AB"), "R": num("R"), "H": num("H"),
            "2B": num("2B"), "3B": num("3B"), "HR": num("HR"), "RBI": num("RBI"),
            "BB": num("BB"), "SO": num("SO"), "SB": num("SB"),
            "AVG": num("AVG", float), "OBP": num("OBP", float),
            "SLG": num("SLG", float), "OPS": num("OPS", float),
        }
    else:
        return {
            "G": num("G"), "GS": num("GS"), "W": num("W"), "L": num("L"), "SV": num("SV"),
            "IP": num("IP", float), "H": num("H"), "ER": num("ER"), "BB": num("BB"), "SO": num("SO"),
            "ERA": num("ERA", float), "WHIP": num("WHIP", float),
        }


def debug_dump(team_slug, stat_type):
    """Run with --debug-dump <team> <hitting|pitching> to save the raw HTML for manual inspection."""
    url = MILB_BASE.format(team=team_slug)
    if stat_type == "pitching":
        url += "pitching"
    resp = requests.get(url, params={"playerPool": "ALL"}, headers=HEADERS, timeout=20)
    fname = f"debug_{team_slug}_{stat_type}.html"
    Path(fname).write_text(resp.text)
    print(f"Saved raw response to {fname} -- open it and search for a real player's name.")
    print("If you don't find real stats in that file, the page is JS-rendered and this script needs a headless browser (e.g. Playwright) instead of plain requests.")


def build_stint(row, level_code, level_label, stat_type):
    if stat_type == "hitting":
        return {
            "level": level_code, "levelLabel": level_label,
            "G": row["G"], "AB": row["AB"], "R": row["R"], "H": row["H"],
            "D": row["2B"], "T": row["3B"], "HR": row["HR"], "RBI": row["RBI"],
            "BB": row["BB"], "SO": row["SO"], "SB": row["SB"],
            "AVG": row["AVG"], "OBP": row["OBP"], "SLG": row["SLG"], "OPS": row["OPS"],
            "WRC": round(100 * row["OPS"] / 0.700),  # same approximation formula used site-wide
        }
    else:
        return {
            "level": level_code, "levelLabel": level_label,
            "G": row["G"], "GS": row.get("GS", 0), "W": row["W"], "L": row["L"],
            "SV": row.get("SV", 0), "IP": row["IP"], "H": row["H"], "ER": row["ER"],
            "BB": row["BB"], "SO": row["SO"], "ERA": row["ERA"], "WHIP": row["WHIP"],
        }


# ---------------------------------------------------------------------------
# NEW PATH (v2): Stats-API-driven fetch for the 3 rookie-level affiliates
# ---------------------------------------------------------------------------

def slugify(name):
    """Matches the existing roster_map.json slug convention (see e.g.
    "ernesto-martinez-jr", "wilberson-de-pena"): lowercase, accents
    stripped to plain ASCII, periods dropped, everything else that isn't
    alphanumeric/space/hyphen dropped, spaces collapsed to single hyphens."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    s = ascii_name.lower().replace(".", "")
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def find_opening_day(team_id, sport_id):
    """Same approach as fetch_farm_stats.py: ask the Stats API for this
    team's real schedule rather than guessing a generic season-start
    date, since rookie-level teams start on different dates than
    full-season affiliates."""
    search_from = f"{SEASON}{SEARCH_FROM_MONTH_DAY}"
    end_date = date.today().isoformat()
    games = get_team_schedule(team_id, search_from, end_date, sport_id=sport_id)
    if not games:
        return None
    dates = [g["gameDate"][:10] for g in games if g.get("gameDate")]
    return min(dates) if dates else None


def merge_raw_stat_dicts(a, b):
    """Sums two raw Stats-API counting-stat dicts (as returned by
    get_player_stats_by_date_range) field by field. Both dicts came from
    the same summation logic in lib/mlb_api.py, so this uses the same
    special-case handling for inningsPitched (outs summed as integers,
    then the combined string rebuilt at the end) rather than naively
    adding the two decimal-looking strings."""
    if a is None:
        return b
    if b is None:
        return a
    merged = dict(a)
    ip_outs_total = None
    for k, v in b.items():
        if k == "inningsPitched":
            def _outs(ip_str):
                whole, _, frac = str(ip_str).partition(".")
                return (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)
            ip_outs_total = _outs(a.get("inningsPitched", "0.0")) + _outs(v)
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            merged[k] = merged.get(k, 0) + v
        elif k not in merged:
            merged[k] = v
    if ip_outs_total is not None:
        merged["inningsPitched"] = f"{ip_outs_total // 3}.{ip_outs_total % 3}"
    return merged


def get_season_total_raw(person_id, group, current_sport_id, current_team_id):
    """Builds this player's TRUE 2026 season total across every level he's
    played at, using raw Stats-API counting stats (so AVG/OBP/SLG can be
    correctly re-derived afterward -- see compute_hitting_line/
    compute_pitching_line, which need hitByPitch/sacFlies for an exact
    OBP that neither milb.com's displayed rate stats nor a naive
    recombination of two already-rounded rate stats can give you).

    Real-world case this exists for: Luis Puello played 7 games for FCL
    Yankees (rookie) before being promoted to Tampa (Low-A) on May 12,
    2026. The milb.com scrape used elsewhere in this script only ever
    shows his CURRENT team's stint (Tampa), so his FCL production was
    invisible in our "current stint" data -- correct for a "who's he
    playing for right now" view, but wrong for a "what's his 2026 total"
    view. This function's job is specifically the latter.

    Uses team_id-filtered date ranges spanning the WHOLE season (not
    promotion-date-bounded) for both levels -- this works cleanly without
    needing to know the actual promotion date, since get_player_stats_by_
    date_range's per-game team_id filtering already naturally excludes
    games the player didn't actually play for that team."""
    search_from = f"{SEASON}{SEARCH_FROM_MONTH_DAY}"
    end_date = date.today().isoformat()

    # Rookie-level component (FCL/DSL, sportId 16). No team_id filter --
    # a Yankees farmhand only ever plays for Yankees-affiliated rookie
    # teams, so this safely captures whichever of FCL Yankees/DSL NYY
    # Yankees/DSL NYY Bombers he was on, without needing to know which.
    rookie_raw = get_player_stats_by_date_range(
        person_id, group, 16, SEASON, search_from, end_date
    )

    # Current full-season level component, team_id-scoped so it only
    # includes games actually played for his current team.
    current_raw = get_player_stats_by_date_range(
        person_id, group, current_sport_id, SEASON, search_from, end_date,
        team_id=current_team_id
    )

    if rookie_raw is None and current_raw is None:
        return None
    return merge_raw_stat_dicts(rookie_raw, current_raw)


def compute_hitting_line(totals):
    """Recomputes rate stats from raw summed counting stats -- same math
    as fetch_farm_stats.py's finalize_team_batting()/finalize_hitter(),
    since get_player_stats_by_date_range() intentionally does NOT compute
    rate stats itself (see its docstring)."""
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
    avg = round(h / ab, 3) if ab else 0.0
    obp = round((h + bb + hbp) / obp_denom, 3) if obp_denom else 0.0
    slg = round(tb / ab, 3) if ab else 0.0
    ops = round(obp + slg, 3)
    return {
        "G": totals.get("gamesPlayed", 0), "AB": ab,
        "R": totals.get("runs", 0), "H": h,
        "2B": doubles, "3B": triples, "HR": hr, "RBI": totals.get("rbi", 0),
        "BB": bb, "SO": totals.get("strikeOuts", 0), "SB": totals.get("stolenBases", 0),
        "AVG": avg, "OBP": obp, "SLG": slg, "OPS": ops,
    }


def compute_pitching_line(totals):
    """Same math as fetch_farm_stats.py's finalize_team_pitching()/finalize_pitcher()."""
    ip_str = str(totals.get("inningsPitched", "0.0"))
    whole, _, frac = ip_str.partition(".")
    outs = (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)
    true_ip = outs / 3 if outs else 0.0
    bb = totals.get("baseOnBalls", 0)
    hits = totals.get("hits", 0)
    er = totals.get("earnedRuns", 0)
    era = round(9 * er / true_ip, 2) if true_ip else 0.0
    whip = round((bb + hits) / true_ip, 2) if true_ip else 0.0
    return {
        "G": totals.get("gamesPlayed", 0), "GS": totals.get("gamesStarted", 0),
        "W": totals.get("wins", 0), "L": totals.get("losses", 0),
        "SV": totals.get("saves", 0),
        "IP": float(ip_str) if ip_str else 0.0,
        "H": hits, "ER": er, "BB": bb, "SO": totals.get("strikeOuts", 0),
        "ERA": era, "WHIP": whip,
    }


def fetch_rookie_team_players(team, graduated_slugs):
    """Dynamically discovers every player on this rookie-level team's
    full-season roster and pulls their team-scoped stat line via the
    Stats API -- no roster_map.json entry required. Returns a list of
    {slug, seasons} dicts ready to push.

    IMPORTANT: get_active_roster() deliberately uses rosterType="fullSeason"
    (see its own docstring in lib/mlb_api.py), which returns EVERYONE who
    was ever on this team's roster this season -- including players who
    have since been promoted to a full-season affiliate. Real-world bug
    this caused: Luis Puello was promoted from FCL Yankees to Tampa on
    May 12, 2026, and has played months of Tampa games since. But because
    he still shows up on FCL's fullSeason roster query, this function
    would generate a "luis-puello" slug with only his old 7-game FCL line
    -- and since this rookie-level loop runs AFTER the roster_map.json
    full-season loop in main(), that stale FCL-only push OVERWROTE his
    correct, much larger Tampa dataset. graduated_slugs (the set of slugs
    already in roster_map.json) lets us skip anyone who has graduated to
    a full-season affiliate, since roster_map.json is the authoritative
    source for them -- this rookie-level path should only ever push
    players who are rookie-level ONLY."""
    opening_day = find_opening_day(team["teamId"], team["sportId"])
    if not opening_day:
        print(f"  WARNING: could not find any completed games for teamId={team['teamId']}. Skipping.")
        return []
    end_date = date.today().isoformat()
    print(f"  Opening Day: {opening_day}. Pulling full-season roster for teamId={team['teamId']}...")

    roster = get_active_roster(team["teamId"], SEASON)
    results = []
    skipped_graduated = []
    for entry in roster:
        person = entry["person"]
        pid = person["id"]
        name = person.get("fullName")
        slug_preview = slugify(name)
        if slug_preview in graduated_slugs:
            skipped_graduated.append(slug_preview)
            continue
        pos_type = (entry.get("position") or {}).get("type", "")
        is_pitcher = pos_type == "Pitcher"
        group = "pitching" if is_pitcher else "hitting"

        stat = get_player_stats_by_date_range(
            pid, group, team["sportId"], SEASON, opening_day, end_date, team_id=team["teamId"]
        )
        if not stat:
            continue

        line = compute_pitching_line(stat) if is_pitcher else compute_hitting_line(stat)
        stint = build_stint(line, team["levelCode"], team["levelLabel"], group)
        slug = slugify(name)
        results.append({
            "slug": slug,
            "name": name,
            "seasons": [{"year": SEASON, "stints": [stint]}],
        })
        time.sleep(0.1)

    print(f"  Found {len(results)} players with 2026 activity for teamId={team['teamId']}.")
    if skipped_graduated:
        print(f"  Skipped {len(skipped_graduated)} player(s) already tracked as full-season "
              f"(graduated from this level): {skipped_graduated}")
    return results


# ---------------------------------------------------------------------------
# Shared push logic
# ---------------------------------------------------------------------------

def push_player(slug, seasons, splits=None):
    resp = requests.post(PUSH_ENDPOINT, json={
        "key": PUSH_KEY, "slug": slug, "seasons": seasons, "splits": splits,
    }, timeout=15)
    if not resp.ok:
        print(f"[ERROR BODY] {slug}: status={resp.status_code} body={resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


def main():
    import sys
    if "--debug-dump" in sys.argv:
        idx = sys.argv.index("--debug-dump")
        team, stype = sys.argv[idx+1], sys.argv[idx+2]
        debug_dump(team, stype)
        return

    if not PUSH_KEY:
        raise SystemExit("WIX_STATS_PUSH_KEY environment variable not set")

    updated, skipped = [], []

    # --- Existing path: full-season teams via roster_map.json + milb.com scrape ---
    roster_map = json.loads(Path("roster_map.json").read_text())
    teams_needed = {(v["team"], v["type"]) for v in roster_map.values()}
    # Every slug already tracked as full-season -- passed to the rookie-level
    # loop below so it never re-pushes stale rookie-level data over a
    # graduated player's correct current stint (see fetch_rookie_team_players()
    # docstring for the real incident this prevents).
    graduated_slugs = set(roster_map.keys())

    team_tables = {}
    for team, stype in teams_needed:
        try:
            team_tables[(team, stype)] = fetch_team_table(team, stype)
        except Exception as e:
            print(f"[ERROR] {team}/{stype}: {e}")
        time.sleep(0.5)

    for slug, info in roster_map.items():
        table = team_tables.get((info["team"], info["type"]), {})
        row = table.get(info.get("mlbId"))
        if not row:
            skipped.append(slug)
            continue
        stint = build_stint(row, info["levelCode"], info["levelLabel"], info["type"])

        # Season total: separate from the current-team stint above. Combines
        # this stint with any rookie-level (FCL/DSL) production earlier this
        # season via the Stats API, so a promoted player's 2026 total isn't
        # silently missing whatever level he started the year at (see
        # get_season_total_raw()'s docstring -- this is the Luis Puello case).
        season_total = None
        team_ids = FULL_SEASON_TEAM_IDS.get(info["team"])
        if team_ids and info.get("mlbId"):
            try:
                raw = get_season_total_raw(
                    info["mlbId"], info["type"], team_ids["sport_id"], team_ids["team_id"]
                )
                if raw:
                    line = (compute_pitching_line(raw) if info["type"] == "pitching"
                            else compute_hitting_line(raw))
                    season_total = {**line, "levelLabel": "2026 Season Total (All Levels)"}
            except Exception as e:
                print(f"[WARN] season total for {slug}: {e}")

        # Wrap as a single-season update. The current-team stint (top-of-page
        # display) is refreshed every run from the milb.com scrape above.
        # seasonTotal (career-page display) is refreshed every run from the
        # Stats API, independent of stints, so it doesn't depend on -- or get
        # corrupted by -- anything this script has or hasn't stored before.
        season_entry = {"year": SEASON, "stints": [stint]}
        if season_total:
            season_entry["seasonTotal"] = season_total
        seasons = [season_entry]
        try:
            push_player(slug, seasons)
            updated.append(slug)
        except Exception as e:
            print(f"[ERROR] pushing {slug}: {e}")
            skipped.append(slug)
        time.sleep(0.2)

    # --- New path: rookie-level teams via Stats API, discovered dynamically ---
    print("\nFetching rookie-level affiliates (FCL/DSL) via Stats API...")
    for team in ROOKIE_TEAMS:
        try:
            players = fetch_rookie_team_players(team, graduated_slugs)
        except Exception as e:
            print(f"[ERROR] teamId={team['teamId']}: {e}")
            continue
        for p in players:
            try:
                push_player(p["slug"], p["seasons"])
                updated.append(p["slug"])
            except Exception as e:
                print(f"[ERROR] pushing {p['slug']} ({p['name']}): {e}")
                skipped.append(p["slug"])
            time.sleep(0.2)

    print(f"\nUpdated {len(updated)} players, skipped {len(skipped)}: {skipped}")


if __name__ == "__main__":
    main()
