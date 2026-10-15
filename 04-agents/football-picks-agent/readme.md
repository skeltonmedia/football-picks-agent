# Football Picks Agent

## Job

Produce disciplined NFL and college football pick reports from current data, market odds, and freshness checks. As of 2026-07-27, "model predictions" means descriptive team-strength context (SP+/PPA/talent for CFB, SRS for NFL) for a human reviewer, not an automated prediction -- a backtest showed rating-based models alone don't beat the closing line. See `model-notes.md`.

The agent should explain where there may be edge, where the data is too stale, and where the market is probably right.

## Files

- **`football.py` — the one agent (2026-09-28).** `status | data | board | check |
  settle | learn | bet | week | daily`. Price-only candidates on both sides of every
  market, all three leagues (NHL since 2026-10-07); edge vs Pinnacle/consensus at the same line gates and
  sizes; win % ranks; every qualified row is logged; `learn` writes
  `lessons.md` + `learned-rules.json` and blocks losing slices. **`daily`
  (2026-10-07, AQ-277)** is the everyday card: `DAILY_PICKS` win-first rows
  across NFL, CFB and NHL, moneyline / over-under / player prop, one per game,
  contract in `03-projects/football-picks-agent/daily-output-format.md`. Everything
  below it is a data fetcher, a settler, or legacy research.
- `archive-snapshots.py` (2026-10-13) — moves snapshot rows for games older than
  30 days into `line-watch/archive/snapshots-YYYY-MM.csv`, losslessly, after
  `compact-snapshots.py` in the daily loop. The hot file went 66MB → 31MB on
  day one; every `football.py`/`grade-clv.py` run reads it in full.
- **`football.py parlay` (2026-10-13, David: "Force me to make smart bets").** Builds
  the day's parlay win-first through a hard gate from `config.env`: at most
  `PARLAY_MAX_LEGS` (3), every leg at least `PARLAY_MIN_LEG_WIN` (56%) market win,
  combined hit at least `PARLAY_MIN_HIT_PCT` (18%), stake at most `PARLAY_MAX_UNITS`
  (0.5u), `PARLAY_MAX_PER_DAY` (1), different games, nothing stale or vetoed. A
  ticket that breaks a rule prints BLOCKED and `--place` refuses to log it.
  `--pick 1,3` runs David's own legs from the last daily card through the same
  gate. `--place` logs one `parlay` row in `bet-ledger.csv`, the legs in
  `line-watch/parlays.json`, and the legs as ledger rows; `settle` resolves the
  parlay from its legs (any loss loses, a push drops out and the payout is
  recomputed). The rule is simple: no ticket from this command, no bet.
- `fetch-nhl-slate.py` — keyless ESPN NHL schedule, one GET per date (ESPN's
  hockey scoreboard refuses date ranges), yesterday through +8 days.
- `fetch-props.py` — rationed player props from The Odds API event endpoint
  for today's games (anytime TD for NFL/NCAA; anytime goal, shots on goal,
  points for NHL). Budget and reserve in `config.env`; receipt in
  `data/props-<date>.json`; rows in `line-watch/props.csv`.
- `settle-props.py` — grades prop rows from ESPN box scores (skater stats for
  NHL, scoring-play text for touchdowns); runs inside `football.py settle`
  and the daily loop.
- `fetch-free-lines.py` — **free continuous lines (2026-09-28):** Pinnacle's
  keyless guest API + DraftKings as ESPN embeds it, all three leagues (NHL =
  Pinnacle league 1456, same-day games only; ESPN by date), every 30
  minutes (`com.davidos.football-free-lines.plist`). Writes
  `line-watch/free-lines.csv` (change-only) and `free-lines-latest.csv` (full
  latest pull); rows inside 75 min of kickoff also go to `snapshots.csv` so
  CLV grades against a real close. Team names resolve through
  `data/team-aliases.csv`; misses log to `line-watch/free-lines-unmatched.txt`.
