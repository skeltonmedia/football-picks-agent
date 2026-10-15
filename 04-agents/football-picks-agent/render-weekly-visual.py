#!/usr/bin/env python3
"""Render a Daddy Dan's Picks weekly markdown card as HTML and PNG.

The scenic background is allowed to vary. All copy is parsed from the locked
`Paste to iMessage` block so image generation never has to typeset betting
lines, team names, or kickoff times.

Usage:
    python3 -B 04-agents/football-picks-agent/render-weekly-visual.py CARD.md
    python3 -B 04-agents/football-picks-agent/render-weekly-visual.py CARD.md \
        --theme classic-fan-cave
    python3 -B 04-agents/football-picks-agent/render-weekly-visual.py CARD.md \
        --background /path/to/new-themed-background.png
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


AGENT = Path(__file__).resolve().parent
ROOT = AGENT.parents[1]
CONFIG = AGENT / "visual-card-config.json"
OUT_DIR = ROOT / "05-outputs" / "reports" / "football-picks-agent"


@dataclass(frozen=True)
class Pick:
    rank: int
    play: str
    matchup: str
    stars: str
    units: int
    kickoff: str
    reason: str
    conditional_units: bool = False
    edge: str | None = None
    cash_confidence: str | None = None
    board_only: bool = False
    decision: str | None = None


@dataclass(frozen=True)
class Card:
    week: str
    stake_line: str
    picks: tuple[Pick, ...]


def extract_imessage_block(markdown: str) -> str:
    match = re.search(
        r"^## Paste to iMessage\s*\n+```(?:text)?\s*\n(.*?)\n```",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValueError("weekly card has no fenced 'Paste to iMessage' block")
    return match.group(1).strip()


def split_play(raw: str) -> tuple[str, str]:
    if " — " in raw:
        play, matchup = raw.split(" — ", 1)
        return play.strip(), matchup.strip()
    match = re.match(r"(.+?)\s+over\s+(.+)$", raw, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip(), f"OVER {match.group(2).strip()}"
    return raw.strip(), ""


def parse_imessage(block: str) -> Card:
    lines = [line.rstrip() for line in block.splitlines()]
    nonempty = [line.strip() for line in lines if line.strip()]
    if len(nonempty) < 3:
        raise ValueError("iMessage block is too short to form a visual card")

    week_match = re.search(r"Week of\s+(.+?)(?:\s+[—·]|$)", nonempty[0], re.I)
    if not week_match:
        raise ValueError("iMessage block has no 'Week of' date")
    week = week_match.group(1).strip()
    stake_line = nonempty[1]

    picks: list[Pick] = []
    i = 0
    # The line may carry edge, manual decision, and unit state. Older cards
    # without a decision label remain supported.
    pick_line = re.compile(
        r"^(\d+)\)\s+(.+?)\s+([⭐★]{1,5})"
        r"(?:\s+·\s+(?:([+-]?\d+(?:\.\d+)?)¢|(\d+(?:\.\d+)?)%\s+CASH))?"
        r"(?:\s+·\s+(PICK|LEAN|WATCH|PASS))?"
        r"(?:\s+·\s+(?:(\d+)\s+units?(\s+if played)?|(board only)[^\n]*))?$",
        flags=re.IGNORECASE,
    )
    while i < len(lines):
        match = pick_line.match(lines[i].strip())
        if not match:
            i += 1
            continue

        rank = int(match.group(1))
        raw_play = match.group(2).strip()
        stars = "★" * len(match.group(3))
        edge = match.group(4)
        cash_confidence = match.group(5)
        decision = match.group(6).upper() if match.group(6) else None
        board_only = bool(match.group(9))
        units = 0 if board_only else int(match.group(7)) if match.group(7) else (2 if len(stars) == 5 else 1)
        conditional_units = bool(match.group(8))
        if decision in {"PASS", "WATCH"} and units != 0:
            raise ValueError(f"{decision} row {rank} must show 0 units")
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        kickoff = lines[i].strip() if i < len(lines) else ""
        i += 1

        reason_lines: list[str] = []
        while i < len(lines):
            stripped = lines[i].strip()
            if pick_line.match(stripped) or stripped.startswith("⭐ ="):
                break
            if not stripped:
                if reason_lines:
                    break
                i += 1
                continue
            reason_lines.append(stripped)
            i += 1

        play, matchup = split_play(raw_play)
        picks.append(
            Pick(
                rank=rank,
                play=play,
                matchup=matchup,
                stars=stars,
                units=units,
                kickoff=kickoff,
                reason=" ".join(reason_lines),
                conditional_units=conditional_units,
                edge=edge,
                cash_confidence=cash_confidence,
                board_only=board_only,
                decision=decision,
            )
        )

    if not picks:
        raise ValueError("iMessage block contains no ranked picks")
    if len(picks) > 10:
        raise ValueError(f"visual card supports at most 10 picks, found {len(picks)}")
    return Card(week=week, stake_line=stake_line, picks=tuple(picks))


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def image_data_uri(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def background_url(background: Path, html_dir: Path = OUT_DIR) -> str:
    """Relative file URL for the CSS background. WHY (AQ-149): the base64
    data URI embedded the 2.5MB background in every daily HTML (3.4MB per
    report, committed nightly). A relative path renders identically from
    file:// for the PNG export and keeps each report ~50KB."""
    return Path(os.path.relpath(background, html_dir)).as_posix()


