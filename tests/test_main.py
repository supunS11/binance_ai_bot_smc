import unittest
from collections import Counter
from unittest.mock import patch

import config
import execution
import main
import risk_manager
import signal_engine
import signal_journal


class _FakeSnapshotSource:
    def snapshot(self, symbol):
        return {"available": False}


class _FakeCandleSource:
    def __init__(self, candles=None):
        self._candles = candles if candles is not None else [{"open_time": 0, "close": 1}]

    def get(self, symbol):
        return self._candles


class _FakeFeed:
    def __init__(self, ltf_candles=None, htf_candles=None, volumes=None):
        self.candles = _FakeCandleSource(ltf_candles)
        self.htf_candles = _FakeCandleSource(htf_candles)
        self.cvd = _FakeSnapshotSource()
        self.depth = _FakeSnapshotSource()
        self.open_interest = _FakeSnapshotSource()
        self.liquidations = _FakeSnapshotSource()
        self.volumes = volumes if volumes is not None else {}


class _FakePositions:
    def __init__(self, has_open=False, in_cooldown=False, count=0):
        self._has_open = has_open
        self._in_cooldown = in_cooldown
        self._count = count
        self.registered = []
        self.positions = {}

    def has_open_position(self, symbol):
        return self._has_open

    def is_in_cooldown(self, symbol):
        return self._in_cooldown

    def open_count(self):
        return self._count

    def mark_entry_failure(self, symbol):
        pass

    def register(self, plan, execution_result, trade_id=None):
        self.registered.append((plan, execution_result, trade_id))


