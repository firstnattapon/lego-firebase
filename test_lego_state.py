"""test_lego_state.py — Step 18 invariants บน FakeDB (รันได้โดยไม่ต้องมี firebase_admin)

ครอบคลุม:
  #7 idempotent : replay run_id เดิม -> no-op, row ไม่หาย
  #8 stale-anchor + monotonic : anchor เก่า -> StaleAnchorError + orphan ถูกเก็บกวาด;
     commit ปกติ -> version+1 + pointer เลื่อน (dna_step, p0, prev_price, prev_actual)
  race-repair : replay ที่ pre-read เห็นค่าเก่า (stale read) ต้อง "ซ่อม" row ไม่ใช่ลบ
  slot guard  : LEGO_SLOT_SECONDS เดิม -> SlotAlreadyConsumed + orphan ถูกเก็บกวาด
"""
from __future__ import annotations

import copy
import os
import sys
import types

# ---- stub firebase_admin.db ก่อน import lego_state --------------------------
STORE: dict = {}
STALE: dict = {}   # path -> จำนวนครั้งที่ get() คืน None (จำลอง stale read / race window)


class FakeRef:
    def __init__(self, path: str):
        self.path = path

    def get(self):
        if STALE.get(self.path, 0) > 0:
            STALE[self.path] -= 1
            return None
        return copy.deepcopy(STORE.get(self.path))

    def set(self, v):
        STORE[self.path] = copy.deepcopy(v)

    def update(self, patch):
        STORE.setdefault(self.path, {}).update(copy.deepcopy(patch))

    def delete(self):
        STORE.pop(self.path, None)

    def transaction(self, fn):
        new = fn(copy.deepcopy(STORE.get(self.path)))
        STORE[self.path] = copy.deepcopy(new)
        return new

    def push(self, v):
        STORE.setdefault(self.path, []).append(copy.deepcopy(v))


_fa = types.ModuleType("firebase_admin")
_db = types.ModuleType("firebase_admin.db")
_db.reference = lambda path: FakeRef(path)
_fa.db = _db
sys.modules.setdefault("firebase_admin", _fa)
sys.modules.setdefault("firebase_admin.db", _db)

from lego_one_row import Config, compute_row                      # noqa: E402
from lego_state import (ROWS_PATH, STATE_PATH, SlotAlreadyConsumed,  # noqa: E402
                        StaleAnchorError, chain_key, commit_final_row,
                        read_anchor)

CFG = Config(symbol="APLS", fix_c=1500.0, diff=60.0)


def _snap(t: str, price: float, holdings: float) -> dict:
    return {"captured_at": t, "price": price, "holdings": holdings}


def _reset():
    STORE.clear()
    STALE.clear()
    os.environ.pop("LEGO_SLOT_SECONDS", None)


# ---- genesis + pointer ------------------------------------------------------
def test_genesis_commit_and_pointer():
    _reset()
    snap = _snap("2026-07-17T14:00:00Z", 10.0, 150.0)
    row = compute_row(CFG, snap, None)
    res = commit_final_row(CFG, snap, None, row)
    assert res["committed"] is True and res["version"] == 1

    state = STORE[f"{STATE_PATH}/{chain_key(CFG)}"]
    assert state["version"] == 1 and state["dna_step"] == 0
    assert state["p0"] == 10.0 and state["prev_price"] == 10.0
    assert state["prev_actual"] == 0.0 and state["prev_holdings"] == 150.0

    doc = STORE[f"{ROWS_PATH}/{res['run_id']}"]
    assert doc["committed"] is True and doc["version"] == 1


# ---- idempotent replay (invariant #7) --------------------------------------
def test_idempotent_replay_noop():
    _reset()
    snap = _snap("2026-07-17T14:00:00Z", 10.0, 150.0)
    row = compute_row(CFG, snap, None)
    res1 = commit_final_row(CFG, snap, None, row)
    res2 = commit_final_row(CFG, snap, None, compute_row(CFG, snap, None))
    assert res2["idempotent"] is True and res2["run_id"] == res1["run_id"]
    assert STORE[f"{STATE_PATH}/{chain_key(CFG)}"]["version"] == 1   # ไม่ขยับ
    assert STORE[f"{ROWS_PATH}/{res1['run_id']}"]["committed"] is True


