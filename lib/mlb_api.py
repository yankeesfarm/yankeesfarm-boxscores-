"""
Thin wrapper around statsapi.mlb.com for the YankeesFarm weekly stats pipeline.
No third-party MLB API package required -- just `requests`.

This talks to statsapi.mlb.com directly with documented byDateRange stat
splits, rather than scraping MiLB.com's website UI. That sidesteps the
caching and ambiguous-split issues we ran into pulling stats manually --
this API returns exact, unambiguous, date-bounded JSON.
"""
import time

import requests

BASE = "https://statsapi.mlb.com/api/v1"


def _get(path, params=None, retries=3, backoff=2):
    url = f"{BASE}{path}"
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            last_exc = RuntimeError(f"{resp.status_code} from {url}: {resp.text[:300]}")
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Failed after {retries} attempts: {last_exc}")


def get_active_roster(team_id, season):
    """Full-season roster (not just 'active' on today's date) so we don't
    miss players who were on the roster earlier in the week but have since
    been optioned/promoted/DFA'd."""
    data = _get(f"/teams/{team_id}/roster", {"rosterType": "fullSeason", "season": season})
    return data.get("roster", [])


def get_player_stats_by_date_range(person_id, group, sport_id, season, start_date, end_date):
    """group: 'hitting' or 'pitching'. Returns None if the player had no
    activity in this window (common -- most of a 30+ man roster won't have
    hitting stats, pitchers won't have pitching stats, injured players will
    have neither)."""
    params = {
        "stats": "byDateRange",
        "group": group,
        "sportId": sport_id,
        "season": season,
        "startDate": start_date,
        "endDate": end_date,
    }
    data = _get(f"/people/{person_id}/stats", params)
    stats_list = data.get("stats", [])
    if not stats_list:
        return None
    splits = stats_list[0].get("splits", [])
    if not splits:
        return None
    return splits[0]["stat"]


def get_team_schedule(team_id, start_date, end_date, sport_id=None):
    """Used by verify_data.py to sanity-check completed-game counts against
    what the stats pull actually captured (narrow, ~1 week windows, so the
    default multi-level sportId is fine there).

    fetch_season_stats.py also uses this to find a team's actual Opening
    Day, searching back several months -- and MLB's API rejects date
    ranges over 45 days when multiple sportIds are requested at once. Pass
    this team's own specific sport_id (already known from config/affiliates.py)
    to search a single level instead, which isn't subject to that limit."""
    data = _get("/schedule", {
        "teamId": team_id,
        "startDate": start_date,
        "endDate": end_date,
        "sportId": sport_id if sport_id else "11,12,13,14,16",
    })
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return [g for g in games if g.get("status", {}).get("statusCode") == "F"]