class EvaluateSymbolRejectCountsTests(unittest.TestCase):
    """Real gap found live (2026-08-11): every rejection reason from
    signal_engine.evaluate() was silently discarded, so "no entries" was
    unexplainable from the logs alone - no way to tell "genuinely no
    qualifying setups yet" apart from "something is over-restrictive".
    These lock in that reject_counts actually captures the reason."""

    def test_missing_candle_data_is_tallied(self):
        feed = _FakeFeed(ltf_candles=[], htf_candles=[])
        positions = _FakePositions()
        reject_counts = Counter()

        main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts)

        self.assertEqual(reject_counts["NO_CANDLE_DATA"], 1)

    def test_signal_engine_rejection_reason_is_tallied(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()

        with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "NOT_IN_OTE"}):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts)

        self.assertEqual(reject_counts["NOT_IN_OTE"], 1)

    def test_missing_reason_falls_back_to_unknown(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()

        with patch.object(signal_engine, "evaluate", return_value={"signal": None}):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts)

        self.assertEqual(reject_counts["UNKNOWN"], 1)

    def test_plan_rejection_is_tallied_with_its_status(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()

        with patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}), \
             patch.object(risk_manager, "build_trade_plan", return_value=(None, "SL_TOO_TIGHT")):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts)

        self.assertEqual(reject_counts["PLAN_REJECTED:SL_TOO_TIGHT"], 1)

    def test_accepted_signal_does_not_touch_reject_counts(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()
        plan = {
            "symbol": "BTCUSDT", "entry_price": 100, "sl_price": 98,
            "tp1_price": 102, "tp2_price": 104,
        }

        with patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}), \
             patch.object(risk_manager, "build_trade_plan", return_value=(plan, "OK")), \
             patch.object(execution, "enter_trade", return_value={"ok": True, "shadow": True}), \
             patch.object(signal_journal, "append_signal", return_value="BTCUSDT_123"):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts)

        self.assertEqual(len(reject_counts), 0)
        self.assertEqual(len(positions.registered), 1)

    def test_quote_volume_is_passed_through_to_signal_engine(self):
        feed = _FakeFeed(volumes={"BTCUSDT": 42_000_000})
        positions = _FakePositions()

        with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "X"}) as evaluate_mock:
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter())

        _, kwargs = evaluate_mock.call_args
        self.assertEqual(kwargs["quote_volume_usdt"], 42_000_000)

    def test_missing_volume_data_is_passed_through_as_none(self):
        feed = _FakeFeed(volumes={})
        positions = _FakePositions()

        with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "X"}) as evaluate_mock:
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter())

        _, kwargs = evaluate_mock.call_args
        self.assertIsNone(kwargs["quote_volume_usdt"])

    def test_reject_counts_none_is_safe_and_does_not_raise(self):
        feed = _FakeFeed(ltf_candles=[], htf_candles=[])
        positions = _FakePositions()

        main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts=None)

    def test_reject_symbols_records_which_symbol_triggered_the_reason(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()
        reject_symbols = {}

        with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "NOT_IN_OTE"}):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts, reject_symbols)

        self.assertEqual(reject_symbols["NOT_IN_OTE"], ["BTCUSDT"])

    def test_reject_symbols_sample_is_capped_but_the_count_keeps_growing(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()
        reject_symbols = {}
        symbols = [f"SYM{i}USDT" for i in range(8)]

        with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "NOT_IN_OTE"}):
            for symbol in symbols:
                main._evaluate_symbol(feed, symbol, positions, 1000, reject_counts, reject_symbols)

        self.assertEqual(reject_counts["NOT_IN_OTE"], 8)
        self.assertEqual(len(reject_symbols["NOT_IN_OTE"]), main._MAX_REJECT_SAMPLE_SYMBOLS)

    def test_same_symbol_rejected_twice_is_not_duplicated_in_the_sample(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()
        reject_symbols = {}

        with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "NOT_IN_OTE"}):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts, reject_symbols)
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts, reject_symbols)

        self.assertEqual(reject_counts["NOT_IN_OTE"], 2)
        self.assertEqual(reject_symbols["NOT_IN_OTE"], ["BTCUSDT"])

    def test_reject_symbols_none_is_safe_and_does_not_raise(self):
        feed = _FakeFeed()
        positions = _FakePositions()

        with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "NOT_IN_OTE"}):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter(), reject_symbols=None)

    def test_operational_skips_are_not_tallied(self):
        # Routine skips (already has a position, cooldown, at capacity)
        # aren't signal-quality rejections - tallying them would dilute
        # the reason breakdown with noise that's expected, not actionable.
        feed = _FakeFeed()
        reject_counts = Counter()

        main._evaluate_symbol(feed, "BTCUSDT", _FakePositions(has_open=True), 1000, reject_counts)
        main._evaluate_symbol(feed, "BTCUSDT", _FakePositions(in_cooldown=True), 1000, reject_counts)
        main._evaluate_symbol(feed, "BTCUSDT", _FakePositions(count=999), 1000, reject_counts)

        self.assertEqual(len(reject_counts), 0)


