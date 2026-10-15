#!/usr/bin/env python3
"""Fetch the current/upcoming NFL slate from ESPN's public scoreboard API.

Keyless. Overwrites data/nfl-schedule.csv with the latest official schedule
snapshot for the week ESPN currently considers "on deck" (or an explicit
--seasontype/--week/--dates override). This is a schedule and kickoff-time
source only -- it carries no odds, injuries, or model output.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
SCHEDULE_PATH = DATA_DIR / "nfl-schedule.csv"
# 2026-08-12: site.api.espn.com now returns HTTP 403 from Akamai for every
# request regardless of User-Agent (browser UA fails identically). The public
# CDN schedule endpoint is unauthenticated, returns 200 with any UA, carries
# the same venue/broadcast/neutral-site detail, and accepts the same
# year/seasontype/week parameters. Switched to it.
API_URL = "https://cdn.espn.com/core/nfl/schedule"
ET = ZoneInfo("America/New_York")

FIELDNAMES = [
    "captured_at_et",
    "event_id",
    "season_year",
    "season_type",
    "week_number",
    "kickoff_utc",
    "kickoff_et",
    "away_team",
    "home_team",
    "away_team_full",
    "home_team_full",
    "matchup",
    "venue",
    "city",
    "state",
    "indoor",
    "neutral_site",
    "broadcast",
    "status",
]

SEASON_TYPE_NAMES = {1: "preseason", 2: "regular", 3: "postseason"}


def api_get(params: dict[str, str]) -> dict:
    params = {**params, "xhr": "1"}   # required: without it the CDN returns HTML, not JSON
    query = urllib.parse.urlencode(params)
    url = f"{API_URL}?{query}"
    # ESPN 403s branded and browser UAs but accepts curl/python defaults
    # (verified 2026-08-16: Mozilla UA -> 403, curl/Python-urllib -> 200).
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"ESPN schedule HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach ESPN scoreboard: {exc}") from exc


def iter_events(data: dict):
    """Yield event dicts from either shape.

    The CDN endpoint nests games under content.schedule[YYYYMMDD]["games"];
    the retired site.api endpoint used a flat events[] list. Support both so a
    future source swap does not require touching normalize_rows.
    """
    if data.get("events"):
        yield from data["events"]
        return
    schedule = (data.get("content") or {}).get("schedule") or {}
    if isinstance(schedule, dict):
        for day in sorted(schedule):
            yield from (schedule[day] or {}).get("games", [])
    elif isinstance(schedule, list):
        for day in schedule:
            yield from (day or {}).get("games", [])


def normalize_rows(data: dict, captured_at: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for event in iter_events(data):
        competitions = event.get("competitions", [])
        comp = competitions[0] if competitions else {}
        venue = comp.get("venue", {})
        address = venue.get("address", {})
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        broadcasts = comp.get("broadcasts", [])
        broadcast_names = broadcasts[0].get("names", []) if broadcasts else []
        kickoff_utc = event.get("date", "")
        kickoff_et = ""
        if kickoff_utc:
            try:
                kickoff_et = (
                    datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
                    .astimezone(ET)
                    .isoformat()
                )
            except ValueError:
                kickoff_et = ""
        season_type = event.get("season", {}).get("type")
        rows.append(
            {
                "captured_at_et": captured_at,
                "event_id": event.get("id", ""),
                "season_year": event.get("season", {}).get("year", ""),
                "season_type": SEASON_TYPE_NAMES.get(season_type, str(season_type or "")),
                "week_number": (
                    event["week"].get("number", "")
                    if isinstance(event.get("week"), dict)
                    else event.get("week", "")
                ),
                "kickoff_utc": kickoff_utc,
                "kickoff_et": kickoff_et,
                "away_team": away.get("team", {}).get("abbreviation", ""),
                "home_team": home.get("team", {}).get("abbreviation", ""),
                "away_team_full": away.get("team", {}).get("displayName", ""),
                "home_team_full": home.get("team", {}).get("displayName", ""),
                "matchup": event.get("shortName", event.get("name", "")),
                "venue": venue.get("fullName", ""),
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "indoor": venue.get("indoor", ""),
                "neutral_site": comp.get("neutralSite", ""),
                "broadcast": ", ".join(broadcast_names),
                "status": comp.get("status", {}).get("type", {}).get("name", ""),
            }
        )
    return rows


def write_schedule(rows: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SCHEDULE_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch the current NFL slate from ESPN's public scoreboard.")
    parser.add_argument("--seasontype", type=int, help="1=preseason, 2=regular, 3=postseason")
    parser.add_argument("--week", type=int, help="Week number within the season type")
    parser.add_argument("--dates", help="YYYYMMDD or YYYYMMDD-YYYYMMDD override")
    args = parser.parse_args()

    params: dict[str, str] = {}
    if args.seasontype:
        params["seasontype"] = str(args.seasontype)
    if args.week:
        params["week"] = str(args.week)
    if args.dates:
        params["dates"] = args.dates

    data = api_get(params)
    captured_at = datetime.now(ET).replace(microsecond=0).isoformat()
    rows = normalize_rows(data, captured_at)
    rows.sort(key=lambda r: r.get("kickoff_utc", ""))
    write_schedule(rows)

    print(f"Captured {len(rows)} NFL games into {SCHEDULE_PATH.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
