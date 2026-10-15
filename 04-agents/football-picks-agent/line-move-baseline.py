#!/usr/bin/env python3
"""line-move-baseline.py — how much do lines actually move? (AQ-168 step 1)

    python3 -B 04-agents/football-picks-agent/line-move-baseline.py [--snapshots PATH] [--out PATH]

READ-ONLY. Reads line-watch/snapshots.csv, writes one markdown report to
05-outputs/reports/football-picks-agent/line-move-baseline-<today>.md and
prints a short summary. No network, no config, stdlib only.

WHY. "This line moved abnormally" is an impression until there is a
distribution to compare against. This script builds that distribution from
the captures we already have, so spot-flags (RLM) and any future steam
signal can be judged against a baseline instead of a feeling.

DEFINITIONS
  value       spreads/totals -> line_points; h2h -> implied probability of
              the American price, in percentage points (so a 0.5/1/2 "point"
              threshold on h2h means 0.5/1/2 probability points).
  one side    each game contributes one series per market: the away team's
              spread / h2h price, and the Over for totals (the other side is
              the mirror image and would double count).
  consensus   median across books at a capture timestamp.
  open/close  first capture and last capture strictly before kickoff.
  move        close - open (signed); tables report |move|.
  hours bucket  hours from capture to kickoff: >72h, 24-72h, 6-24h, <6h.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import pathlib
import statistics
import sys
from collections import defaultdict

AGENT = pathlib.Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
REPORT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"

THRESHOLDS = (0.5, 1.0, 2.0)
BUCKETS = (">72h", "24-72h", "6-24h", "<6h")
DISAGREE_POINTS = 1.0


# ---------------------------------------------------------------- parsing

def parse_ts(s: str) -> datetime.datetime | None:
    """ISO-8601 with offset or trailing Z -> aware datetime (py3.9 safe)."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def implied_prob(odds: float) -> float:
    """American price -> implied probability in percentage points."""
    if odds > 0:
        return 100.0 * 100.0 / (odds + 100.0)
    return 100.0 * (-odds) / ((-odds) + 100.0)


def hours_bucket(hours: float) -> str:
    if hours > 72:
        return ">72h"
    if hours > 24:
        return "24-72h"
    if hours > 6:
        return "6-24h"
    return "<6h"


def row_value(row: dict) -> float | None:
    """The tracked number for a row: points for spreads/totals, prob for h2h."""
    market = row.get("market", "")
    try:
        if market == "h2h":
            return implied_prob(float(row["odds_american"]))
        return float(row["line_points"])
    except (KeyError, TypeError, ValueError):
        return None


def one_side(row: dict) -> bool:
    """Keep one selection per game per market (away side; Over for totals)."""
    market = row.get("market", "")
    sel = row.get("selection", "")
    if market == "totals":
        return sel.lower() == "over"
    return sel == row.get("away_team", "")


def parse_snapshots(path: pathlib.Path) -> tuple[dict, dict]:
    """Return (series, events).

    series[(sport, event_id, market, selection, book)] = sorted
        [(captured_at, value)] for pre-kickoff captures only.
    events[event_id] = {"sport", "kickoff", "away", "home"}.
    """
    series: dict = defaultdict(list)
    events: dict = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            cap = parse_ts(row.get("captured_at_et", ""))
            kick = parse_ts(row.get("commence_time_et", ""))
            if cap is None or kick is None or cap >= kick:
                continue
            val = row_value(row)
            if val is None:
                continue
            key = (row["sport"], row["event_id"], row["market"], row["selection"], row["book"])
            series[key].append((cap, val))
            events.setdefault(row["event_id"], {
                "sport": row["sport"], "kickoff": kick,
                "away": row.get("away_team", ""), "home": row.get("home_team", ""),
            })
    for v in series.values():
        v.sort()
    return series, events


def split_completed(series: dict, events: dict, as_of: datetime.datetime | None = None) -> tuple[dict, dict, dict]:
    """Keep only events that have kicked off by `as_of` (default: now).
    Returns (series, events, pending_events). A pending game's latest capture
    is not a close, so it must not enter move stats."""
    if as_of is None:
        as_of = datetime.datetime.now(datetime.timezone.utc)
    done = {eid: ev for eid, ev in events.items() if ev["kickoff"] <= as_of}
    pending = {eid: ev for eid, ev in events.items() if eid not in done}
    kept = {k: v for k, v in series.items() if k[1] in done}
    return kept, done, pending


# ---------------------------------------------------------------- analysis

