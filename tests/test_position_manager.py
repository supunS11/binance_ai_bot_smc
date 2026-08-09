import time
import unittest
from unittest.mock import patch

import config
import exchange
from position_manager import BREAKEVEN_ACTIVE, TP1_PENDING, PositionManager, _order_type


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


def _candle(high, low, close=None):
    return {
        "open_time": 0, "open": high, "high": high, "low": low,
        "close": high if close is None else close, "volume": 1, "closed": False,
    }


class OrderTypeFieldTests(unittest.TestCase):
    """Real bug, confirmed against v7's proven-working
    find_matching_open_algo_order: the algo-order list endpoint returns
    the type under `orderType`, not `type`. Checking `type` alone matches
    nothing, ever - every "missing order" self-heal attempt then tries to
    place a genuine duplicate and gets rejected with -4130 forever."""

    def test_prefers_the_real_orderType_field(self):
        self.assertEqual(_order_type({"orderType": "STOP_MARKET"}), "STOP_MARKET")

    def test_falls_back_to_type_if_orderType_is_absent(self):
        self.assertEqual(_order_type({"type": "TAKE_PROFIT_MARKET"}), "TAKE_PROFIT_MARKET")

    def test_missing_both_fields_is_empty_not_a_crash(self):
        self.assertEqual(_order_type({}), "")
        self.assertEqual(_order_type(None), "")

    def test_find_open_order_matches_against_the_real_field_shape(self):
        # No "type" key at all - only what Binance actually returns.
        real_tp2 = {"orderType": "TAKE_PROFIT_MARKET", "closePosition": True, "algoId": "real_tp2"}

        with patch.object(exchange, "get_open_algo_orders", return_value=[real_tp2]):
            found = PositionManager._find_open_order("BTCUSDT", "TAKE_PROFIT_MARKET", close_position=True)

        self.assertIsNotNone(found)
        self.assertEqual(found["algoId"], "real_tp2")


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

    def test_adopts_correctly_against_the_real_orderType_field_shape(self):
        # No "type" key, no "origQty" key - only what Binance's algo-order
        # endpoint actually returns (confirmed against v7's proven-working
        # parsing), so this proves the fix against reality, not a guess.
        manager = PositionManager()
        open_orders = [
            {"orderType": "STOP_MARKET", "stopPrice": "98", "algoId": "sl1"},
            {"orderType": "TAKE_PROFIT_MARKET", "closePosition": False, "stopPrice": "102", "quantity": "0.8", "algoId": "tp1_1"},
            {"orderType": "TAKE_PROFIT_MARKET", "closePosition": True, "stopPrice": "104", "algoId": "tp2_1"},
        ]

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position()]), \
             patch.object(exchange, "get_open_algo_orders", return_value=open_orders):
            manager.reconcile_on_startup()

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["sl_price"], 98.0)
        self.assertEqual(position["tp1_price"], 102.0)
        self.assertEqual(position["tp2_price"], 104.0)
        self.assertEqual(position["tp1_quantity"], 0.8)
        self.assertEqual(position["sl_order_id"], "sl1")
        self.assertEqual(position["tp1_order_id"], "tp1_1")
        self.assertEqual(position["tp2_order_id"], "tp2_1")
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

    def test_tp1_pending_adoption_stores_a_real_risk_distance(self):
        # SL is still the genuine original here (TP1 hasn't resolved yet),
        # so entry-to-sl is a trustworthy original risk distance.
        manager = PositionManager()
        open_orders = [
            {"type": "STOP_MARKET", "triggerPrice": "98", "algoId": "sl1"},
        ]

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position(entry=100.0)]), \
             patch.object(exchange, "get_open_algo_orders", return_value=open_orders):
            manager.reconcile_on_startup()

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], TP1_PENDING)
        self.assertEqual(position["risk_distance"], 2.0)

    def test_breakeven_active_adoption_has_no_recoverable_risk_distance(self):
        # Real bug found live (2026-08-09): sl here is the REAL exchange
        # order's price, which by BREAKEVEN_ACTIVE time is the breakeven
        # price (~entry), not the true original stop - that original
        # distance was lost before this restart. Storing
        # abs(entry-sl)=0.02 here instead of None would silently
        # reintroduce the billions-R bug the next time this position's
        # MAE/MFE gets computed at close.
        manager = PositionManager()
        open_orders = [
            {"type": "STOP_MARKET", "triggerPrice": "100.02", "algoId": "sl2"},
            {"type": "TAKE_PROFIT_MARKET", "closePosition": "true", "triggerPrice": "104", "algoId": "tp2_1"},
        ]

        with patch.object(exchange, "get_all_open_positions", return_value=[self._live_position(entry=100.0)]), \
             patch.object(exchange, "get_open_algo_orders", return_value=open_orders):
            manager.reconcile_on_startup()

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], BREAKEVEN_ACTIVE)
        self.assertIsNone(position["risk_distance"])

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