# ---- race-repair: replay ที่ pre-read เห็นค่าเก่า ต้องไม่ลบ committed row ----
def test_race_replay_repairs_not_deletes():
    _reset()
    snap = _snap("2026-07-17T14:00:00Z", 10.0, 150.0)
    res1 = commit_final_row(CFG, snap, None, compute_row(CFG, snap, None))
    rid = res1["run_id"]

    # จำลอง attempt คู่ขนาน: _repair_pending + pre-read เห็น None (ก่อนอีกฝั่ง commit เสร็จ)
    STALE[f"{ROWS_PATH}/{rid}"] = 2
    res2 = commit_final_row(CFG, snap, None, compute_row(CFG, snap, None))
    assert res2["idempotent"] is True

    doc = STORE.get(f"{ROWS_PATH}/{rid}")
    assert doc is not None, "committed row ต้องไม่ถูกลบ (data loss)"
    assert doc["committed"] is True
    assert STORE[f"{STATE_PATH}/{chain_key(CFG)}"]["last_run_id"] == rid


# ---- stale anchor (invariant #8) + orphan cleanup ---------------------------
def test_stale_anchor_raises_and_cleans_orphan():
    _reset()
    snap1 = _snap("2026-07-17T14:00:00Z", 10.0, 150.0)
    commit_final_row(CFG, snap1, None, compute_row(CFG, snap1, None))

    # anchor=None (genesis) ทั้งที่ state เดินไปแล้ว + snapshot ใหม่ -> stale
    snap2 = _snap("2026-07-17T14:30:00Z", 11.0, 150.0)
    row2 = compute_row(CFG, snap2, None)
    from lego_state import make_run_id
    rid2 = make_run_id(chain_key(CFG), None, snap2)
    try:
        commit_final_row(CFG, snap2, None, row2)
        assert False, "ต้อง StaleAnchorError"
    except StaleAnchorError:
        pass
    assert STORE.get(f"{ROWS_PATH}/{rid2}") is None       # orphan ถูกเก็บกวาด
    assert STORE[f"{STATE_PATH}/{chain_key(CFG)}"]["version"] == 1


# ---- second row: version+1 + pointer เลื่อน (invariant #8) ------------------
def test_second_row_advances_monotonic():
    _reset()
    snap1 = _snap("2026-07-17T14:00:00Z", 10.0, 150.0)
    commit_final_row(CFG, snap1, None, compute_row(CFG, snap1, None))

    anchor = read_anchor(CFG)
    assert anchor is not None and anchor.version == 1 and anchor.dna_step == 0

    snap2 = _snap("2026-07-17T14:30:00Z", 12.0, 150.0)
    row2 = compute_row(CFG, snap2, anchor)
    res2 = commit_final_row(CFG, snap2, anchor, row2)
    assert res2["committed"] is True and res2["version"] == 2

    state = STORE[f"{STATE_PATH}/{chain_key(CFG)}"]
    assert state["dna_step"] == 1
    assert state["p0"] == 10.0                 # P0 คงเดิมตาม anchor
    assert state["prev_price"] == 12.0         # Pₙ ล่าสุด
    assert state["prev_actual"] == row2["Aₙ สะสม (USD)"]


# ---- slot guard -------------------------------------------------------------
def test_slot_guard_blocks_same_slot():
    _reset()
    os.environ["LEGO_SLOT_SECONDS"] = "1800"
    try:
        snap1 = _snap("2026-07-17T14:00:00Z", 10.0, 150.0)
        commit_final_row(CFG, snap1, None, compute_row(CFG, snap1, None))

        anchor = read_anchor(CFG)
        snap2 = _snap("2026-07-17T14:10:00Z", 10.5, 150.0)   # slot 1800s เดียวกัน
        row2 = compute_row(CFG, snap2, anchor)
        from lego_state import make_run_id
        rid2 = make_run_id(chain_key(CFG), anchor.version, snap2)
        try:
            commit_final_row(CFG, snap2, anchor, row2)
            assert False, "ต้อง SlotAlreadyConsumed"
        except SlotAlreadyConsumed:
            pass
        assert STORE.get(f"{ROWS_PATH}/{rid2}") is None       # orphan ถูกเก็บกวาด
        assert STORE[f"{STATE_PATH}/{chain_key(CFG)}"]["version"] == 1
    finally:
        os.environ.pop("LEGO_SLOT_SECONDS", None)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
