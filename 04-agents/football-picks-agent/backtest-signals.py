#!/usr/bin/env python3
"""backtest-signals.py — which market signal predicts closing-line value?

    python3 -B 04-agents/football-picks-agent/backtest-signals.py            # report + md
    python3 -B 04-agents/football-picks-agent/backtest-signals.py --no-write # print only

WHY (2026-09-18, AQ-220). The board ranks by a hand-made 0-100 confidence
score whose correlation with CLV is 0.05 at 45 graded decisions: ordering
noise. Before replacing it, test candidate market signals (DK vs consensus,
DK vs Pinnacle, DK vs best book, move since open, book disagreement) on our
own line log, where every (event, market, selection) at the Tuesday capture
is an observation and its CLV to the close is the outcome. Thousands of
rows instead of 45. This changes no selection; it produces evidence.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import importlib.util
import math
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402

SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
CFBD_CLOSES = AGENT / "data" / "cfb-closing-lines.csv"
LEDGER = AGENT / "recommendation-ledger.csv"
OUT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"

SPORTS = {"ncaaf": "cfb", "nfl": "nfl", "nhl": "nhl"}   # nhl added 2026-10-07 (hockey sources)
DK = "draftkings"
PINNACLE = "pinnacle"
MARKETS = ("h2h", "spreads", "totals")
TRACKED_DECISIONS = {"PICK", "LEAN"}
TUESDAY = 1                      # datetime.weekday()
TUE_WINDOW = (timedelta(days=3), timedelta(days=8))
FALLBACK_WINDOW = timedelta(days=7)
FLOOR_MIN_N = 100                # edge-floor search needs this many rows
LADDER = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)   # fixed thresholds shown beside the floor search
LEAGUE_MIN_N = 30                # per-league tables below this are omitted
QUINTILES = 5

SIGNALS = (
    ("dk_vs_consensus_cents", "DK vs consensus of other books (% of implied prob)"),
    ("dk_vs_consensus_points", "DK vs consensus, raw points (spreads/totals)"),
    ("dk_vs_pinnacle_cents", "DK vs Pinnacle (% of implied prob)"),
    ("dk_vs_pinnacle_points", "DK vs Pinnacle, raw points (spreads/totals)"),
    ("dk_vs_best_cents", "DK vs best other book (% of implied prob)"),
    ("move_since_open_cents", "DK move since open toward selection (% of implied prob)"),
    ("move_since_open_points", "DK move since open toward selection, raw points"),
    ("book_disagreement_points", "Book disagreement, max-min (points, or % prob for h2h)"),
)


# ---------------------------------------------------------------------------
# Conventions from grade-clv.py (imported so the two scripts cannot drift)
# ---------------------------------------------------------------------------

def _load_grade_clv():
    spec = importlib.util.spec_from_file_location("grade_clv", AGENT / "grade-clv.py")
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - fall back to the copied constants below
        return None
    return mod


_G = _load_grade_clv()
if _G is not None:
    implied = _G.implied
    points_in_favor = _G.points_in_favor
    point_value_pct = _G.point_value_pct
    POINT_VALUE_PCT = _G.POINT_VALUE_PCT
    _norm = _G._norm
    cfbd_lookup = _G.cfbd_lookup
    CONVENTIONS = "imported from grade-clv.py"
else:  # copied fallback; keep identical to grade-clv.py
    CONVENTIONS = "FALLBACK copies (grade-clv.py import failed)"
    # Hockey points are worth 0 here on purpose: a puck line moving 1.5 -> 2.5 or
    # a total 5.5 -> 6.5 is not a linear cents conversion, so NHL numbers only
    # ever compare at the same line (football.py already sizes same-line only).
    POINT_VALUE_PCT = {("spreads", "nfl"): 4.0, ("spreads", "cfb"): 3.0,
                       ("totals", "nfl"): 3.5, ("totals", "cfb"): 2.5,
                       ("spreads", "nhl"): 0.0, ("totals", "nhl"): 0.0}

    def implied(odds: float) -> float:
        return 100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)

    def point_value_pct(market: str, league: str) -> float:
        lg = (league or "").lower()
        lg = "nfl" if "nfl" in lg else "nhl" if "nhl" in lg else "cfb"
        return POINT_VALUE_PCT.get((market, lg), 3.0)

    def points_in_favor(market, selection, took_pts, close_pts):
        if market == "totals":
            if (selection or "").strip().lower().startswith("over"):
                return close_pts - took_pts
            return took_pts - close_pts
        return took_pts - close_pts

    def _norm(s: str) -> str:
        import unicodedata as _ud
        s = _ud.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
        return "".join(c for c in s.lower() if c.isalnum())

    def cfbd_lookup(closes, away, home):
        a, h = _norm(away), _norm(home)
        if (a, h) in closes:
            return closes[(a, h)]
        hits = [v for (sa, sh), v in closes.items()
                if sa and sh and a.startswith(sa) and h.startswith(sh)]
        return hits[0] if len(hits) == 1 else None


def juice_pct(took_odds: float, ref_odds_implied: float) -> float:
    """Percent change in implied probability from the price taken to a
    reference price. Positive = the reference is a worse price than we got."""
    t = implied(took_odds)
    return (ref_odds_implied - t) / t * 100.0


def edge_pct(market: str, selection: str, league: str,
             took_odds: float, took_pts, ref_impl: float, ref_pts) -> tuple[float, float | None]:
    """Bettor's-seat value of (took_odds, took_pts) against a reference price
    (ref_impl, ref_pts): the CLV formula with the reference in the close's seat.
    Returns (percent, points_or_None)."""
    j = juice_pct(took_odds, ref_impl)
    if market == "h2h" or took_pts is None or ref_pts is None:
        return j, None
    pts = points_in_favor(market, selection, took_pts, ref_pts)
    return j + pts * point_value_pct(market, league), pts


# ---------------------------------------------------------------------------
# Snapshot log -> per-event price series
# ---------------------------------------------------------------------------

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Series:
    """Change-only price history for one (book, market, selection)."""
    __slots__ = ("caps", "rows")

    def __init__(self):
        self.caps: list[datetime] = []
        self.rows: list[tuple[float | None, float]] = []   # (line_points, odds)

    def add(self, cap: datetime, pts, odds: float):
        i = bisect.bisect_right(self.caps, cap)
        self.caps.insert(i, cap)
        self.rows.insert(i, (pts, odds))

    def in_force(self, t: datetime):
        """Latest row captured at or before t (carry-forward), else None."""
        i = bisect.bisect_right(self.caps, t)
        return self.rows[i - 1] if i else None

    def last_before(self, t: datetime):
        """(cap, row) of the last capture strictly before t, else None."""
        i = bisect.bisect_left(self.caps, t)
        return (self.caps[i - 1], self.rows[i - 1]) if i else None

    def opener(self):
        return self.rows[0] if self.rows else None


class Event:
    __slots__ = ("event_id", "sport", "league", "kick", "away", "home", "series", "caps")

    def __init__(self, event_id, sport, kick, away, home):
        self.event_id, self.sport, self.kick = event_id, sport, kick
        self.league = SPORTS[sport]
        self.away, self.home = away, home
        self.series: dict[tuple[str, str, str], Series] = {}
        self.caps: set[datetime] = set()

    def other_side(self, market: str, selection: str) -> str:
        if market == "totals":
            return "Under" if selection.lower().startswith("over") else "Over"
        return self.home if selection == self.away else self.away

    def books_in_force(self, market: str, selection: str, t: datetime):
        out = {}
        for (book, mk, sel), s in self.series.items():
            if mk == market and sel == selection:
                r = s.in_force(t)
                if r is not None:
                    out[book] = r
        return out


def load_events(path: Path) -> dict[str, Event]:
    events: dict[str, Event] = {}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sport = r.get("sport")
            if sport not in SPORTS:
                continue
            odds = _f(r.get("odds_american"))
            if odds is None:
                continue
            try:
                cap = datetime.fromisoformat(r["captured_at_et"])
                kick = datetime.fromisoformat(r["commence_time_et"])
            except (KeyError, ValueError):
                continue
            ev = events.get(r["event_id"])
            if ev is None:
                ev = events[r["event_id"]] = Event(r["event_id"], sport, kick,
                                                   r["away_team"], r["home_team"])
            key = (r["book"], r["market"], r["selection"])
            s = ev.series.get(key)
            if s is None:
                s = ev.series[key] = Series()
            s.add(cap, _f(r.get("line_points")), odds)
            ev.caps.add(cap)
    return events


def decision_capture(ev: Event) -> tuple[datetime, bool] | None:
    """First capture run on a Tuesday (ET) with kickoff 3-8 days out at which
    DK has a row in force; else the first run within 7 days of kickoff
    (flagged fallback). The log is change-only, so a run counts for the event
    when any book changed at that time and DK's opener is already on file."""
    dk_open = min((s.caps[0] for (b, _, _), s in ev.series.items() if b == DK and s.caps),
                  default=None)
    if dk_open is None:
        return None
    caps = sorted(c for c in ev.caps if c >= dk_open and c < ev.kick)
    for c in caps:
        if c.weekday() == TUESDAY and TUE_WINDOW[0] <= ev.kick - c <= TUE_WINDOW[1]:
            return c, False
    for c in caps:
        if ev.kick - c <= FALLBACK_WINDOW:
            return c, True
    return None


