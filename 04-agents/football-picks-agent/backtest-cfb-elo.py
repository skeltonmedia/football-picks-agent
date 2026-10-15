#!/usr/bin/env python3
"""Backtest a naive Elo-based CFB spread model against a completed season's
DraftKings closing lines, before trusting any forward-looking pick.

Methodology, stated explicitly because this is the part most backtests get
wrong:

1. Elo ratings are pulled per-week (CFBD updates them game by game). CFBD's
   week=N Elo already reflects week N's own results (confirmed empirically:
   using it unlagged makes the model "beat" the market by an impossible
   79% ATS in the biggest-edge bucket -- a dead giveaway of leakage). This
   script defaults to a one-week lag, i.e. week N's game is predicted with
   week N-1's Elo, which is the information actually available before
   kickoff.
2. The elo-divisor/home-field-advantage grid search is fit to minimize
   error against the ACTUAL final margin, never against the market line.
   Only after that fit is chosen do we check how it would have done
   against the market. Tuning parameters directly to beat the closing
   line on the same data used to "test" that edge would be classic
   backtest overfitting -- this keeps the two questions separate.
3. One season (~750 games) is not proof of a durable edge. Treat this as
   a go/no-go signal check before building anything further, not a
   finished result. See model-notes.md's evaluation section.
"""

from __future__ import annotations

import csv
from datetime import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
REPORT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"
API_BASE = "https://api.collegefootballdata.com"

DIVISOR_GRID = [20, 25, 30, 35, 40]
HFA_GRID = [0, 1.5, 2.5, 3.5]
EDGE_BUCKETS = [(0, 1), (1, 3), (3, 5), (5, float("inf"))]


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