def consensus_series(series: dict, events: dict) -> dict:
    """(sport, event_id, market, selection) -> sorted [(captured_at, median value)]
    restricted to one side per game per market."""
    by_capture: dict = defaultdict(lambda: defaultdict(list))
    for (sport, eid, market, sel, book), rows in series.items():
        ev = events[eid]
        fake = {"market": market, "selection": sel, "away_team": ev["away"]}
        if not one_side(fake):
            continue
        for cap, val in rows:
            by_capture[(sport, eid, market, sel)][cap].append(val)
    out = {}
    for key, caps in by_capture.items():
        out[key] = sorted((cap, statistics.median(vals)) for cap, vals in caps.items())
    return out


def percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def describe(vals: list) -> dict:
    """Distribution summary of absolute moves."""
    a = sorted(abs(v) for v in vals)
    n = len(a)
    d = {"n": n}
    if n == 0:
        return d
    d.update({
        "mean": statistics.mean(a), "median": statistics.median(a),
        "p10": percentile(a, 0.10), "p90": percentile(a, 0.90),
    })
    for t in THRESHOLDS:
        d[f"ge{t}"] = sum(1 for v in a if v >= t) / n
    return d


def open_to_close_moves(cons: dict, events: dict) -> list:
    """One record per (sport, market, event): signed close-open, capture count, hours span."""
    out = []
    for (sport, eid, market, sel), rows in cons.items():
        if len(rows) < 2:
            continue
        kick = events[eid]["kickoff"]
        out.append({
            "sport": sport, "market": market, "event_id": eid,
            "move": rows[-1][1] - rows[0][1], "captures": len(rows),
            "open_hours": (kick - rows[0][0]).total_seconds() / 3600,
            "close_hours": (kick - rows[-1][0]).total_seconds() / 3600,
        })
    return out


def remaining_moves_by_bucket(cons: dict, events: dict) -> dict:
    """bucket -> market -> [close - value_at_capture] for every non-final capture.
    Answers: seen at H hours out, how far does the consensus still move?"""
    out: dict = defaultdict(lambda: defaultdict(list))
    for (sport, eid, market, sel), rows in cons.items():
        if len(rows) < 2:
            continue
        kick = events[eid]["kickoff"]
        close = rows[-1][1]
        for cap, val in rows[:-1]:
            hrs = (kick - cap).total_seconds() / 3600
            out[hours_bucket(hrs)][market].append(close - val)
    return out


def step_moves_by_bucket(cons: dict, events: dict) -> dict:
    """bucket -> market -> [capture-to-next-capture delta], bucketed at the later capture."""
    out: dict = defaultdict(lambda: defaultdict(list))
    for (sport, eid, market, sel), rows in cons.items():
        kick = events[eid]["kickoff"]
        for (c0, v0), (c1, v1) in zip(rows, rows[1:]):
            hrs = (kick - c1).total_seconds() / 3600
            out[hours_bucket(hrs)][market].append(v1 - v0)
    return out


def per_book(series: dict, events: dict) -> dict:
    """book -> market -> {"series", "moves":[abs open-close], "changes", "steps"}."""
    out: dict = defaultdict(lambda: defaultdict(lambda: {"series": 0, "moves": [], "changes": 0, "steps": 0}))
    for (sport, eid, market, sel, book), rows in series.items():
        ev = events[eid]
        if not one_side({"market": market, "selection": sel, "away_team": ev["away"]}):
            continue
        if len(rows) < 2:
            continue
        cell = out[book][market]
        cell["series"] += 1
        cell["moves"].append(abs(rows[-1][1] - rows[0][1]))
        cell["steps"] += len(rows) - 1
        cell["changes"] += sum(1 for (_, a), (_, b) in zip(rows, rows[1:]) if a != b)
    return out


def closing_disagreement(series: dict, events: dict, threshold: float = DISAGREE_POINTS) -> dict:
    """market -> {"games", "disagree", "spreads": [max-min]} using each book's
    last pre-kickoff value; games with fewer than two books are skipped."""
    last: dict = defaultdict(dict)
    for (sport, eid, market, sel, book), rows in series.items():
        ev = events[eid]
        if not one_side({"market": market, "selection": sel, "away_team": ev["away"]}):
            continue
        last[(sport, eid, market, sel)][book] = rows[-1][1]
    out: dict = defaultdict(lambda: {"games": 0, "disagree": 0, "spreads": []})
    for (sport, eid, market, sel), books in last.items():
        if len(books) < 2:
            continue
        spread = max(books.values()) - min(books.values())
        cell = out[market]
        cell["games"] += 1
        cell["spreads"].append(spread)
        if spread >= threshold:
            cell["disagree"] += 1
    return out


