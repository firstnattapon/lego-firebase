# Protected financial and persistence contracts

## Two ledgers

- `model_ledger`: recurrence 17 columns; ไม่ใช่ cash balance หรือ realized P&L
- `realized_ledger`: broker cumulative fill quantity/price/fee และ matched-leg accounting
- `READY_BUY`/`READY_SELL` เป็น intent; Preview/Place acceptance ไม่ใช่ fill

## Decision and recurrence

```text
V = broker_holdings * P_n
GAP = FIX_C - V
DNA=0                         -> PASS_DNA_ZERO
DNA=1 and abs(GAP)<=DIFF      -> PASS_THRESHOLD
qty = round(abs(GAP)/P_n, precision)
qty<=0                        -> PASS_THRESHOLD
otherwise                     -> READY_BUY / READY_SELL
SELL qty <= holdings rounded down at configured precision
R_n = FIX_C * ln(P_n / P0)
```

Unfinalized row (รวม PASS/READY/PENDING/SUBMITTED):

```text
Delta_A = 0
A       = A_previous
P_acted = previous_P_acted
E       = A_previous - FIX_C * ln(previous_P_acted / P0)
```

Terminal positive cumulative fill ที่ผ่าน identity/evidence/holdings witnesses:

```text
Delta_A = FIX_C * (final_cumulative_average_execution_price / previous_P_acted - 1)
A       = previous_A + Delta_A
P_acted = final_cumulative_average_execution_price
R       = committed row reference_R
E       = A - R
```

Filled quantity เป็น broker evidence และ realized-ledger input แต่ model `Delta_A` ไม่ scale ตาม quantity. ห้าม “แก้” สูตรให้เหมือน cashflow โดยไม่มี protected-contract change gate.

## Genesis and partial lifecycle

- `P0` seed จาก genesis decision snapshot และคงเดิมใน active chain
- `P_acted` มี genesis/legacy-compatible seed; หลัง seed เปลี่ยนเฉพาะ qualifying confirmed execution
- partial nonterminal อัปเดต realized increment แบบ idempotentได้ แต่ model รอ terminal
- terminal `FILLED/CANCELLED/EXPIRED` ที่ cumulative fill > 0 ใช้ final cumulative values และ finalize model ครั้งเดียว
- explicit zero fill ต่างจาก missing/NaN/negative evidence; invalid evidence fail closed
- holdings witness ตรวจ direction ตาม current contract ไม่ใช่ exact conservation; external/manual trade ต้องเข้า manual reconciliation policy

## Protected interfaces

17 column names/order/types, Firebase paths, chain/config identity, `run_id == client_order_id`, outbox identity, cashflow semantics/version, P0/P_acted, DNA/calendar clock, runtime fingerprint, claim generation/money fence, HTTP/env behavior และ legacy repair readers.

การเปลี่ยนข้อใดต้องมี failing regression, reader inventory, isolated fix, migration/rollback + mixed-version test และ explicit approval เมื่อ semantics/schema เปลี่ยน.
