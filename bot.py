#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ElGafas Telegram Futures Bot (script único apto para Android/Termux).

Default safety posture: Binance Futures Demo/Testnet. Live trading requires BOTH
BINANCE_TESTNET=false and ENABLE_LIVE_TRADING=true, plus live API credentials.
This bot supports One-way position mode only and one strategy operation at a time.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# --- Calculadora integrada: no necesita un archivo calculator.py aparte ---
def D(value: Any) -> Decimal:
    return Decimal(str(value))


def decstr(value: Decimal | str | float | int) -> str:
    d = D(value)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def decimal_places(text: str) -> int:
    text = str(text).strip().replace(",", "")
    if "." in text:
        return min(12, len(text.split(".", 1)[1]))
    return 2


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def round_step(value: Decimal, step: Decimal, mode: str) -> Decimal:
    if mode == "floor":
        return floor_step(value, step)
    if mode == "ceil":
        return ceil_step(value, step)
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def js_round(value: Decimal, decimals: int) -> Decimal:
    """Positive-number equivalent of JS Math.round, used by the HTML calculator."""
    quantum = Decimal("1").scaleb(-decimals)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def calc_stop(side: str, total_cost: Decimal, total_qty: Decimal, loss: Decimal) -> Decimal:
    if total_qty <= 0:
        raise ValueError("Cantidad total inválida.")
    return (total_cost - loss) / total_qty if side == "LONG" else (total_cost + loss) / total_qty


def calculator_plan(side: str, entry_price: Decimal, base_qty: Decimal,
                    entry_price_text: str, settings: dict[str, str],
                    tick_size: Decimal, step_size: Decimal) -> dict[str, Any]:
    """Port calculator logic, then applies exchange tick/lot rules for valid order prices and quantities."""
    side = side.upper()
    precision = decimal_places(entry_price_text)
    rebuy_pct = D(settings["rebuy_pct"]) / Decimal("100")
    factor_qty = Decimal("1") + D(settings["qty_increase_pct"]) / Decimal("100")
    loss = D(settings["max_loss_usdt"])
    if side not in {"LONG", "SHORT"} or entry_price <= 0 or base_qty <= 0:
        raise ValueError("Dirección, precio o cantidad inválidos.")
    if rebuy_pct <= 0 or (side == "LONG" and rebuy_pct >= 1):
        raise ValueError("Porcentaje de recompra inválido para esta dirección.")
    if factor_qty <= 0 or loss <= 0:
        raise ValueError("Parámetros de monedas o stop loss inválidos.")

    total_qty = base_qty
    total_cost = entry_price * base_qty
    last_qty = base_qty
    prev_price = entry_price
    levels: list[dict[str, Decimal]] = []
    stop_raw: Decimal | None = None
    adjusted = False

    for level_no in range(1, 9):
        raw = prev_price * (Decimal("1") - rebuy_pct if side == "LONG" else Decimal("1") + rebuy_pct)
        price_calc = js_round(raw, precision)
        # Binance tick compliance: don't make LONG buys more expensive or SHORT sells cheaper.
        price = floor_step(price_calc, tick_size) if side == "LONG" else ceil_step(price_calc, tick_size)
        if price != price_calc:
            adjusted = True
        qty_calc = js_round(last_qty * factor_qty, 3)
        qty = floor_step(qty_calc, step_size)
        if qty != qty_calc:
            adjusted = True
        if price <= 0 or qty <= 0:
            raise ValueError(f"El nivel {level_no} queda en cero después de aplicar los límites del contrato.")

        current_stop = calc_stop(side, total_cost, total_qty, loss)
        crosses = price <= current_stop if side == "LONG" else price >= current_stop
        if crosses:
            stop_raw = current_stop
            break

        notional = price * qty
        levels.append({"n": D(level_no), "price": price, "qty": qty, "usdt": notional})
        total_qty += qty
        total_cost += notional
        last_qty = qty
        prev_price = price

    if stop_raw is None:
        stop_raw = calc_stop(side, total_cost, total_qty, loss)

    # Exchange-valid stop on safer side of the exact formula.
    stop_exchange = ceil_step(stop_raw, tick_size) if side == "LONG" else floor_step(stop_raw, tick_size)
    if stop_exchange != stop_raw:
        adjusted = True
    stop_display = js_round(stop_raw, precision)
    pct_stop = (stop_display / entry_price - Decimal("1")) * Decimal("100")
    if stop_exchange <= 0:
        raise ValueError("El stop calculado es cero o negativo; no se puede enviar una orden de protección válida.")
    return {
        "side": side, "entry_price": entry_price, "base_qty": base_qty,
        "precision": precision, "levels": levels, "stop_raw": stop_raw,
        "stop_exchange": stop_exchange, "stop_display": stop_display,
        "pct_stop": pct_stop, "total_qty": total_qty, "total_cost": total_cost,
        "loss": loss, "adjusted_for_exchange": adjusted,
    }


SCRIPT_DIR = Path(__file__).resolve().parent
# Load the .env beside bot.py regardless of the shell/boot script working directory.
load_dotenv(dotenv_path=SCRIPT_DIR / ".env", override=False)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("elgafas-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_TELEGRAM_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").strip().lower() in {"1", "true", "yes", "si"}
ENABLE_LIVE_TRADING = os.getenv("ENABLE_LIVE_TRADING", "false").strip().lower() in {"1", "true", "yes", "si"}
FAST_LIMIT_OFFSET = Decimal(os.getenv("FAST_LIMIT_OFFSET", "0.002"))
MONITOR_INTERVAL = max(2.0, float(os.getenv("MONITOR_INTERVAL_SECONDS", "3")))
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "elgafas_bot_state.json")).expanduser()
# Relative state paths are anchored beside bot.py, so Termux:Boot and manual launches
# always resume from the same file even if their current directories differ.
if not STATE_FILE.is_absolute():
    STATE_FILE = SCRIPT_DIR / STATE_FILE
STATE_BACKUP_FILE = STATE_FILE.with_name(STATE_FILE.name + ".bak")
RECV_WINDOW = 5000

if not BINANCE_TESTNET and not ENABLE_LIVE_TRADING:
    # Do not allow silently switching to production by changing only one flag.
    BASE_URL = "https://demo-fapi.binance.com"
    ENV_NAME = "DEMO"
else:
    BASE_URL = "https://demo-fapi.binance.com" if BINANCE_TESTNET else "https://fapi.binance.com"
    ENV_NAME = "TESTNET" if BINANCE_TESTNET else "LIVE"

if not BINANCE_TESTNET and ENABLE_LIVE_TRADING:
    log.warning("LIVE TRADING IS ENABLED. Real orders can be sent to Binance Futures.")

DEFAULT_SETTINGS: dict[str, str] = {
    "rebuy_pct": "1",
    "qty_increase_pct": "40",
    "max_loss_usdt": "10",
    "trailing_activation_pct": "1",
    "trailing_callback_pct": "0.5",
}

STATE_LOCK = threading.RLock()
TRADE_LOCK: asyncio.Lock | None = None


def new_client_id(prefix: str) -> str:
    """Return a Binance-compatible unique client id (letters/numbers only)."""
    return (prefix + uuid.uuid4().hex)[:36]


def _empty_state() -> dict[str, Any]:
    return {"settings": dict(DEFAULT_SETTINGS), "active_trade": None}


