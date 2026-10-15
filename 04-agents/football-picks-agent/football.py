#!/usr/bin/env python3
"""football.py — the one football agent (2026-09-28, David: "get aggressive...
combine it all into one and make sure we're learning from our mistakes").

    python3 -B 04-agents/football-picks-agent/football.py status
    python3 -B 04-agents/football-picks-agent/football.py data            # run-daily-data.py
    python3 -B 04-agents/football-picks-agent/football.py board --league nfl --date 2026-10-04
    python3 -B 04-agents/football-picks-agent/football.py board --league ncaaf --hours 168
    python3 -B 04-agents/football-picks-agent/football.py check           # are our numbers still there?
    python3 -B 04-agents/football-picks-agent/football.py settle          # results + CLV into the ledger
    python3 -B 04-agents/football-picks-agent/football.py learn           # what is working, what gets blocked
    python3 -B 04-agents/football-picks-agent/football.py bet 1 --units 1 # log a real wager from the last board
    python3 -B 04-agents/football-picks-agent/football.py week            # data -> settle -> learn -> boards -> check
    python3 -B 04-agents/football-picks-agent/football.py daily           # today's 3 picks: NFL + CFB + NHL, ML/total/prop

DAILY CARD (2026-10-07, David: "serious tuning... add hockey sources... 3 picks
a day: ML, over/under, scorer or any of the best props for each NFL, NCAA and
NHL"). `daily` runs the same gate over every league playing today, adds player
props priced the same way (DK vs the other books on the same player/line, from
fetch-props.py), ranks win-first, one per game, and shows DAILY_PICKS rows.
NHL rides the existing feeds: Pinnacle guest league 1456 and DraftKings via
ESPN in the free pull, icehockey_nhl on the rationed Odds API pull.

WHY ONE AGENT. Through 2026-09-28 the season record was 31-36-6, all CFB, zero
NFL rows, 39% beat-close, and nothing logged for ten days. Three generators
disagreed about what a pick even was: the Tuesday card ranked by price edge,
the game-day cards by market win probability (which is "bet the favorite at
DK's price" — no edge by construction), and both still birthed candidates from
ratings that lost every backtest. The one validated signal — DK's price versus
Pinnacle/consensus in cents (`backtest-signals.py` 2026-09-18: >=1.5c beat the
close 69% of 230 markets, >=2c 73%, Pinnacle Spearman +0.42) — was research
only. This file makes it the whole process:

  candidates  every side of every h2h/spread/total DK offers, both leagues,
              born from the odds log alone (no ratings gate, so NFL is live)
  gate        edge >= EDGE_FLOOR_CENTS vs Pinnacle (consensus if no Pinnacle),
              odds in [-250, +350], >= MIN_CONSENSUS_BOOKS other books, fresh
              (<= 6h), no dog past MAX_DOG_SPREAD_POINTS, no learned block
  rank        among qualified plays, by win probability (David 2026-09-26:
              "about winning the bet not price"), edge as tiebreak, one per game
  size        from edge: >=2c 2u, >=1.5c 1u; totals capped TOTALS_MAX_UNITS;
              fan teams 1u; 2u/bet, 4u/day, 10u/week (staking-config.md)
  log         every qualified row -> recommendation-ledger.csv (PICK or LEAN),
              idempotent, so settle/CLV/learn never have a gap again
  learn       settled rows by league x market x edge bucket; a slice of the
              NEW process that is under water on 30+ rows is BLOCKED from the
              board until it recovers (learned-rules.json); legacy rows are
              reported as history, never re-litigated

Never places a bet. `bet` records what David actually did so the dashboard
stops reading an empty bet-ledger as 0-0-0.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import math
import re
import subprocess
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
VAULT = AGENT.parents[1]
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
QUOTA = AGENT / "line-watch" / "quota-status.json"
LAST_BOARD = AGENT / "line-watch" / "last-board.json"
LEDGER = AGENT / "recommendation-ledger.csv"
BET_LEDGER = AGENT / "bet-ledger.csv"
RULES = AGENT / "learned-rules.json"
LESSONS = AGENT / "lessons.md"
RUN_LOG = AGENT / "run-log.md"
CONFIG = AGENT / "config.env"
STAKING = AGENT / "staking-config.md"
EASTERN = ZoneInfo("America/New_York")
PY = sys.executable

MARKETS = ("h2h", "spreads", "totals")
LEAGUE_LABEL = {"ncaaf": "CFB", "nfl": "NFL", "nhl": "NHL"}
LEAGUE_ARG = {"CFB": "ncaaf", "NFL": "nfl", "NHL": "nhl"}
LEAGUE_EMOJI = {"nfl": "🏈", "ncaaf": "🏈", "nhl": "🏒"}
FOOTBALL = frozenset({"nfl", "ncaaf"})     # the dog cap is a football points question
PROPS = AGENT / "line-watch" / "props.csv"
PROP_LABEL = {"player_anytime_td": "ANYTIME TD", "player_goal_scorer_anytime": "ANYTIME GOAL",
              "player_shots_on_goal": "SOG", "player_points": "PTS", "player_assists": "AST"}
MIN_ODDS, MAX_ODDS = -250, 350
MAX_ODDS_AGE_H = 6.0
FAN_RE = re.compile(r"Ohio State|Buckeyes|Tennessee Titans|\bTitans\b", re.I)
SOURCE = "football.py board"
PROCESS_VERSION = "offered-price-review-v2"
TRACKED = frozenset({"PICK", "LEAN", "WATCH", "PASS"})
LEARN_MIN_N = 30          # the vault's own rule: no verdict under 30 graded rows
BLOCK_BEAT_BELOW = 0.45   # beat-close rate that, with negative ROI, blocks a slice
EDGE_BUCKETS = ((1.5, 2.0, "1.5-2c"), (2.0, 3.0, "2-3c"), (3.0, 99.0, "3c+"))


# ---------------------------------------------------------------------------
# shared pieces (hyphenated filenames, hence importlib)
# ---------------------------------------------------------------------------

def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), AGENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_bs = None


def bs():
    global _bs
    if _bs is None:
        _bs = _load("backtest-signals")
    return _bs


def config() -> dict:
    out = {}
    if CONFIG.exists():
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def cfg_float(c: dict, key: str, default: float) -> float:
    try:
        return float(c.get(key, default))
    except (TypeError, ValueError):
        return default


def staking() -> dict:
    """bankroll/unit from staking-config.md — content lives in markdown."""
    out = {"unit_size": 2.0, "starting_bankroll": 200.0, "max_bet": 2, "max_day": 4, "max_week": 10}
    if STAKING.exists():
        text = STAKING.read_text(encoding="utf-8")
        m = re.search(r"unit_size \| \$([\d.]+)", text)
        if m:
            out["unit_size"] = float(m.group(1))
        m = re.search(r"starting_bankroll \| \$([\d.]+)", text)
        if m:
            out["starting_bankroll"] = float(m.group(1))
    return out


def now_et() -> dt.datetime:
    return dt.datetime.now(EASTERN).replace(microsecond=0)


def implied(o: float) -> float:
    return bs().implied(o)


def profit_per_unit(o: float) -> float:
    return o / 100.0 if o > 0 else 100.0 / -o


def fmt_odds(o: float) -> str:
    return f"{o:+.0f}"


def line_identity(v) -> str:
    try:
        return f"{float(v):g}" if str(v).strip() else ""
    except (TypeError, ValueError):
        return str(v or "").strip()


def identity(row: dict) -> tuple:
    return (str(row.get("decision_date") or ""), str(row.get("event_id") or ""),
            str(row.get("market") or "").lower(), str(row.get("selection") or "").strip().lower(),
            line_identity(row.get("line_points")))


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def rewrite_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def log_run(title: str, lines: list[str]) -> None:
    stamp = now_et().strftime("%Y-%m-%d %H:%M")
    block = f"\n## {stamp} — {title}\n\n" + "\n".join(f"- {l}" for l in lines) + "\n"
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(block)


def learned_rules() -> dict:
    if RULES.exists():
        try:
            return json.loads(RULES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"blocked": [], "working": [], "generated": None}


# ---------------------------------------------------------------------------
# candidates: every side, every market, from the odds log alone
# ---------------------------------------------------------------------------

PULL_TOLERANCE = dt.timedelta(minutes=10)
FREE_HISTORY = AGENT / "line-watch" / "free-lines.csv"
FREE_LATEST = AGENT / "line-watch" / "free-lines-latest.csv"
FREE_CURRENT: set = set()        # (event_id, book, market, selection) in the free feed's latest pull
ODDS_LATEST: dict = {}           # sport -> newest Odds API capture (before the free feed is merged)
PROVENANCE: dict = {}            # exact observation -> original provider update, never retrieval time
PAIR_LAG = dt.timedelta(minutes=10)
CROSS_BOOK_LAG = dt.timedelta(minutes=30)


def parse_stamp(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def valid_odds(value):
    try:
        return math.isfinite(float(value)) and abs(float(value)) >= 100
    except (ValueError, TypeError):
        return False


def capture_provenance(path: Path) -> None:
    for row in read_csv(path):
        cap = parse_stamp(row.get("captured_at_et"))
        if cap is None:
            continue
        # Historical free-feed rows fabricated provider times from retrieval.
        # Only the repaired adapter's explicit status can establish freshness.
        trusted = path not in (FREE_HISTORY, FREE_LATEST) or row.get("provider_timestamp_status") == "verified"
        key = (row.get("event_id"), row.get("book"), row.get("market"), row.get("selection"), cap)
        PROVENANCE[key] = parse_stamp(row.get("bookmaker_last_update")) if trusted else None


def load_all_events() -> dict:
    """Odds API log merged with the free Pinnacle/DK feed (fetch-free-lines.py).
    Same Event objects, same Series; a book's newest row wins in `in_force`."""
    B = bs()
    PROVENANCE.clear()
    if SNAPSHOTS.exists():
        capture_provenance(SNAPSHOTS)
    events = B.load_events(SNAPSHOTS) if SNAPSHOTS.exists() else {}
    ODDS_LATEST.clear()
    ODDS_LATEST.update(latest_pull(events))
    FREE_CURRENT.clear()
    for path in (FREE_HISTORY, FREE_LATEST):
        if not path.exists():
            continue
        capture_provenance(path)
        extra = B.load_events(path)
        for eid, ev2 in extra.items():
            ev = events.get(eid)
            if ev is None:
                events[eid] = ev2
                continue
            for key, ser in ev2.series.items():
                dst = ev.series.get(key)
                if dst is None:
                    ev.series[key] = ser
                else:
                    for cap, row in zip(ser.caps, ser.rows):
                        if cap not in dst.caps:
                            dst.add(cap, row[0], row[1])
                ev.caps.update(ser.caps)
        if path == FREE_LATEST:
            for eid, ev2 in extra.items():
                for (b, mk, s) in ev2.series:
                    FREE_CURRENT.add((eid, b, mk, s))
    return events


