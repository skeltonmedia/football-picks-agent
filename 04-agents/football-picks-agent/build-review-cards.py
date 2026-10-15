#!/usr/bin/env python3
"""Generate fillable pregame-confirmation worksheets, one per upcoming game
with a matched DraftKings line.

This is the actual "manual review workflow" tool: the 2026-07-27 backtest
(see model-notes.md) showed that rating-based models alone don't beat the
closing line, so the real edge in this agent has to come from disciplined
manual review of exactly what instructions.md's Pick Requirements and
billy-walters-operating-doctrine.md's field list call for -- injuries,
weather, situational spots, and price shopping. This script auto-fills
every field the agent can already know (price, key-number proximity, line
movement, rating context) and leaves explicit blanks for what needs a
human to check, so nothing on the checklist gets silently skipped.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
NFL_SCHEDULE_PATH = DATA_DIR / "nfl-schedule.csv"
CFB_SCHEDULE_PATH = DATA_DIR / "cfb-schedule.csv"
CFB_RATINGS_PATH = DATA_DIR / "cfb-team-ratings.csv"
NFL_RATINGS_PATH = DATA_DIR / "nfl-team-ratings.csv"
WEATHER_PATH = DATA_DIR / "weather.csv"
INJURIES_PATH = DATA_DIR / "injuries.csv"
SNAPSHOTS_PATH = AGENT_DIR / "line-watch" / "snapshots.csv"
REPORT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"
ET = ZoneInfo("America/New_York")

# 2026-08-12: was a hard 10, which silently dropped 6 of 16 preseason games
# with no warning in the output — a cap that looks like full coverage is worse
# than no cap. Now a --limit default that reports what it dropped.
CARD_LIMIT = 10
KEY_NUMBERS = {"nfl": {3, 7}, "cfb": {3, 7, 10, 14}}
KEY_NUMBER_PROXIMITY = 1.0


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_odds_by_matchup() -> dict[tuple[str, str], list[dict[str, str]]]:
    rows = read_csv(SNAPSHOTS_PATH)
    dk_rows = [row for row in rows if row.get("book") == "draftkings"]
    by_matchup: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in dk_rows:
        key = (row.get("home_team", ""), row.get("away_team", ""))
        by_matchup.setdefault(key, []).append(row)
    return by_matchup


def load_cfb_ratings() -> dict[str, dict[str, str]]:
    return {row["team"]: row for row in read_csv(CFB_RATINGS_PATH)}


def load_weather() -> dict[str, dict[str, str]]:
    """Kickoff-hour forecast by matchup, from fetch-weather.py. Empty if never run."""
    rows = read_csv(WEATHER_PATH)
    return {r["matchup"]: r for r in rows if r.get("matchup")}


def load_injuries() -> dict[str, list[dict[str, str]]]:
    """NFL injury rows by team abbreviation, from fetch-injuries.py. Empty if never run."""
    by_team: dict[str, list[dict[str, str]]] = {}
    for row in read_csv(INJURIES_PATH):
        if row.get("team_abbrev"):
            by_team.setdefault(row["team_abbrev"], []).append(row)
    return by_team


def injury_summary(team_abbrev: str, injuries_by_team: dict[str, list[dict[str, str]]]) -> str:
    """One line per team: every QB entry by name, everyone else as a count.

    QBs are named because quarterback status is the only single-player input
    that routinely moves a spread by multiple points; the rest of the report
    is a volume signal that tells you whether the depth chart needs a manual
    look, not a substitute for one."""
    rows = injuries_by_team.get(team_abbrev)
    if not rows:
        return "no report on file"
    qbs = [r for r in rows if r.get("is_qb") == "True"]
    notable = [r for r in rows if r.get("status") not in ("Active", "") and r.get("is_qb") != "True"]
    parts = []
    for qb in qbs:
        status = qb.get("status", "?")
        detail = qb.get("injury_type", "")
        parts.append(f"QB {qb.get('player', '?')}: {status}" + (f" ({detail})" if detail else ""))
    parts.append(f"{len(notable)} non-QB not Active")
    return "; ".join(parts)


def load_nfl_ratings() -> dict[str, dict[str, str]]:
    return {row["team"]: row for row in read_csv(NFL_RATINGS_PATH)}


def rating_note(team: str, ratings_by_team: dict[str, dict[str, str]]) -> str:
    rating = ratings_by_team.get(team)
    if not rating:
        return "no rating"
    year_flag = "" if rating.get("is_current_season") == "True" else f" ({rating.get('data_year')}, prior season)"
    return f"SP+ {rating.get('sp_plus_rating', '?')} (#{rating.get('sp_plus_ranking', '?')}){year_flag}"


def nfl_rating_note(team: str, ratings_by_team: dict[str, dict[str, str]]) -> str:
    rating = ratings_by_team.get(team)
    if not rating:
        return "no rating"
    year_flag = "" if rating.get("is_current_season") == "True" else f" ({rating.get('data_year')}, prior season)"
    return f"SRS {rating.get('srs_rating', '?')} (#{rating.get('srs_ranking', '?')}){year_flag}"


def market_lines(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    by_market: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_market.setdefault(row.get("market", ""), []).append(row)
    return by_market


def latest_snapshot_time(rows: list[dict[str, str]]) -> str:
    times = [r.get("captured_at_et", "") for r in rows if r.get("captured_at_et")]
    return max(times) if times else "unknown"


def latest_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Current market state: one row per (market, selection), from the newest
    capture that has it. The full history stays available to movement_lines();
    printing it raw turned every card's snapshot into dozens of repeated
    prices — one per historical fetch — which buried the actual current line
    (found on the first real multi-snapshot cards, 2026-08-22)."""
    newest: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row.get("market", ""), row.get("selection", ""))
        prev = newest.get(key)
        if prev is None or row.get("captured_at_et", "") >= prev.get("captured_at_et", ""):
            newest[key] = row
    return list(newest.values())


