import os
import json
import ast
import math
import types
import traceback
import threading
from contextlib import contextmanager
import time
import requests
from binance_rest import BinanceRESTClient, BinanceRESTError
import random
from datetime import datetime
from decimal import Decimal, getcontext
getcontext().prec = 28
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.uix.popup import Popup
from kivy.clock import Clock, mainthread
from kivy.graphics import Color, Rectangle
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window
from kivy.utils import platform

# Wake Lock para Android
if platform == 'android':
    from jnius import autoclass
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    Activity = autoclass('android.app.Activity')
    PowerManager = autoclass('android.os.PowerManager')

# Cliente Spot propio sobre requests. Evita python-binance/aiohttp y sus extensiones C.
REAL_CLIENT_AVAILABLE = True
BINANCE_IMPORT_ERROR = ""

def ensure_binance_available():
    # requests ya forma parte de los requisitos de la APK.
    return True


# Built from trading_logic.py for Android resource-path reliability.
DEFAULT_TRADING_LOGIC_SOURCE = '"""Estrategia de compra/venta editable desde Magic Pump.\n\nEste archivo se copia al almacenamiento privado para editarlo desde el móvil.\nLa conexión HMAC y el envío/verificación de órdenes permanecen en main.py y\nbinance_rest.py. No incluyas claves API aquí.\n"""\nimport requests\nimport time\nimport math\n\n_LOGGER = print\ndef set_logger(func):\n    global _LOGGER\n    if callable(func):\n        _LOGGER = func\ndef log(message):\n    try:\n        _LOGGER(message)\n    except Exception:\n        pass\n\n# Parámetros iniciales compatibles con Magic Pump 1.1.2\nPUMP_TP_INITIAL = 1.5\nPUMP_TP_STEP = 0.0\nPUMP_SL_INITIAL = 3.0\nAUTO_SCAN_INTERVAL = 30.0\nMAX_CONCURRENT_TRADES = 6\nMAX_SCAN_MARKETS = 100\nMIN_PURCHASE_USDT = 45.0\nMIN_TRADE_USDT = 10.0\nMIN_CURRENT_VALUE_USDT = 5.0\nEARLY_EXIT_AFTER_HOUR_PCT = 0.75  # Meta de salida tras 120 minutos\nNORMAL_TP_WAIT_MINUTES = 120.0\nMAX_HOLD_MINUTES = 1440.0\nTREND_CHECK_INTERVAL_MINUTES = 15.0\nSELL_COOLDOWN_MINUTES = 120\nSERVICE_INTERVAL = 30.0\nPOSITION_MONITOR_INTERVAL = 5.0\nUI_MONITOR_INTERVAL = 1.0\nMIN_QUOTE_VOLUME_USDT = 300000.0\nMIN_MARKET_PRICE = 0.0000001\nMIN_24H_CHANGE_PCT = 1.0\nMIN_24H_TRADES = 800\n\ndef btc_market_state():\n    """Clasifica BTC usando solo velas 4h cerradas; ante error bloquea el escaneo."""\n    try:\n        response = requests.get(\n            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=60",\n            timeout=8\n        )\n        response.raise_for_status()\n        raw = response.json()\n        candles = raw[:-1] if isinstance(raw, list) else []\n        closes = [float(k[4]) for k in candles if len(k) > 4]\n        if len(closes) < 50:\n            return "unknown"\n        ema50 = calculate_ema(closes, 50)\n        ema20 = calculate_ema(closes, 20)\n        current = closes[-1]\n        if not ema50 or not ema20 or ema50 <= 0 or ema20 <= 0:\n            return "unknown"\n        diff50 = (current / ema50 - 1) * 100\n        diff20 = (current / ema20 - 1) * 100\n        if diff50 > 1.2 and diff20 > 0.5:\n            return "bull"\n        if diff50 < -1.5:\n            return "bear"\n        return "neutral"\n    except Exception as exc:\n        log(f"No se pudo validar estado BTC: {exc}")\n        return "unknown"\n\n\ndef _get_live_price(symbol):\n    response = requests.get(\n        f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=8\n    )\n    response.raise_for_status()\n    return float(response.json()["price"])\n\n\ndef _trend_invalidated(symbol):\n    """True si velas 1h cerradas pierden EMA21 o EMA9 cae bajo EMA21."""\n    try:\n        response = requests.get(\n            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=40",\n            timeout=8\n        )\n        response.raise_for_status()\n        raw = response.json()\n        candles = raw[:-1] if isinstance(raw, list) else []\n        closes = [float(k[4]) for k in candles if len(k) > 4]\n        if len(closes) < 22:\n            return False\n        ema9 = calculate_ema(closes, 9)\n        ema21 = calculate_ema(closes, 21)\n        return bool(ema9 and ema21 and (closes[-1] < ema21 or ema9 < ema21))\n    except Exception as exc:\n        log(f"No se pudo comprobar invalidación de tendencia {symbol}: {exc}")\n        return False\n\ndef calculate_ema(closes, period):\n    if len(closes) < period:\n        return None\n    k = 2 / (period + 1)\n    ema = sum(closes[:period]) / period\n    for price in closes[period:]:\n        ema = (price * k) + (ema * (1 - k))\n    return ema\n\ndef detect_buy_signal(symbol, current_price=None):\n    """Señal 1h con indicadores de velas cerradas y confirmación de precio actual."""\n    try:\n        response = requests.get(\n            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=120",\n            timeout=10\n        )\n        response.raise_for_status()\n        raw = response.json()\n        candles = raw[:-1] if isinstance(raw, list) else []\n        if len(candles) < 60:\n            return False\n        opens = [float(k[1]) for k in candles]\n        highs = [float(k[2]) for k in candles]\n        lows = [float(k[3]) for k in candles]\n        closes = [float(k[4]) for k in candles]\n        volumes = [float(k[5]) for k in candles]\n        price = float(current_price) if current_price is not None else _get_live_price(symbol)\n        if price <= 0:\n            return False\n        last_open, last_close, last_high = opens[-1], closes[-1], highs[-1]\n        if last_close <= last_open:\n            return False\n        body = abs(last_close - last_open)\n        upper_wick = last_high - max(last_close, last_open)\n        if body > 0 and upper_wick > body * 1.8:\n            return False\n        ema9 = calculate_ema(closes, 9)\n        ema21 = calculate_ema(closes, 21)\n        if ema9 is None or ema21 is None or ema9 <= 0:\n            return False\n        if price < ema9 or ema9 < ema21:\n            return False\n        # No perseguir una subida que ya se extendió más de 1.8% de EMA9.\n        if price > ema9 * 1.018:\n            return False\n        gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]\n        losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]\n        avg_gain = sum(gains[-14:]) / 14 if gains else 0\n        avg_loss = sum(losses[-14:]) / 14 if losses else 0\n        rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100\n        if rsi < 48 or rsi > 62:\n            return False\n        baseline = sum(volumes[-13:-1]) / 12\n        vol_ratio = volumes[-1] / baseline if baseline > 0 else 0\n        if vol_ratio < 1.05:\n            return False\n        # Retest real: la última vela cerrada toca EMA9 y cierra arriba;\n        # luego el precio cotizado actual confirma que no perdió ese cierre.\n        if lows[-1] > ema9 * 1.003 or last_close < ema9 or price < last_close:\n            return False\n        distance = abs(price - ema9) / ema9 * 100\n        if distance > 1.2:\n            return False\n        log(f"SEÑAL 1h CONFIRMADA {symbol} | RSI:{rsi:.1f} | EMA9:{ema9:.6f} | Vol:{vol_ratio:.2f}x")\n        return True\n    except Exception as exc:\n        log(f"Error detect_buy_signal {symbol}: {exc}")\n        return False\n\ndef is_near_resistance(symbol, current_price, tolerance=0.008):\n    try:\n        response = requests.get(\n            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=4h&limit=80",\n            timeout=8\n        )\n        response.raise_for_status()\n        raw = response.json()\n        candles = raw[:-1] if isinstance(raw, list) else []\n        highs = [float(k[2]) for k in candles if len(k) > 2]\n        if len(highs) < 15:\n            return False\n        recent_high = max(highs[-15:])\n        return float(current_price) >= recent_high * (1 - tolerance)\n    except Exception:\n        return False\n\ndef filter_market_symbols(ticker_data):\n    """Preselecciona mercados USDT positivos, líquidos y con actividad; prioriza volumen."""\n    filtered = []\n    for item in ticker_data or []:\n        symbol = item.get("symbol")\n        if not symbol or not symbol.endswith("USDT"):\n            continue\n        try:\n            volume = float(item.get("quoteVolume", 0))\n            price = float(item.get("lastPrice", 0))\n            change = float(item.get("priceChangePercent", 0))\n            trades = int(item.get("count", 0))\n        except (TypeError, ValueError):\n            continue\n        if (volume < MIN_QUOTE_VOLUME_USDT or price <= MIN_MARKET_PRICE\n                or change < MIN_24H_CHANGE_PCT or trades < MIN_24H_TRADES):\n            continue\n        filtered.append((symbol, volume, change))\n    filtered.sort(key=lambda row: row[1], reverse=True)\n    return [row[0] for row in filtered[:max(1, int(MAX_SCAN_MARKETS))]]\n\ndef initial_sl_tp():\n    """SL y TP normales iniciales para compras nuevas."""\n    return float(PUMP_SL_INITIAL), float(PUMP_TP_INITIAL)\n\ndef calculate_buy_amount(balance, requested, open_positions=None, sl_pct=None):\n    """Tamaño estilo 1.2.4: monto solicitado, limitado al 99% del USDT libre."""\n    try:\n        free_balance = max(0.0, float(balance))\n        requested = max(0.0, float(requested))\n        if not math.isfinite(free_balance) or not math.isfinite(requested):\n            return 0.0\n        return min(requested, free_balance * 0.99)\n    except (TypeError, ValueError, OverflowError):\n        return 0.0\n\ndef manage_position(position, price, now=None):\n    """SL local, TP, invalidación de tendencia y salida forzosa por tiempo máximo."""\n    if not isinstance(position, dict) or position.get("estado", "COMPRADO") != "COMPRADO":\n        return None\n    try:\n        price = float(price)\n        now = float(now if now is not None else time.time())\n        entry = float(position.get("entry_price", 0) or 0)\n        if entry <= 0 or price <= 0:\n            return None\n        symbol = position.get("moneda", "posición")\n        sl_pct = float(position.get("sl_pct", PUMP_SL_INITIAL))\n        if price <= entry * (1 - sl_pct / 100.0):\n            log(f"STOP LOSS local: {symbol}")\n            return f"Stop Loss {sl_pct}% (vigilancia local)"\n        bought_at = float(position.get("buy_time", now) or now)\n        age_minutes = max(0.0, (now - bought_at) / 60.0)\n        if age_minutes >= float(MAX_HOLD_MINUTES):\n            return f"Tiempo máximo de permanencia ({MAX_HOLD_MINUTES:g} min)"\n        wait_minutes = float(NORMAL_TP_WAIT_MINUTES)\n        tp_pct = float(position.get("tp_pct", PUMP_TP_INITIAL))\n        target_pct = tp_pct if age_minutes <= wait_minutes else float(EARLY_EXIT_AFTER_HOUR_PCT)\n        if price >= entry * (1 + target_pct / 100.0):\n            return f"TP {target_pct:.2f}% (espera inicial {wait_minutes:g} min)"\n        # Consulta de tendencia como máximo cada 15 min y solo después del periodo inicial.\n        if age_minutes > wait_minutes:\n            last_check = float(position.get("trend_check_at", 0) or 0)\n            interval = float(TREND_CHECK_INTERVAL_MINUTES) * 60.0\n            if now - last_check >= interval:\n                position["trend_check_at"] = now\n                if _trend_invalidated(symbol):\n                    return "Tendencia 1h invalidada (cierre bajo EMA21)"\n        return None\n    except Exception as exc:\n        log(f"Error evaluando salida {position.get(\'moneda\', \'posición\')}: {exc}")\n        return None\n'

# ====================== CONSTANTES ======================
POSITIONS_FILE = "posiciones.json"
SELL_COOLDOWN_FILE = "sell_cooldown.json"

# ====================== MODO NORMAL ======================
PUMP_TP_INITIAL = 1.5
PUMP_TP_STEP = 0.0
PUMP_SL_INITIAL = 3.0          # ← CAMBIADO de 5.0 a 3.0

