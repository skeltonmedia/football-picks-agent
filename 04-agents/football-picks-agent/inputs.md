# Inputs

## Runtime Files

Built 2026-07-27, live now (see `run-log.md`):

```text
04-agents/football-picks-agent/data/nfl-schedule.csv        -- fetch-nfl-slate.py, keyless (ESPN)
04-agents/football-picks-agent/data/cfb-schedule.csv        -- fetch-cfb-slate.py, keyless (ESPN, FBS only)
04-agents/football-picks-agent/data/cfb-team-ratings.csv    -- fetch-cfbd-ratings.py, requires COLLEGE_FOOTBALL_DATA_API_KEY
04-agents/football-picks-agent/data/nfl-team-ratings.csv    -- fetch-nfl-team-ratings.py, keyless (nflverse games.csv)
04-agents/football-picks-agent/line-watch/snapshots.csv     -- fetch-odds.py, requires ODDS_API_KEY (gitignored)
04-agents/football-picks-agent/recommendation-ledger.csv    -- manual, appended per instructions.md step 11
```

Everything needed for the review-card workflow is built. Not yet built, lower priority: NFL play-by-play-level features (EPA/play, success rate) beyond the SRS margin-based rating -- would need `nflreadpy`/nflverse's larger datasets, not just `games.csv`.

## Config

Private local file (gitignored):

```text
04-agents/football-picks-agent/config.env
```

Templates:

```text
04-agents/football-picks-agent/config.example.env
04-agents/football-picks-agent/line-watch/config.example.env
```

## Required

- Official schedule and kickoff times — `fetch-nfl-slate.py` / `fetch-cfb-slate.py`, keyless, live.
- Timestamped DraftKings spread, total, and moneyline lines, plus at least two comparison books — `fetch-odds.py`, needs `ODDS_API_KEY` (free tier, the-odds-api.com — David to obtain).
- NFL injuries: `fetch-injuries.py` (keyless ESPN league-wide report, in the daily data loop as of 2026-08-22) writes `data/injuries.csv`; review cards auto-fill QB status by name plus a non-Active count per team. ESPN tracks the official Wed/Thu/Fri practice designations closely but is not the league's own report — **confirm the QB line manually before any PICK.** CFB injuries: no league-wide feed exists (reporting isn't mandated); manual review by design.
- Confirmed active/inactive lists (NFL, ~90 min pre-kickoff) or confirmed starters (CFB) — manual review.
- Hourly weather at the venue for open-air stadiums — free, keyless: `api.weather.gov`.

## Preferred

- NFL team-strength context: SRS rating computed from nflverse's `games.csv` — live via `fetch-nfl-team-ratings.py`, keyless. Deeper play-by-play features (EPA/play, success rate) not yet built.
- CFB historical/current modeling: CollegeFootballData SP+, PPA, talent, and returning production — live via `fetch-cfbd-ratings.py`.
- Next Gen Stats or PFF-style player/unit grades, if accessible.
- Opening lines and closing-line archive for CLV tracking — accumulates automatically in `line-watch/snapshots.csv` once odds fetching is live.
- Referee crew tendencies, as context only, never stand-alone.

- **Public betting splits (ticket% vs money%), free/keyless — not yet scraped:**
  - `https://www.scoresandodds.com/ncaaf/consensus-picks` — the useful one: carries **both** bet% (tickets) and money% (handle) per game for spread, total, and ML. The ticket/handle divergence is the quantity `spot-flags.py`'s `RLM` flag currently *estimates* from line movement; this measures it directly. NFL equivalent at `/nfl/consensus-picks`.
  - `https://contests.covers.com/consensus/topoverunderconsensus/ncaaf` — O/U consensus, **tickets only**, no handle. Wider slate coverage; use as a cross-check, not a source of truth.

  Verified returning live data 2026-09-03. Both are HTML scrapes with no API and no
  stability guarantee, so treat a parse failure as missing data, not as "no divergence."
  Neither is a licensed feed and neither publishes methodology or sample size —
  per `model-notes.md`, public ticket percentage is **context for a thesis, never the
  thesis**, and this stays display-only under the AQ-168 measure-first contract.

## Freshness

- Reconfirm starters/actives, injury status, weather, and prices as close to kickoff as the market allows.
- Timestamp every price in Eastern Time (`fetch-odds.py` and both slate fetchers already do this).
- Treat missing or stale required data as a reason to `WATCH` or `PASS` — never `PICK`.
- A line move across a key number (see `model-notes.md`) resets the maximum playable price.