def cfbd_closes(path: Path | None) -> dict:
    out = {}
    if path is None or not path.exists():
        return out
    for r in csv.DictReader(path.open(encoding="utf-8")):
        out[(_norm(r["away_team"]), _norm(r["home_team"]))] = r
    return out


def closing_price(ev: Event, market: str, selection: str, cfbd_row, now: datetime,
                  after: datetime):
    """(close_odds, close_pts, source, close_time) or None.

    CFBD's DraftKings close is used when the row was captured after kickoff;
    the DK odds at the last snapshot before kickoff supply the juice (-110 if
    none). Otherwise the last DK snapshot before kickoff, which must be later
    than the decision capture (else CLV would be zero by construction)."""
    if ev.kick > now:
        return None
    s = ev.series.get((DK, market, selection))
    snap = s.last_before(ev.kick) if s else None
    if cfbd_row is not None and ev.league == "cfb":
        try:
            cfbd_cap = datetime.fromisoformat(cfbd_row.get("captured_at_et", ""))
        except ValueError:
            cfbd_cap = None
        if cfbd_cap is not None and cfbd_cap >= ev.kick:
            is_home = _norm(selection).startswith(_norm(ev.home)) or _norm(ev.home).startswith(_norm(selection))
            if market == "h2h":
                ml = _f(cfbd_row.get("home_ml" if is_home else "away_ml"))
                if ml is not None:
                    return ml, None, "cfbd", cfbd_cap
            elif market == "totals":
                tot = _f(cfbd_row.get("total"))
                if tot is not None:
                    return (snap[1][1] if snap else -110.0), tot, "cfbd", cfbd_cap
            elif market == "spreads":
                sh = _f(cfbd_row.get("spread_home"))
                if sh is not None:
                    return (snap[1][1] if snap else -110.0), (sh if is_home else -sh), "cfbd", cfbd_cap
    if snap is None or snap[0] <= after:
        return None
    pts, odds = snap[1]
    if market != "h2h" and pts is None:
        return None
    return odds, pts, "snapshot", snap[0]


