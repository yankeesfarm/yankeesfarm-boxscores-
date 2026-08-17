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

v3: adds XBH%, BABIP, wOBA, and K-BB% for hitters, and K-BB% and FIP for
pitchers, using the same advanced_stats.py module the leaderboard/season
pipeline already uses. These are computed INLINE inside
build_hitter_row()/build_pitcher_row(), while the raw counting stats
(atBats, hits, doubles, etc.) from extract_stat_group() are still in
scope -- the final row dict never carried those raw fields forward before,
so a separate post-processing pass over the finished rows wouldn't have
had anything to compute from.

FIP needed two supporting changes: extract_stat_group()'s pitching totals
were missing homeRuns and hitByPitch entirely (never summed before,
because nothing needed them until now), and FIP's own constant must be
computed per LEVEL (not org-wide) since offensive environments differ
sharply between e.g. DSL and AAA -- see apply_fip_by_level() below, called
once in main() after every pitcher row is built.

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
# a real constant. 3.10 is a recent MLB-wide norm, not level-accurate, but
# safer than skipping FIP for that level entirely.
FALLBACK_FIP_CONSTANT = 3.10

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


MINOR_LEAGUE_SPORT_IDS = list(SPORT_LEVELS.keys())  # [11, 12, 13, 14, 16] -- deliberately excludes 1 (MLB)


def fetch_hydrated_roster(team_id, season, own_sport_id):
    """
    One call per team: roster entries come back with each player's season
    hitting AND pitching stat blocks already attached via hydrate, scoped
    to the team's own sportId.

    NOTE on cross-level combining (e.g. a player promoted High-A -> AA
    mid-season): this version deliberately does NOT attempt to combine
    stats across levels. Two earlier attempts at that broke in different
    ways without a way to verify the real API response shape from this
    environment (no live access to statsapi.mlb.com from this sandbox).
    Rather than ship a third blind guess, this reverts to the version
    confirmed working across all 7 levels, at the cost of only showing a
    promoted/demoted player's CURRENT level's stats, not his combined
    season line. See the bottom of this file for how to verify and
    re-enable combining safely.
    """
    params = {
        "rosterType": "active",
        "hydrate": f"person(stats(type=season,group=[hitting,pitching],season={season},sportId={own_sport_id}))",
    }
    data = get(f"{BASE}/teams/{team_id}/roster", params=params)
    return data.get("roster", [])


def _ip_to_outs(ip_str):
    """
    Convert MLB's traditional innings-pitched notation (e.g. "62.2" meaning
    62 and 2/3 innings) into whole outs. The digit after the decimal is
    THIRDS of an inning, not a decimal fraction -- .1 = 1 out, .2 = 2 outs,
    NOT .1 = 0.1 innings. Summing this field directly (as a float) across
    multiple stints would silently produce wrong totals, which is exactly
    the kind of error this whole rewrite exists to avoid.
    """
    if not ip_str:
        return 0
    whole, _, frac = str(ip_str).partition(".")
    whole = int(whole or 0)
    frac_outs = int(frac) if frac else 0  # 0, 1, or 2
    return whole * 3 + frac_outs


def _outs_to_ip_display(outs):
    """Inverse of _ip_to_outs -- back to traditional "62.2" notation."""
    return float(f"{outs // 3}.{outs % 3}")