class ReentryCooldownTests(unittest.TestCase):
    def test_symbol_never_closed_is_not_in_cooldown(self):
        manager = PositionManager()
        self.assertFalse(manager.is_in_cooldown("BTCUSDT"))

    def test_symbol_closed_recently_is_in_cooldown(self):
        manager = PositionManager()
        manager._closed_at["BTCUSDT"] = time.time()

        with patch.object(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 900):
            self.assertTrue(manager.is_in_cooldown("BTCUSDT"))

    def test_close_starts_the_cooldown(self):
        manager = PositionManager()
        manager.register(_plan(), {"shadow": True})

        with patch.object(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 900), \
             patch("position_manager.signal_journal.append_outcome"):
            manager._close("BTCUSDT", "SHADOW_SL_HIT")

        self.assertTrue(manager.is_in_cooldown("BTCUSDT"))

    def test_cooldown_expires_after_the_configured_window(self):
        manager = PositionManager()
        manager._closed_at["BTCUSDT"] = 0.0  # far in the past

        with patch.object(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 1):
            self.assertFalse(manager.is_in_cooldown("BTCUSDT"))

    def test_zero_cooldown_disables_it(self):
        manager = PositionManager()
        manager._closed_at["BTCUSDT"] = time.time()

        with patch.object(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 0):
            self.assertFalse(manager.is_in_cooldown("BTCUSDT"))

    def test_mark_entry_failure_starts_the_cooldown_too(self):
        # Real bug found live (STGUSDT/DEXEUSDT, 2026-08-08): a failed
        # entry never reaches register()/_close(), so is_in_cooldown()'s
        # only trigger never fired for it - a symbol that keeps failing
        # entry for a persistent reason got retried on every single eval
        # cycle with no backoff at all.
        manager = PositionManager()

        with patch.object(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 900):
            self.assertFalse(manager.is_in_cooldown("STGUSDT"))
            manager.mark_entry_failure("STGUSDT")
            self.assertTrue(manager.is_in_cooldown("STGUSDT"))


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
        args, kwargs = append_outcome.call_args
        self.assertEqual(args, ("BTCUSDT", "SHADOW_SL_HIT", "BTCUSDT_123456"))
        self.assertIn("mae_r_multiple", kwargs)
        self.assertIn("mfe_r_multiple", kwargs)

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

    def test_confluence_ratio_is_carried_from_the_plan(self):
        manager = PositionManager()
        position = manager.register(dict(_plan(), confluence_ratio=0.25), {"shadow": True})

        self.assertEqual(position["confluence_ratio"], 0.25)
        self.assertFalse(position["early_breakeven_applied"])

    def test_risk_distance_is_read_from_the_plan_when_present(self):
        manager = PositionManager()
        position = manager.register(dict(_plan(), risk_distance=1.75), {"shadow": True})

        self.assertEqual(position["risk_distance"], 1.75)

    def test_risk_distance_falls_back_to_entry_minus_sl_when_missing_from_plan(self):
        # _plan() fixture: entry=100, sl=98
        manager = PositionManager()
        position = manager.register(_plan(), {"shadow": True})

        self.assertEqual(position["risk_distance"], 2)


