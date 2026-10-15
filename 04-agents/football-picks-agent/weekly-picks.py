#!/usr/bin/env python3
"""weekly-picks.py — price-edge research board: spreads, ML, and totals.

    python3 -B 04-agents/football-picks-agent/weekly-picks.py
    python3 -B 04-agents/football-picks-agent/weekly-picks.py --imessage
    python3 -B 04-agents/football-picks-agent/weekly-picks.py --count 10 --league cfb
    python3 -B 04-agents/football-picks-agent/weekly-picks.py --log

WHY (2026-08-28). David: "top 10 picks a week with strength rankings, picks
could be an over or under as well as a ML, confidence score is key. Make
output easy to paste to iMessage." David added spreads to the same global
confidence ranking on 2026-09-03.

GAME-DAY USER-FACING ROUTE (2026-09-20; AQ-257 expanded NCAA on 2026-09-27).
This file remains the cross-book edge/CLV research selector. It does not answer
“which pick is most likely to win?” Use `cash-confidence.py --league nfl|ncaaf`
for Saturday NCAA and Sunday NFL cash boards; it ranks consensus no-vig cash
probability and retains one selection per game.

WHAT THE CONFIDENCE SCORE IS — AND IS NOT
-----------------------------------------
RANKING v3 (2026-09-18, AQ-220/221). The composite below was checked against
its own KPI at 45 decisions: correlation with closing-line value 0.05, and its
40-point book-disagreement term correlates 0.00 with CLV on 1,106 game-markets
from our own line log (backtest-signals.py). The board now RANKS by `edge`:
the DraftKings price versus the consensus of the other books, in cents of
implied probability (points markets add points x the CLV point value). Stars
and units come from the edge; the edge floor (EDGE_FLOOR_CENTS) decides which
rows carry units at all, everything under it prints as "board only". The old
composite is kept as `legacy_score` and logged beside the edge for one month.

It is NOT a probability of winning. Nothing here has earned the right to
publish one: both backtests (model-notes.md) showed rating models losing to
the closing line, so any "72% confident" number would be decoration.

It is a 0-100 ranking strength, composed of things that are individually
defensible and individually visible in the output:

  BOOK DISAGREEMENT (0-40).  Five books, one game. When they hang materially
      different prices the market is unsure and somebody's number is soft.
      When they agree to the decimal, a manual read almost certainly is not
      beating them.
  MODEL-MARKET GAP (0-35).  How far our number sits from the market's, CAPPED
      and then tapered — because the NFL backtest's largest-disagreement
      bucket went 41.9%, the wrong way. A huge gap usually means a stale
      rating, not an opportunity. Moderate gaps score highest.
  PRICE QUALITY AT DRAFTKINGS (0-15).  David bets DK. The same opinion at a
      worse number is worth less, and the report says when FanDuel beats it.
  DATA QUALITY (0-10).  Current-season ratings, both teams matched, forecast
      available. Preseason NFL scores zero and is excluded outright.

Stars are the same number in fewer characters: 5★ = 80+, 4★ = 65+, 3★ = 50+,
2★ = 35+, 1★ below.

WRITE-UPS (2026-08-28)
----------------------
David: "include write ups on the edge and why, weather, anything relevant."
Each pick in the full report carries:
  price      DK, best available across books, what $2 returns
  case       the ratings behind the side, with ranks — or the totals model
             vs the posted number
  market     how far apart the books are, read in plain language
  breaks if  what would invalidate it, stated before the game not after
  movement   opening number vs now, and whether money moved with or against
  personnel  non-Active QBs, the input most likely to blow up the case
  conditions temperature, wind, precipitation — on moneylines too, since wind
             suppresses scoring regardless of which market you bet
  price flag when FanDuel beats DK on the identical bet
The iMessage block carries a compressed one-liner so a pasted board still
explains itself.

MARKETS
-------
  ML      side chosen by rating gap with a 2.5-point home adjustment.
  SPREAD  side whose DraftKings spread leaves at least the configured cover
          gap against that same rating margin. It uses the existing side-gap
          confidence curve; it is a ranking input, not a claimed win rate.
  TOTAL   CFB only. Expected points per team = (that team's SP+ offense +
          opponent's SP+ defense) / 2; the two summed and compared to the
          posted number. Calibrates sanely (NC State/Virginia: model 50.9 vs
          market 51.5). NFL is excluded because the only NFL rating available
          is SRS, a margin measure that says nothing about total scoring.

RANKING CONTRACT (2026-09-01)
-----------------------------
David: "top 10 picks moneyline, over, or under and only rank by confidence."
On 2026-09-03 David expanded the eligible set to include spreads.
After eligibility gates, the board is the global top 10 by the full numeric
confidence score. It does not prefer unique games, a market type, a manual
status, or a stake allocation. Exact-score ties use identifiers only so reruns
are deterministic. Correlated side/total entries stay visible when both qualify.

HOW DAVID USES IT (2026-08-28)
------------------------------
"I'm not going to place all 10, just ones I like — it's a guide for me."
So this is a ranked menu, not a slate to take whole. Every selection is logged
and settled regardless of whether he tails it, which is what makes the record
honest: grading only the ones he liked would sample on his own confidence and
flatter the results.

Never places a bet. `--log` writes selections to the ledger so grade-clv.py
can score them against the close, which is the only test that settles whether
any of this works.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402
from football_team_resolution import (  # noqa: E402
    build_school_crosswalk,
    canonical_cfb_school,
    resolve_team_record,
)

DATA = AGENT / "data"
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
LEDGER = AGENT / "recommendation-ledger.csv"
BASE_BOOK, COMPARE_BOOK = "draftkings", "fanduel"
ALLOWED_MARKETS = frozenset({"h2h", "spreads", "totals"})
TRACKED_DECISIONS = frozenset({"PICK", "LEAN"})
MIN_ODDS, MAX_ODDS = -250, 350
MAX_ODDS_AGE_H = 6.0
EASTERN = ZoneInfo("America/New_York")


def config_float(name: str, default: float) -> float:
    """Read one non-secret numeric setting from env or the local config."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        cfg = AGENT / "config.env"
        if cfg.exists():
            for line in cfg.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(f"{name}="):
                    raw = stripped.split("=", 1)[1].strip()
                    break
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


MIN_SPREAD_EDGE_POINTS = config_float("MIN_SPREAD_EDGE_POINTS", 1.5)

# Live focus knobs. David restored full-game totals to every league board on
# 2026-09-12. `TOTALS_ELIGIBLE` remains configurable for reproducible history,
# but the live config is on. Big-dog and thin-moneyline controls are unchanged.
# Defaults keep the broad market behaviour so tests and history are stable.
TOTALS_ELIGIBLE = config_float("TOTALS_ELIGIBLE", 1.0) >= 1.0
MAX_DOG_SPREAD_POINTS = config_float("MAX_DOG_SPREAD_POINTS", 0.0)  # 0 = no cap
MIN_ML_DISAGREE_PCT = config_float("MIN_ML_DISAGREE_PCT", 0.0)     # 0 = no floor


def focus_filter(candidates: list[dict], *, totals_eligible: bool | None = None,
                 max_dog_spread: float | None = None,
                 min_ml_disagree: float | None = None) -> tuple[list[dict], dict]:
    """Drop candidates outside the current focus. Returns (kept, suppressed counts).

    Runs before ranking so the top 10 is still the global top 10 of what is
    eligible; it never reorders survivors.
    """
    totals_eligible = TOTALS_ELIGIBLE if totals_eligible is None else totals_eligible
    max_dog_spread = MAX_DOG_SPREAD_POINTS if max_dog_spread is None else max_dog_spread
    min_ml_disagree = MIN_ML_DISAGREE_PCT if min_ml_disagree is None else min_ml_disagree
    kept, dropped = [], {"totals": 0, "big_dog": 0, "ml_disagree": 0}
    for c in candidates:
        mk = c.get("market_key")
        if mk == "totals" and not totals_eligible:
            dropped["totals"] += 1
            continue
        if mk == "spreads" and max_dog_spread > 0 and (c.get("line") or 0) > max_dog_spread:
            dropped["big_dog"] += 1
            continue
        if mk == "h2h" and min_ml_disagree > 0 and (c.get("disagree") or 0) < min_ml_disagree:
            dropped["ml_disagree"] += 1
            continue
        kept.append(c)
    return kept, dropped


