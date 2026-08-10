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


def _order_type(order):
    """The algo-order list endpoint returns the order type under
    `orderType`, not `type` (confirmed against v7's proven-working
    find_matching_open_algo_order) - checking `type` alone silently
    matches nothing, ever."""
    return str((order or {}).get("orderType") or (order or {}).get("type") or "").upper()


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PositionManager:
    def __init__(self):
        self.positions = {}
        self._closed_at = {}  # symbol -> timestamp of its most recent close

    def has_open_position(self, symbol):
        return symbol in self.positions

    def is_in_cooldown(self, symbol):
        """After ANY close (win, loss, or breakeven), skip that symbol for
        SYMBOL_REENTRY_COOLDOWN_SECONDS. Evidence (2026-08-08): several
        symbols hit SL repeatedly within seconds-to-minutes of the
        previous close, at nearly the same level - immediate re-entry
        into an actively chopping symbol instead of waiting for the
        picture to actually change."""
        closed_at = self._closed_at.get(symbol)

        if closed_at is None:
            return False

        cooldown = max(int(config.SYMBOL_REENTRY_COOLDOWN_SECONDS), 0)
        return (time.time() - closed_at) < cooldown

    def mark_entry_failure(self, symbol):
        """A failed entry (leverage rejected, SL placement failed, etc.)
        never reaches register(), so is_in_cooldown()'s normal trigger
        (_close()) never fires for it either - real bug found live
        (STGUSDT/DEXEUSDT, 2026-08-08): a symbol that fails entry for a
        persistent reason got retried on every single eval cycle with no
        backoff at all, each attempt opening and market-closing a real
        position. Route a failed attempt through the same cooldown a
        normal close gets, so a broken symbol backs off instead."""
        self._closed_at[symbol] = time.time()

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
            "confluence_ratio": plan.get("confluence_ratio"),
            "early_breakeven_applied": False,
            "mae_price": plan["entry_price"],
            "mfe_price": plan["entry_price"],
            # Fixed at entry, never touched again - see
            # _mae_mfe_r_multiples for why this must never be re-derived
            # from position["sl_price"] later (that field legitimately
            # moves to the breakeven price once a trade is promoted).
            "risk_distance": plan.get("risk_distance") or abs(plan["entry_price"] - plan["sl_price"]),
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
        sl_order = next((o for o in open_orders if _order_type(o) == "STOP_MARKET"), None)
        tp_orders = [o for o in open_orders if _order_type(o) == "TAKE_PROFIT_MARKET"]
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

        tp1_quantity = (
            _safe_float(tp1_order.get("quantity") or tp1_order.get("origQty"))
            if tp1_order else None
        )
        if tp1_quantity is None:
            tp1_quantity = round(quantity * min(max(float(config.TP1_CLOSE_PCT), 0), 100) / 100, 8)
        tp2_quantity = max(round(quantity - tp1_quantity, 8), 0)

        # Real bug found live (2026-08-10): "TP1 order absent" alone used
        # to be a reliable "already promoted" signal, but the early-1R
        # breakeven trigger moves the stop WITHOUT touching TP1 (it's
        # still a live, resting order - the trade just hasn't reached it
        # yet). A position early-promoted before a restart was getting
        # misclassified as still TP1_PENDING, which then computed
        # risk_distance from the already-moved (tiny) stop distance
        # instead of correctly marking it unrecoverable - and, worse, an
        # eventual stop-out on that misclassified position would log as a
        # full SL_HIT instead of the breakeven it actually was. Detect
        # "already protected" directly instead: a stop sitting on the
        # profit side of entry can only be a promoted stop (breakeven or
        # later) - an original risk-bearing stop is never there.
        sl_already_protects_profit = (
            (side == "BUY" and sl_price >= entry_price)
            or (side == "SELL" and sl_price <= entry_price)
        )
        stage = (
            BREAKEVEN_ACTIVE
            if (sl_already_protects_profit or (tp2_order and not tp1_order))
            else TP1_PENDING
        )

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
            # No original signal to read confluence from - kept for
            # journaling only; early breakeven no longer depends on it
            # (see _is_early_breakeven_candidate).
            "confluence_ratio": None,
            "early_breakeven_applied": False,
            # MAE/MFE tracking effectively restarts here too - there's no
            # way to recover the price path from before this restart.
            "mae_price": entry_price,
            "mfe_price": entry_price,
            # Real bug found live (2026-08-09): a position adopted while
            # already BREAKEVEN_ACTIVE has `sl_price` sitting at the
            # breakeven price, not the original stop - the true original
            # risk distance was lost the moment the exchange's SL order
            # got replaced, before this restart, and there is no way to
            # recover it now. Using abs(entry-sl_price) here for that case
            # would divide MAE/MFE by a near-zero breakeven-buffer
            # distance instead of the real risk, producing R-multiples in
            # the billions. None (unknown) is honest; a fabricated number
            # isn't. Only a TP1_PENDING adoption still has its real,
            # untouched original stop to compute this from.
            "risk_distance": abs(entry_price - sl_price) if stage == TP1_PENDING else None,
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
            self._closed_at[symbol] = time.time()
            log_info(f"{symbol} position closed | OUTCOME={outcome}")
            mae_r, mfe_r = self._mae_mfe_r_multiples(position)
            signal_journal.append_outcome(
                symbol, outcome, position.get("trade_id"),
                mae_r_multiple=mae_r, mfe_r_multiple=mfe_r,
                early_breakeven_applied=position.get("early_breakeven_applied", False),
            )

        return outcome

    @staticmethod
    def _update_mae_mfe(position, low_or_price, high_or_price=None):
        """Tracks the worst (MAE) and best (MFE) price seen over the life
        of a trade so far. Live mode passes a single mark-price sample
        (high_or_price defaults to the same value); shadow mode passes
        the current candle's actual low/high so a single candle's full
        range is captured, not just its close. See
        config.MAE_TRACKING_ENABLED for why this exists."""
        if not config.MAE_TRACKING_ENABLED:
            return

        if high_or_price is None:
            high_or_price = low_or_price

        if low_or_price is None or high_or_price is None:
            return

        side = position["side"]
        mae_price = position.get("mae_price", position["entry_price"])
        mfe_price = position.get("mfe_price", position["entry_price"])

        if side == "BUY":
            position["mae_price"] = min(mae_price, low_or_price)
            position["mfe_price"] = max(mfe_price, high_or_price)
        else:
            position["mae_price"] = max(mae_price, high_or_price)
            position["mfe_price"] = min(mfe_price, low_or_price)

    @staticmethod
    def _mae_mfe_r_multiples(position):
        """Both expressed as an R-multiple of the position's ORIGINAL risk
        distance (entry to its stop at the moment the trade opened) -
        comparable across symbols and volatility regimes, unlike a raw
        price distance. A LOSS with mfe_r_multiple near zero never moved
        favorably at all (wrong from the first tick); a LOSS with a large
        mfe_r_multiple was solidly in profit at some point before fully
        reversing - two very different problems that look identical as a
        plain outcome.

        Real bug found live (2026-08-09): this used to recompute
        abs(entry_price - position["sl_price"]) here directly - but
        sl_price is legitimately mutated over a trade's life (moved to
        the breakeven price on promotion, in both shadow mode's direct
        mutation and a startup-reconciliation adoption that picks up an
        already-breakeven real order). Once sl_price sits a few cents
        from entry instead of the original stop's real distance, dividing
        by it inflates the R-multiple into the billions. Must always use
        the fixed `risk_distance` captured once at position start
        (register()/_adopt_position()), never re-derive it from the
        current (possibly-moved) sl_price."""
        entry_price = position["entry_price"]
        risk_distance = position.get("risk_distance")

        if risk_distance is None or risk_distance <= 0:
            return None, None

        mae_price = position.get("mae_price", entry_price)
        mfe_price = position.get("mfe_price", entry_price)
        mae_r = round(abs(entry_price - mae_price) / risk_distance, 4)
        mfe_r = round(abs(entry_price - mfe_price) / risk_distance, 4)
        return mae_r, mfe_r

    def _promote_to_breakeven(self, position, reason="TP1 filled"):
        """Runs once TP1 is detected as filled (or, via `reason`, when a
        trade earns an early promotion before TP1 by reaching
        EARLY_BREAKEVEN_R_MULTIPLE in profit - see
        _is_early_breakeven_candidate). This is where a position can
        legitimately have already closed entirely (the original SL
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
                f"{symbol} {reason} | SL moved to breakeven="
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

    def _is_early_breakeven_candidate(self, position):
        """Cheap, no-network pre-check for early breakeven
        (config.EARLY_BREAKEVEN_ENABLED) - only fetch a current price at
        all (an extra REST call in poll_live) for a position that could
        actually qualify: TP1 still pending, not already promoted.

        Originally gated on confluence_ratio (protect low-confidence
        trades faster) - real evidence (2026-08-10, 61 resolved LOSS
        trades) didn't support that: confluence showed no correlation
        with outcome. What the same data DID show clearly: MFE for LOSS
        trades is bimodal, not a smooth spread - 38% near zero (wrong
        from the first tick, no fix here helps them) but 28% ran 1.0R+ in
        profit before fully reversing to a full loss, completely
        unprotected the whole way down since nothing moves the stop until
        TP1 formally triggers at 2R. This now targets that second,
        evidence-backed population directly - every trade still waiting
        on TP1, not just ones with low confluence."""
        if not config.EARLY_BREAKEVEN_ENABLED or position.get("early_breakeven_applied"):
            return False

        if position["stage"] != TP1_PENDING:
            return False

        return abs(position["entry_price"] - position["sl_price"]) > 0

    @staticmethod
    def _early_breakeven_price_reached(position, current_price):
        """Has price moved EARLY_BREAKEVEN_R_MULTIPLE R in the position's
        favor yet? Shared by poll_live (real mark price) and poll_shadow
        (simulated candle close)."""
        if current_price is None or current_price <= 0:
            return False

        side = position["side"]
        entry_price = position["entry_price"]
        risk_distance = abs(entry_price - position["sl_price"])
        trigger_distance = risk_distance * max(float(config.EARLY_BREAKEVEN_R_MULTIPLE), 0)
        favorable_move = (
            current_price - entry_price if side == "BUY" else entry_price - current_price
        )
        return favorable_move >= trigger_distance

    def poll_live(self, symbol):
        """Returns an outcome string if the position closed this call,
        otherwise None."""
        position = self.positions.get(symbol)

        if not position or position["shadow"]:
            return None

        self._ensure_protection_orders(position)

        # One shared mark-price fetch, reused for both MAE/MFE tracking
        # and the early-breakeven check below - no reason to pay for two
        # REST calls when either (or both) need the same current price.
        early_breakeven_candidate = (
            position["stage"] == TP1_PENDING and self._is_early_breakeven_candidate(position)
        )
        current_price = None

        if config.MAE_TRACKING_ENABLED or early_breakeven_candidate:
            current_price = exchange.get_mark_price(symbol)
            self._update_mae_mfe(position, current_price)

        if position["stage"] == TP1_PENDING:
            if (
                early_breakeven_candidate
                and self._early_breakeven_price_reached(position, current_price)
            ):
                position["early_breakeven_applied"] = True
                self._promote_to_breakeven(
                    position, reason="Early breakeven (1R profit-lock)"
                )
                return None

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
            if _order_type(order) != order_type:
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

        self._update_mae_mfe(position, low, high)

        if position["stage"] == TP1_PENDING:
            hit_sl = (
                low <= position["sl_price"]
                if side == "BUY"
                else high >= position["sl_price"]
            )

            if hit_sl:
                return self._close(symbol, "SHADOW_SL_HIT")

            if self._is_early_breakeven_candidate(position) and self._early_breakeven_price_reached(
                position, latest_candle["close"]
            ):
                position["early_breakeven_applied"] = True
                position["stage"] = BREAKEVEN_ACTIVE
                position["sl_price"] = position["breakeven_price"]
                log_info(f"{symbol} [SHADOW] early breakeven (1R profit-lock) | SL -> breakeven")
                return None

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
