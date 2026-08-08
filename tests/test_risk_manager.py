import unittest
from unittest.mock import patch

import config
import risk_manager


class ComputeStopLossTests(unittest.TestCase):
    def test_buy_stop_is_below_structure_level_minus_atr_buffer(self):
        signal = {"structure_level": 100, "atr": 2}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0.5):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertEqual(sl, 99.0)  # 100 - (2 * 0.5)

    def test_sell_stop_is_above_structure_level_plus_atr_buffer(self):
        signal = {"structure_level": 100, "atr": 2}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0.5):
            sl = risk_manager.compute_stop_loss(signal, "SELL")

        self.assertEqual(sl, 101.0)

    def test_missing_structure_level_returns_none(self):
        sl = risk_manager.compute_stop_loss({"structure_level": None, "atr": 2}, "BUY")
        self.assertIsNone(sl)


class MinStopDistanceFloorTests(unittest.TestCase):
    """A structure level landing pathologically close to entry (fast/noisy
    market, tight fractal window) must not be allowed through as-is - it
    gets hit by ordinary noise, and risk-based sizing would compensate
    with an oversized position to match the tiny distance."""

    def test_pathologically_tight_buy_stop_gets_widened_to_the_floor(self):
        # Structure level just 0.02% below entry - realistic tiny-stop case.
        signal = {"structure_level": 99.98, "atr": 0.001, "entry_price": 100.0}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0.15), \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.3):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertAlmostEqual(sl, 99.7)  # 100 - 0.3%

    def test_pathologically_tight_sell_stop_gets_widened_to_the_floor(self):
        signal = {"structure_level": 100.02, "atr": 0.001, "entry_price": 100.0}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0.15), \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.3):
            sl = risk_manager.compute_stop_loss(signal, "SELL")

        self.assertAlmostEqual(sl, 100.3)  # 100 + 0.3%

    def test_a_stop_already_wider_than_the_floor_is_left_untouched(self):
        signal = {"structure_level": 95.0, "atr": 0, "entry_price": 100.0}

        with patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.3):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertEqual(sl, 95.0)

    def test_zero_min_pct_disables_the_floor(self):
        signal = {"structure_level": 99.999, "atr": 0, "entry_price": 100.0}

        with patch.object(config, "MIN_STOP_DISTANCE_PCT", 0):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertEqual(sl, 99.999)

    def test_missing_entry_price_skips_the_floor_without_crashing(self):
        signal = {"structure_level": 99.999, "atr": 0}

        with patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.3):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertEqual(sl, 99.999)


class ComputeTargetsTests(unittest.TestCase):
    def test_buy_targets_are_r_multiples_above_entry(self):
        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 2.0):
            tp1, tp2 = risk_manager.compute_targets(entry_price=100, sl_price=98, side="BUY")

        self.assertEqual(tp1, 102)  # 100 + 2*1
        self.assertEqual(tp2, 104)  # 100 + 2*2

    def test_sell_targets_are_r_multiples_below_entry(self):
        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 2.0):
            tp1, tp2 = risk_manager.compute_targets(entry_price=100, sl_price=102, side="SELL")

        self.assertEqual(tp1, 98)
        self.assertEqual(tp2, 96)

    def test_zero_risk_distance_returns_none_none(self):
        tp1, tp2 = risk_manager.compute_targets(entry_price=100, sl_price=100, side="BUY")
        self.assertIsNone(tp1)
        self.assertIsNone(tp2)


