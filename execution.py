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
from logger import log_error, log_info


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

    try:
        exchange.setup_leverage(symbol)

        entry_order = exchange.place_market_order(symbol, side, plan["quantity"])

        sl_order = exchange.place_stop_loss(symbol, side, plan["sl_price"])
        tp1_order = exchange.place_take_profit_partial(
            symbol, side, plan["tp1_quantity"], plan["tp1_price"]
        )
        tp2_order = exchange.place_take_profit_full(symbol, side, plan["tp2_price"])

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

    except Exception as exc:
        log_error(f"{symbol} entry execution error: {exc}")
        return {"ok": False, "shadow": False, "error": str(exc)}
