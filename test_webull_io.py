"""The broker adapter, exercised through SDK doubles.

webull_io is the only module that touches real money and it was the least
covered one, because every call needed a TradeClient/DataClient the tests could
not build. conftest supplies those doubles now, so the shape-parsing and
fail-closed branches are testable like everything else.
"""
from __future__ import annotations

import logging
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import webull_io
from conftest import FakeNamespace, fake_data_client, fake_trade_client
from lego_one_row import Config
from webull_io import (_extract_price, fetch_open_orders, fetch_order_detail,
                       fetch_snapshot, place_market_order, preview_market_order)

CFG = Config(symbol="FFWM", fix_c=1000.0, decimal_precision=2)


@pytest.fixture(autouse=True)
def account(monkeypatch):
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("WEBULL_ENV", "UAT")


# ---- _extract_price: the price must belong to the symbol we asked for -------

def test_price_of_another_symbol_is_refused():
    """A quote for TSLA must never price an FFWM row."""
    assert _extract_price([{"symbol": "TSLA", "last": 420.0}], "FFWM") == 0.0
    assert _extract_price({"symbol": "TSLA", "last": 420.0}, "FFWM") == 0.0


def test_price_is_picked_out_of_a_batch_by_symbol():
    snap = [{"symbol": "TSLA", "last": 420.0}, {"symbol": "FFWM", "last": 12.5}]
    assert _extract_price(snap, "ffwm") == 12.5


def test_unnamed_single_symbol_payload_still_works():
    """The ordinary get_snapshot answer carries no symbol field; it is ours."""
    assert _extract_price([{"last": 12.5}], "FFWM") == 12.5
    assert _extract_price({"close": 11.0}, "FFWM") == 11.0
    assert _extract_price({"lastPrice": "9.5"}, "FFWM") == 9.5
    assert _extract_price({"price": 8.25}, "FFWM") == 8.25


def test_nested_by_symbol_shape_still_works():
    assert _extract_price({"FFWM": {"last": 7.75}}, "FFWM") == 7.75
    assert _extract_price({"symbol": "TSLA", "FFWM": {"last": 7.75}}, "FFWM") == 7.75


def test_unusable_price_shapes_return_zero():
    assert _extract_price([], "FFWM") == 0.0
    assert _extract_price(None, "FFWM") == 0.0
    assert _extract_price({"volume": 100}, "FFWM") == 0.0


# ---- fetch_snapshot: fail closed rather than trade on a wrong price ---------

def test_fetch_snapshot_reads_price_and_position():
    trade = fake_trade_client(positions={"positions": [{"symbol": "FFWM", "quantity": "3"}]})
    data = fake_data_client(snapshot=[{"symbol": "FFWM", "last": 12.5}])
    snap = fetch_snapshot(trade, data, CFG)
    assert (snap["price"], snap["holdings"]) == (12.5, 3.0)
    assert snap["captured_at"].endswith("Z")


def test_fetch_snapshot_refuses_a_foreign_price():
    trade = fake_trade_client(positions={"positions": []})
    data = fake_data_client(snapshot=[{"symbol": "TSLA", "last": 420.0}])
    with pytest.raises(ValueError, match="fail closed"):
        fetch_snapshot(trade, data, CFG)


# ---- open orders: every unknown shape stops the dispatch --------------------

def test_open_orders_shapes():
    for payload in ({"orders": [{"symbol": "FFWM", "id": 1}]},
                    {"items": [{"symbol": "FFWM", "id": 1}]},
                    {"data": [{"symbol": "FFWM", "id": 1}]},
                    [{"symbol": "FFWM", "id": 1}]):
        client = fake_trade_client(open_orders=payload)
        assert fetch_open_orders(client, "FFWM") == [{"symbol": "FFWM", "id": 1}]


