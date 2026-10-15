#!/usr/bin/env python3
"""fetch-props.py — rationed player props from The Odds API event endpoint.

    python3 -B 04-agents/football-picks-agent/fetch-props.py                 # today, all leagues
    python3 -B 04-agents/football-picks-agent/fetch-props.py --league nhl --budget 3
    python3 -B 04-agents/football-picks-agent/fetch-props.py --dry-run       # plan only, no credits

WHY (2026-10-07, David: "3 picks a day: ML, over/under, scorer or any of the
best props for each NFL, NCAA and NHL"). Game lines come free (Pinnacle guest
API + DraftKings via ESPN); player props do not. The clean source is The Odds
API's per-event endpoint, billed one credit per market per event, so this is
rationed, never continuous:

  * only games starting on the card date (ET) that have not started, nearest
    start first;
  * one pull per event per day — data/props-<date>.json is the receipt and a
    key already present is never re-pulled;
  * stops when the run budget (PROPS_DAILY_CREDIT_BUDGET) is spent or the
    account would drop under PROPS_MIN_CREDITS_RESERVE, which protects the
    rationed game-line pulls the whole edge process depends on.

Markets per league live in config.env (PROPS_MARKETS_NFL / _NCAAF / _NHL).
Rows land in line-watch/props.csv in the snapshots.csv shape plus `player`,
so football.py prices DraftKings against the other books exactly as it does
game lines. Never places a bet.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import unicodedata
import urllib.error
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
VAULT = AGENT.parents[1]
sys.path.insert(0, str(VAULT / "04-agents" / "lib"))
DATA = AGENT / "data"
PROPS = AGENT / "line-watch" / "props.csv"
CONFIG = AGENT / "config.env"
ET = ZoneInfo("America/New_York")
SPORT_KEYS = {"nfl": "americanfootball_nfl", "ncaaf": "americanfootball_ncaaf", "nhl": "icehockey_nhl"}
SCHEDULES = {"nfl": DATA / "nfl-schedule.csv", "ncaaf": DATA / "cfb-schedule.csv", "nhl": DATA / "nhl-schedule.csv"}
DEFAULT_MARKETS = {"nfl": "player_anytime_td", "ncaaf": "player_anytime_td",
                   "nhl": "player_goal_scorer_anytime,player_shots_on_goal,player_points"}
FIELDS = ["captured_at_et", "sport", "event_id", "commence_time_et", "away_team", "home_team", "book",
          "bookmaker_last_update", "market", "player", "selection", "line_points", "odds_american"]


def load_config() -> dict:
    out = {}
    for name in ("config.example.env", "config.env"):
        p = AGENT / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip() or k.strip() not in out:
                    out[k.strip()] = v.strip()
    return out


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def odds_api():
    """Reuse fetch-odds.py's api_get (key from the environment) and quota writer."""
    spec = importlib.util.spec_from_file_location("fetch_odds", AGENT / "fetch-odds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def todays_games(sport: str, day: dt.date, now: dt.datetime) -> list[dict]:
    path = SCHEDULES[sport]
    if not path.exists():
        return []
    out = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        try:
            kick = dt.datetime.fromisoformat(r["kickoff_et"])
        except (KeyError, ValueError):
            continue
        if kick.astimezone(ET).date() == day and kick > now:
            out.append({"kick": kick, "away": r.get("away_team_full", ""), "home": r.get("home_team_full", "")})
    return sorted(out, key=lambda g: g["kick"])


def match_events(games: list[dict], events: list[dict]) -> list[dict]:
    """Odds API event for each scheduled game: same ET date, both team names."""
    out = []
    for g in games:
        for e in events:
            try:
                kick = dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")).astimezone(ET)
            except (KeyError, ValueError):
                continue
            if kick.date() != g["kick"].astimezone(ET).date():
                continue
            if norm(e.get("home_team")) == norm(g["home"]) and norm(e.get("away_team")) == norm(g["away"]):
                out.append({**g, "event_id": e["id"], "kick": kick})
                break
    return out


def rows_from_event(ev: dict, sport: str, stamp: str) -> list[dict]:
    rows = []
    try:
        kick = dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(ET).isoformat()
    except (KeyError, ValueError):
        return rows
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                if o.get("price") is None or not o.get("description"):
                    continue
                rows.append({
                    "captured_at_et": stamp, "sport": sport, "event_id": ev["id"], "commence_time_et": kick,
                    "away_team": ev.get("away_team", ""), "home_team": ev.get("home_team", ""),
                    "book": b.get("key", ""), "bookmaker_last_update": m.get("last_update", ""),
                    "market": m.get("key", ""), "player": o["description"], "selection": o.get("name", ""),
                    "line_points": "" if o.get("point") is None else f"{float(o['point']):g}",
                    "odds_american": f"{float(o['price']):.0f}",
                })
    return rows


def append_rows(rows: list[dict]) -> None:
    PROPS.parent.mkdir(parents=True, exist_ok=True)
    new = not PROPS.exists() or PROPS.stat().st_size == 0
    with PROPS.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rationed player props for the daily card.")
    ap.add_argument("--league", choices=["nfl", "ncaaf", "nhl", "all"], default="all")
    ap.add_argument("--date", help="card date YYYY-MM-DD (ET); default today")
    ap.add_argument("--budget", type=int, help="credits this run may spend (default PROPS_DAILY_CREDIT_BUDGET)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; spend nothing")
    args = ap.parse_args(argv)

    cfg = load_config()
    for k, v in cfg.items():
        os.environ.setdefault(k, v)
    if not os.environ.get("ODDS_API_KEY"):
        print("props: ODDS_API_KEY is not configured; nothing pulled")
        return 1
    try:
        import davidos_lib as dl
        if not dl.scheduled_run_guard("football-props"):
            print("props: daily guardrail reached; skipping")
            return 3
    except Exception as exc:  # noqa: BLE001
        print(f"props: guardrail unavailable ({exc}); refusing fetch")
        return 1

    now = dt.datetime.now(ET).replace(microsecond=0)
    day = dt.date.fromisoformat(args.date) if args.date else now.date()
    budget = args.budget if args.budget is not None else int(cfg.get("PROPS_DAILY_CREDIT_BUDGET", "6"))
    reserve = int(cfg.get("PROPS_MIN_CREDITS_RESERVE", "60"))
    receipt_path = DATA / f"props-{day.isoformat()}.json"
    receipt = {}
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            receipt = {}
    api = odds_api()
    auth = {"apiKey": os.environ["ODDS_API_KEY"]}
    # Budget is spent in list order, so the first league eats the credits.
    # 2026-10-13 (David: hockey first): follow DAILY_LEAGUE_ORDER from config.
    order = [x.strip().lower() for x in cfg.get("DAILY_LEAGUE_ORDER", "nhl,nfl,cfb").split(",") if x.strip()]
    order = ["ncaaf" if x == "cfb" else x for x in order]
    default_leagues = [x for x in order if x in ("nfl", "ncaaf", "nhl")] or ["nhl", "nfl", "ncaaf"]
    leagues = default_leagues if args.league == "all" else [args.league]
    bookmakers = ",".join(b.strip() for b in cfg.get("ODDS_API_BOOKMAKERS", "draftkings,fanduel,betmgm").split(",") if b.strip())

    plan = []
    for sport in leagues:
        markets = [m.strip() for m in cfg.get(f"PROPS_MARKETS_{sport.upper()}", DEFAULT_MARKETS[sport]).split(",") if m.strip()]
        games = todays_games(sport, day, now)
        if not games or not markets:
            continue
        try:
            events, _ = api.api_get(f"/sports/{SPORT_KEYS[sport]}/events", dict(auth))   # zero-credit listing
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"props {sport}: events listing failed ({exc})")
            continue
        for g in match_events(games, events):
            key = f"{g['away']} @ {g['home']}"
            if key in receipt:
                continue
            plan.append({"sport": sport, "markets": markets, "cost": len(markets), **g, "key": key})
    plan.sort(key=lambda p: p["kick"])

    spent, pulled, rows, notes = 0, 0, [], []
    remaining = None
    stamp = now.isoformat()
    for p in plan:
        if spent + p["cost"] > budget:
            notes.append(f"budget {budget} reached")
            break
        if remaining is not None and remaining - p["cost"] < reserve:
            notes.append(f"reserve {reserve} protected ({remaining} left)")
            break
        if args.dry_run:
            print(f"would pull {p['sport']} {p['key']} ({', '.join(p['markets'])}) = {p['cost']} credit(s)")
            spent += p["cost"]
            continue
        try:
            data, headers = api.api_get(
                f"/sports/{SPORT_KEYS[p['sport']]}/events/{p['event_id']}/odds",
                {**auth, "regions": "us", "markets": ",".join(p["markets"]), "oddsFormat": "american",
                 "bookmakers": bookmakers})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            notes.append(f"{p['key']}: FAILED {exc}")
            continue
        try:
            remaining = int(headers.get("x-requests-remaining", ""))
            spent += int(headers.get("x-requests-last", p["cost"]))
        except ValueError:
            spent += p["cost"]
        api.persist_quota_status(headers, stamp, sport=f"props-{p['sport']}", status="ok")
        receipt[p["key"]] = data
        rows += rows_from_event(data, p["sport"], stamp)
        pulled += 1

    if not args.dry_run and pulled:
        DATA.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        append_rows(rows)
    print(f"props {day}: {pulled} event(s) pulled, {len(rows)} rows, ~{spent} credit(s)"
          + (f", {remaining} remaining" if remaining is not None else "")
          + (f" · planned {len(plan)}" if args.dry_run else "")
          + (" · " + " · ".join(notes) if notes else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
