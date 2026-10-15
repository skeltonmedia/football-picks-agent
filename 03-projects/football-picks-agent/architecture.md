# Architecture

## Recommended First Build

Start with a local Python pipeline, not a complex app.

```text
fetch-nfl-slate.py
fetch-cfb-slate.py
fetch-odds.py
write-weekly-report.py
review-outcomes.py
```

The first version should run manually and write markdown. Once the weekly loop is useful, promote selected steps to scheduled runs.

**2026-07-27 revision:** built on Python stdlib (`urllib`, `csv`, `argparse`, `zoneinfo`) and CSV/markdown storage instead of the parquet/DuckDB/pydantic/httpx stack drafted below. See `decisions.md` — this matches how `mlb-picks-agent` and `world-cup-agent` already run in this vault, with zero new dependencies to install or maintain.

## Storage

CSV under the ignored runtime directory, mirroring `mlb-picks-agent`'s ledger pattern:

```text
04-agents/football-picks-agent/data/nfl-schedule.csv
04-agents/football-picks-agent/data/cfb-schedule.csv
04-agents/football-picks-agent/line-watch/snapshots.csv
04-agents/football-picks-agent/recommendation-ledger.csv
```

Keep derived reports in:

```text
05-outputs/reports/football-picks-agent/
```

(Original parquet/DuckDB direction kept below for reference in case the model layer later outgrows stdlib.)

## Source Layers

### Historical Modeling

- NFL: nflverse.
- CFB: CollegeFootballData.

### Current Market

- The Odds API first, unless another odds provider is chosen.

### Premium Real-Time Upgrade

- SportsDataIO or Sportradar if the system needs more reliable injury, depth chart, play-by-play, or live scoring coverage.

## Model Philosophy

Use the betting market as the baseline, not the enemy. The model should ask whether there is a measurable reason to disagree with the current price.

Minimum viable models:

- Spread model.
- Total model.
- Moneyline probability model.

Each model should produce:

- Fair line or fair probability.
- Confidence interval or uncertainty label.
- Feature contribution summary.
- Data freshness status.

## Agent Boundary

The agent writes reports and recommendations. It does not connect to sportsbook accounts, place wagers, move money, or simulate certainty.

