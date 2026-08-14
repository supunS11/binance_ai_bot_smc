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


class MinStopDistanceAtrFloorTests(unittest.TestCase):
    """The floor is the WIDER of MIN_STOP_DISTANCE_PCT and
    MIN_STOP_DISTANCE_ATR_MULTIPLE*atr, not the percentage alone - real
    evidence (2026-08-13: a direct log-distance trace of 24 real trades
    plus three consecutive journal_analysis.py pulls) showed the flat 0.6%
    floor alone getting hit on ~40% of trades, every one resolving as a
    loss or scratch, because 0.6% is inside normal 1h noise for many of
    this watchlist's volatile small/mid-cap symbols."""

    def test_atr_floor_wins_when_wider_than_the_pct_floor_buy(self):
        # pct floor: 100 * 0.1% = 0.1. atr floor: 2 * 1.0 = 2.0 (wider).
        signal = {"structure_level": 99.95, "atr": 2, "entry_price": 100.0}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.1), \
             patch.object(config, "MIN_STOP_DISTANCE_ATR_MULTIPLE", 1.0):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertAlmostEqual(sl, 98.0)  # 100 - (2 * 1.0)

    def test_atr_floor_wins_when_wider_than_the_pct_floor_sell(self):
        signal = {"structure_level": 100.05, "atr": 2, "entry_price": 100.0}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.1), \
             patch.object(config, "MIN_STOP_DISTANCE_ATR_MULTIPLE", 1.0):
            sl = risk_manager.compute_stop_loss(signal, "SELL")

        self.assertAlmostEqual(sl, 102.0)  # 100 + (2 * 1.0)

    def test_pct_floor_still_wins_when_it_is_wider_than_the_atr_floor(self):
        # pct floor: 100 * 0.6% = 0.6. atr floor: 0.1 * 1.0 = 0.1 (narrower).
        signal = {"structure_level": 99.98, "atr": 0.1, "entry_price": 100.0}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.6), \
             patch.object(config, "MIN_STOP_DISTANCE_ATR_MULTIPLE", 1.0):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertAlmostEqual(sl, 99.4)  # 100 - 0.6%

    def test_zero_atr_multiple_disables_the_atr_floor(self):
        signal = {"structure_level": 99.95, "atr": 2, "entry_price": 100.0}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.1), \
             patch.object(config, "MIN_STOP_DISTANCE_ATR_MULTIPLE", 0):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertAlmostEqual(sl, 99.9)  # 100 - 0.1%, atr floor contributes nothing

    def test_a_stop_wider_than_both_floors_is_left_untouched(self):
        signal = {"structure_level": 90.0, "atr": 0.1, "entry_price": 100.0}

        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "MIN_STOP_DISTANCE_PCT", 0.6), \
             patch.object(config, "MIN_STOP_DISTANCE_ATR_MULTIPLE", 1.0):
            sl = risk_manager.compute_stop_loss(signal, "BUY")

        self.assertEqual(sl, 90.0)


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

    def test_pool_beyond_the_max_bound_is_rejected_not_used(self):
        # Real bug seen live: the nearest *qualifying* pool can still be
        # absurdly far away (~20R) if nothing closer exists - that's not
        # an achievable TP1, so it must be rejected in favor of the
        # bounded fallback instead of blindly taking "nearest that clears
        # the floor" with no ceiling.
        pools = [{"type": "BUY_SIDE", "price": 140}]  # 20R away

        with patch.object(config, "TP1_R_MULTIPLE", 1.0), \
             patch.object(config, "TP1_MAX_R_MULTIPLE", 6.0), \
             patch.object(config, "TP2_R_MULTIPLE", 2.0), \
             patch.object(config, "TP2_MAX_R_MULTIPLE", 10.0):
            tp1, tp2 = risk_manager.compute_targets(100, 98, "BUY", pools=pools)

        self.assertEqual(tp1, 102)  # fallback: 100 + 1R, the 20R pool rejected
        self.assertEqual(tp2, 104)  # fallback: 100 + 2R

    def test_pool_within_bounds_is_still_used_normally(self):
        pools = [{"type": "BUY_SIDE", "price": 106}]  # 3R - within [1, 6]

        with patch.object(config, "TP1_R_MULTIPLE", 1.0), \
             patch.object(config, "TP1_MAX_R_MULTIPLE", 6.0):
            tp1, _ = risk_manager.compute_targets(100, 98, "BUY", pools=pools)

        self.assertEqual(tp1, 106)

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


