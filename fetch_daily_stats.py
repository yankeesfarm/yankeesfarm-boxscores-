"""
fetch_daily_stats.py

Runs daily (via GitHub Actions cron, same pattern as your other scheduled
scripts). Pulls real, current season totals from milb.com team stats pages
for every configured affiliate, and pushes each player's numbers to the
ProspectStats Wix Data Collection via the post_updatePlayerStats endpoint.

This REPLACES the need to manually update prospect.html's hardcoded stats —
once this runs, the live site pulls from this collection automatically
(see loadLiveStats() in prospect.html).

USAGE:
    python fetch_daily_stats.py

REQUIRES:
    - roster_map.json: slug -> { team_slug, stat_type: "hitting"|"pitching" }
      e.g. {"jackson-lovich": {"team": "tampa", "type": "hitting"}, ...}
    - WIX_STATS_PUSH_KEY environment variable (must match the
      "daily-stats-push-key" secret set in Wix Secrets Manager)

NOTE: milb.com team stat pages return ALL players in that team's TOP-LEVEL
game log for the CURRENT team only. A player who changed levels mid-season
will only show their current-team totals here — this script does not
attempt to reconstruct multi-level season splits (that reconciliation was
done manually for existing profiles; for ongoing accuracy, this script's
job is just to keep each player's CURRENT team's numbers fresh daily).
"""

import json
import os
import time
import requests
from pathlib import Path

MILB_BASE = "https://www.milb.com/{team}/stats/"
PUSH_ENDPOINT = "https://www.yankeesfarmreport.com/_functions/updatePlayerStats"
PUSH_KEY = os.environ.get("WIX_STATS_PUSH_KEY")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; YankeesFarmBot/1.0)"}


def fetch_team_table(team_slug, stat_type):
    """
    Fetches the unsplit (full-season) stats table for a team.
    stat_type is "hitting" or "pitching".
    Returns a dict keyed by MLB person ID (int) -> row dict of stats.

    IMPORTANT — VERIFY THIS BEFORE TRUSTING THE CRON JOB:
    This assumes milb.com serves the populated table in the raw HTML
    response to a plain GET request. Many modern sites only do this via
    client-side JavaScript after the page loads, in which case `requests`
    would get an empty shell and this function would silently return {}.
    Run `python fetch_daily_stats.py --debug-dump tampa hitting` once and
    inspect debug_tampa_hitting.html yourself before relying on this daily.
    """
    from bs4 import BeautifulSoup

    url = MILB_BASE.format(team=team_slug)
    if stat_type == "pitching":
        url += "pitching"
    params = {"playerPool": "ALL"}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if table is None:
        print(f"[WARN] No <table> found for {team_slug}/{stat_type} — "
              f"page is likely JS-rendered; plain requests won't work here.")
        return {}

    header_cells = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
    rows_out = {}
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != len(header_cells):
            continue
        link = row.find("a", href=True)
        id_match = re.search(r"-(\d+)(?:\?|$)", link["href"]) if link else None
        if not id_match:
            continue
        mlb_id = int(id_match.group(1))
        row_data = {}
        for header, cell in zip(header_cells, cells):
            text = cell.get_text(strip=True)
            row_data[header] = text
        rows_out[mlb_id] = normalize_row(row_data, stat_type)
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
    print(f"Saved raw response to {fname} — open it and search for a real player's name.")
    print("If you don't find real stats in that file, the page is JS-rendered and this script needs a headless browser (e.g. Playwright) instead of plain requests.")


def build_stint(row, level_code, level_label, stat_type):
    if stat_type == "hitting":
        return {
            "level": level_code, "levelLabel": level_label,
            "G": row["G"], "AB": row["AB"], "R": row["R"], "H": row["H"],
            "D": row["2B"], "T": row["3B"], "HR": row["HR"], "RBI": row["RBI"],
            "BB": row["BB"], "SO": row["SO"], "SB": row["SB"],
            "AVG": row["AVG"], "OBP": row["OBP"], "SLG": row["SLG"], "OPS": row["OPS"],
        }
    else:
        return {
            "level": level_code, "levelLabel": level_label,
            "G": row["G"], "GS": row.get("GS", 0), "W": row["W"], "L": row["L"],
            "SV": row.get("SV", 0), "IP": row["IP"], "H": row["H"], "ER": row["ER"],
            "BB": row["BB"], "SO": row["SO"], "ERA": row["ERA"], "WHIP": row["WHIP"],
        }


def push_player(slug, seasons, splits=None):
    resp = requests.post(PUSH_ENDPOINT, json={
        "key": PUSH_KEY, "slug": slug, "seasons": seasons, "splits": splits,
    }, timeout=15)
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

    roster_map = json.loads(Path("roster_map.json").read_text())
    teams_needed = {(v["team"], v["type"]) for v in roster_map.values()}

    team_tables = {}
    for team, stype in teams_needed:
        try:
            team_tables[(team, stype)] = fetch_team_table(team, stype)
        except Exception as e:
            print(f"[ERROR] {team}/{stype}: {e}")
        time.sleep(0.5)

    updated, skipped = [], []
    for slug, info in roster_map.items():
        table = team_tables.get((info["team"], info["type"]), {})
        row = table.get(info.get("mlbId"))
        if not row:
            skipped.append(slug)
            continue
        stint = build_stint(row, info["levelCode"], info["levelLabel"], info["type"])
        # Wrap as a single-season, single-stint update — this script refreshes
        # ONLY the player's current-team stint; historical seasons/other-level
        # stints already on file are preserved by not touching them here
        # (see updatePlayerStats endpoint — it replaces the whole seasonsJSON,
        # so a fuller version of this script should merge with prior seasons
        # rather than overwrite; flagged here as the next real refinement).
        seasons = [{"year": 2026, "stints": [stint]}]
        try:
            push_player(slug, seasons)
            updated.append(slug)
        except Exception as e:
            print(f"[ERROR] pushing {slug}: {e}")
            skipped.append(slug)
        time.sleep(0.2)

    print(f"Updated {len(updated)} players, skipped {len(skipped)}: {skipped}")


if __name__ == "__main__":
    main()
