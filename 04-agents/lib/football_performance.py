"""Recommendation performance, with repeated publication snapshots counted once."""
from __future__ import annotations

from datetime import date, timedelta

TRACKED = {"PICK", "LEAN"}
MARKETS = {"moneyline": "h2h", "spread": "spreads", "total": "totals"}


def identity(row: dict) -> tuple:
    market = row.get("market", "").strip().lower()
    return (
        row.get("league", "").upper(), row.get("game_date", ""),
        row.get("event_id") or row.get("matchup", "").strip().casefold(),
        MARKETS.get(market, market), row.get("selection", "").strip().casefold(),
    )


def latest_recommendations(rows: list[dict]) -> list[dict]:
    latest = {}
    for row in rows:
        if row.get("decision", "").upper() not in TRACKED or row.get("result") == "VOID":
            continue
        try:
            game_day = date.fromisoformat(row.get("game_date", ""))
            decision_day = date.fromisoformat(row.get("decision_date", ""))
        except ValueError:
            continue
        if decision_day > game_day:
            continue
        key = identity(row)
        stamp = (row.get("decision_date", ""), row.get("captured_at_et", ""))
        previous = latest.get(key)
        if previous is None or stamp > (previous.get("decision_date", ""), previous.get("captured_at_et", "")):
            latest[key] = row
    return sorted(latest.values(), key=lambda r: (r["game_date"], r.get("matchup", ""), r.get("market", ""), r.get("selection", "")))


def record(rows: list[dict]) -> dict:
    counts = {"w": 0, "l": 0, "push": 0, "pending": 0}
    for row in rows:
        key = {"W": "w", "L": "l", "PUSH": "push"}.get(row.get("result", ""), "pending")
        counts[key] += 1
    decided = counts["w"] + counts["l"]
    return {**counts, "total": len(rows), "win_pct": round(100 * counts["w"] / decided, 1) if decided else None}


def performance(rows: list[dict], today: date, week_starts_on: int = 1, main_card_size: int = 10) -> dict:
    if not 0 <= week_starts_on <= 6:
        raise ValueError("week_starts_on must be in 0..6")
    start = today - timedelta(days=(today.weekday() - week_starts_on) % 7)
    end = start + timedelta(days=6)
    unique = latest_recommendations(rows)
    week = [r for r in unique if start.isoformat() <= r["game_date"] <= end.isoformat()]
    eligible = [r for r in rows if r.get("decision", "").upper() in TRACKED and r.get("result") != "VOID"
                and start.isoformat() <= r.get("game_date", "") <= end.isoformat()]
    publications = []
    for published in sorted({r.get("decision_date", "") for r in eligible if start.isoformat() <= r.get("decision_date", "") <= end.isoformat()}):
        published_rows = latest_recommendations([r for r in eligible if r.get("decision_date") == published])
        publications.append({"date": published, **record(published_rows)})
    full_cards = [p for p in publications if p["total"] == main_card_size]
    main_date = full_cards[-1]["date"] if full_cards else None
    main_rows = latest_recommendations([r for r in eligible if r.get("decision_date") == main_date]) if main_date else []
    all_week = record(week)
    week = main_rows
    tiers = []
    for label, lo, hi in (("5 stars", 80, 101), ("4 stars", 65, 80), ("3 stars or below", 0, 65)):
        group = [r for r in week if lo <= float(r.get("confidence") or 0) < hi]
        if group:
            tiers.append({"label": label, **record(group)})
    return {
        "start": start.isoformat(), "end": end.isoformat(), "record": record(week),
        "main_card_date": main_date, "main_card_size": main_card_size,
        "all_recommendations": all_week, "by_confidence": tiers,
        "season": record(unique), "publications": publications,
        "by_market": [{"market": market, **record([r for r in week if MARKETS.get(r.get("market"), r.get("market")) == market])}
                      for market in sorted({MARKETS.get(r.get("market"), r.get("market")) for r in week})],
        "rows": [{key: r.get(key, "") for key in ("game_date", "decision_date", "matchup", "market", "selection", "line_points", "book", "odds_american", "confidence", "result", "source_report")}
                 for r in week],
        "excluded": sum(r.get("decision", "").upper() not in TRACKED or r.get("result") == "VOID"
                        for r in rows if start.isoformat() <= r.get("game_date", "") <= end.isoformat()),
        "repeat_snapshots": len(eligible) - all_week["total"],
    }


def latest_complete_main_card(rows: list[dict], today: date, week_starts_on: int = 1,
                              main_card_size: int = 10, lookback_weeks: int = 12) -> dict:
    """Return the newest full weekly card, including the prior week on rollover day.

    The Tuesday-to-Monday reporting window intentionally rolls over Tuesday morning.
    Packet readers still need the latest completed learning cohort until the new
    Tuesday card exists, so walk backward by reporting week rather than showing 0-0.
    """
    if lookback_weeks < 1:
        raise ValueError("lookback_weeks must be at least 1")
    for weeks_back in range(lookback_weeks):
        report = performance(
            rows,
            today - timedelta(days=7 * weeks_back),
            week_starts_on=week_starts_on,
            main_card_size=main_card_size,
        )
        if report["main_card_date"]:
            return report
    return performance(rows, today, week_starts_on=week_starts_on, main_card_size=main_card_size)