def test_open_orders_filters_by_symbol_and_flattens_groups():
    client = fake_trade_client(open_orders={"orders": [
        {"items": [{"symbol": "TSLA", "id": 1}, {"symbol": "FFWM", "id": 2}]},
        {"symbol": "FFWM", "id": 3},
        "junk",
    ]})
    assert [o["id"] for o in fetch_open_orders(client, "FFWM")] == [2, 3]


@pytest.mark.parametrize("payload", [{"unexpected": []}, "text", {"orders": "nope"}])
def test_open_orders_unknown_shape_fails_closed(payload):
    with pytest.raises(ValueError, match="fail closed"):
        fetch_open_orders(fake_trade_client(open_orders=payload), "FFWM")


# ---- preview: anything that smells like a rejection blocks the order --------

@pytest.mark.parametrize("payload,expected", [
    ({"ok": True}, True),
    ([{"ok": True}], True),
    ({"error": "no buying power"}, False),
    ({"error_code": "RISK"}, False),
    ([{"errorCode": 42}], False),
    ({}, False),
    ([], False),
    (None, False),
    ("text", False),
])
def test_preview_rejects_error_payloads(payload, expected):
    assert preview_market_order(fake_trade_client(preview=payload), []) is expected


def test_open_orders_follow_the_broker_paging(monkeypatch):
    """A full page is a page, not the whole book — our own order may be on page 2."""
    monkeypatch.setenv("LEGO_OPEN_ORDER_PAGE_SIZE", "2")
    pages = [
        {"orders": [{"symbol": "TSLA", "client_order_id": "a"},
                    {"symbol": "TSLA", "client_order_id": "b"}]},
        {"orders": [{"symbol": "FFWM", "client_order_id": "c"}]},
    ]
    client = fake_trade_client(open_orders=lambda *a, **k: pages.pop(0))
    assert fetch_open_orders(client, "FFWM") == [{"symbol": "FFWM", "client_order_id": "c"}]
    assert client.order_v3.get_order_open.calls[1][1]["last_client_order_id"] == "b"


def test_open_order_paging_stops_without_a_usable_cursor(monkeypatch):
    """A full page whose entries carry no id must end the walk, not repeat it."""
    monkeypatch.setenv("LEGO_OPEN_ORDER_PAGE_SIZE", "2")
    client = fake_trade_client(open_orders={"orders": [{"symbol": "TSLA"}, {"symbol": "TSLA"}]})
    assert fetch_open_orders(client, "FFWM") == []
    assert len(client.order_v3.get_order_open.calls) == 1


def test_open_order_paging_is_bounded(monkeypatch):
    monkeypatch.setenv("LEGO_OPEN_ORDER_PAGE_SIZE", "1")
    monkeypatch.setenv("LEGO_OPEN_ORDER_MAX_PAGES", "3")
    seq = {"n": 0}

    def page(*args, **kwargs):
        seq["n"] += 1
        return {"orders": [{"symbol": "TSLA", "client_order_id": f"id-{seq['n']}"}]}

    client = fake_trade_client(open_orders=page)
    assert fetch_open_orders(client, "FFWM") == []
    assert len(client.order_v3.get_order_open.calls) == 3


def test_place_and_detail_pass_the_account_and_payload_through():
    client = fake_trade_client(place={"order_status": "FILLED"},
                               order_detail={"order_status": "FILLED"})
    assert place_market_order(client, [{"client_order_id": "x"}]) == {"order_status": "FILLED"}
    assert client.order_v3.place_order.calls[0][0] == ("acc-1", [{"client_order_id": "x"}])
    assert fetch_order_detail(client, "x") == {"order_status": "FILLED"}
    assert client.order_v3.get_order_detail.calls[0][0] == ("acc-1", "x")


# ---- transient retries ------------------------------------------------------

class _Down(Exception):
    http_status = 503


