#!/usr/bin/env python3
"""
Render Top-10 leaderboard tables (weekly or month-to-date) as Markdown --
ready to paste into an Instagram caption draft or a website recap post.

Usage:
    python generate_leaderboard.py --file data/weekly/week_2026-08-03.json --min-ab 5 --min-ip 3
    python generate_leaderboard.py --file data/monthly/2026-07.json --min-ab 15 --min-ip 15
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.dedupe import combine_by_id

# Raw counting fields to sum when combine_by_id() merges a player's stats
# across multiple affiliate levels (promotions/demotions, or a rookie-level
# + full-season split). Matches the field names fetch_weekly_stats.py and
# fetch_season_stats.py already write into each hitter row.
HITTING_COUNT_FIELDS = [
    "atBats", "hits", "doubles", "triples", "homeRuns", "rbi",
    "baseOnBalls", "hitByPitch", "sacFlies", "stolenBases", "caughtStealing", "strikeOuts",
]

HITTING_CATEGORIES = [
    ("avg", "Batting Average", True),
    ("obp", "On-Base Percentage", True),
    ("slg", "Slugging Percentage", True),
    ("ops", "OPS", True),
    ("hits", "Hits", False),
    ("doubles", "Doubles", False),
    ("triples", "Triples", False),
    ("homeRuns", "Home Runs", False),
    ("rbi", "RBI", False),
    ("stolenBases", "Stolen Bases", False),
]

# These six counting categories also include DSL/FCL (Rookie-level) hitters,
# on top of the four full-season affiliates. Rate categories (AVG/OBP/SLG/
# OPS) and all pitching categories do NOT -- DSL/FCL play much shorter,
# unusually-scheduled seasons, so a rate stat there can be wildly misleading
# on a tiny sample, while a raw counting total is still meaningful.
ROOKIE_LEVEL_INCLUDED_FIELDS = {"hits", "doubles", "triples", "homeRuns", "rbi", "stolenBases"}

# field, label, is_rate_stat, lower_is_better
PITCHING_CATEGORIES = [
    ("era", "ERA", True, True),
    ("whip", "WHIP", True, True),
    ("ip_outs", "Innings Pitched", False, False),
    ("strikeOuts", "Strikeouts", False, False),
    ("avg", "AVG Against", True, True),
    ("so9", "SO/9", True, False),
]

# Raw counting fields to sum when combine_by_id() merges a pitcher's stats
# across multiple affiliate levels. "ip_outs" (innings pitched, as whole
# outs) is included here rather than the raw "inningsPitched" string field
# -- outs are summable integers, the "6.1" display string is not. main()
# is responsible for converting inningsPitched -> ip_outs on each RAW
# per-team row BEFORE combining, so the innings actually add up correctly.
PITCHING_COUNT_FIELDS = [
    "ip_outs", "strikeOuts", "baseOnBalls", "hits", "atBats", "homeRuns",
    "earnedRuns", "runs", "wins", "losses", "saves",
]


def recompute_pitching_rates(row):
    """Same principle as recompute_hitting_rates() above: after
    combine_by_id() sums a promoted/demoted pitcher's innings and earned
    runs etc. across levels, his era/whip/so9/avg-against must be
    recalculated from those combined totals, or his ERA would still
    reflect only whichever single level's row happened to survive
    before."""
    ip_outs = row.get("ip_outs", 0)
    ip_decimal = ip_outs / 3
    row["era"] = round((row.get("earnedRuns", 0) * 9) / ip_decimal, 2) if ip_decimal else 0.0
    row["whip"] = round((row.get("hits", 0) + row.get("baseOnBalls", 0)) / ip_decimal, 2) if ip_decimal else 0.0
    row["so9"] = round((row.get("strikeOuts", 0) * 9) / ip_decimal, 2) if ip_decimal else 0.0
    row["avg"] = round(row.get("hits", 0) / row["atBats"], 3) if row.get("atBats") else 0.0
    return row

TEAM_LABELS = {
    "tampa": "TAM", "hudson_valley": "HV", "somerset": "SOM", "scranton_wb": "SWB",
    "fcl_yankees": "FCL", "dsl_yankees": "DSL-Y", "dsl_bombers": "DSL-B",
}


def recompute_hitting_rates(row):
    """Same principle as fetch_season_stats.py's version of this function:
    never trust a rate stat that wasn't derived from this row's own actual
    counting stats. This matters here specifically because combine_by_id()
    just summed a promoted player's at-bats/hits across multiple levels --
    his OLD avg/obp/slg/ops (from whichever single team's row happened to
    survive before) are now stale and must be recalculated from the
    combined totals, or his batting average would still reflect only one
    of the levels he played at."""
    ab = row.get("atBats", 0)
    h = row.get("hits", 0)
    bb = row.get("baseOnBalls", 0)
    hbp = row.get("hitByPitch", 0)
    sf = row.get("sacFlies", 0)
    doubles, triples, hr = row.get("doubles", 0), row.get("triples", 0), row.get("homeRuns", 0)
    singles = h - doubles - triples - hr
    tb = singles + 2 * doubles + 3 * triples + 4 * hr

    row["avg"] = round(h / ab, 3) if ab else 0.0
    pa_ob = ab + bb + hbp + sf
    row["pa"] = pa_ob  # plate appearances -- the real qualifying metric for
                        # a batting/OBP/SLG/OPS title, used by top_n() below
                        # instead of raw at-bats.
    row["obp"] = round((h + bb + hbp) / pa_ob, 3) if pa_ob else 0.0
    row["slg"] = round(tb / ab, 3) if ab else 0.0
    row["ops"] = round(row["obp"] + row["slg"], 3)
    return row


def fmt_rate(val, field):
    if field in ("era", "whip", "so9"):
        return f"{val:.2f}"
    return f"{val:.3f}"


def fmt_ip(outs):
    return f"{outs // 3}.{outs % 3}"


def top_n(rows, field, min_qual, qual_field, lower_is_better=False, n=10):
    """Returns the top n ranked spots -- NOT necessarily exactly n players.
    If multiple players are tied for the last qualifying spot, all of them
    are included, matching the tie labels ("T7", "T4", etc.) that
    ranked_entries() below applies. A hard cutoff at position n would
    otherwise arbitrarily drop some players tied with others who made the
    list, purely based on array order -- that's the bug this fixes."""
    pool = [r for r in rows if r.get(field) is not None]
    if qual_field:
        pool = [r for r in pool if r.get(qual_field, 0) >= min_qual]
    pool.sort(key=lambda r: r[field], reverse=not lower_is_better)
    if len(pool) <= n:
        return pool
    cutoff_value = pool[n - 1][field]
    extended = pool[:n]
    for r in pool[n:]:
        if r[field] == cutoff_value:
            extended.append(r)
        else:
            break
    return extended


