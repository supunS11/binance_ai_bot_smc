"""Joins signal_journal.csv's signal rows to their outcome rows by
trade_id and breaks down win/breakeven/loss rates by the diagnostic
fields captured at signal time - the actual mechanism for answering "why
did these SL hit" with evidence instead of a guess.

Run directly on the machine with the real journal:
    python journal_analysis.py [hours_back]

`hours_back` restricts this to trades that CLOSED within that window -
use it to look only at trades resolved after a deploy/fix, since a fixed
number of stale pre-fix rows doesn't get diluted out just because more
trades pile up elsewhere. Omit it to include the whole journal.

Only trades logged after the trade_id correlation fix (this file's
counterpart change in signal_journal.py/position_manager.py) can be
joined - older rows without a trade_id are silently skipped, not guessed
at via symbol/time proximity.
"""
import csv
import time
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
    """Returns {trade_id: merged_row}. A signal row supplies every
    signal-time field; its later outcome row (same trade_id) fills in
    `outcome` plus whatever outcome-time-only fields it carries (symbol,
    mae_r_multiple, mfe_r_multiple). Real bug found live (2026-08-09):
    an earlier version of this only ever merged `outcome` from that row,
    so a trade with no matching signal row at all (e.g. one adopted via
    startup reconciliation after a restart) showed "unknown" for every
    single field - including symbol - even though the outcome row had it
    the whole time. Blank fields never overwrite an already-populated one
    on either side, since both rows only ever carry their own half of the
    data (signal-time fields are always blank on the outcome row, and
    vice versa)."""
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


_MAE_MFE_SANITY_BOUND = 1000  # R-multiples - see the outlier guard below


def _average_by_outcome(resolved, label, key, sanity_bound=_MAE_MFE_SANITY_BOUND):
    """Average of a numeric field (mae_r_multiple/mfe_r_multiple),
    grouped by WIN/BREAKEVEN/LOSS - the diagnostic that tells apart a
    LOSS that went wrong from the first tick (near-zero average MFE) from
    one that was solidly in profit before fully reversing (a large
    average MFE) - both look identical as a plain loss count.

    Real bug found live (2026-08-09): a since-fixed position_manager.py
    bug could re-derive the R-multiple's denominator from a moved
    sl_price instead of the original risk distance, producing values in
    the billions. Those rows are already permanently in the journal (this
    file's own schema didn't change, so no fresh-file migration clears
    them) - a plain average has no defense against an outlier that size,
    so anything beyond sanity_bound is excluded and reported, rather than
    silently included or silently dropped."""
    lines = [f"\nAverage {label} by outcome:"]
    sums = defaultdict(float)
    counts = defaultdict(int)
    excluded = 0

    for trade in resolved.values():
        try:
            value = float(trade.get(key))
        except (TypeError, ValueError):
            continue

        if abs(value) > sanity_bound:
            excluded += 1
            continue

        classification = classify(trade.get("outcome", ""))
        sums[classification] += value
        counts[classification] += 1

    for classification in ("WIN", "BREAKEVEN", "LOSS"):
        if counts[classification]:
            lines.append(
                f"  {classification}: avg={sums[classification] / counts[classification]:.3f}R "
                f"n={counts[classification]}"
            )

    if excluded:
        lines.append(
            f"  ({excluded} value(s) excluded as implausible outliers - "
            f"beyond {sanity_bound}R, almost certainly corrupted data)"
        )

    if len(lines) == 1:
        lines.append("  (no trades with this field recorded yet)")

    return lines


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


def _closed_at(trade):
    """The outcome row's timestamp (the trade's actual close time) - since
    load_trades() merges non-blank fields from both rows and the outcome
    row is always written later, "timestamp" in the merged record ends up
    being the close time, not the original signal time."""
    try:
        return float(trade.get("timestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize(journal_path=None, since_timestamp=None):
    """`since_timestamp` (unix seconds) restricts this to trades that
    CLOSED after that point - the way to look only at trades resolved
    after a fix/deploy, instead of a fixed-size batch of stale pre-fix
    rows quietly dragging down an average forever (real case found live,
    2026-08-09: a since-fixed MAE/MFE bug's corrupted rows don't get
    diluted out just because more trades pile up in a different outcome
    bucket - most of them need to be excluded outright)."""
    trades = load_trades(journal_path)
    resolved = {
        tid: t for tid, t in trades.items()
        if t.get("outcome") and (since_timestamp is None or _closed_at(t) >= since_timestamp)
    }

    if not resolved:
        return (
            "No resolved (closed) trades with a matching signal row yet"
            + (" in this window" if since_timestamp else "")
            + ". This needs trades opened after the trade_id correlation "
            "fix - let more run, then check again."
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
    lines += _breakdown_lines(resolved, "confluence score (drives position sizing)", lambda t: t.get("confluence_score", "unknown") or "0")
    lines += _breakdown_lines(resolved, "HTF trend", lambda t: t.get("htf_trend", "unknown") or "unknown")
    lines += _breakdown_lines(resolved, "order block present", lambda t: t.get("order_block_present", "unknown") or "False")
    lines += _breakdown_lines(resolved, "FVG present", lambda t: t.get("fvg_present", "unknown") or "False")
    lines += _breakdown_lines(resolved, "symbol", lambda t: t.get("symbol", "unknown"))

    lines += _average_by_outcome(resolved, "MAE (adverse excursion)", "mae_r_multiple")
    lines += _average_by_outcome(resolved, "MFE (favorable excursion)", "mfe_r_multiple")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    hours_back = float(sys.argv[1]) if len(sys.argv) > 1 else None
    since = (time.time() - hours_back * 3600) if hours_back else None
    print(summarize(since_timestamp=since))
