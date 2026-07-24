"""หา LEGO_DNA_ORIGIN_UTC สำหรับเปิด LEGO_DNA_CLOCK_MODE=market

origin คือ "เวลาของช่องตลาดที่นับเป็น DNA step 0" ถ้าจะต่อ chain เดิม ต้องเดิน
ถอยหลังจากช่องปัจจุบันเท่ากับ step ที่แถวถัดไปจะใช้

    LEGO_SLOT_SECONDS=1800 python find_origin.py 17
    LEGO_SLOT_SECONDS=1800 python find_origin.py 2026-07-27T15:00:00Z 17

ค่า 17 = state.dna_step + 1 (ดูที่ /webull_lego_state/{chain_key})
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from market_clock import NY, resolve_market_slot, session_bounds, slot_seconds

UTC = timezone.utc


def walk_back(at: datetime, steps: int) -> datetime:
    """ถอยหลัง N ช่องตามเวลาตลาด (ข้ามกลางคืน เสาร์อาทิตย์ และวันหยุด)"""
    sec = slot_seconds()
    cur, left = at, steps
    while left > 0:
        session_date = cur.astimezone(NY).date()
        bounds = session_bounds(session_date)
        if bounds:
            session_open, _ = bounds
            index = int((cur - session_open).total_seconds()) // sec
            take = min(left, index)
            cur -= timedelta(seconds=take * sec)
            left -= take
            if left == 0:
                break
        previous = session_date - timedelta(days=1)
        while not session_bounds(previous):
            previous -= timedelta(days=1)
        prev_open, prev_close = session_bounds(previous)
        slots = -(-int((prev_close - prev_open).total_seconds()) // sec)   # ceil
        cur = prev_open + timedelta(seconds=(slots - 1) * sec)
        left -= 1
    return cur


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2
    target = int(argv[-1])
    if target < 0:
        print("target step ต้อง >= 0")
        return 2
    at = (datetime.fromisoformat(argv[1].replace("Z", "+00:00")).astimezone(UTC)
          if len(argv) == 3 else datetime.now(UTC))

    # origin ชั่วคราว = เวลาเปิดตลาดของวันนั้น (ต้องไม่อยู่หลังช่องที่กำลังดู)
    bounds = session_bounds(at.astimezone(NY).date())
    if bounds:
        os.environ["LEGO_DNA_ORIGIN_UTC"] = bounds[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    slot = resolve_market_slot(at) if bounds else None
    if slot is None:
        print(f"{at:%Y-%m-%d %H:%M} UTC ตลาดปิด — ระบุเวลาที่อยู่ในเวลาทำการ เช่น")
        print("  LEGO_SLOT_SECONDS=1800 python find_origin.py 2026-07-27T15:00:00Z 17")
        return 1

    origin = walk_back(slot.slot_start_utc, target)
    origin_text = origin.strftime("%Y-%m-%dT%H:%M:%SZ")
    os.environ["LEGO_DNA_ORIGIN_UTC"] = origin_text
    check = resolve_market_slot(at)
    print(f"LEGO_DNA_ORIGIN_UTC = {origin_text}")
    print(f"ตรวจ: ordinal ของช่องปัจจุบัน = {check.market_ordinal} (ต้องเท่ากับ {target})")
    return 0 if check.market_ordinal == target else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
