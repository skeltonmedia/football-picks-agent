"""Exact team resolution helpers for football data joins.

Sportsbooks usually publish a full display name (school + mascot), while
ratings feeds publish the school name.  The schedule carries both names for
the same team, so it is the authoritative crosswalk.  Substring matching is
deliberately excluded: it can turn North Carolina A&T into North Carolina,
Eastern Illinois into Illinois, or Arkansas-Pine Bluff into Arkansas.
"""

from __future__ import annotations

import unicodedata


# Explicit provider aliases only. Generic prefix matching is intentionally
# forbidden; every entry here names the same school in two source systems.
CFB_SCHOOL_ALIASES = {
    "appalachianstate": "appstate",
    "samhoustonstate": "samhouston",
    "southernmississippi": "southernmiss",
    "umass": "massachusetts",
}


def fold_team_name(value: str) -> str:
    """Normalize punctuation/diacritics without changing word identity."""
    folded = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return "".join(char for char in folded.lower() if char.isalnum())


def build_school_crosswalk(schedule_rows: list[dict]) -> dict[str, str]:
    """Map each schedule display name to its exact normalized school name."""
    crosswalk: dict[str, str] = {}
    for row in schedule_rows:
        for full_col, school_col in (
            ("away_team_full", "away_team_school"),
            ("home_team_full", "home_team_school"),
        ):
            full = fold_team_name(row.get(full_col, ""))
            school = fold_team_name(row.get(school_col, ""))
            if full and school:
                crosswalk[full] = school
    return crosswalk


def canonical_cfb_school(team: str, display_to_school: dict[str, str]) -> str:
    """Return the exact schedule school identity for a provider team name.

    The odds feed occasionally adds a word that ESPN omits, such as
    ``Sam Houston State`` versus ``Sam Houston``. Only the explicit aliases
    above are allowed to bridge that gap.
    """
    display = fold_team_name(team)
    exact = display_to_school.get(display)
    if exact:
        return exact
    for provider_name, schedule_name in CFB_SCHOOL_ALIASES.items():
        if display == provider_name or display.startswith(provider_name):
            return schedule_name
    return display


def resolve_team_record(
    team: str,
    records_by_school: dict[str, dict],
    display_to_school: dict[str, str],
) -> dict | None:
    """Resolve a record only through an exact school or schedule identity."""
    school = canonical_cfb_school(team, display_to_school)
    return records_by_school.get(school)