class SignalStabilityTrackerTests(unittest.TestCase):
    """Real motivation (2026-08-12, live): IOTXUSDT was rejected for
    CVD_NOT_CONFIRMED, then passed 16 seconds later on a marginal score,
    then sat flat for 90+ minutes before losing - CVD is computed over 1m/
    5m/15m windows, so it can flip pass/fail within seconds, meaning a
    single-instant pass can be noise rather than genuine sustained order
    flow. These lock in that a signal must hold for
    config.SIGNAL_CONFIRM_TICKS consecutive calls before it's confirmed."""

    def test_first_call_is_not_yet_confirmed(self):
        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 3):
            tracker = main.SignalStabilityTracker()
            self.assertFalse(tracker.confirm("BTCUSDT", "BUY"))

    def test_confirmed_after_the_required_number_of_consecutive_calls(self):
        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 3):
            tracker = main.SignalStabilityTracker()
            self.assertFalse(tracker.confirm("BTCUSDT", "BUY"))
            self.assertFalse(tracker.confirm("BTCUSDT", "BUY"))
            self.assertTrue(tracker.confirm("BTCUSDT", "BUY"))

    def test_flipping_side_restarts_the_streak(self):
        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 3):
            tracker = main.SignalStabilityTracker()
            tracker.confirm("BTCUSDT", "BUY")
            tracker.confirm("BTCUSDT", "BUY")
            self.assertFalse(tracker.confirm("BTCUSDT", "SELL"))
            self.assertFalse(tracker.confirm("BTCUSDT", "SELL"))
            self.assertTrue(tracker.confirm("BTCUSDT", "SELL"))

    def test_reset_clears_the_streak(self):
        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 2):
            tracker = main.SignalStabilityTracker()
            tracker.confirm("BTCUSDT", "BUY")
            tracker.reset("BTCUSDT")
            self.assertFalse(tracker.confirm("BTCUSDT", "BUY"))

    def test_streaks_are_tracked_independently_per_symbol(self):
        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 2):
            tracker = main.SignalStabilityTracker()
            tracker.confirm("BTCUSDT", "BUY")
            self.assertFalse(tracker.confirm("ETHUSDT", "BUY"))
            self.assertTrue(tracker.confirm("BTCUSDT", "BUY"))

    def test_confirm_ticks_of_one_confirms_immediately(self):
        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 1):
            tracker = main.SignalStabilityTracker()
            self.assertTrue(tracker.confirm("BTCUSDT", "BUY"))


class EvaluateSymbolStabilityTests(unittest.TestCase):
    def _plan(self):
        return {
            "symbol": "BTCUSDT", "entry_price": 100, "sl_price": 98,
            "tp1_price": 102, "tp2_price": 104,
        }

    def test_signal_is_rejected_as_not_yet_stable_before_the_required_ticks(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()
        stability = main.SignalStabilityTracker()

        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 3), \
             patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts, {}, stability)

        self.assertEqual(reject_counts["SIGNAL_NOT_YET_STABLE"], 1)
        self.assertEqual(len(positions.registered), 0)

    def test_entry_fires_once_the_signal_has_held_for_enough_ticks(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter()
        stability = main.SignalStabilityTracker()

        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 3), \
             patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}), \
             patch.object(risk_manager, "build_trade_plan", return_value=(self._plan(), "OK")), \
             patch.object(execution, "enter_trade", return_value={"ok": True, "shadow": True}), \
             patch.object(signal_journal, "append_signal", return_value="BTCUSDT_123"):
            for _ in range(3):
                main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts, {}, stability)

        self.assertEqual(len(positions.registered), 1)
        # Only the first two ticks were rejected as not-yet-stable; the
        # third is the one that actually enters.
        self.assertEqual(reject_counts["SIGNAL_NOT_YET_STABLE"], 2)

    def test_a_gap_in_qualifying_resets_the_streak(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        stability = main.SignalStabilityTracker()

        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 2):
            with patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}):
                main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter(), {}, stability)

            with patch.object(signal_engine, "evaluate", return_value={"signal": None, "reason": "NOT_IN_OTE"}):
                main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter(), {}, stability)

            with patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}):
                # Would be the 2nd consecutive BUY (and confirm) if the gap
                # hadn't reset the streak - it's only the 1st again.
                main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter(), {}, stability)

        self.assertEqual(len(positions.registered), 0)

    def test_streak_resets_after_a_successful_entry(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        stability = main.SignalStabilityTracker()

        with patch.object(config, "SIGNAL_CONFIRM_TICKS", 1), \
             patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}), \
             patch.object(risk_manager, "build_trade_plan", return_value=(self._plan(), "OK")), \
             patch.object(execution, "enter_trade", return_value={"ok": True, "shadow": True}), \
             patch.object(signal_journal, "append_signal", return_value="BTCUSDT_123"):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter(), {}, stability)

        self.assertNotIn("BTCUSDT", stability._streaks)

    def test_stability_none_behaves_like_the_original_ungated_behavior(self):
        feed = _FakeFeed()
        positions = _FakePositions()

        with patch.object(signal_engine, "evaluate", return_value={"signal": "BUY"}), \
             patch.object(risk_manager, "build_trade_plan", return_value=(self._plan(), "OK")), \
             patch.object(execution, "enter_trade", return_value={"ok": True, "shadow": True}), \
             patch.object(signal_journal, "append_signal", return_value="BTCUSDT_123"):
            main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, Counter(), {}, stability=None)

        self.assertEqual(len(positions.registered), 1)