def build_payload(hitters, pitchers, min_ab, min_ip_outs, meta, rookie_hitters=None):
    """Structured leaderboard data -- the single source of truth consumed by
    both the Markdown renderer (below) and push_to_wix.py. Keeping one
    ranking implementation means the website and the Instagram drafts can
    never quietly disagree with each other.

    rookie_hitters (DSL/FCL) are merged in ONLY for the six counting
    categories in ROOKIE_LEVEL_INCLUDED_FIELDS -- rate categories and all
    pitching categories use `hitters` alone.

    That merge uses combine_by_id(), not a plain list concatenation: a
    player who played at both a rookie-level affiliate AND a full-season
    affiliate (e.g. promoted from FCL to Tampa) exists as two separate
    partial rows -- one in each list. Concatenating them would either show
    him twice with two different partial totals, or let each partial total
    individually miss a leaderboard cutoff his TRUE combined total would
    have made. combine_by_id() unifies him into one row first."""
    rookie_hitters = rookie_hitters or []
    counting_pool = combine_by_id(hitters + rookie_hitters, HITTING_COUNT_FIELDS,
                                   "hitters (full-season + rookie-level combined)")
    # NOTE: pitchers arrive here with ip_outs already computed AND already
    # combined across levels by main() -- recomputing inningsPitched -> ip_outs
    # again in this function would read each row's original single-team
    # "inningsPitched" string and silently overwrite the correctly-combined
    # total with a stale partial one. Don't add that block back here.

    categories = {}

    for field, label, is_rate in HITTING_CATEGORIES:
        min_qual = min_ab if is_rate else 0
        pool = counting_pool if field in ROOKIE_LEVEL_INCLUDED_FIELDS else hitters
        leaders = top_n(pool, field, min_qual, "pa" if is_rate else None)
        categories[f"hitting_{field}"] = {
            "label": label, "group": "hitting", "isRate": is_rate,
            "entries": ranked_entries(leaders, field, is_rate),
        }

    for field, label, is_rate, lower in PITCHING_CATEGORIES:
        min_qual = min_ip_outs if is_rate else 0
        qual_field = "ip_outs" if is_rate else None
        leaders = top_n(pitchers, field, min_qual, qual_field, lower_is_better=lower)
        if field == "ip_outs":
            entries = [
                {"rank": str(i), "isTied": False, "name": r["name"],
                 "team": TEAM_LABELS.get(r["team"], r["team"]), "value": fmt_ip(r["ip_outs"])}
                for i, r in enumerate(leaders, 1)
            ]
        else:
            entries = ranked_entries(leaders, field, is_rate)
        categories[f"pitching_{field}"] = {"label": label, "group": "pitching", "isRate": is_rate, "entries": entries}

    return {
        "meta": meta,
        "generatedAt": None,  # filled in by push_to_wix.py at send time (actual push time, not build time)
        "categories": categories,
    }


