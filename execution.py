"""Places the entry, then attaches SL + TP1 + TP2 using the same order
mechanics v7/v8 use: a full-position STOP_MARKET (closePosition=true) for
the stop, a reduce-only TAKE_PROFIT_MARKET for TP1's partial quantity, and
a full-position TAKE_PROFIT_MARKET (closePosition=true) for TP2 covering
whatever remains after TP1.

Defaults to SHADOW mode (config.EXECUTION_MODE) - real order placement
only happens once that's explicitly switched to LIVE, so the very first
run of this bot cannot place a real order by accident.
"""
import config
import exchange
from logger import log_error, log_info, log_warning


def enter_trade(plan):
    symbol = plan["symbol"]
    side = plan["side"]

    if config.EXECUTION_MODE != "LIVE":
        log_info(
            f"[SHADOW] {symbol} would enter {side} qty={plan['quantity']} "
            f"entry~={plan['entry_price']} SL={plan['sl_price']} "
            f"TP1={plan['tp1_price']} TP2={plan['tp2_price']}"
        )
        return {
            "ok": True,
            "shadow": True,
            "entry_order": None,
            "sl_order": None,
            "tp1_order": None,
            "tp2_order": None,
        }

    # Entry + SL are treated as one atomic unit: once the entry order
    # fills, a real position exists on the exchange, so a failure from
    # here on must never be allowed to leave it both naked (no stop) and
    # untracked (main.py only registers the position when this returns
    # ok=True) - that combination is how a position silently loses its
    # protection with the bot having no idea it exists.
    try:
        exchange.setup_leverage(symbol)
        entry_order = exchange.place_market_order(symbol, side, plan["quantity"])
    except Exception as exc:
        log_error(f"{symbol} entry order error: {exc}")
        return {"ok": False, "shadow": False, "error": str(exc)}

    try:
        sl_order = exchange.place_stop_loss(symbol, side, plan["sl_price"])
    except Exception as exc:
        log_error(
            f"{symbol} SL placement failed after entry filled - closing "
            f"position at market rather than leave it unprotected: {exc}"
        )
        try:
            exchange.close_position_market(symbol, side, plan["quantity"])
        except Exception as close_exc:
            log_error(
                f"{symbol} CRITICAL: failed to close unprotected position "
                f"after SL placement failure - manual intervention needed: "
                f"{close_exc}"
            )
        return {"ok": False, "shadow": False, "error": f"SL placement failed: {exc}"}

    # TP1/TP2 are best-effort from here: the position already has real
    # downside protection via SL, so a failure here is degraded (missing
    # profit-taking) but not dangerous - the position still gets tracked.
    tp1_order = None
    tp2_order = None

    try:
        tp1_order = exchange.place_take_profit_partial(
            symbol, side, plan["tp1_quantity"], plan["tp1_price"]
        )
    except Exception as exc:
        log_warning(f"{symbol} TP1 placement failed (SL is active): {exc}")

    try:
        tp2_order = exchange.place_take_profit_full(symbol, side, plan["tp2_price"])
    except Exception as exc:
        log_warning(f"{symbol} TP2 placement failed (SL is active): {exc}")

    log_info(
        f"{symbol} entered {side} qty={plan['quantity']} | "
        f"SL={plan['sl_price']} TP1={plan['tp1_price']} TP2={plan['tp2_price']}"
    )

    return {
        "ok": True,
        "shadow": False,
        "entry_order": entry_order,
        "sl_order": sl_order,
        "tp1_order": tp1_order,
        "tp2_order": tp2_order,
    }
