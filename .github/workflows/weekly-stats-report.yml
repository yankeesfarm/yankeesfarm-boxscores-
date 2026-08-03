"""
YankeesFarm Weekly Stats Report
--------------------------------
Scrapes Last-7-Days batting-average and pitching/innings-pitched pages
for all four affiliates, filters to:
  - Hitters:  AVG >= .250   (sorted by AVG desc)
  - Pitchers: ERA < 4.00    (sorted by IP desc)
and posts the formatted report as a new GitHub Issue.

Requires: playwright, requests
  pip install playwright requests
  playwright install chromium --with-deps

Env vars expected when run in GitHub Actions:
  GITHUB_TOKEN        - auto-provided by Actions
  GITHUB_REPOSITORY   - auto-provided by Actions, e.g. "carlos/yankeesfarm-boxscores"
"""

import os
import re
import sys
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

TEAMS = [
    {
        "name": "Tampa Tarpons",
        "batting_url": "https://www.milb.com/tampa/stats/batting-average?playerPool=ALL&timeframe=-6",
        "pitching_url": "https://www.milb.com/tampa/stats/pitching/innings-pitched?timeframe=-6",
    },
    {
        "name": "Hudson Valley Renegades",
        "batting_url": "https://www.milb.com/hudson-valley/stats/batting-average?timeframe=-6",
        "pitching_url": "https://www.milb.com/hudson-valley/stats/pitching/innings-pitched?timeframe=-6",
    },
    {
        "name": "Somerset Patriots",
        "batting_url": "https://www.milb.com/somerset/stats/batting-average?playerPool=ALL&timeframe=-6",
        "pitching_url": "https://www.milb.com/somerset/stats/pitching/innings-pitched?timeframe=-6",
    },
    {
        "name": "Scranton/Wilkes-Barre RailRiders",
        "batting_url": "https://www.milb.com/scranton-wb/stats/batting-average?timeframe=-6",
        "pitching_url": "https://www.milb.com/scranton-wb/stats/pitching/innings-pitched?timeframe=-6",
    },
]

HITTER_COLS = ["G", "AB", "R", "H", "2B", "3B", "HR", "RBI", "BB", "SO", "SB", "CS", "AVG", "OBP", "SLG", "OPS"]
PITCHER_COLS = ["W", "L", "ERA", "G", "GS", "CG", "SHO", "SV", "SVO", "IP", "H", "R", "ER", "HR", "HB", "BB", "SO", "WHIP", "AVG"]


def fetch_table_rows(page, url):
    """Load a MiLB stats page and return the raw text grid of the data table."""
    page.goto(url, wait_until="networkidle", timeout=60000)
    # The stats table renders client-side — wait for at least one player link to appear.
    page.wait_for_selector("a[href*='/player/']", timeout=30000)

    table = page.locator("table").last
    rows = table.locator("tbody tr")
    count = rows.count()

    parsed = []
    for i in range(count):
        row = rows.nth(i)
        # Player full name — the player link's text is the cleanest source
        # (the raw cell text is duplicated for responsive mobile/desktop spans).
        name_link = row.locator("a[href*='/player/']").first
        name = name_link.inner_text().strip()
        # Collapse "FirstF LastLast" duplication patterns some renders produce
        name = re.sub(r"\s+", " ", name)

        cells = row.locator("td")
        cell_texts = [cells.nth(j).inner_text().strip() for j in range(cells.count())]
        parsed.append({"name": name, "cells": cell_texts})
    return parsed


def parse_hitters(rows):
    out = []
    for r in rows:
        cells = r["cells"]
        if len(cells) < len(HITTER_COLS) + 1:  # +1 for TEAM col
            continue
        # cells layout: [TEAM, G, AB, R, H, 2B, 3B, HR, RBI, BB, SO, SB, CS, AVG, OBP, SLG, OPS]
        vals = dict(zip(["TEAM"] + HITTER_COLS, cells))
        try:
            out.append({
                "name": r["name"],
                "avg": vals["AVG"],
                "obp": vals["OBP"],
                "ops": vals["OPS"],
                "2b": int(vals["2B"]),
                "3b": int(vals["3B"]),
                "hr": int(vals["HR"]),
                "rbi": int(vals["RBI"]),
                "bb": int(vals["BB"]),
                "sb": int(vals["SB"]),
                "avg_float": float(vals["AVG"]),
            })
        except (ValueError, KeyError):
            continue
    return out


