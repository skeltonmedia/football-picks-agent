#!/usr/bin/env python3
"""Rank an NFL or NCAA game-day slate by market-implied cash probability.

This is the user-facing game-day seed list. It answers "which selection is
most likely to win?" rather than "which book is offering the softest price?".
Consensus no-vig probability is the baseline because no internal football model
has beaten the market. Injury, quarterback, weather, and role review can only
lower the displayed confidence or turn a row into WATCH/PASS before publish.

One selection per game is retained by default to avoid presenting correlated
markets as independent confidence. Prices shorter than -250 are excluded by
the standing bankroll guardrail even though they have higher hit rates.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import statistics
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo


AGENT = Path(__file__).resolve().parent
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
EASTERN = ZoneInfo("America/New_York")
ALLOWED_MARKETS = frozenset({"h2h", "spreads", "totals"})
MIN_ODDS = -250
MAX_ODDS = 350
MIN_BOOKS = 2
MAX_AGE_HOURS = 6.0


def implied_probability(american: float) -> float:
    return 100.0 / (american + 100.0) if american > 0 else -american / (-american + 100.0)


def compatible_pair(market: str, line_a: float | None, line_b: float | None) -> bool:
    if market == "h2h":
        return True
    if line_a is None or line_b is None:
        return False
    if market == "totals":
        return abs(line_a - line_b) < 1e-9
    return abs(line_a + line_b) < 1e-9


def market_label(market: str, selection: str, line: float | None) -> str:
    if market == "h2h":
        return f"{selection} MONEYLINE"
    if market == "spreads":
        return f"{selection} {line:+g}"
    return f"{selection.upper()} {line:g}"


def latest_market_rows(path: Path, *, as_of: dt.datetime, league: str) -> list[dict]:
    """Return the newest fresh offer per book for one exact league.

    The exact match matters: ``nfl`` must never admit the separate preseason
    sport key, and ``ncaaf`` must not inherit an NFL-only assumption.
    """
    latest: dict[tuple[str, str, str, str], dict] = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("sport", "").lower() != league:
                continue
            if row.get("market") not in ALLOWED_MARKETS:
                continue
            try:
                captured = dt.datetime.fromisoformat(row["captured_at_et"])
            except (KeyError, TypeError, ValueError):
                continue
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=EASTERN)
            age = (as_of - captured).total_seconds() / 3600.0
            if not (-0.25 <= age <= MAX_AGE_HOURS):
                continue
            key = (row["event_id"], row["market"], row["selection"], row["book"])
            if key not in latest or row["captured_at_et"] > latest[key]["captured_at_et"]:
                latest[key] = row
    return list(latest.values())


def build_candidates(rows: list[dict], *, game_date: dt.date) -> list[dict]:
    offers: dict[str, dict[str, dict[str, dict[str, tuple[float | None, float]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict))
    )
    meta: dict[str, dict] = {}
    for row in rows:
        try:
            start = dt.datetime.fromisoformat(row["commence_time_et"])
            odds = float(row["odds_american"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=EASTERN)
        if start.astimezone(EASTERN).date() != game_date:
            continue
        line = None
        if row["market"] != "h2h":
            try:
                line = float(row["line_points"])
            except (TypeError, ValueError):
                continue
        offers[row["event_id"]][row["market"]][row["selection"]][row["book"]] = (line, odds)
        meta[row["event_id"]] = {**row, "start": start}

    candidates: list[dict] = []
    for event_id, event_markets in offers.items():
        for market, selections in event_markets.items():
            if len(selections) != 2:
                continue
            selection_names = sorted(selections)
            market_candidates = []
            for selection in selection_names:
                other = next(name for name in selection_names if name != selection)
                book_probs = []
                for book, (line, odds) in selections[selection].items():
                    if book not in selections[other]:
                        continue
                    other_line, other_odds = selections[other][book]
                    if not compatible_pair(market, line, other_line):
                        continue
                    raw = implied_probability(odds)
                    raw_other = implied_probability(other_odds)
                    book_probs.append(raw / (raw + raw_other))
                dk_offer = selections[selection].get("draftkings")
                if len(book_probs) < MIN_BOOKS or dk_offer is None:
                    continue
                line, dk_odds = dk_offer
                market_candidates.append(
                    {
                        "event_id": event_id,
                        "market": market,
                        "selection": selection,
                        "line": line,
                        "odds": dk_odds,
                        "cash_probability": statistics.mean(book_probs) * 100.0,
                        "books": len(book_probs),
                        "away_team": meta[event_id]["away_team"],
                        "home_team": meta[event_id]["home_team"],
                        "start": meta[event_id]["start"],
                        "captured_at_et": meta[event_id]["captured_at_et"],
                    }
                )
            if market_candidates:
                favorite = max(market_candidates, key=lambda item: item["cash_probability"])
                if MIN_ODDS <= favorite["odds"] <= MAX_ODDS:
                    candidates.append(favorite)
    return candidates


def rank_candidates(candidates: list[dict], *, count: int, one_per_game: bool = True) -> list[dict]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            -item["cash_probability"],
            item["event_id"],
            item["market"],
            item["selection"],
        ),
    )
    if not one_per_game:
        return ordered[:count]
    ranked, seen = [], set()
    for candidate in ordered:
        if candidate["event_id"] in seen:
            continue
        seen.add(candidate["event_id"])
        ranked.append(candidate)
        if len(ranked) == count:
            break
    return ranked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", choices=("nfl", "ncaaf"), default="nfl")
    parser.add_argument("--date", help="Eastern date, YYYY-MM-DD; default today")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--allow-multiple-markets", action="store_true")
    args = parser.parse_args()

    now = dt.datetime.now(EASTERN)
    game_date = dt.date.fromisoformat(args.date) if args.date else now.date()
    rows = latest_market_rows(SNAPSHOTS, as_of=now, league=args.league)
    candidates = build_candidates(rows, game_date=game_date)
    ranked = rank_candidates(
        candidates,
        count=args.count,
        one_per_game=not args.allow_multiple_markets,
    )

    league_label = "NFL" if args.league == "nfl" else "NCAA"
    print(f"{league_label} CASH-CONFIDENCE BOARD — {game_date.isoformat()}")
    print("Consensus no-vig probability; one selection per game; value does not rank the board.")
    print("Manual injury, QB, weather, role, and movement review must still confirm PICK/LEAN.")
    print()
    for rank, item in enumerate(ranked, 1):
        label = market_label(item["market"], item["selection"], item["line"])
        start = item["start"].astimezone(EASTERN).strftime("%-I:%M%p ET")
        print(
            f"{rank:>2}. {label} | {item['cash_probability']:.1f}% cash confidence | "
            f"DK {item['odds']:+g} | {item['away_team']} @ {item['home_team']} | {start} | "
            f"{item['books']} books"
        )
    if len(ranked) < args.count:
        print(f"\nOnly {len(ranked)} games qualified; no filler was added.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