def extract_stat_group(person, group_name):
    """
    Sum raw counting stats for 'hitting' or 'pitching' across EVERY minor
    league split a player has this season (i.e. across a mid-season
    promotion/demotion), then derive rate stats (AVG/OBP/SLG/OPS or
    ERA/WHIP/K9/BB9) from those true combined totals ourselves.

    Deliberately does NOT trust any single split's precomputed rate stats,
    and does NOT trust an "aggregate" split if the API happens to provide
    one -- summing every team-level split ourselves is the only way to be
    sure MLB time never sneaks into the total, since we explicitly only
    ever request minor-league sportIds in the first place.

    hitting totals now also sum intentionalWalks (used by calc_woba() for
    a slightly more accurate wOBA -- unintentional vs intentional walks
    carry different value). pitching totals now also sum homeRuns and
    hitByPitch (both required by calc_fip()) -- neither was summed before
    because nothing needed them until FIP was added.
    """
    for block in person.get("stats", []):
        if block.get("group", {}).get("displayName") != group_name:
            continue
        splits = block.get("splits", [])
        # Only sum splits that represent an actual team stint. A split
        # without a 'team' key is sometimes a pre-aggregated total the API
        # itself computed -- skip those and sum from the real per-team
        # entries instead, so the math is fully ours and fully auditable.
        team_splits = [s for s in splits if "team" in s]
        if not team_splits:
            team_splits = splits  # fall back if the API only gave one, teamless split
        if not team_splits:
            return None

        if group_name == "hitting":
            totals = {"atBats": 0, "hits": 0, "doubles": 0, "triples": 0, "homeRuns": 0,
                      "rbi": 0, "baseOnBalls": 0, "hitByPitch": 0, "sacFlies": 0,
                      "strikeOuts": 0, "stolenBases": 0, "plateAppearances": 0,
                      "intentionalWalks": 0}
            for s in team_splits:
                stat = s.get("stat", {})
                for key in totals:
                    totals[key] += stat.get(key) or 0
            singles = totals["hits"] - totals["doubles"] - totals["triples"] - totals["homeRuns"]
            total_bases = singles + 2 * totals["doubles"] + 3 * totals["triples"] + 4 * totals["homeRuns"]
            ab, h, bb, hbp, sf = (totals["atBats"], totals["hits"], totals["baseOnBalls"],
                                   totals["hitByPitch"], totals["sacFlies"])
            obp_denom = ab + bb + hbp + sf
            obp = round((h + bb + hbp) / obp_denom, 3) if obp_denom else 0.0
            slg = round(total_bases / ab, 3) if ab else 0.0
            return {
                **totals,
                "avg": round(h / ab, 3) if ab else 0.0,
                "obp": obp,
                "slg": slg,
                "ops": round(obp + slg, 3),
            }

        else:  # pitching
            totals = {"wins": 0, "losses": 0, "earnedRuns": 0, "hits": 0,
                      "baseOnBalls": 0, "strikeOuts": 0, "battersFaced": 0,
                      "homeRuns": 0, "hitByPitch": 0}
            total_outs = 0
            for s in team_splits:
                stat = s.get("stat", {})
                for key in totals:
                    totals[key] += stat.get(key) or 0
                total_outs += _ip_to_outs(stat.get("inningsPitched"))
            true_ip = total_outs / 3 if total_outs else 0.0
            return {
                **totals,
                "inningsPitched": _outs_to_ip_display(total_outs),
                "inningsPitchedOuts": total_outs,  # raw outs, used by calc_fip() below --
                                                    # avoids any float round-trip risk on
                                                    # the display-formatted "62.2" value
                "era": round(9 * totals["earnedRuns"] / true_ip, 2) if true_ip else 0.0,
                "whip": round((totals["baseOnBalls"] + totals["hits"]) / true_ip, 2) if true_ip else 0.0,
                "strikeoutsPer9Inn": round(9 * totals["strikeOuts"] / true_ip, 1) if true_ip else 0.0,
                "walksPer9Inn": round(9 * totals["baseOnBalls"] / true_ip, 1) if true_ip else 0.0,
            }
    return None


def pct(n, d):
    return round(100 * n / d, 1) if d else 0.0


def bio_fields(person):
    """
    Bio fields from the SAME person object already returned by the roster
    hydrate -- no new API call, no new hydrate syntax, nothing that can
    break the working roster/stats fetch. Every field here is read
    defensively (.get with a fallback) so a missing field just shows blank
    on the profile page rather than crashing the whole run.
    """
    birth_city = person.get("birthCity")
    birth_state = person.get("birthStateProvince")
    birth_country = person.get("birthCountry")
    hometown_parts = [p for p in (birth_city, birth_state) if p]
    hometown = ", ".join(hometown_parts)
    if birth_country and birth_country not in ("USA",):
        hometown = f"{hometown}, {birth_country}" if hometown else birth_country

    draft_year = person.get("draftYear")
    # MLB's basic person object reliably includes draftYear for drafted
    # players and omits it for international signees -- there isn't a
    # clean "IFA" flag available without a separate, unverified API call,
    # so this is a reasonable inference, not a guarantee. Flagged as such
    # in the UI (see profile.html) rather than stated as fact.
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


