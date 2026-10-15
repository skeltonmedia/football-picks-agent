#!/usr/bin/env python3
"""test-backtest-signals.py — sign conventions of every backtest signal.

    python3 -B 04-agents/football-picks-agent/test-backtest-signals.py

WHY (2026-09-18, AQ-220). A sign error in any signal would make the backtest
"find" that a bad number predicts CLV. Every signal and the realized CLV are
checked here on a synthetic four-event log where the right answer is known.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

spec = importlib.util.spec_from_file_location("backtest_signals", AGENT / "backtest-signals.py")
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

HEADER = ["captured_at_et", "sport", "event_id", "commence_time_et", "away_team", "home_team",
          "book", "bookmaker_last_update", "market", "selection", "line_points", "odds_american",
          "event_link", "outcome_link"]

# Fixture calendar: Tuesday capture four days before a Saturday kickoff, an
# opener a week earlier (also a Tuesday, but 11 days out: outside the window),
# a Wednesday capture for the fallback case, and a game-morning close.
KICK = datetime(2026, 9, 12, 15, 30, tzinfo=ET)
OPEN = datetime(2026, 9, 1, 7, 30, tzinfo=ET)
TUE = datetime(2026, 9, 8, 7, 30, tzinfo=ET)
WED = datetime(2026, 9, 9, 7, 30, tzinfo=ET)
CLOSE = datetime(2026, 9, 12, 11, 0, tzinfo=ET)
NOW = datetime(2026, 9, 14, 9, 0, tzinfo=ET)

E1, E2, E3, E4 = "e1", "e2", "e3", "e4"
A, H = "Alpha Aardvarks", "Beta Bears"
X, Y = "Gamma Goats", "Delta Dogs"


def row(cap, eid, book, market, sel, pts, odds, sport="ncaaf", away=A, home=H):
    return [cap.isoformat(), sport, eid, KICK.isoformat(), away, home, book, "", market, sel,
            "" if pts is None else f"{pts:g}", f"{odds:g}", "", ""]


def fixture() -> list[list]:
    r = []
    # E1 spreads: DK +7 at the Tuesday capture (opener +7.5), others +6.5,
    # one book (betmgm) at +7.5, Pinnacle +6.5; closes +6.5 at DK.
    r += [row(OPEN, E1, "draftkings", "spreads", A, 7.5, -110), row(OPEN, E1, "draftkings", "spreads", H, -7.5, -110)]
    r += [row(TUE, E1, "draftkings", "spreads", A, 7.0, -110), row(TUE, E1, "draftkings", "spreads", H, -7.0, -110)]
    for b in ("fanduel", "espnbet", "pinnacle"):
        r += [row(TUE, E1, b, "spreads", A, 6.5, -110), row(TUE, E1, b, "spreads", H, -6.5, -110)]
    r += [row(TUE, E1, "betmgm", "spreads", A, 7.5, -110), row(TUE, E1, "betmgm", "spreads", H, -7.5, -110)]
    r += [row(CLOSE, E1, "draftkings", "spreads", A, 6.5, -110), row(CLOSE, E1, "draftkings", "spreads", H, -6.5, -110)]
    # E2 totals: Under 52.5 at DK, others 52; closes 51.5.
    r += [row(OPEN, E2, "draftkings", "totals", "Over", 52.5, -110), row(OPEN, E2, "draftkings", "totals", "Under", 52.5, -110)]
    r += [row(TUE, E2, "fanduel", "totals", "Over", 52.0, -110), row(TUE, E2, "fanduel", "totals", "Under", 52.0, -110)]
    r += [row(TUE, E2, "betrivers", "totals", "Over", 52.0, -110), row(TUE, E2, "betrivers", "totals", "Under", 52.0, -110)]
    r += [row(CLOSE, E2, "draftkings", "totals", "Over", 51.5, -110), row(CLOSE, E2, "draftkings", "totals", "Under", 51.5, -110)]
    # E3 totals, NO Tuesday capture (fallback on Wednesday): Over 46.5 at DK,
    # others 47; closes 48.5.
    r += [row(WED, E3, "draftkings", "totals", "Over", 46.5, -110), row(WED, E3, "draftkings", "totals", "Under", 46.5, -110)]
    r += [row(WED, E3, "fanduel", "totals", "Over", 47.0, -110), row(WED, E3, "fanduel", "totals", "Under", 47.0, -110)]
    r += [row(CLOSE, E3, "draftkings", "totals", "Over", 48.5, -110), row(CLOSE, E3, "draftkings", "totals", "Under", 48.5, -110)]
    # E4 h2h: DK X +150 / Y -170 (opener +140 / -160), others +130 / -150;
    # closes X +130 / Y -150.
    r += [row(OPEN, E4, "draftkings", "h2h", X, None, 140, away=X, home=Y), row(OPEN, E4, "draftkings", "h2h", Y, None, -160, away=X, home=Y)]
    r += [row(TUE, E4, "draftkings", "h2h", X, None, 150, away=X, home=Y), row(TUE, E4, "draftkings", "h2h", Y, None, -170, away=X, home=Y)]
    for b in ("fanduel", "espnbet"):
        r += [row(TUE, E4, b, "h2h", X, None, 130, away=X, home=Y), row(TUE, E4, b, "h2h", Y, None, -150, away=X, home=Y)]
    r += [row(CLOSE, E4, "draftkings", "h2h", X, None, 130, away=X, home=Y), row(CLOSE, E4, "draftkings", "h2h", Y, None, -150, away=X, home=Y)]
    # preseason noise must be ignored
    r += [row(TUE, "pre", "draftkings", "spreads", A, 3.0, -110, sport="nfl_preseason")]
    return r


class SignalSigns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "snapshots.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(fixture())
        cls.obs, cls.events, cls.diag = bs.build_observations(path, None, NOW)
        cls.by = {(o["event_id"], o["market"], o["selection"]): o for o in cls.obs}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_universe(self):
        self.assertEqual(len(self.obs), 8)          # 4 events x 2 sides, preseason dropped
        self.assertEqual(self.diag["decisions_tuesday"], 6)
        self.assertEqual(self.diag["decisions_fallback"], 2)
        self.assertTrue(self.by[(E3, "totals", "Over")]["decision_fallback"])
        self.assertFalse(self.by[(E1, "spreads", A)]["decision_fallback"])
        self.assertEqual(self.by[(E1, "spreads", A)]["dk_row_age_h"], 0.0)

    def test_spread_signals(self):
        o = self.by[(E1, "spreads", A)]
        # DK +7 vs consensus mean(6.5, 6.5, 6.5, 7.5) = 6.75: +0.25 pts, positive cents
        self.assertAlmostEqual(o["dk_vs_consensus_points"], 0.25)
        self.assertAlmostEqual(o["dk_vs_consensus_cents"], 0.25 * bs.point_value_pct("spreads", "cfb"))
        self.assertGreater(o["dk_vs_consensus_cents"], 0)
        # Pinnacle at +6.5: DK better by half a point
        self.assertAlmostEqual(o["dk_vs_pinnacle_points"], 0.5)
        self.assertGreater(o["dk_vs_pinnacle_cents"], 0)
        # best other book (betmgm +7.5) beats DK: negative
        self.assertAlmostEqual(o["dk_vs_best_cents"], -0.5 * bs.point_value_pct("spreads", "cfb"))
        # opener +7.5 -> +7: market moved toward Alpha, positive
        self.assertAlmostEqual(o["move_since_open_points"], 0.5)
        self.assertGreater(o["move_since_open_cents"], 0)
        # disagreement across all books: 7.5 - 6.5
        self.assertAlmostEqual(o["book_disagreement_points"], 1.0)
        # +7 closing +6.5: positive CLV
        self.assertAlmostEqual(o["clv_points"], 0.5)
        self.assertGreater(o["realized_clv_pct"], 0)
        self.assertTrue(o["beat_close"])
        self.assertEqual(o["close_source"], "snapshot")
        # the other side mirrors every sign
        h = self.by[(E1, "spreads", H)]
        self.assertLess(h["dk_vs_consensus_cents"], 0)
        self.assertLess(h["realized_clv_pct"], 0)
        self.assertLess(h["move_since_open_points"], 0)

    def test_under_signals(self):
        o = self.by[(E2, "totals", "Under")]
        self.assertAlmostEqual(o["dk_vs_consensus_points"], 0.5)   # 52.5 vs 52 is good for Under
        self.assertGreater(o["dk_vs_consensus_cents"], 0)
        self.assertAlmostEqual(o["clv_points"], 1.0)                # 52.5 -> 51.5
        self.assertGreater(o["realized_clv_pct"], 0)
        self.assertIsNone(o["dk_vs_pinnacle_cents"])                # no Pinnacle row
        self.assertAlmostEqual(o["move_since_open_points"], 0.0)
        ov = self.by[(E2, "totals", "Over")]
        self.assertLess(ov["dk_vs_consensus_cents"], 0)
        self.assertLess(ov["realized_clv_pct"], 0)

    def test_over_signals(self):
        o = self.by[(E3, "totals", "Over")]
        self.assertAlmostEqual(o["dk_vs_consensus_points"], 0.5)   # 46.5 vs 47 is good for Over
        self.assertGreater(o["dk_vs_consensus_cents"], 0)
        self.assertAlmostEqual(o["clv_points"], 2.0)                # 46.5 -> 48.5
        self.assertGreater(o["realized_clv_pct"], 0)
        self.assertLess(self.by[(E3, "totals", "Under")]["realized_clv_pct"], 0)

    def test_h2h_signals(self):
        o = self.by[(E4, "h2h", X)]
        nv_dk = bs.implied(150) / (bs.implied(150) + bs.implied(-170))
        nv_ot = bs.implied(130) / (bs.implied(130) + bs.implied(-150))
        self.assertAlmostEqual(o["dk_vs_consensus_cents"], (nv_ot - nv_dk) / nv_dk * 100)
        self.assertGreater(o["dk_vs_consensus_cents"], 0)           # +150 beats +130
        self.assertIsNone(o["dk_vs_consensus_points"])
        self.assertGreater(o["dk_vs_best_cents"], 0)                # DK +150 IS the best price here
        # opener +140, now +150: X's price got longer, so the market moved AWAY
        # from X. Opener in the "took" seat vs the decision row: negative.
        self.assertAlmostEqual(o["move_since_open_cents"],
                               (bs.implied(150) - bs.implied(140)) / bs.implied(140) * 100)
        self.assertLess(o["move_since_open_cents"], 0)
        self.assertGreater(y_move := self.by[(E4, "h2h", Y)]["move_since_open_cents"], 0)
        self.assertAlmostEqual(o["realized_clv_pct"],
                               (bs.implied(130) - bs.implied(150)) / bs.implied(150) * 100)
        self.assertGreater(o["realized_clv_pct"], 0)
        y = self.by[(E4, "h2h", Y)]
        self.assertLess(y["dk_vs_consensus_cents"], 0)
        self.assertLess(y["realized_clv_pct"], 0)
        self.assertAlmostEqual(o["book_disagreement_points"], (bs.implied(130) - bs.implied(150)) * 100)

    def test_stats_helpers(self):
        self.assertAlmostEqual(bs.pearson([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)
        self.assertAlmostEqual(bs.spearman([1, 2, 3, 4], [10, 20, 30, 1000]), 1.0)
        self.assertEqual(bs.ranks([10, 20, 10, 30]), [1.5, 3.0, 1.5, 4.0])
        q = bs.quintiles([(i, float(i)) for i in range(12)])
        self.assertEqual([b["n"] for b in q], [3, 3, 2, 2, 2])
        self.assertEqual(q[0]["lo"], 0)
        fl = bs.edge_floor([(i, -1.0 if i < 5 else 1.0) for i in range(10)], min_n=3)
        self.assertEqual(fl["t"], 1)          # >= 1 leaves 9 rows: 4 neg, 5 pos, mean > 0; >= 0 is mean 0
        self.assertIsNone(bs.edge_floor([(i, -1.0) for i in range(10)], min_n=3))


if __name__ == "__main__":
    unittest.main(verbosity=1)
