import unittest
from unittest.mock import patch

import config
import exchange
import execution


def _plan():
    return {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "entry_price": 100,
        "sl_price": 98,
        "tp1_price": 102,
        "tp2_price": 104,
        "breakeven_price": 100.02,
        "quantity": 1.0,
        "tp1_quantity": 0.5,
        "tp2_quantity": 0.5,
    }


class EnterTradeShadowModeTests(unittest.TestCase):
    def test_shadow_mode_places_no_real_orders(self):
        with patch.object(config, "EXECUTION_MODE", "SHADOW"), \
             patch.object(exchange, "place_market_order") as market_order, \
             patch.object(exchange, "place_stop_loss") as stop_loss:
            result = execution.enter_trade(_plan())

        self.assertTrue(result["ok"])
        self.assertTrue(result["shadow"])
        self.assertIsNone(result["entry_order"])
        market_order.assert_not_called()
        stop_loss.assert_not_called()


class EnterTradeLiveModeTests(unittest.TestCase):
    def test_live_mode_places_entry_then_sl_tp1_tp2(self):
        with patch.object(config, "EXECUTION_MODE", "LIVE"), \
             patch.object(exchange, "setup_leverage", return_value=True), \
             patch.object(exchange, "place_market_order", return_value={"orderId": 1, "status": "FILLED"}) as market_order, \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": 2}) as stop_loss, \
             patch.object(exchange, "place_take_profit_partial", return_value={"algoId": 3}) as tp1, \
             patch.object(exchange, "place_take_profit_full", return_value={"algoId": 4}) as tp2:
            result = execution.enter_trade(_plan())

        self.assertTrue(result["ok"])
        self.assertFalse(result["shadow"])
        market_order.assert_called_once_with("BTCUSDT", "BUY", 1.0)
        stop_loss.assert_called_once_with("BTCUSDT", "BUY", 98)
        tp1.assert_called_once_with("BTCUSDT", "BUY", 0.5, 102)
        tp2.assert_called_once_with("BTCUSDT", "BUY", 104)

    def test_live_mode_entry_failure_returns_not_ok(self):
        with patch.object(config, "EXECUTION_MODE", "LIVE"), \
             patch.object(exchange, "setup_leverage", return_value=True), \
             patch.object(exchange, "place_market_order", side_effect=RuntimeError("boom")):
            result = execution.enter_trade(_plan())

        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])


if __name__ == "__main__":
    unittest.main()
