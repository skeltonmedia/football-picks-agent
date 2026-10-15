#!/usr/bin/env python3
"""backtest-cfb-totals.py — does pace earn its way into the totals model?

    python3 -B 04-agents/football-picks-agent/backtest-cfb-totals.py

WHY (2026-08-30). AQ-131, approved by David ("I approve all open tickets").
The rule from model-notes.md stands: a model change ships through a backtest
against real closing lines, or it does not ship.

DESIGN — LEAKAGE-FREE BY CONSTRUCTION
-------------------------------------
Predicting 2025 games with full-season 2025 stats would flatter any model
(the season being predicted is inside the inputs). So both models here use
ONLY prior-season (2024) inputs — exactly the shape they would have had on
each 2025 kickoff day, and exactly the shape Week 1 2026 just ran on:

  v1 (current): expected total = blend of 2024 SP+ offense/defense
  v2 (pace):    v1 scaled by tempo — (teamA plays/gm + teamB plays/gm)
                relative to league average; more snaps, more points.

Ground truth: data/cfb-lines-history-2025-regular.csv (real DraftKings
closing totals + final scores, fetched 2026-07-27).

VERDICT RULE (pre-registered, same as the Elo/SRS tests):
  v2 replaces v1 only if it beats v1 AND the closing line is not still
  better than both by the same margin as before. Specifically:
  - MAE vs actual total: v2 < v1, else v2 stays out.
  - O/U hit rate in the >=3-pt disagreement bucket: report honestly,
    including bucket sizes. 52.4% is breakeven; small buckets are noise.
"""

from __future__ import annotations

import csv
import json
import pathlib
import statistics
import sys
import urllib.parse
import urllib.request

AGENT = pathlib.Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))

LINES = AGENT / "data" / "cfb-lines-history-2025-regular.csv"
OUT = ROOT / "05-outputs" / "reports" / "football-picks-agent" / "backtest-cfb-totals-2025.md"
TIMEOUT = 60


def api(path: str, key: str) -> list:
    req = urllib.request.Request(f"https://api.collegefootballdata.com{path}",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def cfg_key() -> str:
    for line in (AGENT / "config.env").read_text().splitlines():
        if line.startswith("COLLEGE_FOOTBALL_DATA_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    key = cfg_key()
    if not key:
        print("no CFBD key")
        return 1

    # ── 2024 inputs (what a 2025 bettor would have had) ──────────────────
    sp = {r["team"]: r for r in api("/ratings/sp?year=2024", key)}
    stats: dict[str, dict] = {}
    for s in api("/stats/season?year=2024", key):
        stats.setdefault(s["team"], {})[s["statName"]] = s["statValue"]

    def pace(team: str) -> float | None:
        st = stats.get(team)
        if not st or not st.get("games"):
            return None
        plays = (st.get("rushingAttempts", 0) + st.get("passAttempts", 0)
                 + st.get("sacksOpponent", 0))
        return plays / st["games"] if plays else None

    paces = [p for t in stats if (p := pace(t)) is not None]
    league_pace = statistics.mean(paces)

    def v1_total(a: str, h: str) -> float | None:
        ra, rh = sp.get(a), sp.get(h)
        try:
            return ((float(ra["offense"]["rating"]) + float(rh["defense"]["rating"])) / 2
                    + (float(rh["offense"]["rating"]) + float(ra["defense"]["rating"])) / 2)
        except (TypeError, KeyError, ValueError):
            return None

    # ── walk 2025 games with a real closing total ────────────────────────
    rows = []
    for g in csv.DictReader(LINES.open(encoding="utf-8")):
        try:
            close = float(g["dk_over_under"])
            actual = int(g["home_score"]) + int(g["away_score"])
        except (ValueError, TypeError):
            continue
        a, h = g["away_team"], g["home_team"]
        v1 = v1_total(a, h)
        pa, ph = pace(a), pace(h)
        if v1 is None or pa is None or ph is None:
            continue
        tempo = ((pa + ph) / 2) / league_pace
        v2 = v1 * tempo
        rows.append((a, h, close, actual, v1, v2))

    if len(rows) < 200:
        print(f"only {len(rows)} usable games — not enough to judge")
        return 1

    def mae(idx: int) -> float:
        return statistics.mean(abs(r[idx] - r[3]) for r in rows)

    mae_close = statistics.mean(abs(r[2] - r[3]) for r in rows)
    mae_v1, mae_v2 = mae(4), mae(5)

    def ou_bucket(idx: int, edge: float) -> tuple[int, int]:
        """(hits, n) betting the model side when |model - close| >= edge."""
        hits = n = 0
        for a, h, close, actual, v1, v2 in rows:
            model = (v1, v2)[idx - 4]
            if abs(model - close) < edge or actual == close:
                continue
            n += 1
            picked_over = model > close
            if (actual > close) == picked_over:
                hits += 1
        return hits, n

    lines_out = [
        f"# CFB totals backtest — 2025 season, {len(rows)} games",
        "",
        "Inputs are 2024-only (leakage-free, deployable shape). Ground truth:",
        "DraftKings closing totals + final scores.",
        "",
        "| predictor | MAE vs actual total |",
        "|---|---|",
        f"| DK closing line | **{mae_close:.2f}** |",
        f"| v1 SP+ blend (current) | {mae_v1:.2f} |",
        f"| v2 pace-adjusted | {mae_v2:.2f} |",
        "",
        "O/U hit rate when the model disagrees with the close (52.4% = breakeven):",
        "",
        "| edge | v1 | v2 |",
        "|---|---|---|",
    ]
    for edge in (2.0, 3.0, 5.0):
        h1, n1 = ou_bucket(4, edge)
        h2, n2 = ou_bucket(5, edge)
        p1 = f"{h1/n1*100:.1f}% ({n1})" if n1 else "—"
        p2 = f"{h2/n2*100:.1f}% ({n2})" if n2 else "—"
        lines_out.append(f"| ≥{edge:.0f} pts | {p1} | {p2} |")

    verdict_v2 = mae_v2 < mae_v1
    lines_out += [
        "",
        f"**Verdict:** v2 {'BEATS' if verdict_v2 else 'does NOT beat'} v1 on MAE "
        f"({mae_v2:.2f} vs {mae_v1:.2f}); the closing line "
        f"{'still beats both' if mae_close < min(mae_v1, mae_v2) else 'is beaten — check for leakage before believing this'}.",
        "",
        "Per the pre-registered rule: "
        + ("v2 (pace) replaces v1 in weekly-picks.py."
           if verdict_v2 else
           "v1 stays; pace does not ship. The number was checked, not vibed."),
    ]
    report = "\n".join(lines_out) + "\n"
    OUT.write_text(report, encoding="utf-8")
    print(report)
    print(f"report: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
