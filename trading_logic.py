"""Estrategia de compra/venta editable desde Magic Pump.

Este archivo se copia al almacenamiento privado para editarlo desde el móvil.
La conexión HMAC y el envío/verificación de órdenes permanecen en main.py y
binance_rest.py. No incluyas claves API aquí.
"""
import requests
import time
import math

_LOGGER = print
def set_logger(func):
    global _LOGGER
    if callable(func):
        _LOGGER = func
def log(message):
    try:
        _LOGGER(message)
    except Exception:
        pass

# Parámetros iniciales compatibles con Magic Pump 1.1.2
PUMP_TP_INITIAL = 1.5
PUMP_TP_STEP = 0.0
PUMP_SL_INITIAL = 3.0
AUTO_SCAN_INTERVAL = 30.0
MAX_CONCURRENT_TRADES = 6
MAX_SCAN_MARKETS = 100
MIN_PURCHASE_USDT = 45.0
MIN_TRADE_USDT = 10.0
MIN_CURRENT_VALUE_USDT = 5.0
EARLY_EXIT_AFTER_HOUR_PCT = 0.75  # Meta de salida tras 120 minutos
NORMAL_TP_WAIT_MINUTES = 120.0
MAX_HOLD_MINUTES = 1440.0
TREND_CHECK_INTERVAL_MINUTES = 15.0
SELL_COOLDOWN_MINUTES = 120
SERVICE_INTERVAL = 30.0
POSITION_MONITOR_INTERVAL = 5.0
UI_MONITOR_INTERVAL = 1.0
MIN_QUOTE_VOLUME_USDT = 300000.0
MIN_MARKET_PRICE = 0.0000001
MIN_24H_CHANGE_PCT = 1.0
MIN_24H_TRADES = 800

def btc_market_state():
    """Clasifica BTC usando solo velas 4h cerradas; ante error bloquea el escaneo."""
    try:
        response = requests.get(
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=60",
            timeout=8
        )
        response.raise_for_status()
        raw = response.json()
        candles = raw[:-1] if isinstance(raw, list) else []
        closes = [float(k[4]) for k in candles if len(k) > 4]
        if len(closes) < 50:
            return "unknown"
        ema50 = calculate_ema(closes, 50)
        ema20 = calculate_ema(closes, 20)
        current = closes[-1]
        if not ema50 or not ema20 or ema50 <= 0 or ema20 <= 0:
            return "unknown"
        diff50 = (current / ema50 - 1) * 100
        diff20 = (current / ema20 - 1) * 100
        if diff50 > 1.2 and diff20 > 0.5:
            return "bull"
        if diff50 < -1.5:
            return "bear"
        return "neutral"
    except Exception as exc:
        log(f"No se pudo validar estado BTC: {exc}")
        return "unknown"


def _get_live_price(symbol):
    response = requests.get(
        f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=8
    )
    response.raise_for_status()
    return float(response.json()["price"])


def _trend_invalidated(symbol):
    """True si velas 1h cerradas pierden EMA21 o EMA9 cae bajo EMA21."""
    try:
        response = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=40",
            timeout=8
        )
        response.raise_for_status()
        raw = response.json()
        candles = raw[:-1] if isinstance(raw, list) else []
        closes = [float(k[4]) for k in candles if len(k) > 4]
        if len(closes) < 22:
            return False
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        return bool(ema9 and ema21 and (closes[-1] < ema21 or ema9 < ema21))
    except Exception as exc:
        log(f"No se pudo comprobar invalidación de tendencia {symbol}: {exc}")
        return False

def calculate_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def detect_buy_signal(symbol, current_price=None):
    """Señal 1h con indicadores de velas cerradas y confirmación de precio actual."""
    try:
        response = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=120",
            timeout=10
        )
        response.raise_for_status()
        raw = response.json()
        candles = raw[:-1] if isinstance(raw, list) else []
        if len(candles) < 60:
            return False
        opens = [float(k[1]) for k in candles]
        highs = [float(k[2]) for k in candles]
        lows = [float(k[3]) for k in candles]
        closes = [float(k[4]) for k in candles]
        volumes = [float(k[5]) for k in candles]
        price = float(current_price) if current_price is not None else _get_live_price(symbol)
        if price <= 0:
            return False
        last_open, last_close, last_high = opens[-1], closes[-1], highs[-1]
        if last_close <= last_open:
            return False
        body = abs(last_close - last_open)
        upper_wick = last_high - max(last_close, last_open)
        if body > 0 and upper_wick > body * 1.8:
            return False
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        if ema9 is None or ema21 is None or ema9 <= 0:
            return False
        if price < ema9 or ema9 < ema21:
            return False
        # No perseguir una subida que ya se extendió más de 1.8% de EMA9.
        if price > ema9 * 1.018:
            return False
        gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
        losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
        avg_gain = sum(gains[-14:]) / 14 if gains else 0
        avg_loss = sum(losses[-14:]) / 14 if losses else 0
        rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100
        if rsi < 48 or rsi > 62:
            return False
        baseline = sum(volumes[-13:-1]) / 12
        vol_ratio = volumes[-1] / baseline if baseline > 0 else 0
        if vol_ratio < 1.05:
            return False
        # Retest real: la última vela cerrada toca EMA9 y cierra arriba;
        # luego el precio cotizado actual confirma que no perdió ese cierre.
        if lows[-1] > ema9 * 1.003 or last_close < ema9 or price < last_close:
            return False
        distance = abs(price - ema9) / ema9 * 100
        if distance > 1.2:
            return False
        log(f"SEÑAL 1h CONFIRMADA {symbol} | RSI:{rsi:.1f} | EMA9:{ema9:.6f} | Vol:{vol_ratio:.2f}x")
        return True
    except Exception as exc:
        log(f"Error detect_buy_signal {symbol}: {exc}")
        return False

