#!/usr/bin/env python3
"""Snapshot NFL and NCAAF spread/total/moneyline odds from The Odds API.

Requires ODDS_API_KEY (set in the gitignored config.env and exported before
running, or passed via the environment). If no key is configured, this
prints exactly what's missing and exits non-zero rather than writing a
fabricated snapshot -- matching mlb-picks-agent's "no market, no PLAY" rule.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
LINE_WATCH_DIR = AGENT_DIR / "line-watch"
SNAPSHOTS_PATH = LINE_WATCH_DIR / "snapshots.csv"
QUOTA_STATUS_PATH = LINE_WATCH_DIR / "quota-status.json"
PAID_PULL_STATUS_PATH = LINE_WATCH_DIR / "paid-pulls.json"
API_BASE = "https://api.the-odds-api.com/v4"
ET = ZoneInfo("America/New_York")
DEFAULT_COOLDOWN_HOURS = 12.0
DEFAULT_MARKETS = "h2h,spreads,totals"

# NFL preseason is a SEPARATE sport key on The Odds API. The regular-season key
# returns nothing for August games, which is why a 2026-08-12 run looked like
# "no NFL games until September 9" when 16 preseason games were live and priced
# across 9 books. Added 2026-08-12.
SPORT_KEYS = {
    "nfl": "americanfootball_nfl",
    "nfl_preseason": "americanfootball_nfl_preseason",
    "ncaaf": "americanfootball_ncaaf",
    # 2026-10-07: hockey joins the daily card. Same h2h/spreads/totals pull;
    # "spreads" is the puck line. NHL plays daily, so run-daily-data.py
    # schedules its 3-credit pull every morning, football stays rationed.
    "nhl": "icehockey_nhl",
}

FIELDNAMES = [
    "captured_at_et",
    "sport",
    "event_id",
    "commence_time_et",
    "away_team",
    "home_team",
    "book",
    "bookmaker_last_update",
    "market",
    "selection",
    "line_points",
    "odds_american",
    # Bet-slip deep links (The Odds API includeLinks; present for DK/FD/BetMGM
    # in most US markets). outcome_link pre-fills the book's slip with this
    # exact selection; event_link opens the game page. Added 2026-08-16.
    "event_link",
    "outcome_link",
]


class OddsApiError(RuntimeError):
    """Provider error that keeps quota headers available to the caller."""

    def __init__(self, status_code: int, detail: str, headers: dict[str, str]):
        super().__init__(f"Odds API HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


def env_list(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def api_get(path: str, params: dict[str, str]) -> tuple[object, dict[str, str]]:
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "DavidOS-football-picks-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode("utf-8")
            headers = {k.lower(): v for k, v in response.headers.items()}
            return json.loads(body), headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        raise OddsApiError(exc.code, detail, headers) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach The Odds API: {exc}") from exc


def _header_int(headers: dict[str, str], name: str) -> int | None:
    try:
        return int(headers.get(name, ""))
    except (TypeError, ValueError):
        return None


def persist_quota_status(
    headers: dict[str, str],
    captured_at: str,
    *,
    sport: str,
    status: str,
) -> None:
    """Persist provider quota headers without ever storing the API key."""
    LINE_WATCH_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at_et": captured_at,
        "requests_remaining": _header_int(headers, "x-requests-remaining"),
        "requests_used": _header_int(headers, "x-requests-used"),
        "last_request_cost": _header_int(headers, "x-requests-last"),
        "sport": sport,
        "status": status,
    }
    temp = QUOTA_STATUS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(QUOTA_STATUS_PATH)


def persisted_remaining() -> int | None:
    try:
        value = json.loads(QUOTA_STATUS_PATH.read_text(encoding="utf-8")).get(
            "requests_remaining"
        )
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def record_paid_pull(sport: str, captured_at: str) -> None:
    """Free close captures must not postpone the multi-book refresh cooldown."""
    try:
        state = json.loads(PAID_PULL_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state[sport] = captured_at
    temp = PAID_PULL_STATUS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp.replace(PAID_PULL_STATUS_PATH)


def newest_snapshot_at(sport: str, path: Path = SNAPSHOTS_PATH) -> datetime | None:
    """Latest paid-provider capture, not a newer free-feed close confirmation.

    Existing multi-book files identify paid captures by a non-free book or
    bookmaker deep link. Without provenance retain the conservative legacy
    cooldown rather than assuming permission to spend again.
    """
    if path == SNAPSHOTS_PATH:
        try:
            raw = json.loads(PAID_PULL_STATUS_PATH.read_text(encoding="utf-8")).get(sport)
            if raw:
                captured = datetime.fromisoformat(raw)
                if captured.tzinfo is not None:
                    return captured
        except (OSError, ValueError, TypeError):
            pass
    if not path.exists():
        return None
    newest: datetime | None = None
    paid_newest: datetime | None = None
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("sport") != sport:
                continue
            raw = row.get("captured_at_et", "")
            try:
                captured = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=ET)
            if newest is None or captured > newest:
                newest = captured
            if row.get("book") not in {"draftkings", "pinnacle"} or row.get("event_link") or row.get("outcome_link"):
                if paid_newest is None or captured > paid_newest:
                    paid_newest = captured
    return paid_newest or newest


def inside_cooldown(
    latest: datetime | None,
    now: datetime,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
) -> bool:
    if latest is None or cooldown_hours <= 0:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    age_hours = (now - latest).total_seconds() / 3600
    return 0 <= age_hours < cooldown_hours


def normalize_rows(data: list[dict], sport: str, captured_at: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for event in data:
        commence_utc = event.get("commence_time", "")
        commence_et = ""
        if commence_utc:
            try:
                commence_et = (
                    datetime.fromisoformat(commence_utc.replace("Z", "+00:00")).astimezone(ET).isoformat()
                )
            except ValueError:
                commence_et = ""
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                market_key = market.get("key", "")
                for outcome in market.get("outcomes", []):
                    rows.append(
                        {
                            "captured_at_et": captured_at,
                            "sport": sport,
                            "event_id": event.get("id", ""),
                            "commence_time_et": commence_et,
                            "away_team": event.get("away_team", ""),
                            "home_team": event.get("home_team", ""),
                            "book": book.get("key", ""),
                            "bookmaker_last_update": book.get("last_update", ""),
                            "market": market_key,
                            "selection": outcome.get("name", ""),
                            "line_points": outcome.get("point", ""),
                            "odds_american": outcome.get("price", ""),
                            "event_link": book.get("link", "") or "",
                            "outcome_link": outcome.get("link", "") or "",
                        }
                    )
    return rows


def append_rows(rows: list[dict[str, str]]) -> None:
    LINE_WATCH_DIR.mkdir(parents=True, exist_ok=True)
    exists = SNAPSHOTS_PATH.exists()
    if exists:
        # Migrate snapshots written before the link columns existed (2026-08-16):
        # rewrite once with the current header, old rows getting empty links.
        with SNAPSHOTS_PATH.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames and reader.fieldnames != FIELDNAMES:
                old_rows = [{k: r.get(k, "") for k in FIELDNAMES} for r in reader]
                with SNAPSHOTS_PATH.open("w", newline="", encoding="utf-8") as out:
                    w = csv.DictWriter(out, fieldnames=FIELDNAMES)
                    w.writeheader()
                    w.writerows(old_rows)
    with SNAPSHOTS_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot NFL/NCAAF spread, total, and moneyline odds.")
    parser.add_argument("--sport", choices=sorted(SPORT_KEYS), action="append", help="Repeatable; defaults to both")
    parser.add_argument(
        "--cooldown-hours",
        type=float,
        default=DEFAULT_COOLDOWN_HOURS,
        help="skip a league whose newest successful snapshot is newer than this",
    )
    parser.add_argument("--force", action="store_true", help="bypass the duplicate-pull cooldown")
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="read and persist quota headers from the provider's zero-credit sports endpoint",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        print(
            "ODDS_API_KEY is not set. Sign up for a free-tier key at "
            "https://the-odds-api.com/, add it to "
            "04-agents/football-picks-agent/config.env, export it before "
            "running this script, and re-run. No snapshot was written.",
            file=sys.stderr,
        )
        return 1

    captured = datetime.now(ET).replace(microsecond=0)
    captured_at = captured.isoformat()

    if args.status_only:
        try:
            _, headers = api_get("/sports/", {"apiKey": api_key})
        except OddsApiError as exc:
            persist_quota_status(
                exc.headers, captured_at, sport="status", status=f"http-{exc.status_code}"
            )
            print(str(exc), file=sys.stderr)
            return 1
        persist_quota_status(headers, captured_at, sport="status", status="ok")
        print(
            "Odds API credits: "
            f"{headers.get('x-requests-remaining', 'unknown')} remaining, "
            f"{headers.get('x-requests-used', 'unknown')} used "
            f"(status check cost {headers.get('x-requests-last', 'unknown')})."
        )
        return 0

    sports = args.sport or ["nfl", "ncaaf"]   # preseason is opt-in via --sport nfl_preseason
    bookmakers = env_list("ODDS_API_BOOKMAKERS", "draftkings,fanduel,betmgm")
    # 2026-09-03: David restored spreads to the full weekly slate. The Odds
    # API bills by market x region, so the league-specific pull now costs
    # three credits instead of two. The Tue/Thu/Sat NCAA + Sun NFL rationing
    # remains unchanged; an explicit env override can still narrow a run.
    markets = env_list("ODDS_API_MARKETS", DEFAULT_MARKETS)
    total_rows = 0
    failures = 0
    requests_made = 0
    for sport in sports:
        latest = newest_snapshot_at(sport)
        if not args.force and inside_cooldown(latest, captured, args.cooldown_hours):
            age_hours = (captured - latest).total_seconds() / 3600 if latest else 0
            print(
                f"Skipped {sport.upper()} paid pull: newest successful snapshot is "
                f"{age_hours:.1f}h old (cooldown {args.cooldown_hours:g}h)."
            )
            continue
        sport_key = SPORT_KEYS[sport]
        params = {
            "apiKey": api_key,
            "bookmakers": bookmakers,
            "markets": markets,
            "oddsFormat": "american",
            # The explicit bookmaker list includes the retail books and
            # Pinnacle. It takes priority over `regions`; up to 10 books count
            # as one quota region, so h2h+spreads+totals costs 3 credits per
            # league.
            "includeLinks": "true",
        }
        try:
            data, headers = api_get(f"/sports/{sport_key}/odds", params)
        except OddsApiError as exc:
            failures += 1
            persist_quota_status(
                exc.headers, captured_at, sport=sport, status=f"http-{exc.status_code}"
            )
            print(str(exc), file=sys.stderr)
            continue
        requests_made += 1
        persist_quota_status(headers, captured_at, sport=sport, status="ok")
        rows = normalize_rows(data, sport, captured_at)
        append_rows(rows)
        record_paid_pull(sport, captured_at)
        total_rows += len(rows)
        print(
            f"Captured {len(rows)} {sport.upper()} rows across {len(data)} games "
            f"(requests remaining: {headers.get('x-requests-remaining', 'unknown')})."
        )

    remaining = persisted_remaining()
    quota = str(remaining) if remaining is not None else "unknown"
    print(
        f"Wrote {total_rows} total rows to {SNAPSHOTS_PATH.relative_to(ROOT)}; "
        f"{requests_made} paid pull(s), {quota} credits remaining."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
