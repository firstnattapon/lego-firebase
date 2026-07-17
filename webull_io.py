"""webull_io.py — Webull OpenAPI (region 'th') + config + market guard

- credential จาก env (map จาก Secret Manager) — ห้าม hardcode
- SDK ทำ signature + token 2FA อัตโนมัติ (set_token_dir cache token)
- fetch snapshot ทุกแถวเพื่อ recurrence 17 คอลัมน์สมบูรณ์ (LEGO 100%)
- preview กับ place แยกฟังก์ชัน — submit gate ต้องอยู่ระหว่างกลาง (invariant #9)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from lego_one_row import Config

UAT_ENDPOINT = "th-api.uat.webullbroker.com"
PROD_ENDPOINT = "api.webull.co.th"


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
    """guard: จันทร์–ศุกร์ 13:30–20:00 UTC (regular hours)

    ไม่รวมวันหยุด/half-day NYSE — production ต่อปฏิทิน (pandas_market_calendars)
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= minutes < 20 * 60


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

    positions = trade_client.account_v2.get_account_position(account_id).json()
    holdings = _extract_qty(positions, cfg.symbol)

    snap = data_client.market_data.get_snapshot(
        cfg.symbol.upper(), "US_STOCK",
        extend_hour_required=False, overnight_required=False).json()
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
        "quantity": f"{qty:.5f}".rstrip("0").rstrip("."),
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


# ---- helpers (ปรับ path ให้ตรง schema จริงของ SDK response) ----------------
def _extract_qty(positions, symbol: str) -> float:
    if isinstance(positions, list):
        items = positions
    else:
        items = (positions or {}).get("positions", []) or []
    for p in items:
        if str(p.get("symbol", "")).upper() == symbol.upper():
            return float(p.get("quantity", 0) or 0)
    return 0.0


def _extract_price(snap, symbol: str) -> float:
    if isinstance(snap, list):
        snap = snap[0] if snap else {}
    for key in ("last", "lastPrice", "price", "close"):
        if snap.get(key):
            return float(snap[key])
    return 0.0
