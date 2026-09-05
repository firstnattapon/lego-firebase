# รายงานแก้ Webull response contract — 2026-09-05

Repository: `https://github.com/firstnattapon/lego-firebase`  
ฐานโค้ดที่ตรวจและแก้: `474230879d36f1c9e81399dff458400de9575705`

## ผลลัพธ์

แก้ข้อบกพร่องที่ยืนยันได้ครบ 2 ข้อ โดยไม่เปลี่ยน DNA, recurrence, strategy semantics,
UAT-only submit gate, `client_order_id`, outbox/chain fence หรือกติกาห้าม retry Place
อัตโนมัติ

1. Open Orders อ่าน response ทางการที่เป็น array ของ wrapper และเปิด `orders`
   ภายในได้แล้ว ยังคงรองรับ `items` และ flat legacy shapes ใช้
   `client_order_id` ระดับ wrapper เป็น pagination cursor ก่อน และ fail closed เมื่อ
   wrapper ผิดรูปแบบหรือ pagination ยืนยันความครบไม่ได้
2. Order Detail รวม `commission.actual_commission` กับผลรวม
   `fees[].actual_value` ด้วย `Decimal` โดย nested actual breakdown มี precedence
   เหนือ scalar aliases จึงไม่รวมซ้ำ ไม่ใช้ receivable แทน actual และปฏิเสธค่าติดลบ,
   NaN, Infinity หรือค่าที่แปลงไม่ได้

เมื่อ broker ส่ง fee structure มาแล้วแต่ actual values ยังไม่ครบ terminal fill จะอยู่ใน
`AWAITING_EXECUTION_FEES` และอ่าน Order Detail ซ้ำแบบมีเพดาน (ค่าเริ่มต้น 5 ครั้ง,
ตั้งด้วย `LEGO_FEE_CONFIRM_MAX_ATTEMPTS`) ก่อนเปลี่ยนเป็น manual-check terminal
แทนการบันทึก fee เป็นศูนย์เงียบ ๆ ค่าธรรมเนียมที่มาช้าถูกใช้เป็น cumulative fee และ
realized ledger ป้องกัน replay ซ้ำตามเดิม

เพื่อคง compatibility กับ UAT/SDK response รุ่นเดิมที่ไม่ส่ง fee section เลย ระบบยังใช้
lifecycle เดิม แต่ summary ระบุ `fee_actual_complete=false` และ
`fee_source=not_reported` ชัดเจน ผลดังกล่าวจึงไม่ใช่หลักฐานว่า net P&L รวม fee ครบ

## หลักฐานก่อนและหลังแก้

- ก่อนแก้: offline reproduction จาก schema ทางการได้ Open Orders `expected=1,
  actual=0` และ execution fee `expected=2.0, actual=0.0`
- หลังเพิ่ม regression ก่อนแก้ source: `14 failed, 156 passed`
- หลังแก้ targeted contract/worker/lifecycle tests: ผ่านทั้งหมด
- หลังแก้ full suite: `660 passed, 1 skipped in 8.72s`
- `python -m compileall -q .`: ผ่าน
- contract checker หลังแก้: Open Orders `1/1` และ fee `2.0/2.0`
- `git diff --check`: ผ่าน (มีเพียงคำเตือน line-ending LF/CRLF ของ Git บน Windows)

Test ที่ข้ามคือ Firebase Database Emulator rules matrix:
`set FIREBASE_DATABASE_EMULATOR_HOST to run the real rules matrix` จึงไม่ได้นับว่า
emulator ผ่านในรอบนี้

Fixture contract อ้างจากสำเนาเอกสารที่ดาวน์โหลดวันที่ 2026-09-05:

- `https://developer.webull.co.th/apis/docs/reference/trade-api/order-open.md`
- `https://developer.webull.co.th/apis/docs/reference/trade-api/order-detail.md`

ไม่มี credentials, token หรือข้อมูลบัญชีจริงใน fixtures

## Evidence matrix

| ข้อพิสูจน์ | Offline/schema | SDK doubles/Fake RTDB | Live UAT | Production |
|---|---:|---:|---:|---:|
| อ่าน documented `orders` wrapper | ผ่าน | ผ่าน | ยังไม่รัน | ไม่รัน |
| matching open order ทำให้ Place count = 0 | ผ่าน | ผ่าน | ยังไม่รัน | ไม่รัน |
| รวม actual commission + fees = 2.0 | ผ่าน | ผ่าน | ยังไม่รัน | ไม่รัน |
| invalid/receivable-only ไม่ถูกนับเป็น actual | ผ่าน | ผ่าน | ยังไม่รัน | ไม่รัน |
| actual fee มาช้าแล้วลง ledger ครั้งเดียว | ผ่าน | ผ่าน | ยังไม่รัน | ไม่รัน |
| full Place → Fill → reconciliation | ไม่ใช่หลักฐาน live | ผ่านเฉพาะแบบจำลอง | ยังไม่รัน | ไม่รัน |

KPI ตามแผน: `G_n=2`, `F_n=0`, `D_n=0` บน deterministic fixtures ดังนั้น offline
acceptance ผ่าน แต่ไม่ได้หมายถึงการรับรอง live UAT หรือ Production trading

## งานที่ยังรอ live UAT

ยังไม่มีการเรียก broker จริง, Place/Replace/Cancel, Firebase write, deploy หรือ push ใน
รอบนี้ การตรวจ live ต่อควรเริ่มด้วย read-only Open Orders ที่ไม่ว่างและ Order Detail
ของ execution ที่มี actual fees โดยปกปิดข้อมูลสำคัญ หากต้องสร้างออเดอร์ใหม่ต้องได้รับ
คำสั่ง Place ที่ระบุ symbol/side/quantity ชัดเจนก่อน