def latest_pull(events: dict) -> dict:
    """sport -> time of the newest capture. The compactor keeps the newest pull
    per sport intact, so a book missing from it is not offering that market now."""
    out: dict = {}
    for ev in events.values():
        if ev.caps:
            m = max(ev.caps)
            if m > out.get(ev.sport, dt.datetime.min.replace(tzinfo=EASTERN)):
                out[ev.sport] = m
    return out


def current_rows(ev, market: str, sel: str, t: dt.datetime, latest: dt.datetime) -> dict:
    """book -> (line_points, odds) in force at t that is current in its own
    feed: in the Odds API's latest pull for the sport, or in the free feed's
    latest pull. A row more than MAX_ODDS_AGE_H older than the newest capture
    of any feed (`latest`) is dropped so a fresh Pinnacle does not get paired
    with a two-day-old FanDuel. Carried-forward rows from older pulls do not
    count. With a single stale pull everything is still returned (the board
    shows it at 0 units) because nothing newer exists."""
    import bisect
    odds_latest = ODDS_LATEST.get(ev.sport, latest)
    out = {}
    for (b, mk, s), ser in ev.series.items():
        if mk != market or s != sel:
            continue
        i = bisect.bisect_right(ser.caps, t)
        if not i:
            continue
        cap = ser.caps[i - 1]
        if not 0 <= (t - cap).total_seconds() <= MAX_ODDS_AGE_H * 3600:
            continue
        if cap >= odds_latest - PULL_TOLERANCE or (ev.event_id, b, mk, s) in FREE_CURRENT:
            if valid_odds(ser.rows[i - 1][1]):
                out[b] = ser.rows[i - 1]
    return out


def observation(ev, book, market, selection, t):
    import bisect
    ser = ev.series.get((book, market, selection))
    if ser is None:
        return None, None
    i = bisect.bisect_right(ser.caps, t)
    if not i:
        return None, None
    cap = ser.caps[i - 1]
    return cap, PROVENANCE.get((ev.event_id, book, market, selection, cap))


def _mirror(market: str, pts):
    return None if pts is None else (-pts if market == "spreads" else pts)


def same_line(market: str, rows: dict, rows2: dict, dk_pts) -> dict:
    """book -> (odds_sel, odds_other) where both sides sit at DK's line."""
    out = {}
    for b, (pts, odds) in rows.items():
        r2 = rows2.get(b)
        if r2 is None:
            continue
        if market != "h2h":
            if pts is None or dk_pts is None or abs(pts - dk_pts) > 1e-9:
                continue
            if r2[0] is None or abs(r2[0] - _mirror(market, dk_pts)) > 1e-9:
                continue
        out[b] = (odds, r2[1])
    return out


def novig(pair: tuple) -> float:
    p1, p2 = implied(pair[0]), implied(pair[1])
    return p1 / (p1 + p2)


def price_edge(ev, market: str, sel: str, t: dt.datetime, latest: dt.datetime,
               min_books: int) -> dict | None:
    """Bettor's-seat edge of DK's price for `sel` versus a sharper reference at
    the SAME line: Pinnacle when it prices that line, else the consensus of
    the other books at that line. Off-line references (DK -3.5 vs Pinnacle -3)
    are reported with the line gap and never sized: the flat points-to-cents
    conversion misprices key numbers (3, 7)."""
    B = bs()
    other = ev.other_side(market, sel)
    rows = current_rows(ev, market, sel, t, latest)
    rows2 = current_rows(ev, market, other, t, latest)
    dk, dk2 = rows.get(B.DK), rows2.get(B.DK)
    if dk is None or dk2 is None:
        return None
    dk_pts, dk_odds = dk
    if market != "h2h" and (dk_pts is None or dk2[0] is None or abs(dk2[0] - _mirror(market, dk_pts)) > 1e-9):
        return None
    others_any = {b: r for b, r in rows.items() if b != B.DK}
    pairs = {b: p for b, p in same_line(market, rows, rows2, dk_pts).items() if b != B.DK}
    dk_cap, dk_update = observation(ev, B.DK, market, sel, t)
    dk2_cap, dk2_update = observation(ev, B.DK, market, other, t)
    if dk_cap is None or dk2_cap is None or abs(dk_cap - dk2_cap) > PAIR_LAG:
        return None
    aligned = {}
    updates = [dk_update, dk2_update]
    for book, pair in pairs.items():
        cap, update = observation(ev, book, market, sel, t)
        cap2, update2 = observation(ev, book, market, other, t)
        if cap is None or cap2 is None or abs(cap - cap2) > PAIR_LAG or abs(cap - dk_cap) > CROSS_BOOK_LAG:
            continue
        if update and update2 and abs(update - update2) > PAIR_LAG:
            continue
        aligned[book] = pair
        updates.extend([update, update2])
    pairs = aligned
    out = {"dk_odds": dk_odds, "dk_pts": dk_pts, "books": len(others_any), "same_line": len(pairs),
           "edge": None, "ref": None, "win": None, "offline": None,
           "offer_captured_at": dk_cap.isoformat(),
           "provider_updated_at": dk_update.isoformat() if dk_update else None,
           "freshness_verified": all(u is not None and 0 <= (t-u).total_seconds() <= MAX_ODDS_AGE_H*3600 for u in updates),
           "break_even": implied(dk_odds), "ev_pct": None, "disagreement_pp": None}
    if len(pairs) >= max(2, min_books):
        nv_dk = novig((dk_odds, dk2[1]))
        if B.PINNACLE in pairs:
            ref_nv, out["ref"] = novig(pairs[B.PINNACLE]), "Pinnacle"
        else:
            ref_nv, out["ref"] = sum(novig(p) for p in pairs.values()) / len(pairs), "consensus"
        # Offered-price return is distinct from disagreement with DK's fair price.
        out["edge"] = 100 * (ref_nv - out["break_even"])
        out["ev_pct"] = 100 * (ref_nv * (1 + profit_per_unit(dk_odds)) - 1)
        out["disagreement_pp"] = 100 * (ref_nv - nv_dk)
        out["win"] = ref_nv
        return out
    if market != "h2h" and others_any:
        # DK's line differs from every other book: show the gap, do not size it
        ref_b = B.PINNACLE if B.PINNACLE in others_any else None
        ref_pts = others_any[ref_b][0] if ref_b else sum(r[0] for r in others_any.values() if r[0] is not None) / max(1, len(others_any))
        out.update({"edge": 0.0, "ref": (ref_b or "consensus") + " (off-line)", "offline": ref_pts,
                    "win": novig((dk_odds, dk2[1]))})
        return out
    return None


def build_candidates(events: dict, *, league: str, t: dt.datetime, start: dt.datetime,
                     end: dt.datetime, c: dict, rules: dict, reviews=None) -> tuple[list[dict], dict]:
    # Provisional safety margin, not a fitted/validated profitability threshold.
    floor = max(0.0, cfg_float(c, "MIN_OFFER_EDGE_PP", 1.0))
    min_books = int(cfg_float(c, "MIN_CONSENSUS_BOOKS", 2))
    dog_cap = cfg_float(c, "MAX_DOG_SPREAD_POINTS", 0.0)
    totals_max = int(cfg_float(c, "TOTALS_MAX_UNITS", 1))
    blocked = {(b["league"], b["market"]) for b in rules.get("blocked", [])}
    latest = latest_pull(events)
    out = []
    diag = {"events": 0, "no_dk": 0, "few_books": 0, "odds_range": 0, "dog_cap": 0}
    B = bs()
    for ev in events.values():
        if league != "all" and ev.sport != league:
            continue
        if not (start <= ev.kick <= end) or ev.kick <= t:
            continue
        diag["events"] += 1
        lp = latest.get(ev.sport)
        if lp is None:
            continue
        age_h = (t - lp).total_seconds() / 3600.0
        stale = age_h > MAX_ODDS_AGE_H
        for market in MARKETS:
            sels = {sel for (b, mk, sel) in ev.series if mk == market and b == B.DK}
            for sel in sels:
                pe = price_edge(ev, market, sel, t, lp, min_books)
                if pe is None:
                    diag["no_dk"] += 1
                    continue
                odds, pts = pe["dk_odds"], pe["dk_pts"]
                if not MIN_ODDS <= odds <= MAX_ODDS:
                    diag["odds_range"] += 1
                    continue
                if market == "spreads" and dog_cap and ev.sport in FOOTBALL and pts is not None and pts > dog_cap:
                    diag["dog_cap"] += 1
                    continue
                if pe["edge"] is None:
                    diag["few_books"] += 1
                    continue
                edge, win = pe["edge"], pe["win"]
                lg = LEAGUE_LABEL[ev.sport]
                is_blocked = (lg, market) in blocked
                offline = pe["offline"] is not None
                qualified = edge >= floor and pe["ev_pct"] is not None and pe["ev_pct"] > 0 and not is_blocked and not offline
                units = 0
                fan = bool(FAN_RE.search(f"{ev.away} {ev.home} {sel}"))
                out.append({
                    "event_id": ev.event_id, "league": lg, "sport": ev.sport,
                    "away": ev.away, "home": ev.home, "kick": ev.kick,
                    "market": market, "selection": sel, "line": pts, "odds": odds,
                    "edge": round(edge, 2), "ref": pe["ref"], "win": round(win * 100, 1),
                    "books": pe["same_line"] if not offline else pe["books"],
                    "offline": pe["offline"], "age_h": round(age_h, 1),
                    "stale": stale, "qualified": qualified, "blocked": is_blocked,
                    "units": units, "fan": fan,
                    "break_even_pct": round(pe["break_even"] * 100, 3),
                    "ev_pct": round(pe["ev_pct"], 3) if pe["ev_pct"] is not None else None,
                    "disagreement_pp": pe["disagreement_pp"],
                    "probability_kind": "market conditional non-push" if market != "h2h" and pts is not None and float(pts).is_integer() else "market no-vig",
                    "freshness_verified": pe["freshness_verified"],
                    "offer_captured_at": pe["offer_captured_at"], "provider_updated_at": pe["provider_updated_at"],
                    "process_version": PROCESS_VERSION,
                })
                apply_review(out[-1], reviews or [], t)
    return out, diag


def candidate_key(c):
    return (str(c.get("event_id", "")), str(c.get("market", "")).lower(),
            str(c.get("selection", "")).lower(), line_identity(c.get("line", c.get("line_points"))))


