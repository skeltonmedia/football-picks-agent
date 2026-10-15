---
type: format-spec
status: locked
created: 2026-08-29
applies_to: NFL and NCAA (FBS), all season
---

# Weekly output format — locked

## Text-only output and efficient execution (David, 2026-10-13)

This addendum supersedes every graphic, HTML, PNG and visual-review requirement
below. It applies to daily, weekly and weekend picks.

- Deliver one iPhone-friendly text card, saved once as markdown. Each row has
  the play, offered price, market-derived confidence, kickoff, status, units,
  one short reason and its main risk. Keep the weekend top ten when ten qualify;
  keep the daily card's existing count. Aim for at most 600 words for a top ten.
- No graphics, HTML, image generation, browser rendering, screenshots or
  visual Sources panels unless David explicitly asks for them. Existing
  graphics stay archived. Link sources directly in the markdown.
- Use the current packet and relevant rule sections, not full historical logs.
  Run the selector once and show only selected rows and compact diagnostics;
  never print league-wide raw odds/injury payloads into the conversation.
- Reuse verified fresh scheduled feeds. Refresh only missing or stale inputs,
  changed availability and unresolved flags for the exact candidates. Check
  actual provider timestamps; preserve the zero-credit quota check, 12-hour
  per-league cooldown and existing freshness limits.
- Review each selected game's QB/injuries, role, venue/weather and movement.
  NCAA totals still need implied team scores, favorite ceiling, underdog minimum,
  explosives, finishing drives, turnover regression, weather, offensive
  availability, garbage time and a pass number before promotion from WATCH/0u.
- Default to one agent; additional agents require an explicit request. Expand
  research only for a material unresolved risk or on request. Keep selected quotes,
  source links and timestamps needed to reproduce and grade the card.
- Verify rank, prices, unique games and units once. Save one brief feedback
  receipt and run-log entry; omit duplicate long write-ups and repeated audits.
  Missing evidence still means WATCH/PASS. Ranking, paper gates, logging rules,
  bankroll caps and accuracy requirements do not change.

## Binding game-day cash-confidence addendum — 2026-09-20 / 2026-09-27

David explicitly chose likelihood of winning over book value. Accordingly,
user-facing Saturday NCAA and Sunday NFL boards rank fresh eligible selections by
consensus no-vig cash probability, not by DraftKings-versus-consensus price
edge. It uses `cash-confidence.py`, keeps the existing `-250` favorite ceiling,
and displays at most one selection per game. The percentage is labeled **cash
confidence** and is a market-derived baseline—not a proprietary model forecast.
Price/value may appear in the write-up but cannot change the rank.

For this game-day variant, stars summarize cash confidence: 5⭐ ≥65%, 4⭐ ≥60%,
3⭐ ≥55%, 2⭐ ≥52.5%, otherwise 1⭐. Manual current QB/injury, weather, role,
and movement review may lower a tier or assign `WATCH`/`PASS`; it cannot raise
one. `WATCH`/`PASS` always means 0 units. These rules supersede Rules 2, 7, 8
and the one-card cadence only when producing a user-facing game-day cash board.
The Tuesday edge/CLV research cohort keeps its existing contract.

> David, 2026-08-29: "let's write this into the rules so every output is the
> same format each week for nfl too." This file is the contract. The iMessage
> block in `04-agents/football-picks-agent/weekly-picks.py` implements it and
> carries a LOCKED comment pointing here. Changing the format requires David
> asking for the change — not an agent deciding the output could be nicer.
> David changed the contract on 2026-08-29: "Please always show top 10 picks."
> The ten are a ranked menu; bankroll caps control staking, not visibility.
> David clarified the ranking on 2026-09-01: "top 10 picks moneyline, over, or
> under and only rank by confidence." The selector is therefore a global
> confidence sort, not a game-diversity or manual-target sort.
> David expanded the eligible set on 2026-09-03: "add spread betting to the
> full weekly slate." Spreads now enter that same confidence-only sort.
>
> Related: [[../../04-agents/football-picks-agent/staking-config|staking-config]] ·
> [[dry-run-protocol]] · [[../../00-system/writing-style|writing-style]]

## The template

```
🏈 Daddy Dan's Picks · Week of <date> — the plays
<n> units allocated · others size if played
take what you like

<n>) <PLAY IN CAPS> <⭐ strength> · <PICK|LEAN|WATCH|PASS> · <0|1|2> unit(s) [if played]
<Day Mon D · Time ET>
<2-3 sentence write-up: the case with the team named and its national
rank, the model number vs the line, weather when it matters, line
movement in words ("money agrees" / "market leans against"), ⚠️ QB
warnings.> Game notes: <temp>° at kickoff, wind, rain — <venue>.

⭐ = stronger play.
Every pick tracked vs results,
tailed or not, to sharpen this.
```

## Rules

