"""Orchestrator: real-time data -> structure/order-flow signal -> risk plan
-> entry -> TP1/TP2/SL -> breakeven -> close, end to end.

Defaults to SHADOW execution (config.EXECUTION_MODE) - signals are still
fully evaluated, sized, and journaled, and open "positions" are tracked
and resolved against live price action, but no real order is placed until
EXECUTION_MODE is explicitly switched to LIVE in .env. This is the
evidence-gate from the original plan: review shadow signal quality first.
"""
from collections import Counter
import threading
import time

import config
import exchange
import execution
import risk_manager
import signal_engine
import signal_journal
from logger import log_error, log_info, log_warning
from position_manager import PositionManager
from ws_client import RealtimeMarketData


shutdown_event = threading.Event()


def _select_symbols():
    if config.SCAN_SYMBOLS:
        return list(config.SCAN_SYMBOLS)

    supported = exchange.get_supported_symbols()

    if not supported:
        log_error("No supported symbols resolved from exchange info; aborting")
        return []

    volumes = exchange.get_24h_quote_volumes()

    if volumes:
        ranked = sorted(supported, key=lambda symbol: volumes.get(symbol, 0), reverse=True)
    else:
        ranked = sorted(supported)

    return ranked[: max(config.WATCHLIST_SIZE, 1)]


def _current_balance():
    if config.EXECUTION_MODE == "LIVE":
        return exchange.get_balance()

    return config.SHADOW_ACCOUNT_BALANCE_USDT


_MAX_REJECT_SAMPLE_SYMBOLS = 5


def _tally_reject(reject_counts, reject_symbols, symbol, reason):
    """Records one rejection: always a count, plus (capped) which symbols
    actually triggered it. The cap matters - NO_LIVE_STRUCTURE_BREAK alone
    can fire for hundreds of symbols per heartbeat window, so this is a
    representative sample for eyeballing "which symbols", not a full list."""
    if reject_counts is None:
        return

    reject_counts[reason] += 1

    if reject_symbols is None:
        return

    sample = reject_symbols.setdefault(reason, [])

    if symbol not in sample and len(sample) < _MAX_REJECT_SAMPLE_SYMBOLS:
        sample.append(symbol)


def _evaluate_symbol(feed, symbol, positions, balance, reject_counts=None, reject_symbols=None):
    """`reject_counts`/`reject_symbols` (optional) tally why candidates
    don't convert to a trade - signal_engine.evaluate()'s `reason`, plus a
    couple of pre-signal/post-signal cases of our own - and which symbols
    triggered each one. Real gap found live (2026-08-11): with 0 open
    positions and a slow 1h/4h timeframe, "no entries" was completely
    unexplainable from the logs - every rejection reason was silently
    discarded, so there was no way to tell "working as intended, genuinely
    no qualifying setups yet" apart from "something is over-restrictive".
    This makes that visible via the heartbeat instead of guessing.
    Deliberately excludes the has_open_position/cooldown/max_positions
    early-exits above - those are routine operational skips, not signal-
    quality rejections, and would just dilute the tally."""
    if positions.has_open_position(symbol):
        return

    if positions.is_in_cooldown(symbol):
        return

    if positions.open_count() >= config.MAX_TOTAL_POSITIONS:
        return

    ltf_candles = feed.candles.get(symbol)
    htf_candles = feed.htf_candles.get(symbol)

    if not ltf_candles or not htf_candles:
        _tally_reject(reject_counts, reject_symbols, symbol, "NO_CANDLE_DATA")
        return

    cvd_snapshot = feed.cvd.snapshot(symbol)
    depth_snapshot = feed.depth.snapshot(symbol)
    oi_snapshot = feed.open_interest.snapshot(symbol)
    liquidation_snapshot = feed.liquidations.snapshot(symbol)
    quote_volume_usdt = feed.volumes.get(symbol)

    result = signal_engine.evaluate(
        symbol, htf_candles, ltf_candles, cvd_snapshot, depth_snapshot,
        oi_snapshot=oi_snapshot, liquidation_snapshot=liquidation_snapshot,
        quote_volume_usdt=quote_volume_usdt,
    )

    if not result.get("signal"):
        _tally_reject(reject_counts, reject_symbols, symbol, result.get("reason") or "UNKNOWN")
        return

    plan, status = risk_manager.build_trade_plan(result, balance)

    if status != "OK":
        _tally_reject(reject_counts, reject_symbols, symbol, f"PLAN_REJECTED:{status}")
        log_info(f"{symbol} signal found but plan rejected | REASON={status}")
        return

    # Carried through to the position so resolve_break_confirmations() can
    # later check, once this exact candle actually finishes, whether price
    # held beyond the level it broke or snapped back inside first (just a
    # wick, not a real break) - see PositionManager.resolve_break_confirmations.
    # Sourced from the signal itself (the candle signal_engine actually
    # evaluated the break against), NOT blindly ltf_candles[-1] - with
    # REQUIRE_CLOSE_CONFIRMED_BREAK enabled that's the last CLOSED candle,
    # which may already differ from whatever candle is forming by the time
    # this line runs.
    plan["structure_level"] = result.get("structure_level")
    plan["trigger_candle_open_time"] = result.get("trigger_candle_open_time")

    log_info(
        f"{symbol} SIGNAL {result['signal']} | entry~={plan['entry_price']} "
        f"SL={plan['sl_price']} TP1={plan['tp1_price']} TP2={plan['tp2_price']} | "
        f"cvd={result.get('cvd_score')} sweep={result.get('sweep_confluence')} "
        f"htf_trend={result.get('htf_trend')}"
    )

    execution_result = execution.enter_trade(plan)

    if not execution_result.get("ok"):
        log_warning(f"{symbol} entry failed | {execution_result.get('error')}")
        positions.mark_entry_failure(symbol)
        return

    trade_id = signal_journal.append_signal(result, plan)
    positions.register(plan, execution_result, trade_id=trade_id)