class StructureBasedTargetTests(unittest.TestCase):
    """TP1/TP2 should target a real liquidity pool when one exists with
    enough room, matching v7's structure-based TP - the R-multiple is
    only the minimum-room floor / fallback, not the primary target."""

    def test_buy_targets_the_nearest_qualifying_buy_side_pool(self):
        pools = [
            {"type": "BUY_SIDE", "price": 103},  # 1.5R - clears the 1R floor
            {"type": "BUY_SIDE", "price": 110},  # farther, should not be picked for TP1
        ]

        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 4.0):
            tp1, tp2 = risk_manager.compute_targets(100, 98, "BUY", pools=pools)

        self.assertEqual(tp1, 103)  # nearest pool clearing 1R (>=102)
        self.assertEqual(tp2, 110)  # next pool out, clears the widened TP2 floor

    def test_sell_targets_the_nearest_qualifying_sell_side_pool(self):
        pools = [
            {"type": "SELL_SIDE", "price": 97},
            {"type": "SELL_SIDE", "price": 90},
        ]

        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 4.0):
            tp1, tp2 = risk_manager.compute_targets(100, 102, "SELL", pools=pools)

        self.assertEqual(tp1, 97)
        self.assertEqual(tp2, 90)

    def test_pool_too_close_to_clear_the_floor_is_ignored(self):
        # Only 0.5R away - doesn't clear a 1R minimum, must fall back.
        pools = [{"type": "BUY_SIDE", "price": 101}]

        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 2.0):
            tp1, _ = risk_manager.compute_targets(100, 98, "BUY", pools=pools)

        self.assertEqual(tp1, 102)  # fallback: 100 + 1R

    def test_wrong_side_pool_type_is_ignored(self):
        # A SELL_SIDE pool must never be used as a BUY's target.
        pools = [{"type": "SELL_SIDE", "price": 105}]

        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 2.0):
            tp1, _ = risk_manager.compute_targets(100, 98, "BUY", pools=pools)

        self.assertEqual(tp1, 102)  # fallback, the pool was never eligible

    def test_tp2_still_clears_tp1_when_tp1_used_a_far_structure_pool(self):
        # TP1 lands on a pool at 4R (well beyond its 1R floor); TP2's
        # floor must adapt to sit beyond that, not just the configured 2R.
        pools = [{"type": "BUY_SIDE", "price": 108}]  # 4R away

        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 2.0):
            tp1, tp2 = risk_manager.compute_targets(100, 98, "BUY", pools=pools)

        self.assertEqual(tp1, 108)
        self.assertGreater(tp2, tp1)  # fallback must clear tp1, not just 2R

    def test_no_pools_falls_back_to_pure_r_multiples(self):
        with patch.object(config, "TP1_R_MULTIPLE", 1.0), patch.object(config, "TP2_R_MULTIPLE", 2.0):
            tp1, tp2 = risk_manager.compute_targets(100, 98, "BUY", pools=None)

        self.assertEqual(tp1, 102)
        self.assertEqual(tp2, 104)


class ComputeBreakevenPriceTests(unittest.TestCase):
    def test_buy_breakeven_is_slightly_above_entry(self):
        with patch.object(config, "BREAKEVEN_BUFFER_PCT", 0.02):
            price = risk_manager.compute_breakeven_price(100, "BUY")

        self.assertAlmostEqual(price, 100.02)

    def test_sell_breakeven_is_slightly_below_entry(self):
        with patch.object(config, "BREAKEVEN_BUFFER_PCT", 0.02):
            price = risk_manager.compute_breakeven_price(100, "SELL")

        self.assertAlmostEqual(price, 99.98)


class BuildTradePlanTests(unittest.TestCase):
    def _signal(self, side="BUY", entry_price=100, structure_level=98, atr=1):
        return {
            "signal": side,
            "symbol": "BTCUSDT",
            "entry_price": entry_price,
            "structure_level": structure_level,
            "atr": atr,
        }

    def test_happy_path_produces_a_full_plan(self):
        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "TP1_R_MULTIPLE", 1.0), \
             patch.object(config, "TP2_R_MULTIPLE", 2.0), \
             patch.object(config, "TP1_CLOSE_PCT", 50), \
             patch.object(risk_manager, "calculate_position_size", return_value=10.0):
            plan, status = risk_manager.build_trade_plan(self._signal(), balance=1000)

        self.assertEqual(status, "OK")
        self.assertEqual(plan["sl_price"], 98)
        self.assertEqual(plan["tp1_price"], 102)
        self.assertEqual(plan["tp2_price"], 104)
        self.assertEqual(plan["quantity"], 10.0)
        self.assertEqual(plan["tp1_quantity"], 5.0)
        self.assertEqual(plan["tp2_quantity"], 5.0)

    def test_sl_unavailable_when_no_structure_level(self):
        plan, status = risk_manager.build_trade_plan(
            self._signal(structure_level=None), balance=1000
        )
        self.assertIsNone(plan)
        self.assertEqual(status, "SL_UNAVAILABLE")

    def test_sl_on_wrong_side_rejected_for_buy(self):
        # SL above entry for a BUY is nonsensical
        plan, status = risk_manager.build_trade_plan(
            self._signal(entry_price=100, structure_level=105, atr=0), balance=1000
        )
        self.assertIsNone(plan)
        self.assertEqual(status, "SL_ON_WRONG_SIDE")

    def test_zero_position_size_is_rejected(self):
        with patch.object(risk_manager, "calculate_position_size", return_value=0):
            plan, status = risk_manager.build_trade_plan(self._signal(), balance=1000)

        self.assertIsNone(plan)
        self.assertEqual(status, "POSITION_SIZE_ZERO")

    def test_tp1_close_pct_of_100_makes_the_split_invalid(self):
        with patch.object(config, "TP1_CLOSE_PCT", 100), \
             patch.object(risk_manager, "calculate_position_size", return_value=10.0):
            plan, status = risk_manager.build_trade_plan(self._signal(), balance=1000)

        self.assertIsNone(plan)
        self.assertEqual(status, "TP_SPLIT_INVALID")

    def test_invalid_entry_price_is_rejected(self):
        plan, status = risk_manager.build_trade_plan(
            self._signal(entry_price=0), balance=1000
        )
        self.assertIsNone(plan)
        self.assertEqual(status, "INVALID_ENTRY_PRICE")


if __name__ == "__main__":
    unittest.main()