def apply_review(c, reviews, t):
    """Exact-price evidence is a permission/veto, never a probability adjustment."""
    c["decision"], c["review_status"], c["units"] = "WATCH", "missing current review", 0
    matches = [r for r in reviews if candidate_key(r) == candidate_key(c)]
    if not matches:
        return
    r = matches[-1]
    if r.get("decision") == "PASS":
        c["decision"], c["review_status"] = "PASS", r.get("reason", "manual veto")
        c["qualified"] = False
        return
    stamp, confirmed = parse_stamp(r.get("reviewed_at")), parse_stamp(r.get("book_confirmed_at"))
    try:
        exact_price = float(r.get("odds_american")) == c["odds"]
    except (ValueError, TypeError):
        exact_price = False
    fields = ("availability", "venue_weather", "matchup", "line_movement")
    complete = all(isinstance(r.get(k), str) and r[k].strip() for k in fields)
    sources = r.get("sources")
    complete = complete and isinstance(sources, list) and bool(sources) and all(isinstance(s, str) and s.startswith("https://") for s in sources)
    if c["market"] == "totals":
        total = r.get("totals_review") or {}
        complete = complete and all(isinstance(total.get(k), str) and total[k].strip() for k in ("team_scores", "ceiling_floor", "garbage_time"))
    timely = stamp is not None and confirmed is not None and 0 <= (t-stamp).total_seconds() <= 21600 and 0 <= (t-confirmed).total_seconds() <= 1800
    if not (complete and timely and exact_price and r.get("book") == "draftkings"):
        c["review_status"] = "incomplete, stale or mismatched review"
        return
    c["review_status"], c["review"] = "review complete", r
    if r.get("decision") == "PICK" and c["qualified"] and c["freshness_verified"] and not c["stale"]:
        c["decision"], c["units"] = "PICK", 1
    elif not c["freshness_verified"]:
        c["review_status"] = "provider timestamp unknown/stale; surveillance only"


def rank(cands: list[dict], *, count: int, by: str = "win") -> list[dict]:
    """Qualified plays first — by win probability then edge (David 2026-09-26),
    or by edge then win with `by="edge"` — one per game; then the best
    board-only rows by edge to fill `count`."""
    key = (lambda c: (-c["edge"], -c["win"])) if by == "edge" else (lambda c: (-c["win"], -c["edge"]))
    q = sorted((c for c in cands if c["qualified"]), key=key)
    picked, seen = [], set()
    for c in q:
        if c["event_id"] in seen:
            continue
        seen.add(c["event_id"])
        picked.append(c)
    rest = sorted((c for c in cands if not c["qualified"] and c["event_id"] not in seen),
                  key=lambda c: -c["edge"])
    for c in rest:
        if len(picked) >= count:
            break
        if c["event_id"] in seen:
            continue
        seen.add(c["event_id"])
        picked.append(c)
    return picked[:max(count, len([c for c in picked if c["qualified"]]))]


def apply_caps(picks: list[dict], *, day_used: dict, week_used: int, s: dict) -> None:
    """Enforce 2u/bet, 4u/day, 10u/week in rank order. Capped rows keep
    their edge but carry 0 units and a 'cap' note."""
    week_left = s["max_week"] - week_used
    for c in picks:
        if c["units"] <= 0:
            continue
        day = c["kick"].astimezone(EASTERN).date().isoformat()
        day_left = s["max_day"] - day_used.get(day, 0)
        u = min(c["units"], s["max_bet"], day_left, week_left)
        if u <= 0:
            c["units"], c["cap"] = 0, "cap"
            continue
        c["units"] = u
        day_used[day] = day_used.get(day, 0) + u
        week_left -= u


def week_of(d: dt.date) -> dt.date:
    """Betting week runs Tuesday..Monday (the Tuesday card opens it)."""
    return d - dt.timedelta(days=(d.weekday() - 1) % 7)


def exposure(ledger: list[dict], game_days: set[str]) -> tuple[dict, int]:
    day_used: dict = {}
    week_used = 0
    weeks = {week_of(dt.date.fromisoformat(d)) for d in game_days}
    for r in ledger:
        if (r.get("decision") or "").upper() != "PICK":
            continue
        try:
            u = float(r.get("units") or 0)
            gd = dt.date.fromisoformat(r.get("game_date") or "")
        except ValueError:
            continue
        if u <= 0:
            continue
        if gd.isoformat() in game_days:
            day_used[gd.isoformat()] = day_used.get(gd.isoformat(), 0) + u
        if week_of(gd) in weeks:
            week_used += u
    return day_used, week_used


def cap_inputs(ledger, bets, shown, t, coverage_stamp=None):
    """Gross stakes never reset after settlement; unknown wager coverage fails closed.

    Existing exact recommendation allocations are excluded from the new allocation
    budget, then re-applied once at their original ceiling. Accepted bets replace
    matched recommendations instead of being counted twice.
    """
    days = {p["kick"].astimezone(EASTERN).date().isoformat() for p in shown}
    current = {candidate_key(p): p for p in shown}
    actual_keys = {candidate_key(b) for b in bets if b.get("event_id")}
    for key in actual_keys & current.keys():
        current[key]["units"] = 0
        current[key]["decision"] = "WATCH"
        current[key]["cap"] = "already wagered; no duplicate allocation"
    retained = []
    for row in ledger:
        key = candidate_key(row)
        if key in actual_keys:
            continue
        if key in current and row.get("source_report") == SOURCE:
            old = float(row.get("units") or 0)
            current[key]["units"] = min(current[key]["units"], old)
            current[key]["existing_allocation"] = old
            continue
        retained.append(row)
    for b in bets:
        retained.append({**b, "decision": "PICK", "units": b.get("stake_units")})
    day, week = exposure(retained, days)
    stamp = parse_stamp(coverage_stamp)
    known = stamp is not None and 0 <= (t-stamp).total_seconds() <= 21600
    if not known:
        for p in shown:
            p["conditional_units"] = p["units"]
            p["units"], p["decision"] = 0, "PASS" if p.get("decision") == "PASS" else "WATCH"
            p["exposure_status"] = "UNKNOWN: actual wager coverage not confirmed"
    return day, week, known


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------

def is_prop(market) -> bool:
    return str(market or "").startswith("player_")


def label(c: dict) -> str:
    m, sel, line = c["market"], c["selection"], c["line"]
    if is_prop(m):
        player = c.get("player") or sel.rsplit(" ", 1)[0]
        side = (c.get("side") or sel.rsplit(" ", 1)[-1]).lower()
        tag = PROP_LABEL.get(m, m.replace("player_", "").replace("_", " ").upper())
        if side == "yes" or line is None:
            return f"{player.upper()} {tag}"
        return f"{player.upper()} {side.upper()} {line:g} {tag}"
    if m == "h2h":
        return f"{sel.upper()} ML"
    if line is None:
        return sel.upper()
    if m == "totals":
        return f"{sel.upper()} {line:g}"
    return f"{sel.upper()} {line:+g}"


def kick_label(k: dt.datetime) -> str:
    return k.astimezone(EASTERN).strftime("%a %-I:%M %p").replace(":00", "")


def render_board(picks: list[dict], *, league: str, window: str, stale_any: bool,
                 rules: dict, floor: float) -> str:
    lines = [f"{LEAGUE_EMOJI.get(league, '🏈🏒')} {league.upper() if league != 'all' else 'NFL + CFB + NHL'} board · {window}"]
    if stale_any:
        lines.append("⚠️ odds older than 6h — 0 units until `football.py data` refreshes them")
    n = 0
    for c in picks:
        n += 1
        tag = "PICK" if c["units"] > 0 else c.get("decision", "WATCH")
        note = ""
        if c.get("cap"):
            note = " · cap"
        elif c["blocked"]:
            note = " · BLOCKED (learn)"
        elif c.get("offline") is not None:
            note = f" · off-line: market at {c['offline']:+g}" if c["market"] == "spreads" else f" · off-line: market at {c['offline']:g}"
        elif c["stale"]:
            note = " · stale"
        ev = f"{c['ev_pct']:+.2f}%" if c.get("ev_pct") is not None else "unknown"
        lines.append(f"{n}) {label(c)} {fmt_odds(c['odds'])} · market {c['win']:.1f}% · "
                     f"offered edge {c['edge']:+.2f}pp · EV {ev} · {c['units']:g}u · {tag}{note}")
        lines.append(f"   {c['away']} @ {c['home']} · {kick_label(c['kick'])} · {c['books']} books")
        lines.append(f"   {c.get('review_status', 'missing review')} · {c.get('probability_kind', 'market no-vig')}")
    plays = [c for c in picks if c["units"] > 0]
    tot = sum(c["units"] for c in plays)
    lines.append("")
    lines.append(f"Reviewed allocations: {len(plays)} for {tot:g}u · provisional offered-price margin {floor:g}pp")
    if rules.get("blocked"):
        lines.append("Blocked by learn: " + ", ".join(f"{b['league']} {b['market']}" for b in rules["blocked"]))
    lines.append("Market % is a no-vig reference, not a model forecast. Whole-point markets condition on no push. Positive reference EV is not proven profit.")
    if any(p.get("exposure_status") for p in picks):
        lines.append("Actual exposure UNKNOWN: no funded allocation without current wager-coverage confirmation.")
    return "\n".join(lines)


def ensure_units_column(rows: list[dict], cols: list[str]) -> list[str]:
    if "units" not in cols:
        cols = cols + ["units"]
        for r in rows:
            r.setdefault("units", "")
    return cols


def log_board(picks: list[dict], stamp: dt.datetime) -> int:
    rows = read_csv(LEDGER)
    with LEDGER.open(encoding="utf-8") as f:
        cols = list(csv.DictReader(f).fieldnames or [])
    cols = ensure_units_column(rows, cols)
    cols += [k for k in ("process_version", "offer_edge_pp", "reference_ev_pct", "disagreement_pp", "review_status", "review_receipt", "provider_updated_at", "probability_kind") if k not in cols]
    existing = {identity(r) for r in rows if (r.get("decision") or "").upper() in TRACKED}
    # one decision per (event, market, selection) while it is open: a re-run on a
    # later day re-prices via `check`, it does not create a second ledger row
    open_same = {(candidate_key(r), r.get("odds_american"), r.get("process_version"))
                 for r in rows if r.get("source_report") == SOURCE}
    added = 0
    for c in picks:
        if (candidate_key(c), f"{c['odds']:.0f}", PROCESS_VERSION) in open_same:
            continue
        row = {
            "decision_date": stamp.date().isoformat(),
            "game_date": c["kick"].astimezone(EASTERN).date().isoformat(),
            "league": c["league"], "event_id": c["event_id"],
            "matchup": f"{c['away']} @ {c['home']}",
            "market": c["market"], "selection": c["selection"],
            "line_points": f"{c['line']:g}" if c["line"] is not None else "",
            "book": "draftkings", "odds_american": f"{c['odds']:.0f}",
            "captured_at_et": c.get("offer_captured_at", stamp.isoformat()),
            "fair_line_or_probability": f"{c['win']:.1f}",
            "edge_cents": f"{c['edge']:.2f}",   # the number sizing uses; blank since 09-28 left the edge-tier report empty
            "decision": "PICK" if c["units"] > 0 else c.get("decision", "WATCH"),
            "units": str(c["units"]),
            "reason": f"offered edge {c['edge']:+.2f}pp vs {c['ref']}; market {c['win']:.1f}%; {c['books']} books"
                      + (f"; {c['cap']}" if c.get("cap") else ""),
            "source_report": SOURCE,
            "process_version": PROCESS_VERSION, "offer_edge_pp": f"{c['edge']:.3f}",
            "reference_ev_pct": c.get("ev_pct"), "disagreement_pp": c.get("disagreement_pp"),
            "review_status": c.get("review_status", "missing review"),
            "review_receipt": json.dumps(c.get("review", {}), sort_keys=True),
            "provider_updated_at": c.get("provider_updated_at"),
            "probability_kind": c.get("probability_kind"),
        }
        if any(identity(r) == identity(row) and r.get("process_version") == PROCESS_VERSION and r.get("odds_american") == row["odds_american"] for r in rows):
            continue
        rows.append(row)
        existing.add(identity(row))
        added += 1
    if added:
        rewrite_csv(LEDGER, rows, cols)
    return added


