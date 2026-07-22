---
name: webull-order-flow
description: ออกแบบ ตรวจสอบ และแก้ไขวงจรคำสั่งซื้อขาย Webull OpenAPI ตั้งแต่ account/position validation, preview, place, replace, cancel, order status reconciliation จนถึงยืนยันผลการ fill โดยเน้น idempotency, duplicate-order protection, partial fill, timeout recovery และ UAT ก่อน production
---

# Webull Order Flow

ใช้สกิลนี้เมื่อผู้ใช้ต้องการสร้าง ตรวจสอบ หรือแก้ไขระบบส่งคำสั่งซื้อขายผ่าน Webull OpenAPI โดยเฉพาะงานใน `firstnattapon/lego-firebase`

## เป้าหมาย

สร้าง order flow ที่:

1. ไม่ส่งคำสั่งซ้ำ
2. ไม่ถือว่า order สำเร็จเพียงเพราะ API ตอบรับ
3. รองรับ partial fill, rejected, cancelled, expired และ timeout
4. ตรวจสอบ account, position, buying power และ open orders ก่อนส่งคำสั่ง
5. ใช้ `client_order_id` ที่สร้างซ้ำได้อย่างมีหลักการ
6. แยก UAT กับ production ชัดเจน
7. บันทึก audit trail ให้ตรวจสอบย้อนหลังได้
8. ห้ามนับกำไรหรืออัปเดตสถานะ realized จนกว่าจะมี execution ที่ยืนยันแล้ว

## แหล่งอ้างอิงหลัก

ใช้เอกสาร Webull OpenAPI ต่อไปนี้เป็น source of truth:

- Authentication Overview
- Signature
- Token
- Trading API Overview
- Trading API Getting Started
- Accounts
- Stock Trading
- Preview Order
- Place Order
- Replace Order
- Cancel Order
- Order History
- Open Orders
- Order Detail
- Subscribe Trade Events
- Trading API FAQ
- Change Logs

Production endpoints:

- Trading API: `api.webull.co.th`
- Trading Events gRPC: `events-api.webull.co.th`

UAT endpoints:

- Trading API: `th-api.uat.webullbroker.com`
- Trading Events gRPC: `th-events-api.uat.webullbroker.com`

Python SDK:

```bash
pip3 install --upgrade webull-openapi-python-sdk
```

## หลักการบังคับ

### 1. Preview ไม่ใช่ Place

`Preview Order` ใช้ตรวจสอบค่าธรรมเนียม กำลังซื้อ ความสมเหตุสมผล และข้อผิดพลาดเบื้องต้นเท่านั้น

ห้ามตีความ preview success ว่า order ถูกส่งหรือ fill แล้ว

### 2. Place accepted ไม่เท่ากับ Filled

ผลตอบรับจาก `Place Order` หมายถึงระบบรับคำขอแล้วเท่านั้น

ต้องตรวจสอบสถานะจริงจากอย่างน้อยหนึ่งทาง:

- Trade Events ผ่าน gRPC
- Order Detail
- Open Orders
- Order History

### 3. ใช้สถานะจาก broker เป็น source of truth

สถานะใน Firebase เป็น local projection ไม่ใช่สถานะจริงขั้นสุดท้าย

เมื่อข้อมูลขัดกัน ให้ยึด execution/order status จาก Webull และทำ reconciliation

### 4. ห้ามส่งซ้ำเมื่อไม่รู้ผลลัพธ์

เมื่อ `Place Order` timeout หรือ connection หลุด:

- ห้าม retry แบบ blind
- ค้นหาด้วย `client_order_id`
- ตรวจ Open Orders, Order Detail หรือ Order History
- ส่งใหม่ได้ต่อเมื่อพิสูจน์แล้วว่าไม่มี order เดิม

### 5. Partial fill ต้องคิดตามจำนวนที่ fill จริง

เก็บอย่างน้อย:

- requested quantity
- filled quantity
- remaining quantity
- average fill price
- fees ถ้ามี
- last broker status

ห้ามอัปเดต holdings หรือ realized result ด้วย requested quantity หาก fill ไม่ครบ

