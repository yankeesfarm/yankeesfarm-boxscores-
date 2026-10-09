#!/usr/bin/env python3
"""
Generate prospect.html JavaScript profile entries for players that don't
have profiles yet.  Fetches bio + stats + splits + monthly trends from the
MLB Stats API (requires network access — designed to run in GitHub Actions,
NOT from a blocked cloud environment).

Outputs a single JS file (data/profiles/new_profiles.js) containing all
profile entries ready to paste into prospect.html's PLAYERS object.

Usage:
    python generate_profiles.py                         # all registry players without existing profile
    python generate_profiles.py --slug dom-hamel         # single player
    python generate_profiles.py --only-missing           # only players listed in --missing-slugs-file
    python generate_profiles.py --missing-slugs-file slugs.txt  # custom file of slugs (one per line)
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.mlb_api import _get, get_player_splits_by_hand

SEASON = int(os.environ.get("SEASON", 2026))

# ---------- reused from push_prospect_profiles.py ----------

SPORT_TO_LEVEL = {11: "AAA", 12: "AA", 13: "HIA", 14: "LOA", 16: "FCL"}

TEAM_LABELS = {
    531: ("AAA", "SWB RailRiders (Triple-A)"),
    1956: ("AA", "Somerset Patriots (Double-A)"),
    537: ("HIA", "Hudson Valley Renegades (High-A)"),
    587: ("LOA", "Tampa Tarpons (Low-A)"),
    475: ("FCL", "FCL Yankees (Rookie)"),
    635: ("DSLY", "DSL Yankees (Rookie)"),
    634: ("DSLB", "DSL Bombers (Rookie)"),
}

LEVEL_LABELS = {
    "AAA": "Triple-A · SWB RailRiders",
    "AA": "Double-A · Somerset Patriots",
    "HIA": "High-A · Hudson Valley Renegades",
    "LOA": "Low-A · Tampa Tarpons",
    "FCL": "Rookie · FCL Yankees",
    "DSLY": "Rookie · DSL Yankees",
    "DSLB": "Rookie · DSL Bombers",
}

ALL_SPORT_IDS = [11, 12, 13, 14, 16]

MONTH_NAMES = {"03": "MAR", "04": "APR", "05": "MAY", "06": "JUN",
               "07": "JUL", "08": "AUG", "09": "SEP", "10": "OCT"}


def innings_to_outs(ip_str):
    whole, _, frac = str(ip_str).partition(".")
    return (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)


def outs_to_innings_str(outs):
    return f"{outs // 3}.{outs % 3}"


# ---------- bio ----------

def fetch_bio(person_id):
    """Fetch player bio from /people/{id} endpoint."""
    data = _get(f"/people/{person_id}", {"hydrate": "draft"})
    people = data.get("people", [])
    if not people:
        return None
    return people[0]


def format_height(h):
    """Convert API height like \"6' 4\\\"\" to \"6'4\" """
    if not h:
        return None
    return h.replace(" ", "").replace('"', '').replace("\\\"", "")


def format_birth_date(birth_date_str):
    """Convert 1999-04-15 to 'Apr 15, 1999'"""
    if not birth_date_str:
        return None
    try:
        d = datetime.strptime(birth_date_str, "%Y-%m-%d")
        return d.strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return birth_date_str


def compute_age(birth_date_str):
    if not birth_date_str:
        return None
    try:
        bd = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
        today = date.today()
        return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    except ValueError:
        return None


def format_hometown(bio):
    """Build hometown string from birthCity, birthStateProvince, birthCountry."""
    city = bio.get("birthCity", "")
    state = bio.get("birthStateProvince", "")
    country = bio.get("birthCountry", "")
    if country == "USA":
        parts = [p for p in [city, state] if p]
        return ", ".join(parts) if parts else None
    else:
        parts = [p for p in [city, country] if p]
        return ", ".join(parts) if parts else None


def format_draft(bio):
    """Extract draft info if available."""
    draft_list = bio.get("drafts", [])
    if not draft_list:
        return None
    d = draft_list[0]
    year = d.get("year", "")
    rd = d.get("pickRound", "")
    pick = d.get("pickNumber", "")
    team_code = d.get("team", {}).get("abbreviation", "")
    if year and rd and pick:
        return f"{year} · Round {rd} · Pick {pick} ({team_code})"
    return None


