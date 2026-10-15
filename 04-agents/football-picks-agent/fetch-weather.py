#!/usr/bin/env python3
"""Fetch kickoff-hour weather for outdoor games on the current NFL/CFB slate.

Keyless. Two public US-government / OSM sources, no API key anywhere:

  1. Nominatim (OpenStreetMap) — venue name -> lat/lon.
  2. api.weather.gov — lat/lon -> gridpoint -> hourly forecast.

Why this exists (2026-08-12): `readme.md` has claimed since the agent was built
that "Weather comes from the free/keyless api.weather.gov (no key exists for
that)" — but no script ever called it. The review cards asked for weather as a
manual fill and the weekly report never mentioned it. Documented-but-absent is
worse than absent, because the checklist implies the data is there.

Indoor venues are skipped outright: `nfl-schedule.csv` carries an `indoor` flag
from ESPN, and a dome forecast is noise on a totals decision.

Writes `data/weather.csv`. Rows are only as good as the schedule's venue field —
if `fetch-nfl-slate.py` has not been run, there is nothing to geocode.

Rate limits: Nominatim asks for <=1 request/second and a real User-Agent with
contact info. Both are honoured. Geocode results are cached in
`data/venue-coords.csv` so a venue is looked up once, not once per run.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "04-agents" / "football-picks-agent"
DATA_DIR = AGENT_DIR / "data"
NFL_SCHEDULE_PATH = DATA_DIR / "nfl-schedule.csv"
CFB_SCHEDULE_PATH = DATA_DIR / "cfb-schedule.csv"
COORDS_PATH = DATA_DIR / "venue-coords.csv"
WEATHER_PATH = DATA_DIR / "weather.csv"
ET = ZoneInfo("America/New_York")

# Nominatim's usage policy requires an identifying UA with contact info.
UA = "DavidOS-football-picks-agent/1.0 (hello@skeltonmedia.com)"

FIELDNAMES = [
    "captured_at_et",
    "league",
    "matchup",
    "kickoff_et",
    "venue",
    "city",
    "state",
    "lat",
    "lon",
    "forecast_time",
    "temperature_f",
    "wind",
    "wind_direction",
    "precip_probability",
    "short_forecast",
    "note",
]


def get_json(url: str, timeout: int = 25) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


CoordKey = tuple[str, str, str]


def coord_cache_key(venue: str, city: str, state: str) -> CoordKey:
    """Keep identically named venues in different cities from sharing coordinates."""
    return tuple(part.strip() for part in (venue, city, state))


def load_coord_cache() -> dict[CoordKey, tuple[str, str]]:
    cache: dict[CoordKey, tuple[str, str]] = {}
    for row in read_csv(COORDS_PATH):
        if row.get("lat"):
            cache[coord_cache_key(row.get("venue", ""), row.get("city", ""),
                                  row.get("state", ""))] = (row["lat"], row["lon"])
    return cache


def save_coord_cache(cache: dict[CoordKey, tuple[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with COORDS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["venue", "city", "state", "lat", "lon"])
        # Old caches had venue only. Once a location-qualified entry exists,
        # discard its unsafe legacy row instead of letting it linger forever.
        qualified = {venue for venue, city, state in cache if city or state}
        for (venue, city, state), (lat, lon) in sorted(cache.items()):
            if not city and not state and venue in qualified:
                continue
            writer.writerow([venue, city, state, lat, lon])


def geocode_queries(venue: str, city: str, state: str) -> list[str]:
    """Return safest-first US queries; a bare venue name is never sufficient."""
    if not (state or "").strip():
        return []
    return [f"{venue}, {city}, {state}", f"{city}, {state}"]


def geocode(venue: str, city: str, state: str) -> tuple[str, str] | None:
    """Venue name first, then city/state as a fallback. Returns (lat, lon).

    NO US STATE -> NO GEOCODE. The CFB schedule leaves `state` empty for
    international venues; with countrycodes=us pinned, "Aviva Stadium, Dublin"
    resolved to Dublin OHIO and the 2026-08-29 UNC/TCU card carried an Ohio
    forecast for a game in Ireland. NWS is US-only, so there is no honest
    forecast to fetch for these games — better none than the wrong country's.
    """
    for query in geocode_queries(venue, city, state):
        if not query.strip(" ,"):
            continue
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "limit": 1, "countrycodes": "us"}
        )
        try:
            results = get_json(url)
        except (urllib.error.URLError, json.JSONDecodeError):
            return None
        time.sleep(1.1)  # Nominatim: <= 1 req/sec
        if results:
            return results[0]["lat"], results[0]["lon"]
    return None


def forecast_at(lat: str, lon: str, kickoff_utc: str) -> dict[str, str] | None:
    """Hourly forecast period covering kickoff, or the nearest available."""
    try:
        point = get_json(f"https://api.weather.gov/points/{lat},{lon}")
        hourly_url = point["properties"]["forecastHourly"]
        data = get_json(hourly_url)
    except (urllib.error.URLError, KeyError, json.JSONDecodeError):
        return None

    periods = data.get("properties", {}).get("periods", [])
    if not periods:
        return None
    try:
        target = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    except ValueError:
        return None

    best, best_delta = None, None
    for period in periods:
        try:
            start = datetime.fromisoformat(period["startTime"])
        except (ValueError, KeyError):
            continue
        delta = abs((start - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_delta = period, delta
    if best is None:
        return None
    # weather.gov only publishes ~7 days of hourly data; anything further out
    # would silently return the last available hour and look like a real forecast.
    if best_delta > 6 * 3600:
        return {"out_of_range": "true"}
    precip = (best.get("probabilityOfPrecipitation") or {}).get("value")
    return {
        "forecast_time": best.get("startTime", "")[:16],
        "temperature_f": str(best.get("temperature", "")),
        "wind": best.get("windSpeed", ""),
        "wind_direction": best.get("windDirection", ""),
        "precip_probability": "" if precip is None else str(precip),
        "short_forecast": best.get("shortForecast", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch kickoff-hour weather for outdoor games.")
    parser.add_argument("--league", choices=["nfl", "cfb", "both"], default="both")
    args = parser.parse_args()

    games: list[tuple[str, dict[str, str]]] = []
    if args.league in ("nfl", "both"):
        games += [("NFL", g) for g in read_csv(NFL_SCHEDULE_PATH)]
    if args.league in ("cfb", "both"):
        games += [("CFB", g) for g in read_csv(CFB_SCHEDULE_PATH)]
    if not games:
        print("No schedule rows found. Run fetch-nfl-slate.py / fetch-cfb-slate.py first.")
        return 1

    cache = load_coord_cache()
    captured_at = datetime.now(ET).replace(microsecond=0).isoformat()
    rows, skipped_indoor, no_venue, out_of_range = [], 0, 0, 0

    for league, game in games:
        venue = (game.get("venue") or "").strip()
        if str(game.get("indoor", "")).lower() == "true":
            skipped_indoor += 1
            continue
        if not venue:
            no_venue += 1
            continue

        coord_key = coord_cache_key(venue, game.get("city", ""), game.get("state", ""))
        if coord_key in cache:
            lat, lon = cache[coord_key]
        else:
            coords = geocode(venue, game.get("city", ""), game.get("state", ""))
            if not coords:
                rows.append({**{k: "" for k in FIELDNAMES}, "captured_at_et": captured_at,
                             "league": league, "matchup": game.get("matchup", ""),
                             "kickoff_et": game.get("kickoff_et", ""), "venue": venue,
                             "city": game.get("city", ""), "state": game.get("state", ""),
                             "note": "geocode failed"})
                continue
            lat, lon = coords
            cache[coord_key] = (lat, lon)

        fc = forecast_at(lat, lon, game.get("kickoff_utc", ""))
        base = {
            "captured_at_et": captured_at, "league": league,
            "matchup": game.get("matchup", ""), "kickoff_et": game.get("kickoff_et", ""),
            "venue": venue, "city": game.get("city", ""), "state": game.get("state", ""),
            "lat": lat, "lon": lon,
        }
        if fc is None:
            rows.append({**{k: "" for k in FIELDNAMES}, **base, "note": "forecast unavailable"})
        elif fc.get("out_of_range"):
            out_of_range += 1
            rows.append({**{k: "" for k in FIELDNAMES}, **base,
                         "note": "kickoff beyond the ~7-day hourly forecast window — re-run closer to game day"})
        else:
            rows.append({**{k: "" for k in FIELDNAMES}, **base, **fc, "note": ""})

    save_coord_cache(cache)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with WEATHER_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    usable = sum(1 for r in rows if r.get("temperature_f"))
    print(
        f"Wrote {len(rows)} weather rows ({usable} with a usable forecast) to "
        f"{WEATHER_PATH.relative_to(ROOT)}. "
        f"Skipped {skipped_indoor} indoor, {no_venue} with no venue, {out_of_range} beyond the forecast window."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
