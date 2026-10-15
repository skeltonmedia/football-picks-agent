#!/usr/bin/env python3
"""Opt-in, one-shot v3 odds snapshots. Default is offline status; all data is shadow.

Setup, authority, and supported scope live in the project integration note.
No retries, discovery calls, schedules, source merging, or betting actions.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

AGENT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT.parent / "lib"))
from davidos_lib import atomic_write_text

BASE_URL = "https://api.opticodds.com/api/v3"
SHADOW = AGENT / "data" / "opticodds-shadow"
KEY_FILE = Path.home() / ".config" / "davidos" / "opticodds.env"
UTC = dt.timezone.utc
MARKETS = {
    "moneyline": ("h2h", "Moneyline"),
    "point_spread": ("spreads", "Point Spread"),
    "total_points": ("totals", "Total Points"),
    "player_passing_yards": ("player_pass_yds", "Player Passing Yards"),
}
FIELDS = [
    "source", "mode", "captured_at_utc", "source_updated_at_utc", "source_timestamp",
    "sport", "event_id", "source_fixture_id", "commence_time_utc", "away_team", "home_team",
    "source_away_team_id", "source_home_team_id", "book", "source_sportsbook",
    "market", "source_market_id", "period", "selection", "line_points", "odds_american",
    "source_quote_id", "source_selection", "source_normalized_selection",
    "source_team_id", "source_player_id", "source_grouping_key", "is_main",
]


class FeedError(ValueError):
    """A safe, credential-free message suitable for CLI output."""


def utc_iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: object) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise FeedError("invalid_timestamp") from None


def finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeedError("invalid_number")
    result = float(value)
    if not math.isfinite(result):
        raise FeedError("invalid_number")
    return result


def required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise FeedError("missing_identity")
    return value.strip()


def book_key(value: object) -> str:
    # Display names remain in a separate provenance field; this is shadow-only.
    return re.sub(r"[\W_]+", "", required_text(value).casefold())


def unavailable(quote: dict) -> bool:
    for flag in ("locked", "is_locked", "suspended", "is_suspended"):
        if flag in quote and quote[flag] is not False:
            return True
    if "status" in quote and str(quote["status"]).casefold() not in {"open", "active", "available"}:
        return True
    return quote.get("type") == "locked-odds"


def fixture_metadata(fixture: dict, now: dt.datetime, expected_ids: set[str]) -> dict:
    fixture_id = required_text(fixture.get("id"))
    if fixture_id not in expected_ids:
        raise FeedError("unexpected_fixture")
    if fixture.get("sport", {}).get("id") != "football" or fixture.get("league", {}).get("id") not in {"nfl", "ncaaf"}:
        raise FeedError("unsupported_league")
    if fixture.get("status") != "unplayed" or fixture.get("is_live") is not False:
        raise FeedError("not_pregame")
    kickoff = parse_time(fixture.get("start_date"))
    if kickoff <= now:
        raise FeedError("already_started")
    teams = {}
    for side in ("home", "away"):
        competitors = fixture.get(f"{side}_competitors")
        if not isinstance(competitors, list) or len(competitors) != 1 or not isinstance(competitors[0], dict):
            raise FeedError("ambiguous_teams")
        teams[f"source_{side}_team_id"] = required_text(competitors[0].get("id"))
        teams[f"{side}_team"] = required_text(competitors[0].get("name"))
    if teams["source_home_team_id"] == teams["source_away_team_id"] or teams["home_team"] == teams["away_team"]:
        raise FeedError("ambiguous_teams")
    return {"source": "opticodds_v3", "mode": "shadow", "captured_at_utc": utc_iso(now),
            "sport": fixture["league"]["id"], "event_id": f"opticodds:{fixture_id}",
            "source_fixture_id": fixture_id, "commence_time_utc": utc_iso(kickoff), **teams}


def normalize_quote(quote: dict, fixture: dict, now: dt.datetime, allowed_books: set[str],
                    allowed_markets: set[str], max_age_hours: float) -> dict:
    if unavailable(quote):
        raise FeedError("unavailable_quote")
    quote_id = required_text(quote.get("id"))
    book = book_key(quote.get("sportsbook"))
    if book not in allowed_books:
        raise FeedError("unexpected_sportsbook")
    market_id = required_text(quote.get("market_id"))
    if market_id not in MARKETS or market_id not in allowed_markets:
        raise FeedError("unsupported_market")
    market, display_name = MARKETS[market_id]
    # Exact full-game market identity prevents quarters/halves and team totals leaking in.
    if quote.get("market") != display_name or any(quote.get(k) is not None for k in ("period", "period_id")):
        raise FeedError("ambiguous_period")
    if quote.get("is_main") is not True:
        raise FeedError("alternate_line")
    price = finite_number(quote.get("price"))
    if abs(price) < 100 or not price.is_integer():
        raise FeedError("invalid_american_price")
    timestamp = finite_number(quote.get("timestamp"))
    try:
        updated = dt.datetime.fromtimestamp(timestamp, UTC)
    except (ValueError, OverflowError, OSError):
        raise FeedError("invalid_timestamp") from None
    age = (now - updated).total_seconds()
    if age < -60 or age > max_age_hours * 3600:
        raise FeedError("stale_or_future_quote")
    selection = required_text(quote.get("selection"))
    team_id = quote.get("team_id")
    player_id = quote.get("player_id")
    points = quote.get("points")
    side = quote.get("selection_line")
    team_names = {fixture["source_home_team_id"]: fixture["home_team"],
                  fixture["source_away_team_id"]: fixture["away_team"]}
    if market in {"h2h", "spreads"}:
        if team_id not in team_names or selection != team_names[team_id] or player_id is not None or side is not None:
            raise FeedError("ambiguous_team_selection")
        normalized_selection = team_names[team_id]
        if market == "h2h":
            if points is not None:
                raise FeedError("moneyline_has_points")
        else:
            points = finite_number(points)
    elif market == "totals":
        if team_id is not None or player_id is not None or side not in {"over", "under"} or selection.casefold() != side:
            raise FeedError("ambiguous_total_selection")
        normalized_selection = side.title()
        points = finite_number(points)
        if points < 0:
            raise FeedError("invalid_line")
    else:
        # Preserve provider player identity. No name-only match to an existing roster.
        required_text(player_id)
        if team_id not in team_names or side not in {"over", "under"}:
            raise FeedError("ambiguous_player_selection")
        normalized_selection = side.title()
        points = finite_number(points)
        if points < 0:
            raise FeedError("invalid_line")
    return {**fixture, "source_updated_at_utc": utc_iso(updated), "source_timestamp": str(timestamp),
            "book": book, "source_sportsbook": quote["sportsbook"], "market": market,
            "source_market_id": market_id, "period": "full_game", "selection": normalized_selection,
            "line_points": "" if points is None else f"{points:g}", "odds_american": f"{price:g}",
            "source_quote_id": quote_id, "source_selection": selection,
            "source_normalized_selection": quote.get("normalized_selection") or "",
            "source_team_id": team_id or "", "source_player_id": player_id or "",
            "source_grouping_key": quote.get("grouping_key") or "", "is_main": "true"}


def normalize(payload: dict, *, now: dt.datetime, fixture_ids: list[str], sportsbooks: list[str],
              markets: list[str], max_age_hours: float = 6) -> tuple[list[dict], dict]:
    """Normalize one complete snapshot; invalid duplicates cannot resurrect older quotes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise FeedError("invalid_response_schema")
    rejected = Counter()
    candidates = defaultdict(list)
    for raw in payload["data"]:
        try:
            meta = fixture_metadata(raw, now, set(fixture_ids))
            if not isinstance(raw.get("odds"), list):
                raise FeedError("missing_odds")
        except (FeedError, AttributeError, TypeError) as error:
            rejected[str(error) if isinstance(error, FeedError) else "invalid_fixture"] += 1
            continue
        for quote in raw["odds"]:
            if not isinstance(quote, dict):
                rejected["invalid_quote"] += 1
                continue
            try:
                identity = (meta["source_fixture_id"], book_key(quote.get("sportsbook")), required_text(quote.get("id")))
            except FeedError as error:
                rejected[str(error)] += 1
                continue
            candidates[identity].append((quote, meta))
    normalized = []
    for group in candidates.values():
        try:
            if any(unavailable(q) for q, _ in group):
                raise FeedError("unavailable_quote")
            newest = max(finite_number(q.get("timestamp")) for q, _ in group)
            latest = [(q, m) for q, m in group if finite_number(q.get("timestamp")) == newest]
            rows = [normalize_quote(q, m, now, {book_key(b) for b in sportsbooks}, set(markets), max_age_hours)
                    for q, m in latest]
            if any(row != rows[0] for row in rows[1:]):
                raise FeedError("conflicting_source_quote")
            normalized.append(rows[0])
            rejected["duplicate_source_quote"] += len(group) - 1
        except (FeedError, TypeError) as error:
            rejected[str(error) if isinstance(error, FeedError) else "invalid_quote"] += len(group)
    # One bookmaker contributes one observation at a selection/line, even with duplicate IDs.
    groups = defaultdict(list)
    for row in normalized:
        identity = tuple(row[k] for k in ("source_fixture_id", "book", "market", "period", "selection", "line_points", "source_player_id"))
        groups[identity].append(row)
    rows = []
    for group in groups.values():
        newest = max(float(r["source_timestamp"]) for r in group)
        latest = [r for r in group if float(r["source_timestamp"]) == newest]
        if len({r["odds_american"] for r in latest}) > 1:
            rejected["conflicting_book_quote"] += len(group)
            continue
        rows.append(sorted(latest, key=lambda r: r["source_quote_id"])[0])
        rejected["duplicate_book_quote"] += len(group) - 1
    return sorted(rows, key=lambda r: (r["source_fixture_id"], r["book"], r["source_quote_id"])), dict(+rejected)


