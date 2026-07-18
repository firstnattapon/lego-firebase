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


# ---- retry เฉพาะ error ชั่วคราว (5xx/timeout) — budget-aware ใต้ Cloud Run 120s ----
def _retry_attempts() -> int:
    return max(1, int(os.environ.get("WEBULL_RETRY_ATTEMPTS", "2")))


def _retry_base_sleep() -> float:
    return max(0.0, float(os.environ.get("WEBULL_RETRY_BASE_SLEEP", "0.5")))


def _retry_deadline() -> float:
    return max(0.0, float(os.environ.get("WEBULL_RETRY_DEADLINE", "60")))


def _is_transient(exc: Exception) -> bool:
    """True เฉพาะ 5xx / gateway-timeout / timeout / connection (retry แล้วมีโอกาสหาย).

    4xx / INVALID_TOKEN / 403 = ไม่ transient -> raise ทันที (retry ไม่ช่วย, fail closed)
    ตรวจทั้ง attribute (SDK บางตัวมี) และ str(exc) (Webull ServerException ฝัง status ในข้อความ)
    """
    for attr in ("status_code", "http_status", "status", "code"):
        val = getattr(exc, attr, None)
        try:
            if val is not None and 500 <= int(val) < 600:
                return True
        except (TypeError, ValueError):
            pass
    msg = str(exc)
    if "INVALID_TOKEN" in msg or "HTTP Status: 4" in msg:
        return False
    markers = ("HTTP Status: 5", "GATEWAY_TIMEOUT", "timed out", "Timeout",
               "Connection", "Read timed out", "Max retries")
    return any(m in msg for m in markers)


def _call_with_retry(fn, *args, deadline_start: float | None = None, **kwargs):
    """เรียก fn พร้อม retry แบบ exponential backoff เฉพาะ transient; ปิดที่ deadline กัน timeout ชน"""
    deadline_start = time.monotonic() if deadline_start is None else deadline_start
    attempts = _retry_attempts()
    base = _retry_base_sleep()
    deadline = _retry_deadline()
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — จำแนกด้วย _is_transient
            last = exc
            elapsed = time.monotonic() - deadline_start
            if (attempt >= attempts or not _is_transient(exc)
                    or elapsed >= deadline):
                raise
            sleep_s = base * (2 ** (attempt - 1))
            if elapsed + sleep_s >= deadline:   # จะเลย deadline หลัง sleep -> เลิก
                raise
            time.sleep(sleep_s)
    if last is not None:                        # unreachable (กันเหนียว)
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
    """guard: จันทร์–ศุกร์ 9:30–16:00 America/New_York (DST-aware regular hours)

    กรอบ UTC คงที่ 13:30–20:00 ถูกเฉพาะฤดูร้อน (EDT) — ฤดูหนาว (EST) ตลาดจริงคือ
    14:30–21:00 UTC จึงต้องเทียบเวลา New York ตรง ๆ; ถ้าเครื่องไม่มี tz database
    ให้ fallback กรอบ UTC เดิม (ยอมกว้าง/แคบผิดฤดู ดีกว่า crash ทั้งรอบ)
    ไม่รวมวันหยุด/half-day NYSE — production ต่อปฏิทิน (pandas_market_calendars)
    """
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et = None
    if et is not None:
        if et.weekday() >= 5:
            return False
        minutes = et.hour * 60 + et.minute
        return 9 * 60 + 30 <= minutes < 16 * 60
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


def fetch_snapshot(trade_client, data_client, cfg: Config,
                   fallback_holdings: float | None = None) -> dict:
    """คืน {captured_at, price, holdings}; fail closed ถ้า price <= 0

    - ดึงราคา (market-data) ก่อน แล้วค่อย positions (trade endpoint) — decouple
      ให้ trade endpoint ที่ flaky ไม่บล็อก market-data path
    - ทั้งสอง call หุ้มด้วย retry เฉพาะ transient (504/timeout)
    - positions ล้ม transient หลังครบ retry + มี fallback_holdings -> ใช้ค่า holdings
      ล่าสุด (จาก state) แทน ให้รอบไม่ตายเพราะ trade endpoint outage ชั่วคราว
    """
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    start = time.monotonic()

    # 1) ราคา (market-data) — จำเป็น, ล้ม = fail closed
    snap = _call_with_retry(
        lambda: data_client.market_data.get_snapshot(
            cfg.symbol.upper(), "US_STOCK",
            extend_hour_required=False, overnight_required=False).json(),
        deadline_start=start)
    price = _extract_price(snap, cfg.symbol)
    if not (price and price > 0):
        raise ValueError(f"snapshot price ไม่ถูกต้อง ({price}) — fail closed")

    # 2) positions (trade endpoint) — transient outage + มี fallback -> ใช้ค่าเก่า
    try:
        positions = _call_with_retry(
            lambda: trade_client.account_v2.get_account_position(account_id).json(),
            deadline_start=start)
        holdings = _extract_qty(positions, cfg.symbol)
    except Exception as exc:  # noqa: BLE001
        if not (_is_transient(exc) and fallback_holdings is not None):
            raise
        _log_positions_fallback(cfg.symbol, fallback_holdings, exc)
        holdings = float(fallback_holdings)

    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": float(price),
        "holdings": float(holdings),
    }


def _log_positions_fallback(symbol: str, holdings: float, exc: Exception) -> None:
    """best-effort log ว่าใช้ holdings fallback (trade อย่าล้มเพราะ log ล้ม)"""
    try:
        from firebase_admin import db
        db.reference("webull_lego_errors").push({
            "error": f"get_account_position transient — ใช้ fallback_holdings={holdings}: {exc}",
            "type": "PositionsFallback", "symbol": symbol,
        })
    except Exception:
        pass


def build_order_payload(cfg: Config, side: str, qty: float, client_order_id: str) -> list[dict]:
    # format ตาม cfg.decimal_precision (ฮาร์ดโค้ด 5 จะตัดทศนิยมเมื่อ dp > 5);
    # strip ศูนย์ท้ายเฉพาะเมื่อมีจุดทศนิยม (dp=0 เช่น "20" ห้ามโดน strip เหลือ "2")
    qty_str = f"{qty:.{cfg.decimal_precision}f}"
    if "." in qty_str:
        qty_str = qty_str.rstrip("0").rstrip(".")
    return [{
        "combo_type": "NORMAL", "client_order_id": client_order_id,
        "symbol": cfg.symbol.upper(), "instrument_type": "EQUITY", "market": "US",
        "order_type": "MARKET",
        "quantity": qty_str,
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
