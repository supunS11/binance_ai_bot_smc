import time
import unittest
from unittest.mock import patch

import config
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


def _exchange_info(symbol, lot_size_max, market_lot_size_max):
    return {
        "symbols": [{
            "symbol": symbol,
            "quantityPrecision": 3,
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": "1",
                    "minQty": "1",
                    "maxQty": str(lot_size_max),
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "stepSize": "1",
                    "minQty": "1",
                    "maxQty": str(market_lot_size_max),
                },
            ],
        }]
    }


class QuantityRuleMaxFilterTests(unittest.TestCase):
    """Real bug found live (XAIUSDT, 2026-08-08): a TAKE_PROFIT_MARKET
    TP1 quantity that passed LOT_SIZE's max alone still got rejected by
    Binance with -4005 "Quantity greater than max quantity" - conditional
    orders that execute as MARKET once triggered are apparently validated
    against the tighter of the two filters, not LOT_SIZE alone."""

    def test_conditional_order_type_uses_the_tighter_of_both_filters(self):
        with patch.object(exchange, "get_exchange_info", return_value=_exchange_info("XAIUSDT", 900000, 300000)):
            rules = exchange.get_symbol_quantity_rules("XAIUSDT", order_type="CONDITIONAL")

        self.assertEqual(rules["max_qty"], "300000.0")

    def test_market_order_type_still_uses_market_lot_size(self):
        with patch.object(exchange, "get_exchange_info", return_value=_exchange_info("XAIUSDT", 900000, 300000)):
            rules = exchange.get_symbol_quantity_rules("XAIUSDT", order_type="MARKET")

        self.assertEqual(rules["max_qty"], "300000")

    def test_conditional_falls_back_to_lot_size_when_market_lot_size_missing(self):
        info = {
            "symbols": [{
                "symbol": "XAIUSDT",
                "quantityPrecision": 3,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "900000"},
                ],
            }]
        }

        with patch.object(exchange, "get_exchange_info", return_value=info):
            rules = exchange.get_symbol_quantity_rules("XAIUSDT", order_type="CONDITIONAL")

        self.assertEqual(rules["max_qty"], "900000.0")


class GetIncomeHistoryTests(unittest.TestCase):
    def test_returns_the_records_list(self):
        records = [{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "1.5", "time": 1000}]

        with patch.object(exchange.client, "futures_income_history", return_value=records) as mock_call:
            result = exchange.get_income_history(start_time=500)

        self.assertEqual(result, records)
        _, kwargs = mock_call.call_args
        self.assertEqual(kwargs["startTime"], 500)
        self.assertNotIn("symbol", kwargs)
        self.assertNotIn("incomeType", kwargs)

    def test_passes_through_optional_filters(self):
        with patch.object(exchange.client, "futures_income_history", return_value=[]) as mock_call:
            exchange.get_income_history(
                symbol="BTCUSDT", income_type="REALIZED_PNL", start_time=1, end_time=2, limit=50
            )

        _, kwargs = mock_call.call_args
        self.assertEqual(kwargs["symbol"], "BTCUSDT")
        self.assertEqual(kwargs["incomeType"], "REALIZED_PNL")
        self.assertEqual(kwargs["startTime"], 1)
        self.assertEqual(kwargs["endTime"], 2)
        self.assertEqual(kwargs["limit"], 50)

    def test_non_list_response_returns_empty_list(self):
        with patch.object(exchange.client, "futures_income_history", return_value={"unexpected": "shape"}):
            result = exchange.get_income_history()

        self.assertEqual(result, [])

    def test_error_returns_empty_list_instead_of_raising(self):
        with patch.object(exchange.client, "futures_income_history", side_effect=RuntimeError("boom")):
            result = exchange.get_income_history()

        self.assertEqual(result, [])


class GetSymbolMaxOrderQuantityTests(unittest.TestCase):
    def test_returns_the_tighter_max_across_market_and_conditional(self):
        with patch.object(exchange, "get_exchange_info", return_value=_exchange_info("XAIUSDT", 900000, 300000)):
            result = exchange.get_symbol_max_order_quantity("XAIUSDT")

        self.assertEqual(result, 300000.0)

    def test_returns_zero_when_symbol_unknown(self):
        with patch.object(exchange, "get_exchange_info", return_value={"symbols": []}):
            result = exchange.get_symbol_max_order_quantity("DOESNOTEXIST")

        self.assertEqual(result, 0.0)


class OpenInterestTests(unittest.TestCase):
    def setUp(self):
        exchange._oi_unavailable_symbols.clear()

    def tearDown(self):
        exchange._oi_unavailable_symbols.clear()

    def test_returns_open_interest_as_float(self):
        with patch.object(exchange.client, "futures_open_interest", return_value={"symbol": "BTCUSDT", "openInterest": "12345.67"}):
            result = exchange.get_open_interest("BTCUSDT")

        self.assertEqual(result, 12345.67)

    def test_returns_none_on_error_instead_of_raising(self):
        with patch.object(exchange.client, "futures_open_interest", side_effect=RuntimeError("boom")):
            result = exchange.get_open_interest("BTCUSDT")

        self.assertIsNone(result)


