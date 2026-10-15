# Data Source Research

Research date: 2026-07-09.

## Shortlist

### CollegeFootballData

Use for college football schedules, teams, historical data, advanced metrics, and model-building workflows.

Notes:

- The official site says the 2026 edition includes 2025 season data plus the full 2026 schedule.
- Free keys are available for testing.
- Patreon tiers add more calls, live data, GraphQL, and app-scale features.

Sources:

- https://collegefootballdata.com/
- https://collegefootballdata.com/key
- https://api.collegefootballdata.com/api/docs

### nflverse

Use for historical NFL modeling and feature engineering.

Notes:

- nflverse packages provide NFL play-by-play data back to 1999.
- nflreadr provides fast data downloads, caching, and data dictionaries.
- This is best for historical modeling, not low-latency game-day operations.

Sources:

- https://nflverse.nflverse.com/
- https://nflreadr.nflverse.com/

### The Odds API

Use for current and historical market odds if pricing and quota fit.

Notes:

- Supports NFL and NCAAF sport keys.
- Odds endpoint returns upcoming and live games with recent odds.
- Markets include moneyline, spreads, and totals.
- Scores endpoint can return live/upcoming and recently completed games.
- Event IDs match between odds and scores responses, which helps normalize snapshots.

Source:

- https://the-odds-api.com/liveapi/guides/v4/

### Sportradar

Use as a serious commercial option for real-time NFL and NCAA football data.

Notes:

- NFL API offers schedules, standings, team/player data, real-time scores, play-by-play, injuries, depth charts, and push feeds.
- NCAA Football API covers 100% of Division I games, with real-time play-by-play for all FBS games.
- This is likely the highest-quality option if the budget supports it.

Sources:

- https://developer.sportradar.com/football/reference/nfl-overview
- https://developer.sportradar.com/football/reference/ncaafb-overview

### SportsDataIO

Use as another commercial option for league-specific data and betting feeds.

Notes:

- Developer portal advertises scores, stats, plays, injuries, lineups, depth charts, live odds feeds, and historical odds.
- Needs pricing/access confirmation before selecting as source of truth.

Source:

- https://sportsdata.io/developers/api-documentation/nfl

## Recommended MVP Stack

Start with:

- CollegeFootballData for CFB historical/current football data.
- nflverse for NFL historical modeling.
- The Odds API for odds snapshots.
- Manual or API-assisted injury/weather review.

Upgrade path:

- Add SportsDataIO or Sportradar if the MVP proves useful and the main bottleneck is data freshness, injury quality, or live play-by-play.

## Source Selection Criteria

- Coverage: NFL + FBS.
- Freshness: timestamped updates; reliable pregame odds and scores.
- Cost: sustainable for the season.
- Terms: API access allowed; no brittle scraping.
- Historical depth: enough seasons for model training and backtesting.
- Normalization: stable IDs or mapping path across games, teams, and books.

