#!/usr/bin/env python3
"""Fetch current NFL injury reports from ESPN's keyless injuries endpoint.

Keyless, one request. Writes `data/injuries.csv`, replacing the previous
snapshot (injuries are a current-state report, not a history — the ledger
freezes injury status per decision, which is where history belongs).

Why this exists (2026-08-22): `model-notes.md` concluded after the failed
ratings backtest that whatever eventually earns a `PICK` needs inputs the
market prices less efficiently — and named injuries first. Yet `inputs.md`
listed injury data as "manual for now" and every review card shipped with a
blank injury line. The single highest-leverage line on the card was the one
the pipeline did nothing to fill.

Scope is honest:
  - NFL is comprehensive; CFB is sparse by nature. Added 2026-08-28: ESPN
    does expose a college-football injuries endpoint, but coverage is thin
    (3 teams league-wide on 2026-08-28) because college programs are not
    required to publish reports. Sparse is not useless — when a college entry
    exists it is usually a real, named starter, and one of those three was
    Virginia, a team on that night's board. So pull it, label the league, and
    never read absence as health.
  - This report is a starting point, not a verdict. Official league injury
    designations (Questionable/Doubtful/Out) follow practice-participation
    rules during the season; ESPN's feed tracks them closely but is not the
    league's own report. Confirm the QB line before any PICK, per
    billy-walters-operating-doctrine.md's freshness gates.

QB status gets its own column because quarterback is the only position whose
availability routinely moves a spread by multiple points — the exact
"information timing" edge the doctrine file describes.
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
INJURIES_PATH = DATA_DIR / "injuries.csv"
NFL_SCHEDULE_PATH = DATA_DIR / "nfl-schedule.csv"
ET = ZoneInfo("America/New_York")

URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
CFB_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/injuries"
# ESPN's site API 403s identifying UAs; the slate fetchers established this
# convention (see fetch-nfl-slate.py) — keep all ESPN calls consistent.
UA = "curl/8.4.0"
TIMEOUT_SECONDS = 30
MAX_REPORT_AGE_DAYS = 180

FIELDNAMES = [
    "captured_at_et",
    "team",
    "team_abbrev",
    "player",
    "position",
    "status",
    "injury_type",
    "detail",
    "reported_at",
    "is_qb",
    "league",
]


def fetch() -> dict:
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.load(resp)


def load_abbrevs() -> dict[str, str]:
    """Full team name -> abbreviation, from the schedule fetcher's own output.

    The injuries payload carries only `id` and `displayName` per team — no
    abbreviation. Deriving it from nfl-schedule.csv keeps the mapping in data
    (never hardcoded in code, per vault rules) and guarantees the same names
    the review-card join uses."""
    mapping: dict[str, str] = {}
    if not NFL_SCHEDULE_PATH.exists():
        return mapping
    with NFL_SCHEDULE_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for side in ("away", "home"):
                full = row.get(f"{side}_team_full", "")
                abbr = row.get(f"{side}_team", "")
                if full and abbr:
                    mapping[full] = abbr
    return mapping


def normalize(payload: dict, captured_at: str, league: str = "NFL") -> list[dict[str, str]]:
    abbrevs = load_abbrevs()
    rows: list[dict[str, str]] = []
    for team_block in payload.get("injuries", []):
        team = team_block.get("displayName", "")
        abbrev = abbrevs.get(team, "")
        for item in team_block.get("injuries", []):
            athlete = item.get("athlete") or {}
            position = ((athlete.get("position") or {}).get("abbreviation") or "").strip()
            details = item.get("details") or {}
            rows.append({
                "captured_at_et": captured_at,
                "team": team,
                "team_abbrev": abbrev,
                "player": athlete.get("displayName", ""),
                "position": position,
                "status": item.get("status", ""),
                "injury_type": details.get("type", ""),
                "detail": (details.get("detail") or "").strip(),
                "reported_at": item.get("date", ""),
                "is_qb": "True" if position == "QB" else "False",
                "league": league,
            })
    return rows


def filter_recent_rows(
    rows: list[dict[str, str]], captured_at: str, max_age_days: int = MAX_REPORT_AGE_DAYS
) -> tuple[list[dict[str, str]], int]:
    """Drop stale or undated injury rows from a current-state snapshot."""
    captured = datetime.fromisoformat(captured_at)
    cutoff = captured - timedelta(days=max_age_days)
    kept: list[dict[str, str]] = []
    dropped = 0
    for row in rows:
        try:
            reported = datetime.fromisoformat(
                row.get("reported_at", "").replace("Z", "+00:00")
            ).astimezone(captured.tzinfo)
        except (ValueError, AttributeError):
            dropped += 1
            continue
        if reported < cutoff:
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def main() -> int:
    captured_at = datetime.now(ET).replace(microsecond=0).isoformat()
    try:
        payload = fetch()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(
            f"ESPN injuries fetch failed ({exc}). No snapshot written — the "
            "previous injuries.csv, if any, is stale; do not treat it as fresh.",
            file=sys.stderr,
        )
        return 1

    rows = normalize(payload, captured_at, "NFL")
    # College: same shape, far thinner coverage. A failure here must not sink
    # the NFL rows, which are the ones that reliably exist.
    try:
        req = urllib.request.Request(CFB_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            cfb_rows = normalize(json.load(resp), captured_at, "CFB")
        cfb_rows, dropped = filter_recent_rows(cfb_rows, captured_at)
        rows += cfb_rows
        if dropped:
            print(
                f"Discarded {dropped} stale or undated CFB injury row(s); "
                "official team reports remain required.",
                file=sys.stderr,
            )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"CFB injuries unavailable ({exc}); NFL rows still written.", file=sys.stderr)
    if not rows:
        print(
            "ESPN returned no injury rows. No snapshot written — an empty "
            "league-wide report is a feed problem, not a healthy league.",
            file=sys.stderr,
        )
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with INJURIES_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    teams = len({r["team"] for r in rows})
    leagues = ", ".join(sorted({r["league"] for r in rows}))
    qbs = [r for r in rows if r["is_qb"] == "True"]
    print(
        f"Wrote {len(rows)} injury rows across {teams} teams ({leagues}) to "
        f"{INJURIES_PATH.relative_to(ROOT)} ({len(qbs)} QB entries)."
    )
    for r in qbs:
        print(f"  QB: {r['team_abbrev']} {r['player']} — {r['status']} ({r['injury_type']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
