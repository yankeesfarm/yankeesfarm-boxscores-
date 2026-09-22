#!/usr/bin/env python3
"""
push_prospect_stats.py

Feeds the prospect PROFILE pages (getPlayerStats) the exact shape they read:
per-affiliate season stints + LH/RH splits + FanGraphs wRC+, keyed by slug.

Built on the SAME proven fetch path as fetch_season_stats.py / fetch_farm_stats.py
(lib/mlb_api.py + config/affiliates) so a promoted player's per-level lines are
correct and nothing re-invents roster/stat fetching. For each prospect in
prospect_profiles.json we:
  1. walk the Yankees affiliate rosters (fullSeason) and capture that player's
     own per-team stint via get_player_stats_by_date_range (game log filtered
     in code, same as the rest of the pipeline)
  2. fetch vs-LHP/RHP (or vs-LHB/RHB) season splits via statSplits
  3. pull wRC+ from FanGraphs (the ONLY source for wRC+ -- never computed):
       - splitTeam=true  -> per-level wRC+ on each stint row (current team etc.)
       - splitTeam=false -> season-combined wRC+ on the season's TOTAL row
     reusing the SAME Playwright/Cloudflare path as fetch_farm_stats.py.
  4. shape it as { seasons:[{year,stints:[...],combinedWRC}], splits:{...} } and
     upsert to ProspectProfileStats by mlbId (slug is what the profile queries).

Run from the GitHub Action (can reach statsapi.mlb.com AND run Playwright).

--- VERIFY ON FIRST RUN (accuracy, not assumed) -------------------------------
* G and R (hitters) and G/GS/W/L/SV (pitchers) are read straight from the
  summed stat dict lib/mlb_api.py returns. If any come back 0 on a player who
  clearly has games, lib/mlb_api.py isn't summing that field -- spot-check one
  player against MiLB.com before trusting these two columns.
* Splits come from statSplits; the profile re-combines split entries, so per-team
  vs combined both display right. If splits can't be fetched we send splits:null
  and the profile keeps its curated splits.
* wRC+ PER-LEVEL matching (splitTeam=true) maps FanGraphs' level string to our
  codes (AAA/AA/HIA/LOA/FCL/DSLY/DSLB). DSL is ambiguous on FanGraphs (both
  Yankees DSL teams share level "DSL"), so DSL rows are disambiguated by team
  name when present, else left null (the profile then carries over its curated
  wRC+). Verify one promoted hitter's per-level wRC+ on the first run. The
  COMBINED wRC+ (splitTeam=false, name-only match) mirrors fetch_farm_stats.py
  and is the reliable one.
* NON-YANKEES stints: walks Yankees affiliates only. A mid-season pickup will
  have only his Yankees portion in the feed; loadLiveStats replaces the whole
  season, so his curated line is more complete. Decide per player.
-------------------------------------------------------------------------------
"""

import json
import os
import sys
from datetime import date

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.affiliates import AFFILIATES, ROOKIE_AFFILIATES
from lib.mlb_api import get_active_roster, get_player_stats_by_date_range, get_team_schedule

SEASON = int(os.environ.get("SEASON", "2026"))
SEARCH_FROM = f"{SEASON}-02-01"
BASE = "https://statsapi.mlb.com/api/v1"

# team_id -> profile level code + label (stable MLB team ids, same as pipeline)
TEAM_META = {
    531: ("AAA",  "Scranton/Wilkes-Barre RailRiders (Triple-A)"),
    589: ("AA",   "Somerset Patriots (Double-A)"),
    588: ("HIA",  "Hudson Valley Renegades (High-A)"),
    587: ("LOA",  "Tampa Tarpons (Low-A)"),
    475: ("FCL",  "FCL Yankees (Rookie)"),
    635: ("DSLY", "DSL Yankees (Rookie)"),
    634: ("DSLB", "DSL Bombers (Rookie)"),
}

WIX_PUSH_URL = os.environ.get("WIX_PROSPECTS_ENDPOINT")
PUSH_SECRET = os.environ.get("WIX_PUSH_SECRET")

# FanGraphs minor-league advanced batting board, org 9, all levels, no PA min.
FG_LG = "2,4,5,6,7,8,9,10,11,14,12,13,15,16,17,18,30,32"


# ---- numeric helpers (mirror the pipeline) ----------------------------------
def _i(stat, key):
    v = stat.get(key)
    try:
        return int(v) if v not in (None, "-", "") else 0
    except (TypeError, ValueError):
        return 0

