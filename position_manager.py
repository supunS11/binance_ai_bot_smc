"""Tracks open positions from entry through TP1 -> breakeven -> TP2/SL,
mirroring v7's multi_tp.py state machine: once TP1's algo order genuinely
triggers (status `FINISHED` - not just missing, which can also mean
cancelled because something else closed the position first), cancel the
original SL and replace it with one at breakeven for the remaining
quantity.

In SHADOW mode there is nothing to poll on the exchange, so outcomes are
simulated against the live candle stream instead - shadow trades still
produce real win/loss evidence about signal quality before anything is
placed for real.
"""
import time

import config
import exchange
from logger import log_error, log_info, log_warning


TP1_PENDING = "TP1_PENDING"
BREAKEVEN_ACTIVE = "BREAKEVEN_ACTIVE"


class PositionManager:
    def __init__(self):
        self.positions = {}

    def has_open_position(self, symbol):
        return symbol in self.positions

    def open_count(self):
        return len(self.positions)

    def register(self, plan, execution_result):
        symbol = plan["symbol"]
        shadow = execution_result.get("shadow", True)

        position = {
            "symbol": symbol,
            "side": plan["side"],
            "entry_price": plan["entry_price"],
            "sl_price": plan["sl_price"],
            "tp1_price": plan["tp1_price"],
            "tp2_price": plan["tp2_price"],
            "breakeven_price": plan["breakeven_price"],
            "quantity": plan["quantity"],
            "tp1_quantity": plan["tp1_quantity"],
            "tp2_quantity": plan["tp2_quantity"],
            "sl_order_id": (
                exchange._accepted_order_id(execution_result.get("sl_order"))
                if not shadow else None
            ),
            "tp1_order_id": (
                exchange._accepted_order_id(execution_result.get("tp1_order"))
                if not shadow else None
            ),
            "tp2_order_id": (
                exchange._accepted_order_id(execution_result.get("tp2_order"))
                if not shadow else None
            ),
            "stage": TP1_PENDING,
            "shadow": shadow,
            "opened_at": time.time(),
        }
        self.positions[symbol] = position
        return position

    def _close(self, symbol, outcome):
        position = self.positions.pop(symbol, None)

        if position:
            log_info(f"{symbol} position closed | OUTCOME={outcome}")

        return outcome

    def _promote_to_breakeven(self, position):
        """Runs once TP1 is detected as filled. This is where a position
        can legitimately have already closed entirely (the original SL
        firing in the same window as TP1, or manual intervention) - so the
        ground truth is checked on the exchange first rather than assuming
        the remainder is still there. Returns an outcome string if the
        position closed as part of this call, otherwise None - the retry
        must never be silently repeated forever with no state change and
        no escape hatch, since that can leave a position genuinely
        unprotected between a failed cancel and a failed replace."""
        symbol = position["symbol"]

        if not config.MOVE_SL_TO_BREAKEVEN_AFTER_TP1:
            position["stage"] = BREAKEVEN_ACTIVE
            return None

        try:
            live_position = exchange._fetch_open_position_detail(symbol)
        except Exception as exc:
            # Couldn't confirm ground truth this cycle (network/backoff) -
            # do nothing rather than guess; the next poll tries again.
            log_warning(f"{symbol} position-state check failed, retrying next poll: {exc}")
            return None

        if live_position is None:
            # TP1 filling coincided with the position closing entirely
            # (e.g. the original SL also triggered) - nothing left to
            # promote. Stop retrying a doomed replacement.
            if position["tp2_order_id"]:
                exchange.cancel_algo_order(symbol, position["tp2_order_id"])
            return self._close(symbol, "TP1_THEN_POSITION_ALREADY_CLOSED")

        try:
            if position["sl_order_id"]:
                exchange.cancel_algo_order(symbol, position["sl_order_id"])

            new_sl_order = exchange.place_stop_loss(
                symbol, position["side"], position["breakeven_price"]
            )
            position["sl_order_id"] = exchange._accepted_order_id(new_sl_order)
            position["stage"] = BREAKEVEN_ACTIVE
            log_info(
                f"{symbol} TP1 filled | SL moved to breakeven="
                f"{position['breakeven_price']}"
            )
            return None

        except Exception as exc:
            if "-2021" in str(exc):
                # The breakeven trigger price is already behind current
                # price - Binance refuses to place a stop that would fire
                # instantly. The remainder is effectively unprotected
                # right now, so close it at market immediately instead of
                # leaving it exposed and retrying the same failing order.
                log_warning(
                    f"{symbol} breakeven level already reached by price - "
                    "closing remainder at market"
                )
                return self._close_remainder_at_market(position)

            log_error(f"{symbol} breakeven SL replacement error: {exc}")
            return None

    def _close_remainder_at_market(self, position):
        symbol = position["symbol"]

        try:
            live_position = exchange._fetch_open_position_detail(symbol)
        except Exception as exc:
            log_error(f"{symbol} market-close position check error: {exc}")
            return None

        if live_position is None:
            if position["tp2_order_id"]:
                exchange.cancel_algo_order(symbol, position["tp2_order_id"])
            return self._close(symbol, "TP1_THEN_POSITION_ALREADY_CLOSED")

        try:
            exchange.close_position_market(
                symbol, position["side"], live_position["quantity"]
            )

            if position["tp2_order_id"]:
                exchange.cancel_algo_order(symbol, position["tp2_order_id"])

            return self._close(symbol, "BREAKEVEN_TRIGGER_MARKET_CLOSE")

        except Exception as exc:
            log_error(f"{symbol} market-close-remainder error: {exc}")
            return None

    def poll_live(self, symbol):
        """Returns an outcome string if the position closed this call,
        otherwise None."""
        position = self.positions.get(symbol)

        if not position or position["shadow"]:
            return None

        if position["stage"] == TP1_PENDING:
            tp1_status = exchange.get_algo_order_status(symbol, position["tp1_order_id"])

            if tp1_status == "FINISHED":
                return self._promote_to_breakeven(position)

            sl_status = exchange.get_algo_order_status(symbol, position["sl_order_id"])

            if sl_status == "FINISHED":
                exchange.cancel_algo_order(symbol, position["tp1_order_id"])
                exchange.cancel_algo_order(symbol, position["tp2_order_id"])
                return self._close(symbol, "SL_HIT")

            tp2_status = exchange.get_algo_order_status(symbol, position["tp2_order_id"])

            if tp2_status == "FINISHED":
                exchange.cancel_algo_order(symbol, position["sl_order_id"])
                exchange.cancel_algo_order(symbol, position["tp1_order_id"])
                return self._close(symbol, "TP2_HIT_DIRECT")

            return None

        if position["stage"] == BREAKEVEN_ACTIVE:
            sl_status = exchange.get_algo_order_status(symbol, position["sl_order_id"])

            if sl_status == "FINISHED":
                exchange.cancel_algo_order(symbol, position["tp2_order_id"])
                return self._close(symbol, "BREAKEVEN_STOP_HIT")

            tp2_status = exchange.get_algo_order_status(symbol, position["tp2_order_id"])

            if tp2_status == "FINISHED":
                exchange.cancel_algo_order(symbol, position["sl_order_id"])
                return self._close(symbol, "TP2_HIT")

        return None

    def poll_shadow(self, symbol, latest_candle):
        """Simulates the same TP1 -> breakeven -> TP2/SL sequence against
        live price action. When both the stop and a target fall inside the
        same candle's range, the SL side is assumed to have been touched
        first - a deliberately conservative simplification so shadow stats
        don't overstate win rate; it is not a substitute for real fills."""
        position = self.positions.get(symbol)

        if not position or not position["shadow"] or not latest_candle:
            return None

        high = latest_candle["high"]
        low = latest_candle["low"]
        side = position["side"]

        if position["stage"] == TP1_PENDING:
            hit_sl = (
                low <= position["sl_price"]
                if side == "BUY"
                else high >= position["sl_price"]
            )

            if hit_sl:
                return self._close(symbol, "SHADOW_SL_HIT")

            hit_tp1 = (
                high >= position["tp1_price"]
                if side == "BUY"
                else low <= position["tp1_price"]
            )

            if hit_tp1:
                position["stage"] = BREAKEVEN_ACTIVE
                position["sl_price"] = position["breakeven_price"]
                log_info(f"{symbol} [SHADOW] TP1 would have filled | SL -> breakeven")

            return None

        if position["stage"] == BREAKEVEN_ACTIVE:
            hit_sl = (
                low <= position["sl_price"]
                if side == "BUY"
                else high >= position["sl_price"]
            )

            if hit_sl:
                return self._close(symbol, "SHADOW_BREAKEVEN_STOP_HIT")

            hit_tp2 = (
                high >= position["tp2_price"]
                if side == "BUY"
                else low <= position["tp2_price"]
            )

            if hit_tp2:
                return self._close(symbol, "SHADOW_TP2_HIT")

        return None