def _parse_state_file(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("El estado raíz no es un objeto JSON.")
    settings = obj.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("Los parámetros guardados no son un objeto.")
    active_trade = obj.get("active_trade")
    if active_trade is not None and not isinstance(active_trade, dict):
        raise ValueError("La operación guardada no es un objeto válido.")
    obj["settings"] = dict(settings)
    for key, default in DEFAULT_SETTINGS.items():
        obj["settings"].setdefault(key, default)
    obj["active_trade"] = active_trade
    return obj


def _atomic_copy(source: Path, destination: Path) -> None:
    destination_tmp = destination.with_name(destination.name + ".tmp")
    with source.open("rb") as src, destination_tmp.open("wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(destination_tmp, destination)


def load_state() -> dict[str, Any]:
    """Load durable strategy state; recover from the last known-good backup if needed."""
    global STATE_RECOVERED_FROM_BACKUP
    STATE_RECOVERED_FROM_BACKUP = False
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (STATE_FILE, STATE_BACKUP_FILE):
        try:
            if not candidate.exists():
                continue
            obj = _parse_state_file(candidate)
            if candidate == STATE_BACKUP_FILE:
                STATE_RECOVERED_FROM_BACKUP = True
                log.error("El estado principal está ausente/dañado. Recuperé la operación desde %s", candidate)
                try:
                    _atomic_copy(candidate, STATE_FILE)
                except Exception:
                    log.exception("Recuperé el estado en memoria, pero no pude restaurar el archivo principal.")
            return obj
        except Exception:
            log.exception("No pude leer el archivo de estado %s", candidate)
    log.warning("No hay un estado persistido válido. Se iniciarán parámetros predeterminados y sin operación activa.")
    return _empty_state()


STATE_RECOVERED_FROM_BACKUP = False
STATE = load_state()


def save_state() -> None:
    """Durable atomic save plus a last-known-good backup, resilient to abrupt shutdowns."""
    with STATE_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        try:
            # Preserve the previous primary state only if it is valid JSON/state. Never
            # overwrite a good backup with a corrupted primary file.
            if STATE_FILE.exists():
                try:
                    _parse_state_file(STATE_FILE)
                    _atomic_copy(STATE_FILE, STATE_BACKUP_FILE)
                except Exception:
                    log.warning("No actualizaré el backup desde un estado principal inválido.")
            content = json.dumps(STATE, ensure_ascii=False, indent=2)
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, STATE_FILE)
            # Sync the directory entry where supported, so the rename survives a sudden shutdown.
            try:
                dir_fd = os.open(str(STATE_FILE.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except (OSError, AttributeError):
                pass
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


class BinanceAPIError(RuntimeError):
    def __init__(self, message: str, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


class BinanceFutures:
    def __init__(self) -> None:
        self.base_url = BASE_URL
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.session = requests.Session()
        self._request_lock = threading.RLock()
        self.time_offset_ms = 0
        self._exchange_info: dict[str, Any] | None = None
        self._exchange_info_at = 0.0

    def _require_keys(self) -> None:
        if not self.api_key or not self.api_secret:
            raise BinanceAPIError(
                "Faltan BINANCE_API_KEY o BINANCE_API_SECRET en el archivo .env. "
                "Usa claves de Futures Demo/Testnet para las primeras pruebas."
            )

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 signed: bool = False, retry_time: bool = True) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {}
        if signed:
            self._require_keys()
            headers["X-MBX-APIKEY"] = self.api_key
            params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            params["recvWindow"] = RECV_WINDOW
            query = urlencode(params, doseq=True)
            signature = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            url = f"{self.base_url}{path}?{query}&signature={signature}"
            with self._request_lock:
                response = self.session.request(method, url, headers=headers, timeout=12)
        else:
            url = f"{self.base_url}{path}"
            with self._request_lock:
                response = self.session.request(method, url, params=params, headers=headers, timeout=12)
        try:
            payload = response.json()
        except Exception:
            payload = {"msg": response.text[:500]}
        if response.status_code >= 400 or (isinstance(payload, dict) and "code" in payload and int(payload.get("code", 0)) < 0):
            code = payload.get("code") if isinstance(payload, dict) else response.status_code
            msg = payload.get("msg", response.text) if isinstance(payload, dict) else response.text
            if signed and retry_time and str(code) == "-1021":
                self.sync_time()
                return self._request(method, path, {k: v for k, v in params.items() if k not in {"timestamp", "recvWindow"}}, signed=True, retry_time=False)
            raise BinanceAPIError(f"Binance ({code}): {msg}", ambiguous=response.status_code >= 500)
        return payload

    def sync_time(self) -> None:
        result = self._request("GET", "/fapi/v1/time", signed=False)
        self.time_offset_ms = int(result["serverTime"]) - int(time.time() * 1000)

    def public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params, signed=False)

    def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params, signed=True)

    def exchange_info(self, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._exchange_info is None or time.time() - self._exchange_info_at > 3600:
            self._exchange_info = self.public("/fapi/v1/exchangeInfo")
            self._exchange_info_at = time.time()
        return self._exchange_info

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper().strip()
        for item in self.exchange_info().get("symbols", []):
            if item.get("symbol") == symbol:
                if item.get("status") != "TRADING":
                    raise BinanceAPIError(f"{symbol} no está en estado TRADING.")
                if item.get("quoteAsset") != "USDT" or item.get("contractType") != "PERPETUAL":
                    raise BinanceAPIError(f"{symbol} no es un perpetuo USDT-M admitido por este bot.")
                filters = {f["filterType"]: f for f in item.get("filters", [])}
                price_filter = filters.get("PRICE_FILTER", {})
                lot = filters.get("LOT_SIZE", {})
                market_lot = filters.get("MARKET_LOT_SIZE", lot)
                notional = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
                return {
                    "raw": item,
                    "tick_size": D(price_filter.get("tickSize", "0.01")),
                    "min_price": D(price_filter.get("minPrice", "0")),
                    "max_price": D(price_filter.get("maxPrice", "1E50")),
                    "step_size": D(lot.get("stepSize", "0.001")),
                    "min_qty": D(lot.get("minQty", "0")),
                    "max_qty": D(lot.get("maxQty", "1E50")),
                    "market_step_size": D(market_lot.get("stepSize", lot.get("stepSize", "0.001"))),
                    "market_min_qty": D(market_lot.get("minQty", lot.get("minQty", "0"))),
                    "market_max_qty": D(market_lot.get("maxQty", lot.get("maxQty", "1E50"))),
                    "min_notional": D(notional.get("notional", notional.get("minNotional", "0"))),
                }
        raise BinanceAPIError(f"No encontré el contrato {symbol} en Binance Futures.")

    def mark_price(self, symbol: str) -> Decimal:
        data = self.public("/fapi/v1/premiumIndex", {"symbol": symbol})
        return D(data["markPrice"])

    def dual_side_mode(self) -> bool:
        return bool(self.signed("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition", False))

    def position(self, symbol: str) -> dict[str, Any] | None:
        data = self.signed("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(data, dict):
            data = [data]
        for p in data:
            if p.get("symbol") == symbol and p.get("positionSide", "BOTH") == "BOTH":
                return p
        return None

    def order(self, symbol: str, order_id: int | str | None = None,
              client_order_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id not in (None, ""):
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise BinanceAPIError("No hay orderId ni clientOrderId para consultar la orden.")
        return self.signed("GET", "/fapi/v1/order", params)

    def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.signed("POST", "/fapi/v1/order", params)

    def cancel_order(self, symbol: str, order_id: int | str) -> Any:
        return self.signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        result = self.signed("GET", "/fapi/v1/openOrders", {"symbol": symbol})
        return result if isinstance(result, list) else []

    def cancel_all_normal_orders(self, symbol: str) -> Any:
        return self.signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def place_algo(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.signed("POST", "/fapi/v1/algoOrder", params)

    def query_algo(self, symbol: str, algo_id: int | str) -> dict[str, Any]:
        return self.signed("GET", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})

    def cancel_algo(self, symbol: str, algo_id: int | str) -> Any:
        return self.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})

    def open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        result = self.signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        return result if isinstance(result, list) else []

    def close_market(self, symbol: str, side: str, quantity: Decimal, step_size: Decimal) -> dict[str, Any] | None:
        qty = floor_step(quantity, step_size)
        if qty <= 0:
            return None
        return self.place_order({
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": decstr(qty),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
        })


BINANCE = BinanceFutures()


def fnum(value: Decimal | str | float | int, maximum: int = 8) -> str:
    d = D(value)
    s = format(d, f",.{maximum}f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def plan_text(symbol: str, side: str, price: Decimal, qty: Decimal, plan: dict[str, Any],
              settings: dict[str, str], mode: str, env_name: str) -> str:
    lines = [
        f"PREVISUALIZACIÓN {side} · {symbol}",
        f"Entorno: {env_name}",
        f"Modo de entrada: {mode}",
        f"Entrada usada para estimar: {fnum(price, 12)} USDT",
        f"Cantidad inicial: {fnum(qty, 8)}",
        f"Recompra: {settings['rebuy_pct']}% · Aumento de monedas: {settings['qty_increase_pct']}%",
        f"Pérdida máxima configurada: {settings['max_loss_usdt']} USDT",
        "",
        "# | PRECIO | MONEDAS | USDT",
    ]
    for lv in plan["levels"]:
        lines.append(f"{int(lv['n'])} | {fnum(lv['price'], 12)} | {fnum(lv['qty'], 8)} | {fnum(lv['usdt'], 2)}")
    lines += [
        f"SL ({fnum(plan['pct_stop'], 2)}%): {fnum(plan['stop_exchange'], 12)}",
        f"Monedas previstas acumuladas: {fnum(plan['total_qty'], 8)}",
        f"USDT previsto acumulado: {fnum(plan['total_cost'], 2)}",
        "",
    ]
    if plan.get("adjusted_for_exchange"):
        lines.append("⚠️ Algún precio se ajustó al tick permitido por Binance.")
    if mode == "MERCADO":
        lines.append("⚠️ Es una estimación. Tras la ejecución se recalcularán los niveles con el precio y cantidad reales.")
    elif mode == "LÍMITE RÁPIDO ±0.002":
        lines.append("⚠️ La orden inicial se enviará cerca del precio de mercado, no al precio de referencia escrito. Tras el fill se recalcula la escalera.")
    lines += [
        "",
        "El bot colocará primero la protección SL y luego las recompras. Las cantidades/precios deben cumplir los filtros del contrato.",
        "Confirma solo si has revisado el símbolo, dirección, cantidades y riesgo.",
    ]
    return "\n".join(lines)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Configuración / Parámetros", callback_data="config")],
        [InlineKeyboardButton("🟢 LONG", callback_data="long"), InlineKeyboardButton("🔴 SHORT", callback_data="short")],
        [InlineKeyboardButton("📊 Estado", callback_data="status"), InlineKeyboardButton("❌ Cancelar borrador", callback_data="cancel_draft")],
    ])


