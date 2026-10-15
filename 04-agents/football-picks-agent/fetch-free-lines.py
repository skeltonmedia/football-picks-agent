#!/usr/bin/env python3
"""fetch-free-lines.py — continuous Pinnacle + DraftKings lines for $0.

    python3 -B 04-agents/football-picks-agent/fetch-free-lines.py            # one pull, both leagues
    python3 -B 04-agents/football-picks-agent/fetch-free-lines.py --dry-run  # print, write nothing

WHY (2026-09-28). David: "not going to pay for data, get creative." The paid
Odds API tier would have bought near-continuous line tracking. Two keyless
public endpoints carry the two books that matter to the process:

  Pinnacle  guest.api.arcadia.pinnacle.com — the sharp reference the whole
            edge is measured against (NFL league 889, NCAA 880); full-game
            spread, total, moneyline with home/away/over/under designations.
  DraftKings via ESPN's scoreboard (site.web.api.espn.com), which embeds DK's
            spread, total and moneyline for every NFL and FBS game — the book
            David bets at, the price the ledger freezes.

Rows are written in the snapshots.csv schema so football.py can merge them
with the rationed Odds API log:

  line-watch/free-lines.csv         change-only history (small, append-only)
  line-watch/free-lines-latest.csv  the full latest pull (overwritten): a book
                                    absent here is not offering that market now
  line-watch/snapshots.csv          ALSO receives each distinct retrieval for games kicking within
                                    CLOSE_WINDOW_MIN, so Pinnacle and DK closes
                                    reach grade-clv.py without a paid capture

Event ids map to the Odds API's ids through data/event-index.json (built from
snapshots.csv + schedules, refreshed when stale) so both feeds land on the same
Event. Unmatched team names are logged to line-watch/free-lines-unmatched.txt;
fix them in data/team-aliases.csv (content lives in files, not code).

`captured_at_et` is retrieval time, NOT the bookmaker's update time. A missing
provider update stays blank and requires direct book confirmation before staking.
Near-kickoff unchanged prices are observations, not proof a cached quote is live.

Read-only GETs, one request per league per source, every 30 minutes at most.
No API key. Never places a bet.
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
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
VAULT = AGENT.parents[1]
sys.path.insert(0, str(VAULT / "04-agents" / "lib"))
LW = AGENT / "line-watch"
SNAPSHOTS = LW / "snapshots.csv"
HISTORY = LW / "free-lines.csv"
LATEST = LW / "free-lines-latest.csv"
UNMATCHED = LW / "free-lines-unmatched.txt"
INDEX = AGENT / "data" / "event-index.json"
ALIASES = AGENT / "data" / "team-aliases.csv"
SCHEDULES = {"nfl": AGENT / "data" / "nfl-schedule.csv", "ncaaf": AGENT / "data" / "cfb-schedule.csv",
             "nhl": AGENT / "data" / "nhl-schedule.csv",
             # soccer has no schedule file: ESPN's names are the universe (see fetch_all)
             "epl": AGENT / "data" / "epl-schedule.csv", "laliga": AGENT / "data" / "laliga-schedule.csv",
             "ucl": AGENT / "data" / "ucl-schedule.csv"}
ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

FIELDS = ["captured_at_et", "sport", "event_id", "commence_time_et", "away_team", "home_team", "book",
          "bookmaker_last_update", "market", "selection", "line_points", "odds_american",
          "event_link", "outcome_link"]
PIN_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"          # Pinnacle's public guest key (in their web client)
# 2026-10-07: NHL (Pinnacle league 1456) joins both feeds. Pinnacle's period-0
# hockey markets are the full game including OT/shootout, which is also what
# DraftKings' moneyline and total settle on, so the same parser applies; the
# hockey "spread" is the puck line. ESPN's NHL scoreboard takes a date range
# instead of a week number (see fetch_all).
# 2026-10-14 (David: "I like the top leagues including la liga premier and champions
# league"): three soccer leagues. Moneylines are three-way (home/draw/away); the
# fetcher stores the Draw row, and football.py keeps soccer off the gate until
# its three-way math lands (AQ-298).
PIN_LEAGUES = {"nfl": 889, "ncaaf": 880, "nhl": 1456, "epl": 1980, "laliga": 2196, "ucl": 2627}
SOCCER = {"epl", "laliga", "ucl"}
PIN_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
ESPN = {"nfl": "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        "ncaaf": "https://site.web.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=200",
        "nhl": "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?limit=100",
        "epl": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?limit=100",
        "laliga": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard?limit=100",
        "ucl": "https://site.web.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard?limit=100"}
DATE_RANGE_SPORTS = {"nhl", "epl", "laliga", "ucl"}   # ESPN scoreboards queried by dates=YYYYMMDD (one GET per day), not week
DAILY_DAYS_AHEAD = 2                 # today + 2: DraftKings posts hockey lines a day or two out
DAYS_AHEAD = {"epl": 7, "laliga": 7, "ucl": 7}        # soccer lines post a week out; weekend slates need the reach
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
HORIZON_DAYS = 8
CLOSE_WINDOW_MIN = 75
INDEX_MAX_AGE_H = 6
TIMEOUT = 20


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def load_aliases() -> dict[str, str]:
    out = {}
    if ALIASES.exists():
        for r in csv.DictReader(ALIASES.open(encoding="utf-8")):
            if r.get("alias") and r.get("canonical"):
                out[norm(r["alias"])] = r["canonical"]
    return out


class Names:
    """Resolve a source's team label to the Odds API full name for a league."""

    def __init__(self, sport: str, aliases: dict[str, str], universe: list[str] | None = None):
        self.sport = sport
        self.aliases = aliases
        self.full: set[str] = set(universe or [])
        self.school_to_full: dict[str, str] = {}
        self.abbr_to_full: dict[str, str] = {}
        path = SCHEDULES[sport]
        if path.exists():
            for r in csv.DictReader(path.open(encoding="utf-8")):
                for side in ("home", "away"):
                    full = r.get(f"{side}_team_full") or ""
                    if not full:
                        continue
                    self.full.add(full)
                    school = r.get(f"{side}_team_school")
                    if school:
                        self.school_to_full[norm(school)] = full
                    abbr = r.get(f"{side}_team")
                    if abbr:
                        self.abbr_to_full[norm(abbr)] = full
        self.full_norm = {norm(f): f for f in self.full}
        self.unmatched: set[str] = set()

    def resolve(self, label: str) -> str | None:
        n = norm(label)
        if n in self.aliases:
            canon = self.aliases[n]
            return canon if canon in self.full or not self.full else canon
        if n in self.full_norm:
            return self.full_norm[n]
        if n in self.school_to_full:
            return self.school_to_full[n]
        if n in self.abbr_to_full:
            return self.abbr_to_full[n]
        # exact school prefix of exactly one full name ("penn state" -> "Penn State Nittany Lions")
        hits = [f for k, f in self.full_norm.items() if k.startswith(n + " ")]
        if len(hits) == 1:
            return hits[0]
        if self.sport in SOCCER and self.full:
            # "Bournemouth" vs "AFC Bournemouth", "Atletico Madrid" vs "Atlético Madrid": one
            # containment hit in the day's ESPN universe is the match; two is ambiguous.
            toks = [t for t in n.split() if t not in ("fc", "cf", "afc", "sc", "ac", "club", "de", "club")]
            hits = [f for k, f in self.full_norm.items() if n in k or k in n or (toks and all(t in k.split() for t in toks))]
            if len(hits) == 1:
                return hits[0]
        if not self.full:            # no schedule on disk: pass the label through
            return label
        self.unmatched.add(label)
        return None


