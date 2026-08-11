import unittest
from unittest.mock import patch

import config
import market_structure as ms


def _candle(open_time, high, low, close=None, open_=None, closed=True):
    close = high if close is None else close
    open_ = low if open_ is None else open_
    return {
        "open_time": open_time,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
        "closed": closed,
    }


class FindSwingPointsTests(unittest.TestCase):
    def test_detects_a_single_high_and_low_fractal(self):
        # left=right=1: index i is a pivot if it's the extreme of [i-1,i+1]
        candles = [
            _candle(0, high=10, low=9),
            _candle(1, high=14, low=5),  # pivot HIGH and pivot LOW
            _candle(2, high=10, low=9),
        ]
        swings = ms.find_swing_points(candles, left=1, right=1)
        kinds = {(s.index, s.kind, s.price) for s in swings}

        self.assertIn((1, "HIGH", 14), kinds)
        self.assertIn((1, "LOW", 5), kinds)

    def test_edges_without_full_window_produce_no_swings(self):
        candles = [_candle(0, high=10, low=9), _candle(1, high=12, low=8)]
        swings = ms.find_swing_points(candles, left=1, right=1)
        self.assertEqual(swings, [])


class ZigzagTests(unittest.TestCase):
    def test_consecutive_same_kind_highs_collapse_to_the_higher_one(self):
        swings = [
            ms.SwingPoint(0, 0, 10, "HIGH"),
            ms.SwingPoint(1, 1, 15, "HIGH"),
        ]
        collapsed = ms._zigzag(swings)

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0].price, 15)

    def test_consecutive_same_kind_lows_collapse_to_the_lower_one(self):
        swings = [
            ms.SwingPoint(0, 0, 10, "LOW"),
            ms.SwingPoint(1, 1, 5, "LOW"),
        ]
        collapsed = ms._zigzag(swings)

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0].price, 5)


class ClassifySwingsTests(unittest.TestCase):
    """Hand-built alternating swing sequences, so BOS/CHoCH classification
    is tested directly against known trend shapes rather than depending on
    precisely engineering fractal candle data."""

    def _swing(self, index, price, kind):
        return ms.SwingPoint(index, index, price, kind)

    def test_higher_high_after_higher_low_is_bullish_bos(self):
        swings = [
            self._swing(1, 8, "LOW"),
            self._swing(3, 12, "HIGH"),   # first high -> BOS (trend was None)
            self._swing(5, 9, "LOW"),     # higher low, no trend flip
            self._swing(7, 14, "HIGH"),   # higher high -> continuation BOS
        ]
        result = ms._classify_swings(swings)

        self.assertTrue(result["available"])
        self.assertEqual(result["trend"], "BULLISH")
        self.assertEqual(result["last_event"]["type"], "BOS")
        self.assertEqual(result["last_swing_high"], 14)
        self.assertEqual(result["last_swing_low"], 9)

    def test_lower_low_after_bullish_trend_is_choch(self):
        swings = [
            self._swing(1, 8, "LOW"),
            self._swing(3, 12, "HIGH"),  # first high: recorded, no trend yet
            self._swing(5, 9, "LOW"),    # higher low, no flip
            self._swing(7, 14, "HIGH"),  # higher high -> BOS, trend BULLISH
            self._swing(9, 5, "LOW"),    # lower low -> CHoCH into BEARISH
        ]
        result = ms._classify_swings(swings)

        self.assertEqual(result["trend"], "BEARISH")
        self.assertEqual(result["last_event"]["type"], "CHoCH")
        self.assertEqual(result["last_event"]["direction"], "BEARISH")

    def test_lower_low_after_bearish_trend_is_continuation_bos(self):
        swings = [
            self._swing(1, 12, "HIGH"),
            self._swing(3, 8, "LOW"),   # first low -> BOS, trend BEARISH
            self._swing(5, 10, "HIGH"), # lower high, no flip
            self._swing(7, 6, "LOW"),   # lower low -> continuation BOS
        ]
        result = ms._classify_swings(swings)

        self.assertEqual(result["trend"], "BEARISH")
        self.assertEqual(result["last_event"]["type"], "BOS")

    def test_fewer_than_two_swings_is_unavailable(self):
        result = ms._classify_swings([self._swing(1, 10, "HIGH")])
        self.assertFalse(result["available"])


