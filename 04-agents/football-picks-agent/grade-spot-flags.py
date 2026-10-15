#!/usr/bin/env python3
"""grade-spot-flags.py — score the situational flags against results (AQ-168 step 2)

    python3 -B 04-agents/football-picks-agent/grade-spot-flags.py [--year 2026] [--no-network]

READ-ONLY analysis. Reads data/spot-flags.csv (written by spot-flags.py),
line-watch/snapshots.csv (closing lines: last pre-kickoff row, pinnacle else
draftkings) and final scores through settle-picks.py's own cfb_scores /
nfl_scores (one free-tier CFBD call + the nflverse CSV). Writes
05-outputs/reports/football-picks-agent/spot-flags-grade-<today>.md.

WHY. spot-flags.py has shipped SHORT_REST / REST_EDGE / LOOKAHEAD / LETDOWN /
RLM display-only since 2026-08-30 with the promise "measure first, weight
later". This is the measuring. Nothing here changes a weight; the report
says "sample too small, no weight change" whenever a flag has < 30 graded
games.

RLM is graded two ways: (a) from spot-flags.csv rows as written, and (b)
retrospectively, by re-applying spot-flags.rlm_flags' exact rule to every
kicked-off game in snapshots.csv, because the flags file only keeps the
latest computation and RLM rows are rare in it.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import importlib.util
import pathlib
import re
import statistics
import sys
import unicodedata
from collections import defaultdict

AGENT = pathlib.Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))

FLAGS_CSV = AGENT / "data" / "spot-flags.csv"
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
NFL_SCHEDULE = AGENT / "data" / "nfl-schedule.csv"
CFB_SCHEDULE = AGENT / "data" / "cfb-schedule.csv"
REPORT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"

FLAG_NAMES = ("SHORT_REST", "REST_EDGE", "LOOKAHEAD", "LETDOWN", "RLM")
MIN_SAMPLE = 30
BOOK_PREFERENCE = ("pinnacle", "draftkings")
MATCH_WINDOW_H = 36

# spot-flags.rlm_flags thresholds (probability as a fraction, not points)
RLM_CONS_MIN = 0.01
RLM_DK_MIN = 0.005

_FLAG_RE = re.compile(r"^(SHORT_REST|REST_EDGE|LOOKAHEAD|LETDOWN)\s+(.+?)\s*\((.*)\)\s*$")
_RLM_RE = re.compile(r"^RLM:\s*books moved (toward|off)\s+(.+?),\s*DK went the other way\s*$")


# ---------------------------------------------------------------- helpers

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in s.lower() if c.isalnum())


def parse_ts(s: str) -> datetime.datetime | None:
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


def prob(odds: float) -> float:
    """American price -> implied probability as a fraction (same as spot-flags)."""
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def parse_flags(flags: str) -> list:
    """'SHORT_REST A (0d) | LOOKAHEAD B (next: X) | RLM: books moved toward Y, DK went the other way'
    -> [{"flag", "team", "detail", "direction"}]. Unknown tokens keep flag=first word, team=''."""
    out = []
    for raw in (flags or "").split("|"):
        part = raw.strip()
        if not part:
            continue
        m = _FLAG_RE.match(part)
        if m:
            out.append({"flag": m.group(1), "team": m.group(2).strip(), "detail": m.group(3).strip(), "direction": ""})
            continue
        m = _RLM_RE.match(part)
        if m:
            out.append({"flag": "RLM", "team": m.group(2).strip(), "detail": part[4:].strip(),
                        "direction": m.group(1)})
            continue
        head = part.split(":")[0].split()[0] if part.split() else part
        out.append({"flag": head, "team": "", "detail": part, "direction": ""})
    return out


def grade_ats(margin_team: float, spread_team: float) -> str:
    """margin_team = team pts - opp pts; spread_team = the team's closing handicap."""
    v = margin_team + spread_team
    return "W" if v > 0 else ("P" if v == 0 else "L")


def grade_su(margin_team: float) -> str:
    return "W" if margin_team > 0 else ("T" if margin_team == 0 else "L")


