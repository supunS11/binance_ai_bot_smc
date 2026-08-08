"""Generic Binance USDT-M futures REST plumbing.

Ported from v7/v8's exchange.py: only the parts that are pure exchange
mechanics (rate limiting, backoff, exchange-info/symbol-rules, historical
kline fetch) - not the trend-following strategy's order-lifecycle/DCA
reconciliation logic, which belongs to a strategy-specific execution layer
built in a later phase, not here.
"""
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
import re
import threading
import time

import pandas as pd
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL

import config
from logger import log_error, log_info, log_warning


client = Client(config.API_KEY, config.SECRET_KEY, ping=False)

if config.BINANCE_TESTNET:
    # python-binance's own `testnet=` constructor flag does NOT redirect
    # USDS-M futures REST calls in this version - it only affects a
    # separate websocket-API client, and FUTURES_URL is left pointed at
    # production regardless (verified empirically: client.FUTURES_URL
    # stays "https://fapi.binance.com/fapi" even with testnet=True).
    # Overriding it directly is the only way that actually works.
    client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

_exchange_info_cache = None
_exchange_info_cache_at = 0.0
_exchange_info_ttl_seconds = 3600

_public_request_weights = deque()
_public_request_lock = threading.Lock()

_public_rest_backoff_until = 0.0
_public_rest_lock = threading.Lock()
_public_rest_log_times = {}

_kline_cache = {}
_kline_cache_lock = threading.RLock()
_kline_cache_ttl_seconds = 5

_BAN_UNTIL_RE = re.compile(r"banned until\s+(\d+)", re.IGNORECASE)
_RATE_LIMIT_RE = re.compile(r"(code=-1003|too many requests)", re.IGNORECASE)


def sync_client_time():
    try:
        server_time = client.get_server_time()
        client.timestamp_offset = (
            server_time["serverTime"] - int(time.time() * 1000)
        )
        return True

    except Exception as exc:
        client.timestamp_offset = 0
        log_warning(
            "Binance startup time sync unavailable; "
            "using zero timestamp offset | "
            f"ERROR={exc}"
        )
        return False


# =========================
# RATE LIMITING / BACKOFF (ported convention from v7/v8)
# =========================
def _rate_limit_public_request(weight=1):
    max_weight = float(config.BINANCE_PUBLIC_WEIGHT_LIMIT_PER_MINUTE)

    if max_weight <= 0:
        return

    window_seconds = max(float(config.BINANCE_PUBLIC_RATE_WINDOW_SECONDS), 1.0)
    weight = max(float(weight or 1), 0.1)

    while True:
        now = time.time()

        with _public_request_lock:
            while (
                _public_request_weights
                and now - _public_request_weights[0][0] >= window_seconds
            ):
                _public_request_weights.popleft()

            used_weight = sum(item[1] for item in _public_request_weights)

            if used_weight + weight <= max_weight:
                _public_request_weights.append((now, weight))
                return

            oldest_at = _public_request_weights[0][0]
            wait_seconds = window_seconds - (now - oldest_at) + 0.05

        time.sleep(min(max(wait_seconds, 0.05), 5.0))


def _public_rest_log_allowed(key, cooldown=30):
    now = time.time()

    with _public_rest_lock:
        last_logged_at = _public_rest_log_times.get(key, 0.0)

        if now - last_logged_at < cooldown:
            return False

        _public_rest_log_times[key] = now
        return True


def _extract_public_rest_backoff_seconds(error):
    message = str(error)
    buffer_seconds = 60.0
    match = _BAN_UNTIL_RE.search(message)

    if match:
        try:
            banned_until_ms = int(match.group(1))
            banned_until_seconds = banned_until_ms / 1000
            return max(
                banned_until_seconds - time.time() + buffer_seconds,
                buffer_seconds,
            )
        except (TypeError, ValueError):
            pass

    if _RATE_LIMIT_RE.search(message):
        return 300.0

    return 0.0


def _set_public_rest_backoff(error, context):
    global _public_rest_backoff_until

    pause_seconds = _extract_public_rest_backoff_seconds(error)

    if pause_seconds <= 0:
        return

    until = time.time() + pause_seconds

    with _public_rest_lock:
        _public_rest_backoff_until = max(_public_rest_backoff_until, until)

    if _public_rest_log_allowed("public_rest_backoff_set"):
        log_warning(
            "Binance public REST backoff active | "
            f"CALL={context} | PAUSE_SECONDS={round(pause_seconds, 1)} | "
            f"ERROR={error}"
        )


