#!/usr/bin/env python3
"""Football + hockey data loop: slates, rationed odds, props, weather, dashboard.

Runs at 7:30 AM ET every day via com.davidos.football-data.plist (daily since
2026-10-07 for the NHL card; football odds stay on their weekday ration).
Tuesday pulls BOTH football leagues so the one weekly card ranks NFL and NCAA
together (AQ-222, 2026-09-18); Thursday, Saturday and Sunday pull the league
playing that day for the price check on Tuesday's card and for close
captures. NHL's 3-credit pull runs every morning. fetch-odds.py enforces a
12-hour per-league cooldown so a card run cannot duplicate the morning pull.
Decisions stay manual; this loop only moves data.

Every subprocess has a hard timeout (agents rule). A step failing does not stop
the others — the run log records per-step status and the dashboard's own
freshness labels do the user-facing honesty.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
VAULT = AGENT.parents[1]
sys.path.insert(0, str(VAULT / "04-agents" / "lib"))
import davidos_lib as dl  # noqa: E402
RUN_LOG = AGENT / "run-log.md"
PY = sys.executable
ET = ZoneInfo("America/New_York")

BEFORE_ODDS_STEPS = [
    ("nfl-slate", [PY, str(AGENT / "fetch-nfl-slate.py")], 60),
    ("cfb-slate", [PY, str(AGENT / "fetch-cfb-slate.py")], 60),
    # 2026-09-07: SP+ was frozen at the Aug 22 preseason pull while the board
    # rated September games with it (big-dog spreads 0-3 by 45+). CFBD is
    # free-tier and keyed from config.env; in-season SP+ lands here weekly.
    ("cfb-ratings", [PY, str(AGENT / "fetch-cfbd-ratings.py")], 150),
    # 2026-10-07: hockey joins the daily card; keyless ESPN schedule.
    ("nhl-slate", [PY, str(AGENT / "fetch-nhl-slate.py")], 60),
]

AFTER_ODDS_STEPS = [
    # 2026-09-01: lossless change-only compaction of the odds log right after
    # the paid pull, so every reader below opens the small file. Keeps the
    # newest pull, first row per key, all near-kickoff rows, every price change.
    ("compact-odds", [PY, str(AGENT / "compact-snapshots.py"), "--apply"], 120),
    # 2026-10-07: rationed player props for today's card (budget + reserve in
    # config.env; one pull per event per day, receipt in data/props-<date>.json).
    ("props", [PY, str(AGENT / "fetch-props.py"), "--league", "all"], 180),
    ("weather", [PY, str(AGENT / "fetch-weather.py")], 300),
    ("injuries", [PY, str(AGENT / "fetch-injuries.py")], 60),
    ("spot-flags", [PY, str(AGENT / "spot-flags.py")], 120),
    ("settle", [PY, str(AGENT / "settle-picks.py"), "--write"], 120),
    ("settle-props", [PY, str(AGENT / "settle-props.py"), "--write"], 180),
    # 2026-09-07: CFBD posts DraftKings' true closing spread/total/ML the day
    # after; grade-clv prefers it over the 7:30 AM snapshot for CFB rows.
    ("cfb-closes", [PY, str(AGENT / "fetch-cfbd-closing-lines.py")], 60),
    ("clv", [PY, str(AGENT / "grade-clv.py"), "--write"], 60),
    # AQ-271 (2026-10-02): edge.py ledger gets its Pinnacle close and result here.
    ("edge-close", [PY, str(AGENT / "edge.py"), "close"], 120),
    ("edge-settle", [PY, str(AGENT / "edge.py"), "settle"], 120),
    ("preflight", [PY, str(AGENT / "test-picks.py")], 60),
    ("packet", [PY, str(AGENT / "generate-football-packet.py")], 30),
    ("dashboard", [PY, str(VAULT / "00-system/scripts/refresh-football-dashboard.py")], 60),
]


CLOSE_WINDOW_HOUR = 10  # runs at/after this hour are near-kickoff "close" captures
CLOSE_COOLDOWN_HOURS = 2.0


def scheduled_odds_sports(now: datetime) -> list[str]:
    """One paid league pull per product day, plus near-kickoff close captures.

    2026-09-07: CLV is the KPI and it was being graded against a 7:30 AM
    snapshot, up to 13 hours before kickoff. Evening runs capture a real
    close. 2026-09-18 (AQ-222): Tuesday pulls both leagues for the single
    combined card. Credits: ~33/week (Tue 6, Thu 9, Sat 9, Sun 6, Mon 3)
    against a 500/month quota.
    """
    morning = now.hour < CLOSE_WINDOW_HOUR
    football = {
        0: [] if morning else ["nfl"],                  # Monday night close
        1: ["ncaaf", "nfl"],                            # Tuesday: the one weekly card, both leagues
        3: ["ncaaf"] if morning else ["ncaaf", "nfl"],  # Thu refresh / Thu night close
        5: ["ncaaf"],                                   # Saturday morning, 11 AM, 6 PM
        6: ["nfl"],                                     # Sunday morning, noon
    }.get(now.weekday(), [])
    # 2026-10-07: NHL plays daily, so its 3-credit pull runs every morning for
    # the daily card (~90 credits/month). Closes come from the free feed.
    return (["nhl"] if morning else []) + football


def build_steps(now: datetime, sports: list[str], force_odds: bool = False) -> list[tuple]:
    odds_steps = []
    for sport in sports:
        cmd = [PY, str(AGENT / "fetch-odds.py"), "--sport", sport]
        if force_odds:
            cmd.append("--force")
        elif now.hour >= CLOSE_WINDOW_HOUR:
            # The 12h default cooldown would skip every same-day close capture.
            cmd += ["--cooldown-hours", str(CLOSE_COOLDOWN_HOURS)]
        odds_steps.append((f"odds-{sport}", cmd, 90))
    before = list(BEFORE_ODDS_STEPS)
    # 2026-10-13: SP+ is reviewer context only (no ratings gate since 09-28),
    # so the 150s CFBD pull runs once a week, Tuesday, not every morning.
    if now.weekday() != 1:
        before = [s for s in before if s[0] != "cfb-ratings"]
    after = list(AFTER_ODDS_STEPS)
    # Keep the hot odds log small: archive games older than 30 days right after compaction.
    i = next((k for k, s in enumerate(after) if s[0] == "compact-odds"), None)
    if i is not None:
        after.insert(i + 1, ("archive-odds", [PY, str(AGENT / "archive-snapshots.py"), "--apply"], 120))
    return before + odds_steps + after


def load_config_env() -> dict:
    """launchd inherits no shell env; config.env is the gitignored key store."""
    env = dict(__import__("os").environ)
    cfg = AGENT / "config.env"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    env[k.strip()] = v.strip()
    return env


def nfl_slate_needs_advance() -> list[str]:
    """ESPN's scoreboard default lags after a week completes: the 2026-08-16
    17:00 run overwrote a hand-pulled wk3 slate with wk2 (all finished games),
    emptying the dashboard's NFL tab. If every fetched kickoff is in the past,
    return args for the NEXT week so the runner self-heals."""
    import csv
    path = AGENT / "data" / "nfl-schedule.csv"
    if not path.exists():
        return []
    rows = list(csv.DictReader(path.open()))
    if not rows:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    if any((r.get("kickoff_et", "")[:10] >= today) for r in rows):
        return []
    stype = {"preseason": "1", "regular": "2", "postseason": "3"}.get(
        rows[0].get("season_type", ""), "2")
    try:
        next_week = int(rows[0].get("week_number", "")) + 1
    except ValueError:
        # Empty or unparseable week: bail rather than re-fetch week 1.
        return []
    return ["--seasontype", stype, "--week", str(next_week)]


def main() -> int:
    if not dl.scheduled_run_guard("football-data"):  # AQ-262: scheduled-loop guardrail
        return 3
    parser = argparse.ArgumentParser(description="Refresh football data with rationed league odds.")
    parser.add_argument(
        "--odds-sport",
        choices=["nfl", "ncaaf", "nhl"],
        action="append",
        help="repeatable override for the weekday league schedule",
    )
    parser.add_argument("--no-odds", action="store_true", help="run free data steps only")
    parser.add_argument(
        "--force-odds",
        action="store_true",
        help="bypass the paid-pull cooldown for an explicitly required new snapshot",
    )
    args = parser.parse_args()

    env = load_config_env()
    now = datetime.now(ET)
    started = now.strftime("%Y-%m-%d %H:%M")
    sports = [] if args.no_odds else (args.odds_sport or scheduled_odds_sports(now))
    steps = build_steps(now, sports, args.force_odds)
    lines, failures = [], 0
    for name, cmd, timeout in steps:
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                               timeout=timeout, cwd=str(VAULT))
            tail = (r.stdout or r.stderr).strip().splitlines()
            summary = tail[-1][:160] if tail else ""
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok, summary = False, f"timeout after {timeout}s"
        except Exception as exc:  # noqa: BLE001
            ok, summary = False, f"error: {exc}"
        if name == "nfl-slate" and ok:
            adv = nfl_slate_needs_advance()
            if adv:
                try:
                    r2 = subprocess.run(cmd + adv, env=env, capture_output=True,
                                        text=True, timeout=timeout, cwd=str(VAULT))
                    ok = r2.returncode == 0
                    summary += f" | advanced to wk{adv[-1]}: {'ok' if ok else 'failed'}"
                except subprocess.TimeoutExpired:
                    ok, summary = False, summary + f" | advance timeout after {timeout}s"
                except Exception as exc:  # noqa: BLE001
                    ok, summary = False, summary + f" | advance error: {exc}"
        failures += 0 if ok else 1
        lines.append(f"- {'✅' if ok else '❌'} `{name}` — {summary}")

    # AQ-070: shared lib helper — newest entry on top, overflow archived, cap 35.
    odds_note = ", ".join(sports).upper() if sports else "none"
    entry = (f"## {started} — football data loop ({len(steps) - failures}/{len(steps)} steps ok; paid odds: {odds_note})\n\n"
             + "\n".join(lines) + "\n\n")
    dl.prepend_run_log(RUN_LOG, entry, title="football-picks-agent run log", max_entries=15)
    print(f"football data loop: {len(steps) - failures}/{len(steps)} ok; paid odds: {odds_note}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
