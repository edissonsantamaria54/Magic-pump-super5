"""Foreground trading service used only while Magic Pump is backgrounded.

The service shares the app-private data folder passed in PYTHON_SERVICE_ARGUMENT.
It reuses the exact strategy functions from main.py and owns the scan loop while
its activity is paused, avoiding a duplicate foreground scanner.
"""
import json
import math
import os
import sys
import time
import traceback
import threading

DATA_DIR = os.environ.get("PYTHON_SERVICE_ARGUMENT", "").strip()
if DATA_DIR:
    os.environ["MAGICPUMP_DATA_DIR"] = DATA_DIR
    os.makedirs(DATA_DIR, exist_ok=True)

# python-for-android puts the application root on sys.path; keep a fallback for
# local testing and make diagnostics useful if the import itself fails.
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

import main as app

try:
    from jnius import autoclass
    PythonService = autoclass('org.kivy.android.PythonService')
except Exception:
    PythonService = None

SERVICE_INTERVAL = 10.0
POSITION_MONITOR_INTERVAL = 5.0
SYMBOL_REFRESH_SECONDS = 4 * 60 * 60
MIN_TRADE_USDT = 10.0
MAX_POSITIONS = app.NUM_TRADING_SECTIONS


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class BackgroundBot:
    def __init__(self, data_dir):
        self.data_dir = data_dir or app.get_data_dir()
        os.environ["MAGICPUMP_DATA_DIR"] = self.data_dir
        # Use the same active editable strategy as the foreground app, and
        # notice atomic active-file replacement if the service outlives the UI.
        self.strategy_module = None
        self.refresh_strategy_settings(force_reload=True)
        self.positions_path = os.path.join(self.data_dir, app.POSITIONS_FILE)
        self.cooldown_path = os.path.join(self.data_dir, app.SELL_COOLDOWN_FILE)
        self.symbols = []
        self.symbols_updated = 0.0
        self.exchange_status = {}
        self.exchange_status_updated = 0.0
        self.client = None
        self.client_key = None
        self.last_api_warning = 0.0
        self.last_connection_warning = 0.0
        self.next_client_attempt = 0.0
        self.partial_wake_lock = None
        self.last_dust_reconcile = 0.0

    def log(self, message):
        app.log(f"[SERVICIO] {message}")

    def refresh_strategy_settings(self, force_reload=False):
        """Reload the active strategy when its atomically replaced file changes."""
        self.strategy_module = app.get_strategy_logic(force_reload=force_reload)
        self.service_interval = max(1.0, float(app.SERVICE_INTERVAL))
        self.position_monitor_interval = max(1.0, float(app.POSITION_MONITOR_INTERVAL))
        self.min_trade_usdt = max(1.0, float(app.MIN_TRADE_USDT))
        self.max_positions = max(1, int(app.MAX_CONCURRENT_TRADES))
        self.max_scan_markets = max(1, int(app.MAX_SCAN_MARKETS))

    def enabled(self):
        state = _read_json(os.path.join(self.data_dir, "bot_control.json"), {"enabled": True})
        return bool(state.get("enabled", True))

    def start_wake_lock(self):
        if not PythonService:
            return
        try:
            Activity = autoclass('android.app.Activity')
            PowerManager = autoclass('android.os.PowerManager')
            service = PythonService.mService
            manager = service.getSystemService(Activity.POWER_SERVICE)
            self.partial_wake_lock = manager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK, "MagicPump::BackgroundBot"
            )
            self.partial_wake_lock.acquire()
        except Exception as exc:
            self.log(f"No se pudo activar el bloqueo parcial de suspensión: {exc}")

    def stop_wake_lock(self):
        try:
            if self.partial_wake_lock and self.partial_wake_lock.isHeld():
                self.partial_wake_lock.release()
        except Exception:
            pass

    def api_client(self):
        keys = app.load_api_keys()
        key = (keys.get("api", ""), keys.get("secret", ""))
        if not key[0] or not key[1]:
            if time.time() - self.last_api_warning > 300:
                self.log("API no guardada en el almacenamiento de esta APK; escaneo sin órdenes privadas")
                self.last_api_warning = time.time()
            return None, keys
        if not app.ensure_binance_available():
            if time.time() - self.last_api_warning > 300:
                self.log(f"Cliente Binance REST no disponible: {app.BINANCE_IMPORT_ERROR}")
                self.last_api_warning = time.time()
            return None, keys
        if self.client is None or self.client_key != key:
            if time.time() < self.next_client_attempt:
                return None, keys
            candidate = app.RealBinanceClientWrapper(key[0], key[1])
            if not candidate.use_real:
                if time.time() - self.last_connection_warning > 300:
                    self.log("La conexión privada con Binance no está lista; no se enviarán órdenes")
                    self.last_connection_warning = time.time()
                self.client = None
                self.next_client_attempt = time.time() + 60.0
            else:
                self.next_client_attempt = 0.0
                self.client = candidate
                self.client_key = key
                self.log("Cliente REST inicializado; la primera consulta privada validará permisos (claves ocultas)")
        return self.client, keys

    def load_positions(self):
        data = _read_json(self.positions_path, [])
        if not isinstance(data, list):
            return []
        clean = []
        for pos in data:
            try:
                if pos.get("moneda") and float(pos.get("cantidad", 0)) > 0:
                    clean.append(pos)
            except Exception:
                continue
        return clean

    def save_positions(self, positions):
        _write_json(self.positions_path, positions)

    def load_cooldown(self):
        return _read_json(self.cooldown_path, {})

    def save_cooldown(self, cooldown):
        _write_json(self.cooldown_path, cooldown)

    def clear_stale_sell_flags(self):
        """Clear process-local in-flight markers after restart; balances still cap sales."""
        with app.engine_file_lock():
            positions = self.load_positions()
            changed = False
            for position in positions:
                if position.get("sell_pending"):
                    position["sell_pending"] = False
                    changed = True
            if changed:
                self.save_positions(positions)
                self.log("Marcadores de venta pendientes recuperados tras reinicio; se verificará saldo antes de vender")

    def refresh_symbols(self):
        try:
            response = app.requests.get(
                "https://api.binance.com/api/v3/ticker/24hr", timeout=15
            )
            response.raise_for_status()
            data = response.json()
            self.symbols = app.filter_market_symbols(data)
            self.symbols_updated = time.time()
            self.log(f"Lista actualizada: {len(self.symbols)} mercados (máximo {self.max_scan_markets})")
        except Exception as exc:
            self.log(f"No se pudo refrescar la lista de mercados: {exc}")
            if not self.symbols:
                self.symbols = [
                    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
                    "TONUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "NEARUSDT",
                    "SUIUSDT", "HBARUSDT", "APTUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "FETUSDT",
                    "WIFUSDT", "PEPEUSDT", "LTCUSDT", "ETCUSDT", "FILUSDT", "VETUSDT", "ALGOUSDT",
                    "KASUSDT", "STXUSDT", "ENAUSDT", "ONDOUSDT"
                ]

    def is_symbol_safe(self, symbol):
        now = time.time()
        if not self.exchange_status or now - self.exchange_status_updated > 3600:
            try:
                response = app.requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10)
                response.raise_for_status()
                self.exchange_status = {
                    item.get("symbol"): item.get("status") == "TRADING"
                    for item in response.json().get("symbols", [])
                }
                self.exchange_status_updated = now
            except Exception as exc:
                self.log(f"No se pudo validar exchangeInfo; compra omitida: {exc}")
                return False
        return bool(self.exchange_status.get(symbol, False))

    def adjusted_qty(self, client, symbol, desired_qty, is_sell=False):
        qty, _verified = app.adjusted_qty_with_status(client, symbol, desired_qty)
        return qty

    def prune_untradeable_positions(self, client):
        """Free local slots for confirmed dust; never sells or converts the asset."""
        positions = self.load_positions()
        kept = []
        changed = False
        for position in positions:
            symbol = position.get("moneda")
            try:
                if not symbol or not symbol.endswith("USDT"):
                    kept.append(position)
                    continue
                balance = client.get_asset_balance(symbol[:-4])
                if not isinstance(balance, dict):
                    kept.append(position)
                    continue
                free_qty = max(0.0, float(balance.get("free", 0) or 0))
                locked_qty = max(0.0, float(balance.get("locked", 0) or 0))
                epsilon = max(float(position.get("cantidad", 0) or 0) * 1e-8, 1e-12)
                if free_qty <= epsilon:
                    if locked_qty <= epsilon:
                        self.log(f"{symbol}: sin saldo para vender; se libera el espacio local, sin tocar Binance")
                        changed = True
                        continue
                    kept.append(position)
                    continue
                price = app.get_price_usdt(symbol)
                if not price:
                    kept.append(position)
                    continue
                tracked_qty = max(0.0, float(position.get("cantidad", 0) or 0))
                candidate = min(tracked_qty, free_qty)
                adjusted, verified = app.adjusted_qty_with_status(
                    client, symbol, candidate, price=price
                )
                if verified and adjusted is None and locked_qty <= epsilon:
                    self.log(f"{symbol}: residuo bajo los mínimos de venta; se libera el espacio local, el saldo queda en Binance")
                    changed = True
                    continue
                if verified and adjusted is not None and 0 < candidate < tracked_qty:
                    ratio = candidate / tracked_qty if tracked_qty > 0 else 0.0
                    position["cantidad"] = candidate
                    position["inversion"] = max(0.0, float(position.get("inversion", 0) or 0) * ratio)
                    changed = True
                kept.append(position)
            except Exception as exc:
                self.log(f"No se pudo reconciliar {symbol}: {exc}; se conserva")
                kept.append(position)
        if changed:
            self.save_positions(kept)

    def order_filled(self, client, symbol, order):
        # A market-order response may already contain complete fill accounting.
        # Keep it as a fallback if the subsequent status query briefly fails.
        status = order if isinstance(order, dict) else None
        try:
            time.sleep(1.0)
            queried = client.get_order(symbol, order["orderId"])
            if queried:
                status = queried
            if status and status.get("status") not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                client.cancel_order(symbol, order["orderId"])
                time.sleep(0.3)
                status = client.get_order(symbol, order["orderId"]) or status
            return status
        except Exception as exc:
            self.log(f"No se pudo verificar orden {symbol}; se conserva la respuesta disponible: {exc}")
            return status

    def buy(self, client, keys, symbol, price):
        positions = self.load_positions()
        if any(pos.get("moneda") == symbol for pos in positions) or len(positions) >= self.max_positions:
            return
        balance = client.client.get_asset_balance("USDT")
        free_usdt = float(balance.get("free", 0)) if balance else 0.0
        strategy = app.get_strategy_logic()
        if free_usdt < self.min_trade_usdt:
            return
        try:
            requested = float(keys.get("invest_amount", app.MIN_PURCHASE_USDT))
            sl_pct, tp_pct = app.initial_sl_tp()
            investment = float(app.calculate_buy_amount(free_usdt, requested, positions, sl_pct))
            if not math.isfinite(investment) or investment <= 0:
                raise ValueError("monto de compra inválido")
            investment = min(investment, free_usdt * 0.99)
        except Exception as exc:
            self.log(f"Compra bloqueada {symbol}: error en estrategia editable: {exc}")
            return
        if investment < self.min_trade_usdt or not (0.1 <= float(sl_pct) <= 30) or not (0.1 <= float(tp_pct) <= 100):
            return
        qty = self.adjusted_qty(client, symbol, investment / price)
        if not qty:
            self.log(f"Compra omitida; monto o cantidad bajo los mínimos de {symbol}")
            return
        order = client.create_market_buy_order(symbol, qty)
        if not order:
            return
        status = self.order_filled(client, symbol, order)
        qty_exec = float((status or {}).get("executedQty", 0) or 0)
        if qty_exec <= 0:
            self.log(f"Compra no ejecutada {symbol}")
            return
        quote = float((status or {}).get("cummulativeQuoteQty", 0) or 0)
        entry = quote / qty_exec if quote > 0 else price
        fills = (status or {}).get("fills") or order.get("fills") or []
        if fills:
            total_qty = sum(float(fill.get("qty", 0)) for fill in fills)
            total_quote = sum(float(fill.get("qty", 0)) * float(fill.get("price", 0)) for fill in fills)
            if total_qty > 0 and total_quote > 0:
                entry = total_quote / total_qty
                quote = total_quote
        position = {
            "moneda": symbol,
            "entry_price": entry,
            "cantidad": qty_exec,
            "inversion": quote if quote > 0 else qty_exec * entry,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "buy_time": time.time(),
            "buy_order_id": str((status or {}).get("orderId", order.get("orderId", "")) or ""),
            "current_tp_price": entry * (1 + tp_pct / 100.0),
            "last_tp_price": entry * (1 + tp_pct / 100.0),
            "tp_reached": False,
            "sl_pause": False,
            "sell_pending": False,
        }
        positions.append(position)
        self.save_positions(positions)
        self.log(f"COMPRA {symbol} | cantidad {qty_exec:.8f} | inversión aprox. ${position['inversion']:.2f}")

    def sell(self, client, position, price, reason):
        positions = self.load_positions()
        current = next((item for item in positions if item.get("moneda") == position.get("moneda")), None)
        if current is None or current.get("sell_pending"):
            return
        symbol = current["moneda"]
        current["sell_pending"] = True
        self.save_positions(positions)
        try:
            asset = symbol[:-4] if symbol.endswith("USDT") else symbol
            balance = client.get_asset_balance(asset)
            free_qty = float(balance.get("free", 0)) if balance else 0.0
            qty_requested = min(free_qty, float(current.get("cantidad", 0)))
            qty = self.adjusted_qty(client, symbol, qty_requested, is_sell=True)
            if not qty:
                residual_state, residual_qty, _value = app.inspect_residual_sellability(
                    client, symbol, float(current.get("cantidad", 0) or 0)
                )
                if residual_state == "dust":
                    positions = [item for item in positions if item.get("moneda") != symbol]
                    self.save_positions(positions)
                    cooldown = self.load_cooldown()
                    cooldown[symbol] = time.time()
                    self.save_cooldown(cooldown)
                    self.log(f"{symbol}: saldo bajo mínimos de venta; espacio liberado, residuo sin tocar en Binance ({residual_qty:.8f})")
                else:
                    current["sell_pending"] = False
                    self.save_positions(positions)
                    self.log(f"Venta omitida; no pude confirmar si la cantidad está bajo los mínimos de {symbol}; se conserva el seguimiento")
                return
            order = client.create_market_sell_order(symbol, qty)
            if not order:
                residual_state, residual_qty, _value = app.inspect_residual_sellability(
                    client, symbol, qty_requested
                )
                if residual_state == "dust":
                    positions = [item for item in positions if item.get("moneda") != symbol]
                    self.save_positions(positions)
                    cooldown = self.load_cooldown()
                    cooldown[symbol] = time.time()
                    self.save_cooldown(cooldown)
                    self.log(f"{symbol}: Binance rechazó el mínimo; espacio liberado, residuo no vendido {residual_qty:.8f}")
                else:
                    current["sell_pending"] = False
                    self.save_positions(positions)
                return
            status = self.order_filled(client, symbol, order)
            qty_exec = float((status or {}).get("executedQty", 0) or 0)
            if qty_exec <= 0:
                residual_state, residual_qty, _value = app.inspect_residual_sellability(
                    client, symbol, qty_requested
                )
                if residual_state == "dust":
                    positions = [item for item in positions if item.get("moneda") != symbol]
                    self.save_positions(positions)
                    cooldown = self.load_cooldown()
                    cooldown[symbol] = time.time()
                    self.save_cooldown(cooldown)
                    self.log(f"{symbol}: orden sin ejecución y residuo no vendible {residual_qty:.8f}; espacio liberado")
                else:
                    current["sell_pending"] = False
                    self.save_positions(positions)
                    self.log(f"Venta no ejecutada {symbol}; se conserva seguimiento")
                return
            quote = float((status or {}).get("cummulativeQuoteQty", 0) or 0)
            sell_price = quote / qty_exec if quote > 0 else price
            proceeds = quote if quote > 0 else sell_price * qty_exec
            old_qty = float(current.get("cantidad", 0))
            old_cost = float(current.get("inversion", 0))
            cost = old_cost * min(1.0, qty_exec / old_qty) if old_qty > 0 else 0.0
            realized = proceeds - cost
            accum_path = os.path.join(self.data_dir, "acumulado_mes.json")
            accum = _read_json(accum_path, {"acumulado_real": 0.0})
            accum["acumulado_real"] = float(accum.get("acumulado_real", 0)) + realized
            _write_json(accum_path, accum)
            remaining = max(0.0, old_qty - qty_exec)
            epsilon = max(old_qty * 1e-8, 1e-12)
            residual_state = "complete"
            residual_qty = remaining
            residual_value = None
            if remaining > epsilon:
                residual_state, residual_qty, residual_value = app.inspect_residual_sellability(
                    client, symbol, remaining
                )
            if remaining > epsilon and residual_state != "dust":
                current["cantidad"] = residual_qty if residual_state == "sellable" else remaining
                current["inversion"] = max(0.0, old_cost - cost)
                current["sell_pending"] = False
                self.save_positions(positions)
                if residual_state == "sellable":
                    self.log(f"VENTA PARCIAL {symbol} | restante vendible {residual_qty:.8f} | G/P aprox. ${realized:+.2f} | {reason}")
                else:
                    self.log(f"VENTA PARCIAL {symbol} | saldo restante no confirmado; se conserva seguimiento | G/P aprox. ${realized:+.2f} | {reason}")
            else:
                positions = [item for item in positions if item.get("moneda") != symbol]
                self.save_positions(positions)
                cooldown = self.load_cooldown()
                cooldown[symbol] = time.time()
                self.save_cooldown(cooldown)
                if residual_state == "dust" and residual_qty > 0:
                    self.log(f"VENTA {symbol} | residuo no vendible {residual_qty:.8f} sigue en Binance; espacio liberado | G/P aprox. ${realized:+.2f} | {reason}")
                else:
                    self.log(f"VENTA {symbol} | G/P aprox. ${realized:+.2f} | {reason}")
        except Exception as exc:
            current["sell_pending"] = False
            self.save_positions(positions)
            self.log(f"Error vendiendo {symbol}: {exc}")

    def load_cooldown(self):
        return _read_json(self.cooldown_path, {})

    def save_cooldown(self, cooldown):
        _write_json(self.cooldown_path, cooldown)

    def monitor_positions(self, client):
        positions = self.load_positions()
        changed = False
        for position in list(positions):
            symbol = position.get("moneda")
            if not symbol:
                continue
            price = app.get_price_usdt(symbol)
            if not price:
                continue
            reason = app.manage_strategy_position(position, price, time.time())
            changed = True
            if reason:
                self.save_positions(positions)
                self.sell(client, position, price, reason)
                positions = self.load_positions()
                changed = False
        if changed:
            self.save_positions(positions)

    def scan_once(self, client, keys):
        self.refresh_strategy_settings()
        if time.time() - self.symbols_updated >= SYMBOL_REFRESH_SECONDS or not self.symbols:
            self.refresh_symbols()
        btc_state = str(app.btc_market_state() or "unknown").strip().lower()
        self.log(f"Estado BTC: {btc_state.upper()}")
        if btc_state == "bear":
            self.log("Compras automáticas bloqueadas: BTC está en bear")
            return
        if btc_state not in ("bull", "neutral"):
            self.log("Escaneo omitido: no se pudo validar el estado de BTC")
            return
        for symbol in list(self.symbols[:self.max_scan_markets]):
            if not self.enabled():
                return
            # Each symbol is serialized with the UI/service engine, but the lock is
            # released between markets so the position monitor can run during long scans.
            try:
                with app.engine_file_lock():
                    if not self.enabled():
                        return
                    positions = self.load_positions()
                    active = {pos.get("moneda") for pos in positions}
                    if symbol in active or len(positions) >= self.max_positions:
                        continue
                    cooldown = self.load_cooldown()
                    cooldown_at = float(cooldown.get(symbol, 0) or 0)
                    if cooldown_at and time.time() - cooldown_at < app.SELL_COOLDOWN_MINUTES * 60:
                        continue
                    if not self.is_symbol_safe(symbol):
                        continue
                    price = app.get_price_usdt(symbol)
                    if not price or app.is_near_resistance(symbol, price):
                        continue
                    if btc_state not in ("bull", "neutral"):
                        continue
                    # 1.3.0: el servicio de fondo también opera solo en modo normal.
                    if app.detect_buy_signal(symbol, price):
                        self.buy(client, keys, symbol, price)
            except Exception as exc:
                self.log(f"Error analizando {symbol}; se continúa con el siguiente mercado: {exc}")

    def monitor_loop(self, stop_event):
        while not stop_event.is_set() and self.enabled():
            self.refresh_strategy_settings()
            try:
                client, _keys = self.api_client()
                if client:
                    with app.engine_file_lock():
                        if self.enabled():
                            self.monitor_positions(client)
            except Exception as exc:
                self.log(f"Error vigilando posiciones: {exc}")
            stop_event.wait(self.position_monitor_interval)

    def run(self):
        self.start_wake_lock()
        if PythonService:
            try:
                PythonService.mService.setAutoRestartService(True)
            except Exception as exc:
                self.log(f"No se pudo habilitar el reinicio del servicio: {exc}")
        try:
            self.clear_stale_sell_flags()
        except Exception as exc:
            self.log(f"No se pudieron recuperar marcadores pendientes al iniciar: {exc}")
        stop_event = threading.Event()
        monitor = threading.Thread(target=self.monitor_loop, args=(stop_event,), daemon=True)
        monitor.start()
        self.log("Servicio persistente iniciado; escaneo secuencial y vigilancia periódica de posiciones")
        try:
            while self.enabled():
                self.refresh_strategy_settings()
                started = time.time()
                try:
                    client, keys = self.api_client()
                    if client:
                        if time.time() - self.last_dust_reconcile >= 300.0:
                            with app.engine_file_lock():
                                if self.enabled():
                                    self.prune_untradeable_positions(client)
                            self.last_dust_reconcile = time.time()
                        self.scan_once(client, keys)
                    elif time.time() - self.symbols_updated >= SYMBOL_REFRESH_SECONDS or not self.symbols:
                        self.refresh_symbols()
                except Exception:
                    self.log("Fallo en una vuelta del servicio; se reintentará:\n" + traceback.format_exc())
                remaining = max(0.0, self.service_interval - (time.time() - started))
                deadline = time.time() + remaining
                while time.time() < deadline and self.enabled():
                    time.sleep(min(1.0, deadline - time.time()))
        finally:
            stop_event.set()
            monitor.join(timeout=3.0)
            self.stop_wake_lock()
            self.log("Servicio persistente detenido")
            if PythonService:
                try:
                    PythonService.mService.stopSelf()
                except Exception:
                    pass


def main():
    data_dir = DATA_DIR or os.path.join(os.getcwd(), "magicpump_data")
    os.makedirs(data_dir, exist_ok=True)
    app.log("Servicio Android recibió directorio privado de la app")
    BackgroundBot(data_dir).run()


if __name__ == "__main__":
    main()