class EarlyBreakevenEligibilityTests(unittest.TestCase):
    """config.EARLY_BREAKEVEN_ENABLED - low-confluence trades get pulled
    to breakeven before TP1, instead of gating entry on confluence at
    all. These test the eligibility check in isolation from any exchange
    or shadow-candle mechanics."""

    def _position(self, **overrides):
        position = {
            "side": "BUY",
            "entry_price": 100,
            "sl_price": 98,
            "breakeven_price": 100.02,
            "stage": TP1_PENDING,
            "confluence_ratio": 0.25,
            "early_breakeven_applied": False,
        }
        position.update(overrides)
        return position

    def test_disabled_config_is_never_a_candidate(self):
        manager = PositionManager()

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", False):
            self.assertFalse(manager._is_early_breakeven_candidate(self._position()))

    def test_already_applied_is_never_a_candidate_again(self):
        manager = PositionManager()

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True):
            self.assertFalse(manager._is_early_breakeven_candidate(
                self._position(early_breakeven_applied=True)
            ))

    def test_wrong_stage_is_not_a_candidate(self):
        manager = PositionManager()

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True):
            self.assertFalse(manager._is_early_breakeven_candidate(
                self._position(stage=BREAKEVEN_ACTIVE)
            ))

    def test_missing_confluence_ratio_is_not_a_candidate(self):
        # No original signal to read confluence from (e.g. a position
        # adopted via startup reconciliation) - no evidence, no early
        # promotion.
        manager = PositionManager()

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True):
            self.assertFalse(manager._is_early_breakeven_candidate(
                self._position(confluence_ratio=None)
            ))

    def test_confluence_above_threshold_is_not_a_candidate(self):
        manager = PositionManager()

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True), \
             patch.object(config, "EARLY_BREAKEVEN_CONFLUENCE_THRESHOLD", 0.5):
            self.assertFalse(manager._is_early_breakeven_candidate(
                self._position(confluence_ratio=0.75)
            ))

    def test_confluence_at_or_below_threshold_is_a_candidate(self):
        manager = PositionManager()

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True), \
             patch.object(config, "EARLY_BREAKEVEN_CONFLUENCE_THRESHOLD", 0.5):
            self.assertTrue(manager._is_early_breakeven_candidate(
                self._position(confluence_ratio=0.5)
            ))
            self.assertTrue(manager._is_early_breakeven_candidate(
                self._position(confluence_ratio=0.25)
            ))


class EarlyBreakevenPriceReachedTests(unittest.TestCase):
    def _position(self, side="BUY", entry_price=100, sl_price=98):
        return {"side": side, "entry_price": entry_price, "sl_price": sl_price}

    def test_none_price_is_not_reached(self):
        with patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 1.0):
            self.assertFalse(
                PositionManager._early_breakeven_price_reached(self._position(), None)
            )

    def test_buy_reaches_trigger_at_full_r_multiple(self):
        # risk_distance = 2 (100 - 98), R multiple 1.0 -> needs +2 favorable
        with patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 1.0):
            self.assertTrue(
                PositionManager._early_breakeven_price_reached(self._position(), 102)
            )
            self.assertFalse(
                PositionManager._early_breakeven_price_reached(self._position(), 101)
            )

    def test_sell_reaches_trigger_at_full_r_multiple(self):
        position = self._position(side="SELL", entry_price=100, sl_price=102)

        with patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 1.0):
            self.assertTrue(PositionManager._early_breakeven_price_reached(position, 98))
            self.assertFalse(PositionManager._early_breakeven_price_reached(position, 99))

    def test_smaller_r_multiple_triggers_earlier(self):
        with patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 0.5):
            # only +1 favorable needed now (0.5 * risk_distance 2)
            self.assertTrue(
                PositionManager._early_breakeven_price_reached(self._position(), 101)
            )