def get_public_rest_backoff_remaining():
    with _public_rest_lock:
        return max(_public_rest_backoff_until - time.time(), 0.0)


def is_public_rest_backoff_active():
    return get_public_rest_backoff_remaining() > 0


def _raise_if_public_rest_backoff(context):
    remaining = get_public_rest_backoff_remaining()

    if remaining <= 0:
        return

    raise RuntimeError(
        "Binance public REST backoff active | "
        f"CALL={context} | WAIT_SECONDS={round(remaining, 1)}"
    )


def _is_public_rest_backoff_error(error):
    return "Binance public REST backoff active" in str(error)


def _public_rest_call(context, func, *args, weight=1, **kwargs):
    _raise_if_public_rest_backoff(context)
    _rate_limit_public_request(weight)

    try:
        return func(*args, **kwargs)
    except Exception as exc:
        if not _is_public_rest_backoff_error(exc):
            _set_public_rest_backoff(exc, context)
        raise


# =========================
# EXCHANGE INFO / SYMBOL RULES
# =========================
def get_exchange_info(force=False):
    global _exchange_info_cache, _exchange_info_cache_at

    now = time.time()

    if (
        force
        or _exchange_info_cache is None
        or now - _exchange_info_cache_at >= _exchange_info_ttl_seconds
    ):
        _exchange_info_cache = _public_rest_call(
            "futures_exchange_info",
            client.futures_exchange_info,
            weight=config.EXCHANGE_INFO_REQUEST_WEIGHT,
        )
        _exchange_info_cache_at = now

    return _exchange_info_cache


def get_supported_symbols(quote_asset=None):
    quote_asset = (quote_asset or config.QUOTE_ASSET).upper()

    try:
        symbols = set()

        for item in get_exchange_info().get("symbols", []):
            if item.get("status") != "TRADING":
                continue

            if item.get("contractType") != "PERPETUAL":
                continue

            if item.get("quoteAsset") != quote_asset:
                continue

            symbols.add(item["symbol"])

        return symbols

    except Exception as exc:
        log_error(f"supported symbols error: {exc}")
        return set()


def get_symbol_precision(symbol):
    info = get_exchange_info()

    for item in info.get("symbols", []):
        if item.get("symbol") == symbol:
            return item.get("quantityPrecision", 3)

    return 3


def get_price_precision(symbol):
    info = get_exchange_info()

    for item in info.get("symbols", []):
        if item.get("symbol") == symbol:
            return int(item.get("pricePrecision", 4))

    return 4


def get_symbol_price_rules(symbol):
    try:
        for item in get_exchange_info().get("symbols", []):
            if item.get("symbol") != symbol:
                continue

            filters = {
                entry.get("filterType"): entry
                for entry in item.get("filters", [])
            }
            price_filter = filters.get("PRICE_FILTER") or {}
            return {
                "available": True,
                "tick_size": price_filter.get("tickSize", "0"),
                "min_price": price_filter.get("minPrice", "0"),
                "max_price": price_filter.get("maxPrice", "0"),
                "precision": int(item.get("pricePrecision", 4)),
            }

    except Exception as exc:
        log_warning(f"{symbol} price rule lookup warning: {exc}")

    return {
        "available": False,
        "tick_size": "0",
        "min_price": "0",
        "max_price": "0",
        "precision": 8,
    }


# =========================
# KLINES (REST - used to seed history before the websocket feed catches
# up; the live/forming candle for signal detection comes from ws_client)
# =========================
def _kline_cache_key(symbol, interval, limit):
    return (symbol, interval, int(limit))


def _get_cached_kline_df(key):
    with _kline_cache_lock:
        entry = _kline_cache.get(key)

    if entry is None:
        return None

    stored_at, df = entry

    if time.time() - stored_at >= _kline_cache_ttl_seconds:
        return None

    return df.copy(deep=True)


def _store_cached_kline_df(key, df):
    with _kline_cache_lock:
        _kline_cache[key] = (time.time(), df.copy(deep=True))


