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
import risk_manager
import signal_journal
from logger import log_error, log_info, log_warning


TP1_PENDING = "TP1_PENDING"
BREAKEVEN_ACTIVE = "BREAKEVEN_ACTIVE"


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PositionManager:
    def __init__(self):
        self.positions = {}

    def has_open_position(self, symbol):
        return symbol in self.positions

    def open_count(self):
        return len(self.positions)

    def register(self, plan, execution_result, trade_id=None):
        symbol = plan["symbol"]
        shadow = execution_result.get("shadow", True)

        position = {
            "symbol": symbol,
            "trade_id": trade_id,
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

    def reconcile_on_startup(self):
        """Rebuild tracking for positions already open on the exchange
        when the process starts - crash, manual restart, or a redeploy.
        Without this, a restart makes the bot blind to real open
        positions: has_open_position() would wrongly say "no" for a
        symbol that already has one (risking a duplicate entry stacked on
        top of it), and the existing position would never get TP1 ->
        breakeven promotion, missing-order self-heal, or its outcome
        journaled - even though its real SL/TP1/TP2 orders keep working
        on Binance's side regardless of whether the bot remembers them."""
        live_positions = exchange.get_all_open_positions()
        adopted = 0

        for live_position in live_positions:
            symbol = live_position["symbol"]

            if symbol in self.positions:
                continue

            self._adopt_position(symbol, live_position)
            adopted += 1

        if live_positions:
            log_info(
                f"Startup reconciliation | {len(live_positions)} open "
                f"position(s) found on the exchange | {adopted} adopted"
            )

    def _adopt_position(self, symbol, live_position):
        side = live_position["side"]
        entry_price = live_position["entry_price"]
        quantity = live_position["quantity"]

        open_orders = exchange.get_open_algo_orders(symbol)
        sl_order = next((o for o in open_orders if o.get("type") == "STOP_MARKET"), None)
        tp_orders = [o for o in open_orders if o.get("type") == "TAKE_PROFIT_MARKET"]
        tp1_order = next(
            (o for o in tp_orders if str(o.get("closePosition")).lower() != "true"), None
        )
        tp2_order = next(
            (o for o in tp_orders if str(o.get("closePosition")).lower() == "true"), None
        )

        def _trigger_price(order):
            if not order:
                return None
            return _safe_float(order.get("triggerPrice") or order.get("stopPrice"))

        sl_price = _trigger_price(sl_order)

        if sl_price is None:
            # A real open position with no stop at all - treat as an
            # emergency: reconstruct a minimum-distance stop and place it
            # immediately rather than leave it unprotected until the next
            # opportunity to notice.
            sl_price = risk_manager._apply_min_stop_distance(entry_price, entry_price, side)
            log_warning(
                f"{symbol} open position found with NO stop-loss during "
                f"startup reconciliation - placing an emergency stop at {sl_price}"
            )

        # Real trigger prices are used where an order actually exists;
        # anything missing gets a reconstructed target (current config's
        # R-multiples off the recovered SL) so _ensure_protection_orders
        # has a real price to place on the next poll - the same self-heal
        # path that already recovers a mid-session placement failure.
        fallback_tp1, fallback_tp2 = risk_manager.compute_targets(entry_price, sl_price, side)
        tp1_price = _trigger_price(tp1_order) or fallback_tp1
        tp2_price = _trigger_price(tp2_order) or fallback_tp2

        tp1_quantity = _safe_float(tp1_order.get("origQty")) if tp1_order else None
        if tp1_quantity is None:
            tp1_quantity = round(quantity * min(max(float(config.TP1_CLOSE_PCT), 0), 100) / 100, 8)
        tp2_quantity = max(round(quantity - tp1_quantity, 8), 0)

        # Only TP2 existing (no TP1) means TP1 already resolved before
        # restart; anything else defaults to TP1_PENDING so a genuinely
        # missing TP1 gets self-healed rather than silently skipped.
        stage = BREAKEVEN_ACTIVE if (tp2_order and not tp1_order) else TP1_PENDING

        position = {
            "symbol": symbol,
            "trade_id": f"{symbol}_RECOVERED_{int(time.time() * 1000)}",
            "side": side,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "breakeven_price": risk_manager.compute_breakeven_price(entry_price, side),
            "quantity": quantity,
            "tp1_quantity": tp1_quantity,
            "tp2_quantity": tp2_quantity,
            "sl_order_id": exchange._accepted_order_id(sl_order) if sl_order else "",
            "tp1_order_id": exchange._accepted_order_id(tp1_order) if tp1_order else "",
            "tp2_order_id": exchange._accepted_order_id(tp2_order) if tp2_order else "",
            "stage": stage,
            "shadow": False,
            "opened_at": time.time(),
        }

        if not sl_order:
            try:
                new_sl = exchange.place_stop_loss(symbol, side, sl_price)
                position["sl_order_id"] = exchange._accepted_order_id(new_sl)
            except Exception as exc:
                log_error(
                    f"{symbol} CRITICAL: failed to place emergency stop "
                    f"during startup reconciliation - manual intervention "
                    f"needed: {exc}"
                )

        self.positions[symbol] = position
        log_info(
            f"{symbol} adopted existing open position | side={side} "
            f"entry={entry_price} sl={sl_price} tp1={tp1_price} "
            f"tp2={tp2_price} stage={stage}"
        )

    def _close(self, symbol, outcome):
        position = self.positions.pop(symbol, None)

        if position:
            log_info(f"{symbol} position closed | OUTCOME={outcome}")
            signal_journal.append_outcome(symbol, outcome, position.get("trade_id"))

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
            exchange.cancel_all_open_orders(symbol)
            return self._close(symbol, "TP1_THEN_POSITION_ALREADY_CLOSED")

        try:
            # Cancel whatever SL is *actually* sitting on the exchange,
            # not just whatever id local tracking happens to have - a
            # stale/wrong local id cancels nothing, leaves the real order
            # in place, and the placement below then fails with -4130
            # ("already existing") every single poll forever.
            existing_sl = self._find_open_order(symbol, "STOP_MARKET", close_position=True)

            if existing_sl:
                exchange.cancel_algo_order(symbol, exchange._accepted_order_id(existing_sl))
            elif position["sl_order_id"]:
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
            exchange.cancel_all_open_orders(symbol)
            return self._close(symbol, "TP1_THEN_POSITION_ALREADY_CLOSED")

        try:
            exchange.close_position_market(
                symbol, position["side"], live_position["quantity"]
            )
            exchange.cancel_all_open_orders(symbol)

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

        self._ensure_protection_orders(position)

        if position["stage"] == TP1_PENDING:
            tp1_status = self._status_or_missing(symbol, position["tp1_order_id"])

            if tp1_status == "FINISHED":
                return self._promote_to_breakeven(position)

            sl_status = self._status_or_missing(symbol, position["sl_order_id"])

            if sl_status == "FINISHED":
                exchange.cancel_all_open_orders(symbol)
                return self._close(symbol, "SL_HIT")

            tp2_status = self._status_or_missing(symbol, position["tp2_order_id"])

            if tp2_status == "FINISHED":
                exchange.cancel_all_open_orders(symbol)
                return self._close(symbol, "TP2_HIT_DIRECT")

            return None

        if position["stage"] == BREAKEVEN_ACTIVE:
            sl_status = self._status_or_missing(symbol, position["sl_order_id"])

            if sl_status == "FINISHED":
                exchange.cancel_all_open_orders(symbol)
                return self._close(symbol, "BREAKEVEN_STOP_HIT")

            tp2_status = self._status_or_missing(symbol, position["tp2_order_id"])

            if tp2_status == "FINISHED":
                exchange.cancel_all_open_orders(symbol)
                return self._close(symbol, "TP2_HIT")

        return None

    @staticmethod
    def _status_or_missing(symbol, order_id):
        """Never calls Binance with a blank id - that's a guaranteed
        -1102 on every single poll forever, for a leg that isn't even
        placed yet (or failed to place, see _ensure_protection_orders)."""
        if not order_id:
            return "MISSING"

        return exchange.get_algo_order_status(symbol, order_id)

    def _ensure_protection_orders(self, position):
        """SL is guaranteed atomic at entry - execution.py aborts the
        whole trade if it can't be placed. TP1/TP2 are best-effort there
        (a placement failure doesn't abort an otherwise-safe, SL-protected
        trade), which means either can legitimately be missing. Retry
        placing whichever is missing instead of leaving that leg
        permanently degraded (no profit-taking on it) with no way to
        recover."""
        symbol = position["symbol"]
        side = position["side"]

        if position["stage"] == TP1_PENDING and not position["tp1_order_id"]:
            # Check the exchange for a real TP1-shaped order before
            # attempting to place one - if local tracking merely lost the
            # id (a reconciliation mismatch, for instance) while the real
            # order is still there, placing another one gets rejected
            # with -4130 ("already existing") on every single poll
            # forever. Re-sync from the real order instead of duplicating.
            existing = self._find_open_order(symbol, "TAKE_PROFIT_MARKET", close_position=False)

            if existing:
                position["tp1_order_id"] = exchange._accepted_order_id(existing)
                log_info(f"{symbol} TP1 order tracking re-synced from exchange")
            else:
                try:
                    order = exchange.place_take_profit_partial(
                        symbol, side, position["tp1_quantity"], position["tp1_price"]
                    )
                    position["tp1_order_id"] = exchange._accepted_order_id(order)

                    if position["tp1_order_id"]:
                        log_info(f"{symbol} TP1 order recovered")
                except Exception as exc:
                    if "-2021" in str(exc):
                        # Price has already passed the TP1 level entirely -
                        # a conditional order there would fire instantly,
                        # which is exactly what "take profit" means here.
                        # Take it at market instead of leaving TP1
                        # permanently unplaceable and retried forever.
                        log_warning(
                            f"{symbol} TP1 level already passed by price - "
                            "closing TP1 quantity at market instead"
                        )
                        self._market_close_tp1(position)
                    else:
                        log_warning(f"{symbol} TP1 recovery attempt failed: {exc}")

        if not position["tp2_order_id"]:
            existing = self._find_open_order(symbol, "TAKE_PROFIT_MARKET", close_position=True)

            if existing:
                position["tp2_order_id"] = exchange._accepted_order_id(existing)
                log_info(f"{symbol} TP2 order tracking re-synced from exchange")
            else:
                try:
                    order = exchange.place_take_profit_full(
                        symbol, side, position["tp2_price"]
                    )
                    position["tp2_order_id"] = exchange._accepted_order_id(order)

                    if position["tp2_order_id"]:
                        log_info(f"{symbol} TP2 order recovered")
                except Exception as exc:
                    log_warning(f"{symbol} TP2 recovery attempt failed: {exc}")

    @staticmethod
    def _find_open_order(symbol, order_type, close_position):
        """Ground truth from the exchange: is there already a matching
        order sitting there, regardless of what local tracking thinks?
        Used before both placing a "missing" order (self-heal without
        creating a duplicate) and before cancelling a "known" order
        (cancel the real one, not a possibly-stale local id)."""
        for order in exchange.get_open_algo_orders(symbol):
            if order.get("type") != order_type:
                continue

            is_close_position = str(order.get("closePosition")).lower() == "true"

            if is_close_position == close_position:
                return order

        return None

    def _market_close_tp1(self, position):
        """TP1's price was already passed by the market before the order
        could be placed - close that quantity at market (the position is
        still SL-protected throughout) and promote the remainder to
        breakeven, the same outcome a genuine TP1 fill would have led to."""
        symbol = position["symbol"]
        side = position["side"]

        try:
            exchange.close_position_market(symbol, side, position["tp1_quantity"])
        except Exception as exc:
            log_error(f"{symbol} TP1 market-close-instead error: {exc}")
            return

        log_info(f"{symbol} TP1 quantity closed at market (price already past TP1)")
        self._promote_to_breakeven(position)

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