class MaeMfeTrackingTests(unittest.TestCase):
    """config.MAE_TRACKING_ENABLED - the diagnostic that tells apart a
    trade that went wrong from the first tick (near-zero MFE) from one
    that was solidly in profit before fully reversing (large MFE), which
    look identical as a plain WIN/LOSS outcome."""

    def _position(self, side="BUY", entry_price=100, sl_price=98, **overrides):
        position = {
            "side": side, "entry_price": entry_price, "sl_price": sl_price,
            "mae_price": entry_price, "mfe_price": entry_price,
            "risk_distance": abs(entry_price - sl_price),
        }
        position.update(overrides)
        return position

    def test_disabled_config_leaves_mae_mfe_untouched(self):
        position = self._position()

        with patch.object(config, "MAE_TRACKING_ENABLED", False):
            PositionManager._update_mae_mfe(position, 90, 110)

        self.assertEqual(position["mae_price"], 100)
        self.assertEqual(position["mfe_price"], 100)

    def test_buy_tracks_low_as_adverse_and_high_as_favorable(self):
        position = self._position(side="BUY")

        with patch.object(config, "MAE_TRACKING_ENABLED", True):
            PositionManager._update_mae_mfe(position, 97, 103)

        self.assertEqual(position["mae_price"], 97)
        self.assertEqual(position["mfe_price"], 103)

    def test_sell_tracks_high_as_adverse_and_low_as_favorable(self):
        position = self._position(side="SELL", entry_price=100, sl_price=102)

        with patch.object(config, "MAE_TRACKING_ENABLED", True):
            PositionManager._update_mae_mfe(position, 97, 103)

        self.assertEqual(position["mae_price"], 103)
        self.assertEqual(position["mfe_price"], 97)

    def test_single_price_variant_updates_both_extremes_from_one_sample(self):
        position = self._position(side="BUY")

        with patch.object(config, "MAE_TRACKING_ENABLED", True):
            PositionManager._update_mae_mfe(position, 105)  # no high_or_price given

        self.assertEqual(position["mae_price"], 100)  # unchanged, 105 isn't adverse
        self.assertEqual(position["mfe_price"], 105)

    def test_extremes_only_ever_move_in_the_worse_or_better_direction(self):
        # A later, less-extreme sample must not undo an already-recorded
        # worst/best price.
        position = self._position(side="BUY")

        with patch.object(config, "MAE_TRACKING_ENABLED", True):
            PositionManager._update_mae_mfe(position, 95, 105)
            PositionManager._update_mae_mfe(position, 98, 101)

        self.assertEqual(position["mae_price"], 95)
        self.assertEqual(position["mfe_price"], 105)

    def test_none_price_is_ignored(self):
        position = self._position()

        with patch.object(config, "MAE_TRACKING_ENABLED", True):
            PositionManager._update_mae_mfe(position, None)

        self.assertEqual(position["mae_price"], 100)
        self.assertEqual(position["mfe_price"], 100)

    def test_r_multiples_are_normalized_to_risk_distance(self):
        # entry=100, sl=98 -> risk_distance=2
        position = self._position(mae_price=97, mfe_price=103)

        mae_r, mfe_r = PositionManager._mae_mfe_r_multiples(position)

        self.assertAlmostEqual(mae_r, 1.5)  # (100-97)/2
        self.assertAlmostEqual(mfe_r, 1.5)  # (103-100)/2

    def test_r_multiples_use_the_stored_risk_distance_not_the_live_sl_price(self):
        # Real bug found live (2026-08-09): once a trade is promoted to
        # breakeven, sl_price legitimately moves to ~entry_price (shadow
        # mode mutates it directly; a reconciled-while-already-breakeven
        # position picks it up from the real exchange order). Recomputing
        # risk_distance from that moved sl_price at close time divided
        # real MAE/MFE price distances by a near-zero breakeven-buffer
        # distance instead of the original ~2.0, producing R-multiples in
        # the billions. The fixed risk_distance field must be used
        # instead, completely ignoring wherever sl_price ended up.
        position = self._position(
            sl_price=98, mae_price=97, mfe_price=104,
            risk_distance=2.0,  # captured once at entry, before promotion
        )
        position["sl_price"] = 100.02  # moved to breakeven, as it really would be

        mae_r, mfe_r = PositionManager._mae_mfe_r_multiples(position)

        self.assertAlmostEqual(mae_r, 1.5)  # (100-97)/2, NOT /0.02
        self.assertAlmostEqual(mfe_r, 2.0)  # (104-100)/2, NOT /0.02

    def test_r_multiples_are_none_when_risk_distance_is_unknown(self):
        # The stored risk_distance can itself be None (a position adopted
        # via startup reconciliation while already BREAKEVEN_ACTIVE has no
        # recoverable original risk distance) - must stay honestly
        # unknown rather than falling back to a live sl_price-derived
        # value that would reintroduce the same bug.
        position = self._position(risk_distance=None)

        mae_r, mfe_r = PositionManager._mae_mfe_r_multiples(position)

        self.assertIsNone(mae_r)
        self.assertIsNone(mfe_r)

    def test_r_multiples_are_none_when_risk_distance_is_zero(self):
        position = self._position(entry_price=100, sl_price=100)

        mae_r, mfe_r = PositionManager._mae_mfe_r_multiples(position)

        self.assertIsNone(mae_r)
        self.assertIsNone(mfe_r)


