# Lessons — what the record already taught us (auto-maintained by `football.py learn`)

These are encoded as rules, not remembered as advice:

1. **Model-born totals lost** (CFB totals 11-21, 34% beat-close through 2026-09-18;
   the SP+ blend lost its own backtest). Rule: totals are only ever price-born
   and carry at most `TOTALS_MAX_UNITS` (config.env, default 1).
2. **Ratings never beat the close** (Elo, SRS, pace, cheap-EPA all lost). Rule:
   no ratings gate anywhere; candidates come from the odds log; NFL is live.
3. **Confidence weights ordered noise** (correlation with CLV 0.05; the 80+ bucket
   was negative). Rule: rank by win % among edge-qualified rows; size from edge.
4. **Price chasing** (movement-toward-us bonus bought worse numbers). Rule:
   `check` flags a listed number that is gone; nothing is re-rated upward.
5. **Sizing outran evidence** (2u on an unvalidated tier lost the week). Rule:
   2u only at >=2c edge; caps live in staking-config.md and are enforced in code.
6. **Unlogged boards teach nothing** (Sep 26-27 wrote zero ledger rows). Rule:
   every qualified row is logged, PICK or LEAN, before it is shown.

Any league x market slice of the new process that is under water on 30+ graded
rows (beat-close < 45% and negative flat ROI) is blocked from the board by
`learned-rules.json` until it recovers. Legacy rows are history, shown below,
never re-litigated.


## Auto report — 2026-10-13T21:11

New-process rows: 77 · legacy rows: 81 · verdicts need 30 graded rows per slice.


## New process (football.py)

### league × market

| slice | n | W-L-P | flat ROI | beat close | CLV |
|---|---:|---|---:|---:|---:|
| CFB player_anytime_td | 1 | 1-0-0 | +0.56u (+55.6%) | — | — |
| CFB spreads | 25 | 11-13-0 | -2.88u (-12.0%) | 42% (24) | -1.86% |
| CFB totals | 1 | 0-1-0 | -1.00u (-100.0%) | 0% (1) | -2.07% |
| NFL player_anytime_td | 1 | 0-1-0 | -1.00u (-100.0%) | — | — |
| NFL spreads | 34 | 18-14-2 | +2.28u (+6.7%) | 28% (25) | -2.38% |
| NFL totals | 1 | 1-0-0 | +0.85u (+84.7%) | — | — |
| NHL h2h | 1 | 0-1-0 | -1.00u (-100.0%) | — | — |
| NHL player_points | 1 | 0-0-0 | +0.00u | — | — |
| NHL spreads | 10 | 6-4-0 | +2.11u (+21.1%) | — | — |
| NHL totals | 2 | 1-1-0 | -0.23u (-11.5%) | — | — |

### edge × bucket

| slice | n | W-L-P | flat ROI | beat close | CLV |
|---|---:|---|---:|---:|---:|
| 2-3c | 1 | 0-1-0 | -1.00u (-100.0%) | — | — |
| <1.5c | 76 | 38-34-2 | +0.69u (+0.9%) | 34% (50) | -2.12% |


## Legacy process (history only)

### league × market

| slice | n | W-L-P | flat ROI | beat close | CLV |
|---|---:|---|---:|---:|---:|
| CFB h2h | 13 | 9-4-0 | +1.08u (+8.3%) | 38% (13) | -0.80% |
| CFB spreads | 22 | 11-11-0 | -0.97u (-4.4%) | 45% (22) | -0.24% |
| CFB totals | 32 | 11-21-0 | -10.80u (-33.7%) | 56% (32) | +0.55% |
| NFL h2h | 2 | 2-0-0 | +1.30u (+64.8%) | 100% (2) | +8.04% |
| NFL spreads | 6 | 5-0-1 | +4.75u (+79.2%) | 67% (6) | +3.60% |
| NFL totals | 6 | 2-4-0 | -2.15u (-35.8%) | 50% (6) | -0.09% |

### edge × bucket

| slice | n | W-L-P | flat ROI | beat close | CLV |
|---|---:|---|---:|---:|---:|
| 1.5-2c | 8 | 6-2-0 | +2.44u (+30.5%) | 50% (8) | +1.08% |
| 2-3c | 9 | 7-1-1 | +5.27u (+58.6%) | 44% (9) | +0.78% |
| 3c+ | 3 | 1-2-0 | -1.00u (-33.3%) | 67% (3) | +4.55% |
| <1.5c | 4 | 2-2-0 | -0.40u (-10.1%) | 50% (4) | +0.39% |
| ? | 57 | 24-33-0 | -13.09u (-23.0%) | 53% (57) | +0.14% |
