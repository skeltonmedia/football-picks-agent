#!/usr/bin/env python3
"""run-weekly-card.py — the locked weekly format, start to finish.

    python3 -B 04-agents/football-picks-agent/run-weekly-card.py            # Tuesday: the one weekly card
    python3 -B 04-agents/football-picks-agent/run-weekly-card.py --check    # Thu/Sat/Sun: price check only

WHY (2026-08-28). David: "This is perfect let's lock in the format."
2026-09-18 (AQ-222): one combined NFL + NCAA card a week, ranked on Tuesday
over both leagues. Every other day runs --check, which re-prices Tuesday's
picks and says "still playable" or "number gone"; it never ranks or logs.

Locks the Thursday routine into one command so the format cannot drift week to
week and cannot be skipped when the slate gets loud:

  1. refresh data      run-daily-data.py — odds, schedules, injuries, weather
  2. select            weekly-picks.py — global top 10 by confidence across
                       spread, moneyline, over, and under, with DraftKings price,
                       FanDuel compared, and ledger rows for CLV grading
  3. grade last week   grade-clv.py — did previous selections beat the close?
  4. write the card    05-outputs/reports/football-picks-agent/weekly-card-<date>.md

Text-only output is the default; visual rendering is retired from this runner.

The card is the artifact David reads. Everything else is plumbing.

Staking, per staking-config.md: $2 units; units come from the edge in cents
(AQ-221): under the floor 0, at the floor 1u, at the two-unit line 2u; max
4/day, max 10/week. The card prints total exposure so the cap is a decision,
never an accident.

Never places a bet.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern, prepend_run_log  # noqa: E402

OUT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"
RUN_LOG = AGENT / "run-log.md"
TIMEOUT = 300
TOTAL_STEPS = 4


def run(script: str, *flags: str) -> tuple[bool, str]:
    try:
        p = subprocess.run([sys.executable, "-B", str(AGENT / script), *flags],
                           capture_output=True, text=True, timeout=TIMEOUT, cwd=ROOT)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"{script} exceeded {TIMEOUT}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--no-log", action="store_true", help="do not append to the ledger")
    ap.add_argument("--check", action="store_true",
                    help="price-check Tuesday's card instead of ranking (Thu/Sat/Sun)")
    args = ap.parse_args()

    stamp = now_eastern()
    print(f"── weekly card · {stamp.strftime('%a %b %-d, %-I:%M %p ET')}")

    print("── 1/4 refreshing data…")
    ok_data, data_out = run("run-daily-data.py")
    print("   " + (data_out.strip().splitlines() or ["no output"])[-1])

    if args.check:
        # AQ-222: game-day runs re-price the Tuesday cohort; they do not rank.
        # The stale-odds gate below protects a BOARD; a price check prints the
        # capture age on every row instead and is never a reason to rank.
        ok_chk, chk_out = run("price-check.py", "--write")
        print(chk_out.rstrip())
        prepend_run_log(RUN_LOG, "price check on the Tuesday card: " + ("ok" if ok_chk else "FAILED"),
                        "football-picks-agent — run log (newest first)")
        return 0 if ok_chk else 1

    # ── pre-flight gate (2026-08-29, "how do we make this reliable"):
    # bad data means NO board. A wrong card is worse than no card — the
    # Dublin-Ohio forecast and the stale-odds "steady" read both shipped
    # because nothing stood between questionable data and the output.
    ok_gate, gate_out = run("test-picks.py")
    print(gate_out.rstrip())
    if not ok_gate:
        print("\n✗ pre-flight FAILED — no selections made, nothing logged.")
        print("  Fix the data (or wait for quota reset) and re-run.")
        prepend_run_log(RUN_LOG, "weekly card ABORTED at pre-flight gate",
                        "football-picks-agent — run log (newest first)")
        return 1

    print("── 2/4 grading last week's selections…")
    ok_clv, clv_out = run("grade-clv.py", "--write")
    # W/L on every prior pick, tailed or not — the record that makes the
    # confidence score falsifiable over time.
    ok_settle, settle_out = run("settle-picks.py", "--write")
    print("   " + (settle_out.strip().splitlines() or ["no output"])[0])

    print("── 3/4 selecting…")
    sel_flags = ["--count", str(args.count), "--hours", str(args.hours)]
    if not args.no_log:
        sel_flags.append("--log")
    ok_sel, sel_out = run("weekly-picks.py", *sel_flags)
    print(sel_out.rstrip())
    # Paste-ready block for iMessage, generated from the same selection run.
    _, im_out = run("weekly-picks.py", "--count", str(args.count),
                    "--hours", str(args.hours), "--imessage")

    ledger_note = (
        "This run was not appended to the recommendation ledger; the Tuesday card "
        "is the only CLV cohort (AQ-222)."
        if args.no_log else
        "These selections were appended to the recommendation ledger for CLV grading. "
        "This is the week's one ranked card, NFL and NCAA together; later days only re-price it."
    )

    print("── 4/4 writing card…")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    card = OUT_DIR / f"weekly-card-{stamp.date().isoformat()}.md"
    body = f"""---