class ComputeEarlyBreakevenPriceTests(unittest.TestCase):
    def test_zero_lock_multiple_falls_back_to_flat_breakeven(self):
        with patch.object(config, "EARLY_BREAKEVEN_LOCK_R_MULTIPLE", 0), \
             patch.object(config, "BREAKEVEN_BUFFER_PCT", 0.02):
            price = risk_manager.compute_early_breakeven_price(100, "BUY", 2.0)

        self.assertAlmostEqual(price, 100.02)

    def test_buy_locks_profit_at_the_configured_r_multiple(self):
        with patch.object(config, "EARLY_BREAKEVEN_LOCK_R_MULTIPLE", 0.3):
            price = risk_manager.compute_early_breakeven_price(100, "BUY", 2.0)

        self.assertAlmostEqual(price, 100.6)  # 100 + 2*0.3

    def test_sell_locks_profit_at_the_configured_r_multiple(self):
        with patch.object(config, "EARLY_BREAKEVEN_LOCK_R_MULTIPLE", 0.3):
            price = risk_manager.compute_early_breakeven_price(100, "SELL", 2.0)

        self.assertAlmostEqual(price, 99.4)  # 100 - 2*0.3

    def test_negative_lock_multiple_is_clamped_to_zero(self):
        with patch.object(config, "EARLY_BREAKEVEN_LOCK_R_MULTIPLE", -1), \
             patch.object(config, "BREAKEVEN_BUFFER_PCT", 0.02):
            price = risk_manager.compute_early_breakeven_price(100, "BUY", 2.0)

        self.assertAlmostEqual(price, 100.02)