def ip_to_outs(ip):
    if ip in (None, ""):
        return 0
    whole, _, frac = str(ip).partition(".")
    return (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)

def outs_to_ip_float(outs):
    return float(f"{outs // 3}.{outs % 3}") if outs else 0.0

def _r3(x): return round(x, 3)
def _r2(x): return round(x, 2)


# ---- stint shaping (profile field names) ------------------------------------
def hitter_stint(level, label, stat):
    ab  = _i(stat, "atBats");   h = _i(stat, "hits")
    d   = _i(stat, "doubles");  t = _i(stat, "triples"); hr = _i(stat, "homeRuns")
    bb  = _i(stat, "baseOnBalls"); hbp = _i(stat, "hitByPitch"); sf = _i(stat, "sacFlies")
    singles = h - d - t - hr
    tb = singles + 2*d + 3*t + 4*hr
    obp_den = ab + bb + hbp + sf
    avg = _r3(h/ab) if ab else 0.0
    obp = _r3((h+bb+hbp)/obp_den) if obp_den else 0.0
    slg = _r3(tb/ab) if ab else 0.0
    return {
        "level": level, "levelLabel": label,
        "G": _i(stat, "gamesPlayed"), "AB": ab, "R": _i(stat, "runs"), "H": h,
        "D": d, "T": t, "HR": hr, "RBI": _i(stat, "rbi"),
        "BB": bb, "SO": _i(stat, "strikeOuts"), "SB": _i(stat, "stolenBases"),
        "AVG": avg, "OBP": obp, "SLG": slg, "OPS": _r3(obp+slg),
        "WRC": None,  # filled from FanGraphs per-level below; null -> profile carries over
    }

def pitcher_stint(level, label, stat):
    outs = ip_to_outs(stat.get("inningsPitched"))
    ip_dec = outs / 3 if outs else 0.0
    er = _i(stat, "earnedRuns"); h = _i(stat, "hits"); bb = _i(stat, "baseOnBalls")
    return {
        "level": level, "levelLabel": label,
        "G": _i(stat, "gamesPlayed"), "GS": _i(stat, "gamesStarted"),
        "W": _i(stat, "wins"), "L": _i(stat, "losses"), "SV": _i(stat, "saves"),
        "IP": outs_to_ip_float(outs),
        "H": h, "ER": er, "BB": bb, "SO": _i(stat, "strikeOuts"),
        "ERA": _r2(er*9/ip_dec) if ip_dec else 0.0,
        "WHIP": _r2((bb+h)/ip_dec) if ip_dec else 0.0,
    }


# ---- season stints via the proven roster path -------------------------------
def opening_day(team_id, sport_id, end_date):
    games = get_team_schedule(team_id, SEARCH_FROM, end_date, sport_id=sport_id)
    dates = [g["gameDate"][:10] for g in games if g.get("gameDate")] if games else []
    return min(dates) if dates else None

def fetch_seasons(players, end_date, names_out):
    """players: {mlbId(int): {slug, group}}. Fills names_out[id]=fullName.
    Returns {mlbId: [stints]}."""
    stints_by_id = {pid: [] for pid in players}
    for cfg in list(AFFILIATES.values()) + list(ROOKIE_AFFILIATES.values()):
        team_id, sport_id = cfg["team_id"], cfg["sport_id"]
        od = opening_day(team_id, sport_id, end_date)
        if not od:
            continue
        level, label = TEAM_META.get(team_id, (None, cfg.get("display_name", "")))
        roster = get_active_roster(team_id, SEASON)
        by_id = {int(e["person"]["id"]): e["person"].get("fullName") for e in roster}
        for pid, meta in players.items():
            if pid not in by_id:
                continue
            names_out.setdefault(pid, by_id[pid])
            group = meta["group"]
            stat = get_player_stats_by_date_range(
                str(pid), group, sport_id, SEASON, od, end_date, team_id=team_id
            )
            if not stat:
                continue
            if group == "hitting":
                if _i(stat, "atBats") <= 0:
                    continue
                stints_by_id[pid].append(hitter_stint(level, label, stat))
            else:
                if ip_to_outs(stat.get("inningsPitched")) <= 0:
                    continue
                stints_by_id[pid].append(pitcher_stint(level, label, stat))
    return stints_by_id


# ---- LH/RH splits via statSplits --------------------------------------------
SIT = {"L": "vl", "R": "vr"}

