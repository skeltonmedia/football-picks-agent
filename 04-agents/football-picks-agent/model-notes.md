# Model and Evaluation Notes

## Price math

American odds to decimal odds:

- Positive `A`: `D = 1 + A / 100`
- Negative `A`: `D = 1 + 100 / |A|`

Then:

- Break-even probability: `1 / D`
- Expected ROI: `p × D - 1`

For a two-sided market, remove vig proportionally:

`no-vig p(side) = raw implied p(side) / [raw implied p(side) + raw implied p(other side)]`

Staking and Kelly sizing are explicitly out of scope for this agent (see `readme.md`) — it outputs `PICK`/`LEAN`/`WATCH`/`PASS`, not a stake.

## Spread and total probability

Approximate cover probability from a point spread using a normal (or logistic) distribution of the scoring margin, with a sport-specific standard deviation:

- NFL: roughly 13-14 points.
- CFB: roughly 16-17 points, wider because talent variance between FBS programs is far larger than between NFL rosters.

Treat model-derived cover probabilities as a shrinkage-adjusted range, not a precise output. Validate against the no-vig market price before trusting the model number, especially early in the season before any calibration data exists.

## Key numbers

- NFL: 3 and 7 hold outsized probability mass in the scoring-margin distribution. A half-point or full-point move across 3 or 7 is worth materially more than the same move elsewhere on the line.
- CFB: 3, 7, 10, and 14 matter, though the much wider scoring variance dulls their edge relative to the NFL.

A line move across a key number resets the maximum playable price — do not chase a lost key number.

## Market comparison, not stacking

Spread, total, and moneyline on the same game are correlated views of the same two teams, not three independent confirmations of an edge. A model disagreement that shows up in all three markets is one signal, not three. Parlays are out of scope for this agent (see `readme.md`), so this matters for confidence calibration, not joint-probability math.

## Probability discipline

Early model or manual percentages are screening estimates, not a validated predictive model. Use a range and the conservative lower bound. Shrink uncertain estimates toward the no-vig market until genuine out-of-sample skill is demonstrated — the same discipline `mlb-picks-agent` applies to its moneyline screens.

Do not use win-loss record, ATS record, public ticket percentage, preseason polls, recruiting rankings alone, or narrative storylines ("revenge game," "trap game," "letdown spot") as stand-alone evidence. They are context for a thesis already built on price, personnel, and situational data — never the thesis itself.

## What's already been tested and failed (2026-07-27)

A naive Elo-rating-only spread model was backtested against the full 2025 CFB season (715 games, real DraftKings closing lines, leakage-free weekly Elo). It does not beat the market: lower correlation to actual margin than the closing line, higher MAE, and ATS win rate by disagreement bucket landing at 52.5% / 49.3% / 44.2% / 56.4% — noise around the 52.4% breakeven, not a durable edge. Full writeup: `05-outputs/reports/football-picks-agent/backtest-cfb-elo-2025.md`.

Conclusion: team-strength ratings alone (SP+, PPA, Elo, talent) are not sufficient to beat a market this efficient. They stay in the report as descriptive context for the manual checklist, not as an automated `PICK`/`LEAN` signal. Whatever eventually earns a spot in `Best Picks` needs inputs the market prices less efficiently — injuries, situational spots, weather, pace mismatches — combined with disciplined price-shopping, not a smarter rating number alone.

**The NFL SRS rating has now been backtested (2026-08-24, AQ-101)** — the assumption above held. `backtest-nfl-srs.py` ran an expanding-window SRS (each week's rating built only from that same season's strictly-prior weeks — no future data, no leakage) against 208 games from the 2025 season (weeks 5-18; weeks 1-4 excluded as too little same-season data to rate anything). Result: the closing spread line is a better predictor of the actual margin than the SRS model on every axis that matters — correlation 0.513 (market) vs 0.417 (model), MAE 9.99 (market) vs 10.51 (model). The ATS-by-edge-bucket breakdown shows the same noise-not-signal pattern as the CFB Elo backtest: small edges (0-1, 1-3 pts) land slightly above the 52.4% breakeven on modest samples (45 and 88 games), but the largest-edge bucket (5+ pts, 31 games) goes the *wrong* way at 41.9% — exactly the kind of result that looks meaningful until you check the games column, which the CFB backtest's own "Reading this honestly" section warned about. Full writeup: `05-outputs/reports/football-picks-agent/backtest-nfl-srs-2025.md`. Conclusion stands, now measured instead of assumed: NFL SRS is descriptive context for the manual checklist, not an automated `PICK`/`LEAN` signal, same as CFB's SP+/PPA/talent.

## Evaluation

Track, per `execution-plan.md`'s Phase 3/Evaluation sections:

- win/loss/push by market (spread, total, moneyline);
- closing-line value;
- Brier score and log loss for probabilities;
- calibration by confidence bucket;
- simulated ROI, clearly marked as simulation until real stakes exist;
- pick volume and pass reasons.

A few good weeks do not prove the process works. The Phase 3 backtest against historical closing lines, and the first several in-season weeks, are a calibration test — not proof of a durable edge.


## NFL team-name matching was silently wrong (found and fixed 2026-09-02)

