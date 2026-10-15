#!/usr/bin/env python3
"""Assemble the weekly football-picks-agent slate report from whatever data
is currently on disk (schedule CSVs, and odds snapshots if fetch-odds.py has
been run with a configured ODDS_API_KEY).

Never invents a PICK or LEAN from missing data -- every game with no odds
snapshot is reported WATCH with the specific reason, matching the "default
to PASS/WATCH" philosophy in instructions.md.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
NFL_SCHEDULE_PATH = DATA_DIR / "nfl-schedule.csv"
CFB_SCHEDULE_PATH = DATA_DIR / "cfb-schedule.csv"
CFB_RATINGS_PATH = DATA_DIR / "cfb-team-ratings.csv"
NFL_RATINGS_PATH = DATA_DIR / "nfl-team-ratings.csv"
SNAPSHOTS_PATH = AGENT_DIR / "line-watch" / "snapshots.csv"
LEDGER_PATH = AGENT_DIR / "recommendation-ledger.csv"
REPORT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"
ET = ZoneInfo("America/New_York")

WATCHLIST_LIMIT = 15


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_odds_by_matchup() -> tuple[dict[tuple[str, str], list[dict[str, str]]], int]:
    """Key DraftKings snapshot rows by the CSV's (home_team, away_team) columns.

    The Odds API populates those columns with full team names, so they match
    ESPN's full names. ESPN (schedule source) and The Odds API (odds source)
    use unrelated event-ID namespaces, so games must be matched by team name,
    not ID.
    """
    rows = read_csv(SNAPSHOTS_PATH)
    dk_rows = [row for row in rows if row.get("book") == "draftkings"]
    by_matchup: dict[tuple[str, str], list[dict[str, str]]] = {}
    event_ids: set[str] = set()
    for row in dk_rows:
        event_ids.add(row.get("event_id", ""))
        key = (row.get("home_team", ""), row.get("away_team", ""))
        by_matchup.setdefault(key, []).append(row)
    return by_matchup, len(event_ids)


def line_preview(rows: list[dict[str, str]]) -> str:
    parts = []
    h2h = {r["selection"]: r["odds_american"] for r in rows if r.get("market") == "h2h"}
    if h2h:
        parts.append("ML " + ", ".join(f"{team} {price}" for team, price in h2h.items()))
    spreads = {r["selection"]: (r["line_points"], r["odds_american"]) for r in rows if r.get("market") == "spreads"}
    if spreads:
        parts.append(
            "Spread " + ", ".join(f"{team} {pt} ({price})" for team, (pt, price) in spreads.items())
        )
    totals = [(r["selection"], r["line_points"], r["odds_american"]) for r in rows if r.get("market") == "totals"]
    if totals:
        parts.append("Total " + ", ".join(f"{name} {pt} ({price})" for name, pt, price in totals))
    return " · ".join(parts) if parts else "DraftKings line present but no h2h/spreads/totals rows parsed"


def load_cfb_ratings() -> dict[str, dict[str, str]]:
    rows = read_csv(CFB_RATINGS_PATH)
    return {row["team"]: row for row in rows}


def load_nfl_ratings() -> dict[str, dict[str, str]]:
    rows = read_csv(NFL_RATINGS_PATH)
    return {row["team"]: row for row in rows}


def rating_note(team: str, ratings_by_team: dict[str, dict[str, str]]) -> str:
    rating = ratings_by_team.get(team)
    if not rating:
        return ""
    year_flag = "" if rating.get("is_current_season") == "True" else f" ({rating.get('data_year')}, prior season)"
    return f"SP+ {rating.get('sp_plus_rating', '?')} (#{rating.get('sp_plus_ranking', '?')}){year_flag}"


def nfl_rating_note(team: str, ratings_by_team: dict[str, dict[str, str]]) -> str:
    rating = ratings_by_team.get(team)
    if not rating:
        return ""
    year_flag = "" if rating.get("is_current_season") == "True" else f" ({rating.get('data_year')}, prior season)"
    return f"SRS {rating.get('srs_rating', '?')} (#{rating.get('srs_ranking', '?')}){year_flag}"


def freshness_line(label: str, path: Path, rows: list[dict[str, str]], fetcher: str) -> str:
    if not path.exists():
        return f"- **{label}:** not yet fetched — run `python3 {fetcher}`."
    captured = rows[0].get("captured_at_et", "unknown") if rows else "unknown"
    return f"- **{label}:** {len(rows)} rows, captured {captured} (`{path.relative_to(ROOT)}`)"


def watchlist_row(
    game: dict[str, str],
    sport: str,
    odds_rows: list[dict[str, str]] | None,
    cfb_ratings_by_team: dict[str, dict[str, str]],
    nfl_ratings_by_team: dict[str, dict[str, str]],
) -> str:
    matchup = game.get("matchup", "")
    kickoff = game.get("kickoff_et", "") or game.get("kickoff_utc", "")
    status = game.get("status", "")

    parts = []
    if sport == "cfb":
        away_rating = rating_note(game.get("away_team_school", ""), cfb_ratings_by_team)
        home_rating = rating_note(game.get("home_team_school", ""), cfb_ratings_by_team)
        if away_rating or home_rating:
            away_label = game.get("away_team_school", "away")
            home_label = game.get("home_team_school", "home")
            parts.append(f"{away_label}: {away_rating or 'no rating'} vs {home_label}: {home_rating or 'no rating'}")
    else:
        # Preseason guard, 2026-08-12 — mirrors build-review-cards.py. A prior-season
        # SRS describes starters who will play a series or two. Printing it unqualified
        # next to a market line invites a false "disagreement" read: on this slate TEN
        # rated #30 and SF #6, yet SF was a 4.5-point home UNDERDOG. That is a
        # snap-count decision, not an edge.
        is_preseason = str(game.get("season_type", "")).lower() == "preseason"
        away_rating = nfl_rating_note(game.get("away_team", ""), nfl_ratings_by_team)
        home_rating = nfl_rating_note(game.get("home_team", ""), nfl_ratings_by_team)
        if is_preseason:
            parts.append("PRESEASON — prior-season ratings suppressed (they describe starters who play 1-2 series)")
        elif away_rating or home_rating:
            away_label = game.get("away_team", "away")
            home_label = game.get("home_team", "home")
            parts.append(f"{away_label}: {away_rating or 'no rating'} vs {home_label}: {home_rating or 'no rating'}")
    if odds_rows:
        parts.append(f"DraftKings: {line_preview(odds_rows)}")
    else:
        parts.append("no DraftKings line matched yet")
    parts.append("injury/starter review not yet run")
    reason = " — ".join(parts) + " — needs full checklist review before any PICK"
    return f"| {sport.upper()} | {matchup} | {kickoff} | {status} | WATCH | {reason} |"


def ledger_sections(rows: list[dict[str, str]], today: str) -> list[str]:
    """Best Picks / Leans / season CLV, read from the recommendation ledger.

    Per profitable-betting-literature.md: CLV is the season KPI and the report
    must show it next to W-L and say which matters. A decided game whose
    closing-line columns are still blank is called out as a process failure,
    not skipped — an unfilled CLV column is how a losing process hides."""
    picks = [r for r in rows if r.get("decision") == "PICK"]
    leans = [r for r in rows if r.get("decision") == "LEAN"]
    lines: list[str] = ["## Best Picks", ""]

    def rows_block(subset: list[dict[str, str]], empty_text: str) -> list[str]:
        if not subset:
            return [empty_text]
        out = []
        for r in subset:
            clv = r.get("closing_line_value_pct", "")
            result = r.get("result", "") or "pending"
            out.append(
                f"- {r.get('game_date','')} {r.get('league','').upper()} {r.get('matchup','')} — "
                f"{r.get('market','')} {r.get('selection','')} {r.get('line_points','')} "
                f"({r.get('odds_american','')}) · result: {result} · CLV: {clv or '**UNFILLED**'}"
            )
        return out

    lines.extend(rows_block(
        picks,
        "None. No `PICK` has been recorded in `recommendation-ledger.csv`. "
        "That is the expected state: per `model-notes.md` (2026-07-27 backtest) no validated "
        "fair-line model exists, so a PICK only appears here after a human completes the "
        "review-card checklist and logs a decision.",
    ))
    lines.extend(["", "## Leans", ""])
    lines.extend(rows_block(leans, "None recorded."))

    decided = [r for r in picks + leans if r.get("result", "").strip().lower() in ("win", "loss", "push")]
    wins = sum(1 for r in decided if r["result"].strip().lower() == "win")
    losses = sum(1 for r in decided if r["result"].strip().lower() == "loss")
    pushes = len(decided) - wins - losses
    clv_vals = []
    for r in picks + leans:
        try:
            clv_vals.append(float(r.get("closing_line_value_pct", "")))
        except ValueError:
            continue
    unfilled = [
        r for r in picks + leans
        if not r.get("closing_line_value_pct", "").strip()
        and r.get("game_date", "") and r.get("game_date", "") < today
    ]
    lines.extend(["", "## Season Process Scorecard", ""])
    if not picks and not leans:
        lines.append("No decisions logged yet — the scorecard starts with the first PICK/LEAN row.")
    else:
        avg_clv = f"{sum(clv_vals) / len(clv_vals):+.2f}%" if clv_vals else "no CLV rows filled yet"
        lines.append(f"- Record: **{wins}-{losses}-{pushes}** · Season CLV (avg): **{avg_clv}** "
                     f"({len(clv_vals)}/{len(picks) + len(leans)} decisions graded)")
        lines.append("- When record and CLV disagree, **CLV is the signal** — beating the closing "
                     "number is the only self-test that predicts long-run results "
                     "(see `profitable-betting-literature.md`).")
        if unfilled:
            lines.append(f"- ⚠️ **Process failure: {len(unfilled)} decided game(s) with unfilled "
                         "closing-line columns.** Fill them before the next slate; ungraded "
                         "decisions are how a losing process hides.")
    return lines


def main() -> int:
    nfl_games = read_csv(NFL_SCHEDULE_PATH)
    cfb_games = read_csv(CFB_SCHEDULE_PATH)
    odds_by_matchup, odds_event_count = load_odds_by_matchup()
    cfb_ratings = load_cfb_ratings()
    cfb_ratings_rows = read_csv(CFB_RATINGS_PATH)
    nfl_ratings = load_nfl_ratings()
    nfl_ratings_rows = read_csv(NFL_RATINGS_PATH)

    generated_at = datetime.now(ET).replace(microsecond=0).isoformat()
    stamp = datetime.now(ET).strftime("%Y-%m-%d")

    combined = [("nfl", g) for g in nfl_games] + [("cfb", g) for g in cfb_games]
    combined.sort(key=lambda item: item[1].get("kickoff_utc", ""))
    watchlist = combined[:WATCHLIST_LIMIT]

    lines = [
        f"# Football Picks — {stamp}",
        "",
        f"**Report timestamp:** {generated_at}",
        "",
        "## Data Freshness",
        "",
        freshness_line("NFL schedule", NFL_SCHEDULE_PATH, nfl_games, "fetch-nfl-slate.py"),
        freshness_line("CFB (FBS) schedule", CFB_SCHEDULE_PATH, cfb_games, "fetch-cfb-slate.py"),
        (
            f"- **Odds snapshot:** {odds_event_count} DraftKings events covered "
            f"(`{SNAPSHOTS_PATH.relative_to(ROOT)}`)"
            if SNAPSHOTS_PATH.exists()
            else "- **Odds snapshot:** none — `ODDS_API_KEY` not configured. "
            "See `line-watch/readme.md`."
        ),
        freshness_line("CFB team ratings (SP+/PPA/talent)", CFB_RATINGS_PATH, cfb_ratings_rows, "fetch-cfbd-ratings.py"),
        freshness_line("NFL team ratings (SRS)", NFL_RATINGS_PATH, nfl_ratings_rows, "fetch-nfl-team-ratings.py"),
        "",
    ]
    # 2026-08-22: Best Picks / Leans were hardcoded "None" — the report never
    # actually read the ledger it told the user to write to. Now ledger-driven,
    # with the season CLV scorecard required by profitable-betting-literature.md.
    lines.extend(ledger_sections(read_csv(LEDGER_PATH), stamp))
    lines.extend([
        "",
        "## Watchlist",
        "",
        f"Next {len(watchlist)} scheduled games (of {len(combined)} total on the current slate):",
        "",
        "| League | Matchup | Kickoff (ET) | Status | Decision | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for sport, game in watchlist:
        key = (game.get("home_team_full", ""), game.get("away_team_full", ""))
        lines.append(watchlist_row(game, sport, odds_by_matchup.get(key), cfb_ratings, nfl_ratings))

    lines.extend(
        [
            "",
            "## Passes",
            "",
            "No decisions have cleared the checklist yet — every game above is `WATCH`, not `PASS`, "
            "because the required inputs (odds, injury review) have not been checked, not because "
            "they were checked and failed.",
            "",
            "## Model vs Market",
            "",
            "No fair-line model exists. A 2026-07-27 backtest of a naive rating-based CFB spread model "
            "against a full season of real closing lines found it does not beat the market -- see "
            "`model-notes.md`. SP+/PPA/talent (CFB) and SRS (NFL) ratings shown in the Watchlist above "
            "are descriptive context for manual review, not a calibrated prediction or an implied signal.",
            "",
            "## Postgame Review",
            "",
            "N/A — no picks have been made.",
            "",
        ]
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"football-picks-{stamp}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {report_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
