"""Cumulative volume delta (CVD) from the real-time aggTrade tape.

This is the order-flow confirmation layer: SMC structure (market_structure.py,
built in Phase 2) tells us *where* a setup is; CVD tells us whether real
aggressive buying/selling is actually behind it, which is what separates a
genuine order-block/FVG signal from a false one instead of just waiting for
a candle to close and hoping.
"""
import threading
import time
from collections import deque

import config


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class CVDEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self._trades = {}  # symbol -> deque[(timestamp, signed_notional, notional)]

    def _series(self, symbol):
        series = self._trades.get(symbol)

        if series is None:
            series = deque()
            self._trades[symbol] = series

        return series

    def record_trade(self, symbol, price, quantity, is_buyer_maker, timestamp=None):
        """`is_buyer_maker=True` means the resting order was a buy and the
        aggressor was a seller (Binance's aggTrade convention) - so a
        buyer-maker trade is signed negative (sell pressure) here."""
        price = _safe_float(price)
        quantity = _safe_float(quantity)

        if price <= 0 or quantity <= 0:
            return

        symbol = symbol.upper()
        timestamp = time.time() if timestamp is None else float(timestamp)
        notional = price * quantity
        signed_notional = -notional if is_buyer_maker else notional
        max_window = max(int(config.ORDER_FLOW_MAX_WINDOW_SECONDS), 60)

        with self.lock:
            series = self._series(symbol)
            series.append((timestamp, signed_notional, notional))
            cutoff = timestamp - max_window

            while series and series[0][0] < cutoff:
                series.popleft()

    def snapshot(self, symbol, windows=(60, 300, 900), now=None):
        symbol = symbol.upper()
        now = time.time() if now is None else now
        min_notional = max(float(config.ORDER_FLOW_MIN_NOTIONAL_USDT), 0)

        with self.lock:
            series = list(self._trades.get(symbol, ()))

        if not series:
            return {"available": False, "symbol": symbol}

        ratios = {}
        notionals = {}

        for window in windows:
            cutoff = now - window
            selected = [trade for trade in series if trade[0] >= cutoff]
            total = sum(trade[2] for trade in selected)
            signed = sum(trade[1] for trade in selected)
            ratios[window] = (signed / total) if total >= min_notional else None
            notionals[window] = total

        weighted_parts = [
            (ratios[window], weight)
            for window, weight in zip(windows, (0.5, 0.3, 0.2))
            if ratios.get(window) is not None
        ]
        weight_total = sum(weight for _, weight in weighted_parts)
        cvd_score = (
            sum(value * weight for value, weight in weighted_parts) / weight_total
            if weight_total > 0
            else None
        )

        return {
            "available": cvd_score is not None,
            "symbol": symbol,
            "cvd_score": round(cvd_score, 4) if cvd_score is not None else None,
            "ratio_1m": ratios.get(windows[0]),
            "ratio_5m": ratios.get(windows[1]) if len(windows) > 1 else None,
            "ratio_15m": ratios.get(windows[2]) if len(windows) > 2 else None,
            "notional_1m": round(notionals.get(windows[0], 0), 2),
            "sample_count": len(series),
        }

    def reset(self, symbol=None):
        with self.lock:
            if symbol is None:
                self._trades.clear()
            else:
                self._trades.pop(symbol.upper(), None)
