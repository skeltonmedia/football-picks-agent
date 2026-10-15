#!/usr/bin/env python3
"""edge.py — the one-question football engine (AQ-271, 2026-10-02).

Question: when DraftKings posts a number better than Pinnacle's, does that
number beat the Pinnacle close? Nothing else is modeled. No totals, no
confidence score, no ratings.

  edge.py flag   [--league ncaaf|nfl|all] [--days N]  read the free feed, log new flags
  edge.py close                                       fill the Pinnacle close for kicked-off flags
  edge.py settle                                      grade W/L/P from final scores
  edge.py status                                      beat-close %, paper units, real units

Inputs: line-watch/free-lines-latest.csv (Pinnacle + DraftKings, every 30 min),
line-watch/snapshots.csv (closes), settle-picks.py score feeds.
Output: edge-ledger.csv (append-only; one row per event, first number wins).
Thresholds live in config.env, never here.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
LATEST = AGENT / "line-watch/free-lines-latest.csv"
SNAPSHOTS = AGENT / "line-watch/snapshots.csv"
LEDGER = AGENT / "edge-ledger.csv"
ET = ZoneInfo("America/New_York")

FIELDS = ["flag_date", "league", "event_id", "matchup", "kickoff_et", "market", "selection",
          "dk_points", "dk_odds", "pin_points", "pin_odds", "pin_fair_odds",
          "edge_points", "edge_ev_pct", "units", "captured_at_et",
          "close_pin_points", "close_pin_odds", "close_pin_fair_odds", "close_captured_at_et",
          "clv_points", "clv_ev_pct", "beat_close", "away_score", "home_score", "result",
          "paper_units", "real_units"]


def cfg(key: str, default: str) -> str:
    for name in ("config.env", "config.example.env"):
        p = AGENT / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return default


MIN_SPREAD_POINTS = float(cfg("EDGE_MIN_SPREAD_POINTS", "1.0"))
SAME_POINT_CENTS = float(cfg("EDGE_SAME_POINT_CENTS", "20"))
MIN_ML_EV_PCT = float(cfg("EDGE_MIN_ML_EV_PCT", "2.5"))
ML_CEILING = float(cfg("EDGE_ML_CEILING", "-250"))
ML_DOG_CEILING = float(cfg("EDGE_ML_DOG_CEILING", "250"))
MAX_PRICE_GIVEBACK_CENTS = float(cfg("EDGE_MAX_PRICE_GIVEBACK_CENTS", "10"))
PAPER_FLAGS = int(cfg("EDGE_PAPER_FLAGS", "100"))
FOOTBALL = {"nfl", "ncaaf"}
PAPER_GATE_PCT = float(cfg("EDGE_PAPER_GATE_PCT", "54"))


def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def ts(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def num(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def dec(odds: float) -> float:
    return 1 + odds / 100 if odds > 0 else 1 + 100 / -odds


def implied(odds: float) -> float:
    return 1 / dec(odds)


def to_american(p: float) -> float:
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def cents(odds: float) -> float:
    return odds - 100 if odds > 0 else odds + 100


def no_vig(odds_a: float, odds_b: float) -> tuple[float, float]:
    a, b = implied(odds_a), implied(odds_b)
    return a / (a + b), b / (a + b)


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    with LEDGER.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_ledger(rows: list[dict]) -> None:
    with LEDGER.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def pairs(rows: list[dict]) -> dict:
    """(event, market) -> book -> selection -> row, from one feed pull."""
    out: dict = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        if r["market"] not in ("spreads", "h2h"):
            continue
        out[(r["event_id"], r["market"])][r["book"]][r["selection"]] = r
    return out


def spread_edge(dk: dict, pin: dict) -> dict | None:
    """DK number on `dk['selection']` vs Pinnacle's number on the same side."""
    dp, do = num(dk["line_points"]), num(dk["odds_american"])
    pp, po = num(pin["line_points"]), num(pin["odds_american"])
    if None in (dp, do, pp, po):
        return None
    pts = dp - pp  # more points on your side is better
    if pts >= MIN_SPREAD_POINTS and cents(po) - cents(do) <= MAX_PRICE_GIVEBACK_CENTS:
        return {"edge_points": pts, "edge_ev_pct": ""}
    if pts == 0 and cents(do) - cents(po) >= SAME_POINT_CENTS:
        return {"edge_points": 0.0, "edge_ev_pct": "", "same_point_cents": cents(do) - cents(po)}
    return None


def ml_edge(dk: dict, pin_side: dict, pin_other: dict) -> dict | None:
    do, po, pq = num(dk["odds_american"]), num(pin_side["odds_american"]), num(pin_other["odds_american"])
    # longshots excluded: no-vig math overstates big dogs (favorite-longshot bias)
    if None in (do, po, pq) or do < ML_CEILING or do > ML_DOG_CEILING:
        return None
    p, _ = no_vig(po, pq)
    ev = (p * dec(do) - 1) * 100
    if ev >= MIN_ML_EV_PCT:
        return {"edge_points": "", "edge_ev_pct": ev, "pin_fair_odds": to_american(p)}
    return None


