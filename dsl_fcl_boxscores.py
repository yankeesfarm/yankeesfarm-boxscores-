#!/usr/bin/env python3
"""
YankeesFarm Daily Box Score Puller (all affiliate levels)
------------------------------------------------------------
Pulls box scores for every Yankees minor league affiliate game on a given
date -- AAA (RailRiders), AA (Patriots), High-A (Renegades), Single-A
(Tarpons), and Rookie (DSL/FCL) -- using MLB's free public Stats API. No API
key needed. Yankees affiliates are identified dynamically by organization ID
(147), not by name, so this keeps working even if MiLB affiliations change.

USAGE:
    python3 dsl_fcl_boxscores.py                # yesterday's completed games
    python3 dsl_fcl_boxscores.py 2026-07-20      # specific date

OUTPUT:
    ./output/YYYY-MM-DD_yankees_boxscores.json   -- raw structured data
    ./output/YYYY-MM-DD_yankees_boxscores.html   -- ready-to-embed HTML
    ./output/YYYY-MM-DD_yankees_recap.txt        -- Carlos's recap format,
                                                     ready to hand-edit

SCHEDULING (pick one, since this script itself doesn't run on a timer):
  - Cron (Mac/Linux server): 
        0 9 * * * cd /path/to/script && python3 dsl_fcl_boxscores.py >> log.txt 2>&1
  - GitHub Actions (free, no server needed): a workflow with a `schedule:`
    cron trigger that runs this script and commits the output.
  - Windows Task Scheduler: same idea, daily trigger.
"""

import json
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

STATS_API = "https://statsapi.mlb.com/api/v1"
YANKEES_ORG_ID = 147  # New York Yankees, MLB team ID
MILB_SPORT_IDS = (11, 12, 13, 14, 16)  # AAA, AA, High-A, Single-A, Rookie
LEVEL_LABELS = {11: "AAA", 12: "AA", 13: "High-A", 14: "A", 16: "Rookie"}


def get_yankees_team_ids(season: str) -> dict:
    """Return {team_id: {"name":..., "sportId":...}} for every current
    Yankees affiliate, across all MiLB levels, by matching parentOrgId.
    Queries each level separately -- the /teams endpoint doesn't reliably
    accept a comma-joined sportId list the way /schedule does."""
    teams = {}
    for sid in MILB_SPORT_IDS:
        url = (
            f"{STATS_API}/teams"
            f"?sportId={sid}&season={season}&hydrate=parentOrgName"
        )
        try:
            data = fetch_json(url)
        except Exception as e:
            print(f"Warning: teams lookup failed for sportId={sid}: {e}")
            continue
        for t in data.get("teams", []):
            if t.get("parentOrgId") == YANKEES_ORG_ID:
                teams[t["id"]] = {"name": t["name"], "sportId": sid}
    return teams


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "YankeesFarm/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"ERROR fetching {url}: {e}")
        raise


def get_schedule(game_date: str) -> list:
    """Return list of game dicts across all MiLB levels for the given date."""
    sport_ids_str = ",".join(str(s) for s in MILB_SPORT_IDS)
    url = (
        f"{STATS_API}/schedule"
        f"?sportId={sport_ids_str}"
        f"&date={game_date}"
        f"&hydrate=linescore,team"
    )
    data = fetch_json(url)
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def is_yankees_game(game: dict, yankees_ids: dict) -> bool:
    home_id = game["teams"]["home"]["team"]["id"]
    away_id = game["teams"]["away"]["team"]["id"]
    return home_id in yankees_ids or away_id in yankees_ids


def level_label(game: dict, yankees_ids: dict) -> str:
    home = game["teams"]["home"]["team"]
    away = game["teams"]["away"]["team"]
    sport_id = None
    if home["id"] in yankees_ids:
        sport_id = yankees_ids[home["id"]]["sportId"]
    elif away["id"] in yankees_ids:
        sport_id = yankees_ids[away["id"]]["sportId"]
    label = LEVEL_LABELS.get(sport_id, "")
    if label == "Rookie":
        if home["name"].startswith("DSL") or away["name"].startswith("DSL"):
            return "DSL"
        if home["name"].startswith("FCL") or away["name"].startswith("FCL"):
            return "FCL"
        return "ACL"  # Arizona Complex League also sits under sportId 16
    return label