def format_college_or_school(bio):
    """Get college or last school."""
    # Check draft info for school
    draft_list = bio.get("drafts", [])
    if draft_list:
        school = draft_list[0].get("school", {}).get("name")
        if school:
            return school
    return None


# ---------- stats (reused from push_prospect_profiles.py) ----------

def get_year_by_year(person_id, group):
    data = _get(f"/people/{person_id}/stats", {
        "stats": "yearByYear", "group": group, "season": SEASON,
    })
    stats_list = data.get("stats", [])
    return stats_list[0].get("splits", []) if stats_list else []


def get_game_log(person_id, group, season):
    data = _get(f"/people/{person_id}/stats", {
        "stats": "gameLog", "group": group, "season": season,
    })
    stats_list = data.get("stats", [])
    return stats_list[0].get("splits", []) if stats_list else []


def build_seasons_hitter(splits):
    by_year = defaultdict(list)
    for split in splits:
        year = split.get("season")
        stat = split.get("stat", {})
        team_info = split.get("team", {})
        team_id = team_info.get("id")
        sport_id = (split.get("sport") or {}).get("id")
        if not year or not stat or sport_id == 1:
            continue
        ab = int(stat.get("atBats", 0) or 0)
        if ab == 0:
            continue
        level_code, level_label = TEAM_LABELS.get(team_id, (
            SPORT_TO_LEVEL.get(sport_id, "UNK"),
            f"{team_info.get('name', 'Unknown')} ({split.get('league', {}).get('name', '')})"
        ))
        h = int(stat.get("hits", 0) or 0)
        bb = int(stat.get("baseOnBalls", 0) or 0)
        hbp = int(stat.get("hitByPitch", 0) or 0)
        sf = int(stat.get("sacFlies", 0) or 0)
        doubles = int(stat.get("doubles", 0) or 0)
        triples = int(stat.get("triples", 0) or 0)
        hr = int(stat.get("homeRuns", 0) or 0)
        singles = h - doubles - triples - hr
        tb = singles + 2 * doubles + 3 * triples + 4 * hr
        pa = ab + bb + hbp + sf
        avg = round(h / ab, 3) if ab else 0
        obp = round((h + bb + hbp) / pa, 3) if pa else 0
        slg = round(tb / ab, 3) if ab else 0
        ops = round(obp + slg, 3)
        by_year[year].append({
            "level": level_code, "levelLabel": level_label,
            "G": int(stat.get("gamesPlayed", 0) or 0), "AB": ab,
            "R": int(stat.get("runs", 0) or 0), "H": h,
            "D": doubles, "T": triples, "HR": hr,
            "RBI": int(stat.get("rbi", 0) or 0), "BB": bb,
            "SO": int(stat.get("strikeOuts", 0) or 0),
            "SB": int(stat.get("stolenBases", 0) or 0),
            "AVG": avg, "OBP": obp, "SLG": slg, "OPS": ops, "WRC": None,
        })
    return [{"year": int(y), "stints": by_year[y]} for y in sorted(by_year)]