def _poll_positions(feed, positions):
    # Outcome journaling happens inside PositionManager._close() itself
    # (it's the only place that still has the trade_id after a position
    # is popped from tracking), not here.
    for symbol in list(positions.positions.keys()):
        position = positions.positions.get(symbol)

        if not position:
            continue

        if position["shadow"]:
            latest_candle = feed.candles.latest(symbol)
            positions.poll_shadow(symbol, latest_candle)
        else:
            positions.poll_live(symbol)


def _resolve_break_confirmations(feed, positions):
    positions.resolve_break_confirmations(feed.candles)


def _log_heartbeat(feed, symbols, positions, reject_counts=None, reject_symbols=None):
    log_info(
        f"Heartbeat | WATCHING={len(symbols)} | OPEN_POSITIONS={positions.open_count()} "
        f"| MODE={config.EXECUTION_MODE}"
    )

    if reject_counts:
        top = sorted(reject_counts.items(), key=lambda kv: -kv[1])[:8]
        parts = []

        for reason, count in top:
            sample = (reject_symbols or {}).get(reason, [])
            more = ",..." if sample and count > len(sample) else ""
            symbols_note = f"[{','.join(sample)}{more}]" if sample else ""
            parts.append(f"{reason}={count}{symbols_note}")

        log_info(f"  REJECTED since last heartbeat | {' '.join(parts)}")

    for symbol in list(positions.positions.keys()):
        position = positions.positions[symbol]
        log_info(
            f"  OPEN {symbol} {position['side']} stage={position['stage']} "
            f"entry={position['entry_price']} sl={position['sl_price']} "
            f"tp1={position['tp1_price']} tp2={position['tp2_price']}"
        )


def main():
    exchange.sync_client_time()

    symbols = _select_symbols()

    if not symbols:
        log_error("No symbols selected for the watchlist; nothing to watch")
        return

    log_info(f"Watching {len(symbols)} symbols in {config.EXECUTION_MODE} mode: {', '.join(symbols)}")

    feed = RealtimeMarketData(symbols, shutdown_event=shutdown_event)
    feed.start()

    positions = PositionManager()

    if config.EXECUTION_MODE == "LIVE":
        positions.reconcile_on_startup()

    eval_interval = max(config.SIGNAL_EVAL_INTERVAL_SECONDS, 1)
    heartbeat_every = max(int(30 / eval_interval), 1)
    # Position polling makes several private REST calls per open position
    # (SL/TP1/TP2 order-status lookups, etc.) - paced on its own cadence
    # instead of every single signal-eval tick, so a large
    # MAX_TOTAL_POSITIONS doesn't multiply REST traffic by the (much
    # faster) eval interval. Real bug found live (2026-08-11):
    # POSITION_POLL_INTERVAL_SECONDS existed in config but was never
    # actually wired to anything - positions were polled on every eval
    # tick regardless, which (combined with the private REST layer's
    # throttle being a no-op - see exchange._private_rest_call) directly
    # contributed to a real Binance IP ban (-1003 Way too many requests).
    poll_every_ticks = max(round(config.POSITION_POLL_INTERVAL_SECONDS / eval_interval), 1)
    tick = 0
    reject_counts = Counter()
    reject_symbols = {}

    try:
        while not shutdown_event.is_set():
            time.sleep(eval_interval)
            tick += 1

            balance = _current_balance()

            if tick % poll_every_ticks == 0:
                _poll_positions(feed, positions)
                _resolve_break_confirmations(feed, positions)

            for symbol in symbols:
                _evaluate_symbol(feed, symbol, positions, balance, reject_counts, reject_symbols)

            if tick % heartbeat_every == 0:
                _log_heartbeat(feed, symbols, positions, reject_counts, reject_symbols)
                reject_counts.clear()
                reject_symbols.clear()

    except KeyboardInterrupt:
        log_warning("Shutdown requested (KeyboardInterrupt)")

    finally:
        shutdown_event.set()
        feed.stop()


if __name__ == "__main__":
    main()