# ---------------------------------------------------------------- report

def fmt(d: dict) -> str:
    if d["n"] == 0:
        return "0 | – | – | – | – | – | – | –"
    return (f"{d['n']} | {d['mean']:.2f} | {d['median']:.2f} | {d['p10']:.2f} | {d['p90']:.2f}"
            f" | {d['ge0.5']:.0%} | {d['ge1.0']:.0%} | {d['ge2.0']:.0%}")


HEAD = "n | mean | median | p10 | p90 | ≥0.5 | ≥1.0 | ≥2.0"
RULE = "---|---|---|---|---|---|---|---"


def build_report(series: dict, events: dict, today: str, src: str, as_of=None) -> str:
    all_events = events
    series, events, pending = split_completed(series, events, as_of)
    cons = consensus_series(series, events)
    moves = open_to_close_moves(cons, events)
    L = []
    L.append(f"# Line-move baseline — {today}")
    L.append("")
    L.append(f"Source: `{src}` · read-only · AQ-168 step 1. "
             "Values: spread/total points; h2h in implied-probability points. "
             "One side per game per market (away / Over). Consensus = median across books "
             "at each capture; open = first capture, close = last pre-kickoff capture. "
             "All tables report |move|.")
    L.append("")
    n_moves = len(moves)
    caps = [m["captures"] for m in moves]
    pend = defaultdict(int)
    for ev in pending.values():
        pend[ev["sport"]] += 1
    done = defaultdict(int)
    for ev in events.values():
        done[ev["sport"]] += 1
    L.append(f"- events in file: {len(all_events)} · kicked off (graded here): {len(events)} "
             f"({', '.join(f'{k} {v}' for k, v in sorted(done.items()))}) · pending, excluded: {len(pending)} "
             f"({', '.join(f'{k} {v}' for k, v in sorted(pend.items()))})")
    L.append(f"- consensus series with ≥2 captures: {n_moves}"
             f" · median captures per series: {statistics.median(caps) if caps else 0:.0f}"
             f" · series at 10+ captures: {sum(1 for c in caps if c >= 10)}")
    L.append("")

    # 1. open-to-close by market, by sport
    L.append("## 1. Open-to-close consensus moves")
    L.append("")
    L.append("### By market")
    L.append("")
    L.append(f"market | {HEAD}")
    L.append("---|" + RULE)
    for market in ("spreads", "totals", "h2h"):
        L.append(f"{market} | {fmt(describe([m['move'] for m in moves if m['market'] == market]))}")
    L.append("")
    L.append("### By sport × market")
    L.append("")
    L.append(f"sport | market | {HEAD}")
    L.append("---|---|" + RULE)
    for sport in sorted({m["sport"] for m in moves}):
        for market in ("spreads", "totals", "h2h"):
            sub = [m["move"] for m in moves if m["sport"] == sport and m["market"] == market]
            L.append(f"{sport} | {market} | {fmt(describe(sub))}")
    L.append("")

    # 2. by hours-to-kickoff
    L.append("## 2. By hours-to-kickoff at capture")
    L.append("")
    L.append("### Remaining move to close (seen at H hours out, how far does consensus still go?)")
    L.append("")
    rem = remaining_moves_by_bucket(cons, events)
    L.append(f"bucket | market | {HEAD}")
    L.append("---|---|" + RULE)
    for b in BUCKETS:
        for market in ("spreads", "totals", "h2h"):
            L.append(f"{b} | {market} | {fmt(describe(rem[b][market]))}")
    L.append("")
    L.append("### Capture-to-next-capture step (bucketed at the later capture)")
    L.append("")
    steps = step_moves_by_bucket(cons, events)
    L.append(f"bucket | market | {HEAD}")
    L.append("---|---|" + RULE)
    for b in BUCKETS:
        for market in ("spreads", "totals", "h2h"):
            L.append(f"{b} | {market} | {fmt(describe(steps[b][market]))}")
    L.append("")
    L.append("### Open-to-close by sport × opening bucket (when the first capture landed)")
    L.append("")
    L.append(f"sport | opened | market | {HEAD}")
    L.append("---|---|---|" + RULE)
    for sport in sorted({m["sport"] for m in moves}):
        for b in BUCKETS:
            for market in ("spreads", "totals", "h2h"):
                sub = [m["move"] for m in moves if m["sport"] == sport and m["market"] == market
                       and hours_bucket(m["open_hours"]) == b]
                if sub:
                    L.append(f"{sport} | {b} | {market} | {fmt(describe(sub))}")
    L.append("")

    # 3. per book
    L.append("## 3. Per-book move frequency (each book's own open→close, one side per game)")
    L.append("")
    pb = per_book(series, events)
    L.append("book | market | series | mean |move| | ≥0.5 | ≥1.0 | ≥2.0 | changes/step")
    L.append("---|---|---|---|---|---|---|---")
    for book in sorted(pb):
        for market in ("spreads", "totals", "h2h"):
            c = pb[book][market]
            if c["series"] == 0:
                continue
            mv = c["moves"]
            n = len(mv)
            L.append(f"{book} | {market} | {n} | {statistics.mean(mv):.2f} | "
                     f"{sum(1 for v in mv if v >= 0.5) / n:.0%} | {sum(1 for v in mv if v >= 1.0) / n:.0%} | "
                     f"{sum(1 for v in mv if v >= 2.0) / n:.0%} | "
                     f"{(c['changes'] / c['steps']) if c['steps'] else 0:.2f}")
    L.append("")

    # 4. disagreement
    L.append(f"## 4. Book disagreement at last pre-kickoff capture (max−min ≥ {DISAGREE_POINTS:g})")
    L.append("")
    dis = closing_disagreement(series, events)
    L.append("market | games (≥2 books) | disagree ≥1.0 | share | median max−min | p90 max−min")
    L.append("---|---|---|---|---|---")
    for market in ("spreads", "totals", "h2h"):
        c = dis[market]
        if c["games"] == 0:
            L.append(f"{market} | 0 | 0 | – | – | –")
            continue
        s = sorted(c["spreads"])
        L.append(f"{market} | {c['games']} | {c['disagree']} | {c['disagree'] / c['games']:.0%} | "
                 f"{statistics.median(s):.2f} | {percentile(s, 0.9):.2f}")
    L.append("")
    L.append("Note: a book's \"last\" value is its own final pre-kickoff row, which can be hours "
             "older than another book's, so part of any gap is staleness rather than a live price "
             "difference. Pinnacle has few rows in this file; its share of these comparisons is small.")
    L.append("")
    L.append("## Caveats")
    L.append("")
    L.append("- Captures are irregular (the collector runs on a schedule, not on every tick), so "
             "\"open\" is the first time we looked, not the market open.")
    L.append("- Post-kickoff rows are dropped; events that have not kicked off yet "
             "are excluded entirely (their latest capture is not a close). Single-capture series are excluded.")
    L.append("- h2h thresholds are in implied-probability points, not cents; 1 point ≈ 4-5 cents near -110.")
    L.append("")
    return "\n".join(L)