# ---------------------------------------------------------------------------
# Signals at a decision time, from the bettor's seat for `selection`
# ---------------------------------------------------------------------------

def _novig(book_rows_sel: dict, book_rows_other: dict) -> dict:
    """book -> two-sided no-vig probability for the selection; books missing
    either side are skipped."""
    out = {}
    for b, (_, o1) in book_rows_sel.items():
        r2 = book_rows_other.get(b)
        if r2 is None:
            continue
        p1, p2 = implied(o1), implied(r2[1])
        out[b] = p1 / (p1 + p2)
    return out


def signals_at(ev: Event, market: str, selection: str, t: datetime) -> dict | None:
    s_dk = ev.series.get((DK, market, selection))
    dk = s_dk.in_force(t) if s_dk else None
    if dk is None:
        return None
    dk_pts, dk_odds = dk
    if market != "h2h" and dk_pts is None:
        return None
    league = ev.league
    dk_cap = s_dk.caps[bisect.bisect_right(s_dk.caps, t) - 1]
    sig = {"dk_odds": dk_odds, "dk_pts": dk_pts,
           "dk_row_age_h": (t - dk_cap).total_seconds() / 3600.0}
    for k, _ in SIGNALS:
        sig[k] = None

    rows = ev.books_in_force(market, selection, t)
    others = {b: r for b, r in rows.items() if b != DK}

    if market == "h2h":
        other_sel = ev.other_side(market, selection)
        rows2 = ev.books_in_force(market, other_sel, t)
        nv = _novig(rows, rows2)
        nv_dk = nv.get(DK)
        nv_others = {b: p for b, p in nv.items() if b != DK}
        if nv_dk is not None and nv_others:
            cons = statistics.fmean(nv_others.values())
            sig["dk_vs_consensus_cents"] = (cons - nv_dk) / nv_dk * 100.0
            if PINNACLE in nv_others:
                sig["dk_vs_pinnacle_cents"] = (nv_others[PINNACLE] - nv_dk) / nv_dk * 100.0
        if others:
            # best other book = lowest vigged implied for the selection
            sig["dk_vs_best_cents"] = min(juice_pct(dk_odds, implied(r[1])) for r in others.values())
            probs = [implied(r[1]) for r in rows.values()]
            sig["book_disagreement_points"] = (max(probs) - min(probs)) * 100.0
    else:
        usable = {b: r for b, r in others.items() if r[0] is not None}
        if usable:
            cons_pts = statistics.fmean(r[0] for r in usable.values())
            cons_impl = statistics.fmean(implied(r[1]) for r in usable.values())
            c, p = edge_pct(market, selection, league, dk_odds, dk_pts, cons_impl, cons_pts)
            sig["dk_vs_consensus_cents"], sig["dk_vs_consensus_points"] = c, p
            if PINNACLE in usable:
                pr = usable[PINNACLE]
                c, p = edge_pct(market, selection, league, dk_odds, dk_pts, implied(pr[1]), pr[0])
                sig["dk_vs_pinnacle_cents"], sig["dk_vs_pinnacle_points"] = c, p
            sig["dk_vs_best_cents"] = min(
                edge_pct(market, selection, league, dk_odds, dk_pts, implied(r[1]), r[0])[0]
                for r in usable.values())
            all_pts = [r[0] for r in rows.values() if r[0] is not None]
            sig["book_disagreement_points"] = max(all_pts) - min(all_pts)

    op = s_dk.opener()
    if op is not None and (market == "h2h" or op[0] is not None):
        # opener in the "took" seat, decision row in the "close" seat:
        # positive = the market has moved toward the selection since open
        c, p = edge_pct(market, selection, league, op[1], op[0], implied(dk_odds), dk_pts)
        sig["move_since_open_cents"], sig["move_since_open_points"] = c, p
    return sig


def realized(ev: Event, market: str, selection: str, dk_odds: float, dk_pts,
             close) -> dict:
    close_odds, close_pts, source, close_time = close
    if market == "h2h":
        clv, pts = juice_pct(dk_odds, implied(close_odds)), None
    else:
        pts = points_in_favor(market, selection, dk_pts, close_pts)
        clv = juice_pct(dk_odds, implied(close_odds)) + pts * point_value_pct(market, ev.league)
    return {"realized_clv_pct": clv, "beat_close": clv > 0, "clv_points": pts,
            "close_odds": close_odds, "close_pts": close_pts,
            "close_source": source, "close_time": close_time}


# ---------------------------------------------------------------------------
# Observation universe
# ---------------------------------------------------------------------------