def cmd_board(args) -> int:
    B = bs()
    t = now_et()
    c = config()
    rules = learned_rules()
    review_doc = {}
    if getattr(args, "reviews", None):
        review_doc = json.loads(Path(args.reviews).read_text(encoding="utf-8"))
        if not isinstance(review_doc, dict) or not isinstance(review_doc.get("reviews", []), list):
            raise ValueError("review receipt must contain a reviews list")
    if not SNAPSHOTS.exists() and not FREE_LATEST.exists():
        print("no odds log; run `football.py data` first")
        return 1
    events = load_all_events()
    if args.date:
        d = dt.date.fromisoformat(args.date)
        start = dt.datetime.combine(d, dt.time(0), EASTERN)
        until = dt.date.fromisoformat(args.until) if getattr(args, "until", None) else d
        end = dt.datetime.combine(until, dt.time(23, 59, 59), EASTERN)
        window = d.strftime("%a %b %-d")
    else:
        start, end = t, t + dt.timedelta(hours=args.hours)
        if getattr(args, "until", None):
            end = dt.datetime.combine(dt.date.fromisoformat(args.until), dt.time(23, 59, 59), EASTERN)
        window = f"next {args.hours}h"
    if end < start:
        raise ValueError("--until cannot precede the start date")
    window = f"{start.date().isoformat()} through {end.date().isoformat()} ET"
    cands, diag = build_candidates(events, league=args.league, t=t, start=start, end=end, c=c, rules=rules, reviews=review_doc.get("reviews", []))
    by = args.rank or c.get("RANK_BY", "win")
    picks = rank(cands, count=args.count, by=by)
    shown, hidden = picks[:args.count], picks[args.count:]
    for p in hidden:            # a play David never sees is not a play
        p["units"] = 0
    ledger = read_csv(LEDGER)
    day_used, week_used, exposure_known = cap_inputs(ledger, read_csv(BET_LEDGER), shown, t, review_doc.get("exposure_complete_through"))
    apply_caps(shown, day_used=day_used, week_used=week_used, s=staking())
    stale_any = any(p["stale"] for p in shown)
    floor = max(0.0, cfg_float(c, "MIN_OFFER_EDGE_PP", 1.0))
    text = render_board(shown, league=args.league, window=window, stale_any=stale_any,
                        rules=rules, floor=floor)
    json_mode = getattr(args, "json", False)
    if not json_mode:
        print(text)
    if hidden and not json_mode:
        print(f"(+{len(hidden)} more qualified row(s) logged as LEAN at 0u, not shown)")
    if not picks and not json_mode:
        print(f"({diag['events']} game(s) in window · {diag['few_books']} side(s) with fewer than "
              f"{int(cfg_float(c, 'MIN_CONSENSUS_BOOKS', 2))} other books priced · {diag['no_dk']} without a DK row · "
              f"{diag['odds_range']} outside {MIN_ODDS}/{MAX_ODDS} · {diag['dog_cap']} past the dog cap. "
              f"Early in the week most books have not posted; the 7:30 AM pull fills them in.)")
    payload = {
        "generated": t.isoformat(), "league": args.league, "window": window,
        "process_version": PROCESS_VERSION, "exposure_known": exposure_known, "diagnostics": diag,
        "rows": [{**p, "kick": p["kick"].isoformat()} for p in picks],
    }
    if json_mode:
        print(json.dumps(payload, indent=2))
    added = 0
    if not args.no_log:
        LAST_BOARD.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        added = log_board(picks, t)
        if not json_mode:
            print(f"\nlogged {added} new frozen decision/shadow row(s) to {LEDGER.name}")
        log_run(f"board {args.league} ({window})",
            [f"{len(cands)} candidates · {sum(1 for p in picks if p['qualified'])} qualified · "
             f"{sum(1 for p in picks if p['units'] > 0)} plays for "
             f"{sum(p['units'] for p in picks)}u · logged {added}" + (" · STALE odds" if stale_any else "")])
    return 0


# ---------------------------------------------------------------------------
# daily card — DAILY_PICKS a day across the leagues playing today (2026-10-07)
# ---------------------------------------------------------------------------

MIRROR = {"over": "Under", "under": "Over"}


def load_prop_quotes(t: dt.datetime, path: Path | None = None) -> dict:
    """(sport, event_id, market, player, selection, line) ->
    {"books": {book: (odds, captured, provider_update)}, "away", "home", "kick"},
    newest capture per book; anything older than MAX_ODDS_AGE_H is dropped."""
    out: dict = {}
    for r in read_csv(path or PROPS):
        cap = parse_stamp(r.get("captured_at_et"))
        if cap is None or cap > t or (t - cap).total_seconds() > MAX_ODDS_AGE_H * 3600:
            continue
        if not valid_odds(r.get("odds_american")):
            continue
        key = (r.get("sport"), r.get("event_id"), r.get("market"), r.get("player"),
               (r.get("selection") or "").strip(), line_identity(r.get("line_points")))
        meta = out.setdefault(key, {"books": {}, "away": r.get("away_team"), "home": r.get("home_team"),
                                    "kick": parse_stamp(r.get("commence_time_et"))})
        prev = meta["books"].get(r.get("book"))
        if prev is None or cap > prev[1]:
            meta["books"][r["book"]] = (float(r["odds_american"]), cap, parse_stamp(r.get("bookmaker_last_update")))
    return out


def build_prop_candidates(t: dt.datetime, start: dt.datetime, end: dt.datetime, *, league: str, c: dict,
                          rules: dict, reviews=None, quotes: dict | None = None) -> list[dict]:
    """DraftKings' prop price against the other books on the same player, market
    and line, the way price_edge treats game lines. A 'Yes' market has one side,
    so its reference is Pinnacle's implied probability when Pinnacle prices the
    player, else the mean implied across the other books, vig included and
    labelled so. Over/Under markets de-vig per book when both sides are posted."""
    B = bs()
    floor = max(0.0, cfg_float(c, "MIN_OFFER_EDGE_PP", 1.0))
    min_books = int(cfg_float(c, "MIN_CONSENSUS_BOOKS", 2))
    max_odds = cfg_float(c, "PROPS_MAX_ODDS", MAX_ODDS)
    blocked = {(b["league"], b["market"]) for b in rules.get("blocked", [])}
    quotes = quotes if quotes is not None else load_prop_quotes(t)
    out = []
    for (sport, eid, market, player, sel, line), meta in quotes.items():
        if sport not in LEAGUE_LABEL or (league != "all" and sport != league):
            continue
        kick = meta["kick"]
        if kick is None or not (start <= kick <= end) or kick <= t:
            continue
        books = meta["books"]
        dk = books.get(B.DK)
        if dk is None:
            continue
        dk_odds, dk_cap, dk_update = dk
        if not MIN_ODDS <= dk_odds <= max_odds:
            continue
        others = {b: q for b, q in books.items() if b != B.DK}
        if len(others) < max(1, min_books):
            continue
        side = sel.lower()
        kind, ref_name = "market implied (incl. vig)", "consensus"
        if side in MIRROR:
            mirror = quotes.get((sport, eid, market, player, MIRROR[side], line), {"books": {}})["books"]
            devig = {b: novig((q[0], mirror[b][0])) for b, q in others.items() if b in mirror}
            if devig:
                kind = "market no-vig"
                ref = devig[B.PINNACLE] if B.PINNACLE in devig else sum(devig.values()) / len(devig)
                ref_name = "Pinnacle" if B.PINNACLE in devig else "consensus"
            else:
                ref = sum(implied(q[0]) for q in others.values()) / len(others)
        elif B.PINNACLE in others:
            ref, ref_name = implied(others[B.PINNACLE][0]), "Pinnacle"
        else:
            ref = sum(implied(q[0]) for q in others.values()) / len(others)
        be = implied(dk_odds)
        edge = 100 * (ref - be)
        ev = 100 * (ref * (1 + profit_per_unit(dk_odds)) - 1)
        lg = LEAGUE_LABEL[sport]
        is_blocked = (lg, market) in blocked
        age_h = (t - dk_cap).total_seconds() / 3600
        fresh = dk_update is not None and 0 <= (t - dk_update).total_seconds() <= MAX_ODDS_AGE_H * 3600
        cand = {
            "event_id": eid, "league": lg, "sport": sport, "away": meta["away"], "home": meta["home"],
            "kick": kick, "market": market, "selection": f"{player} {sel}", "player": player, "side": sel,
            "line": float(line) if line else None, "odds": dk_odds,
            "edge": round(edge, 2), "ref": ref_name, "win": round(ref * 100, 1), "books": len(others),
            "offline": None, "age_h": round(age_h, 1), "stale": age_h > MAX_ODDS_AGE_H,
            "qualified": edge >= floor and ev > 0 and not is_blocked, "blocked": is_blocked, "units": 0,
            "fan": bool(FAN_RE.search(f"{meta['away']} {meta['home']}")),
            "break_even_pct": round(be * 100, 3), "ev_pct": round(ev, 3), "disagreement_pp": None,
            "probability_kind": kind, "freshness_verified": fresh,
            "offer_captured_at": dk_cap.isoformat(),
            "provider_updated_at": dk_update.isoformat() if dk_update else None,
            "process_version": PROCESS_VERSION,
        }
        apply_review(cand, reviews or [], t)
        out.append(cand)
    return out


def suggested_units(c: dict, cfg: dict) -> int:
    """What staking-config.md would size this at if David plays it. The board
    allocates nothing on its own: units come only through apply_review."""
    if not c["qualified"] or c["stale"]:
        return 0
    two, one = cfg_float(cfg, "EDGE_TWO_UNIT_CENTS", 2.0), cfg_float(cfg, "EDGE_FLOOR_CENTS", 1.5)
    u = 2 if c["edge"] >= two else 1 if c["edge"] >= one else 0
    if c["market"] == "totals":
        u = min(u, int(cfg_float(cfg, "TOTALS_MAX_UNITS", 1)))
    if is_prop(c["market"]):
        u = min(u, int(cfg_float(cfg, "PROPS_MAX_UNITS", 1)))
    if c.get("fan"):
        u = min(u, 1)
    return min(u, staking()["max_bet"])


