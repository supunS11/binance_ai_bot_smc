"""Real-time liquidity sweep / stop-hunt detector.

A sweep is price wicking through a marked liquidity pool (equal highs/lows
from market_structure.find_liquidity_pools) and rejecting back inside
before/at candle close - the classic "run the stops, then reverse"
pattern. Checked against the live, still-forming candle so it's caught as
it happens rather than after the candle closes.
"""


def detect_sweep(candles, pools):
    if not candles or not pools:
        return None

    latest = candles[-1]
    high = latest["high"]
    low = latest["low"]
    close = latest["close"]

    for pool in pools:
        level = pool["price"]

        if pool["type"] == "BUY_SIDE" and high > level and close < level:
            # Swept buy-side liquidity (stops above a high) and rejected
            # back down - bearish signal.
            return {
                "direction": "BEARISH",
                "level": level,
                "wick_size": high - level,
                "pool": pool,
            }

        if pool["type"] == "SELL_SIDE" and low < level and close > level:
            # Swept sell-side liquidity (stops below a low) and rejected
            # back up - bullish signal.
            return {
                "direction": "BULLISH",
                "level": level,
                "wick_size": level - low,
                "pool": pool,
            }

    return None