def test_transient_broker_error_is_retried_by_the_adapter(monkeypatch):
    monkeypatch.setattr(webull_io.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _Down("gateway")
        return {"positions": [{"symbol": "FFWM", "quantity": 2}]}

    trade = fake_trade_client(positions=flaky)
    data = fake_data_client(snapshot={"last": 10.0})
    assert fetch_snapshot(trade, data, CFG)["holdings"] == 2.0
    assert attempts["n"] == 3


def test_permanent_broker_error_is_not_retried():
    client = fake_trade_client(open_orders=ValueError("bad request"))
    with pytest.raises(ValueError, match="bad request"):
        fetch_open_orders(client, "FFWM")
    assert len(client.order_v3.get_order_open.calls) == 1


class _Throttled(Exception):
    http_status = 429


def test_rate_limiting_counts_as_transient():
    """Every endpoint here has a small per-minute cap; a throttle is a wait."""
    assert webull_io.is_transient_exception(_Throttled("slow down")) is True
    assert webull_io.is_transient_exception(_Down("gateway")) is True
    assert webull_io.is_transient_exception(ValueError("bad request")) is False


def test_a_placed_order_is_never_sent_twice():
    """A transient failure says nothing about whether the broker took the order."""
    client = fake_trade_client(place=_Down("gateway"))
    with pytest.raises(_Down):
        place_market_order(client, [{"client_order_id": "x"}])
    assert len(client.order_v3.place_order.calls) == 1


# ---- market data entitlement -----------------------------------------------

class _Forbidden(Exception):
    http_status = 403


def test_market_data_403_is_named_for_what_it_is():
    trade = fake_trade_client(positions={"positions": []})
    data = fake_data_client(snapshot=_Forbidden("forbidden"))
    with pytest.raises(webull_io.MarketDataForbidden, match="subscription"):
        fetch_snapshot(trade, data, CFG)


def test_the_snapshot_category_is_configurable_and_validated(monkeypatch):
    trade = fake_trade_client(positions={"positions": []})
    data = fake_data_client(snapshot=[{"symbol": "FFWM", "last": 12.5}])
    monkeypatch.setenv("LEGO_MARKET_CATEGORY", "us_etf")
    fetch_snapshot(trade, data, CFG)
    assert data.market_data.get_snapshot.calls[0][0][1] == "US_ETF"

    monkeypatch.setenv("LEGO_MARKET_CATEGORY", "US_STONK")
    with pytest.raises(ValueError, match="fail closed"):
        fetch_snapshot(trade, data, CFG)


def test_the_default_category_is_unchanged(monkeypatch):
    monkeypatch.delenv("LEGO_MARKET_CATEGORY", raising=False)
    assert webull_io.market_category() == "US_STOCK"


# ---- token lifecycle: the SDK renews nothing --------------------------------

def _write_token(tmp_path, monkeypatch, token: str, expires_at: datetime,
                 status: str = "NORMAL"):
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(tmp_path))
    # pytest's tmp_path lives under /tmp, which is exactly what the ephemeral
    # check refuses; that condition has its own test and would mask these.
    monkeypatch.setattr(webull_io, "token_dir_is_ephemeral", lambda: False)
    (tmp_path / "token.txt").write_text(
        f"{token}\n{int(expires_at.timestamp() * 1000)}\n{status}\n", encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_token_health_reads_the_file_the_sdk_writes(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch, "tok", _now() + timedelta(days=9))
    health = webull_io.token_health()
    assert health["ok"] is True and health["found"] is True
    assert 8.9 < health["days_left"] < 9.1


@pytest.mark.parametrize("days,status,reason", [
    (1, "NORMAL", "หมดอายุ"),
    (9, "EXPIRED", "NORMAL"),
])
def test_token_health_flags_what_will_stop_the_chain(tmp_path, monkeypatch,
                                                     days, status, reason):
    _write_token(tmp_path, monkeypatch, "tok", _now() + timedelta(days=days), status)
    health = webull_io.token_health()
    assert health["ok"] is False
    assert any(reason in text for text in health["reasons"])


def test_a_missing_token_file_is_not_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(tmp_path))
    health = webull_io.token_health()
    assert health["ok"] is False and health["found"] is False


