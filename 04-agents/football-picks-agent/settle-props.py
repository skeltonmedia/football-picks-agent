#!/usr/bin/env python3
"""settle-props.py — grade player-prop ledger rows from ESPN box scores (keyless).

    python3 -B 04-agents/football-picks-agent/settle-props.py            # report only
    python3 -B 04-agents/football-picks-agent/settle-props.py --write    # write results

WHY (2026-10-07). The daily card logs anytime-scorer and skater props next to
game lines, and settle-picks.py only knows final scores. ESPN's public summary
endpoint carries the official box score: NHL skater goals/assists/shots, and
football scoring plays with the scorer's name. Only STATUS_FINAL games grade;
everything else stays pending. Names match exactly after normalising, else by
last name + first initial when that is unique ("Zachary" vs "Zack" Bolduc).

Markets: player_anytime_td, player_goal_scorer_anytime (Yes -> W if the player
scored), player_shots_on_goal / player_points (Over/Under vs line, push on the
number). A player missing from the box score of a final game is an L for a
"Yes" (he did not score) and stays pending for Over/Under (scratched players
are voided by the book, which this script will not guess).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
LEDGER = AGENT / "recommendation-ledger.csv"
DATA = AGENT / "data"
ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
ESPN = "https://site.web.api.espn.com/apis/site/v2/sports"
SPORT_PATH = {"NFL": "football/nfl", "CFB": "football/college-football", "NHL": "hockey/nhl"}
SCHEDULES = {"NFL": DATA / "nfl-schedule.csv", "CFB": DATA / "cfb-schedule.csv", "NHL": DATA / "nhl-schedule.csv"}
PROP_MARKETS = {"player_anytime_td", "player_goal_scorer_anytime", "player_shots_on_goal", "player_points",
                "player_assists"}
STAT_KEY = {"player_shots_on_goal": "shotsTotal", "player_points": "points", "player_assists": "assists"}
TD_RE = re.compile(r"^(?P<name>.+?)\s+\d+\s*Yd\b", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def split_selection(selection: str) -> tuple[str, str]:
    """'Nathan MacKinnon Yes' -> ('Nathan MacKinnon', 'yes'); Over/Under likewise."""
    parts = (selection or "").strip().rsplit(" ", 1)
    if len(parts) == 2 and parts[1].lower() in ("yes", "over", "under"):
        return parts[0], parts[1].lower()
    return selection.strip(), "yes"


def match_name(name: str, table: dict) -> str | None:
    """Exact normalised match, else unique last-name + first-initial match."""
    n = norm(name)
    if n in table:
        return n
    toks = n.split()
    if not toks:
        return None
    hits = [k for k in table if k.split()[-1:] == toks[-1:] and k[:1] == n[:1]]
    return hits[0] if len(hits) == 1 else None


# --- box score readers -------------------------------------------------------

def nhl_stats(summary: dict) -> dict:
    """player -> {'goals','assists','points','shotsTotal'} from the skaters table."""
    out = {}
    for team in (summary.get("boxscore") or {}).get("players", []):
        for block in team.get("statistics", []):
            if block.get("name") != "skaters":
                continue
            keys = block.get("keys") or []
            for a in block.get("athletes", []):
                stats = dict(zip(keys, a.get("stats") or []))
                name = (a.get("athlete") or {}).get("displayName", "")
                if not name:
                    continue
                def num(k):
                    try:
                        return float(stats.get(k, "0") or 0)
                    except ValueError:
                        return 0.0
                g, as_ = num("goals"), num("assists")
                out[norm(name)] = {"goals": g, "assists": as_, "points": g + as_, "shotsTotal": num("shotsTotal")}
    return out


def td_scorers(summary: dict) -> dict:
    """player -> touchdowns from scoringPlays (receiver on a pass, runner on a rush, returner on a return)."""
    out: dict = {}
    for p in summary.get("scoringPlays", []):
        if ((p.get("scoringType") or {}).get("name") or "").lower() != "touchdown":
            continue
        text = (p.get("text") or "").strip()
        m = TD_RE.match(text)
        name = m.group("name") if m else re.split(r"\s+\d", text, 1)[0]
        if name:
            out[norm(name)] = out.get(norm(name), 0) + 1
    return out


def game_final(summary: dict) -> bool:
    comps = ((summary.get("header") or {}).get("competitions")) or []
    status = ((comps[0].get("status") or {}).get("type") or {}) if comps else {}
    return status.get("name") == "STATUS_FINAL" or bool(status.get("completed"))


# --- event lookup -------------------------------------------------------------

def espn_event_id(league: str, game_date: str, away: str, home: str) -> str | None:
    path = SCHEDULES.get(league)
    if path and path.exists():
        for r in csv.DictReader(path.open(encoding="utf-8")):
            if (r.get("kickoff_et") or "")[:10] != game_date:
                continue
            if norm(r.get("home_team_full")) == norm(home) and norm(r.get("away_team_full")) == norm(away):
                return r.get("event_id")
    try:
        day = dt.date.fromisoformat(game_date)
        extra = "&groups=80&limit=300" if league == "CFB" else "&limit=100"
        board = get_json(f"{ESPN}/{SPORT_PATH[league]}/scoreboard?dates={day:%Y%m%d}{extra}")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    for e in board.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): (c.get("team") or {}).get("displayName", "") for c in comp.get("competitors", [])}
        if norm(sides.get("home")) == norm(home) and norm(sides.get("away")) == norm(away):
            return e.get("id")
    return None


def grade(row: dict, summary: dict) -> str | None:
    market = (row.get("market") or "").lower()
    player, side = split_selection(row.get("selection", ""))
    if market in ("player_anytime_td", "player_goal_scorer_anytime"):
        table = td_scorers(summary) if market == "player_anytime_td" else {k: v["goals"] for k, v in nhl_stats(summary).items()}
        key = match_name(player, table)
        scored = (table.get(key, 0) if key else 0) >= 1
        return "W" if scored else "L"
    stat = STAT_KEY.get(market)
    if not stat:
        return None
    table = nhl_stats(summary)
    key = match_name(player, table)
    if key is None:
        return None
    try:
        line = float(row.get("line_points") or "")
    except ValueError:
        return None
    val = table[key][stat]
    if val == line:
        return "PUSH"
    if side == "over":
        return "W" if val > line else "L"
    return "W" if val < line else "L"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--days", type=int, default=10, help="look back this many days for pending rows")
    args = ap.parse_args(argv)
    if not LEDGER.exists():
        print("no ledger")
        return 0
    with LEDGER.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows, cols = list(reader), list(reader.fieldnames or [])
    today = dt.datetime.now(ET).date()
    pending = [r for r in rows if (r.get("market") or "").lower() in PROP_MARKETS and not r.get("result")
               and r.get("game_date") and today - dt.timedelta(days=args.days) <= dt.date.fromisoformat(r["game_date"]) <= today]
    if not pending:
        print("settle-props: no pending prop rows")
        return 0
    summaries: dict = {}
    settled, waiting, unmatched = 0, 0, 0
    for r in pending:
        league = (r.get("league") or "").upper()
        if league not in SPORT_PATH or " @ " not in (r.get("matchup") or ""):
            continue
        away, home = r["matchup"].split(" @ ", 1)
        ck = (league, r["game_date"], away, home)
        if ck not in summaries:
            eid = espn_event_id(league, r["game_date"], away, home)
            try:
                summaries[ck] = get_json(f"{ESPN}/{SPORT_PATH[league]}/summary?event={eid}") if eid else None
            except (urllib.error.URLError, OSError, ValueError):
                summaries[ck] = None
        s = summaries[ck]
        if not s:
            unmatched += 1
            continue
        if not game_final(s):
            waiting += 1
            continue
        res = grade(r, s)
        if res is None:
            unmatched += 1
            continue
        r["result"] = res
        settled += 1
    if args.write and settled:
        with LEDGER.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    print(f"settle-props: {settled} graded · {waiting} not final · {unmatched} unmatched"
          + ("" if args.write else " (dry run; add --write)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
