import unittest
from unittest.mock import patch

import config
from open_interest import OpenInterestEngine


class OpenInterestEngineTests(unittest.TestCase):
    def test_snapshot_unavailable_with_no_samples(self):
        engine = OpenInterestEngine()
        snapshot = engine.snapshot("DOESNOTEXIST")

        self.assertFalse(snapshot["available"])
        self.assertIsNone(snapshot["oi_change_pct"])

    def test_single_sample_has_no_change_pct_yet(self):
        engine = OpenInterestEngine()
        engine.record("BTCUSDT", 1000, timestamp=1000)

        snapshot = engine.snapshot("BTCUSDT", now=1001)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["oi_value"], 1000)
        self.assertIsNone(snapshot["oi_change_pct"])

    def test_rising_oi_produces_positive_change_pct(self):
        engine = OpenInterestEngine()

        with patch.object(config, "OI_LOOKBACK_SECONDS", 900):
            engine.record("BTCUSDT", 1000, timestamp=1000)
            engine.record("BTCUSDT", 1100, timestamp=1500)

            snapshot = engine.snapshot("BTCUSDT", now=1500)

        self.assertAlmostEqual(snapshot["oi_change_pct"], 10.0)

    def test_falling_oi_produces_negative_change_pct(self):
        engine = OpenInterestEngine()

        with patch.object(config, "OI_LOOKBACK_SECONDS", 900):
            engine.record("BTCUSDT", 1000, timestamp=1000)
            engine.record("BTCUSDT", 900, timestamp=1500)

            snapshot = engine.snapshot("BTCUSDT", now=1500)

        self.assertAlmostEqual(snapshot["oi_change_pct"], -10.0)

    def test_samples_older_than_lookback_are_not_used_as_baseline(self):
        engine = OpenInterestEngine()

        with patch.object(config, "OI_LOOKBACK_SECONDS", 100):
            engine.record("BTCUSDT", 500, timestamp=0)      # far outside lookback
            engine.record("BTCUSDT", 1000, timestamp=1000)  # baseline (in-window)
            engine.record("BTCUSDT", 1100, timestamp=1050)

            snapshot = engine.snapshot("BTCUSDT", now=1050)

        # Baseline should be 1000 (in-window), not 500 (stale) -> +10%, not +120%.
        self.assertAlmostEqual(snapshot["oi_change_pct"], 10.0)

    def test_ignores_none_and_negative_values(self):
        engine = OpenInterestEngine()
        engine.record("BTCUSDT", None, timestamp=1000)
        engine.record("BTCUSDT", -5, timestamp=1001)

        snapshot = engine.snapshot("BTCUSDT")
        self.assertFalse(snapshot["available"])

    def test_reset_clears_a_single_symbol_only(self):
        engine = OpenInterestEngine()
        engine.record("BTCUSDT", 1000, timestamp=1000)
        engine.record("ETHUSDT", 500, timestamp=1000)

        engine.reset("BTCUSDT")

        self.assertFalse(engine.snapshot("BTCUSDT")["available"])
        self.assertTrue(engine.snapshot("ETHUSDT")["available"])


if __name__ == "__main__":
    unittest.main()