def focus_notice(dropped: dict) -> str:
    parts = []
    if dropped.get("totals"):
        parts.append(f"{dropped['totals']} total(s) watch-only (no validated totals model)")
    if dropped.get("big_dog"):
        parts.append(f"{dropped['big_dog']} dog(s) past +{MAX_DOG_SPREAD_POINTS:g}")
    if dropped.get("ml_disagree"):
        parts.append(f"{dropped['ml_disagree']} ML under {MIN_ML_DISAGREE_PCT:g}% book disagreement")
    return "focus: suppressed " + ", ".join(parts) if parts else ""


def prob(o: float) -> float:
    return 100.0 / (o + 100.0) if o > 0 else -o / (-o + 100.0)


def profit(o: float, stake: float) -> float:
    return stake * o / 100 if o > 0 else stake * 100 / -o


def norm(s: str) -> str:
    # ASCII-fold first: Hawai'i / Hawaiʻi / Hawaii must all normalize equal
    # (the 2026-08-29 apostrophe lesson — a missed game is a silent miss).
    import unicodedata as _ud
    s = _ud.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in s.lower() if c.isalnum())


def load(name: str) -> list[dict]:
    p = DATA / name
    return list(csv.DictReader(p.open(encoding="utf-8"))) if p.exists() else []


EDGE_FLOOR_CENTS = config_float("EDGE_FLOOR_CENTS", 1.5)        # units below this: 0 (board only)
EDGE_TWO_UNIT_CENTS = config_float("EDGE_TWO_UNIT_CENTS", 2.0)  # 2 units at or above this


def star_count(score: float) -> int:
    """Stars are cents of edge vs consensus: the backtest ladder (>=1.0 beat 58-61%,
    >=1.5 67-69%, >=2.0 70-73%)."""
    return 5 if score >= 2.0 else 4 if score >= 1.5 else 3 if score >= 1.0 else 2 if score >= 0.5 else 1


def nfl_full_name_crosswalk(schedule_rows: list[dict]) -> dict[str, str]:
    """Map a normalized NFL full team name to its normalized ESPN abbreviation.

    WHY (2026-09-02): weekly-picks.py's team-rating lookup used a generic
    substring matcher built for CFB's long full names. Against NFL's 2-3
    letter codes it silently matched the wrong team's rating into a live
    pick -- "ne" matched inside "buccaNEers" (Tampa Bay), inside "New
    Orleans", and inside both NY teams' names; "phi" matched inside
    "miamidolPHIns"; "car" inside "arizonaCARdinals"; "chi" inside
    "kansascityCHIefs" -- and 7 of 32 teams (49ers, Rams, Jaguars, Raiders,
    Packers, Commanders, Chargers) matched nothing at all, with no warning
    either way. nfl-schedule.csv already carries both the full name and the
    exact abbreviation for the same game; an exact crosswalk from that has no
    ambiguity to resolve. CFB uses the same schedule-authority principle via
    its full-name-to-school crosswalk.
    """
    crosswalk: dict[str, str] = {}
    for r in schedule_rows:
        for full_col, abbr_col in (("away_team_full", "away_team"), ("home_team_full", "home_team")):
            if r.get(full_col) and r.get(abbr_col):
                crosswalk[norm(r[full_col])] = norm(r[abbr_col])
    return crosswalk


def stars(score: float) -> str:
    n = star_count(score)
    return "★" * n + "☆" * (5 - n)


def units_for_score(score: float) -> int:
    """AQ-221 edge floor: 0 units under EDGE_FLOOR_CENTS, 2 at EDGE_TWO_UNIT_CENTS."""
    if score >= EDGE_TWO_UNIT_CENTS:
        return 2
    return 1 if score >= EDGE_FLOOR_CENTS else 0


def automatic_board_units(candidate: dict) -> int:
    """Fail closed on automated CFB totals until a manual fair-total review.

    The leakage-free 2025 backtest found that the SP+ blend lost to the closing
    total (13.02 MAE versus 12.20) and returned only breakeven hit rates across
    disagreement buckets. Cross-book price edge is still useful for ranking a
    review menu, but it cannot turn that unvalidated projection into a stake.
    A completed manual review may promote a total separately, capped at 1u by
    staking-config.md; this automated board must print 0u meanwhile.
    """
    if candidate.get("league") == "cfb" and candidate.get("market_key") == "totals":
        return 0
    return units_for_score(float(candidate.get("score", 0.0)))


def _units_for_score_legacy(score: float) -> int:
    """Locked sizing rule: five-star plays are 2u; every other play is 1u."""
    return 2 if star_count(score) == 5 else 1


def kickoff_label(start: datetime.datetime) -> str:
    """Show the calendar date so a seven-day board cannot look already stale."""
    return start.astimezone(EASTERN).strftime("%a %b %-d · %-I:%M%p ET")


def usable_forecast(weather: dict | None) -> bool:
    """A matched venue row is not a forecast when its values are blank."""
    return bool(weather and str(weather.get("temperature_f") or "").strip())


def fresh_snapshot(row: dict, as_of: datetime.datetime,
                   max_age_h: float = MAX_ODDS_AGE_H) -> bool:
    """Reject a stale offer even when another book refreshed the same game.

    The global preflight proves that *some* odds refreshed recently. It does
    not prove that every book/market tuple did. Without this row-level gate,
    a month-old DraftKings price can win today's best-price comparison beside
    current offers from other books.
    """
    try:
        captured = datetime.datetime.fromisoformat(row["captured_at_et"])
    except (KeyError, TypeError, ValueError):
        return False
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=EASTERN)
    age_h = (as_of - captured).total_seconds() / 3600
    return -0.25 <= age_h <= max_age_h


def eligible_pick(candidate: dict) -> bool:
    """Final market guard: spread, moneyline, over, or under."""
    kind = candidate.get("kind")
    market = candidate.get("market_key")
    label = str(candidate.get("label") or "").strip().lower()
    if kind == "ML":
        return market == "h2h"
    if kind == "SPREAD":
        try:
            float(candidate.get("line"))
        except (TypeError, ValueError):
            return False
        return market == "spreads" and bool(str(candidate.get("selection") or "").strip())
    if kind == "TOTAL":
        return market == "totals" and label.startswith(("over ", "under "))
    return False


def confidence_sort_key(candidate: dict) -> tuple:
    """Confidence is the sole quality rank; remaining fields break exact ties."""
    return (
        -float(candidate["score"]),
        -float(candidate.get("legacy_score", 0.0)),
        str(candidate.get("eid") or ""),
        str(candidate.get("market_key") or ""),
        str(candidate.get("label") or ""),
    )


def _line_identity(value: object) -> str:
    try:
        return f"{float(value):g}" if str(value).strip() else ""
    except (TypeError, ValueError):
        return str(value or "").strip()


def recommendation_identity(row: dict) -> tuple[str, str, str, str, str]:
    """Stable key for idempotent same-day recommendation logging."""
    return (
        str(row.get("decision_date") or ""),
        str(row.get("event_id") or ""),
        str(row.get("market") or "").lower(),
        str(row.get("selection") or "").strip().lower(),
        _line_identity(row.get("line_points")),
    )