def get_boxscore(game_pk: int) -> dict:
    url = f"{STATS_API}/game/{game_pk}/boxscore"
    return fetch_json(url)


def get_season_totals(person_id: int, season: str, sport_id: int) -> dict:
    """Season-to-date cumulative hitting totals for a player (used for the
    '(14)' style running totals in Carlos's recap format). sport_id must be
    the player's actual MiLB level -- without it, the API defaults to MLB
    stats and returns nothing for minor leaguers."""
    url = (
        f"{STATS_API}/people/{person_id}/stats"
        f"?stats=season&group=hitting&season={season}&sportId={sport_id}"
    )
    try:
        data = fetch_json(url)
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return {}
        s = splits[0]["stat"]
        return {
            "doubles": s.get("doubles", 0),
            "triples": s.get("triples", 0),
            "homeRuns": s.get("homeRuns", 0),
            "rbi": s.get("rbi", 0),
            "stolenBases": s.get("stolenBases", 0),
        }
    except Exception as e:
        print(f"Warning: season totals lookup failed for person {person_id}: {e}")
        return {}


def strip_league_prefix(name: str) -> str:
    for p in ("FCL ", "DSL "):
        if name.startswith(p):
            return name[len(p):]
    return name


def format_batting_line(pos: str, name: str, game_stat: dict, season: dict) -> str:
    events = []

    ab = game_stat.get("atBats", 0)
    h = game_stat.get("hits", 0)
    bb = game_stat.get("baseOnBalls", 0)
    hbp = game_stat.get("hitByPitch", 0)
    sf = game_stat.get("sacFlies", 0)
    doubles = game_stat.get("doubles", 0)
    triples = game_stat.get("triples", 0)
    hr = game_stat.get("homeRuns", 0)
    singles = h - doubles - triples - hr
    rbi = game_stat.get("rbi", 0)
    sb = game_stat.get("stolenBases", 0)
    runs = game_stat.get("runs", 0)

    if bb == 1:
        events.append("BB")
    elif bb > 1:
        events.append(f"{bb} BB")
    if hbp:
        events.append("HBP" if hbp == 1 else f"{hbp} HBP")
    if sf:
        events.append("SF")
    if singles == 1:
        events.append("Single")
    elif singles > 1:
        events.append(f"{singles} Singles")
    if doubles:
        events.append(f"Double ({season.get('doubles', '?')})" if doubles == 1
                       else f"{doubles} Doubles ({season.get('doubles', '?')})")
    if triples:
        events.append(f"Triple ({season.get('triples', '?')})" if triples == 1
                       else f"{triples} Triples ({season.get('triples', '?')})")
    if hr:
        events.append(f"HR ({season.get('homeRuns', '?')})" if hr == 1
                       else f"{hr} HR ({season.get('homeRuns', '?')})")
    if rbi:
        events.append(f"RBI ({season.get('rbi', '?')})" if rbi == 1
                       else f"{rbi} RBI ({season.get('rbi', '?')})")
    if sb:
        events.append(f"SB ({season.get('stolenBases', '?')})" if sb == 1
                       else f"{sb} SB ({season.get('stolenBases', '?')})")
    if runs == 1:
        events.append("Run")
    elif runs > 1:
        events.append(f"{runs} Runs")

    prefix = f"{pos} {name} {h}-{ab}"
    if events:
        return f"{prefix}: " + ", ".join(events)
    return prefix


def format_pitching_line(role: str, name: str, game_stat: dict) -> str:
    ip = game_stat.get("inningsPitched", "0.0")
    h = game_stat.get("hits", 0)
    r = game_stat.get("runs", 0)
    er = game_stat.get("earnedRuns", 0)
    bb = game_stat.get("baseOnBalls", 0)
    so = game_stat.get("strikeOuts", 0)
    if er == r:
        return f"{role} {name} {ip} IP | {h} H, {r} R, {bb} BB, {so} K"
    return f"{role} {name} {ip} IP | {h} H, {r} R, {er} ER, {bb} BB, {so} K"


def summarize_batting(team_box: dict) -> list:
    rows = []
    players = team_box.get("players", {})
    order = team_box.get("battingOrder", [])
    for pid in order:
        p = players.get(f"ID{pid}")
        if not p:
            continue
        stats = p.get("stats", {}).get("batting", {})
        if not stats:
            continue
        rows.append({
            "name": p["person"]["fullName"],
            "position": p.get("position", {}).get("abbreviation", ""),
            "ab": stats.get("atBats", 0),
            "r": stats.get("runs", 0),
            "h": stats.get("hits", 0),
            "rbi": stats.get("rbi", 0),
            "bb": stats.get("baseOnBalls", 0),
            "so": stats.get("strikeOuts", 0),
            "avg": stats.get("avg", ""),
        })
    return rows


