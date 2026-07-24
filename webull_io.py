"""Webull OpenAPI adapter with fail-closed response parsing and transient retries."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from lego_one_row import Config
from lego_orders import PROD, UAT

UAT_ENDPOINT = "th-api.uat.webullbroker.com"
PROD_ENDPOINT = "api.webull.co.th"
_TRANSIENT_HTTP = {500, 502, 503, 504}
# Any of these present-and-truthy means the broker rejected the preview.
_PREVIEW_ERROR_KEYS = ("error", "error_code", "errorCode")


def is_transient_exception(exc: Exception) -> bool:
    status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    code = str(getattr(exc, "error_code", "") or "").upper()
    return status in _TRANSIENT_HTTP or code in {"GATEWAY_TIMEOUT", "TIMEOUT", "SERVICE_UNAVAILABLE"}


def _retry_transient(fn, attempts: int = 3, base_delay: float = 2.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_transient_exception(exc):
                raise
            last = exc
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last


def load_config() -> Config:
    return Config(
        symbol=os.environ["LEGO_SYMBOL"],
        fix_c=float(os.environ["LEGO_FIX_C"]),
        diff=float(os.environ.get("LEGO_DIFF", "0")),
        dna_code=os.environ.get("LEGO_DNA_CODE", "bypass:100"),
        strategy_id=os.environ.get("LEGO_STRATEGY_ID", "shannon_demon_lego"),
        decimal_precision=int(os.environ.get("LEGO_DECIMAL_PRECISION", "5")),
    )


def environment_label() -> str:
    return UAT if os.environ.get("WEBULL_ENV", "UAT").upper() == "UAT" else PROD


def _endpoint() -> str:
    return UAT_ENDPOINT if environment_label() == UAT else PROD_ENDPOINT


def build_clients():
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    from webull.data.data_client import DataClient

    api = ApiClient(os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"], "th")
    api.add_endpoint("th", _endpoint())
    api.set_token_dir(os.environ.get("WEBULL_TOKEN_DIR", "/tmp/webull_token"))
    return TradeClient(api), DataClient(api)


def fetch_snapshot(trade_client, data_client, cfg: Config) -> dict:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    positions = _retry_transient(
        lambda: trade_client.account_v2.get_account_position(account_id).json())
    holdings = _extract_qty(positions, cfg.symbol)
    snap = _retry_transient(
        lambda: data_client.market_data.get_snapshot(
            cfg.symbol.upper(), "US_STOCK",
            extend_hour_required=False, overnight_required=False).json())
    price = _extract_price(snap, cfg.symbol)
    if not (price and price > 0):
        raise ValueError(f"snapshot price ไม่ถูกต้อง ({price}) — fail closed")
    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": float(price),
        "holdings": float(holdings),
    }


def build_order_payload(cfg: Config, side: str, qty: float, client_order_id: str) -> list[dict]:
    return [{
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": cfg.symbol.upper(),
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": f"{qty:.{cfg.decimal_precision}f}".rstrip("0").rstrip(".") or "0",
        "side": side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "support_trading_session": "CORE",
    }]


def preview_market_order(trade_client, order: list[dict]) -> bool:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    pr = _retry_transient(lambda: trade_client.order_v3.preview_order(account_id, order).json())
    if not pr:
        return False
    if isinstance(pr, list):
        pr = pr[0] if pr else {}
    if not isinstance(pr, dict):
        return False
    return not any(pr.get(key) for key in _PREVIEW_ERROR_KEYS)


def place_market_order(trade_client, order: list[dict]) -> dict:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return _retry_transient(lambda: trade_client.order_v3.place_order(account_id, order).json(), attempts=2)


def fetch_order_detail(trade_client, client_order_id: str) -> dict:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return _retry_transient(
        lambda: trade_client.order_v3.get_order_detail(account_id, client_order_id).json())


def fetch_open_orders(trade_client, symbol: str) -> list[dict]:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    res = _retry_transient(lambda: trade_client.order_v3.get_order_open(account_id).json())
    if isinstance(res, list):
        items = res
    elif isinstance(res, dict):
        found = False
        items = []
        for key in ("orders", "items", "data"):
            if key in res:
                found = True
                items = res.get(key) or []
                break
        if not found:
            raise ValueError("open-orders response shape ไม่รู้จัก — fail closed")
    else:
        raise ValueError("open-orders response shape ไม่รู้จัก — fail closed")
    if not isinstance(items, list):
        raise ValueError("open-orders items ต้องเป็น list — fail closed")
    out: list[dict] = []
    for o in items:
        if not isinstance(o, dict):
            continue
        inner = o.get("items")
        cands = inner if isinstance(inner, list) else [o]
        for c in cands:
            if isinstance(c, dict) and str(c.get("symbol", "")).upper() == symbol.upper():
                out.append(c)
    return out


def _extract_qty(positions, symbol: str) -> float:
    if isinstance(positions, list):
        items = positions
    elif isinstance(positions, dict):
        for key in ("positions", "items", "data"):
            if key in positions:
                items = positions.get(key) or []
                break
        else:
            raise ValueError("positions response shape ไม่รู้จัก — fail closed")
    else:
        raise ValueError("positions response shape ไม่รู้จัก — fail closed")
    if not isinstance(items, list):
        raise ValueError("positions items ต้องเป็น list — fail closed")
    for p in items:
        if isinstance(p, dict) and str(p.get("symbol", "")).upper() == symbol.upper():
            return float(p.get("quantity", 0) or 0)
    return 0.0


def _extract_price(snap, symbol: str) -> float:
    if isinstance(snap, list):
        snap = snap[0] if snap else {}
    if not isinstance(snap, dict):
        return 0.0
    for key in ("last", "lastPrice", "price", "close"):
        if snap.get(key):
            return float(snap[key])
    nested = snap.get(symbol.upper()) or snap.get(symbol)
    if isinstance(nested, dict):
        for key in ("last", "lastPrice", "price", "close"):
            if nested.get(key):
                return float(nested[key])
    return 0.0