def grade_total(total_points: float, line: float) -> str:
    return "O" if total_points > line else ("P" if total_points == line else "U")


def flags_file_status(rows: list) -> dict:
    """Does the file hold one computation or a history? (spot-flags.py writes with mode 'w'.)"""
    stamps = sorted({(r.get("computed_at_et") or "")[:16] for r in rows})
    return {"rows": len(rows), "computations": len(stamps),
            "first": stamps[0] if stamps else "", "last": stamps[-1] if stamps else ""}


# ---------------------------------------------------------------- snapshots

def latest_per_game(rows: list[dict]) -> list[dict]:
    """spot-flags.csv appends one computation per run (since 2026-09-07);
    the pregame flag that counts is the last one computed before kickoff."""
    latest: dict = {}
    for r in rows:
        key = (r.get("league"), r.get("start"), r.get("away_team"), r.get("home_team"))
        if key not in latest or (r.get("computed_at_et") or "") > (latest[key].get("computed_at_et") or ""):
            latest[key] = r
    return list(latest.values())


def load_closing(path: pathlib.Path) -> dict:
    """event_id -> {sport, kickoff, away, home, last: {(market, selection, book): (captured_at, points, odds)},
    h2h_series: {(selection, book): [(captured_at, prob)]}} — pre-kickoff rows only."""
    events: dict = {}
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            cap, kick = parse_ts(r.get("captured_at_et", "")), parse_ts(r.get("commence_time_et", ""))
            if cap is None or kick is None or cap >= kick:
                continue
            ev = events.setdefault(r["event_id"], {
                "sport": r["sport"], "kickoff": kick, "away": r["away_team"], "home": r["home_team"],
                "last": {}, "h2h_series": defaultdict(list),
            })
            key = (r["market"], r["selection"], r["book"])
            try:
                pts = float(r["line_points"]) if r.get("line_points") not in ("", None) else None
            except ValueError:
                pts = None
            try:
                odds = float(r["odds_american"]) if r.get("odds_american") not in ("", None) else None
            except ValueError:
                odds = None
            prev = ev["last"].get(key)
            if prev is None or cap >= prev[0]:
                ev["last"][key] = (cap, pts, odds)
            if r["market"] == "h2h" and odds is not None:
                ev["h2h_series"][(r["selection"], r["book"])].append((cap, odds))
    for ev in events.values():
        for v in ev["h2h_series"].values():
            v.sort()
    return events


def closing_spread(ev: dict, team_full: str, prefer=BOOK_PREFERENCE):
    for book in prefer:
        row = ev["last"].get(("spreads", team_full, book))
        if row and row[1] is not None:
            return row[1], book
    return None, None


def closing_total(ev: dict, prefer=BOOK_PREFERENCE):
    for book in prefer:
        row = ev["last"].get(("totals", "Over", book))
        if row and row[1] is not None:
            return row[1], book
    return None, None


def rlm_retrospective(ev: dict, cons_min: float = RLM_CONS_MIN, dk_min: float = RLM_DK_MIN):
    """Re-apply spot-flags.rlm_flags to one event's full pre-kickoff h2h history.
    Returns (selection the consensus moved toward, cons, dk) or None."""
    moves: dict = defaultdict(dict)
    for (sel, book), rows in ev["h2h_series"].items():
        if len(rows) >= 2:
            moves[sel][book] = prob(rows[-1][1]) - prob(rows[0][1])
    best = None
    for sel, by_book in moves.items():
        dk = by_book.get("draftkings")
        others = [v for b, v in by_book.items() if b != "draftkings"]
        if dk is None or len(others) < 2:
            continue
        cons = statistics.mean(others)
        if abs(cons) >= cons_min and dk * cons < 0 and abs(dk) >= dk_min and cons > 0:
            if best is None or cons > best[1]:
                best = (sel, cons, dk)
    return best


# ---------------------------------------------------------------- matching

