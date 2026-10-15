# API Shortlist

Research date: 2026-07-09.

## Recommendation

Start with a hybrid stack:

1. `nflreadpy` / nflverse for NFL historical modeling.
2. CollegeFootballData for college football historical, advanced, and schedule data.
3. The Odds API for market odds snapshots.
4. Trial SportsDataIO if we want accessible paid injuries/depth charts/current stats.
5. Price Sportradar if we want the premium real-time feed.
6. Evaluate PFF only if we can license/export the data cleanly.

## Best Fits

### NFL Historical Modeling: nflreadpy / nflverse

Use for play-by-play, rosters, player stats, team stats, and model training.

Why:

- Python-native `nflreadpy` exists.
- It loads play-by-play, player stats, and team stats.
- nflverse includes play-by-play back to 1999.
- Great for model training, less suited for low-latency game-day injury/news.

Sources:

- https://nflreadpy.nflverse.com/
- https://nflreadr.nflverse.com/
- https://nflverse.nflverse.com/

### College Football Modeling: CollegeFootballData

Use for CFB schedules, teams, advanced metrics, box scores, SP+ trends, win probability, and model-building workflows.

Why:

- Best value CFB source.
- Free API key available.
- Official Python and TypeScript libraries are linked from the site.
- Tiers add higher call limits, live data, GraphQL, and app-scale usage.

Sources:

- https://collegefootballdata.com/
- https://collegefootballdata.com/key

### Odds: The Odds API

Use for current market price snapshots and no-vig comparisons.

Why:

- Supports NFL and NCAAF sport keys.
- Provides odds, events, scores, and historical odds endpoints.
- Markets include moneyline, spreads, and totals.

Source:

- https://the-odds-api.com/liveapi/guides/v4/

### Paid Operational Feed: SportsDataIO

Use if we want a more packaged commercial feed for scores, stats, play-by-play, injuries, lineups/depth charts, odds, fantasy, and news.

Why:

- Likely more accessible than Sportradar for a small build.
- Good candidate for our first paid trial.
- Needs cost/access check before committing.

Source:

- https://sportsdata.io/developers/api-documentation/nfl

### Premium Real-Time Feed: Sportradar

Use if the project graduates from hobby/MVP to serious season operations.

Why:

- NFL feed includes real-time play-by-play, game stats, schedules, rosters, depth charts, weekly injuries, weather in schedules, and push feeds.
- NCAA feed covers Division I and real-time play-by-play for all FBS games when full coverage is available.
- Likely premium/enterprise pricing.

Sources:

- https://developer.sportradar.com/football/reference/nfl-overview
- https://developer.sportradar.com/football/reference/ncaafb-overview

### Player/Snap Quality Layer: PFF

Use only if licensing/export access is clean.

Why:

- PFF grades every player on every play and has NFL + college football coverage.
- Useful for offensive line, pass rush, coverage, and player contribution features traditional box scores miss.
- Treat grades as a feature, not gospel.

Source:

- https://www.pff.com/grades

## MVP Stack

```text
NFL historical: nflreadpy
CFB historical/current: CollegeFootballData
Odds: The Odds API
Injuries/depth charts: manual first, SportsDataIO trial second
Premium upgrade: Sportradar
Player quality: PFF if accessible
```

## Decision

Use `low-cost` posture first:

- Get CollegeFootballData key.
- Get The Odds API key.
- Build local models on nflreadpy and CollegeFootballData.
- Manually review injuries/weather for the first dry run.
- Trial SportsDataIO only after we know exactly which freshness gaps are hurting us.