1. **The play leads, in caps, in plain words.** `STANFORD MONEYLINE — Hawaii
   @ Stanford`, `COLORADO +6.5 — Colorado @ Georgia Tech`, `UNDER 51.5 —
   Wolfpack @ Cavaliers`. Moneyline is written explicitly so the word “over”
   can only mean a total. A spread always includes its signed point number.
   No odds in the message — the sportsbook shows the price; the full card
   keeps every number.
2. **Stars are the ranking.** Since 2026-09-18 (AQ-220) the ranking is the
   price edge in cents: 5⭐ ≥ 2.0¢, 4⭐ ≥ 1.5¢, 3⭐ ≥ 1.0¢, 2⭐ ≥ 0.5¢. (Until
   then: 5⭐ ≥ 80 confidence, 4⭐ ≥ 65, 3⭐ ≥ 50.) Star
   thresholds move only if the settled record shows the tiers don't separate.
3. **Every row shows a unit number** (approved by David 2026-09-03 and
   clarified 2026-09-20, within
   staking-config's max-2-on-a-single-bet rule): **5⭐ = 2 units, everything
   else = 1 unit.** An allocated target prints the unit number directly. A
   `LEAN` outside the cap-compliant stake shortlist may print that number with
   **`if played`**. A manual `PASS` or `WATCH` always prints **`0 units`** so a
   rejected bet cannot look conditionally endorsed. Only `PICK` units count
   toward stated exposure. Unit size itself lives in staking-config.md only.
   **CFB total exception, approved 2026-09-26:** automated Overs and Unders
   remain ranked and visible but show `WATCH · 0 units` until the manual
   fair-total checklist is completed. A promoted college total is capped at
   1 unit while the totals model remains unvalidated; price edge alone cannot
   produce a 2-unit total.
4. **Every pick gets a write-up** — a case a person can argue with, not a
   number. Named teams (school names, not mascot fragments), national ranks,
   plain-English movement, weather.
5. **Game notes on every outdoor game** — kickoff temp, wind, rain, venue.
   Domes get none. A neutral-site venue (e.g. Aviva Stadium, Dublin) is part
   of the case and the write-up should treat it as one.
6. **Always show the top 10 when ten live eligible plays exist.** A
   manual target gate may change an entry to `LEAN`, `WATCH`, or `PASS`
   and leave it unallocated, but it may not delete that entry from the graphic.
   `LEAN` rows may retain the conditional `1 unit if played` or `2 units if
   played` label. `WATCH` and `PASS` rows show `0 units` as required by Rule 3.
   The display never shrinks merely because taking every default unit would
   breach a daily or weekly cap. If the actual slate has fewer than 10 live
   eligible markets, show all of them and state the shortfall plainly — never
   invent a line or a game.
7. **Rank only by confidence.** After freshness, league, price, and model-data
   eligibility gates, sort every spread, Moneyline, Over, and Under candidate by its
   full numeric confidence score and take ranks 1 through 10. Do not maximize
   game diversity first. Do not reorder for market type, manual status,
   suggested units, team preference, or narrative strength. Exact-score ties
   use a deterministic identifier only. Multiple markets from the same game
   may appear; label the correlation, but do not demote any of them.
8. **Same format for NFL and NCAA.** Full-game spreads, Moneylines, Overs, and
   Unders are eligible for both leagues and compete in one confidence sort.
   Before current-season NFL ratings clear the Week 5 gate, an NFL spread or
   Moneyline needs a completed manual review stating a fair spread or fair win
   probability and checking quarterback/injury availability, venue/weather,
   and line movement. NFL totals likewise need a completed manual fair-total
   review until an automated totals model exists. Never present prior-season
   SRS as a current scoring model, invent a model total, or rank an unreviewed
   early-season NFL market. Missing review means WATCH/PASS. Preseason remains
   excluded outright.
9. **The footer's tracking line stays.** It is the honest signature: every
   pick is graded, tailed or not.
10. **Every kickoff includes month and day.** A seven-day menu can contain two
   Saturdays. `Sat Sep 5 · 12:00PM ET` is explicit; `Sat 12:00PM ET` can
   falsely look like a game already started.

## Weekly cadence

- **Tuesday research benchmark:** run the full-week board, log it, and freeze
  that snapshot for CLV evaluation.
- **Thursday live board:** recompute the full score for every currently
  eligible market and rerank the full-week top 10 from fresh prices, weather,
  and availability. Archive the output as its own dated execution snapshot.
- **Saturday NCAA / Sunday NFL game-day boards:** recompute again within the
  calendar-day boundary. These are no-log execution views; the dated report
  artifacts preserve the snapshot without duplicating the Tuesday ledger.
- The graphic always shows the **current snapshot's** rank and confidence.
  Tuesday rank/price may appear in the write-up as audit context only.

## Cadence addendum — 2026-09-01 (David, pre-Thursday-launch)

David: *"since this week is a larger slate for college football the top 10
picks on Tuesday would be good so we can truly show confidence scores across
all the slate. Money line or over under picks only."*

- **The weekly board runs TUESDAY**, over the full week window (`--hours 168`
  covers Thursday NFL through the following Monday). A bigger slate is the
  point: confidence scores mean more when ranked across 60+ games than 12.
- **Historical market scope:** this addendum originally limited the board to
  moneylines and totals and used `ODDS_API_MARKETS=h2h,totals`. That restriction
  is superseded by the spread-market addendum below.
- The Tuesday board is a ranked menu priced at Tuesday numbers. CLV grading
  vs close (grade-clv.py, Pinnacle-preferred) is what proves or disproves the
  Tuesday-pricing edge — a Tuesday board that beats the close is the product.
- **Superseded 2026-09-03 by AQ-162:** Thursday and Saturday do not preserve
  this order. They fully rerank current eligible plays. Tuesday's frozen
  ledger rows remain the distinct benchmark, so CLV stays measurable without
  making an early-week confidence score masquerade as today's view.
- Paid odds pulls are rationed to one league at 7:30 AM ET: NCAA on
  Tuesday/Thursday/Saturday and NFL on Sunday. A 12-hour per-league cooldown
  blocks duplicate card/automation calls; quota status checks use the
  provider's zero-credit endpoint.

## Live-snapshot addendum — 2026-09-03 (David, AQ-162)

David approved the two-board model after asking why the Thursday card still
said confidence was frozen Tuesday.

- Tuesday is the research board: frozen price, rank, and confidence for CLV.
- Thursday and game-day are decision boards: score the current market from
  scratch after all freshness gates, then take ranks 1–10.
- Never blend the two rankings. A prior rank is provenance, not a current
  confidence input.
- Preserve every execution snapshot in its dated markdown, HTML, and PNG.
  The no-log game-day rule remains in force so the official Tuesday
  recommendation cohort is not duplicated.

## One-card addendum — 2026-09-18 (David, AQ-222)

David: "combine nfl with ncaa so we only have good picks once a week."

- **Tuesday is the card.** The 7:30 AM data loop pulls both leagues; the card
  ranks NFL and NCAA in one sort over the full week window and is the only
  recommendation-ledger cohort. Ten rows stay for the graphic; AQ-221's edge
  floor decides which rows carry units.
- **Thursday, Saturday and Sunday are price checks, not boards.** The loop
  pulls the league playing that day; `run-weekly-card.py --check` re-prices
  every Tuesday row at DraftKings and reports one of: `still playable at X`,
  `number gone` (a key number crossed against us, or more than
  `CHECK_MAX_POINT_LOSS` points / `CHECK_MAX_PRICE_LOSS_CENTS` cents lost),
  or `check QB` (a starting QB Out or Doubtful in the injury file). A flag
  never adds, replaces, or reorders a pick.
- **Supersedes** the live-snapshot addendum's Thursday and game-day reranks as
  decision boards. Tuesday's frozen rows remain the CLV benchmark, unchanged.
- **NFL before the Week 5 ratings gate** enters the combined card only with the
  manual fair-line review already required above; otherwise it prints as
  WATCH. AQ-220's price-edge score, which needs no rating of ours, removes
  that dependency when it lands.
- Credits: about 33 a week (Tue 6, Thu 9, Sat 9, Sun 6, Mon 3).

## Spread-market addendum — 2026-09-03 (David, AQ-165)

- The full weekly candidate set is spread, Moneyline, Over, and Under.
- Every spread prints the selected team and signed point number. DraftKings is
  the baseline; same-number juice is price-shopped, while a better point number
  is disclosed separately rather than collapsed into an ambiguous “best” line.
- Spreads use the existing side confidence components and the configured
  minimum cover gap. They receive no quota and compete for the same ten ranks.
- Same-game side, moneyline, and total entries are correlated views, not
  independent confirmation.
- The default paid pull is now `h2h,spreads,totals`: three credits per
  league-specific run instead of two. The Tue/Thu/Sat NCAA and Sunday NFL
  rationing and 12-hour cooldown do not change.

## Card-shape addendum — 2026-10-02 (David)

David: "The way I'd prefer to pick is ml over under and parlays of the 3 nfl
and college and nfl touchdown scorers or player props for that week."

The user-facing card is now five blocks, iPhone format (short numbered lines,
no tables, units not cents):

1. **Moneylines** — NFL and college, ranked by Pinnacle no-vig win %, inside
   the -250 ceiling, one per game.
2. **Over/unders** — per game: Pinnacle's number and shade, DraftKings' number
   and price; call out any game where DK's number or price is better than
   Pinnacle's on a side. Automated CFB totals stay 0u (fail-closed rule).
3. **3-leg parlays** — one NFL, one college, built from the top moneylines.
   Always print the fair odds (product of no-vig win %) next to the book
   payout so the parlay tax is visible.
4. **NFL touchdown scorers / player props** — anytime TD prices de-vigged
   across the game's field, best book named; Odds API event endpoint
   (credits), saved raw to `data/props-<date>.json` so a pull is never repeated.
5. **Staking** — unchanged: edge.py paper gate, fan-team 1u, caps from
   staking-config.md. The card shape does not create units.