def build_seasons_pitcher(splits):
    by_year = defaultdict(list)
    for split in splits:
        year = split.get("season")
        stat = split.get("stat", {})
        team_info = split.get("team", {})
        team_id = team_info.get("id")
        sport_id = (split.get("sport") or {}).get("id")
        if not year or not stat or sport_id == 1:
            continue
        ip_raw = stat.get("inningsPitched", "0")
        ip_outs = innings_to_outs(ip_raw)
        if ip_outs == 0:
            continue
        level_code, level_label = TEAM_LABELS.get(team_id, (
            SPORT_TO_LEVEL.get(sport_id, "UNK"),
            f"{team_info.get('name', 'Unknown')} ({split.get('league', {}).get('name', '')})"
        ))
        ip_decimal = ip_outs / 3
        h = int(stat.get("hits", 0) or 0)
        er = int(stat.get("earnedRuns", 0) or 0)
        bb = int(stat.get("baseOnBalls", 0) or 0)
        so = int(stat.get("strikeOuts", 0) or 0)
        era = round((er * 9) / ip_decimal, 2) if ip_decimal else 0
        whip = round((h + bb) / ip_decimal, 2) if ip_decimal else 0
        # Compute Kpct: BF ≈ round(IP*3) + H + BB, kpct = SO/BF × 100
        bf = ip_outs + h + bb
        kpct = round((so / bf) * 100, 1) if bf else 0
        by_year[year].append({
            "level": level_code, "levelLabel": level_label,
            "G": int(stat.get("gamesPlayed", 0) or 0),
            "GS": int(stat.get("gamesStarted", 0) or 0),
            "W": int(stat.get("wins", 0) or 0),
            "L": int(stat.get("losses", 0) or 0),
            "SV": int(stat.get("saves", 0) or 0),
            "IP": float(ip_raw), "H": h, "ER": er, "BB": bb, "SO": so,
            "ERA": era, "WHIP": whip, "Kpct": kpct,
        })
    return [{"year": int(y), "stints": by_year[y]} for y in sorted(by_year)]


def build_splits_hitter(person_id):
    combined = {}
    for sid in ALL_SPORT_IDS:
        try:
            raw = get_player_splits_by_hand(person_id, "hitting", sid, SEASON)
        except Exception:
            continue
        for code in ("vr", "vl"):
            if code in raw and code not in combined:
                combined[code] = raw[code]
            elif code in raw:
                for k, v in raw[code].items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        combined[code][k] = combined[code].get(k, 0) + v
    result = {}
    for code, key in [("vr", "vsRHP"), ("vl", "vsLHP")]:
        if code not in combined:
            continue
        s = combined[code]
        ab = int(s.get("atBats", 0) or 0)
        h = int(s.get("hits", 0) or 0)
        result[key] = [{"level": str(SEASON), "AB": ab, "H": h,
            "D": int(s.get("doubles", 0) or 0), "HR": int(s.get("homeRuns", 0) or 0),
            "BB": int(s.get("baseOnBalls", 0) or 0), "SO": int(s.get("strikeOuts", 0) or 0),
            "AVG": round(h / ab, 3) if ab else 0}]
    return result


def build_splits_pitcher(person_id):
    combined = {}
    for sid in ALL_SPORT_IDS:
        try:
            raw = get_player_splits_by_hand(person_id, "pitching", sid, SEASON)
        except Exception:
            continue
        for code in ("vr", "vl"):
            if code in raw and code not in combined:
                combined[code] = raw[code]
            elif code in raw:
                for k, v in raw[code].items():
                    if k == "inningsPitched":
                        existing_outs = innings_to_outs(combined[code].get(k, "0"))
                        new_outs = innings_to_outs(v)
                        combined[code][k] = outs_to_innings_str(existing_outs + new_outs)
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        combined[code][k] = combined[code].get(k, 0) + v
    result = {}
    for code, key in [("vl", "vsLHB"), ("vr", "vsRHB")]:
        if code not in combined:
            continue
        s = combined[code]
        ip_raw = s.get("inningsPitched", "0")
        ip_outs = innings_to_outs(ip_raw)
        ip_dec = ip_outs / 3
        ab = int(s.get("atBats", 0) or 0)
        h = int(s.get("hits", 0) or 0)
        bb = int(s.get("baseOnBalls", 0) or 0)
        so = int(s.get("strikeOuts", 0) or 0)
        result[key] = [{"level": str(SEASON), "IP": float(ip_raw), "H": h,
            "HR": int(s.get("homeRuns", 0) or 0), "BB": bb, "SO": so,
            "AVG": round(h / ab, 3) if ab else 0,
            "WHIP": round((h + bb) / ip_dec, 2) if ip_dec else 0}]
    return result


