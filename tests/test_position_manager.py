import unittest
from unittest.mock import patch

import config
import exchange
from position_manager import BREAKEVEN_ACTIVE, TP1_PENDING, PositionManager


# _close() journals every outcome for real (signal_journal.append_outcome)
# so it can associate a trade_id with its result - none of these tests
# care about that side effect, so it's patched out for the whole module
# rather than in every single test that reaches a close.
_journal_patcher = None


def setUpModule():
    global _journal_patcher
    _journal_patcher = patch("position_manager.signal_journal.append_outcome")
    _journal_patcher.start()


def tearDownModule():
    _journal_patcher.stop()


def _plan(side="BUY"):
    if side == "SELL":
        return {
            "symbol": "BTCUSDT",
            "side": side,
            "entry_price": 100,
            "sl_price": 102,
            "tp1_price": 98,
            "tp2_price": 96,
            "breakeven_price": 99.98,
            "quantity": 1.0,
            "tp1_quantity": 0.5,
            "tp2_quantity": 0.5,
        }

    return {
        "symbol": "BTCUSDT",
        "side": side,
        "entry_price": 100,
        "sl_price": 98,
        "tp1_price": 102,
        "tp2_price": 104,
        "breakeven_price": 100.02,
        "quantity": 1.0,
        "tp1_quantity": 0.5,
        "tp2_quantity": 0.5,
    }


def _candle(high, low):
    return {"open_time": 0, "open": high, "high": high, "low": low, "close": high, "volume": 1, "closed": False}


class ReconcileOnStartupTests(unittest.TestCase):
    def _live_position(self, symbol="BTCUSDT", side="BUY", entry=100.0, qty=1.0):
        return {"symbol": symbol, "side": side, "entry_price": entry, "quantity": qty}

    def test_no_open_positions_does_nothing(self):
        manager = PositionManager()

        with patch.object(exchange, "get_all_open_positions", return_value=[]):
            manager.reconcile_on_startup()

        self.assertEqual(manager.open_count(), 0)

    def test_already_tracked_symbol_is_not_re_adopted(self):
        manager = PositionManager()
        manager.register(_plan(), {"shadow": True})

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position()]), \
             patch.object(exchange, "get_open_algo_orders") as get_orders:
            manager.reconcile_on_startup()

        get_orders.assert_not_called()

    def test_full_order_set_found_adopts_with_real_prices_and_ids(self):
        manager = PositionManager()
        open_orders = [
            {"type": "STOP_MARKET", "triggerPrice": "98", "algoId": "sl1"},
            {"type": "TAKE_PROFIT_MARKET", "closePosition": "false", "triggerPrice": "102", "origQty": "0.8", "algoId": "tp1_1"},
            {"type": "TAKE_PROFIT_MARKET", "closePosition": "true", "triggerPrice": "104", "algoId": "tp2_1"},
        ]

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position()]), \
             patch.object(exchange, "get_open_algo_orders", return_value=open_orders):
            manager.reconcile_on_startup()

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["sl_price"], 98.0)
        self.assertEqual(position["tp1_price"], 102.0)
        self.assertEqual(position["tp2_price"], 104.0)
        self.assertEqual(position["sl_order_id"], "sl1")
        self.assertEqual(position["tp1_order_id"], "tp1_1")
        self.assertEqual(position["tp2_order_id"], "tp2_1")
        self.assertEqual(position["stage"], TP1_PENDING)
        self.assertTrue(position["trade_id"].startswith("BTCUSDT_RECOVERED_"))

    def test_only_sl_and_tp2_found_means_tp1_already_resolved(self):
        manager = PositionManager()
        open_orders = [
            {"type": "STOP_MARKET", "triggerPrice": "100.02", "algoId": "sl2"},
            {"type": "TAKE_PROFIT_MARKET", "closePosition": "true", "triggerPrice": "104", "algoId": "tp2_1"},
        ]

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position()]), \
             patch.object(exchange, "get_open_algo_orders", return_value=open_orders):
            manager.reconcile_on_startup()

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], BREAKEVEN_ACTIVE)
        self.assertEqual(position["tp1_order_id"], "")

    def test_no_stop_loss_found_places_an_emergency_stop(self):
        manager = PositionManager()

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position()]), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": "emergency_sl"}) as place_sl, \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.3):
            manager.reconcile_on_startup()

        position = manager.positions["BTCUSDT"]
        place_sl.assert_called_once()
        self.assertEqual(position["sl_order_id"], "emergency_sl")
        # Emergency stop is at least the configured minimum distance away.
        self.assertLessEqual(position["sl_price"], 100.0 * (1 - 0.003))

    def test_no_orders_at_all_still_produces_a_trackable_position(self):
        manager = PositionManager()

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position()]), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": "emergency_sl"}):
            manager.reconcile_on_startup()

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], TP1_PENDING)
        self.assertEqual(position["tp1_order_id"], "")
        self.assertEqual(position["tp2_order_id"], "")
        self.assertIsNotNone(position["tp1_price"])
        self.assertIsNotNone(position["tp2_price"])

    def test_emergency_stop_placement_failure_does_not_raise(self):
        manager = PositionManager()

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position()]), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(exchange, "place_stop_loss", side_effect=RuntimeError("rejected")):
            manager.reconcile_on_startup()  # must not raise

        self.assertTrue(manager.has_open_position("BTCUSDT"))
        self.assertEqual(manager.positions["BTCUSDT"]["sl_order_id"], "")