class BuildTradePlanTests(unittest.TestCase):
    def setUp(self):
        # MAX_ENTRY_EXTENSION_R defaults to 0.5 in config, but this
        # fixture's default entry_price/structure_level gap sits well
        # above that - off by default here so tests not about this
        # feature aren't coupled to it; ExtensionCapTests turns it back
        # on locally. Same treatment for MAX_SL_ROI_PCT - MaxSlRoiTests
        # turns it back on locally.
        self.extension_patcher = patch.object(config, "MAX_ENTRY_EXTENSION_R", 0)
        self.extension_patcher.start()
        self.sl_roi_patcher = patch.object(config, "MAX_SL_ROI_PCT", 0)
        self.sl_roi_patcher.start()

    def tearDown(self):
        self.extension_patcher.stop()
        self.sl_roi_patcher.stop()

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

    def test_plan_is_unaffected_by_limit_entry_mode(self):
        # config.LIMIT_ENTRY_MODE_ENABLED only changes execution.py/
        # position_manager.py (how the entry gets placed and tracked) -
        # the risk plan itself (SL/TP/sizing) must be identical either
        # way, since a LIMIT entry uses the same signal["entry_price"]
        # build_trade_plan already computes off of today.
        with patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "TP1_R_MULTIPLE", 1.0), \
             patch.object(config, "TP2_R_MULTIPLE", 2.0), \
             patch.object(config, "TP1_CLOSE_PCT", 50), \
             patch.object(risk_manager, "calculate_position_size", return_value=10.0):
            with patch.object(config, "LIMIT_ENTRY_MODE_ENABLED", False):
                plan_off, status_off = risk_manager.build_trade_plan(self._signal(), balance=1000)
            with patch.object(config, "LIMIT_ENTRY_MODE_ENABLED", True):
                plan_on, status_on = risk_manager.build_trade_plan(self._signal(), balance=1000)

        self.assertEqual(status_off, status_on)
        self.assertEqual(plan_off, plan_on)

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

    def test_full_confluence_scales_the_risk_budget_up_to_the_max_multiplier(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True), \
             patch.object(config, "CONFLUENCE_SIZING_MIN_MULTIPLIER", 0.5), \
             patch.object(config, "CONFLUENCE_SIZING_MAX_MULTIPLIER", 1.25), \
             patch.object(config, "RISK_BASED_POSITION_SIZING_ENABLED", True), \
             patch.object(risk_manager, "get_position_risk_budget", return_value=10.0), \
             patch.object(risk_manager, "calculate_position_size", return_value=5.0) as mock_size:
            plan, status = risk_manager.build_trade_plan(
                dict(self._signal(), confluence_ratio=1.0), balance=1000
            )

        self.assertEqual(status, "OK")
        self.assertEqual(plan["size_multiplier"], 1.25)
        _, kwargs = mock_size.call_args
        self.assertAlmostEqual(kwargs["risk_budget_override"], 12.5)

    def test_zero_confluence_scales_the_risk_budget_down_to_the_min_multiplier(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True), \
             patch.object(config, "CONFLUENCE_SIZING_MIN_MULTIPLIER", 0.5), \
             patch.object(config, "CONFLUENCE_SIZING_MAX_MULTIPLIER", 1.25), \
             patch.object(config, "RISK_BASED_POSITION_SIZING_ENABLED", True), \
             patch.object(risk_manager, "get_position_risk_budget", return_value=10.0), \
             patch.object(risk_manager, "calculate_position_size", return_value=5.0) as mock_size:
            plan, status = risk_manager.build_trade_plan(
                dict(self._signal(), confluence_ratio=0.0), balance=1000
            )

        self.assertEqual(status, "OK")
        self.assertEqual(plan["size_multiplier"], 0.5)
        _, kwargs = mock_size.call_args
        self.assertAlmostEqual(kwargs["risk_budget_override"], 5.0)

    def test_missing_confluence_ratio_behaves_as_full_normal_size(self):
        # Every trade still trades - a signal with no confluence_ratio at
        # all (e.g. an older/unrelated caller) must size exactly as it did
        # before this feature existed, not get penalized.
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True), \
             patch.object(config, "RISK_BASED_POSITION_SIZING_ENABLED", True), \
             patch.object(risk_manager, "get_position_risk_budget", return_value=10.0), \
             patch.object(risk_manager, "calculate_position_size", return_value=5.0) as mock_size:
            plan, status = risk_manager.build_trade_plan(self._signal(), balance=1000)

        self.assertEqual(status, "OK")
        self.assertEqual(plan["size_multiplier"], 1.0)
        _, kwargs = mock_size.call_args
        self.assertAlmostEqual(kwargs["risk_budget_override"], 10.0)

    def test_confluence_sizing_disabled_always_uses_normal_size(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", False), \
             patch.object(config, "RISK_BASED_POSITION_SIZING_ENABLED", True), \
             patch.object(risk_manager, "get_position_risk_budget", return_value=10.0), \
             patch.object(risk_manager, "calculate_position_size", return_value=5.0) as mock_size:
            plan, status = risk_manager.build_trade_plan(
                dict(self._signal(), confluence_ratio=0.0), balance=1000
            )

        self.assertEqual(status, "OK")
        self.assertEqual(plan["size_multiplier"], 1.0)
        _, kwargs = mock_size.call_args
        self.assertAlmostEqual(kwargs["risk_budget_override"], 10.0)

    def test_flat_sizing_mode_scales_margin_instead_of_risk_budget(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True), \
             patch.object(config, "CONFLUENCE_SIZING_MIN_MULTIPLIER", 0.5), \
             patch.object(config, "CONFLUENCE_SIZING_MAX_MULTIPLIER", 1.25), \
             patch.object(config, "RISK_BASED_POSITION_SIZING_ENABLED", False), \
             patch.object(config, "MARGIN_PER_TRADE", 20), \
             patch.object(risk_manager, "calculate_position_size", return_value=5.0) as mock_size:
            plan, status = risk_manager.build_trade_plan(
                dict(self._signal(), confluence_ratio=1.0), balance=1000
            )

        self.assertEqual(status, "OK")
        _, kwargs = mock_size.call_args
        self.assertAlmostEqual(kwargs["margin_override"], 25.0)
        self.assertNotIn("risk_budget_override", kwargs)


class EntryExtensionCapTests(unittest.TestCase):
    """See config.MAX_ENTRY_EXTENSION_R - real motivation (2026-08-12,
    live): confirmation delays (REQUIRE_CLOSE_CONFIRMED_BREAK +
    SIGNAL_CONFIRM_TICKS) can let price run well past the break level
    before entry fires, and execution.py always market-orders at
    whatever price exists by then with no check on how far that already
    was from the level that made the setup valid in the first place."""

    def test_extension_ratio_for_a_buy(self):
        ratio = risk_manager._entry_extension_r(
            {"structure_level": 98}, entry_price=98.8, side="BUY", risk_distance=2.0
        )
        self.assertAlmostEqual(ratio, 0.4)  # 0.8/2.0

    def test_extension_ratio_for_a_sell_is_measured_in_the_opposite_direction(self):
        ratio = risk_manager._entry_extension_r(
            {"structure_level": 102}, entry_price=100, side="SELL", risk_distance=2.0
        )
        self.assertAlmostEqual(ratio, 1.0)  # (102-100)/2.0

    def test_extension_ratio_is_none_without_a_structure_level(self):
        # Never gate/route on data we don't actually have.
        ratio = risk_manager._entry_extension_r(
            {"structure_level": None}, entry_price=500, side="BUY", risk_distance=2.0
        )
        self.assertIsNone(ratio)

    def test_extension_ratio_is_none_with_zero_risk_distance(self):
        ratio = risk_manager._entry_extension_r(
            {"structure_level": 98}, entry_price=100, side="BUY", risk_distance=0
        )
        self.assertIsNone(ratio)

    def test_within_the_cap_is_not_extended(self):
        with patch.object(config, "MAX_ENTRY_EXTENSION_R", 0.5):
            too_extended = risk_manager._entry_too_extended(0.4)

        self.assertFalse(too_extended)  # 0.4R <= 0.5R cap

    def test_beyond_the_cap_is_too_extended(self):
        with patch.object(config, "MAX_ENTRY_EXTENSION_R", 0.5):
            too_extended = risk_manager._entry_too_extended(1.0)

        self.assertTrue(too_extended)  # 1.0R > 0.5R cap

    def test_zero_cap_disables_the_check(self):
        with patch.object(config, "MAX_ENTRY_EXTENSION_R", 0):
            too_extended = risk_manager._entry_too_extended(100.0)

        self.assertFalse(too_extended)

    def test_none_extension_does_not_block(self):
        with patch.object(config, "MAX_ENTRY_EXTENSION_R", 0.5):
            too_extended = risk_manager._entry_too_extended(None)

        self.assertFalse(too_extended)

    def test_build_trade_plan_rejects_an_extended_entry(self):
        with patch.object(config, "MAX_ENTRY_EXTENSION_R", 0.5), \
             patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0):
            plan, status = risk_manager.build_trade_plan(
                {
                    "signal": "BUY", "symbol": "BTCUSDT", "entry_price": 100,
                    "structure_level": 98, "atr": 1,
                },
                balance=1000,
            )

        self.assertIsNone(plan)
        self.assertEqual(status, "ENTRY_TOO_EXTENDED")

    def test_build_trade_plan_allows_an_entry_within_the_cap(self):
        with patch.object(config, "MAX_ENTRY_EXTENSION_R", 2.0), \
             patch.object(config, "MAX_SL_ROI_PCT", 0), \
             patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(risk_manager, "calculate_position_size", return_value=10.0):
            plan, status = risk_manager.build_trade_plan(
                {
                    "signal": "BUY", "symbol": "BTCUSDT", "entry_price": 100,
                    "structure_level": 98, "atr": 1,
                },
                balance=1000,
            )

        self.assertEqual(status, "OK")

    def test_build_trade_plan_includes_entry_extension_r(self):
        # config.ENTRY_ROUTING_EXTENSION_THRESHOLD_R (main.py) reads this
        # off the plan to decide market vs. limit routing per-signal.
        with patch.object(config, "MAX_ENTRY_EXTENSION_R", 2.0), \
             patch.object(config, "MAX_SL_ROI_PCT", 0), \
             patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(risk_manager, "calculate_position_size", return_value=10.0):
            plan, status = risk_manager.build_trade_plan(
                {
                    "signal": "BUY", "symbol": "BTCUSDT", "entry_price": 100,
                    "structure_level": 98, "atr": 1,
                },
                balance=1000,
            )

        self.assertEqual(status, "OK")
        self.assertAlmostEqual(plan["entry_extension_r"], 1.0)  # (100-98)/2.0