def build_monthly_trend_hitter(person_id):
    games = get_game_log(person_id, "hitting", SEASON)
    by_month = defaultdict(lambda: defaultdict(int))
    for g in games:
        game_date = g.get("date", "")[:10]
        if not game_date:
            continue
        mk = game_date[5:7]
        stat = g.get("stat", {})
        for f in ["atBats", "hits", "doubles", "triples", "homeRuns",
                   "stolenBases", "baseOnBalls", "strikeOuts", "hitByPitch", "sacFlies"]:
            by_month[mk][f] += int(stat.get(f, 0) or 0)
    trend = []
    for m in sorted(by_month):
        d = by_month[m]
        ab = d["atBats"]
        if ab == 0:
            continue
        pa = ab + d["baseOnBalls"] + d["hitByPitch"] + d["sacFlies"]
        trend.append({"month": MONTH_NAMES.get(m, m),
            "avg": round(d["hits"] / ab, 3), "doubles": d["doubles"],
            "hr": d["homeRuns"], "sb": d["stolenBases"],
            "bb_pct": round((d["baseOnBalls"] / pa) * 100, 1) if pa else 0,
            "k_pct": round((d["strikeOuts"] / pa) * 100, 1) if pa else 0})
    return trend


def build_monthly_trend_pitcher(person_id):
    games = get_game_log(person_id, "pitching", SEASON)
    by_month = defaultdict(lambda: {"ip_outs": 0, "hits": 0, "earnedRuns": 0,
                                     "baseOnBalls": 0, "strikeOuts": 0})
    for g in games:
        game_date = g.get("date", "")[:10]
        if not game_date:
            continue
        mk = game_date[5:7]
        stat = g.get("stat", {})
        by_month[mk]["ip_outs"] += innings_to_outs(stat.get("inningsPitched", "0"))
        for f in ["hits", "earnedRuns", "baseOnBalls", "strikeOuts"]:
            by_month[mk][f] += int(stat.get(f, 0) or 0)
    trend = []
    for m in sorted(by_month):
        d = by_month[m]
        if d["ip_outs"] == 0:
            continue
        ip_dec = d["ip_outs"] / 3
        # Compute Kpct for trend
        bf = d["ip_outs"] + d["hits"] + d["baseOnBalls"]
        trend.append({"month": MONTH_NAMES.get(m, m),
            "era": round((d["earnedRuns"] * 9) / ip_dec, 2),
            "whip": round((d["hits"] + d["baseOnBalls"]) / ip_dec, 2),
            "k9": round((d["strikeOuts"] * 9) / ip_dec, 2),
            "bb9": round((d["baseOnBalls"] * 9) / ip_dec, 2),
            "kpct": round((d["strikeOuts"] / bf) * 100, 1) if bf else 0})
    return trend


# ---------- ticker ----------

def build_ticker_hitter(seasons):
    """Build ticker string from most recent season's combined stats."""
    if not seasons:
        return None
    latest = seasons[-1]["stints"]
    # Combine all stints in most recent year
    totals = {"AB": 0, "H": 0, "BB": 0, "HR": 0, "SB": 0,
              "D": 0, "T": 0, "SO": 0, "HBP": 0, "SF": 0}
    for s in latest:
        for k in totals:
            totals[k] += s.get(k, 0) or 0
    ab = totals["AB"]
    if ab == 0:
        return None
    h = totals["H"]
    bb = totals["BB"]
    pa = ab + bb + totals.get("HBP", 0) + totals.get("SF", 0)
    doubles = totals["D"]
    triples = totals["T"]
    hr = totals["HR"]
    singles = h - doubles - triples - hr
    tb = singles + 2 * doubles + 3 * triples + 4 * hr
    avg = h / ab if ab else 0
    obp = (h + bb) / pa if pa else 0
    slg = tb / ab if ab else 0
    ops = obp + slg
    return f".{avg:.3f}"[1:] + f" AVG | .{obp:.3f}"[1:] + f" OBP | .{slg:.3f}"[1:] + f" SLG | .{ops:.3f}"[1:] + f" OPS | {hr} HR | {totals['SB']} SB"


