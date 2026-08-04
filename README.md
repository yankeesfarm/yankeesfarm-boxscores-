# YankeesFarm Weekly/Monthly Stat Leaders Pipeline

Pulls hitting and pitching stats for all four Yankees full-season affiliates
(Tampa, Hudson Valley, Somerset, Scranton/WB) from the MLB Stats API
(`statsapi.mlb.com`), filters out anyone with confirmed MLB experience, and
builds Top-10 leaderboards -- weekly, and accumulated into exact monthly
totals -- ready to post.

## Why the MLB Stats API instead of scraping MiLB.com

Manually pulling from MiLB.com's website hit two real problems: the pitching
stats pages served a cached snapshot that was over a week stale, and the
"July" split parameter behaved inconsistently across teams and page loads.
The Stats API returns exact, documented `byDateRange` JSON instead of a
cached UI state, which removes both failure modes. It's also the same API
your `yankeesfarm-boxscores` repo already calls successfully.

## Setup

```bash
pip install requests
```

No API key needed -- `statsapi.mlb.com` is a public, unauthenticated endpoint.

### First-run sanity check (do this before trusting anything)

```bash
python -c "from lib.mlb_api import get_active_roster; \
    r = get_active_roster(587, 2026); \
    print([p['person']['fullName'] for p in r[:5]])"
```

Repeat for each `team_id` in `config/affiliates.py` (537, 1956, 531) and
confirm the names match the real current roster of that team. If a team_id
is wrong you'll get either an empty list or another team's players entirely
-- both are obvious. I compiled these team_ids from MiLB.com's public pages,
not a live API call (this sandbox can't reach `statsapi.mlb.com`), so this
one-time check matters.

## Running it

**Pull the last 7 days:**
```bash
python fetch_weekly_stats.py
```

**Pull a specific window** (e.g., to backfill a missed week):
```bash
python fetch_weekly_stats.py --start 2026-07-28 --end 2026-08-03
```

**Check the pull before trusting it:**
```bash
python verify_data.py --file data/weekly/week_2026-08-03.json
```

**Generate a Markdown leaderboard from that week:**
```bash
python generate_leaderboard.py --file data/weekly/week_2026-08-03.json --min-ab 5 --min-ip 3
```
(Lower qualifying minimums for a single week -- 5 AB / 3 IP -- since a full
week of at-bats is a small sample. Use 15/15 for monthly, matching what we
used manually.)

**Roll every week of a month into an exact monthly total:**
```bash
python accumulate_monthly.py --month 2026-08
python generate_leaderboard.py --file data/monthly/2026-08.json --min-ab 15 --min-ip 15
```

## Automation (GitHub Actions)

`.github/workflows/weekly-farm-stats.yml` runs every Monday at 9am ET,
pulls the past week, verifies it, generates both the weekly and updated
month-to-date leaderboards, commits the data to the repo, and opens a
GitHub Issue with everything pasted in for you to review -- **it does not
post to Instagram automatically.** That step stays manual and human,
on purpose.

## Maintaining the prospect exclusion list

`config/excluded_players.json` is keyed by MLB Stats API person ID (not
name -- names collide, accents break string matching, IDs don't). Add an
entry whenever a name jumps onto a leaderboard that shouldn't be there:
a rehab assignment, a new veteran minor-league signing, a player added to
the 40-man. The `verify_data.py` output won't catch this automatically --
it's a judgment call each time someone new shows up near the top of a
category.

## What this system does and does not guarantee

**It does:**
- Pull exact, date-bounded stats from MLB's own API, not a cached webpage
- Recompute every rate stat (AVG, OBP, SLG, ERA, WHIP, etc.) from summed
  counting stats at accumulation time, so monthly totals are mathematically
  exact rollups of the underlying games, not averages-of-averages
- Flag duplicate player entries, empty-activity rows, and missing team
  coverage before you see a leaderboard
- Keep a fully auditable, ID-keyed exclusion list so "who's a prospect"
  is a documented decision, not a guess baked into the code

**It does not:**
- Guarantee zero errors. APIs have outages and occasional data-entry quirks
  from the league itself; this pipeline can only be as accurate as the
  source data it's given.
- Catch a promoted player's stats being attributed to the wrong team mid-week
  (it flags this as a warning, but doesn't resolve it for you)
- Replace a human glance at the top few names in each category before you
  post. Build that into your weekly routine -- it's the actual guardrail
  for your credibility, not a nice-to-have.