def ip_to_outs(ip_str):
    if "." in ip_str:
        whole, frac = ip_str.split(".")
    else:
        whole, frac = ip_str, "0"
    return int(whole) * 3 + int(frac)


def parse_pitchers(rows):
    out = []
    for r in rows:
        cells = r["cells"]
        if len(cells) < len(PITCHER_COLS) + 1:
            continue
        vals = dict(zip(["TEAM"] + PITCHER_COLS, cells))
        try:
            out.append({
                "name": r["name"],
                "era": float(vals["ERA"]),
                "whip": float(vals["WHIP"]),
                "ip": vals["IP"],
                "h": int(vals["H"]),
                "r": int(vals["R"]),
                "er": int(vals["ER"]),
                "bb": int(vals["BB"]),
                "k": int(vals["SO"]),
                "ip_outs": ip_to_outs(vals["IP"]),
            })
        except (ValueError, KeyError):
            continue
    return out


def format_hitter(p):
    line1 = f"{p['avg']}/{p['obp']} - {p['ops']} OPS"
    xbh = p["2b"] + p["3b"] + p["hr"]
    mid_parts = []
    if xbh > 0:
        piece = f"{xbh} XBH"
        extra = []
        if p["hr"] > 0:
            extra.append(f"{p['hr']} HR")
        if p["rbi"] > 0:
            extra.append(f"{p['rbi']} RBI")
        if extra:
            piece += " - " + ", ".join(extra)
        mid_parts.append(piece)
    elif p["rbi"] > 0:
        mid_parts.append(f"{p['rbi']} RBI")
    if p["bb"] > 0:
        mid_parts.append(f"{p['bb']} BB")
    if p["sb"] > 0:
        mid_parts.append(f"{p['sb']} SB")
    return line1 + (" | " + " | ".join(mid_parts) if mid_parts else "")


def format_pitcher(p):
    line1 = f"{p['era']:.2f} ERA - {p['whip']:.2f} WHIP"
    line2 = f"{p['ip']} IP - {p['h']} H, {p['r']} R, {p['er']} ER, {p['bb']} BB, {p['k']} K"
    return f"{line1} | {line2}"


def build_report():
    lines = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        for team in TEAMS:
            lines.append(f"## {team['name']}")
            lines.append("")
            lines.append("**Hitters**")

            hitter_rows = fetch_table_rows(page, team["batting_url"])
            hitters = parse_hitters(hitter_rows)
            hitters = [h for h in hitters if h["avg_float"] >= 0.250]
            hitters.sort(key=lambda h: -h["avg_float"])

            for h in hitters:
                lines.append(h["name"])
                lines.append(format_hitter(h))

            lines.append("")
            lines.append("▪️")
            lines.append("")
            lines.append("**Pitchers**")

            pitcher_rows = fetch_table_rows(page, team["pitching_url"])
            pitchers = parse_pitchers(pitcher_rows)
            pitchers = [p for p in pitchers if p["era"] < 4.00]
            pitchers.sort(key=lambda p: -p["ip_outs"])

            for p in pitchers:
                lines.append(p["name"])
                lines.append(format_pitcher(p))

            lines.append("")
            lines.append("---")
            lines.append("")

        browser.close()
    return "\n".join(lines)


def post_github_issue(body):
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    today = datetime.now().strftime("%Y-%m-%d")
    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": f"Weekly Stats Report — {today}",
            "body": body,
            "labels": ["weekly-report"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Issue created: {resp.json()['html_url']}")


def main():
    report = build_report()
    print(report)  # also shows up in the Actions log for quick sanity-checking
    if "GITHUB_TOKEN" in os.environ:
        post_github_issue(report)
    else:
        print("\n(No GITHUB_TOKEN found — skipping issue creation. Running locally?)")


if __name__ == "__main__":
    main()
