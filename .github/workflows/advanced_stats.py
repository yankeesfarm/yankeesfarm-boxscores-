"""
advanced_stats.py

Advanced hitting/pitching stat calculations for YankeesFarmReport.
Designed to slot into fetch_season_stats.py / fetch_weekly_stats.py / generate_leaderboard.py.

Each function takes a stats dict (matching the shape returned by the MLB Stats API
'stat' object, i.e. keys like atBats, hits, homeRuns, baseOnBalls, strikeOuts, plateAppearances,
inningsPitched, etc.) and returns a float, or None if the sample size is too small / data missing.

All functions return None (not 0 or a crash) when denominators are zero, so downstream
code can decide how to handle small samples (e.g. DSL/FCL early-season players).
"""

from typing import Optional, Dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_div(numerator: float, denominator: float) -> Optional[float]:
    """Returns None instead of raising/inf on divide-by-zero."""
    if denominator is None or denominator == 0:
        return None
    return numerator / denominator


def innings_to_outs(innings_pitched) -> Optional[int]:
    """
    MLB Stats API returns innings pitched as a string like '5.1' or '5.2',
    where the decimal represents OUTS, not tenths (.1 = 1 out, .2 = 2 outs).
    Converts to total outs recorded for accurate math.
    """
    if innings_pitched is None:
        return None
    ip_str = str(innings_pitched)
    if "." in ip_str:
        whole, partial = ip_str.split(".")
        whole = int(whole)
        partial = int(partial)  # 0, 1, or 2 -- already represents outs, not decimal
    else:
        whole = int(ip_str)
        partial = 0
    return (whole * 3) + partial


def outs_to_innings(outs: int) -> float:
    """Converts total outs back to traditional IP notation (e.g. 16 outs -> 5.1)."""
    whole = outs // 3
    partial = outs % 3
    return whole + (partial / 10)


# ---------------------------------------------------------------------------
# Hitting - Tier 1 (pure box score math)
# ---------------------------------------------------------------------------

def calc_bb_rate(stats: Dict) -> Optional[float]:
    """BB% = walks / plate appearances"""
    bb = stats.get("baseOnBalls", 0) or 0
    pa = stats.get("plateAppearances", 0) or 0
    result = _safe_div(bb, pa)
    return round(result, 4) if result is not None else None


def calc_k_rate(stats: Dict) -> Optional[float]:
    """K% = strikeouts / plate appearances"""
    so = stats.get("strikeOuts", 0) or 0
    pa = stats.get("plateAppearances", 0) or 0
    result = _safe_div(so, pa)
    return round(result, 4) if result is not None else None


def calc_k_minus_bb_rate_hitter(stats: Dict) -> Optional[float]:
    """K-BB% for hitters = K% - BB% (lower is better discipline)"""
    k_pct = calc_k_rate(stats)
    bb_pct = calc_bb_rate(stats)
    if k_pct is None or bb_pct is None:
        return None
    return round(k_pct - bb_pct, 4)


def calc_xbh_rate(stats: Dict) -> Optional[float]:
    """XBH% = extra base hits / total hits"""
    doubles = stats.get("doubles", 0) or 0
    triples = stats.get("triples", 0) or 0
    hr = stats.get("homeRuns", 0) or 0
    hits = stats.get("hits", 0) or 0
    xbh = doubles + triples + hr
    result = _safe_div(xbh, hits)
    return round(result, 4) if result is not None else None


def calc_iso(stats: Dict) -> Optional[float]:
    """ISO = SLG - AVG (raw power, independent of average)"""
    slg = stats.get("slg")
    avg = stats.get("avg")
    if slg is None or avg is None:
        return None
    try:
        return round(float(slg) - float(avg), 3)
    except (TypeError, ValueError):
        return None


def calc_babip(stats: Dict) -> Optional[float]:
    """BABIP = (H - HR) / (AB - SO - HR + SF)"""
    h = stats.get("hits", 0) or 0
    hr = stats.get("homeRuns", 0) or 0
    ab = stats.get("atBats", 0) or 0
    so = stats.get("strikeOuts", 0) or 0
    sf = stats.get("sacFlies", 0) or 0

    numerator = h - hr
    denominator = ab - so - hr + sf
    result = _safe_div(numerator, denominator)
    return round(result, 3) if result is not None else None


# ---------------------------------------------------------------------------
# Hitting - Tier 2 (needs league-average weights / constants)
# ---------------------------------------------------------------------------

# Standard wOBA linear weights. These are published MLB-wide constants (recent-year
# averages) since level-specific (DSL/FCL/A/A+/AA) weights aren't publicly available.
# Good enough for relative year-over-year and player-to-player comparison within your
# own dataset -- just don't present these as "FanGraphs official."
DEFAULT_WOBA_WEIGHTS = {
    "wBB": 0.690,
    "wHBP": 0.722,
    "w1B": 0.888,
    "w2B": 1.271,
    "w3B": 1.616,
    "wHR": 2.101,
}


