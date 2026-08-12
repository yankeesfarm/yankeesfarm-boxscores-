"""
Thin wrapper around statsapi.mlb.com for the YankeesFarm weekly stats pipeline.
No third-party MLB API package required -- just `requests`.

This talks to statsapi.mlb.com directly with documented byDateRange stat
splits, rather than scraping MiLB.com's website UI. That sidesteps the
caching and ambiguous-split issues we ran into pulling stats manually --
this API returns exact, unambiguous, date-bounded JSON.
"""
import time
from datetime import date

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


def get_player_stats_by_date_range(person_id, group, sport_id, season, start_date, end_date, team_id=None):
    """group: 'hitting' or 'pitching'. Returns None if the player had no
    activity in this window (common -- most of a 30+ man roster won't have
    hitting stats, pitchers won't have pitching stats, injured players will
    have neither).

    This pulls the player's full game-by-game log for the season, then
    filters to the requested date range AND team ENTIRELY IN THIS FUNCTION
    -- it does not ask the API's byDateRange/teamId aggregate filters to do
    that filtering, because that was tried twice in production and failed
    both times. sportId alone can't distinguish two teams at the same level
    (DSL NYY Yankees and DSL NYY Bombers are both sportId 16); adding a
    'teamId' parameter to that aggregate endpoint was the next attempt, and
    it ALSO silently didn't work -- both "team-specific" queries kept
    returning the player's identical whole-season rookie-level total. Since
    that aggregate endpoint's team-filtering can't be trusted, this instead
    fetches his raw per-game log (which includes each game's actual team)
    and sums the counting stats itself for just the games that are truly
    within this date range and for this team. This is more API calls per
    player, but it's the one thing that's fully verifiable and within our
    own control rather than depending on an unconfirmed filter parameter.

    Rate stats (avg/obp/etc.) are NOT computed here -- callers already
    recompute those from summed counting stats (recompute_hitting_rates /
    recompute_pitching_rates), which is the correct approach regardless of
    how the counting stats were sourced."""
    params = {"stats": "gameLog", "group": group, "sportId": sport_id, "season": season}
    data = _get(f"/people/{person_id}/stats", params)
    stats_list = data.get("stats", [])
    if not stats_list:
        return None
    splits = stats_list[0].get("splits", [])
    if not splits:
        return None

    start_d = date.fromisoformat(start_date)
    end_d = date.fromisoformat(end_date)

    relevant = []
    for g in splits:
        game_date_str = g.get("date")
        if not game_date_str:
            continue
        try:
            game_date = date.fromisoformat(game_date_str[:10])
        except ValueError:
            continue
        if not (start_d <= game_date <= end_d):
            continue
        if team_id is not None:
            g_team_id = (g.get("team") or {}).get("id")
            if g_team_id != team_id:
                continue
        relevant.append(g.get("stat", {}))

    if not relevant:
        return None

    summed = {}
    ip_outs_total = 0
    for stat in relevant:
        for k, v in stat.items():
            if k == "inningsPitched":
                # "5.1" means 5 and 1/3 innings (i.e. 16 outs), NOT 5.1 in
                # decimal -- outs are summed as integers, then the combined
                # innings string is rebuilt from the total at the end.
                ip_str = str(v)
                whole, _, frac = ip_str.partition(".")
                ip_outs_total += (int(whole) if whole else 0) * 3 + (int(frac) if frac else 0)
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                summed[k] = summed.get(k, 0) + v
            elif k not in summed:
                summed[k] = v
    if ip_outs_total or group == "pitching":
        summed["inningsPitched"] = f"{ip_outs_total // 3}.{ip_outs_total % 3}"
    return summed


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
