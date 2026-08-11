import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))


def env_bool(name, default="False"):
    return os.getenv(name, default).strip().lower() == "true"


def env_int(name, default):
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def env_float(name, default):
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def env_str_list(name, default):
    value = os.getenv(name)

    if value in (None, ""):
        return default

    result = [item.strip().upper() for item in value.split(",") if item.strip()]
    return result or default


# =========================
# EXCHANGE / ACCOUNT
# =========================
API_KEY = os.getenv("API_KEY", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
# When True, every REST call and websocket stream targets Binance's
# Futures Testnet (testnet.binancefuture.com / stream.binancefuture.com)
# instead of production - put your testnet API_KEY/SECRET_KEY above (they
# are a different key pair than your real account, generated separately
# at https://testnet.binancefuture.com).
BINANCE_TESTNET = env_bool("BINANCE_TESTNET", "False")

# =========================
# RATE LIMITING (ported convention from v7/v8 exchange.py)
# =========================
BINANCE_PUBLIC_WEIGHT_LIMIT_PER_MINUTE = env_float(
    "BINANCE_PUBLIC_WEIGHT_LIMIT_PER_MINUTE", 1800
)
BINANCE_PUBLIC_RATE_WINDOW_SECONDS = env_float(
    "BINANCE_PUBLIC_RATE_WINDOW_SECONDS", 60
)
KLINE_REQUEST_WEIGHT = env_float("KLINE_REQUEST_WEIGHT", 2)
EXCHANGE_INFO_REQUEST_WEIGHT = env_float("EXCHANGE_INFO_REQUEST_WEIGHT", 1)
# Minimum time between ANY two rate-limited REST calls (public, or private
# calls that pass a weight), regardless of weight. Real bug found live
# (2026-08-11): an unpaced per-symbol loop (80 symbols x 2 timeframes at
# startup) fired ~160 calls back-to-back - their summed weight was well
# under BINANCE_PUBLIC_WEIGHT_LIMIT_PER_MINUTE, so the weight budget alone
# never blocked it, but Binance still hard-banned the IP. A cumulative
# weight-per-minute budget can't catch a burst of many calls landing in
# the same second - this floor protects against that shape of bug in any
# caller, not just the one instance found so far. 0 disables it (weight
# budget alone, the original behavior).
BINANCE_MIN_REQUEST_GAP_SECONDS = env_float("BINANCE_MIN_REQUEST_GAP_SECONDS", 0.05)

# =========================
# SYMBOL UNIVERSE / WATCHLIST
# =========================
# Empty means "every tradable USDT-M perpetual" - resolved at startup via
# exchange.get_supported_symbols().
SCAN_SYMBOLS = env_str_list("SCAN_SYMBOLS", [])
QUOTE_ASSET = os.getenv("QUOTE_ASSET", "USDT")
# How many symbols get promoted from cheap kline-only scanning to the
# heavier depth+aggTrade websocket tier (mirrors v7's
# ORDER_FLOW_SHADOW_MAX_SYMBOLS pattern).
WATCHLIST_SIZE = env_int("WATCHLIST_SIZE", 15)
WATCHLIST_REFRESH_SECONDS = env_int("WATCHLIST_REFRESH_SECONDS", 300)
# How often ws_client refreshes the 24h-quote-volume map (a single bulk
# REST call covering every symbol, not per-symbol) that backs
# MIN_24H_QUOTE_VOLUME_USDT below.
VOLUME_POLL_INTERVAL_SECONDS = env_int("VOLUME_POLL_INTERVAL_SECONDS", 300)
# Signal-time liquidity floor - independent of watchlist selection, so a
# broad/unfiltered watchlist (e.g. SCAN_SYMBOLS pinned to the full 500+
# symbol universe) can still be scanned for structure without illiquid/
# vanity-ticker symbols actually being tradeable. 0 disables it. Real
# motivation (2026-08-11): reintroducing the full original symbol list
# for broader coverage reintroduced exactly the illiquid-symbol noise
# that narrowing the watchlist had removed - this restores that quality
# filter at signal time instead of watchlist time, so both can be tuned
# independently. Starting value is a reasonable floor, not yet calibrated
# against real trade data - revisit once there's evidence for where the
# real quality cutoff sits. A symbol with no volume data yet (poll hasn't
# completed, or the ticker endpoint has nothing for it) is let through
# rather than blocked - never gate on data we don't actually have.
MIN_24H_QUOTE_VOLUME_USDT = env_float("MIN_24H_QUOTE_VOLUME_USDT", 3000000)

# =========================
# WEBSOCKET DATA LAYER
# =========================
WS_ENABLED = env_bool("WS_ENABLED", "True")
WS_KLINE_INTERVAL = os.getenv("WS_KLINE_INTERVAL", "5m")
WS_KLINE_HISTORY_LIMIT = env_int("WS_KLINE_HISTORY_LIMIT", 200)
# HTF bias/dealing-range timeframe - separate stream+buffer from the LTF
# structure-trigger timeframe above.
HTF_KLINE_INTERVAL = os.getenv("HTF_KLINE_INTERVAL", "1h")
HTF_KLINE_HISTORY_LIMIT = env_int("HTF_KLINE_HISTORY_LIMIT", 200)
WS_STREAMS_PER_SOCKET = env_int("WS_STREAMS_PER_SOCKET", 100)
WS_DEPTH_STREAMS_PER_SOCKET = env_int("WS_DEPTH_STREAMS_PER_SOCKET", 50)
WS_DEPTH_LEVELS = os.getenv("WS_DEPTH_LEVELS", "20")
WS_DEPTH_SPEED_MS = os.getenv("WS_DEPTH_SPEED_MS", "100ms")
WS_STALE_SECONDS = env_float("WS_STALE_SECONDS", 45)
WS_WATCHDOG_INTERVAL_SECONDS = env_float("WS_WATCHDOG_INTERVAL_SECONDS", 15)
WS_RESTART_COOLDOWN_SECONDS = env_float("WS_RESTART_COOLDOWN_SECONDS", 30)

# =========================
# ORDER FLOW (CVD)
# =========================
ORDER_FLOW_MAX_WINDOW_SECONDS = env_int("ORDER_FLOW_MAX_WINDOW_SECONDS", 900)
ORDER_FLOW_MIN_NOTIONAL_USDT = env_float("ORDER_FLOW_MIN_NOTIONAL_USDT", 5000)
ORDER_FLOW_DIVERGENCE_LOOKBACK = env_int("ORDER_FLOW_DIVERGENCE_LOOKBACK", 20)

# =========================
# MARKET STRUCTURE (ICT/SMC)
# =========================
SWING_LEFT = env_int("SWING_LEFT", 2)
SWING_RIGHT = env_int("SWING_RIGHT", 2)
STRUCTURE_LOOKBACK_CANDLES = env_int("STRUCTURE_LOOKBACK_CANDLES", 150)
FVG_LOOKBACK_CANDLES = env_int("FVG_LOOKBACK_CANDLES", 50)
LIQUIDITY_POOL_TOLERANCE_PCT = env_float("LIQUIDITY_POOL_TOLERANCE_PCT", 0.001)
PREMIUM_DISCOUNT_LOOKBACK_CANDLES = env_int(
    "PREMIUM_DISCOUNT_LOOKBACK_CANDLES", 100
)
OTE_RETRACEMENT_MIN = env_float("OTE_RETRACEMENT_MIN", 0.618)
OTE_RETRACEMENT_MAX = env_float("OTE_RETRACEMENT_MAX", 0.79)
ATR_PERIOD = env_int("ATR_PERIOD", 14)

# =========================
# SIGNAL ENGINE - order-flow confirmation thresholds
# =========================
SIGNAL_MIN_CVD_SCORE = env_float("SIGNAL_MIN_CVD_SCORE", 0.15)
SIGNAL_MIN_DEPTH_IMBALANCE = env_float("SIGNAL_MIN_DEPTH_IMBALANCE", 0.10)
REQUIRE_ORDER_BLOCK_OR_FVG = env_bool("REQUIRE_ORDER_BLOCK_OR_FVG", "True")
# EMA alignment - the same direction-confirmation concept v7 uses (its
# EMA_WRONG_SIDE live-entry guard). Informational only, NOT a gate: an
# EMA is a lagging/smoothed indicator by construction, so requiring price
# already on its correct side can delay entry on a sharp move until price
# has run further - a real cost against this bot's real-time premise,
# and not yet backed by evidence it's worth paying. Computed and logged
# on every signal (ema_value, ema_aligned) so that evidence can
# accumulate before this is ever turned into a hard gate.
EMA_CONFIRMATION_ENABLED = env_bool("EMA_CONFIRMATION_ENABLED", "True")
EMA_CONFIRMATION_PERIOD = env_int("EMA_CONFIRMATION_PERIOD", 20)
# Open Interest - informational only, NOT a gate (same treatment as EMA
# above). OI rising during a directional break points at fresh
# positioning behind the move (new longs on a bullish break, new shorts
# on a bearish one); OI falling on the same break points at the opposite
# side closing out instead (short-covering / long-liquidation) - a real
# distinction, but with no evidence yet on how much it separates winners
# from losers here. Polled via REST since Binance has no public OI
# websocket stream (it changes far slower than kline/aggTrade anyway).
OI_CONFIRMATION_ENABLED = env_bool("OI_CONFIRMATION_ENABLED", "True")
OI_POLL_INTERVAL_SECONDS = env_int("OI_POLL_INTERVAL_SECONDS", 60)
OI_LOOKBACK_SECONDS = env_int("OI_LOOKBACK_SECONDS", 900)
OI_HISTORY_MAX_SAMPLES = env_int("OI_HISTORY_MAX_SAMPLES", 60)
# Evidence (2026-08-08, live, WATCHING=519): a large watchlist includes
# symbols the OI endpoint will never answer for - delisted/settling/
# pre-trading (-4108) or simply no longer valid (-1121, e.g. stale
# entries surviving in the hourly exchange-info cache). Left unhandled,
# these get retried and logged on every single poll cycle forever. Skip a
# symbol that just failed with one of these permanent-style errors for
# this long before trying it again, instead of hammering it every
# OI_POLL_INTERVAL_SECONDS indefinitely.
OI_UNAVAILABLE_SYMBOL_COOLDOWN_SECONDS = env_int("OI_UNAVAILABLE_SYMBOL_COOLDOWN_SECONDS", 3600)
# Liquidation clustering - informational only, NOT a gate. Real forced
# liquidations at/around a detected sweep are the closest confirmation
# ICT's "stop hunt" concept has to actual ground truth: a BULLISH break
# (stops below a low swept, then price reverses up) should show real
# long-liquidation flow if it was a genuine stop hunt, not just a wick.
# Ported from v7's proven liquidation_shadow.py (`!forceOrder@arr`
# combined stream), simplified to this bot's direct-lock engine style
# (order_flow.py/orderbook.py) instead of v7's queue+worker-thread
# version, which existed for a heavier multi-symbol shadow monitor than
# this bot's single evaluate-on-tick usage needs.
LIQUIDATION_CONFIRMATION_ENABLED = env_bool("LIQUIDATION_CONFIRMATION_ENABLED", "True")
LIQUIDATION_WINDOW_SECONDS = env_int("LIQUIDATION_WINDOW_SECONDS", 120)
LIQUIDATION_CLUSTER_MIN_NOTIONAL_USDT = env_float("LIQUIDATION_CLUSTER_MIN_NOTIONAL_USDT", 50000)
LIQUIDATION_MAX_EVENTS_PER_SYMBOL = env_int("LIQUIDATION_MAX_EVENTS_PER_SYMBOL", 200)
# Require the LTF candle that broke structure to have actually CLOSED
# beyond the level before entering, instead of reacting to a still-forming
# candle's wick - a real behavior change (this bot's original premise was
# reacting before candle close), not just another journaled field.
# Evidence (2026-08-11, 40 resolved trades post-early-breakeven): 60% of
# remaining LOSS trades were near-zero MFE (wrong from the first tick) -
# up from 38% before early breakeven started peeling off the other loss
# population, meaning entry timing is now the dominant unsolved driver.
# The break_confirmed_by_close journal field (observational only, added
# 2026-08-10) showed the same direction on a thin sample: wick-only breaks
# that didn't hold to their candle's close were 4/4 (100%) LOSS vs 53% for
# closed-confirmed breaks. Combined with standard ICT/SMC doctrine (a BOS/
# CHoCH is only real once confirmed by a closed candle), that's enough to
# ship this gated, not just log it. Costs up to one candle of entry
# latency. Reversible at zero cost if the next batch doesn't show
# separation - see market_structure.live_break_check.
REQUIRE_CLOSE_CONFIRMED_BREAK = env_bool("REQUIRE_CLOSE_CONFIRMED_BREAK", "True")

# =========================
# RISK MANAGEMENT (ported convention from v7/v8)
# =========================
MARGIN_PER_TRADE = env_float("MARGIN_PER_TRADE", 10)
LEVERAGE = env_int("LEVERAGE", 10)
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "ISOLATED")
RISK_BASED_POSITION_SIZING_ENABLED = env_bool(
    "RISK_BASED_POSITION_SIZING_ENABLED", "True"
)
POSITION_RISK_PCT = env_float("POSITION_RISK_PCT", 1.0)
POSITION_RISK_MAX_USDT = env_float("POSITION_RISK_MAX_USDT", 0)
STRUCTURE_STOP_ATR_BUFFER = env_float("STRUCTURE_STOP_ATR_BUFFER", 0.5)
# Hard floor: SL is never allowed closer to entry than this % of entry
# price, regardless of how close the structure level happened to land -
# prevents a pathologically tight stop (and the oversized position that
# risk-based sizing would produce to compensate for it). Evidence
# (2026-08-08, 79 resolved live trades): 68% hit SL despite CVD/sweep
# confirmation being statistically identical between winners and losers -
# ruling out signal-selection quality as the driver and pointing at the
# stop still being too tight for normal price movement even with the
# floor active (observed average stop distance on SL-hit trades was only
# ~0.45%). Raised from 0.3 -> 0.6 on that evidence.
MIN_STOP_DISTANCE_PCT = env_float("MIN_STOP_DISTANCE_PCT", 0.6)
# Confluence-weighted position sizing - see signal_engine.py's
# confluence_ratio (how many of sweep/EMA/OI/liquidation agree with the
# signal, out of how many were actually available to check). Scales the
# risk taken per trade instead of gating entry on any of these
# individually: every signal that qualifies today still trades, a
# 0-confluence one just risks less and a fully-aligned one risks more.
# Chosen over a hard gate specifically because it's testable against the
# existing trade count immediately (every trade gets sized, not just a
# rejected subset), reversible at zero cost if the score turns out
# uncorrelated with outcome, and can extract information from these
# fields collectively even before any one of them individually clears a
# significance bar on its own.
# DISABLED 2026-08-09 on real evidence: a 54-trade journal_analysis.py
# pull showed confluence_score trending flat-to-inverse against outcome
# (score=1 80% loss, score=2 96% loss, score=3 89% loss) - the opposite
# of what this multiplier assumes. Exercising the "reversible at zero
# cost" design above. confluence_score/ratio is still computed and
# journaled either way - re-enable only if a larger, cleaner sample
# (see MAE_TRACKING_ENABLED below) actually shows separation.
CONFLUENCE_SIZING_ENABLED = env_bool("CONFLUENCE_SIZING_ENABLED", "False")
CONFLUENCE_SIZING_MIN_MULTIPLIER = env_float("CONFLUENCE_SIZING_MIN_MULTIPLIER", 0.5)
CONFLUENCE_SIZING_MAX_MULTIPLIER = env_float("CONFLUENCE_SIZING_MAX_MULTIPLIER", 1.25)
MAX_TOTAL_POSITIONS = env_int("MAX_TOTAL_POSITIONS", 2)
# After ANY position closes (win, loss, or breakeven), that symbol is
# skipped for this long before it can be re-entered. Evidence (same
# 2026-08-08 review): RSRUSDT/SANDUSDT/TAIKOUSDT/SUSHIUSDT each hit SL
# repeatedly within seconds-to-minutes of the previous close, at nearly
# the same level - immediate re-entry into a symbol that's actively
# chopping instead of waiting for the picture to change.
SYMBOL_REENTRY_COOLDOWN_SECONDS = env_int("SYMBOL_REENTRY_COOLDOWN_SECONDS", 900)

