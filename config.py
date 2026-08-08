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
