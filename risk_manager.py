"""Turns a signal_engine candidate into concrete SL / TP1 / TP2 prices and
a position size.

The stop comes first - beyond the structure level that triggered the
entry, plus a small ATR buffer - and everything else derives from it:
position size is solved from the risk distance (the "1% rule", via
risk_management.calculate_position_size).

TP1/TP2 target real liquidity pools when one exists with enough room,
matching v7's find_structure_take_profit (price is drawn toward real
liquidity, not an arbitrary multiple of the stop - that's the actual
premise of SMC). The R-multiple (2R/4R) is a floor, not the primary
target: it sets the *minimum* acceptable distance a structure target must
clear, and is used directly as a fallback only when no real level exists
out that far - same shape as v7's ENTRY_MIN_TP_ROOM_ROI/STRUCTURE_TP_MIN_ROI
fallback.
"""
import config
from risk_management import calculate_position_size, get_position_risk_budget


def _find_structure_target(pools, entry_price, side, min_r_multiple, max_r_multiple, risk_distance):
    """Nearest real liquidity-pool price in the trade's favorable
    direction that clears `min_r_multiple` R of room *and* stays within
    `max_r_multiple` R, or None if no pool qualifies. The max bound
    matters: without it, the "nearest qualifying pool" can still be
    absurdly far away if nothing closer exists (a real case seen live -
    a ~20R target that's realistically never going to be reached, which
    defeats the point of TP1 as an achievable first partial). BUY targets
    BUY_SIDE pools (resistance above entry - where breakout-buy stops
    sit); SELL targets SELL_SIDE pools (support below entry - where long
    stops sit) - the same pools liquidity_sweep.py already uses, just on
    the opposite side of price from where a sweep would be found."""
    if risk_distance <= 0:
        return None

    min_distance = min_r_multiple * risk_distance
    max_distance = max_r_multiple * risk_distance if max_r_multiple else None
    pool_type = "BUY_SIDE" if side == "BUY" else "SELL_SIDE"
    candidates = []

    for pool in pools or []:
        price = pool.get("price")

        if price is None or pool.get("type") != pool_type:
            continue

        distance = (price - entry_price) if side == "BUY" else (entry_price - price)

        if distance < min_distance:
            continue

        if max_distance is not None and distance > max_distance:
            continue

        candidates.append((distance, price))

    if not candidates:
        return None

    candidates.sort(key=lambda candidate: candidate[0])
    return candidates[0][1]


def _resolve_target(pools, entry_price, side, min_r_multiple, max_r_multiple, risk_distance):
    structure_price = _find_structure_target(
        pools, entry_price, side, min_r_multiple, max_r_multiple, risk_distance
    )

    if structure_price is not None:
        return structure_price

    distance = min_r_multiple * risk_distance
    return entry_price + distance if side == "BUY" else entry_price - distance


def _apply_min_stop_distance(sl_price, entry_price, side):
    """Structure can occasionally land pathologically close to entry - a
    fast/noisy market, or a tight fractal window finding a swing point
    right next to current price. Left alone, that produces a stop that's
    essentially inside normal noise (gets hit immediately) and, because
    position size is solved from the stop distance, an oversized position
    to match. Widen the stop out to a minimum distance from entry rather
    than let one through at a size ordinary noise will trigger."""
    entry_price = float(entry_price or 0)
    min_pct = max(float(config.MIN_STOP_DISTANCE_PCT), 0) / 100

    if entry_price <= 0 or min_pct <= 0:
        return sl_price

    min_distance = entry_price * min_pct

    if abs(entry_price - sl_price) >= min_distance:
        return sl_price

    return entry_price - min_distance if side == "BUY" else entry_price + min_distance


def compute_stop_loss(signal, side):
    level = signal.get("structure_level")

    if level is None:
        return None

    atr = signal.get("atr") or 0
    buffer = atr * max(float(config.STRUCTURE_STOP_ATR_BUFFER), 0)
    sl_price = level - buffer if side == "BUY" else level + buffer

    return _apply_min_stop_distance(sl_price, signal.get("entry_price"), side)


def compute_targets(entry_price, sl_price, side, pools=None):
    risk_distance = abs(entry_price - sl_price)

    if risk_distance <= 0:
        return None, None

    tp1_min = max(float(config.TP1_R_MULTIPLE), 0)
    tp2_min = max(float(config.TP2_R_MULTIPLE), tp1_min)
    tp1_max = max(float(config.TP1_MAX_R_MULTIPLE), tp1_min)
    tp2_max = max(float(config.TP2_MAX_R_MULTIPLE), tp2_min)

    tp1_price = _resolve_target(pools, entry_price, side, tp1_min, tp1_max, risk_distance)
    tp1_actual_multiple = abs(tp1_price - entry_price) / risk_distance

    # TP2 must clear whichever is further out: the configured floor, or
    # at least 1R beyond wherever TP1 actually landed - guarantees TP2
    # always sits meaningfully beyond TP1 regardless of whether either
    # came from a real structure level or the fallback. Capped at its own
    # max the same way TP1 is, for the same reason.
    tp2_min_multiple = min(max(tp2_min, tp1_actual_multiple + 1.0), tp2_max)
    tp2_price = _resolve_target(pools, entry_price, side, tp2_min_multiple, tp2_max, risk_distance)

    return tp1_price, tp2_price


def compute_breakeven_price(entry_price, side):
    """A hair beyond flat entry, not exactly at it - so the breakeven stop
    still covers exchange fees/slippage instead of being a guaranteed
    zero-sum exit."""
    buffer_pct = max(float(config.BREAKEVEN_BUFFER_PCT), 0) / 100

    if side == "BUY":
        return entry_price * (1 + buffer_pct)

    return entry_price * (1 - buffer_pct)


def _confluence_size_multiplier(signal):
    """Scales risk per trade by how much of sweep/EMA/OI/liquidation
    confluence agrees with this signal (signal_engine's confluence_ratio),
    instead of gating entry on any of them individually - every signal
    that reaches build_trade_plan still trades, only the size adapts.
    Linear between CONFLUENCE_SIZING_MIN_MULTIPLIER (no confluence) and
    CONFLUENCE_SIZING_MAX_MULTIPLIER (full confluence). See
    config.CONFLUENCE_SIZING_ENABLED for the rationale."""
    if not config.CONFLUENCE_SIZING_ENABLED:
        return 1.0

    ratio = signal.get("confluence_ratio")

    if ratio is None:
        return 1.0

    min_mult = float(config.CONFLUENCE_SIZING_MIN_MULTIPLIER)
    max_mult = float(config.CONFLUENCE_SIZING_MAX_MULTIPLIER)
    return min_mult + (max_mult - min_mult) * ratio


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

    tp1_price, tp2_price = compute_targets(
        entry_price, sl_price, side, pools=signal.get("liquidity_pools")
    )

    if tp1_price is None:
        return None, "TARGETS_UNAVAILABLE"

    size_multiplier = _confluence_size_multiplier(signal)

    if config.RISK_BASED_POSITION_SIZING_ENABLED:
        risk_budget = get_position_risk_budget(balance) * size_multiplier
        quantity = calculate_position_size(
            balance, entry_price, sl_price, symbol, risk_budget_override=risk_budget
        )
    else:
        margin = max(float(config.MARGIN_PER_TRADE), 0) * size_multiplier
        quantity = calculate_position_size(
            balance, entry_price, sl_price, symbol, margin_override=margin
        )

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
        "size_multiplier": size_multiplier,
    }, "OK"