def ranked_entries(leaders, field, is_rate):
    values = [r[field] for r in leaders]
    entries = []
    for i, r in enumerate(leaders):
        val = r[field]
        rank = i + 1
        while rank > 1 and values[rank - 2] == val:
            rank -= 1
        is_tied = values.count(val) > 1
        entries.append({
            "rank": (f"T{rank}" if is_tied else str(rank)),
            "isTied": is_tied,
            "name": r["name"],
            "team": TEAM_LABELS.get(r["team"], r["team"]),
            "value": fmt_rate(val, field) if is_rate else val,
        })
    return entries


def render_table(leaders, field, is_rate):
    """Standard competition ranking (1, 2, 2, 4 -- ties share a rank, next
    rank skips ahead), with a 'T' prefix on tied rows so ties are obvious
    at a glance in the Instagram-ready output."""
    lines = ["| # | Player | Team | Value |", "|---|--------|------|-------|"]
    for e in ranked_entries(leaders, field, is_rate):
        lines.append(f"| {e['rank']} | {e['name']} | {e['team']} | {e['value']} |")
    return "\n".join(lines)


def render_hitting(rows, min_ab, rookie_rows=None):
    rookie_rows = rookie_rows or []
    counting_pool = combine_by_id(rows + rookie_rows, HITTING_COUNT_FIELDS,
                                   "hitters (full-season + rookie-level combined)")
    out = ["## HITTING LEADERS\n"]
    for field, label, is_rate in HITTING_CATEGORIES:
        min_qual = min_ab if is_rate else 0
        pool = counting_pool if field in ROOKIE_LEVEL_INCLUDED_FIELDS else rows
        leaders = top_n(pool, field, min_qual, "pa" if is_rate else None)
        out.append(f"### {label}")
        out.append(render_table(leaders, field, is_rate))
        out.append("")
    return "\n".join(out)


