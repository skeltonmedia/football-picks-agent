#!/usr/bin/env python3
"""spot-flags.py — situational trap-game flags, computed from data on disk.

    python3 -B 04-agents/football-picks-agent/spot-flags.py [--days 8]

WHY (2026-08-30). AQ-132, approved by David. Week 1's UNLV loss had the
classic trap shape: confident market, heavy home favorite, no line movement,
live dog. Trap games are situational, and every input needed to flag them is
already fetched.

FLAGS (per upcoming game)
  SHORT_REST   a team plays on fewer than 6 days' rest
  REST_EDGE    rest differential >= 3 days (bye vs short week)
  LOOKAHEAD    a favorite's NEXT opponent is top-25-rated — the classic
               look-past-this-week spot
  LETDOWN      a team coming off a win over a top-25-rated opponent —
               emotional peak, flat spot follows
  RLM          reverse line movement: DraftKings' price moved opposite to
               the multi-book consensus move — the free proxy for
               bet-percentage data (the public on one side, money on the other)

DISPLAY-ONLY, BY DESIGN. Two model upgrades died in backtests today (pace,
offense-EPA — see model-notes.md). Flags therefore ship as information with
NO confidence-score effect until they accumulate their own settled record in
the ledger: every flagged pick carries its flags in the `reason` column, so
in a month we can measure "did LETDOWN games actually underperform?" and
weight only what proves out. Measure first, weight later.

Writes data/spot-flags.csv; weekly-picks.py reads it into write-ups.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import pathlib
import sys
import unicodedata
import urllib.request

AGENT = pathlib.Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402

DATA = AGENT / "data"
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
OUT = DATA / "spot-flags.csv"
TIMEOUT = 60
TOP_RANK = 25


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in s.lower() if c.isalnum())


def cfbd(path: str) -> list:
    key = ""
    for line in (AGENT / "config.env").read_text().splitlines():
        if line.startswith("COLLEGE_FOOTBALL_DATA_API_KEY="):
            key = line.split("=", 1)[1].strip()
    req = urllib.request.Request(f"https://api.collegefootballdata.com{path}",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def season_games() -> list[dict]:
    """Full-season CFB schedule (for rest/lookahead) + NFL from nflverse."""
    year = now_eastern().year
    games = []
    for g in cfbd(f"/games?year={year}&seasonType=regular"):
        games.append({
            "league": "cfb", "week": g.get("week"),
            "start": g.get("startDate") or "",
            "away": g.get("awayTeam") or "", "home": g.get("homeTeam") or "",
            "away_pts": g.get("awayPoints"), "home_pts": g.get("homePoints"),
        })
    url = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
    import io
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        for g in csv.DictReader(io.StringIO(r.read().decode())):
            if g.get("season") != str(year) or g.get("game_type") != "REG":
                continue
            games.append({
                "league": "nfl", "week": g.get("week"),
                "start": g.get("gameday", "") + "T17:00:00.000Z",
                "away": g.get("away_team") or "", "home": g.get("home_team") or "",
                "away_pts": g.get("away_score") or None, "home_pts": g.get("home_score") or None,
            })
    return games


def rankings() -> set[str]:
    """Top-25-rated CFB teams by SP+ rank from the ratings file we already fetch."""
    top = set()
    p = DATA / "cfb-team-ratings.csv"
    if p.exists():
        for r in csv.DictReader(p.open(encoding="utf-8")):
            try:
                if int(r.get("sp_plus_ranking") or 999) <= TOP_RANK:
                    top.add(norm(r["team"]))
            except ValueError:
                continue
    p = DATA / "nfl-team-ratings.csv"
    if p.exists():
        for r in csv.DictReader(p.open(encoding="utf-8")):
            try:
                if int(r.get("srs_ranking") or 99) <= 8:  # NFL "elite" ~ top quarter
                    top.add(norm(r["team"]))
            except ValueError:
                continue
    return top


def rlm_flags(horizon_h: int) -> dict[frozenset, str]:
    """DK moved opposite the consensus of the other books -> RLM."""
    from collections import defaultdict
    hist = defaultdict(list)
    meta = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in csv.DictReader(SNAPSHOTS.open(encoding="utf-8")):
        if r["market"] != "h2h":
            continue
        try:
            st = datetime.datetime.fromisoformat(r["commence_time_et"])
        except ValueError:
            continue
        if st.tzinfo is None:
            st = st.replace(tzinfo=datetime.timezone.utc)
        if not (now < st <= now + datetime.timedelta(hours=horizon_h)):
            continue
        try:
            odds = float(r["odds_american"])
        except (ValueError, TypeError):
            continue
        hist[(r["event_id"], r["selection"], r["book"])].append((r["captured_at_et"], odds))
        meta[r["event_id"]] = r

    def prob(o):
        return 100 / (o + 100) if o > 0 else -o / (-o + 100)

    moves = defaultdict(dict)  # (eid, sel) -> book -> prob move
    for (eid, sel, book), rows in hist.items():
        rows.sort()
        if len(rows) >= 2:
            moves[(eid, sel)][book] = prob(rows[-1][1]) - prob(rows[0][1])
    out = {}
    for (eid, sel), by_book in moves.items():
        dk = by_book.get("draftkings")
        others = [v for b, v in by_book.items() if b != "draftkings"]
        if dk is None or len(others) < 2:
            continue
        import statistics
        cons = statistics.mean(others)
        # consensus moved meaningfully one way, DK the other
        if abs(cons) >= 0.01 and dk * cons < 0 and abs(dk) >= 0.005:
            m = meta[eid]
            out[frozenset((norm(m["away_team"]), norm(m["home_team"])))] = (
                f"RLM: books moved {'toward' if cons > 0 else 'off'} {sel.split()[-1]}, DK went the other way")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    args = ap.parse_args()

    games = season_games()
    top = rankings()
    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(days=args.days)

    # per-team chronological schedule
    by_team: dict[str, list] = {}
    parsed = []
    for g in games:
        try:
            st = datetime.datetime.fromisoformat(g["start"].replace("Z", "+00:00"))
        except ValueError:
            continue
        parsed.append((st, g))
        for t in (g["away"], g["home"]):
            by_team.setdefault(norm(t), []).append((st, g))
    for v in by_team.values():
        v.sort(key=lambda x: x[0])

    def prior_next(team: str, st):
        sched = by_team.get(norm(team), [])
        prior = next((x for x in reversed(sched) if x[0] < st), None)
        nxt = next((x for x in sched if x[0] > st), None)
        return prior, nxt

    rlm = rlm_flags(args.days * 24)
    rows_out = []
    for st, g in sorted(parsed, key=lambda x: x[0]):
        if not (now < st <= horizon):
            continue
        flags = []
        rests = {}
        for team in (g["away"], g["home"]):
            prior, nxt = prior_next(team, st)
            if prior:
                rest = (st - prior[0]).days
                rests[team] = rest
                if rest < 6:
                    flags.append(f"SHORT_REST {team} ({rest}d)")
                pg = prior[1]
                opp = pg["home"] if norm(pg["away"]) == norm(team) else pg["away"]
                won = None
                try:
                    tp = pg["home_pts"] if norm(pg["home"]) == norm(team) else pg["away_pts"]
                    op = pg["away_pts"] if norm(pg["home"]) == norm(team) else pg["home_pts"]
                    if tp is not None and op is not None:
                        won = int(tp) > int(op)
                except (ValueError, TypeError):
                    pass
                if won and norm(opp) in top:
                    flags.append(f"LETDOWN {team} (beat {opp})")
            if nxt:
                nopp = nxt[1]["home"] if norm(nxt[1]["away"]) == norm(team) else nxt[1]["away"]
                if norm(nopp) in top:
                    flags.append(f"LOOKAHEAD {team} (next: {nopp})")
        if len(rests) == 2:
            vals = list(rests.items())
            diff = vals[0][1] - vals[1][1]
            if abs(diff) >= 3:
                adv = vals[0][0] if diff > 0 else vals[1][0]
                flags.append(f"REST_EDGE {adv} (+{abs(diff)}d)")
        key = frozenset((norm(g["away"]), norm(g["home"])))
        if key in rlm:
            flags.append(rlm[key])
        if flags:
            rows_out.append({
                "computed_at_et": now_eastern().isoformat(),
                "league": g["league"].upper(),
                "start": g["start"],
                "away_team": g["away"], "home_team": g["home"],
                "flags": " | ".join(flags),
            })

    # 2026-09-07: append, never overwrite. Mode "w" threw away every prior
    # computation, so no flag could ever reach the 30 graded games its own
    # contract demands (AQ-168 step 2 found one day of history). Readers take
    # the latest computed_at_et per game; grade-spot-flags.py does.
    new_file = not OUT.exists() or OUT.stat().st_size == 0
    with OUT.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["computed_at_et", "league", "start",
                                          "away_team", "home_team", "flags"])
        if new_file:
            w.writeheader()
        w.writerows(rows_out)
    print(f"{len(rows_out)} game(s) flagged -> {OUT.relative_to(ROOT)}")
    for r in rows_out[:12]:
        print(f"  {r['league']} {r['away_team'].split()[-1][:12]} @ "
              f"{r['home_team'].split()[-1][:12]}: {r['flags']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