def rank_daily(cands: list[dict], *, count: int) -> tuple[list[dict], list[dict]]:
    """Win-first (David 2026-10-04: "we're in this to win"): qualified rows by
    market win %, edge as tiebreak, one per game. Short of `count`, the best
    unqualified rows fill the card flagged board-only. Returns (shown, extra
    qualified rows that still get logged)."""
    key = lambda c: (-c["win"], -c["edge"])   # noqa: E731
    picked, seen = [], set()
    for c in sorted((c for c in cands if c["qualified"]), key=key):
        if c["event_id"] in seen:
            continue
        seen.add(c["event_id"])
        picked.append(c)
    shown, extra = picked[:count], picked[count:]
    for c in sorted((c for c in cands if not c["qualified"] and c.get("decision") != "PASS"), key=key):
        if len(shown) >= count:
            break
        if c["event_id"] in seen:
            continue
        seen.add(c["event_id"])
        c["board_only"] = True
        shown.append(c)
    return shown, extra


SLOTS = (("ML", lambda c: c["market"] == "h2h"),
         ("O/U", lambda c: c["market"] == "totals"),
         ("PROP", lambda c: is_prop(c["market"])))
LEAGUE_ORDER = ("NHL", "NFL", "CFB")   # default; DAILY_LEAGUE_ORDER in config.env overrides


def league_order(c: dict | None = None) -> tuple:
    """Card order. David 2026-10-13: "really getting into hockey, prioritize
    that", so NHL leads whenever it has games."""
    raw = (c or config()).get("DAILY_LEAGUE_ORDER", "")
    order = tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return order or LEAGUE_ORDER


def rank_slots(cands: list[dict], *, count: int = 3) -> dict:
    """DAILY_SHAPE=slots (David 2026-10-07: "ML, over under, scorer or any of the
    best props for each NFL, NCAA and NHL"): per league, the likeliest winner,
    the likelier side of a total, the likeliest prop — win-first inside each
    slot, different games where the slate allows. Edge decides units, not the
    pick; a row the gate did not pass is shown board-only with the reason."""
    key = lambda c: (-c["win"], -c["edge"])   # noqa: E731
    out: dict = {}
    for lg in league_order():
        rows = [c for c in cands if c["league"] == lg and c.get("decision") != "PASS"]
        if not rows:
            continue
        picks, seen = [], set()
        for slot, pred in SLOTS[:count]:
            pool = sorted((c for c in rows if pred(c)), key=key)
            choice = next((c for c in pool if c["event_id"] not in seen), pool[0] if pool else None)
            if choice is None:
                continue
            seen.add(choice["event_id"])
            choice["slot"] = slot
            if not choice["qualified"]:
                choice["board_only"] = True
            picks.append(choice)
        out[lg] = picks
    return out


def why_not(c: dict) -> str:
    """Plain words for a row the gate did not pass (shown board-only)."""
    if c.get("decision") == "PASS":
        return "manual pass"
    if c.get("blocked"):
        return "blocked by learn"
    if c.get("stale"):
        return "odds stale"
    if c.get("offline") is not None:
        return "DK's number is not Pinnacle's; shown, never sized"
    if (c.get("books") or 0) < 2:
        return f"only {c.get('books') or 0} other book(s) priced; the 7:30 paid pull adds the rest"
    if c.get("ev_pct") is not None and c["ev_pct"] <= 0:
        return "DK's price is below the market"
    return "under the edge floor"


def pick_lines(i: int, c: dict, cfg: dict) -> list[str]:
    u = suggested_units(c, cfg)
    tag = "board only" if c.get("board_only") else (f"{u}u if played" if u else "0u")
    first = f"{i}) {label(c)} · {c['win']:.0f}% to win · DK {fmt_odds(c['odds'])} · {tag} · {kick_label(c['kick'])}"
    if c.get("board_only"):
        second = f"   {c['away']} @ {c['home']} · {c['league']} · market {c['win']:.1f}% · no edge: {why_not(c)}"
    else:
        second = (f"   {c['away']} @ {c['home']} · {c['league']} · {c['ref']} {c['win']:.1f}% vs DK's "
                  f"{c['break_even_pct']:.1f}% ({c['edge']:+.1f}pp) · {c['books']} books")
    return [first, second]


FOOTER = ("Win % is the market's number, not a forecast (a 60% pick loses 2 in 5). "
          "Units are suggestions until reviewed; `football.py bet N --units U` logs what you place.")


def render_daily_slots(by_league: dict, *, day: dt.date, cfg: dict, diag: dict | None = None) -> str:
    lines = [f"🏈🏒 Daily picks · {day:%a %b %-d}"]
    n = 0
    for lg, picks in by_league.items():
        lines.append(f"{LEAGUE_EMOJI[LEAGUE_ARG[lg]]} {lg}")
        for c in picks:
            n += 1
            lines += pick_lines(n, c, cfg)
    if not n:
        d = diag or {}
        lines.append(f"nothing priced yet ({d.get('events', 0)} game(s) today; props pull runs with the 7:30 data loop)")
    lines.append("Per league: likeliest winner, likelier side of a total, likeliest prop. " + FOOTER)
    return "\n".join(lines)


def render_daily(picks: list[dict], extra: list[dict], *, day: dt.date, cfg: dict, diag: dict | None = None) -> str:
    emojis = "".join(dict.fromkeys(LEAGUE_EMOJI[c["sport"]] for c in picks)) or "🏈🏒"
    lines = [f"{emojis} Daily {len(picks)} · {day:%a %b %-d}"]
    for i, c in enumerate(picks, 1):
        lines += pick_lines(i, c, cfg)
    if not picks:
        d = diag or {}
        lines.append(f"nothing priced yet ({d.get('events', 0)} game(s) today; props pull runs with the 7:30 data loop)")
    if extra:
        lines.append(f"+{len(extra)} more qualified row(s) logged, not shown")
    lines.append("One per game. " + FOOTER)
    return "\n".join(lines)


def cmd_daily(args) -> int:
    t = now_et()
    c = config()
    rules = learned_rules()
    day = dt.date.fromisoformat(args.date) if args.date else t.date()
    start = max(t, dt.datetime.combine(day, dt.time(0), EASTERN))
    end = dt.datetime.combine(day, dt.time(23, 59, 59), EASTERN)
    count = args.count or int(cfg_float(c, "DAILY_PICKS", 3))
    # The card is moneyline, over/under and player props (David 2026-10-02 and
    # 2026-10-07: "not a spread list"); spreads and puck lines stay on `board`.
    markets = {m.strip() for m in c.get("DAILY_MARKETS", "h2h,totals,player").split(",") if m.strip()}
    games, diag = [], {"events": 0}
    if SNAPSHOTS.exists() or FREE_LATEST.exists():
        events = load_all_events()
        games, diag = build_candidates(events, league=args.league, t=t, start=start, end=end, c=c, rules=rules)
        games = [g for g in games if g["market"] in markets]
    props = build_prop_candidates(t, start, end, league=args.league, c=c, rules=rules) if "player" in markets else []
    shape = (getattr(args, "shape", None) or c.get("DAILY_SHAPE", "slots")).lower()
    if shape == "slots":
        by_league = rank_slots(games + props, count=count)
        picks, extra = [p for ps in by_league.values() for p in ps], []
        text = render_daily_slots(by_league, day=day, cfg=c, diag=diag)
    else:
        picks, extra = rank_daily(games + props, count=count)
        text = render_daily(picks, extra, day=day, cfg=c, diag=diag)
    for p in picks:
        p["suggested_units"] = suggested_units(p, c)
    payload = {"generated": t.isoformat(), "league": args.league, "window": f"{day.isoformat()} ET", "card": f"daily-{shape}",
               "process_version": PROCESS_VERSION, "diagnostics": {**diag, "props": len(props)},
               "rows": [{**p, "kick": p["kick"].isoformat()} for p in picks + extra]}
    print(json.dumps(payload, indent=2) if args.json else text)
    if not args.no_log:
        LAST_BOARD.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        added = log_board(picks + extra, t)
        if not args.json:
            print(f"\nlogged {added} new row(s) to {LEDGER.name}")
        log_run(f"daily ({day.isoformat()})",
                [f"{len(games)} game-line + {len(props)} prop candidate(s) · {len(picks)} shown "
                 f"({sum(1 for p in picks if p['qualified'])} qualified) · logged {added}"])
    return 0


# ---------------------------------------------------------------------------
# check — is the number we listed still there?
# ---------------------------------------------------------------------------

def cmd_check(args) -> int:
    B = bs()
    t = now_et()
    c = config()
    max_pts = cfg_float(c, "CHECK_MAX_POINT_LOSS", 1.0)
    max_cents = cfg_float(c, "CHECK_MAX_PRICE_LOSS_CENTS", 10)
    events = load_all_events()
    latest = latest_pull(events)
    min_books = int(cfg_float(c, "MIN_CONSENSUS_BOOKS", 2))
    open_rows = [r for r in read_csv(LEDGER)
                 if (r.get("decision") or "").upper() in TRACKED and not r.get("result")
                 and r.get("source_report") == SOURCE]
    if not open_rows:
        print("no open football.py rows to check")
        return 0
    lines = []
    for r in open_rows:
        if is_prop(r.get("market")):
            continue            # props are pulled once a day; there is no live number to re-check
        ev = events.get(r["event_id"])
        if ev is None or ev.kick <= t:
            continue
        try:
            took_odds = float(r["odds_american"])
            took_pts = float(r["line_points"]) if r.get("line_points") else None
        except ValueError:
            continue
        pe = price_edge(ev, r["market"], r["selection"], t, latest.get(ev.sport, t), min_books)
        if pe is None:
            lines.append(f"? {r['selection']} — DK is not offering this market in the latest pull")
            continue
        now_odds, now_pts = pe["dk_odds"], pe["dk_pts"]
        league = {"CFB": "cfb", "NHL": "nhl"}.get(r["league"], "nfl")
        cents, pts = B.edge_pct(r["market"], r["selection"], league,
                                took_odds, took_pts, implied(now_odds), now_pts)
        # cents > 0: our listed price beats the current one (number is gone for new money).
        # NHL: a puck line or goal total that moved against us at all is gone (no linear points value).
        gone = cents > max_cents or (pts is not None and (pts > max_pts or (league == "nhl" and pts > 1e-9)))
        edge_now = pe["edge"]
        state = "NUMBER GONE" if gone else ("better now" if cents < -0.05 else "still there")

        def pts_label(p):
            if p is None:
                return ""
            return f"{p:+g} " if r["market"] == "spreads" else f"{p:g} "

        edge_txt = f" · edge now {edge_now:+.1f}¢" if edge_now is not None else ""
        lines.append(f"{'✗' if gone else '✓'} {r['selection'].upper()} {pts_label(took_pts)}{fmt_odds(took_odds)} → "
                     f"now {pts_label(now_pts)}{fmt_odds(now_odds)} · {state}{edge_txt}")
    print("\n".join(lines) if lines else "no open rows with a future kickoff")
    log_run("check", [f"{len(lines)} open row(s) checked · "
                      f"{sum(1 for l in lines if l.startswith('✗'))} number(s) gone"])
    return 0


