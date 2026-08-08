import unittest
from unittest.mock import patch

import config
import liquidity_sweep
import market_structure
import signal_engine


def _ltf_candles(close):
    return [{
        "open_time": 0, "open": close - 0.5, "high": close + 0.5,
        "low": close - 1.0, "close": close, "volume": 1.0, "closed": False,
    }]


HTF_BULLISH = {"available": True, "trend": "BULLISH"}
HTF_BEARISH = {"available": True, "trend": "BEARISH"}
ZONE = {
    "available": True,
    "midpoint": 100,
    "bullish_ote_zone": (90, 95),
    "bearish_ote_zone": (105, 110),
}
LTF_BULLISH_BREAK = {
    "available": True,
    "live_break": {"broken": True, "direction": "BULLISH", "level": 90},
    "fair_value_gaps": [{"type": "BULLISH", "top": 95, "bottom": 90, "index": 0}],
    "atr": 1.0,
}
LTF_BEARISH_BREAK = {
    "available": True,
    "live_break": {"broken": True, "direction": "BEARISH", "level": 110},
    "fair_value_gaps": [{"type": "BEARISH", "top": 110, "bottom": 105, "index": 0}],
    "atr": 1.0,
}


class SignalEngineTests(unittest.TestCase):
    def _run(
        self,
        ltf_close=93.0,
        cvd=None,
        depth=None,
        htf_structure=None,
        zone=None,
        ltf_analysis=None,
        order_block=None,
        sweep_direction="BULLISH",
        ema_value=85.0,
    ):
        cvd = {"available": True, "cvd_score": 0.5} if cvd is None else cvd
        depth = {"available": True, "depth_imbalance": 0.2} if depth is None else depth
        htf_structure = HTF_BULLISH if htf_structure is None else htf_structure
        zone = ZONE if zone is None else zone
        ltf_analysis = LTF_BULLISH_BREAK if ltf_analysis is None else ltf_analysis
        sweep = {"direction": sweep_direction} if sweep_direction else None

        with patch.object(market_structure, "structure_state", return_value=htf_structure), \
             patch.object(market_structure, "premium_discount_zone", return_value=zone), \
             patch.object(market_structure, "analyze", return_value=ltf_analysis), \
             patch.object(market_structure, "find_order_block", return_value=order_block), \
             patch.object(market_structure, "find_liquidity_pools", return_value=[]), \
             patch.object(market_structure, "find_swing_points", return_value=[]), \
             patch.object(market_structure, "exponential_moving_average", return_value=ema_value), \
             patch.object(liquidity_sweep, "detect_sweep", return_value=sweep):
            return signal_engine.evaluate(
                "BTCUSDT", ["htf_placeholder"], _ltf_candles(ltf_close), cvd, depth
            )

    def test_full_buy_signal_when_everything_aligns(self):
        result = self._run()

        self.assertEqual(result["signal"], "BUY")
        self.assertTrue(result["sweep_confluence"])
        self.assertEqual(result["premium_discount_zone"], "DISCOUNT")

    def test_full_sell_signal_when_everything_aligns(self):
        result = self._run(
            ltf_close=108.0,
            cvd={"available": True, "cvd_score": -0.5},
            depth={"available": True, "depth_imbalance": -0.2},
            htf_structure=HTF_BEARISH,
            ltf_analysis=LTF_BEARISH_BREAK,
            sweep_direction="BEARISH",
            ema_value=115.0,
        )

        self.assertEqual(result["signal"], "SELL")
        self.assertTrue(result["sweep_confluence"])
        self.assertEqual(result["premium_discount_zone"], "PREMIUM")

    def test_no_signal_when_htf_structure_unavailable(self):
        result = self._run(htf_structure={"available": False})
        self.assertIsNone(result["signal"])
        self.assertEqual(result["reason"], "HTF_STRUCTURE_UNAVAILABLE")

    def test_no_signal_when_zone_unavailable(self):
        result = self._run(zone={"available": False})
        self.assertEqual(result["reason"], "ZONE_UNAVAILABLE")

    def test_no_signal_when_ltf_structure_unavailable(self):
        result = self._run(ltf_analysis={"available": False})
        self.assertEqual(result["reason"], "LTF_STRUCTURE_UNAVAILABLE")

    def test_no_signal_when_no_live_break(self):
        analysis = dict(LTF_BULLISH_BREAK)
        analysis["live_break"] = {"broken": False}
        result = self._run(ltf_analysis=analysis)
        self.assertEqual(result["reason"], "NO_LIVE_STRUCTURE_BREAK")

    def test_no_signal_against_htf_bias(self):
        result = self._run(htf_structure=HTF_BEARISH)
        self.assertIn("AGAINST_HTF_BIAS", result["reason"])

    def test_no_signal_when_price_not_in_discount_for_buy(self):
        result = self._run(ltf_close=105.0)
        self.assertIn("NOT_IN_DISCOUNT", result["reason"])

    def test_no_signal_when_not_in_ote(self):
        # 99 is still < midpoint(100) -> discount, but outside (90, 95) OTE
        result = self._run(ltf_close=99.0)
        self.assertEqual(result["reason"], "NOT_IN_OTE")

    def test_no_signal_without_order_block_or_fvg_when_required(self):
        analysis = dict(LTF_BULLISH_BREAK)
        analysis["fair_value_gaps"] = []

        with patch.object(config, "REQUIRE_ORDER_BLOCK_OR_FVG", True):
            result = self._run(ltf_analysis=analysis, order_block=None)

        self.assertEqual(result["reason"], "NO_ORDER_BLOCK_OR_FVG")

    def test_signal_allowed_without_ob_or_fvg_when_not_required(self):
        analysis = dict(LTF_BULLISH_BREAK)
        analysis["fair_value_gaps"] = []

        with patch.object(config, "REQUIRE_ORDER_BLOCK_OR_FVG", False):
            result = self._run(ltf_analysis=analysis, order_block=None)

        self.assertEqual(result["signal"], "BUY")

    def test_ema_wrong_side_is_logged_but_does_not_block_a_buy(self):
        # Informational only: price below EMA on a BUY is recorded as
        # ema_aligned=False, but must NOT reject the signal - an EMA is a
        # lagging indicator, so gating on it would delay real-time entries
        # on sharp moves without evidence it's actually worth the cost.
        result = self._run(ema_value=95.0)  # ltf_close defaults to 93 < 95

        self.assertEqual(result["signal"], "BUY")
        self.assertFalse(result["ema_aligned"])
        self.assertEqual(result["ema_value"], 95.0)

    def test_ema_aligned_true_for_sell_when_price_is_below_ema(self):
        result = self._run(
            ltf_close=108.0,
            cvd={"available": True, "cvd_score": -0.5},
            depth={"available": True, "depth_imbalance": -0.2},
            htf_structure=HTF_BEARISH,
            ltf_analysis=LTF_BEARISH_BREAK,
            sweep_direction="BEARISH",
            ema_value=115.0,  # 108 < 115 -> aligned for a SELL
        )

        self.assertEqual(result["signal"], "SELL")
        self.assertTrue(result["ema_aligned"])

    def test_ema_unavailable_does_not_block_the_signal(self):
        result = self._run(ema_value=None)

        self.assertEqual(result["signal"], "BUY")
        self.assertIsNone(result["ema_aligned"])
        self.assertIsNone(result["ema_value"])

    def test_ema_fields_are_none_when_confirmation_disabled(self):
        with patch.object(config, "EMA_CONFIRMATION_ENABLED", False):
            result = self._run(ema_value=95.0)  # would be misaligned if computed

        self.assertEqual(result["signal"], "BUY")
        self.assertIsNone(result["ema_value"])
        self.assertIsNone(result["ema_aligned"])

    def test_no_signal_when_order_flow_data_unavailable(self):
        result = self._run(cvd={"available": False})
        self.assertEqual(result["reason"], "ORDER_FLOW_DATA_UNAVAILABLE")

    def test_no_signal_when_cvd_score_missing(self):
        result = self._run(cvd={"available": True, "cvd_score": None})
        self.assertEqual(result["reason"], "ORDER_FLOW_SCORE_UNAVAILABLE")

    def test_no_signal_when_cvd_not_confirmed_for_buy(self):
        with patch.object(config, "SIGNAL_MIN_CVD_SCORE", 0.15):
            result = self._run(cvd={"available": True, "cvd_score": 0.05})

        self.assertIn("CVD_NOT_CONFIRMED", result["reason"])

    def test_no_signal_when_depth_opposing_for_buy(self):
        with patch.object(config, "SIGNAL_MIN_DEPTH_IMBALANCE", 0.10):
            result = self._run(depth={"available": True, "depth_imbalance": -0.5})

        self.assertIn("DEPTH_OPPOSING", result["reason"])

    def test_signal_produced_when_depth_data_unavailable(self):
        result = self._run(depth={"available": False})
        self.assertEqual(result["signal"], "BUY")
        self.assertIsNone(result["depth_imbalance"])

    def test_no_signal_without_ltf_candles(self):
        result = signal_engine.evaluate("BTCUSDT", ["htf"], [], {}, {})
        self.assertEqual(result["reason"], "INSUFFICIENT_CANDLES")


if __name__ == "__main__":
    unittest.main()
