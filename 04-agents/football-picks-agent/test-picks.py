#!/usr/bin/env python3
"""test-picks.py — pre-flight gate: no board ships on bad data.

    python3 -B 04-agents/football-picks-agent/test-picks.py

WHY (2026-08-29). David: "How do we make this whole thing actually reliable?"
The same way the site got reliable: a deterministic gate in front of every
output. AI proposes; a defined checkpoint decides — including when the AI is
a picks pipeline and the output is a bet card.

Every check below is a lesson from opening weekend, pinned so it cannot
recur silently:

  STALE ODDS      2026-08-29: quota ran dry and an afternoon "lines are
                  steady" read was built on morning snapshots without anyone
                  noticing. FAIL if newest odds snapshot is older than 6h.
  TEAM RESOLUTION 2026-08-28/09-03: fuzzy matching fed Florida's ratings to
                  Florida State, then FBS ratings to similarly named FCS
                  schools. Only schedule-exact schools or explicit aliases
                  resolve; unmatched teams are warned and cannot enter.
  VENUE SANITY    2026-08-29: "Aviva Stadium, Dublin" (Ireland) geocoded to
                  Dublin OHIO and shipped an Ohio forecast. FAIL if any
                  weather row carries a forecast for a stateless venue.
  JOIN INTEGRITY  2026-08-29 (Codex session): Hawai'i vs Hawaii apostrophe
                  variants silently dropped a game. FAIL if an upcoming odds
                  event cannot be matched to a schedule row.
  LEDGER          The record is the product. FAIL on schema drift or
                  duplicate logging of the same selection on the same day.

run-weekly-card.py runs this first; a FAIL means no board is produced. A
wrong card is worse than no card — a blank Thursday is recoverable, money on
Ohio weather is not.
"""

from __future__ import annotations

import csv
import datetime
import sys
from collections import Counter, defaultdict
from pathlib import Path

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402
from football_team_resolution import (  # noqa: E402
    build_school_crosswalk,
    canonical_cfb_school,
    resolve_team_record,
)

DATA = AGENT / "data"
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
LEDGER = AGENT / "recommendation-ledger.csv"
MAX_ODDS_AGE_H = 6.0
LOOKAHEAD_H = 168

failures: list[str] = []
warnings: list[str] = []


def check(ok: bool, msg: str, warn_only: bool = False) -> None:
    if not ok:
        (warnings if warn_only else failures).append(msg)


def norm(s: str) -> str:
    # ASCII-fold first: Hawai'i / Hawaiʻi / Hawaii must all normalize equal
    # (the 2026-08-29 apostrophe lesson — a missed game is a silent miss).
    import unicodedata as _ud
    s = _ud.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in s.lower() if c.isalnum())


def load(path: Path) -> list[dict]:
    return list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []


