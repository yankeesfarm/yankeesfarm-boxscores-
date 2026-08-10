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


# Lowest level to highest -- used by combine_by_id() to pick which team a
# combined player is displayed under (his most advanced level reached), and
# to build a readable multi-level list in ascending order.
LEVEL_ORDER = [
    "dsl_yankees", "dsl_bombers", "fcl_yankees",
    "tampa", "hudson_valley", "somerset", "scranton_wb",
]


def _level_rank(team_key):
    try:
        return LEVEL_ORDER.index(team_key)
    except ValueError:
        return -1


def combine_by_id(records, count_fields, label="records", verbose=True):
    """Unlike dedupe_by_id() above (which keeps only the FIRST entry it
    sees for a player and silently drops the rest), this SUMS the given
    counting fields across every entry for the same player id.

    Why this exists: dedupe_by_id() was built on the assumption that two
    entries for the same player in the same window are identical duplicates
    (e.g. an API quirk during a mid-week promotion) -- so keeping either one
    was harmless. That assumption turned out to be wrong for a player who
    genuinely played meaningful time at MULTIPLE levels in the same window
    (e.g. promoted from Hudson Valley to Somerset partway through the
    season): each team's stat pull only returns his stats AT THAT LEVEL, so
    dedupe_by_id() was silently discarding real production, not a
    duplicate. combine_by_id() adds his numbers together instead of
    throwing half of them away.

    The merged row is displayed under his most advanced level reached
    (e.g. "somerset"), with the full list of levels he played kept in
    teams_played for anyone who wants to show progression later."""
    combined = {}
    for r in records:
        pid = r["id"]
        if pid not in combined:
            combined[pid] = dict(r)
            combined[pid]["_teams_played"] = {r.get("team")}
        else:
            for f in count_fields:
                combined[pid][f] = combined[pid].get(f, 0) + r.get(f, 0)
            combined[pid]["_teams_played"].add(r.get("team"))

    merged_count = 0
    for row in combined.values():
        teams = row.pop("_teams_played")
        if len(teams) > 1:
            merged_count += 1
            row["team"] = max(teams, key=_level_rank)
            row["teams_played"] = sorted(teams, key=_level_rank)
        else:
            row["teams_played"] = list(teams)

    if merged_count and verbose:
        print(f"  NOTE: combined {merged_count} {label} across multiple affiliate "
              f"levels this season (promotions/demotions) -- their counting "
              f"totals now reflect their FULL season, not just one team.")
    return list(combined.values())
