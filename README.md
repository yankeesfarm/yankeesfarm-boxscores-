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

## Publishing to the website (yankeesfarmreport.com)

The weekly workflow also pushes both the weekly and month-to-date leaderboards
to your Wix site automatically -- **no manual step, it publishes live as soon
as the job runs** (this was a deliberate choice: unlike the Instagram Issue,
which you review before posting, the website update is fully automatic).

The one safety net that still applies: if `verify_data.py` finds that an
entire team's data is missing (the clearest sign of a fetch failure, as
opposed to a normal stat outcome), the push is **skipped automatically** and
the site keeps showing last week's numbers instead of publishing something
broken. You'll still get the review Issue either way, flagged accordingly.

### One-time Wix setup

1. **Create the Data Collection.** In the Wix Editor, go to CMS -> Create
   Collection, name it exactly `LeaderboardEntries`, and add these fields:

   | Field | Type |
   |---|---|
   | period | Text |
   | category | Text |
   | group | Text |
   | label | Text |
   | rank | Text (not Number -- ties are stored like `"T4"`) |
   | sortOrder | Number |
   | playerName | Text |
   | team | Text |
   | value | Text |
   | isRate | Boolean |
   | generatedAt | Date and Time |

   Set collection permissions so visitors can **read** but not **write** --
   the write only ever happens through the backend code below, which runs
   with its own elevated permissions.

2. **Add the backend function.** Copy `wix/backend/http-functions.js` from
   this repo into your Wix site's own `src/backend/http-functions.js` (if you
   already have an `http-functions.js` file for other endpoints, add the
   `post_leaderboards` function into it rather than overwriting the file).

3. **Add the shared secret.** In the Wix Editor: Settings -> Secrets Manager
   -> add a secret named `YANKEESFARM_PUSH_SECRET`. Generate a long random
   value for it (e.g. run `openssl rand -hex 32` in any terminal) and save
   that same value for step 5 below. Treat it like a password.

4. **Publish the site.** Wix http-functions only go live after a real
   publish, not in preview mode.

5. **Add GitHub secrets.** In your GitHub repo: Settings -> Secrets and
   variables -> Actions -> New repository secret. Add two:
   - `WIX_PUSH_ENDPOINT` = `https://www.yankeesfarmreport.com/_functions/leaderboards`
     (swap in your actual domain; the path comes from the function being
     named `post_leaderboards`)
   - `WIX_PUSH_SECRET` = the same value you generated in step 3

6. **Build the front-end display.** In the Wix Editor, add a Repeater (or
   Table) element, connect it to the `LeaderboardEntries` collection, filter
   by `period` ("weekly" or "monthly") and `category` (e.g. `"hitting_avg"`),
   and sort by `sortOrder` ascending. Repeat for each stat category you want
   a section for. This is a native Wix dataset binding -- no code needed on
   this side once the collection is filled.

### Testing it end-to-end

Trigger the workflow manually (Actions tab -> Weekly Farm Stats -> Run
workflow) and check three things afterward: the GitHub Issue it opens says
"Website push ran," the `LeaderboardEntries` collection in your Wix CMS has
fresh rows with today's `generatedAt`, and the actual page on
yankeesfarmreport.com shows the update.

### If you're on Wix's newer CLI/SDK setup instead of classic Velo

Wix has a newer `@wix/data` SDK alongside the classic `wix-data` Velo module
used above. If your site was built with the newer Wix CLI tooling rather
than the in-browser Velo editor, the import syntax differs slightly --
tell me and I'll adapt `http-functions.js` accordingly.

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