def get_klines(symbol, interval, limit=200):
    try:
        cache_key = _kline_cache_key(symbol, interval, limit)
        cached = _get_cached_kline_df(cache_key)

        if cached is not None:
            return cached

        _raise_if_public_rest_backoff("klines")
        _rate_limit_public_request(config.KLINE_REQUEST_WEIGHT)

        klines = client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        df = pd.DataFrame(klines, columns=[
            'time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'qav', 'trades', 'tbbav', 'tbqav', 'ignore'
        ])

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        _store_cached_kline_df(cache_key, df)
        return df.copy(deep=True)

    except Exception as exc:
        if _is_public_rest_backoff_error(exc):
            return None

        _set_public_rest_backoff(exc, f"futures_klines:{symbol}:{interval}")
        log_error(f"{symbol} klines error: {exc}")
        return None


def get_symbol_price_rules(symbol):
    try:
        for item in get_exchange_info().get("symbols", []):
            if item.get("symbol") != symbol:
                continue

            filters = {
                entry.get("filterType"): entry
                for entry in item.get("filters", [])
            }
            price_filter = filters.get("PRICE_FILTER") or {}
            return {
                "available": True,
                "tick_size": price_filter.get("tickSize", "0"),
                "min_price": price_filter.get("minPrice", "0"),
                "max_price": price_filter.get("maxPrice", "0"),
                "precision": int(item.get("pricePrecision", 4)),
            }

    except Exception as exc:
        log_warning(f"{symbol} price rule lookup warning: {exc}")

    return {
        "available": False,
        "tick_size": "0",
        "min_price": "0",
        "max_price": "0",
        "precision": 8,
    }


def normalize_order_price(symbol, price, rounding="nearest"):
    try:
        rules = get_symbol_price_rules(symbol)

        if not rules.get("available", True):
            return 0.0

        value = Decimal(str(float(price)))
        tick = Decimal(str(rules.get("tick_size") or "0"))
        minimum = Decimal(str(rules.get("min_price") or "0"))
        maximum = Decimal(str(rules.get("max_price") or "0"))

        if value <= 0:
            return 0.0

        if tick <= 0:
            precision = max(int(rules.get("precision", 4)), 0)
            return round(float(value), precision)

        rounding_mode = {
            "down": ROUND_DOWN,
            "up": ROUND_UP,
            "nearest": ROUND_HALF_UP,
        }.get(str(rounding or "nearest").lower(), ROUND_HALF_UP)
        steps = (value / tick).to_integral_value(rounding=rounding_mode)
        normalized = steps * tick

        if minimum > 0:
            normalized = max(normalized, minimum)

        if maximum > 0:
            normalized = min(normalized, maximum)

        return float(normalized)

    except (InvalidOperation, TypeError, ValueError) as exc:
        log_warning(f"{symbol} price normalization warning: {exc}")
        return 0.0


def normalize_trigger_price(symbol, side, order_type, price):
    """Round a stop/TP trigger away from the market on the side that
    would otherwise make it trigger immediately - SL rounds against the
    position, TP rounds against the position too but in the opposite
    direction, matching Binance's own trigger-validation direction."""
    side = str(side or "").upper()
    order_type = str(order_type or "").upper()
    take_profit = "TAKE_PROFIT" in order_type

    if side == SIDE_BUY:
        rounding = "down" if take_profit else "up"
    else:
        rounding = "up" if take_profit else "down"

    return normalize_order_price(symbol, price, rounding=rounding)


