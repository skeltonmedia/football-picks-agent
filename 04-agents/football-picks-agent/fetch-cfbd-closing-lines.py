#!/usr/bin/env python3
"""fetch-cfbd-closing-lines.py — true DraftKings closing lines for FBS games.

    python3 -B 04-agents/football-picks-agent/fetch-cfbd-closing-lines.py [--year 2026] [--weeks 1,2]

WHY (2026-09-07). CLV is the KPI, and every "close" graded so far was the
7:30 AM Saturday odds capture, up to 13 hours before kickoff. CollegeFootballData
publishes DraftKings' closing spread, total, and moneylines per game the day
after (free tier, keyed from config.env). DraftKings is also the board's base
book, so this is the exact number the recommendation was compared against.

Writes data/cfb-closing-lines.csv (one row per game, latest provider row wins).
grade-clv.py prefers these closes for CFB rows over the snapshot close.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
OUT = AGENT / "data" / "cfb-closing-lines.csv"
CONFIG = AGENT / "config.env"
API = "https://api.collegefootballdata.com/lines"
ET = ZoneInfo("America/New_York")
PROVIDERS = ("DraftKings", "Draft Kings")  # CFBD ships both spellings; first wins
FIELDS = ["cfbd_game_id", "season", "week", "game_date_et", "commence_time_et",
          "away_team", "home_team", "provider", "spread_home", "spread_home_open",
          "total", "total_open", "home_ml", "away_ml", "away_score", "home_score",
          "captured_at_et"]


def cfg(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    if CONFIG.exists():
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


def api_get(key: str, year: int, week: int) -> list[dict]:
    url = f"{API}?year={year}&week={week}&seasonType=regular"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pick_line(lines: list[dict]) -> dict | None:
    """Prefer the DraftKings row that carries moneylines; merge the legacy row's
    fields when the primary lacks them."""
    by = {}
    for ln in lines:
        p = ln.get("provider")
        if p in PROVIDERS and p not in by:
            by[p] = ln
    if not by:
        return None
    primary = by.get("DraftKings") or by.get("Draft Kings")
    other = by.get("Draft Kings") if primary is by.get("DraftKings") else by.get("DraftKings")
    merged = dict(primary)
    if other:
        for k, v in other.items():
            if merged.get(k) is None and v is not None:
                merged[k] = v
    return merged


def rows_for(games: list[dict], captured: str) -> list[dict]:
    out = []
    for g in games:
        ln = pick_line(g.get("lines") or [])
        if not ln:
            continue
        try:
            start = datetime.fromisoformat(g["startDate"].replace("Z", "+00:00")).astimezone(ET)
        except (KeyError, ValueError):
            continue
        out.append({
            "cfbd_game_id": g.get("id"), "season": g.get("season"), "week": g.get("week"),
            "game_date_et": start.date().isoformat(), "commence_time_et": start.isoformat(),
            "away_team": g.get("awayTeam"), "home_team": g.get("homeTeam"),
            "provider": ln.get("provider"),
            "spread_home": ln.get("spread"), "spread_home_open": ln.get("spreadOpen"),
            "total": ln.get("overUnder"), "total_open": ln.get("overUnderOpen"),
            "home_ml": ln.get("homeMoneyline"), "away_ml": ln.get("awayMoneyline"),
            "away_score": g.get("awayScore"), "home_score": g.get("homeScore"),
            "captured_at_et": captured,
        })
    return out


def merge_write(new_rows: list[dict]) -> int:
    existing = {}
    if OUT.exists():
        for r in csv.DictReader(OUT.open(encoding="utf-8")):
            existing[r["cfbd_game_id"]] = r
    for r in new_rows:
        # Only completed games carry a final close; keep pregame rows too so
        # a mid-week pull does not erase what a post-game pull wrote.
        prev = existing.get(str(r["cfbd_game_id"]))
        if prev and prev.get("home_score") not in ("", None) and r.get("home_score") in ("", None):
            continue
        existing[str(r["cfbd_game_id"])] = {k: ("" if v is None else v) for k, v in r.items()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(existing.values(), key=lambda x: (str(x["game_date_et"]), str(x["home_team"]))):
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return len(existing)


def current_weeks(now: datetime) -> list[int]:
    """Season week 1 = the week of Labor Day weekend; pull this week and last."""
    season_start = datetime(now.year, 8, 30, tzinfo=ET)  # 2026: Week 0 Sat Aug 29
    wk = max(1, (now - season_start).days // 7 + 1)
    return sorted({max(1, wk - 1), wk})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=datetime.now(ET).year)
    ap.add_argument("--weeks", help="comma list; default = current and previous week")
    args = ap.parse_args()
    key = cfg("COLLEGE_FOOTBALL_DATA_API_KEY")
    if not key:
        print("COLLEGE_FOOTBALL_DATA_API_KEY is not set; no closing lines written.", file=sys.stderr)
        return 1
    weeks = [int(w) for w in args.weeks.split(",")] if args.weeks else current_weeks(datetime.now(ET))
    captured = datetime.now(ET).isoformat(timespec="seconds")
    rows = []
    for wk in weeks:
        try:
            rows += rows_for(api_get(key, args.year, wk), captured)
        except Exception as exc:  # noqa: BLE001 — a missing week must not break the loop
            print(f"week {wk}: {exc}", file=sys.stderr)
    n = merge_write(rows)
    print(f"Wrote {len(rows)} DraftKings closing line(s) for weeks {weeks}; "
          f"{n} game(s) on file -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