def load_name_maps() -> tuple[dict, dict]:
    """NFL abbreviation -> full name, CFB school -> full name, from cached schedules."""
    nfl, cfb = {}, {}
    if NFL_SCHEDULE.exists():
        for r in csv.DictReader(NFL_SCHEDULE.open(encoding="utf-8")):
            for side in ("away", "home"):
                if r.get(f"{side}_team") and r.get(f"{side}_team_full"):
                    nfl[r[f"{side}_team"]] = r[f"{side}_team_full"]
    if CFB_SCHEDULE.exists():
        for r in csv.DictReader(CFB_SCHEDULE.open(encoding="utf-8")):
            for side in ("away", "home"):
                if r.get(f"{side}_team_school") and r.get(f"{side}_team_full"):
                    cfb[norm(r[f"{side}_team_school"])] = r[f"{side}_team_full"]
    return nfl, cfb


def name_matches(flag_name: str, snap_full: str) -> bool:
    a, b = norm(flag_name), norm(snap_full)
    return bool(a) and bool(b) and (b.startswith(a) or a.startswith(b))


def match_event(events: dict, league: str, start, away: str, home: str, nfl_map: dict, cfb_map: dict):
    """Find the snapshot event for a flagged game; nearest kickoff within MATCH_WINDOW_H."""
    if league.upper() == "NFL":
        away, home = nfl_map.get(away, away), nfl_map.get(home, home)
    else:
        away, home = cfb_map.get(norm(away), away), cfb_map.get(norm(home), home)
    best = None
    for eid, ev in events.items():
        if not (name_matches(away, ev["away"]) and name_matches(home, ev["home"])):
            continue
        gap = abs((ev["kickoff"] - start).total_seconds()) / 3600 if start else 0
        if gap > MATCH_WINDOW_H:
            continue
        if best is None or gap < best[0]:
            best = (gap, eid)
    return best[1] if best else None


def side_for_flag(entry: dict, away_flag: str, home_flag: str, ev: dict):
    """Which side (away/home) the flag points at. RLM 'off X' -> X's opponent."""
    if entry["flag"] == "RLM":
        nick = norm(entry["team"])
        hit = None
        for side in ("away", "home"):
            full = ev[side]
            if norm(full.split()[-1]) == nick or norm(full).endswith(nick):
                hit = side
        if hit is None:
            return None
        if entry["direction"] == "off":
            return "home" if hit == "away" else "away"
        return hit
    t = norm(entry["team"])
    if t == norm(away_flag):
        return "away"
    if t == norm(home_flag):
        return "home"
    return None


# ---------------------------------------------------------------- grading

def grade_one(ev: dict, side: str, score) -> dict:
    """score = (away_pts, home_pts). Returns dict with su/ats/ou and the lines used."""
    ap, hp = score
    team_full = ev[side]
    margin = (hp - ap) if side == "home" else (ap - hp)
    spread, sbook = closing_spread(ev, team_full)
    total, tbook = closing_total(ev)
    return {
        "su": grade_su(margin),
        "ats": grade_ats(margin, spread) if spread is not None else "",
        "ou": grade_total(ap + hp, total) if total is not None else "",
        "spread": spread, "spread_book": sbook, "total": total, "total_book": tbook,
        "margin": margin, "score": f"{int(ap)}-{int(hp)}",
    }


def record(gs: list, key: str, order: str) -> str:
    c = defaultdict(int)
    for g in gs:
        if g.get(key):
            c[g[key]] += 1
    return "-".join(str(c[k]) for k in order)