# ---------------------------------------------------------------------------
# settle / data — existing scripts, one door
# ---------------------------------------------------------------------------

def run_script(name: str, *flags: str, timeout: int = 600) -> tuple[bool, str]:
    try:
        p = subprocess.run([PY, "-B", str(AGENT / name), *flags], capture_output=True,
                           text=True, timeout=timeout, cwd=str(VAULT))
    except subprocess.TimeoutExpired:
        return False, f"{name}: timeout"
    out = (p.stdout or "") + (("\n" + p.stderr) if p.returncode and p.stderr else "")
    return p.returncode == 0, out.strip()


def normalized_result(value):
    return {"W": "win", "WIN": "win", "L": "loss", "LOSS": "loss", "PUSH": "push", "VOID": "void"}.get(str(value or "").upper(), "")


def settlement_key(row):
    key = candidate_key(row)
    if not row.get("event_id") or not row.get("game_date") or (row.get("market") != "h2h" and not key[-1]):
        return None
    return (row.get("game_date"),) + key


def settle_bets(ledger: list[dict]) -> int:
    """Copy a final grade only for the identical event/date/market/selection/line.

    Old wagers missing identity/line stay pending; no odds, line or stake is
    invented. Conflicting recommendation grades also stay pending.
    """
    bets = read_csv(BET_LEDGER)
    if not bets:
        return 0
    s = staking()
    by_key = {}
    for r in ledger:
        res, key = normalized_result(r.get("result")), settlement_key(r)
        if res and key:
            by_key.setdefault(key, set()).add(res)
    changed = 0
    bank = s["starting_bankroll"]
    parlay_book = json.loads(PARLAYS.read_text(encoding="utf-8")) if PARLAYS.exists() else {}
    for b in bets:
        before = dict(b)
        if not normalized_result(b.get("result")):
            if b.get("market") in ("parlay", "lotto"):
                res, odds = settle_parlay(b, by_key, parlay_book)
                if res:
                    b["result"] = res
                if odds is not None:
                    b["odds_american"] = f"{odds:.0f}"   # a pushed leg dropped out
            else:
                results = by_key.get(settlement_key(b), set())
                if len(results) == 1:
                    b["result"] = next(iter(results))
        else:
            b["result"] = normalized_result(b["result"])
        if normalized_result(b.get("result")):
            try:
                u, o = float(b.get("stake_units")), float(b.get("odds_american"))
            except (ValueError, TypeError):
                continue
            if not math.isfinite(u) or u <= 0 or not valid_odds(o):
                continue
            net = u * profit_per_unit(o) if b["result"] == "win" else (-u if b["result"] == "loss" else 0.0)
            b["net_units"], b["net_dollars"] = f"{net:.2f}", f"{net * s['unit_size']:.2f}"
            bank += net * s["unit_size"]
            b["bankroll_after"] = f"{bank:.2f}"
        if b != before:
                changed += 1
    if changed:
        with BET_LEDGER.open(encoding="utf-8") as f:
            cols = list(csv.DictReader(f).fieldnames or [])
        rewrite_csv(BET_LEDGER, bets, cols)
    return changed


def cmd_settle(args) -> int:
    ok1, o1 = run_script("settle-picks.py", "--write")
    print(o1.splitlines()[0] if o1 else "settle: no output")
    ok3, o3 = run_script("settle-props.py", "--write")
    print(o3.splitlines()[-1] if o3 else "settle-props: no output")
    ok2, o2 = run_script("grade-clv.py", "--write")
    print(o2.splitlines()[-1] if o2 else "clv: no output")
    n = settle_bets(read_csv(LEDGER))
    if n:
        print(f"settled {n} real wager(s) in {BET_LEDGER.name}")
    log_run("settle", [f"settle-picks {'ok' if ok1 else 'FAIL'} · settle-props {'ok' if ok3 else 'FAIL'} · "
                       f"grade-clv {'ok' if ok2 else 'FAIL'} · {n} wager(s) settled"])
    return 0 if ok1 and ok2 and ok3 else 1


def cmd_data(args) -> int:
    ok, out = run_script("run-daily-data.py", timeout=1200)
    print(out.splitlines()[-1] if out else "data: no output")
    ok2, out2 = run_script("fetch-free-lines.py", timeout=120)
    print(out2.splitlines()[-1] if out2 else "free-lines: no output")
    return 0 if ok and ok2 else 1


# ---------------------------------------------------------------------------
# learn — the loop that turns mistakes into rules
# ---------------------------------------------------------------------------

def edge_bucket(v: float | None) -> str:
    if v is None:
        return "?"
    for lo, hi, name in EDGE_BUCKETS:
        if lo <= v < hi:
            return name
    return "<1.5c"


def slice_stats(rows: list[dict]) -> dict:
    w = sum(1 for r in rows if r.get("result") == "W")
    l = sum(1 for r in rows if r.get("result") == "L")
    p = sum(1 for r in rows if r.get("result") == "PUSH")
    roi = 0.0
    for r in rows:
        try:
            o = float(r.get("odds_american"))
        except (TypeError, ValueError):
            continue
        if r.get("result") == "W":
            roi += profit_per_unit(o)
        elif r.get("result") == "L":
            roi -= 1.0
    graded = w + l + p
    # grade-clv.py writes closing_odds_american + beat_close; it never wrote the
    # clv_status / closing_source columns this once demanded, so beat-close read
    # "—" on every slice and the learn gate could not fire (found 2026-10-13).
    valid_closes = [r for r in rows if normalized_result(r.get("result")) in ("win", "loss", "push")
                    and r.get("closing_odds_american") and r.get("beat_close") in ("0", "1")]
    bc = [r.get("beat_close") for r in valid_closes if r.get("beat_close") in ("0", "1")]
    clv = []
    for r in valid_closes:
        try:
            clv.append(float(r.get("closing_line_value_pct")))
        except (TypeError, ValueError):
            pass
    return {"n": len(rows), "graded": graded, "w": w, "l": l, "p": p,
            "roi_u": round(roi, 2), "roi_pct": round(roi / graded * 100, 1) if graded else None,
            "beat": round(bc.count("1") / len(bc), 2) if bc else None, "beat_n": len(bc),
            "clv": round(sum(clv) / len(clv), 2) if clv else None}


def learn(ledger: list[dict]) -> dict:
    tracked = [r for r in ledger if (r.get("decision") or "").upper() in TRACKED]
    new = [r for r in tracked if r.get("source_report") == SOURCE and r.get("process_version") == PROCESS_VERSION]
    legacy = [r for r in tracked if r not in new]
    out = {"generated": now_et().isoformat(), "new_rows": len(new), "legacy_rows": len(legacy),
           "slices": {}, "legacy": {}, "blocked": [], "working": [], "pending_review": []}

    def grp(rows, keyf):
        d = defaultdict(list)
        for r in rows:
            d[keyf(r)].append(r)
        return d

    def edge_of(r):
        try:
            return float(r.get("edge_cents"))
        except (TypeError, ValueError):
            return None

    for name, keyf in (("league_market", lambda r: f"{r['league']} {r['market']}"),
                       ("edge_bucket", lambda r: edge_bucket(edge_of(r))),
                       ("league", lambda r: r["league"]),
                       ("market", lambda r: r["market"])):
        out["slices"][name] = {k: slice_stats(v) for k, v in sorted(grp(new, keyf).items())}
        out["legacy"][name] = {k: slice_stats(v) for k, v in sorted(grp(legacy, keyf).items())}

    for k, st in out["slices"]["league_market"].items():
        if st["graded"] < LEARN_MIN_N or st["beat_n"] < LEARN_MIN_N:
            continue
        lg, mk = k.split(" ", 1)
        if st["beat"] is not None and st["beat"] < BLOCK_BEAT_BELOW and (st["roi_u"] or 0) < 0:
            out["pending_review"].append({"league": lg, "market": mk, "signal": "underperforming", **st})
        elif st["beat"] is not None and st["beat"] >= 0.55 and (st["roi_u"] or 0) > 0:
            out["pending_review"].append({"league": lg, "market": mk, "signal": "promising, not validated", **st})
    return out


SEED_LESSONS = """\
# Lessons — what the record already taught us (auto-maintained by `football.py learn`)

These are encoded as rules, not remembered as advice:

1. **Model-born totals lost** (CFB totals 11-21, 34% beat-close through 2026-09-18;
   the SP+ blend lost its own backtest). Rule: totals are only ever price-born
   and carry at most `TOTALS_MAX_UNITS` (config.env, default 1).
2. **Ratings never beat the close** (Elo, SRS, pace, cheap-EPA all lost). Rule:
   no ratings gate anywhere; candidates come from the odds log; NFL is live.
3. **Confidence weights ordered noise** (correlation with CLV 0.05; the 80+ bucket
   was negative). Rule: rank by win % among edge-qualified rows; size from edge.
4. **Price chasing** (movement-toward-us bonus bought worse numbers). Rule:
   `check` flags a listed number that is gone; nothing is re-rated upward.
5. **Sizing outran evidence** (2u on an unvalidated tier lost the week). Rule:
   2u only at >=2c edge; caps live in staking-config.md and are enforced in code.
6. **Unlogged boards teach nothing** (Sep 26-27 wrote zero ledger rows). Rule:
   every qualified row is logged, PICK or LEAN, before it is shown.

Any league x market slice of the new process that is under water on 30+ graded
rows (beat-close < 45% and negative flat ROI) is blocked from the board by
`learned-rules.json` until it recovers. Legacy rows are history, shown below,
never re-litigated.
"""


def render_lessons(res: dict) -> str:
    def table(title, d):
        lines = [f"### {title}", "", "| slice | n | W-L-P | flat ROI | beat close | CLV |", "|---|---:|---|---:|---:|---:|"]
        for k, s in d.items():
            beat = f"{s['beat']*100:.0f}% ({s['beat_n']})" if s["beat"] is not None else "—"
            roi = f"{s['roi_u']:+.2f}u" + (f" ({s['roi_pct']:+.1f}%)" if s["roi_pct"] is not None else "")
            clv = f"{s['clv']:+.2f}%" if s["clv"] is not None else "—"
            lines.append(f"| {k} | {s['n']} | {s['w']}-{s['l']}-{s['p']} | {roi} | {beat} | {clv} |")
        return "\n".join(lines) + "\n"
    parts = [SEED_LESSONS, f"\n## Auto report — {res['generated'][:16]}\n",
             f"New-process rows: {res['new_rows']} · legacy rows: {res['legacy_rows']} · "
             f"verdicts need {LEARN_MIN_N} graded rows per slice.\n"]
    if res["blocked"]:
        parts.append("**BLOCKED:** " + ", ".join(f"{b['league']} {b['market']} ({b['w']}-{b['l']}, beat {b['beat']*100:.0f}%)" for b in res["blocked"]) + "\n")
    if res["working"]:
        parts.append("**Working:** " + ", ".join(f"{b['league']} {b['market']} ({b['w']}-{b['l']}, beat {b['beat']*100:.0f}%)" for b in res["working"]) + "\n")
    parts.append("\n## New process (football.py)\n")
    for name in ("league_market", "edge_bucket"):
        if res["slices"][name]:
            parts.append(table(name.replace("_", " × "), res["slices"][name]))
    parts.append("\n## Legacy process (history only)\n")
    for name in ("league_market", "edge_bucket"):
        if res["legacy"][name]:
            parts.append(table(name.replace("_", " × "), res["legacy"][name]))
    return "\n".join(parts)


