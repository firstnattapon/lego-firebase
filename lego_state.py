"""lego_state.py — Step 18 persistence บน Firebase Realtime Database (RTDB)

3 path แทน Firestore 3 collection:
  /webull_lego_rows/{run_id}          -> row immutable (+ flag committed)
  /webull_lego_state/{chain_key}      -> pointer + baseline + version (node เดียว)
  /webull_lego_order_audit/{event_id} -> redacted audit

invariant:
  #7 idempotent : run_id = sha256(chain_key, anchor.version, snapshot)[:32]; ซ้ำ -> no-op
  #8 stale-anchor + monotonic : anchor.version ต้อง == state.version มิฉะนั้น StaleAnchorError;
     commit -> version+1 + เลื่อน pointer (dna_step, p0, prev_price=Pₙ, prev_actual=Aₙ)

ลำดับเขียน (ปิดช่อง crash กลางคัน โดยไม่ต้องมี multi-doc transaction):
  1) เขียน row ก่อน (committed=False)
  2) transaction บน state node เดียว (compare-and-set version)
     - stale -> ลบ orphan row แล้ว raise
  3) update row committed=True
  ถ้าตายระหว่าง 2)-3): state advance แล้ว row ค้าง committed=False
  -> รอบถัดไป _repair_pending ซ่อมให้ (idempotent)

slot guard (option, กัน Scheduler retry สร้าง 2 แถวใน slot เดียว):
  LEGO_SLOT_SECONDS > 0 -> ถ้า state.slot เดิม == slot ใหม่ raise SlotAlreadyConsumed
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from firebase_admin import db

from lego_one_row import Anchor, Config

ROWS_PATH = "webull_lego_rows"
STATE_PATH = "webull_lego_state"
AUDIT_PATH = "webull_lego_order_audit"


class StaleAnchorError(RuntimeError):
    """anchor.version != state.version -> restart Step 0"""


class SlotAlreadyConsumed(RuntimeError):
    """slot เวลาเดียวกันถูก commit ไปแล้ว (กัน scheduler retry ยิงซ้ำ)"""


class _Idempotent(Exception):
    """internal: replay ด้วย run_id เดิม -> no-op"""


# ---- คีย์ derive ----------------------------------------------------------
def config_hash(cfg: Config) -> str:
    payload = json.dumps(
        {"s": cfg.strategy_id, "sym": cfg.symbol, "fix": cfg.fix_c,
         "diff": cfg.diff, "dp": cfg.decimal_precision, "dna": cfg.dna_code},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def chain_key(cfg: Config) -> str:
    # เปลี่ยน strategy_id/fix_c/diff/precision/dna -> config_hash เปลี่ยน -> chain ใหม่
    return f"{cfg.symbol}_{config_hash(cfg)}"


def make_run_id(ck: str, anchor_version: int | None, snapshot: dict) -> str:
    raw = (f"{ck}|{anchor_version}|{snapshot['captured_at']}|"
           f"{snapshot['price']}|{snapshot.get('holdings', 0)}")
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _slot_of(captured_at: str) -> int | None:
    """slot index = floor(epoch / LEGO_SLOT_SECONDS); 0/ไม่ตั้ง = ปิด guard"""
    sec = int(os.environ.get("LEGO_SLOT_SECONDS", "0"))
    if sec <= 0:
        return None
    dt = datetime.strptime(captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) // sec


# ---- อ่าน anchor ล่าสุด ---------------------------------------------------
def read_anchor(cfg: Config) -> Anchor | None:
    state = db.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    if not state:
        return None
    return Anchor(
        version=int(state["version"]),
        dna_step=int(state["dna_step"]),
        p0=float(state["p0"]),
        prev_price=float(state["prev_price"]),
        prev_actual=float(state["prev_actual"]),
        # state เก่าไม่มี prev_holdings -> None -> fallback ปิดเอง (fail closed) จนรอบใหม่เขียนค่า
        prev_holdings=(float(state["prev_holdings"])
                       if state.get("prev_holdings") is not None else None),
    )


def _repair_pending(state: dict | None) -> None:
    """state ชี้ run_id ที่ commit แล้วแต่ row ยังค้าง committed=False -> ซ่อม (idempotent)"""
    if not state:
        return
    rid = state.get("last_run_id")
    if not rid:
        return
    ref = db.reference(f"{ROWS_PATH}/{rid}")
    doc = ref.get()
    if doc is not None and doc.get("committed") is False:
        ref.update({"committed": True})


# ---- Step 18: commit ------------------------------------------------------
def commit_final_row(cfg: Config, snapshot: dict, anchor: Anchor | None, row: dict) -> dict:
    ck = chain_key(cfg)
    anchor_version = None if anchor is None else anchor.version
    run_id = make_run_id(ck, anchor_version, snapshot)
    expected_version = 1 if anchor is None else anchor.version + 1
    slot = _slot_of(snapshot["captured_at"])

    row_ref = db.reference(f"{ROWS_PATH}/{run_id}")
    state_ref = db.reference(f"{STATE_PATH}/{ck}")
    meta = row["_meta"]

    # ซ่อมงานค้างรอบก่อน (ถ้ามี) — ทำก่อนตัดสินใจใด ๆ
    _repair_pending(state_ref.get())

    # (invariant #7) pre-read dedupe
    existing = row_ref.get()
    if existing is not None and existing.get("committed"):
        return {"committed": False, "idempotent": True, "run_id": run_id}

    # 1) เขียน row ก่อน (committed=False) — orphan ปลอดภัย: dashboard กรอง committed
    doc = {k: v for k, v in row.items() if k != "_meta"}
    doc.update({"run_id": run_id, "chain_key": ck,
                "version": expected_version, "committed": False})
    row_ref.set(doc)

    # 2) transaction บน state node เดียว (atomic compare-and-set)
    def txn(current):
        if current is None:                       # genesis
            if anchor_version is not None:
                raise StaleAnchorError("state ว่างแต่ anchor ไม่ใช่ genesis -> restart Step 0")
        else:
            if current.get("last_run_id") == run_id:
                raise _Idempotent()
            if anchor_version != current.get("version"):     # invariant #8
                raise StaleAnchorError(
                    f"stale anchor: anchor.version={anchor_version} "
                    f"state.version={current.get('version')} -> restart Step 0"
                )
            if slot is not None and current.get("slot") == slot:
                raise SlotAlreadyConsumed(
                    f"slot {slot} commit ไปแล้ว (scheduler retry?) -> no new row")
        return {
            "version": expected_version,               # monotonic +1
            "dna_step": int(meta["step"]),
            "p0": float(snapshot["price"]) if anchor is None else float(anchor.p0),
            "prev_price": float(snapshot["price"]),    # Pₙ
            "prev_actual": float(meta["actual_next"]), # Aₙ
            "prev_holdings": float(snapshot["holdings"]),  # holdings ล่าสุด (fallback รอบหน้า)
            "last_run_id": run_id,
            "slot": slot,
            "updated_at": snapshot["captured_at"],
            "config_hash": config_hash(cfg),
            "symbol": cfg.symbol,
        }

    try:
        state_ref.transaction(txn)
    except _Idempotent:
        # state.last_run_id == run_id -> commit นี้เคยสำเร็จแล้ว และ row เนื้อหาเดียวกัน
        # (run_id เดิม = derive จาก chain/anchor/snapshot เดิม -> doc deterministic เหมือนกัน)
        # ห้าม delete: อาจลบ row ที่ attempt คู่ขนาน commit ไปแล้ว -> data loss
        # เขียน doc ฉบับ committed=True ทับ = ซ่อมทั้ง flag และเนื้อหาให้ครบ (idempotent)
        row_ref.set({**doc, "committed": True})
        return {"committed": False, "idempotent": True, "run_id": run_id}
    except (StaleAnchorError, SlotAlreadyConsumed):
        row_ref.delete()      # orphan จาก attempt ที่แพ้ race — ลบก่อน raise
        raise

    # 3) mark committed (ตายตรงนี้ = _repair_pending รอบหน้าซ่อม)
    row_ref.update({"committed": True})
    return {"committed": True, "run_id": run_id, "version": expected_version}


# ---- audit (redacted) -----------------------------------------------------
def write_order_audit(event_id: str, payload: dict) -> None:
    redacted = {k: v for k, v in payload.items()
                if k not in {"app_key", "app_secret", "access_token"}}
    db.reference(f"{AUDIT_PATH}/{event_id}").set(redacted)