class LiveBreakCheckTests(unittest.TestCase):
    def test_close_above_last_swing_high_is_a_bullish_break(self):
        candles = [_candle(0, high=10, low=9, close=10.5, closed=False)]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        result = ms.live_break_check(candles, structure, require_closed_candle=False)

        self.assertTrue(result["broken"])
        self.assertEqual(result["direction"], "BULLISH")
        self.assertFalse(result["candle_closed"])

    def test_close_below_last_swing_low_is_a_bearish_break(self):
        candles = [_candle(0, high=10, low=4, close=4.5)]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        result = ms.live_break_check(candles, structure)

        self.assertTrue(result["broken"])
        self.assertEqual(result["direction"], "BEARISH")

    def test_close_inside_range_is_no_break(self):
        candles = [_candle(0, high=8, low=6, close=7)]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        result = ms.live_break_check(candles, structure)

        self.assertFalse(result["broken"])

    def test_unavailable_structure_is_never_broken(self):
        result = ms.live_break_check([_candle(0, 10, 9)], {"available": False})
        self.assertFalse(result["broken"])

    def test_broken_result_includes_the_evaluated_candles_open_time(self):
        candles = [_candle(123, high=10, low=9, close=10.5, closed=False)]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        result = ms.live_break_check(candles, structure, require_closed_candle=False)

        self.assertEqual(result["open_time"], 123)

    def test_require_closed_candle_ignores_a_forming_candles_break(self):
        # The forming candle (open_time=1) wicks above the swing high, but
        # require_closed_candle=True must only look at the last CLOSED
        # candle (open_time=0), which never broke it.
        candles = [
            _candle(0, high=9.5, low=8, close=9, closed=True),
            _candle(1, high=11, low=9, close=10.5, closed=False),
        ]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        result = ms.live_break_check(candles, structure, require_closed_candle=True)

        self.assertFalse(result["broken"])

    def test_require_closed_candle_fires_once_the_breaking_candle_closes(self):
        candles = [
            _candle(0, high=9.5, low=8, close=9, closed=True),
            _candle(1, high=11, low=9.5, close=10.5, closed=True),
        ]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        result = ms.live_break_check(candles, structure, require_closed_candle=True)

        self.assertTrue(result["broken"])
        self.assertEqual(result["direction"], "BULLISH")
        self.assertTrue(result["candle_closed"])
        self.assertEqual(result["open_time"], 1)

    def test_require_closed_candle_with_no_closed_candle_yet_is_not_broken(self):
        candles = [_candle(0, high=11, low=9, close=10.5, closed=False)]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        result = ms.live_break_check(candles, structure, require_closed_candle=True)

        self.assertFalse(result["broken"])

    def test_require_closed_candle_defaults_from_config(self):
        candles = [
            _candle(0, high=9.5, low=8, close=9, closed=True),
            _candle(1, high=11, low=9.5, close=10.5, closed=False),
        ]
        structure = {"available": True, "last_swing_high": 10, "last_swing_low": 5}

        with patch.object(config, "REQUIRE_CLOSE_CONFIRMED_BREAK", True):
            result = ms.live_break_check(candles, structure)

        self.assertFalse(result["broken"])


class FindOrderBlockTests(unittest.TestCase):
    def test_bullish_break_uses_last_red_candle(self):
        candles = [
            _candle(0, high=10, low=9, open_=9.5, close=9.2),   # red
            _candle(1, high=11, low=9.5, open_=9.6, close=10.9),  # green (impulse)
        ]
        block = ms.find_order_block(candles, index=1, direction="BULLISH")

        self.assertIsNotNone(block)
        self.assertEqual(block["index"], 0)

    def test_bearish_break_uses_last_green_candle(self):
        candles = [
            _candle(0, high=10, low=9, open_=9.2, close=9.8),   # green
            _candle(1, high=9.5, low=8, open_=9.4, close=8.2),  # red (impulse down)
        ]
        block = ms.find_order_block(candles, index=1, direction="BEARISH")

        self.assertIsNotNone(block)
        self.assertEqual(block["index"], 0)


class FairValueGapTests(unittest.TestCase):
    def test_detects_bullish_gap(self):
        candles = [
            _candle(0, high=10, low=9),
            _candle(1, high=10.5, low=10.2),
            _candle(2, high=12, low=11),  # low(11) > candle0 high(10) -> bullish FVG
        ]
        gaps = ms.find_fair_value_gaps(candles, lookback=10)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["type"], "BULLISH")

    def test_detects_bearish_gap(self):
        candles = [
            _candle(0, high=12, low=11),
            _candle(1, high=10.5, low=10.2),
            _candle(2, high=10, low=9),  # high(10) < candle0 low(11) -> bearish FVG
        ]
        gaps = ms.find_fair_value_gaps(candles, lookback=10)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["type"], "BEARISH")

    def test_no_gap_when_candles_overlap(self):
        candles = [
            _candle(0, high=10, low=9),
            _candle(1, high=10.5, low=9.5),
            _candle(2, high=10.2, low=9.8),
        ]
        gaps = ms.find_fair_value_gaps(candles, lookback=10)
        self.assertEqual(gaps, [])