def summary_line(series: dict, events: dict, as_of=None) -> str:
    n_all = len(events)
    series, events, pending = split_completed(series, events, as_of)
    cons = consensus_series(series, events)
    moves = open_to_close_moves(cons, events)
    parts = []
    for market in ("spreads", "totals", "h2h"):
        d = describe([m["move"] for m in moves if m["market"] == market])
        if d["n"]:
            parts.append(f"{market}: n={d['n']} median|move|={d['median']:.2f} ≥1.0={d['ge1.0']:.0%}")
    dis = closing_disagreement(series, events)
    sp = dis["spreads"]
    dpart = f"; spread books disagree ≥1: {sp['disagree']}/{sp['games']}" if sp["games"] else ""
    return (f"line-move baseline: {len(events)} kicked-off events of {n_all} · "
            + " · ".join(parts) + dpart)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshots", default=str(SNAPSHOTS))
    ap.add_argument("--out", default=None, help="report path (default: 05-outputs/.../line-move-baseline-<today>.md)")
    ap.add_argument("--today", default=None)
    args = ap.parse_args(argv)

    src = pathlib.Path(args.snapshots)
    if not src.exists():
        print(f"missing {src}", file=sys.stderr)
        return 1
    today = args.today or datetime.date.today().isoformat()
    out = pathlib.Path(args.out) if args.out else REPORT_DIR / f"line-move-baseline-{today}.md"

    series, events = parse_snapshots(src)
    try:
        rel = str(src.relative_to(ROOT))
    except ValueError:
        rel = str(src)
    text = build_report(series, events, today, rel)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(summary_line(series, events))
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