def config_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("% Recompra / venta", callback_data="set:rebuy_pct")],
        [InlineKeyboardButton("% Aumento de monedas", callback_data="set:qty_increase_pct")],
        [InlineKeyboardButton("Pérdida máxima (USDT)", callback_data="set:max_loss_usdt")],
        [InlineKeyboardButton("Activación trailing (%)", callback_data="set:trailing_activation_pct")],
        [InlineKeyboardButton("Retroceso trailing (%)", callback_data="set:trailing_callback_pct")],
        [InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")],
    ])


def auth_ok(update: Update) -> bool:
    if not ALLOWED_TELEGRAM_USER_ID:
        return False
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and chat.type == "private" and str(user.id) == ALLOWED_TELEGRAM_USER_ID)


def settings_snapshot() -> dict[str, str]:
    with STATE_LOCK:
        return dict(STATE["settings"])


def active_trade_snapshot() -> dict[str, Any] | None:
    with STATE_LOCK:
        return json.loads(json.dumps(STATE.get("active_trade"))) if STATE.get("active_trade") else None


def set_active_trade(trade: dict[str, Any] | None) -> None:
    with STATE_LOCK:
        # Store a detached JSON-safe snapshot so local updates cannot mutate saved state invisibly.
        STATE["active_trade"] = json.loads(json.dumps(trade)) if trade is not None else None
        save_state()


def set_setting(key: str, value: str) -> None:
    with STATE_LOCK:
        STATE["settings"][key] = value
        save_state()


def validate_numeric_setting(key: str, value_text: str) -> str:
    try:
        value = D(value_text.replace(",", "."))
    except Exception as exc:
        raise ValueError("Envía un número válido; por ejemplo: 1.5") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("El valor debe ser un número positivo.")
    if key in {"rebuy_pct", "trailing_activation_pct"} and value > 100:
        raise ValueError("Ese porcentaje no puede superar 100%.")
    if key == "qty_increase_pct" and value > 10000:
        raise ValueError("El aumento de monedas no puede superar 10000%.")
    if key == "trailing_callback_pct" and not (Decimal("0.1") <= value <= Decimal("20")):
        raise ValueError("Binance permite callbackRate entre 0.1% y 20% en este endpoint.")
    return decstr(value)


def require_configured_api() -> None:
    BINANCE._require_keys()
    if not BINANCE_TESTNET and not ENABLE_LIVE_TRADING:
        raise BinanceAPIError("Trading real está bloqueado. Configura BINANCE_TESTNET=false y ENABLE_LIVE_TRADING=true de forma consciente.")


def check_one_way_and_clean_symbol(symbol: str) -> dict[str, Any]:
    require_configured_api()
    info = BINANCE.symbol_info(symbol)
    if BINANCE.dual_side_mode():
        raise BinanceAPIError("Tu cuenta está en modo Hedge. Este bot v1 exige One-way Mode; cambia el modo en Binance o no ejecutes la orden.")
    pos = BINANCE.position(symbol)
    if pos and D(pos.get("positionAmt", "0")) != 0:
        raise BinanceAPIError(f"Ya hay una posición abierta en {symbol}. Para evitar mezclar estrategias, el bot no abrirá otra.")
    normal = BINANCE.open_orders(symbol)
    algo = BINANCE.open_algo_orders(symbol)
    if normal or algo:
        raise BinanceAPIError(
            f"Hay órdenes abiertas en {symbol} que el bot no creó ({len(normal)} normales, {len(algo)} condicionales). "
            "Cancélalas o revisa el símbolo manualmente antes de continuar."
        )
    return info


def to_valid_initial(symbol_info: dict[str, Any], price: Decimal, qty: Decimal, side: str,
                     mode: str) -> tuple[Decimal, Decimal]:
    tick = symbol_info["tick_size"]
    step = symbol_info["market_step_size"] if mode == "MERCADO" else symbol_info["step_size"]
    minimum_qty = symbol_info["market_min_qty"] if mode == "MERCADO" else symbol_info["min_qty"]
    maximum_qty = symbol_info["market_max_qty"] if mode == "MERCADO" else symbol_info["max_qty"]
    q = floor_step(qty, step)
    if q < minimum_qty:
        raise ValueError(f"La cantidad {fnum(q, 10)} es inferior al mínimo del contrato ({fnum(minimum_qty,10)}).")
    if q > maximum_qty:
        raise ValueError(f"La cantidad {fnum(q,10)} supera el máximo permitido por Binance ({fnum(maximum_qty,10)}).")
    if mode == "LÍMITE":
        p = floor_step(price, tick) if side == "LONG" else ceil_step(price, tick)
        if p <= 0:
            raise ValueError("El precio de entrada queda en cero tras aplicar el tick de Binance.")
    elif mode == "LÍMITE RÁPIDO ±0.002":
        p = price
    else:
        p = price
    if mode != "MERCADO" and (p < symbol_info["min_price"] or p > symbol_info["max_price"]):
        raise ValueError("El precio de entrada está fuera del rango permitido por Binance para este contrato.")
    min_notional = symbol_info.get("min_notional", Decimal("0"))
    if mode != "MERCADO" and min_notional > 0 and p * q < min_notional:
        raise ValueError(f"El nominal de la entrada ({fnum(p*q, 4)} USDT) es inferior al mínimo del contrato ({fnum(min_notional,4)} USDT).")
    return p, q


def validate_plan_exchange_rules(plan: dict[str, Any], info: dict[str, Any]) -> None:
    min_notional = info.get("min_notional", Decimal("0"))
    for level in plan["levels"]:
        qty, price = D(level["qty"]), D(level["price"])
        if qty < info["min_qty"] or qty > info["max_qty"]:
            raise ValueError(
                f"La recompra #{int(level['n'])} ({fnum(qty,10)} monedas) incumple el rango de cantidad del contrato "
                f"({fnum(info['min_qty'],10)}–{fnum(info['max_qty'],10)}). Ajusta la cantidad inicial o los parámetros."
            )
        if price < info["min_price"] or price > info["max_price"]:
            raise ValueError(f"El precio de recompra #{int(level['n'])} queda fuera del rango permitido por Binance.")
        if min_notional > 0 and price * qty < min_notional:
            raise ValueError(
                f"La recompra #{int(level['n'])} equivale a {fnum(price*qty,4)} USDT, menos del mínimo de {fnum(min_notional,4)} USDT. "
                "No enviaré una escalera que Binance pueda rechazar."
            )
    stop = D(plan["stop_exchange"])
    if stop < info["min_price"] or stop > info["max_price"]:
        raise ValueError("El precio del stop calculado queda fuera del rango permitido por el contrato de Binance.")


def create_preview(draft: dict[str, Any]) -> tuple[dict[str, Any], str]:
    symbol = draft["symbol"]
    side = draft["side"]
    mode = draft["mode"]
    info = BINANCE.symbol_info(symbol)
    typed_price = D(draft["entry_price_text"].replace(",", "."))
    qty = D(draft["qty_text"].replace(",", "."))
    if mode == "MERCADO":
        preview_price = typed_price
        actual_mode_price = None
    elif mode == "LÍMITE RÁPIDO ±0.002":
        market = BINANCE.mark_price(symbol)
        raw = market + FAST_LIMIT_OFFSET if side == "LONG" else market - FAST_LIMIT_OFFSET
        if raw <= 0:
            raise ValueError("El desplazamiento 0.002 supera el precio actual. Usa LÍMITE o MERCADO para este contrato.")
        # Aggressive limit: buy above current; sell below current.
        actual_mode_price = ceil_step(raw, info["tick_size"]) if side == "LONG" else floor_step(raw, info["tick_size"])
        preview_price = actual_mode_price
    else:
        actual_mode_price = None
        preview_price = typed_price
    initial_price, initial_qty = to_valid_initial(info, preview_price, qty, side, mode)
    settings = dict(draft.get("settings_snapshot") or settings_snapshot())
    draft["settings_snapshot"] = settings
    plan = calculator_plan(side, initial_price, initial_qty, draft["entry_price_text"], settings,
                           info["tick_size"], info["step_size"])
    validate_plan_exchange_rules(plan, info)
    draft["executable_price"] = decstr(initial_price) if mode != "MERCADO" else None
    draft["executable_qty"] = decstr(initial_qty)
    draft["preview_plan"] = serializable_plan(plan)
    return plan, plan_text(symbol, side, initial_price, initial_qty, plan, settings, mode, ENV_NAME)


