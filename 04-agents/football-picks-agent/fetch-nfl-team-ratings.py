#!/usr/bin/env python3
"""Compute a simple NFL team-strength rating from nflverse's public games
history (keyless, no dependency install) -- the NFL equivalent of CFB's
SP+ context in the weekly report and review cards.

There is no free API-hosted power rating for the NFL the way
CollegeFootballData hosts SP+ for CFB, so this computes an SRS (Simple
Rating System) from margin of victory: iterate `rating[team] = average
game margin + average opponent rating` to convergence, the same method
pro-football-reference uses for its SRS column. Blowout margins are capped
(MOV_CAP) so one 45-point win doesn't dominate the rating, a standard
technique in these systems.

Descriptive context only -- see model-notes.md. The 2026-07-27 CFB backtest
found that rating-based models (including a well-established one, Elo)
don't beat the closing line; the 2026-08-24 NFL backtest (backtest-nfl-srs.py,
AQ-101) confirmed the same holds for this SRS specifically -- closing lines
correlate with actual margins better than the model on 208 games from the
2025 season. See model-notes.md for the full result.
"""

from __future__ import annotations

import csv
import io
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
RATINGS_PATH = DATA_DIR / "nfl-team-ratings.csv"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
ET = ZoneInfo("America/New_York")
MOV_CAP = 24
SRS_ITERATIONS = 100
FALLBACK_YEARS_BACK = 2

# AQ-158 (2026-09-02, approved). Was: "current season" required 200 completed
# games league-wide (~week 13-14) before is_current_season ever flipped True,
# leaving weekly-picks.py's NFL board empty for most of the season. Replaced
# with the SAME threshold backtest-nfl-srs.py already validated (MIN_WEEK=5,
# >=30 prior games) -- weeks 1-4 excluded as too thin to rate anything, which
# is also where the review cards already carry a preseason/small-sample
# warning. compute_srs() itself is untouched; only the gate that decides
# whether to trust the current season's data moved. See model-notes.md.
MIN_WEEK = 5
MIN_SAME_SEASON_GAMES = 30

# nflverse's team abbreviations differ from ESPN's (fetch-nfl-slate.py's
# source) for exactly two teams. Normalize to ESPN's convention here so
# downstream code can match on team abbreviation directly, no mapping
# table needed elsewhere -- same lesson as the CFB team-name mismatch.
ABBR_TO_ESPN = {"LA": "LAR", "WAS": "WSH"}

FIELDNAMES = [
    "captured_at_et",
    "data_year",
    "is_current_season",
    "team",
    "srs_rating",
    "srs_ranking",
    "games_played",
    "avg_margin",
]


def fetch_games_csv() -> list[dict[str, str]]:
    req = urllib.request.Request(GAMES_URL, headers={"User-Agent": "DavidOS-football-picks-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"nflverse games.csv HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach nflverse: {exc}") from exc
    return list(csv.DictReader(io.StringIO(body)))


def completed_games_for_year(games: list[dict[str, str]], year: int) -> list[dict[str, str]]:
    # REG only, matching backtest-nfl-srs.py's load_season_games(): playoff
    # games weren't part of what got validated, so they don't feed a live
    # rating either. (2025's games.csv carries 272 REG + 13 postseason.)
    return [
        g
        for g in games
        if g.get("season") == str(year) and g.get("game_type") == "REG"
        and g.get("home_score") not in ("", None) and g.get("away_score") not in ("", None)
    ]


def current_season_ready(completed_current_year: list[dict[str, str]]) -> bool:
    """Mirrors backtest-nfl-srs.py's validated enabling condition, translated
    from "predict week W using weeks < W" to "is there enough same-season
    history banked right now to trust a live rating": the current/next week
    must be MIN_WEEK or later (i.e. weeks 1..MIN_WEEK-1 have completed games),
    and at least MIN_SAME_SEASON_GAMES have been played league-wide.
    """
    weeks = set()
    for g in completed_current_year:
        w = g.get("week", "")
        if str(w).isdigit():
            weeks.add(int(w))
    return (max(weeks, default=0) >= MIN_WEEK - 1
            and len(completed_current_year) >= MIN_SAME_SEASON_GAMES)


def find_year_with_data(games: list[dict[str, str]], start_year: int) -> tuple[int, list[dict[str, str]]]:
    current = completed_games_for_year(games, start_year)
    if current_season_ready(current):
        return start_year, current
    for year in range(start_year - 1, start_year - FALLBACK_YEARS_BACK - 1, -1):
        completed = completed_games_for_year(games, year)
        if len(completed) >= 200:  # a real, mostly-complete regular season
            return year, completed
    raise SystemExit(f"No substantially-complete NFL season found from {start_year} back through {start_year - FALLBACK_YEARS_BACK}.")


def compute_srs(games: list[dict[str, str]]) -> dict[str, dict[str, float]]:
    matchups: list[tuple[str, str, float]] = []  # (team, opponent, capped_margin)
    for g in games:
        home = ABBR_TO_ESPN.get(g["home_team"], g["home_team"])
        away = ABBR_TO_ESPN.get(g["away_team"], g["away_team"])
        try:
            home_score, away_score = float(g["home_score"]), float(g["away_score"])
        except ValueError:
            continue
        margin = home_score - away_score
        capped = max(-MOV_CAP, min(MOV_CAP, margin))
        matchups.append((home, away, capped))
        matchups.append((away, home, -capped))

    teams = sorted({t for t, _, _ in matchups})
    by_team: dict[str, list[tuple[str, float]]] = {t: [] for t in teams}
    for team, opponent, margin in matchups:
        by_team[team].append((opponent, margin))

    rating = {t: 0.0 for t in teams}
    for _ in range(SRS_ITERATIONS):
        new_rating = {}
        for team in teams:
            games_played = by_team[team]
            new_rating[team] = sum(margin + rating[opp] for opp, margin in games_played) / len(games_played)
        mean = sum(new_rating.values()) / len(new_rating)
        rating = {t: v - mean for t, v in new_rating.items()}

    avg_margin = {t: sum(m for _, m in by_team[t]) / len(by_team[t]) for t in teams}
    games_played_count = {t: len(by_team[t]) for t in teams}
    return {
        "rating": rating,
        "avg_margin": avg_margin,
        "games_played": games_played_count,
    }


def write_ratings(year: int, is_current: bool, stats: dict, captured_at: str) -> None:
    rating, avg_margin, games_played = stats["rating"], stats["avg_margin"], stats["games_played"]
    ranked = sorted(rating.items(), key=lambda kv: kv[1], reverse=True)
    rows = []
    for rank, (team, value) in enumerate(ranked, start=1):
        rows.append(
            {
                "captured_at_et": captured_at,
                "data_year": year,
                "is_current_season": is_current,
                "team": team,
                "srs_rating": round(value, 2),
                "srs_ranking": rank,
                "games_played": games_played[team],
                "avg_margin": round(avg_margin[team], 2),
            }
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with RATINGS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    games = fetch_games_csv()
    current_year = datetime.now(ET).year
    year, completed = find_year_with_data(games, current_year)
    is_current = year == current_year
    if not is_current:
        print(
            f"No substantially-complete {current_year} NFL season yet -- using {year} as the most "
            "recent completed season. Rows are labeled is_current_season=False."
        )

    stats = compute_srs(completed)
    captured_at = datetime.now(ET).replace(microsecond=0).isoformat()
    write_ratings(year, is_current, stats, captured_at)
    print(f"Wrote {len(stats['rating'])} NFL team ratings ({year}, {len(completed)} games) to {RATINGS_PATH.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