def render_pitching(rows, min_ip_outs):
    # NOTE: rows arrive here with ip_outs already computed AND combined
    # across levels by main() -- see the matching comment in build_payload()
    # for why that recomputation must not happen again in this function.

    out = ["## PITCHING LEADERS\n"]
    for field, label, is_rate, lower in PITCHING_CATEGORIES:
        min_qual = min_ip_outs if is_rate else 0
        leaders = top_n(rows, field, min_qual, "ip_outs" if is_rate else None, lower_is_better=lower)
        out.append(f"### {label}")
        if field == "ip_outs":
            lines = ["| # | Player | Team | Value |", "|---|--------|------|-------|"]
            for i, r in enumerate(leaders, 1):
                team = TEAM_LABELS.get(r["team"], r["team"])
                lines.append(f"| {i} | {r['name']} | {team} | {fmt_ip(r['ip_outs'])} |")
            out.append("\n".join(lines))
        else:
            out.append(render_table(leaders, field, is_rate))
        out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--min-ab", type=int, default=15)
    parser.add_argument("--min-ip", type=float, default=15.0)
    parser.add_argument("--json-out", action="store_true",
                         help="Also write a _wix_payload.json alongside the markdown, "
                              "for push_to_wix.py to consume.")
    args = parser.parse_args()

    with open(args.file) as f:
        data = json.load(f)

    # combine_by_id() (not dedupe_by_id()) for hitters: a player promoted
    # through multiple affiliate levels this season needs his stats SUMMED
    # across every level, not just the first one this list happens to
    # contain. Rates are then recalculated from those combined counts, or
    # his batting average would still reflect only one level.
    data["hitters"] = combine_by_id(data["hitters"], HITTING_COUNT_FIELDS, "hitters")
    for row in data["hitters"]:
        recompute_hitting_rates(row)
    rookie_hitters = combine_by_id(data.get("rookie_hitters", []), HITTING_COUNT_FIELDS, "rookie hitters")

    # Same treatment for pitchers, same reasoning: a pitcher promoted
    # between levels (e.g. Hudson Valley -> Somerset) needs his innings,
    # strikeouts, and earned runs SUMMED, not just kept from whichever
    # team's row survived a plain dedupe. inningsPitched -> ip_outs is
    # converted here, on the RAW per-team rows, BEFORE combining -- ip_outs
    # is a summable integer, the "6.1" display string is not.
    for row in data["pitchers"]:
        ip_val = row.get("inningsPitched")
        if isinstance(ip_val, str) and "." in ip_val:
            whole, frac = ip_val.split(".")
            row["ip_outs"] = int(whole) * 3 + int(frac)
        else:
            row["ip_outs"] = 0
    data["pitchers"] = combine_by_id(data["pitchers"], PITCHING_COUNT_FIELDS, "pitchers")
    for row in data["pitchers"]:
        recompute_pitching_rates(row)

    # Season files carry a real, computed "qualified" threshold (3.1 PA /
    # team game, 1 IP / team game -- the same standard MLB itself uses),
    # derived from each affiliate's actual games played this season. Weekly
    # files don't have this -- "qualified for a weekly title" isn't a real
    # baseball concept -- so those keep using the flat --min-ab/--min-ip
    # CLI defaults, just now measured in PA rather than raw at-bats.
    if "qualifying_pa" in data and "qualifying_ip" in data:
        min_ab = data["qualifying_pa"]
        min_ip_outs = int(round(data["qualifying_ip"] * 3))
        print(f"Using this file's computed qualifying threshold: "
              f"{min_ab} PA / {data['qualifying_ip']} IP "
              f"(derived from {data.get('team_games_played')}).")
    else:
        min_ab = args.min_ab
        min_ip_outs = int(round(args.min_ip * 3))

    title = data.get("month") or f"{data.get('start_date')} to {data.get('end_date')}"
    ip_display = min_ip_outs / 3
    report = [f"# YankeesFarm Stat Leaders — {title}", f"**Source file:** {args.file}",
              f"**Qualifying minimums:** {min_ab} PA / {ip_display} IP", ""]
    report.append(render_hitting(data["hitters"], min_ab, rookie_hitters))
    report.append(render_pitching(data["pitchers"], min_ip_outs))
    report.append("---")
    report.append("*Auto-generated. Verify top names per category against MiLB.com before posting.*")

    out_path = args.file.replace(".json", "_leaderboard.md")
    with open(out_path, "w") as f:
        f.write("\n".join(report))
    print(f"Leaderboard written to {out_path}")

    if args.json_out:
        meta = {
            "title": title,
            "period": "monthly" if data.get("month") else "weekly",
            "sourceFile": args.file,
            "minPA": min_ab,
            "minIP": ip_display,
        }
        payload = build_payload(data["hitters"], data["pitchers"], min_ab, min_ip_outs, meta,
                                 rookie_hitters=rookie_hitters)
        json_path = args.file.replace(".json", "_wix_payload.json")
        with open(json_path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"Wix payload written to {json_path}")


if __name__ == "__main__":
    main()