def serializable_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": plan["side"], "entry_price": decstr(plan["entry_price"]), "base_qty": decstr(plan["base_qty"]),
        "precision": plan["precision"],
        "levels": [{"n": int(x["n"]), "price": decstr(x["price"]), "qty": decstr(x["qty"]), "usdt": decstr(x["usdt"])} for x in plan["levels"]],
        "stop_raw": decstr(plan["stop_raw"]), "stop_exchange": decstr(plan["stop_exchange"]),
        "stop_display": decstr(plan["stop_display"]), "pct_stop": decstr(plan["pct_stop"]),
        "total_qty": decstr(plan["total_qty"]), "total_cost": decstr(plan["total_cost"]), "loss": decstr(plan["loss"]),
        "adjusted_for_exchange": bool(plan.get("adjusted_for_exchange")),
    }


def restore_plan(plan: dict[str, Any]) -> dict[str, Any]:
    out = dict(plan)
    for key in ("entry_price", "base_qty", "stop_raw", "stop_exchange", "stop_display", "pct_stop", "total_qty", "total_cost", "loss"):
        out[key] = D(out[key])
    out["levels"] = [{"n": D(x["n"]), "price": D(x["price"]), "qty": D(x["qty"]), "usdt": D(x["usdt"])} for x in out["levels"]]
    return out


def submit_entry(draft: dict[str, Any], chat_id: int) -> dict[str, Any]:
    info = check_one_way_and_clean_symbol(draft["symbol"])
    symbol, side, mode = draft["symbol"], draft["side"], draft["mode"]
    typed_price = D(draft["entry_price_text"].replace(",", "."))
    qty_in = D(draft["qty_text"].replace(",", "."))
    if mode == "LÍMITE RÁPIDO ±0.002":
        market = BINANCE.mark_price(symbol)
        raw = market + FAST_LIMIT_OFFSET if side == "LONG" else market - FAST_LIMIT_OFFSET
        if raw <= 0:
            raise ValueError("El precio actual es demasiado bajo para usar un desplazamiento de 0.002.")
        price = ceil_step(raw, info["tick_size"]) if side == "LONG" else floor_step(raw, info["tick_size"])
    else:
        price = typed_price
    price, qty = to_valid_initial(info, price, qty_in, side, mode)

    # Persist a submitting record before calling Binance, so a process restart won't forget intent.
    settings = dict(draft.get("settings_snapshot") or settings_snapshot())
    trade = {
        "environment": ENV_NAME, "status": "submitting_entry", "chat_id": chat_id,
        "settings": settings,
        "symbol": symbol, "side": side, "entry_mode": mode,
        "entry_order_id": None, "entry_client_order_id": new_client_id("E"),
        "entry_price_text": draft["entry_price_text"],
        "requested_entry_price": decstr(typed_price), "requested_qty": decstr(qty_in),
        "sent_entry_price": decstr(price) if mode != "MERCADO" else None,
        "sent_qty": decstr(qty), "created_at": time.time(),
        "grid_orders": [], "stop_algo_id": None, "trailing_algo_id": None,
        "trailing_activated": False, "last_notified_state": None,
    }
    set_active_trade(trade)
    api_side = "BUY" if side == "LONG" else "SELL"
    if mode == "MERCADO":
        response = BINANCE.place_order({
            "symbol": symbol, "side": api_side, "type": "MARKET", "quantity": decstr(qty),
            "newOrderRespType": "RESULT", "newClientOrderId": trade["entry_client_order_id"],
        })
    else:
        response = BINANCE.place_order({
            "symbol": symbol, "side": api_side, "type": "LIMIT", "timeInForce": "GTC",
            "quantity": decstr(qty), "price": decstr(price), "newOrderRespType": "RESULT",
            "newClientOrderId": trade["entry_client_order_id"],
        })
    trade["entry_order_id"] = response.get("orderId")
    trade["status"] = "waiting_entry"
    trade["entry_response"] = {k: response.get(k) for k in ("orderId", "status", "avgPrice", "executedQty", "cumQuote")}
    set_active_trade(trade)

    # Market or a marketable limit may fill immediately.
    executed = D(response.get("executedQty", "0"))
    if response.get("status") == "FILLED" and executed > 0:
        initialize_trade_after_entry(trade, response, info)
        return {"trade": active_trade_snapshot(), "message": initial_success_message(active_trade_snapshot())}

    return {"trade": active_trade_snapshot(), "message": (
        f"✅ Orden inicial enviada a Binance {ENV_NAME}.\nSímbolo: {symbol} · {side}\n"
        f"Tipo: {mode}\nPrecio enviado: {fnum(price, 12) if mode != 'MERCADO' else 'Market'}\n"
        f"Cantidad: {fnum(qty, 10)}\nOrder ID: {response.get('orderId')}\n\n"
        "El bot esperará el llenado para calcular con el precio real y colocar el SL y las recompras. "
        "Una orden límite pendiente todavía no tiene posición que proteger."
    )}


def initial_success_message(trade: dict[str, Any] | None) -> str:
    if not trade:
        return "La orden se envió, pero no pude recuperar el estado persistido. Revisa Binance antes de continuar."
    plan = trade.get("plan", {})
    return (
        f"✅ Entrada confirmada en Binance {trade.get('environment')}\n"
        f"{trade.get('symbol')} · {trade.get('side')}\n"
        f"Precio promedio real: {trade.get('entry_avg_price')}\n"
        f"Cantidad ejecutada: {trade.get('entry_executed_qty')}\n"
        f"Recompras enviadas: {len(trade.get('grid_orders', []))}\n"
        f"SL condicional: {plan.get('stop_exchange')} ({plan.get('pct_stop')}%)\n"
        f"Trailing: se activará al moverse favorablemente {trade.get('trailing_activation_pct')}% desde la entrada.\n"
        "El bot está vigilando el estado de la operación."
    )


def algo_stop(side: str, symbol: str, stop_price: Decimal, client_algo_id: str | None = None) -> dict[str, Any]:
    close_side = "SELL" if side == "LONG" else "BUY"
    return BINANCE.place_algo({
        "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
        "type": "STOP_MARKET", "triggerPrice": decstr(stop_price),
        "closePosition": "true", "workingType": "MARK_PRICE", "newOrderRespType": "RESULT",
        "clientAlgoId": client_algo_id or new_client_id("S"),
    })


def position_qty_and_avg(symbol: str) -> tuple[Decimal, Decimal]:
    pos = BINANCE.position(symbol)
    if not pos:
        return Decimal("0"), Decimal("0")
    return D(pos.get("positionAmt", "0")), D(pos.get("entryPrice", "0"))


def safe_cancel_algo(symbol: str, algo_id: Any) -> None:
    if algo_id:
        try:
            BINANCE.cancel_algo(symbol, algo_id)
        except Exception as exc:
            log.warning("No pude cancelar algo order %s para %s: %s", algo_id, symbol, exc)


def safe_cancel_normal(symbol: str, order_id: Any) -> None:
    if order_id:
        try:
            BINANCE.cancel_order(symbol, order_id)
        except Exception as exc:
            log.warning("No pude cancelar order %s para %s: %s", order_id, symbol, exc)


