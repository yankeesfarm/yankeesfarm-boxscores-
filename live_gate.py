#!/usr/bin/env python3
"""
live_gate.py -- decides whether the game-end stats refresh has any work to do.

Looks at yesterday + today (US/Eastern) for every Yankees affiliate and finds
games whose status is Final that have not been processed yet
(data/processed_games.json).  Writes has_new=true/false to $GITHUB_OUTPUT so
the workflow only runs the (slow) stat pushes when a game just ended.

    python3 live_gate.py check   # sets has_new, writes data/pending_games.json
    python3 live_gate.py commit  # marks pending games as processed
"""
import json, os, sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.affiliates import AFFILIATES, ROOKIE_AFFILIATES
from lib.mlb_api import _get

DONE = Path("data/processed_games.json")
PENDING = Path("data/pending_games.json")
KEEP_DAYS_OF_IDS = 400  # cap file size


def final_games():
    now = datetime.now(ZoneInfo("America/New_York"))
    start, end = (now - timedelta(days=1)).date().isoformat(), now.date().isoformat()
    found = {}
    for cfg in list(AFFILIATES.values()) + list(ROOKIE_AFFILIATES.values()):
        data = _get("/schedule", {"teamId": cfg["team_id"], "startDate": start,
                                  "endDate": end, "sportId": cfg["sport_id"]})
        for d in data.get("dates", []):
            for g in d.get("games", []):
                if g.get("status", {}).get("abstractGameState") == "Final":
                    found[str(g["gamePk"])] = cfg["display_name"]
    return found


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    done = json.loads(DONE.read_text()) if DONE.exists() else {}
    if mode == "commit":
        pending = json.loads(PENDING.read_text()) if PENDING.exists() else {}
        done.update(pending)
        for k in sorted(done, key=int)[:-KEEP_DAYS_OF_IDS]:
            done.pop(k)
        DONE.parent.mkdir(exist_ok=True)
        DONE.write_text(json.dumps(done, indent=0, sort_keys=True))
        PENDING.unlink(missing_ok=True)
        return
    new = {k: v for k, v in final_games().items() if k not in done}
    DONE.parent.mkdir(exist_ok=True)
    PENDING.write_text(json.dumps(new))
    print(f"{len(new)} newly final game(s): {sorted(set(new.values()))}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"has_new={'true' if new else 'false'}\n")


if __name__ == "__main__":
    main()
