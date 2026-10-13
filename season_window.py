"""
Season-window helper for the Top Performers feature.

Top Performers is an in-season widget. It should stop updating (and stop
being pushed to the website) once the minor league season is over --
by end of September. Off-season runs skip cleanly with exit code 0 so the
daily workflow does not fail.
"""

from datetime import date

# Inclusive (month, day) bounds of the window in which Top Performers runs.
# Opening Day for the earliest affiliate (AAA) is late March; the season
# ends by September 30.
SEASON_START = (3, 1)
SEASON_END = (9, 30)


def top_performers_in_season(game_date: str) -> bool:
    """True if game_date (YYYY-MM-DD) falls inside the Top Performers window."""
    d = date.fromisoformat(game_date)
    return SEASON_START <= (d.month, d.day) <= SEASON_END