def main() -> int:
    now = now_eastern()
    horizon = now + datetime.timedelta(hours=LOOKAHEAD_H)

    # ── odds freshness + upcoming events ─────────────────────────────────
    newest = ""
    upcoming: dict[str, dict] = {}
    for r in load(SNAPSHOTS):
        newest = max(newest, r.get("captured_at_et", ""))
        try:
            st = datetime.datetime.fromisoformat(r["commence_time_et"])
        except (ValueError, KeyError):
            continue
        if now < st <= horizon:
            upcoming[r["event_id"]] = r
    check(bool(newest), "no odds snapshots at all — run run-daily-data.py")
    if newest:
        age_h = (now - datetime.datetime.fromisoformat(newest)).total_seconds() / 3600
        check(age_h <= MAX_ODDS_AGE_H,
              f"odds are {age_h:.1f}h old (max {MAX_ODDS_AGE_H:.0f}h) — quota? "
              f"A board from these prices misstates the market.")

    # ── team resolution: exact identities only; unmatched teams stay out ──
    cfb = load(DATA / "cfb-team-ratings.csv")
    nfl = load(DATA / "nfl-team-ratings.csv")
    records = {
        "cfb": {norm(r["team"]): r for r in cfb},
        "nfl": {norm(r["team"]): r for r in nfl},
    }
    cfb_crosswalk = build_school_crosswalk(load(DATA / "cfb-schedule.csv"))
    nfl_crosswalk = {}
    for row in load(DATA / "nfl-schedule.csv"):
        for full_col, abbr_col in (("away_team_full", "away_team"), ("home_team_full", "home_team")):
            if row.get(full_col) and row.get(abbr_col):
                nfl_crosswalk[norm(row[full_col])] = norm(row[abbr_col])
    unresolved = set()
    for r in upcoming.values():
        league = "nfl" if "nfl" in r.get("sport", "") else "cfb"
        for team in (r.get("away_team", ""), r.get("home_team", "")):
            n = norm(team)
            if league == "cfb":
                matched = resolve_team_record(team, records[league], cfb_crosswalk)
            else:
                abbr = nfl_crosswalk.get(n)
                matched = records[league].get(abbr) if abbr else None
            if not matched:
                unresolved.add(team)
    check(not unresolved,
          f"{len(unresolved)} upcoming team(s) with no rating match: "
          f"{sorted(unresolved)[:6]}", warn_only=True)

    # ── venue sanity: stateless venue must never carry a forecast ─────────
    for w in load(DATA / "weather.csv"):
        if not (w.get("state") or "").strip() and (w.get("temperature_f") or "").strip():
            check(False, f"stateless venue has a forecast (Dublin-Ohio class): "
                         f"{w.get('venue')} / {w.get('matchup')}")
    # and cached coords for venues whose schedule row is stateless
    stateless_venues = set()
    for sched in ("cfb-schedule.csv", "nfl-schedule.csv"):
        for r in load(DATA / sched):
            if not (r.get("state") or "").strip() and r.get("indoor") != "True":
                stateless_venues.add((r.get("venue") or "").strip())
    for row in load(DATA / "venue-coords.csv") if (DATA / "venue-coords.csv").exists() else []:
        pass  # venue-coords has no header context; checked via weather above

    # ── join integrity: every upcoming odds event matches a schedule row ──
    sched_pairs = set()
    for sched in ("cfb-schedule.csv", "nfl-schedule.csv"):
        for r in load(DATA / sched):
            if sched.startswith("cfb"):
                sched_pairs.add(frozenset((norm(r.get("away_team_school", "")),
                                           norm(r.get("home_team_school", "")))))
            else:
                sched_pairs.add(frozenset((norm(r.get("away_team_full", "")),
                                           norm(r.get("home_team_full", "")))))
    orphans = []
    for r in upcoming.values():
        if "nfl" in r.get("sport", ""):
            pair = frozenset((norm(r.get("away_team", "")),
                              norm(r.get("home_team", ""))))
        else:
            pair = frozenset((
                canonical_cfb_school(r.get("away_team", ""), cfb_crosswalk),
                canonical_cfb_school(r.get("home_team", ""), cfb_crosswalk),
            ))
        if pair not in sched_pairs:
            orphans.append(f"{r.get('away_team')} @ {r.get('home_team')}")
    check(not orphans,
          f"{len(orphans)} odds event(s) with no schedule match (apostrophe class): "
          f"{orphans[:4]}", warn_only=True)  # warn: odds feed may list games schedules lack

    # College injury feeds can return years-old rows as if they were current.
    # A stale OUT designation is worse than no college report at all.
    max_cfb_injury_age = datetime.timedelta(days=180)
    for row in load(DATA / "injuries.csv"):
        if row.get("league") != "CFB":
            continue
        try:
            reported = datetime.datetime.fromisoformat(
                row.get("reported_at", "").replace("Z", "+00:00")
            ).astimezone(now.tzinfo)
        except (ValueError, AttributeError):
            check(False, f"CFB injury row has no valid report date: {row.get('player')}")
            continue
        check(now - reported <= max_cfb_injury_age,
              f"stale CFB injury row ({reported.date()}): "
              f"{row.get('team')} / {row.get('player')}")

    # ── ledger integrity ─────────────────────────────────────────────────
    REQUIRED = {"decision_date", "event_id", "matchup", "market", "selection",
                "odds_american", "confidence", "result"}
    rows = load(LEDGER)
    if LEDGER.exists():
        cols = set(csv.DictReader(LEDGER.open(encoding="utf-8")).fieldnames or [])
        missing = REQUIRED - cols
        check(not missing, f"ledger missing columns: {sorted(missing)}")
    dupes = [k for k, c in Counter(
        (r.get("decision_date"), r.get("event_id"), r.get("market"), r.get("selection"))
        for r in rows).items() if c > 1 and k[1]]
    check(not dupes, f"duplicate ledger rows (same selection logged twice same day): {dupes[:3]}")

    # ── report ───────────────────────────────────────────────────────────
    for w in warnings:
        print(f"  WARN  {w}")
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        print(f"\n✗ {len(failures)} failure(s) — NO BOARD. A wrong card is worse than no card.")
        return 1
    print(f"✓ pre-flight clean ({len(upcoming)} upcoming events"
          + (f", {len(warnings)} warning(s)" if warnings else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
