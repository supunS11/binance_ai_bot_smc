"""Turns a signal_engine candidate into concrete SL / TP1 / TP2 prices and
a position size.

The stop comes first - beyond the structure level that triggered the
entry, plus a small ATR buffer - and everything else derives from it:
position size is solved from the risk distance (the "1% rule", via
risk_management.calculate_position_size), and TP1/TP2 are expressed as
R-multiples of that same distance. This mirrors v7's ordering (stop
distance drives size) rather than picking a size first and hoping the
stop fits.
"""
import config
from risk_management import calculate_position_size


def compute_stop_loss(signal, side):
    level = signal.get("structure_level")

    if level is None:
        return None

    atr = signal.get("atr") or 0
    buffer = atr * max(float(config.STRUCTURE_STOP_ATR_BUFFER), 0)

    if side == "BUY":
        return level - buffer

    return level + buffer


def compute_targets(entry_price, sl_price, side):
    risk_distance = abs(entry_price - sl_price)

    if risk_distance <= 0:
        return None, None

    tp1_multiple = max(float(config.TP1_R_MULTIPLE), 0)
    tp2_multiple = max(float(config.TP2_R_MULTIPLE), tp1_multiple)

    if side == "BUY":
        return (
            entry_price + risk_distance * tp1_multiple,
            entry_price + risk_distance * tp2_multiple,
        )

    return (
        entry_price - risk_distance * tp1_multiple,
        entry_price - risk_distance * tp2_multiple,
    )


def compute_breakeven_price(entry_price, side):
    """A hair beyond flat entry, not exactly at it - so the breakeven stop
    still covers exchange fees/slippage instead of being a guaranteed
    zero-sum exit."""
    buffer_pct = max(float(config.BREAKEVEN_BUFFER_PCT), 0) / 100

    if side == "BUY":
        return entry_price * (1 + buffer_pct)

    return entry_price * (1 - buffer_pct)


def build_trade_plan(signal, balance):
    """`signal` is a dict from signal_engine.evaluate() where
    signal["signal"] is "BUY" or "SELL" (never call this for a rejected
    candidate). Returns (plan_dict_or_None, status_reason)."""
    side = signal["signal"]
    symbol = signal["symbol"]
    entry_price = float(signal["entry_price"])

    if entry_price <= 0:
        return None, "INVALID_ENTRY_PRICE"

    sl_price = compute_stop_loss(signal, side)

    if sl_price is None or sl_price <= 0:
        return None, "SL_UNAVAILABLE"

    if side == "BUY" and sl_price >= entry_price:
        return None, "SL_ON_WRONG_SIDE"

    if side == "SELL" and sl_price <= entry_price:
        return None, "SL_ON_WRONG_SIDE"

    tp1_price, tp2_price = compute_targets(entry_price, sl_price, side)

    if tp1_price is None:
        return None, "TARGETS_UNAVAILABLE"

    quantity = calculate_position_size(balance, entry_price, sl_price, symbol)

    if quantity <= 0:
        return None, "POSITION_SIZE_ZERO"

    tp1_close_pct = min(max(float(config.TP1_CLOSE_PCT), 0), 100)
    tp1_quantity = round(quantity * tp1_close_pct / 100, 8)
    tp2_quantity = round(quantity - tp1_quantity, 8)

    if tp1_quantity <= 0 or tp2_quantity <= 0:
        return None, "TP_SPLIT_INVALID"

    return {
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "breakeven_price": compute_breakeven_price(entry_price, side),
        "quantity": quantity,
        "tp1_quantity": tp1_quantity,
        "tp2_quantity": tp2_quantity,
        "risk_distance": abs(entry_price - sl_price),
    }, "OK"