def build_observations(snap_path: Path, cfbd_path: Path | None = None,
                       now: datetime | None = None) -> tuple[list[dict], dict, dict]:
    """One row per (event, market, selection) with a Tuesday (or fallback)
    DK decision capture and a close. Returns (observations, events, diagnostics)."""
    now = now or now_eastern()
    events = load_events(snap_path)
    cfbd = cfbd_closes(cfbd_path)
    diag = defaultdict(int)
    obs: list[dict] = []
    matched_events: set[str] = set()
    for ev in events.values():
        if ev.kick > now:
            diag["events_future"] += 1
            continue
        diag["events_past"] += 1
        dc = decision_capture(ev)
        if dc is None:
            diag["events_no_decision_capture"] += 1
            continue
        t, fallback = dc
        cfbd_row = cfbd_lookup(cfbd, ev.away, ev.home) if ev.league == "cfb" and cfbd else None
        if ev.league == "cfb":
            diag["cfb_events_cfbd_matched" if cfbd_row else "cfb_events_cfbd_unmatched"] += 1
        sels = sorted({(mk, sel) for (b, mk, sel) in ev.series if b == DK and mk in MARKETS})
        for market, selection in sels:
            sig = signals_at(ev, market, selection, t)
            if sig is None:
                diag["sel_no_dk_row_at_decision"] += 1
                continue
            close = closing_price(ev, market, selection, cfbd_row, now, t)
            if close is None:
                diag["sel_no_close"] += 1
                continue
            row = {"event_id": ev.event_id, "sport": ev.sport, "league": ev.league,
                   "matchup": f"{ev.away} @ {ev.home}", "kick": ev.kick,
                   "market": market, "selection": selection,
                   "decision_time": t, "decision_fallback": fallback}
            row.update(sig)
            row.update(realized(ev, market, selection, sig["dk_odds"], sig["dk_pts"], close))
            obs.append(row)
            matched_events.add(ev.event_id)
            diag[f"close_{close[2]}"] += 1
            diag["decisions_fallback" if fallback else "decisions_tuesday"] += 1
    diag["events_with_observations"] = len(matched_events)
    return obs, events, dict(diag)


def ledger_observations(events: dict[str, Event], ledger_path: Path, cfbd_path: Path | None,
                        now: datetime | None = None) -> tuple[list[dict], dict]:
    """The real decisions: earliest PICK/LEAN row per (event, market,
    selection), signals computed at that row's own capture time."""
    now = now or now_eastern()
    diag = defaultdict(int)
    if not ledger_path.exists():
        return [], dict(diag)
    first: dict = {}
    for r in csv.DictReader(ledger_path.open(encoding="utf-8")):
        if (r.get("decision") or "").upper() not in TRACKED_DECISIONS:
            continue
        key = (r.get("event_id"), r.get("market"), r.get("selection"))
        stamp = (r.get("decision_date", ""), r.get("captured_at_et", ""))
        if key not in first or stamp < first[key][0]:
            first[key] = (stamp, r)
    diag["ledger_decisions"] = len(first)
    cfbd = cfbd_closes(cfbd_path)
    out = []
    for (eid, market, selection), (_, r) in first.items():
        ev = events.get(eid)
        if ev is None or market not in MARKETS:
            diag["ledger_event_missing"] += 1
            continue
        try:
            t = datetime.fromisoformat(r.get("captured_at_et", ""))
        except ValueError:
            diag["ledger_bad_capture_time"] += 1
            continue
        sig = signals_at(ev, market, selection, t)
        if sig is None:
            diag["ledger_no_dk_row"] += 1
            continue
        cfbd_row = cfbd_lookup(cfbd, ev.away, ev.home) if ev.league == "cfb" and cfbd else None
        close = closing_price(ev, market, selection, cfbd_row, now, t)
        if close is None:
            diag["ledger_no_close"] += 1
            continue
        row = {"event_id": eid, "league": ev.league, "market": market, "selection": selection,
               "matchup": f"{ev.away} @ {ev.home}", "decision_time": t,
               "confidence": _f(r.get("confidence")),
               "ledger_clv_pct": _f(r.get("closing_line_value_pct"))}
        row.update(sig)
        row.update(realized(ev, market, selection, sig["dk_odds"], sig["dk_pts"], close))
        out.append(row)
    diag["ledger_scored"] = len(out)
    return out, dict(diag)


# ---------------------------------------------------------------------------
# Statistics (stdlib only)
# ---------------------------------------------------------------------------

def pearson(xs, ys) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def ranks(xs) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs, ys) -> float | None:
    return pearson(ranks(xs), ranks(ys)) if len(xs) >= 3 else None


def quintiles(pairs: list[tuple[float, float]], k: int = QUINTILES) -> list[dict]:
    """pairs = (signal, clv). Equal-count bins by signal, ascending."""
    pairs = sorted(pairs)
    n = len(pairs)
    if n < k:
        return []
    out = []
    base, rem = divmod(n, k)
    start = 0
    for b in range(k):
        size = base + (1 if b < rem else 0)
        chunk = pairs[start:start + size]
        start += size
        clvs = [c for _, c in chunk]
        out.append({"bin": b + 1, "n": len(chunk), "lo": chunk[0][0], "hi": chunk[-1][0],
                    "mean_clv": statistics.fmean(clvs),
                    "beat": sum(1 for c in clvs if c > 0) / len(clvs)})
    return out


def edge_floor(pairs: list[tuple[float, float]], min_n: int = FLOOR_MIN_N,
               by: str = "mean") -> dict | None:
    """Smallest threshold t such that rows with signal >= t have mean CLV > 0
    (by="mean") or a beat-close rate >= 50% (by="beat"), with n >= min_n.
    None if no threshold qualifies; "at_min" marks a floor equal to the
    slice minimum, i.e. the whole slice qualifies and t is not a floor."""
    if not pairs:
        return None
    desc = sorted(pairs, key=lambda p: -p[0])
    best = None
    n = 0
    tot = 0.0
    beat = 0
    i = 0
    while i < len(desc):
        t = desc[i][0]
        j = i
        while j < len(desc) and desc[j][0] == t:
            n += 1
            tot += desc[j][1]
            beat += 1 if desc[j][1] > 0 else 0
            j += 1
        ok = (tot / n > 0) if by == "mean" else (beat / n >= 0.5)
        if n >= min_n and ok:
            best = {"t": t, "n": n, "mean_clv": tot / n, "beat": beat / n,
                    "at_min": j == len(desc)}
        i = j
    return best