def build_hitter_row(person, stat, level_id):
    pa = stat.get("plateAppearances") or 0
    bb = stat.get("baseOnBalls") or 0
    so = stat.get("strikeOuts") or 0
    avg = float(stat.get("avg") or 0)
    slg = float(stat.get("slg") or 0)
    bbp = pct(bb, pa)
    kp = pct(so, pa)

    # XBH%, BABIP, and wOBA computed here (not in a later pass) because
    # `stat` -- the raw counting-stat dict from extract_stat_group() -- is
    # only ever in scope at this call site; the row dict returned below
    # never carries atBats/hits/doubles/etc. forward on its own.
    xbh_pct = calc_xbh_rate(stat)
    babip = calc_babip(stat)
    woba = calc_woba(stat)

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
        "bbp": bbp,
        "kp": kp,
        "kbb": round(kp - bbp, 1),  # K-BB%, same 0-100 scale as bbp/kp above
        "iso": round(slg - avg, 3),
        "xbhp": round(xbh_pct * 100, 1) if xbh_pct is not None else None,  # same 0-100 scale as bbp/kp
        "babip": babip,   # kept as a .358-style fraction, matching AVG/OBP/SLG convention
        "woba": woba,     # kept as a .379-style fraction, matching AVG/OBP/SLG convention
        # Statcast fields -- not available from this endpoint; left null
        # rather than fabricated. See enrich_with_statcast() below.
        "maxev": None,
        "barrelp": None,
        "gbp": None,
        "fbp": None,
        **bio_fields(person),
    }


def build_pitcher_row(person, stat, level_id):
    bf = stat.get("battersFaced") or 0
    so = stat.get("strikeOuts") or 0
    bb = stat.get("baseOnBalls") or 0
    kp = pct(so, bf)
    bbp = pct(bb, bf)

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
        "kp": kp,
        "bbp": bbp,
        "kbb": round(kp - bbp, 1),  # K-BB%, same 0-100 scale as kp/bbp above
        # FIP is intentionally NOT set here -- it needs a level-wide
        # constant that only exists after every pitcher at this level has
        # been built. See apply_fip_by_level() in main(), which fills this
        # in as a second pass. Raw fields it needs are stashed below so
        # that second pass doesn't need to re-fetch or re-derive anything.
        "fip": None,
        "_homeRuns": stat.get("homeRuns", 0),
        "_hitByPitch": stat.get("hitByPitch", 0),
        "_inningsPitchedOuts": stat.get("inningsPitchedOuts", 0),
        "_strikeOuts": so,
        "_baseOnBalls": bb,
        **bio_fields(person),
    }


def apply_fip_by_level(pitchers):
    """
    Computes FIP for every pitcher, grouped by level (AAA/AA/A+/A/ROK/
    DSL1/DSL2) -- a level-specific FIP constant, not one org-wide constant,
    since offensive environments differ sharply between e.g. DSL and AAA.
    Same approach used in fetch_season_stats.py and generate_leaderboard.py
    for the weekly/season leaderboard pipeline.

    Cleans up the temporary "_"-prefixed raw fields build_pitcher_row()
    stashed on each row afterward -- those exist only to make this
    function possible without a second data-fetch pass, not as fields
    analytics.html should ever see or display.
    """
    by_level = {}
    for row in pitchers:
        by_level.setdefault(row["level"], []).append(row)

    for level, rows in by_level.items():
        # Build the small dicts calc_fip_constant()/calc_fip() actually
        # expect (homeRuns, baseOnBalls, hitByPitch, strikeOuts,
        # inningsPitched-as-string), from the stashed raw fields.
        fip_inputs = []
        for row in rows:
            outs = row["_inningsPitchedOuts"]
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
                  f"level '{level}' -- could not compute a real per-level constant "
                  f"(likely too few qualifying pitchers).")

        for row, fip_input in zip(rows, fip_inputs):
            row["fip"] = calc_fip(fip_input, fip_constant)

    # Strip the temporary raw fields now that FIP is set -- they were
    # never meant to reach data.json / analytics.html.
    for row in pitchers:
        for key in ("_homeRuns", "_hitByPitch", "_inningsPitchedOuts", "_strikeOuts", "_baseOnBalls"):
            row.pop(key, None)

    return pitchers


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

    apply_fip_by_level(pitchers)
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


# --- Notes on re-enabling cross-level stat combining ---
#
# To fix "promoted player only shows current level's stats" properly, the
# real MLB Stats API response shape needs to be inspected first -- both
# attempts at this broke in different ways from blind guesses at hydrate
# syntax. Before trying again, run this from a machine that CAN reach
# statsapi.mlb.com and inspect the raw JSON directly:
#
#   https://statsapi.mlb.com/api/v1/people/<Roderick-Arias-mlbId>/stats?
#     stats=season&group=hitting&season=2026&sportId=11,12,13,14,16
#
# Check specifically:
#   1. Does this return multiple splits (one per team), or does it error?
#   2. If multiple splits, does each split include enough info (team id,
#      sport id) to sum them reliably in this script?
#   3. Try the bracket-array form too: sportId=[11,12,13,14,16]
#
# Once the real shape is confirmed, extract_stat_group() can be extended
# to sum across splits -- the summation/rate-recomputation logic already
# in that function is unit-tested and correct; it's only the REQUEST that
# needs a verified-working query.
