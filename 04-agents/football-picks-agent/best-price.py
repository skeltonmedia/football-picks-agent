#!/usr/bin/env python3
"""best-price.py — the edge that does not require predicting anything.

    python3 -B 04-agents/football-picks-agent/best-price.py [--hours 72] [--min-edge 1.5]

WHY (2026-08-28). David: "I thought the point of this agent was to show me the
way to bet and what to bet on... where is the edge?"

Fair challenge, and the honest answer has two halves.

HALF ONE — the edge that is gone. Predicting scores better than the market.
Both backtests measured this and it is not there (model-notes.md): a solo
bettor with public ratings does not out-forecast a market that has already
priced every public input. Chasing it is how bankrolls die.

HALF TWO — the edge that is real, and mechanical. THE SAME BET IS PRICED
DIFFERENTLY AT DIFFERENT BOOKS. Take Memphis +160 at one book when another
hangs +150 and you have gained ~2.6% of expected value on that wager, with no
opinion about Memphis required. You are not beating the market's judgment; you
are refusing its worst price. That edge is small, boring, repeatable, and it
is what actually separates disciplined bettors from the field.

This script compares every book's price on the same selection and reports where
the spread between best and worst is wide enough to matter. It does not tell
you WHO to bet. It tells you, once you have decided, WHERE to bet it — and
which games the market itself disagrees about, which is a decent shortlist of
where a manual read has room to be right.

Reads line-watch/snapshots.csv (latest snapshot per book/market/selection).
Never places a bet. Never claims a winner.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402

SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"


def implied(odds: float) -> float:
    return 100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)


def profit(odds: float, stake: float = 2.0) -> float:
    return stake * odds / 100 if odds > 0 else stake * 100 / -odds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72, help="lookahead window")
    ap.add_argument("--min-edge", type=float, default=1.5,
                    help="minimum best-vs-worst gap in implied %% to report")
    ap.add_argument("--stake", type=float, default=2.0)
    args = ap.parse_args()

    if not SNAPSHOTS.exists():
        print(f"MISSING: {SNAPSHOTS.relative_to(ROOT)}")
        return 1

    latest: dict = {}
    meta: dict = {}
    for r in csv.DictReader(SNAPSHOTS.open(encoding="utf-8")):
        key = (r["event_id"], r["market"], r["selection"], r["book"])
        prev = latest.get(key)
        if prev is None or r["captured_at_et"] > prev["captured_at_et"]:
            latest[key] = r
        meta[r["event_id"]] = r

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(hours=args.hours)

    # group: (event, market, selection) -> [(book, odds, points)]
    groups = defaultdict(list)
    for (eid, market, sel, book), r in latest.items():
        try:
            start = datetime.datetime.fromisoformat(r["commence_time_et"])
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc)
        if not (now < start <= horizon):
            continue
        try:
            odds = float(r["odds_american"])
        except (ValueError, TypeError):
            continue
        groups[(eid, market, sel)].append((book, odds, r.get("line_points", ""), start))

    rows = []
    for (eid, market, sel), offers in groups.items():
        if len(offers) < 2:
            continue
        # Compare like with like: on spreads/totals a different number is a
        # different bet, so only compare books hanging the same line_points.
        by_pts = defaultdict(list)
        for book, odds, pts, start in offers:
            by_pts[pts].append((book, odds, start))
        for pts, same in by_pts.items():
            if len(same) < 2:
                continue
            best = max(same, key=lambda o: -implied(o[1]))   # lowest implied = best price
            worst = min(same, key=lambda o: -implied(o[1]))
            gap = (implied(worst[1]) - implied(best[1])) * 100
            if gap < args.min_edge:
                continue
            m = meta[eid]
            rows.append({
                "start": best[2], "away": m["away_team"], "home": m["home_team"],
                "sport": m["sport"], "market": market, "sel": sel, "pts": pts,
                "best_book": best[0], "best_odds": best[1],
                "worst_book": worst[0], "worst_odds": worst[1],
                "gap": gap, "books": len(same),
                "extra": profit(best[1], args.stake) - profit(worst[1], args.stake),
            })

    rows.sort(key=lambda r: -r["gap"])
    print(f"Best-price scan — {now_eastern().strftime('%b %-d, %Y at %-I:%M %p ET')}")
    print(f"  next {args.hours}h · {len(groups)} priced selections · "
          f"{len(rows)} with a gap ≥ {args.min_edge}%\n")
    if not rows:
        print("  No meaningful price gaps. That is a real result: the books agree,")
        print("  so there is no free value to collect on this slate.")
        return 0

    print(f"  {'gap':>6}  {'bet':44} {'take at':12} {'not at':12} {'+$':>6}")
    print(f"  {'-'*6}  {'-'*44} {'-'*12} {'-'*12} {'-'*6}")
    for r in rows[:25]:
        league = "NFL" if "nfl" in r["sport"] else "CFB"
        pts = f" {r['pts']}" if r["pts"] else ""
        label = f"{league} {r['away'][:11]}@{r['home'][:11]} {r['market'][:6]} {r['sel'][:12]}{pts}"
        print(f"  {r['gap']:5.2f}%  {label:44} "
              f"{r['best_book'][:9]:>9}{r['best_odds']:>+6.0f} "
              f"{r['worst_book'][:9]:>9}{r['worst_odds']:>+6.0f} "
              f"{r['extra']:>+6.2f}")

    total = sum(r["extra"] for r in rows[:4])
    print(f"\n  On a ${args.stake:.0f} stake, the top 4 gaps are worth ${total:.2f} more")
    print("  than taking the worst price on the same bets. Small. Free. Repeatable.")
    print("\n  This says WHERE to bet, never WHO to bet. The selection is still yours,")
    print("  and it still needs the card work (model-notes.md: ratings alone do not")
    print("  beat the close).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