# ---------------------------------------------------------------------------
# event index: exact sport, kick date ET, away and home -> Odds API event id
# ---------------------------------------------------------------------------

def build_index() -> dict:
    """Event ids plus the universe of team names the Odds API has ever used,
    so a Pinnacle 'Kentucky' resolves to 'Kentucky Wildcats' even in a week
    the schedule file does not list."""
    idx: dict[str, str] = {}
    teams: dict[str, set] = {sport: set() for sport in PIN_LEAGUES}
    if SNAPSHOTS.exists():
        with SNAPSHOTS.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                sport = r.get("sport")
                if sport not in PIN_LEAGUES:
                    continue
                teams[sport].add(r.get("home_team") or "")
                teams[sport].add(r.get("away_team") or "")
                try:
                    kick = dt.datetime.fromisoformat(r["commence_time_et"]).astimezone(ET)
                except (KeyError, ValueError):
                    continue
                key = f"{sport}|{kick.date().isoformat()}|{norm(r['away_team'])}|{norm(r['home_team'])}"
                idx.setdefault(key, r["event_id"])
    return {"schema_version": 2, "built_at": dt.datetime.now(ET).isoformat(), "index": idx,
            "teams": {k: sorted(t for t in v if t) for k, v in teams.items()}}


def load_index() -> dict:
    if INDEX.exists():
        try:
            d = json.loads(INDEX.read_text(encoding="utf-8"))
            age = dt.datetime.now(ET) - dt.datetime.fromisoformat(d["built_at"])
            if age < dt.timedelta(hours=INDEX_MAX_AGE_H) and "teams" in d and d.get("schema_version") == 2:
                return d
        except (ValueError, KeyError):
            pass
    d = build_index()
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(d, indent=0), encoding="utf-8")
    return d