def emergency_flatten(trade: dict[str, Any], reason: str) -> tuple[bool, str]:
    """Best-effort emergency cleanup; never forget a trade unless flat and orders are verified."""
    symbol = trade["symbol"]
    problems: list[str] = []

    try:
        BINANCE.cancel_all_normal_orders(symbol)
    except Exception as exc:
        problems.append(f"cancelar órdenes normales: {exc}")
        log.exception("No pude cancelar todas las órdenes normales de %s", symbol)

    # First try to exit while the existing stop is still live.
    position_after_close: Decimal | None = None
    try:
        pos_amt, _avg = position_qty_and_avg(symbol)
        if pos_amt != 0:
            info = BINANCE.symbol_info(symbol)
            close_side = "SELL" if pos_amt > 0 else "BUY"
            BINANCE.close_market(symbol, close_side, abs(pos_amt), info["market_step_size"])
        position_after_close, _avg = position_qty_and_avg(symbol)
    except Exception as exc:
        problems.append(f"cerrar/verificar posición: {exc}")
        log.exception("Falló el cierre de emergencia de %s", symbol)

    # The bot refuses symbols with pre-existing algo orders, so open algos here belong to this strategy.
    # Do NOT remove a protective stop while a position may still be open.
    if position_after_close == 0:
        try:
            for algo in BINANCE.open_algo_orders(symbol):
                algo_id = algo.get("algoId")
                if algo_id:
                    try:
                        BINANCE.cancel_algo(symbol, algo_id)
                    except Exception as exc:
                        problems.append(f"cancelar protección {algo_id}: {exc}")
                        log.exception("No pude cancelar algo order %s", algo_id)
        except Exception as exc:
            problems.append(f"consultar protecciones condicionales: {exc}")
            log.exception("No pude consultar las protecciones de %s", symbol)
    else:
        problems.append("La posición puede seguir abierta; conservé las órdenes condicionales de protección.")

    # Verify both the position and the absence of leftover orders before clearing persistent state.
    remaining_position: Decimal | None = None
    remaining_normal: list[dict[str, Any]] | None = None
    remaining_algo: list[dict[str, Any]] | None = None
    try:
        remaining_position, _avg = position_qty_and_avg(symbol)
    except Exception as exc:
        problems.append(f"verificar posición: {exc}")
    try:
        remaining_normal = BINANCE.open_orders(symbol)
    except Exception as exc:
        problems.append(f"verificar órdenes normales: {exc}")
    try:
        remaining_algo = BINANCE.open_algo_orders(symbol)
    except Exception as exc:
        problems.append(f"verificar órdenes condicionales: {exc}")

    is_flat = remaining_position is not None and remaining_position == 0
    no_orders = remaining_normal == [] and remaining_algo == []
    if is_flat and no_orders and not problems:
        set_active_trade(None)
        return True, "La posición está cerrada y no quedan órdenes abiertas en el símbolo."

    trade["status"] = "emergency_review"
    trade["emergency_reason"] = reason[:800]
    trade["emergency_last_attempt_at"] = time.time()
    trade["emergency_last_error"] = "; ".join(problems)[:1000]
    set_active_trade(trade)
    detail = "; ".join(problems) if problems else "No pude confirmar que la posición y todas las órdenes estén cerradas."
    return False, detail


def initialize_trade_after_entry(trade: dict[str, Any], order: dict[str, Any], info: dict[str, Any] | None = None) -> None:
    symbol, side = trade["symbol"], trade["side"]
    trade["status"] = "initializing_protection"
    trade["initializing_started_at"] = time.time()
    set_active_trade(trade)
    try:
        info = info or BINANCE.symbol_info(symbol)
        executed_qty = D(order.get("executedQty", "0"))
        avg_price = D(order.get("avgPrice", "0"))
        if avg_price <= 0 and executed_qty > 0:
            cum_quote = D(order.get("cumQuote", "0"))
            avg_price = cum_quote / executed_qty if cum_quote > 0 else D(order.get("price", trade.get("sent_entry_price") or "0"))
        if executed_qty <= 0 or avg_price <= 0:
            raise BinanceAPIError("Binance informó un llenado sin cantidad o precio promedio válido.")

        settings = dict(trade.get("settings") or settings_snapshot())
        plan = calculator_plan(side, avg_price, executed_qty, trade["entry_price_text"], settings,
                               info["tick_size"], info["step_size"])
        validate_plan_exchange_rules(plan, info)
        mark = BINANCE.mark_price(symbol)
        stop_price = plan["stop_exchange"]
        stop_already_crossed = (side == "LONG" and mark <= stop_price) or (side == "SHORT" and mark >= stop_price)
        if stop_already_crossed:
            raise BinanceAPIError("El precio ya cruzó el SL calculado durante la entrada.")
        # Persist the unique conditional-order ID before sending it, then place protection before the grid.
        stop_client_algo_id = trade.get("stop_client_algo_id") or new_client_id("S")
        trade["stop_client_algo_id"] = stop_client_algo_id
        trade["entry_avg_price"] = decstr(avg_price)
        trade["entry_executed_qty"] = decstr(executed_qty)
        trade["plan"] = serializable_plan(plan)
        set_active_trade(trade)
        stop_resp = algo_stop(side, symbol, stop_price, stop_client_algo_id)
    except Exception as exc:
        closed, detail = emergency_flatten(trade, f"No pude calcular o colocar SL tras la entrada: {exc}")
        if closed:
            raise BinanceAPIError(f"No pude armar la estrategia protegida ({exc}). {detail} Se intentó cerrar por seguridad.") from exc
        raise BinanceAPIError(f"FALLO CRÍTICO: no pude armar el SL ({exc}) y tampoco confirmar el cierre de emergencia. Revisa Binance inmediatamente. {detail}") from exc

    grid_orders: list[dict[str, Any]] = []
    trade["stop_algo_id"] = stop_resp.get("algoId")
    trade["stop_client_algo_id"] = stop_resp.get("clientAlgoId", trade.get("stop_client_algo_id"))
    trade["grid_orders"] = grid_orders
    trade["status"] = "initializing_grid"
    set_active_trade(trade)
    try:
        entry_side = "BUY" if side == "LONG" else "SELL"
        for level in plan["levels"]:
            client_order_id = new_client_id("G")
            row = {
                "order_id": None, "client_order_id": client_order_id, "level": int(level["n"]),
                "price": decstr(level["price"]), "qty": decstr(level["qty"]), "status": "SUBMITTING",
            }
            grid_orders.append(row)
            set_active_trade(trade)
            response = BINANCE.place_order({
                "symbol": symbol, "side": entry_side, "type": "LIMIT", "timeInForce": "GTC",
                "quantity": decstr(level["qty"]), "price": decstr(level["price"]),
                "newOrderRespType": "RESULT", "newClientOrderId": client_order_id,
            })
            row["order_id"] = response.get("orderId")
            row["status"] = response.get("status", "NEW")
            set_active_trade(trade)
    except Exception as exc:
        # If any rung failed, clean all orders and only forget the strategy after verifying flat/no orders.
        closed, detail = emergency_flatten(trade, f"Falló una orden de la escalera: {exc}")
        if closed:
            raise BinanceAPIError(f"Falló una recompra ({exc}). {detail} Se cerró la posición por seguridad.") from exc
        raise BinanceAPIError(f"FALLO CRÍTICO: falló una recompra ({exc}) y no pude confirmar el cierre. Revisa Binance inmediatamente. {detail}") from exc

    trade["status"] = "active"
    trade["entry_avg_price"] = decstr(avg_price)
    trade["entry_executed_qty"] = decstr(executed_qty)
    trade["plan"] = serializable_plan(plan)
    trade["grid_orders"] = grid_orders
    trade["stop_algo_id"] = stop_resp.get("algoId")
    trade["stop_client_algo_id"] = stop_resp.get("clientAlgoId")
    trade["trailing_activation_pct"] = settings["trailing_activation_pct"]
    trade["trailing_callback_pct"] = settings["trailing_callback_pct"]
    trade["trailing_activated"] = False
    trade["initialized_at"] = time.time()
    set_active_trade(trade)


