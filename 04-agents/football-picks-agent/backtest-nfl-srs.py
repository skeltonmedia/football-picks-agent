#!/usr/bin/env python3
"""Backtest the NFL SRS rating (fetch-nfl-team-ratings.py) against a completed
season's closing lines, before trusting it any more than descriptive context.

Companion to backtest-cfb-elo.py -- same discipline, adapted to what's
actually available for the NFL:

1. **No weekly-snapshot API exists for NFL power ratings** the way CFBD
   serves week-by-week Elo for CFB. So leakage-free lag here means something
   more literal: to predict week W, recompute SRS from scratch using ONLY
   games from weeks strictly before W in the same season (an expanding
   window). Weeks before MIN_WEEK are skipped entirely -- there isn't enough
   same-season data yet for the rating to mean anything, and a real bettor
   wouldn't have it either.
2. SRS math is imported from fetch-nfl-team-ratings.py, not reimplemented --
   one SRS formula in the codebase, not two that can quietly drift apart.
3. The scale and home-field-advantage grid search is fit to minimize error
   against the ACTUAL final margin, never against the market line, exactly
   like the CFB backtest -- tuning to beat the market on the same data used
   to test that edge would be the backtest overfitting the CFB script's
   docstring warns about.
4. Closing lines come from nflverse's own games.csv `spread_line` column
   (home team perspective: positive = home favored by that many points,
   confirmed against nflreadr's published data dictionary and against the
   `result` column's sign in a spot-check) -- the same free, keyless source
   fetch-nfl-team-ratings.py already reads. No new data source, no API key,
   per AQ-101.
5. One season (~200 matched games) is a go/no-go signal check, not proof of
   a durable edge -- see model-notes.md's evaluation section.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
REPORT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"
ET = ZoneInfo("America/New_York")

_RATINGS_SCRIPT = AGENT_DIR / "fetch-nfl-team-ratings.py"
_spec = importlib.util.spec_from_file_location("fetch_nfl_team_ratings", _RATINGS_SCRIPT)
ratings_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(ratings_mod)

MIN_WEEK = 5  # earliest week with enough same-season games for SRS to mean anything
SCALE_GRID = [0.8, 0.9, 1.0, 1.1, 1.2]
HFA_GRID = [0, 1.0, 1.5, 2.0, 2.5, 3.0]
EDGE_BUCKETS = [(0, 1), (1, 3), (3, 5), (5, float("inf"))]


def load_season_games(year: int) -> list[dict[str, str]]:
    all_games = ratings_mod.fetch_games_csv()
    reg = [g for g in all_games if g.get("season") == str(year) and g.get("game_type") == "REG"]
    completed = [g for g in reg if g.get("home_score") not in ("", None) and g.get("away_score") not in ("", None)]
    return completed


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = (var_x * var_y) ** 0.5
    return cov / denom if denom else float("nan")


def build_matched_games(games: list[dict[str, str]]) -> list[dict]:
    """Expanding-window SRS: week W is predicted using SRS computed only from
    weeks < W of the same season. Recomputes SRS once per week rather than
    once per game -- one rating snapshot serves every game in that week,
    matching how a bettor would actually check ratings once before a slate."""
    by_week: dict[int, list[dict[str, str]]] = {}
    for g in games:
        try:
            week = int(g["week"])
        except (KeyError, ValueError):
            continue
        by_week.setdefault(week, []).append(g)

    matched: list[dict] = []
    for week in sorted(w for w in by_week if w >= MIN_WEEK):
        prior_games = [g for w, gs in by_week.items() if w < week for g in gs]
        if len(prior_games) < 30:
            continue
        stats = ratings_mod.compute_srs(prior_games)
        rating = stats["rating"]
        for g in by_week[week]:
            home = ratings_mod.ABBR_TO_ESPN.get(g["home_team"], g["home_team"])
            away = ratings_mod.ABBR_TO_ESPN.get(g["away_team"], g["away_team"])
            home_r, away_r = rating.get(home), rating.get(away)
            spread = g.get("spread_line", "")
            if home_r is None or away_r is None or spread in ("", None, "NA"):
                continue
            try:
                actual_margin = float(g["home_score"]) - float(g["away_score"])
                market_home_margin = float(spread)  # nflverse: positive = home favored, lines up with `result`
            except ValueError:
                continue
            matched.append({
                "week": week,
                "home_team": home,
                "away_team": away,
                "rating_diff": home_r - away_r,
                "actual_margin": actual_margin,
                "market_home_margin": market_home_margin,
            })
    return matched


def fit_best_params(matched: list[dict]) -> tuple[float, float, float]:
    best = (None, None, float("inf"))
    for scale in SCALE_GRID:
        for hfa in HFA_GRID:
            errors = [abs((g["rating_diff"] * scale + hfa) - g["actual_margin"]) for g in matched]
            mae = sum(errors) / len(errors)
            if mae < best[2]:
                best = (scale, hfa, mae)
    return best


def ats_bucket(diff: float) -> str:
    for low, high in EDGE_BUCKETS:
        if low <= diff < high:
            return f"{low}-{high if high != float('inf') else '+'}"
    return "unknown"


def run_backtest(matched: list[dict], scale: float, hfa: float) -> dict:
    predicted = [g["rating_diff"] * scale + hfa for g in matched]
    actual = [g["actual_margin"] for g in matched]
    market = [g["market_home_margin"] for g in matched]
    corr_model_actual = pearson(predicted, actual)
    corr_market_actual = pearson(market, actual)
    mae_model = sum(abs(p - a) for p, a in zip(predicted, actual)) / len(matched)
    mae_market = sum(abs(m - a) for m, a in zip(market, actual)) / len(matched)

    buckets: dict[str, list[int]] = {f"{lo}-{hi if hi != float('inf') else '+'}": [0, 0, 0] for lo, hi in EDGE_BUCKETS}
    always_home = [0, 0, 0]
    for g, pred in zip(matched, predicted):
        edge = pred - g["market_home_margin"]
        cover_margin = g["actual_margin"] - g["market_home_margin"]
        if cover_margin == 0:
            continue  # push
        covering_side = "home" if cover_margin > 0 else "away"
        model_side = "home" if edge > 0 else "away"
        bucket = ats_bucket(abs(edge))
        if model_side == covering_side:
            buckets[bucket][0] += 1
        else:
            buckets[bucket][1] += 1
        buckets[bucket][2] += 1

        if covering_side == "home":
            always_home[0] += 1
        else:
            always_home[1] += 1
        always_home[2] += 1

    return {
        "n_games": len(matched),
        "corr_model_actual": corr_model_actual,
        "corr_market_actual": corr_market_actual,
        "mae_model": mae_model,
        "mae_market": mae_market,
        "buckets": buckets,
        "always_home_ats": always_home,
    }


def write_report(year: int, scale: float, hfa: float, mae_fit: float, result: dict) -> Path:
    lines = [
        f"# NFL SRS Backtest — {year} Season (AQ-101)",
        "",
        "Methodology: expanding-window SRS (each week's rating computed from only "
        "the same season's strictly-prior weeks -- no future data), scale/HFA fit "
        "to minimize error against the actual final margin, then checked against "
        "nflverse's closing `spread_line` separately. Weeks before "
        f"{MIN_WEEK} are excluded -- not enough same-season data for the rating to "
        "mean anything yet. One season is a signal check, not proof of a durable "
        "edge. See `backtest-cfb-elo.py`'s docstring for why the fit/test split matters "
        "and `model-notes.md` for how this feeds pick policy.",
        "",
        "## Fit",
        "",
        f"- Best-fit rating scale: `{scale}`",
        f"- Best-fit home-field advantage: `{hfa}` points",
        f"- Mean absolute error vs. actual margin (fit target): `{mae_fit:.2f}` points",
        f"- Games with a computable prior-weeks SRS and a closing spread line: {result['n_games']}",
        "",
        "## Does the model track reality at all?",
        "",
        f"- Correlation (model prediction vs. actual margin): `{result['corr_model_actual']:.3f}`",
        f"- Correlation (closing spread vs. actual margin): `{result['corr_market_actual']:.3f}`",
        f"- MAE (model vs. actual): `{result['mae_model']:.2f}` points",
        f"- MAE (market vs. actual): `{result['mae_market']:.2f}` points",
        "",
        "The market's correlation/MAE should be at least as good as the model's — "
        "closing lines are famously efficient. If the model's correlation is "
        "meaningfully *higher* than the market's here, be suspicious of a bug "
        "before getting excited.",
        "",
        "## ATS record when betting the model's favored side vs. the closing line",
        "",
        "| Model edge vs. market | Record (W-L) | Win % | Games |",
        "| --- | --- | --- | ---: |",
    ]
    for bucket_label, (win, loss, total) in result["buckets"].items():
        pct = f"{100 * win / total:.1f}%" if total else "n/a"
        lines.append(f"| {bucket_label} pts | {win}-{loss} | {pct} | {total} |")

    ah_win, ah_loss, ah_total = result["always_home_ats"]
    ah_pct = f"{100 * ah_win / ah_total:.1f}%" if ah_total else "n/a"
    lines.extend([
        "",
        f"Baseline for context — home team covered {ah_win}/{ah_total} ({ah_pct}) of the time. "
        "A market-efficient world puts every bucket near 50% and the breakeven line at "
        "a standard -110 price is 52.4%.",
        "",
        "## Reading this honestly",
        "",
        "- Small buckets (especially 5+ points) can look great or terrible from noise alone at "
        "this sample size — check the games column before trusting a percentage.",
        "- This is in-sample for the scale/HFA fit (fit and tested on the same season). A real "
        "proof needs an out-of-season holdout before any of this touches a live pick.",
        "- SRS alone ignores injuries, weather, pace, and situational factors the checklist in "
        "`instructions.md` requires — this is a first-pass signal check, not a fair-line model.",
        "- Weeks 1-" + str(MIN_WEEK - 1) + " are entirely excluded from this backtest (no prior-week "
        "same-season data), which is also exactly when the review cards already carry a preseason/"
        "small-sample warning against trusting SRS.",
        "",
    ])

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"backtest-nfl-srs-{year}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Backtest the NFL SRS rating against a season's closing lines.")
    parser.add_argument("--year", type=int, default=datetime.now(ET).year - 1)
    args = parser.parse_args()

    print(f"Fetching nflverse games.csv for {args.year}...")
    games = load_season_games(args.year)
    if len(games) < 200:
        print(f"Only {len(games)} completed REG games found for {args.year} — season likely incomplete. "
              "Pass an earlier --year.", file=sys.stderr)
        return 1

    matched = build_matched_games(games)
    print(f"Matched {len(matched)} of {len(games)} games to a prior-weeks SRS + closing spread (weeks >= {MIN_WEEK}).")
    if len(matched) < 30:
        print("Too few matched games for a meaningful backtest.", file=sys.stderr)
        return 1

    scale, hfa, mae_fit = fit_best_params(matched)
    result = run_backtest(matched, scale, hfa)
    report_path = write_report(args.year, scale, hfa, mae_fit, result)
    print(f"Wrote {report_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