class OpenInterestUnavailableSymbolTests(unittest.TestCase):
    """Real bug found live (2026-08-08, WATCHING=519): a large watchlist
    includes symbols the OI endpoint permanently rejects (delisted/
    settling/pre-trading, or simply invalid) - unhandled, these got
    retried and logged on every single poll cycle forever."""

    def setUp(self):
        exchange._oi_unavailable_symbols.clear()

    def tearDown(self):
        exchange._oi_unavailable_symbols.clear()

    def test_delisted_symbol_error_marks_it_unavailable_and_skips_future_calls(self):
        error = Exception("APIError(code=-4108): Symbol is on delivering or delivered or settling or closed or pre-trading.")

        with patch.object(exchange.client, "futures_open_interest", side_effect=error) as mock_call:
            first = exchange.get_open_interest("ALLOUSDT")
            second = exchange.get_open_interest("ALLOUSDT")

        self.assertIsNone(first)
        self.assertIsNone(second)
        # Second call never hit the network - short-circuited by the cooldown.
        self.assertEqual(mock_call.call_count, 1)

    def test_invalid_symbol_error_also_triggers_the_cooldown(self):
        error = Exception("APIError(code=-1121): Invalid symbol.")

        with patch.object(exchange.client, "futures_open_interest", side_effect=error) as mock_call:
            exchange.get_open_interest("IRYSUSDT")
            exchange.get_open_interest("IRYSUSDT")

        self.assertEqual(mock_call.call_count, 1)

    def test_unavailable_symbol_is_retried_again_after_the_cooldown_expires(self):
        error = Exception("APIError(code=-4108): Symbol is on delivering or delivered or settling or closed or pre-trading.")

        with patch.object(config, "OI_UNAVAILABLE_SYMBOL_COOLDOWN_SECONDS", 60), \
             patch.object(exchange.client, "futures_open_interest", side_effect=error) as mock_call:
            exchange.get_open_interest("ALLOUSDT")

            with patch.object(exchange.time, "time", return_value=time.time() + 61):
                exchange.get_open_interest("ALLOUSDT")

        self.assertEqual(mock_call.call_count, 2)

    def test_unrelated_error_does_not_trigger_the_cooldown(self):
        with patch.object(exchange.client, "futures_open_interest", side_effect=RuntimeError("boom")) as mock_call:
            exchange.get_open_interest("BTCUSDT")
            exchange.get_open_interest("BTCUSDT")

        self.assertEqual(mock_call.call_count, 2)

    def test_a_symbol_that_recovers_clears_its_unavailable_marker(self):
        error = Exception("APIError(code=-4108): Symbol is on delivering or delivered or settling or closed or pre-trading.")

        with patch.object(config, "OI_UNAVAILABLE_SYMBOL_COOLDOWN_SECONDS", 60), \
             patch.object(exchange.client, "futures_open_interest", side_effect=error):
            exchange.get_open_interest("ALLOUSDT")

        with patch.object(exchange.time, "time", return_value=time.time() + 61), \
             patch.object(exchange.client, "futures_open_interest", return_value={"symbol": "ALLOUSDT", "openInterest": "500"}):
            result = exchange.get_open_interest("ALLOUSDT")

        self.assertEqual(result, 500.0)
        self.assertNotIn("ALLOUSDT", exchange._oi_unavailable_symbols)


class CancelAllOpenOrdersTests(unittest.TestCase):
    """Binance treats regular orders and algo/conditional orders (which is
    what our SL/TP1/TP2 actually are) as two separate cancel-all
    endpoints - calling only one silently leaves the other kind in place."""

    def test_calls_both_the_regular_and_conditional_cancel_all_endpoints(self):
        with patch.object(exchange.client, "futures_cancel_all_open_orders") as mock_cancel:
            exchange.cancel_all_open_orders("BTCUSDT")

        self.assertEqual(mock_cancel.call_count, 2)
        calls = mock_cancel.call_args_list
        self.assertEqual(calls[0].kwargs, {"symbol": "BTCUSDT"})
        self.assertEqual(calls[1].kwargs, {"symbol": "BTCUSDT", "conditional": True})

    def test_a_failure_on_the_regular_endpoint_does_not_skip_the_conditional_one(self):
        with patch.object(
            exchange.client,
            "futures_cancel_all_open_orders",
            side_effect=[Exception("regular failed"), {"ok": True}],
        ) as mock_cancel:
            exchange.cancel_all_open_orders("BTCUSDT")

        self.assertEqual(mock_cancel.call_count, 2)

    def test_never_raises_even_if_both_calls_fail(self):
        with patch.object(
            exchange.client,
            "futures_cancel_all_open_orders",
            side_effect=Exception("boom"),
        ):
            exchange.cancel_all_open_orders("BTCUSDT")  # must not raise


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