class MaxSlRoiTests(unittest.TestCase):
    """See config.MAX_SL_ROI_PCT - real motivation (2026-08-14, operator
    feedback): risk-based sizing already caps the ACCOUNT-level $ loss per
    trade regardless of stop width, but the POSITION-level ROI% (PnL
    against margin used, what the exchange UI shows) is a different
    number entirely - ROI_at_SL = stop_distance_% * LEVERAGE, independent
    of position size (quantity cancels out of the ratio), and wasn't
    capped anywhere before this. Rejects outright, per explicit operator
    choice, rather than shrinking the position to fit."""

    def test_within_the_cap_is_not_too_high(self):
        # 2% stop distance * 10x leverage = 20% ROI, under a 30% cap.
        with patch.object(config, "MAX_SL_ROI_PCT", 30), \
             patch.object(config, "LEVERAGE", 10):
            too_high = risk_manager._stop_roi_too_high(risk_distance=2, entry_price=100)

        self.assertFalse(too_high)

    def test_beyond_the_cap_is_too_high(self):
        # 4% stop distance * 10x leverage = 40% ROI, over a 30% cap.
        with patch.object(config, "MAX_SL_ROI_PCT", 30), \
             patch.object(config, "LEVERAGE", 10):
            too_high = risk_manager._stop_roi_too_high(risk_distance=4, entry_price=100)

        self.assertTrue(too_high)

    def test_higher_leverage_lowers_the_stop_distance_that_trips_the_cap(self):
        # Same 2% stop distance, but 20x leverage -> 40% ROI, over the cap.
        with patch.object(config, "MAX_SL_ROI_PCT", 30), \
             patch.object(config, "LEVERAGE", 20):
            too_high = risk_manager._stop_roi_too_high(risk_distance=2, entry_price=100)

        self.assertTrue(too_high)

    def test_zero_cap_disables_the_check(self):
        with patch.object(config, "MAX_SL_ROI_PCT", 0), \
             patch.object(config, "LEVERAGE", 10):
            too_high = risk_manager._stop_roi_too_high(risk_distance=100, entry_price=100)

        self.assertFalse(too_high)

    def test_zero_entry_price_does_not_crash(self):
        with patch.object(config, "MAX_SL_ROI_PCT", 30):
            too_high = risk_manager._stop_roi_too_high(risk_distance=2, entry_price=0)

        self.assertFalse(too_high)

    def test_build_trade_plan_rejects_a_stop_with_too_high_an_roi(self):
        with patch.object(config, "MAX_SL_ROI_PCT", 30), \
             patch.object(config, "MAX_ENTRY_EXTENSION_R", 0), \
             patch.object(config, "LEVERAGE", 10), \
             patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0):
            plan, status = risk_manager.build_trade_plan(
                {
                    # 4% stop distance * 10x leverage = 40% ROI, over the cap.
                    "signal": "BUY", "symbol": "BTCUSDT", "entry_price": 100,
                    "structure_level": 96, "atr": 0,
                },
                balance=1000,
            )

        self.assertIsNone(plan)
        self.assertEqual(status, "SL_ROI_TOO_HIGH")

    def test_build_trade_plan_allows_a_stop_within_the_roi_cap(self):
        with patch.object(config, "MAX_SL_ROI_PCT", 30), \
             patch.object(config, "MAX_ENTRY_EXTENSION_R", 0), \
             patch.object(config, "LEVERAGE", 10), \
             patch.object(config, "STRUCTURE_STOP_ATR_BUFFER", 0), \
             patch.object(config, "TP1_R_MULTIPLE", 1.0), \
             patch.object(config, "TP2_R_MULTIPLE", 2.0), \
             patch.object(risk_manager, "calculate_position_size", return_value=10.0):
            plan, status = risk_manager.build_trade_plan(
                {
                    # 2% stop distance * 10x leverage = 20% ROI, under the cap.
                    "signal": "BUY", "symbol": "BTCUSDT", "entry_price": 100,
                    "structure_level": 98, "atr": 0,
                },
                balance=1000,
            )

        self.assertEqual(status, "OK")