def cmd_learn(args) -> int:
    ledger = read_csv(LEDGER)
    res = learn(ledger)
    LESSONS.write_text(render_lessons(res), encoding="utf-8")
    existing = learned_rules()
    RULES.write_text(json.dumps({**existing, "generated": res["generated"],
                                 "pending_review": res["pending_review"]}, indent=1), encoding="utf-8")
    lm = res["slices"]["league_market"]
    print(f"learn: {res['new_rows']} new-process rows, {res['legacy_rows']} legacy")
    for k, s in lm.items():
        beat = f"beat {s['beat']*100:.0f}%" if s["beat"] is not None else "beat —"
        print(f"  {k:14} {s['w']}-{s['l']}-{s['p']} · {s['roi_u']:+.2f}u · {beat}"
              + (" · needs 30" if s["graded"] < LEARN_MIN_N else ""))
    if res["blocked"]:
        print("  BLOCKED: " + ", ".join(f"{b['league']} {b['market']}" for b in res["blocked"]))
    else:
        print("  blocked: none")
    lg = res["legacy"]["market"]
    if lg:
        print("  legacy by market: " + " · ".join(f"{k} {s['w']}-{s['l']} ({s['roi_u']:+.1f}u)" for k, s in lg.items()))
    print(f"  wrote {LESSONS.name}, {RULES.name}")
    log_run("learn", [f"{res['new_rows']} new rows · blocked {len(res['blocked'])} · working {len(res['working'])}"])
    return 0


# ---------------------------------------------------------------------------
# bet — record what David actually did
# ---------------------------------------------------------------------------

def cmd_bet(args) -> int:
    if not LAST_BOARD.exists():
        print("no board on file; run `football.py board` first")
        return 1
    board = json.loads(LAST_BOARD.read_text(encoding="utf-8"))
    rows = board["rows"]
    if not 1 <= args.row <= len(rows):
        print(f"row must be 1..{len(rows)}")
        return 1
    c = rows[args.row - 1]
    s = staking()
    odds = args.odds if args.odds is not None else c["odds"]
    kick = dt.datetime.fromisoformat(c["kick"])
    bet = {
        "placed_at_et": now_et().isoformat(), "game_date": kick.astimezone(EASTERN).date().isoformat(),
        "league": c["league"], "matchup": f"{c['away']} @ {c['home']}", "market": c["market"],
        "selection": c["selection"], "odds_american": f"{odds:.0f}", "book": args.book,
        "stake_units": f"{args.units:g}", "stake_dollars": f"{args.units * s['unit_size']:.2f}",
        "fan_team": "YES" if c.get("fan") else "NO",
        "edge_note": f"edge {c['edge']:+.2f}c vs {c['ref']}; win {c['win']:.0f}%; board {board['generated'][:16]}",
        "result": "", "net_units": "", "net_dollars": "", "bankroll_after": "",
    }
    with BET_LEDGER.open(encoding="utf-8") as f:
        cols = list(csv.DictReader(f).fieldnames or list(bet))
    with BET_LEDGER.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=cols, lineterminator="\n", extrasaction="ignore").writerow(bet)
    print(f"logged: {label(c)} {fmt_odds(odds)} · {args.units:g}u (${args.units * s['unit_size']:.2f}) at {args.book}")
    log_run("bet", [f"{label(c)} {fmt_odds(odds)} {args.units:g}u at {args.book}"])
    return 0


# ---------------------------------------------------------------------------
# parlay — the gate David asked for (2026-10-13: "Force me to make smart bets")
# ---------------------------------------------------------------------------
PARLAYS = AGENT / "line-watch" / "parlays.json"   # legs behind each logged parlay row, for settlement


def parlay_rules(c: dict, lotto: bool = False) -> dict:
    """Hard limits from config.env; a ticket that breaks one is BLOCKED, never softened.
    David 2026-10-13: "profit over quick profit ... confident bets vs lottos, maybe 1 fun
    lotto per slate that is juicy." The lotto lane is looser on legs and floor, tighter
    on stake, and still one a day."""
    k = "LOTTO" if lotto else "PARLAY"
    d = {"max_legs": 5, "min_leg_win": 40.0, "min_hit": 3.0, "max_units": 0.25, "max_per_day": 1} if lotto else \
        {"max_legs": 3, "min_leg_win": 56.0, "min_hit": 18.0, "max_units": 0.5, "max_per_day": 1}
    return {"max_legs": int(cfg_float(c, f"{k}_MAX_LEGS", d["max_legs"])),
            "min_leg_win": cfg_float(c, f"{k}_MIN_LEG_WIN", d["min_leg_win"]),
            "min_hit": cfg_float(c, f"{k}_MIN_HIT_PCT", d["min_hit"]),
            "max_units": cfg_float(c, f"{k}_MAX_UNITS", d["max_units"]),
            "max_per_day": int(cfg_float(c, f"{k}_MAX_PER_DAY", d["max_per_day"])),
            "kind": "lotto" if lotto else "parlay"}


def american_to_decimal(o: float) -> float:
    return 1 + o / 100.0 if o > 0 else 1 + 100.0 / -o


def decimal_to_american(d: float) -> int:
    return int(round((d - 1) * 100)) if d >= 2 else int(round(-100 / (d - 1)))


def build_parlay(cands: list[dict], *, legs: int = 3, min_leg_win: float = 56.0, juicy: bool = False) -> list[dict]:
    """Win-first legs, one per game, nothing under the floor, nothing stale or vetoed.
    `juicy` (the lotto lane) takes the longest prices that still clear the floor."""
    key = (lambda c: (-american_to_decimal(c["odds"]), -c["win"])) if juicy else (lambda c: (-c["win"], -c["edge"]))
    out, seen = [], set()
    pool = [c for c in cands if c.get("decision") != "PASS" and not c.get("stale") and not c.get("blocked") and c["win"] >= min_leg_win]
    for c in sorted(pool, key=key):
        if c["event_id"] in seen:
            continue
        seen.add(c["event_id"])
        out.append(c)
        if len(out) == legs:
            break
    return out


def parlay_verdict(legs: list[dict], *, rules: dict, units: float, placed_today: int) -> dict:
    reasons = []
    if len(legs) < 2:
        reasons.append(f"only {len(legs)} leg(s) clear the {rules['min_leg_win']:g}% floor; no parlay today")
    if len(legs) > rules["max_legs"]:
        reasons.append(f"{len(legs)} legs; max is {rules['max_legs']}")
    if len({l["event_id"] for l in legs}) != len(legs):
        reasons.append("two legs from the same game")
    for l in legs:
        if l["win"] < rules["min_leg_win"]:
            reasons.append(f"{label(l)} wins {l['win']:.0f}%, under the {rules['min_leg_win']:g}% floor")
        if l.get("stale"):
            reasons.append(f"{label(l)} price is stale")
        if l.get("decision") == "PASS":
            reasons.append(f"{label(l)} was vetoed")
    hit, dec = 1.0, 1.0
    for l in legs:
        hit *= l["win"] / 100.0
        dec *= american_to_decimal(l["odds"])
    hit_pct = hit * 100.0
    if legs and hit_pct < rules["min_hit"]:
        reasons.append(f"hits {hit_pct:.0f}% of the time; floor is {rules['min_hit']:g}%")
    if units > rules["max_units"]:
        reasons.append(f"{units:g}u; parlays cap at {rules['max_units']:g}u")
    if placed_today >= rules["max_per_day"]:
        reasons.append(f"already placed {placed_today} parlay(s) today; max is {rules['max_per_day']}")
    return {"approved": not reasons, "reasons": reasons, "hit_pct": round(hit_pct, 1), "decimal": round(dec, 3),
            "american": decimal_to_american(dec) if legs else None,
            "fair_american": decimal_to_american(1 / hit) if legs and hit > 0 else None,
            "ev_pct": round((dec * hit - 1) * 100, 1) if legs else None}


def parlays_placed_today(t: dt.datetime, kind: str = "parlay") -> int:
    day = t.date().isoformat()
    return sum(1 for b in read_csv(BET_LEDGER) if b.get("market") == kind and (b.get("placed_at_et") or "")[:10] == day)


def render_parlay(legs: list[dict], v: dict, *, day: dt.date, units: float, rules: dict) -> str:
    head = "🎰 Lotto" if rules.get("kind") == "lotto" else "🎟 Parlay"
    lines = [f"{head} · {day.strftime('%a %b %-d')} · {len(legs)} legs" + (f" · hits {v['hit_pct']:.0f}%" if legs else "")
             + (f" · DK {fmt_odds(v['american'])} (fair {fmt_odds(v['fair_american'])})" if v["american"] is not None else "")]
    for i, l in enumerate(legs, 1):
        when = l["kick"].astimezone(EASTERN).strftime("%-I:%M %p") if isinstance(l["kick"], dt.datetime) else ""
        tag = "" if l.get("qualified") else " · no edge"
        lines.append(f"{i}) {label(l)} {fmt_odds(l['odds'])} · {l['win']:.0f}% · {l['away']} @ {l['home']} · {when}{tag}")
    if v["approved"]:
        lines.append(f"APPROVED · {units:g}u. Place it at DK, then run the same command with --place so it is logged."
                     + (" This is the fun one: expect it to lose most nights." if rules.get("kind") == "lotto" else ""))
    else:
        lines.append("BLOCKED · " + " · ".join(v["reasons"]))
        lines.append("No ticket. A blocked parlay is not a bet you place anyway.")
    lines.append(f"Rules: ≤{rules['max_legs']} legs · every leg ≥{rules['min_leg_win']:g}% · hits ≥{rules['min_hit']:g}% · "
                 f"≤{rules['max_units']:g}u · {rules['max_per_day']} a day · different games.")
    return "\n".join(lines)