def build_ticker_pitcher(seasons):
    """Build ticker from most recent season's combined stats."""
    if not seasons:
        return None
    latest = seasons[-1]["stints"]
    ip_outs = 0
    totals = {"H": 0, "BB": 0, "SO": 0, "ER": 0}
    for s in latest:
        ip_outs += innings_to_outs(str(s.get("IP", 0)))
        for k in totals:
            totals[k] += s.get(k, 0) or 0
    ip_dec = ip_outs / 3
    if ip_dec == 0:
        return None
    era = round((totals["ER"] * 9) / ip_dec, 2)
    whip = round((totals["H"] + totals["BB"]) / ip_dec, 2)
    ip_str = outs_to_innings_str(ip_outs)
    # Compute AVG against: need AB. Approximate BF = ip_outs + H + BB
    bf = ip_outs + totals["H"] + totals["BB"]
    ab_approx = bf - totals["BB"]  # rough
    avg_against = round(totals["H"] / ab_approx, 3) if ab_approx else 0
    return f"{era:.2f} ERA | {whip:.2f} WHIP | {ip_str} IP | {totals['H']} H | {totals['BB']} BB | {totals['SO']} K | .{avg_against:.3f}"[:-1] + f"{avg_against:.3f}"[-3:] + " AVG"


# ---------- current level ----------

# Affiliate code mapping from the directory
DIRECTORY_AFFILIATES = {}  # Will be populated from registry context


def determine_current_level(seasons, affiliate_code):
    """Determine the player's current level from their most recent stint."""
    if seasons:
        latest_stints = seasons[-1]["stints"]
        if latest_stints:
            last_stint = latest_stints[-1]
            code = last_stint["level"]
            label = LEVEL_LABELS.get(code, last_stint.get("levelLabel", ""))
            return {"code": code, "label": label, "logo": None}
    # Fall back to directory affiliate code
    if affiliate_code:
        label = LEVEL_LABELS.get(affiliate_code, affiliate_code)
        return {"code": affiliate_code, "label": label, "logo": None}
    return {"code": "UNK", "label": "Unknown", "logo": None}


# ---------- JS output ----------