def get_symbol_quantity_rules(symbol, order_type="MARKET"):
    try:
        for item in get_exchange_info().get("symbols", []):
            if item.get("symbol") != symbol:
                continue

            filters = {
                entry.get("filterType"): entry
                for entry in item.get("filters", [])
            }
            order_type = str(order_type or "MARKET").upper()
            lot_size = filters.get("LOT_SIZE") or {}

            if order_type == "MARKET":
                lot_size = filters.get("MARKET_LOT_SIZE") or lot_size or {}
                max_qty = lot_size.get("maxQty", "0")
            else:
                # Algo/conditional orders that carry an explicit quantity
                # (TAKE_PROFIT_MARKET's partial TP1 leg) execute as a
                # MARKET order once triggered - confirmed live (XAIUSDT,
                # 2026-08-08): a TP1 quantity that passed LOT_SIZE's max
                # alone still got rejected server-side with -4005 "Quantity
                # greater than max quantity". Clamp to whichever of
                # LOT_SIZE/MARKET_LOT_SIZE is tighter instead of assuming
                # which one Binance enforces for this undocumented case.
                market_lot_size = filters.get("MARKET_LOT_SIZE") or {}
                candidates = [
                    value for value in (
                        _safe_float_local(lot_size.get("maxQty")),
                        _safe_float_local(market_lot_size.get("maxQty")),
                    )
                    if value > 0
                ]
                max_qty = str(min(candidates)) if candidates else lot_size.get("maxQty", "0")

            return {
                "available": True,
                "step_size": lot_size.get("stepSize", "1"),
                "min_qty": lot_size.get("minQty", "0"),
                "max_qty": max_qty,
                "precision": int(item.get("quantityPrecision", 3)),
            }
    except Exception as exc:
        log_warning(f"{symbol} quantity rule lookup warning: {exc}")

    return {
        "available": False,
        "step_size": "0",
        "min_qty": "0",
        "max_qty": "0",
        "precision": 8,
    }


def get_symbol_max_order_quantity(symbol):
    """The most restrictive max order quantity across every order type
    this bot actually places for a symbol over a trade's lifecycle
    (MARKET for entry, TAKE_PROFIT_MARKET for the TP1 partial). Sizing a
    position against only one of these lets the other get rejected later
    with -4005 once TP1 is a fraction of an entry quantity that was only
    ever checked against its own filter - size against the tightest limit
    up front instead."""
    market_max = _safe_float_local(get_symbol_quantity_rules(symbol, order_type="MARKET").get("max_qty"))
    algo_max = _safe_float_local(get_symbol_quantity_rules(symbol, order_type="CONDITIONAL").get("max_qty"))
    candidates = [value for value in (market_max, algo_max) if value > 0]
    return min(candidates) if candidates else 0.0


def normalize_order_quantity(symbol, quantity, order_type="MARKET"):
    try:
        rules = get_symbol_quantity_rules(symbol, order_type=order_type)

        if not rules.get("available", True):
            return 0.0

        value = Decimal(str(abs(float(quantity))))
        step = Decimal(str(rules.get("step_size") or "1"))
        min_qty = Decimal(str(rules.get("min_qty") or "0"))
        max_qty = Decimal(str(rules.get("max_qty") or "0"))

        if step > 0:
            value = (value / step).to_integral_value(rounding=ROUND_DOWN) * step

        if value < min_qty or value <= 0:
            return 0.0

        if max_qty > 0:
            value = min(value, max_qty)

        precision = max(int(rules.get("precision", 3)), 0)
        quantum = Decimal("1").scaleb(-precision)
        value = value.quantize(quantum, rounding=ROUND_DOWN)
        return float(value)

    except (InvalidOperation, TypeError, ValueError) as exc:
        log_warning(f"{symbol} quantity normalization warning: {exc}")
        return 0.0


def get_mark_price(symbol):
    try:
        mark = _public_rest_call(
            f"futures_mark_price:{symbol}",
            client.futures_mark_price,
            symbol=symbol,
            weight=1,
        )
        return float(mark["markPrice"])

    except Exception as exc:
        if _is_public_rest_backoff_error(exc):
            return None

        _set_public_rest_backoff(exc, f"futures_mark_price:{symbol}")
        log_error(f"{symbol} mark price error: {exc}")
        return None


# =========================
# PRIVATE REST (account/orders) - same weight-budget + backoff convention
# as the public layer above, applied to authenticated endpoints.
# =========================
_private_rest_backoff_until = 0.0
_private_rest_lock = threading.Lock()
_private_rest_log_times = {}


def _private_rest_log_allowed(key, cooldown=30):
    now = time.time()

    with _private_rest_lock:
        last_logged_at = _private_rest_log_times.get(key, 0.0)

        if now - last_logged_at < cooldown:
            return False

        _private_rest_log_times[key] = now
        return True


