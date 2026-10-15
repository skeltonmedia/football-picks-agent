#!/usr/bin/env python3
"""Fetch a completed CFB season's DraftKings closing lines plus final scores
from CollegeFootballData, for backtesting a model before it's ever trusted
on a live slate.

Requires COLLEGE_FOOTBALL_DATA_API_KEY. Defaults to the most recently
completed season (this calendar year's season won't have scores yet before
kickoff, so pass --year explicitly once mid-season data exists).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
API_BASE = "https://api.collegefootballdata.com"
ET = ZoneInfo("America/New_York")

FIELDNAMES = [
    "captured_at_et",
    "season",
    "season_type",
    "week",
    "game_id",
    "start_date",
    "home_team",
    "away_team",
    "home_score",
    "away_score",
    "dk_spread",
    "dk_spread_open",
    "dk_over_under",
    "dk_over_under_open",
    "dk_home_moneyline",
    "dk_away_moneyline",
]


def api_get(path: str, api_key: str, params: dict[str, str]) -> list[dict]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API_BASE}{path}?{query}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"CollegeFootballData HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach CollegeFootballData: {exc}") from exc


def normalize_rows(games: list[dict], captured_at: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for game in games:
        if game.get("homeScore") is None or game.get("awayScore") is None:
            continue
        dk = next((line for line in game.get("lines", []) if line.get("provider") == "DraftKings"), None)
        if dk is None:
            continue
        rows.append(
            {
                "captured_at_et": captured_at,
                "season": game.get("season", ""),
                "season_type": game.get("seasonType", ""),
                "week": game.get("week", ""),
                "game_id": game.get("id", ""),
                "start_date": game.get("startDate", ""),
                "home_team": game.get("homeTeam", ""),
                "away_team": game.get("awayTeam", ""),
                "home_score": game.get("homeScore", ""),
                "away_score": game.get("awayScore", ""),
                "dk_spread": dk.get("spread", ""),
                "dk_spread_open": dk.get("spreadOpen", ""),
                "dk_over_under": dk.get("overUnder", ""),
                "dk_over_under_open": dk.get("overUnderOpen", ""),
                "dk_home_moneyline": dk.get("homeMoneyline", ""),
                "dk_away_moneyline": dk.get("awayMoneyline", ""),
            }
        )
    return rows


def write_rows(rows: list[dict[str, str]], path: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a completed CFB season's DraftKings closing lines + scores.")
    parser.add_argument("--year", type=int, default=datetime.now(ET).year - 1)
    parser.add_argument("--season-type", default="regular", choices=["regular", "postseason"])
    args = parser.parse_args()

    api_key = os.environ.get("COLLEGE_FOOTBALL_DATA_API_KEY", "").strip()
    if not api_key:
        print(
            "COLLEGE_FOOTBALL_DATA_API_KEY is not set. See ../config.env.",
            file=sys.stderr,
        )
        return 1

    games = api_get("/lines", api_key, {"year": str(args.year), "seasonType": args.season_type})
    captured_at = datetime.now(ET).replace(microsecond=0).isoformat()
    rows = normalize_rows(games, captured_at)

    out_path = DATA_DIR / f"cfb-lines-history-{args.year}-{args.season_type}.csv"
    write_rows(rows, out_path)
    print(f"Wrote {len(rows)} completed games with DraftKings lines to {out_path.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