- `agent.md` — permissions and lifecycle contract.
- `instructions.md` — the run workflow.
- `inputs.md` — data sources, required vs. preferred, freshness rules.
- `model-notes.md` — price math, key numbers, and the 2026-07-27 backtest finding.
- `fetch-nfl-slate.py` / `fetch-cfb-slate.py` — keyless ESPN schedule fetchers. NFL uses `cdn.espn.com` as of 2026-08-12; `site.api.espn.com` now returns HTTP 403 from Akamai for every User-Agent.
- `fetch-odds.py` — DraftKings/FanDuel/BetMGM spread, total, moneyline snapshots (needs `ODDS_API_KEY`).
- `fetch-cfbd-ratings.py` — SP+, PPA, talent, returning production for FBS teams (needs `COLLEGE_FOOTBALL_DATA_API_KEY`).
- `fetch-nfl-team-ratings.py` — SRS rating computed from keyless nflverse game history (no API-hosted NFL rating exists).
- `fetch-cfbd-lines-history.py` / `backtest-cfb-elo.py` — historical closing lines and the leakage-free Elo backtest.
- `fetch-weather.py` — kickoff-hour forecast for outdoor games via Nominatim + api.weather.gov, both keyless. Skips domes using the schedule's `indoor` flag. Caches venue coordinates in `data/venue-coords.csv`. **Added 2026-08-12** — the readme had claimed weather coverage since the agent was built, but no script ever called it.
- `build-review-cards.py` — generates fillable pregame-confirmation worksheets, the actual manual-review tool. `--limit 0` for the full slate.
- `write-weekly-report.py` — assembles the weekly slate report.
- **Text-only default 2026-10-13:** daily, weekly and weekend picks are compact
  iPhone cards under the addendum in `weekly-output-format.md`. Graphics and
  their assets remain archived; rendering requires David's explicit request.
- `run-weekly-card.py` — (legacy) runs the locked top-ten workflow and saves
  markdown only. It no longer invokes the visual renderer.
- `cash-confidence.py` — seeds user-facing Saturday NCAA and Sunday NFL boards from conservative
  consensus no-vig cash probability, one selection per game; value does not
  determine rank.
- `render-weekly-visual.py` — parses the weekly markdown source, typesets exact
  copy over a themeable empty fan-cave background, and exports HTML + PNG.
- `visual-card-config.json` — brand tokens and registered background themes.
- `line-watch/` — timestamped odds snapshot ledger (gitignored -- regenerable, high-volume).
- `recommendation-ledger.csv` — every `PICK`/`LEAN`/`PASS` decision, frozen at decision time, updated postgame.
- `run-log.md` — run history.

Reports land in `05-outputs/reports/football-picks-agent/`.

For an explicitly requested graphic, the archived presentation contract is
`03-projects/football-picks-agent/visual-design-language.md`; invoke
`render-weekly-visual.py <card.md> --theme <name>` directly. Routine cards do
not run this step.

## Scope

MVP:

- NFL.
- FBS college football.
- NHL (2026-10-07, for the daily card).
- Pregame spread, total, and moneyline.
- Player props on the daily card: anytime TD (NFL, NCAA); anytime goal,
  shots on goal, points (NHL). Rationed by credits; graded from ESPN box scores.
- Weekly slate reports and the daily 3-pick card.
- Postgame tracking.

Out of scope:

- Autonomous wagering.
- Live betting.
- Player props beyond the daily-card markets above.
- Parlays built outside `football.py parlay` (the gated 3-leg ticket is in scope since 2026-10-13).
- DFS.
- Bet sizing beyond optional future unit labeling.

## Data Sources

Live:

- ESPN's public scoreboard API for NFL/CFB/NHL schedules and NHL finals (keyless).
- CollegeFootballData for CFB ratings, talent, and historical lines.
- The Odds API for current spread/total/moneyline odds (NFL, NCAAF, NHL) and
  rationed player props.
- Pinnacle's keyless guest API and DraftKings via ESPN for free 30-minute lines.
- ESPN's summary endpoint (box scores, scoring plays) for prop settlement.

Not yet built:

- SportsDataIO or Sportradar if real-time coverage becomes worth the cost (deferred, see `03-projects/football-picks-agent/decisions.md`).

## Run Philosophy

The agent should be skeptical. It should show its work, identify uncertainty, and prefer a clean pass over a thin pick.