def monitor_once() -> list[tuple[int, str]]:
    """One synchronous monitoring pass; returns (chat_id, message) notifications."""
    notifications: list[tuple[int, str]] = []
    trade = active_trade_snapshot()
    if not trade:
        return notifications
    chat_id = int(trade.get("chat_id", 0))
    symbol = trade["symbol"]
    if trade.get("environment") != ENV_NAME:
        notifications.append((chat_id, f"🚨 El entorno actual ({ENV_NAME}) no coincide con el de la operación guardada ({trade.get('environment')}). No tocaré órdenes automáticamente. Revisa Binance y el archivo de estado."))
        return notifications

    if trade.get("status") in {"initializing_protection", "initializing_grid", "trailing_activating"}:
        reason = f"El proceso se reinició durante el estado {trade.get('status')}"
        closed, detail = emergency_flatten(trade, reason)
        if closed:
            notifications.append((chat_id, f"⚠️ Detecté una interrupción durante la preparación de {symbol}. Cerré la posición y limpié las órdenes para evitar duplicados."))
        else:
            notifications.append((chat_id, f"🚨 FALLO CRÍTICO tras interrupción en {symbol}: no pude confirmar el cierre completo. Revisa Binance inmediatamente. {detail}"))
        return notifications

    if trade.get("status") == "emergency_review":
        last_attempt = float(trade.get("emergency_last_attempt_at", 0) or 0)
        if time.time() - last_attempt < 10:
            return notifications
        closed, detail = emergency_flatten(trade, trade.get("emergency_reason", "Revisión de emergencia"))
        if closed:
            notifications.append((chat_id, f"✅ Recuperación de emergencia: {symbol}. La posición está cerrada y no quedan órdenes abiertas."))
        else:
            current = active_trade_snapshot() or trade
            last_notice = float(current.get("emergency_last_notification_at", 0) or 0)
            if time.time() - last_notice >= 60:
                current["emergency_last_notification_at"] = time.time()
                set_active_trade(current)
                notifications.append((chat_id, f"🚨 FALLO CRÍTICO en {symbol}: sigo sin poder confirmar el cierre completo. Revisa Binance inmediatamente. Detalle: {detail}"))
        return notifications

    if trade.get("status") in {"submitting_entry", "waiting_entry"}:
        try:
            order = BINANCE.order(symbol, trade.get("entry_order_id"), trade.get("entry_client_order_id"))
        except Exception as exc:
            log.warning("No pude consultar orden de entrada %s: %s", trade.get("entry_order_id"), exc)
            return notifications
        if order.get("orderId") is not None and order.get("orderId") != trade.get("entry_order_id"):
            trade["entry_order_id"] = order.get("orderId")
            set_active_trade(trade)
        status = order.get("status", "")
        executed = D(order.get("executedQty", "0"))
        if status == "FILLED" and executed > 0:
            try:
                initialize_trade_after_entry(trade, order)
                new_trade = active_trade_snapshot()
                notifications.append((chat_id, initial_success_message(new_trade)))
            except Exception as exc:
                notifications.append((chat_id, f"🚨 Problema al activar la protección de entrada: {exc}"))
        elif status == "PARTIALLY_FILLED" and executed > 0:
            # Never initialize from a partial entry until Binance confirms the unfilled remainder is gone.
            try:
                BINANCE.cancel_order(symbol, trade["entry_order_id"])
            except Exception as exc:
                log.warning("No pude cancelar remanente de entrada parcial: %s", exc)
            try:
                latest = BINANCE.order(symbol, trade.get("entry_order_id"), trade.get("entry_client_order_id"))
                if latest.get("status") in {"NEW", "PARTIALLY_FILLED"}:
                    reason = "No pude confirmar la cancelación del remanente de una entrada parcialmente ejecutada."
                    closed, detail = emergency_flatten(trade, reason)
                    if closed:
                        notifications.append((chat_id, f"⚠️ Entrada parcialmente ejecutada, pero Binance no confirmó la cancelación del remanente. Cerré por seguridad y limpié las órdenes. {detail}"))
                    else:
                        notifications.append((chat_id, f"🚨 FALLO CRÍTICO: no pude cancelar el remanente ni confirmar cierre seguro. Revisa Binance inmediatamente. {detail}"))
                    return notifications
                initialize_trade_after_entry(trade, latest)
                new_trade = active_trade_snapshot()
                notifications.append((chat_id,
                    "⚠️ La entrada límite se llenó parcialmente; cancelé el remanente y armé la estrategia con la cantidad realmente ejecutada.\n\n" + initial_success_message(new_trade)))
            except Exception as exc:
                notifications.append((chat_id, f"🚨 Hubo llenado parcial de entrada y fallo al activar la protección: {exc}. Revisa Binance inmediatamente."))
        elif status in {"CANCELED", "EXPIRED", "REJECTED"}:
            if executed > 0:
                try:
                    initialize_trade_after_entry(trade, order)
                    notifications.append((chat_id, "⚠️ La orden de entrada terminó parcialmente; armé la estrategia con la cantidad ejecutada.\n\n" + initial_success_message(active_trade_snapshot())))
                except Exception as exc:
                    notifications.append((chat_id, f"🚨 La orden terminó con ejecución parcial, pero no pude armar protección: {exc}. Revisa Binance inmediatamente."))
            else:
                set_active_trade(None)
                notifications.append((chat_id, f"La orden de entrada {status.lower()} sin ejecución. No se creó la escalera."))
        return notifications

    try:
        pos_amt, avg_entry = position_qty_and_avg(symbol)
    except Exception as exc:
        log.warning("No pude consultar posición %s: %s", symbol, exc)
        return notifications

    if pos_amt == 0:
        # No position means SL/trailing/manual close/liquidation ended it. Cancel this strategy's remaining orders.
        try:
            BINANCE.cancel_all_normal_orders(symbol)
        except Exception as exc:
            log.warning("Error cancelando órdenes normales tras cierre: %s", exc)
        safe_cancel_algo(symbol, trade.get("stop_algo_id"))
        safe_cancel_algo(symbol, trade.get("trailing_algo_id"))
        set_active_trade(None)
        notifications.append((chat_id, f"✅ La posición de {symbol} ya está cerrada. Cancelé las órdenes pendientes restantes de la estrategia. Revisa el historial de Binance para ver si cerró por SL, trailing o manualmente."))
        return notifications

    # Audit grid orders. If a planned order is unexpectedly rejected/cancelled, flatten rather than risk a mismatched SL.
    if not trade.get("trailing_activated"):
        for row in trade.get("grid_orders", []):
            if row.get("completed_check"):
                continue
            try:
                order = BINANCE.order(symbol, row.get("order_id"), row.get("client_order_id"))
                status = order.get("status", "")
                if status == "FILLED":
                    row["status"] = "FILLED"
                    row["completed_check"] = True
                    set_active_trade(trade)
                elif status in {"CANCELED", "EXPIRED", "REJECTED"}:
                    executed = D(order.get("executedQty", "0"))
                    if executed > 0:
                        row["status"] = status
                        row["executed_qty"] = decstr(executed)
                        row["completed_check"] = True
                        set_active_trade(trade)
                    else:
                        # A canceled/rejected rung changes projected inventory; close safest rather than use a mismatched SL.
                        raise BinanceAPIError(f"La recompra #{row['level']} terminó en {status} sin llenado.")
            except BinanceAPIError as exc:
                log.error("Escalera no íntegra: %s", exc)
                try:
                    BINANCE.cancel_all_normal_orders(symbol)
                    safe_cancel_algo(symbol, trade.get("stop_algo_id"))
                    p_amt, _ = position_qty_and_avg(symbol)
                    info = BINANCE.symbol_info(symbol)
                    if p_amt != 0:
                        close_side = "SELL" if p_amt > 0 else "BUY"
                        BINANCE.close_market(symbol, close_side, abs(p_amt), info["market_step_size"])
                    safe_cancel_algo(symbol, trade.get("trailing_algo_id"))
                    set_active_trade(None)
                    notifications.append((chat_id, f"🚨 {exc}\nCancelé la escalera y ordené cerrar la posición para no dejar una estrategia desajustada. Confirma el estado en Binance."))
                except Exception as close_exc:
                    notifications.append((chat_id, f"🚨 {exc}\nAdemás falló el cierre de emergencia: {close_exc}. Entra a Binance inmediatamente y revisa la posición."))
                return notifications
            except Exception as exc:
                log.warning("No pude auditar recompra %s: %s", row.get("order_id"), exc)

    if not trade.get("trailing_activated"):
        last_attempt = float(trade.get("trailing_last_attempt_at", 0) or 0)
        if last_attempt and time.time() - last_attempt < 30:
            return notifications
        try:
            settings = dict(trade.get("settings") or settings_snapshot())
            mark = BINANCE.mark_price(symbol)
            entry = D(trade["entry_avg_price"])
            activation_pct = D(trade.get("trailing_activation_pct", settings["trailing_activation_pct"])) / Decimal("100")
            activation_mark = entry * (Decimal("1") + activation_pct if trade["side"] == "LONG" else Decimal("1") - activation_pct)
            reached = mark >= activation_mark if trade["side"] == "LONG" else mark <= activation_mark
            if reached:
                info = BINANCE.symbol_info(symbol)
                # Calculate a loss cap based on what is ACTUALLY held once unfilled grid orders are cancelled.
                actual_cost = avg_entry * abs(pos_amt)
                dynamic_stop_raw = calc_stop(trade["side"], actual_cost, abs(pos_amt), D(settings["max_loss_usdt"]))
                if dynamic_stop_raw <= 0:
                    # Cannot represent a positive SL for current size. Keep existing ladder/SL instead of stripping protection.
                    if not trade.get("trailing_infeasible_notified"):
                        notifications.append((chat_id,
                            "⚠️ El trailing aún no se activó: con la cantidad actual, un SL por la pérdida máxima configurada produciría un precio no válido. "
                            "Conservo la protección SL y las recompras para no alterar el riesgo."))
                        trade["trailing_infeasible_notified"] = True
                        set_active_trade(trade)
                    return notifications
                dynamic_stop = ceil_step(dynamic_stop_raw, info["tick_size"]) if trade["side"] == "LONG" else floor_step(dynamic_stop_raw, info["tick_size"])
                if (trade["side"] == "LONG" and dynamic_stop >= mark) or (trade["side"] == "SHORT" and dynamic_stop <= mark):
                    if not trade.get("trailing_infeasible_notified"):
                        notifications.append((chat_id, "⚠️ El trailing todavía no se activó porque el SL de la pérdida máxima quedaría del lado incorrecto del mercado. Mantengo las protecciones y lo volveré a comprobar."))
                        trade["trailing_infeasible_notified"] = True
                        set_active_trade(trade)
                    return notifications

                # First cancel the rungs that are still waiting so they cannot reopen after a trailing close.
                for row in trade.get("grid_orders", []):
                    if row.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                        safe_cancel_normal(symbol, row.get("order_id"))
                        row["status"] = "CANCELED_BY_TRAILING"
                        row["completed_check"] = True
                set_active_trade(trade)
                # Persist intent before a potentially ambiguous network request.
                old_stop_id = trade.get("stop_algo_id")
                new_stop_client_id = new_client_id("S")
                trade["pending_stop_client_algo_id"] = new_stop_client_id
                trade["trailing_last_attempt_at"] = time.time()
                trade["status"] = "trailing_activating"
                set_active_trade(trade)
                # Place replacement protection before cancelling the old stop.
                new_stop_resp = algo_stop(trade["side"], symbol, dynamic_stop, new_stop_client_id)
                trade["stop_algo_id"] = new_stop_resp.get("algoId")
                trade["stop_price_after_trailing_activation"] = decstr(dynamic_stop)
                trade["trailing_last_attempt_at"] = time.time()
                trade["status"] = "trailing_activating"
                set_active_trade(trade)
                if old_stop_id:
                    safe_cancel_algo(symbol, old_stop_id)
                # Place native exchange trailing stop (Algo Service endpoint, not old /order endpoint).
                close_side = "SELL" if trade["side"] == "LONG" else "BUY"
                trail_activation = ceil_step(mark + info["tick_size"], info["tick_size"]) if trade["side"] == "LONG" else floor_step(mark - info["tick_size"], info["tick_size"])
                trailing_resp = BINANCE.place_algo({
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
                    "type": "TRAILING_STOP_MARKET", "quantity": decstr(floor_step(abs(pos_amt), info["step_size"])),
                    "reduceOnly": "true", "activatePrice": decstr(trail_activation),
                    "callbackRate": settings["trailing_callback_pct"], "workingType": "MARK_PRICE",
                    "newOrderRespType": "RESULT",
                })
                trade["trailing_algo_id"] = trailing_resp.get("algoId")
                trade["trailing_activate_price"] = decstr(trail_activation)
                trade["trailing_trigger_level"] = decstr(activation_mark)
                trade["trailing_activated"] = True
                trade["status"] = "trailing"
                trade["trailing_activated_at"] = time.time()
                set_active_trade(trade)
                notifications.append((chat_id,
                    f"🟢 Trailing stop activado para {symbol} ({trade['side']}).\n"
                    f"Se cancelaron las recompras que no se habían ejecutado.\n"
                    f"SL actualizado por la pérdida máxima: {fnum(dynamic_stop, 12)}\n"
                    f"Trailing activation: {fnum(trail_activation, 12)}\n"
                    f"Retroceso: {settings['trailing_callback_pct']}%.\n"
                    "El SL sigue activo como protección de respaldo."))
        except Exception as exc:
            log.exception("Error al activar trailing para %s", symbol)
            # Keep whatever protective SL id was last persisted; retry no faster than every 30 seconds.
            current = active_trade_snapshot() or trade
            current["trailing_last_attempt_at"] = time.time()
            current["trailing_last_error"] = str(exc)[:500]
            if current.get("stop_algo_id"):
                current["status"] = "trailing_pending"
            set_active_trade(current)
            notifications.append((chat_id, f"⚠️ No pude activar el trailing stop: {exc}. El SL condicional permanece registrado como protección; revisa el bot/Binance."))

    return notifications


