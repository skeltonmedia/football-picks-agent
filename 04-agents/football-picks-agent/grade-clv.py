#!/usr/bin/env python3
"""Report or write provenance-checked, post-kickoff closing-line comparisons.

No future game is final. Snapshot close evidence requires both a pre-kickoff
retrieval and a real provider-update timestamp within 75 minutes of kickoff.
Unknown provider freshness and retrospective CFBD rows without quote timestamps
remain explicitly unverified. Point CLV is not converted into probability CLV.

--write writes verified fields; --write --repair-unverified explicitly clears
legacy fields that cannot be reproduced by these rules. Back up the ledger and
inspect the report before using the repair flag. Never records or settles a bet.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402

LEDGER = AGENT / "recommendation-ledger.csv"
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
MIN_SAMPLE = 30  # below this, report but do not interpret
TRACKED_DECISIONS = {"PICK", "LEAN", "WATCH"}  # 2026-10-13: the review gate leaves every shown row WATCH; excluding it graded nothing for two weeks
ET = ZoneInfo("America/New_York")
CLOSE_WINDOW_MIN = 75
FREE_HISTORY = AGENT / "line-watch" / "free-lines.csv"
FREE_LATEST = AGENT / "line-watch" / "free-lines-latest.csv"


def implied(odds: float) -> float:
    """American odds -> implied probability (with vig, which is fine for CLV:
    both sides carry it, so the comparison stays apples-to-apples)."""
    return 100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)


# 2026-09-07 (AQ-167): spread/total CLV was graded on juice only, so a total
# taken at 50.5 that closed 52.0 read as *negative* CLV because the price went
# -115 -> -110. Points are the primary quantity for those markets. The
# Legacy points-to-percent conversion below is retained ONLY for reproducible
# historical backtests that import it. Current CLV grading never uses it. A half
# point on a non-key number runs ~7-10 cents at retail, i.e. ~1.5-2.2% of
# implied probability), not a fitted model; the raw points column is printed
# beside it so nobody has to trust the constant.
POINT_VALUE_PCT = {
    ("spreads", "nfl"): 4.0,
    ("spreads", "cfb"): 3.0,
    ("totals", "nfl"): 3.5,
    ("totals", "cfb"): 2.5,
    # NHL (2026-10-07): a puck-line or goal-total point is not a linear cents
    # move; hockey CLV is price-only at the same number.
    ("spreads", "nhl"): 0.0,
    ("totals", "nhl"): 0.0,
}


def point_value_pct(market: str, league: str) -> float:
    lg = (league or "").lower()
    lg = "nfl" if "nfl" in lg else "nhl" if "nhl" in lg else "cfb"
    return POINT_VALUE_PCT.get((market, lg), 3.0)


def points_in_favor(market: str, selection: str, took_pts: float, close_pts: float) -> float:
    """Points of line value gained (+) or lost (-) from the bettor's seat.

    spreads: `line_points` is the selected team's number, so a higher number
             is always better for the bettor (+7 -> +7.5 good, -9.5 -> -9 good):
             took - close.
    totals:  Over wants a lower number (46.5 taken, 52 close = +5.5 good):
             close - took. Under wants a higher number: took - close.
    """
    if market == "totals":
        if (selection or "").strip().lower().startswith("over"):
            return close_pts - took_pts
        return took_pts - close_pts
    return took_pts - close_pts


def clv_eligible(row: dict) -> bool:
    """Only genuine recommendations belong in aggregate CLV.

    Invalidated rows stay in the ledger as an audit trail, but including their
    impossible stale prices would turn a data defect into fake positive CLV.
    """
    return (row.get("decision") or "").upper() in TRACKED_DECISIONS


CFBD_CLOSES = AGENT / "data" / "cfb-closing-lines.csv"


def _norm(s: str) -> str:
    import unicodedata as _ud
    s = _ud.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in s.lower() if c.isalnum())


def cfbd_closes() -> dict:
    """Keep season and Eastern game date in identity; never collapse rematches."""
    out = {}
    if not CFBD_CLOSES.exists():
        return out
    for r in csv.DictReader(CFBD_CLOSES.open(encoding="utf-8")):
        out[(r.get("season"), r.get("game_date_et"), _norm(r["away_team"]), _norm(r["home_team"]))] = r
    return out


def cfbd_lookup(closes: dict, away: str, home: str, game_date: str | None = None,
                season: str | None = None) -> dict | None:
    """Ledger uses full names ("Florida Atlantic Owls"); CFBD uses schools
    ("Florida Atlantic"). Prefix-match both sides; ambiguity returns None."""
    try:
        date = datetime.strptime(game_date or "", "%Y-%m-%d").date()
    except ValueError:
        return None
    season = str(season or (date.year if date.month >= 8 else date.year - 1))
    a, h = _norm(away), _norm(home)
    hits = []
    for v in closes.values():
        sa, sh = _norm(v.get("away_team")), _norm(v.get("home_team"))
        kick = timestamp(v.get("commence_time_et"))
        if (v.get("game_date_et") == game_date and str(v.get("season")) == season
                and kick and kick.astimezone(ET).date() == date and sa and sh
                and a.startswith(sa) and h.startswith(sh)):
            hits.append(v)
    return hits[0] if len(hits) == 1 else None


def cfbd_close_for(row: dict, closes: dict) -> tuple[float | None, float | None] | None:
    """Return (close_odds, close_points) for a ledger row from the CFBD DK close,
    or None when the game or field is missing. 2026-09-07: this is the true
    close; the snapshot close is a 7:30 AM capture on most game days."""
    if (row.get("league") or "").upper() != "CFB":
        return None
    try:
        away, home = [x.strip() for x in (row.get("matchup") or "").split("@")]
    except ValueError:
        return None
    c = cfbd_lookup(closes, away, home, row.get("game_date"), row.get("season"))
    if not c:
        return None
    market = (row.get("market") or "").lower()
    sel = _norm(row.get("selection") or "")

    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if market in ("h2h", "moneyline", "ml"):
        if sel not in (_norm(home), _norm(away)):
            return None
        is_home = sel == _norm(home)
        odds = f(c.get("home_ml") if is_home else c.get("away_ml"))
        return (odds, None) if odds is not None else None
    if market == "totals":
        pts = f(c.get("total"))
        return (None, pts) if pts is not None else None
    if market == "spreads":
        sh = f(c.get("spread_home"))
        if sh is None:
            return None
        if sel not in (_norm(home), _norm(away)):
            return None
        is_home = sel == _norm(home)
        return (None, sh if is_home else -sh)
    return None


PROCESS_FIELDS = ["beat_close", "clv_points", "move_since_pick", "consensus_side", "process_grade"]
CLV_FIELDS = ["closing_odds_american", "closing_line_points", "closing_line_value_pct", *PROCESS_FIELDS,
              "clv_status", "clv_source", "clv_book", "clv_captured_at_et", "clv_provider_updated_at",
              "clv_kickoff_et", "clv_close_age_minutes", "clv_method", "clv_graded_at_et"]
BEST_PRICE_TOL_CENTS = 5.0
FLAT_POINTS, FLAT_CENTS = 0.25, 2.0


def timestamp(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t if t.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def number(value) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def observation_key(r: dict) -> tuple:
    return tuple(r.get(k, "") for k in ("captured_at_et", "sport", "event_id", "book", "market",
                                        "selection", "line_points", "odds_american"))


def fabricated_free_observations() -> set:
    """Legacy free adapters used capture time as an invented provider timestamp.

    Exclude those exact observed rows, not unrelated paid quotes from the same
    book/event. New adapters identify their source and leave unknown updates blank.
    """
    out = set()
    for path in (FREE_HISTORY, FREE_LATEST):
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if (not r.get("event_link") and r.get("bookmaker_last_update") == r.get("captured_at_et")):
                        out.add(observation_key(r))
    return out


def verified_close(r: dict, now: datetime, fabricated: set | None = None) -> bool:
    cap, kick, update = (timestamp(r.get(k)) for k in
                         ("captured_at_et", "commence_time_et", "bookmaker_last_update"))
    if not cap or not kick or not update or observation_key(r) in (fabricated or set()):
        return False
    return (kick <= now and kick - timedelta(minutes=CLOSE_WINDOW_MIN) <= update <= cap <= kick
            and bool(r.get("event_id")) and bool(r.get("book")))


def cents(odds: float) -> float:
    """American odds on one axis so -105 -> +105 is 10 cents, not 210."""
    return odds - 100.0 if odds > 0 else odds + 100.0


def book_series(keys: set, now: datetime | None = None) -> dict:
    """(event, market, selection) -> book -> [(captured_at, row)] pre-kickoff, sorted."""
    out: dict = defaultdict(lambda: defaultdict(list))
    now = now or now_eastern()
    fabricated = fabricated_free_observations()
    if not SNAPSHOTS.exists():
        return out
    with SNAPSHOTS.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r.get("event_id"), r.get("market"), r.get("selection"))
            if key not in keys:
                continue
            cap, kick, update = (timestamp(r.get(k)) for k in
                                 ("captured_at_et", "commence_time_et", "bookmaker_last_update"))
            if (cap and kick and update and kick <= now and update <= cap <= kick
                    and cap - update <= timedelta(minutes=CLOSE_WINDOW_MIN)
                    and observation_key(r) not in fabricated):
                out[key][r["book"]].append((cap, r))
    for key in out:
        for book in out[key]:
            out[key][book].sort(key=lambda t: t[0])
    return out


def in_force(series: list, at: datetime) -> dict | None:
    """Latest row captured at or before `at` (the log is change-only)."""
    hit = None
    for cap, row in series:
        if cap <= at and at - cap <= timedelta(minutes=CLOSE_WINDOW_MIN):
            hit = row
        else:
            break
    return hit


def process_columns(r: dict, clv_pct: float | None, pts: float | None, books: dict,
                    now: datetime | None = None) -> dict:
    """The five AQ-221 columns for one graded row; blanks where data is missing."""
    market = (r.get("market") or "").lower()
    sel = r.get("selection") or ""
    signal = pts if pts not in (None, 0.0) else clv_pct
    out = {"beat_close": ("1" if signal > 0 else "0") if signal is not None else "",
           "clv_points": f"{pts:+.1f}" if pts is not None else "",
           "move_since_pick": "", "consensus_side": "", "process_grade": ""}
    try:
        took_odds = float(r["odds_american"])
        took_pts = float(r["line_points"]) if r.get("line_points") not in (None, "") else None
        at = datetime.fromisoformat(r["captured_at_et"])
    except (ValueError, KeyError, TypeError):
        return out
    now = now or now_eastern()
    valid_books = {b: [(cap, row) for cap, row in ser if verified_close(row, now)
                       and row.get("commence_time_et") == r.get("clv_kickoff_et")]
                   for b, ser in books.items()}
    dk = valid_books.get("draftkings") or []
    dk_close = dk[-1][1] if dk else None
    if dk_close:
        try:
            if market in ("spreads", "totals") and took_pts is not None:
                mv = points_in_favor(market, sel, took_pts, float(dk_close["line_points"]))
                out["move_since_pick"] = f"{mv:+.1f}pt"
            else:
                out["move_since_pick"] = f"{cents(took_odds) - cents(float(dk_close['odds_american'])):+.0f}c"
        except (ValueError, KeyError, TypeError):
            pass
    others = [(b, ser) for b, ser in valid_books.items() if b != "draftkings" and ser]
    if others:
        try:
            if market in ("spreads", "totals") and took_pts is not None:
                close_mean = sum(float(ser[-1][1]["line_points"]) for _, ser in others) / len(others)
                mv = points_in_favor(market, sel, took_pts, close_mean)
                out["consensus_side"] = "with" if mv > FLAT_POINTS else "against" if mv < -FLAT_POINTS else "flat"
            else:
                close_mean = sum(cents(float(ser[-1][1]["odds_american"])) for _, ser in others) / len(others)
                mv = cents(took_odds) - close_mean
                out["consensus_side"] = "with" if mv > FLAT_CENTS else "against" if mv < -FLAT_CENTS else "flat"
        except (ValueError, KeyError, TypeError):
            pass
    shopped = None
    try:
        at_pick = [float(row["odds_american"]) for b, ser in books.items() if b != "draftkings"
                   for row in [in_force(ser, at)] if row
                   and (market in ("h2h", "moneyline", "ml") or (took_pts is not None
                        and row.get("line_points") not in ("", None)
                        and abs(float(row["line_points"]) - took_pts) < 0.01))]
        if at_pick:
            shopped = cents(took_odds) >= max(cents(o) for o in at_pick) - BEST_PRICE_TOL_CENTS
    except (ValueError, KeyError, TypeError):
        pass
    if shopped is not None and signal is not None:
        n = int(signal > 0) + int(shopped)
        out["process_grade"] = "A" if n == 2 else "B" if n == 1 else "C"
    return out


def closing_rows(now: datetime | None = None) -> dict:
    """(event_id, market, selection) -> last snapshot before kickoff."""
    best: dict = {}
    now = now or now_eastern()
    fabricated = fabricated_free_observations()
    if not SNAPSHOTS.exists():
        return best
    # A timely verified Pinnacle reference outranks other timely references.
    # An old/unknown Pinnacle quote never overrides a genuine near-close capture.
    pinnacle: dict = {}
    with SNAPSHOTS.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not verified_close(r, now, fabricated):
                continue
            cap = timestamp(r["captured_at_et"])
            key = (r.get("event_id"), r.get("market"), r.get("selection"))
            prev = best.get(key)
            if prev is None or cap > prev[0]:
                best[key] = (cap, r)
            if r.get("book") == "pinnacle":
                pprev = pinnacle.get(key)
                if pprev is None or cap > pprev[0]:
                    pinnacle[key] = (cap, r)
    best.update(pinnacle)  # pinnacle close wins wherever it exists
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write CLV columns back into the ledger (default: report only)")
    args = ap.parse_args()

    if not LEDGER.exists():
        print(f"MISSING: {LEDGER.relative_to(ROOT)}")
        return 1
    rows = list(csv.DictReader(LEDGER.open(encoding="utf-8")))
    if not rows:
        print("Ledger is empty — no decisions recorded yet.\n")
        print("A dry run starts by writing decisions into")
        print(f"  {LEDGER.relative_to(ROOT)}")
        print("one row per PICK/LEAN, with the price you would have taken.")
        print("No money required. That is the point.")
        return 0

    eligible = [r for r in rows if clv_eligible(r)]
    excluded = len(rows) - len(eligible)
    closes = closing_rows()
    cfbd = cfbd_closes()
    series = book_series({(r.get("event_id"), r.get("market"), r.get("selection")) for r in eligible})
    graded, unmatched, from_cfbd = [], 0, 0
    for r in eligible:
        key = (r.get("event_id") or r.get("matchup"), r.get("market"), r.get("selection"))
        hit = closes.get(key)
        true_close = cfbd_close_for(r, cfbd)
        if not hit and not true_close:
            unmatched += 1
            continue
        c = dict(hit[1]) if hit else {"odds_american": "-110", "line_points": ""}
        if true_close:
            odds, pts = true_close
            if odds is not None:
                c["odds_american"] = f"{odds:.0f}"
            if pts is not None:
                c["line_points"] = f"{pts:g}"
                if not hit:
                    c["odds_american"] = "-110"  # CFBD carries points only for spread/total
            from_cfbd += 1
        market = (r.get("market") or "").lower()
        try:
            took_odds = float(r["odds_american"])
            close_odds = float(c["odds_american"])
        except (ValueError, KeyError, TypeError):
            unmatched += 1
            continue

        if market in ("h2h", "moneyline", "ml"):
            # Value = probability you bought below what the market closed at.
            clv_pct = (implied(close_odds) - implied(took_odds)) / implied(took_odds) * 100.0
            pts = None
        else:
            try:
                took_pts = float(r.get("line_points") or 0)
                close_pts = float(c.get("line_points") or 0)
            except ValueError:
                unmatched += 1
                continue
            pts = points_in_favor(market, r.get("selection") or "", took_pts, close_pts)
            juice = (implied(close_odds) - implied(took_odds)) / implied(took_odds) * 100.0
            clv_pct = juice + pts * point_value_pct(market, r.get("league") or "")

        r["closing_odds_american"] = f"{close_odds:.0f}"
        r["closing_line_points"] = c.get("line_points", "")
        r["closing_line_value_pct"] = f"{clv_pct:.2f}"
        r.update(process_columns(r, clv_pct, pts,
                                 series.get((r.get("event_id"), r.get("market"), r.get("selection")), {})))
        graded.append((r, clv_pct, pts))

    print(f"CLV report — {now_eastern().strftime('%b %-d, %Y at %-I:%M %p ET')}")
    print(f"  {len(eligible)} eligible decision(s) · {len(graded)} matched to a closing line "
          f"({from_cfbd} at the CFBD DraftKings close) · {unmatched} unmatched · "
          f"{excluded} invalid/pass row(s) excluded\n")
    if not graded:
        print("  Nothing to grade yet. Snapshots must cover the same event/market/selection")
        print("  as the decision, captured before kickoff.")
        return 0

    print(f"  {'CLV%':>8}  {'pts':>5}  matchup / market / selection")
    print(f"  {'-'*8}  {'-'*5}  {'-'*52}")
    for r, clv, pts in sorted(graded, key=lambda g: -g[1]):
        p = f"{pts:+.1f}" if pts is not None else "  — "
        print(f"  {clv:+7.2f}%  {p:>5}  {r.get('matchup','?')[:28]:28} "
              f"{r.get('market','?')[:8]:8} {r.get('selection','?')[:14]}")

    avg = sum(c for _, c, _ in graded) / len(graded)
    beat = sum(1 for _, c, _ in graded if c > 0)
    print(f"\n  average CLV: {avg:+.2f}%  ·  beat the close: {beat}/{len(graded)}")
    grades = defaultdict(int)
    for r, _, _ in graded:
        grades[r.get("process_grade") or "?"] += 1
    sides = defaultdict(int)
    for r, _, _ in graded:
        sides[r.get("consensus_side") or "?"] += 1
    print(f"  process grade: " + " · ".join(f"{g} {n}" for g, n in sorted(grades.items()))
          + "   (A = beat close + best price; B = one; C = neither)")
    print(f"  consensus close moved: " + " · ".join(f"{k} {n}" for k, n in sorted(sides.items())))
    if len(graded) < MIN_SAMPLE:
        print(f"\n  ⚠️  {len(graded)} decisions is below the {MIN_SAMPLE}-decision floor. Report the")
        print("      number, do not interpret it. A good week of CLV is not evidence of edge,")
        print("      the same way a good week of wins is not (see model-notes.md).")
    elif avg > 0:
        print("\n  Positive average CLV over a real sample is the first honest sign the")
        print("  process finds prices the market later agrees with. Keep going; it is a")
        print("  leading indicator, not a profit guarantee.")
    else:
        print("\n  Negative average CLV means the market moved against these prices. That is")
        print("  the process failing its cheapest test — before any money was at risk.")

    if args.write:
        fields = list(rows[0].keys()) + [c for c in PROCESS_FIELDS if c not in rows[0]]
        for r in rows:
            for c in PROCESS_FIELDS:
                r.setdefault(c, "")
        with LEDGER.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        print(f"\n  wrote CLV columns into {LEDGER.relative_to(ROOT)}")
    else:
        print("\n  (report only — re-run with --write to fill the ledger's CLV columns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