def js_val(v):
    """Format a Python value as JavaScript literal."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        # Escape single quotes
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1000:
            return str(int(v))
        return f"{v}"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        items = ",\n".join("        " + js_obj(x) if isinstance(x, dict) else f"        {js_val(x)}" for x in v)
        return f"[\n{items}\n      ]"
    if isinstance(v, dict):
        return js_obj(v)
    return str(v)


def js_obj(d, indent=0):
    """Convert dict to JS object literal."""
    prefix = "  " * indent
    parts = []
    for k, v in d.items():
        parts.append(f"{k}:{js_val(v)}")
    return "{ " + ", ".join(parts) + " }"


def format_profile_js(slug, profile):
    """Format a single profile as a JS object entry for PLAYERS."""
    bio = profile["bio"]
    cl = profile["currentLevel"]
    lines = []
    lines.append(f'  "{slug}": {{')
    lines.append(f'    type: "{profile["type"]}",')
    lines.append(f'    bio: {{')
    lines.append(f'      fullName: "{bio["fullName"]}",')
    lines.append(f'      displayFirst: "{bio["displayFirst"]}",')
    lines.append(f'      displayLast: "{bio["displayLast"]}",')
    lines.append(f'      jerseyNumber: {js_val(bio.get("jerseyNumber"))},')
    lines.append(f'      position: "{bio["position"]}",')
    lines.append(f'      bats: "{bio["bats"]}", throws: "{bio["throws"]}",')
    lines.append(f'      height: "{bio["height"]}", weight: "{bio["weight"]}",')
    lines.append(f'      age: {bio["age"]},')
    lines.append(f'      hometown: {js_val(bio.get("hometown"))},')
    lines.append(f'      born: {js_val(bio.get("born"))},')

    signed_via = bio.get("signedVia", "unknown")
    lines.append(f'      signedVia: "{signed_via}",')

    if bio.get("college"):
        lines.append(f'      college: "{bio["college"]}",')

    lines.append(f'      headshotUrl: "{bio["headshotUrl"]}",')

    if bio.get("draft"):
        lines.append(f'      draft: "{bio["draft"]}"')
    else:
        lines.append(f'      draft: null')

    lines.append(f'    }},')
    lines.append(f'    currentLevel: {{ code: "{cl["code"]}", label: "{cl["label"]}", logo: null }},')

    ticker = profile.get("ticker")
    lines.append(f'    ticker: {js_val(ticker)},')

    # Seasons
    seasons_parts = []
    for season in profile["seasons"]:
        stint_parts = []
        for stint in season["stints"]:
            stint_parts.append("        " + js_obj(stint))
        stints_str = ",\n".join(stint_parts)
        seasons_parts.append(f'      {{ year:{season["year"]}, stints:[\n{stints_str}\n      ] }}')
    if seasons_parts:
        lines.append(f'    seasons: [\n' + ",\n".join(seasons_parts) + '\n    ],')
    else:
        lines.append(f'    seasons: [],')

    # Monthly trend
    if profile["monthlyTrend"]:
        trend_parts = [f'      {js_obj(t)}' for t in profile["monthlyTrend"]]
        lines.append(f'    monthlyTrend: [\n' + ",\n".join(trend_parts) + '\n    ],')
    else:
        lines.append(f'    monthlyTrend: [],')

    lines.append(f'    meta: {{ views: 0, lastUpdated: "{datetime.now(timezone.utc).isoformat()}" }},')
    lines.append(f'    trophies: [],')

    # Splits
    splits = profile.get("splits", {})
    if profile["type"] == "pitcher":
        vsLHB = splits.get("vsLHB", [])
        vsRHB = splits.get("vsRHB", [])
        lhb_str = "[" + ", ".join(js_obj(s) for s in vsLHB) + "]" if vsLHB else "[]"
        rhb_str = "[" + ", ".join(js_obj(s) for s in vsRHB) + "]" if vsRHB else "[]"
        lines.append(f'    splits: {{')
        lines.append(f'      vsLHB: {lhb_str},')
        lines.append(f'      vsRHB: {rhb_str}')
        lines.append(f'    }}')
    else:
        vsRHP = splits.get("vsRHP", [])
        vsLHP = splits.get("vsLHP", [])
        rhp_str = "[" + ", ".join(js_obj(s) for s in vsRHP) + "]" if vsRHP else "[]"
        lhp_str = "[" + ", ".join(js_obj(s) for s in vsLHP) + "]" if vsLHP else "[]"
        lines.append(f'    splits: {{')
        lines.append(f'      vsRHP: {rhp_str},')
        lines.append(f'      vsLHP: {lhp_str}')
        lines.append(f'    }}')

    lines.append(f'  }},')
    return "\n".join(lines)


# ---------- main ----------

def build_profile(slug, info, affiliate_code=None):
    """Build a complete profile dict for one player."""
    pid = info["personId"]
    ptype = info["type"]

    # Fetch bio
    bio_raw = fetch_bio(pid)
    if not bio_raw:
        print(f"    WARNING: No bio data from API for {slug} (pid={pid})")
        return None

    # Bio fields
    full_name = bio_raw.get("fullFMLName") or bio_raw.get("fullName", f'{info["displayFirst"]} {info["displayLast"]}')
    first_name = info["displayFirst"]
    last_name = info["displayLast"]

    pos = bio_raw.get("primaryPosition", {}).get("abbreviation", "P")
    bats = bio_raw.get("batSide", {}).get("code", "R")
    throws = bio_raw.get("pitchHand", {}).get("code", "R")
    height = format_height(bio_raw.get("height"))
    weight = bio_raw.get("weight")
    birth_date = bio_raw.get("birthDate")
    born_str = format_birth_date(birth_date)
    age = compute_age(birth_date)
    hometown = format_hometown(bio_raw)
    jersey = bio_raw.get("primaryNumber")
    if jersey:
        try:
            jersey = int(jersey)
        except (ValueError, TypeError):
            jersey = None

    draft = format_draft(bio_raw)
    college = format_college_or_school(bio_raw)
    signed_via = "draft" if draft else "ifa"

    headshot_url = f"https://img.mlbstatic.com/mlb-photos/image/upload/ar_1:1,c_pad,q_auto:best/w_180/v1/people/{pid}/headshot/milb/current"

    bio = {
        "fullName": full_name,
        "displayFirst": first_name,
        "displayLast": last_name,
        "jerseyNumber": jersey,
        "position": pos,
        "bats": bats,
        "throws": throws,
        "height": height or "N/A",
        "weight": weight or "N/A",
        "age": age,
        "hometown": hometown,
        "born": born_str,
        "signedVia": signed_via,
        "college": college,
        "headshotUrl": headshot_url,
        "draft": draft,
    }

    # Stats
    if ptype == "hitter":
        raw_splits = get_year_by_year(pid, "hitting")
        seasons = build_seasons_hitter(raw_splits)
        splits = build_splits_hitter(pid)
        monthly = build_monthly_trend_hitter(pid)
        ticker = build_ticker_hitter(seasons)
    else:
        raw_splits = get_year_by_year(pid, "pitching")
        seasons = build_seasons_pitcher(raw_splits)
        splits = build_splits_pitcher(pid)
        monthly = build_monthly_trend_pitcher(pid)
        ticker = build_ticker_pitcher(seasons)

    current_level = determine_current_level(seasons, affiliate_code)

    return {
        "type": ptype,
        "bio": bio,
        "currentLevel": current_level,
        "ticker": ticker,
        "seasons": seasons,
        "monthlyTrend": monthly,
        "splits": splits,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate prospect.html profile JS entries")
    parser.add_argument("--slug", help="Process only this player slug")
    parser.add_argument("--only-missing", action="store_true",
                        help="Only process slugs listed in --missing-slugs-file")
    parser.add_argument("--missing-slugs-file", default="data/profiles/missing_slugs.txt",
                        help="File with one slug per line (default: data/profiles/missing_slugs.txt)")
    parser.add_argument("--registry", default="config/prospect_registry.json")
    parser.add_argument("--output", default="data/profiles/new_profiles.js",
                        help="Output JS file path")
    parser.add_argument("--directory-data", default=None,
                        help="JSON file mapping slug → affiliateCode (for currentLevel fallback)")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    reg_path = os.path.join(script_dir, args.registry)
    with open(reg_path) as f:
        registry = json.load(f)

    # Determine which slugs to process
    if args.slug:
        slugs = [args.slug]
    elif args.only_missing:
        mf = os.path.join(script_dir, args.missing_slugs_file)
        if not os.path.exists(mf):
            print(f"ERROR: Missing slugs file not found: {mf}")
            sys.exit(1)
        with open(mf) as f:
            slugs = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    else:
        slugs = list(registry.keys())

    # Load directory data for affiliate codes if provided
    affiliate_map = {}
    if args.directory_data:
        dp = os.path.join(script_dir, args.directory_data)
        if os.path.exists(dp):
            with open(dp) as f:
                affiliate_map = json.load(f)

    out_path = os.path.join(script_dir, args.output)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    results = []
    total = len(slugs)
    success, failed = 0, 0

    for i, slug in enumerate(slugs, 1):
        if slug not in registry:
            print(f"[{i}/{total}] {slug}: SKIP (not in registry)")
            continue

        info = registry[slug]
        pid = info.get("personId")
        if not pid:
            print(f"[{i}/{total}] {slug}: SKIP (no personId)")
            continue

        print(f"[{i}/{total}] {slug} (pid={pid}, {info['type']})...")

        try:
            affiliate_code = affiliate_map.get(slug)
            profile = build_profile(slug, info, affiliate_code)
            if profile:
                js = format_profile_js(slug, profile)
                results.append(js)
                success += 1
                print(f"    OK: {len(profile['seasons'])} seasons, {len(profile.get('splits', {}))} split sides, {len(profile['monthlyTrend'])} months")
            else:
                failed += 1
        except Exception as e:
            print(f"    ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

        # Rate limiting
        time.sleep(0.3)

    # Write output
    header = f"// Generated {datetime.now(timezone.utc).isoformat()}\n"
    header += f"// {success} profiles generated, {failed} failed\n"
    header += "// Paste these entries into the PLAYERS object in prospect.html\n"
    header += "// (right before the closing '};')\n\n"

    with open(out_path, "w") as f:
        f.write(header)
        f.write("\n".join(results))

    print(f"\nDone: {success} profiles written to {out_path}, {failed} failed")

    # Also save as JSON for the push pipeline
    json_path = out_path.replace(".js", ".json")
    with open(json_path, "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(),
                    "count": success, "failed": failed}, f, indent=2)


if __name__ == "__main__":
    main()
