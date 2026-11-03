"""Binance Spot REST client using requests and HMAC-SHA256 only."""
import hashlib
import hmac
import requests
import threading
import time
import uuid
from decimal import Decimal
from urllib.parse import urlencode

class BinanceRESTError(Exception):
    """Error REST sanitizado; no contiene clave, secreto ni firma."""


class BinanceRESTClient:
    BASE_URL = "https://api.binance.com"
    RECV_WINDOW_MS = 10000

    def __init__(self, api_key, api_secret):
        # Elimina espacios accidentales o saltos al pegar credenciales; no las registra.
        self.api_key = "".join(str(api_key or "").split())
        self.api_secret = "".join(str(api_secret or "").split()).encode("utf-8")
        self.session = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "Accept": "application/json",
        })
        self.time_offset_ms = 0
        self.last_time_sync = 0.0
        self._lock = threading.RLock()

    @staticmethod
    def _format_number(value):
        number = Decimal(str(value))
        return format(number, "f")

    def _public_request(self, method, path, params=None):
        try:
            response = self.session.request(
                method, self.BASE_URL + path, params=params, timeout=(8, 20)
            )
        except requests.RequestException as exc:
            raise BinanceRESTError(f"Error de red Binance ({type(exc).__name__})") from None
        try:
            data = response.json()
        except Exception:
            data = None
        if response.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else "?"
            message = data.get("msg", "respuesta no válida") if isinstance(data, dict) else "respuesta no válida"
            raise BinanceRESTError(f"Binance HTTP {response.status_code}, código {code}: {str(message)[:180]}")
        if data is None:
            raise BinanceRESTError("Binance devolvió una respuesta JSON inválida")
        return data

    def _sync_server_time(self):
        data = self._public_request("GET", "/api/v3/time")
        server_ms = int(data["serverTime"])
        self.time_offset_ms = server_ms - int(time.time() * 1000)
        self.last_time_sync = time.monotonic()

    def ping(self):
        with self._lock:
            self._public_request("GET", "/api/v3/ping")
            self._sync_server_time()
        return True

    def _signed_request(self, method, path, params=None, retry_clock=True):
        with self._lock:
            if time.monotonic() - self.last_time_sync > 1800:
                self._sync_server_time()
            signed_params = dict(params or {})
            signed_params["recvWindow"] = self.RECV_WINDOW_MS
            signed_params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            query = urlencode(signed_params, doseq=True)
            signature = hmac.new(self.api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
            request_params = list(signed_params.items()) + [("signature", signature)]
            headers = {"X-MBX-APIKEY": self.api_key, "Accept": "application/json"}
            try:
                response = self.session.request(
                    method, self.BASE_URL + path, params=request_params,
                    headers=headers, timeout=(8, 20)
                )
            except requests.RequestException as exc:
                # No se reintenta una orden tras desconexión: Binance pudo aceptarla.
                client_order_id = signed_params.get("newClientOrderId", "")
                hint = f"; ID cliente {client_order_id}" if client_order_id else ""
                raise BinanceRESTError(f"Resultado de red ambiguo ({type(exc).__name__}){hint}; verifica Binance antes de repetir") from None
            try:
                data = response.json()
            except Exception:
                data = None
            if response.status_code >= 400:
                code = data.get("code") if isinstance(data, dict) else "?"
                message = data.get("msg", "respuesta no válida") if isinstance(data, dict) else "respuesta no válida"
                if code == -1021 and retry_clock:
                    self._sync_server_time()
                    return self._signed_request(method, path, params, retry_clock=False)
                raise BinanceRESTError(f"Binance HTTP {response.status_code}, código {code}: {str(message)[:180]}")
            if data is None:
                raise BinanceRESTError("Binance devolvió una respuesta JSON inválida")
            return data

    def create_order(self, symbol, side, type, quantity):
        params = {
            "symbol": symbol,
            "side": str(side).upper(),
            "type": str(type).upper(),
            "quantity": self._format_number(quantity),
            "newOrderRespType": "FULL",
            "newClientOrderId": "MP" + uuid.uuid4().hex[:32],
        }
        return self._signed_request("POST", "/api/v3/order", params)

    def get_symbol_info(self, symbol):
        data = self._public_request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        return symbols[0] if symbols else None

    def get_account(self):
        return self._signed_request("GET", "/api/v3/account")

    def get_asset_balance(self, asset):
        account = self.get_account()
        for balance in account.get("balances", []):
            if balance.get("asset") == asset:
                return balance
        return {"asset": asset, "free": "0", "locked": "0"}

    def get_order(self, symbol, orderId):
        return self._signed_request("GET", "/api/v3/order", {"symbol": symbol, "orderId": orderId})

    def cancel_order(self, symbol, orderId):
        return self._signed_request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": orderId})

    def get_my_trades(self, symbol, limit=100):
        return self._signed_request("GET", "/api/v3/myTrades", {"symbol": symbol, "limit": int(limit)})