def render_html(card: Card, config: dict, theme_name: str, background: Path) -> str:
    themes = config.get("themes", {})
    if theme_name not in themes:
        available = ", ".join(sorted(themes)) or "none"
        raise ValueError(f"unknown theme '{theme_name}'; available: {available}")
    theme = themes[theme_name]
    esc = html.escape

    rows = []
    for pick in card.picks:
        metric = ""
        if pick.edge:
            metric = f'<div class="edge">{esc(pick.edge)}¢ vs books</div>'
        elif pick.cash_confidence:
            metric = f'<div class="edge">{esc(pick.cash_confidence)}% cash</div>'
        if pick.decision in {"PASS", "WATCH"}:
            unit_class = "units conditional"
            unit_label = f"{pick.decision} · 0U"
        elif pick.board_only:
            unit_class = "units conditional"
            unit_label = "BOARD ONLY"
        elif pick.conditional_units:
            unit_class = "units conditional"
            unit_label = f"{pick.units}U IF PLAYED"
        else:
            unit_class = "units"
            unit_label = f"{pick.units} UNIT{'S' if pick.units != 1 else ''}"
        rows.append(
            f"""
            <article class="pick-row">
              <div class="rank">{pick.rank}</div>
              <div class="selection">
                <div class="play">{esc(pick.play)}</div>
                <div class="matchup">{esc(pick.matchup)}</div>
              </div>
              <div class="meta">
                <div class="stars">{esc(pick.stars)}</div>{metric}
                <div class="{unit_class}">{unit_label}</div>
                <div class="kickoff">{esc(pick.kickoff)}</div>
              </div>
              <div class="reason">{esc(pick.reason)}</div>
            </article>
            """.strip()
        )

    empty_rows = max(0, 10 - len(card.picks))
    short_board = ""
    if empty_rows:
        short_message = config.get(
            "short_board_message",
            "JUST {count} MADE THE BOARD THIS WEEK.",
        ).format(count=len(card.picks))
        short_board = f"""
          <div class="short-board" style="grid-row: span {empty_rows}">
            <div>{esc(short_message)}</div>
          </div>
        """.strip()

    board_items = rows + ([short_board] if short_board else [])
    board_html = "\n".join(board_items)

    title = esc(config["brand_name"])
    stake = esc(card.stake_line.upper())
    footer = esc(config["footer"])
    background_uri = background_url(background)
    width, height = int(config["width"]), int(config["height"])
    accent = esc(theme["accent"])
    accent_dark = esc(theme["accent_dark"])
    paper = esc(theme["paper"])
    muted = esc(theme["muted"])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={width}, initial-scale=1">