def schedule_weeks(sport: str, now: dt.datetime, horizon: dt.datetime) -> list[int]:
    """ESPN week numbers for games in the window, from our own schedule file."""
    weeks = set()
    path = SCHEDULES[sport]
    if path.exists():
        for r in csv.DictReader(path.open(encoding="utf-8")):
            try:
                kick = dt.datetime.fromisoformat(r["kickoff_et"])
                wk = int(r["week_number"])
            except (KeyError, ValueError):
                continue
            if now - dt.timedelta(days=1) <= kick <= horizon:
                weeks.add(wk)
                weeks.add(wk + 1)
    return sorted(weeks)


def event_id_for(index: dict, sport: str, kick: dt.datetime, home_full: str, away_full: str) -> str:
    k = kick.astimezone(ET)
    key = f"{sport}|{k.date().isoformat()}|{norm(away_full)}|{norm(home_full)}"
    if key in index:
        return index[key]
    return f"free:{sport}:{k.date().isoformat()}:{norm(away_full)}:{norm(home_full)}".replace(" ", "-")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def get_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def iso_et(value: str) -> dt.datetime:
    v = value.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(v)
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(ET)


def provider_update(payload: dict, now: dt.datetime) -> str:
    """Only explicit quote-level update fields, never event dates or retrieval time.

    Neither adapter requires providers to supply this metadata. Missing, malformed,
    timezone-less, or future values are unknown rather than manufactured freshness.
    """
    for key in ("lastUpdated", "lastUpdate", "updatedAt"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        try:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is not None and stamp <= now:
            return stamp.astimezone(ET).isoformat()
    return ""


def parse_pinnacle(matchups: list, markets: list, sport: str, names: Names, index: dict,
                   now: dt.datetime, horizon: dt.datetime) -> list[dict]:
    """Full-game, non-alternate spread/total/moneyline for parent matchups."""
    meta = {}
    for m in matchups:
        if m.get("type") != "matchup" or m.get("parent") or not m.get("participants"):
            continue
        sides = {p.get("alignment"): p.get("name") for p in m["participants"]}
        if "home" not in sides or "away" not in sides:
            continue
        try:
            kick = iso_et(m["startTime"])
        except (KeyError, ValueError):
            continue
        if not (now < kick <= horizon):
            continue
        home, away = names.resolve(sides["home"]), names.resolve(sides["away"])
        if not home or not away:
            continue
        meta[m["id"]] = {"kick": kick, "home": home, "away": away,
                         "event_id": event_id_for(index, sport, kick, home, away)}
    rows = []
    stamp = now.isoformat()
    for mk in markets:
        if mk.get("period") != 0 or mk.get("isAlternate") or mk.get("status") not in (None, "open"):
            continue
        ev = meta.get(mk.get("matchupId"))
        if ev is None:
            continue
        typ = mk.get("type")
        market = {"spread": "spreads", "total": "totals", "moneyline": "h2h"}.get(typ)
        if market is None:
            continue
        for p in mk.get("prices", []):
            desig, price, pts = p.get("designation"), p.get("price"), p.get("points")
            if desig not in ("home", "away", "draw", "over", "under") or price is None:
                continue
            if desig == "draw" and market != "h2h":
                continue
            sel = {"home": ev["home"], "away": ev["away"], "draw": "Draw", "over": "Over", "under": "Under"}[desig]
            rows.append(_row(stamp, sport, ev, "pinnacle", market, sel, pts if market != "h2h" else None, price,
                             provider_update=provider_update(p, now) or provider_update(mk, now),
                             source_url=f"{PIN_BASE}/leagues/{PIN_LEAGUES[sport]}/markets/straight"))
    return rows


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().lstrip("ou").replace("EVEN", "+100").replace("even", "+100")
    try:
        return float(s)
    except ValueError:
        return None


def parse_espn(payload: dict, sport: str, names: Names, index: dict, now: dt.datetime,
               horizon: dt.datetime) -> list[dict]:
    """DraftKings spread/total/moneyline as ESPN embeds them (provider id 100)."""
    rows = []
    stamp = now.isoformat()
    for e in payload.get("events", []):
        try:
            comp = e["competitions"][0]
            kick = iso_et(e["date"])
        except (KeyError, IndexError, ValueError):
            continue
        if not (now < kick <= horizon):
            continue
        sides = {c.get("homeAway"): c.get("team", {}).get("displayName") for c in comp.get("competitors", [])}
        if "home" not in sides or "away" not in sides:
            continue
        home, away = names.resolve(sides["home"]), names.resolve(sides["away"])
        if not home or not away:
            continue
        ev = {"kick": kick, "home": home, "away": away, "event_id": event_id_for(index, sport, kick, home, away)}
        for o in comp.get("odds") or []:
            if not isinstance(o, dict):   # ESPN soccer lists can hold null odds entries
                continue
            prov = o.get("provider") or {}   # ESPN soccer odds entries can carry provider: null
            if str(prov.get("id")) != "100" and "draftkings" not in (prov.get("name") or "").lower():
                continue
            ml = o.get("moneyline") or {}
            for side, team in (("home", home), ("away", away)):
                c = (ml.get(side) or {}).get("close") or {}
                odds = _num(c.get("odds"))
                if odds is not None:
                    rows.append(_row(stamp, sport, ev, "draftkings", "h2h", team, None, odds,
                                     provider_update=provider_update(c, now) or provider_update(o, now),
                                     source_url=ESPN[sport]))
            draw = None
            if sport in SOCCER:   # ESPN carries the draw under moneyline.draw.close like the two sides, else drawOdds.moneyLine
                dc = (ml.get("draw") or {}).get("close") or {}
                draw = _num(dc.get("odds"))
                if draw is None and isinstance(o.get("drawOdds"), dict):
                    draw = _num(o["drawOdds"].get("moneyLine"))
            if draw is not None:
                rows.append(_row(stamp, sport, ev, "draftkings", "h2h", "Draw", None, draw,
                                 provider_update=provider_update(o, now), source_url=ESPN[sport]))
            ps = o.get("pointSpread") or {}
            for side, team in (("home", home), ("away", away)):
                c = (ps.get(side) or {}).get("close") or {}
                line, odds = _num(c.get("line")), _num(c.get("odds"))
                if line is not None and odds is not None:
                    rows.append(_row(stamp, sport, ev, "draftkings", "spreads", team, line, odds,
                                     provider_update=provider_update(c, now) or provider_update(o, now),
                                     source_url=ESPN[sport]))
            tot = o.get("total") or {}
            for side, sel in (("over", "Over"), ("under", "Under")):
                c = (tot.get(side) or {}).get("close") or {}
                line, odds = _num(c.get("line")), _num(c.get("odds"))
                if line is not None and odds is not None:
                    rows.append(_row(stamp, sport, ev, "draftkings", "totals", sel, line, odds,
                                     provider_update=provider_update(c, now) or provider_update(o, now),
                                     source_url=ESPN[sport]))
            break
    return rows


def _row(stamp, sport, ev, book, market, sel, pts, odds, *, provider_update="", source_url="") -> dict:
    return {"captured_at_et": stamp, "sport": sport, "event_id": ev["event_id"],
            "commence_time_et": ev["kick"].isoformat(), "away_team": ev["away"], "home_team": ev["home"],
            "book": book, "bookmaker_last_update": provider_update, "market": market, "selection": sel,
            "line_points": "" if pts is None else f"{float(pts):g}", "odds_american": f"{float(odds):.0f}",
            "event_link": source_url, "outcome_link": ""}


def fetch_all(now: dt.datetime, sports=tuple(PIN_LEAGUES)) -> tuple[list[dict], list[str], dict]:
    horizon = now + dt.timedelta(days=HORIZON_DAYS)
    loaded = load_index()
    index, universe = loaded["index"], loaded.get("teams", {})
    aliases = load_aliases()
    rows, notes, unmatched = [], [], {}
    for sport in sports:
        names = Names(sport, aliases, universe.get(sport))
        espn_payloads = None
        if sport in SOCCER:
            # ESPN's display names are the canonical universe for soccer: fetch them first
            # so Pinnacle's "Man Utd" / "Atletico Madrid" resolve to the same event ids.
            try:
                base = ESPN[sport]
                sep = "&" if "?" in base else "?"
                espn_payloads = [get_json(f"{base}{sep}dates={now + dt.timedelta(days=d):%Y%m%d}")
                                 for d in range(DAYS_AHEAD.get(sport, DAILY_DAYS_AHEAD) + 1)]
                for p in espn_payloads:
                    for e in p.get("events", []):
                        for c in (e.get("competitions") or [{}])[0].get("competitors", []):
                            nm = (c.get("team") or {}).get("displayName")
                            if nm:
                                names.full.add(nm)
                names.full_norm = {norm(f): f for f in names.full}
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
                notes.append(f"espn/dk {sport}: FAILED {exc}")
        try:
            mus = get_json(f"{PIN_BASE}/leagues/{PIN_LEAGUES[sport]}/matchups", {"X-API-Key": PIN_KEY})
            mks = get_json(f"{PIN_BASE}/leagues/{PIN_LEAGUES[sport]}/markets/straight", {"X-API-Key": PIN_KEY})
            pr = parse_pinnacle(mus, mks, sport, names, index, now, horizon)
            rows += pr
            notes.append(f"pinnacle {sport}: {len(pr)} rows")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
            notes.append(f"pinnacle {sport}: FAILED {exc}")
        try:
            base = ESPN[sport]
            sep = "&" if "?" in base else "?"
            if sport in DATE_RANGE_SPORTS:
                # ESPN's hockey scoreboard answers 400 to a date range, so one
                # GET per day; DAILY_DAYS_AHEAD keeps the 30-minute loop light.
                weeks = []
                payloads = espn_payloads if espn_payloads is not None else \
                    [get_json(f"{base}{sep}dates={now + dt.timedelta(days=d):%Y%m%d}")
                     for d in range(DAYS_AHEAD.get(sport, DAILY_DAYS_AHEAD) + 1)]
            else:
                weeks = schedule_weeks(sport, now, horizon)
                payloads = [get_json(f"{base}{sep}dates={now.year}&seasontype=2&week={wk}") for wk in weeks] \
                    if weeks else [get_json(f"{base}{sep}dates={now.year}")]
            er = []
            for p in payloads:
                er += parse_espn(p, sport, names, index, now, horizon)
            rows += er
            notes.append(f"espn/dk {sport}: {len(er)} rows ({f'{DAYS_AHEAD.get(sport, DAILY_DAYS_AHEAD) + 1} days' if sport in DATE_RANGE_SPORTS else 'weeks ' + str(weeks or '?')})")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
            notes.append(f"espn/dk {sport}: FAILED {exc}")
        if names.unmatched:
            unmatched[sport] = sorted(names.unmatched)
    # de-dup rows within the pull (two ESPN weeks can overlap)
    seen, out = set(), []
    for r in rows:
        k = (r["sport"], r["event_id"], r["book"], r["market"], r["selection"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out, notes, unmatched


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def last_prices(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[(r["sport"], r["event_id"], r["book"], r["market"], r["selection"])] = (r["line_points"], r["odds_american"])
    return out


def append(path: Path, rows: list[dict]) -> None:
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerows(rows)


def write(rows: list[dict], now: dt.datetime, *, dry_run: bool) -> dict:
    prev = last_prices(HISTORY)
    changed = [r for r in rows if prev.get((r["sport"], r["event_id"], r["book"], r["market"], r["selection"]))
               != (r["line_points"], r["odds_american"])]
    closing = [r for r in rows if 0 < (dt.datetime.fromisoformat(r["commence_time_et"]) - now).total_seconds()
               <= CLOSE_WINDOW_MIN * 60]
    # History is change-only; close evidence is observation-based. An unchanged
    # price freshly retrieved near kickoff must retain that confirming capture.
    def observation(r):
        return tuple(r.get(k, "") for k in FIELDS)
    observed = set()
    if closing and SNAPSHOTS.exists():
        with SNAPSHOTS.open(encoding="utf-8") as f:
            observed = {observation(r) for r in csv.DictReader(f)}
    closing = [r for r in closing if observation(r) not in observed]
    if not dry_run:
        LW.mkdir(parents=True, exist_ok=True)
        # Never expose a partially rewritten current feed to concurrent readers.
        temporary = LATEST.with_suffix(".csv.tmp")
        with temporary.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        temporary.replace(LATEST)
        if changed:
            append(HISTORY, changed)
        if closing:
            append(SNAPSHOTS, closing)
    return {"rows": len(rows), "changed": len(changed), "closing": len(closing)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sport", choices=sorted(PIN_LEAGUES), action="append")
    args = ap.parse_args(argv)
    try:
        import davidos_lib as dl
        guard = dl.LoopGuardrail("football-free-lines", max_iterations=1, max_cost_usd=0.0,
                                 max_daily_iterations=80)
        if not guard.check_and_increment(1, 0.0):
            print("free-lines: daily guardrail reached; skipping")
            return 1
    except Exception as exc:
        print(f"free-lines: guardrail unavailable ({exc}); refusing fetch")
        return 1
    now = dt.datetime.now(ET).replace(microsecond=0)
    rows, notes, unmatched = fetch_all(now, tuple(args.sport or tuple(PIN_LEAGUES)))
    if notes and all(": FAILED " in note for note in notes):
        print("free-lines: all sources failed; preserving prior files · " + " · ".join(notes))
        return 1
    res = write(rows, now, dry_run=args.dry_run)
    if unmatched:
        if args.dry_run:
            print("unmatched:", json.dumps(unmatched))
        else:
            with UNMATCHED.open("a", encoding="utf-8") as f:
                f.write(f"{now.isoformat()} {json.dumps(unmatched)}\n")
    print(f"free-lines {now.strftime('%Y-%m-%d %H:%M')}: {res['rows']} rows, {res['changed']} changed, "
          f"{res['closing']} closing rows -> snapshots · " + " · ".join(notes)
          + (f" · unmatched {sum(len(v) for v in unmatched.values())} (see {UNMATCHED.name})" if unmatched else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