def _load_settle():
    spec = importlib.util.spec_from_file_location("settle_picks", AGENT / "settle-picks.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["settle_picks"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-network", action="store_true", help="skip score fetch (structure check only)")
    args = ap.parse_args(argv)

    today = args.today or datetime.date.today().isoformat()
    year = args.year or int(today[:4])
    out = pathlib.Path(args.out) if args.out else REPORT_DIR / f"spot-flags-grade-{today}.md"
    now = datetime.datetime.now(datetime.timezone.utc)

    flag_rows = list(csv.DictReader(FLAGS_CSV.open(encoding="utf-8"))) if FLAGS_CSV.exists() else []
    flag_rows = latest_per_game(flag_rows)
    status = flags_file_status(flag_rows)
    events = load_closing(SNAPSHOTS)
    nfl_map, cfb_map = load_name_maps()

    cfb, nfl = {}, {}
    if not args.no_network:
        settle = _load_settle()
        cfb = settle.cfb_scores(year)
        nfl = settle.nfl_scores(year)
        lookup = settle.lookup
    else:
        def lookup(scores, a, h):  # noqa: E306
            return None

    # ---- (a) flags as written
    per_flag: dict = defaultdict(list)          # flag -> graded dicts
    flagged: dict = defaultdict(int)            # flag -> games flagged (flag entries)
    ungraded: dict = defaultdict(lambda: defaultdict(int))  # flag -> reason -> n
    detail_rows = []
    for r in flag_rows:
        start = parse_ts(r.get("start", ""))
        league = r.get("league", "")
        scores = nfl if league.upper() == "NFL" else cfb
        score = lookup(scores, r["away_team"], r["home_team"])
        eid = match_event(events, league, start, r["away_team"], r["home_team"], nfl_map, cfb_map)
        for entry in parse_flags(r.get("flags", "")):
            flagged[entry["flag"]] += 1
            if start and start > now:
                ungraded[entry["flag"]]["not played yet"] += 1
                continue
            if score is None:
                ungraded[entry["flag"]]["no final score"] += 1
                continue
            if eid is None:
                ungraded[entry["flag"]]["no odds feed (SU only)"] += 1
                ev = {"away": r["away_team"], "home": r["home_team"], "last": {}}
            else:
                ev = events[eid]
            side = side_for_flag(entry, r["away_team"], r["home_team"], ev)
            if side is None:
                ungraded[entry["flag"]]["team not resolved"] += 1
                continue
            g = grade_one(ev, side, score)
            if g["spread"] is None and eid is not None:
                ungraded[entry["flag"]]["no closing spread"] += 1
            g.update({"flag": entry["flag"], "team": ev[side], "game": f"{ev['away']} @ {ev['home']}",
                      "detail": entry["detail"]})
            per_flag[entry["flag"]].append(g)
            detail_rows.append(g)

    # ---- (b) RLM retrospective over every kicked-off snapshot event
    def retro(cons_min: float, dk_min: float):
        graded, candidates, unscored = [], 0, 0
        inv = {v: k for k, v in nfl_map.items()}
        for eid, ev in events.items():
            if ev["kickoff"] > now:
                continue
            hit = rlm_retrospective(ev, cons_min, dk_min)
            if hit is None:
                continue
            candidates += 1
            sel, cons, dk = hit
            side = "away" if sel == ev["away"] else ("home" if sel == ev["home"] else None)
            if side is None:
                unscored += 1
                continue
            if ev["sport"].startswith("nfl"):
                score = lookup(nfl, inv.get(ev["away"], ev["away"]), inv.get(ev["home"], ev["home"]))
            else:
                score = lookup(cfb, ev["away"], ev["home"])
            if score is None:
                unscored += 1
                continue
            g = grade_one(ev, side, score)
            g.update({"flag": "RLM(retro)", "team": ev[side], "game": f"{ev['away']} @ {ev['home']}",
                      "detail": f"cons {cons:+.3f} dk {dk:+.3f}", "sport": ev["sport"]})
            graded.append(g)
        return graded, candidates, unscored

    rlm_retro, rlm_candidates, rlm_unscored = retro(RLM_CONS_MIN, RLM_DK_MIN)
    rlm_loose, loose_candidates, loose_unscored = retro(0.0, 0.0)

    # ---- report
    L = [f"# Spot-flags grade — {today}", ""]
    L.append("AQ-168 step 2 · read-only · closing line = last pre-kickoff snapshot row, pinnacle else draftkings "
             "· scores via settle-picks.py (CFBD + nflverse) · ATS/SU are for the flagged team; O/U is the game.")
    L.append("")
    L.append("## Data status")
    L.append("")
    if status["computations"] <= 1:
        L.append(f"- `data/spot-flags.csv` holds **one computation only** ({status['rows']} rows, computed "
                 f"{status['last'] or 'n/a'}). spot-flags.py opens the file with mode `w`, so every daily run "
                 "overwrites the previous day's flags: there is no history to grade beyond the games in the "
                 "latest window. **It should start appending** (one row per game per computation, deduped on "
                 "grade) so this report accumulates a sample; not changed here.")
    else:
        L.append(f"- `data/spot-flags.csv`: {status['rows']} rows across {status['computations']} computations "
                 f"({status['first']} → {status['last']}).")
    kicked = sum(1 for ev in events.values() if ev["kickoff"] <= now)
    L.append(f"- snapshots.csv: {len(events)} events, {kicked} kicked off · scores loaded: {len(cfb)} CFB, {len(nfl)} NFL"
             + (" (network skipped)" if args.no_network else ""))
    L.append("")
    L.append("## Flags as written in spot-flags.csv")
    L.append("")
    L.append("flag | flagged | graded | ATS W-L-P | SU W-L-T | O/U O-U-P | ungraded (reason)")
    L.append("---|---|---|---|---|---|---")
    small = []
    for flag in FLAG_NAMES + tuple(f for f in flagged if f not in FLAG_NAMES):
        gs = per_flag.get(flag, [])
        ug = ", ".join(f"{n} {k}" for k, n in sorted(ungraded.get(flag, {}).items())) or "–"
        graded = sum(1 for g in gs if g["ats"])
        L.append(f"{flag} | {flagged.get(flag, 0)} | {graded} | {record(gs, 'ats', 'WLP')} | "
                 f"{record(gs, 'su', 'WLT')} | {record(gs, 'ou', 'OUP')} | {ug}")
        if graded < MIN_SAMPLE:
            small.append(flag)
    L.append("")
    if small:
        L.append(f"**Sample too small, no weight change** — fewer than {MIN_SAMPLE} graded games for: "
                 f"{', '.join(small)}.")
        L.append("")
    if detail_rows:
        L.append("### Graded games")
        L.append("")
        L.append("flag | team | game | score | spread (book) | ATS | SU | total (book) | O/U")
        L.append("---|---|---|---|---|---|---|---|---")
        for g in detail_rows:
            sp = f"{g['spread']:+g} ({g['spread_book']})" if g["spread"] is not None else "–"
            tt = f"{g['total']:g} ({g['total_book']})" if g["total"] is not None else "–"
            L.append(f"{g['flag']} | {g['team']} | {g['game']} | {g['score']} | {sp} | {g['ats'] or '–'} | "
                     f"{g['su']} | {tt} | {g['ou'] or '–'}")
        L.append("")

    L.append("## RLM, retrospective (same rule as spot-flags.rlm_flags, applied to every kicked-off game in snapshots.csv)")
    L.append("")
    L.append("The rule: DraftKings' implied-probability move (first → last pre-kickoff capture) has the opposite "
             f"sign to the mean move of ≥2 other books, |consensus| ≥ {RLM_CONS_MIN}, |DK| ≥ {RLM_DK_MIN}. "
             "Graded side = the team the consensus moved toward (the 'money' side). Live spot-flags runs see a "
             "shorter window than this full history, so daily flags may differ from these.")
    L.append("")
    graded_retro = sum(1 for g in rlm_retro if g["ats"])
    L.append(f"- candidates: {rlm_candidates} · graded: {graded_retro} · unscored (no final / name mismatch): {rlm_unscored}")
    L.append(f"- money-side ATS **{record(rlm_retro, 'ats', 'WLP')}** · SU {record(rlm_retro, 'su', 'WLT')} · "
             f"game O/U {record(rlm_retro, 'ou', 'OUP')}")
    by_sport = defaultdict(list)
    for g in rlm_retro:
        by_sport[g["sport"]].append(g)
    for sp, gs in sorted(by_sport.items()):
        L.append(f"  - {sp}: ATS {record(gs, 'ats', 'WLP')} · SU {record(gs, 'su', 'WLT')} · O/U {record(gs, 'ou', 'OUP')}")
    if graded_retro < MIN_SAMPLE:
        L.append(f"- **Sample too small, no weight change** ({graded_retro} < {MIN_SAMPLE}).")
    L.append("")
    graded_loose = sum(1 for g in rlm_loose if g["ats"])
    L.append("### Sensitivity: same rule with no magnitude floor (any opposite-sign DK move)")
    L.append("")
    L.append(f"- candidates: {loose_candidates} · graded: {graded_loose} · unscored: {loose_unscored}")
    L.append(f"- money-side ATS **{record(rlm_loose, 'ats', 'WLP')}** · SU {record(rlm_loose, 'su', 'WLT')} · "
             f"game O/U {record(rlm_loose, 'ou', 'OUP')}")
    by_sport_l = defaultdict(list)
    for g in rlm_loose:
        by_sport_l[g["sport"]].append(g)
    for sp, gs in sorted(by_sport_l.items()):
        L.append(f"  - {sp}: ATS {record(gs, 'ats', 'WLP')} · SU {record(gs, 'su', 'WLT')} · O/U {record(gs, 'ou', 'OUP')}")
    if graded_loose < MIN_SAMPLE:
        L.append(f"- **Sample too small, no weight change** ({graded_loose} < {MIN_SAMPLE}).")
    L.append("- The live thresholds fire on roughly 1% of kicked-off games; at that rate the flag cannot reach "
             f"{MIN_SAMPLE} graded games in a season. The floor, not the idea, is what needs a decision.")
    L.append("")
    if rlm_loose:
        L.append("team (money side) | game | score | spread (book) | ATS | SU | total | O/U | moves")
        L.append("---|---|---|---|---|---|---|---|---")
        for g in rlm_loose:
            sp = f"{g['spread']:+g} ({g['spread_book']})" if g["spread"] is not None else "–"
            tt = f"{g['total']:g}" if g["total"] is not None else "–"
            L.append(f"{g['team']} | {g['game']} | {g['score']} | {sp} | {g['ats'] or '–'} | {g['su']} | {tt} | "
                     f"{g['ou'] or '–'} | {g['detail']}")
        L.append("")
    if rlm_retro:
        L.append("team (money side) | game | score | spread (book) | ATS | SU | total | O/U | moves")
        L.append("---|---|---|---|---|---|---|---|---")
        for g in rlm_retro:
            sp = f"{g['spread']:+g} ({g['spread_book']})" if g["spread"] is not None else "–"
            tt = f"{g['total']:g}" if g["total"] is not None else "–"
            L.append(f"{g['team']} | {g['game']} | {g['score']} | {sp} | {g['ats'] or '–'} | {g['su']} | {tt} | "
                     f"{g['ou'] or '–'} | {g['detail']}")
        L.append("")
    L.append("## Gaps")
    L.append("")
    L.append("- Flags file is overwritten daily, so only the current window is gradable; start appending.")
    L.append("- NFL regular-season finals are empty until Week 1 is played; nflverse rows carry no score until then.")
    L.append("- Games outside the odds feed (FCS/D-II) get a straight-up grade only.")
    L.append("- nflverse has no preseason finals, so nfl_preseason snapshot events cannot be graded.")
    L.append("- Pinnacle appears in few snapshot rows; most closing lines here are DraftKings.")
    L.append("- RLM as written encodes only a nickname (last word of the Odds API team name); this script maps it "
             "back to a side through the snapshot event's team names.")
    L.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")

    graded_total = sum(1 for g in detail_rows if g["ats"])
    print(f"spot-flags grade: {len(flag_rows)} flagged rows ({status['computations']} computation(s)) · "
          f"{graded_total} flag-games graded · RLM retro {graded_retro} graded, money-side ATS "
          f"{record(rlm_retro, 'ats', 'WLP')}")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