<title>{title} · Week of {esc(card.week)}</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; width: {width}px; height: {height}px; overflow: hidden; }}
  body {{ background: #090705; }}
  .poster {{
    position: relative;
    width: {width}px;
    height: {height}px;
    overflow: hidden;
    background: #090705 url('{background_uri}') center / cover no-repeat;
    color: {paper};
    font-family: "Avenir Next Condensed", "Arial Narrow", Arial, sans-serif;
  }}
  .board {{
    position: absolute;
    left: 207px;
    top: 92px;
    width: 610px;
    height: 1280px;
    display: grid;
    grid-template-rows: 224px minmax(0, 1fr) 76px;
    padding: 5px 16px 9px;
    background: linear-gradient(90deg, rgba(4,4,4,.04), rgba(8,8,8,.18), rgba(4,4,4,.04));
    text-shadow: 0 2px 2px rgba(0,0,0,.9);
  }}
  .header {{ text-align: center; display: flex; flex-direction: column; align-items: center; }}
  .brand-top {{
    margin-top: 3px;
    font-family: Impact, "Arial Narrow", sans-serif;
    font-size: 60px;
    line-height: .92;
    letter-spacing: 2.4px;
    text-transform: uppercase;
    color: #f8ede2;
    -webkit-text-stroke: 2px {accent};
    text-shadow: 0 0 5px #fff, 0 0 10px {accent}, 0 0 24px {accent}, 0 4px 2px #000;
  }}
  .brand-picks {{
    margin-top: -2px;
    font-family: "Marker Felt", "Chalkboard SE", "Comic Sans MS", cursive;
    font-size: 69px;
    line-height: .9;
    font-style: italic;
    font-weight: 900;
    letter-spacing: 2px;
    color: {paper};
    transform: rotate(-1deg);
  }}
  .week-ribbon {{
    margin-top: 12px;
    min-width: 342px;
    padding: 7px 24px 6px;
    color: #fff8eb;
    background: linear-gradient(180deg, {accent}, {accent_dark});
    border: 2px solid rgba(255,255,255,.18);
    box-shadow: 0 3px 0 #3b080d, 0 5px 10px rgba(0,0,0,.65);
    font-size: 20px;
    font-weight: 900;
    letter-spacing: 2px;
    text-transform: uppercase;
    clip-path: polygon(0 10%, 5% 10%, 7% 0, 93% 0, 95% 10%, 100% 10%, 96% 50%, 100% 90%, 95% 90%, 93% 100%, 7% 100%, 5% 90%, 0 90%, 4% 50%);
  }}
  .stake {{ margin-top: 11px; font-family: "Marker Felt", "Chalkboard SE", cursive; font-size: 17px; font-weight: 800; letter-spacing: 1px; color: {paper}; }}
  .picks {{ display: grid; grid-template-rows: repeat(10, minmax(0, 1fr)); min-height: 0; border-top: 2px solid rgba(242,234,220,.6); }}
  .pick-row {{
    min-height: 0;
    display: grid;
    grid-template-columns: 42px 150px 76px minmax(0, 1fr);
    gap: 6px;
    align-items: center;
    border-bottom: 1px solid rgba(242,234,220,.45);
    padding: 3px 1px;
  }}
  .rank {{
    width: 38px;
    height: 56px;
    display: grid;
    place-items: center;
    background: linear-gradient(145deg, {accent}, {accent_dark});
    border: 2px solid rgba(242,234,220,.72);
    box-shadow: inset 0 0 0 2px rgba(0,0,0,.22), 2px 2px 0 rgba(0,0,0,.65);
    font-family: Impact, sans-serif;
    font-size: 32px;
    line-height: 1;
    color: {paper};
  }}
  .selection {{ min-width: 0; }}
  .play {{
    font-family: Impact, "Arial Narrow", sans-serif;
    font-size: 18px;
    line-height: 1.02;
    letter-spacing: .35px;
    color: {paper};
    text-transform: uppercase;
    overflow-wrap: anywhere;
  }}
  .matchup {{
    margin-top: 5px;
    font-size: 14px;
    line-height: 1.05;
    font-weight: 900;
    letter-spacing: .4px;
    color: #e54a51;
    text-transform: uppercase;
    overflow-wrap: anywhere;
  }}
  .meta {{ align-self: center; text-align: center; }}
  .stars {{ color: #e94a50; font-size: 14px; line-height: 1; letter-spacing: 0; white-space: nowrap; }}
  .edge {{ color: #f3d9a4; font-size: 10px; letter-spacing: .04em; line-height: 1; margin-top: 2px; white-space: nowrap; }}
  .units {{
    margin-top: 5px;
    font-family: Impact, "Arial Narrow", sans-serif;
    font-size: 11px;
    line-height: 1;
    letter-spacing: .65px;
    color: {paper};
    white-space: nowrap;
  }}
  .units.conditional {{
    font-size: 9px;
    letter-spacing: .25px;
  }}
  .kickoff {{
    display: inline-block;
    margin-top: 5px;
    padding: 4px 2px 3px;
    border: 1px solid rgba(242,234,220,.62);
    border-radius: 2px;
    font-family: Impact, "Arial Narrow", sans-serif;
    font-size: 8px;
    line-height: 1;
    letter-spacing: .05px;
    color: {paper};
    white-space: nowrap;
  }}
  .reason {{
    align-self: center;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 8;
    overflow: hidden;
    font-family: "Avenir Next", Arial, sans-serif;
    font-size: 9.7px;
    line-height: 1.18;
    font-weight: 550;
    color: #e9e3da;
  }}
  .short-board {{
    display: grid;
    place-items: center;
    min-height: 0;
    border-bottom: 1px solid rgba(242,234,220,.45);
    padding: 24px 50px;
    text-align: center;
    color: rgba(242,234,220,.46);
    font-family: "Marker Felt", "Chalkboard SE", cursive;
    font-size: 27px;
    font-weight: 800;
    line-height: 1.25;
    letter-spacing: 1.2px;
    transform: rotate(-1deg);
  }}
  .footer {{
    align-self: end;
    justify-self: center;
    width: 365px;
    padding: 10px 16px 9px;
    border: 2px solid rgba(242,234,220,.6);
    background: rgba(7,7,7,.86);
    box-shadow: 0 4px 10px rgba(0,0,0,.72);
    text-align: center;
    font-family: "Marker Felt", "Chalkboard SE", cursive;
    font-size: 15px;
    font-weight: 800;
    line-height: 1.2;
    letter-spacing: .35px;
    color: {paper};
  }}
  .footer::after {{ content: "  ↗"; color: {muted}; font-size: 20px; }}
</style>
</head>
<body>
  <main class="poster" aria-label="{title}, week of {esc(card.week)}">
    <section class="board">
      <header class="header">
        <div class="brand-top">DADDY DAN'S</div>
        <div class="brand-picks">PICKS</div>
        <div class="week-ribbon">WEEK OF {esc(card.week)}</div>
        <div class="stake">{stake}</div>
      </header>
      <section class="picks">{board_html}</section>
      <footer class="footer">{footer}</footer>
    </section>
  </main>
</body>
</html>
"""


def find_browser(explicit: str | None = None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if candidate.exists() else None
    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ):
        if candidate.exists():
            return candidate
    return None


def is_complete_png(path: Path, width: int, height: int) -> bool:
    """Return True only for a PNG whose IHDR matches the requested canvas.

    Chrome on macOS can finish writing a headless screenshot but keep its
    process alive past our timeout. Checking the PNG signature and dimensions
    lets that completed artifact count as success without accepting a stale or
    partial file.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return False
    return (
        len(header) == 24
        and header[:8] == b"\x89PNG\r\n\x1a\n"
        and header[12:16] == b"IHDR"
        and int.from_bytes(header[16:20], "big") == width
        and int.from_bytes(header[20:24], "big") == height
    )


def export_png(html_path: Path, png_path: Path, width: int, height: int, browser: Path) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    # Never mistake yesterday's same-date artifact for this export.
    # destructive-ok: deletes only this script's own prior export artifact (same path it immediately rewrites)
    png_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="daddy-dans-chrome-", dir="/private/tmp") as profile:
        command = [
            str(browser),
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--disable-crash-reporter",
            "--no-first-run",
            "--no-default-browser-check",
            "--run-all-compositor-stages-before-draw",
            "--disable-features=PaintHolding",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "--force-device-scale-factor=1",
            "--virtual-time-budget=5000",
            f"--screenshot={png_path}",
            html_path.resolve().as_uri(),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired:
            if is_complete_png(png_path, width, height):
                return
            raise
    if result.returncode != 0 or not is_complete_png(png_path, width, height):
        detail = (result.stderr or result.stdout or "browser returned no detail").strip()
        raise RuntimeError(f"PNG export failed: {detail}")


def default_card_path() -> Path:
    cards = sorted(OUT_DIR.glob("weekly-card-*.md"))
    if not cards:
        raise FileNotFoundError(f"no weekly-card markdown found in {OUT_DIR}")
    return cards[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("card", nargs="?", type=Path)
    parser.add_argument("--theme")
    parser.add_argument("--background", type=Path)
    parser.add_argument("--browser")
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args()

    config = load_config()
    theme_name = args.theme or config["default_theme"]
    theme = config.get("themes", {}).get(theme_name)
    if not theme:
        available = ", ".join(sorted(config.get("themes", {}))) or "none"
        print(f"unknown theme '{theme_name}'; available: {available}", file=sys.stderr)
        return 2

    card_path = (args.card or default_card_path()).resolve()
    if not card_path.exists():
        print(f"weekly card not found: {card_path}", file=sys.stderr)
        return 2
    try:
        card = parse_imessage(extract_imessage_block(card_path.read_text(encoding="utf-8")))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    background = (args.background or AGENT / theme["background"]).resolve()
    if not background.exists():
        print(f"background not found: {background}", file=sys.stderr)
        return 2

    stamp_match = re.search(r"(\d{4}-\d{2}-\d{2})", card_path.stem)
    stamp = stamp_match.group(1) if stamp_match else "latest"
    html_path = OUT_DIR / f"daddy-dans-picks-{stamp}.html"
    png_path = OUT_DIR / f"daddy-dans-picks-{stamp}.png"
    html_path.write_text(render_html(card, config, theme_name, background), encoding="utf-8")
    print(html_path.relative_to(ROOT))

    if args.html_only:
        return 0
    browser = find_browser(args.browser)
    if not browser:
        print("no Chromium-family browser found; HTML was written but PNG was not", file=sys.stderr)
        return 1
    try:
        export_png(html_path, png_path, int(config["width"]), int(config["height"]), browser)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(png_path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
