"""RTDB persistence, durable pending-order outbox, and realized fill ledger."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from firebase_admin import db

from lego_one_row import Anchor, Config, validate_row_columns
from lego_orders import apply_fill, normalize_status

ROWS_PATH = "webull_lego_rows"
STATE_PATH = "webull_lego_state"
AUDIT_PATH = "webull_lego_order_audit"
REALIZED_PATH = "webull_lego_realized"
CASHFLOW_SEMANTICS = "gated_theoretical_v2"

ORDER_TERMINAL = {"FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED", "NOT_PLACED"}


class StaleAnchorError(RuntimeError):
    pass


class SlotAlreadyConsumed(RuntimeError):
    pass


class PendingOrderExists(RuntimeError):
    pass


class _Idempotent(Exception):
    pass


def config_hash(cfg: Config) -> str:
    payload = json.dumps(
        {"s": cfg.strategy_id, "sym": cfg.symbol, "fix": cfg.fix_c,
         "diff": cfg.diff, "dp": cfg.decimal_precision, "dna": cfg.dna_code},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def chain_key(cfg: Config) -> str:
    return f"{cfg.symbol}_{config_hash(cfg)}"


def make_run_id(ck: str, anchor_version: int | None, snapshot: dict) -> str:
    raw = (f"{ck}|{anchor_version}|{snapshot['captured_at']}|"
           f"{snapshot['price']}|{snapshot.get('holdings', 0)}")
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _slot_of(captured_at: str) -> int | None:
    sec = int(os.environ.get("LEGO_SLOT_SECONDS", "0"))
    if sec <= 0:
        return None
    dt = datetime.strptime(captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) // sec


def read_anchor(cfg: Config) -> Anchor | None:
    state = db.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    if not state:
        return None
    ph = state.get("prev_holdings")
    same_semantics = state.get("cashflow_semantics") == CASHFLOW_SEMANTICS
    return Anchor(
        version=int(state["version"]),
        dna_step=int(state["dna_step"]),
        p0=float(state["p0"]),
        prev_price=float(state["prev_price"]),
        prev_actual=float(state["prev_actual"]) if same_semantics else 0.0,
        prev_holdings=None if ph is None else float(ph),
    )


def _repair_pending_row(state: dict | None) -> None:
    if not state:
        return
    rid = state.get("last_run_id")
    if not rid:
        return
    ref = db.reference(f"{ROWS_PATH}/{rid}")
    doc = ref.get()
    if doc is not None and doc.get("committed") is False:
        ref.update({"committed": True})


def _is_pending_order(order: dict | None) -> bool:
    if not isinstance(order, dict):
        return False
    return normalize_status(order.get("status")) not in ORDER_TERMINAL


def commit_final_row(cfg: Config, snapshot: dict, anchor: Anchor | None,
                     row: dict, order_intent: dict | None = None) -> dict:
    """Commit row and state pointer.

    The pending order intent is stored inside the same state transaction that
    advances the DNA pointer. Therefore a crash after commit cannot lose the
    order intent. The row remains exactly the original 17 columns plus metadata.
    """
    validate_row_columns(row)
    ck = chain_key(cfg)
    anchor_version = None if anchor is None else anchor.version
    run_id = make_run_id(ck, anchor_version, snapshot)
    expected_version = 1 if anchor is None else anchor.version + 1
    slot = _slot_of(snapshot["captured_at"])
    row_ref = db.reference(f"{ROWS_PATH}/{run_id}")
    state_ref = db.reference(f"{STATE_PATH}/{ck}")
    meta = row["_meta"]
    _repair_pending_row(state_ref.get())

    existing = row_ref.get()
    if existing is not None and existing.get("committed"):
        return {"committed": False, "idempotent": True, "run_id": run_id,
                "version": existing.get("version")}

    doc = {k: v for k, v in row.items() if k != "_meta"}
    doc.update({
        "run_id": run_id,
        "chain_key": ck,
        "version": expected_version,
        "committed": False,
        "semantics": CASHFLOW_SEMANTICS,
    })
    row_ref.set(doc)

    if order_intent is not None:
        order_intent = dict(order_intent)
        order_intent.update({
            "run_id": run_id,
            "client_order_id": run_id,
            "chain_key": ck,
            "status": normalize_status(order_intent.get("status") or "PENDING"),
            "created_at": snapshot["captured_at"],
        })

    def txn(current):
        current = current or None
        if current is None:
            if anchor_version is not None:
                raise StaleAnchorError("state ว่างแต่ anchor ไม่ใช่ genesis")
            existing_pending = None
        else:
            if current.get("last_run_id") == run_id:
                raise _Idempotent()
            if anchor_version != current.get("version"):
                raise StaleAnchorError(
                    f"stale anchor: anchor.version={anchor_version} "
                    f"state.version={current.get('version')}")
            if slot is not None and current.get("slot") == slot:
                raise SlotAlreadyConsumed(f"slot {slot} commit ไปแล้ว")
            existing_pending = current.get("pending_order")

        if order_intent is not None and _is_pending_order(existing_pending):
            raise PendingOrderExists(
                f"pending order {existing_pending.get('run_id')} ยังไม่จบ")

        next_state = {
            "version": expected_version,
            "dna_step": int(meta["step"]),
            "p0": float(snapshot["price"]) if anchor is None else float(anchor.p0),
            "prev_price": float(meta["acted_price_next"]),
            "prev_actual": float(meta["actual_next"]),
            "prev_holdings": float(snapshot.get("holdings", 0.0) or 0.0),
            "last_run_id": run_id,
            "slot": slot,
            "updated_at": snapshot["captured_at"],
            "config_hash": config_hash(cfg),
            "symbol": cfg.symbol,
            "cashflow_semantics": CASHFLOW_SEMANTICS,
        }
        if order_intent is not None:
            next_state["pending_order"] = order_intent
        elif _is_pending_order(existing_pending):
            next_state["pending_order"] = existing_pending
        return next_state

    try:
        state_ref.transaction(txn)
    except _Idempotent:
        row_ref.update({"committed": True})
        return {"committed": False, "idempotent": True,
                "run_id": run_id, "version": expected_version}
    except (StaleAnchorError, SlotAlreadyConsumed, PendingOrderExists):
        row_ref.delete()
        raise

    row_ref.update({"committed": True})
    return {"committed": True, "run_id": run_id, "version": expected_version,
            "order_intent": order_intent}


def get_pending_order(cfg: Config) -> dict | None:
    state = db.reference(f"{STATE_PATH}/{chain_key(cfg)}").get() or {}
    pending = state.get("pending_order")
    return pending if _is_pending_order(pending) else None


def update_pending_order(cfg: Config, run_id: str, fields: dict) -> dict | None:
    ref = db.reference(f"{STATE_PATH}/{chain_key(cfg)}")
    def txn(current):
        if not current:
            return current
        pending = current.get("pending_order")
        if not isinstance(pending, dict) or pending.get("run_id") != run_id:
            return current
        updated = dict(pending)
        updated.update(fields)
        updated["status"] = normalize_status(updated.get("status"))
        current = dict(current)
        current["pending_order"] = updated
        return current
    result = ref.transaction(txn)
    return (result or {}).get("pending_order") if isinstance(result, dict) else None


def clear_pending_order(cfg: Config, run_id: str) -> None:
    ref = db.reference(f"{STATE_PATH}/{chain_key(cfg)}")
    def txn(current):
        if not current:
            return current
        pending = current.get("pending_order")
        if not isinstance(pending, dict) or pending.get("run_id") != run_id:
            return current
        current = dict(current)
        current.pop("pending_order", None)
        return current
    ref.transaction(txn)


def write_order_audit(event_id: str, payload: dict) -> None:
    redacted = {k: v for k, v in payload.items()
                if k not in {"app_key", "app_secret", "access_token", "x-signature"}}
    ref = db.reference(f"{AUDIT_PATH}/{event_id}")
    def txn(current):
        merged = dict(current or {})
        merged.update(redacted)
        return merged
    ref.transaction(txn)


def update_order_audit(event_id: str, fields: dict) -> None:
    safe = {k: v for k, v in fields.items()
            if k not in {"app_key", "app_secret", "access_token", "x-signature"}}
    db.reference(f"{AUDIT_PATH}/{event_id}").update(safe)


def pending_audits(terminal_statuses: set[str], limit: int = 20) -> dict:
    all_audits = db.reference(AUDIT_PATH).get() or {}
    rows: list[tuple[str, dict]] = []
    for event_id, payload in all_audits.items():
        if not isinstance(payload, dict):
            continue
        if normalize_status(payload.get("status")) in terminal_statuses:
            continue
        rows.append((event_id, payload))
    rows.sort(key=lambda item: str(item[1].get("placed_at") or item[1].get("created_at") or ""))
    return dict(rows[:limit])


def apply_realized_fill(ck: str, event_id: str, side: str,
                        cumulative_qty: float, price: float,
                        cumulative_fee: float = 0.0) -> dict:
    """Apply only the newly filled quantity for one broker order.

    Webull detail is treated as cumulative. applied_fills prevents double count
    across polling, retries, partial fills, and function restarts.
    """
    cumulative_qty = float(cumulative_qty)
    price = float(price)
    cumulative_fee = float(cumulative_fee or 0.0)
    if cumulative_qty < 0 or cumulative_fee < 0:
        raise ValueError("cumulative fill/fee ติดลบไม่ได้")
    ref = db.reference(f"{REALIZED_PATH}/{ck}")

    def txn(current):
        state = dict(current or {})
        applied = dict(state.get("applied_fills") or {})
        prev = dict(applied.get(event_id) or {})
        prev_qty = float(prev.get("quantity", 0.0) or 0.0)
        prev_fee = float(prev.get("fee", 0.0) or 0.0)
        prev_avg_price = float(prev.get("average_price", prev.get("price", 0.0)) or 0.0)
        delta_qty = cumulative_qty - prev_qty
        delta_fee = max(0.0, cumulative_fee - prev_fee)
        if delta_qty <= 1e-9:
            return state
        cumulative_notional = cumulative_qty * price
        previous_notional = prev_qty * prev_avg_price
        delta_price = (cumulative_notional - previous_notional) / delta_qty
        if not (delta_price > 0):
            raise ValueError("incremental fill price ต้อง > 0")
        legs, realized_delta = apply_fill(
            state.get("open_legs"), side, delta_qty, delta_price, delta_fee)
        cumulative = float(state.get("cumulative_realized", 0.0) or 0.0) + realized_delta
        applied[event_id] = {"quantity": cumulative_qty, "fee": cumulative_fee,
                             "average_price": price, "side": str(side).upper()}
        state.update({
            "open_legs": legs,
            "applied_fills": applied,
            "cumulative_realized": cumulative,
            "last_realized_delta": realized_delta,
            "last_event_id": event_id,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return state

    result = ref.transaction(txn) or {}
    return {
        "realized_delta": float(result.get("last_realized_delta", 0.0) or 0.0),
        "realized_cumulative": float(result.get("cumulative_realized", 0.0) or 0.0),
        "open_legs": result.get("open_legs") or {"buys": [], "sells": []},
    }