def load_games(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def elo_by_week(api_key: str, year: int, weeks: set[str]) -> dict[str, dict[str, float]]:
    cache: dict[str, dict[str, float]] = {}
    for week in sorted(weeks, key=lambda w: int(w)):
        rows = api_get("/ratings/elo", api_key, {"year": str(year), "week": week})
        cache[week] = {row.get("team", ""): row.get("elo") for row in rows if row.get("elo") is not None}
    return cache


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


def build_matched_games(
    games: list[dict[str, str]], elo_cache: dict[str, dict[str, float]], elo_lag: int = 0
) -> list[dict]:
    matched = []
    for game in games:
        week = game.get("week", "")
        lookup_week = str(max(1, int(week) - elo_lag)) if week else week
        week_elo = elo_cache.get(lookup_week, {})
        home_elo = week_elo.get(game.get("home_team", ""))
        away_elo = week_elo.get(game.get("away_team", ""))
        dk_spread = game.get("dk_spread", "")
        if home_elo is None or away_elo is None or dk_spread in ("", None):
            continue
        try:
            actual_margin = float(game["home_score"]) - float(game["away_score"])
            market_home_margin = -float(dk_spread)
        except ValueError:
            continue
        matched.append(
            {
                "home_team": game["home_team"],
                "away_team": game["away_team"],
                "week": week,
                "home_elo": home_elo,
                "away_elo": away_elo,
                "elo_diff": home_elo - away_elo,
                "actual_margin": actual_margin,
                "market_home_margin": market_home_margin,
            }
        )
    return matched


def fit_best_params(matched: list[dict]) -> tuple[float, float, float]:
    best = (None, None, float("inf"))
    for divisor in DIVISOR_GRID:
        for hfa in HFA_GRID:
            errors = [
                abs((g["elo_diff"] / divisor + hfa) - g["actual_margin"]) for g in matched
            ]
            mae = sum(errors) / len(errors)
            if mae < best[2]:
                best = (divisor, hfa, mae)
    return best


def ats_bucket(diff: float) -> str:
    for low, high in EDGE_BUCKETS:
        if low <= diff < high:
            return f"{low}-{high if high != float('inf') else '+'}"
    return "unknown"


def run_backtest(matched: list[dict], divisor: float, hfa: float) -> dict:
    predicted = [g["elo_diff"] / divisor + hfa for g in matched]
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


def write_report(year: int, divisor: float, hfa: float, mae_fit: float, result: dict, elo_lag: int = 0) -> Path:
    diagnostic_warning = (
        [
            "",
            f"**DIAGNOSTIC RUN (`--elo-lag {elo_lag}`), NOT the canonical result.** "
            "The default `--elo-lag 1` run is the leakage-free one. This file exists "
            "to document what look-ahead leakage does to the numbers -- do not use it "
            "to justify a pick.",
            "",
        ]
        if elo_lag != 1
        else []
    )
    lines = [
        f"# CFB Elo Model Backtest — {year} Season",
        "",
        *diagnostic_warning,
        "Methodology: weekly Elo ratings, one-week lag (week N predicted using week N-1's "
        "rating -- see `backtest-cfb-elo.py` docstring for why the unlagged version leaks), "
        "divisor/HFA fit to minimize error against the actual final margin, then checked "
        "against the market separately. One season is a signal check, not proof.",
        "",
        "## Fit",
        "",
        f"- Best-fit Elo divisor: `{divisor}`",
        f"- Best-fit home-field advantage: `{hfa}` points",
        f"- Mean absolute error vs. actual margin (fit target): `{mae_fit:.2f}` points",
        f"- Games with a matched DraftKings closing line and Elo for both teams: {result['n_games']}",
        "",
        "## Does the model track reality at all?",
        "",
        f"- Correlation (model prediction vs. actual margin): `{result['corr_model_actual']:.3f}`",
        f"- Correlation (DraftKings closing line vs. actual margin): `{result['corr_market_actual']:.3f}`",
        f"- MAE (model vs. actual): `{result['mae_model']:.2f}` points",
        f"- MAE (market vs. actual): `{result['mae_market']:.2f}` points",
        "",
        (
            "The market's correlation/MAE should be at least as good as the model's — "
            "closing lines are famously efficient. If the model's correlation is "
            "meaningfully *higher* than the market's here, be suspicious of a bug "
            "before getting excited."
        ),
        "",
        "## ATS record when betting the model's favored side vs. the DraftKings close",
        "",
        "| Model edge vs. market | Record (W-L) | Win % | Games |",
        "| --- | --- | --- | ---: |",
    ]
    for bucket_label, (win, loss, total) in result["buckets"].items():
        pct = f"{100 * win / total:.1f}%" if total else "n/a"
        lines.append(f"| {bucket_label} pts | {win}-{loss} | {pct} | {total} |")

    ah_win, ah_loss, ah_total = result["always_home_ats"]
    ah_pct = f"{100 * ah_win / ah_total:.1f}%" if ah_total else "n/a"
    lines.extend(
        [
            "",
            f"Baseline for context — home team covered {ah_win}/{ah_total} ({ah_pct}) of the time. "
            "A market-efficient world puts every bucket near 50% and the breakeven line at "
            "DraftKings' standard -110 is 52.4%.",
            "",
            "## Reading this honestly",
            "",
            "- Small buckets (especially 5+ points) can look great or terrible from noise alone at "
            "this sample size — check the games column before trusting a percentage.",
            "- This is in-sample for the divisor/HFA fit (fit and tested on the same season). A real "
            "proof needs an out-of-season holdout before any of this touches a live pick.",
            "- Elo alone ignores injuries, weather, pace, and situational factors the checklist in "
            "`instructions.md` requires — this is a first-pass signal check, not a fair-line model.",
            "",
        ]
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"-elolag{elo_lag}-diagnostic" if elo_lag != 1 else ""
    path = REPORT_DIR / f"backtest-cfb-elo-{year}{suffix}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Backtest a naive Elo CFB spread model against a season's closing lines.")
    parser.add_argument("--year", type=int, default=datetime.now(ZoneInfo("America/New_York")).year - 1)
    parser.add_argument(
        "--elo-lag",
        type=int,
        default=1,
        help="Use week-N-minus-lag's Elo to predict week N. Default 1 (the correct, leakage-free "
        "setting, confirmed empirically -- see module docstring). Pass 0 only to reproduce the "
        "known-leaky diagnostic.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("COLLEGE_FOOTBALL_DATA_API_KEY", "").strip()
    if not api_key:
        print("COLLEGE_FOOTBALL_DATA_API_KEY is not set. See ../config.env.", file=sys.stderr)
        return 1

    lines_path = DATA_DIR / f"cfb-lines-history-{args.year}-regular.csv"
    if not lines_path.exists():
        print(f"Run fetch-cfbd-lines-history.py --year {args.year} first.", file=sys.stderr)
        return 1

    games = load_games(lines_path)
    weeks = {g["week"] for g in games if g.get("week")}
    print(f"Fetching weekly Elo for {len(weeks)} weeks of {args.year}...")
    elo_cache = elo_by_week(api_key, args.year, weeks)

    matched = build_matched_games(games, elo_cache, args.elo_lag)
    print(f"Matched {len(matched)} of {len(games)} games to weekly Elo + a DraftKings spread.")
    if len(matched) < 30:
        print("Too few matched games for a meaningful backtest.", file=sys.stderr)
        return 1

    divisor, hfa, mae_fit = fit_best_params(matched)
    result = run_backtest(matched, divisor, hfa)
    report_path = write_report(args.year, divisor, hfa, mae_fit, result, args.elo_lag)
    print(f"Wrote {report_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