class PollShadowTests(unittest.TestCase):
    def _manager_with_position(self, side="BUY", confluence_ratio=None):
        manager = PositionManager()
        manager.register(dict(_plan(side), confluence_ratio=confluence_ratio), {"shadow": True})
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

    def test_low_confluence_early_breakeven_triggers_before_tp1_reached(self):
        # entry=100, sl=98 (risk=2), R multiple 0.5 -> trigger at close=101,
        # well below tp1(102) - isolates the early trigger from a genuine
        # TP1 hit.
        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True), \
             patch.object(config, "EARLY_BREAKEVEN_CONFLUENCE_THRESHOLD", 0.5), \
             patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 0.5):
            manager = self._manager_with_position(confluence_ratio=0.25)
            outcome = manager.poll_shadow("BTCUSDT", _candle(high=101.5, low=99, close=101))

        self.assertIsNone(outcome)
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], BREAKEVEN_ACTIVE)
        self.assertTrue(position["early_breakeven_applied"])
        self.assertEqual(position["sl_price"], position["breakeven_price"])

    def test_low_confluence_but_price_not_moved_enough_stays_pending(self):
        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True), \
             patch.object(config, "EARLY_BREAKEVEN_CONFLUENCE_THRESHOLD", 0.5), \
             patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 0.5):
            manager = self._manager_with_position(confluence_ratio=0.25)
            outcome = manager.poll_shadow("BTCUSDT", _candle(high=100.8, low=99, close=100.5))

        self.assertIsNone(outcome)
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], TP1_PENDING)
        self.assertFalse(position["early_breakeven_applied"])

    def test_high_confluence_never_triggers_early_breakeven(self):
        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True), \
             patch.object(config, "EARLY_BREAKEVEN_CONFLUENCE_THRESHOLD", 0.5), \
             patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 0.5):
            manager = self._manager_with_position(confluence_ratio=0.75)
            # Same favorable close as the triggering test above, but this
            # trade has enough confluence to not qualify.
            outcome = manager.poll_shadow("BTCUSDT", _candle(high=101.5, low=99, close=101))

        self.assertIsNone(outcome)
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], TP1_PENDING)
        self.assertFalse(position["early_breakeven_applied"])

    def test_mae_mfe_track_the_full_candle_range_not_just_the_close(self):
        # BUY, entry=100, sl=98, tp1=102 - low stays above sl and high
        # stays below tp1, so the position survives this poll untouched
        # and both extremes are purely from the range tracking.
        manager = self._manager_with_position()
        manager.poll_shadow("BTCUSDT", _candle(high=101, low=98.5, close=99.5))

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["mae_price"], 98.5)
        self.assertEqual(position["mfe_price"], 101)

    def test_mae_mfe_disabled_leaves_tracking_at_entry(self):
        with patch.object(config, "MAE_TRACKING_ENABLED", False):
            manager = self._manager_with_position()
            manager.poll_shadow("BTCUSDT", _candle(high=101, low=98.5, close=99.5))

        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["mae_price"], 100)
        self.assertEqual(position["mfe_price"], 100)

    def test_journal_receives_mae_mfe_r_multiples_on_close(self):
        manager = self._manager_with_position()  # BUY, entry=100, sl=98, risk=2

        with patch("position_manager.signal_journal.append_outcome") as append_outcome:
            # Moved favorably to 101 within the same candle before also
            # touching down to 97 (low(97) <= sl(98)) -> closes as
            # SHADOW_SL_HIT, but with real favorable excursion recorded
            # along the way - exactly the case MFE exists to distinguish
            # from a trade that went straight down.
            outcome = manager.poll_shadow("BTCUSDT", _candle(high=101, low=97))

        self.assertEqual(outcome, "SHADOW_SL_HIT")
        append_outcome.assert_called_once()
        _, kwargs = append_outcome.call_args
        self.assertAlmostEqual(kwargs["mae_r_multiple"], 1.5)  # (100-97)/2
        self.assertAlmostEqual(kwargs["mfe_r_multiple"], 0.5)  # (101-100)/2


