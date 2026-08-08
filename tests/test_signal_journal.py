import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import signal_journal


def _signal(**overrides):
    base = {
        "symbol": "BTCUSDT",
        "signal": "BUY",
        "htf_trend": "BULLISH",
        "structure_level": 98.0,
        "atr": 1.0,
        "premium_discount_zone": "DISCOUNT",
        "order_block": {"index": 1},
        "fvg": None,
        "cvd_score": 0.6,
        "depth_imbalance": 0.2,
        "sweep_confluence": True,
    }
    base.update(overrides)
    return base


def _plan(**overrides):
    base = {
        "entry_price": 100.0,
        "sl_price": 98.0,
        "tp1_price": 102.0,
        "tp2_price": 104.0,
        "quantity": 1.0,
        "risk_distance": 2.0,
    }
    base.update(overrides)
    return base


class SignalJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self.tmpdir.name) / "journal.csv"
        self.patcher = patch.object(signal_journal, "JOURNAL_PATH", self.journal_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmpdir.cleanup()

    def _read_rows(self):
        with open(self.journal_path, newline="") as handle:
            return list(csv.DictReader(handle))

    def test_append_signal_returns_a_trade_id(self):
        trade_id = signal_journal.append_signal(_signal(), _plan())
        self.assertTrue(trade_id.startswith("BTCUSDT_"))

    def test_append_signal_writes_diagnostic_fields(self):
        signal_journal.append_signal(_signal(), _plan())
        rows = self._read_rows()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "BTCUSDT")
        self.assertEqual(row["side"], "BUY")
        self.assertEqual(row["risk_distance_pct"], "2.0")
        self.assertEqual(row["order_block_present"], "True")
        self.assertEqual(row["fvg_present"], "False")
        self.assertEqual(row["sweep_confluence"], "True")
        self.assertEqual(row["outcome"], "")

    def test_append_outcome_carries_the_same_trade_id(self):
        trade_id = signal_journal.append_signal(_signal(), _plan())
        signal_journal.append_outcome("BTCUSDT", "SL_HIT", trade_id)

        rows = self._read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["trade_id"], trade_id)
        self.assertEqual(rows[1]["trade_id"], trade_id)
        self.assertEqual(rows[1]["outcome"], "SL_HIT")
        self.assertEqual(rows[0]["outcome"], "")

    def test_zero_entry_price_does_not_crash_risk_distance_calc(self):
        signal_journal.append_signal(_signal(), _plan(entry_price=0))
        rows = self._read_rows()
        self.assertEqual(rows[0]["risk_distance_pct"], "")


if __name__ == "__main__":
    unittest.main()