async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global TRADE_LOCK
    if TRADE_LOCK is None:
        TRADE_LOCK = asyncio.Lock()
    if TRADE_LOCK.locked():
        return
    async with TRADE_LOCK:
        try:
            notifications = await asyncio.to_thread(monitor_once)
        except Exception:
            log.exception("Error en el monitor")
            return
    for chat_id, message in notifications:
        try:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text=message, reply_markup=main_keyboard())
        except Exception:
            log.exception("No pude enviar notificación a Telegram")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(
            f"Tu Telegram user ID es: {user.id}\n"
            "Pon ese número en ALLOWED_TELEGRAM_USER_ID dentro del archivo .env y reinicia el bot."
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth_ok(update):
        await update.effective_message.reply_text("Acceso privado. Usa /id para obtener tu ID y configurarlo en ALLOWED_TELEGRAM_USER_ID.")
        return
    mode_warning = "🧪 Entorno de pruebas Binance Futures Demo/Testnet" if ENV_NAME in {"DEMO", "TESTNET"} else "🚨 ENTORNO REAL DE BINANCE FUTURES"
    active = active_trade_snapshot()
    await update.effective_message.reply_text(
        f"Bot ElGafas Trading\n{mode_warning}\n\n"
        f"Una sola operación gestionada a la vez. Parámetros guardados aunque reinicies el bot.\n"
        f"Operación activa: {'sí' if active else 'no'}",
        reply_markup=main_keyboard(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth_ok(update):
        await update.effective_message.reply_text("No autorizado.")
        return
    active = active_trade_snapshot()
    if not active:
        await update.effective_message.reply_text("No hay operación gestionada por el bot.", reply_markup=main_keyboard())
        return
    msg = (
        f"Operación: {active['symbol']} · {active['side']}\nEstado: {active['status']}\n"
        f"Entorno: {active.get('environment')}\nModo entrada: {active.get('entry_mode')}\n"
        f"Order inicial: {active.get('entry_order_id')}\nRecompras registradas: {len(active.get('grid_orders', []))}\n"
        f"SL algo ID: {active.get('stop_algo_id')}\nTrailing algo ID: {active.get('trailing_algo_id')}"
    )
    await update.effective_message.reply_text(msg, reply_markup=main_keyboard())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not auth_ok(update):
        await query.edit_message_text("No autorizado. Usa /id para ver tu ID y configurarlo en el .env.")
        return
    data = query.data or ""

    if data == "menu":
        await query.edit_message_text("Menú principal", reply_markup=main_keyboard())
        return
    if data == "config":
        s = settings_snapshot()
        text = (
            "⚙️ CONFIGURACIÓN (se guarda hasta que la cambies)\n\n"
            f"1. Recompra/venta: {s['rebuy_pct']}%\n"
            f"2. Aumento de monedas: {s['qty_increase_pct']}%\n"
            f"3. Pérdida máxima: {s['max_loss_usdt']} USDT\n"
            f"4. Activación del trailing: {s['trailing_activation_pct']}% desde la entrada\n"
            f"5. Retroceso del trailing: {s['trailing_callback_pct']}%\n\n"
            "El trailing espera un movimiento favorable; después cancela las recompras pendientes, actualiza el SL por la pérdida máxima de la posición actual y crea un trailing nativo en Binance."
        )
        await query.edit_message_text(text, reply_markup=config_keyboard())
        return
    if data.startswith("set:"):
        key = data.split(":", 1)[1]
        labels = {
            "rebuy_pct": "% de recompra/venta",
            "qty_increase_pct": "% de aumento de monedas",
            "max_loss_usdt": "pérdida máxima en USDT",
            "trailing_activation_pct": "% favorable para activar trailing",
            "trailing_callback_pct": "% de retroceso del trailing (0.1 a 20)",
        }
        context.user_data["awaiting"] = f"setting:{key}"
        await query.edit_message_text(f"Envía el nuevo valor para {labels.get(key, key)}.\nEnvía /cancelar para volver sin cambiarlo.")
        return
    if data in {"long", "short"}:
        if active_trade_snapshot():
            await query.edit_message_text("Ya hay una operación gestionada. Espera a que termine antes de crear otra.", reply_markup=main_keyboard())
            return
        context.user_data.clear()
        context.user_data["draft"] = {"side": "LONG" if data == "long" else "SHORT"}
        context.user_data["awaiting"] = "symbol"
        await query.edit_message_text(
            f"Elegiste {context.user_data['draft']['side']}.\n\nEnvía el símbolo de Binance Futures USDT-M, por ejemplo BTCUSDT."
        )
        return
    if data.startswith("mode:"):
        draft = context.user_data.get("draft")
        if not draft or "symbol" not in draft or "entry_price_text" not in draft or "qty_text" not in draft:
            await query.edit_message_text("Se perdió el borrador. Vuelve al menú y empieza de nuevo.", reply_markup=main_keyboard())
            return
        mode_key = data.split(":", 1)[1]
        draft["mode"] = {"limit": "LÍMITE", "quick": "LÍMITE RÁPIDO ±0.002", "market": "MERCADO"}.get(mode_key, "LÍMITE")
        try:
            plan, preview = await asyncio.to_thread(create_preview, draft)
        except Exception as exc:
            await query.edit_message_text(f"No pude calcular una vista previa: {exc}", reply_markup=main_keyboard())
            return
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = None
        # Telegram message limit is 4096 chars; a 8-level preview is well below it.
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ EJECUTAR EN BINANCE", callback_data="execute"), InlineKeyboardButton("❌ CANCELAR", callback_data="cancel_draft")],
        ])
        await query.edit_message_text(preview, reply_markup=keyboard)
        return
    if data == "execute":
        draft = context.user_data.get("draft")
        if not draft:
            await query.edit_message_text("No hay borrador para ejecutar.", reply_markup=main_keyboard())
            return
        if active_trade_snapshot():
            await query.edit_message_text("Ya hay una operación activa; no enviaré otra.", reply_markup=main_keyboard())
            return
        global TRADE_LOCK
        if TRADE_LOCK is None:
            TRADE_LOCK = asyncio.Lock()
        async with TRADE_LOCK:
            try:
                result = await asyncio.to_thread(submit_entry, draft, update.effective_chat.id)
                context.user_data.pop("draft", None)
                context.user_data["awaiting"] = None
                await query.edit_message_text(result["message"], reply_markup=main_keyboard())
            except Exception as exc:
                log.exception("Falló el envío de la entrada")
                active = active_trade_snapshot()
                if active and active.get("status") == "submitting_entry" and not active.get("entry_order_id"):
                    if isinstance(exc, BinanceAPIError) and not exc.ambiguous:
                        set_active_trade(None)
                        await query.edit_message_text(
                            f"❌ Binance rechazó la orden y no la aceptó: {exc}\nPuedes corregir los datos y volver a preparar la operación.",
                            reply_markup=main_keyboard(),
                        )
                    else:
                        await query.edit_message_text(
                            f"⚠️ No pude confirmar la respuesta de Binance: {exc}\n"
                            "Por seguridad conservé el ID único de la solicitud y no permitiré otra operación. "
                            "Revisa Binance antes de reintentar; el bot intentará localizar la orden automáticamente.",
                            reply_markup=main_keyboard(),
                        )
                else:
                    await query.edit_message_text(f"❌ No se pudo enviar la operación: {exc}", reply_markup=main_keyboard())
        return
    if data == "cancel_draft":
        context.user_data.pop("draft", None)
        context.user_data["awaiting"] = None
        await query.edit_message_text("Borrador cancelado. No se enviaron órdenes.", reply_markup=main_keyboard())
        return
    if data == "status":
        active = active_trade_snapshot()
        if not active:
            await query.edit_message_text("No hay operación gestionada por el bot.", reply_markup=main_keyboard())
        else:
            await query.edit_message_text(
                f"{active['symbol']} · {active['side']}\nEstado: {active['status']}\n"
                f"Entorno: {active.get('environment')}\nEntry order: {active.get('entry_order_id')}\n"
                f"Recompras: {len(active.get('grid_orders', []))}\nSL algo: {active.get('stop_algo_id')}\nTrailing algo: {active.get('trailing_algo_id')}",
                reply_markup=main_keyboard(),
            )
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth_ok(update):
        return
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return
    if text.lower() in {"/cancelar", "cancelar"}:
        context.user_data["awaiting"] = None
        context.user_data.pop("draft", None)
        await message.reply_text("Entrada cancelada. No se enviaron órdenes.", reply_markup=main_keyboard())
        return
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        await message.reply_text("Usa los botones del menú para comenzar.", reply_markup=main_keyboard())
        return

    if awaiting.startswith("setting:"):
        key = awaiting.split(":", 1)[1]
        try:
            value = validate_numeric_setting(key, text)
            set_setting(key, value)
            context.user_data["awaiting"] = None
            await message.reply_text(f"✅ Parámetro guardado: {key} = {value}", reply_markup=config_keyboard())
        except ValueError as exc:
            await message.reply_text(str(exc))
        return

    draft = context.user_data.get("draft")
    if not draft:
        context.user_data["awaiting"] = None
        await message.reply_text("No hay una operación en preparación. Vuelve al menú.", reply_markup=main_keyboard())
        return

    if awaiting == "symbol":
        symbol = re.sub(r"[^A-Za-z0-9]", "", text).upper()
        if not re.fullmatch(r"[A-Z0-9]{4,30}", symbol):
            await message.reply_text("Símbolo inválido. Envía algo como BTCUSDT, ETHUSDT o SOLUSDT.")
            return
        try:
            info = await asyncio.to_thread(BINANCE.symbol_info, symbol)
        except Exception as exc:
            await message.reply_text(f"No puedo usar ese símbolo: {exc}")
            return
        draft["symbol"] = symbol
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = "entry_price"
        await message.reply_text(f"Contrato validado: {symbol} (perpetuo USDT).\nEnvía el precio de entrada objetivo/de referencia.")
        return

    if awaiting == "entry_price":
        try:
            raw = text.replace(",", ".")
            value = D(raw)
            if not value.is_finite() or value <= 0:
                raise ValueError
        except Exception:
            await message.reply_text("Precio inválido. Envía un número positivo, por ejemplo 0.2072.")
            return
        draft["entry_price_text"] = raw
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = "qty"
        await message.reply_text("Ahora envía la cantidad inicial de monedas/contratos, por ejemplo 50 o 0.01.")
        return

    if awaiting == "qty":
        try:
            raw = text.replace(",", ".")
            value = D(raw)
            if not value.is_finite() or value <= 0:
                raise ValueError
        except Exception:
            await message.reply_text("Cantidad inválida. Envía un número positivo.")
            return
        draft["qty_text"] = raw
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = None
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 LÍMITE al precio escrito", callback_data="mode:limit")],
            [InlineKeyboardButton("⚡ LÍMITE RÁPIDO ±0.002 del mercado", callback_data="mode:quick")],
            [InlineKeyboardButton("📈 MERCADO", callback_data="mode:market")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_draft")],
        ])
        await message.reply_text(
            "Elige cómo entrar:\n\n"
            "• LÍMITE: deja la orden en el precio que escribiste; si está lejos puede esperar.\n"
            "• LÍMITE RÁPIDO: usa el precio de mercado ±0.002 USDT, no el precio de referencia escrito.\n"
            "• MERCADO: entra al precio disponible; la escalera se recalcula con el precio real ejecutado.",
            reply_markup=keyboard,
        )
        return


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth_ok(update):
        await update.effective_message.reply_text("No autorizado.")
        return
    context.user_data["awaiting"] = None
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text("Borrador cancelado. Las operaciones ya enviadas a Binance no se cancelan con este comando.", reply_markup=main_keyboard())


