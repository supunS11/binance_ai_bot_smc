"""Append-only CSV journal of every generated entry signal and its
eventual outcome - the same "always write real evidence, never trust
memory" convention as v7/v8's signal_journal.py.

Only signals that actually pass signal_engine (a real BUY/SELL, not every
rejected NO_SIGNAL evaluation - that would be enormous at tick frequency)
get a row here.

Each signal gets a `trade_id`; its eventual outcome is appended as a
separate row carrying the same `trade_id` (append-only, crash-safe - never
mutates the original row). journal_analysis.py joins the two by that id -
without it, a symbol with several signals close together (a known real
pattern - see the repeated PIPPINUSDT/MANAUSDT re-entries) can't be told
apart, which makes "why did this SL hit" unanswerable.
"""
import csv
from pathlib import Path
import time

import config


JOURNAL_PATH = Path(__file__).resolve().parent / "data" / "signal_journal.csv"

FIELDNAMES = [
    "timestamp", "trade_id", "symbol", "side", "entry_price", "sl_price",
    "tp1_price", "tp2_price", "quantity", "risk_distance_pct",
    "structure_level", "atr", "htf_trend", "premium_discount_zone",
    "order_block_present", "fvg_present", "cvd_score", "depth_imbalance",
    "sweep_confluence", "tp1_r_multiple", "tp2_r_multiple",
    "execution_mode", "outcome",
]


def _ensure_header():
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not JOURNAL_PATH.exists():
        with open(JOURNAL_PATH, "w", newline="") as handle:
            csv.DictWriter(handle, fieldnames=FIELDNAMES).writeheader()


def _append_row(row):
    _ensure_header()

    with open(JOURNAL_PATH, "a", newline="") as handle:
        csv.DictWriter(handle, fieldnames=FIELDNAMES).writerow(row)


def _make_trade_id(symbol):
    return f"{symbol}_{int(time.time() * 1000)}"


def append_signal(signal, plan):
    """Returns the trade_id so the caller can pass it to position_manager,
    which threads it through to append_outcome when the trade closes."""
    trade_id = _make_trade_id(signal.get("symbol") or "UNKNOWN")
    entry_price = plan.get("entry_price") or 0
    risk_distance = plan.get("risk_distance") or 0

    row = {field: "" for field in FIELDNAMES}
    row.update({
        "timestamp": time.time(),
        "trade_id": trade_id,
        "symbol": signal.get("symbol"),
        "side": signal.get("signal"),
        "entry_price": entry_price,
        "sl_price": plan.get("sl_price"),
        "tp1_price": plan.get("tp1_price"),
        "tp2_price": plan.get("tp2_price"),
        "quantity": plan.get("quantity"),
        "risk_distance_pct": (
            round(risk_distance / entry_price * 100, 4) if entry_price else ""
        ),
        "structure_level": signal.get("structure_level"),
        "atr": signal.get("atr"),
        "htf_trend": signal.get("htf_trend"),
        "premium_discount_zone": signal.get("premium_discount_zone"),
        "order_block_present": bool(signal.get("order_block")),
        "fvg_present": bool(signal.get("fvg")),
        "cvd_score": signal.get("cvd_score"),
        "depth_imbalance": signal.get("depth_imbalance"),
        "sweep_confluence": signal.get("sweep_confluence"),
        "tp1_r_multiple": config.TP1_R_MULTIPLE,
        "tp2_r_multiple": config.TP2_R_MULTIPLE,
        "execution_mode": config.EXECUTION_MODE,
    })
    _append_row(row)
    return trade_id


def append_outcome(symbol, outcome, trade_id=None):
    row = {field: "" for field in FIELDNAMES}
    row["timestamp"] = time.time()
    row["trade_id"] = trade_id or ""
    row["symbol"] = symbol
    row["outcome"] = outcome
    _append_row(row)