def flag(league: str, days: int) -> int:
    if not LATEST.exists():
        print("no free feed yet — load com.davidos.football-free-lines")
        return 1
    with LATEST.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    now = now_et()
    age_h = (now - ts(rows[0]["captured_at_et"])).total_seconds() / 3600 if rows else 99
    print(f"free feed {rows[0]['captured_at_et'] if rows else '—'} ({age_h:.1f}h old) · {len(rows)} rows")
    if age_h > 2:
        print("feed is stale (>2h); run fetch-free-lines.py first")
        return 1
    horizon = now + dt.timedelta(days=days)
    ledger = read_ledger()
    seen_events = {r["event_id"] for r in ledger}
    candidates: dict[str, dict] = {}
    for (eid, market), books in pairs(rows).items():
        dk, pin = books.get("draftkings"), books.get("pinnacle")
        if not dk or not pin or eid in seen_events:
            continue
        sample = next(iter(dk.values()))
        if league != "all" and sample["sport"] != league:
            continue
        # 2026-10-07: the free feed now carries NHL. The spread rule is a
        # football points-to-cents question; hockey only joins the engine when
        # asked for explicitly, and then moneyline only (puck lines skipped).
        if league == "all" and sample["sport"] not in FOOTBALL:
            continue
        if sample["sport"] == "nhl" and market == "spreads":
            continue
        kick = ts(sample["commence_time_et"])
        if not kick or not (now < kick <= horizon):
            continue
        for sel, drow in dk.items():
            prow = pin.get(sel)
            other = [v for k, v in pin.items() if k != sel]
            if not prow or len(other) != 1:
                continue
            e = spread_edge(drow, prow) if market == "spreads" else ml_edge(drow, prow, other[0])
            if not e:
                continue
            row = {
                "flag_date": now.date().isoformat(), "league": sample["sport"].upper(),
                "event_id": eid, "matchup": f"{sample['away_team']} @ {sample['home_team']}",
                "kickoff_et": sample["commence_time_et"], "market": market, "selection": sel,
                "dk_points": drow["line_points"], "dk_odds": drow["odds_american"],
                "pin_points": prow["line_points"], "pin_odds": prow["odds_american"],
                "pin_fair_odds": e.get("pin_fair_odds", ""),
                "edge_points": f"{e['edge_points']:+.1f}" if e["edge_points"] != "" else "",
                "edge_ev_pct": f"{e['edge_ev_pct']:+.1f}" if e["edge_ev_pct"] != "" else "",
                "captured_at_et": drow["captured_at_et"],
            }
            # one row per game: a spread flag outranks a moneyline flag
            cur = candidates.get(eid)
            if cur is None or (market == "spreads" and cur["market"] == "h2h"):
                candidates[eid] = row
    paper = len(ledger) < PAPER_FLAGS
    new = sorted(candidates.values(), key=lambda r: r["kickoff_et"])
    (AGENT / "line-watch/edge-last-run.txt").write_text(f"{now.isoformat()} flags={len(new)}\n", encoding="utf-8")
    for r in new:
        r["units"] = "0" if paper else "1"
        r["real_units"] = "0"
    write_ledger(ledger + new)
    print(f"{len(new)} new flag(s) · ledger {len(ledger) + len(new)} · "
          f"{'paper (0u) until ' + str(PAPER_FLAGS) + ' flags' if paper else 'live 1u'}\n")
    for i, r in enumerate(new, 1):
        k = ts(r["kickoff_et"])
        line = f"{r['selection']} {r['dk_points']} ({r['dk_odds']})" if r["market"] == "spreads" \
            else f"{r['selection']} ML ({r['dk_odds']})"
        pin = f"Pin {r['pin_points']} ({r['pin_odds']})" if r["market"] == "spreads" \
            else f"Pin fair {r['pin_fair_odds']}"
        edge = f"{r['edge_points']} pt" if r["edge_points"] else f"{r['edge_ev_pct']}% EV"
        print(f"{i}. {r['league']} {k:%a %-I:%M%p} · {r['matchup']}\n   DK {line} · {pin} · edge {edge} · {r['units']}u")
    return 0


