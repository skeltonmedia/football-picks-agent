#!/usr/bin/env python3
"""Render the football betting dashboard payload from the ledgers. Never hand-type.

    python3 00-system/scripts/refresh-football-dashboard.py           # write payload
    python3 00-system/scripts/refresh-football-dashboard.py --check   # exit 1 on drift

Reads (never writes):
  04-agents/football-picks-agent/staking-config.md        bankroll, unit size, caps
  04-agents/football-picks-agent/bet-ledger.csv           actual wagers → W/L, units, bankroll
  04-agents/football-picks-agent/recommendation-ledger.csv  today's leans/picks/passes
  04-agents/football-picks-agent/line-watch/snapshots.csv   line movement → sharp-money proxy
  04-agents/football-picks-agent/data/{nfl,cfb}-schedule.csv  upcoming slates → matchup cards
  04-agents/football-picks-agent/data/weather.csv             per-game forecast (or honest absence)
  04-agents/football-picks-agent/data/*-team-ratings.csv      prior-season ratings, ALWAYS labeled

Writes:
  06-html/assets/football-betting-data.js                 window.FOOTBALL_DATA payload

Honesty rules baked in, in the spirit of the agent's manifest:
- Sections with no underlying data render as explicit empty states, never sample data.
- "Sharp money" is a line-movement proxy and is labeled as such; with fewer than two
  snapshots per game it says "insufficient snapshots" instead of guessing.
- Fan-team cohort (Ohio State, Titans) is always computed and always shown.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
AGENT = VAULT / "04-agents/football-picks-agent"
OUT = VAULT / "06-html/assets/football-betting-data.js"
sys.path.insert(0, str(VAULT / "04-agents/lib"))
from davidos_lib import now_eastern
from football_performance import latest_recommendations, performance


def performance_config() -> dict:
    text = (VAULT / "03-projects/football-picks-agent/dashboard-metrics.md").read_text(encoding="utf-8")
    block = re.search(r"```json\s*\n(.*?)\n```", text, re.S)
    if not block:
        raise ValueError("Dashboard metric definitions are missing")
    return json.loads(block.group(1))

FAN_RE = re.compile(r"ohio\s*state|buckeyes|\bosu\b|titans|tennessee\s+titans", re.I)


def read_staking() -> dict:
    text = (AGENT / "staking-config.md").read_text(encoding="utf-8")
    def grab(key: str, default: float) -> float:
        m = re.search(rf"\|\s*{key}\s*\|\s*\$?([\d.]+)", text)
        return float(m.group(1)) if m else default
    return {
        "bankroll_start": grab("starting_bankroll", 200.0),
        "unit": grab("unit_size", 2.0),
        "max_day_units": 4, "max_week_units": 10,
        "stop_loss_pct": 20,
    }


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if any(v.strip() for v in row.values())]


def settle(bets: list[dict], cfg: dict) -> dict:
    unit = cfg["unit"]
    w = l = p = pend = 0
    net_units = 0.0
    fan = {"w": 0, "l": 0, "p": 0, "net": 0.0}
    other = {"w": 0, "l": 0, "p": 0, "net": 0.0}
    week_units = season_units = 0.0
    today = now_eastern().date()
    iso_week = today.isocalendar()[:2]
    for b in bets:
        stake = float(b.get("stake_units") or 1)
        season_units += stake
        try:
            placed = datetime.strptime(b["placed_at_et"][:10], "%Y-%m-%d").date()
            if placed.isocalendar()[:2] == iso_week:
                week_units += stake
        except (ValueError, KeyError):
            pass
        res = (b.get("result") or "").strip().lower()
        odds = int(b.get("odds_american") or -110)
        cohort = fan if (b.get("fan_team", "").upper() == "YES") else other
        if res == "win":
            gain = stake * (odds / 100 if odds > 0 else 100 / abs(odds))
            net_units += gain; cohort["net"] += gain; cohort["w"] += 1; w += 1
        elif res == "loss":
            net_units -= stake; cohort["net"] -= stake; cohort["l"] += 1; l += 1
        elif res == "push":
            cohort["p"] += 1; p += 1
        else:
            pend += 1
    return {
        "record": {"w": w, "l": l, "push": p, "pending": pend},
        "net_units": round(net_units, 2),
        "net_dollars": round(net_units * unit, 2),
        "bankroll": round(cfg["bankroll_start"] + net_units * unit, 2),
        "units_week": week_units, "units_season": season_units,
        "fan": fan, "other": other,
    }


def today_board(recs: list[dict], links: dict | None = None) -> dict:
    """Leans, upsets, and plus-money chances from the recommendation ledger.

    Each entry gets add-to-slip links when the newest odds capture has this
    exact selection at DK/FD. The slip pre-fills; David still taps place —
    and Pikkit picks the bet up from account sync after placement."""
    leans, upsets, plus_money = [], [], []
    today = now_eastern().date().isoformat()
    for r in latest_recommendations(recs):
        if (r.get("decision") or "").strip().upper() not in {"PICK", "LEAN"}:
            continue
        if r.get("game_date") != today or r.get("result"):
            continue
        entry = {
            "matchup": r.get("matchup", ""), "market": r.get("market", ""),
            "selection": r.get("selection", ""), "odds": r.get("odds_american", ""),
            "line_points": r.get("line_points", ""),
            "edge": r.get("edge_points_or_pct", ""), "confidence": r.get("confidence", ""),
            "reason": (r.get("reason") or "")[:140],
            "fan_team": bool(FAN_RE.search(r.get("matchup", "") + " " + r.get("selection", ""))),
        }
        if links:
            sel, mkt = entry["selection"].strip().lower(), entry["market"].strip().lower()
            mkt_key = {"spread": "spreads", "moneyline": "h2h", "total": "totals"}.get(mkt, mkt)
            hit = {}
            for (away, home, m, s), books in links["outcomes"].items():
                if m == mkt_key and (s == sel or (sel and sel in s) or (s and s in sel)):
                    if away in entry["matchup"].lower() or home in entry["matchup"].lower() or not entry["matchup"]:
                        hit = books
                        break
            entry["slip_links"] = {b: v["link"] for b, v in hit.items()}
            entry["live_odds"] = {b: v["odds"] for b, v in hit.items()}
        leans.append(entry)
        try:
            if int(r.get("odds_american") or 0) > 0:
                plus_money.append(entry)
                if "moneyline" in (r.get("market") or "").lower():
                    upsets.append(entry)
        except ValueError:
            pass
    return {"leans": leans, "upsets": upsets, "plus_money": plus_money}


def sharp_proxy(snaps: list[dict]) -> dict:
    """Line movement between captures, per (game, book, market, selection).

    Rewritten 2026-08-16: the first version keyed off a `matchup` column that
    snapshots.csv never had (rows carry away_team/home_team) — 7,500 rows in the
    file, 0 games tracked. Spreads/totals compare line_points; moneylines
    compare American odds. A proxy either way, and labeled as one.
    """
    series = defaultdict(list)
    for s in snaps:
        away, home = s.get("away_team", ""), s.get("home_team", "")
        if not away or not home:
            continue
        key = (f"{away} @ {home}", s.get("book", ""), s.get("market", ""), s.get("selection", ""))
        series[key].append(s)
    games = {k[0] for k in series}
    moves = []
    for (matchup, book, market, sel), rows in series.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r.get("captured_at_et", ""))
        field = "odds_american" if market == "h2h" else "line_points"
        def val(row):
            try:
                return float(row.get(field) or "")
            except ValueError:
                return None
        first, last = val(rows[0]), val(rows[-1])
        if first is None or last is None or first == last:
            continue
        if market == "h2h":
            # Rank moneyline moves by implied-probability change, and skip
            # unbettable extremes (-20000 favorites drifting to -100000 is
            # noise, not sharps). ±2000 is already a 95% implied line.
            if abs(first) > 2000 or abs(last) > 2000:
                continue
            imp = lambda o: (abs(o) / (abs(o) + 100)) if o < 0 else (100 / (o + 100))
            weight = abs(imp(last) - imp(first)) * 100  # prob points
            if weight < 1.5:
                continue
        else:
            weight = abs(last - first)
        moves.append({
            "matchup": f"{matchup} — {sel}" + (f" ({book})" if book else ""),
            "open": first, "now": last, "move": round(last - first, 1),
            "snapshots": len(rows), "kind": "odds" if market == "h2h" else "points",
            "weight": round(weight, 2),
        })
    moves.sort(key=lambda m: m["weight"], reverse=True)
    return {"tracked_games": len(games), "moves": moves[:8],
            "sufficient": any(len(r) >= 2 for r in series.values())}


def build_links_index(snaps: list[dict]) -> dict:
    """Bet-slip deep links from the NEWEST odds capture, DK + FD only.

    Two indexes:
      events[(away_full, home_full)][book]                    -> event page link
      outcomes[(away_full, home_full, market, selection)][book] -> add-to-slip link
    Older captures are ignored so links never point at moved/expired markets.
    """
    if not snaps:
        return {"events": {}, "outcomes": {}, "captured": ""}
    # Newest capture PER SPORT — the NFL and NCAAF pulls run as separate script
    # invocations minutes apart, and a single global "latest" silently dropped
    # whichever sport was pulled first (found 2026-08-16: 0 NCAA links joined).
    latest_by_sport: dict = {}
    for s in snaps:
        sp = s.get("sport", "")
        if s.get("captured_at_et", "") > latest_by_sport.get(sp, ""):
            latest_by_sport[sp] = s.get("captured_at_et", "")
    norm = lambda s: (s or "").strip().lower()
    events, outcomes = {}, {}
    for s in snaps:
        if (s.get("captured_at_et") != latest_by_sport.get(s.get("sport", ""))
                or s.get("book") not in ("draftkings", "fanduel")):
            continue
        gk = (norm(s.get("away_team")), norm(s.get("home_team")))
        if s.get("event_link"):
            events.setdefault(gk, {})[s["book"]] = s["event_link"]
        if s.get("outcome_link"):
            ok = gk + (norm(s.get("market")), norm(s.get("selection")))
            outcomes.setdefault(ok, {})[s["book"]] = {
                "link": s["outcome_link"], "odds": s.get("odds_american", ""),
                "points": s.get("line_points", ""),
            }
    return {"events": events, "outcomes": outcomes, "captured": max(latest_by_sport.values())[:16] if latest_by_sport else ""}


def line_shop(snaps: list[dict]) -> dict:
    """Same bet, different price: where books disagree on an identical selection.

    Compares the NEWEST capture per sport, same market AND same points (a -2.5
    at one book vs -3 at another is a different bet, not a price gap). Gap is
    measured in implied-probability points; sorted by what shopping is worth.
    """
    latest_by_sport: dict = {}
    for s in snaps:
        sp = s.get("sport", "")
        if s.get("captured_at_et", "") > latest_by_sport.get(sp, ""):
            latest_by_sport[sp] = s.get("captured_at_et", "")
    def imp(o: float) -> float:
        return (abs(o) / (abs(o) + 100)) if o < 0 else (100 / (o + 100))
    offers = defaultdict(list)
    for s in snaps:
        if s.get("captured_at_et") != latest_by_sport.get(s.get("sport", "")):
            continue
        try:
            odds = float(s.get("odds_american") or "")
        except ValueError:
            continue
        if abs(odds) > 2000:
            continue
        key = (s.get("sport", ""), f"{s.get('away_team','')} @ {s.get('home_team','')}",
               s.get("market", ""), s.get("selection", ""), s.get("line_points", ""))
        offers[key].append({"book": s.get("book", ""), "odds": odds,
                            "link": s.get("outcome_link", "")})
    rows = []
    for (sport, matchup, market, sel, pts), books in offers.items():
        if len({b["book"] for b in books}) < 2:
            continue
        best = max(books, key=lambda b: b["odds"])
        worst = min(books, key=lambda b: b["odds"])
        gap = (imp(worst["odds"]) - imp(best["odds"])) * 100
        if gap < 1.0:
            continue
        rows.append({
            "sport": "NFL" if sport.startswith("nfl") else "NHL" if sport == "nhl" else "NCAA",
            "matchup": matchup, "market": market, "selection": sel,
            "points": pts, "gap_pct": round(gap, 1),
            "best": {"book": best["book"], "odds": int(best["odds"]), "link": best["link"]},
            "others": [{"book": b["book"], "odds": int(b["odds"])}
                       for b in sorted(books, key=lambda x: -x["odds"])[1:3]],
        })
    rows.sort(key=lambda r: r["gap_pct"], reverse=True)
    return {"rows": rows[:10], "compared": len(offers)}


def build_slates(links: dict | None = None) -> dict:
    """Matchup cards per league. Ratings are PRIOR SEASON and labeled as such —
    the agent's own run-log lesson (unqualified 2025 SRS on preseason games)."""
    data = AGENT / "data"
    weather = {}
    for w in read_csv(data / "weather.csv"):
        weather[(w.get("league", ""), w.get("matchup", ""))] = w

    nfl_r = {r["team"]: r for r in read_csv(data / "nfl-team-ratings.csv")}
    cfb_r = {r["team"]: r for r in read_csv(data / "cfb-team-ratings.csv")}

    def rating_for(league, key):
        src, field = (nfl_r, "srs_rating") if league == "NFL" else (cfb_r, "sp_plus_rating")
        row = src.get(key)
        if not row:
            return None
        try:
            return {"rating": float(row[field]), "rank": int(float(row.get(field.replace("_rating", "_ranking")) or 0)),
                    "year": row.get("data_year", "")}
        except (ValueError, KeyError):
            return None

    def cards(path, league):
        rows = read_csv(path)
        captured = rows[0].get("captured_at_et", "")[:10] if rows else ""
        out = []
        today = datetime.now().strftime("%Y-%m-%d")
        for g in rows:
            kick = g.get("kickoff_et", "")
            if kick[:10] < today:
                continue
            away_key = g.get("away_team_school") or g.get("away_team", "")
            home_key = g.get("home_team_school") or g.get("home_team", "")
            w = weather.get((league, g.get("matchup", "")), {})
            has_forecast = bool(w.get("temperature_f"))
            indoor = (g.get("indoor", "") or "").strip().lower() == "true"
            card = {
                "matchup": g.get("matchup", ""),
                "away": g.get("away_team_full", away_key), "home": g.get("home_team_full", home_key),
                "kickoff": kick, "venue": g.get("venue", ""),
                "city": ", ".join(x for x in (g.get("city", ""), g.get("state", "")) if x),
                "indoor": indoor, "week": g.get("week_number", ""), "season_type": g.get("season_type", ""),
                "away_rating": rating_for(league, away_key), "home_rating": rating_for(league, home_key),
                "fan_team": bool(FAN_RE.search(g.get("matchup", "") + " " + g.get("away_team_full", "") + " " + g.get("home_team_full", ""))),
                "weather": ({"temp": w.get("temperature_f"), "wind": w.get("wind", ""),
                             "precip": w.get("precip_probability", ""), "short": w.get("short_forecast", "")}
                            if has_forecast else None),
            }
            r_a, r_h = card["away_rating"], card["home_rating"]
            if r_a and r_h:
                card["rating_gap"] = round(r_h["rating"] - r_a["rating"], 1)
            if links:
                gk = (card["away"].strip().lower(), card["home"].strip().lower())
                card["book_links"] = links["events"].get(gk, {})
            out.append(card)
        out.sort(key=lambda c: (c["kickoff"], c["matchup"]))
        return {"captured": captured, "games": out[:60],
                "ratings_note": "Ratings are PRIOR-SEASON (2025) power numbers — context, not a prediction for this game."}

    return {"NFL": cards(data / "nfl-schedule.csv", "NFL"),
            "NCAA": cards(data / "cfb-schedule.csv", "NCAA")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    cfg = read_staking()
    bets = read_csv(AGENT / "bet-ledger.csv")
    recs = read_csv(AGENT / "recommendation-ledger.csv")
    snaps = read_csv(AGENT / "line-watch" / "snapshots.csv")

    settled = settle(bets, cfg)
    links = build_links_index(snaps)
    week = performance(recs, now_eastern().date(), **performance_config())
    week["ledger_modified"] = datetime.fromtimestamp((AGENT / "recommendation-ledger.csv").stat().st_mtime, tz=now_eastern().tzinfo).isoformat(timespec="minutes")
    payload = {
        "generated": now_eastern().strftime("%Y-%m-%d %H:%M ET"),
        "config": cfg,
        "ledger": settled,
        "performance": week,
        "board": today_board(recs, links),
        "slates": build_slates(links),
        "line_shop": line_shop(snaps),
        "links_captured": links["captured"],
        "sharp": sharp_proxy(snaps),
        "stop_loss_floor": round(cfg["bankroll_start"] * (1 - cfg["stop_loss_pct"] / 100), 2),
        "counts": {"bets_logged": len(bets), "recommendations": len(recs)},
    }
    body = "window.FOOTBALL_DATA = " + json.dumps(payload, indent=2) + ";\n"

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        # Compare everything except the timestamp line.
        strip = lambda t: re.sub(r'"generated": "[^"]*"', '', t)
        if strip(current) == strip(body):
            print("OK: dashboard payload matches ledgers")
            return 0
        print("STALE: dashboard payload does not match ledgers", file=sys.stderr)
        return 1

    OUT.write_text(body, encoding="utf-8")
    r = settled["record"]
    print(f"UPDATED: {r['w']}-{r['l']}-{r['push']} ({r['pending']} pending), "
          f"net {settled['net_units']:+.2f}u (${settled['net_dollars']:+.2f}), "
          f"bankroll ${settled['bankroll']:.2f}, "
          f"{len(payload['board']['leans'])} live leans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
