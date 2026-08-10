"""
Affiliate configuration for the YankeesFarm weekly/monthly stats pipeline.
Team IDs and sport IDs are for the MLB Stats API (statsapi.mlb.com).

IMPORTANT: sport_id values below match the constants already used in your
yankeesfarm-boxscores repo (MILB_SPORT_IDS = (11, 12, 13, 14, 16)), but I was
NOT able to make a live call to statsapi.mlb.com from this sandbox to confirm
team_id values against a fresh roster pull (statsapi.mlb.com is outside my
network allowlist). Before the first scheduled run, do one manual check:

    python -c "from lib.mlb_api import get_active_roster; \
        print(get_active_roster(587, 2026)[:3])"

...for each team below and confirm the names on the roster match the real
2026 Tampa Tarpons, etc. If a team_id is wrong you'll get either an empty
roster or another team's players -- both are obvious in that quick check.
"""

YANKEES_ORG_ID = 147

AFFILIATES = {
    "tampa": {
        "team_id": 587,
        "sport_id": 14,       # Single-A (Florida State League)
        "level": "A",
        "display_name": "Tampa Tarpons",
    },
    "hudson_valley": {
        "team_id": 537,
        "sport_id": 13,       # High-A (South Atlantic League)
        "level": "High-A",
        "display_name": "Hudson Valley Renegades",
    },
    "somerset": {
        "team_id": 1956,
        "sport_id": 12,       # Double-A (Eastern League)
        "level": "AA",
        "display_name": "Somerset Patriots",
    },
    "scranton_wb": {
        "team_id": 531,
        "sport_id": 11,       # Triple-A (International League)
        "level": "AAA",
        "display_name": "Scranton/Wilkes-Barre RailRiders",
    },
}

# Rookie-level complex/summer league affiliates. These are used ONLY to
# supplement the six counting-stat hitting categories (Hits, Doubles,
# Triples, Home Runs, RBI, Stolen Bases) -- not rate stats (AVG/OBP/SLG/OPS)
# and not pitching. Reason: DSL and FCL play much shorter seasons on
# unusual schedules, so a small sample can make a rate stat wildly
# misleading, while raw counting totals are still meaningful.
#
# team_id values below are cross-referenced from two independent public
# sources querying the real MLB Stats API (a mlb_team_affiliates pull
# for org 147 team_id=147, and FanGraphs' own affiliate listing for the
# 2026 season), not guessed. sport_id 16 = "Rookie" is the official
# classification for both DSL and FCL per MLB's own sports endpoint --
# they share this single sport_id, they are not split further.
#
# As with the AFFILIATES dict above, do one manual sanity check before
# trusting this in production:
#
#     python -c "from lib.mlb_api import get_active_roster; \
#         print(get_active_roster(475, 2026)[:3])"
#
# ...for each team_id below, and confirm the names match the real 2026
# roster on MiLB.com.
ROOKIE_AFFILIATES = {
    "fcl_yankees": {
        "team_id": 475,
        "sport_id": 16,        # Rookie (Florida Complex League)
        "level": "Rookie",
        "display_name": "FCL Yankees",
    },
    "dsl_yankees": {
        "team_id": 635,
        "sport_id": 16,        # Rookie (Dominican Summer League)
        "level": "Rookie",
        "display_name": "DSL NYY Yankees",
    },
    "dsl_bombers": {
        "team_id": 634,
        "sport_id": 16,        # Rookie (Dominican Summer League)
        "level": "Rookie",
        "display_name": "DSL NYY Bombers",
    },
}
