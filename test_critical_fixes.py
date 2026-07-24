from __future__ import annotations

import pytest

from conftest import FAKE_DB
from lego_one_row import (Anchor, Config, PASS_THRESHOLD, READY_BUY,
                          compute_recurrence, compute_row)
from lego_orders import UAT, apply_fill
import lego_state
from lego_state import (PendingOrderExists, apply_realized_fill, chain_key,
                        clear_pending_order, commit_final_row,
                        get_pending_order, read_anchor)


@pytest.fixture(autouse=True)
def reset_db(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.setenv('LEGO_SLOT_SECONDS', '600')


def test_critical_1_pass_threshold_signal_one_freezes_model_ledger():
    cfg = Config(symbol='AAPL', fix_c=3000.0, diff=5.0, dna_code='bypass:10', decimal_precision=2)
    anchor = Anchor(version=1, dna_step=0, p0=320.64,
                    prev_price=320.64, prev_actual=0.0)
    snap = {'captured_at': '2026-07-23T18:10:05Z',
            'price': 320.87, 'holdings': 9.34492}
    row = compute_row(cfg, snap, anchor)
    assert row['DNA signal'] == 1
    assert row['สถานะ'] == PASS_THRESHOLD
    assert row['จำนวนสั่ง (หุ้น)'] == 0
    assert row['ΔAₙ ต่อสเต็ป (USD)'] == 0
    assert row['Aₙ สะสม (USD)'] == 0
    assert row['_meta']['acted'] is False
    assert row['_meta']['acted_price_next'] == pytest.approx(320.64)


def test_ready_decision_is_the_only_model_act():
    cfg = Config(symbol='AAPL', fix_c=3000.0, diff=5.0, dna_code='bypass:10', decimal_precision=2)
    anchor = Anchor(version=2, dna_step=1, p0=320.64,
                    prev_price=320.64, prev_actual=0.0)
    snap = {'captured_at': '2026-07-23T18:20:14Z',
            'price': 320.32, 'holdings': 9.34492}
    row = compute_row(cfg, snap, anchor)
    assert row['สถานะ'] == READY_BUY
    assert row['_meta']['acted'] is True
    expected = 3000.0 * (320.32 / 320.64 - 1.0)
    assert row['ΔAₙ ต่อสเต็ป (USD)'] == pytest.approx(expected)


def test_compute_recurrence_requires_boolean_acted():
    cfg = Config('AAPL', 3000.0)
    anchor = Anchor(1, 0, 100.0, 100.0, 0.0)
    with pytest.raises(ValueError):
        compute_recurrence(cfg, 101.0, anchor, acted=1)


def test_critical_2_realized_occurs_only_when_fill_legs_close():
    legs, realized1 = apply_fill(None, 'BUY', 2.0, 100.0, fee=1.0)
    assert realized1 == 0.0
    legs, realized2 = apply_fill(legs, 'SELL', 1.5, 110.0, fee=0.75)
    assert realized2 == pytest.approx(13.5)
    assert legs['buys'][0][0] == pytest.approx(0.5)


def test_realized_fill_is_idempotent_and_partial_fill_uses_incremental_price():
    ck = 'AAPL_test'
    first = apply_realized_fill(ck, 'buy-1', 'BUY', 1.0, 100.0, 0.10)
    assert first['realized_cumulative'] == 0.0
    again = apply_realized_fill(ck, 'buy-1', 'BUY', 1.0, 100.0, 0.10)
    assert again['realized_cumulative'] == 0.0
    partial = apply_realized_fill(ck, 'buy-1', 'BUY', 2.0, 105.0, 0.20)
    assert partial['open_legs']['buys'][0][:2] == pytest.approx([1.0, 100.0])
    assert partial['open_legs']['buys'][1][:2] == pytest.approx([1.0, 110.0])
    closed = apply_realized_fill(ck, 'sell-1', 'SELL', 2.0, 120.0, 0.20)
    assert closed['realized_delta'] == pytest.approx(29.6)
    assert closed['realized_cumulative'] == pytest.approx(29.6)
    assert closed['open_legs'] == {'buys': [], 'sells': []}


def _ready_row_and_intent(cfg, captured='2026-07-23T18:20:14Z'):
    snap = {'captured_at': captured, 'price': 320.0, 'holdings': 9.0}
    row = compute_row(cfg, snap, None)
    assert row['สถานะ'] == READY_BUY
    intent = {
        'status': 'PENDING', 'row_status': row['สถานะ'],
        'side': row['_meta']['side'], 'quantity': row['_meta']['quantity'],
        'symbol': cfg.symbol, 'step': row['DNA step'], 'created_at': captured,
    }
    return snap, row, intent


def test_critical_3_order_intent_is_atomic_with_state_advance():
    cfg = Config('AAPL', 3000.0, diff=5.0, dna_code='bypass:10', decimal_precision=2)
    snap, row, intent = _ready_row_and_intent(cfg)
    result = commit_final_row(cfg, snap, None, row, order_intent=intent)
    assert result['committed'] is True
    pending = get_pending_order(cfg)
    assert pending is not None
    assert pending['run_id'] == result['run_id']
    state = FAKE_DB.reference(f'{lego_state.STATE_PATH}/{chain_key(cfg)}').get()
    assert state['version'] == 1
    assert state['pending_order']['status'] == 'PENDING'
    persisted_row = FAKE_DB.reference(f'{lego_state.ROWS_PATH}/{result["run_id"]}').get()
    assert persisted_row['committed'] is True
    assert 'pending_order' not in persisted_row


def test_pending_order_survives_restart_and_can_be_cleared():
    cfg = Config('AAPL', 3000.0, diff=5.0, dna_code='bypass:10', decimal_precision=2)
    snap, row, intent = _ready_row_and_intent(cfg)
    result = commit_final_row(cfg, snap, None, row, order_intent=intent)
    pending = get_pending_order(cfg)
    assert pending['client_order_id'] == result['run_id']
    clear_pending_order(cfg, result['run_id'])
    assert get_pending_order(cfg) is None
    assert read_anchor(cfg).version == 1


def test_new_order_intent_cannot_overwrite_unresolved_pending():
    cfg = Config('AAPL', 3000.0, diff=5.0, dna_code='bypass:10', decimal_precision=2)
    snap1, row1, intent1 = _ready_row_and_intent(cfg)
    commit_final_row(cfg, snap1, None, row1, order_intent=intent1)
    anchor = read_anchor(cfg)
    snap2 = {'captured_at': '2026-07-23T18:30:14Z', 'price': 319.0, 'holdings': 9.0}
    row2 = compute_row(cfg, snap2, anchor)
    intent2 = {'status': 'PENDING', 'row_status': row2['สถานะ'],
               'side': row2['_meta']['side'], 'quantity': row2['_meta']['quantity'],
               'symbol': cfg.symbol, 'step': row2['DNA step'],
               'created_at': snap2['captured_at']}
    with pytest.raises(PendingOrderExists):
        commit_final_row(cfg, snap2, anchor, row2, order_intent=intent2)


def test_17_column_contract_is_preserved():
    cfg = Config('AAPL', 3000.0, diff=5.0, dna_code='bypass:10', decimal_precision=2)
    _, row, _ = _ready_row_and_intent(cfg)
    assert len([k for k in row if k != '_meta']) == 17


def _pending_fixture():
    return {
        'run_id': 'rid-1', 'client_order_id': 'rid-1', 'chain_key': 'AAPL_x',
        'row_status': READY_BUY, 'side': 'BUY', 'quantity': 1.0,
        'symbol': 'AAPL', 'step': 3, 'created_at': '2026-07-23T18:20:14Z',
        'status': 'PENDING',
    }


def test_critical_4_pre_place_error_returns_503_and_keeps_retryable(monkeypatch):
    import main
    cfg = Config('AAPL', 3000.0, diff=5.0, decimal_precision=2)
    pending = _pending_fixture()
    monkeypatch.setattr(main, 'environment_label', lambda: UAT)
    monkeypatch.setattr(main, 'fetch_open_orders', lambda *_: [])
    monkeypatch.setattr(main, 'preview_market_order',
                        lambda *_: (_ for _ in ()).throw(RuntimeError('preview down')))
    monkeypatch.setattr(main, 'update_pending_order', lambda *args, **kwargs: None)
    monkeypatch.setattr(main, 'update_order_audit', lambda *args, **kwargs: None)
    monkeypatch.setattr(main, 'write_order_audit', lambda *args, **kwargs: None)
    out, code = main._dispatch_pending(object(), cfg, pending)
    assert code == 503
    assert out['pipeline_status'] == 'ORDER_RETRY_REQUIRED'
    assert out['phase'] == 'PRE_PLACE'


def test_critical_4_terminal_order_returns_200_with_explicit_pipeline_status(monkeypatch):
    import main
    cfg = Config('AAPL', 3000.0, diff=5.0, decimal_precision=2)
    pending = _pending_fixture()
    monkeypatch.setattr(main, 'environment_label', lambda: UAT)
    monkeypatch.setattr(main, 'fetch_open_orders', lambda *_: [])
    monkeypatch.setattr(main, 'preview_market_order', lambda *_: True)
    monkeypatch.setattr(main, 'place_market_order', lambda *_: {'client_order_id': 'rid-1'})
    monkeypatch.setattr(main, '_poll_order_status',
                        lambda *_: {'status': 'FILLED', 'realized': True,
                                    'filled_quantity': '1', 'filled_price': 100.0})
    monkeypatch.setattr(main, '_apply_realized_if_available', lambda cfg, p, s: s)
    monkeypatch.setattr(main, '_persist_order_summary', lambda *args, **kwargs: None)
    monkeypatch.setattr(main, 'update_pending_order', lambda *args, **kwargs: None)
    monkeypatch.setattr(main, 'update_order_audit', lambda *args, **kwargs: None)
    monkeypatch.setattr(main, 'write_order_audit', lambda *args, **kwargs: None)
    out, code = main._dispatch_pending(object(), cfg, pending)
    assert code == 200
    assert out['pipeline_status'] == 'ROW_COMMITTED_ORDER_TERMINAL'
