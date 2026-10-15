# Staking Config — Fall 2026

**Source of truth for bankroll, unit size, and discipline rules.** The dashboard
refresh script and the agent both read this file. Change numbers here, never in code.

Approved by David 2026-08-16 ("Right now my units are $2 and $200 bankroll…
don't let me bet with emotion") — this satisfies the manifest's
"ask David before using staking guidance" escalation.

## Bankroll

| Setting | Value |
|---|---|
| starting_bankroll | $200.00 |
| unit_size | $2.00 |
| unit_percent | 1.0% of starting bankroll |
| season | Fall 2026 (NFL + FBS) |

A $2 unit on a $200 roll is a genuinely disciplined 1% — the config's job is to
keep it that way when the season gets loud.

## Stake sizing

- Default stake: **1 unit.**
- **Edge engine paper gate (AQ-271, David 2026-10-02):** every `edge.py` flag
  carries **0 units** until the ledger holds `EDGE_PAPER_FLAGS` (100) closed
  flags. Then 1 unit flat only if beat-close ≥ `EDGE_PAPER_GATE_PCT` (54%);
  under that, stay at 0 and the engine has no edge. Fan-team leans (Ohio
  State, Titans) stay 1u with the case written first, outside the engine.
- **Game-day cash confidence is not a staking edge (David, 2026-09-20;
  expanded via AQ-257 on 2026-09-27):** a higher market-implied chance to win
  determines user-facing Saturday NCAA and Sunday NFL rank, but it does not
  by itself authorize more money. A reviewed `PICK` defaults to 1 unit; 2 units
  still requires separately documented price/matchup edge and room under every
  cap. `WATCH` and `PASS` are always 0 units. The `-250` price ceiling remains.
- **Edge sizing (AQ-221, approved 2026-09-18; replaces confidence sizing of
  2026-08-29):** units come from the pick's edge versus the book consensus, in
  cents: **≥ EDGE_TWO_UNIT_CENTS (2.0) = 2 units, ≥ EDGE_FLOOR_CENTS (1.5) =
  1 unit, under the floor = 0 units ("board only")**. Floors live in
  `config.env` and came from `backtest-signals.py` (≥1.5¢ beat the close 69%
  of 230 game-markets; ≥2.0¢ 73% of 175). Some weeks that is three bets;
  some weeks none. This uses the existing 2-unit single-bet maximum.
- **CFB total fail-closed rule (David, 2026-09-26):** an automated college
  Over/Under may rank in the visible top ten by cross-book price edge, but it
  carries **0 units** until a completed manual fair-total review independently
  checks the market-implied team scores, each offense's plausible ceiling and
  floor against opponent quality, blowout/garbage-time paths, current
  availability, weather, and movement. The SP+ blend is screening context,
  not the fair total: its leakage-free 2025 backtest lost to the closing line.
  A manually promoted CFB total is capped at **1 unit** until an out-of-sample
  totals model beats the market. Price edge alone never authorizes 2 units.
- **Visibility is not allocation (David, 2026-09-01):** the graphic always
  keeps the ranked top 10 when ten live markets exist. Every row shows its
  edge-derived size: **≥2¢ = 2 units, ≥1.5¢ = 1 unit, else 0**. Entries
  labeled `LEAN` outside the cap-compliant suggested stake shortlist may
  append **`if played`**. A manual `PASS` or `WATCH` always shows **0 units**
  (David, 2026-09-20), regardless of edge-derived size. Conditional units are
  not allocated and do not count toward exposure unless David elects to add
  the play; every cap below still applies.
- Maximum stake on any single bet: **2 units**, and only when the ledger's edge
  column clears the model threshold with room.
- Maximum daily exposure: **4 units** across all pending bets.
- Maximum weekly exposure: **10 units.** When 10 are staked, the week is done —
  no Sunday-night rescue bets.
- Unit size **never changes mid-season.** Recalculate only between seasons, from
  actual ending bankroll, still at ~1%.

## Discipline rules (the "don't let me bet with emotion" contract)

1. **Fan-team flag — Ohio State and Tennessee Titans.** Any selection ON or
   AGAINST either team is tagged `fan_team=YES` in the bet ledger. Fan-team bets:
   - are capped at **1 unit**, always;
   - require the agent's edge case to be written down *before* the bet, not after;
   - are tracked as their own cohort on the dashboard. If the fan cohort
     underperforms the rest of the ledger, the dashboard says so in plain text.
   Betting against them counts too — spite is also emotion.
2. **No chasing.** Stakes never increase after a loss. A losing day changes
   nothing about the next day's sizing. Martingale thinking is how $200 becomes $0.
3. **Stop-loss:** if the bankroll draws down **20% ($40 / 20 units)** from its
   season high, betting pauses until David and the agent review the ledger's
   process grades together. The pause is the feature.
4. **No parlays by default.** Straight bets only. A parlay requires David to say
   so explicitly, counts its full risk against daily/weekly caps, and is never
   fan-team.
5. **PASS is a position.** The agent's best output is often no bet. A week with
   zero bets and an intact bankroll is a won week.
6. **Every wager gets a ledger row before kickoff.** Unlogged bets don't exist to
   the system, which means the system can't protect the bankroll. Log first, then bet.
7. **Chat advice is cap-aware.** Before a manual night card states an aggregate
   stake, read `bet-ledger.csv`. If David's earlier wagers are not present,
   exposure is unknown: show new sizes as conditional, do not call them the
   “actual card,” and ask for the placed bets before adding risk. A recommendation
   menu is not permission to exceed the 4-unit day or 10-unit week.

## What the dashboard must always show

- Units wagered (week / season) against the caps above
- Net units and net dollars, derived from the bet ledger — never hand-typed
- Current bankroll and distance from stop-loss
- Fan-team cohort record vs. everything else

Related: [[agent|manifest]] · [[recommendation-ledger.csv|recommendation ledger]] ·
[[../../03-projects/soccer-coaching/readme|(unrelated, but this is what Saturdays are actually for)]]

## Strategy (David, 2026-10-13)

"Profit over quick profit. It may take longer but I'm ok just being entertained by
confident bets vs lottos, maybe 1 fun lotto per slate that is juicy."

- The money goes on confident plays: singles and 2-3 leg moneyline parlays on
  favorites, every leg at or above the parlay floor, through `football.py parlay`.
- One lotto per slate, at most, through `football.py parlay --lotto`: up to 5 legs,
  longest prices that still clear the lotto floor, 0.25u. It is entertainment and is
  expected to lose most nights; it never grows past its cap to "chase".
- No ticket from the command, no bet. A blocked ticket is a pass day.