def corr_block(rows: list[dict], signal: str) -> dict | None:
    xs = [r[signal] for r in rows if r.get(signal) is not None]
    ys = [r["realized_clv_pct"] for r in rows if r.get(signal) is not None]
    if len(xs) < 3:
        return None
    return {"n": len(xs), "pearson": pearson(xs, ys), "spearman": spearman(xs, ys),
            "mean_clv": statistics.fmean(ys),
            "quintiles": quintiles(list(zip(xs, ys)))}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _r(v, d=3):
    return "n/a" if v is None else f"{v:+.{d}f}"


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.0f}%"


H2H_ODDS_CAP = 300   # robustness slice: h2h with DK odds inside -300..+300


def _groups(obs: list[dict]) -> list[tuple[str, list[dict]]]:
    out = [("pooled", obs)]
    for mk in MARKETS:
        out.append((mk, [o for o in obs if o["market"] == mk]))
    out.append((f"h2h |odds|<={H2H_ODDS_CAP}", [o for o in obs if o["market"] == "h2h"
                                                and abs(o["dk_odds"]) <= H2H_ODDS_CAP]))
    out.append(("pooled, DK row fresh", [o for o in obs if o.get("dk_row_age_h", 1) == 0]))
    for lg in ("cfb", "nfl"):
        sub = [o for o in obs if o["league"] == lg]
        if len(sub) >= LEAGUE_MIN_N:
            out.append((f"{lg} pooled", sub))
            for mk in MARKETS:
                sub2 = [o for o in sub if o["market"] == mk]
                if len(sub2) >= LEAGUE_MIN_N:
                    out.append((f"{lg} {mk}", sub2))
    return out


def analyze(obs: list[dict]) -> dict:
    res = {"groups": {}, "floors": {}}
    for name, rows in _groups(obs):
        res["groups"][name] = {sig: corr_block(rows, sig) for sig, _ in SIGNALS}
    for name in ("pooled",) + MARKETS:
        rows = [o for o in obs if name == "pooled" or o["market"] == name]
        pairs = [(o["dk_vs_consensus_cents"], o["realized_clv_pct"]) for o in rows
                 if o.get("dk_vs_consensus_cents") is not None]
        res["floors"][name] = (edge_floor(pairs, by="mean"), edge_floor(pairs, by="beat"), len(pairs))
    return res


def verdict(res: dict) -> tuple[str | None, str]:
    """Which signal, if any, the numbers support: pooled Spearman >= 0.10,
    top quintile mean CLV > 0 and above the bottom quintile, and the sign of
    the Spearman agreeing in every market with n >= LEAGUE_MIN_N."""
    pooled = res["groups"]["pooled"]
    ranked = []
    for sig, _ in SIGNALS:
        b = pooled.get(sig)
        if not b or b["spearman"] is None or not b["quintiles"]:
            continue
        q = b["quintiles"]
        sign_ok = all(
            (res["groups"][mk].get(sig) or {}).get("spearman") is None
            or (res["groups"][mk][sig]["n"] < LEAGUE_MIN_N)
            or (res["groups"][mk][sig]["spearman"] > 0) == (b["spearman"] > 0)
            for mk in MARKETS)
        ok = (b["spearman"] >= 0.10 and q[-1]["mean_clv"] > 0
              and q[-1]["mean_clv"] > q[0]["mean_clv"] and sign_ok)
        ranked.append((b["spearman"], sig, ok, sign_ok, q))
    ranked.sort(reverse=True)
    if not ranked:
        return None, "No signal had enough rows to evaluate."
    top_rho, top_sig, top_ok, top_sign_ok, q = ranked[0]
    supported = [r for r in ranked if r[2]]
    lines = []
    lines.append(f"The strongest pooled rank correlation is {top_sig} (Spearman {top_rho:+.3f}, "
                 f"n={pooled[top_sig]['n']}); its top quintile averages {q[-1]['mean_clv']:+.2f}% CLV "
                 f"against {q[0]['mean_clv']:+.2f}% in the bottom quintile.")
    if supported:
        f = res["floors"]

        def fl_text(fl):
            if not fl:
                return "no threshold"
            return (f"t >= {fl['t']:+.2f}" + (" (slice min, not a floor)" if fl["at_min"] else "")
                    + f" (n={fl['n']}, mean {fl['mean_clv']:+.2f}%, beat {_pct(fl['beat'])})")
        listed = "; ".join(f"{sig} (Spearman {rho:+.3f}, n={pooled[sig]['n']})" for rho, sig, _, _, _ in supported)
        lines.append(f"By the test stated above (pooled Spearman >= 0.10, top quintile positive and above "
                     f"the bottom, sign agreeing across markets), the signals that qualify are: {listed}. "
                     f"Of these, {supported[0][1]} has the largest rank correlation and "
                     f"{max(supported, key=lambda r: pooled[r[1]]['n'])[1]} the widest coverage.")
        lines.append("Edge floor on dk_vs_consensus_cents, by mean CLV > 0: " + "; ".join(
            f"{mk} {fl_text(f[mk][0])}" for mk in ("pooled",) + MARKETS) + ".")
        lines.append("By beat-close rate >= 50%: " + "; ".join(
            f"{mk} {fl_text(f[mk][1])}" for mk in ("pooled",) + MARKETS) + ". "
            "A floor marked 'slice min' is not evidence for a floor; 'no threshold' means the numbers do not "
            "support a floor on that market.")
        return supported[0][1], " ".join(lines)
    lines.append("By the test stated above (pooled Spearman >= 0.10, top quintile positive and above "
                 "the bottom, sign agreeing across markets), none of the candidate signals qualifies "
                 "as a ranking basis on this sample, and no edge floor is supported. The honest "
                 "disposition is to keep collecting Tuesday captures and rerun this before any "
                 "replacement score is adopted.")
    return None, " ".join(lines)


