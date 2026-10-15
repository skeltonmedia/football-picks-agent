#!/usr/bin/env python3
"""price-check.py — is Tuesday's pick still there at today's number?

    python3 -B 04-agents/football-picks-agent/price-check.py            # print
    python3 -B 04-agents/football-picks-agent/price-check.py --write    # also save the md
    python3 -B 04-agents/football-picks-agent/price-check.py --cohort 2026-09-15 --all

WHY (2026-09-18, AQ-222). David: one combined NFL + NCAA card a week. Thursday,
Saturday and Sunday no longer rank anything; they check the Tuesday cohort
against the current DraftKings number and say "still playable at X" or
"number gone". A listed pick never gets replaced here and nothing is logged:
the Tuesday ledger rows stay the only CLV cohort.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402

LEDGER = AGENT / "recommendation-ledger.csv"
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
INJURIES = AGENT / "data" / "injuries.csv"
OUT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"
TRACKED = {"PICK", "LEAN"}
BOOK = "draftkings"
KEY_NUMBERS = {"nfl": (3, 7), "cfb": (3, 7, 10, 14)}


def _grade_clv():
    spec = importlib.util.spec_from_file_location("grade_clv", AGENT / "grade-clv.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def config_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None and (AGENT / "config.env").exists():
        for line in (AGENT / "config.env").read_text().splitlines():
            if line.startswith(name + "="):
                val = line.split("=", 1)[1].strip()
    try:
        return float(val) if val not in (None, "") else default
    except ValueError:
        return default


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def cents(odds: float) -> float:
    """American odds on one 'cents' axis so -105 -> +105 is 10, not 210."""
    return odds - 100.0 if odds > 0 else odds + 100.0


def _norm(s: str) -> str:
    import unicodedata as _ud
    s = _ud.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in s.lower() if c.isalnum())


def latest_tuesday_cohort(rows: list[dict]) -> str | None:
    dates = sorted({r["decision_date"] for r in rows if r.get("decision_date")})
    if not dates:
        return None
    newest = datetime.fromisoformat(dates[-1])
    for d in reversed(dates):
        try:
            day = datetime.fromisoformat(d)
        except ValueError:
            continue
        if day.weekday() == 1 and (newest - day) <= timedelta(days=7):
            return d
    return dates[-1]


def crossed_key(league: str, took: float, cur: float) -> int | None:
    """Key number strictly between the taken and current spread magnitudes."""
    lo, hi = sorted((abs(took), abs(cur)))
    for k in KEY_NUMBERS["nfl" if "nfl" in league.lower() else "cfb"]:
        if lo < k < hi or (lo == k != hi) or (hi == k != lo):
            return k
    return None


def current_offers(event_ids: set[str]) -> dict:
    """(event, market, selection) -> {book: latest row} for the cohort's games."""
    out: dict = {}
    if not SNAPSHOTS.exists():
        return out
    with SNAPSHOTS.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["event_id"] not in event_ids:
                continue
            key = (r["event_id"], r["market"], r["selection"])
            slot = out.setdefault(key, {})
            prev = slot.get(r["book"])
            if prev is None or r["captured_at_et"] > prev["captured_at_et"]:
                slot[r["book"]] = r
    return out


def qb_flags(matchup: str) -> list[str]:
    if not INJURIES.exists():
        return []
    teams = [_norm(t) for t in matchup.split("@")]
    flags = []
    for r in csv.DictReader(INJURIES.open(encoding="utf-8")):
        if (r.get("is_qb") or "").lower() != "true":
            continue
        if (r.get("status") or "").strip().lower() not in {"out", "doubtful"}:
            continue
        team = _norm(r.get("team", ""))
        if any(team and (team in t or t in team) for t in teams if t):
            flags.append(f"QB {r['player']} {r['status']} ({r['team']})")
    return flags


