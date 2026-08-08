import unittest
from unittest.mock import patch

import exchange


class OrderParameterTests(unittest.TestCase):
    """Binance's algo-order endpoint (client.futures_create_algo_order)
    requires the trigger price under the key `triggerPrice` - NOT
    `stopPrice`, which is only valid on the plain /fapi/v1/order endpoint.
    Sending the wrong key fails with APIError(-1102) at the exchange, which
    a test that only mocks exchange.place_stop_loss itself (as
    test_execution.py does) can never catch - these test the actual
    parameters built by exchange.py."""

    def test_place_stop_loss_sends_trigger_price_not_stop_price(self):
        with patch.object(exchange, "place_algo_order") as mock_place, \
             patch.object(exchange, "normalize_trigger_price", side_effect=lambda s, side, t, p: p):
            exchange.place_stop_loss("BTCUSDT", "BUY", 98.0)

        _, kwargs = mock_place.call_args
        self.assertEqual(kwargs["triggerPrice"], 98.0)
        self.assertNotIn("stopPrice", kwargs)
        self.assertEqual(kwargs["closePosition"], "true")

    def test_place_take_profit_partial_sends_trigger_price_not_stop_price(self):
        with patch.object(exchange, "place_algo_order") as mock_place, \
             patch.object(exchange, "normalize_trigger_price", side_effect=lambda s, side, t, p: p), \
             patch.object(exchange, "normalize_order_quantity", side_effect=lambda s, q, order_type=None: q):
            exchange.place_take_profit_partial("BTCUSDT", "BUY", 0.5, 102.0)

        _, kwargs = mock_place.call_args
        self.assertEqual(kwargs["triggerPrice"], 102.0)
        self.assertNotIn("stopPrice", kwargs)
        self.assertEqual(kwargs["reduceOnly"], "true")

    def test_place_take_profit_full_sends_trigger_price_not_stop_price(self):
        with patch.object(exchange, "place_algo_order") as mock_place, \
             patch.object(exchange, "normalize_trigger_price", side_effect=lambda s, side, t, p: p):
            exchange.place_take_profit_full("BTCUSDT", "BUY", 104.0)

        _, kwargs = mock_place.call_args
        self.assertEqual(kwargs["triggerPrice"], 104.0)
        self.assertNotIn("stopPrice", kwargs)
        self.assertEqual(kwargs["closePosition"], "true")


class ClosePositionMarketTests(unittest.TestCase):
    def test_closing_a_buy_position_sends_a_sell_reduce_only_order(self):
        with patch.object(exchange, "normalize_order_quantity", side_effect=lambda s, q, order_type=None: q), \
             patch.object(exchange.client, "futures_create_order") as mock_order:
            exchange.close_position_market("BTCUSDT", "BUY", 0.5)

        _, kwargs = mock_order.call_args
        self.assertEqual(kwargs["side"], "SELL")
        self.assertEqual(kwargs["type"], "MARKET")
        self.assertEqual(kwargs["quantity"], 0.5)
        self.assertEqual(kwargs["reduceOnly"], "true")

    def test_closing_a_sell_position_sends_a_buy_reduce_only_order(self):
        with patch.object(exchange, "normalize_order_quantity", side_effect=lambda s, q, order_type=None: q), \
             patch.object(exchange.client, "futures_create_order") as mock_order:
            exchange.close_position_market("BTCUSDT", "SELL", 0.5)

        _, kwargs = mock_order.call_args
        self.assertEqual(kwargs["side"], "BUY")

    def test_zero_normalized_quantity_raises(self):
        with patch.object(exchange, "normalize_order_quantity", return_value=0.0):
            with self.assertRaises(ValueError):
                exchange.close_position_market("BTCUSDT", "BUY", 0.5)


if __name__ == "__main__":
    unittest.main()
