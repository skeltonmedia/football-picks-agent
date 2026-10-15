---
type: format-spec
status: locked
created: 2026-10-07
applies_to: the daily card — NFL, NCAA (FBS) and NHL, every day of the season
tags: [football-picks-agent, hockey, daily-card, format]
---

# Daily card format — locked

Output is text-only under [[weekly-output-format#Text-only output and efficient execution (David, 2026-10-13)|the text-only execution addendum]]:
one compact card, direct source links, fresh cached inputs reused, no graphics
or rendering unless David explicitly requests them. The daily slots below stay unchanged.

> David, 2026-10-07: "Like to do some serious tuning to my sports betting agent
> I want to add hockey sources and then the goal would be 3 picks a day. Ml
> over under scorer or any of the best props for each nfl ncaa and nhl."

This is the contract for `football.py daily`. The weekly card keeps its own
contract in [[weekly-output-format]]; the staking contract in
[[../../04-agents/football-picks-agent/staking-config|staking-config]] is
unchanged. Changing this format requires David asking for the change.

## The template (`DAILY_SHAPE=slots`, the default)

```
🏈🏒 Daily picks · Wed Oct 7
🏈 NFL
1) <PLAY IN CAPS> · <win %> to win · DK <price> · <0|1|2>u if played | board only · <Day time ET>
   <Away> @ <Home> · NFL · <Pinnacle|consensus> <win %> vs DK's <break-even %> (<edge pp>) · <n> books
2) <the total>
3) <the prop>
🏒 NHL
4) ... 5) ... 6) ...
Per league: likeliest winner, likelier side of a total, likeliest prop. Win % is the
market's number, not a forecast (a 60% pick loses 2 in 5). Units are suggestions
until reviewed; `football.py bet N --units U` logs what you place.
```

A board-only row carries its reason in plain words instead of the edge line
("no edge: DK's price is below the market", "only 1 other book priced", ...).

## Rules

1. **Three slots per league playing that day: the likeliest moneyline, the
   likelier side of a total, the likeliest prop** (David: "ML, over under,
   scorer or any of the best props for each NFL, NCAA and NHL"). NHL plays
   daily; football joins on its days, so a Wednesday card is three rows and a
   Sunday card is six. Different games where the slate allows. The alternative
   shape, `DAILY_SHAPE=top`, shows the best `DAILY_PICKS` rows across every
   league regardless of market (qualified rows first). The shape and count are
   `config.env` settings, never code.
2. **Markets: moneyline, over/under, player prop.** `DAILY_MARKETS=h2h,totals,player`.
   Spreads and puck lines are not on the daily card (David 2026-10-02: "not a
   spread list"); they stay on `football.py board`. Props today: anytime TD
   (NFL, NCAA), anytime goal, shots on goal and points (NHL), from
   `fetch-props.py` within its credit budget.
3. **Win-first, one per game** (David 2026-10-04: "we're in this to win").
   Qualified rows rank by market win % (Pinnacle no-vig when Pinnacle prices
   the line, else the de-vigged consensus; single-sided "Yes" props show the
   implied % with vig and say so). Edge is the tiebreak, never the rank.
4. **Qualified = DraftKings beats the reference** by at least
   `MIN_OFFER_EDGE_PP` with positive EV, at least `MIN_CONSENSUS_BOOKS` other
   books on the same line, inside the -250/+350 window, not stale, not a
   slice `learn` has blocked. Short of three qualified rows, the best
   unqualified rows fill the card labelled **board only** with the reason in
   plain words. A thin card is honest, not broken.
5. **Units are suggestions.** The number after the price is what
   staking-config.md would size the play at if David plays it (edge ≥ 2.0 → 2u,
   ≥ 1.5 → 1u; totals and props capped at 1u; fan teams 1u). Nothing is
   allocated by the card: the review-receipt gate from 2026-10-02 still
   governs `PICK`, and `football.py bet N --units U` records the real wager.
6. **Every row is logged** to `recommendation-ledger.csv` (shown and
   overflow), so settle, CLV and `learn` grade the daily card the same as the
   weekly one. Props settle from ESPN box scores (`settle-props.py`).
7. **iPhone format.** Short numbered lines, no tables, units not cents, the
   same block pasted to iMessage.

## Sources (all three leagues)

| Feed | Football | Hockey |
|---|---|---|
| Pinnacle (free, every 30 min) | leagues 889 / 880, the whole week | league 1456, **same day only** (the guest API lists only today's NHL games) |
| DraftKings via ESPN (free, every 30 min) | by week | by date, today + 2 days |
| The Odds API (rationed) | Tue/Thu/Sat/Sun pulls | `icehockey_nhl`, 3 credits every morning |
| Player props (The Odds API event endpoint) | `player_anytime_td` | `player_goal_scorer_anytime`, `player_shots_on_goal`, `player_points` |
| Schedules / finals | ESPN + nflverse + CFBD | ESPN NHL scoreboard (one GET per date) |
| Prop grading | ESPN summary `scoringPlays` | ESPN summary box score (`skaters`) |

Credit arithmetic at the 500/month quota: football ~140, NHL ~90, props at
`PROPS_DAILY_CREDIT_BUDGET=6` ~180; `PROPS_MIN_CREDITS_RESERVE=60` protects
the game-line pulls when the month runs long.

Related: [[../../04-agents/football-picks-agent/readme|agent readme]] ·
[[decisions]] · [[weekly-output-format]]
