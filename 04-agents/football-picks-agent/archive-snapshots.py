#!/usr/bin/env python3
"""archive-snapshots.py — move finished games out of the hot odds log, losslessly.

WHY: snapshots.csv is read in full by football.py, grade-clv.py and edge.py on
every run. On 2026-10-13 it held 197K rows / 70MB, and 109K of those rows were
for games more than 30 days old whose closes were already graded. Rows move to
line-watch/archive/snapshots-YYYY-MM.csv (by game month); nothing is deleted.

    python3 -B 04-agents/football-picks-agent/archive-snapshots.py           # dry run
    python3 -B 04-agents/football-picks-agent/archive-snapshots.py --apply   # backup, archive, rewrite
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import shutil
from collections import defaultdict
from pathlib import Path

AGENT = Path(__file__).resolve().parent
SNAPSHOTS = AGENT / "line-watch" / "snapshots.csv"
ARCHIVE_DIR = AGENT / "line-watch" / "archive"
BACKUP_DIR = AGENT / "line-watch" / "backups"
KEEP_DAYS = 30


def kickoff(row: dict) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat((row.get("commence_time_et") or "").replace("Z", "+00:00"))
        return t if t.tzinfo else None
    except ValueError:
        return None


def split(rows: list[dict], keep_days: int, now: dt.datetime) -> tuple[list[dict], dict[str, list[dict]]]:
    cut = now - dt.timedelta(days=keep_days)
    hot, cold = [], defaultdict(list)
    for r in rows:
        k = kickoff(r)
        if k is None or k >= cut:
            hot.append(r)          # unknown kickoff stays hot: never archive what we cannot date
        else:
            cold[k.strftime("%Y-%m")].append(r)
    return hot, cold


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write archives and rewrite the hot file (default: report only)")
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    ap.add_argument("--path", type=Path, default=SNAPSHOTS)
    args = ap.parse_args()
    if not args.path.exists():
        print(f"no snapshot file at {args.path}")
        return 0
    with args.path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        rows = list(reader)
    now = dt.datetime.now(dt.timezone.utc)
    hot, cold = split(rows, args.keep_days, now)
    n_cold = sum(len(v) for v in cold.values())
    before = args.path.stat().st_size
    print(f"rows {len(rows):,} → hot {len(hot):,} · archive {n_cold:,} across {len(cold)} month file(s) · "
          f"{before/1048576:.1f}MB → ~{before*len(hot)/max(1,len(rows))/1048576:.1f}MB")
    if not args.apply:
        print("dry run — pass --apply to archive (a backup is written first)")
        return 0
    if n_cold == 0:
        print("nothing to archive")
        return 0
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.path, BACKUP_DIR / f"snapshots-prearchive-{stamp}.csv")
    for month, group in sorted(cold.items()):
        target = ARCHIVE_DIR / f"snapshots-{month}.csv"
        new = not target.exists()
        with target.open("a", newline="", encoding="utf-8") as out:   # append: a month may archive in several runs
            w = csv.DictWriter(out, fieldnames=fields)
            if new:
                w.writeheader()
            w.writerows(group)
        print(f"  {target.name}: +{len(group):,} rows")
    tmp = args.path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as out:
        w = csv.DictWriter(out, fieldnames=fields)
        w.writeheader()
        w.writerows(hot)
    os.replace(tmp, args.path)
    with (ARCHIVE_DIR / "manifest.md").open("a", encoding="utf-8") as m:
        m.write(f"- {now.isoformat(timespec='seconds')}: archived {n_cold:,} rows older than {args.keep_days}d "
                f"into {', '.join(sorted(cold))}; hot file {len(hot):,} rows; backup snapshots-prearchive-{stamp}.csv\n")
    print(f"rewrote {args.path.name}: {args.path.stat().st_size/1048576:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
