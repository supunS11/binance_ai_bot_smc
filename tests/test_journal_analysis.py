import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import signal_journal
import journal_analysis as ja


def _write_trade(path, trade_id, symbol, outcome=None, mae_r_multiple=None, mfe_r_multiple=None, **signal_fields):
    """Writes a signal row (and optionally its outcome row) directly via
    the real signal_journal writer, so these tests exercise the exact
    on-disk format the analysis code has to parse."""
    fields = {
        "symbol": symbol,
        "signal": "BUY",
        "htf_trend": "BULLISH",
        "structure_level": 98.0,
        "atr": 1.0,
        "premium_discount_zone": "DISCOUNT",
        "order_block": None,
        "fvg": None,
        "cvd_score": 0.5,
        "depth_imbalance": 0.1,
        "sweep_confluence": False,
    }
    fields.update(signal_fields)
    plan = {
        "entry_price": 100.0, "sl_price": 98.0, "tp1_price": 102.0,
        "tp2_price": 104.0, "quantity": 1.0, "risk_distance": 2.0,
    }

    with patch.object(signal_journal, "JOURNAL_PATH", path), \
         patch.object(signal_journal, "_make_trade_id", return_value=trade_id):
        signal_journal.append_signal(fields, plan)

        if outcome:
            signal_journal.append_outcome(
                symbol, outcome, trade_id,
                mae_r_multiple=mae_r_multiple, mfe_r_multiple=mfe_r_multiple,
            )


def _write_outcome_only(path, trade_id, symbol, outcome, mae_r_multiple=None, mfe_r_multiple=None):
    """Simulates a trade with no matching signal row at all - exactly
    what a startup-reconciliation-adopted position looks like on disk
    (real bug found live, 2026-08-09: these showed "unknown" for every
    field, including symbol, before load_trades() was fixed to merge
    outcome-row fields too)."""
    with patch.object(signal_journal, "JOURNAL_PATH", path):
        signal_journal.append_outcome(
            symbol, outcome, trade_id,
            mae_r_multiple=mae_r_multiple, mfe_r_multiple=mfe_r_multiple,
        )


class ClassifyTests(unittest.TestCase):
    def test_sl_hit_variants_are_loss(self):
        self.assertEqual(ja.classify("SL_HIT"), "LOSS")
        self.assertEqual(ja.classify("SHADOW_SL_HIT"), "LOSS")

    def test_breakeven_variants_are_breakeven(self):
        self.assertEqual(ja.classify("BREAKEVEN_STOP_HIT"), "BREAKEVEN")
        self.assertEqual(ja.classify("BREAKEVEN_TRIGGER_MARKET_CLOSE"), "BREAKEVEN")

    def test_tp2_variants_are_win(self):
        self.assertEqual(ja.classify("TP2_HIT"), "WIN")
        self.assertEqual(ja.classify("SHADOW_TP2_HIT"), "WIN")

    def test_unknown_outcome_is_unknown(self):
        self.assertEqual(ja.classify("SOMETHING_ELSE"), "UNKNOWN")


class LoadTradesAndSummarizeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self.tmpdir.name) / "journal.csv"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_empty_journal_returns_empty_dict(self):
        self.assertEqual(ja.load_trades(self.journal_path), {})

    def test_signal_and_outcome_rows_merge_by_trade_id(self):
        _write_trade(self.journal_path, "T1", "BTCUSDT", outcome="SL_HIT", cvd_score=0.8)

        trades = ja.load_trades(self.journal_path)

        self.assertIn("T1", trades)
        self.assertEqual(trades["T1"]["outcome"], "SL_HIT")
        self.assertEqual(trades["T1"]["symbol"], "BTCUSDT")
        self.assertEqual(trades["T1"]["cvd_score"], "0.8")

    def test_unresolved_trade_has_no_outcome_key_populated(self):
        _write_trade(self.journal_path, "T2", "ETHUSDT")
        trades = ja.load_trades(self.journal_path)
        self.assertNotIn("outcome", trades.get("T2", {}))

    def test_summarize_with_no_resolved_trades(self):
        _write_trade(self.journal_path, "T3", "ETHUSDT")
        report = ja.summarize(self.journal_path)
        self.assertIn("No resolved", report)

    def test_summarize_counts_wins_and_losses_correctly(self):
        _write_trade(self.journal_path, "A", "BTCUSDT", outcome="TP2_HIT", cvd_score=0.9, sweep_confluence=True)
        _write_trade(self.journal_path, "B", "ETHUSDT", outcome="SL_HIT", cvd_score=0.1, sweep_confluence=False)
        _write_trade(self.journal_path, "C", "BTCUSDT", outcome="SL_HIT", cvd_score=0.15, sweep_confluence=False)

        report = ja.summarize(self.journal_path)

        self.assertIn("Resolved trades: 3", report)
        self.assertIn("WIN=1", report)
        self.assertIn("LOSS=2", report)
        # The weak-CVD bucket should show the 2/2 loss rate concentration.
        self.assertIn("weak (<0.3): n=2", report)
        self.assertIn("loss_rate=100%", report)

    def test_outcome_only_trade_still_gets_its_symbol_from_the_outcome_row(self):
        # Real bug found live: a startup-reconciliation-adopted position
        # (no matching signal row) used to show "unknown" for every
        # field, including symbol, even though the outcome row had it.
        _write_outcome_only(self.journal_path, "SYM_RECOVERED_1", "BTCUSDT", "SL_HIT")

        trades = ja.load_trades(self.journal_path)

        self.assertEqual(trades["SYM_RECOVERED_1"]["symbol"], "BTCUSDT")
        self.assertEqual(trades["SYM_RECOVERED_1"]["outcome"], "SL_HIT")

    def test_mae_mfe_are_merged_from_the_outcome_row(self):
        _write_trade(self.journal_path, "T4", "BTCUSDT", outcome="SL_HIT", mae_r_multiple=1.5, mfe_r_multiple=0.5)

        trades = ja.load_trades(self.journal_path)

        self.assertEqual(trades["T4"]["mae_r_multiple"], "1.5")
        self.assertEqual(trades["T4"]["mfe_r_multiple"], "0.5")

    def test_signal_time_fields_are_not_clobbered_by_the_blank_outcome_row(self):
        _write_trade(self.journal_path, "T5", "BTCUSDT", outcome="TP2_HIT", cvd_score=0.77)

        trades = ja.load_trades(self.journal_path)

        # The outcome row's cvd_score field is blank - must not overwrite
        # the real value the signal row already wrote.
        self.assertEqual(trades["T5"]["cvd_score"], "0.77")

    def test_summarize_includes_average_mae_mfe_by_outcome(self):
        _write_trade(self.journal_path, "A", "BTCUSDT", outcome="SL_HIT", mae_r_multiple=1.5, mfe_r_multiple=0.5)
        _write_trade(self.journal_path, "B", "ETHUSDT", outcome="SL_HIT", mae_r_multiple=1.0, mfe_r_multiple=0.1)
        _write_trade(self.journal_path, "C", "BTCUSDT", outcome="TP2_HIT", mae_r_multiple=0.2, mfe_r_multiple=4.0)

        report = ja.summarize(self.journal_path)

        self.assertIn("Average MAE (adverse excursion) by outcome:", report)
        self.assertIn("LOSS: avg=1.250R n=2", report)  # (1.5+1.0)/2
        self.assertIn("Average MFE (favorable excursion) by outcome:", report)
        self.assertIn("WIN: avg=4.000R n=1", report)


if __name__ == "__main__":
    unittest.main()
