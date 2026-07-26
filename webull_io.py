"""Webull OpenAPI adapter: fail-closed parsing, cached clients, token upkeep."""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

from lego_one_row import Config
from lego_orders import PROD, UAT

logger = logging.getLogger(__name__)

UAT_ENDPOINT = "th-api.uat.webullbroker.com"
PROD_ENDPOINT = "api.webull.co.th"
DEFAULT_TOKEN_DIR = "/tmp/webull_token"
TOKEN_FILE = "token.txt"

# 408/429 belong here as much as any 5xx, and are likelier: every call this
# adapter makes sits under a small per-endpoint Webull limit (account 10/30s,
# market data 60/60s, order query 40/2s). Treating a throttled read as a hard
# failure turned a two-second wait into a missed slot.
_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
_TRANSIENT_CODES = {
    "GATEWAY_TIMEOUT", "TIMEOUT", "SERVICE_UNAVAILABLE",
    "RATE_LIMIT", "RATE_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS",
    "REQUEST_LIMIT_EXCEEDED", "FREQUENCY_LIMIT",
}
# Any of these present-and-truthy means the broker rejected the preview.
_PREVIEW_ERROR_KEYS = ("error", "error_code", "errorCode")
# webull/data/common/category.py of the pinned SDK 2.0.15.
CATEGORIES = ("US_STOCK", "US_ETF", "US_OPTION", "US_CRYPTO", "US_FUTURES",
              "US_EVENT", "HK_STOCK", "HK_ETF", "HK_FUTURES", "CN_STOCK")


class MarketDataForbidden(RuntimeError):
    """403 from a market-data call: an entitlement problem, not a bad symbol.

    OpenAPI market data is a separate subscription from the one in the Webull
    app, so this is the one error here that no amount of retrying or code fixing
    resolves. It says so instead of arriving as a bare 403.
    """