def _split_rows(pid, group, sit_code):
    params = {"stats": "statSplits", "group": group, "season": SEASON,
              "sportIds": "11,12,13,14,16", "sitCodes": sit_code}
    try:
        r = requests.get(f"{BASE}/people/{pid}/stats", params=params, timeout=30)
        r.raise_for_status()
        blocks = r.json().get("stats", [])
    except Exception:
        return None
    rows = []
    for b in blocks:
        rows.extend(b.get("splits", []))
    return rows

def hitter_split_entry(sp):
    s = sp.get("stat", {})
    ab = _i(s, "atBats"); h = _i(s, "hits")
    d = _i(s, "doubles"); t = _i(s, "triples"); hr = _i(s, "homeRuns")
    bb = _i(s, "baseOnBalls"); hbp = _i(s, "hitByPitch"); sf = _i(s, "sacFlies")
    singles = h - d - t - hr
    tb = singles + 2*d + 3*t + 4*hr
    obp_den = ab + bb + hbp + sf
    avg = _r3(h/ab) if ab else 0.0
    obp = _r3((h+bb+hbp)/obp_den) if obp_den else 0.0
    slg = _r3(tb/ab) if ab else 0.0
    level = TEAM_META.get((sp.get("team") or {}).get("id"), (None, None))[0]
    return {"level": level, "AB": ab, "H": h, "D": d, "HR": hr,
            "BB": bb, "SO": _i(s, "strikeOuts"),
            "AVG": avg, "OBP": obp, "SLG": slg, "OPS": _r3(obp+slg)}

def pitcher_split_entry(sp):
    s = sp.get("stat", {})
    outs = ip_to_outs(s.get("inningsPitched"))
    ab = _i(s, "atBats"); h = _i(s, "hits")
    level = TEAM_META.get((sp.get("team") or {}).get("id"), (None, None))[0]
    return {"level": level, "IP": outs_to_ip_float(outs), "H": h,
            "HR": _i(s, "homeRuns"), "HB": _i(s, "hitByPitch"),
            "BB": _i(s, "baseOnBalls"), "SO": _i(s, "strikeOuts"),
            "AVG": _r3(h/ab) if ab else 0.0}

def fetch_splits(pid, group):
    left = _split_rows(pid, group, SIT["L"])
    right = _split_rows(pid, group, SIT["R"])
    if left is None or right is None:
        return None
    entry = pitcher_split_entry if group == "pitching" else hitter_split_entry
    if group == "pitching":
        return {"vsLHB": [entry(sp) for sp in left], "vsRHB": [entry(sp) for sp in right]}
    return {"vsLHP": [entry(sp) for sp in left], "vsRHP": [entry(sp) for sp in right]}


# ---- FanGraphs wRC+ (only source; per-level + combined) ----------------------
def _fg_api_path(season, split_team):
    lg = FG_LG.replace(",", "%2C")
    st = "true" if split_team else "false"
    return (f"/api/leaders/minor-league/data?pos=all&lg={lg}&stats=bat&qual=0"
            f"&type=1&season={season}&seasonEnd={season}&org=9&ind=0"
            f"&splitTeam={st}&level=0")

