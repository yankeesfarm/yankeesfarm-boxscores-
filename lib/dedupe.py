"""
Shared dedupe logic. A player promoted/demoted mid-week shows up on two
team rosters; since byDateRange stats are scoped to the PLAYER not the team,
both lookups return identical numbers for that window. If a duplicate isn't
removed before stats get summed (in accumulate_monthly.py) or rendered
(in generate_leaderboard.py), that player's week gets double-counted or
listed twice under two teams.

Called defensively in fetch_weekly_stats.py, accumulate_monthly.py, and
generate_leaderboard.py -- not just once upstream -- so a bug or a
hand-edited data file in any one stage can't silently corrupt totals
downstream.
"""


def dedupe_by_id(records, label="records", verbose=True):
    seen = {}
    dupes_found = []
    for r in records:
        if r["id"] in seen:
            dupes_found.append(r["id"])
            continue
        seen[r["id"]] = r
    if dupes_found and verbose:
        print(f"  NOTE: deduped {len(dupes_found)} duplicate {label} entr"
              f"{'y' if len(dupes_found) == 1 else 'ies'} (likely mid-week "
              f"promotion/demotion): {sorted(set(dupes_found))}")
    return list(seen.values())