def load_key() -> str | None:
    value = os.environ.get("OPTICODDS_API_KEY", "").strip()
    if not value and KEY_FILE.exists():
        info = KEY_FILE.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise FeedError("Private credential file must be a regular owner-only file (mode 600).")
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("OPTICODDS_API_KEY="):
                value = line.strip().split("=", 1)[1].strip().strip("\"'")
                break
    if value and (any(ord(c) < 32 for c in value) or value in {"YOUR_API_KEY", "REPLACE_ME"}):
        raise FeedError("Credential is malformed or still a placeholder.")
    return value or None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FeedError("API redirect refused; no credential forwarded.")


def api_get(params: list[tuple[str, str]], key: str) -> dict:
    request = urllib.request.Request(f"{BASE_URL}/fixtures/odds?{urllib.parse.urlencode(params)}",
                                     headers={"X-Api-Key": key, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
            raw = response.read(20_000_001)
            if len(raw) > 20_000_000:
                raise FeedError("API response exceeds snapshot size limit.")
            # Do not persist a credential even if a provider accidentally echoes it.
            if key.encode() in raw:
                raise FeedError("API response unexpectedly contains a credential; receipt refused.")
            payload = json.loads(raw)
    except urllib.error.HTTPError as error:
        raise FeedError(f"API returned HTTP {error.code}; stopped without retry.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FeedError("API connection failed; stopped without retry.") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FeedError("API returned invalid JSON; stopped without retry.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise FeedError("API response has no fixture list; stopped without retry.")
    return payload


def plan_requests(fixture_ids: list[str], sportsbooks: list[str], markets: list[str]) -> list[list[tuple[str, str]]]:
    if not fixture_ids or not sportsbooks:
        raise FeedError("Supply explicit --fixture-id and --sportsbook values.")
    if len(set(fixture_ids)) != len(fixture_ids) or len({book_key(b) for b in sportsbooks}) != len(sportsbooks):
        raise FeedError("Duplicate fixture or sportsbook requested.")
    if len(sportsbooks) > 5 or len(fixture_ids) > 50:
        raise FeedError("Local trial limit: 5 sportsbooks and 50 explicit fixtures per run.")
    for value in fixture_ids + sportsbooks:
        required_text(value)
    return [[("fixture_id", item) for item in fixture_ids[i:i + 5]] +
            [("sportsbook", item) for item in sportsbooks] + [("market", item) for item in markets] +
            [("odds_format", "AMERICAN"), ("is_main", "true")]
            for i in range(0, len(fixture_ids), 5)]


def save_snapshot(receipt: dict, rows: list[dict], run_id: str) -> tuple[Path, Path]:
    # No latest file: missing/suspended quotes are never carried forward between pulls.
    raw_path = SHADOW / f"{run_id}-receipt.json"
    csv_path = SHADOW / f"{run_id}-quotes.csv"
    if raw_path.exists() or csv_path.exists():
        raise FeedError("Snapshot name already exists; refusing overwrite.")
    atomic_write_text(raw_path, json.dumps(receipt, indent=2, allow_nan=False) + "\n")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(csv_path, buffer.getvalue())
    return raw_path, csv_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Offline status (the default).")
    mode.add_argument("--dry-run", action="store_true", help="Print plan without reading credentials, writing, or requesting.")
    mode.add_argument("--execute", action="store_true", help="Run an explicitly authorized, bounded shadow pull.")
    parser.add_argument("--fixture-id", action="append", default=[])
    parser.add_argument("--sportsbook", action="append", default=[])
    parser.add_argument("--market", action="append", choices=sorted(MARKETS))
    parser.add_argument("--max-requests", type=int, help="Required for execution; hard HTTP request ceiling (1–10).")
    args = parser.parse_args(argv)
    try:
        if not args.execute and not args.dry_run:
            print(json.dumps({"mode": "shadow", "network_requests": 0,
                              "environment_key_present": bool(os.environ.get("OPTICODDS_API_KEY")),
                              "private_key_file_present": KEY_FILE.is_file(),
                              "access_and_cost": "unverified", "production_connected": False,
                              "shadow_directory": str(SHADOW)}))
            return 0
        markets = args.market or ["moneyline", "point_spread", "total_points"]
        plan = plan_requests(args.fixture_id, args.sportsbook, markets)
        if args.max_requests is not None and not 1 <= args.max_requests <= 10:
            raise FeedError("--max-requests must be between 1 and 10.")
        if args.execute and args.max_requests is None:
            raise FeedError("Execution requires an explicit --max-requests allowance.")
        if args.max_requests is not None and len(plan) > args.max_requests:
            raise FeedError("Planned requests exceed --max-requests; no request made.")
        if args.dry_run:
            print(json.dumps({"mode": "dry_run", "network_requests": 0, "planned_requests": len(plan),
                              "endpoint": f"{BASE_URL}/fixtures/odds", "query_parameters": plan,
                              "access_and_cost": "unverified"}, indent=2))
            return 0
        key = load_key()
        if not key:
            raise FeedError("No credential configured; zero requests made. See the integration note.")
        receipt = {"source": "opticodds_v3", "mode": "shadow", "max_requests": args.max_requests,
                   "requests_attempted": 0, "complete": False, "requests": [], "normalization_rejections": {}}
        run_id = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        rows = []
        failure = None
        for params in plan:
            receipt["requests_attempted"] += 1
            try:
                payload = api_get(params, key)
                captured = dt.datetime.now(UTC)
                receipt["requests"].append({"endpoint": "/fixtures/odds", "params": params,
                                            "captured_at_utc": utc_iso(captured), "response": payload})
                batch, rejected = normalize(payload, now=captured,
                                            fixture_ids=[v for k, v in params if k == "fixture_id"],
                                            sportsbooks=args.sportsbook, markets=markets)
                rows.extend(batch)
                receipt["normalization_rejections"] = dict(Counter(receipt["normalization_rejections"]) + Counter(rejected))
            except FeedError as error:
                failure = str(error)
                break
        receipt["complete"] = failure is None
        receipt["error"] = failure
        # Partial pulls remain evidence, but cannot be mistaken for a complete normalized snapshot.
        paths = save_snapshot(receipt, rows if failure is None else [], run_id)
        print(json.dumps({"mode": "shadow", "complete": receipt["complete"],
                          "requests_attempted": receipt["requests_attempted"],
                          "normalized_rows": len(rows) if failure is None else 0,
                          "rejected": receipt["normalization_rejections"],
                          "receipt": str(paths[0]), "quotes": str(paths[1]), "error": failure}))
        return 1 if failure else 0
    except FeedError as error:
        print(json.dumps({"mode": "shadow", "error": str(error)}))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"mode": "shadow", "error": "Local configuration or storage failed; no exception details logged."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