def calc_woba(stats: Dict, weights: Dict = None) -> Optional[float]:
    """
    wOBA = (wBB*BB + wHBP*HBP + w1B*1B + w2B*2B + w3B*3B + wHR*HR)
           / (AB + BB - IBB + SF + HBP)

    Pass a custom `weights` dict to override DEFAULT_WOBA_WEIGHTS if you later
    derive level-specific weights from your own league data.
    """
    w = weights or DEFAULT_WOBA_WEIGHTS

    bb = stats.get("baseOnBalls", 0) or 0
    ibb = stats.get("intentionalWalks", 0) or 0
    hbp = stats.get("hitByPitch", 0) or 0
    hits = stats.get("hits", 0) or 0
    doubles = stats.get("doubles", 0) or 0
    triples = stats.get("triples", 0) or 0
    hr = stats.get("homeRuns", 0) or 0
    ab = stats.get("atBats", 0) or 0
    sf = stats.get("sacFlies", 0) or 0

    singles = hits - doubles - triples - hr

    numerator = (
        w["wBB"] * (bb - ibb)
        + w["wHBP"] * hbp
        + w["w1B"] * singles
        + w["w2B"] * doubles
        + w["w3B"] * triples
        + w["wHR"] * hr
    )
    denominator = ab + bb - ibb + sf + hbp

    result = _safe_div(numerator, denominator)
    return round(result, 3) if result is not None else None


