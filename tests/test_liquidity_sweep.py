import unittest

import liquidity_sweep as ls


def _candle(high, low, close):
    return {"open_time": 0, "open": close, "high": high, "low": low, "close": close, "volume": 1, "closed": False}


class DetectSweepTests(unittest.TestCase):
    def test_wick_above_buy_side_pool_then_reject_is_bearish(self):
        candles = [_candle(high=105, low=99, close=100)]
        pools = [{"type": "BUY_SIDE", "price": 103, "touches": 2}]

        sweep = ls.detect_sweep(candles, pools)

        self.assertIsNotNone(sweep)
        self.assertEqual(sweep["direction"], "BEARISH")
        self.assertAlmostEqual(sweep["wick_size"], 2)

    def test_wick_below_sell_side_pool_then_reject_is_bullish(self):
        candles = [_candle(high=101, low=95, close=100)]
        pools = [{"type": "SELL_SIDE", "price": 97, "touches": 2}]

        sweep = ls.detect_sweep(candles, pools)

        self.assertIsNotNone(sweep)
        self.assertEqual(sweep["direction"], "BULLISH")

    def test_wick_through_without_reject_is_not_a_sweep(self):
        # closes beyond the level -> real breakout, not a stop-hunt reject
        candles = [_candle(high=106, low=99, close=105)]
        pools = [{"type": "BUY_SIDE", "price": 103, "touches": 2}]

        sweep = ls.detect_sweep(candles, pools)
        self.assertIsNone(sweep)

    def test_no_pools_returns_none(self):
        candles = [_candle(high=105, low=99, close=100)]
        self.assertIsNone(ls.detect_sweep(candles, []))

    def test_no_candles_returns_none(self):
        pools = [{"type": "BUY_SIDE", "price": 103, "touches": 2}]
        self.assertIsNone(ls.detect_sweep([], pools))


if __name__ == "__main__":
    unittest.main()
