"""Minimal stand-in for the vault-wide helper module the agent was built against.

The original is a 3,600-line library shared by every agent in a personal
Obsidian vault. The betting agent uses five things from it, reproduced here so
the scripts run unchanged outside the vault.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_DIR = ROOT / "07-archive"
STATE_DIR = ROOT / "00-system" / "state"


def now_eastern() -> datetime.datetime:
    return datetime.datetime.now(tz=EASTERN)


def utc_to_eastern(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(EASTERN)


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def prepend_run_log(path: Path, entry: str, title: str, max_entries: int = 15) -> None:
    """Prepend a '## ...' entry under the H1; overflow goes to 07-archive/run-logs/."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {title}\n\n{entry}", encoding="utf-8")
        return
    current = path.read_text(encoding="utf-8")
    h1_end = current.find("\n")
    head = current[: h1_end + 1] if h1_end != -1 else f"# {title}\n"
    body = current[h1_end + 1:] if h1_end != -1 else current
    entries = [e for e in re.split(r"(?=^##\s)", body, flags=re.M) if e.strip()]
    entries.insert(0, entry if entry.endswith("\n") else entry + "\n")
    kept, overflow = entries[:max_entries], entries[max_entries:]
    path.write_text((head + "\n" + "".join(kept)).rstrip() + "\n", encoding="utf-8")
    if overflow:
        archive_path = ARCHIVE_DIR / "run-logs" / f"{path.parent.name}-{path.name}"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if archive_path.exists():
            text = archive_path.read_text(encoding="utf-8")
            cut = text.find("\n")
            archive_path.write_text(text[: cut + 1] + "\n" + "".join(overflow) + text[cut + 1:], encoding="utf-8")
        else:
            archive_path.write_text(f"# {title} (archive)\n\n" + "".join(overflow), encoding="utf-8")


class LoopGuardrail:
    """Runaway-retrigger cap for scheduled loops: at most `max_daily_iterations` runs per day.

    The vault version also tracks model spend. Nothing here calls a model, so the
    cost arguments are accepted and ignored.
    """

    def __init__(self, name: str, max_iterations: int = 3, max_cost_usd: float = 0.0,
                 max_daily_iterations: int = 40, max_daily_cost_usd: float = 0.0, **_):
        self.name = name
        self.max_iterations = max_iterations
        self.max_daily_iterations = max_daily_iterations
        self.state_path = STATE_DIR / f"loop-guard-{name}.json"
        self.iterations = 0

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def check_and_increment(self, n: int = 1, cost_usd: float = 0.0) -> bool:
        today = now_eastern().date().isoformat()
        state = self._load()
        if state.get("date") != today:
            state = {"date": today, "count": 0}
        if self.iterations + n > self.max_iterations or state["count"] + n > self.max_daily_iterations:
            return False
        self.iterations += n
        state["count"] += n
        atomic_write_text(self.state_path, json.dumps(state))
        return True

    def record(self, *_, **__) -> None:
        return None


def scheduled_run_guard(name: str, max_daily: int = 40) -> bool:
    guard = LoopGuardrail(name, max_iterations=3, max_cost_usd=0.0,
                          max_daily_iterations=max_daily, max_daily_cost_usd=0.0)
    return bool(guard.check_and_increment(1))