def check(cohort: str | None, include_all: bool, now: datetime) -> tuple[list[dict], str]:
    gc = _grade_clv()
    rows = list(csv.DictReader(LEDGER.open(encoding="utf-8")))
    cohort = cohort or latest_tuesday_cohort(rows)
    picks = [r for r in rows if r.get("decision_date") == cohort
             and (r.get("decision") or "").upper() in TRACKED]
    if not include_all:
        picks = [r for r in picks if not (r.get("result") or "").strip()
                 and (r.get("game_date") or "9999") >= now.date().isoformat()]
    seen: set = set()
    picks = [r for r in picks if not ((r["event_id"], r["market"], r["selection"]) in seen
                                      or seen.add((r["event_id"], r["market"], r["selection"])))]
    offers = current_offers({r["event_id"] for r in picks})
    max_pts = config_float("CHECK_MAX_POINT_LOSS", 1.0)
    max_cents = config_float("CHECK_MAX_PRICE_LOSS_CENTS", 10.0)
    out = []
    for r in picks:
        key = (r["event_id"], r["market"], r["selection"])
        dk = offers.get(key, {}).get(BOOK)
        took_pts, took_odds = _f(r.get("line_points")), _f(r.get("odds_american"))
        item = {"row": r, "current": dk, "reasons": [], "verdict": "no current price"}
        if dk:
            cur_pts, cur_odds = _f(dk.get("line_points")), _f(dk.get("odds_american"))
            # points_in_favor(took, current) > 0 means Tuesday's number was the
            # better one, i.e. today's offer is WORSE and the number is slipping away.
            pts_lost = (gc.points_in_favor(r["market"], r["selection"], took_pts, cur_pts)
                        if r["market"] in ("spreads", "totals") and took_pts is not None and cur_pts is not None else 0.0)
            cents_lost = cents(took_odds) - cents(cur_odds) if took_odds is not None and cur_odds is not None else 0.0
            item.update(pts_lost=pts_lost, cents_lost=cents_lost, cur_pts=cur_pts, cur_odds=cur_odds,
                        age_h=(now - datetime.fromisoformat(dk["captured_at_et"])).total_seconds() / 3600)
            key_hit = (crossed_key(r["league"], took_pts, cur_pts)
                       if r["market"] == "spreads" and took_pts is not None and cur_pts is not None and pts_lost > 0 else None)
            if key_hit:
                item["reasons"].append(f"crossed key number {key_hit}")
            if pts_lost > max_pts:
                item["reasons"].append(f"lost {pts_lost:g} pts")
            if cents_lost > max_cents:
                item["reasons"].append(f"price {int(cents_lost)}¢ worse")
            item["verdict"] = "number gone" if item["reasons"] else "still playable"
        qbs = qb_flags(r.get("matchup", ""))
        item["qb"] = qbs
        if qbs and item["verdict"] == "still playable":
            item["verdict"] = "check QB"
        out.append(item)
    return out, cohort or "none"


def fmt_line(r: dict, pts, odds) -> str:
    if r["market"] == "h2h":
        return f"ML {int(odds):+d}" if odds is not None else "ML ?"
    p = f"{pts:+g}" if r["market"] == "spreads" and pts is not None else (f"{pts:g}" if pts is not None else "?")
    o = f" ({int(odds):+d})" if odds is not None else ""
    return f"{p}{o}"


def render(items: list[dict], cohort: str, now: datetime) -> tuple[str, str]:
    stamp = now.strftime("%a %b %-d, %-I:%M %p ET")
    md = [f"# Price check on the {cohort} card — {stamp}", "",
          "Tuesday's picks against the current DraftKings number. Nothing here is a new pick;",
          "\"number gone\" means do not chase it. Flags come from key numbers, the configured",
          "tolerances in `config.env`, and the injury file's QB statuses.", "",
          "| verdict | pick | took | now | move | flags |", "|---|---|---|---|---|---|"]
    im = [f"Price check · {now.strftime('%a %-m/%-d')} · on the {cohort} card"]
    if not items:
        md.append("| — | no open picks in this cohort | | | | |")
        im.append("No open picks to check.")
    for it in items:
        r, dk = it["row"], it["current"]
        took = fmt_line(r, _f(r.get("line_points")), _f(r.get("odds_american")))
        sel = r["selection"] if r["market"] != "totals" else f"{r['selection']} {r['matchup']}"
        if dk:
            nowv = fmt_line(r, it["cur_pts"], it["cur_odds"])
            move = (f"{-it['pts_lost']:+g} pts" if r["market"] != "h2h" and it["pts_lost"] else "") + (f" {-it['cents_lost']:+.0f}¢" if it["cents_lost"] else "")
            move = move.strip() or "steady"
            age = f" · {it['age_h']:.0f}h old" if it["age_h"] > 6 else ""
        else:
            nowv, move, age = "?", "", ""
        flags = "; ".join(it["reasons"] + it["qb"]) or ""
        md.append(f"| **{it['verdict']}** | {sel} ({r['league']}, {r['market']}) | {took} | {nowv}{age} | {move} | {flags} |")
        mark = {"still playable": "✅", "number gone": "⛔", "check QB": "⚠️"}.get(it["verdict"], "❔")
        im.append(f"{mark} {sel} {took} → now {nowv}" + (f" · {flags}" if flags else ""))
    md += ["", "```", *im, "```", ""]
    return "\n".join(md), "\n".join(im)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", help="decision_date of the Tuesday card; default = latest Tuesday in the ledger")
    ap.add_argument("--all", action="store_true", help="include started and settled rows (for testing)")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    now = now_eastern()
    items, cohort = check(args.cohort, args.all, now)
    md, im = render(items, cohort, now)
    print(im)
    gone = sum(1 for i in items if i["verdict"] == "number gone")
    print(f"\n{len(items)} checked · {gone} number gone · {sum(1 for i in items if i['qb'])} QB flags")
    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"price-check-{now.date().isoformat()}.md"
        path.write_text(md, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