def _extract_private_rest_backoff_seconds(error):
    message = str(error)
    buffer_seconds = 60.0
    match = _BAN_UNTIL_RE.search(message)

    if match:
        try:
            banned_until_ms = int(match.group(1))
            banned_until_seconds = banned_until_ms / 1000
            return max(
                banned_until_seconds - time.time() + buffer_seconds,
                buffer_seconds,
            )
        except (TypeError, ValueError):
            pass

    if _RATE_LIMIT_RE.search(message):
        return 300.0

    return 0.0


def _set_private_rest_backoff(error, context):
    global _private_rest_backoff_until

    pause_seconds = _extract_private_rest_backoff_seconds(error)

    if pause_seconds <= 0:
        return

    until = time.time() + pause_seconds

    with _private_rest_lock:
        _private_rest_backoff_until = max(_private_rest_backoff_until, until)

    if _private_rest_log_allowed("private_rest_backoff_set"):
        log_warning(
            "Binance private REST backoff active | "
            f"CALL={context} | PAUSE_SECONDS={round(pause_seconds, 1)} | "
            f"ERROR={error}"
        )


def get_private_rest_backoff_remaining():
    with _private_rest_lock:
        return max(_private_rest_backoff_until - time.time(), 0.0)


def is_private_rest_backoff_active():
    return get_private_rest_backoff_remaining() > 0


def _raise_if_private_rest_backoff(context):
    remaining = get_private_rest_backoff_remaining()

    if remaining <= 0:
        return

    raise RuntimeError(
        "Binance private REST backoff active | "
        f"CALL={context} | WAIT_SECONDS={round(remaining, 1)}"
    )


def _private_rest_call(context, func, *args, weight=0, **kwargs):
    _raise_if_private_rest_backoff(context)

    if weight > 0:
        _rate_limit_public_request(weight)

    try:
        return func(*args, **kwargs)
    except Exception as exc:
        _set_private_rest_backoff(exc, context)
        raise


# =========================
# ACCOUNT / POSITIONS
# =========================
_balance_cache = {"time": 0.0, "value": None}
_balance_cache_lock = threading.Lock()
_balance_cache_ttl_seconds = 5


def get_balance(force=False):
    """USDT futures wallet balance, short-TTL cached so risk sizing and a
    position-manager poll loop don't each trigger their own REST call."""
    now = time.time()

    with _balance_cache_lock:
        cached_at, cached_value = _balance_cache["time"], _balance_cache["value"]

    if not force and cached_value is not None and now - cached_at < _balance_cache_ttl_seconds:
        return cached_value

    try:
        balances = _private_rest_call(
            "futures_account_balance",
            client.futures_account_balance,
        )

        for item in balances:
            if item.get("asset") == "USDT":
                value = float(item["balance"])

                with _balance_cache_lock:
                    _balance_cache["time"] = now
                    _balance_cache["value"] = value

                return value

        return 0.0

    except Exception as exc:
        log_error(f"balance fetch error: {exc}")
        return cached_value if cached_value is not None else 0.0


def _fetch_open_position_detail(symbol):
    """Raises on a REST failure instead of swallowing it - callers that
    need to tell "confirmed flat" apart from "couldn't check right now"
    (e.g. deciding whether to give up on a stop replacement) should call
    this directly rather than get_open_position_detail, which collapses
    both cases to None."""
    positions = _private_rest_call(
        f"futures_position_information:{symbol}",
        client.futures_position_information,
        symbol=symbol,
    )

    for position in positions or []:
        amount = float(position.get("positionAmt", 0) or 0)

        if amount == 0:
            continue

        return {
            "symbol": symbol,
            "amount": amount,
            "side": "BUY" if amount > 0 else "SELL",
            "quantity": abs(amount),
            "entry_price": float(position.get("entryPrice", 0) or 0),
            "mark_price": float(position.get("markPrice", 0) or 0),
            "unrealized_pnl": float(position.get("unRealizedProfit", 0) or 0),
        }

    return None


def get_open_position_detail(symbol, force=False):
    """Single-symbol position snapshot, one-way (non-hedge) mode only."""
    try:
        return _fetch_open_position_detail(symbol)
    except Exception as exc:
        log_error(f"{symbol} position fetch error: {exc}")
        return None


def has_open_position(symbol, force=True):
    return get_open_position_detail(symbol, force=force) is not None


