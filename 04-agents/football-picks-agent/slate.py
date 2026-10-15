#!/usr/bin/env python3
"""slate.py — on-demand game briefing: best moneyline, win %, stats, weather.

    python3 -B 04-agents/football-picks-agent/slate.py                 # next 72h, both leagues
    python3 -B 04-agents/football-picks-agent/slate.py --league cfb    # nfl | cfb | all
    python3 -B 04-agents/football-picks-agent/slate.py --hours 24
    python3 -B 04-agents/football-picks-agent/slate.py --team Stanford

WHY (2026-08-28). David: "I want the best ML available on odds of winning the
game stats and weather for nfl or ncaa on demand that's it."

Everything here was already being fetched — five books of odds, team ratings,
weather, injuries — and it lived in five CSVs nobody wants to open on a
Saturday. This assembles one briefing per game:

  BEST MONEYLINE   the top price across all books, and which book has it
  WIN %            the market's implied probability, vig removed
  STATS            SP+/rank for CFB, SRS/rank for NFL, plus rating gap
  WEATHER          forecast at kickoff
  QB               non-Active quarterbacks, because that is the input that moves a line

The market's win % is the market's opinion, not a forecast of ours — the
backtests (model-notes.md) say our ratings do not beat it. Stats are printed
as context for a human read, never as a pick.

Reads only local CSVs, so it is instant and works offline. Run
`run-daily-data.py` first if the data is stale; freshness is printed per section.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import sys
from collections import defaultdict
from pathlib import Path

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402

DATA = AGENT / "data"
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"


def american_to_prob(odds: float) -> float:
    return 100.0 / (odds + 100.0) if odds > 0 else -odds / (-odds + 100.0)


def profit(odds: float, stake: float) -> float:
    return stake * odds / 100 if odds > 0 else stake * 100 / -odds


def load_csv(name: str) -> list[dict]:
    p = DATA / name
    if not p.exists():
        return []
    return list(csv.DictReader(p.open(encoding="utf-8")))


def latest_prices() -> tuple[dict, dict, str]:
    """(event_id -> {selection -> [(book, odds)]}, event_id -> meta, captured_at)"""
    latest, meta, cap = {}, {}, ""
    if not SNAPSHOTS.exists():
        return {}, {}, ""
    for r in csv.DictReader(SNAPSHOTS.open(encoding="utf-8")):
        if r["market"] != "h2h":
            continue
        key = (r["event_id"], r["selection"], r["book"])
        prev = latest.get(key)
        if prev is None or r["captured_at_et"] > prev["captured_at_et"]:
            latest[key] = r
        meta[r["event_id"]] = r
        cap = max(cap, r["captured_at_et"])
    out: dict = defaultdict(lambda: defaultdict(list))
    for (eid, sel, book), r in latest.items():
        try:
            out[eid][sel].append((book, float(r["odds_american"])))
        except (ValueError, TypeError):
            continue
    return out, meta, cap


def norm(s: str) -> str:
    # ASCII-fold first: Hawai'i / Hawaiʻi / Hawaii must all normalize equal
    # (the 2026-08-29 apostrophe lesson — a missed game is a silent miss).
    import unicodedata as _ud
    s = _ud.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in s.lower() if c.isalnum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--league", choices=["nfl", "cfb", "all"], default="all")
    ap.add_argument("--team", help="filter to games involving this team (substring)")
    ap.add_argument("--stake", type=float, default=2.0)
    args = ap.parse_args()

    prices, meta, price_cap = latest_prices()
    if not prices:
        print("No odds captured yet. Run: python3 -B run-daily-data.py")
        return 1

    # Ratings, keyed loosely so "Stanford Cardinal" matches "Stanford".
    cfb_r = {norm(r["team"]): r for r in load_csv("cfb-team-ratings.csv")}
    nfl_r = {norm(r["team"]): r for r in load_csv("nfl-team-ratings.csv")}
    weather = {}
    for r in load_csv("weather.csv"):
        weather[norm(r["matchup"])] = r
    # Weather keys on abbreviations ("UNC VS TCU"); odds carry full names
    # ("North Carolina Tar Heels"). The schedule files hold both, so use them
    # as the bridge instead of guessing at substrings.
    fullname_to_matchup = {}
    for sched in ("cfb-schedule.csv", "nfl-schedule.csv"):
        for r in load_csv(sched):
            mk = norm(r.get("matchup", ""))
            for col in ("away_team_full", "home_team_full"):
                if r.get(col):
                    fullname_to_matchup.setdefault(norm(r[col]), set()).add(mk)
    inj = defaultdict(list)
    for r in load_csv("injuries.csv"):
        if r.get("is_qb") == "True" and r.get("status") not in ("Active", ""):
            inj[norm(r["team"])].append(f"{r['player']} ({r['status']})")

    def rating_for(team: str, league: str):
        table = nfl_r if league == "nfl" else cfb_r
        n = norm(team)
        if n in table:
            return table[n]
        cands = [(k, v) for k, v in table.items() if k and (k in n or n.startswith(k))]
        if not cands:
            return None
        # Longest key wins: "florida" and "floridastate" both match
        # "floridastateseminoles", and picking first-seen fed FLORIDA's ratings
        # to FLORIDA STATE games (same for Michigan/Eastern Michigan and
        # Georgia/Georgia Tech). Found 2026-08-28 via wrong names in write-ups.
        return max(cands, key=lambda kv: len(kv[0]))[1]

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(hours=args.hours)
    games = []
    for eid, sels in prices.items():
        m = meta[eid]
        try:
            start = datetime.datetime.fromisoformat(m["commence_time_et"])
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc)
        if not (now < start <= horizon):
            continue
        league = "nfl" if "nfl" in m["sport"] else "cfb"
        if args.league != "all" and args.league != league:
            continue
        if args.team and norm(args.team) not in norm(m["away_team"] + m["home_team"]):
            continue
        games.append((start, eid, m, sels, league))
    games.sort(key=lambda g: g[0])

    print(f"\nSLATE — {now_eastern().strftime('%a %b %-d, %-I:%M %p ET')} · "
          f"next {args.hours}h · {len(games)} game(s)")
    print(f"odds captured {price_cap[:16] or '?'} · ${args.stake:.0f} stake\n")

    for start, eid, m, sels, league in games:
        tag = "NFL" if league == "nfl" else "CFB"
        print("═" * 78)
        print(f"{tag}  {m['away_team']} @ {m['home_team']}")
        print(f"     {start.astimezone().strftime('%a %b %-d, %-I:%M %p ET')}")

        # ── best moneyline + de-vigged win probability
        best = {}
        for sel, offers in sels.items():
            if offers:
                best[sel] = max(offers, key=lambda o: -american_to_prob(o[1]))
        raw = {s: american_to_prob(odds) for s, (_book, odds) in best.items()}
        tot = sum(raw.values()) or 1.0
        print(f"\n  BEST MONEYLINE          book        win%   ${args.stake:.0f} wins")
        for sel, (book, odds) in sorted(best.items(), key=lambda kv: american_to_prob(kv[1][1]), reverse=True):
            fair = raw[sel] / tot * 100
            n_books = len(sels[sel])
            print(f"   {sel[:22]:22} {book[:10]:10} {odds:>+6.0f}  {fair:5.1f}%   "
                  f"${profit(odds, args.stake):5.2f}   ({n_books} books)")

        # ── stats
        ra = rating_for(m["away_team"], league)
        rh = rating_for(m["home_team"], league)
        print("\n  STATS")
        if league == "cfb" and ra and rh:
            try:
                gap = float(ra["sp_plus_rating"]) - float(rh["sp_plus_rating"])
            except (ValueError, TypeError):
                gap = None
            for r, who in ((ra, m["away_team"]), (rh, m["home_team"])):
                print(f"   {who[:22]:22} SP+ {r['sp_plus_rating']:>6} (#{r['sp_plus_ranking']:>3})  "
                      f"off {r['sp_offense_rating']:>5} / def {r['sp_defense_rating']:>5}  "
                      f"{r['conference'][:14]}")
            if gap is not None:
                lead = m["away_team"] if gap > 0 else m["home_team"]
                print(f"   SP+ gap: {abs(gap):.1f} toward {lead[:24]} "
                      f"({ra['data_year']} {'current' if ra['is_current_season']=='True' else 'prior'} season)")
        elif league == "nfl" and ra and rh:
            for r, who in ((ra, m["away_team"]), (rh, m["home_team"])):
                print(f"   {who[:22]:22} SRS {r['srs_rating']:>6} (#{r['srs_ranking']:>2})  "
                      f"avg margin {r['avg_margin']:>5}  [{r['data_year']} "
                      f"{'current' if r['is_current_season']=='True' else 'PRIOR season'}]")
            print("   ⚠ NFL preseason: prior-season ratings describe starters who play a series or two.")
        else:
            print("   no rating matched for one or both teams")

        # ── LEAN: where the ratings and the market disagree.
        # NOT a pick and not validated (backtests: ratings alone lose to the
        # close). Its job is triage — turning a 99-game Saturday into the two
        # or three games where a manual read has room to be right. A rating
        # gap the market already agrees with is not an opportunity.
        if ra and rh:
            try:
                if league == "cfb":
                    edge_pts = float(ra["sp_plus_rating"]) - float(rh["sp_plus_rating"])
                else:
                    edge_pts = float(ra["srs_rating"]) - float(rh["srs_rating"])
                edge_pts -= 2.5   # standard home-field adjustment, away-positive
                fav_rating = m["away_team"] if edge_pts > 0 else m["home_team"]
                market_fav = max(raw, key=raw.get)
                mkt_pct = raw[market_fav] / tot * 100
                # ~7 rating points ≈ a touchdown ≈ roughly 20 points of win prob
                rating_pct = min(92.0, 50.0 + abs(edge_pts) * 2.9)
                agree = norm(fav_rating)[:6] in norm(market_fav)
                print("\n  LEAN")
                if not agree:
                    print(f"   ⚑ DISAGREE — ratings favor {fav_rating[:26]} by {abs(edge_pts):.1f} pts "
                          f"(HFA-adj); market favors {market_fav[:26]} at {mkt_pct:.0f}%")
                    print("     Worth the card work. This is triage, not a pick.")
                else:
                    delta = rating_pct - mkt_pct
                    if abs(delta) >= 8:
                        side = "stronger" if delta > 0 else "weaker"
                        print(f"   Same side, different size: ratings say {fav_rating[:22]} is "
                              f"{abs(delta):.0f}% {side} than the market's {mkt_pct:.0f}%")
                        print("     Look at the dog price if ratings say weaker; at the favorite if stronger.")
                    else:
                        print(f"   Ratings and market agree ({market_fav[:24]}, ~{mkt_pct:.0f}%). "
                              f"No disagreement to exploit — likely a PASS.")
            except (ValueError, TypeError, KeyError):
                pass

        # ── weather
        away_keys = fullname_to_matchup.get(norm(m["away_team"]), set())
        home_keys = fullname_to_matchup.get(norm(m["home_team"]), set())
        wkey = next((k for k in (away_keys & home_keys) if k in weather), None)
        if wkey is None:  # fall back to loose substring for anything unmapped
            for k in weather:
                if norm(m["away_team"])[:6] in k and norm(m["home_team"])[:6] in k:
                    wkey = k
                    break
        print("\n  WEATHER")
        if wkey:
            w = weather[wkey]
            print(f"   {w['temperature_f']}°F · wind {w['wind']} {w['wind_direction']} · "
                  f"{w['precip_probability']}% precip · {w['short_forecast']}")
            print(f"   {w['venue']}, {w['city']} {w['state']} (forecast {w['forecast_time'][:16]})")
        else:
            print("   no forecast matched (dome, or venue not in coords file)")

        # ── QBs
        qa, qh = inj.get(norm(m["away_team"]), []), inj.get(norm(m["home_team"]), [])
        print("\n  QB STATUS")
        print(f"   {m['away_team'][:22]:22} {', '.join(qa) if qa else 'all QBs Active'}")
        print(f"   {m['home_team'][:22]:22} {', '.join(qh) if qh else 'all QBs Active'}")
        print()

    print("═" * 78)
    print("Win% is the market's opinion with vig removed — not our forecast.")
    print("Ratings are context for your read; backtests say they do not beat the close.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