def cmd_parlay(args) -> int:
    t = now_et()
    c = config()
    rules, learned = parlay_rules(c, lotto=getattr(args, "lotto", False)), learned_rules()
    day = dt.date.fromisoformat(args.date) if args.date else t.date()
    units = args.units if args.units is not None else rules["max_units"]
    if args.pick:
        if not LAST_BOARD.exists():
            print("no card on file; run `football.py daily` first")
            return 1
        rows = json.loads(LAST_BOARD.read_text(encoding="utf-8"))["rows"]
        try:
            idx = [int(x) for x in args.pick.split(",")]
            legs = [{**rows[i - 1], "kick": dt.datetime.fromisoformat(rows[i - 1]["kick"])} for i in idx]
        except (ValueError, IndexError):
            print(f"--pick wants row numbers 1..{len(rows)} from the last card")
            return 1
    else:
        start = max(t, dt.datetime.combine(day, dt.time(0), EASTERN))
        end = dt.datetime.combine(day, dt.time(23, 59, 59), EASTERN)
        markets = {m.strip() for m in c.get("DAILY_MARKETS", "h2h,totals,player").split(",") if m.strip()}
        games = []
        if SNAPSHOTS.exists() or FREE_LATEST.exists():
            games, _ = build_candidates(load_all_events(), league=args.league, t=t, start=start, end=end, c=c, rules=learned)
            games = [g for g in games if g["market"] in markets]
        props = build_prop_candidates(t, start, end, league=args.league, c=c, rules=learned) if "player" in markets else []
        legs = build_parlay(games + props, legs=args.legs if args.legs != 3 or rules["kind"] != "lotto" else rules["max_legs"],
                            min_leg_win=rules["min_leg_win"], juicy=rules["kind"] == "lotto")
    v = parlay_verdict(legs, rules=rules, units=units, placed_today=parlays_placed_today(t, rules["kind"]))
    print(render_parlay(legs, v, day=day, units=units, rules=rules))
    if not args.place:
        return 0 if v["approved"] else 2
    if not v["approved"]:
        print("not logged: the gate did not approve this ticket")
        return 2
    s = staking()
    bet = {"placed_at_et": t.isoformat(), "game_date": day.isoformat(),
           "league": "/".join(sorted({l["league"] for l in legs})),
           "matchup": " | ".join(f"{l['away']} @ {l['home']}" for l in legs), "market": rules["kind"],
           "selection": " | ".join(label(l) for l in legs), "odds_american": f"{v['american']:.0f}", "book": args.book,
           "stake_units": f"{units:g}", "stake_dollars": f"{units * s['unit_size']:.2f}",
           "fan_team": "YES" if any(l.get("fan") for l in legs) else "NO",
           "edge_note": f"parlay gate approved; hits {v['hit_pct']:.0f}%; fair {fmt_odds(v['fair_american'])}; "
                        + "; ".join(f"{label(l)} {l['win']:.0f}%" for l in legs),
           "result": "", "net_units": "", "net_dollars": "", "bankroll_after": ""}
    with BET_LEDGER.open(encoding="utf-8") as f:
        cols = list(csv.DictReader(f).fieldnames or list(bet))
    with BET_LEDGER.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=cols, lineterminator="\n", extrasaction="ignore").writerow(bet)
    book = json.loads(PARLAYS.read_text(encoding="utf-8")) if PARLAYS.exists() else {}
    book[bet["placed_at_et"]] = [{"event_id": l["event_id"], "game_date": l["kick"].astimezone(EASTERN).date().isoformat(),
                                  "market": l["market"], "selection": l["selection"],
                                  "line_points": f"{l['line']:g}" if l.get("line") is not None else "",
                                  "odds_american": f"{l['odds']:.0f}", "label": label(l)} for l in legs]
    PARLAYS.write_text(json.dumps(book, indent=1), encoding="utf-8")
    log_board(legs, t)   # legs become ledger rows, so settle grades them and the parlay resolves from them
    print(f"logged: {len(legs)}-leg {rules['kind']} {fmt_odds(v['american'])} · {units:g}u (${units * s['unit_size']:.2f}) at {args.book}")
    log_run(rules["kind"], [f"{len(legs)} legs {fmt_odds(v['american'])} hits {v['hit_pct']:.0f}% · {units:g}u at {args.book} · " + bet["selection"]])
    return 0


def settle_parlay(b: dict, by_key: dict, book: dict) -> tuple[str, float | None]:
    """Resolve a parlay row from its legs' ledger grades. Any loss is a loss; a
    pushed leg drops out and the payout is recomputed from the legs that stand;
    an ungraded leg leaves the ticket pending. Returns (result, odds or None)."""
    legs = book.get(b.get("placed_at_et") or "", [])
    if not legs:
        return "", None
    standing = []
    for leg in legs:
        res = by_key.get(settlement_key(leg), set())
        if len(res) != 1:
            return "", None
        r = next(iter(res))
        if r == "loss":
            return "loss", None
        if r == "win":
            standing.append(leg)
    if not standing:
        return "push", None
    dec = 1.0
    for leg in standing:
        dec *= american_to_decimal(float(leg["odds_american"]))
    return "win", (float(decimal_to_american(dec)) if len(standing) < len(legs) else None)


# ---------------------------------------------------------------------------
# status / week
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    t = now_et()
    last = {}
    if SNAPSHOTS.exists():
        with SNAPSHOTS.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                sp = r.get("sport")
                if sp in LEAGUE_LABEL and r["captured_at_et"] > last.get(sp, ""):
                    last[sp] = r["captured_at_et"]
    for sp, cap in sorted(last.items()):
        age = (t - dt.datetime.fromisoformat(cap)).total_seconds() / 3600
        print(f"odds api {LEAGUE_LABEL[sp]}: {cap[:16]} ({age:.1f}h old{' — STALE' if age > MAX_ODDS_AGE_H else ''})")
    if FREE_LATEST.exists():
        free = read_csv(FREE_LATEST)
        if free:
            cap = free[0]["captured_at_et"]
            age = (t - dt.datetime.fromisoformat(cap)).total_seconds() / 3600
            books = sorted({r["book"] for r in free})
            print(f"free feed: {cap[:16]} ({age:.1f}h old) · {len(free)} rows · {', '.join(books)}")
    else:
        print("free feed: never pulled (fetch-free-lines.py)")
    if QUOTA.exists():
        q = json.loads(QUOTA.read_text(encoding="utf-8"))
        print(f"Odds API: {q.get('requests_remaining')} credits left (a pull costs {q.get('last_request_cost')})")
    ledger = read_csv(LEDGER)
    tracked = [r for r in ledger if (r.get("decision") or "").upper() in TRACKED]
    new = [r for r in tracked if r.get("source_report") == SOURCE]
    for name, rows in (("all", tracked), ("football.py", new)):
        s = slice_stats(rows)
        beat = f"beat {s['beat']*100:.0f}%" if s["beat"] is not None else "beat —"
        print(f"ledger {name}: {s['w']}-{s['l']}-{s['p']} · flat {s['roi_u']:+.2f}u · {beat} · "
              f"{s['n'] - s['graded']} open")
    bets = read_csv(BET_LEDGER)
    if bets:
        net = sum(float(b.get("net_units") or 0) for b in bets)
        print(f"real wagers: {len(bets)} · net {net:+.2f}u · last bankroll ${bets[-1].get('bankroll_after') or '—'}")
    else:
        print("real wagers: none logged (use `football.py bet N --units U`)")
    rules = learned_rules()
    print("blocked slices: " + (", ".join(f"{b['league']} {b['market']}" for b in rules["blocked"]) or "none")
          + (f" (learn ran {rules['generated'][:16]})" if rules.get("generated") else " (learn never ran)"))
    open_rows = [r for r in new if not r.get("result")]
    if open_rows:
        u = sum(int(float(r.get("units") or 0)) for r in open_rows if r.get("decision") == "PICK")
        print(f"open: {len(open_rows)} row(s), {u}u at risk")
    return 0


def cmd_week(args) -> int:
    print("── data")
    cmd_data(args)
    print("── settle")
    cmd_settle(args)
    print("── learn")
    cmd_learn(args)
    for lg in ("nfl", "ncaaf", "nhl"):
        print(f"── board {lg}")
        ns = argparse.Namespace(league=lg, date=None, hours=args.hours, count=args.count,
                                no_log=args.no_log, rank=args.rank)
        cmd_board(ns)
    print("── check")
    cmd_check(args)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the one football agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("data")
    b = sub.add_parser("board")
    b.add_argument("--league", choices=["nfl", "ncaaf", "nhl", "all"], default="all")
    b.add_argument("--date", help="YYYY-MM-DD (ET game date)")
    b.add_argument("--hours", type=int, default=168)
    b.add_argument("--count", type=int, default=10)
    b.add_argument("--no-log", action="store_true")
    b.add_argument("--rank", choices=["win", "edge"], help="default RANK_BY in config.env (win)")
    sub.add_parser("check")
    sub.add_parser("settle")
    sub.add_parser("learn")
    bt = sub.add_parser("bet")
    bt.add_argument("row", type=int)
    bt.add_argument("--units", type=float, required=True)
    bt.add_argument("--odds", type=float)
    bt.add_argument("--book", default="draftkings")
    w = sub.add_parser("week")
    w.add_argument("--hours", type=int, default=168)
    w.add_argument("--count", type=int, default=10)
    w.add_argument("--no-log", action="store_true")
    w.add_argument("--rank", choices=["win", "edge"])
    d = sub.add_parser("daily", help="today's DAILY_PICKS across NFL, CFB and NHL: ML, total or player prop, win-first")
    d.add_argument("--league", choices=["nfl", "ncaaf", "nhl", "all"], default="all")
    d.add_argument("--date", help="YYYY-MM-DD (ET card date); default today")
    d.add_argument("--count", type=int, help="override DAILY_PICKS from config.env")
    d.add_argument("--shape", choices=["slots", "top"], help="slots: per league ML + O/U + prop (default); top: best N across leagues")
    d.add_argument("--no-log", action="store_true")
    d.add_argument("--json", action="store_true")
    pl = sub.add_parser("parlay", help="build today's parlay through the gate; --place logs it once APPROVED")
    pl.add_argument("--legs", type=int, default=3)
    pl.add_argument("--pick", help="row numbers from the last daily card, e.g. 1,3 (your own legs, same gate)")
    pl.add_argument("--units", type=float, help="default PARLAY_MAX_UNITS")
    pl.add_argument("--book", default="draftkings")
    pl.add_argument("--league", choices=["nfl", "ncaaf", "nhl", "all"], default="all")
    pl.add_argument("--date", help="YYYY-MM-DD (ET); default today")
    pl.add_argument("--place", action="store_true", help="log the ticket to bet-ledger.csv (refused unless APPROVED)")
    pl.add_argument("--lotto", action="store_true", help="the one fun ticket per slate: up to LOTTO_MAX_LEGS, LOTTO_MAX_UNITS (0.25u), same gate otherwise")
    args = ap.parse_args(argv)
    return {"status": cmd_status, "data": cmd_data, "board": cmd_board, "check": cmd_check,
            "settle": cmd_settle, "learn": cmd_learn, "bet": cmd_bet, "week": cmd_week,
            "daily": cmd_daily, "parlay": cmd_parlay}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
