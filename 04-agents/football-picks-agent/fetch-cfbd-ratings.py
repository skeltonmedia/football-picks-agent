#!/usr/bin/env python3
"""Fetch FBS team-strength ratings from CollegeFootballData: SP+, PPA
(predicted points added), and returning production/talent.

Requires COLLEGE_FOOTBALL_DATA_API_KEY (set in ../config.env and exported
before running). Tries the current season year first; CFBD often hasn't
published preseason SP+/talent/returning-production yet in July, so this
falls back to the most recent year with data and labels every row with
data_year and is_current_season so the report/model never mistakes a
year-old rating for a fresh one.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
RATINGS_PATH = DATA_DIR / "cfb-team-ratings.csv"
API_BASE = "https://api.collegefootballdata.com"
ET = ZoneInfo("America/New_York")
FALLBACK_YEARS_BACK = 2

FIELDNAMES = [
    "captured_at_et",
    "data_year",
    "is_current_season",
    "team",
    "conference",
    "sp_plus_rating",
    "sp_plus_ranking",
    "sp_offense_rating",
    "sp_defense_rating",
    "ppa_offense_overall",
    "ppa_defense_overall",
    "talent_composite",
    "returning_production_overall",
]


def api_get(path: str, api_key: str, params: dict[str, str]) -> list[dict]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API_BASE}{path}?{query}" if query else f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    # AQ-262: CFBD reads timed out at 20s in a third of scheduled runs; give it
    # 45s and one retry before failing the step.
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(f"CollegeFootballData HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(5)
    raise SystemExit(f"Could not reach CollegeFootballData: {last_exc}")


def find_year_with_data(api_key: str, start_year: int) -> tuple[int, list[dict]]:
    for year in range(start_year, start_year - FALLBACK_YEARS_BACK - 1, -1):
        sp = api_get("/ratings/sp", api_key, {"year": str(year)})
        if sp:
            return year, sp
    raise SystemExit(
        f"No SP+ data found for {start_year} back through {start_year - FALLBACK_YEARS_BACK}. "
        "CollegeFootballData may be having an outage."
    )


def index_by_team(rows: list[dict], team_key: str = "team") -> dict[str, dict]:
    return {row.get(team_key, ""): row for row in rows}


def merge_rows(
    year: int,
    is_current: bool,
    sp_rows: list[dict],
    ppa_rows: list[dict],
    talent_rows: list[dict],
    returning_rows: list[dict],
    captured_at: str,
) -> list[dict[str, str]]:
    ppa_by_team = index_by_team(ppa_rows)
    talent_by_team = index_by_team(talent_rows)
    returning_by_team = index_by_team(returning_rows)

    merged: list[dict[str, str]] = []
    for sp in sp_rows:
        team = sp.get("team", "")
        ppa = ppa_by_team.get(team, {})
        talent = talent_by_team.get(team, {})
        returning = returning_by_team.get(team, {})
        merged.append(
            {
                "captured_at_et": captured_at,
                "data_year": year,
                "is_current_season": is_current,
                "team": team,
                "conference": sp.get("conference", ""),
                "sp_plus_rating": sp.get("rating", ""),
                "sp_plus_ranking": sp.get("ranking", ""),
                "sp_offense_rating": (sp.get("offense") or {}).get("rating", ""),
                "sp_defense_rating": (sp.get("defense") or {}).get("rating", ""),
                "ppa_offense_overall": (ppa.get("offense") or {}).get("overall", ""),
                "ppa_defense_overall": (ppa.get("defense") or {}).get("overall", ""),
                "talent_composite": talent.get("talent", ""),
                "returning_production_overall": returning.get("percentPPA", ""),
            }
        )
    return merged


def write_ratings(rows: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with RATINGS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    api_key = os.environ.get("COLLEGE_FOOTBALL_DATA_API_KEY", "").strip()
    if not api_key:
        print(
            "COLLEGE_FOOTBALL_DATA_API_KEY is not set. Get a free key at "
            "https://collegefootballdata.com/key, add it to "
            "04-agents/football-picks-agent/config.env, export it before "
            "running this script, and re-run. No ratings were written.",
            file=sys.stderr,
        )
        return 1

    current_year = datetime.now(ET).year
    year, sp_rows = find_year_with_data(api_key, current_year)
    is_current = year == current_year
    if not is_current:
        print(
            f"No {current_year} SP+ data yet (CFBD usually publishes preseason "
            f"ratings later in the summer) -- using {year} as the most recent "
            "available season. Rows are labeled is_current_season=False."
        )

    ppa_rows = api_get("/ppa/teams", api_key, {"year": str(year)})
    talent_rows = api_get("/talent", api_key, {"year": str(year)})
    returning_rows = api_get("/player/returning", api_key, {"year": str(year)})

    captured_at = datetime.now(ET).replace(microsecond=0).isoformat()
    merged = merge_rows(year, is_current, sp_rows, ppa_rows, talent_rows, returning_rows, captured_at)
    write_ratings(merged)

    print(f"Wrote {len(merged)} FBS team ratings ({year}) to {RATINGS_PATH.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
