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
from lib.dedupe import dedupe_by_id

HITTING_CATEGORIES = [
    ("avg", "Batting Average", True),
    ("obp", "On-Base Percentage", True),
    ("slg", "Slugging Percentage", True),
    ("ops", "OPS", True),
    ("hits", "Hits", False),
    ("doubles", "Doubles", False),
    ("homeRuns", "Home Runs", False),
    ("rbi", "RBI", False),
    ("stolenBases", "Stolen Bases", False),
]

# field, label, is_rate_stat, lower_is_better
PITCHING_CATEGORIES = [
    ("era", "ERA", True, True),
    ("whip", "WHIP", True, True),
    ("ip_outs", "Innings Pitched", False, False),
    ("strikeOuts", "Strikeouts", False, False),
    ("avg", "AVG Against", True, True),
    ("so9", "SO/9", True, False),
]

TEAM_LABELS = {
    "tampa": "TAM", "hudson_valley": "HV", "somerset": "SOM", "scranton_wb": "SWB",
}


def fmt_rate(val, field):
    if field in ("era", "whip", "so9"):
        return f"{val:.2f}"
    return f"{val:.3f}"


def fmt_ip(outs):
    return f"{outs // 3}.{outs % 3}"


def top_n(rows, field, min_qual, qual_field, lower_is_better=False, n=10):
    pool = [r for r in rows if r.get(field) is not None]
    if qual_field:
        pool = [r for r in pool if r.get(qual_field, 0) >= min_qual]
    pool.sort(key=lambda r: r[field], reverse=not lower_is_better)
    return pool[:n]


def render_table(leaders, field, is_rate):
    """Standard competition ranking (1, 2, 2, 4 -- ties share a rank, next
    rank skips ahead), with a 'T' prefix on tied rows so ties are obvious
    at a glance in the Instagram-ready output."""
    lines = ["| # | Player | Team | Value |", "|---|--------|------|-------|"]
    values = [r[field] for r in leaders]
    for i, r in enumerate(leaders):
        val = r[field]
        rank = i + 1  # position in the already-sorted list = competition rank
        # walk back to find the true rank if this value ties an earlier one
        while rank > 1 and values[rank - 2] == val:
            rank -= 1
        is_tied = values.count(val) > 1
        prefix = f"T{rank}" if is_tied else str(rank)
        display = fmt_rate(val, field) if is_rate else val
        team = TEAM_LABELS.get(r["team"], r["team"])
        lines.append(f"| {prefix} | {r['name']} | {team} | {display} |")
    return "\n".join(lines)


def render_hitting(rows, min_ab):
    out = ["## HITTING LEADERS\n"]
    for field, label, is_rate in HITTING_CATEGORIES:
        min_qual = min_ab if is_rate else 0
        leaders = top_n(rows, field, min_qual, "atBats" if is_rate else None)
        out.append(f"### {label}")
        out.append(render_table(leaders, field, is_rate))
        out.append("")
    return "\n".join(out)


def render_pitching(rows, min_ip_outs):
    for row in rows:
        # innings pitched sorts on whole outs, not the "X.Y" display string
        ip_val = row.get("inningsPitched")
        if isinstance(ip_val, str) and "." in ip_val:
            whole, frac = ip_val.split(".")
            row["ip_outs"] = int(whole) * 3 + int(frac)
        else:
            row["ip_outs"] = 0

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
    args = parser.parse_args()

    with open(args.file) as f:
        data = json.load(f)

    data["hitters"] = dedupe_by_id(data["hitters"], "hitter")
    data["pitchers"] = dedupe_by_id(data["pitchers"], "pitcher")

    min_ip_outs = int(round(args.min_ip * 3))

    title = data.get("month") or f"{data.get('start_date')} to {data.get('end_date')}"
    report = [f"# YankeesFarm Stat Leaders — {title}", f"**Source file:** {args.file}",
              f"**Qualifying minimums:** {args.min_ab} AB / {args.min_ip} IP", ""]
    report.append(render_hitting(data["hitters"], args.min_ab))
    report.append(render_pitching(data["pitchers"], min_ip_outs))
    report.append("---")
    report.append("*Auto-generated. Verify top names per category against MiLB.com before posting.*")

    out_path = args.file.replace(".json", "_leaderboard.md")
    with open(out_path, "w") as f:
        f.write("\n".join(report))
    print(f"Leaderboard written to {out_path}")


if __name__ == "__main__":
    main()
