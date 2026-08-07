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
