"""Joins signal_journal.csv's signal rows to their outcome rows by
trade_id and breaks down win/breakeven/loss rates by the diagnostic
fields captured at signal time - the actual mechanism for answering "why
did these SL hit" with evidence instead of a guess.

Run directly on the machine with the real journal:
    python journal_analysis.py

Only trades logged after the trade_id correlation fix (this file's
counterpart change in signal_journal.py/position_manager.py) can be
joined - older rows without a trade_id are silently skipped, not guessed
at via symbol/time proximity.
"""
import csv
from collections import defaultdict
from pathlib import Path

from signal_journal import JOURNAL_PATH


LOSS_OUTCOMES = {"SL_HIT", "SHADOW_SL_HIT", "TP1_THEN_POSITION_ALREADY_CLOSED"}
BREAKEVEN_OUTCOMES = {
    "BREAKEVEN_STOP_HIT", "SHADOW_BREAKEVEN_STOP_HIT",
    "BREAKEVEN_TRIGGER_MARKET_CLOSE",
}
WIN_OUTCOMES = {"TP2_HIT", "TP2_HIT_DIRECT", "SHADOW_TP2_HIT"}


def classify(outcome):
    if outcome in LOSS_OUTCOMES:
        return "LOSS"
    if outcome in BREAKEVEN_OUTCOMES:
        return "BREAKEVEN"
    if outcome in WIN_OUTCOMES:
        return "WIN"
    return "UNKNOWN"


def load_trades(journal_path=None):
    """Returns {trade_id: merged_row}. A signal row supplies every field
    except outcome; its later outcome row (same trade_id, everything else
    blank) fills in `outcome` on the same record."""
    path = Path(journal_path) if journal_path else JOURNAL_PATH

    if not path.exists():
        return {}

    trades = {}

    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            trade_id = row.get("trade_id")

            if not trade_id:
                continue

            existing = trades.setdefault(trade_id, {})

            if row.get("outcome"):
                existing["outcome"] = row["outcome"]
            else:
                existing.update({k: v for k, v in row.items() if v != ""})

    return trades


def _bucket_cvd(value):
    try:
        value = abs(float(value))
    except (TypeError, ValueError):
        return "unknown"

    if value < 0.3:
        return "weak (<0.3)"
    if value < 0.6:
        return "moderate (0.3-0.6)"
    return "strong (>=0.6)"


def _breakdown_lines(resolved, label, key_fn):
    lines = [f"\nBy {label}:"]
    buckets = defaultdict(lambda: defaultdict(int))

    for trade in resolved.values():
        buckets[key_fn(trade)][classify(trade.get("outcome", ""))] += 1

    for key, counts in sorted(buckets.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counts.values())
        loss_rate = (counts["LOSS"] / total * 100) if total else 0
        lines.append(
            f"  {key}: n={total} WIN={counts['WIN']} BREAKEVEN={counts['BREAKEVEN']} "
            f"LOSS={counts['LOSS']} loss_rate={loss_rate:.0f}%"
        )

    return lines


def summarize(journal_path=None):
    trades = load_trades(journal_path)
    resolved = {tid: t for tid, t in trades.items() if t.get("outcome")}

    if not resolved:
        return (
            "No resolved (closed) trades with a matching signal row yet. "
            "This needs trades opened after the trade_id correlation fix - "
            "let more run, then check again."
        )

    lines = [f"Resolved trades: {len(resolved)}"]
    overall = defaultdict(int)

    for trade in resolved.values():
        overall[classify(trade.get("outcome", ""))] += 1

    lines.append(
        f"  WIN={overall['WIN']} BREAKEVEN={overall['BREAKEVEN']} "
        f"LOSS={overall['LOSS']} UNKNOWN={overall['UNKNOWN']}"
    )

    lines += _breakdown_lines(resolved, "CVD score strength", lambda t: _bucket_cvd(t.get("cvd_score")))
    lines += _breakdown_lines(resolved, "sweep confluence", lambda t: t.get("sweep_confluence", "unknown") or "False")
    lines += _breakdown_lines(resolved, "EMA aligned (informational)", lambda t: t.get("ema_aligned", "unknown") or "False")
    lines += _breakdown_lines(resolved, "OI rising (informational)", lambda t: t.get("oi_rising", "unknown") or "False")
    lines += _breakdown_lines(resolved, "liquidation cluster (informational)", lambda t: t.get("liquidation_cluster", "unknown") or "False")
    lines += _breakdown_lines(resolved, "liquidation aligned (informational)", lambda t: t.get("liquidation_aligned", "unknown") or "False")
    lines += _breakdown_lines(resolved, "HTF trend", lambda t: t.get("htf_trend", "unknown") or "unknown")
    lines += _breakdown_lines(resolved, "order block present", lambda t: t.get("order_block_present", "unknown") or "False")
    lines += _breakdown_lines(resolved, "FVG present", lambda t: t.get("fvg_present", "unknown") or "False")
    lines += _breakdown_lines(resolved, "symbol", lambda t: t.get("symbol", "unknown"))

    return "\n".join(lines)


if __name__ == "__main__":
    print(summarize())