def summarize_pitching(team_box: dict) -> list:
    rows = []
    players = team_box.get("players", {})
    for pid in team_box.get("pitchers", []):
        p = players.get(f"ID{pid}")
        if not p:
            continue
        stats = p.get("stats", {}).get("pitching", {})
        if not stats:
            continue
        rows.append({
            "name": p["person"]["fullName"],
            "ip": stats.get("inningsPitched", ""),
            "h": stats.get("hits", 0),
            "r": stats.get("runs", 0),
            "er": stats.get("earnedRuns", 0),
            "bb": stats.get("baseOnBalls", 0),
            "so": stats.get("strikeOuts", 0),
            "era": stats.get("era", ""),
        })
    return rows


def render_recap_text(game: dict, box: dict, season: str, yankees_ids: dict) -> str:
    """Render one game in Carlos's YankeesFarm recap style."""
    home = game["teams"]["home"]
    away = game["teams"]["away"]
    home_name = home["team"]["name"]
    away_name = away["team"]["name"]
    league = level_label(game, yankees_ids)

    if home["team"]["id"] in yankees_ids:
        us, opp = home, away
        us_name, opp_name = home_name, away_name
        us_box, opp_box = box["teams"]["home"], box["teams"]["away"]
    else:
        us, opp = away, home
        us_name, opp_name = away_name, home_name
        us_box, opp_box = box["teams"]["away"], box["teams"]["home"]

    us_score = us_box.get("teamStats", {}).get("batting", {}).get("runs", 0)
    opp_score = opp_box.get("teamStats", {}).get("batting", {}).get("runs", 0)

    record = us.get("leagueRecord", {})
    wins, losses = record.get("wins", "?"), record.get("losses", "?")
    result = "W" if us_score > opp_score else "L"
    us_sport_id = yankees_ids[us["team"]["id"]]["sportId"]

    opp_display = strip_league_prefix(opp_name) if league == "FCL" else opp_name
    header = f"{us_name}: vs {opp_display} {us_score}-{opp_score} {result}\u25aa\ufe0f(Record {wins}-{losses})\u25aa\ufe0f"

    lines = [header, ""]

    players = us_box.get("players", {})
    for pid in us_box.get("battingOrder", []):
        p = players.get(f"ID{pid}")
        if not p:
            continue
        stat = p.get("stats", {}).get("batting", {})
        if not stat:
            continue
        pos = p.get("position", {}).get("abbreviation", "")
        name = p["person"]["fullName"]
        season_totals = get_season_totals(p["person"]["id"], season, us_sport_id)
        lines.append(format_batting_line(pos, name, stat, season_totals))

    lines.append("\u25fc\ufe0f")

    for pid in us_box.get("pitchers", []):
        p = players.get(f"ID{pid}")
        if not p:
            continue
        stat = p.get("stats", {}).get("pitching", {})
        if not stat:
            continue
        name = p["person"]["fullName"]
        role = "SP" if pid == us_box.get("pitchers", [None])[0] else "RP"
        lines.append(format_pitching_line(role, name, stat))

    return "\n".join(lines)


def build_game_record(game: dict, yankees_ids: dict) -> dict:
    game_pk = game["gamePk"]
    home_name = game["teams"]["home"]["team"]["name"]
    away_name = game["teams"]["away"]["team"]["name"]
    status = game["status"]["detailedState"]
    league = level_label(game, yankees_ids)

    record = {
        "game_pk": game_pk,
        "league": league,
        "status": status,
        "away_team": away_name,
        "home_team": home_name,
        "away_score": None,
        "home_score": None,
        "away_batting": [],
        "home_batting": [],
        "away_pitching": [],
        "home_pitching": [],
    }

    if status not in ("Final", "Game Over", "Completed Early"):
        return record  # game hasn't finished yet -- no box score to pull

    box = get_boxscore(game_pk)
    teams = box.get("teams", {})
    away_box = teams.get("away", {})
    home_box = teams.get("home", {})

    record["away_score"] = away_box.get("teamStats", {}).get("batting", {}).get("runs")
    record["home_score"] = home_box.get("teamStats", {}).get("batting", {}).get("runs")
    record["away_batting"] = summarize_batting(away_box)
    record["home_batting"] = summarize_batting(home_box)
    record["away_pitching"] = summarize_pitching(away_box)
    record["home_pitching"] = summarize_pitching(home_box)

    return record