### 6. การแก้คำสั่งต้องอ้าง order เดิม

ก่อน Replace Order:

- อ่าน Order Detail ล่าสุด
- ตรวจว่ายังแก้ไขได้
- ตรวจ filled quantity ปัจจุบัน
- ห้าม replace ด้วยจำนวนรวมที่ทำให้เกินยอดคงเหลือ

### 7. Cancel accepted ไม่เท่ากับ Cancelled

หลัง Cancel Order ต้องติดตามจนได้ terminal status

อาจเกิด race condition ที่ order fill ระหว่างส่ง cancel

### 8. คำสั่ง SELL ต้องไม่เกิน position ที่ขายได้

คำนวณจาก broker position ล่าสุด ไม่ใช่ local cache อย่างเดียว

ถ้ามี open sell orders ต้องหัก reserved quantity ก่อน

## State Machine มาตรฐาน

```text
INTENT_CREATED
  -> VALIDATING
  -> PREVIEWING
  -> READY_TO_PLACE
  -> PLACING
  -> SUBMITTED
  -> PARTIALLY_FILLED
  -> FILLED

ทางเลือก:
  -> REJECTED
  -> CANCELLING
  -> CANCELLED
  -> REPLACING
  -> EXPIRED
  -> UNKNOWN_RECONCILIATION_REQUIRED
```

Terminal states:

- `FILLED`
- `CANCELLED`
- `REJECTED`
- `EXPIRED`

`UNKNOWN_RECONCILIATION_REQUIRED` ไม่ใช่ terminal state และห้ามสร้าง order ใหม่ทันที

## Flow มาตรฐาน

### Step 1 — สร้าง Order Intent

สร้าง immutable intent ก่อนเรียก broker:

```json
{
  "strategy": "lego_rebalancing",
  "symbol": "TQQQ",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": "1.00000",
  "limit_price": "50.00",
  "time_in_force": "DAY",
  "dna_step": 18,
  "reason": "REBALANCE_GAP",
  "environment": "uat"
}
```

### Step 2 — สร้าง Idempotency Key

สร้าง `client_order_id` จากข้อมูลที่ระบุ intent เดียวกัน เช่น:

```text
lego:{environment}:{account_id}:{symbol}:{dna_step}:{side}:{intent_hash}
```

ข้อกำหนด:

- intent เดิมต้องได้ key เดิม
- intent ใหม่ต้องได้ key ใหม่
- ห้ามใช้ timestamp เพียงอย่างเดียว
- บันทึก key ก่อนเรียก Webull

### Step 3 — Acquire Lock

ใช้ Firestore transaction หรือ lease lock ต่อ account/symbol/strategy step

ตรวจ:

- ไม่มี invocation อื่นถือ lock
- fencing token ตรงกับ invocation ปัจจุบัน
- lock มี TTL และ self-heal

### Step 4 — Authentication Gate

ตรวจ:

- App Key และ App Secret พร้อมใช้งาน
- HMAC-SHA256 signature ถูกต้อง
- token อยู่สถานะ `NORMAL`
- environment/host ตรงกัน
- ห้าม log secret, signature เต็ม หรือ token เต็ม

### Step 5 — Account Gate

เรียก Account List และ Account Balance

ตรวจ:

- account ถูกต้อง
- account เปิดใช้งาน
- currency และ buying power เพียงพอ
- ไม่มีข้อจำกัดที่ทำให้ส่งคำสั่งไม่ได้

### Step 6 — Position และ Open Order Gate

เรียก Account Positions และ Open Orders

BUY:

- ตรวจ buying power
- ตรวจ duplicate buy order

SELL:

- ตรวจ sellable position
- หักจำนวนที่ถูกจองใน open sell orders
- clamp quantity ให้ไม่เกินจำนวนขายได้

### Step 7 — Market/Instrument Gate

ตรวจ instrument และพารามิเตอร์:

- symbol ถูกต้อง
- product รองรับ
- order type รองรับ
- time in force รองรับ
- quantity precision ถูกต้อง
- price tick ถูกต้อง
- market session สอดคล้องกับคำสั่ง