def is_near_resistance(symbol, current_price, tolerance=0.008):
    try:
        response = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=4h&limit=80",
            timeout=8
        )
        response.raise_for_status()
        raw = response.json()
        candles = raw[:-1] if isinstance(raw, list) else []
        highs = [float(k[2]) for k in candles if len(k) > 2]
        if len(highs) < 15:
            return False
        recent_high = max(highs[-15:])
        return float(current_price) >= recent_high * (1 - tolerance)
    except Exception:
        return False

def filter_market_symbols(ticker_data):
    """Preselecciona mercados USDT positivos, líquidos y con actividad; prioriza volumen."""
    filtered = []
    for item in ticker_data or []:
        symbol = item.get("symbol")
        if not symbol or not symbol.endswith("USDT"):
            continue
        try:
            volume = float(item.get("quoteVolume", 0))
            price = float(item.get("lastPrice", 0))
            change = float(item.get("priceChangePercent", 0))
            trades = int(item.get("count", 0))
        except (TypeError, ValueError):
            continue
        if (volume < MIN_QUOTE_VOLUME_USDT or price <= MIN_MARKET_PRICE
                or change < MIN_24H_CHANGE_PCT or trades < MIN_24H_TRADES):
            continue
        filtered.append((symbol, volume, change))
    filtered.sort(key=lambda row: row[1], reverse=True)
    return [row[0] for row in filtered[:max(1, int(MAX_SCAN_MARKETS))]]

def initial_sl_tp():
    """SL y TP normales iniciales para compras nuevas."""
    return float(PUMP_SL_INITIAL), float(PUMP_TP_INITIAL)

def calculate_buy_amount(balance, requested, open_positions=None, sl_pct=None):
    """Tamaño estilo 1.2.4: monto solicitado, limitado al 99% del USDT libre."""
    try:
        free_balance = max(0.0, float(balance))
        requested = max(0.0, float(requested))
        if not math.isfinite(free_balance) or not math.isfinite(requested):
            return 0.0
        return min(requested, free_balance * 0.99)
    except (TypeError, ValueError, OverflowError):
        return 0.0

def manage_position(position, price, now=None):
    """SL local, TP, invalidación de tendencia y salida forzosa por tiempo máximo."""
    if not isinstance(position, dict) or position.get("estado", "COMPRADO") != "COMPRADO":
        return None
    try:
        price = float(price)
        now = float(now if now is not None else time.time())
        entry = float(position.get("entry_price", 0) or 0)
        if entry <= 0 or price <= 0:
            return None
        symbol = position.get("moneda", "posición")
        sl_pct = float(position.get("sl_pct", PUMP_SL_INITIAL))
        if price <= entry * (1 - sl_pct / 100.0):
            log(f"STOP LOSS local: {symbol}")
            return f"Stop Loss {sl_pct}% (vigilancia local)"
        bought_at = float(position.get("buy_time", now) or now)
        age_minutes = max(0.0, (now - bought_at) / 60.0)
        if age_minutes >= float(MAX_HOLD_MINUTES):
            return f"Tiempo máximo de permanencia ({MAX_HOLD_MINUTES:g} min)"
        wait_minutes = float(NORMAL_TP_WAIT_MINUTES)
        tp_pct = float(position.get("tp_pct", PUMP_TP_INITIAL))
        target_pct = tp_pct if age_minutes <= wait_minutes else float(EARLY_EXIT_AFTER_HOUR_PCT)
        if price >= entry * (1 + target_pct / 100.0):
            return f"TP {target_pct:.2f}% (espera inicial {wait_minutes:g} min)"
        # Consulta de tendencia como máximo cada 15 min y solo después del periodo inicial.
        if age_minutes > wait_minutes:
            last_check = float(position.get("trend_check_at", 0) or 0)
            interval = float(TREND_CHECK_INTERVAL_MINUTES) * 60.0
            if now - last_check >= interval:
                position["trend_check_at"] = now
                if _trend_invalidated(symbol):
                    return "Tendencia 1h invalidada (cierre bajo EMA21)"
        return None
    except Exception as exc:
        log(f"Error evaluando salida {position.get('moneda', 'posición')}: {exc}")
        return None
