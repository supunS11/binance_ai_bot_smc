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
STRUCTURE_STOP_ATR_BUFFER = env_float("STRUCTURE_STOP_ATR_BUFFER", 0.15)
MAX_TOTAL_POSITIONS = env_int("MAX_TOTAL_POSITIONS", 2)

# =========================
# TP1 / TP2 (mirrors v7's partial-TP + full-close ladder: TP1 closes
# TP1_CLOSE_PCT of the position and moves the remainder's stop to
# breakeven; TP2 closes what's left)
# =========================
TP1_CLOSE_PCT = env_float("TP1_CLOSE_PCT", 50)
TP1_R_MULTIPLE = env_float("TP1_R_MULTIPLE", 1.0)
TP2_R_MULTIPLE = env_float("TP2_R_MULTIPLE", 2.0)
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