def select_picks(candidates: list[dict], count: int) -> list[dict]:
    """Return the globally highest-confidence eligible plays, in order.

    No unique-game pass, manual target label, unit allocation, or market-type
    preference may reorder the board. Multiple markets from one matchup can
    appear when their scores belong in the global top 10; the report labels
    them as correlated rather than silently replacing any one of them.
    """
    if count <= 0:
        return []
    eligible = [candidate for candidate in candidates if eligible_pick(candidate)]
    return sorted(eligible, key=confidence_sort_key)[:count]


def nationally_supportive(rank: str | None, cutoff: int = 50) -> bool:
    """Only describe a national unit rank as evidence when it is top-50."""
    try:
        return int(rank or "") <= cutoff
    except ValueError:
        return False


def _signals_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("backtest_signals", AGENT / "backtest-signals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


MIN_CONSENSUS_BOOKS = int(config_float("MIN_CONSENSUS_BOOKS", 2))


def price_edge(c: dict, ml: dict, spr: dict, tot: dict, sig) -> tuple[float, int] | None:
    """DraftKings vs the consensus of the other books, from the bettor's seat,
    in cents of implied probability — the same dk_vs_consensus_cents the
    backtest ranked by. Returns (edge, other-book count); None when fewer than
    MIN_CONSENSUS_BOOKS other books price the market (one book is not a consensus)."""
    eid, sel, mk = c["eid"], c["selection"], c["market_key"]
    try:
        if mk == "h2h":
            rows = {b: (None, o) for b, o in ml[eid][sel].items()}
            other_sel = next((s for s in ml[eid] if s != sel), None)
            if other_sel is None:
                return None
            rows2 = {b: (None, o) for b, o in ml[eid][other_sel].items()}
            nv = sig._novig(rows, rows2)
            nv_dk = nv.get(BASE_BOOK)
            others = [p for b, p in nv.items() if b != BASE_BOOK]
            if nv_dk is None or len(others) < MIN_CONSENSUS_BOOKS:
                return None
            return (statistics.mean(others) - nv_dk) / nv_dk * 100.0, len(others)
        books = (spr if mk == "spreads" else tot)[eid][sel]
        dk_pts, dk_odds = books[BASE_BOOK]
        others = [(p, o) for b, (p, o) in books.items() if b != BASE_BOOK]
        if len(others) < MIN_CONSENSUS_BOOKS:
            return None
        cons_pts = statistics.mean(p for p, _ in others)
        cons_impl = statistics.mean(sig.implied(o) for _, o in others)
        return sig.edge_pct(mk, sel, c["league"], dk_odds, dk_pts, cons_impl, cons_pts)[0], len(others)
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return None


def taper(gap: float, best: float, cap: float) -> float:
    """Peak at `best`, fall off both directions, zero past `cap`.
    Encodes the backtest finding that the biggest disagreements were worst."""
    if gap <= 0 or gap >= cap:
        return 0.0
    return 1.0 - abs(gap - best) / max(best, cap - best)


def spread_cover_edge(projected_away_margin: float, selection: str,
                      away_team: str, home_team: str,
                      spread_points: float) -> float | None:
    """Return points of cover room at the offered spread for one side.

    `projected_away_margin` is positive when the ratings favor the away team.
    A higher spread number is always better for the selected team, so the
    cover edge is its projected margin plus its offered points.
    """
    if norm(selection) == norm(away_team):
        return projected_away_margin + spread_points
    if norm(selection) == norm(home_team):
        return -projected_away_margin + spread_points
    return None


def spread_movement_alignment(open_line: float, current_line: float) -> str:
    """Describe whether the spread market moved toward the selected team.

    Lines here are stored from the selected team's perspective. A smaller
    number means the market strengthened that team (favorite -7 to -8, or
    underdog +7 to +6); a larger number gives the bettor a better entry only
    because the market moved against the team.
    """
    shift = current_line - open_line
    if abs(shift) < 0.5:
        return "steady"
    return "with us" if shift < 0 else "against us"


def spread_key_note(line: float, league: str) -> str:
    """Describe proximity to sport-specific full-game key numbers."""
    keys = (3, 7) if league == "nfl" else (3, 7, 10, 14)
    nearest = min(keys, key=lambda key: abs(abs(line) - key))
    distance = abs(abs(line) - nearest)
    if distance == 0:
        return f"on key number {nearest}"
    if distance <= 0.5:
        return f"within a half-point of key number {nearest}"
    return "away from a primary key number"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--hours", type=int, default=168,
                    help="lookahead. 168h = a full week of games; a shorter window\n                         silently shrinks the board to whatever one day offers")
    ap.add_argument("--league", choices=["nfl", "cfb", "all"], default="all")
    ap.add_argument("--stake", type=float, default=2.0)
    ap.add_argument("--imessage", action="store_true", help="compact paste-ready output")
    ap.add_argument("--log", action="store_true")
    args = ap.parse_args()

    snapshot_as_of = now_eastern()
    latest, meta = {}, {}
    newest_capture = ""   # true newest pull on file, before the freshness gate
    for r in csv.DictReader(SNAPSHOTS.open(encoding="utf-8")):
        if r["captured_at_et"] > newest_capture:
            newest_capture = r["captured_at_et"]
        if r["market"] not in ALLOWED_MARKETS:
            continue
        if not fresh_snapshot(r, snapshot_as_of):
            continue
        k = (r["event_id"], r["market"], r["selection"], r["book"])
        if k not in latest or r["captured_at_et"] > latest[k]["captured_at_et"]:
            latest[k] = r
        meta[r["event_id"]] = r

    ml = defaultdict(lambda: defaultdict(dict))     # eid -> sel -> book -> odds
    spr = defaultdict(lambda: defaultdict(dict))    # eid -> sel -> book -> (points, odds)
    tot = defaultdict(lambda: defaultdict(dict))    # eid -> sel -> book -> (points, odds)
    for (eid, market, sel, book), r in latest.items():
        try:
            odds = float(r["odds_american"])
        except (ValueError, TypeError):
            continue
        if market == "h2h":
            ml[eid][sel][book] = odds
        elif market in {"spreads", "totals"}:
            try:
                target = spr if market == "spreads" else tot
                target[eid][sel][book] = (float(r["line_points"]), odds)
            except (ValueError, TypeError):
                continue

    cfb_r = {norm(r["team"]): r for r in load("cfb-team-ratings.csv")}
    nfl_r = {norm(r["team"]): r for r in load("nfl-team-ratings.csv")}
    defs = [float(r["sp_defense_rating"]) for r in cfb_r.values() if r.get("sp_defense_rating")]
    AVG_DEF = statistics.mean(defs) if defs else 26.0
    # Offense/defense ranks (SP+ offense: higher better; defense: lower better).
    _off = sorted((r for r in cfb_r.values() if r.get("sp_offense_rating")),
                  key=lambda r: -float(r["sp_offense_rating"]))
    _def = sorted((r for r in cfb_r.values() if r.get("sp_defense_rating")),
                  key=lambda r: float(r["sp_defense_rating"]))
    # Stamp ranks onto the rating records so the fuzzy team-name matching in
    # rating_for() carries them for free (name-keyed lookups miss: the odds
    # feed says "New Mexico State Aggies", the ratings say "New Mexico State").
    for i, r in enumerate(_off):
        r["_off_rank"] = str(i + 1)
    for i, r in enumerate(_def):
        r["_def_rank"] = str(i + 1)

    weather = {norm(r["matchup"]): r for r in load("weather.csv")}
    # Situational spot flags (AQ-132) — display-only until they earn weight
    # from their own settled record; carried into ledger reasons so the
    # record accrues per flag type.
    spot = {}
    for r in load("spot-flags.csv"):
        spot[frozenset((norm(r["away_team"]), norm(r["home_team"])))] = r["flags"]
    cfb_schedule_rows = load("cfb-schedule.csv")
    cfb_full_to_school = build_school_crosswalk(cfb_schedule_rows)
    # Neutral-site + international flags (the Dublin lesson, 2026-08-29:
    # neutral_site=True and state="" were in the schedule all along, unused —
    # TCU got a home bump and an Ohio forecast for a game in Ireland).
    neutral, intl = set(), set()
    for sched in ("cfb-schedule.csv", "nfl-schedule.csv"):
        for r in load(sched):
            if sched.startswith("cfb"):
                key = frozenset((norm(r.get("away_team_school", "")),
                                 norm(r.get("home_team_school", ""))))
            else:
                key = frozenset((norm(r.get("away_team_full", "")),
                                 norm(r.get("home_team_full", ""))))
            if r.get("neutral_site") == "True":
                neutral.add(key)
            if not (r.get("state") or "").strip() and r.get("indoor") != "True":
                intl.add(key)
    # QB status: the single input that moves a football number most.
    IMPACT = {"QB", "RB", "WR", "TE", "OT", "G", "C", "LT", "RT"}  # scoring-side
    OUT_STATUSES = {"Out", "Injured Reserve", "Doubtful", "Suspension"}
    qbs, hurt = defaultdict(list), defaultdict(list)
    for r in load("injuries.csv"):
        status, pos = r.get("status", ""), r.get("position", "")
        if status in ("Active", ""):
            continue
        note = f"{r['player']} {pos} {status}" + (f" ({r['injury_type']})" if r.get("injury_type") else "")
        if pos == "QB":
            qbs[norm(r["team"])].append(note)
        elif pos in IMPACT or status in OUT_STATUSES:
            hurt[norm(r["team"])].append((status in OUT_STATUSES, note))
    # Opening price per selection, so the write-up can show which way money moved.
    opens: dict = {}
    for r in csv.DictReader(SNAPSHOTS.open(encoding="utf-8")):
        if r["market"] not in ALLOWED_MARKETS or r["book"] != BASE_BOOK:
            continue
        k = (r["event_id"], r["market"], r["selection"])
        if k not in opens or r["captured_at_et"] < opens[k]["captured_at_et"]:
            opens[k] = r
    full2mk = defaultdict(set)
    for sched in ("cfb-schedule.csv", "nfl-schedule.csv"):
        for r in load(sched):
            for col in ("away_team_full", "home_team_full"):
                if r.get(col):
                    full2mk[norm(r[col])].add(norm(r.get("matchup", "")))
            if sched.startswith("cfb"):
                for col in ("away_team_school", "home_team_school"):
                    if r.get(col):
                        full2mk[norm(r[col])].add(norm(r.get("matchup", "")))

    nfl_full_to_abbr = nfl_full_name_crosswalk(load("nfl-schedule.csv"))

    def rating(team, league):
        tbl = nfl_r if league == "nfl" else cfb_r
        n = norm(team)
        if league == "nfl":
            abbr = nfl_full_to_abbr.get(n)
            return tbl.get(abbr) if abbr else None
        # Exact schedule identity only. Ratings contain FBS school names while
        # odds contain display names with mascots. Prefix/substring matching
        # silently mapped FCS schools to similarly named FBS programs (North
        # Carolina A&T -> North Carolina; Eastern Illinois -> Illinois).
        return resolve_team_record(team, tbl, cfb_full_to_school)

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(hours=args.hours)
    cands, nfl_ratings_gated = [], 0

    for eid in set(ml) | set(spr) | set(tot):
        m = meta[eid]
        try:
            start = datetime.datetime.fromisoformat(m["commence_time_et"])
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc)
        if not (now < start <= horizon):
            continue
        league = "nfl" if "nfl" in m["sport"] else "cfb"
        if args.league != "all" and args.league != league:
            continue
        ra, rh = rating(m["away_team"], league), rating(m["home_team"], league)
        if not (ra and rh):
            continue
        if league == "nfl" and ra.get("is_current_season") == "False":
            nfl_ratings_gated += 1
            continue

        away_key = norm(m["away_team"])
        home_key = norm(m["home_team"])
        if league == "cfb":
            away_key = canonical_cfb_school(m["away_team"], cfb_full_to_school)
            home_key = canonical_cfb_school(m["home_team"], cfb_full_to_school)
        wk = next((k for k in (full2mk[away_key] & full2mk[home_key])
                   if k in weather), None)
        wx = weather.get(wk)
        data_q = 10.0 if (wx and ra.get("is_current_season") == "True") else 6.0

        # ── SIDE RATING GAP + MONEYLINE ──────────────────────────────────
        try:
            if league == "cfb":
                gap = float(ra["sp_plus_rating"]) - float(rh["sp_plus_rating"])
            else:
                gap = float(ra["srs_rating"]) - float(rh["srs_rating"])
            gkey = frozenset((away_key, home_key))
            is_neutral = gkey in neutral
            if not is_neutral:
                gap -= 2.5  # neutral sites earn no home bump
        except (ValueError, TypeError, KeyError):
            gap = None
            is_neutral = False
            gkey = frozenset((norm(m["away_team"]), norm(m["home_team"])))
        if gap is not None and eid in ml:
            side = m["away_team"] if gap > 0 else m["home_team"]
            sel = next((s for s in ml[eid]
                        if norm(s)[:8] in norm(side) or norm(side)[:8] in norm(s)), None)
            if sel:
                books = ml[eid][sel]
                dk = books.get(BASE_BOOK)
                if dk is not None and MIN_ODDS <= dk <= MAX_ODDS:
                    bb, bo = max(books.items(), key=lambda kv: -prob(kv[1]))
                    worst = min(books.values(), key=lambda o: -prob(o))
                    disagree = (prob(worst) - prob(bo)) * 100
                    score = (min(disagree / 4.0, 1.0) * 40
                             + taper(abs(gap), 4.0, 14.0) * 35
                             + max(0.0, 1.0 - (prob(dk) - prob(bo)) * 100 / 3.0) * 15
                             + data_q)
                    cands.append({
                        "kind": "ML", "eid": eid, "m": m, "league": league, "start": start,
                        "label": sel, "selection": sel,
                        "dk": dk, "fd": books.get(COMPARE_BOOK),
                        "best_book": bb, "best_odds": bo, "n": len(books),
                        "score": score, "disagree": disagree,
                        "why": f"ratings +{abs(gap):.1f} pts, books apart {disagree:.1f}%",
                        "line": None, "wx": wx, "ra": ra, "rh": rh, "gap": gap,
                        "side_is_home": side == m["home_team"], "exp_total": None,
                        "neutral": is_neutral, "intl": gkey in intl,
                        "open": opens.get((eid, "h2h", sel)), "market_key": "h2h",
                        "qb_away": qbs.get(norm(m["away_team"]), []),
                        "qb_home": qbs.get(norm(m["home_team"]), []),
                        "hurt_away": hurt.get(norm(m["away_team"]), []),
                        "hurt_home": hurt.get(norm(m["home_team"]), []),
                    })

        # ── SPREAD ───────────────────────────────────────────────────────
        # The rating margin is descriptive, not a validated ATS model. It
        # can rank a spread option only when the offered DraftKings number
        # leaves the configured minimum cover room. No market quota follows:
        # spreads compete with ML and totals for the same global top 10.
        if gap is not None and eid in spr:
            for sel, books in spr[eid].items():
                if BASE_BOOK not in books:
                    continue
                dk_pts, dk_odds = books[BASE_BOOK]
                if not (MIN_ODDS <= dk_odds <= MAX_ODDS):
                    continue
                edge = spread_cover_edge(
                    gap, sel, m["away_team"], m["home_team"], dk_pts
                )
                if edge is None or edge < MIN_SPREAD_EDGE_POINTS:
                    continue
                # Compare juice only at the identical point spread. A better
                # point number is disclosed separately; it is not collapsed
                # into a misleading single "best" offer.
                same = {b: o for b, (p, o) in books.items() if p == dk_pts}
                if len(same) < 2:
                    continue
                bb, bo = max(same.items(), key=lambda kv: -prob(kv[1]))
                worst = min(same.values(), key=lambda o: -prob(o))
                disagree = (prob(worst) - prob(bo)) * 100
                score = (min(disagree / 4.0, 1.0) * 40
                         + taper(abs(edge), 4.0, 14.0) * 35
                         + max(0.0, 1.0 - (prob(dk_odds) - prob(bo)) * 100 / 3.0) * 15
                         + data_q)
                best_line = max(p for p, _ in books.values())
                best_line_offers = {
                    b: o for b, (p, o) in books.items() if p == best_line
                }
                best_line_book, best_line_odds = max(
                    best_line_offers.items(), key=lambda kv: -prob(kv[1])
                )
                side_is_home = norm(sel) == norm(m["home_team"])
                fair_margin = -gap if side_is_home else gap
                cands.append({
                    "kind": "SPREAD", "eid": eid, "m": m, "league": league,
                    "start": start, "label": f"{sel} {dk_pts:+g}",
                    "selection": sel, "dk": dk_odds,
                    "fd": same.get(COMPARE_BOOK), "best_book": bb,
                    "best_odds": bo, "n": len(same), "score": score,
                    "disagree": disagree,
                    "why": (f"rating margin {fair_margin:+.1f} vs spread "
                            f"{dk_pts:+g} ({edge:+.1f} raw rating gap)"),
                    "line": dk_pts, "wx": wx, "ra": ra, "rh": rh,
                    "gap": edge, "game_gap": gap,
                    "fair_margin": fair_margin,
                    "side_is_home": side_is_home, "exp_total": None,
                    "best_line": best_line,
                    "best_line_book": best_line_book,
                    "best_line_odds": best_line_odds,
                    "neutral": is_neutral, "intl": gkey in intl,
                    "open": opens.get((eid, "spreads", sel)),
                    "market_key": "spreads",
                    "qb_away": qbs.get(norm(m["away_team"]), []),
                    "qb_home": qbs.get(norm(m["home_team"]), []),
                    "hurt_away": hurt.get(norm(m["away_team"]), []),
                    "hurt_home": hurt.get(norm(m["home_team"]), []),
                })

        # ── TOTAL (CFB only — NFL has no off/def split available) ────────
        if league == "cfb" and eid in tot:
            try:
                exp = ((float(ra["sp_offense_rating"]) + float(rh["sp_defense_rating"])) / 2
                       + (float(rh["sp_offense_rating"]) + float(ra["sp_defense_rating"])) / 2)
            except (ValueError, TypeError, KeyError):
                exp = None
            if exp is not None:
                for sel in ("Over", "Under"):
                    if sel not in tot[eid]:
                        continue
                    books = tot[eid][sel]
                    if BASE_BOOK not in books:
                        continue
                    dk_pts, dk_odds = books[BASE_BOOK]
                    if not (MIN_ODDS <= dk_odds <= MAX_ODDS):
                        continue
                    diff = exp - dk_pts
                    if (sel == "Over" and diff <= 0) or (sel == "Under" and diff >= 0):
                        continue  # model disagrees with this side
                    same = {b: o for b, (p, o) in books.items() if p == dk_pts}
                    if len(same) < 2:
                        continue
                    bb, bo = max(same.items(), key=lambda kv: -prob(kv[1]))
                    worst = min(same.values(), key=lambda o: -prob(o))
                    disagree = (prob(worst) - prob(bo)) * 100
                    # Blowout-script guard (confidence v2): when one side is
                    # a monster ML favorite, the game script kills overs —
                    # Week 1 heavy-fav games went 4/4 UNDER.
                    blowout_pen = 0.0
                    if sel == "Over" and eid in ml:
                        fav = min((min(bk.values()) for bk in ml[eid].values() if bk), default=0)
                        if fav <= -260:
                            blowout_pen = 25.0
                    wind = 0
                    if wx:
                        try:
                            wind = int((wx.get("wind") or "0").split()[0])
                        except (ValueError, IndexError):
                            wind = 0
                    wx_bonus = 3.0 if (sel == "Under" and wind >= 15) else 0.0
                    score = (min(disagree / 4.0, 1.0) * 40
                             + taper(abs(diff), 5.0, 18.0) * 35
                             + max(0.0, 1.0 - (prob(dk_odds) - prob(bo)) * 100 / 3.0) * 15
                             + data_q + wx_bonus - blowout_pen)
                    fd = books.get(COMPARE_BOOK)
                    cands.append({
                        "kind": "TOTAL", "eid": eid, "m": m, "league": league, "start": start,
                        "label": f"{sel} {dk_pts:g}", "selection": sel,
                        "dk": dk_odds,
                        "fd": fd[1] if fd else None, "best_book": bb, "best_odds": bo,
                        "n": len(same), "score": score, "disagree": disagree,
                        "why": f"model {exp:.1f} vs line {dk_pts:g} ({diff:+.1f})"
                               + (f", wind {wind}mph" if wx_bonus else ""),
                        "line": dk_pts, "wx": wx, "ra": ra, "rh": rh, "gap": diff,
                        "side_is_home": None, "exp_total": exp,
                        "neutral": frozenset((norm(m["away_team"]), norm(m["home_team"]))) in neutral,
                        "intl": frozenset((norm(m["away_team"]), norm(m["home_team"]))) in intl,
                        "open": opens.get((eid, "totals", sel)), "market_key": "totals",
                        "qb_away": qbs.get(norm(m["away_team"]), []),
                        "qb_home": qbs.get(norm(m["home_team"]), []),
                        "hurt_away": hurt.get(norm(m["away_team"]), []),
                        "hurt_home": hurt.get(norm(m["home_team"]), []),
                    })

    # Confidence v2: line movement joins the score. Week 1: both settled
    # "market leans against" picks lost; agreement was displayed, not scored.
    for c in cands:
        op = c.get("open")
        if not op:
            continue
        try:
            if c["market_key"] == "totals":
                o_pts = float(op["line_points"])
                if abs(o_pts - c["line"]) >= 1.0:
                    with_us = (c["line"] < o_pts) == c["label"].lower().startswith("u")
                    c["score"] += 6.0 if with_us else -12.0
            elif c["market_key"] == "spreads":
                o_pts = float(op["line_points"])
                line_shift = c["line"] - o_pts
                if abs(line_shift) >= 0.5:
                    alignment = spread_movement_alignment(o_pts, c["line"])
                    c["score"] += 6.0 if alignment == "with us" else -12.0
                else:
                    shift = (prob(c["dk"]) - prob(float(op["odds_american"]))) * 100
                    if shift >= 1.5:
                        c["score"] += 6.0
                    elif shift <= -1.5:
                        c["score"] -= 12.0
            else:
                shift = (prob(c["dk"]) - prob(float(op["odds_american"]))) * 100
                if shift >= 1.5:
                    c["score"] += 6.0
                elif shift <= -1.5:
                    c["score"] -= 12.0
        except (ValueError, TypeError, KeyError):
            continue

    # RANKING v3: the composite becomes legacy_score; `score` is now the price
    # edge in cents. A candidate no other book prices cannot be ranked.
    sig = _signals_mod()
    ranked = []
    for c in cands:
        c["legacy_score"] = c["score"]
        hit = price_edge(c, ml, spr, tot, sig)
        if hit is None:
            continue
        edge_c, n_books = hit
        c["score"] = edge_c
        c["why"] = f"DK {edge_c:+.2f}¢ vs consensus of {n_books} books · " + c["why"]
        ranked.append(c)
    cands = ranked
    cands, suppressed = focus_filter(cands)
    cands.sort(key=confidence_sort_key)
    # The locked product is the globally highest-confidence 10 across
    # spread, moneyline, over, and under. Staking and manual context never
    # reorder it.
    picks = select_picks(cands, args.count)
    stamp = now_eastern()

    # ── iMessage: short lines, no tables, no box drawing ─────────────────
    if args.imessage:
        # FORMAT LOCKED — spec lives in
        # 03-projects/football-picks-agent/weekly-output-format.md (2026-08-29).
        # Same format every week, NFL and NCAA. Change only when David asks.
        # 2026-09-01: `latest` only holds rows inside the 6h freshness gate, so
        # it is empty whenever odds are older than that — and the old fallback
        # printed a fabricated "99h STALE (API quota?)" for BOTH "odds are 11h
        # old" and "fresh odds, no games in the window". Measure the real age
        # from every row read, and say which case it is.
        try:
            _age_h = (stamp - datetime.datetime.fromisoformat(newest_capture)).total_seconds() / 3600
        except (ValueError, TypeError):
            _age_h = None
        print(f"🏈 Daddy Dan's Picks · Week of {stamp.strftime('%b %-d')} — the plays")
        if _age_h is None:
            print("⚠️ NO ODDS ON FILE — run run-daily-data.py before trusting any price")
        elif _age_h > MAX_ODDS_AGE_H:
            print(f"⚠️ ODDS {_age_h:.0f}h STALE — verify prices in the app "
                  f"(last pull {newest_capture[:16].replace('T', ' ')} ET)")
        elif not latest:
            print(f"ℹ️ odds fresh ({_age_h:.0f}h) — no eligible games inside the {args.hours}h window")
        print(f"0 units allocated · edge vs consensus in ¢ · side sizing: ≥{EDGE_FLOOR_CENTS:g}¢ = 1u, "
              f"≥{EDGE_TWO_UNIT_CENTS:g}¢ = 2u · CFB totals = 0u pending fair-total review")
        print("take what you like")
        print()

        def team_short(name, record=None):
            if record and record.get("team"):
                return record["team"]
            return name.rsplit(" ", 1)[0] if len(name.split()) > 2 else name

        for i, c in enumerate(picks, 1):
            m = c["m"]
            away, home = m["away_team"], m["home_team"]
            away_s, home_s = away.split()[-1][:12], home.split()[-1][:12]
            n_stars = star_count(c["score"])
            if c["kind"] == "TOTAL":
                word = "UNDER" if c["label"].lower().startswith("u") else "OVER"
                play = f"{word} {c['line']:g} — {away_s} @ {home_s}"
            elif c["kind"] == "SPREAD":
                play = f"{c['selection'].upper()} {c['line']:+g} — {away_s} @ {home_s}"
            else:
                play = f"{c['label'].upper()} MONEYLINE — {away_s} @ {home_s}"
            units = automatic_board_units(c)
            unit_word = "unit" if units == 1 else "units"
            if c.get("league") == "cfb" and c.get("market_key") == "totals":
                size = "WATCH pending manual fair-total review · 0 units"
            else:
                size = f"{units} {unit_word} if played" if units else "board only, no units"
            print(f"{i}) {play} {'⭐'*n_stars} · {c['score']:+.2f}¢ · {size}")
            print(kickoff_label(c["start"]))

            # ── the write-up: 2-3 sentences of actual case ────────────────
            s = []
            ra, rh = c.get("ra"), c.get("rh")
            if c["kind"] == "TOTAL":
                under = c["label"].lower().startswith("u")
                if ra and rh:
                    if under:
                        r1, r2 = ra.get("_def_rank"), rh.get("_def_rank")
                        if r1 and r2:
                            best = min(int(r1), int(r2))
                            who = team_short(away, ra) if int(r1) < int(r2) else team_short(home, rh)
                            if nationally_supportive(str(best)):
                                s.append(f"{who}'s defense ranks #{best} nationally, "
                                         f"and the model makes this a {c['exp_total']:.0f}-point "
                                         f"game against a {c['line']:g} line.")
                    else:
                        r1, r2 = ra.get("_off_rank"), rh.get("_off_rank")
                        if r1 and r2:
                            best = min(int(r1), int(r2))
                            who = team_short(away, ra) if int(r1) < int(r2) else team_short(home, rh)
                            if nationally_supportive(str(best)):
                                s.append(f"{who}'s offense ranks #{best} nationally, "
                                         f"and the model expects {c['exp_total']:.0f} points "
                                         f"against a {c['line']:g} line.")
                if not s:
                    s.append(f"The model makes this {c['exp_total']:.0f} points against "
                             f"a line of {c['line']:g} — {abs(c['gap']):.0f} points of gap.")
                wx = c.get("wx")
                if wx and under:
                    try:
                        wind = int((wx.get("wind") or "0").split()[0])
                    except (ValueError, IndexError):
                        wind = 0
                    rain = int(wx.get("precip_probability") or 0)
                    if rain >= 40:
                        s.append(f"Forecast has {rain}% rain; verify conditions "
                                 f"before treating that as an Under factor.")
                    elif wind >= 12:
                        s.append(f"Wind at {wind}mph cuts into the passing game.")
            elif c["kind"] == "SPREAD":
                try:
                    key = "sp_plus_ranking" if c["league"] == "cfb" else "srs_ranking"
                    ours, theirs = ((int(rh[key]), int(ra[key])) if c.get("side_is_home")
                                    else (int(ra[key]), int(rh[key])))
                    side_name = team_short(home, rh) if c.get("side_is_home") else team_short(away, ra)
                    s.append(f"{side_name} ranks #{ours} against #{theirs}; the ratings "
                             f"make its side margin {c['fair_margin']:+.1f} while DK gives "
                             f"{c['line']:+g}, leaving {c['gap']:.1f} points of cover room.")
                except (KeyError, TypeError, ValueError):
                    s.append(f"The ratings make this side {c['fair_margin']:+.1f}; DK offers "
                             f"{c['line']:+g}, leaving {c['gap']:.1f} points of cover room.")
                s.append(f"The spread is {spread_key_note(c['line'], c['league'])}.")
                if c["disagree"] >= 2.0:
                    s.append(f"Books differ {c['disagree']:.1f}% on the juice at this number.")
            else:
                try:
                    key = "sp_plus_ranking" if c["league"] == "cfb" else "srs_ranking"
                    ours, theirs = ((int(rh[key]), int(ra[key]))
                                    if c.get("side_is_home")
                                    else (int(ra[key]), int(rh[key])))
                    side_name = team_short(home, rh) if c.get("side_is_home") else team_short(away, ra)
                    tail = "."
                    if c.get("neutral"):
                        tail = " — neutral site, no home edge for either team."
                    elif c.get("side_is_home"):
                        tail = " and gets this one at home."
                    # 2026-09-01 (caught on the Tuesday launch board): when the
                    # pick's rank NUMBER is higher (worse) — e.g. Cal #59 over
                    # UCLA #46 — the old sentence claimed "better team on the
                    # numbers," which is false; the model's edge came from
                    # home-field/price, not ratings. Sentences expose numbers;
                    # the write-up must not overstate the model's own case.
                    if ours <= theirs:
                        s.append(f"{side_name} is the better team on the numbers "
                                 f"(#{ours} vs #{theirs}){tail}")
                    else:
                        why = ("home field closes the ratings gap" if c.get("side_is_home")
                               else "the price, not the ratings, is the edge")
                        neutral_note = " Neutral site." if c.get("neutral") else ""
                        s.append(f"{side_name} ranks below the opponent "
                                 f"(#{ours} vs #{theirs}) — {why}. The rating "
                                 f"margin alone does not establish value.{neutral_note}")
                except (KeyError, TypeError, ValueError):
                    s.append(f"Ratings give this side a {abs(c['gap']):.0f}-point edge.")
                if c["disagree"] >= 2.0:
                    s.append(f"Six-book prices span {c['disagree']:.1f}%; "
                             f"shop the exact selection.")

            # movement, in words
            op = c.get("open")
            if op:
                try:
                    if c["market_key"] == "totals":
                        o_pts = float(op["line_points"])
                        if abs(o_pts - c["line"]) >= 1.0:
                            with_us = (c["line"] < o_pts) == c["label"].lower().startswith("u")
                            direction = "rose" if c["line"] > o_pts else "fell"
                            alignment = "toward" if with_us else "against"
                            s.append(f"The total {direction} from {o_pts:g} to "
                                     f"{c['line']:g}, moving {alignment} this side.")
                    elif c["market_key"] == "spreads":
                        o_pts = float(op["line_points"])
                        if abs(o_pts - c["line"]) >= 0.5:
                            if spread_movement_alignment(o_pts, c["line"]) == "with us":
                                s.append(f"The spread moved from {o_pts:+g} to "
                                         f"{c['line']:+g} toward this side.")
                            else:
                                s.append(f"Heads up: the spread moved from {o_pts:+g} to "
                                         f"{c['line']:+g} against this side.")
                    else:
                        shift = (prob(c["dk"]) - prob(float(op["odds_american"]))) * 100
                        if shift <= -1.5:
                            s.append("Heads up: the price has drifted against this "
                                     "side since it opened.")
                except (ValueError, TypeError, KeyError):
                    pass
            if c.get("qb_away") or c.get("qb_home"):
                s.append("⚠️ A QB is on the injury report — check news before betting.")
            _bp = (norm(m["away_team"]), norm(m["home_team"]))
            sf = spot.get(frozenset(_bp))
            if sf is None:
                # flags key on school names ("colorado"); board carries full
                # names ("Colorado Buffaloes") — prefix-match both teams.
                for fk, fv in spot.items():
                    ft = tuple(fk)
                    if len(ft) == 2 and all(any(b.startswith(f) or f.startswith(b)
                                                for b in _bp) for f in ft):
                        sf = fv
                        break
            if sf:
                s.append(f"🚩 Spot: {sf} — situational, unweighted, tracked.")
                c["spot_flags"] = sf
            if c.get("intl"):
                s.append("⚠️ International venue — no US forecast exists, travel is "
                         "real, and every number deserves extra doubt.")
            elif c.get("neutral"):
                s.append("Neutral-site game.")
            # Game notes: conditions on every outdoor game, not just when they
            # drive the pick (David, game day 2026-08-29).
            wx = c.get("wx")
            if usable_forecast(wx):
                try:
                    wind = int((wx.get("wind") or "0").split()[0])
                except (ValueError, IndexError):
                    wind = 0
                rain = int(wx.get("precip_probability") or 0)
                note = f"Game notes: {wx['temperature_f']}° at kickoff"
                note += f", wind {wind}mph" if wind else ", calm"
                if rain >= 20:
                    note += f", {rain}% rain"
                if wx.get("venue"):
                    note += f" — {wx['venue']}"
                s.append(note + ".")
            print(" ".join(s))
            print()
        print("⭐ = stronger play.")
        print("Every pick tracked vs results,")
        print("tailed or not, to sharpen this.")
        return 0

    # ── full report ──────────────────────────────────────────────────────
    print(f"\nWEEKLY BOARD — {stamp.strftime('%a %b %-d, %Y  %-I:%M %p ET')}")
    if len(picks) == args.count:
        print(f"{len(cands)} qualifying play(s) · showing top {args.count} · ${args.stake:.0f}/unit")
    else:
        print(f"{len(cands)} qualifying play(s) · showing all {len(picks)}; "
              f"{args.count - len(picks)} short of the top-{args.count} target · "
              f"${args.stake:.0f}/unit")
    if nfl_ratings_gated:
        print(
            f"excluded {nfl_ratings_gated} NFL game(s): "
            "current-season ratings gate not met"
        )
    if focus_notice(suppressed):
        print(focus_notice(suppressed))
    print()
    print(f"  {'#':>2} {'edge¢':>5} {'strength':10} {'u':>2} {'play':26} {'DK':>6} {'FD':>6}  why")
    print(f"  {'-'*2} {'-'*4} {'-'*10} {'-'*2} {'-'*26} {'-'*6} {'-'*6}  {'-'*40}")
    for i, c in enumerate(picks, 1):
        fd = f"{c['fd']:+.0f}" if c["fd"] is not None else "  n/a"
        better = " ←FD" if (c["fd"] is not None and prob(c["fd"]) < prob(c["dk"]) - 0.005) else ""
        print(f"  {i:>2} {c['score']:>+5.2f} {stars(c['score']):10} "
              f"{automatic_board_units(c):>2} "
              f"{c['label'][:26]:26} {c['dk']:>+6.0f} {fd:>6}{better}  {c['why']}")
    print()
    for i, c in enumerate(picks, 1):
        m, ra, rh = c["m"], c.get("ra"), c.get("rh")
        print(f"  {i}. {c['label']}  [{c['kind']}]  {m['away_team']} @ {m['home_team']}"
              f" · {kickoff_label(c['start'])}  ({c['score']:+.1f}¢ {stars(c['score'])} · legacy {int(round(c['legacy_score']))})")
        stake_units = automatic_board_units(c)
        stake_dollars = args.stake * stake_units
        zero_note = ""
        if not stake_units:
            zero_note = (" — manual fair-total review required, automated stake blocked"
                         if c.get("league") == "cfb" and c.get("market_key") == "totals"
                         else " — under the edge floor, board only")
        print(f"     size if played: {stake_units}u (${stake_dollars:.0f}){zero_note} · price: DK {c['dk']:+.0f} · best of {c['n']} books "
              f"{c['best_book']} {c['best_odds']:+.0f} · ${stake_dollars:.0f} wins "
              f"${profit(c['dk'], stake_dollars):.2f}")

        # THE CASE — plain language, from the same numbers that drove the score.
        if c["kind"] == "ML" and ra and rh:
            try:
                if c["league"] == "cfb":
                    av, hv = float(ra["sp_plus_rating"]), float(rh["sp_plus_rating"])
                    lbl, arank, hrank = "SP+", ra.get("sp_plus_ranking"), rh.get("sp_plus_ranking")
                else:
                    av, hv = float(ra["srs_rating"]), float(rh["srs_rating"])
                    lbl, arank, hrank = "SRS", ra.get("srs_ranking"), rh.get("srs_ranking")
                venue_note = "at neutral site" if c.get("neutral") else "including home field"
                print(f"     case: {lbl} {m['away_team'][:18]} {av:+.1f} (#{arank}) vs "
                      f"{m['home_team'][:18]} {hv:+.1f} (#{hrank}); "
                      f"{abs(c['gap']):.1f}-pt rating margin to this side {venue_note} "
                      "(not a validated betting edge)")
            except (ValueError, TypeError, KeyError):
                pass
            print(f"     market: {c['n']} books spread {c['disagree']:.1f}% on this price — "
                  + ("price variation; not proof of positive expected value"
                     if c["disagree"] >= 2.0 else "tight price range; not proof of value"))
            print("     breaks if: a QB or key starter is out late, or the rating is stale "
                  "(ratings are prior-form, not news)")
        elif c["kind"] == "SPREAD":
            print(f"     case: rating margin for {c['selection'][:18]} "
                  f"{c['fair_margin']:+.1f} vs DraftKings {c['line']:+g} "
                  f"({c['gap']:+.1f} raw rating gap, not a validated edge); "
                  f"{spread_key_note(c['line'], c['league'])}")
            print(f"     market: {c['n']} books at the identical spread differ "
                  f"{c['disagree']:.1f}% on price — "
                  + ("price variation; not proof of positive expected value"
                     if c["disagree"] >= 2.0 else "tight price range; not proof of value"))
            if c.get("best_line") != c["line"]:
                print(f"     best number: {c['best_line_book']} {c['best_line']:+g} "
                      f"at {c['best_line_odds']:+.0f}; price comparison above remains "
                      f"at DK's {c['line']:+g}")
            print("     breaks if: late QB/personnel news changes the fair margin, or "
                  "the rating gap is stale (the spread market beat this model in backtests)")
        elif c["kind"] == "TOTAL":
            print(f"     case: expected points {c['exp_total']:.1f} vs posted {c['line']:g} "
                  f"({c['gap']:+.1f}) — SP+ offense blended against opposing defense")
            if usable_forecast(c.get("wx")):
                wx = c["wx"]
                print(f"     conditions: {wx['temperature_f']}°F, wind {wx['wind']} "
                      f"{wx['wind_direction']}, {wx['precip_probability']}% precip — "
                      f"{wx['short_forecast']}")
            print("     breaks if: pace runs against the model (tempo and turnovers move "
                  "totals more than talent), or weather turns")
        # MOVEMENT — where the money went since this number was posted.
        op = c.get("open")
        if op:
            try:
                o_odds = float(op["odds_american"])
                if c["market_key"] == "totals":
                    o_pts = float(op["line_points"])
                    if abs(o_pts - c["line"]) >= 0.5:
                        direction = "down" if c["line"] < o_pts else "up"
                        side_note = ("with us" if (direction == "down") == c["label"].lower().startswith("u")
                                     else "against us")
                        print(f"     movement: total opened {o_pts:g}, now {c['line']:g} "
                              f"({direction} {abs(o_pts-c['line']):.1f}) — moving {side_note}")
                    else:
                        print(f"     movement: total steady at {c['line']:g} since posting "
                              f"(price {o_odds:+.0f} → {c['dk']:+.0f})")
                elif c["market_key"] == "spreads":
                    o_pts = float(op["line_points"])
                    if abs(o_pts - c["line"]) >= 0.5:
                        side_note = spread_movement_alignment(o_pts, c["line"])
                        print(f"     movement: spread opened {o_pts:+g}, now "
                              f"{c['line']:+g} ({abs(c['line']-o_pts):.1f} pts) — "
                              f"moving {side_note}")
                    else:
                        shift = (prob(c["dk"]) - prob(o_odds)) * 100
                        if abs(shift) >= 1.0:
                            way = "toward this side" if shift > 0 else "away from this side"
                            print(f"     movement: spread steady at {c['line']:+g}; "
                                  f"price {o_odds:+.0f} → {c['dk']:+.0f} "
                                  f"({abs(shift):.1f}% {way})")
                        else:
                            print(f"     movement: spread steady at {c['line']:+g} "
                                  f"(price {o_odds:+.0f} → {c['dk']:+.0f})")
                else:
                    shift = (prob(c["dk"]) - prob(o_odds)) * 100
                    if abs(shift) >= 1.0:
                        way = "toward this side" if shift > 0 else "away from this side"
                        print(f"     movement: {o_odds:+.0f} → {c['dk']:+.0f} "
                              f"({abs(shift):.1f}% {way}) since posting")
                    else:
                        print(f"     movement: price steady ({o_odds:+.0f} → {c['dk']:+.0f})")
            except (ValueError, TypeError, KeyError):
                pass

        # PERSONNEL — the input most likely to invalidate everything above.
        qa, qh = c.get("qb_away") or [], c.get("qb_home") or []
        ha, hh = c.get("hurt_away") or [], c.get("hurt_home") or []
        if qa or qh or ha or hh:
            for who, qb, oth in ((m["away_team"], qa, ha), (m["home_team"], qh, hh)):
                bits = []
                if qb:
                    bits.append("QB " + "; ".join(x.split(" QB ")[0] + " " +
                                                  x.split(" QB ")[-1] for x in qb[:2]))
                outs = [n for is_out, n in oth if is_out]
                quest = [n for is_out, n in oth if not is_out]
                if outs:
                    bits.append(f"OUT: {', '.join(o.rsplit(' (', 1)[0] for o in outs[:3])}")
                if quest:
                    bits.append(f"{len(quest)} questionable")
                if bits:
                    mark = "⚠" if (qb or outs) else " "
                    print(f"     {mark} personnel {who[:18]}: {' · '.join(bits)}")
        if c["league"] == "cfb":
            print("     note: college injury reporting is sparse by rule — absence of a")
            print("           listing is NOT evidence of health. Check the beat writers.")
        elif not (qa or qh or ha or hh):
            print("     personnel: nothing listed as out (confirm morning of)")

        # CONDITIONS on side picks too — wind is a scoring input, not just a total.
        if c["kind"] in {"ML", "SPREAD"} and usable_forecast(c.get("wx")):
            wx = c["wx"]
            try:
                wind = int((wx.get("wind") or "0").split()[0])
            except (ValueError, IndexError):
                wind = 0
            note = " — wind at this level suppresses passing and scoring" if wind >= 15 else ""
            print(f"     conditions: {wx['temperature_f']}°F, wind {wx['wind']} "
                  f"{wx['wind_direction']}, {wx['precip_probability']}% precip{note}")

        if c["fd"] is not None and prob(c["fd"]) < prob(c["dk"]) - 0.005:
            edge = (prob(c["dk"]) - prob(c["fd"])) * 100
            print(f"     ⚑ price: FanDuel {c['fd']:+.0f} beats DK by {edge:.1f}% — "
                  f"same bet and line, better price. Over a season this gap IS the edge.")
        print()
    exposure_units = sum(automatic_board_units(c) for c in picks)
    print(f"\n  This is a menu, not a slate. Taking all {len(picks)} would be "
          f"${args.stake*exposure_units:.2f} ({exposure_units} units); the weekly cap is 10 units and")
    print("  the daily cap is 4.")
    if args.log:
        print("  Every pick is logged and graded whether you tail it or not,")
        print("  so skipping one still teaches the system something.")
    else:
        print("  This no-log execution snapshot is not added to the recommendation ledger.")
    print("\n  Confidence is ranking strength (book disagreement + model gap + price +")
    print("  data quality), NOT a win probability. Nothing here is validated; the")
    print("  test is average CLV across 30+ logged selections.")

    if args.log:
        with LEDGER.open(encoding="utf-8") as existing_file:
            reader = csv.DictReader(existing_file)
            cols = list(reader.fieldnames or [])
            existing = {
                recommendation_identity(row)
                for row in reader
                if str(row.get("decision") or "").upper() in TRACKED_DECISIONS
            }
        logged = 0
        with LEDGER.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            for c in picks:
                row = {
                    "decision_date": stamp.date().isoformat(),
                    "game_date": c["start"].date().isoformat(),
                    "league": c["league"].upper(), "event_id": c["eid"],
                    "matchup": f"{c['m']['away_team']} @ {c['m']['home_team']}",
                    "market": c["market_key"],
                    "selection": c["selection"],
                    "line_points": c["line"] if c["line"] is not None else "",
                    "book": BASE_BOOK, "odds_american": f"{c['dk']:.0f}",
                    "captured_at_et": stamp.isoformat(), "decision": "LEAN",
                    "confidence": f"{int(round(c['legacy_score']))}",
                    "edge_cents": f"{c['score']:.2f}",
                    "reason": c["why"] + (f" | SPOT: {c['spot_flags']}" if c.get("spot_flags") else ""),
                    "source_report": "weekly-picks.py",
                }
                identity = recommendation_identity(row)
                if identity in existing:
                    continue
                w.writerow(row)
                existing.add(identity)
                logged += 1
        print(f"\n  logged {logged} new selection(s) to {LEDGER.name}; "
              f"skipped {len(picks) - logged} existing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
