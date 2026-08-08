"""Append-only CSV journal of every generated entry signal and its
eventual outcome - the same "always write real evidence, never trust
memory" convention as v7/v8's signal_journal.py.

Only signals that actually pass signal_engine (a real BUY/SELL, not every
rejected NO_SIGNAL evaluation - that would be enormous at tick frequency)
get a row here.
"""
import csv
from pathlib import Path
import time

import config


JOURNAL_PATH = Path(__file__).resolve().parent / "data" / "signal_journal.csv"

FIELDNAMES = [
    "timestamp", "symbol", "side", "entry_price", "sl_price", "tp1_price",
    "tp2_price", "quantity", "htf_trend", "cvd_score", "depth_imbalance",
    "sweep_confluence", "execution_mode", "outcome",
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


def append_signal(signal, plan):
    row = {field: "" for field in FIELDNAMES}
    row.update({
        "timestamp": time.time(),
        "symbol": signal.get("symbol"),
        "side": signal.get("signal"),
        "entry_price": plan.get("entry_price"),
        "sl_price": plan.get("sl_price"),
        "tp1_price": plan.get("tp1_price"),
        "tp2_price": plan.get("tp2_price"),
        "quantity": plan.get("quantity"),
        "htf_trend": signal.get("htf_trend"),
        "cvd_score": signal.get("cvd_score"),
        "depth_imbalance": signal.get("depth_imbalance"),
        "sweep_confluence": signal.get("sweep_confluence"),
        "execution_mode": config.EXECUTION_MODE,
    })
    _append_row(row)


def append_outcome(symbol, outcome):
    """Outcomes are appended as their own row rather than mutating the
    original one - simpler and crash-safe, matching the append-only
    philosophy of v7's journal."""
    row = {field: "" for field in FIELDNAMES}
    row["timestamp"] = time.time()
    row["symbol"] = symbol
    row["outcome"] = outcome
    _append_row(row)