def render(obs, res, ledger_rows, diag, ldiag, runtime_s, stamp, snap_path, cfbd_path) -> str:
    L = ["---", "type: agent-report", "agent: football-picks-agent",
         f"date: {stamp.date().isoformat()}", "report: backtest-signals", "---", "",
         f"# Which market signal predicts closing-line value? — {stamp.strftime('%b %-d, %Y')}", "",
         f"Run {stamp.strftime('%Y-%m-%d %H:%M ET')} in {runtime_s:.1f}s by `backtest-signals.py` (AQ-220 step 1-2). "
         f"Conventions {CONVENTIONS}. Source `{snap_path.name}`; CFB closes `{cfbd_path.name if cfbd_path else 'none'}`.", ""]

    # universe
    L += ["## Universe", "",
          f"- Events in log (ncaaf + nfl, preseason excluded): {diag.get('events_past', 0) + diag.get('events_future', 0)}; "
          f"kicked off already: {diag.get('events_past', 0)}; still in the future (no close possible): {diag.get('events_future', 0)}.",
          f"- Events with no usable DK decision capture (no Tuesday capture 3-8 days out and none inside 7 days): {diag.get('events_no_decision_capture', 0)}.",
          f"- Events contributing observations: {diag.get('events_with_observations', 0)}.",
          f"- Observations (event × market × selection): {len(obs)} — decision capture on a Tuesday: {diag.get('decisions_tuesday', 0)}, "
          f"fallback (first capture inside 7 days): {diag.get('decisions_fallback', 0)}.",
          f"- Close source: CFBD DraftKings close {diag.get('close_cfbd', 0)}, last DK snapshot before kickoff {diag.get('close_snapshot', 0)}. "
          f"Selections dropped for no close: {diag.get('sel_no_close', 0)}; for no DK row at the decision capture: {diag.get('sel_no_dk_row_at_decision', 0)}.",
          f"- CFB events matched to CFBD by school-name prefix: {diag.get('cfb_events_cfbd_matched', 0)} matched, {diag.get('cfb_events_cfbd_unmatched', 0)} unmatched.",
          f"- DK price captured at the decision time (fresh): {sum(1 for o in obs if o.get('dk_row_age_h', 0) == 0)}; carried forward from an earlier capture: {sum(1 for o in obs if o.get('dk_row_age_h', 0) > 0)}.", ""]
    L += ["| slice | n | mean CLV % | beat close |", "|---|---:|---:|---:|"]
    for name, rows in _groups(obs):
        if rows:
            clv = [o["realized_clv_pct"] for o in rows]
            L.append(f"| {name} | {len(rows)} | {statistics.fmean(clv):+.2f} | {_pct(sum(1 for c in clv if c > 0) / len(clv))} |")
    L += ["", "Both sides of every market are in the universe, so the points component of CLV nets to ~zero "
          "across a pair and the mean is mostly juice; a signal has to sort the sides, not the games.", ""]

    # per-signal
    L += ["## Signals vs realized CLV", "",
          "Correlation with realized_clv_pct. Quintiles are equal-count bins by the signal, ascending; "
          "edges are printed in the signal's own units (percent of implied probability for `_cents`, "
          "points for `_points`; book_disagreement is points, or probability points for h2h).", ""]
    for sig, desc in SIGNALS:
        L += [f"### {sig}", "", f"{desc}. Positive = good for the bettor of that selection.", "",
              "| slice | n | Pearson | Spearman | mean CLV % |", "|---|---:|---:|---:|---:|"]
        for name, _ in _groups(obs):
            b = res["groups"][name].get(sig)
            if b:
                L.append(f"| {name} | {b['n']} | {_r(b['pearson'])} | {_r(b['spearman'])} | {b['mean_clv']:+.2f} |")
        L.append("")
        for name in ("pooled",) + MARKETS:
            b = res["groups"][name].get(sig)
            if not b or not b["quintiles"]:
                continue
            L += [f"Quintiles, {name}:", "", "| bin | n | signal range | mean CLV % | beat close |", "|---|---:|---|---:|---:|"]
            for q in b["quintiles"]:
                L.append(f"| Q{q['bin']} | {q['n']} | {q['lo']:+.2f} to {q['hi']:+.2f} | {q['mean_clv']:+.2f} | {_pct(q['beat'])} |")
            L.append("")

    # edge floor
    L += ["## Edge floor on dk_vs_consensus_cents", "",
          f"Smallest threshold t such that observations with signal >= t have (a) mean realized CLV > 0, or "
          f"(b) a beat-close rate of at least 50%, each with n >= {FLOOR_MIN_N}. Both are shown because the "
          "relative-percent CLV of long-shot moneylines skews the mean; the beat rate does not care about magnitude.", "",
          "| slice | rows with signal | (a) floor t, mean > 0 | n | mean CLV % | beat | (b) floor t, beat >= 50% | n | mean CLV % | beat |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name in ("pooled",) + MARKETS:
        fm, fb, npairs = res["floors"][name]
        cells = [name, str(npairs)]
        for fl in (fm, fb):
            if fl:
                cells += [f"{fl['t']:+.2f}" + (" (slice min)" if fl["at_min"] else ""), str(fl["n"]),
                          f"{fl['mean_clv']:+.2f}", _pct(fl["beat"])]
            else:
                cells += ["none", "—", "—", "—"]
        L.append("| " + " | ".join(cells) + " |")
    L += ["", "\"(slice min)\" means the whole slice already qualifies, so t is the smallest value present and is not "
          "a floor at all; read it with the quintile table above. \"none\" means no threshold leaves "
          f"{FLOOR_MIN_N} rows that qualify.", "",
          "Fixed thresholds on the same signal (rows with dk_vs_consensus_cents >= t): n / mean CLV % / beat close.", "",
          "| slice | " + " | ".join(f"t >= {t:g}" for t in LADDER) + " |",
          "|---|" + "---:|" * len(LADDER)]
    for name, rows in _groups(obs):
        if name.startswith(("cfb", "nfl")) and name not in ("cfb pooled", "nfl pooled"):
            continue
        cells = []
        for t in LADDER:
            sub = [o["realized_clv_pct"] for o in rows
                   if o.get("dk_vs_consensus_cents") is not None and o["dk_vs_consensus_cents"] >= t]
            cells.append(f"{len(sub)} / {statistics.fmean(sub):+.2f} / {_pct(sum(1 for c in sub if c > 0) / len(sub))}"
                         if sub else "0 / — / —")
        L.append(f"| {name} | " + " | ".join(cells) + " |")
    L.append("")

    # ledger
    L += ["## The 45 real decisions: confidence vs the signals", "",
          f"Ledger PICK/LEAN rows, earliest per (event, market, selection): {ldiag.get('ledger_decisions', 0)}; "
          f"scored here: {ldiag.get('ledger_scored', 0)} "
          f"(missing event {ldiag.get('ledger_event_missing', 0)}, no DK row at capture {ldiag.get('ledger_no_dk_row', 0)}, "
          f"no close {ldiag.get('ledger_no_close', 0)}). Signals are computed at each row's own capture time, "
          "not the Tuesday capture; CLV is recomputed here with the same close rules as above, and the "
          "ledger's own closing_line_value_pct is shown beside it.", ""]
    if ledger_rows:
        clv = [r["realized_clv_pct"] for r in ledger_rows]
        lclv = [r["ledger_clv_pct"] for r in ledger_rows if r["ledger_clv_pct"] is not None]
        L += [f"Mean recomputed CLV {statistics.fmean(clv):+.2f}% (beat close {_pct(sum(1 for c in clv if c > 0) / len(clv))}); "
              f"ledger's own CLV column mean {statistics.fmean(lclv):+.2f}% over {len(lclv)} rows"
              + (f"; correlation between the two {_r(pearson([r['realized_clv_pct'] for r in ledger_rows if r['ledger_clv_pct'] is not None], lclv))}." if len(lclv) >= 3 else "."), "",
              "| score | n | Pearson vs recomputed CLV | Spearman vs recomputed CLV | Pearson vs ledger CLV | Spearman vs ledger CLV |",
              "|---|---:|---:|---:|---:|---:|"]
        for sig in ("confidence",) + tuple(s for s, _ in SIGNALS):
            xs = [r[sig] for r in ledger_rows if r.get(sig) is not None]
            ys = [r["realized_clv_pct"] for r in ledger_rows if r.get(sig) is not None]
            xl = [r[sig] for r in ledger_rows if r.get(sig) is not None and r["ledger_clv_pct"] is not None]
            yl = [r["ledger_clv_pct"] for r in ledger_rows if r.get(sig) is not None and r["ledger_clv_pct"] is not None]
            if len(xs) >= 3:
                L.append(f"| {sig} | {len(xs)} | {_r(pearson(xs, ys))} | {_r(spearman(xs, ys))} | "
                         f"{_r(pearson(xl, yl))} | {_r(spearman(xl, yl))} |")
        L.append("")
        by_mk = defaultdict(list)
        for r in ledger_rows:
            by_mk[r["market"]].append(r)
        L += ["Ledger rows by market:", "", "| market | n | mean confidence | mean dk_vs_consensus_cents | mean recomputed CLV % |", "|---|---:|---:|---:|---:|"]
        for mk in MARKETS:
            rows = by_mk.get(mk, [])
            if rows:
                conf = [r["confidence"] for r in rows if r["confidence"] is not None]
                dvc = [r["dk_vs_consensus_cents"] for r in rows if r["dk_vs_consensus_cents"] is not None]
                L.append(f"| {mk} | {len(rows)} | {statistics.fmean(conf) if conf else float('nan'):.1f} | "
                         f"{statistics.fmean(dvc) if dvc else float('nan'):+.2f} | "
                         f"{statistics.fmean(r['realized_clv_pct'] for r in rows):+.2f} |")
        L.append("")
    else:
        L += ["No ledger rows could be scored.", ""]

    # caveats
    L += ["## Reading this honestly", "",
          "- The decision capture is the first Tuesday capture 3-8 days before kickoff (a 7:30 AM ET pull on most weeks), "
          "not the true opener. \"Move since open\" measures from the first row in our log, which for early-season games "
          "is a late-July capture and for later games is whenever the market was first posted to us.",
          "- The close is the last capture we have. For CFB it is CFBD's DraftKings close when that row was captured after "
          "kickoff; otherwise (and for the NFL) it is the last DK snapshot before kickoff, which on most game days is a "
          "morning pull hours before the true close.",
          f"- Points are converted to percent with a rule of thumb ({', '.join(f'{m} {l} {v:g}' for (m, l), v in sorted(POINT_VALUE_PCT.items()))} "
          "percent per point), unweighted for key numbers. The raw-points columns are printed so nothing rests on the constants.",
          "- Signals and outcomes come from the same log over the same weeks. Correlation here is in-sample description, "
          "not out-of-sample skill; a floor chosen from this table will look worse on new weeks.",
          f"- Fallback decision captures ({diag.get('decisions_fallback', 0)} of {len(obs)}) are closer to kickoff than the Tuesday ones, "
          "so their CLV is mechanically smaller.",
          "- Book coverage differs by row: the consensus is the mean of whichever other books were in force at the capture, "
          "and Pinnacle is present only for a subset.",
          f"- The log is change-only, so DK's row in force at the decision capture may have been captured earlier: "
          f"{sum(1 for o in obs if o.get('dk_row_age_h', 0) > 0)} of {len(obs)} rows carry a DK price older than the capture "
          f"({sum(1 for o in obs if o.get('dk_row_age_h', 0) > 24)} older than 24 hours). A stale DK number looks like an edge "
          "versus consensus and then 'moves' to the close; the 'pooled, DK row fresh' slice restricts to rows whose DK price "
          "was captured at the decision time.",
          "- h2h CLV is a relative percent of implied probability, which is large for long shots: a +4000 that closes +2000 "
          "reads +95%, a -400 that closes -250 reads -11%. Means and Pearson on h2h are dominated by those rows; the "
          f"Spearman and the 'h2h |odds|<={H2H_ODDS_CAP}' slice are the honest reads.",
          "- CLV is measured against DK's own close, so a DK number off consensus that converges to consensus registers as "
          "CLV by construction. That is the real edge if the number can be bet at DK before it moves, and nothing else.", ""]
    L += ["## What the numbers support", "", verdict(res)[1], ""]
    return "\n".join(L)


def summary_lines(obs, res, ledger_rows) -> list[str]:
    out = [f"  {'signal':26} {'slice':9} {'n':>5} {'pearson':>8} {'spearman':>9}"]
    for sig, _ in SIGNALS:
        for name in ("pooled",) + MARKETS:
            b = res["groups"][name].get(sig)
            if b:
                out.append(f"  {sig:26} {name:9} {b['n']:5d} {_r(b['pearson']):>8} {_r(b['spearman']):>9}")
    out.append("  edge floor (dk_vs_consensus_cents, n >= %d): (a) mean CLV > 0 | (b) beat-close >= 50%%" % FLOOR_MIN_N)
    for name in ("pooled",) + MARKETS:
        fm, fb, npairs = res["floors"][name]
        parts = []
        for fl in (fm, fb):
            parts.append((f"t >= {fl['t']:+.2f}{' (slice min)' if fl['at_min'] else ''} n={fl['n']} "
                          f"mean {fl['mean_clv']:+.2f}% beat {_pct(fl['beat'])}") if fl else "none")
        out.append(f"    {name:9} (a) {parts[0]} | (b) {parts[1]}")
    if ledger_rows:
        xs = [r["confidence"] for r in ledger_rows if r["confidence"] is not None]
        ys = [r["realized_clv_pct"] for r in ledger_rows if r["confidence"] is not None]
        out.append(f"  ledger: {len(ledger_rows)} rows, confidence vs recomputed CLV pearson {_r(pearson(xs, ys))} spearman {_r(spearman(xs, ys))}")
    return out


def unique_path(path: Path) -> Path:
    """Never overwrite a file this run did not create: suffix on collision."""
    if not path.exists():
        return path
    for i in range(2, 100):
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true", help="print the summary only")
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    ap.add_argument("--cfbd", type=Path, default=CFBD_CLOSES)
    ap.add_argument("--ledger", type=Path, default=LEDGER)
    args = ap.parse_args()
    if not args.snapshots.exists():
        print(f"MISSING: {args.snapshots}")
        return 1
    archive = args.snapshots.parent / "archive"
    if archive.exists() and any(archive.glob("snapshots-*.csv")):
        print(f"note: games older than 30 days live in {archive.relative_to(AGENT)}/ (archive-snapshots.py); "
              "pass --snapshots <merged file> to backtest across them")
    t0 = time.perf_counter()
    stamp = now_eastern()
    obs, events, diag = build_observations(args.snapshots, args.cfbd if args.cfbd.exists() else None, stamp)
    ledger_rows, ldiag = ledger_observations(events, args.ledger, args.cfbd if args.cfbd.exists() else None, stamp)
    res = analyze(obs)
    runtime = time.perf_counter() - t0
    print(f"backtest-signals — {stamp.strftime('%b %-d, %Y at %-I:%M %p ET')} — {runtime:.1f}s")
    print(f"  {len(obs)} observations from {diag.get('events_with_observations', 0)} events "
          f"({diag.get('decisions_tuesday', 0)} Tuesday captures, {diag.get('decisions_fallback', 0)} fallback); "
          f"closes: {diag.get('close_cfbd', 0)} CFBD, {diag.get('close_snapshot', 0)} snapshot")
    for line in summary_lines(obs, res, ledger_rows):
        print(line)
    print("  " + verdict(res)[1])
    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = unique_path(OUT_DIR / f"backtest-signals-{stamp.date().isoformat()}.md")
        out.write_text(render(obs, res, ledger_rows, diag, ldiag, runtime, stamp,
                              args.snapshots, args.cfbd if args.cfbd.exists() else None), encoding="utf-8")
        print(f"  wrote {out.relative_to(ROOT)}")
    print(f"  runtime {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
