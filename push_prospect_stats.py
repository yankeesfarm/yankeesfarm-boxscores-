#!/usr/bin/env python3
"""
push_prospect_stats.py

Keeps YankeesFarm prospect-profile stat lines current automatically.

Flow:
  prospect_profiles.json (slug -> mlbId, maintained by hand, once per player)
    -> fetch each player's COMBINED season line across all 7 affiliate levels
    -> fetch vs-LHP/RHP (or vs-LHB/RHB) platoon splits, combined across levels
    -> compute advanced stats inline (standalone — nothing to import)
    -> push one row per mlbId to the Wix ProspectProfileStats collection

The profile page then reads its own line + splits from that collection by mlbId.
No more screenshots.

Reuses the same MLB Stats API behavior already documented in the repo:
  - sportIds passed explicitly so it never silently defaults to MLB-level data
  - counting stats summed across levels for promoted players
  - innings-pitched "72.1" outs format handled via innings_to_outs()
"""

import json
import os
import sys
import requests


def innings_to_outs(ip):
    """'72.1' -> 217 outs (.1/.2 are outs, not tenths)."""
    if ip in (None, ""):
        return 0
    whole, _, frac = str(ip).partition(".")
    return int(whole) * 3 + (int(frac) if frac else 0)


def _r3(x):
    """Round rate stats to 3 decimals, guarding divide-by-zero."""
    return round(x, 3) if x is not None else None


def hitter_advanced(s):
    """BB%, K%, K-BB%, XBH%, ISO, BABIP, wOBA + slash line from counting totals.
    wOBA uses standard weights (FanGraphs, no-IBB approximation). If you want
    these identical to the rest of the site, swap this body for a call into
    your advanced_stats.py."""
    ab   = s.get("atBats", 0)
    h    = s.get("hits", 0)
    bb   = s.get("baseOnBalls", 0)
    hbp  = s.get("hitByPitch", 0)
    sf   = s.get("sacFlies", 0)
    so   = s.get("strikeOuts", 0)
    d    = s.get("doubles", 0)
    t    = s.get("triples", 0)
    hr   = s.get("homeRuns", 0)
    tb   = s.get("totalBases", 0)
    pa   = s.get("plateAppearances", 0) or (ab + bb + hbp + sf)
    singles = h - d - t - hr
    xbh  = d + t + hr
    obp_den = ab + bb + hbp + sf
    babip_den = ab - so - hr + sf
    woba_den = ab + bb + sf + hbp
    woba_num = (0.690 * bb + 0.722 * hbp + 0.888 * singles +
                1.271 * d + 1.616 * t + 2.101 * hr)
    out = {
        "avg":   _r3(h / ab) if ab else None,
        "obp":   _r3((h + bb + hbp) / obp_den) if obp_den else None,
        "slg":   _r3(tb / ab) if ab else None,
        "iso":   _r3((tb / ab) - (h / ab)) if ab else None,
        "babip": _r3((h - hr) / babip_den) if babip_den else None,
        "bb_pct":   _r3(bb / pa) if pa else None,
        "k_pct":    _r3(so / pa) if pa else None,
        "k_bb_pct": _r3((so - bb) / pa) if pa else None,
        "xbh_pct":  _r3(xbh / h) if h else None,   # XBH as share of hits
        "woba":  _r3(woba_num / woba_den) if woba_den else None,
    }
    out["ops"] = _r3((out["obp"] or 0) + (out["slg"] or 0))
    return out


# FIP constant. A single league value is used because a line combined across
# levels has no one home level. Set per-season if you like; ~3.10 is typical.
FIP_CONSTANT = float(os.environ.get("FIP_CONSTANT", "3.10"))


def pitcher_advanced(s):
    """K%, BB%, K-BB%, FIP + ERA/WHIP from counting totals. FIP uses a single
    league constant (see FIP_CONSTANT); swap in your per-level cFIP via
    advanced_stats.py if you want it site-consistent."""
    outs = s.get("outs", 0)
    ip   = outs / 3 if outs else 0
    bf   = s.get("battersFaced", 0)
    so   = s.get("strikeOuts", 0)
    bb   = s.get("baseOnBalls", 0)
    hr   = s.get("homeRuns", 0)
    h    = s.get("hits", 0)
    er   = s.get("earnedRuns", 0)
    return {
        "era":  _r3(9 * er / ip) if ip else None,
        "whip": _r3((bb + h) / ip) if ip else None,
        "k_pct":    _r3(so / bf) if bf else None,
        "bb_pct":   _r3(bb / bf) if bf else None,
        "k_bb_pct": _r3((so - bb) / bf) if bf else None,
        "fip":  _r3((13 * hr + 3 * bb - 2 * so) / ip + FIP_CONSTANT) if ip else None,
    }

# --- Config ---------------------------------------------------------------
SEASON = int(os.environ.get("SEASON", "2026"))

# All seven affiliate levels. sportIds are passed on every call.
SPORT_IDS = [11, 12, 13, 14, 16]  # AAA, AA, A+, A, and Rk (FCL + both DSL)