async def post_init(application: Application) -> None:
    global TRADE_LOCK
    TRADE_LOCK = asyncio.Lock()
    application.job_queue.run_repeating(monitor_job, interval=MONITOR_INTERVAL, first=2, name="monitor-futures")
    log.info("Estado persistente: %s | backup: %s", STATE_FILE, STATE_BACKUP_FILE)
    saved_trade = active_trade_snapshot()
    if saved_trade:
        # The monitor will reconcile the saved operation against Binance before changing it.
        try:
            recovery_chat = int(saved_trade.get("chat_id", 0) or 0)
            if recovery_chat:
                await application.bot.send_message(
                    chat_id=recovery_chat,
                    text=(
                        "♻️ Reinicio/conexión recuperada. Encontré una operación persistida: "
                        f"{saved_trade.get('symbol')} · {saved_trade.get('side')}\n"
                        f"Estado guardado: {saved_trade.get('status')}\n"
                        "Estoy reanudando la comprobación contra Binance. No volveré a crear la entrada inicial automáticamente."
                    ),
                    reply_markup=main_keyboard(),
                )
        except Exception:
            log.exception("No pude enviar la notificación de recuperación a Telegram")
    try:
        await application.bot.set_my_commands([
            ("start", "Abrir menú del bot"),
            ("status", "Ver operación gestionada"),
            ("id", "Mostrar tu ID de Telegram"),
            ("cancelar", "Cancelar un borrador no enviado"),
        ])
    except Exception:
        log.exception("No pude registrar comandos de Telegram")
    log.info("Bot inicializado. Entorno Binance=%s; intervalo monitor=%.1fs", ENV_NAME, MONITOR_INTERVAL)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN. Crea un bot con BotFather y pon el token en .env.")
    if not BINANCE_TESTNET and ENABLE_LIVE_TRADING:
        log.warning("ATENCIÓN: el bot podrá enviar órdenes REALES. Asegúrate de estar usando una API key limitada y revisada.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    # Keep retrying Telegram bootstrap indefinitely after a network/power restart; the
    # OS/Termux boot launcher should also restart the process if it exits unexpectedly.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
