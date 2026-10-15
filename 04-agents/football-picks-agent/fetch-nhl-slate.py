#!/usr/bin/env python3
"""Fetch the NHL slate from ESPN's public scoreboard (keyless).

    python3 -B 04-agents/football-picks-agent/fetch-nhl-slate.py [--days 8]

Writes data/nhl-schedule.csv in the nfl-schedule.csv column shape, so the
free-lines name resolver, settle-props.py's ESPN event lookup and the
dashboard read all three leagues the same way. Hockey has no week number: the
window is yesterday through today+N days ET (yesterday so a game that ended
after midnight still carries its FINAL status for settlement). Added
2026-10-07 for the daily card (David: "add hockey sources"). Schedule and
start times only; no odds, no model output.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
SCHEDULE_PATH = DATA_DIR / "nhl-schedule.csv"
API_URL = "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
ET = ZoneInfo("America/New_York")
SEASON_TYPE_NAMES = {1: "preseason", 2: "regular", 3: "postseason"}

FIELDNAMES = [
    "captured_at_et", "event_id", "season_year", "season_type", "week_number",
    "kickoff_utc", "kickoff_et", "away_team", "home_team", "away_team_full",
    "home_team_full", "matchup", "venue", "city", "state", "indoor",
    "neutral_site", "broadcast", "status",
]


def api_get(day: datetime) -> dict:
    """One date per request: ESPN's hockey scoreboard answers 400 to a date range."""
    req = urllib.request.Request(f"{API_URL}?dates={day:%Y%m%d}&limit=100", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def normalize_rows(data: dict, captured_at: str) -> list[dict[str, str]]:
    rows = []
    for event in data.get("events", []):
        comps = event.get("competitions", [])
        comp = comps[0] if comps else {}
        venue = comp.get("venue", {}) or {}
        address = venue.get("address", {}) or {}
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        broadcasts = comp.get("broadcasts", [])
        names = broadcasts[0].get("names", []) if broadcasts else []
        kickoff_utc = event.get("date", "")
        try:
            kickoff_et = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00")).astimezone(ET).isoformat()
        except ValueError:
            kickoff_et = ""
        stype = (event.get("season") or {}).get("type")
        rows.append({
            "captured_at_et": captured_at,
            "event_id": event.get("id", ""),
            "season_year": (event.get("season") or {}).get("year", ""),
            "season_type": SEASON_TYPE_NAMES.get(stype, str(stype or "")),
            "week_number": "",
            "kickoff_utc": kickoff_utc,
            "kickoff_et": kickoff_et,
            "away_team": (away.get("team") or {}).get("abbreviation", ""),
            "home_team": (home.get("team") or {}).get("abbreviation", ""),
            "away_team_full": (away.get("team") or {}).get("displayName", ""),
            "home_team_full": (home.get("team") or {}).get("displayName", ""),
            "matchup": event.get("shortName", event.get("name", "")),
            "venue": venue.get("fullName", ""),
            "city": address.get("city", ""),
            "state": address.get("state", ""),
            "indoor": "True",
            "neutral_site": comp.get("neutralSite", ""),
            "broadcast": ", ".join(names),
            "status": ((comp.get("status") or {}).get("type") or {}).get("name", ""),
        })
    return rows


def write_schedule(rows: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SCHEDULE_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch the NHL slate from ESPN (keyless).")
    ap.add_argument("--days", type=int, default=8, help="days ahead to capture (default 8)")
    args = ap.parse_args()
    now = datetime.now(ET)
    days = [now + timedelta(days=d) for d in range(-1, args.days + 1)]
    rows, failed = [], 0
    for day in days:
        try:
            rows += normalize_rows(api_get(day), now.replace(microsecond=0).isoformat())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
            failed += 1
    if failed == len(days):
        print("nhl-slate: every ESPN day fetch failed; schedule file left as is")
        return 1
    seen, unique = set(), []
    for r in sorted(rows, key=lambda r: r["kickoff_utc"]):
        if r["event_id"] in seen:
            continue
        seen.add(r["event_id"])
        unique.append(r)
    write_schedule(unique)
    span = f"{days[0]:%Y-%m-%d}..{days[-1]:%Y-%m-%d}"
    print(f"Captured {len(unique)} NHL games ({span}{f', {failed} day(s) failed' if failed else ''}) "
          f"into {SCHEDULE_PATH.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
