#!/usr/bin/env python3
"""compact-snapshots.py — keep line-watch/snapshots.csv lossless but small.

    python3 -B 04-agents/football-picks-agent/compact-snapshots.py            # dry run
    python3 -B 04-agents/football-picks-agent/compact-snapshots.py --apply    # backup, then rewrite

WHY (2026-09-01 deep dive). Every paid pull stores every upcoming event the
provider returns — the whole season, ~1,500 rows — and 80% of those rows are
games more than a week out whose price did not move since the last pull. The
file reached 83MB / 254K rows in 17 capture days with no retention rule, and
nine scripts re-read all of it on every run.

A row is dropped only when ALL of these hold, so no consumer loses a value:
  - it is not in the newest pull for its sport (per-row freshness gates in
    weekly-picks/test-picks/best-price read those rows as "current offers");
  - it is not the first row ever seen for its (event, book, market,
    selection) key (weekly-picks/build-review-cards use the open);
  - its game is more than NEAR_DAYS from the capture (CLV grading wants the
    last pre-kickoff capture untouched, so the final week is never thinned);
  - its (line_points, odds_american) equals the previous kept row for the key
    (a change-only log reconstructs the full price series exactly).

--apply writes a timestamped backup beside the vault (the same iCloud
large-assets folder the monthly backup uses) before replacing the file.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
BACKUP_DIR = Path(os.environ.get(
    "DAVIDOS_LARGE_ASSETS",
    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/DavidOS Backups/large-assets",
))
NEAR_DAYS = 8
KEY = ("event_id", "book", "market", "selection")


def _parse(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def newest_pull_per_sport(rows: list[dict]) -> dict[str, str]:
    """Newest parseable capture per sport; garbage timestamps never win."""
    newest: dict[str, str] = {}
    for r in rows:
        s, c = r.get("sport", ""), r.get("captured_at_et", "")
        if _parse(c) is not None and c > newest.get(s, ""):
            newest[s] = c
    return newest


def compact(rows: list[dict], near_days: int = NEAR_DAYS) -> tuple[list[dict], dict]:
    """Return (kept_rows, stats). Pure function; output sorted by capture time."""
    rows = sorted(rows, key=lambda r: (r.get("captured_at_et", ""), r.get("sport", "")))
    newest = newest_pull_per_sport(rows)
    last_kept: dict[tuple, tuple] = {}
    kept: list[dict] = []
    stats = {"in": len(rows), "kept": 0, "dropped_unchanged_far": 0,
             "kept_newest_pull": 0, "kept_first": 0, "kept_near": 0, "kept_changed": 0,
             "kept_unparseable": 0}
    for r in rows:
        k = tuple(r.get(f, "") for f in KEY)
        price = (r.get("line_points", ""), r.get("odds_american", ""))
        cap, kick = _parse(r.get("captured_at_et", "")), _parse(r.get("commence_time_et", ""))
        near = (kick is None) or (cap is not None and (kick - cap) <= dt.timedelta(days=near_days))
        if cap is None:
            reason = "kept_unparseable"       # an anomaly is evidence; never thin it
        elif r.get("captured_at_et", "") == newest.get(r.get("sport", ""), ""):
            reason = "kept_newest_pull"
        elif k not in last_kept:
            reason = "kept_first"
        elif near:
            reason = "kept_near"
        elif last_kept[k] != price:
            reason = "kept_changed"
        else:
            stats["dropped_unchanged_far"] += 1
            continue
        stats[reason] += 1
        last_kept[k] = price
        kept.append(r)
    stats["kept"] = len(kept)
    return kept, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="rewrite the file (default: report only)")
    ap.add_argument("--near-days", type=int, default=NEAR_DAYS)
    ap.add_argument("--path", type=Path, default=SNAPSHOTS)
    args = ap.parse_args()
    if not args.path.exists():
        print(f"no snapshot file at {args.path}")
        return 0
    with args.path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        rows = list(reader)
    kept, st = compact(rows, args.near_days)
    before = args.path.stat().st_size
    est_after = int(before * st["kept"] / max(1, st["in"]))
    print(f"rows {st['in']:,} → {st['kept']:,} "
          f"(newest pull {st['kept_newest_pull']:,} · first {st['kept_first']:,} · "
          f"near-kickoff {st['kept_near']:,} · changed {st['kept_changed']:,}); "
          f"dropped {st['dropped_unchanged_far']:,} unchanged far-future rows · "
          f"{before/1048576:.1f}MB → ~{est_after/1048576:.1f}MB")
    if not args.apply:
        print("dry run — pass --apply to rewrite (a backup is written first)")
        return 0
    if st["dropped_unchanged_far"] == 0:
        print("nothing to compact")
        return 0
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"snapshots-{stamp}.csv"
    shutil.copy2(args.path, backup)
    tmp = args.path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as out:
        w = csv.DictWriter(out, fieldnames=fields)
        w.writeheader()
        w.writerows(kept)
    os.replace(tmp, args.path)
    print(f"rewrote {args.path.name}: {args.path.stat().st_size/1048576:.1f}MB · backup {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