class RegisterTests(unittest.TestCase):
    def test_shadow_registration_has_no_order_ids(self):
        manager = PositionManager()
        position = manager.register(_plan(), {"shadow": True})

        self.assertTrue(manager.has_open_position("BTCUSDT"))
        self.assertEqual(manager.open_count(), 1)
        self.assertTrue(position["shadow"])
        self.assertIsNone(position["sl_order_id"])
        self.assertEqual(position["stage"], TP1_PENDING)

    def test_trade_id_is_stored_and_threaded_through_to_the_outcome_journal(self):
        manager = PositionManager()
        manager.register(_plan(), {"shadow": True}, trade_id="BTCUSDT_123456")
        self.assertEqual(manager.positions["BTCUSDT"]["trade_id"], "BTCUSDT_123456")

        with patch("position_manager.signal_journal.append_outcome") as append_outcome:
            outcome = manager._close("BTCUSDT", "SHADOW_SL_HIT")

        self.assertEqual(outcome, "SHADOW_SL_HIT")
        append_outcome.assert_called_once_with("BTCUSDT", "SHADOW_SL_HIT", "BTCUSDT_123456")

    def test_live_registration_extracts_order_ids(self):
        manager = PositionManager()
        execution_result = {
            "shadow": False,
            "sl_order": {"algoId": "sl1"},
            "tp1_order": {"algoId": "tp1_1"},
            "tp2_order": {"algoId": "tp2_1"},
        }
        position = manager.register(_plan(), execution_result)

        self.assertEqual(position["sl_order_id"], "sl1")
        self.assertEqual(position["tp1_order_id"], "tp1_1")
        self.assertEqual(position["tp2_order_id"], "tp2_1")


class PollShadowTests(unittest.TestCase):
    def _manager_with_position(self, side="BUY"):
        manager = PositionManager()
        manager.register(_plan(side), {"shadow": True})
        return manager

    def test_tp1_pending_sl_hit_closes_as_sl(self):
        manager = self._manager_with_position()
        outcome = manager.poll_shadow("BTCUSDT", _candle(high=99, low=97))  # low <= sl(98)

        self.assertEqual(outcome, "SHADOW_SL_HIT")
        self.assertFalse(manager.has_open_position("BTCUSDT"))

    def test_tp1_pending_tp1_hit_moves_to_breakeven_and_stays_open(self):
        manager = self._manager_with_position()
        outcome = manager.poll_shadow("BTCUSDT", _candle(high=103, low=99))  # high >= tp1(102)

        self.assertIsNone(outcome)
        self.assertTrue(manager.has_open_position("BTCUSDT"))
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], BREAKEVEN_ACTIVE)
        self.assertEqual(position["sl_price"], position["breakeven_price"])

    def test_ambiguous_candle_hitting_both_is_conservatively_sl(self):
        manager = self._manager_with_position()
        # low(97) <= sl(98) AND high(103) >= tp1(102) in the same candle
        outcome = manager.poll_shadow("BTCUSDT", _candle(high=103, low=97))

        self.assertEqual(outcome, "SHADOW_SL_HIT")

    def test_breakeven_stage_tp2_hit_closes_as_tp2(self):
        manager = self._manager_with_position()
        manager.poll_shadow("BTCUSDT", _candle(high=103, low=99))  # promote to breakeven
        outcome = manager.poll_shadow("BTCUSDT", _candle(high=105, low=101))  # tp2(104) hit

        self.assertEqual(outcome, "SHADOW_TP2_HIT")

    def test_breakeven_stage_stop_hit_closes_as_breakeven_stop(self):
        manager = self._manager_with_position()
        manager.poll_shadow("BTCUSDT", _candle(high=103, low=99))  # promote to breakeven
        outcome = manager.poll_shadow(
            "BTCUSDT", _candle(high=100.5, low=100.0)
        )  # low <= breakeven(100.02)

        self.assertEqual(outcome, "SHADOW_BREAKEVEN_STOP_HIT")

    def test_no_candle_returns_none(self):
        manager = self._manager_with_position()
        self.assertIsNone(manager.poll_shadow("BTCUSDT", None))

    def test_unknown_symbol_returns_none(self):
        manager = PositionManager()
        self.assertIsNone(manager.poll_shadow("NOPE", _candle(100, 99)))

    def test_sell_side_uses_inverted_high_low_logic(self):
        manager = self._manager_with_position(side="SELL")
        # SELL: sl=102 (above entry). A candle that stays below the stop
        # must not close the position.
        outcome = manager.poll_shadow("BTCUSDT", _candle(high=101, low=99))
        self.assertIsNone(outcome)

        # Now push the high through the SELL stop (102) -> should close.
        outcome = manager.poll_shadow("BTCUSDT", _candle(high=103, low=99))
        self.assertEqual(outcome, "SHADOW_SL_HIT")