ห้ามเดา precision หรือ enum จากความจำ หากโค้ดหรือเอกสารเวอร์ชันล่าสุดระบุไว้ต่างกัน

### Step 8 — Preview Order

ส่ง preview ด้วย payload เดียวกับที่จะ place

ตรวจ:

- estimated cost
- fees
- buying power impact
- warnings
- rejection reason

หาก preview ไม่ผ่าน ให้จบที่ `REJECTED_LOCAL_VALIDATION` และห้าม place

### Step 9 — Persist Before Place

บันทึก atomically:

- intent
- `client_order_id`
- payload hash
- preview response summary
- state = `READY_TO_PLACE`
- fencing token
- attempt number

### Step 10 — Place Order

เปลี่ยน state เป็น `PLACING` ก่อน network call

หลังตอบรับ:

- บันทึก broker order id
- state = `SUBMITTED`
- บันทึก response โดยตัดข้อมูลลับ

เมื่อ timeout:

- state = `UNKNOWN_RECONCILIATION_REQUIRED`
- ห้าม retry place ทันที

### Step 11 — Reconcile

ค้นหาตามลำดับ:

1. Trade Events
2. Order Detail ด้วย broker order id
3. Open Orders
4. Order History
5. ค้นด้วย `client_order_id`

ผลลัพธ์:

- พบ order เดิม → ผูกกับ intent เดิม
- พบ fill → อัปเดตตาม execution จริง
- ไม่พบและผ่าน reconciliation policy แล้ว → อนุญาต retry ด้วย `client_order_id` เดิมตามข้อกำหนด API
- ยังไม่ชัดเจน → คงสถานะ unknown และแจ้งเตือน

### Step 12 — Handle Partial Fill

เมื่อ partial fill:

- บันทึก execution ใหม่แบบ append-only
- อัปเดต filled quantity จากผลรวม execution
- คำนวณ weighted average fill price
- ห้ามสร้างคำสั่งใหม่ทับ remaining quantity โดยอัตโนมัติ
- ก่อน replace/cancel ต้องอ่านสถานะล่าสุดอีกครั้ง

### Step 13 — Replace หรือ Cancel

Replace:

```text
SUBMITTED/PARTIALLY_FILLED
  -> read latest detail
  -> validate remaining qty
  -> REPLACING
  -> reconcile replacement
```

Cancel:

```text
SUBMITTED/PARTIALLY_FILLED
  -> CANCELLING
  -> wait for broker terminal state
  -> CANCELLED or FILLED/PARTIALLY_FILLED
```

### Step 14 — Commit Portfolio State

อัปเดต holdings/cash จาก execution จริงเท่านั้น

BUY:

```text
position_delta = +filled_qty
cash_delta = -(filled_qty × fill_price + fees)
```

SELL:

```text
position_delta = -filled_qty
cash_delta = +(filled_qty × fill_price - fees)
```

ใช้ transaction และตรวจ fencing token ก่อน commit

### Step 15 — Realized Cycle Gate

สำหรับระบบ rebalancing:

- ขา BUY เดี่ยวไม่ใช่กำไร realized
- ขา SELL เดี่ยวไม่ใช่กำไร realized
- ต้องจับคู่ execution ตามหลักบัญชีที่ระบบกำหนด
- นับ realized result เฉพาะวงจรที่ปิดแล้ว
- `ΔAₙ` และ `Aₙ` ต้องไม่เพิ่มจากการเปลี่ยนราคาเพียงอย่างเดียว

### Step 16 — Release Lock

ปล่อย lock ที่ node เดียวหลัง persist ผลลัพธ์แล้ว

ห้าม invocation เก่าปล่อย lock ของ invocation ใหม่ ต้องตรวจ fencing token ทุกครั้ง

## โครงสร้างข้อมูลแนะนำ

```text
order_intents/{client_order_id}
  intent
  payload_hash
  broker_order_id
  state
  requested_qty
  filled_qty
  remaining_qty
  avg_fill_price
  fees
  environment
  fencing_token
  created_at
  updated_at
  last_reconciled_at
  last_error

order_intents/{client_order_id}/events/{event_id}
  source
  broker_status
  event_type
  filled_qty_delta
  fill_price
  raw_response_redacted
  received_at
```