AUTO_SCAN_INTERVAL = 30.0
MAX_CONCURRENT_TRADES = 6
MAX_SCAN_MARKETS = 100
MIN_TRADE_USDT = 10.0
MIN_QUOTE_VOLUME_USDT = 300000.0
MIN_MARKET_PRICE = 0.0000001
MIN_24H_CHANGE_PCT = 1.0
MIN_24H_TRADES = 800
SERVICE_INTERVAL = 30.0
POSITION_MONITOR_INTERVAL = 5.0
UI_MONITOR_INTERVAL = 1.0
MIN_PURCHASE_USDT = 45.0  # monto predeterminado; mínimo real depende del mercado
MIN_CURRENT_VALUE_USDT = 5.0

EARLY_EXIT_AFTER_HOUR_PCT = 0.5  # Meta posterior al periodo TP configurado
NORMAL_TP_WAIT_MINUTES = 120.0
MAX_HOLD_MINUTES = 1440.0
TREND_CHECK_INTERVAL_MINUTES = 15.0
SELL_COOLDOWN_MINUTES = 120

COLOR_BG_DARK = (0.1, 0.1, 0.15, 1)
COLOR_CARD_DARK = (0.15, 0.15, 0.2, 1)
COLOR_ACCENT_NEON = (0.0, 0.8, 0.5, 1)
COLOR_ACCENT_ERROR = (0.9, 0.2, 0.2, 1)
COLOR_TEXT_LIGHT = (0.9, 0.9, 0.9, 1)
COLOR_TEXT_MUTED = (0.6, 0.6, 0.6, 1)
NUM_TRADING_SECTIONS = 5

FONT_SIZE_TITLE = "24sp"
FONT_SIZE_NORMAL = "14sp"
FONT_SIZE_SMALL = "12sp"
FONT_SIZE_ACCENT = "16sp"

# ====================== LOGS ======================
LOG_LINES = []
MAX_LOG_LINES = 1000
LOG_LOCK = threading.RLock()

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {message}"
    print(full_msg)
    with LOG_LOCK:
        LOG_LINES.append(full_msg)
        if len(LOG_LINES) > MAX_LOG_LINES:
            del LOG_LINES[:-MAX_LOG_LINES]
        try:
            path = os.path.join(get_data_dir(), "magicpump.log")
            with open(path, "a", encoding="utf-8") as log_file:
                log_file.write(full_msg + "\n")
                log_file.flush()
        except Exception:
            pass

def load_persistent_logs(limit=MAX_LOG_LINES):
    try:
        path = os.path.join(get_data_dir(), "magicpump.log")
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()[-limit:]
    except Exception:
        return []

# ====================== UTILIDADES ======================
def get_price_usdt(symbol):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=8)
        return float(r.json()["price"])
    except:
        return None

def get_real_usdt_balance_total():
    keys = load_api_keys()
    if not keys.get("api") or not keys.get("secret"):
        return 0.0
    try:
        client = RealBinanceClientWrapper(keys["api"], keys["secret"])
        if not client.use_real:
            return 0.0
        balance = client.get_asset_balance("USDT")
        return float(balance.get("free", 0) or 0) if balance else 0.0
    except Exception as exc:
        log(f"Error consultando saldo Binance: {exc}")
        return 0.0


