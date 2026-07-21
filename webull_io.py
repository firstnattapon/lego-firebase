"""webull_io.py — Webull OpenAPI (region 'th') + config + market guard

- credential จาก env (map จาก Secret Manager) — ห้าม hardcode
- SDK ทำ signature + token 2FA อัตโนมัติ (set_token_dir cache token)
- fetch snapshot ทุกแถวเพื่อ recurrence 17 คอลัมน์สมบูรณ์ (LEGO 100%)
- preview กับ place แยกฟังก์ชัน — submit gate ต้องอยู่ระหว่างกลาง (invariant #9)
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from lego_one_row import Config

UAT_ENDPOINT = "th-api.uat.webullbroker.com"
PROD_ENDPOINT = "api.webull.co.th"

_TRANSIENT_HTTP = {500, 502, 503, 504}


def _retry_transient(fn, attempts: int = 3, base_delay: float = 2.0):
    """เรียก fn(); retry เฉพาะ 5xx ชั่วคราวจากฝั่ง Webull (backoff 2s/4s)
    error อื่น (signature, 403, ValueError) raise ทันที — fail closed เหมือนเดิม"""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
            transient = (status in _TRANSIENT_HTTP
                         or getattr(exc, "error_code", "") == "GATEWAY_TIMEOUT")
            if not transient:
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
    return "Test (UAT)" if os.environ.get("WEBULL_ENV", "UAT").upper() == "UAT" else "Production"


def _endpoint() -> str:
    return UAT_ENDPOINT if environment_label() == "Test (UAT)" else PROD_ENDPOINT


def is_us_market_open(now: datetime | None = None) -> bool:
    """guard: จันทร์–ศุกร์ 09:30–16:00 America/New_York (DST-aware)

    หน้าต่าง UTC ตายตัวใช้ไม่ได้: ฤดูหนาว (EST) ตลาดคือ 14:30–21:00 UTC —
    เปิดเร็วไป = กิน DNA slot ด้วยราคา pre-market, ปิดเร็วไป = พลาดชั่วโมงสุดท้าย
    ไม่รวมวันหยุด/half-day NYSE — production ต่อปฏิทิน (pandas_market_calendars)
    """
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        ny = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # ไม่มี tzdata -> fallback หน้าต่าง EDT (ฝั่งกว้าง: ไม่พลาดชั่วโมงเทรดจริง)
        ny = None
    if ny is None:
        if now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        return 13 * 60 + 30 <= minutes < 21 * 60
    if ny.weekday() >= 5:
        return False
    minutes = ny.hour * 60 + ny.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


# ---- Webull clients (lazy import) -----------------------------------------
def build_clients():
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    from webull.data.data_client import DataClient

    api = ApiClient(os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"], "th")
    api.add_endpoint("th", _endpoint())
    api.set_token_dir(os.environ.get("WEBULL_TOKEN_DIR", "/tmp/webull_token"))
    return TradeClient(api), DataClient(api)


def fetch_snapshot(trade_client, data_client, cfg: Config) -> dict:
    """คืน {captured_at, price, holdings}; fail closed ถ้า price <= 0"""
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
        "combo_type": "NORMAL", "client_order_id": client_order_id,
        "symbol": cfg.symbol.upper(), "instrument_type": "EQUITY", "market": "US",
        "order_type": "MARKET",
        "quantity": f"{qty:.{cfg.decimal_precision}f}".rstrip("0").rstrip(".") or "0",
        "side": side, "time_in_force": "DAY", "entrust_type": "QTY",
        "support_trading_session": "CORE",
    }]


def preview_market_order(trade_client, order: list[dict]) -> bool:
    """preview อย่างเดียว — ไม่ส่ง order; คืน True ถ้าผ่าน"""
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    pr = trade_client.order_v3.preview_order(account_id, order).json()
    return bool(pr) and "error" not in pr


def place_market_order(trade_client, order: list[dict]) -> dict:
    """ส่ง order จริง — เรียกได้เฉพาะหลัง evaluate_submit_gate ผ่านแล้วเท่านั้น"""
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return trade_client.order_v3.place_order(account_id, order).json()


def fetch_order_detail(trade_client, client_order_id: str) -> dict:
    """สถานะจริงของ order หลัง place (invariant #10: FILLED ต้องยืนยัน ไม่เดา)"""
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return _retry_transient(
        lambda: trade_client.order_v3.get_order_detail(account_id, client_order_id).json())


def fetch_open_orders(trade_client, symbol: str) -> list[dict]:
    """open orders ของ symbol — guard กัน order ซ้อนก่อน place รอบใหม่"""
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    res = _retry_transient(
        lambda: trade_client.order_v3.get_order_open(account_id).json())
    if isinstance(res, list):
        items = res
    else:
        items = ((res or {}).get("orders") or (res or {}).get("items") or
                 (res or {}).get("data") or [])
    out = []
    for o in items:
        inner = o.get("items") if isinstance(o, dict) else None
        cands = inner if isinstance(inner, list) else [o]
        for c in cands:
            if str(c.get("symbol", "")).upper() == symbol.upper():
                out.append(c)
    return out


# ---- helpers (ปรับ path ให้ตรง schema จริงของ SDK response) ----------------
def _extract_qty(positions, symbol: str) -> float:
    """fail closed เมื่อ response shape ไม่รู้จัก — holdings=0 ปลอมทำให้ gap เต็ม FIX_C
    แล้ว READY_BUY ซ้ำทั้งก้อน; "ไม่มีหุ้น" ที่ถูกต้องคือ list ว่างใน key ที่รู้จัก"""
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
    for p in items:
        if isinstance(p, dict) and str(p.get("symbol", "")).upper() == symbol.upper():
            return float(p.get("quantity", 0) or 0)
    return 0.0


def _extract_price(snap, symbol: str) -> float:
    if isinstance(snap, list):
        snap = snap[0] if snap else {}
    for key in ("last", "lastPrice", "price", "close"):
        if snap.get(key):
            return float(snap[key])
    return 0.0