Event records ต้อง append-only เท่าที่ทำได้

## Error Classification

แบ่งข้อผิดพลาดเป็น:

### AUTH_ERROR

ตัวอย่าง:

- signature ไม่ถูกต้อง
- token ไม่ NORMAL
- token expired
- environment mismatch

การตอบสนอง: ห้าม retry place จนกว่า auth จะถูกแก้

### VALIDATION_ERROR

ตัวอย่าง:

- quantity ไม่ถูกต้อง
- buying power ไม่พอ
- position ไม่พอ
- order type ไม่รองรับ

การตอบสนอง: ปฏิเสธ intent และแสดง field ที่ผิด

### BROKER_REJECTION

บันทึก broker code/message โดยไม่แปลความเกินหลักฐาน

### NETWORK_TIMEOUT

เปลี่ยนเป็น reconciliation flow ห้าม blind retry

### RATE_LIMIT

ใช้ bounded backoff เฉพาะ read/reconciliation calls

สำหรับ place/replace/cancel ต้องตรวจสถานะก่อน retry ทุกครั้ง

### DATA_CONFLICT

เมื่อ local state ขัดกับ broker ให้ broker เป็น source of truth และสร้าง audit event

## UAT Checklist

ต้องทดสอบอย่างน้อย:

1. BUY สำเร็จและ fill ครบ
2. SELL สำเร็จและ fill ครบ
3. preview reject
4. place reject
5. partial fill
6. cancel ก่อน fill
7. fill ระหว่าง cancel
8. replace price
9. timeout หลัง place แต่ broker รับ order แล้ว
10. duplicate scheduler invocation
11. stale lock และ TTL recovery
12. token หมดอายุ
13. buying power ไม่พอ
14. sell quantity มากกว่า position
15. restart process แล้ว reconcile ต่อได้
16. event มาถึงซ้ำหรือผิดลำดับ
17. UAT host ไม่ปะปนกับ production

## Production Gate

ห้ามเปิด production จนกว่า:

- UAT checklist ผ่าน
- secrets ไม่อยู่ใน repository
- logging redaction ผ่าน
- duplicate protection ผ่าน
- timeout reconciliation ผ่าน
- partial fill ผ่าน
- emergency disable switch พร้อม
- max order value และ max daily exposure พร้อม
- audit trail ตรวจย้อนหลังได้
- มี manual kill switch

## รูปแบบการตรวจโค้ด

เมื่อผู้ใช้ขอ review ให้รายงานตามลำดับ:

1. **เห็นด้วย / ไม่เห็นด้วย** กับหลักการ
2. จุดที่ถูกต้อง
3. จุดที่เสี่ยงหรือผิด
4. เส้นทางที่ทำให้เกิด duplicate order
5. เส้นทางที่ local state อาจไม่ตรง broker
6. ปัญหา partial fill
7. ปัญหา timeout/retry
8. ปัญหา realized accounting
9. patch ที่ควรแก้
10. test cases ที่พิสูจน์ได้

ห้ามรับรองว่า “100% ถูกต้อง” หากยังไม่ได้ทดสอบ UAT และตรวจ broker reconciliation จริง

## Output ที่สกิลต้องสร้างได้

- Mermaid flowchart
- state transition table
- Python pseudocode หรือ production patch
- Firestore schema
- UAT test matrix
- failure-mode analysis
- reconciliation algorithm
- checklist ก่อน production

## ตัวอย่างคำสั่งเรียกใช้

```text
/webull-order-flow ตรวจ main.py ว่ามีโอกาสยิงคำสั่งซ้ำหรือไม่
```

```text
/webull-order-flow ออกแบบ flow BUY/SELL ที่รองรับ partial fill และ timeout
```

```text
/webull-order-flow ตรวจ UAT ก่อนเปิด production โดยยึด Webull OpenAPI เป็นหลัก
```

```text
/webull-order-flow ตรวจว่า ΔAₙ และ Aₙ อัปเดตเฉพาะรอบซื้อ-ขายที่ปิดจริงหรือไม่
```
