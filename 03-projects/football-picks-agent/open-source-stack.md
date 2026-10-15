# Open Source Stack

Research date: 2026-07-09.

## Short Answer

Yes: open source is strong enough for the modeling and backtesting layer.

No: open source is not enough by itself for serious real-time odds, injury, and depth-chart freshness. For actual weekly betting decisions, we should expect at least one API-keyed odds source and maybe one paid/current data source.

## Recommended Open Source Core

### NFL: nflreadpy

Python package for downloading NFL data from nflverse repositories.

Use for:

- Play-by-play.
- Player game-level stats.
- Team stats.
- Rosters.
- Backtesting features.

Why it matters:

- Python-native.
- Uses Polars.
- Includes caching.
- MIT-licensed package.
- nflverse data is broadly licensed, mostly CC-BY 4.0, with some FTN data CC-BY-SA 4.0.

Source:

- https://nflreadpy.nflverse.com/

### NFL Alternative: nfl-data-py

Python library for NFL data sourced from nflfastR/nfldata/dynastyprocess/Draft Scout.

Use for:

- Play-by-play back to 1999.
- Weekly and seasonal data.
- Rosters.
- Schedules.
- Scoring lines.
- Win totals.
- Injury reports.
- Depth charts.
- Snap counts.
- NGS data.
- QBR.

Why it matters:

- Mature Python option.
- MIT license.
- Good breadth for modeling and historical research.

Potential concern:

- Latest PyPI release observed in research was 2024-09-20, so compare maintenance velocity against `nflreadpy` before choosing.

Sources:

- https://pypi.org/project/nfl-data-py/
- https://github.com/nflverse/nfl_data_py

### NFL: nflverse / nflfastR / nflreadr

R-first ecosystem with the deepest community around open NFL analytics.

Use for:

- Historical play-by-play.
- EPA/WP style modeling.
- Fourth-down analysis.
- Season simulations.
- Data dictionaries.

Why it matters:

- The community standard for open NFL analysis.
- Play-by-play back to 1999.
- Excellent reference even if the implementation uses Python.

Sources:

- https://nflverse.nflverse.com/
- https://nflreadr.nflverse.com/

### College Football: cfbfastR

R API wrapper around CollegeFootballData plus open-source expected-points and win-probability tooling.

Use for:

- CFB play-by-play.
- Games, drives, teams, players, stats, metrics, ratings.
- Betting line examples.
- Recruiting/coaches/venues.
- Open-source expected points and win probability benchmarking.

Why it matters:

- Best open-source CFB analytics ecosystem.
- Strong for feature engineering and model comparison.

Important:

- CollegeFootballData now requires an API key, even for free usage.

Source:

- https://cfbfastr.sportsdataverse.org/

### SportsDataverse

Broader open sports analytics ecosystem.

Use for:

- Package discovery.
- College football, basketball, hockey, baseball, plotting, and odds wrappers.

Source:

- https://www.sportsdataverse.org/

## Open Source Is Good For

- Historical modeling.
- Backtesting.
- Feature engineering.
- EPA / success rate / explosiveness.
- Team strength ratings.
- Rest/travel/schedule features.
- Closing-line value analysis when historical lines are available.
- Report generation and local dashboards.

## Open Source Is Weak For

- Real-time odds.
- Injury report freshness.
- Depth-chart updates close to kickoff.
- News, beat reports, and late scratches.
- Legal sportsbook line shopping.
- Official real-time play-by-play.

## Recommended Build Choice

Use open source as the base:

```text
NFL model base: nflreadpy
NFL fallback/reference: nfl-data-py + nflverse docs
CFB model base: cfbfastR / CollegeFootballData
Storage: DuckDB or parquet
Modeling: scikit-learn / xgboost / lightgbm
Reports: markdown first
```

Then add one API-keyed current data layer:

```text
Odds: The Odds API or similar
Injuries/depth charts: manual first, SportsDataIO/Sportradar later
```

