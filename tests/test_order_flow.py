import unittest
from unittest.mock import patch

import config
from order_flow import CVDEngine


class CVDEngineTests(unittest.TestCase):
    def test_buyer_maker_trade_is_signed_negative(self):
        engine = CVDEngine()
        engine.record_trade("BTCUSDT", price=100, quantity=1, is_buyer_maker=True, timestamp=1000)

        with patch.object(config, "ORDER_FLOW_MIN_NOTIONAL_USDT", 0):
            snapshot = engine.snapshot("BTCUSDT", now=1001)

        self.assertTrue(snapshot["available"])
        self.assertLess(snapshot["ratio_1m"], 0)

    def test_aggressive_buy_trade_is_signed_positive(self):
        engine = CVDEngine()
        engine.record_trade("BTCUSDT", price=100, quantity=1, is_buyer_maker=False, timestamp=1000)

        with patch.object(config, "ORDER_FLOW_MIN_NOTIONAL_USDT", 0):
            snapshot = engine.snapshot("BTCUSDT", now=1001)

        self.assertGreater(snapshot["ratio_1m"], 0)

    def test_mixed_trades_produce_proportional_ratio(self):
        engine = CVDEngine()
        # 3x buy notional vs 1x sell notional -> ratio should be +0.5
        engine.record_trade("ETHUSDT", price=100, quantity=3, is_buyer_maker=False, timestamp=1000)
        engine.record_trade("ETHUSDT", price=100, quantity=1, is_buyer_maker=True, timestamp=1000)

        with patch.object(config, "ORDER_FLOW_MIN_NOTIONAL_USDT", 0):
            snapshot = engine.snapshot("ETHUSDT", now=1001)

        self.assertAlmostEqual(snapshot["ratio_1m"], 0.5)

    def test_snapshot_unavailable_with_no_trades(self):
        engine = CVDEngine()
        snapshot = engine.snapshot("DOESNOTEXIST")

        self.assertFalse(snapshot["available"])

    def test_thin_window_below_min_notional_is_excluded(self):
        engine = CVDEngine()
        engine.record_trade("BTCUSDT", price=1, quantity=1, is_buyer_maker=False, timestamp=1000)

        with patch.object(config, "ORDER_FLOW_MIN_NOTIONAL_USDT", 1_000_000):
            snapshot = engine.snapshot("BTCUSDT", now=1001)

        self.assertIsNone(snapshot["ratio_1m"])
        self.assertFalse(snapshot["available"])

    def test_old_trades_are_pruned_outside_the_max_window(self):
        engine = CVDEngine()

        with patch.object(config, "ORDER_FLOW_MAX_WINDOW_SECONDS", 60):
            engine.record_trade("BTCUSDT", price=100, quantity=1, is_buyer_maker=False, timestamp=1000)
            engine.record_trade("BTCUSDT", price=100, quantity=1, is_buyer_maker=False, timestamp=1200)

        with engine.lock:
            series = list(engine._trades["BTCUSDT"])

        self.assertEqual(len(series), 1)
        self.assertEqual(series[0][0], 1200)

    def test_zero_or_negative_price_or_quantity_is_ignored(self):
        engine = CVDEngine()
        engine.record_trade("BTCUSDT", price=0, quantity=1, is_buyer_maker=False)
        engine.record_trade("BTCUSDT", price=100, quantity=0, is_buyer_maker=False)

        snapshot = engine.snapshot("BTCUSDT")

        self.assertFalse(snapshot["available"])

    def test_reset_clears_a_single_symbol_only(self):
        engine = CVDEngine()
        engine.record_trade("BTCUSDT", price=100, quantity=1, is_buyer_maker=False, timestamp=1000)
        engine.record_trade("ETHUSDT", price=100, quantity=1, is_buyer_maker=False, timestamp=1000)

        engine.reset("BTCUSDT")

        with patch.object(config, "ORDER_FLOW_MIN_NOTIONAL_USDT", 0):
            self.assertFalse(engine.snapshot("BTCUSDT", now=1001)["available"])
            self.assertTrue(engine.snapshot("ETHUSDT", now=1001)["available"])


if __name__ == "__main__":
    unittest.main()