# =========================
# TP1 / TP2 (mirrors v7's partial-TP + full-close ladder: TP1 closes
# TP1_CLOSE_PCT of the position and moves the remainder's stop to
# breakeven; TP2 closes what's left)
# =========================
TP1_CLOSE_PCT = env_float("TP1_CLOSE_PCT", 50)
TP1_R_MULTIPLE = env_float("TP1_R_MULTIPLE", 2.0)
TP2_R_MULTIPLE = env_float("TP2_R_MULTIPLE", 4.0)
# Upper bound on how far a real structure target is allowed to be. The
# R-multiples above are a MINIMUM room requirement - without a maximum
# too, "nearest qualifying pool" can still land absurdly far away if
# nothing closer exists (seen live: a ~20R target that's realistically
# never reached), silently turning TP1 into an unreachable target instead
# of an achievable first partial. Beyond this, the plain R-multiple
# fallback is used instead of the distant pool.
TP1_MAX_R_MULTIPLE = env_float("TP1_MAX_R_MULTIPLE", 6.0)
TP2_MAX_R_MULTIPLE = env_float("TP2_MAX_R_MULTIPLE", 10.0)
MOVE_SL_TO_BREAKEVEN_AFTER_TP1 = env_bool(
    "MOVE_SL_TO_BREAKEVEN_AFTER_TP1", "True"
)
BREAKEVEN_BUFFER_PCT = env_float("BREAKEVEN_BUFFER_PCT", 0.02)
# Early breakeven - protects profit on a trade before it reaches TP1,
# instead of leaving the original (wider) stop in place the whole way
# there. Originally gated on confluence_ratio (protect low-confidence
# trades faster) and disabled 2026-08-09 when real data showed confluence
# didn't correlate with outcome at all. Re-enabled 2026-08-10 with a
# different, evidence-backed trigger: a journal_analysis.py MAE/MFE
# distribution pull (61 resolved LOSS trades) showed a clear bimodal
# split - 38% of losses were near-zero MFE (wrong from the first tick,
# nothing here helps them), but 28% ran 1.0R+ in profit before fully
# reversing to a full loss, completely unprotected the whole way down
# since nothing moves the stop until TP1 formally triggers at 2R. This
# now applies to every trade still waiting on TP1 (not just low-
# confluence ones): once price has moved EARLY_BREAKEVEN_R_MULTIPLE R in
# its favor, the SL moves to breakeven. Known tradeoff: a genuine winner
# that dips back through breakeven on its way to a real TP1/TP2 would
# close early instead of running - real cost, not yet measured, weighed
# against the 28% of losses this targets. Does not change entry/trade
# count - same principle as the sizing feature: adapt what happens to a
# trade that's already happening, not whether it happens.
EARLY_BREAKEVEN_ENABLED = env_bool("EARLY_BREAKEVEN_ENABLED", "True")
EARLY_BREAKEVEN_R_MULTIPLE = env_float("EARLY_BREAKEVEN_R_MULTIPLE", 1.0)
# How much profit (as an R-multiple) to lock in when early breakeven
# promotes a trade, instead of moving the stop to flat entry (a scratch).
# 0 preserves the original flat-breakeven behavior (see
# risk_manager.compute_early_breakeven_price). Rationale (2026-08-11): the
# early-breakeven population so far is roughly half WIN, half BREAKEVEN
# with zero LOSS - a modest lock converts some of those scratches into
# small realized wins instead of leaving them at exactly zero. Known
# tradeoff, not yet measured: a genuine TP1/TP2 runner that dips slightly
# on its way to target now gets stopped out at this smaller locked amount
# instead of running further. Watch the WIN vs BREAKEVEN split among
# early_breakeven_applied=True trades after this ships - see the
# EARLY_BREAKEVEN_PROFIT_HIT outcome in journal_analysis.py.
EARLY_BREAKEVEN_LOCK_R_MULTIPLE = env_float("EARLY_BREAKEVEN_LOCK_R_MULTIPLE", 0.3)
# MAE/MFE (max adverse/favorable excursion) tracking - the diagnostic
# that's actually missing right now. A plain WIN/LOSS outcome can't tell
# apart a trade that was wrong from the first tick (near-zero MFE, went
# straight to the stop) from one that moved solidly in its favor and
# still reversed all the way back to a loss (large MFE) - those need
# completely different fixes (rework entry timing vs. tighten profit-
# taking/trailing). Tracks the worst/best price seen over a trade's life
# and journals both as an R-multiple of the original risk distance, so
# they're comparable across symbols/volatility regimes. Purely
# observational - never gates or sizes anything.
MAE_TRACKING_ENABLED = env_bool("MAE_TRACKING_ENABLED", "True")

# =========================
# EXECUTION
# =========================
# SHADOW: evaluate signals, size them, log to the journal - place no real
# orders. LIVE: actually enter/attach TP1/TP2/SL. Defaults to SHADOW so
# running this bot for the first time cannot place a real order by
# accident - flip explicitly once shadow signal quality has been reviewed.
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "SHADOW").strip().upper()
POSITION_POLL_INTERVAL_SECONDS = env_int("POSITION_POLL_INTERVAL_SECONDS", 10)
SIGNAL_EVAL_INTERVAL_SECONDS = env_int("SIGNAL_EVAL_INTERVAL_SECONDS", 5)
# Used only in SHADOW mode so risk-based position sizing has a balance to
# size against without needing real API keys / an authenticated call.
SHADOW_ACCOUNT_BALANCE_USDT = env_float("SHADOW_ACCOUNT_BALANCE_USDT", 1000)

# =========================
# LOGGING / ALERTING
# =========================
TELEGRAM_ENABLED = env_bool("TELEGRAM_ENABLED", "False")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT_SECONDS = env_float("TELEGRAM_TIMEOUT_SECONDS", 10)