def movement_lines(rows: list[dict[str, str]]) -> list[str]:
    """Compare earliest vs. latest snapshot per (market, selection). Needs
    more than one fetch-odds.py run over time to say anything -- on a
    single-snapshot day this honestly reports that, rather than inventing
    a "no movement" claim from a single data point."""
    times = sorted({r.get("captured_at_et", "") for r in rows if r.get("captured_at_et")})
    if len(times) < 2:
        return [f"insufficient history (only {len(times)} snapshot so far -- re-run fetch-odds.py on a later day to start tracking movement)"]

    earliest, latest = times[0], times[-1]
    lines = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        market, selection = row.get("market", ""), row.get("selection", "")
        key = (market, selection)
        if key in seen:
            continue
        seen.add(key)
        open_row = next((r for r in rows if r["captured_at_et"] == earliest and r["market"] == market and r["selection"] == selection), None)
        close_row = next((r for r in rows if r["captured_at_et"] == latest and r["market"] == market and r["selection"] == selection), None)
        if not open_row or not close_row:
            continue
        open_price, close_price = open_row.get("odds_american", ""), close_row.get("odds_american", "")
        open_pt, close_pt = open_row.get("line_points", ""), close_row.get("line_points", "")
        if open_price == close_price and open_pt == close_pt:
            continue
        lines.append(f"{market}/{selection}: {open_pt} ({open_price}) -> {close_pt} ({close_price})")
    if not lines:
        lines.append("no movement yet between first and latest snapshot")
    return lines


def key_number_check(sport: str, spread_rows: list[dict[str, str]]) -> str:
    keys = KEY_NUMBERS.get(sport, set())
    flags = []
    for row in spread_rows:
        try:
            point = abs(float(row.get("line_points", "")))
        except ValueError:
            continue
        for key in keys:
            if abs(point - key) <= KEY_NUMBER_PROXIMITY:
                flags.append(f"{row.get('selection', '')} {row.get('line_points', '')} is within {KEY_NUMBER_PROXIMITY} of key number {key}")
    return "; ".join(flags) if flags else f"no key number ({', '.join(str(k) for k in sorted(keys))}) within {KEY_NUMBER_PROXIMITY} pts"


