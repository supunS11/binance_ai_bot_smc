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
