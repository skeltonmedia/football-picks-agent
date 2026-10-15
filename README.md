# football-picks-agent

A sports betting agent that runs on live data every day without me. NFL, college football and the NHL. Built entirely with Claude Code as a side project to see if an AI-built system can run autonomously, keep an honest record, and get better from it.

It does not win yet. The record since the price-first rewrite is 49-40-3, up about four units flat, beating the closing line 41 percent of the time. The pipeline is solid. The edge is not proven. That is why it is here: I would rather compare notes with other people building the same thing than keep guessing alone.

Write-up with diagrams: https://skeltonmedia.com/blog/a-betting-agent-on-live-data.html

## What it does

Every morning a launchd job runs 20 steps (`run-daily-data.py`): schedules from ESPN, one rationed paid odds pull from The Odds API, weather from api.weather.gov, injuries, college ratings from CollegeFootballData, a small budget of player props. Then it settles yesterday's picks from box scores, grades every logged pick against the closing line, and writes a dashboard payload.

Every 30 minutes a second job (`fetch-free-lines.py`) pulls free lines: Pinnacle's keyless guest API as the sharp reference, DraftKings as ESPN embeds it as the bettable line. Rows captured inside 75 minutes of kickoff become the close.

`football.py` is the one agent. Everything else is a fetcher, a settler, or research that lost.

```
python3 -B 04-agents/football-picks-agent/football.py status   # freshness, credits, record
python3 -B 04-agents/football-picks-agent/football.py daily    # today's win-first card
python3 -B 04-agents/football-picks-agent/football.py parlay   # the gated 3-leg ticket
python3 -B 04-agents/football-picks-agent/football.py board --league ncaaf
python3 -B 04-agents/football-picks-agent/football.py settle
python3 -B 04-agents/football-picks-agent/football.py learn    # writes lessons.md + learned-rules.json
```

## The loop

1. Candidates: both sides of every market, moneyline, total, prop, three leagues.
2. Edge versus Pinnacle: no-vig fair probability at the same line. Is DraftKings offering better than fair?
3. The gate: edge floor, stale price, blocked slice. Parlays: three legs at most, every leg at 56 percent or better, half a unit, one a day. A ticket that breaks a rule prints BLOCKED and `--place` refuses to log it.
4. The card: win-first, one per game, plain text. Every shown row is logged.
5. A human places the bet. The agent never wagers.
6. Settle from ESPN finals and box scores. Grade closing line value.
7. Learn: any league and market slice under water on 30 or more graded rows gets written into `learned-rules.json`, which the gate reads the next morning.

The agent does not predict games. An Elo model backtested against 715 college games with real closing lines landed at noise around the 52.4 percent breakeven. An NFL SRS model on 208 games had the same shape. Ratings are context for the human reviewer, never a gate. See `04-agents/football-picks-agent/model-notes.md`.

## Layout

The agent was built inside an Obsidian vault with numbered top-level folders, and the scripts resolve paths relative to that layout. This repo keeps the same shape so nothing had to be rewritten to publish it:

```
04-agents/football-picks-agent/   the agent, fetchers, settlers, docs
04-agents/lib/                    shared helpers (team resolution, performance, a small shim)
00-system/scripts/                dashboard payload writer
03-projects/football-picks-agent/ output contracts and source research
```

Everything is plain CSV, JSON and markdown. There is no database. When the output looks wrong you read the input in a text editor.

## Setup

Python 3.9+, standard library only.

```
cp 04-agents/football-picks-agent/config.example.env 04-agents/football-picks-agent/config.env
```

Fill in `ODDS_API_KEY` (The Odds API) and `COLLEGE_FOOTBALL_DATA_API_KEY` (CollegeFootballData). ESPN, Pinnacle's guest API, api.weather.gov, Nominatim and nflverse need no key. Thresholds and the parlay gate live in the same file.

```
python3 -B 04-agents/football-picks-agent/run-daily-data.py --no-odds   # free steps only
python3 -B 04-agents/football-picks-agent/fetch-free-lines.py
python3 -B 04-agents/football-picks-agent/football.py status
```

The two `.plist` files are the launchd schedules with paths replaced by placeholders.

Not in the repo: `config.env`, the odds log (`line-watch/`, 180MB and regenerable), my ledgers, and the retired graphics renderer's assets.

## Where it struggles

- Team names. Every feed spells teams differently. `data/team-aliases.csv` and `free-lines-unmatched.txt` are the current answer and I do not believe they are the right one.
- Credits. The paid pull is rationed, so most of the day the agent compares a fresh DraftKings line to a morning snapshot of the other books.
- The parlay pool only admits games where DraftKings disagrees with Pinnacle, so a plain 70 percent favorite at a fair price never gets in.
- The hold. A favorite at a fair price loses about four percent to the margin. The agent sizes those at zero, which is correct and means most days it says do not bet.

If you are building the same thing: where does a price-first agent find edge the market prices slowly, what would you log that I am not, and how are you handling team names? Open an issue.

## Scope, on purpose

No autonomous wagering. No live betting. No DFS. The agent proposes; a human and a written rule decide.

MIT.