class ConfluenceSizeMultiplierTests(unittest.TestCase):
    def test_disabled_config_always_returns_normal_multiplier(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", False):
            multiplier = risk_manager._confluence_size_multiplier({"confluence_ratio": 0.0})

        self.assertEqual(multiplier, 1.0)

    def test_none_ratio_returns_normal_multiplier(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True):
            multiplier = risk_manager._confluence_size_multiplier({"confluence_ratio": None})

        self.assertEqual(multiplier, 1.0)

    def test_full_ratio_returns_the_max_multiplier(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True), \
             patch.object(config, "CONFLUENCE_SIZING_MIN_MULTIPLIER", 0.5), \
             patch.object(config, "CONFLUENCE_SIZING_MAX_MULTIPLIER", 1.25):
            multiplier = risk_manager._confluence_size_multiplier({"confluence_ratio": 1.0})

        self.assertAlmostEqual(multiplier, 1.25)

    def test_zero_ratio_returns_the_min_multiplier(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True), \
             patch.object(config, "CONFLUENCE_SIZING_MIN_MULTIPLIER", 0.5), \
             patch.object(config, "CONFLUENCE_SIZING_MAX_MULTIPLIER", 1.25):
            multiplier = risk_manager._confluence_size_multiplier({"confluence_ratio": 0.0})

        self.assertAlmostEqual(multiplier, 0.5)

    def test_half_ratio_returns_the_midpoint(self):
        with patch.object(config, "CONFLUENCE_SIZING_ENABLED", True), \
             patch.object(config, "CONFLUENCE_SIZING_MIN_MULTIPLIER", 0.5), \
             patch.object(config, "CONFLUENCE_SIZING_MAX_MULTIPLIER", 1.25):
            multiplier = risk_manager._confluence_size_multiplier({"confluence_ratio": 0.5})

        self.assertAlmostEqual(multiplier, 0.875)


if __name__ == "__main__":
    unittest.main()