A pre-season deep checkup found that `rating()` in `weekly-picks.py` used the
CFB-tuned fuzzy substring matcher for NFL too. Two-to-three letter NFL codes
are short enough to appear as accidental substrings inside unrelated full
names: "ne" matched inside "buccaNEers" (Tampa Bay), inside "New Orleans
Saints", and inside both New York teams' names; "phi" matched inside
"miamidolPHIns"; "car" matched inside "arizonaCARdinals"; "chi" matched
inside "kansascityCHIefs". Verified against the real Week 1 schedule: **8 of
32 teams silently received another team's SRS rating**, and **7 more matched
nothing at all** (49ers, Rams, Jaguars, Raiders, Packers, Commanders,
Chargers) with no warning either way. A pick could have shipped a "case:"
line citing the wrong team's rating as fact.

Fixed by adding `nfl_full_name_crosswalk()`: an exact full-name to
abbreviation map built from `nfl-schedule.csv`'s own two columns for the same
game, used for NFL only (CFB's matcher is untouched). Verified 32/32 correct
against the live 2026 schedule; 4 regression tests added to
`test_football_picks.py`.

## NFL model was structurally dark until roughly Week 13 (found 2026-09-02, FIXED same day — AQ-158)

Separately, and worse: `fetch-nfl-team-ratings.py`'s `find_year_with_data()`
requires **200 completed games league-wide** (of 272 in a season) before it
will compute SRS from the current season at all — roughly week 13-14. Until
then every 2026 game reads `is_current_season=False`, and `weekly-picks.py`
line ~384 (`if league == "nfl" and ra.get("is_current_season") == "False":
preseason += 1; continue`) drops the game entirely, both markets. Verified
live 2026-09-02: 0 completed 2026 games, so **every NFL game this week reads
as "preseason" and is excluded** — this is not a bug in the sense of
producing a wrong answer, it is a policy gate that happens to leave the NFL
board empty for most of the season.

This is inconsistent with how CFB is treated: CFB uses SP+ even when it is
last-season/preseason data, with the risk disclosed in the write-up ("major
roster turnover makes preseason ratings fragile"). The already-validated
`backtest-nfl-srs.py` used a per-team **expanding window from that same
season's strictly-prior weeks**, excluding only weeks 1-4 as too thin to
rate anything — a much lower bar than "200 games league-wide," and one that
already has a real backtest behind it. Fixing the live gate to match the
validated backtest approach would let NFL start producing picks around week
2-5 instead of week 13. See AQ (filed 2026-09-02) for the proposal; this is a
selection-logic change and needs David's approval before it ships, per the
agent's own change rights (fair-price math / thresholds require an AQ).
**David approved same day (AQ-158); shipped.** `current_season_ready()` now
gates on MIN_WEEK=5 + >=30 same-season games — the backtest's own threshold —
and `completed_games_for_year()` is REG-only (the fallback-year SRS had been
silently including playoff games; SEA/LAR both showed `games_played=20`
instead of 17). `backtest-nfl-srs.py --year 2025` re-run byte-identical after
the change. Tests: `test_nfl_team_ratings_gate.py` (9 new, all green).

## Where the edge is (2026-08-28)

David asked the right question: "where is the edge?" Answer, in two halves.

**Dead:** out-forecasting the market. Measured twice (CFB Elo 715 games, NFL SRS
208 games) — public ratings do not beat the closing line. Any pick sold on
"our model likes it" is selling noise.

**Alive, and mechanical:** the same bet is priced differently at different
books. `best-price.py` scans the five books already in `line-watch/snapshots.csv`
and reports the best-vs-worst gap per selection. Live example, 2026-08-28
slate: UNC ML +290 at FanDuel vs +250 at BetRivers — 2.93% of expected value,
$0.80 on a $2 stake, for no opinion at all.

Why this matters more than it looks: breakeven at -110 is 52.4%. Systematically
taking the best available price instead of the worst is worth roughly 2-3% per
wager, which drops the required hit rate to about 51%. That does not make a
losing process profitable. It is the difference between a marginal edge being
real and being eaten by the vig.

Second use, unpriced: **book disagreement is a shortlist.** When five books hang
the same number, the market is confident and a manual read is unlikely to be
right. When they are 3% apart, there is genuine uncertainty — those are the
games where doing the card work can actually pay.


## AQ-131 verdicts (2026-08-30) — both upgrades failed their backtests

**Pace-adjusted CFB totals: does not ship.** Leakage-free test (2024 inputs
→ 2025 games, 693 real DK closing totals): v2 pace MAE 13.23 vs v1 blend
13.02 vs closing line 12.20. O/U hit rates by disagreement bucket all sit
51.7-53.0% — breakeven noise. Full report:
`05-outputs/reports/football-picks-agent/backtest-cfb-totals-2025.md`.

**Prior-season offensive EPA (nflverse): does not ship.** Correlation with
2025 margins 0.150 vs closing spread 0.504 — worse than the in-season SRS
(0.417) it was meant to replace. Offense-only EPA without opponent-adjusted
defense is not a rating. A real net-EPA build needs play-by-play aggregation
and would face the same backtest.

The meta-lesson stands: every "obvious" model improvement tested so far
(Elo, SRS, pace, cheap EPA) loses to the closing line. The durable edges
remain price-shopping, book disagreement, situational spots (now tracked
via spot-flags), and discipline.