def adjusted_qty_with_status(client_wrapper, symbol, desired_qty, price=None):
    """Return (orderable_qty, verified). None+True means verified below exchange filters."""
    try:
        info = client_wrapper.get_symbol_info(symbol) if client_wrapper else None
        if not info:
            return None, False
        market_price = float(price if price is not None else (get_price_usdt(symbol) or 0))
        requested = float(desired_qty)
        if not math.isfinite(market_price) or market_price <= 0 or not math.isfinite(requested):
            return None, False
        min_notional = 0.0
        min_qty = 0.0
        step_size = 0.0
        for rule in info.get("filters", []):
            if rule.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = max(min_notional, float(rule.get("minNotional", 0) or 0))
            elif rule.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                min_qty = max(min_qty, float(rule.get("minQty", 0) or 0))
                step_size = max(step_size, float(rule.get("stepSize", 0) or 0))
        if requested <= 0:
            return None, True
        qty = Decimal(str(requested))
        if step_size > 0:
            step = Decimal(str(step_size))
            qty = (qty // step) * step
        if qty <= 0 or qty < Decimal(str(min_qty)) or float(qty) * market_price < min_notional:
            return None, True
        return float(qty), True
    except Exception as exc:
        log(f"No se pudieron verificar los mínimos de venta de {symbol}: {exc}")
        return None, False


def inspect_residual_sellability(client_wrapper, symbol, tracked_remaining):
    """Return (state, tracked_qty, value): state is sellable, dust, or unknown."""
    epsilon = max(float(tracked_remaining or 0) * 1e-8, 1e-12)
    try:
        asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        balance = client_wrapper.get_asset_balance(asset)
        if not isinstance(balance, dict):
            return "unknown", float(tracked_remaining or 0), None
        free_qty = max(0.0, float(balance.get("free", 0) or 0))
        locked_qty = max(0.0, float(balance.get("locked", 0) or 0))
        remaining = max(0.0, float(tracked_remaining or 0))
        if locked_qty > epsilon:
            return "unknown", min(remaining, free_qty + locked_qty), None
        candidate = min(remaining, free_qty)
        if candidate <= epsilon:
            return "dust", 0.0, 0.0
        price = get_price_usdt(symbol)
        if not price:
            return "unknown", candidate, None
        order_qty, verified = adjusted_qty_with_status(
            client_wrapper, symbol, candidate, price=float(price)
        )
        if not verified:
            return "unknown", candidate, candidate * float(price)
        if order_qty is None:
            return "dust", candidate, candidate * float(price)
        return "sellable", candidate, candidate * float(price)
    except Exception as exc:
        log(f"No se pudo reconciliar el residuo de {symbol}: {exc}")
        return "unknown", float(tracked_remaining or 0), None


class RealBinanceClientWrapper:
    def __init__(self, api_key, api_secret):
        self.use_real = bool(api_key and api_secret and api_key != "SIM_API")
        self.client = None
        if self.use_real:
            try:
                self.client = BinanceRESTClient(api_key.strip(), api_secret.strip())
                self.client.ping()
            except Exception as exc:
                self.use_real = False
                self.client = None
                log(f"Error conectando con Binance REST: {exc}")

    def create_market_buy_order(self, symbol, quantity):
        if not self.use_real:
            return None
        try:
            return self.client.create_order(symbol=symbol, side="BUY", type="MARKET", quantity=quantity)
        except Exception as exc:
            log(f"Error compra {symbol}: {exc}")
            return None

    def create_market_sell_order(self, symbol, quantity):
        if not self.use_real:
            return None
        try:
            return self.client.create_order(symbol=symbol, side="SELL", type="MARKET", quantity=quantity)
        except Exception as exc:
            log(f"Error venta {symbol}: {exc}")
            return None

    def get_symbol_info(self, symbol):
        if not self.use_real:
            return None
        try:
            return self.client.get_symbol_info(symbol)
        except Exception as exc:
            log(f"Error consultando filtros de {symbol}: {exc}")
            return None

    def get_asset_balance(self, asset):
        if not self.use_real:
            return None
        try:
            return self.client.get_asset_balance(asset)
        except Exception as exc:
            log(f"Error consultando saldo {asset}: {exc}")
            return None

    def get_order(self, symbol, order_id):
        if not self.use_real:
            return None
        try:
            return self.client.get_order(symbol=symbol, orderId=order_id)
        except Exception as exc:
            log(f"Error consultando orden {symbol}: {exc}")
            return None

    def cancel_order(self, symbol, order_id):
        if not self.use_real:
            return None
        try:
            return self.client.cancel_order(symbol=symbol, orderId=order_id)
        except Exception as exc:
            log(f"Error cancelando orden {symbol}: {exc}")
            return None

    def get_account(self):
        if not self.use_real:
            return None
        try:
            return self.client.get_account()
        except Exception as exc:
            log(f"Error consultando cuenta Binance: {exc}")
            return None

    def get_my_trades(self, symbol, limit=100):
        if not self.use_real:
            return []
        try:
            return self.client.get_my_trades(symbol=symbol, limit=limit)
        except Exception as exc:
            log(f"Error consultando operaciones {symbol}: {exc}")
            return []

# ====================== FUNCIONES DE ARCHIVOS ======================
def get_data_dir():
    shared = os.environ.get("MAGICPUMP_DATA_DIR")
    if shared:
        os.makedirs(shared, exist_ok=True)
        return shared
    try:
        path = App.get_running_app().user_data_dir
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        path = os.path.join(os.getcwd(), "magicpump_data")
        os.makedirs(path, exist_ok=True)
        return path

@contextmanager
def engine_file_lock():
    """Cross-process lock so the UI and Android service never trade simultaneously."""
    lock_path = os.path.join(get_data_dir(), "bot_engine.lock")
    handle = open(lock_path, "a+")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        handle.close()

def load_api_keys():
    path = os.path.join(get_data_dir(), "api_config.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return {"api": "", "secret": "", "invest_amount": MIN_PURCHASE_USDT}

def save_api_keys(api, secret, invest_amount):
    path = os.path.join(get_data_dir(), "api_config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        api_clean = "".join(str(api or "").split())
        secret_clean = "".join(str(secret or "").split())
        json.dump({"api": api_clean, "secret": secret_clean, "invest_amount": float(invest_amount)}, f)
    os.replace(temp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def load_acumulado():
    path = os.path.join(get_data_dir(), "acumulado_mes.json")
    default = {"acumulado_real": 0.0}
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return default

def save_acumulado(data):
    path = os.path.join(get_data_dir(), "acumulado_mes.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def load_positions_from_file():
    path = os.path.join(get_data_dir(), POSITIONS_FILE)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
                log(f"✅ Cargadas {len(data)} posiciones")
                return data
        except Exception as e:
            log(f"Error leyendo posiciones.json: {e}")
    return []

def position_records_from_sections(sections):
    active_data = []
    for section in sections:
        data = section.data
        if data.get("estado") == "COMPRADO" and data.get("moneda"):
            active_data.append({
                "moneda": data["moneda"],
                "entry_price": float(data["entry_price"]),
                "cantidad": float(data["cantidad"]),
                "inversion": float(data["inversion"]),
                "sl_pct": float(data["sl_pct"]),
                "tp_pct": float(data["tp_pct"]),
                "buy_time": float(data.get("buy_time", time.time())),
                "buy_order_id": str(data.get("buy_order_id", "") or ""),
                "current_tp_price": float(data.get("current_tp_price", 0)),
                "last_tp_price": float(data.get("last_tp_price", 0)),
                "tp_reached": bool(data.get("tp_reached", False)),
                "sl_pause": bool(data.get("sl_pause", False))
            })
    return active_data


def save_position_records(records):
    path = os.path.join(get_data_dir(), POSITIONS_FILE)
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except Exception as e:
        log(f"Error guardando posiciones: {e}")


def save_positions(sections):
    save_position_records(position_records_from_sections(sections))

def load_sell_cooldown():
    path = os.path.join(get_data_dir(), SELL_COOLDOWN_FILE)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
                return {k: float(v) for k, v in data.items()}
        except:
            pass
    return {}

def save_sell_cooldown(cooldown_dict):
    path = os.path.join(get_data_dir(), SELL_COOLDOWN_FILE)
    try:
        with open(path, 'w') as f:
            json.dump(cooldown_dict, f, indent=4)
    except:
        pass

# ====================== NUEVA FUNCIÓN ESTADO DEL MERCADO BTC ======================
def _builtin_btc_market_state():
    try:
        response = requests.get("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=60", timeout=8)
        response.raise_for_status()
        raw = response.json()
        candles = raw[:-1] if isinstance(raw, list) else []
        closes = [float(k[4]) for k in candles if len(k) > 4]
        if len(closes) < 50:
            return "unknown"
        ema50 = _builtin_calculate_ema(closes, 50)
        ema20 = _builtin_calculate_ema(closes, 20)
        if not ema50 or not ema20:
            return "unknown"
        diff50 = (closes[-1] / ema50 - 1) * 100
        diff20 = (closes[-1] / ema20 - 1) * 100
        if diff50 > 1.2 and diff20 > 0.5:
            return "bull"
        if diff50 < -1.5:
            return "bear"
        return "neutral"
    except Exception as exc:
        log(f"No se pudo validar BTC: {exc}")
        return "unknown"

def _builtin_calculate_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

# ==================== FUNCIÓN MEJORADA: detect_buy_signal (ANTICIPACIÓN DE SUBIDA) ====================
def _builtin_detect_buy_signal(symbol, current_price=None):
    return _embedded_strategy_function("detect_buy_signal")(symbol, current_price)


# ==================== FUNCIÓN EVITAR RESISTENCIA (sin cambios) ====================
def _builtin_is_near_resistance(symbol, current_price, tolerance=0.008):
    return _embedded_strategy_function("is_near_resistance")(symbol, current_price, tolerance)

STRATEGY_REQUIRED_FUNCTIONS = (
    "btc_market_state", "calculate_ema", "detect_buy_signal",
    "is_near_resistance", "filter_market_symbols",
    "initial_sl_tp", "calculate_buy_amount", "manage_position",
)
STRATEGY_CONSTANT_NAMES = (
    "PUMP_TP_INITIAL", "PUMP_TP_STEP", "PUMP_SL_INITIAL",
    "AUTO_SCAN_INTERVAL", "MAX_CONCURRENT_TRADES", "MAX_SCAN_MARKETS",
    "MIN_PURCHASE_USDT", "MIN_TRADE_USDT", "MIN_CURRENT_VALUE_USDT",
    "EARLY_EXIT_AFTER_HOUR_PCT", "NORMAL_TP_WAIT_MINUTES", "MAX_HOLD_MINUTES", "TREND_CHECK_INTERVAL_MINUTES",
    "SELL_COOLDOWN_MINUTES", "SERVICE_INTERVAL",
    "POSITION_MONITOR_INTERVAL", "UI_MONITOR_INTERVAL", "MIN_QUOTE_VOLUME_USDT",
    "MIN_MARKET_PRICE", "MIN_24H_CHANGE_PCT", "MIN_24H_TRADES",
)
STRATEGY_NUMERIC_BOUNDS = {
    "PUMP_TP_INITIAL": (0.1, 100.0), "PUMP_TP_STEP": (0.0, 50.0),
    "PUMP_SL_INITIAL": (0.1, 30.0),
    "AUTO_SCAN_INTERVAL": (1.0, 3600.0), "MAX_CONCURRENT_TRADES": (1.0, 6.0),
    "MAX_SCAN_MARKETS": (1.0, 100.0), "MIN_PURCHASE_USDT": (1.0, 10000000.0),
    "MIN_TRADE_USDT": (1.0, 10000000.0), "MIN_CURRENT_VALUE_USDT": (0.0, 10000000.0),
    "EARLY_EXIT_AFTER_HOUR_PCT": (0.01, 100.0), "NORMAL_TP_WAIT_MINUTES": (1.0, 10080.0),
    "MAX_HOLD_MINUTES": (15.0, 10080.0), "TREND_CHECK_INTERVAL_MINUTES": (1.0, 240.0),
    "SELL_COOLDOWN_MINUTES": (0.0, 10080.0),
    "SERVICE_INTERVAL": (1.0, 3600.0), "POSITION_MONITOR_INTERVAL": (1.0, 3600.0),
    "UI_MONITOR_INTERVAL": (0.5, 60.0), "MIN_QUOTE_VOLUME_USDT": (0.0, 1e15),
    "MIN_MARKET_PRICE": (1e-12, 1e12), "MIN_24H_CHANGE_PCT": (0.0, 1000.0),
    "MIN_24H_TRADES": (0.0, 1e9),
}
_INTEGER_STRATEGY_CONSTANTS = {"MAX_CONCURRENT_TRADES", "MAX_SCAN_MARKETS", "SELL_COOLDOWN_MINUTES", "MIN_24H_TRADES"}
_STRATEGY_DEFAULT_CONSTANTS = {name: globals()[name] for name in STRATEGY_CONSTANT_NAMES}
_STRATEGY_MODULE = None
_STRATEGY_LOAD_ATTEMPTED = False
_STRATEGY_SOURCE_SIGNATURE = None
_STRATEGY_LOCK = threading.RLock()


def _strategy_paths():
    data_dir = get_data_dir()
    return {
        "active": os.path.join(data_dir, "trading_logic_active.py"),
        "pending": os.path.join(data_dir, "trading_logic_pending.py"),
    }


def _active_strategy_source():
    paths = _strategy_paths()
    if os.path.exists(paths["active"]):
        with open(paths["active"], "r", encoding="utf-8") as f:
            return f.read()
    return DEFAULT_TRADING_LOGIC_SOURCE


def _strategy_source_signature():
    paths = _strategy_paths()
    if not os.path.exists(paths["active"]):
        return ("embedded-default", len(DEFAULT_TRADING_LOGIC_SOURCE))
    try:
        stat = os.stat(paths["active"])
        return (paths["active"], stat.st_mtime_ns, stat.st_size, getattr(stat, "st_ino", 0))
    except OSError:
        return (paths["active"], None, None, None)


def read_strategy_source(path=None):
    paths = _strategy_paths()
    if path is not None:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    if os.path.exists(paths["pending"]):
        path = paths["pending"]
    elif os.path.exists(paths["active"]):
        path = paths["active"]
    else:
        return DEFAULT_TRADING_LOGIC_SOURCE
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def validate_strategy_source(source):
    try:
        compile(source, "trading_logic.py", "exec")
        tree = ast.parse(source, filename="trading_logic.py")
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = [name for name in STRATEGY_REQUIRED_FUNCTIONS if name not in names]
        if missing:
            return False, "Faltan funciones requeridas: " + ", ".join(missing)
        assigned = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value_node = node.value
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in STRATEGY_NUMERIC_BOUNDS:
                    assigned[target.id] = ast.literal_eval(value_node)
        missing_values = [name for name in STRATEGY_NUMERIC_BOUNDS if name not in assigned]
        if missing_values:
            return False, "Faltan parámetros numéricos: " + ", ".join(missing_values)
        for name, (minimum, maximum) in STRATEGY_NUMERIC_BOUNDS.items():
            value = assigned[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                return False, f"{name} debe tener un valor numérico finito"
            if not minimum <= float(value) <= maximum:
                return False, f"{name} debe estar entre {minimum:g} y {maximum:g}"
            if name in _INTEGER_STRATEGY_CONSTANTS and int(value) != float(value):
                return False, f"{name} debe ser un número entero"
        return True, "Sintaxis, funciones y parámetros correctos"
    except SyntaxError as exc:
        return False, f"Error de sintaxis en línea {exc.lineno}: {exc.msg}"
    except Exception as exc:
        return False, f"No se pudo validar: {exc}"


def _backup_strategy(source):
    data_dir = get_data_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(data_dir, f"trading_logic_backup_{stamp}.py")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        old = sorted(
            (os.path.join(data_dir, n) for n in os.listdir(data_dir)
             if n.startswith("trading_logic_backup_") and n.endswith(".py")),
            key=os.path.getmtime, reverse=True
        )
        for item in old[5:]:
            try:
                os.remove(item)
            except OSError:
                pass
    except Exception as exc:
        log(f"No se pudo crear respaldo del código de estrategia: {exc}")


def save_pending_strategy_source(source):
    ok, message = validate_strategy_source(source)
    if not ok:
        return False, message
    paths = _strategy_paths()
    try:
        _backup_strategy(_active_strategy_source())
        temp = paths["pending"] + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            f.write(source)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, paths["pending"])
        return True, "Guardado; se aplicará al reiniciar Magic Pump."
    except Exception as exc:
        return False, f"No se pudo guardar: {exc}"


def restore_default_strategy_pending():
    try:
        return save_pending_strategy_source(DEFAULT_TRADING_LOGIC_SOURCE)
    except Exception as exc:
        return False, f"No se pudo cargar la estrategia incluida: {exc}"


def activate_pending_strategy():
    paths = _strategy_paths()
    pending = paths["pending"]
    if not os.path.exists(pending):
        return False
    try:
        source = read_strategy_source(pending)
        ok, message = validate_strategy_source(source)
        if not ok:
            log(f"Estrategia pendiente no activada: {message}")
            return False
        _backup_strategy(_active_strategy_source())
        os.replace(pending, paths["active"])
        log("Cambios de estrategia activados al iniciar")
        return True
    except Exception as exc:
        log(f"No se pudo activar el código guardado; se conserva la estrategia anterior: {exc}")
        return False


def _load_strategy_source(source, label):
    ok, message = validate_strategy_source(source)
    if not ok:
        raise ValueError(message)
    module = types.ModuleType("magicpump_editable_strategy")
    module.__file__ = label
    exec(compile(source, label, "exec"), module.__dict__)
    if callable(getattr(module, "set_logger", None)):
        module.set_logger(log)
    return module


_EMBEDDED_STRATEGY_MODULE = None

def _embedded_strategy_function(name):
    global _EMBEDDED_STRATEGY_MODULE
    if _EMBEDDED_STRATEGY_MODULE is None:
        _EMBEDDED_STRATEGY_MODULE = _load_strategy_source(
            DEFAULT_TRADING_LOGIC_SOURCE, "<embedded safety strategy>"
        )
    return getattr(_EMBEDDED_STRATEGY_MODULE, name)


def get_strategy_logic(force_reload=False):
    """Loads the private override or embedded default without relying on APK paths."""
    global _STRATEGY_MODULE, _STRATEGY_LOAD_ATTEMPTED, _STRATEGY_SOURCE_SIGNATURE
    with _STRATEGY_LOCK:
        signature = _strategy_source_signature()
        if (not force_reload and _STRATEGY_LOAD_ATTEMPTED
                and signature == _STRATEGY_SOURCE_SIGNATURE):
            return _STRATEGY_MODULE
        paths = _strategy_paths()
        is_custom = os.path.exists(paths["active"])
        try:
            source = _active_strategy_source()
            module = _load_strategy_source(source, paths["active"] if is_custom else "<embedded trading_logic.py>")
            _STRATEGY_MODULE = module
            for name in STRATEGY_CONSTANT_NAMES:
                if hasattr(module, name):
                    globals()[name] = getattr(module, name)
            log("Estrategia personalizada cargada" if is_custom else "Estrategia original integrada cargada")
            return module
        except Exception as exc:
            log(f"No se pudo cargar el script editable; se usarán reglas internas: {exc}")
            _STRATEGY_MODULE = None
            for name, value in _STRATEGY_DEFAULT_CONSTANTS.items():
                globals()[name] = value
            return None
        finally:
            _STRATEGY_LOAD_ATTEMPTED = True
            _STRATEGY_SOURCE_SIGNATURE = signature


def _strategy_call(name, fallback, *args):
    module = get_strategy_logic()
    if module is not None:
        try:
            return getattr(module, name)(*args)
        except Exception as exc:
            log(f"Error en {name} del código editable; se aplica la regla incluida: {exc}")
    return fallback(*args)


def btc_market_state():
    return _strategy_call("btc_market_state", _builtin_btc_market_state)


def calculate_ema(closes, period):
    return _strategy_call("calculate_ema", _builtin_calculate_ema, closes, period)


def detect_buy_signal(symbol, current_price=None):
    module = get_strategy_logic()
    if module is not None:
        try:
            fn = getattr(module, "detect_buy_signal")
            try:
                return bool(fn(symbol, current_price))
            except TypeError:
                return bool(fn(symbol))
        except Exception as exc:
            log(f"Error en detect_buy_signal editable; se usa regla integrada: {exc}")
    return bool(_builtin_detect_buy_signal(symbol, current_price))


def is_near_resistance(symbol, current_price, tolerance=0.008):
    return bool(_strategy_call("is_near_resistance", _builtin_is_near_resistance, symbol, current_price, tolerance))


def _builtin_filter_market_symbols(ticker_data):
    return _embedded_strategy_function("filter_market_symbols")(ticker_data)

def filter_market_symbols(ticker_data):
    return _strategy_call("filter_market_symbols", _builtin_filter_market_symbols, ticker_data)


def initial_sl_tp():
    return _strategy_call("initial_sl_tp", lambda: (PUMP_SL_INITIAL, PUMP_TP_INITIAL))


def calculate_buy_amount(balance, requested, open_positions=None, sl_pct=None):
    """Tamaño fijo estilo 1.2.4: monto solicitado, hasta 99% del saldo libre.

    El cálculo no cambia por SL ni por límites de riesgo; SL/TP siguen siendo
    reglas de salida locales en la estrategia 1.2.5.
    """
    try:
        free_balance = max(0.0, float(balance))
        requested_value = max(0.0, float(requested))
        if not math.isfinite(free_balance) or not math.isfinite(requested_value):
            return 0.0
        return min(requested_value, free_balance * 0.99)
    except (TypeError, ValueError, OverflowError):
        return 0.0

def _builtin_manage_position(position, price, now=None):
    return _embedded_strategy_function("manage_position")(position, price, now)

def manage_strategy_position(position, price, now=None):
    now_value = float(now if now is not None else time.time())
    reason = _strategy_call("manage_position", _builtin_manage_position, position, price, now_value)
    if reason:
        return reason
    # Outer hard time cap also applies if a user-edited strategy omits it.
    if isinstance(position, dict) and position.get("estado", "COMPRADO") == "COMPRADO":
        try:
            bought_at = float(position.get("buy_time", now_value) or now_value)
            if now_value - bought_at >= float(MAX_HOLD_MINUTES) * 60.0:
                return f"Tiempo máximo de permanencia ({MAX_HOLD_MINUTES:g} min)"
        except (TypeError, ValueError):
            pass
    return None


class TradingInterface(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Activación de la estrategia guardada; cualquier fallo usa reglas internas.
        activate_pending_strategy()
        get_strategy_logic(force_reload=True)
        self.sections = []
        if not LOG_LINES:
            LOG_LINES.extend([line.rstrip("\n") for line in load_persistent_logs()])
        self.acum = load_acumulado()
        self.keys = load_api_keys()
        log(f"Configuración API cargada: {'sí' if bool(self.keys.get('api') and self.keys.get('secret')) else 'no'} (claves ocultas)")
        self.scanning = False
        self.bot_start_time = None
        self.sell_cooldown = load_sell_cooldown()
        self.trade_lock = threading.RLock()
        self.update_running = False
        self.safe_symbol_cache = {}
        self.exchange_status_cache = {}
        self.exchange_status_cache_at = 0.0
        self.recovery_in_progress = True
        self.background_mode = False
        self.foreground_loops_paused = False
        self.service_started = False

        self.static_symbols = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
            "TONUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "NEARUSDT",
            "SUIUSDT", "HBARUSDT", "APTUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "FETUSDT",
            "WIFUSDT", "PEPEUSDT", "LTCUSDT", "ETCUSDT", "FILUSDT", "VETUSDT", "ALGOUSDT",
            "KASUSDT", "STXUSDT", "ENAUSDT", "ONDOUSDT"
        ]

        self.symbols_to_scan = []

        if platform == 'android':
            try:
                mActivity = PythonActivity.mActivity
                pm = mActivity.getSystemService(Activity.POWER_SERVICE)
                self.wake_lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MagicPump::PartialWakeLock")
                self.wake_lock.acquire()
                log("Wake Lock adquirido")
            except Exception as e:
                log(f"Error wake lock: {e}")

        self.orientation = "vertical"
        self.spacing = 10
        self.padding = 10

        with self.canvas.before:
            Color(*COLOR_BG_DARK)
            self.rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_bg, pos=self._update_bg)

        self.add_widget(Label(text="MAGIC PUMP 24/7 - AGRESIVO", font_size=FONT_SIZE_TITLE, size_hint_y=0.1, color=COLOR_ACCENT_NEON, bold=True))

        self.capital_lbl = Label(text="Capital: Cargando...", size_hint_y=0.04, color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        self.add_widget(self.capital_lbl)

        top = BoxLayout(size_hint_y=0.06, spacing=8)
        self.btn_api = Button(text="API + MONTO", background_color=COLOR_CARD_DARK, font_size=FONT_SIZE_SMALL)
        self.btn_logs = Button(text="LOGS", background_color=COLOR_CARD_DARK, font_size=FONT_SIZE_SMALL)
        self.btn_strategy = Button(text="CÓDIGO COMPRA/VENTA", background_color=COLOR_CARD_DARK, font_size=FONT_SIZE_SMALL)
        self.btn_api.bind(on_press=self.config_api)
        self.btn_logs.bind(on_press=self.show_logs_popup)
        self.btn_strategy.bind(on_press=self.open_strategy_editor)
        top.add_widget(self.btn_api)
        top.add_widget(self.btn_logs)
        top.add_widget(self.btn_strategy)
        self.add_widget(top)

        self.btn_manual_buy = Button(text="COMPRAR MANUAL", background_color=(0.5,1,0,1), size_hint_y=0.06, font_size=FONT_SIZE_NORMAL)
        self.btn_manual_buy.bind(on_press=self.manual_buy_popup)
        self.add_widget(self.btn_manual_buy)

        self.btn_recover_binance = Button(text="RECUPERAR DESDE BINANCE", background_color=(0.9, 0.6, 0.1, 1), size_hint_y=0.06, font_size=FONT_SIZE_NORMAL)
        self.btn_recover_binance.bind(on_press=self.confirm_recover_binance)
        self.add_widget(self.btn_recover_binance)

        self.status_lbl = Label(text="Bot iniciando...", size_hint_y=0.04, color=COLOR_ACCENT_NEON, font_size=FONT_SIZE_NORMAL)
        self.add_widget(self.status_lbl)

        scroll = ScrollView(size_hint=(1, 0.5), do_scroll_x=False, bar_width=8, bar_color=(0.5, 0.5, 0.5, 0.7))
        cont = BoxLayout(orientation='vertical', spacing=10, size_hint_y=None)
        cont.bind(minimum_height=cont.setter('height'))

        for i in range(NUM_TRADING_SECTIONS):
            sec = self.create_section(i)
            self.sections.append(sec)
            cont.add_widget(sec)

        scroll.add_widget(cont)
        self.add_widget(scroll)

        bottom = BoxLayout(size_hint_y=0.14, orientation="vertical", padding=8)
        with bottom.canvas.before:
            Color(*COLOR_CARD_DARK)
            Rectangle(size=bottom.size, pos=bottom.pos)
        bottom.add_widget(Label(text="G/P ACUMULADA REAL", color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL))
        self.acum_real_lbl = Label(text="$0.00 USDT", color=COLOR_ACCENT_NEON, font_size=FONT_SIZE_ACCENT)
        bottom.add_widget(self.acum_real_lbl)
        self.date_lbl = Label(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL)
        bottom.add_widget(self.date_lbl)
        self.add_widget(bottom)

        Clock.schedule_once(lambda dt: threading.Thread(
            target=self._recover_startup, daemon=True
        ).start(), 0.5)
        Clock.schedule_interval(self.update_all, max(0.5, float(UI_MONITOR_INTERVAL)))
        Clock.schedule_once(self.update_capital, 0.5)
        Clock.schedule_once(self.on_start_activation, 1.0)

        self.auto_save_event = Clock.schedule_interval(self.periodic_save, 8)
        Window.bind(on_keyboard=self._on_keyboard)

    def _recover_startup(self):
        try:
            with engine_file_lock():
                self.recover_all_active_positions()
        except Exception as e:
            log(f"Error recuperando posiciones guardadas: {e}")
            self.alert("Recuperación", "No se pudieron verificar todas las posiciones; revisa Binance")
        finally:
            self.recovery_in_progress = False

    def _write_service_control(self, enabled):
        try:
            path = os.path.join(get_data_dir(), "bot_control.json")
            temp = path + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump({"enabled": bool(enabled), "updated": time.time()}, f)
            os.replace(temp, path)
        except Exception as e:
            log(f"No se pudo actualizar el control del servicio: {e}")

    def enter_background_mode(self):
        if platform != "android" or self.background_mode:
            return
        self.background_mode = True
        self.foreground_loops_paused = True
        Clock.unschedule(self.auto_scan)
        Clock.unschedule(self.update_all)
        Clock.unschedule(self.periodic_save)
        Clock.unschedule(self.periodic_refresh_symbols)
        try:
            # Persisted position/config writes already happen when trades/settings
            # change. Start promptly from onPause; the service serializes itself
            # behind any current UI worker before it reads or trades shared state.
            self._write_service_control(True)
            from jnius import autoclass
            ServiceBot = autoclass('com.eddy.magicpump.ServiceBot')
            activity = PythonActivity.mActivity
            # p4a arbitrary services use start(activity, argument); the argument
            # becomes PYTHON_SERVICE_ARGUMENT in the service process.
            ServiceBot.start(activity, get_data_dir())
            self.service_started = True
            log("Servicio Android de trading iniciado")
        except Exception as e:
            self.background_mode = False
            self._write_service_control(False)
            log(f"No se pudo iniciar el servicio Android: {e}")
            self._resume_foreground_loops()

    def leave_background_mode(self):
        if platform != "android":
            return
        self._write_service_control(False)
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            activity = PythonActivity.mActivity
            ServiceBot = autoclass('com.eddy.magicpump.ServiceBot')
            activity.stopService(Intent(activity, ServiceBot))
        except Exception as e:
            log(f"Aviso al detener servicio: {e}")
        self.service_started = False
        self.background_mode = False
        # Do not block the UI thread waiting on a service/network operation. The
        # latest position snapshot is loaded in a worker before scans resume.
        self.status_lbl.text = "Sincronizando posiciones..."
        threading.Thread(target=self._load_service_snapshot, daemon=True).start()

    def _load_service_snapshot(self):
        try:
            with engine_file_lock():
                snapshot = (
                    load_acumulado(),
                    load_positions_from_file(),
                    load_sell_cooldown()
                )
        except Exception as e:
            log(f"Error sincronizando posiciones al volver: {e}")
            snapshot = (load_acumulado(), [], load_sell_cooldown())
        Clock.schedule_once(lambda dt: self._apply_service_snapshot(snapshot), 0)

    def _apply_service_snapshot(self, snapshot):
        self.acum, positions, self.sell_cooldown = snapshot
        for sec in self.sections:
            sec.data.update({
                "estado": "INACTIVO", "moneda": None, "entry_price": 0.0,
                "cantidad": 0.0, "inversion": 0.0, "buy_time": 0.0,
                "last_tp_price": 0.0, "tp_reached": False,
                "sl_pause": False, "sell_pending": False
            })
            self.update_section_display(sec)
        for pos in positions:
            self.assign_position(pos, qty=float(pos.get("cantidad", 0) or 0),
                                 price=float(pos.get("entry_price", 0) or 0))
        self.update_capital()
        self._resume_foreground_loops()
        log("Interfaz en primer plano; posiciones y capital sincronizados; escáner reanudado")

    def _resume_foreground_loops(self):
        if not self.foreground_loops_paused:
            return
        self.foreground_loops_paused = False
        Clock.schedule_interval(self.update_all, max(0.5, float(UI_MONITOR_INTERVAL)))
        Clock.schedule_interval(self.periodic_save, 8)
        Clock.schedule_interval(self.periodic_refresh_symbols, 14400)
        self.scan_event = Clock.schedule_interval(self.auto_scan, AUTO_SCAN_INTERVAL)

    def periodic_save(self, dt):
        if self.background_mode or self.recovery_in_progress:
            return
        with engine_file_lock():
            save_positions(self.sections)
            save_sell_cooldown(self.sell_cooldown)

    def _on_keyboard(self, window, key, *largs):
        if key == 27:
            return True

    def _update_bg(self, *args):
        self.rect.size = self.size
        self.rect.pos = self.pos

    def open_strategy_editor(self, inst=None):
        try:
            source = read_strategy_source()
        except Exception as exc:
            self.alert("Editor", f"No se pudo abrir el código: {exc}")
            return
        content = BoxLayout(orientation="vertical", spacing=5, padding=6)
        notice = Label(
            text="Edita filtros, compras, SL/TP y salidas. Guarda y reinicia la app para aplicar.\nEl código corre con permisos de Magic Pump: no pegues código de desconocidos.",
            size_hint_y=0.13, font_size=FONT_SIZE_SMALL, color=COLOR_TEXT_LIGHT
        )
        editor = TextInput(
            text=source, multiline=True, size_hint_y=0.78,
            font_size="12sp", padding=(8, 8),
            background_color=(0.08, 0.08, 0.11, 1), foreground_color=COLOR_TEXT_LIGHT
        )
        status = Label(text="Edita el código de estrategia", size_hint_y=0.05,
                       font_size=FONT_SIZE_SMALL, color=COLOR_ACCENT_NEON)
        row = BoxLayout(size_hint_y=0.08, spacing=4)
        check_btn = Button(text="VALIDAR", font_size=FONT_SIZE_SMALL, background_color=COLOR_CARD_DARK)
        save_btn = Button(text="GUARDAR", font_size=FONT_SIZE_SMALL, background_color=COLOR_ACCENT_NEON)
        restore_btn = Button(text="ORIGINAL", font_size=FONT_SIZE_SMALL, background_color=COLOR_CARD_DARK)
        close_btn = Button(text="CERRAR", font_size=FONT_SIZE_SMALL, background_color=COLOR_CARD_DARK)
        for button in (check_btn, save_btn, restore_btn, close_btn):
            row.add_widget(button)
        content.add_widget(notice)
        content.add_widget(editor)
        content.add_widget(status)
        content.add_widget(row)
        popup = Popup(title="Código de compra y venta", content=content,
                      size_hint=(0.98, 0.96), auto_dismiss=False)

        def validate_code(*_):
            ok, message = validate_strategy_source(editor.text)
            status.text = ("✔ " if ok else "✖ ") + message
            status.color = COLOR_ACCENT_NEON if ok else COLOR_ACCENT_ERROR

        def save_code(*_):
            ok, message = save_pending_strategy_source(editor.text)
            status.text = ("✔ " if ok else "✖ ") + message
            status.color = COLOR_ACCENT_NEON if ok else COLOR_ACCENT_ERROR
            if ok:
                self.alert("Código guardado", "Se activa al cerrar completamente y volver a abrir la app.")

        def restore_code(*_):
            ok, message = restore_default_strategy_pending()
            if ok:
                editor.text = read_strategy_source()
            status.text = ("✔ " if ok else "✖ ") + message
            status.color = COLOR_ACCENT_NEON if ok else COLOR_ACCENT_ERROR

        check_btn.bind(on_press=validate_code)
        save_btn.bind(on_press=save_code)
        restore_btn.bind(on_press=restore_code)
        close_btn.bind(on_press=lambda *_: popup.dismiss())
        popup.open()

    def config_api(self, inst):
        content = BoxLayout(orientation="vertical", padding=10, spacing=10)
        api_in = TextInput(hint_text="API Key", text=self.keys.get("api",""))
        sec_in = TextInput(hint_text="Secret Key", password=True, text=self.keys.get("secret",""))
        invest_in = TextInput(hint_text="Monto por operación (USDT)", text=str(self.keys.get("invest_amount", MIN_PURCHASE_USDT)))
        
        btn_save = Button(text="GUARDAR", background_color=COLOR_ACCENT_NEON)
        
        def save_config(x):
            try:
                new_amount = float(invest_in.text)
                if new_amount < MIN_TRADE_USDT:
                    new_amount = float(MIN_TRADE_USDT)
            except:
                new_amount = MIN_PURCHASE_USDT
            save_api_keys(api_in.text, sec_in.text, new_amount)
            self.keys = load_api_keys()
            popup.dismiss()
            self.update_capital()
            self.alert("Configuración", f"Guardado correctamente\nMonto por operación: ${new_amount:.2f} USDT")

        btn_save.bind(on_press=save_config)
        
        content.add_widget(Label(text="Configurar API Binance + Monto"))
        content.add_widget(api_in)
        content.add_widget(sec_in)
        content.add_widget(Label(text="Monto fijo por compra (USDT)"))
        content.add_widget(invest_in)
        content.add_widget(btn_save)
        popup = Popup(title="Configuración", content=content, size_hint=(0.9,0.65))
        popup.open()

    def buy_if_possible(self, symbol, client, investment=None, sl_pct=None, tp_pct=None, is_manual=False):
        # This method is called from worker threads; keep network I/O off the UI thread.
        with self.trade_lock:
            if not client or not client.use_real:
                self.alert("API", "Configura una API válida de Binance para operar")
                return
            sec = next((s for s in self.sections if s.data["estado"] == "INACTIVO"), None)
            if sec is None:
                self.alert("Compra", "No hay espacios libres para registrar la posición")
                return
            if any(s.data.get("moneda") == symbol and s.data["estado"] == "COMPRADO" for s in self.sections):
                self.alert("Compra", f"Ya hay una posición activa en {symbol}")
                return
            price = get_price_usdt(symbol)
            if not price:
                self.alert("Error", f"Precio no disponible: {symbol}")
                return
            balance = get_real_usdt_balance_total()
            if balance < MIN_TRADE_USDT:
                log(f"Compra bloqueada {symbol}: saldo USDT libre menor al mínimo configurado")
                return

            try:
                strategy_sl, strategy_tp = initial_sl_tp()
                requested = float(investment if investment is not None else self.keys.get("invest_amount", MIN_PURCHASE_USDT))
                sl_value = float(sl_pct if sl_pct is not None else strategy_sl)
                tp_value = float(tp_pct if tp_pct is not None else strategy_tp)
            except Exception as exc:
                log(f"Compra bloqueada {symbol}: estrategia inválida: {exc}")
                self.alert("Compra", "Monto, SL, TP o estrategia inválidos")
                return
            if requested < MIN_TRADE_USDT or not (0.1 <= sl_value <= 30) or not (0.1 <= tp_value <= 100):
                self.alert("Compra", f"Monto mínimo ${MIN_TRADE_USDT:g}; SL/TP fuera del rango permitido")
                return

            try:
                active_positions = [dict(s.data) for s in self.sections if s.data.get("estado") == "COMPRADO"]
                calculated = float(calculate_buy_amount(balance, requested, active_positions, sl_value))
                if not math.isfinite(calculated) or calculated <= 0:
                    raise ValueError("monto calculado inválido")
                desired_investment = min(calculated, balance * 0.99)
            except Exception as exc:
                log(f"Compra bloqueada {symbol}: error calculando monto: {exc}")
                return
            if desired_investment < MIN_TRADE_USDT:
                log(f"Compra bloqueada {symbol}: saldo libre insuficiente")
                return

            qty = self.get_adjusted_qty(symbol, desired_investment / price)
            if not qty:
                self.alert("Compra", f"Monto inferior al mínimo/notional permitido para {symbol}")
                return
            order = client.create_market_buy_order(symbol, qty)
            if not order:
                self.alert("Error", f"Compra fallida: {symbol}")
                return

            time.sleep(1.0)
            order_status = client.get_order(symbol, order['orderId'])
            if order_status and order_status.get('status') not in ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'):
                client.cancel_order(symbol, order['orderId'])
                time.sleep(0.3)
                order_status = client.get_order(symbol, order['orderId']) or order_status
            if not order_status:
                self.alert("Atención", f"No pude verificar la orden de {symbol}; revisa Binance antes de reintentar")
                return

            qty_exec = float(order_status.get('executedQty', 0) or 0)
            if qty_exec <= 0:
                self.alert("Compra", f"Orden no ejecutada ({order_status.get('status', 'desconocido')}): {symbol}")
                return
            quote_exec = float(order_status.get('cummulativeQuoteQty', 0) or 0)
            entry_price = quote_exec / qty_exec if quote_exec > 0 else price
            fills = order_status.get('fills') or order.get('fills') or []
            if fills:
                filled_qty = sum(float(f.get('qty', 0)) for f in fills)
                filled_quote = sum(float(f.get('price', 0)) * float(f.get('qty', 0)) for f in fills)
                if filled_qty > 0 and filled_quote > 0:
                    entry_price = filled_quote / filled_qty
                    quote_exec = filled_quote
            inversion = quote_exec if quote_exec > 0 else qty_exec * entry_price
            if order_status.get('status') != 'FILLED':
                log(f"Compra parcial registrada {symbol}: {qty_exec:g} unidades")

            first_tp = entry_price * (1 + tp_value / 100.0)
            sec.data.update({
                "estado": "COMPRADO", "moneda": symbol, "entry_price": entry_price,
                "cantidad": qty_exec, "inversion": inversion, "sl_pct": sl_value,
                "tp_pct": tp_value, "buy_time": time.time(),
                "buy_order_id": str(order_status.get("orderId", order.get("orderId", "")) or ""),
                "current_tp_price": first_tp, "last_tp_price": first_tp,
                "tp_reached": False, "sl_pause": False, "sell_pending": False
            })
            self.update_section_display(sec, entry_price)
            save_positions(self.sections)
            self.alert("COMPRA OK", f"NORMAL | {symbol} por ${inversion:.2f} @ {entry_price:.8f}")
            self.update_capital()

    def check_tp_sl(self, sec, price):
        d = sec.data
        if d.get("estado") != "COMPRADO":
            return
        reason = manage_strategy_position(d, price, time.time())
        if reason:
            self.sell_section(sec, reason=reason)
            return
        self.update_section_display(sec, price)
        save_positions(self.sections)

    @mainthread
    def update_section_display(self, sec, price=None):
        w = sec.widgets
        d = sec.data

        if d["estado"] != "COMPRADO":
            w["txt_symbol"].text = ""
            w["lbl_state"].text = "INACTIVO"
            w["btn_vender"].disabled = True
            return

        if not price:
            price = get_price_usdt(d["moneda"]) or d["entry_price"]

        sl_price = d["entry_price"] * (1 - d["sl_pct"] / 100)
        current_tp = d.get("current_tp_price", 0)

        w["txt_symbol"].text = d["moneda"].replace("USDT", "")
        w["inp_real"].text = f"{price:.6f}"
        w["inp_entry"].text = f"{d['entry_price']:.6f}"

        w["spin_sl"].text = f"{d['sl_pct']:.1f}%"
        w["spin_tp"].text = f"{d['tp_pct']:.1f}%"

        w["lbl_sl_price"].text = f"{sl_price:.6f}"
        w["lbl_tp_price"].text = f"{current_tp:.6f}"
        w["lbl_p_venta"].text = f"{current_tp:.6f}"

        w["lbl_inversion"].text = f"${d['inversion']:.2f}"
        w["lbl_cantidad"].text = f"{d['cantidad']:.4f}"

        if d.get("sl_pause", False):
            w["lbl_state"].text = "⛔ SL PAUSADO"
            w["lbl_state"].color = (1, 0.2, 0.2, 1)
        else:
            w["lbl_state"].text = "COMPRADO"
            w["lbl_state"].color = COLOR_TEXT_LIGHT

        w["btn_vender"].disabled = False

        pg = (price * d["cantidad"]) - d["inversion"]
        w["inp_pg"].text = f"${pg:+.2f} USDT"
        w["inp_pg"].background_color = (
            COLOR_ACCENT_NEON if pg >= 0
            else COLOR_ACCENT_ERROR
        )

    def _release_untradeable_slot(self, sec, symbol, residual_qty, message, title="RESIDUO NO VENDIBLE"):
        """Clear only the app's local position record; the Binance asset is untouched."""
        self.sell_cooldown[symbol] = time.time()
        save_sell_cooldown(self.sell_cooldown)
        sec.data.update({
            "estado": "INACTIVO", "moneda": None, "entry_price": 0.0,
            "cantidad": 0.0, "inversion": 0.0, "buy_time": 0.0,
            "current_tp_price": 0.0,
            "last_tp_price": 0.0, "tp_reached": False, "sl_pause": False,
            "sell_pending": False
        })
        self.update_section_display(sec)
        save_positions(self.sections)
        self.update_capital()
        log(f"{symbol}: espacio liberado; residuo confirmado no vendible={float(residual_qty or 0):.8f}")
        self.alert(title, message)

    def sell_section(self, sec, reason="Manual"):
        d = sec.data
        if d.get("estado") != "COMPRADO" or d.get("sell_pending", False):
            return
        d["sell_pending"] = True
        original_qty = float(d.get("cantidad", 0))
        original_investment = float(d.get("inversion", 0))
        symbol = d.get("moneda")
        try:
            client = RealBinanceClientWrapper(self.keys.get("api", ""), self.keys.get("secret", ""))
            if not client.use_real:
                self.alert("API", "No se pudo conectar a Binance; posición conservada")
                return
            asset = symbol.replace("USDT", "")
            bal = client.get_asset_balance(asset)
            free_balance = float(bal.get("free", 0)) if bal else 0.0
            # Nunca vender holdings ajenos a la cantidad que esta posición registra.
            qty_to_sell = min(free_balance, original_qty)
            if qty_to_sell <= 0:
                residual_state, residual_qty, _value = inspect_residual_sellability(
                    client, symbol, original_qty
                )
                if residual_state == "dust":
                    self._release_untradeable_slot(
                        sec, symbol, residual_qty,
                        f"No queda saldo vendible de {asset}; el registro local se liberó. No se vendió ni convirtió ningún residuo."
                    )
                    return
                self.alert("Error", f"Sin saldo libre de {asset}; posición conservada porque no pude confirmar el remanente")
                return
            qty = self.get_adjusted_qty(symbol, qty_to_sell, is_sell=True)
            if not qty or qty <= 0:
                residual_state, residual_qty, _value = inspect_residual_sellability(
                    client, symbol, qty_to_sell
                )
                if residual_state == "dust":
                    self._release_untradeable_slot(
                        sec, symbol, residual_qty,
                        f"El residuo de {asset} no cumple los mínimos de Binance. Sigue en tu cuenta; liberé el espacio y no envié otra orden."
                    )
                    return
                self.alert("Error", f"Cantidad no apta para vender {symbol}; conservo el seguimiento porque no pude confirmar los mínimos")
                return
            order = client.create_market_sell_order(symbol, qty)
            if not order:
                residual_state, residual_qty, _value = inspect_residual_sellability(
                    client, symbol, qty_to_sell
                )
                if residual_state == "dust":
                    self._release_untradeable_slot(
                        sec, symbol, residual_qty,
                        f"Binance no aceptó la cantidad mínima de {asset}. El residuo sigue en tu cuenta; liberé el espacio sin convertirlo."
                    )
                    return
                self.alert("Error", f"No se pudo crear la venta de {symbol}; conservo el seguimiento")
                return
            time.sleep(1.0)
            order_status = client.get_order(symbol, order['orderId'])
            if order_status and order_status.get('status') not in ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'):
                client.cancel_order(symbol, order['orderId'])
                time.sleep(0.3)
                order_status = client.get_order(symbol, order['orderId']) or order_status
            if not order_status:
                self.alert("Atención", f"No pude verificar la venta de {symbol}; revisa Binance")
                return
            qty_exec = float(order_status.get('executedQty', 0) or 0)
            if qty_exec <= 0:
                residual_state, residual_qty, _value = inspect_residual_sellability(
                    client, symbol, qty_to_sell
                )
                if residual_state == "dust":
                    self._release_untradeable_slot(
                        sec, symbol, residual_qty,
                        f"La venta de {symbol} no se ejecutó y el saldo restante no cumple los mínimos. El residuo sigue en Binance; liberé el espacio."
                    )
                    return
                self.alert("Error", f"Venta no ejecutada ({order_status.get('status', 'desconocido')}): {symbol}; conservo el seguimiento")
                return
            quote_exec = float(order_status.get('cummulativeQuoteQty', 0) or 0)
            sell_price = quote_exec / qty_exec if quote_exec > 0 else get_price_usdt(symbol) or d["entry_price"]
            fills = order_status.get('fills') or order.get('fills') or []
            if fills:
                filled_qty = sum(float(f.get('qty', 0)) for f in fills)
                filled_quote = sum(float(f.get('price', 0)) * float(f.get('qty', 0)) for f in fills)
                if filled_qty > 0 and filled_quote > 0:
                    sell_price = filled_quote / filled_qty
                    quote_exec = filled_quote
            proceeds = quote_exec if quote_exec > 0 else sell_price * qty_exec
            cost_basis = original_investment * min(1.0, qty_exec / original_qty) if original_qty > 0 else 0.0
            pg = proceeds - cost_basis
            self.acum["acumulado_real"] += pg
            save_acumulado(self.acum)
            remaining_qty = max(0.0, original_qty - qty_exec)
            epsilon = max(original_qty * 1e-8, 1e-12)
            residual_state = "complete"
            residual_qty = remaining_qty
            residual_value = None
            if remaining_qty > epsilon:
                residual_state, residual_qty, residual_value = inspect_residual_sellability(
                    client, symbol, remaining_qty
                )

            if remaining_qty > epsilon and residual_state == "dust":
                if residual_qty > 0:
                    residual_note = f"residuo no vendible {residual_qty:.8f} {symbol[:-4]} queda en Binance; espacio liberado"
                else:
                    residual_note = "no queda saldo vendible; espacio liberado"
                self._release_untradeable_slot(
                    sec, symbol, residual_qty,
                    f"Venta ejecutada: {symbol} | cantidad: {qty_exec:.8f} | G/P aprox.: ${pg:+.2f} USDT | {residual_note}",
                    title="VENTA OK"
                )
                return

            # Refresh after every confirmed execution, including partial sales.
            self.update_capital()
            if remaining_qty > epsilon and residual_state != "dust":
                if residual_state == "sellable":
                    d["cantidad"] = residual_qty
                else:
                    d["cantidad"] = remaining_qty
                d["inversion"] = max(0.0, original_investment - cost_basis)
                d["sell_pending"] = False
                self.update_section_display(sec, get_price_usdt(symbol))
                save_positions(self.sections)
                if residual_state == "sellable":
                    self.alert("VENTA PARCIAL", f"{symbol} | vendido: {qty_exec:.8f} | restante vendible: {residual_qty:.8f} | G/P aprox.: ${pg:+.2f} USDT")
                else:
                    self.alert("VENTA PARCIAL", f"{symbol} | venta ejecutada; no pude confirmar si el saldo restante es vendible. Mantengo la posición para no perder seguimiento.")
                return

            self.sell_cooldown[symbol] = time.time()
            save_sell_cooldown(self.sell_cooldown)
            d.update({
                "estado": "INACTIVO", "moneda": None, "entry_price": 0.0,
                "cantidad": 0.0, "inversion": 0.0, "buy_time": 0.0,
                "last_tp_price": 0.0, "tp_reached": False, "sl_pause": False,
                "sell_pending": False
            })
            self.update_section_display(sec)
            save_positions(self.sections)
            self.alert("VENTA OK", f"{symbol} | precio: {sell_price:.8f} | cantidad: {qty_exec:.8f} | G/P aprox.: ${pg:+.2f} USDT | {reason}")
        except Exception as e:
            log(f"Error en venta {symbol}: {e}")
            self.alert("Error", f"Falló la venta de {symbol}; revisa la cuenta de Binance")
        finally:
            if d.get("estado") == "COMPRADO":
                d["sell_pending"] = False

    def show_logs_popup(self, inst):
        persisted = load_persistent_logs()
        if persisted:
            LOG_LINES[:] = [line.rstrip("\n") for line in persisted[-MAX_LOG_LINES:]]
        if not LOG_LINES:
            self.alert("LOGS", "No hay logs")
            return
        content = BoxLayout(orientation="vertical")
        log_text = TextInput(text="\n".join(LOG_LINES[-500:]), readonly=True, size_hint_y=None, height=800)
        scroll = ScrollView()
        scroll.add_widget(log_text)
        content.add_widget(scroll)
        close_btn = Button(text="CERRAR", size_hint_y=None, height=50)
        close_btn.bind(on_press=lambda x: popup.dismiss())
        content.add_widget(close_btn)
        popup = Popup(title="Logs", content=content, size_hint=(0.95, 0.9))
        popup.open()

    @mainthread
    def alert(self, title, message):
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(text=message))
        btn = Button(text="OK", size_hint_y=None, height=40)
        popup = Popup(title=title, content=content, size_hint=(0.7, 0.3), auto_dismiss=False)
        btn.bind(on_press=popup.dismiss)
        content.add_widget(btn)
        popup.open()
        Clock.schedule_once(lambda dt: popup.dismiss() if popup.content else None, 5)

    def confirm_sell_popup(self, sec):
        if sec.data["estado"] != "COMPRADO": return
        symbol = sec.data["moneda"]
        try:
            current_price = float(sec.widgets["inp_real"].text)
        except (TypeError, ValueError):
            current_price = sec.data["entry_price"]
        pg = (current_price * sec.data["cantidad"]) - sec.data["inversion"]
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(text=f"¿Confirmas VENTA de {symbol}?"))
        content.add_widget(Label(text=f"Precio actual: ${current_price:.6f}"))
        content.add_widget(Label(text=f"G/P estimada: ${pg:+.2f} USDT"))
        btn_layout = BoxLayout(size_hint_y=None, height=60, spacing=20)
        btn_yes = Button(text="SÍ, VENDER", background_color=(0.9, 0.2, 0.2, 1))
        btn_no = Button(text="CANCELAR", background_color=(0.3, 0.3, 0.3, 1))
        def do_sell(instance):
            popup.dismiss()
            threading.Thread(target=self._manual_sell_worker, args=(sec,), daemon=True).start()
        def cancel(instance):
            popup.dismiss()
        btn_yes.bind(on_press=do_sell)
        btn_no.bind(on_press=cancel)
        btn_layout.add_widget(btn_yes)
        btn_layout.add_widget(btn_no)
        content.add_widget(btn_layout)
        popup = Popup(title="Confirmar Venta Manual", content=content, size_hint=(0.85, 0.55))
        popup.open()

    def create_section(self, idx):
        sec = BoxLayout(orientation="vertical", size_hint_y=None, height=350, spacing=4, padding=8)
        with sec.canvas.before:
            Color(*COLOR_CARD_DARK)
            Rectangle(size=sec.size, pos=sec.pos)

        row1 = BoxLayout(size_hint_y=0.15, spacing=6)
        txt_symbol = TextInput(hint_text="Símbolo", readonly=True, background_color=COLOR_BG_DARK, foreground_color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        btn_vender = Button(text="VENDER AHORA", disabled=True, background_color=COLOR_ACCENT_ERROR, font_size=FONT_SIZE_NORMAL)
        row1.add_widget(txt_symbol)
        row1.add_widget(btn_vender)

        row_inv = BoxLayout(size_hint_y=0.1, spacing=4)
        row_inv.add_widget(Label(text="INVERSIÓN", color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL))
        row_inv.add_widget(Label(text="CANTIDAD", color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL))
        row_inv.add_widget(Label(text="P. COMPRA", color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL))

        row_inv_val = BoxLayout(size_hint_y=0.15, spacing=4)
        lbl_inversion = Label(text="$0.00", color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        lbl_cantidad = Label(text="0.00", color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        inp_entry = TextInput(readonly=True, background_color=COLOR_BG_DARK, foreground_color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        row_inv_val.add_widget(lbl_inversion)
        row_inv_val.add_widget(lbl_cantidad)
        row_inv_val.add_widget(inp_entry)

        row_head = BoxLayout(size_hint_y=0.1, spacing=4)
        for text in ["P. REAL", "P. VENTA", "SL (%)", "TP (%)", "ESTADO"]:
            row_head.add_widget(Label(text=text, color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL))

        row_val = BoxLayout(size_hint_y=0.15, spacing=4)
        inp_real = TextInput(readonly=True, background_color=COLOR_BG_DARK, foreground_color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        lbl_p_venta = Label(text="$0.000000", color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        spin_sl = Spinner(text=f"{PUMP_SL_INITIAL:.1f}%", values=[f"{x/10:.1f}%" for x in range(5, 301)], font_size=FONT_SIZE_NORMAL)
        spin_tp = Spinner(text=f"{PUMP_TP_INITIAL:.1f}%", values=[f"{x/10:.1f}%" for x in range(10, 1001)], font_size=FONT_SIZE_NORMAL)
        lbl_state = Label(text="INACTIVO", color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_NORMAL)
        row_val.add_widget(inp_real)
        row_val.add_widget(lbl_p_venta)
        row_val.add_widget(spin_sl)
        row_val.add_widget(spin_tp)
        row_val.add_widget(lbl_state)

        row_prices = BoxLayout(size_hint_y=0.1, spacing=4)
        row_prices.add_widget(Label(text="SL Precio", color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL))
        lbl_sl_price = Label(text="$0.000000", color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        row_prices.add_widget(lbl_sl_price)
        row_prices.add_widget(Label(text="TP Precio", color=COLOR_TEXT_MUTED, font_size=FONT_SIZE_SMALL))
        lbl_tp_price = Label(text="$0.000000", color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL)
        row_prices.add_widget(lbl_tp_price)

        row_pg = BoxLayout(size_hint_y=0.2)
        row_pg.add_widget(Label(text="G/P (USDT)", color=COLOR_TEXT_MUTED, size_hint_x=0.4, font_size=FONT_SIZE_SMALL))
        inp_pg = TextInput(text="$0.00 USDT", readonly=True, background_color=COLOR_CARD_DARK, foreground_color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_ACCENT)
        row_pg.add_widget(inp_pg)

        sec.add_widget(row1)
        sec.add_widget(row_inv)
        sec.add_widget(row_inv_val)
        sec.add_widget(row_head)
        sec.add_widget(row_val)
        sec.add_widget(row_prices)
        sec.add_widget(row_pg)

        sec.widgets = {
            "txt_symbol": txt_symbol, "inp_real": inp_real, "inp_entry": inp_entry,
            "spin_sl": spin_sl, "spin_tp": spin_tp, "lbl_state": lbl_state,
            "lbl_sl_price": lbl_sl_price, "lbl_tp_price": lbl_tp_price,
            "lbl_inversion": lbl_inversion, "lbl_cantidad": lbl_cantidad,
            "lbl_p_venta": lbl_p_venta, "inp_pg": inp_pg, "btn_vender": btn_vender
        }

        sec.data = {
            "estado": "INACTIVO",
            "moneda": None,
            "entry_price": 0.0,
            "cantidad": 0.0,
            "inversion": 0.0,
            "sl_pct": PUMP_SL_INITIAL,
            "tp_pct": PUMP_TP_INITIAL,
            "buy_time": 0.0,

            "current_tp_price": 0.0,
            "last_tp_price": 0.0,
            "tp_reached": False,
            "sl_pause": False
        }

        sec.widgets["spin_tp"].bind(text=lambda instance, value: self.on_manual_tp_change(sec, value))
        sec.widgets["spin_sl"].bind(text=lambda instance, value: self.on_manual_sl_change(sec, value))
        btn_vender.bind(on_press=lambda x, s=sec: self.confirm_sell_popup(s))
        return sec

    def on_manual_tp_change(self, sec, value_str):
        if sec.data["estado"] != "COMPRADO": return
        try:
            new_tp = float(value_str.replace("%", ""))
            if new_tp < 0.1: return
            sec.data["tp_pct"] = new_tp
            if not sec.data.get("tp_reached", False):
                sec.data["current_tp_price"] = sec.data["entry_price"] * (1 + new_tp / 100.0)
                sec.data["last_tp_price"] = sec.data["current_tp_price"]
            with engine_file_lock():
                save_positions(self.sections)
        except:
            pass

    def on_manual_sl_change(self, sec, value_str):
        if sec.data["estado"] != "COMPRADO": return
        try:
            new_sl = float(value_str.replace("%", ""))
            if new_sl < 0.1: return
            sec.data["sl_pct"] = new_sl
            with engine_file_lock():
                save_positions(self.sections)
        except:
            pass

    def get_adjusted_qty(self, symbol, desired_qty, is_sell=False):
        client_wrapper = RealBinanceClientWrapper(
            self.keys.get("api", ""), self.keys.get("secret", "")
        )
        qty, _verified = adjusted_qty_with_status(
            client_wrapper, symbol, desired_qty
        )
        return qty

    # ====================== FUNCIONES CORREGIDAS ======================
    def recover_all_active_positions(self, dt=None):
        client = RealBinanceClientWrapper(self.keys.get("api", ""), self.keys.get("secret", ""))
        json_positions = load_positions_from_file()
        recovered_count = 0
        active_symbols = set()
        recovered_symbols = set()
        confirmed_empty_symbols = set()

        # Si falta la API o Binance no responde, conservar y mostrar posiciones guardadas.
        if not client.use_real:
            for pos in json_positions:
                symbol = pos.get("moneda")
                qty = float(pos.get("cantidad", 0) or 0)
                if not symbol or qty <= 0 or symbol in active_symbols:
                    continue
                price = get_price_usdt(symbol) or float(pos.get("entry_price", 0) or 0)
                self.assign_position(pos, qty=qty, price=price)
                active_symbols.add(symbol)
                recovered_count += 1
            if recovered_count:
                self.alert("RECUPERACIÓN", f"{recovered_count} posiciones locales cargadas; API no conectada")
            self.recovery_in_progress = False
            return

        for pos in json_positions:
            symbol = pos.get("moneda")
            if not symbol or not symbol.endswith("USDT") or symbol in active_symbols:
                continue
            asset = symbol[:-4]
            bal = client.get_asset_balance(asset)
            if not isinstance(bal, dict):
                continue
            qty = max(0.0, float(bal.get("free", 0) or 0))
            locked = max(0.0, float(bal.get("locked", 0) or 0))
            current_price = get_price_usdt(symbol)
            if not current_price:
                continue

            # Una posición local sin saldo libre ni bloqueado, o con un saldo
            # confirmado por debajo de los filtros de venta, es polvo: se libera
            # el espacio sin vender ni borrar la moneda de la cuenta Binance.
            if qty <= 1e-12:
                if locked <= 1e-12:
                    confirmed_empty_symbols.add(symbol)
                    log(f"Recuperación: {symbol} no tiene saldo libre/bloqueado; se limpia el registro local")
                continue
            adjusted_qty, filters_verified = adjusted_qty_with_status(
                client, symbol, qty, price=current_price
            )
            if filters_verified and adjusted_qty is None and locked <= 1e-12:
                confirmed_empty_symbols.add(symbol)
                log(f"Recuperación: residuo no vendible de {symbol}; se libera el espacio, sin tocar Binance")
                continue

            entry_price = float(pos.get("entry_price", current_price or 0))
            inversion = float(pos.get("inversion", qty * entry_price))
            if abs(inversion - qty * entry_price) > inversion * 0.2:
                inversion = qty * entry_price

            pos_data = {
                "moneda": symbol,
                "entry_price": entry_price,
                "cantidad": qty,
                "inversion": inversion,
                "sl_pct": float(pos.get("sl_pct", PUMP_SL_INITIAL)),
                "tp_pct": float(pos.get("tp_pct", PUMP_TP_INITIAL)),
                "buy_time": float(pos.get("buy_time", time.time())),
                "current_tp_price": float(pos.get("current_tp_price", entry_price)),
                "last_tp_price": float(pos.get("last_tp_price", entry_price)),
                "tp_reached": bool(pos.get("tp_reached", False)),
                "sl_pause": bool(pos.get("sl_pause", False))
            }
            self.assign_position(pos_data, qty=qty, price=current_price)
            active_symbols.add(symbol)
            recovered_symbols.add(symbol)
            recovered_count += 1

        if recovered_count > 0:
            self.alert("RECUPERACIÓN", f"{recovered_count} posiciones recuperadas")

        if confirmed_empty_symbols:
            # Elimina solo polvo/saldos ausentes confirmados por Binance; conserva
            # cualquier otro registro que no se haya podido verificar.
            unresolved = [
                pos for pos in json_positions
                if pos.get("moneda") not in confirmed_empty_symbols
                and pos.get("moneda") not in recovered_symbols
            ]
            merged = position_records_from_sections(self.sections)
            merged_symbols = {item.get("moneda") for item in merged}
            merged.extend(
                pos for pos in unresolved
                if pos.get("moneda") not in merged_symbols
            )
            save_position_records(merged)
        elif recovered_count == len(json_positions):
            save_positions(self.sections)
        elif json_positions:
            log("Posiciones locales no confirmadas conservadas en el archivo")
        self.recovery_in_progress = False

    def confirm_recover_binance(self, inst):
        content = BoxLayout(orientation="vertical", padding=10, spacing=10)
        content.add_widget(Label(
            text="Se recuperarán únicamente pedidos BUY con ID previamente guardado por Magic Pump al comprar en la sesión. "
                 "Compras externas/anteriores sin registro local se omiten; si el saldo no respalda el pedido guardado, también se omite. "
                 "¿Quieres continuar?",
            text_size=(Window.width * 0.8, None), halign="center", valign="middle"
        ))
        buttons = BoxLayout(size_hint_y=None, height=52, spacing=8)
        popup = Popup(title="Confirmar recuperación", content=content, size_hint=(0.9, 0.45))
        yes = Button(text="CONTINUAR", background_color=COLOR_ACCENT_ERROR)
        no = Button(text="CANCELAR")
        yes.bind(on_press=lambda *_: (popup.dismiss(), threading.Thread(
            target=self.recover_from_binance, daemon=True
        ).start()))
        no.bind(on_press=popup.dismiss)
        buttons.add_widget(yes)
        buttons.add_widget(no)
        content.add_widget(buttons)
        popup.open()

    def recover_from_binance(self, dt=None):
        log("🔄 Recuperación limitada a compras registradas por Magic Pump en la sesión...")
        self.alert("Recuperando", "Solo se buscarán pedidos BUY cuyo ID quedó guardado por el bot al comprar.")

        client = RealBinanceClientWrapper(self.keys.get("api", ""), self.keys.get("secret", ""))
        if not client.use_real:
            self.alert("Error", "API no configurada")
            return

        try:
            account = client.get_account()
            if not account:
                self.alert("Error", "No se pudo obtener cuenta")
                return
            # This local record is the provenance link to a Magic Pump buy in the active tracked session.
            session_records = load_positions_from_file()
            by_symbol = {p.get("moneda"): p for p in session_records
                         if p.get("moneda") and p.get("buy_order_id")}
            recovered = 0
            skipped = 0
            active_symbols = {section.data.get("moneda") for section in self.sections
                              if section.data.get("estado") == "COMPRADO" and section.data.get("moneda")}
            balances = {str(b.get("asset", "")): b for b in account.get("balances", [])}

            for symbol, record in by_symbol.items():
                if not symbol.endswith("USDT") or symbol in active_symbols:
                    continue
                asset = symbol[:-4]
                balance = balances.get(asset)
                free = max(0.0, float((balance or {}).get("free", 0) or 0))
                order_id = record.get("buy_order_id")
                if not order_id:
                    skipped += 1
                    continue
                order = client.get_order(symbol, order_id)
                if not isinstance(order, dict) or str(order.get("side", "")).upper() != "BUY":
                    skipped += 1
                    log(f"RECUPERACIÓN OMITIDA {symbol}: no se pudo verificar el pedido BUY guardado")
                    continue
                executed_qty = float(order.get("executedQty", 0) or 0)
                quote_cost = float(order.get("cummulativeQuoteQty", 0) or 0)
                if executed_qty <= 0 or quote_cost <= 0:
                    skipped += 1
                    log(f"RECUPERACIÓN OMITIDA {symbol}: pedido guardado sin ejecución/costo verificable")
                    continue
                tolerance = max(executed_qty * 0.002, 1e-8)
                if free + tolerance < executed_qty:
                    skipped += 1
                    log(f"RECUPERACIÓN OMITIDA {symbol}: saldo libre menor que la cantidad del pedido guardado; no atribuyo compras ajenas ni lotes anteriores")
                    continue
                entry_price = quote_cost / executed_qty
                pos_data = {
                    "moneda": symbol,
                    "entry_price": entry_price,
                    "cantidad": executed_qty,
                    "inversion": quote_cost,
                    "sl_pct": float(record.get("sl_pct", PUMP_SL_INITIAL)),
                    "tp_pct": float(record.get("tp_pct", PUMP_TP_INITIAL)),
                    "buy_time": float(record.get("buy_time", time.time())),
                    "buy_order_id": str(order_id),
                    "current_tp_price": entry_price * (1 + float(record.get("tp_pct", PUMP_TP_INITIAL)) / 100.0),
                    "last_tp_price": entry_price * (1 + float(record.get("tp_pct", PUMP_TP_INITIAL)) / 100.0),
                    "tp_reached": False,
                    "sl_pause": bool(record.get("sl_pause", False))
                }
                current_price = get_price_usdt(symbol) or entry_price
                self.assign_position(pos_data, qty=executed_qty, price=current_price)
                active_symbols.add(symbol)
                recovered += 1
                extra = max(0.0, free - executed_qty)
                log(f"RECUPERACIÓN SESIÓN {symbol} | orderId {order_id} | qty {executed_qty:.8f} | costo ${quote_cost:.2f} | otras unidades excluidas {extra:.8f}")

            if recovered or skipped:
                self.alert("Recuperación", f"Compras de sesión recuperadas por ID de orden: {recovered}. Omitidas por falta de registro verificable: {skipped}. Compras anteriores o externas no se consideran.")
            else:
                self.alert("Info", "No hay compras de sesión guardadas para recuperar. Las compras hechas antes de esta corrección no tienen ID de orden local y no pueden atribuirse con seguridad.")
        except Exception as exc:
            log(f"❌ Error recuperación: {exc}")
            self.alert("Error", f"Error: {str(exc)}")
        save_positions(self.sections)

    def assign_position(self, pos_data, qty, price):
        for sec in self.sections:
            if sec.data["estado"] == "INACTIVO":
                entry = pos_data.get("entry_price", price)
                sec.data.update({
                    "estado": "COMPRADO",
                    "moneda": pos_data["moneda"],
                    "entry_price": entry,
                    "cantidad": qty,
                    "inversion": pos_data.get("inversion", qty * entry),
                    "sl_pct": pos_data.get("sl_pct", PUMP_SL_INITIAL),
                    "tp_pct": pos_data.get("tp_pct", PUMP_TP_INITIAL),
                    "buy_time": pos_data.get("buy_time", time.time()),
                    "buy_order_id": str(pos_data.get("buy_order_id", "") or ""),

                    "current_tp_price": entry * (1 + float(pos_data.get("tp_pct", PUMP_TP_INITIAL)) / 100.0),
                    "last_tp_price": entry * (1 + float(pos_data.get("tp_pct", PUMP_TP_INITIAL)) / 100.0),
                    "tp_reached": False,
                    "sl_pause": pos_data.get("sl_pause", False)
                })
                self.update_section_display(sec, price)
                save_positions(self.sections)
                break

    def on_start_activation(self, dt=None):
        self.bot_start_time = time.time()
        threading.Thread(target=self.refresh_symbols_to_scan, daemon=True).start()
        self.status_lbl.text = "Bot activo - cargando mercados"
        self.scan_event = Clock.schedule_interval(self.auto_scan, AUTO_SCAN_INTERVAL)
        Clock.schedule_interval(self.periodic_refresh_symbols, 14400)

    def update_capital(self, dt=None):
        threading.Thread(target=self._read_capital, daemon=True).start()

    def _read_capital(self):
        balance = get_real_usdt_balance_total()
        Clock.schedule_once(lambda dt: self._set_capital_label(balance), 0)

    @mainthread
    def _set_capital_label(self, balance):
        self.capital_lbl.text = f"Capital libre: ${balance:,.2f} USDT"

    def update_all(self, dt):
        if self.background_mode:
            return
        self.date_lbl.text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.acum_real_lbl.text = f"${self.acum.get('acumulado_real', 0):,.2f} USDT"
        if not self.update_running:
            self.update_running = True
            threading.Thread(target=self._update_sections_async, daemon=True).start()

    def _update_sections_async(self):
        try:
            with engine_file_lock():
                if self.background_mode:
                    return
                active_sections = [s for s in self.sections if s.data["estado"] == "COMPRADO" and s.data["moneda"]]
                if not active_sections:
                    return
                symbols = [s.data["moneda"] for s in active_sections]
                prices = {sym: get_price_usdt(sym) for sym in symbols}
                for sec in active_sections:
                    price = prices.get(sec.data["moneda"])
                    if price:
                        self.check_tp_sl(sec, price)
                        self.update_section_display(sec, price)
                save_positions(self.sections)
        finally:
            self.update_running = False

    def _can_scan(self):
        active = sum(1 for s in self.sections if s.data["estado"] == "COMPRADO")
        return active < MAX_CONCURRENT_TRADES

    def auto_scan(self, dt):
        if self.background_mode or self.recovery_in_progress or not self._can_scan() or self.scanning:
            return
        self.scanning = True
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        try:
            self.scan_pumps(self.symbols_to_scan)
        finally:
            self.scanning = False

    def scan_pumps(self, symbols):
        if self.bot_start_time is None:
            return

        btc_state = str(btc_market_state() or "unknown").strip().lower()
        log(f"Estado BTC: {btc_state.upper()}")
        if btc_state == "bear":
            log("Compras automáticas bloqueadas: BTC está en bear")
            return
        if btc_state not in ("bull", "neutral"):
            log("Escaneo omitido: no se pudo validar el estado de BTC")
            return

        client = RealBinanceClientWrapper(self.keys["api"], self.keys["secret"])

        for symbol in list(symbols)[:max(1, int(MAX_SCAN_MARKETS))]:
            if self.background_mode:
                break
            # Release the shared lock between symbols so the position monitor can
            # run while the sequential scanner makes slow network requests.
            with engine_file_lock():
                if self.background_mode:
                    break
                active = {s.data["moneda"] for s in self.sections if s.data["moneda"]}
                current_time = time.time()
                if symbol in active or (symbol in self.sell_cooldown and
                        current_time - self.sell_cooldown[symbol] < SELL_COOLDOWN_MINUTES * 60):
                    continue
                if sum(1 for s in self.sections if s.data["estado"] == "INACTIVO") == 0:
                    break
                if not self.is_symbol_safe(symbol):
                    continue
                price = get_price_usdt(symbol)
                if not price or is_near_resistance(symbol, price):
                    continue
                if btc_state not in ("bull", "neutral"):
                    continue
                # 1.3.0: las compras automáticas explosivas están deshabilitadas.
                if detect_buy_signal(symbol, price):
                    self.buy_if_possible(symbol, client)

    def is_symbol_safe(self, symbol):
        now = time.time()
        cached = self.safe_symbol_cache.get(symbol)
        if cached and now - cached[0] < 3600:
            return cached[1]
        try:
            if not self.exchange_status_cache or now - self.exchange_status_cache_at >= 3600:
                response = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=8)
                response.raise_for_status()
                data = response.json()
                self.exchange_status_cache = {
                    item.get("symbol"): item.get("status") == "TRADING"
                    for item in data.get("symbols", [])
                }
                self.exchange_status_cache_at = now
            safe = self.exchange_status_cache.get(symbol, False)
            self.safe_symbol_cache[symbol] = (now, safe)
            if not safe:
                log(f"{symbol} no está habilitado para trading")
            return safe
        except Exception as e:
            log(f"No se pudo validar {symbol}; compra bloqueada: {e}")
            return False

    def _manual_buy_worker(self, symbol, investment, sl_pct, tp_pct):
        with engine_file_lock():
            if self.background_mode:
                log(f"Compra manual de {symbol} omitida durante el traspaso al servicio")
                return
            self.buy_if_possible(
                symbol, RealBinanceClientWrapper(self.keys.get("api", ""), self.keys.get("secret", "")),
                investment=investment, sl_pct=sl_pct, tp_pct=tp_pct,
                is_manual=True
            )

    def _manual_sell_worker(self, sec):
        with engine_file_lock():
            if self.background_mode:
                log("Venta manual cancelada durante el traspaso al servicio")
                return
            self.sell_section(sec, reason="Manual Confirmada")

    def manual_buy_popup(self, inst):
        content = BoxLayout(orientation="vertical", padding=10, spacing=10)
        symbol_in = TextInput(hint_text="Símbolo ej. BTCUSDT", font_size=FONT_SIZE_NORMAL)
        invest_in = TextInput(text=str(self.keys.get("invest_amount", MIN_PURCHASE_USDT)), hint_text="Inversión USDT", font_size=FONT_SIZE_NORMAL)
        sl_spin = Spinner(text=f"{PUMP_SL_INITIAL:.1f}%", values=[f"{x/10:.1f}%" for x in range(5, 301)])
        tp_spin = Spinner(text=f"{PUMP_TP_INITIAL:.1f}%", values=[f"{x/10:.1f}%" for x in range(10, 1001)])
        btn_buy = Button(text="COMPRAR NORMAL", background_color=COLOR_ACCENT_NEON)

        def do_buy(x):
            symbol = symbol_in.text.strip().upper()
            if not symbol.endswith("USDT"): symbol += "USDT"
            try:
                invest = float(invest_in.text) if invest_in.text else MIN_PURCHASE_USDT
                sl = float(sl_spin.text.replace("%", ""))
                tp = float(tp_spin.text.replace("%", ""))
                if not symbol or invest < MIN_TRADE_USDT:
                    raise ValueError(f"Símbolo o monto inválido (mínimo ${MIN_TRADE_USDT:g} USDT)")
            except ValueError as e:
                self.alert("Datos inválidos", str(e))
                return
            threading.Thread(
                target=self._manual_buy_worker,
                args=(symbol, invest, sl, tp), daemon=True
            ).start()
            popup.dismiss()

        btn_buy.bind(on_press=do_buy)
        content.add_widget(Label(text="Compra Manual"))
        content.add_widget(symbol_in)
        content.add_widget(invest_in)
        content.add_widget(Label(text="SL %"))
        content.add_widget(sl_spin)
        content.add_widget(Label(text="TP %"))
        content.add_widget(tp_spin)
        content.add_widget(btn_buy)
        popup = Popup(title="Compra Manual", content=content, size_hint=(0.9,0.7))
        popup.open()

    def refresh_symbols_to_scan(self):
        try:
            response = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
            data = response.json()
            self.symbols_to_scan = filter_market_symbols(data)
            log("🚀 PRO ULTRA: " + str(len(self.symbols_to_scan)) + " monedas activas")
            Clock.schedule_once(lambda dt: self._set_scan_status(), 0)
            return self.symbols_to_scan
        except Exception as e:
            log("Error cargando mercados: " + str(e))
            self.symbols_to_scan = self.static_symbols
            Clock.schedule_once(lambda dt: self._set_scan_status(), 0)
            return self.symbols_to_scan

    @mainthread
    def _set_scan_status(self):
        if self.bot_start_time is not None:
            self.status_lbl.text = f"Bot activo - {len(self.symbols_to_scan)} monedas (AGRESIVO)"

    def periodic_refresh_symbols(self, dt):
        threading.Thread(target=self.refresh_symbols_to_scan, daemon=True).start()


class StartupErrorScreen(BoxLayout):
    """Keep the app open and expose startup errors instead of silently closing."""
    def __init__(self, detail, **kwargs):
        super().__init__(orientation="vertical", padding=12, spacing=8, **kwargs)
        self.add_widget(Label(text="Magic Pump no pudo iniciar", size_hint_y=0.12,
                              color=COLOR_ACCENT_ERROR, font_size=FONT_SIZE_TITLE))
        self.add_widget(Label(text="Envía una captura de este detalle para corregirlo.",
                              size_hint_y=0.08, color=COLOR_TEXT_LIGHT, font_size=FONT_SIZE_NORMAL))
        view = ScrollView(size_hint=(1, 0.72))
        details = TextInput(text=detail, readonly=True, multiline=True,
                            font_size=FONT_SIZE_SMALL, size_hint_y=None,
                            background_color=(0.08, 0.08, 0.11, 1), foreground_color=COLOR_TEXT_LIGHT)
        details.bind(minimum_height=details.setter("height"))
        view.add_widget(details)
        self.add_widget(view)
        close_btn = Button(text="CERRAR", size_hint_y=0.08, background_color=COLOR_CARD_DARK)
        close_btn.bind(on_press=lambda *_: App.get_running_app().stop())
        self.add_widget(close_btn)

    def enter_background_mode(self):
        return None

    def leave_background_mode(self):
        return None


class MagicPumpApp(App):
    def build(self):
        try:
            return TradingInterface()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}\n\n" + traceback.format_exc()
            try:
                path = os.path.join(get_data_dir(), "magicpump_startup_error.log")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(detail)
            except Exception:
                pass
            return StartupErrorScreen(detail)

    def on_start(self):
        log("Magic Pump listo; la interfaz abre antes del escaneo")
        def check_binance_sdk():
            available = ensure_binance_available()
            log("Cliente Binance REST: disponible" if available else f"Cliente Binance REST no disponible: {BINANCE_IMPORT_ERROR}")
        threading.Thread(target=check_binance_sdk, daemon=True).start()
        if platform == "android":
            try:
                from android.permissions import request_permissions
                request_permissions(["android.permission.POST_NOTIFICATIONS"])
            except Exception as e:
                log(f"No se pudo solicitar permiso de notificaciones: {e}")

    def on_pause(self):
        if platform == "android" and self.root:
            self.root.enter_background_mode()
        return True

    def on_resume(self):
        if platform == "android" and self.root:
            self.root.leave_background_mode()
        return True

    def on_stop(self):
        try:
            root = self.root
            wake_lock = getattr(root, "wake_lock", None)
            if wake_lock and wake_lock.isHeld():
                wake_lock.release()
                log("Wake Lock liberado")
        except Exception as e:
            log(f"No se pudo liberar Wake Lock: {e}")

if __name__ == '__main__':
    MagicPumpApp().run()