class PollLiveTests(unittest.TestCase):
    def _manager_with_position(self):
        manager = PositionManager()
        execution_result = {
            "shadow": False,
            "sl_order": {"algoId": "sl1"},
            "tp1_order": {"algoId": "tp1_1"},
            "tp2_order": {"algoId": "tp2_1"},
        }
        manager.register(_plan(), execution_result)
        return manager

    def test_tp1_finished_promotes_to_breakeven(self):
        manager = self._manager_with_position()

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp1_1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "_fetch_open_position_detail", return_value={"quantity": 0.5}), \
             patch.object(exchange, "cancel_algo_order") as cancel, \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": "sl2"}) as new_sl:
            outcome = manager.poll_live("BTCUSDT")

        self.assertIsNone(outcome)
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], BREAKEVEN_ACTIVE)
        self.assertEqual(position["sl_order_id"], "sl2")
        cancel.assert_called_once_with("BTCUSDT", "sl1")
        new_sl.assert_called_once_with("BTCUSDT", "BUY", position["breakeven_price"])

    def test_tp1_finished_but_position_already_closed_gives_up_cleanly(self):
        # TP1 filling can coincide with the original SL also having fired
        # (or manual intervention) - the position is genuinely gone, so
        # this must close out tracking instead of retrying forever.
        manager = self._manager_with_position()

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp1_1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "_fetch_open_position_detail", return_value=None), \
             patch.object(exchange, "cancel_all_open_orders") as cancel_all, \
             patch.object(exchange, "place_stop_loss") as new_sl:
            outcome = manager.poll_live("BTCUSDT")

        self.assertEqual(outcome, "TP1_THEN_POSITION_ALREADY_CLOSED")
        self.assertFalse(manager.has_open_position("BTCUSDT"))
        new_sl.assert_not_called()
        cancel_all.assert_called_once_with("BTCUSDT")

    def test_tp1_finished_but_position_check_fails_retries_next_poll(self):
        # A transient network/backoff error while checking ground truth
        # must NOT be treated as "position closed" - that would abandon a
        # still-open, still-unprotected position.
        manager = self._manager_with_position()

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp1_1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "_fetch_open_position_detail", side_effect=RuntimeError("timeout")), \
             patch.object(exchange, "cancel_algo_order") as cancel, \
             patch.object(exchange, "place_stop_loss") as new_sl:
            outcome = manager.poll_live("BTCUSDT")

        self.assertIsNone(outcome)
        self.assertTrue(manager.has_open_position("BTCUSDT"))
        self.assertEqual(manager.positions["BTCUSDT"]["stage"], TP1_PENDING)
        cancel.assert_not_called()
        new_sl.assert_not_called()

    def test_breakeven_placement_immediately_triggers_closes_at_market(self):
        # Binance rejects a stop that would fire the instant it's placed -
        # that means price already passed the breakeven level, so the
        # remainder must be closed at market instead of left unprotected.
        manager = self._manager_with_position()

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp1_1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(
                 exchange,
                 "_fetch_open_position_detail",
                 return_value={"quantity": 0.5},
             ), \
             patch.object(exchange, "cancel_algo_order") as cancel, \
             patch.object(exchange, "cancel_all_open_orders") as cancel_all, \
             patch.object(
                 exchange,
                 "place_stop_loss",
                 side_effect=Exception("APIError(code=-2021): Order would immediately trigger."),
             ), \
             patch.object(exchange, "close_position_market") as market_close:
            outcome = manager.poll_live("BTCUSDT")

        self.assertEqual(outcome, "BREAKEVEN_TRIGGER_MARKET_CLOSE")
        self.assertFalse(manager.has_open_position("BTCUSDT"))
        market_close.assert_called_once_with("BTCUSDT", "BUY", 0.5)
        # The original SL (via the targeted cancel before the failed
        # replace attempt) and everything else on the symbol (via the
        # comprehensive cancel-all after the market close) get cleaned up.
        cancel.assert_called_once_with("BTCUSDT", "sl1")
        cancel_all.assert_called_once_with("BTCUSDT")

    def test_sl_finished_closes_and_cancels_all_open_orders(self):
        manager = self._manager_with_position()

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "sl1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "cancel_all_open_orders") as cancel_all:
            outcome = manager.poll_live("BTCUSDT")

        self.assertEqual(outcome, "SL_HIT")
        self.assertFalse(manager.has_open_position("BTCUSDT"))
        cancel_all.assert_called_once_with("BTCUSDT")

    def test_tp2_finished_directly_closes_as_tp2_hit_direct(self):
        manager = self._manager_with_position()

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp2_1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "cancel_all_open_orders") as cancel_all:
            outcome = manager.poll_live("BTCUSDT")

        self.assertEqual(outcome, "TP2_HIT_DIRECT")
        cancel_all.assert_called_once_with("BTCUSDT")

    def test_breakeven_stage_sl_finished_closes(self):
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["stage"] = BREAKEVEN_ACTIVE

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "sl1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "cancel_all_open_orders") as cancel_all:
            outcome = manager.poll_live("BTCUSDT")

        self.assertEqual(outcome, "BREAKEVEN_STOP_HIT")
        cancel_all.assert_called_once_with("BTCUSDT")

    def test_breakeven_stage_tp2_finished_closes(self):
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["stage"] = BREAKEVEN_ACTIVE

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp2_1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "cancel_all_open_orders") as cancel_all:
            outcome = manager.poll_live("BTCUSDT")

        self.assertEqual(outcome, "TP2_HIT")
        cancel_all.assert_called_once_with("BTCUSDT")

    def test_shadow_position_is_ignored_by_poll_live(self):
        manager = PositionManager()
        manager.register(_plan(), {"shadow": True})

        outcome = manager.poll_live("BTCUSDT")
        self.assertIsNone(outcome)

    def test_unknown_symbol_returns_none(self):
        manager = PositionManager()
        self.assertIsNone(manager.poll_live("NOPE"))

    def test_missing_order_id_is_never_sent_to_the_status_lookup(self):
        # A blank algoId is a guaranteed -1102 from Binance on every call -
        # this must short-circuit locally instead of hitting the exchange.
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp1_order_id"] = ""

        with patch.object(exchange, "get_algo_order_status", return_value="NEW") as status, \
             patch.object(exchange, "place_take_profit_partial", return_value={"algoId": ""}):
            manager.poll_live("BTCUSDT")

        called_ids = {call.args[1] for call in status.call_args_list}
        self.assertNotIn("", called_ids)

    def test_missing_tp1_order_is_recovered(self):
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp1_order_id"] = ""

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(
                 exchange, "place_take_profit_partial", return_value={"algoId": "tp1_new"}
             ) as recover:
            manager.poll_live("BTCUSDT")

        recover.assert_called_once()
        self.assertEqual(manager.positions["BTCUSDT"]["tp1_order_id"], "tp1_new")

    def test_missing_tp2_order_is_recovered_in_either_stage(self):
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp2_order_id"] = ""
        manager.positions["BTCUSDT"]["stage"] = BREAKEVEN_ACTIVE

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(
                 exchange, "place_take_profit_full", return_value={"algoId": "tp2_new"}
             ) as recover:
            manager.poll_live("BTCUSDT")

        recover.assert_called_once()
        self.assertEqual(manager.positions["BTCUSDT"]["tp2_order_id"], "tp2_new")

    def test_tp1_recovery_is_not_attempted_once_in_breakeven_stage(self):
        # TP1 is already resolved by the time BREAKEVEN_ACTIVE is reached -
        # a blank tp1_order_id there is expected (it filled), not missing.
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp1_order_id"] = ""
        manager.positions["BTCUSDT"]["stage"] = BREAKEVEN_ACTIVE

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(exchange, "place_take_profit_partial") as recover:
            manager.poll_live("BTCUSDT")

        recover.assert_not_called()

    def test_recovery_failure_is_handled_gracefully(self):
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp1_order_id"] = ""

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(
                 exchange, "place_take_profit_partial", side_effect=RuntimeError("rejected")
             ):
            outcome = manager.poll_live("BTCUSDT")  # must not raise

        self.assertIsNone(outcome)
        self.assertEqual(manager.positions["BTCUSDT"]["tp1_order_id"], "")


if __name__ == "__main__":
    unittest.main()