def get_all_open_positions():
    """Every non-zero position on the account, account-wide, in one call
    - used for startup reconciliation instead of checking symbol by
    symbol (and catches a position outside the current watchlist too, if
    SCAN_SYMBOLS changed since it was opened)."""
    try:
        positions = _private_rest_call(
            "futures_position_information:all",
            client.futures_position_information,
        )
        result = []

        for position in positions or []:
            amount = float(position.get("positionAmt", 0) or 0)

            if amount == 0:
                continue

            result.append({
                "symbol": position["symbol"],
                "side": "BUY" if amount > 0 else "SELL",
                "quantity": abs(amount),
                "entry_price": float(position.get("entryPrice", 0) or 0),
            })

        return result

    except Exception as exc:
        log_error(f"account-wide open positions fetch error: {exc}")
        return []


_OI_UNAVAILABLE_SYMBOL_RE = re.compile(r"code=-4108|code=-1121")
_oi_unavailable_symbols = {}
_oi_unavailable_lock = threading.Lock()


def get_open_interest(symbol):
    """Current total open interest (in contracts) for a symbol. Binance
    has no public OI websocket stream, so this is REST-polled - see
    open_interest.py, which turns a series of these into a % change.

    A symbol that's delisted/settling/pre-trading (-4108) or simply no
    longer a valid symbol (-1121 - can outlive the hourly exchange-info
    cache) will never answer here - skip it for
    OI_UNAVAILABLE_SYMBOL_COOLDOWN_SECONDS after the first such failure
    instead of hitting the same permanent error on every single poll."""
    now = time.time()

    with _oi_unavailable_lock:
        unavailable_until = _oi_unavailable_symbols.get(symbol, 0.0)

    if now < unavailable_until:
        return None

    try:
        data = _public_rest_call(
            f"futures_open_interest:{symbol}",
            client.futures_open_interest,
            symbol=symbol,
            weight=1,
        )

        with _oi_unavailable_lock:
            _oi_unavailable_symbols.pop(symbol, None)

        return float(data["openInterest"])

    except Exception as exc:
        if _is_public_rest_backoff_error(exc):
            return None

        if _OI_UNAVAILABLE_SYMBOL_RE.search(str(exc)):
            cooldown = max(float(config.OI_UNAVAILABLE_SYMBOL_COOLDOWN_SECONDS), 0)

            with _oi_unavailable_lock:
                _oi_unavailable_symbols[symbol] = now + cooldown

            if _public_rest_log_allowed(f"oi_unavailable:{symbol}", cooldown=cooldown):
                log_warning(
                    f"{symbol} open interest unavailable (delisted/settling/invalid) - "
                    f"skipping for {int(cooldown / 60)}m | ERROR={exc}"
                )

            return None

        _set_public_rest_backoff(exc, f"futures_open_interest:{symbol}")
        log_error(f"{symbol} open interest error: {exc}")
        return None


def get_open_algo_orders(symbol):
    """Every open algo/conditional order (our SL/TP1/TP2) for a symbol -
    used to rebuild position tracking on startup reconciliation."""
    method = getattr(client, "futures_get_open_algo_orders", None)

    try:
        if method:
            response = _private_rest_call(
                f"futures_get_open_algo_orders:{symbol}",
                method,
                symbol=symbol,
            )
        else:
            response = _private_rest_call(
                f"futures_get_open_algo_orders:{symbol}",
                client._request_futures_api,
                "get",
                "openAlgoOrders",
                True,
                data={"symbol": symbol},
            )

        if isinstance(response, dict):
            # v7's proven-working _normalise_algo_orders checks both keys -
            # the wrapper key isn't consistently "data".
            wrapped = response.get("orders") or response.get("data")
            return wrapped if isinstance(wrapped, list) else []

        return response if isinstance(response, list) else []

    except Exception as exc:
        log_error(f"{symbol} open algo orders fetch error: {exc}")
        return []


def setup_leverage(symbol):
    try:
        response = _private_rest_call(
            f"futures_change_leverage:{symbol}",
            client.futures_change_leverage,
            symbol=symbol,
            leverage=config.LEVERAGE,
        )
        actual = int(response["leverage"])

        if actual != config.LEVERAGE:
            log_warning(f"{symbol} leverage mismatch | REQUESTED={config.LEVERAGE} | ACTUAL={actual}")
            return False

        log_info(f"{symbol} leverage set: {actual}x")
        return True

    except Exception as exc:
        log_error(f"{symbol} leverage error: {exc}")
        return False


