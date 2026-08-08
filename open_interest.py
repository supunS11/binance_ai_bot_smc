"""Open interest tracking via REST polling (Binance has no public OI
websocket stream) - the confirmation layer for whether a directional
break is fresh positioning or just the opposite side closing out. See
config.OI_CONFIRMATION_ENABLED for the informational-only rationale.
"""
import threading
import time
from collections import deque

import config


class OpenInterestEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self._samples = {}  # symbol -> deque[(timestamp, oi_value)]

    def _series(self, symbol):
        series = self._samples.get(symbol)

        if series is None:
            series = deque(maxlen=max(int(config.OI_HISTORY_MAX_SAMPLES), 2))
            self._samples[symbol] = series

        return series

    def record(self, symbol, oi_value, timestamp=None):
        if oi_value is None or oi_value < 0:
            return

        symbol = symbol.upper()
        timestamp = time.time() if timestamp is None else float(timestamp)

        with self.lock:
            self._series(symbol).append((timestamp, float(oi_value)))

    def snapshot(self, symbol, now=None):
        symbol = symbol.upper()
        now = time.time() if now is None else now
        lookback = max(int(config.OI_LOOKBACK_SECONDS), 1)

        with self.lock:
            series = list(self._samples.get(symbol, ()))

        if not series:
            return {
                "available": False,
                "symbol": symbol,
                "oi_value": None,
                "oi_change_pct": None,
                "sample_count": 0,
            }

        latest_value = series[-1][1]

        if len(series) < 2:
            return {
                "available": True,
                "symbol": symbol,
                "oi_value": latest_value,
                "oi_change_pct": None,
                "sample_count": 1,
            }

        cutoff = now - lookback
        baseline_value = next((value for ts, value in series if ts >= cutoff), latest_value)
        oi_change_pct = (
            round((latest_value - baseline_value) / baseline_value * 100, 4)
            if baseline_value
            else None
        )

        return {
            "available": True,
            "symbol": symbol,
            "oi_value": latest_value,
            "oi_change_pct": oi_change_pct,
            "sample_count": len(series),
        }

    def reset(self, symbol=None):
        with self.lock:
            if symbol is None:
                self._samples.clear()
            else:
                self._samples.pop(symbol.upper(), None)
