"""The broker adapter, exercised through SDK doubles.

webull_io is the only module that touches real money and it was the least
covered one, because every call needed a TradeClient/DataClient the tests could
not build. conftest supplies those doubles now, so the shape-parsing and
fail-closed branches are testable like everything else.
"""
from __future__ import annotations

import sys
import types

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


# ---- client construction ----------------------------------------------------

def _install_fake_sdk(monkeypatch):
    """Stand in for the SDK package tree build_clients imports lazily."""
    built = {}

    class ApiClient:
        def __init__(self, key, secret, region):
            built["credentials"] = (key, secret, region)

        def add_endpoint(self, region, endpoint):
            built["endpoint"] = (region, endpoint)

        def set_token_dir(self, token_dir):
            built["token_dir"] = token_dir

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


# ---- the pin that keeps the money path reproducible ------------------------

def test_broker_sdk_stays_pinned():
    """An unplanned SDK upgrade changes signing and order payloads at deploy time."""
    lines = [line.strip() for line in
             open("requirements.txt", encoding="utf-8").read().splitlines()
             if line.strip() and not line.startswith("#")]
    sdk = [line for line in lines if line.startswith("webull-openapi-python-sdk")]
    assert sdk and "==" in sdk[0], "webull-openapi-python-sdk ต้อง pin เป็น exact version"
