#!/usr/bin/env python3
"""settle-picks.py — grade every logged selection W/L, tailed or not.

    python3 -B 04-agents/football-picks-agent/settle-picks.py            # report
    python3 -B 04-agents/football-picks-agent/settle-picks.py --write    # fill results

WHY (2026-08-28). David: "track each set of picks... track w/l even if I don't
tail. That will make you sharp over time."

Exactly right, and it is the difference between a tool and a system that
learns. Selections that never got a wager are still predictions, and a
prediction nobody grades is a prediction nobody can improve. Grading only the
bets David placed would sample on the outcome he was most confident about —
the classic way a betting record flatters itself.

So every row in recommendation-ledger.csv gets settled from real final scores,
whether or not money was on it.

WHAT THIS MEASURES THAT CLV CANNOT
----------------------------------
grade-clv.py asks "did we take a better number than the close?" — a leading
indicator, fast, available before results. This asks "were we right?" — slow,
noisy, but the thing being predicted.

The valuable output is not the overall record. It is the **hit rate by
confidence tier**. The confidence score is a hypothesis: that 5★ plays win
more than 3★ plays. If they do, the ranking carries information and can be
tuned. If 5★ and 2★ land the same, the score is decoration and should be
rebuilt or removed. That comparison needs every pick graded, including the
ones that looked bad and were skipped.

Breakeven at -110 is 52.4%. A tier below that is losing money at scale
regardless of how it feels.

SOURCES
  CFB — CollegeFootballData /games (key already in config.env)
  NFL — nflverse games.csv, keyless, same source as the SRS ratings

Never places a bet. Only writes result columns for games that are final.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402
from football_performance import latest_recommendations  # noqa: E402

LEDGER = AGENT / "recommendation-ledger.csv"
CONFIG = AGENT / "config.env"
NFL_GAMES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CFBD = "https://api.collegefootballdata.com/games"
ESPN_CFB = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
TIMEOUT = 30
BREAKEVEN = 52.4


def cfg(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    if CONFIG.exists():
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


def norm(s: str) -> str:
    # ASCII-fold first: Hawai'i / Hawaiʻi / Hawaii must all normalize equal
    # (the 2026-08-29 apostrophe lesson — a missed game is a silent miss).
    import unicodedata as _ud
    s = _ud.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    folded = "".join(c for c in s.lower() if c.isalnum())
    return {
        "appalachianstate": "appstate",
        "appalachianstatemountaineers": "appstatemountaineers",
    }.get(folded, folded)


def cfb_scores(year: int) -> dict:
    key = cfg("COLLEGE_FOOTBALL_DATA_API_KEY")
    if not key:
        return espn_cfb_scores(year) if year == now_eastern().year else {}
    out = {}
    for kind in ("regular", "postseason"):
        url = f"{CFBD}?year={year}&seasonType={kind}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                games = json.load(r)
        except Exception:  # noqa: BLE001 — a missing feed must not break settling
            continue
        for g in games:
            if g.get("completed") is False:
                continue
            # CFBD returns camelCase (awayPoints); accept snake_case too in case
            # the API changes back, rather than silently settling nothing.
            hp = g.get("homePoints", g.get("home_points"))
            ap = g.get("awayPoints", g.get("away_points"))
            if hp is None or ap is None:
                continue
            away = g.get("awayTeam", g.get("away_team"))
            home = g.get("homeTeam", g.get("home_team"))
            out[(norm(away), norm(home))] = (ap, hp)
    # CFBD can lag or fail silently on the same night a slate completes. ESPN's
    # keyless scoreboard is already the schedule authority in this agent, so
    # merge its recent completed games before reporting or writing settlement.
    # Keep CFBD as the season-history source and use ESPN only for the current
    # eight-day window so one settlement run stays lightweight.
    if year == now_eastern().year:
        out.update(espn_cfb_scores(year))
    return out


def parse_espn_cfb_scores(data: dict) -> dict:
    """Return completed ESPN CFB games keyed by normalized away/home names."""
    out = {}
    for event in data.get("events", []):
        competitions = event.get("competitions", [])
        comp = competitions[0] if competitions else {}
        status = comp.get("status", {}).get("type", {})
        if not (status.get("completed") or status.get("name") == "STATUS_FINAL"):
            continue
        competitors = comp.get("competitors", [])
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        try:
            ap = int(float(away.get("score")))
            hp = int(float(home.get("score")))
        except (TypeError, ValueError):
            continue
        away_name = away.get("team", {}).get("displayName") or away.get("team", {}).get("location")
        home_name = home.get("team", {}).get("displayName") or home.get("team", {}).get("location")
        if away_name and home_name:
            out[(norm(away_name), norm(home_name))] = (ap, hp)
    return out


def espn_cfb_scores(year: int, days: int = 8) -> dict:
    today = now_eastern().date()
    start = max(today - timedelta(days=days), today.replace(month=1, day=1))
    params = urlencode({
        "groups": "80",
        "dates": f"{start:%Y%m%d}-{today:%Y%m%d}",
        "limit": "1000",
    })
    req = urllib.request.Request(f"{ESPN_CFB}?{params}", headers={"User-Agent": "curl/8.4.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - CFBD results remain usable if fallback fails
        return {}
    return parse_espn_cfb_scores(data)


def nfl_scores(year: int) -> dict:
    try:
        with urllib.request.urlopen(NFL_GAMES, timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for g in csv.DictReader(io.StringIO(text)):
        if g.get("season") != str(year):
            continue
        try:
            ap, hp = int(g["away_score"]), int(g["home_score"])
        except (ValueError, KeyError, TypeError):
            continue
        out[(norm(g.get("away_team")), norm(g.get("home_team")))] = (ap, hp)
    # nflverse lags a week and keys by abbreviation ("LA"), which the prefix
    # lookup cannot match to "Los Angeles Rams". ESPN's keyless scoreboard has
    # finals the same night under full names, so the current window overrides
    # (2026-10-13: six Oct 4 spreads sat open nine days).
    if year == now_eastern().year:
        out.update(espn_nfl_scores())
    return out


ESPN_NFL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


def espn_nfl_scores(days: int = 10) -> dict:
    """Finals from ESPN's NFL scoreboard, one date per request like hockey."""
    end = now_eastern().date()
    events = []
    for back in range(days + 1):
        day = end - timedelta(days=back)
        url = f"{ESPN_NFL}?dates={day:%Y%m%d}&limit=100"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                events += json.load(resp).get("events", [])
        except Exception:  # noqa: BLE001 — a dead feed leaves rows pending, never guessed
            continue
    out = {}
    for e in events:
        comp = (e.get("competitions") or [{}])[0]
        if ((comp.get("status") or {}).get("type") or {}).get("name") != "STATUS_FINAL":
            continue
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        try:
            away, home = sides["away"], sides["home"]
            out[(norm(away["team"]["displayName"]), norm(home["team"]["displayName"]))] = (
                int(away["score"]), int(home["score"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


ESPN_NHL = "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"


def nhl_scores(days: int = 5) -> dict:
    """Finals from ESPN's NHL scoreboard over the last `days` (keyless, 2026-10-07).
    ESPN's hockey scoreboard takes one date per request (a range is HTTP 400),
    so this is `days`+1 small GETs. Hockey moneylines include OT/shootout, so
    the final score decides them."""
    end = now_eastern().date()
    events = []
    for back in range(days + 1):
        day = end - timedelta(days=back)
        url = f"{ESPN_NHL}?dates={day:%Y%m%d}&limit=100"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                events += json.load(resp).get("events", [])
        except Exception:  # noqa: BLE001 — a dead feed leaves rows pending, never guessed
            continue
    out = {}
    for e in events:
        comp = (e.get("competitions") or [{}])[0]
        if ((comp.get("status") or {}).get("type") or {}).get("name") != "STATUS_FINAL":
            continue
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        try:
            away, home = sides["away"], sides["home"]
            out[(norm(away["team"]["displayName"]), norm(home["team"]["displayName"]))] = (
                int(away["score"]), int(home["score"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def lookup(scores: dict, away: str, home: str):
    """Allow full school-name prefixes only when one score pair matches."""
    a, h = norm(away), norm(home)
    if (a, h) in scores:
        return scores[(a, h)]
    matches = [v for (sa, sh), v in scores.items()
               if sa and sh and (a.startswith(sa) or sa.startswith(a))
               and (h.startswith(sh) or sh.startswith(h))]
    return matches[0] if len(matches) == 1 else None


def picked_home(selection: str, away: str, home: str) -> bool | None:
    """Ledger selections use the same complete team names as the matchup."""
    selected = norm(selection)
    if selected == norm(home) and selected != norm(away):
        return True
    if selected == norm(away) and selected != norm(home):
        return False
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--year", type=int, default=now_eastern().year)
    args = ap.parse_args()

    rows = list(csv.DictReader(LEDGER.open(encoding="utf-8")))
    if not rows:
        print("Ledger is empty — nothing to settle yet.")
        return 0

    cfb = cfb_scores(args.year)
    nfl = nfl_scores(args.year)
    nhl = nhl_scores()
    print(f"score feeds: {len(cfb)} CFB · {len(nfl)} NFL final(s) for {args.year} · {len(nhl)} NHL final(s), last 5 days\n")

    settled, pending = 0, 0
    for r in rows:
        if r.get("result"):
            settled += 1
            continue
        matchup = r.get("matchup", "")
        if " @ " not in matchup:
            continue
        away, home = matchup.split(" @ ", 1)
        lg = (r.get("league") or "").upper()
        table = nfl if lg == "NFL" else nhl if lg == "NHL" else cfb
        got = lookup(table, away, home)
        if not got:
            pending += 1
            continue
        ascore, hscore = got
        market = (r.get("market") or "").lower()
        sel = r.get("selection", "")

        if market in ("h2h", "moneyline"):
            home_side = picked_home(sel, away, home)
            if home_side is None:
                pending += 1
                continue
            won = (hscore > ascore) if home_side else (ascore > hscore)
            res = "PUSH" if hscore == ascore else ("W" if won else "L")
        elif market == "totals":
            try:
                line = float(r.get("line_points") or 0)
            except ValueError:
                continue
            total = ascore + hscore
            if total == line:
                res = "PUSH"
            elif sel.lower().startswith("o"):
                res = "W" if total > line else "L"
            else:
                res = "W" if total < line else "L"
        elif market == "spreads":
            try:
                line = float(r.get("line_points") or 0)
            except ValueError:
                continue
            home_side = picked_home(sel, away, home)
            if home_side is None:
                pending += 1
                continue
            margin = (hscore - ascore) if home_side else (ascore - hscore)
            adj = margin + line
            res = "PUSH" if adj == 0 else ("W" if adj > 0 else "L")
        else:
            continue

        r["result"] = res
        settled += 1

    unique = latest_recommendations(rows)
    graded = [r for r in unique if r.get("result") in ("W", "L", "PUSH")]
    pending = sum(not r.get("result") for r in unique)
    print(f"{len(unique)} unique selection(s) · {len(graded)} settled · {pending} awaiting final score "
          f"({len(rows)} publication/audit rows)\n")
    if not graded:
        print("  Nothing final yet. Re-run after games complete.")
        return 0

    def record(sub):
        w = sum(1 for r in sub if r["result"] == "W")
        l = sum(1 for r in sub if r["result"] == "L")
        p = sum(1 for r in sub if r["result"] == "PUSH")
        pct = w / (w + l) * 100 if (w + l) else 0.0
        return w, l, p, pct

    w, l, p, pct = record(graded)
    print(f"  OVERALL  {w}-{l}" + (f"-{p}" if p else "") + f"  ({pct:.1f}%)")
    print(f"           breakeven at -110 is {BREAKEVEN}%\n")

    # Ranking v3 (2026-09-18): the live hypothesis is the edge in cents.
    etiers = defaultdict(list)
    for r in graded:
        try:
            e = float(r.get("edge_cents") or "")
        except ValueError:
            continue
        etiers["≥2.0¢ (2u)" if e >= 2.0 else "1.5-2.0¢ (1u)" if e >= 1.5 else
               "1.0-1.5¢ board" if e >= 1.0 else "<1.0¢ board"].append(r)
    if etiers:
        print("  BY EDGE TIER — the live hypothesis (ranking since 2026-09-18)")
        print(f"    {'tier':14} {'record':>10} {'hit%':>7}   n")
        for tier in ("≥2.0¢ (2u)", "1.5-2.0¢ (1u)", "1.0-1.5¢ board", "<1.0¢ board"):
            if tier in etiers:
                tw, tl, tp, tpct = record(etiers[tier])
                print(f"    {tier:14} {f'{tw}-{tl}':>10} {tpct:>6.1f}%   {tw+tl}")
        print()

    # Legacy confidence composite, ranked on until 2026-09-18; kept one month for comparison.
    tiers = defaultdict(list)
    for r in graded:
        try:
            c = int(float(r.get("confidence") or 0))
        except ValueError:
            continue
        tier = "5★ 80+" if c >= 80 else "4★ 65-79" if c >= 65 else \
               "3★ 50-64" if c >= 50 else "2★ 35-49" if c >= 35 else "1★ <35"
        tiers[tier].append(r)
    if tiers:
        print("  BY LEGACY CONFIDENCE TIER — not ranked on since 2026-09-18; comparison only")
        print(f"    {'tier':10} {'record':>10} {'hit%':>7}   n")
        print(f"    {'-'*10} {'-'*10} {'-'*7}   -")
        for tier in ("5★ 80+", "4★ 65-79", "3★ 50-64", "2★ 35-49", "1★ <35"):
            if tier not in tiers:
                continue
            tw, tl, tp, tpct = record(tiers[tier])
            flag = ""
            if tw + tl >= 20:
                flag = "  ✓ above breakeven" if tpct > BREAKEVEN else "  ✗ below breakeven"
            print(f"    {tier:10} {f'{tw}-{tl}':>10} {tpct:>6.1f}%   {tw+tl}{flag}")
        if sum(len(v) for v in tiers.values()) < 40:
            print("\n    Too few settled to read the tiers. What you want to see eventually:")
            print("    hit% rising with tier. If 5★ and 2★ land together, the confidence")
            print("    score carries no information and needs rebuilding.")

    for label, keyfn in (("BY MARKET", lambda r: (r.get("market") or "?").upper()),
                         ("BY LEAGUE", lambda r: (r.get("league") or "?").upper())):
        buckets = defaultdict(list)
        for r in graded:
            buckets[keyfn(r)].append(r)
        if len(buckets) > 1:
            print(f"\n  {label}")
            for k, sub in sorted(buckets.items()):
                bw, bl, bp, bpct = record(sub)
                print(f"    {k:10} {f'{bw}-{bl}':>10} {bpct:>6.1f}%   {bw+bl}")

    if args.write:
        fields = list(rows[0].keys())
        with LEDGER.open("w", newline="", encoding="utf-8") as f:
            wtr = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            wtr.writeheader()
            wtr.writerows(rows)
        print(f"\n  wrote results into {LEDGER.name}")
    else:
        print("\n  (report only — re-run with --write to save results)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
