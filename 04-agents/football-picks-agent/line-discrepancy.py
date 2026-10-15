#!/usr/bin/env python3
"""line-discrepancy.py — cross-book line gaps big enough to play both sides.

    python3 -B 04-agents/football-picks-agent/line-discrepancy.py [--hours 168] [--league cfb]

WHY (2026-09-10). David asked to see where books disagree on the NUMBER, not
just the price, so both sides of a game can be explored: a spread MIDDLE
(Team A -3 at book1, Team B +4.5 at book2 — both bets win if A wins by 4), a
totals middle (Over 46.5 at book1, Under 49.5 at book2 — both win on 47-49),
or a true moneyline ARBITRAGE (best price each side sums to under 100%
implied, a guaranteed profit regardless of who wins).

`best-price.py` already covers price-shopping the SAME number across books.
This is the other half: DIFFERENT numbers across books, which is what a
middle or an arb needs. Read-only. Never places a bet, never says who wins.

Reads line-watch/snapshots.csv (latest offer per book/market/selection).
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
from football_team_resolution import (  # noqa: E402
    build_school_crosswalk,
    canonical_cfb_school,
    fold_team_name,
)

SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"

# Same lists weekly-picks.py's spread_key_note() uses, so "key number" means
# one thing across the agent. James Holzhauer, on his own method (CBS Sports,
# 2019-06-03): "A big part of my game is trying to hit a middle on the key
# number, like I see one sports book is dealing +10.5, another is -9.5.
# There are definitely times I'll bet both." A middle is only as good as the
# number it straddles; 3 and 7 carry most of the mass of NFL/CFB margins.
KEY_NUMBERS = {"nfl": (3, 7), "cfb": (3, 7, 10, 14)}


def implied(odds: float) -> float:
    return 100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)


def profit(odds: float, stake: float) -> float:
    return stake * odds / 100 if odds > 0 else stake * 100 / -odds


def load_latest(hours: int, league: str):
    latest, meta = {}, {}
    for r in csv.DictReader(SNAPSHOTS.open(encoding="utf-8")):
        key = (r["event_id"], r["market"], r["selection"], r["book"])
        prev = latest.get(key)
        if prev is None or r["captured_at_et"] > prev["captured_at_et"]:
            latest[key] = r
        meta[r["event_id"]] = r

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(hours=hours)
    kept = {}
    for key, r in latest.items():
        m = meta[r["event_id"]]
        lg = "nfl" if "nfl" in m["sport"] else "cfb"
        if league != "all" and league != lg:
            continue
        try:
            start = datetime.datetime.fromisoformat(r["commence_time_et"])
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc)
        if not (now < start <= horizon):
            continue
        kept[key] = r
    return kept, meta, now


def find_spread_middles(rows: dict, meta: dict, min_gap: float):
    """Best number for each side of each game; a middle exists when the two
    numbers, read on the same scale, leave daylight between them."""
    by_event = defaultdict(lambda: defaultdict(list))  # eid -> selection -> [(book,pts,odds)]
    for (eid, market, sel, book), r in rows.items():
        if market != "spreads":
            continue
        try:
            pts = float(r["line_points"]); odds = float(r["odds_american"])
        except (TypeError, ValueError):
            continue
        by_event[eid][sel].append((book, pts, odds))

    out = []
    for eid, sides in by_event.items():
        if len(sides) != 2:
            continue
        (sel_a, offers_a), (sel_b, offers_b) = sides.items()
        # "Best" side-A number = the largest points (least to cover / most to get).
        best_a = max(offers_a, key=lambda o: o[1])
        best_b = max(offers_b, key=lambda o: o[1])
        gap = best_a[1] + best_b[1]  # opposite-signed; >0 means the numbers don't meet
        if gap < min_gap:
            continue
        m = meta[eid]
        league = "nfl" if "nfl" in m["sport"] else "cfb"
        # The margin-of-victory window a middle wins on is (|favorite's
        # number|, dog's number) — NOT the raw signed pair. -9.5/+10.5 opens
        # a window of (9.5, 10.5); sorting the signed values instead would
        # (wrongly) treat everything between -9.5 and +10.5 as "in the gap"
        # and flag every key number on any wide spread as a false hit.
        neg = [v for v in (best_a[1], best_b[1]) if v < 0]
        pos = [v for v in (best_a[1], best_b[1]) if v > 0]
        if neg and pos:
            lo, hi = abs(neg[0]), pos[0]
            keys_hit = [k for k in KEY_NUMBERS[league] if lo < k < hi]
        else:
            keys_hit = []  # both sides same sign (rare edge case); no clean margin window
        out.append({
            "eid": eid, "start": r_start(m), "away": m["away_team"], "home": m["home_team"],
            "sport": m["sport"], "kind": "SPREAD MIDDLE", "gap": gap, "keys_hit": keys_hit,
            "leg_a": (sel_a, best_a), "leg_b": (sel_b, best_b),
        })
    return out


def find_total_middles(rows: dict, meta: dict, min_gap: float):
    by_event = defaultdict(lambda: defaultdict(list))
    for (eid, market, sel, book), r in rows.items():
        if market != "totals":
            continue
        try:
            pts = float(r["line_points"]); odds = float(r["odds_american"])
        except (TypeError, ValueError):
            continue
        by_event[eid][sel].append((book, pts, odds))

    out = []
    for eid, sides in by_event.items():
        overs, unders = sides.get("Over"), sides.get("Under")
        if not overs or not unders:
            continue
        best_over = min(overs, key=lambda o: o[1])   # lowest Over number
        best_under = max(unders, key=lambda o: o[1])  # highest Under number
        gap = best_under[1] - best_over[1]
        if gap < min_gap:
            continue
        m = meta[eid]
        out.append({
            "eid": eid, "start": r_start(m), "away": m["away_team"], "home": m["home_team"],
            "sport": m["sport"], "kind": "TOTAL MIDDLE", "gap": gap, "keys_hit": [],
            "leg_a": ("Over", best_over), "leg_b": ("Under", best_under),
        })
    return out


def find_ml_arbs(rows: dict, meta: dict, stake: float):
    by_event = defaultdict(lambda: defaultdict(list))
    for (eid, market, sel, book), r in rows.items():
        if market != "h2h":
            continue
        try:
            odds = float(r["odds_american"])
        except (TypeError, ValueError):
            continue
        by_event[eid][sel].append((book, odds))

    out = []
    for eid, sides in by_event.items():
        if len(sides) != 2:
            continue
        (sel_a, offers_a), (sel_b, offers_b) = sides.items()
        best_a = max(offers_a, key=lambda o: -implied(o[1]))
        best_b = max(offers_b, key=lambda o: -implied(o[1]))
        total_implied = (implied(best_a[1]) + implied(best_b[1])) * 100
        if total_implied >= 100:
            continue
        m = meta[eid]
        # Stake split so both legs return the same amount (classic arb sizing).
        inv_a, inv_b = 1 / (1 + best_a[1] / 100) if best_a[1] > 0 else 1 / (1 - 100 / best_a[1]), \
                       1 / (1 + best_b[1] / 100) if best_b[1] > 0 else 1 / (1 - 100 / best_b[1])
        pool = stake * 2
        stake_a = pool * inv_a / (inv_a + inv_b)
        stake_b = pool - stake_a
        ret = stake_a * (1 + best_a[1] / 100) if best_a[1] > 0 else stake_a * (1 + 100 / -best_a[1])
        out.append({
            "eid": eid, "start": r_start(m), "away": m["away_team"], "home": m["home_team"],
            "sport": m["sport"], "kind": "ML ARBITRAGE", "gap": 100 - total_implied,
            "leg_a": (sel_a, (best_a[0], None, best_a[1])),
            "leg_b": (sel_b, (best_b[0], None, best_b[1])),
            "stake_a": stake_a, "stake_b": stake_b, "guaranteed": ret - pool,
        })
    return out


def r_start(m: dict) -> datetime.datetime:
    try:
        s = datetime.datetime.fromisoformat(m["commence_time_et"])
    except ValueError:
        return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    return s if s.tzinfo else s.replace(tzinfo=datetime.timezone.utc)


def render(r: dict, stake: float) -> str:
    league = "NFL" if "nfl" in r["sport"] else "CFB"
    game = f"{r['away'][:20]} @ {r['home'][:20]}"
    kickoff = r["start"].strftime("%a %-I:%M%p ET") if r["start"] != datetime.datetime.max.replace(tzinfo=datetime.timezone.utc) else "?"
    sel_a, (book_a, pts_a, odds_a) = r["leg_a"]
    sel_b, (book_b, pts_b, odds_b) = r["leg_b"]
    leg_a_s = f"{sel_a} {pts_a:+g}" if pts_a is not None else sel_a
    leg_b_s = f"{sel_b} {pts_b:+g}" if pts_b is not None else sel_b
    tag = f"  \U0001F3AF key number {','.join(str(k) for k in r.get('keys_hit', []))}" if r.get("keys_hit") else ""
    lines = [f"[{league}] {r['kind']}  {game}  ({kickoff})  gap {r['gap']:.1f}{tag}"]
    if r["kind"] == "ML ARBITRAGE":
        lines.append(f"    leg A: {leg_a_s:22} {book_a:10} {odds_a:+.0f}  stake ${r['stake_a']:.2f}")
        lines.append(f"    leg B: {leg_b_s:22} {book_b:10} {odds_b:+.0f}  stake ${r['stake_b']:.2f}")
        lines.append(f"    guaranteed profit: ${r['guaranteed']:+.2f} on ${stake*2:.0f} risked, regardless of winner")
    else:
        lines.append(f"    leg A: {leg_a_s:22} {book_a:10} {odds_a:+.0f}")
        lines.append(f"    leg B: {leg_b_s:22} {book_b:10} {odds_b:+.0f}")
        hit = ("margin of victory lands in the gap" if r["kind"] == "SPREAD MIDDLE"
               else "combined score lands in the gap")
        lines.append(f"    both win if the {hit}; otherwise one wins, one loses (not risk-free)")
    return "\n".join(lines)


def load_ratings():
    """fold_team_name(school) -> SP+ rank, from the CFB ratings file."""
    ranks = {}
    path = AGENT / "data" / "cfb-team-ratings.csv"
    if not path.exists():
        return ranks
    for r in csv.DictReader(path.open(encoding="utf-8")):
        try:
            rank = int(r["sp_plus_ranking"])
        except (KeyError, ValueError):
            continue
        ranks[fold_team_name(r["team"])] = rank
    return ranks


def load_schedule_crosswalk():
    path = AGENT / "data" / "cfb-schedule.csv"
    if not path.exists():
        return {}
    return build_school_crosswalk(list(csv.DictReader(path.open(encoding="utf-8"))))


def is_marquee(r: dict, ranks_norm: dict, crosswalk: dict, cutoff: int = 30) -> bool:
    """Exact schedule crosswalk only (school name, e.g. 'Miami' vs
    'Miami (OH)' must not collide) — see football_team_resolution's docstring
    on why substring matching is banned here."""
    def team_rank(name: str):
        school = canonical_cfb_school(name, crosswalk)
        return ranks_norm.get(school)
    ra, rh = team_rank(r["away"]), team_rank(r["home"])
    return (ra is not None and ra <= cutoff) or (rh is not None and rh <= cutoff)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--league", choices=["cfb", "nfl", "all"], default="cfb")
    ap.add_argument("--min-spread-gap", type=float, default=1.0)
    ap.add_argument("--min-total-gap", type=float, default=2.0)
    ap.add_argument("--stake", type=float, default=2.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    if not SNAPSHOTS.exists():
        print(f"MISSING: {SNAPSHOTS.relative_to(ROOT)}")
        return 1

    rows, meta, now = load_latest(args.hours, args.league)
    global r_start  # used inside find_* via closure-free helper above
    middles = (find_spread_middles(rows, meta, args.min_spread_gap)
               + find_total_middles(rows, meta, args.min_total_gap))
    arbs = find_ml_arbs(rows, meta, args.stake)
    ranks = load_ratings()
    crosswalk = load_schedule_crosswalk()
    marquee = [r for r in middles if is_marquee(r, ranks, crosswalk)]

    print(f"Line discrepancy scan — {now_eastern().strftime('%b %-d, %Y at %-I:%M %p ET')}")
    print(f"  next {args.hours}h · {args.league.upper()} · "
          f"{len(arbs)} arb(s) · {len(middles)} middle(s) ≥ configured gap\n")

    if arbs:
        print("== MONEYLINE ARBITRAGE (guaranteed profit, split stake both ways) ==")
        for r in sorted(arbs, key=lambda x: -x["guaranteed"])[:args.top]:
            print(render(r, args.stake)); print()
    else:
        print("== MONEYLINE ARBITRAGE ==\n  None found. Two-way CFB/NFL moneylines rarely gap that wide; expected.\n")

    def holzhauer_sort(x):
        return (0 if x.get("keys_hit") else 1, -x["gap"])

    if marquee:
        print(f"== TOP-30 MATCHUPS WITH A MIDDLE ({len(marquee)}) ==")
        for r in sorted(marquee, key=holzhauer_sort)[:args.top]:
            print(render(r, args.stake)); print()
    else:
        print("== TOP-30 MATCHUPS ==\n  No ranked team currently has a middle at this gap threshold.\n")

    if middles:
        on_key = [r for r in middles if r.get("keys_hit")]
        print(f"== MIDDLES ON A KEY NUMBER ({len(on_key)}) — Holzhauer's own method ==")
        if on_key:
            for r in sorted(on_key, key=lambda x: -x["gap"])[:args.top]:
                print(render(r, args.stake)); print()
        else:
            print("  None this window. A middle off the key number still pays on a wider gap;")
            print("  see the full list below.\n")
        print(f"== ALL {min(args.top, len(middles))} MIDDLES, ANY MATCHUP (biggest gap first) ==")
        for r in sorted(middles, key=holzhauer_sort)[:args.top]:
            print(render(r, args.stake)); print()
    else:
        print("== MIDDLES ==\n  None found at the current gap thresholds.\n")

    print("Both legs need their own price checked before betting; snapshots lag")
    print("live book prices. This never says who wins the game.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
