"""Real-time liquidity sweep / stop-hunt detector.

A sweep is price wicking through a marked liquidity pool (equal highs/lows
from market_structure.find_liquidity_pools) and rejecting back inside
before/at candle close - the classic "run the stops, then reverse"
pattern.
"""
import config


def detect_sweep(candles, pools, require_closed_candle=None):
    """By default (require_closed_candle=False) this checks the *current,
    possibly still-forming* candle - real-time, catching the sweep as it
    happens rather than after the candle closes.

    When require_closed_candle is True (config.REQUIRE_CLOSE_CONFIRMED_BREAK
    - the same flag market_structure.live_break_check uses, reused here
    rather than a new one, since it's the same principle applied
    uniformly), this instead checks the most recently CLOSED candle. Real
    motivation (2026-08-13, two live trades traced against actual Binance
    price history): both entered on a still-forming candle's wick-and-
    reject that hadn't actually held by the time the candle finished
    forming - price resumed the original move and hit SL within minutes.
    Deliberately does NOT just test `candles[-1]["closed"]` - see
    live_break_check's docstring for why that would almost never fire."""
    if not candles or not pools:
        return None

    if require_closed_candle is None:
        require_closed_candle = config.REQUIRE_CLOSE_CONFIRMED_BREAK

    if require_closed_candle:
        closed_candles = [c for c in candles if c.get("closed")]

        if not closed_candles:
            return None

        latest = closed_candles[-1]
    else:
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
                "open_time": latest["open_time"],
            }

        if pool["type"] == "SELL_SIDE" and low < level and close > level:
            # Swept sell-side liquidity (stops below a low) and rejected
            # back up - bullish signal.
            return {
                "direction": "BULLISH",
                "level": level,
                "wick_size": level - low,
                "pool": pool,
                "open_time": latest["open_time"],
            }

    return None