def _fetch_fangraphs_rows(season, split_team):
    """Same Cloudflare-clearance-then-in-context-fetch approach as
    fetch_farm_stats.enrich_with_fangraphs(). Returns [] on any failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed; wRC+ will be null (profile carries over).")
        return []
    api_path = _fg_api_path(season, split_team)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
                viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="America/New_York")
            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page.goto("https://www.fangraphs.com/", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(5_000)
            payload = page.evaluate(f"""
                async () => {{
                    const r = await fetch('{api_path}', {{
                        headers: {{'Accept':'application/json',
                                   'Referer':'https://www.fangraphs.com/leaders/minor-league'}} }});
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return await r.json();
                }}""")
            ctx.close(); browser.close()
    except Exception as e:
        print(f"  FanGraphs fetch (splitTeam={split_team}) failed ({e}); wRC+ null for this pass.")
        return []
    return payload.get("data", []) if isinstance(payload, dict) else (payload or [])

def _fg(row, *keys):
    for k in keys:
        if row.get(k) is not None:
            return row[k]
    return None

def _fg_level_to_code(level_str, team_str):
    if not level_str:
        return None
    s = str(level_str).strip().upper()
    direct = {"AAA": "AAA", "AA": "AA", "A+": "HIA", "A": "LOA",
              "R": "FCL", "RK": "FCL", "CPX": "FCL", "FCL": "FCL"}
    if s in direct:
        return direct[s]
    if s.startswith("DSL"):
        # both Yankees DSL clubs share level "DSL" on FanGraphs -> use team name
        if team_str and "bomber" in str(team_str).lower():
            return "DSLB"
        if team_str and "yankee" in str(team_str).lower():
            return "DSLY"
        return None  # ambiguous -> leave null, profile carries over
    return None

def build_wrc_lookups(season):
    """Returns (combined_by_name, perlevel_by_name_level)."""
    combined = {}
    for row in _fetch_fangraphs_rows(season, split_team=False):
        name = (_fg(row, "PlayerName", "Name", "playerName") or "").strip().lower()
        w = _fg(row, "wRC+", "wRCplus", "wrc_plus", "wRCPlus")
        if name and w is not None:
            combined[name] = w
    perlevel = {}
    for row in _fetch_fangraphs_rows(season, split_team=True):
        name = (_fg(row, "PlayerName", "Name", "playerName") or "").strip().lower()
        w = _fg(row, "wRC+", "wRCplus", "wrc_plus", "wRCPlus")
        lvl = _fg(row, "Level", "aLevel", "AbbLevel", "level")
        team = _fg(row, "Team", "TeamName", "team")
        code = _fg_level_to_code(lvl, team)
        if name and w is not None and code:
            perlevel[(name, code)] = w
    print(f"  FanGraphs wRC+: {len(combined)} combined, {len(perlevel)} per-level rows matched.")
    return combined, perlevel


# ---- push -------------------------------------------------------------------
def push(mlb_id, slug, group, seasons, splits):
    payload = {"mlbId": mlb_id, "slug": slug, "group": group, "season": SEASON,
               "stats": {"seasons": seasons}, "splits": splits}
    r = requests.post(WIX_PUSH_URL, json=payload,
                      headers={"Authorization": f"Bearer {PUSH_SECRET}"}, timeout=30)
    r.raise_for_status()
    return r.status_code


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "prospect_profiles.json")) as f:
        entries = json.load(f)["players"]

    players = {int(p["mlbId"]): {"slug": p["slug"], "group": p.get("group", "hitting")}
               for p in entries}
    end_date = date.today().isoformat()
    names = {}

    print(f"Fetching {SEASON} season stints for {len(players)} prospects...")
    stints_by_id = fetch_seasons(players, end_date, names)

    # wRC+ from FanGraphs (one pair of calls, only if any hitters present)
    has_hitters = any(m["group"] == "hitting" for m in players.values())
    combined_wrc, perlevel_wrc = ({}, {})
    if has_hitters:
        print("Pulling wRC+ from FanGraphs (per-level + combined)...")
        combined_wrc, perlevel_wrc = build_wrc_lookups(SEASON)

    ok, empty, failed = 0, [], []
    for pid, meta in players.items():
        slug, group = meta["slug"], meta["group"]
        try:
            stints = stints_by_id.get(pid, [])
            if not stints:
                empty.append(slug)
                continue

            season_obj = {"year": SEASON, "stints": stints}
            if group == "hitting":
                nm = (names.get(pid) or "").strip().lower()
                # per-level wRC+ on each stint (null where unmatched -> carry over)
                for st in stints:
                    w = perlevel_wrc.get((nm, st.get("level")))
                    if w is not None:
                        st["WRC"] = w
                # season-combined wRC+ for the TOTAL row (see renderSeasons edit)
                cw = combined_wrc.get(nm)
                if cw is not None:
                    season_obj["combinedWRC"] = cw

            splits = fetch_splits(pid, group)
            push(pid, slug, group, [season_obj], splits)
            ok += 1
            levels = "/".join(s["level"] or "?" for s in stints)
            flag = "" if splits else "  (splits kept curated)"
            print(f"  \u2713 {slug:26s} {levels}{flag}")
        except Exception as e:
            failed.append((slug, str(e)))
            print(f"  \u2717 {slug:26s} {e}", file=sys.stderr)

    print(f"\nPushed {ok}/{len(players)} profiles for {SEASON}.")
    if empty:
        print(f"No {SEASON} Yankees-affiliate data (didn't play / non-org / wrong group?): "
              f"{', '.join(empty)}")
    if failed:
        print(f"Failed: {', '.join(s for s, _ in failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