title: Daddy Dan's Picks — {stamp.strftime('%b %-d, %Y')}
type: agent-report
agent: football-picks-agent
date: {stamp.date().isoformat()}
---

# Daddy Dan's Picks — {stamp.strftime('%a %b %-d, %Y')}

*Generated {stamp.strftime('%-I:%M %p ET')} by `run-weekly-card.py`. Format locked
2026-08-29; ranking v3 (price edge) 2026-09-18. Staking per `staking-config.md`:
units from the edge (≥2¢ = 2 units, ≥1.5¢ = 1 unit, under the floor = board only),
max 4/day, max 10/week.*

**Tracking:** {ledger_note}

## Paste to iMessage

```
{im_out.rstrip()}
```

## Full board

```
{sel_out.rstrip()}
```

## Record — every pick, tailed or not

```
{settle_out.rstrip()}
```

## Closing-line value on prior selections

```
{clv_out.rstrip()}
```

## Data freshness

```
{data_out.strip()}
```

## How to use this

1. This is the week's **one card, NFL and NCAA ranked together** (AQ-222): the
   global top-10 by **edge** (DraftKings versus the consensus of the other
   books, in cents) when ten live candidates qualify, not an instruction to
   place ten bets. Rows under the edge floor print as board only. Spread, Moneyline, Over, and Under are the eligible play
   types. Multiple markets from the same game can qualify; treat them as
   correlated. Thursday, Saturday and Sunday re-price these rows
   (`run-weekly-card.py --check`) and never add new ones.
2. The **side** is named; the **DraftKings price** is your baseline. Where
   FanDuel is flagged as better, that gap is free EV on an identical bet.
3. Do the card work before you place: confirm QBs, weather, and whether
   anything moved since this ran. `slate.py --team <name>` gives the briefing.
4. Use the printed sizing: **≥2¢ = 2 units; ≥1.5¢ = 1 unit; under 1.5¢ = board
   only, no units.** Fan-team
   games (Ohio State, Tennessee Titans) stay capped at 1 unit and need their
   case written first. The top 10 stays visible even when its summed units
   exceed the cap; never cross the 4-unit daily or 10-unit weekly cap.
5. Log real wagers in `bet-ledger.csv`. {ledger_note}

**This is a rules-based shortlist, not a proven edge.** Ranking is the price
edge: DraftKings versus the consensus of the other books, in cents of implied
probability, the one signal that predicted closing-line value on 1,106
game-markets from our own line log (`backtest-signals.py`, 2026-09-18). The
old confidence score is logged beside it for comparison. No manual or
market-type reorder follows it. The test is average CLV and beat-close rate by
edge bucket (`calibration-report.py`), not a week of wins.
"""
    card.write_text(body, encoding="utf-8")
    print(f"   {card.relative_to(ROOT)}")

    # CLV and settlement share the grading step; neither may be silently lost.
    steps = sum([ok_data, ok_clv and ok_settle, ok_sel, True])
    prepend_run_log(RUN_LOG,
                    f"weekly card: {steps}/{TOTAL_STEPS} steps ok -> {card.name}",
                    "football-picks-agent — run log (newest first)")
    print(f"\nweekly card: {steps}/{TOTAL_STEPS} steps ok")
    return 0 if steps == TOTAL_STEPS else 1


if __name__ == "__main__":
    sys.exit(main())