def test_an_ephemeral_token_dir_is_reported(monkeypatch):
    monkeypatch.setenv("WEBULL_TOKEN_DIR", "/tmp/webull_token")
    assert webull_io.token_dir_is_ephemeral() is True
    assert any("รีไซเคิล" in text for text in webull_io.token_health()["reasons"])


class _FakeApi:
    def __init__(self):
        self.token = None

    def set_token(self, token):
        self.token = token


def _install_fake_token_operation(monkeypatch, payload):
    calls = []

    class TokenOperation:
        def __init__(self, api_client):
            self.api_client = api_client

        def refresh_token(self, token):
            calls.append(token)
            if isinstance(payload, Exception):
                raise payload
            return FakeNamespace(json=lambda: payload)

    module = FakeNamespace(TokenOperation=TokenOperation)
    monkeypatch.setitem(
        sys.modules, "webull.core.http.initializer.token.token_operation", module)
    for name in ("webull", "webull.core", "webull.core.http",
                 "webull.core.http.initializer", "webull.core.http.initializer.token"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(
        sys.modules, "webull.core.http.initializer.token.token_operation", module)
    return calls


def test_a_token_near_expiry_is_refreshed_before_it_dies(tmp_path, monkeypatch):
    """Nothing in the SDK calls token/refresh; a 15-day token just stops working."""
    _write_token(tmp_path, monkeypatch, "old", _now() + timedelta(days=1))
    new_expiry = int((_now() + timedelta(days=15)).timestamp() * 1000)
    calls = _install_fake_token_operation(
        monkeypatch, {"token": "new", "expires": new_expiry, "status": "NORMAL"})

    api = _FakeApi()
    out = webull_io.ensure_token_fresh(api)
    assert out["refreshed"] is True and out["ok"] is True
    assert calls == ["old"] and api.token == "new"
    assert webull_io.read_local_token()["token"] == "new"


def test_a_healthy_token_is_left_alone(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch, "tok", _now() + timedelta(days=10))
    calls = _install_fake_token_operation(monkeypatch, {"token": "new", "expires": 1})
    assert webull_io.ensure_token_fresh(_FakeApi())["refreshed"] is False
    assert calls == []


def test_a_failed_refresh_keeps_the_still_valid_token(tmp_path, monkeypatch):
    """The old token has days left; blocking the slot would cause the outage."""
    _write_token(tmp_path, monkeypatch, "old", _now() + timedelta(days=1))
    _install_fake_token_operation(monkeypatch, RuntimeError("network down"))
    out = webull_io.ensure_token_fresh(_FakeApi())
    assert out["refreshed"] is False and "network down" in out["refresh_error"]
    assert webull_io.read_local_token()["token"] == "old"


def test_an_unusable_refresh_payload_is_not_written(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch, "old", _now() + timedelta(days=1))
    _install_fake_token_operation(monkeypatch, {"status": "NORMAL"})
    out = webull_io.ensure_token_fresh(_FakeApi())
    assert out["refreshed"] is False and "refresh_error" in out
    assert webull_io.read_local_token()["token"] == "old"


# ---- client construction ----------------------------------------------------

def _install_fake_sdk(monkeypatch):
    """Stand in for the SDK package tree build_clients imports lazily."""
    built = {"clients": 0}

    class ApiClient:
        def __init__(self, key, secret, region):
            built["credentials"] = (key, secret, region)
            built["clients"] += 1

        def add_endpoint(self, region, endpoint):
            built["endpoint"] = (region, endpoint)

        def set_token_dir(self, token_dir):
            built["token_dir"] = token_dir

        def set_stream_logger(self, log_level=None, stream=None, format_string=None):
            built["stream_logger"] = log_level

        def set_file_logger(self, *args, **kwargs):        # pragma: no cover
            raise AssertionError("the SDK file logger must never be reached")

        def set_token(self, token):
            built["token"] = token

    modules = {
        "webull": types.ModuleType("webull"),
        "webull.core": types.ModuleType("webull.core"),
        "webull.core.client": FakeNamespace(ApiClient=ApiClient),
        "webull.trade": types.ModuleType("webull.trade"),
        "webull.trade.trade_client": FakeNamespace(TradeClient=lambda api: ("trade", api)),
        "webull.data": types.ModuleType("webull.data"),
        "webull.data.data_client": FakeNamespace(DataClient=lambda api: ("data", api)),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return built


@pytest.fixture(autouse=True)
def fresh_client_cache():
    """Clients are cached per instance; no test may inherit another's."""
    webull_io.reset_clients()
    yield
    webull_io.reset_clients()


def test_build_clients_targets_uat_by_default(monkeypatch):
    built = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")
    monkeypatch.delenv("WEBULL_TOKEN_DIR", raising=False)

    trade, data = webull_io.build_clients()
    assert (trade[0], data[0]) == ("trade", "data")
    assert built["credentials"] == ("key", "secret", "th")
    assert built["endpoint"] == ("th", webull_io.UAT_ENDPOINT)
    assert built["token_dir"] == "/tmp/webull_token"


def test_build_clients_targets_production_when_asked(monkeypatch):
    built = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")
    monkeypatch.setenv("WEBULL_ENV", "PROD")
    monkeypatch.setenv("WEBULL_TOKEN_DIR", "/tmp/other")

    webull_io.build_clients()
    assert built["endpoint"] == ("th", webull_io.PROD_ENDPOINT)
    assert built["token_dir"] == "/tmp/other"


def test_a_stream_logger_is_installed_before_the_clients_are_built(monkeypatch):
    """Otherwise the SDK writes a rotating log file into a read-only directory."""
    built = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")

    webull_io.build_clients()
    assert built["stream_logger"] == logging.INFO


def test_warm_instances_reuse_one_authenticated_client_pair(monkeypatch):
    """Each build is two token-create calls, against a limit of 10 per 30s."""
    built = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")

    first = webull_io.build_clients()
    second = webull_io.build_clients()
    assert first is not None and second[0] is first[0] and second[1] is first[1]
    assert built["clients"] == 1


def test_the_cache_is_dropped_when_the_environment_moves(monkeypatch):
    built = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")
    webull_io.build_clients()

    monkeypatch.setenv("WEBULL_ENV", "PROD")
    webull_io.build_clients()
    assert built["clients"] == 2
    assert built["endpoint"] == ("th", webull_io.PROD_ENDPOINT)


def test_the_cache_expires_so_a_long_lived_instance_reauthenticates(monkeypatch):
    built = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")
    monkeypatch.setenv("LEGO_CLIENT_CACHE_TTL_SECONDS", "60")
    clock = {"t": 0.0}
    monkeypatch.setattr(webull_io.time, "monotonic", lambda: clock["t"])

    webull_io.build_clients()
    clock["t"] = 59.0
    webull_io.build_clients()
    assert built["clients"] == 1
    clock["t"] = 61.0
    webull_io.build_clients()
    assert built["clients"] == 2


# ---- the pin that keeps the money path reproducible ------------------------

def test_broker_sdk_stays_pinned():
    """An unplanned SDK upgrade changes signing and order payloads at deploy time."""
    lines = [line.strip() for line in
             open("requirements.txt", encoding="utf-8").read().splitlines()
             if line.strip() and not line.startswith("#")]
    sdk = [line for line in lines if line.startswith("webull-openapi-python-sdk")]
    assert sdk and "==" in sdk[0], "webull-openapi-python-sdk ต้อง pin เป็น exact version"