def render_html(game_date: str, records: list) -> str:
    parts = [f"<h2>Yankees Affiliate Box Scores — {game_date}</h2>"]
    if not records:
        parts.append("<p>No Yankees affiliate games found for this date.</p>")
        return "\n".join(parts)

    for g in records:
        parts.append(f'<div class="boxscore-game" data-league="{g["league"]}">')
        parts.append(f'<h3>[{g["league"]}] {g["away_team"]} @ {g["home_team"]}</h3>')
        if g["status"] not in ("Final", "Game Over", "Completed Early"):
            parts.append(f'<p><em>{g["status"]}</em></p></div>')
            continue
        parts.append(f'<p><strong>Final: {g["away_team"]} {g["away_score"]} — '
                      f'{g["home_team"]} {g["home_score"]}</strong></p>')

        for side_label, batting, pitching in (
            (g["away_team"], g["away_batting"], g["away_pitching"]),
            (g["home_team"], g["home_batting"], g["home_pitching"]),
        ):
            parts.append(f"<h4>{side_label} Batting</h4><table border='1' cellpadding='4'>")
            parts.append("<tr><th>Player</th><th>Pos</th><th>AB</th><th>R</th><th>H</th>"
                          "<th>RBI</th><th>BB</th><th>SO</th><th>AVG</th></tr>")
            for b in batting:
                parts.append(
                    f"<tr><td>{b['name']}</td><td>{b['position']}</td><td>{b['ab']}</td>"
                    f"<td>{b['r']}</td><td>{b['h']}</td><td>{b['rbi']}</td>"
                    f"<td>{b['bb']}</td><td>{b['so']}</td><td>{b['avg']}</td></tr>"
                )
            parts.append("</table>")

            parts.append(f"<h4>{side_label} Pitching</h4><table border='1' cellpadding='4'>")
            parts.append("<tr><th>Player</th><th>IP</th><th>H</th><th>R</th><th>ER</th>"
                          "<th>BB</th><th>SO</th><th>ERA</th></tr>")
            for p in pitching:
                parts.append(
                    f"<tr><td>{p['name']}</td><td>{p['ip']}</td><td>{p['h']}</td>"
                    f"<td>{p['r']}</td><td>{p['er']}</td><td>{p['bb']}</td>"
                    f"<td>{p['so']}</td><td>{p['era']}</td></tr>"
                )
            parts.append("</table>")

        parts.append("</div><hr>")

    return "\n".join(parts)


def main():
    default_date = (date.today() - timedelta(days=1)).isoformat()
    game_date = sys.argv[1] if len(sys.argv) > 1 else default_date
    datetime.strptime(game_date, "%Y-%m-%d")
    season = game_date.split("-")[0]

    yankees_ids = get_yankees_team_ids(season)

    all_games = get_schedule(game_date)
    target_games = [g for g in all_games if is_yankees_game(g, yankees_ids)]

    print(f"Found {len(target_games)} Yankees affiliate game(s) on {game_date}"
          f" (out of {len(all_games)} total MiLB games at these levels).")

    records = [build_game_record(g, yankees_ids) for g in target_games]

    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)

    json_path = out_dir / f"{game_date}_yankees_boxscores.json"
    html_path = out_dir / f"{game_date}_yankees_boxscores.html"
    recap_path = out_dir / f"{game_date}_yankees_recap.txt"

    json_path.write_text(json.dumps(records, indent=2))
    html_path.write_text(render_html(game_date, records))

    recap_chunks = []
    for g in target_games:
        status = g["status"]["detailedState"]
        if status not in ("Final", "Game Over", "Completed Early"):
            continue
        box = get_boxscore(g["gamePk"])
        recap_chunks.append(render_recap_text(g, box, season, yankees_ids))
    recap_path.write_text("\n\n\n".join(recap_chunks))

    print(f"Wrote {json_path}")
    print(f"Wrote {html_path}")
    print(f"Wrote {recap_path}")


if __name__ == "__main__":
    main()
