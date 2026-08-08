"""Orchestrator: real-time data -> structure/order-flow signal -> risk plan
-> entry -> TP1/TP2/SL -> breakeven -> close, end to end.

Defaults to SHADOW execution (config.EXECUTION_MODE) - signals are still
fully evaluated, sized, and journaled, and open "positions" are tracked
and resolved against live price action, but no real order is placed until
EXECUTION_MODE is explicitly switched to LIVE in .env. This is the
evidence-gate from the original plan: review shadow signal quality first.
"""
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


def _evaluate_symbol(feed, symbol, positions, balance):
    if positions.has_open_position(symbol):
        return

    if positions.is_in_cooldown(symbol):
        return

    if positions.open_count() >= config.MAX_TOTAL_POSITIONS:
        return

    ltf_candles = feed.candles.get(symbol)
    htf_candles = feed.htf_candles.get(symbol)

    if not ltf_candles or not htf_candles:
        return

    cvd_snapshot = feed.cvd.snapshot(symbol)
    depth_snapshot = feed.depth.snapshot(symbol)
    oi_snapshot = feed.open_interest.snapshot(symbol)
    liquidation_snapshot = feed.liquidations.snapshot(symbol)

    result = signal_engine.evaluate(
        symbol, htf_candles, ltf_candles, cvd_snapshot, depth_snapshot,
        oi_snapshot=oi_snapshot, liquidation_snapshot=liquidation_snapshot,
    )

    if not result.get("signal"):
        return

    plan, status = risk_manager.build_trade_plan(result, balance)

    if status != "OK":
        log_info(f"{symbol} signal found but plan rejected | REASON={status}")
        return

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


def _log_heartbeat(feed, symbols, positions):
    log_info(
        f"Heartbeat | WATCHING={len(symbols)} | OPEN_POSITIONS={positions.open_count()} "
        f"| MODE={config.EXECUTION_MODE}"
    )

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
    tick = 0

    try:
        while not shutdown_event.is_set():
            time.sleep(eval_interval)
            tick += 1

            balance = _current_balance()
            _poll_positions(feed, positions)

            for symbol in symbols:
                _evaluate_symbol(feed, symbol, positions, balance)

            if tick % heartbeat_every == 0:
                _log_heartbeat(feed, symbols, positions)

    except KeyboardInterrupt:
        log_warning("Shutdown requested (KeyboardInterrupt)")

    finally:
        shutdown_event.set()
        feed.stop()


if __name__ == "__main__":
    main()
