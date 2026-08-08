import unittest
from unittest.mock import patch

import pandas as pd

import config
from ws_client import CandleStore, RealtimeMarketData


class CandleStoreTests(unittest.TestCase):
    def test_seed_loads_history_as_closed_candles(self):
        store = CandleStore(maxlen=50)
        df = pd.DataFrame([
            {"time": 1000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
            {"time": 2000, "open": 1.5, "high": 2.5, "low": 1, "close": 2, "volume": 12},
        ])

        store.seed("btcusdt", df)
        candles = store.get("BTCUSDT")

        self.assertEqual(len(candles), 2)
        self.assertTrue(all(c["closed"] for c in candles))
        self.assertEqual(candles[-1]["close"], 2)

    def test_update_with_same_open_time_replaces_forming_candle(self):
        store = CandleStore(maxlen=50)
        store.update("BTCUSDT", {
            "open_time": 1000, "open": 1, "high": 1, "low": 1, "close": 1,
            "volume": 1, "closed": False,
        })
        store.update("BTCUSDT", {
            "open_time": 1000, "open": 1, "high": 1.2, "low": 0.9, "close": 1.1,
            "volume": 3, "closed": False,
        })

        candles = store.get("BTCUSDT")

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["close"], 1.1)
        self.assertFalse(candles[0]["closed"])

    def test_update_with_new_open_time_appends_new_candle(self):
        store = CandleStore(maxlen=50)
        store.update("BTCUSDT", {
            "open_time": 1000, "open": 1, "high": 1, "low": 1, "close": 1,
            "volume": 1, "closed": True,
        })
        store.update("BTCUSDT", {
            "open_time": 2000, "open": 1, "high": 1, "low": 1, "close": 1.5,
            "volume": 1, "closed": False,
        })

        candles = store.get("BTCUSDT")

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["closed"], True)
        self.assertEqual(candles[-1]["closed"], False)

    def test_latest_returns_none_for_unknown_symbol(self):
        store = CandleStore(maxlen=50)
        self.assertIsNone(store.latest("NOPE"))


class RealtimeMarketDataMessageHandlingTests(unittest.TestCase):
    """These exercise the pure message-parsing/routing logic without ever
    opening a real socket (start()/connect() are never called)."""

    def _feed(self):
        return RealtimeMarketData(["BTCUSDT"])

    def test_handle_kline_updates_candle_store(self):
        feed = self._feed()
        feed._handle_kline({
            "s": "BTCUSDT",
            "k": {
                "t": 1000, "o": "1", "h": "2", "l": "0.5", "c": "1.8",
                "v": "10", "x": False,
            },
        })

        latest = feed.candles.latest("BTCUSDT")

        self.assertIsNotNone(latest)
        self.assertEqual(latest["close"], 1.8)
        self.assertFalse(latest["closed"])

    def test_handle_kline_missing_fields_is_ignored_not_raised(self):
        feed = self._feed()
        feed._handle_kline({"s": "BTCUSDT", "k": {}})

        self.assertIsNone(feed.candles.latest("BTCUSDT"))

    def test_handle_agg_trade_feeds_cvd_engine(self):
        feed = self._feed()
        feed._handle_agg_trade({"s": "BTCUSDT", "p": "100", "q": "1", "m": False, "T": 1000000})

        with patch.object(config, "ORDER_FLOW_MIN_NOTIONAL_USDT", 0):
            snapshot = feed.cvd.snapshot("BTCUSDT", now=1001)

        self.assertTrue(snapshot["available"])
        self.assertGreater(snapshot["ratio_1m"], 0)

    def test_handle_depth_message_feeds_orderbook_engine(self):
        feed = self._feed()
        feed._handle_depth_message({
            "s": "BTCUSDT",
            "b": [["100", "10"]],
            "a": [["101", "1"]],
        })

        snapshot = feed.depth.snapshot("BTCUSDT")

        self.assertTrue(snapshot["available"])
        self.assertGreater(snapshot["depth_imbalance"], 0)

    def test_worker_active_false_after_stop_event_set(self):
        feed = self._feed()
        generation = feed.generation
        feed.stop_event.set()

        self.assertFalse(feed._worker_active(generation))

    def test_worker_active_false_for_stale_generation(self):
        feed = self._feed()
        generation = feed.generation
        feed.generation += 1

        self.assertFalse(feed._worker_active(generation))


if __name__ == "__main__":
    unittest.main()