class PollLiveTests(unittest.TestCase):
    def _manager_with_position(self, confluence_ratio=None):
        manager = PositionManager()
        execution_result = {
            "shadow": False,
            "sl_order": {"algoId": "sl1"},
            "tp1_order": {"algoId": "tp1_1"},
            "tp2_order": {"algoId": "tp2_1"},
        }
        manager.register(dict(_plan(), confluence_ratio=confluence_ratio), execution_result)
        return manager

    def test_tp1_finished_promotes_to_breakeven(self):
        manager = self._manager_with_position()

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp1_1" else "NEW"

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "_fetch_open_position_detail", return_value={"quantity": 0.5}), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(exchange, "cancel_algo_order") as cancel, \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": "sl2"}) as new_sl:
            outcome = manager.poll_live("BTCUSDT")

        self.assertIsNone(outcome)
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], BREAKEVEN_ACTIVE)
        self.assertEqual(position["sl_order_id"], "sl2")
        cancel.assert_called_once_with("BTCUSDT", "sl1")
        new_sl.assert_called_once_with("BTCUSDT", "BUY", position["breakeven_price"])

    def test_breakeven_promotion_cancels_the_real_sl_not_a_stale_local_id(self):
        # Real bug seen live: local tracking's sl_order_id can be stale
        # (e.g. from a reconciliation mismatch) while a real SL is still
        # open under a different id - cancelling the stale id cancels
        # nothing, and the new placement then fails with -4130 forever.
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["sl_order_id"] = "stale_local_id"

        def status_side_effect(symbol, order_id):
            return "FINISHED" if order_id == "tp1_1" else "NEW"

        real_sl_order = {"type": "STOP_MARKET", "closePosition": True, "algoId": "real_sl_on_exchange"}

        with patch.object(exchange, "get_algo_order_status", side_effect=status_side_effect), \
             patch.object(exchange, "_fetch_open_position_detail", return_value={"quantity": 0.5}), \
             patch.object(exchange, "get_open_algo_orders", return_value=[real_sl_order]), \
             patch.object(exchange, "cancel_algo_order") as cancel, \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": "sl2"}):
            manager.poll_live("BTCUSDT")

        cancel.assert_called_once_with("BTCUSDT", "real_sl_on_exchange")

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
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
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
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(exchange, "place_take_profit_partial", return_value={"algoId": ""}):
            manager.poll_live("BTCUSDT")

        called_ids = {call.args[1] for call in status.call_args_list}
        self.assertNotIn("", called_ids)

    def test_missing_tp1_order_is_recovered(self):
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp1_order_id"] = ""

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(
                 exchange, "place_take_profit_partial", return_value={"algoId": "tp1_new"}
             ) as recover:
            manager.poll_live("BTCUSDT")

        recover.assert_called_once()
        self.assertEqual(manager.positions["BTCUSDT"]["tp1_order_id"], "tp1_new")

    def test_missing_tp1_order_already_exists_on_exchange_is_resynced_not_duplicated(self):
        # The real bug seen live: local tracking lost the id while the
        # real order is still there - placing another gets rejected with
        # -4130 forever. Must adopt the real id instead of duplicating.
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp1_order_id"] = ""
        real_tp1 = {"type": "TAKE_PROFIT_MARKET", "closePosition": False, "algoId": "real_tp1"}

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(exchange, "get_open_algo_orders", return_value=[real_tp1]), \
             patch.object(exchange, "place_take_profit_partial") as place:
            manager.poll_live("BTCUSDT")

        place.assert_not_called()
        self.assertEqual(manager.positions["BTCUSDT"]["tp1_order_id"], "real_tp1")

    def test_missing_tp2_order_is_recovered_in_either_stage(self):
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp2_order_id"] = ""
        manager.positions["BTCUSDT"]["stage"] = BREAKEVEN_ACTIVE

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(
                 exchange, "place_take_profit_full", return_value={"algoId": "tp2_new"}
             ) as recover:
            manager.poll_live("BTCUSDT")

        recover.assert_called_once()
        self.assertEqual(manager.positions["BTCUSDT"]["tp2_order_id"], "tp2_new")

    def test_tp1_market_close_instead_when_price_already_passed_it(self):
        # -2021 on TP1 specifically means price already passed that level -
        # take the partial at market instead of retrying a placement that
        # can never succeed.
        manager = self._manager_with_position()
        manager.positions["BTCUSDT"]["tp1_order_id"] = ""

        with patch.object(exchange, "get_algo_order_status", return_value="NEW"), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(
                 exchange,
                 "place_take_profit_partial",
                 side_effect=Exception("APIError(code=-2021): Order would immediately trigger."),
             ), \
             patch.object(exchange, "close_position_market") as market_close, \
             patch.object(exchange, "_fetch_open_position_detail", return_value={"quantity": 0.5}), \
             patch.object(exchange, "cancel_algo_order"), \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": "sl2"}):
            manager.poll_live("BTCUSDT")

        market_close.assert_called_once_with("BTCUSDT", "BUY", 0.5)

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
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(
                 exchange, "place_take_profit_partial", side_effect=RuntimeError("rejected")
             ):
            outcome = manager.poll_live("BTCUSDT")  # must not raise

        self.assertIsNone(outcome)
        self.assertEqual(manager.positions["BTCUSDT"]["tp1_order_id"], "")

    def test_low_confluence_triggers_early_breakeven_before_the_normal_tp1_check(self):
        manager = self._manager_with_position(confluence_ratio=0.25)

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True), \
             patch.object(config, "EARLY_BREAKEVEN_CONFLUENCE_THRESHOLD", 0.5), \
             patch.object(config, "EARLY_BREAKEVEN_R_MULTIPLE", 1.0), \
             patch.object(exchange, "get_mark_price", return_value=102.0), \
             patch.object(exchange, "get_algo_order_status") as status_mock, \
             patch.object(exchange, "_fetch_open_position_detail", return_value={"quantity": 1.0}), \
             patch.object(exchange, "get_open_algo_orders", return_value=[]), \
             patch.object(exchange, "cancel_algo_order") as cancel, \
             patch.object(exchange, "place_stop_loss", return_value={"algoId": "sl2"}) as new_sl:
            outcome = manager.poll_live("BTCUSDT")

        self.assertIsNone(outcome)
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], BREAKEVEN_ACTIVE)
        self.assertTrue(position["early_breakeven_applied"])
        self.assertEqual(position["sl_order_id"], "sl2")
        new_sl.assert_called_once_with("BTCUSDT", "BUY", position["breakeven_price"])
        cancel.assert_called_once_with("BTCUSDT", "sl1")
        # Never reached the normal TP1/SL/TP2 status checks this cycle.
        status_mock.assert_not_called()

    def test_high_confluence_never_triggers_early_breakeven(self):
        # MAE tracking still fetches mark price for every position
        # regardless of confluence - only the early-breakeven trigger
        # itself is confluence-gated.
        manager = self._manager_with_position(confluence_ratio=0.75)

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", True), \
             patch.object(config, "EARLY_BREAKEVEN_CONFLUENCE_THRESHOLD", 0.5), \
             patch.object(exchange, "get_mark_price", return_value=102.0), \
             patch.object(exchange, "get_algo_order_status", return_value="NEW"):
            outcome = manager.poll_live("BTCUSDT")

        self.assertIsNone(outcome)
        position = manager.positions["BTCUSDT"]
        self.assertEqual(position["stage"], TP1_PENDING)
        self.assertFalse(position["early_breakeven_applied"])

    def test_mark_price_never_fetched_when_both_mae_tracking_and_early_breakeven_are_off(self):
        manager = self._manager_with_position(confluence_ratio=0.25)

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", False), \
             patch.object(config, "MAE_TRACKING_ENABLED", False), \
             patch.object(exchange, "get_mark_price") as mark_price_mock, \
             patch.object(exchange, "get_algo_order_status", return_value="NEW"):
            manager.poll_live("BTCUSDT")

        mark_price_mock.assert_not_called()

    def test_mae_tracking_still_fetches_mark_price_when_early_breakeven_is_off(self):
        manager = self._manager_with_position(confluence_ratio=0.25)

        with patch.object(config, "EARLY_BREAKEVEN_ENABLED", False), \
             patch.object(config, "MAE_TRACKING_ENABLED", True), \
             patch.object(exchange, "get_mark_price", return_value=101.0) as mark_price_mock, \
             patch.object(exchange, "get_algo_order_status", return_value="NEW"):
            manager.poll_live("BTCUSDT")

        mark_price_mock.assert_called_once_with("BTCUSDT")
        self.assertEqual(manager.positions["BTCUSDT"]["mfe_price"], 101.0)


if __name__ == "__main__":
    unittest.main()