# =========================
# ORDERS - entry, and TP/SL via the algo-order (conditional) endpoint,
# same convention v7/v8 use: STOP_MARKET/TAKE_PROFIT_MARKET, MARK_PRICE
# working type, priceProtect on, one-way mode (reduceOnly instead of a
# positionSide close).
# =========================
def place_market_order(symbol, side, quantity):
    quantity = normalize_order_quantity(symbol, quantity, order_type="MARKET")

    if quantity <= 0:
        raise ValueError(f"{symbol} entry quantity normalized to zero")

    return _private_rest_call(
        f"futures_create_order:{symbol}",
        client.futures_create_order,
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=quantity,
    )


def close_position_market(symbol, position_side, quantity):
    """Immediately market-close `quantity` of an open position.
    `position_side` is the position's own side (BUY/SELL, not the closing
    order's side) - the closing order is placed on the opposite side with
    reduceOnly, so it can never accidentally open a new position in the
    other direction."""
    close_side = SIDE_SELL if position_side == SIDE_BUY else SIDE_BUY
    quantity = normalize_order_quantity(symbol, quantity, order_type="MARKET")

    if quantity <= 0:
        raise ValueError(f"{symbol} close quantity normalized to zero")

    return _private_rest_call(
        f"futures_create_order:{symbol}:close",
        client.futures_create_order,
        symbol=symbol,
        side=close_side,
        type="MARKET",
        quantity=quantity,
        reduceOnly="true",
    )


def place_algo_order(**params):
    """SL/TP conditional orders go through Binance's algo-order namespace
    (same as v7/v8), not the plain order endpoint - `algoStatus="FINISHED"`
    on this namespace is the only reliable signal that a stop/TP actually
    triggered, vs. `CANCELED`/`EXPIRED` because something else closed the
    position first."""
    method = getattr(client, "futures_create_algo_order", None)

    if method:
        return _private_rest_call(
            f"futures_create_algo_order:{params.get('symbol', 'unknown')}",
            method,
            **params,
        )

    return _private_rest_call(
        f"futures_create_algo_order:{params.get('symbol', 'unknown')}",
        client._request_futures_api,
        "post",
        "algoOrder",
        True,
        data=params,
    )


def _accepted_order_id(order):
    order = order or {}
    status = str(order.get("status") or order.get("algoStatus") or "").upper()

    if status in {"REJECTED", "EXPIRED", "CANCELED", "CANCELLED", "FAILED"}:
        return ""

    return order.get("orderId") or order.get("algoId") or ""


def place_stop_loss(symbol, side, trigger_price):
    """Full-position STOP_MARKET, closePosition=true - always closes
    everything still open on that symbol, so it stays valid through a TP1
    partial fill without needing to be resized."""
    close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
    trigger_price = normalize_trigger_price(symbol, side, "STOP_MARKET", trigger_price)

    if trigger_price <= 0:
        raise ValueError(f"{symbol} SL trigger price is invalid")

    return place_algo_order(
        symbol=symbol,
        side=close_side,
        type="STOP_MARKET",
        triggerPrice=trigger_price,
        closePosition="true",
        workingType="MARK_PRICE",
        priceProtect="TRUE",
        timeInForce="GTC",
    )


def place_take_profit_partial(symbol, side, quantity, trigger_price):
    """Reduce-only TAKE_PROFIT_MARKET for an exact quantity - this is TP1,
    closing part of the position and leaving the rest open."""
    close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
    quantity = normalize_order_quantity(symbol, quantity, order_type="CONDITIONAL")
    trigger_price = normalize_trigger_price(symbol, side, "TAKE_PROFIT_MARKET", trigger_price)

    if quantity <= 0:
        raise ValueError(f"{symbol} TP1 quantity normalized to zero")

    if trigger_price <= 0:
        raise ValueError(f"{symbol} TP1 trigger price is invalid")

    return place_algo_order(
        symbol=symbol,
        side=close_side,
        type="TAKE_PROFIT_MARKET",
        triggerPrice=trigger_price,
        quantity=quantity,
        reduceOnly="true",
        workingType="MARK_PRICE",
        priceProtect="TRUE",
        timeInForce="GTC",
    )


