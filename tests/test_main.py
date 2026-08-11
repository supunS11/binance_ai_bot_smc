import unittest
from collections import Counter
from unittest.mock import patch

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
    def __init__(self, ltf_candles=None, htf_candles=None):
        self.candles = _FakeCandleSource(ltf_candles)
        self.htf_candles = _FakeCandleSource(htf_candles)
        self.cvd = _FakeSnapshotSource()
        self.depth = _FakeSnapshotSource()
        self.open_interest = _FakeSnapshotSource()
        self.liquidations = _FakeSnapshotSource()


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

    def test_reject_counts_none_is_safe_and_does_not_raise(self):
        feed = _FakeFeed(ltf_candles=[], htf_candles=[])
        positions = _FakePositions()

        main._evaluate_symbol(feed, "BTCUSDT", positions, 1000, reject_counts=None)

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


if __name__ == "__main__":
    unittest.main()
