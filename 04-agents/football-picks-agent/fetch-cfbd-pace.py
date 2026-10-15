#!/usr/bin/env python3
"""fetch-cfbd-pace.py — CFB pace/tempo aggregates from CFBD (free, existing key).

    python3 -B fetch-cfbd-pace.py [--year 2026]

WHY: totals are pace x efficiency, and the model currently carries efficiency
only. Data-roadmap item #3 calls this "the single biggest upgrade to the CFB
totals model" — the UNC/TCU 25-point rock fight (Week 1 miss) is what missing
it looks like: two efficient-but-glacial teams priced like a shootout.

CONTRACT: fetch-and-store only. This script adds NO term to the live model.
Integration happens behind the backtest gate (model-notes.md: "measured twice,
lost twice" — no unvalidated model changes), tracked as its own AQ item.
Writes data/cfbd-pace-{year}.json: {team: {plays_pg, sec_per_play, games}}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "lib"))
API = "https://api.collegefootballdata.com"


def api_get(path: str, params: dict, key: str, timeout: int = 30):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{API}{path}?{q}", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=datetime.now().year)
    a = ap.parse_args()

    # Same key convention as every other fetch-cfbd-* script: config.env.
    key = os.environ.get("COLLEGE_FOOTBALL_DATA_API_KEY", "").strip()
    if not key:
        env = HERE / "config.env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("COLLEGE_FOOTBALL_DATA_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"')
    if not key:
        print("no CFBD key (COLLEGE_FOOTBALL_DATA_API_KEY in config.env)", file=sys.stderr)
        return 1

    # /stats/season has no literal "plays" stat (verified 2026-09-01 against
    # the live API): offensive plays derive as passAttempts + rushingAttempts
    # (NCAA books sacks as rushing plays), and possessionTime is in seconds,
    # so possessionTime/plays gives offensive seconds-per-play — the tempo
    # number. Game total pace = both teams' plays; the model combines sides.
    rows = api_get("/stats/season", {"year": a.year}, key)
    WANT = {"games", "totalYards", "passAttempts", "rushingAttempts", "possessionTime"}
    agg: dict[str, dict] = {}
    for r in rows:
        stat = r.get("statName")
        if stat not in WANT:
            continue
        team = r.get("team")
        if not team:
            continue
        try:
            agg.setdefault(team, {})[stat] = float(r.get("statValue"))
        except (TypeError, ValueError):
            continue

    out = {}
    for team, s in agg.items():
        games = s.get("games")
        plays = (s.get("passAttempts") or 0) + (s.get("rushingAttempts") or 0)
        if not games or not plays:
            continue
        poss = s.get("possessionTime")
        out[team] = {
            "plays_pg": round(plays / games, 1),
            "sec_per_play": round(poss / plays, 2) if poss else None,
            "yards_per_play": round((s.get("totalYards") or 0) / plays, 2),
            "games": int(games),
        }

    dest = HERE / "data" / f"cfbd-pace-{a.year}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(
        {"fetched_at": datetime.now().isoformat(timespec="seconds"),
         "year": a.year, "teams": out}, indent=1))
    print(f"wrote {dest.name}: {len(out)} teams "
          f"(early-season note: <3 games per team means these are noisy priors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