def build_card(
    sport: str,
    game: dict[str, str],
    odds_rows: list[dict[str, str]],
    cfb_ratings_by_team: dict[str, dict[str, str]],
    nfl_ratings_by_team: dict[str, dict[str, str]],
    weather_by_matchup: dict[str, dict[str, str]] | None = None,
    injuries_by_team: dict[str, list[dict[str, str]]] | None = None,
) -> str:
    matchup = game.get("matchup", "")
    kickoff = game.get("kickoff_et", "") or game.get("kickoff_utc", "")
    # Snapshot and key-number sections describe the CURRENT market;
    # movement_lines() below still reads the full history.
    by_market = market_lines(latest_rows(odds_rows))

    lines = [f"### {matchup} — {sport.upper()} — {kickoff}", ""]
    lines.append(f"**Market snapshot** (DraftKings, captured {latest_snapshot_time(odds_rows)})")
    for market_key, label in (("h2h", "Moneyline"), ("spreads", "Spread"), ("totals", "Total")):
        rows = by_market.get(market_key, [])
        if not rows:
            lines.append(f"- {label}: not posted")
            continue
        parts = []
        for r in rows:
            selection, point, price = r.get("selection", ""), r.get("line_points", ""), r.get("odds_american", "")
            entry = f"{selection} {point} ({price})" if point else f"{selection} ({price})"
            parts.append(entry)
        lines.append(f"- {label}: " + ", ".join(parts))
    lines.append("- Line movement: " + "; ".join(movement_lines(odds_rows)))
    lines.append("- Key-number check: " + key_number_check(sport, by_market.get("spreads", [])))
    lines.append("")

    lines.append("**Context**")
    if sport == "cfb":
        away = game.get("away_team_school", "")
        home = game.get("home_team_school", "")
        lines.append(f"- CFB rating: {away} {rating_note(away, cfb_ratings_by_team)} vs {home} {rating_note(home, cfb_ratings_by_team)}")
    else:
        away = game.get("away_team", "")
        home = game.get("home_team", "")
        lines.append(f"- NFL rating: {away} {nfl_rating_note(away, nfl_ratings_by_team)} vs {home} {nfl_rating_note(home, nfl_ratings_by_team)}")
        # Preseason guard, added 2026-08-12. A prior-season SRS describes a
        # starting roster that will play somewhere between zero and two series.
        # Showing it unqualified invites exactly the wrong read: on 2026-08-13
        # DET rated #10 and CIN #23, yet CIN was a 6.5-point home favourite --
        # a "disagreement" that is really just a snap-count decision. A rating
        # that is actively misleading is worse than no rating at all.
        if str(game.get("season_type", "")).lower() == "preseason":
            lines.append(
                "- ⚠️ **PRESEASON — ignore the rating line above.** It describes last "
                "season's starters, who will play a series or two at most. Rating-vs-market "
                "disagreement here is snap-count noise, not signal. The decisive input is who "
                "plays and for how long, and that is usually unknown until hours before kickoff."
            )
    lines.append(
        "- Reminder: no validated fair-line model exists (2026-07-27 backtest showed ratings alone don't "
        "beat the closing line -- see model-notes.md). Your number below is a manual judgment call, not a model output."
    )
    lines.append("")

    lines.append("**Fill in before any PICK/LEAN** (per instructions.md + billy-walters-operating-doctrine.md)")
    if sport == "nfl" and injuries_by_team:
        away_abbr, home_abbr = game.get("away_team", ""), game.get("home_team", "")
        injury_captured = next(
            (r[0].get("captured_at_et", "")[:16]
             for r in (injuries_by_team.get(away_abbr), injuries_by_team.get(home_abbr)) if r),
            "",
        )
        injury_line = (
            f"- Injury/QB status (ESPN report{', fetched ' + injury_captured if injury_captured else ''} "
            f"— confirm before PICK): {away_abbr}: {injury_summary(away_abbr, injuries_by_team)} | "
            f"{home_abbr}: {injury_summary(home_abbr, injuries_by_team)}"
        )
    else:
        # No league-wide CFB injury feed exists (college programs aren't
        # required to report). Manual by design, not by omission.
        injury_line = "- Injury/QB status (both teams), timestamp checked:"
    lines.extend(
        [
            injury_line,
            "- Starter/lineup confirmation:",
            (
            "- Weather: " + (
                f"**{w['temperature_f']}°F, wind {w['wind']} {w['wind_direction']}, "
                f"{w['precip_probability']}% precip — {w['short_forecast']}** "
                f"(forecast for {w['forecast_time']}, fetched {w['captured_at_et'][:16]})"
                if (w := (weather_by_matchup or {}).get(game.get("matchup", ""))) and w.get("temperature_f")
                else "indoor or no forecast on file — run fetch-weather.py"
            )
        ),
            "- Rest, travel, situational notes (revenge/lookahead/short week):",
            "- Your fair line or fair probability:",
            "- If reviewing a total — market-implied away/home score (total + spread):",
            "- If reviewing a total — independently reasoned away/home score ranges (do not reuse the automated blend):",
            "- If reviewing a total — favorite scoring ceiling and evidence:",
            "- If reviewing a total — underdog minimum contribution and evidence:",
            "- If reviewing a total — explosive-play matchup and finishing-drives context:",
            "- If reviewing a total — turnover regression and non-offensive-score risk:",
            "- If reviewing a total — weather and offensive availability impact:",
            "- If reviewing a total — garbage-time path:",
            "- If reviewing a total — exact play/pass number and maximum 1u stake:",
            "- Is the current price still playable at your number?",
            "- Main risk:",
            "- Confidence:",
            "- **Decision:** PICK / LEAN / WATCH / PASS",
            "- **Reason:**",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build fillable pregame review cards.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max cards (0 = no limit). Default %d." % CARD_LIMIT)
    args = parser.parse_args()

    nfl_games = read_csv(NFL_SCHEDULE_PATH)
    cfb_games = read_csv(CFB_SCHEDULE_PATH)
    odds_by_matchup = load_odds_by_matchup()
    cfb_ratings = load_cfb_ratings()
    nfl_ratings = load_nfl_ratings()
    weather = load_weather()
    injuries = load_injuries()

    combined = [("nfl", g) for g in nfl_games] + [("cfb", g) for g in cfb_games]
    combined.sort(key=lambda item: item[1].get("kickoff_utc", ""))

    # A review card for a game that already kicked off is dead weight, and
    # worse than dead weight under --limit: the soonest-first sort put played
    # preseason games in the top 10 and pushed the entire live Week 1 slate
    # into the "dropped" count (found 2026-08-22). Unparsable kickoffs are
    # kept, not dropped — a card for an unknown time beats a silent skip.
    now_utc = datetime.now(dt_timezone.utc)
    already_played = 0
    upcoming: list[tuple[str, dict]] = []
    for sport, game in combined:
        raw = (game.get("kickoff_utc", "") or "").replace("Z", "+00:00")
        try:
            kickoff = datetime.fromisoformat(raw)
        except ValueError:
            upcoming.append((sport, game))
            continue
        if kickoff < now_utc:
            already_played += 1
            continue
        upcoming.append((sport, game))

    cards = []
    matched: list[tuple[str, dict, list]] = []
    for sport, game in upcoming:
        key = (game.get("home_team_full", ""), game.get("away_team_full", ""))
        odds_rows = odds_by_matchup.get(key)
        if not odds_rows:
            continue
        matched.append((sport, game, odds_rows))

    limit = args.limit if args.limit is not None else CARD_LIMIT
    if limit == 0:
        limit = len(matched)
    dropped = max(0, len(matched) - limit)
    for sport, game, odds_rows in matched[:limit]:
        cards.append(build_card(sport, game, odds_rows, cfb_ratings, nfl_ratings, weather, injuries))

    stamp = datetime.now(ET).strftime("%Y-%m-%d")
    generated_at = datetime.now(ET).replace(microsecond=0).isoformat()
    header = [
        f"# Review Cards — {stamp}",
        "",
        f"**Generated:** {generated_at}",
        "",
        f"{len(cards)} upcoming games with a matched DraftKings line, soonest first"
        + (f" — **{dropped} more matched games were dropped by the --limit cap**" if dropped else "")
        + ". "
        "Games with no line posted yet aren't worth a card -- they stay `WATCH` in the weekly "
        "report until a line exists.",
        "",
        "---",
        "",
    ]

    if not cards:
        header.append("No games currently have a matched DraftKings line. Run fetch-odds.py closer to game day.")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"review-cards-{stamp}.md"
    path.write_text("\n".join(header) + "\n\n---\n\n" + "\n\n---\n\n".join(cards), encoding="utf-8")
    print(f"Wrote {len(cards)} review cards to {path.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