def _http_status(exc: Exception) -> int | None:
    status = getattr(exc, "http_status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def is_transient_exception(exc: Exception) -> bool:
    code = str(getattr(exc, "error_code", "") or "").upper()
    return _http_status(exc) in _TRANSIENT_HTTP or code in _TRANSIENT_CODES


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


def market_category() -> str:
    """The Category the snapshot is requested under.

    Hard-coding US_STOCK priced every symbol as a stock. The strategy is usually
    run on leveraged ETFs, and category is a query parameter of the snapshot
    endpoint, so the wrong one is answered by the broker, not by this code.
    Default is unchanged; an unknown value fails closed rather than reaching the
    API as a typo.
    """
    value = os.environ.get("LEGO_MARKET_CATEGORY", "US_STOCK").strip().upper()
    if value not in CATEGORIES:
        raise ValueError(
            f"LEGO_MARKET_CATEGORY={value!r} ไม่อยู่ใน Category ของ SDK — fail closed "
            f"(ค่าที่ใช้ได้: {', '.join(CATEGORIES)})")
    return value


def environment_label() -> str:
    return UAT if os.environ.get("WEBULL_ENV", "UAT").upper() == "UAT" else PROD


def _endpoint() -> str:
    return UAT_ENDPOINT if environment_label() == UAT else PROD_ENDPOINT


def token_dir() -> str:
    return os.environ.get("WEBULL_TOKEN_DIR", DEFAULT_TOKEN_DIR)


def token_file_path() -> str:
    return os.path.join(token_dir(), TOKEN_FILE)


def token_dir_is_ephemeral() -> bool:
    """True when the token cannot survive an instance recycle.

    /tmp is the only writable path on Cloud Functions and also the first thing
    the runtime throws away. The SDK reacts to a missing token by creating a new
    one, which needs a human to approve 2FA in the app within 300 seconds; there
    is nobody to do that on a scheduler, so it ends in ERROR_INIT_TOKEN and the
    bot stops until someone notices.
    """
    return os.path.abspath(token_dir()).startswith("/tmp")


def _expires_datetime(raw) -> datetime | None:
    """The SDK stores `expires` as Unix time; the API documents milliseconds."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e11:                        # milliseconds, per the API docs
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def read_local_token() -> dict | None:
    """The three lines TokenManager writes: token, expires, status."""
    try:
        with open(token_file_path(), "r", encoding="utf-8") as handle:
            token = handle.readline().strip()
            expires = handle.readline().strip()
            status = handle.readline().strip()
    except OSError:
        return None
    if not token:
        return None
    return {"token": token, "expires": expires, "status": status,
            "expires_at": _expires_datetime(expires)}


def _write_local_token(token: str, expires, status: str) -> None:
    path = token_file_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n{expires}\n{status}\n")


def _refresh_margin_days() -> float:
    return float(os.environ.get("LEGO_TOKEN_REFRESH_MARGIN_DAYS", "3"))


def token_health(now: datetime | None = None) -> dict:
    """What the stored token says, without spending a request to ask the broker.

    Purely local so it is free to call on every slot. `ok` is False for anything
    that ends with the bot silently unable to authenticate: no token file, a
    non-NORMAL status, an expiry inside the refresh margin, or a token kept
    somewhere that does not survive the container.
    """
    now = now or datetime.now(timezone.utc)
    info = {
        "token_dir": token_dir(),
        "ephemeral_token_dir": token_dir_is_ephemeral(),
        "found": False,
        "status": None,
        "expires_at": None,
        "days_left": None,
        "ok": True,
        "reasons": [],
    }
    if info["ephemeral_token_dir"]:
        info["ok"] = False
        info["reasons"].append(
            f"token dir {token_dir()} อยู่บน storage ที่หายเมื่อ instance ถูกรีไซเคิล — "
            "ตั้ง WEBULL_TOKEN_DIR ไปยัง volume ที่คงอยู่ (เช่น GCS FUSE mount)")
    local = read_local_token()
    if local is None:
        info["ok"] = False
        info["reasons"].append(
            f"ไม่พบ token file ที่ {token_file_path()} — ครั้งถัดไปจะต้องยืนยัน 2FA ใหม่")
        return info
    info["found"] = True
    info["status"] = local["status"] or None
    expires_at = local["expires_at"]
    if expires_at is None:
        info["ok"] = False
        info["reasons"].append("token file ไม่มีวันหมดอายุที่อ่านได้")
        return info
    days_left = (expires_at - now).total_seconds() / 86400.0
    info["expires_at"] = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    info["days_left"] = round(days_left, 3)
    if local["status"] and local["status"] != "NORMAL":
        info["ok"] = False
        info["reasons"].append(f"token status={local['status']} (ต้องเป็น NORMAL)")
    if days_left <= _refresh_margin_days():
        info["ok"] = False
        info["reasons"].append(f"token เหลืออีก {days_left:.2f} วันก่อนหมดอายุ")
    return info


def ensure_token_fresh(api_client) -> dict:
    """Renew the token before it expires, because nothing in the SDK will.

    /openapi/auth/token/refresh is wrapped by TokenOperation.refresh_token and
    called from nowhere: TokenManager.init_token reads `expires` out of the file
    and discards it. The token simply dies at 15 days, mid-session, and recovery
    needs 2FA that only a human can give. Refreshing while the token is still
    valid keeps that from ever being the failure mode.

    Never raises. A refresh that fails leaves a token that is still good for the
    whole margin, so blocking the slot over it would cause the outage it exists
    to prevent.
    """
    health = token_health()
    out = {"refreshed": False, **health}
    if not health["found"] or health["days_left"] is None:
        return out                       # nothing to refresh from
    if health["days_left"] > _refresh_margin_days():
        return out
    local = read_local_token()
    try:
        from webull.core.http.initializer.token.token_operation import TokenOperation

        response = TokenOperation(api_client).refresh_token(local["token"]).json()
    except Exception as exc:             # noqa: BLE001 - see docstring
        out["refresh_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("webull token refresh failed: %s", out["refresh_error"])
        return out
    token = (response or {}).get("token")
    expires = (response or {}).get("expires")
    if not token or not expires:
        out["refresh_error"] = "refresh response ไม่มี token/expires — เก็บ token เดิมไว้"
        logger.warning("webull token refresh returned an unusable payload")
        return out
    status = str((response or {}).get("status") or "NORMAL")
    try:
        _write_local_token(token, expires, status)
    except OSError as exc:
        out["refresh_error"] = f"เขียน token file ไม่ได้: {exc}"
        logger.warning("webull token refresh could not be stored: %s", exc)
        return out
    api_client.set_token(token)
    # Re-read rather than patch the old answer: a refresh fixes the expiry and
    # nothing else, and an ephemeral token dir is still worth saying out loud.
    out = {"refreshed": True, **token_health()}
    logger.info("webull token refreshed, expires_at=%s", out["expires_at"])
    return out


_CLIENTS: tuple | None = None


def _client_cache_ttl() -> float:
    return float(os.environ.get("LEGO_CLIENT_CACHE_TTL_SECONDS", "3600"))


def reset_clients() -> None:
    """Drop the cached clients so the next call re-authenticates."""
    global _CLIENTS
    _CLIENTS = None


def _sdk_log_level() -> int:
    name = os.environ.get("LEGO_WEBULL_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, name, logging.INFO)


def build_clients():
    """Build (or reuse) the SDK clients for this instance.

    Two things the SDK does at construction time make a fresh pair per
    invocation the wrong default:

    * TradeClient and DataClient each run ClientInitializer.initializer on the
      same ApiClient, and each run is a config call plus a create_token call. One
      build_clients() is therefore four auth requests, and token create is capped
      at 10 per 30 seconds — with the order worker also building clients, a busy
      slot can spend the whole budget on setup.
    * With no logger configured they install a TimedRotatingFileHandler writing
      into the working directory. On Cloud Functions everything outside /tmp is
      read-only, so that is an OSError at construction; where it does succeed it
      adds another pair of handlers to the shared 'webull.core' logger on every
      call. Setting a stream logger first makes the SDK skip that branch and
      sends the same records to stdout, which is what Cloud Logging reads.

    Warm instances therefore reuse one authenticated pair, and the TTL forces a
    rebuild often enough that a long-lived instance never runs on a token whose
    state it has stopped checking.
    """
    global _CLIENTS
    cache_key = (_endpoint(), os.environ["WEBULL_APP_KEY"], token_dir())
    if _CLIENTS is not None:
        key, built_at, trade, data = _CLIENTS
        if key == cache_key and (time.monotonic() - built_at) < _client_cache_ttl():
            return trade, data

    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    from webull.data.data_client import DataClient

    api = ApiClient(os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"], "th")
    api.add_endpoint("th", _endpoint())
    api.set_token_dir(token_dir())
    api.set_stream_logger(log_level=_sdk_log_level(), stream=sys.stdout,
                          format_string="%(asctime)s %(name)s %(levelname)s %(message)s")
    if token_dir_is_ephemeral():
        logger.warning("WEBULL_TOKEN_DIR=%s อยู่บน storage ชั่วคราว — token จะหายเมื่อ "
                       "instance ถูกรีไซเคิลและต้องยืนยัน 2FA ใหม่", token_dir())
    trade, data = TradeClient(api), DataClient(api)
    ensure_token_fresh(api)
    _CLIENTS = (cache_key, time.monotonic(), trade, data)
    return trade, data


def fetch_snapshot(trade_client, data_client, cfg: Config) -> dict:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    positions = _retry_transient(
        lambda: trade_client.account_v2.get_account_position(account_id).json())
    holdings = _extract_qty(positions, cfg.symbol)
    category = market_category()
    try:
        snap = _retry_transient(
            lambda: data_client.market_data.get_snapshot(
                cfg.symbol.upper(), category,
                extend_hour_required=False, overnight_required=False).json())
    except Exception as exc:
        if _http_status(exc) == 403:
            raise MarketDataForbidden(
                "403 จาก market data — OpenAPI ต้องซื้อ subscription (LV1/LV2) แยกจาก "
                "แอป Webull; ตรวจสิทธิ์ที่ /app/subscriptions/list") from exc
        raise
    price = _extract_price(snap, cfg.symbol)
    if not (price and price > 0):
        raise ValueError(f"snapshot price ไม่ถูกต้อง ({price}) — fail closed")
    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": float(price),
        "holdings": float(holdings),
    }


def quantity_string(qty: float, precision: int) -> str:
    """Format an order quantity, stripping trailing zeros only after a point.

    At LEGO_DECIMAL_PRECISION=0 (whole shares — a documented, allowed setting)
    the formatted quantity has no '.' to stop rstrip('0'), so 20 shares became
    '2' and 100 became '1'. Nothing downstream could catch it: the 17-column
    ledger is theoretical and never reads the filled quantity, so the dashboard
    would show a healthy chain while the real position was 10% of target.

    A quantity that rounds away to zero is refused rather than sent as '0'.
    """
    text = f"{qty:.{precision}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or float(text) <= 0:
        raise ValueError(
            f"quantity {qty!r} ปัดที่ {precision} ทศนิยมแล้วเหลือ 0 — fail closed ไม่ส่ง")
    return text


def build_order_payload(cfg: Config, side: str, qty: float, client_order_id: str) -> list[dict]:
    return [{
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": cfg.symbol.upper(),
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": quantity_string(qty, cfg.decimal_precision),
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
    """Send the order exactly once.

    This used to retry on a transient error, which is the one retry the flow
    cannot afford: a timeout or a 502 says nothing about whether the broker
    accepted the order, so re-sending is a blind second attempt at real money.
    The caller records PLACING_UNKNOWN before this call, and the worker resolves
    it by asking for the same client_order_id — that is the sanctioned recovery,
    and it works whether or not the first attempt landed.
    """
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return trade_client.order_v3.place_order(account_id, order).json()


def fetch_order_detail(trade_client, client_order_id: str) -> dict:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return _retry_transient(
        lambda: trade_client.order_v3.get_order_detail(account_id, client_order_id).json())


def _open_order_items(res) -> list:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ("orders", "items", "data"):
            if key in res:
                items = res.get(key) or []
                if not isinstance(items, list):
                    raise ValueError("open-orders items ต้องเป็น list — fail closed")
                return items
    raise ValueError("open-orders response shape ไม่รู้จัก — fail closed")


def _open_order_page_size() -> int:
    return max(1, int(os.environ.get("LEGO_OPEN_ORDER_PAGE_SIZE", "50")))


def _open_order_max_pages() -> int:
    return max(1, int(os.environ.get("LEGO_OPEN_ORDER_MAX_PAGES", "5")))


def fetch_open_orders(trade_client, symbol: str) -> list[dict]:
    """Every open order for *symbol*, following the broker's paging.

    get_order_open answers 10 orders per page by default and the reply is a page,
    not the whole book. The dispatcher uses an empty result to mean 'nothing of
    ours is live at the broker', so ten unrelated orders on the first page were
    enough to hide our own and let a second order go out on top of it.
    """
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    page_size = _open_order_page_size()
    cursor = None
    out: list[dict] = []
    for _ in range(_open_order_max_pages()):
        res = _retry_transient(
            lambda after=cursor: trade_client.order_v3.get_order_open(
                account_id, page_size=page_size, last_client_order_id=after).json())
        items = _open_order_items(res)
        for o in items:
            if not isinstance(o, dict):
                continue
            inner = o.get("items")
            cands = inner if isinstance(inner, list) else [o]
            for c in cands:
                if isinstance(c, dict) and str(c.get("symbol", "")).upper() == symbol.upper():
                    out.append(c)
        if len(items) < page_size:
            break
        last = items[-1] if isinstance(items[-1], dict) else {}
        next_cursor = last.get("client_order_id")
        if not next_cursor or next_cursor == cursor:
            break                        # no usable cursor: stop rather than loop
        cursor = next_cursor
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


_PRICE_KEYS = ("last", "lastPrice", "price", "close")


def _entry_symbol(entry: dict) -> str:
    return str(entry.get("symbol") or "").upper()


def _price_of(entry: dict) -> float:
    for key in _PRICE_KEYS:
        if entry.get(key):
            return float(entry[key])
    return 0.0


def _extract_price(snap, symbol: str) -> float:
    """Read the price of *symbol*, never of whatever came first.

    _extract_qty refuses to answer with another symbol's position; this function
    took the first entry it saw, so a batch response or a mis-routed quote priced
    the row off a different stock. That price goes straight into build_decision,
    where gap = fix_c - holdings*price decides BUY/SELL/PASS and the order size,
    with no later check that could notice.

    An entry that names a different symbol is now skipped. Returning 0.0 is the
    fail-closed answer: fetch_snapshot already turns a non-positive price into a
    ValueError, so the row is never built rather than built on a wrong price.
    """
    want = symbol.upper()
    if isinstance(snap, list):
        entries = [e for e in snap if isinstance(e, dict)]
        matching = [e for e in entries if _entry_symbol(e) == want]
        if matching:
            snap = matching[0]
        elif any(_entry_symbol(e) for e in entries):
            return 0.0                  # the response is about other symbols only
        else:
            # Unnamed single-symbol payload: the historical shape get_snapshot
            # returns for a one-symbol request.
            snap = entries[0] if entries else {}
    if not isinstance(snap, dict):
        return 0.0
    named = _entry_symbol(snap)
    if not named or named == want:
        price = _price_of(snap)
        if price:
            return price
    nested = snap.get(want) or snap.get(symbol)
    if isinstance(nested, dict):
        return _price_of(nested)
    return 0.0