class LiquidityPoolTests(unittest.TestCase):
    def test_two_equal_highs_form_a_buy_side_pool(self):
        swings = [
            ms.SwingPoint(0, 0, 100.0, "HIGH"),
            ms.SwingPoint(1, 1, 100.05, "HIGH"),
        ]
        pools = ms.find_liquidity_pools(swings, tolerance_pct=0.001)

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0]["type"], "BUY_SIDE")
        self.assertEqual(pools[0]["touches"], 2)

    def test_two_equal_lows_form_a_sell_side_pool(self):
        swings = [
            ms.SwingPoint(0, 0, 50.0, "LOW"),
            ms.SwingPoint(1, 1, 50.02, "LOW"),
        ]
        pools = ms.find_liquidity_pools(swings, tolerance_pct=0.001)

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0]["type"], "SELL_SIDE")

    def test_single_swing_never_forms_a_pool(self):
        swings = [ms.SwingPoint(0, 0, 100.0, "HIGH")]
        pools = ms.find_liquidity_pools(swings, tolerance_pct=0.001)
        self.assertEqual(pools, [])

    def test_swings_far_apart_do_not_cluster(self):
        swings = [
            ms.SwingPoint(0, 0, 100.0, "HIGH"),
            ms.SwingPoint(1, 1, 150.0, "HIGH"),
        ]
        pools = ms.find_liquidity_pools(swings, tolerance_pct=0.001)
        self.assertEqual(pools, [])


class PremiumDiscountZoneTests(unittest.TestCase):
    def _candles(self):
        return [_candle(i, high=110, low=90) for i in range(5)]

    def test_zone_bounds_and_midpoint(self):
        zone = ms.premium_discount_zone(self._candles(), lookback=10)

        self.assertTrue(zone["available"])
        self.assertEqual(zone["range_high"], 110)
        self.assertEqual(zone["range_low"], 90)
        self.assertEqual(zone["midpoint"], 100)

    def test_zone_for_price_above_and_below_midpoint(self):
        zone = ms.premium_discount_zone(self._candles(), lookback=10)

        self.assertEqual(ms.zone_for_price(zone, 105), "PREMIUM")
        self.assertEqual(ms.zone_for_price(zone, 95), "DISCOUNT")

    def test_empty_candles_are_unavailable(self):
        zone = ms.premium_discount_zone([], lookback=10)
        self.assertFalse(zone["available"])

    def test_in_ote_bullish_zone(self):
        zone = ms.premium_discount_zone(self._candles(), lookback=10)
        with patch.object(config, "OTE_RETRACEMENT_MIN", 0.6), patch.object(
            config, "OTE_RETRACEMENT_MAX", 0.8
        ):
            zone = ms.premium_discount_zone(self._candles(), lookback=10)
            # range 90-110, bullish OTE = high - range*[0.8, 0.6] = [94, 98]
            self.assertTrue(ms.in_ote(zone, 96, "BULLISH"))
            self.assertFalse(ms.in_ote(zone, 105, "BULLISH"))


class AverageTrueRangeTests(unittest.TestCase):
    def test_atr_is_positive_for_moving_candles(self):
        candles = [_candle(i, high=100 + i, low=95 + i, close=97 + i) for i in range(20)]
        atr = ms.average_true_range(candles, period=14)
        self.assertGreater(atr, 0)

    def test_atr_zero_with_too_few_candles(self):
        candles = [_candle(0, high=100, low=95, close=97)]
        atr = ms.average_true_range(candles, period=14)
        self.assertEqual(atr, 0.0)


class ExponentialMovingAverageTests(unittest.TestCase):
    def test_none_with_too_few_candles(self):
        candles = [_candle(i, high=101, low=99, close=100) for i in range(5)]
        self.assertIsNone(ms.exponential_moving_average(candles, period=20))

    def test_flat_price_series_converges_to_that_price(self):
        candles = [_candle(i, high=101, low=99, close=100) for i in range(60)]
        ema = ms.exponential_moving_average(candles, period=20)
        self.assertAlmostEqual(ema, 100, places=6)

    def test_rising_prices_pull_ema_up_but_below_the_latest_close(self):
        candles = [_candle(i, high=i + 1, low=i - 1, close=float(i)) for i in range(1, 61)]
        ema = ms.exponential_moving_average(candles, period=20)
        self.assertLess(ema, candles[-1]["close"])
        self.assertGreater(ema, candles[0]["close"])


class AnalyzeTests(unittest.TestCase):
    def test_unavailable_with_too_few_candles(self):
        result = ms.analyze([_candle(0, 10, 9)])
        self.assertFalse(result["available"])


if __name__ == "__main__":
    unittest.main()