class LogHeartbeatRejectSummaryTests(unittest.TestCase):
    def test_reject_counts_are_logged_sorted_by_frequency(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter({"NOT_IN_OTE": 5, "CVD_NOT_CONFIRMED": 12, "NO_LIVE_STRUCTURE_BREAK": 80})

        with patch.object(main, "log_info") as log_mock:
            main._log_heartbeat(feed, ["BTCUSDT"], positions, reject_counts)

        logged = " ".join(call.args[0] for call in log_mock.call_args_list)
        self.assertIn("NO_LIVE_STRUCTURE_BREAK=80", logged)
        self.assertIn("CVD_NOT_CONFIRMED=12", logged)
        self.assertIn("NOT_IN_OTE=5", logged)
        # Most frequent reason appears before less frequent ones.
        self.assertLess(logged.index("NO_LIVE_STRUCTURE_BREAK=80"), logged.index("NOT_IN_OTE=5"))

    def test_empty_reject_counts_logs_no_summary_line(self):
        feed = _FakeFeed()
        positions = _FakePositions()

        with patch.object(main, "log_info") as log_mock:
            main._log_heartbeat(feed, ["BTCUSDT"], positions, Counter())

        logged = " ".join(call.args[0] for call in log_mock.call_args_list)
        self.assertNotIn("REJECTED", logged)

    def test_reject_counts_defaults_to_none_without_raising(self):
        feed = _FakeFeed()
        positions = _FakePositions()

        main._log_heartbeat(feed, ["BTCUSDT"], positions)

    def test_symbol_sample_is_included_next_to_its_reason(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter({"NOT_IN_OTE": 2})
        reject_symbols = {"NOT_IN_OTE": ["BTCUSDT", "ETHUSDT"]}

        with patch.object(main, "log_info") as log_mock:
            main._log_heartbeat(feed, ["BTCUSDT"], positions, reject_counts, reject_symbols)

        logged = " ".join(call.args[0] for call in log_mock.call_args_list)
        self.assertIn("NOT_IN_OTE=2[BTCUSDT,ETHUSDT]", logged)

    def test_truncated_sample_gets_an_ellipsis_marker(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter({"NO_LIVE_STRUCTURE_BREAK": 679})
        reject_symbols = {"NO_LIVE_STRUCTURE_BREAK": ["BTCUSDT", "ETHUSDT", "SOLUSDT"]}

        with patch.object(main, "log_info") as log_mock:
            main._log_heartbeat(feed, ["BTCUSDT"], positions, reject_counts, reject_symbols)

        logged = " ".join(call.args[0] for call in log_mock.call_args_list)
        self.assertIn("NO_LIVE_STRUCTURE_BREAK=679[BTCUSDT,ETHUSDT,SOLUSDT,...]", logged)

    def test_reason_without_a_sample_has_no_bracket_suffix(self):
        feed = _FakeFeed()
        positions = _FakePositions()
        reject_counts = Counter({"UNKNOWN": 3})

        with patch.object(main, "log_info") as log_mock:
            main._log_heartbeat(feed, ["BTCUSDT"], positions, reject_counts, reject_symbols=None)

        logged = " ".join(call.args[0] for call in log_mock.call_args_list)
        self.assertIn("UNKNOWN=3 ", logged + " ")
        self.assertNotIn("UNKNOWN=3[", logged)


if __name__ == "__main__":
    unittest.main()
