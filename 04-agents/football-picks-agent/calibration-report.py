#!/usr/bin/env python3
"""calibration-report.py — does the confidence score predict closing-line value?

    python3 -B 04-agents/football-picks-agent/calibration-report.py
    python3 -B 04-agents/football-picks-agent/calibration-report.py --write   # also save the md

WHY (2026-09-01 deep dive). The board ranks by a hand-set formula (book
disagreement 40 · rating gap 35 · price quality 15 · data quality) that has
never been checked against the one KPI the agent itself names: average CLV
vs the Pinnacle close. The rules forbid touching the weights before 30
matched decisions. This report is the evidence that decision will need,
computed from recommendation-ledger.csv only — it changes no selection.

Read it like this: if the top confidence bucket does not beat the lower ones
on CLV once the sample is there, the ranking is ordering noise and the
weights are a proposal, not a finding.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
sys.path.insert(0, str(ROOT / "04-agents" / "lib"))
from davidos_lib import now_eastern  # noqa: E402
from football_performance import identity  # noqa: E402

LEDGER = AGENT / "recommendation-ledger.csv"
OUT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"
MIN_SAMPLE = 30          # the agent's own rule before any weight change
BUCKETS = ((80, 101, "80+"), (70, 80, "70-79"), (60, 70, "60-69"), (0, 60, "<60"))
# AQ-221: the live ranking since 2026-09-18 is the edge in cents (ledger column
# edge_cents). Buckets follow the backtest ladder and the unit floors.
EDGE_BUCKETS = ((2.0, 1e9, "≥2.0¢ (2u)"), (1.5, 2.0, "1.5-2.0¢ (1u)"), (1.0, 1.5, "1.0-1.5¢ (board)"),
                (0.0, 1.0, "0-1.0¢ (board)"), (-1e9, 0.0, "<0¢"))


def _f(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(path: Path = LEDGER) -> list[dict]:
    """One row per unique selection, at its EARLIEST valid publication.

    2026-09-07: the ledger carries the same selection under several
    publication dates (Tuesday benchmark, Thursday rerank, Saturday card), and
    counting each copy inflated "decisions" from ~34 to 47 and tripped the
    30-row floor on duplicates. CLV's benchmark is the frozen first price
    (AQ-162), so the earliest publication is the row that counts here; the
    W-L record uses the latest line and lives in football_performance.
    """
    if not path.exists():
        return []
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    first: dict = {}
    for r in rows:
        if r.get("decision") not in ("PICK", "LEAN") or r.get("result") == "VOID":
            continue
        key = identity(r)
        stamp = (r.get("decision_date", ""), r.get("captured_at_et", ""))
        prev = first.get(key)
        if prev is None or stamp < (prev.get("decision_date", ""), prev.get("captured_at_et", "")):
            first[key] = r
    return list(first.values())


def summarize(rows: list[dict]) -> dict:
    graded = [r for r in rows if _f(r.get("closing_line_value_pct", "")) is not None]
    out = {"decisions": len(rows), "graded": len(graded), "by_bucket": [], "by_edge": [], "by_market": [],
           "by_league": [], "record": {"W": 0, "L": 0, "PUSH": 0, "VOID": 0, "pending": 0}}
    for r in rows:
        out["record"][r.get("result") or "pending"] = out["record"].get(r.get("result") or "pending", 0) + 1

    def agg(group_rows: list[dict]) -> dict:
        clv = [_f(r["closing_line_value_pct"]) for r in group_rows]
        wins = sum(1 for r in group_rows if r.get("result") == "W")
        losses = sum(1 for r in group_rows if r.get("result") == "L")
        return {"n": len(clv), "avg_clv": statistics.mean(clv) if clv else None,
                "beat_close": sum(1 for c in clv if c > 0), "w": wins, "l": losses}

    for lo, hi, label in BUCKETS:
        g = [r for r in graded if (_f(r.get("confidence", "")) or -1) >= lo
             and (_f(r.get("confidence", "")) or -1) < hi]
        out["by_bucket"].append((label, agg(g)))
    with_edge = [r for r in graded if _f(r.get("edge_cents", "")) is not None]
    for lo, hi, label in EDGE_BUCKETS:
        g = [r for r in with_edge if lo <= _f(r["edge_cents"]) < hi]
        out["by_edge"].append((label, agg(g)))
    out["edge_rows"] = len(with_edge)
    for key, name in (("market", "by_market"), ("league", "by_league")):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in graded:
            groups[r.get(key, "") or "?"].append(r)
        out[name] = [(k, agg(v)) for k, v in sorted(groups.items())]
    overall = agg(graded)
    out["overall"] = overall
    return out


def render(s: dict) -> str:
    stamp = now_eastern()
    lines = ["---", "type: agent-report", "agent: football-picks-agent",
             f"date: {stamp.date().isoformat()}", "report: calibration", "---", "",
             f"# Confidence vs CLV calibration — {stamp.strftime('%b %-d, %Y')}", ""]
    o = s["overall"]
    verdict = ("**Sample too small to judge the weights** "
               f"({s['graded']} graded of {MIN_SAMPLE} required). NO CHANGE is the only honest disposition."
               if s["graded"] < MIN_SAMPLE else
               "**Sample large enough to evaluate the weights.** Compare the buckets below before proposing any change.")
    lines += [verdict, "",
              f"Decisions {s['decisions']} · graded {s['graded']} · "
              f"avg CLV {o['avg_clv']:+.2f}% · beat close {o['beat_close']}/{o['n']} · "
              f"record {s['record'].get('W',0)}-{s['record'].get('L',0)}"
              + (f"-{s['record']['PUSH']}" if s['record'].get('PUSH') else "")
              + f" ({s['record'].get('pending',0)} pending)" if o["n"] else
              f"Decisions {s['decisions']} · nothing graded yet", ""]

    def table(title: str, groups: list) -> None:
        lines.extend([f"## {title}", "", "| bucket | n | avg CLV | beat close | W-L |", "|---|---:|---:|---:|---:|"])
        for label, a in groups:
            if a["n"] == 0:
                lines.append(f"| {label} | 0 | — | — | — |")
            else:
                lines.append(f"| {label} | {a['n']} | {a['avg_clv']:+.2f}% | {a['beat_close']}/{a['n']} | {a['w']}-{a['l']} |")
        lines.append("")

    table(f"By edge bucket (the ranking since 2026-09-18; {s.get('edge_rows', 0)} rows carry an edge)", s["by_edge"])
    table("By legacy confidence bucket (ranked on until 2026-09-18; logged for comparison)", s["by_bucket"])
    table("By market", s["by_market"])
    table("By league", s["by_league"])
    lines += ["## How to read it", "",
              "- CLV is the KPI (positive = took a better number than the close). W-L is shown because",
              "  David will ask; over 30 decisions it is mostly variance.",
              "- The edge table is the live test: the ≥1.5¢ rows should beat the close well above the",
              "  board-only rows (the backtest said 69% vs 41-50%). If they do not after 30+ edge rows,",
              "  the floor is wrong or the signal decayed; that is the evidence an AQ needs.",
              "- Legacy confidence is kept for one month of side-by-side comparison, never ranked on.",
              "- Rows are PICK/LEAN only; INVALID and WATCH never count.", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="also write the md report")
    ap.add_argument("--ledger", type=Path, default=LEDGER)
    args = ap.parse_args()
    s = summarize(load(args.ledger))
    text = render(s)
    print(text)
    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / f"calibration-{now_eastern().date().isoformat()}.md"
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