def place_take_profit_full(symbol, side, trigger_price):
    """Full-position TAKE_PROFIT_MARKET, closePosition=true - this is TP2,
    closing whatever remains after TP1."""
    close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
    trigger_price = normalize_trigger_price(symbol, side, "TAKE_PROFIT_MARKET", trigger_price)

    if trigger_price <= 0:
        raise ValueError(f"{symbol} TP2 trigger price is invalid")

    return place_algo_order(
        symbol=symbol,
        side=close_side,
        type="TAKE_PROFIT_MARKET",
        triggerPrice=trigger_price,
        closePosition="true",
        workingType="MARK_PRICE",
        priceProtect="TRUE",
        timeInForce="GTC",
    )


def cancel_algo_order(symbol, algo_id):
    method = getattr(client, "futures_cancel_algo_order", None)

    try:
        if method:
            return _private_rest_call(
                f"futures_cancel_algo_order:{symbol}",
                method,
                symbol=symbol,
                algoId=algo_id,
            )

        return _private_rest_call(
            f"futures_cancel_algo_order:{symbol}",
            client._request_futures_api,
            "delete",
            "algoOrder",
            True,
            data={"symbol": symbol, "algoId": algo_id},
        )
    except Exception as exc:
        log_warning(f"{symbol} algo order {algo_id} cancel warning: {exc}")
        return None


def cancel_all_open_orders(symbol):
    """Cancel every open order for a symbol - both regular orders and
    algo/conditional orders, which is what our SL/TP1/TP2 actually are
    (Binance treats these as two separate cancel-all endpoints, not one -
    calling only the regular one silently leaves SL/TP orders in place).
    Used whenever a position closes, so a stray or improperly-tracked
    order can never survive on the exchange regardless of whether this
    bot's own order-id bookkeeping is accurate at that moment."""
    try:
        _private_rest_call(
            f"futures_cancel_all_open_orders:{symbol}",
            client.futures_cancel_all_open_orders,
            symbol=symbol,
        )
    except Exception as exc:
        log_warning(f"{symbol} cancel-all regular orders warning: {exc}")

    try:
        _private_rest_call(
            f"futures_cancel_all_open_orders:{symbol}:conditional",
            client.futures_cancel_all_open_orders,
            symbol=symbol,
            conditional=True,
        )
    except Exception as exc:
        log_warning(f"{symbol} cancel-all algo orders warning: {exc}")


def get_algo_order_status(symbol, algo_id):
    """`FINISHED` = genuinely triggered. `CANCELED`/`EXPIRED` = removed
    without triggering (e.g. the other leg of the OCO pair filled first).
    `NOT_FOUND` after a cancel call is the expected/successful outcome."""
    method = getattr(client, "futures_get_algo_order", None)

    try:
        if method:
            order = _private_rest_call(
                f"futures_get_algo_order:{symbol}",
                method,
                symbol=symbol,
                algoId=algo_id,
            )
        else:
            order = _private_rest_call(
                f"futures_get_algo_order:{symbol}",
                client._request_futures_api,
                "get",
                "algoOrder",
                True,
                data={"symbol": symbol, "algoId": algo_id},
            )

        data = order.get("data") if isinstance(order, dict) and isinstance(order.get("data"), dict) else order
        return str((data or {}).get("algoStatus") or (data or {}).get("status") or "").upper()

    except Exception as exc:
        message = str(exc).lower()

        if "unknown order" in message or "order does not exist" in message or "not found" in message:
            return "NOT_FOUND"

        log_warning(f"{symbol} algo order {algo_id} status lookup warning: {exc}")
        return "UNKNOWN"


def get_24h_quote_volumes():
    """One cheap REST call for 24h quote volume across every symbol -
    used to rank the universe for the watchlist instead of guessing."""
    try:
        tickers = _public_rest_call(
            "futures_ticker",
            client.futures_ticker,
            weight=40,
        )
        return {
            item["symbol"]: _safe_float_local(item.get("quoteVolume"))
            for item in tickers
            if item.get("symbol")
        }
    except Exception as exc:
        log_error(f"24h quote volume fetch error: {exc}")
        return {}


def _safe_float_local(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