def calc_ops_plus(stats: Dict, league_obp: float, league_slg: float) -> Optional[float]:
    """
    OPS+ = 100 * (OBP/lgOBP + SLG/lgSLG - 1)

    Requires league_obp and league_slg -- the average OBP/SLG across all qualifying
    players at the SAME LEVEL for the SAME SEASON. Compute these once per level/season
    from your leaderboard aggregate data and pass them in here (see
    `calc_league_averages()` below for how to build that lookup).
    """
    obp = stats.get("obp")
    slg = stats.get("slg")
    if obp is None or slg is None or not league_obp or not league_slg:
        return None
    try:
        obp = float(obp)
        slg = float(slg)
        result = 100 * ((obp / league_obp) + (slg / league_slg) - 1)
        return round(result, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def calc_league_averages(all_player_stats: list) -> Dict:
    """
    Builds league-average OBP/SLG/wOBA from a list of qualifying player stat dicts
    (same level, same season). Feed this the qualifying pool from your leaderboard
    pipeline BEFORE per-player filtering, so the average reflects the full league.

    Returns: {"lg_obp": float, "lg_slg": float, "lg_woba": float}

    NOTE: this is also the foundation piece for wRC+ -- once this is confirmed
    working, wRC+ needs one more league constant (lgR/PA, i.e. runs per plate
    appearance league-wide) plus the "wRC+ scaling" step. Flagging that here so
    it's an easy next add rather than a rebuild.
    """
    total_obp_num = 0.0
    total_slg_num = 0.0
    total_woba_num = 0.0
    total_pa = 0
    total_ab = 0
    count = 0

    for stats in all_player_stats:
        pa = stats.get("plateAppearances", 0) or 0
        ab = stats.get("atBats", 0) or 0
        obp = stats.get("obp")
        slg = stats.get("slg")
        woba = calc_woba(stats)

        if pa == 0:
            continue

        if obp is not None:
            total_obp_num += float(obp) * pa
        if slg is not None:
            total_slg_num += float(slg) * ab
        if woba is not None:
            total_woba_num += woba * pa

        total_pa += pa
        total_ab += ab
        count += 1

    if count == 0 or total_pa == 0:
        return {"lg_obp": None, "lg_slg": None, "lg_woba": None}

    return {
        "lg_obp": round(_safe_div(total_obp_num, total_pa) or 0, 3),
        "lg_slg": round(_safe_div(total_slg_num, total_ab) or 0, 3) if total_ab else None,
        "lg_woba": round(_safe_div(total_woba_num, total_pa) or 0, 3),
    }


# ---------------------------------------------------------------------------
# Pitching - Tier 1 (pure box score math)
# ---------------------------------------------------------------------------

def calc_k_rate_pitcher(stats: Dict) -> Optional[float]:
    """K% = strikeouts / total batters faced"""
    so = stats.get("strikeOuts", 0) or 0
    tbf = stats.get("battersFaced", 0) or 0
    result = _safe_div(so, tbf)
    return round(result, 4) if result is not None else None


def calc_bb_rate_pitcher(stats: Dict) -> Optional[float]:
    """BB% = walks / total batters faced"""
    bb = stats.get("baseOnBalls", 0) or 0
    tbf = stats.get("battersFaced", 0) or 0
    result = _safe_div(bb, tbf)
    return round(result, 4) if result is not None else None


def calc_k_minus_bb_rate_pitcher(stats: Dict) -> Optional[float]:
    """K-BB% for pitchers = K% - BB% (higher is better)"""
    k_pct = calc_k_rate_pitcher(stats)
    bb_pct = calc_bb_rate_pitcher(stats)
    if k_pct is None or bb_pct is None:
        return None
    return round(k_pct - bb_pct, 4)


def calc_whip(stats: Dict) -> Optional[float]:
    """WHIP = (BB + H) / IP  -- uses true outs-based IP, not the string decimal"""
    bb = stats.get("baseOnBalls", 0) or 0
    h = stats.get("hits", 0) or 0
    outs = innings_to_outs(stats.get("inningsPitched"))
    if not outs:
        return None
    true_ip = outs / 3
    result = _safe_div(bb + h, true_ip)
    return round(result, 3) if result is not None else None


# ---------------------------------------------------------------------------
# Pitching - Tier 2 (needs a level/season FIP constant)
# ---------------------------------------------------------------------------

def calc_fip_constant(all_pitcher_stats: list, league_era: float) -> Optional[float]:
    """
    FIP constant = lgERA - raw_FIP_without_constant, computed league-wide for
    a given level/season so FIP scales to look like ERA.

    Pass the full qualifying pitcher pool for one level/season, plus that level's
    league ERA (average ERA across the same pool), and this backs out the constant
    to feed into calc_fip() below. Recompute per level per season -- DSL/FCL run
    very different offensive environments than High-A/AA.
    """
    total_hr = total_bb = total_hbp = total_k = total_outs = 0

    for stats in all_pitcher_stats:
        total_hr += stats.get("homeRuns", 0) or 0
        total_bb += stats.get("baseOnBalls", 0) or 0
        total_hbp += stats.get("hitByPitch", 0) or 0
        total_k += stats.get("strikeOuts", 0) or 0
        outs = innings_to_outs(stats.get("inningsPitched"))
        if outs:
            total_outs += outs

    if total_outs == 0 or league_era is None:
        return None

    total_ip = total_outs / 3
    raw_fip = ((13 * total_hr) + (3 * (total_bb + total_hbp)) - (2 * total_k)) / total_ip
    return round(league_era - raw_fip, 2)


def calc_fip(stats: Dict, fip_constant: float) -> Optional[float]:
    """
    FIP = ((13*HR) + (3*(BB+HBP)) - (2*K)) / IP + FIP_constant

    `fip_constant` should come from calc_fip_constant() for the matching
    level/season. If you don't have one yet, 3.10 is a reasonable placeholder
    (recent MLB-wide norm) but will be off for lower levels -- swap it out
    once you've computed a real one.
    """
    hr = stats.get("homeRuns", 0) or 0
    bb = stats.get("baseOnBalls", 0) or 0
    hbp = stats.get("hitByPitch", 0) or 0
    k = stats.get("strikeOuts", 0) or 0
    outs = innings_to_outs(stats.get("inningsPitched"))

    if not outs:
        return None

    true_ip = outs / 3
    raw = ((13 * hr) + (3 * (bb + hbp)) - (2 * k)) / true_ip
    result = raw + fip_constant
    return round(result, 2)


# ---------------------------------------------------------------------------
# Quick self-test / usage example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample_hitter = {
        "plateAppearances": 250,
        "atBats": 220,
        "hits": 66,
        "doubles": 14,
        "triples": 2,
        "homeRuns": 9,
        "baseOnBalls": 24,
        "intentionalWalks": 1,
        "strikeOuts": 55,
        "hitByPitch": 3,
        "sacFlies": 3,
        "avg": ".300",
        "obp": ".375",
        "slg": ".500",
    }

    print("--- Hitting ---")
    print("BB%:", calc_bb_rate(sample_hitter))
    print("K%:", calc_k_rate(sample_hitter))
    print("K-BB%:", calc_k_minus_bb_rate_hitter(sample_hitter))
    print("XBH%:", calc_xbh_rate(sample_hitter))
    print("ISO:", calc_iso(sample_hitter))
    print("BABIP:", calc_babip(sample_hitter))
    print("wOBA:", calc_woba(sample_hitter))

    sample_pitcher = {
        "battersFaced": 300,
        "strikeOuts": 85,
        "baseOnBalls": 28,
        "hits": 62,
        "homeRuns": 7,
        "hitByPitch": 4,
        "inningsPitched": "72.1",
    }

    print("\n--- Pitching ---")
    print("K%:", calc_k_rate_pitcher(sample_pitcher))
    print("BB%:", calc_bb_rate_pitcher(sample_pitcher))
    print("K-BB%:", calc_k_minus_bb_rate_pitcher(sample_pitcher))
    print("WHIP:", calc_whip(sample_pitcher))
    print("FIP (placeholder constant 3.10):", calc_fip(sample_pitcher, 3.10))