def close() -> int:
    ledger = read_ledger()
    now = now_et()
    todo = [r for r in ledger if not r.get("close_pin_odds") and ts(r["kickoff_et"]) and ts(r["kickoff_et"]) <= now]
    if not todo:
        print("nothing to close")
        return 0
    keys = {(r["event_id"], r["market"]) for r in todo}
    series: dict = defaultdict(lambda: defaultdict(list))
    with SNAPSHOTS.open(encoding="utf-8") as f:
        for s in csv.DictReader(f):
            if s["book"] != "pinnacle" or (s["event_id"], s["market"]) not in keys:
                continue
            cap, kick = ts(s["captured_at_et"]), ts(s["commence_time_et"])
            if cap and kick and cap <= kick:
                series[(s["event_id"], s["market"])][s["selection"]].append((cap, s))
    done = 0
    for r in todo:
        sides = series.get((r["event_id"], r["market"]), {})
        mine = sorted(sides.get(r["selection"], []))
        if not mine:
            continue
        cap, c = mine[-1]
        r["close_pin_points"], r["close_pin_odds"], r["close_captured_at_et"] = c["line_points"], c["odds_american"], c["captured_at_et"]
        took_odds, close_odds = num(r["dk_odds"]), num(c["odds_american"])
        if r["market"] == "spreads":
            pts = num(r["dk_points"]) - num(c["line_points"])
            r["clv_points"] = f"{pts:+.1f}"
            signal = pts if pts != 0 else cents(took_odds) - cents(close_odds)
        else:
            others = [v for k, v in sides.items() if k != r["selection"]]
            if not others:
                continue
            ocap, o = sorted(others[0])[-1]
            p, _ = no_vig(close_odds, num(o["odds_american"]))
            r["close_pin_fair_odds"] = to_american(p)
            ev = (p * dec(took_odds) - 1) * 100
            r["clv_ev_pct"] = f"{ev:+.1f}"
            signal = ev
        r["beat_close"] = "1" if signal > 0 else "0" if signal < 0 else "tie"
        done += 1
    write_ledger(ledger)
    print(f"closed {done} of {len(todo)}")
    return 0


def settle() -> int:
    spec = importlib.util.spec_from_file_location("settle_picks", AGENT / "settle-picks.py")
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    year = now_et().year
    scores = {"NCAAF": sp.cfb_scores(year), "NFL": sp.nfl_scores(year), "NHL": sp.nhl_scores()}
    ledger = read_ledger()
    done = 0
    for r in ledger:
        if r.get("result"):
            continue
        away, home = r["matchup"].split(" @ ", 1)
        got = sp.lookup(scores.get(r["league"], {}), away, home)
        if not got:
            continue
        ap, hp = got
        picked_home = sp.picked_home(r["selection"], away, home)
        if picked_home is None:
            continue
        mine, theirs = (hp, ap) if picked_home else (ap, hp)
        margin = mine - theirs + (num(r["dk_points"]) or 0.0 if r["market"] == "spreads" else 0.0)
        res = "W" if margin > 0 else "L" if margin < 0 else "P"
        odds = num(r["dk_odds"])
        paper = (dec(odds) - 1) if res == "W" else -1.0 if res == "L" else 0.0
        real = num(r.get("real_units")) or 0.0
        r.update({"away_score": ap, "home_score": hp, "result": res,
                  "paper_units": f"{paper:+.2f}", "real_units": f"{real * paper:+.2f}" if real else r.get("real_units", "0")})
        done += 1
    write_ledger(ledger)
    print(f"settled {done}")
    return 0


def status() -> int:
    ledger = read_ledger()
    closed = [r for r in ledger if r.get("beat_close") in ("1", "0")]
    beat = sum(r["beat_close"] == "1" for r in closed)
    graded = [r for r in ledger if r.get("result") in ("W", "L", "P")]
    w = sum(r["result"] == "W" for r in graded)
    l = sum(r["result"] == "L" for r in graded)
    paper = sum(num(r.get("paper_units")) or 0 for r in graded)
    real = sum(num(r.get("real_units")) or 0 for r in graded)
    pct = 100 * beat / len(closed) if closed else 0
    print(f"flags {len(ledger)} · open {len(ledger) - len(graded)}")
    print(f"beat close {beat}-{len(closed) - beat} ({pct:.0f}%) · gate {PAPER_GATE_PCT:.0f}% on {PAPER_FLAGS}+ flags")
    print(f"paper 1u flat {w}-{l} · {paper:+.2f}u")
    print(f"real units {real:+.2f}u")
    if len(ledger) >= PAPER_FLAGS:
        print("staking: " + ("LIVE 1u (gate passed)" if pct >= PAPER_GATE_PCT else "0u (gate failed — no edge)"))
    else:
        print(f"staking: 0u paper until {PAPER_FLAGS} flags")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("flag")
    f.add_argument("--league", default="all", choices=["all", "ncaaf", "nfl", "nhl"])
    f.add_argument("--days", type=int, default=4)
    sub.add_parser("close")
    sub.add_parser("settle")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "flag":
        return flag(a.league, a.days)
    return {"close": close, "settle": settle, "status": status}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
