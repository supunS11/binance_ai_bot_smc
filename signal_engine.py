"""Combines HTF bias, LTF structure (CHoCH/BOS into an order block or FVG,
inside the OTE zone), liquidity-sweep confluence, and real-time order-flow
confirmation (CVD + depth imbalance) into a single entry signal.

This replaces v7/v8's strategy.py trend/confirm/entry cascade. The
difference isn't the concepts (structure + confirmation is the same
shape) - it's that every input here is live/event-driven off the websocket
feed, evaluated against the current forming candle, instead of only ever
looking at what already closed.

`evaluate()` always returns a dict with at least `signal` (None or
"BUY"/"SELL") and `reason` - callers that want to see *why* a candidate
was rejected (for the shadow journal) get that for free.
"""
import config
import liquidity_sweep
import market_structure


_BULLISH_TO_SIDE = {"BULLISH": "BUY", "BEARISH": "SELL"}


def _reject(reason, **extra):
    return {"signal": None, "reason": reason, **extra}


def evaluate(symbol, htf_candles, ltf_candles, cvd_snapshot, depth_snapshot):
    if not htf_candles or not ltf_candles:
        return _reject("INSUFFICIENT_CANDLES")

    htf_structure = market_structure.structure_state(htf_candles)

    if not htf_structure.get("available"):
        return _reject("HTF_STRUCTURE_UNAVAILABLE")

    zone = market_structure.premium_discount_zone(htf_candles)

    if not zone.get("available"):
        return _reject("ZONE_UNAVAILABLE")

    ltf_analysis = market_structure.analyze(ltf_candles)

    if not ltf_analysis.get("available"):
        return _reject("LTF_STRUCTURE_UNAVAILABLE")

    live_break = ltf_analysis["live_break"]

    if not live_break.get("broken"):
        return _reject("NO_LIVE_STRUCTURE_BREAK")

    direction = live_break["direction"]
    side = _BULLISH_TO_SIDE.get(direction)

    if side is None:
        return _reject("UNKNOWN_BREAK_DIRECTION")

    htf_side = _BULLISH_TO_SIDE.get(htf_structure.get("trend"))

    if htf_side and side != htf_side:
        return _reject(f"AGAINST_HTF_BIAS htf={htf_structure.get('trend')} ltf={direction}")

    latest_price = ltf_candles[-1]["close"]
    price_zone = market_structure.zone_for_price(zone, latest_price)

    if side == "BUY" and price_zone != "DISCOUNT":
        return _reject(f"NOT_IN_DISCOUNT price_zone={price_zone}")

    if side == "SELL" and price_zone != "PREMIUM":
        return _reject(f"NOT_IN_PREMIUM price_zone={price_zone}")

    if not market_structure.in_ote(zone, latest_price, direction):
        return _reject("NOT_IN_OTE")

    order_block = market_structure.find_order_block(
        ltf_candles, len(ltf_candles) - 1, direction
    )
    fvgs = ltf_analysis["fair_value_gaps"]
    matching_fvg = next(
        (gap for gap in reversed(fvgs) if gap["type"] == direction), None
    )

    if config.REQUIRE_ORDER_BLOCK_OR_FVG and not order_block and not matching_fvg:
        return _reject("NO_ORDER_BLOCK_OR_FVG")

    # Informational only, not gating: an EMA is a lagging/smoothed
    # indicator by construction, so requiring price to already be on its
    # correct side can delay entry on a sharp move until price has run
    # further - a real cost against this bot's real-time premise, and one
    # not yet backed by evidence it's worth paying. Computed and logged
    # here so that evidence can accumulate (same treatment as
    # sweep_confluence below) before this is ever turned into a hard gate.
    ema_value = None
    ema_aligned = None

    if config.EMA_CONFIRMATION_ENABLED:
        ema_value = market_structure.exponential_moving_average(ltf_candles)

        if ema_value is not None:
            ema_aligned = (
                latest_price > ema_value if side == "BUY" else latest_price < ema_value
            )

    if not cvd_snapshot.get("available"):
        return _reject("ORDER_FLOW_DATA_UNAVAILABLE")

    cvd_score = cvd_snapshot.get("cvd_score")

    if cvd_score is None:
        return _reject("ORDER_FLOW_SCORE_UNAVAILABLE")

    min_cvd = config.SIGNAL_MIN_CVD_SCORE

    if side == "BUY" and cvd_score < min_cvd:
        return _reject(f"CVD_NOT_CONFIRMED score={round(cvd_score, 4)} < {min_cvd}")

    if side == "SELL" and cvd_score > -min_cvd:
        return _reject(f"CVD_NOT_CONFIRMED score={round(cvd_score, 4)} > {-min_cvd}")

    depth_imbalance = None

    if depth_snapshot.get("available"):
        depth_imbalance = depth_snapshot.get("depth_imbalance", 0)
        min_depth = config.SIGNAL_MIN_DEPTH_IMBALANCE

        if side == "BUY" and depth_imbalance < -min_depth:
            return _reject(f"DEPTH_OPPOSING imbalance={depth_imbalance}")

        if side == "SELL" and depth_imbalance > min_depth:
            return _reject(f"DEPTH_OPPOSING imbalance={depth_imbalance}")

    pools = market_structure.find_liquidity_pools(
        market_structure.find_swing_points(ltf_candles)
    )
    sweep = liquidity_sweep.detect_sweep(ltf_candles, pools)
    sweep_confluence = bool(sweep and sweep["direction"] == direction)

    return {
        "signal": side,
        "reason": "OK",
        "symbol": symbol,
        "entry_price": latest_price,
        "htf_trend": htf_structure.get("trend"),
        "structure_level": live_break.get("level"),
        "order_block": order_block,
        "fvg": matching_fvg,
        "sweep_confluence": sweep_confluence,
        "cvd_score": cvd_score,
        "depth_imbalance": depth_imbalance,
        "atr": ltf_analysis.get("atr"),
        "premium_discount_zone": price_zone,
        "liquidity_pools": pools,
        "ema_value": ema_value,
        "ema_aligned": ema_aligned,
    }