# Platoon splits. Meaning depends on group:
#   hitter : vl = vs LHP, vr = vs RHP
#   pitcher: vl = vs LHB, vr = vs RHB
SIT_CODES = {"vs_lh": "vl", "vs_rh": "vr"}

HITTING_SUM = ["gamesPlayed", "plateAppearances", "atBats", "runs", "hits",
               "doubles", "triples", "homeRuns", "rbi", "baseOnBalls",
               "strikeOuts", "stolenBases", "hitByPitch", "sacFlies",
               "totalBases"]
PITCHING_SUM = ["gamesPlayed", "gamesStarted", "wins", "losses", "saves",
                "hits", "runs", "earnedRuns", "homeRuns", "baseOnBalls",
                "strikeOuts", "battersFaced"]

STATS_URL = "https://statsapi.mlb.com/api/v1/people/{mlbId}/stats"

# Wix push. Endpoint URL comes from a secret (same pattern as your other
# pushes); auth reuses WIX_PUSH_SECRET. The handler defines this route.
WIX_PUSH_URL = os.environ.get("WIX_PROSPECTS_ENDPOINT")
PUSH_SECRET = os.environ.get("WIX_PUSH_SECRET")


def _get_splits(mlb_id, group, stats_type, sit_code=None):
    """Raw API call returning the list of per-level split rows."""
    params = {
        "stats": stats_type,
        "group": group,
        "season": SEASON,
        "sportIds": ",".join(map(str, SPORT_IDS)),  # never defaults to MLB
    }
    if sit_code:
        params["sitCodes"] = sit_code
    resp = requests.get(STATS_URL.format(mlbId=mlb_id), params=params, timeout=30)
    resp.raise_for_status()
    rows = []
    for block in resp.json().get("stats", []):
        rows.extend(block.get("splits", []))
    return rows


def _combine(rows, group):
    """Sum counting stats across every level into one line. Recomputed rate
    stats off these totals ARE the AB-weighted average across stints."""
    if not rows:
        return None
    fields = HITTING_SUM if group == "hitting" else PITCHING_SUM
    combined = {f: 0 for f in fields}
    outs, levels = 0, []
    for sp in rows:
        stat = sp.get("stat", {})
        for f in fields:
            combined[f] += int(stat.get(f, 0) or 0)
        if group == "pitching":
            outs += innings_to_outs(stat.get("inningsPitched"))
        lvl = sp.get("sport", {}).get("abbreviation") or sp.get("league", {}).get("name")
        if lvl:
            levels.append(lvl)
    if group == "pitching":
        combined["outs"] = outs
        combined["inningsPitched"] = f"{outs // 3}.{outs % 3}"
    combined["levels"] = sorted(set(levels))
    return combined


def fetch_combined_line(mlb_id, group):
    """One summed season line across every level the player appeared at."""
    return _combine(_get_splits(mlb_id, group, "season"), group)


def fetch_splits(mlb_id, group):
    """Return {'vs_lh': line, 'vs_rh': line} combined across levels.
    Low levels (FCL/DSL) sometimes return no split rows — those come back None."""
    out = {}
    for key, code in SIT_CODES.items():
        out[key] = _combine(_get_splits(mlb_id, group, "statSplits", code), group)
    return out


def with_advanced(line, group):
    """Layer advanced stats onto a combined counting line."""
    if group == "hitting":
        line.update(hitter_advanced(line))
    else:
        line.update(pitcher_advanced(line))
    return line


def push(mlb_id, slug, group, line):
    """Upsert one prospect's stat row into Wix, keyed by mlbId."""
    payload = {
        "mlbId": mlb_id,
        "slug": slug,
        "group": group,
        "season": SEASON,
        "stats": line,
        "splits": line.pop("_splits", {}),  # {vs_lh, vs_rh}, each may be null
    }
    resp = requests.post(
        WIX_PUSH_URL,
        json=payload,
        headers={"Authorization": f"Bearer {PUSH_SECRET}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.status_code


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "prospect_profiles.json")) as f:
        players = json.load(f)["players"]

    ok, empty, failed = 0, [], []
    for p in players:
        slug, mlb_id, group = p["slug"], p["mlbId"], p.get("group", "hitting")
        try:
            line = fetch_combined_line(mlb_id, group)
            if not line:
                empty.append(slug)
                continue
            line = with_advanced(line, group)
            # platoon splits, advanced stats layered onto each side
            splits = fetch_splits(mlb_id, group)
            line["_splits"] = {
                k: with_advanced(v, group) if v else None
                for k, v in splits.items()
            }
            push(mlb_id, slug, group, line)
            ok += 1
            has_splits = any(line["_splits"].get(k) for k in SIT_CODES)
            flag = "" if has_splits else "  (no split data)"
            print(f"  ✓ {slug:24s} {'/'.join(line['levels'])}{flag}")
        except Exception as e:  # keep going; one bad player shouldn't kill the run
            failed.append((slug, str(e)))
            print(f"  ✗ {slug:24s} {e}", file=sys.stderr)

    print(f"\nPushed {ok}/{len(players)} profiles for {SEASON}.")
    if empty:
        print(f"No {SEASON} data (didn't play / wrong group?): {', '.join(empty)}")
    if failed:
        print(f"Failed: {', '.join(s for s, _ in failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
