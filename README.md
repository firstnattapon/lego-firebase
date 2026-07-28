# Deploy — LEGO × Firebase RTDB × Cloud Functions × Scheduler × Streamlit

Stack: **Scheduler (เวลา) → Cloud Function (engine) → RTDB (data) → Streamlit (dashboard)**

> ต้องการคู่มือเริ่มต้นแบบ Cloud Shell + GitHub auto deploy + Streamlit? ดู [QUICKSTART_TH.md](QUICKSTART_TH.md)

สองเครื่องจักรอิสระ ห้ามเอา state ฝั่งหนึ่งไปคุมอีกฝั่ง:

| module | หน้าที่ |
|---|---|
| `main.py` | Cloud Function 3 ตัว: `lego_one_row` (เดิน DNA + ตัดสินใจ), `lego_order_worker` (ส่ง/ตาม order + finalize ΔAₙ/Aₙ/Eₙ), `lego_archive_worker` (ย้าย record ที่จบแล้วออกจาก path ที่ loop สแกน) |
| `market_clock.py` | นาฬิกาตลาด: slot, market ordinal, ปฏิทิน NYSE, calendar fingerprint |
| `lego_one_row.py` | สมการ 17 คอลัมน์: DNA step/signal, decision, Rₙ และ `finalize_recurrence` (ΔAₙ/Aₙ/Eₙ จาก fill จริง) |
| `dna_engine.py` | ถอด DNA code เป็น gate array 0/1 |
| `lego_state.py` | Step 18 persistence: transaction, idempotency, guard ทุกตัว, realized ledger |
| `lego_outbox.py` | order outbox (1 decision = 1 intent) แยกจาก DNA pointer |
| `lego_orders.py` | submit gate, normalize ผล broker, จับคู่ fill เป็น realized |
| `lego_archive.py` | ย้าย intent/audit ที่ terminal แล้วไป `*_archive` (ไม่ลบ) ให้ path ที่ scan โตตามงานที่ยังค้างเท่านั้น |
| `webull_io.py` | adapter ของ Webull OpenAPI (snapshot, position, preview/place/detail) |
| `find_origin.py` | เครื่องมือหา `LEGO_DNA_ORIGIN_UTC` ก่อนเปิด clock mode `market` |

```
PROJECT=your-gcp-project
REGION=asia-southeast1
DB_URL=https://your-project-default-rtdb.asia-southeast1.firebasedatabase.app
```

## 1. Secret Manager (ห้าม hardcode / commit key)

```bash
for s in webull-app-key webull-app-secret webull-account-id; do
  printf "%s" "<value>" | gcloud secrets create $s --data-file=- --project=$PROJECT
done
```

## 2. RTDB security rules (dashboard อ่านอย่างเดียว; เขียนผ่าน service account เท่านั้น)

repo นี้ track policy ตัวจริงไว้ที่ `database.rules.json` และ `firebase.json`
เพื่อไม่ให้ rules ในเอกสาร drift จาก deployment:

```bash
firebase deploy --only database --project="$PROJECT"
```

```json
{
  "rules": {
    ".read": false,
    ".write": false,
    "webull_lego_rows":  { ".read": true, ".write": false },
    "webull_lego_state": { ".read": true, ".write": false },
    "webull_lego_order_audit": { ".read": true, ".write": false },
    "webull_lego_order_audit_archive": { ".read": true, ".write": false },
    "webull_lego_order_outbox": { ".read": false, ".write": false },
    "webull_lego_order_outbox_archive": { ".read": false, ".write": false },
    "webull_lego_realized": { ".read": false, ".write": false },
    "webull_lego_errors":{ ".read": false, ".write": false },
    "webull_lego_warnings":{ ".read": true, ".write": false }
  }
}
```
> service account ของ Cloud Function (Admin SDK) ข้าม rules อยู่แล้ว จึงเขียนได้; client อื่นอ่านได้อย่างเดียว
> root/unmatched paths deny โดย default; client write ถูกปิดทุก path

## 3. Deploy Cloud Functions (Gen2, HTTP) — ต้อง deploy ทั้ง 3 ตัว

```bash
# LEGO_DNA_ORIGIN_UTC + LEGO_DNA_CLOCK_MODE อยู่ในชุดนี้ตั้งแต่ต้น ไม่ใช่ของเสริมทีหลัง:
# ไม่ตั้ง origin = clock resolve ไม่ได้ = โหมด degraded ซึ่ง "commit แถวได้ปกติ แต่ไม่สร้าง
# order intent เลยสักใบ" (response จะมี outbox_skipped + นับใน webull_lego_warnings)
# หาค่า origin ด้วย `python find_origin.py <dna_step ถัดไป>` ก่อน แล้วค่อย deploy
ENVS=FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=APLS,LEGO_FIX_C=1500,LEGO_DIFF=60,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=5,LEGO_SLOT_SECONDS=1800,LEGO_DNA_ORIGIN_UTC=2026-07-23T13:30:00Z,LEGO_DNA_CLOCK_MODE=market,AUTO_SUBMIT=false
SECRETS=WEBULL_APP_KEY=webull-app-key:latest,WEBULL_APP_SECRET=webull-app-secret:latest,WEBULL_ACCOUNT_ID=webull-account-id:latest

gcloud functions deploy lego-one-row \
  --gen2 --runtime=python312 --region=$REGION \
  --source=. --entry-point=lego_one_row \
  --trigger-http --no-allow-unauthenticated \
  --memory=512Mi --timeout=120s \
  --set-env-vars=$ENVS --set-secrets=$SECRETS --project=$PROJECT

gcloud functions deploy lego-order-worker \
  --gen2 --runtime=python312 --region=$REGION \
  --source=. --entry-point=lego_order_worker \
  --trigger-http --no-allow-unauthenticated \
  --memory=512Mi --timeout=300s \
  --set-env-vars=$ENVS --set-secrets=$SECRETS --project=$PROJECT

gcloud functions deploy lego-archive-worker \
  --gen2 --runtime=python312 --region=$REGION \
  --source=. --entry-point=lego_archive_worker \
  --trigger-http --no-allow-unauthenticated \
  --memory=512Mi --timeout=300s \
  --set-env-vars=$ENVS --set-secrets=$SECRETS --project=$PROJECT
```
> UAT ก่อนเสมอ (`WEBULL_ENV=UAT`), `AUTO_SUBMIT=false` จน pipeline นิ่ง แล้วค่อยเปิด
> `WEBULL_ENV` รับเฉพาะ `UAT`, `PROD`, `PRODUCTION` (ไม่สนตัวพิมพ์เล็ก/ใหญ่);
> ค่าอื่นเป็น `CONFIG_ERROR` และจะไม่ไหลไป Production
> **ไม่ deploy `lego-order-worker` = intent ทุกใบจะหมดอายุเป็น `EXPIRED_UNSENT`** เพราะ
> `LEGO_INLINE_ORDER_WORKER` default `false` (ตั้งใจ: broker latency ห้ามถ่วงเวลา DNA)
> `lego-archive-worker` เป็นงานบ้าน (วันละครั้งพอ) ไม่ deploy ก็เทรดได้ แต่ path
> `webull_lego_order_outbox` / `webull_lego_order_audit` จะโตไม่หยุดและทุก tick ต้องโหลดทั้งก้อน

ตาราง env ครบทุกตัวอยู่ใน [QUICKSTART_TH.md](QUICKSTART_TH.md) หัวข้อ 6

### แบ่งหน้าที่: decision (`lego_one_row`) กับ execution (`lego_order_worker`)

`READY_BUY`/`READY_SELL` คือ **เจตนา** ไม่ใช่การซื้อขายที่สำเร็จ ทั้งสองฝั่งจึงเขียน state
คนละชุด และไม่มีใครเขียนของอีกฝั่ง:

| ฝั่ง | เขียนอะไร | เงื่อนไข |
|---|---|---|
| `lego_one_row` | decision pointer: `version`, `dna_step`, `p0`, `slot_id`, `market_ordinal` + คอลัมน์ตัดสินใจทั้งหมด และ `Rₙ` (live ทุกแถว) | ทุก slot ที่ commit สำเร็จ |
| `lego_order_worker` | execution cashflow: `execution_cashflow.last_action_price` (P_acted), `execution_cashflow.actual_cumulative` (Aₙ) + คอลัมน์ `ΔAₙ`/`Aₙ`/`Eₙ` ของแถวนั้น | เฉพาะเมื่อ broker ยืนยัน `cumulative_filled_quantity > 0` **และ** อ่าน holdings หลัง fill แล้วเปลี่ยนจริง |

- แถวที่ commit แล้วแต่ยังไม่ fill มี `cashflow_status = PENDING_EXECUTION` และ `ΔAₙ = 0`
  (`Aₙ` ค้างที่ค่าจาก fill ล่าสุด) — `SUBMITTED`/`PENDING_DISPATCH`/`READY_*` ไม่เคยเพิ่ม `Aₙ`
- fill แล้ว → `cashflow_status = FINALIZED` พร้อม `execution_price`, `execution_quantity`,
  `post_execution_holdings` (ทั้งหมดอยู่นอก 17 คอลัมน์)
- `ΔAₙ` ใช้ **filled_price จริง** ไม่ใช่ `decision_price`; holdings ใช้ค่าที่อ่านกลับจาก broker
  ไม่ใช่จำนวนที่สั่ง
- finalize เป็น transaction เดียว idempotent ที่ `run_id` (= `client_order_id`) → retry, poll ซ้ำ
  ของ partial fill และ worker หลาย instance บันทึกได้ครั้งเดียว
- fill ที่ broker ยืนยันแต่ holdings ยังไม่ขยับ → `AWAITING_FILL_CONFIRMATION` (ไม่ terminal)
  แล้วลองใหม่จนถึงเพดาน `LEGO_FILL_CONFIRM_MAX_ATTEMPTS` (default 5) จึงปล่อยออกจากคิว
  พร้อม `needs_manual_check` — ห้าม book cashflow จากคำพูด broker อย่างเดียว

> **อัปเกรด chain เดิม:** `cashflow_semantics` เปลี่ยนเป็น `execution_confirmed_v1`
> ความหมายของ `Aₙ` ต่างจาก `gated_theoretical_v2` (อันเดิมนับ decision เป็น act) จึงลากต่อกันไม่ได้
> — chain เดิมจะ **รีเซ็ต baseline `Aₙ` เป็น 0** ในรอบแรกหลัง deploy โดย DNA/slot/`version` เดินต่อปกติ
> ส่วน `P_acted` ใช้ `prev_price` เดิมเป็นค่าตั้งต้น (กติกาเดียวกับที่ใช้มาทุกครั้งที่ semantics เปลี่ยน)

> `Eₙ = Aₙ − Rₙ ≥ 0` เป็นจริงเมื่อ `Aₙ` กับ `Rₙ` เดินบนราคาชุดเดียวกัน ตอนนี้ `Aₙ` ใช้ราคา fill
> ส่วน `Rₙ` ใช้ราคาที่ตัดสินใจ ดังนั้น `Eₙ` ติดลบเล็กน้อยได้เท่ากับ slippage ที่จ่ายจริง —
> นี่คือสิ่งที่ตั้งใจวัด ไม่ใช่ความผิดพลาด

### อัปเกรด chain เดิม: runtime identity guard

state ใหม่เก็บเฉพาะ SHA-256 fingerprint ของ `WEBULL_ACCOUNT_ID` + environment
(ไม่เก็บ account ID จริง) เพื่อห้ามนำ anchor/outbox เดิมไปใช้ข้ามบัญชีหรือ UAT/Production

- chain ใหม่: ไม่ต้องทำอะไร
- chain เดิมที่ยังไม่มี fingerprint: **adopt อัตโนมัติ** — commit แรกหลัง deploy จะผูก chain
  กับ account/environment ปัจจุบัน แล้วนับที่ `webull_lego_warnings/runtime_identity_adopted`
  (ไม่ต้องตั้ง env อะไร ไม่ต้อง deploy สองรอบ และ DNA ไม่หยุดสักรอบ)
  → **หน้าที่ operator: หลัง deploy ให้เปิด warning นั้นดูหนึ่งครั้ง** ว่า `WEBULL_ACCOUNT_ID`
  กับ `WEBULL_ENV` เป็นชุดที่ตั้งใจจริง
- fingerprint ไม่ตรง: fail closed เป็น `CONFIG_ERROR` — ไม่มีสวิตช์ให้ข้าม ถ้าตั้งใจย้ายบัญชี
  ต้องเริ่ม chain ใหม่

## 4. Cloud Scheduler (ทุก 30 นาที ในกรอบตลาดสหรัฐฯ; โค้ด guard วันหยุดเอง)

```bash
URL=$(gcloud functions describe lego-one-row --gen2 --region=$REGION --format='value(serviceConfig.uri)')
SA=$(gcloud functions describe lego-one-row --gen2 --region=$REGION --format='value(serviceConfig.serviceAccountEmail)')

gcloud scheduler jobs create http lego-tick \
  --location=$REGION --schedule="*/30 13-20 * * 1-5" --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$URL" --http-method=POST \
  --oidc-service-account-email="$SA" --oidc-token-audience="$URL" \
  --project=$PROJECT

WURL=$(gcloud functions describe lego-order-worker --gen2 --region=$REGION --format='value(serviceConfig.uri)')
gcloud scheduler jobs create http lego-order-tick \
  --location=$REGION --schedule="*/5 13-20 * * 1-5" --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$WURL" --http-method=POST \
  --oidc-service-account-email="$SA" --oidc-token-audience="$WURL" \
  --project=$PROJECT

AURL=$(gcloud functions describe lego-archive-worker --gen2 --region=$REGION --format='value(serviceConfig.uri)')
gcloud scheduler jobs create http lego-archive-tick \
  --location=$REGION --schedule="30 22 * * *" --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$AURL" --http-method=POST \
  --oidc-service-account-email="$SA" --oidc-token-audience="$AURL" \
  --project=$PROJECT
```
> cron ยิงเผื่อไว้ 13:00–20:30 UTC (ครอบทั้ง EDT 13:30–20:00 และ EST 14:30–21:00);
> `market_clock.is_regular_session()` ตัดนอกเวลาด้วยปฏิทินเดียวกับที่คำนวณ ordinal —
> **9:30–16:00 America/New_York (DST-aware), รู้จักวันหยุดและ early close 13:00** → `PASS_MARKET_CLOSED`
> `--max-retry-attempts=0` + `LEGO_SLOT_SECONDS=1800` = สองชั้นกัน retry สร้าง 2 แถวใน slot เดียว (กิน DNA step ซ้ำ)
> order worker ยิงถี่กว่า slot ได้ (ไม่กระทบ DNA) — มันแค่ไล่ intent ที่ค้างในหน้าต่างของ slot นั้น

## 5. Streamlit (streamlit.app)

- dashboard อยู่คนละ repo: `firstnattapon/lego-firebase-streamlit` — ชี้ Main file path ที่ `streamlit_app.py` (root ของ repo นั้น)
- ใน **Secrets** ของ streamlit.app ใส่:
  ```toml
  FIREBASE_DB_URL = "https://your-project-default-rtdb.asia-southeast1.firebasedatabase.app"
  FIREBASE_SA_JSON = '{...service account json แบบ read-only...}'
  ```

## 6. Smoke test (UAT)

```bash
gcloud scheduler jobs run lego-tick --location=$REGION --project=$PROJECT
# ดู log / RTDB /webull_lego_rows ว่ามี 1 แถวใหม่ + /webull_lego_state version เดินหน้า
# ยิงซ้ำด้วย snapshot เดิม -> ควร idempotent (no-op) หรือ StaleAnchorError ถ้า version ขยับแล้ว
```

## pipeline_status ที่ `lego_one_row` คืน

| pipeline_status | HTTP | เมื่อไหร่ |
|---|---|---|
| `ROW_COMMITTED` | 200 | commit สำเร็จ (หรือ idempotent) |
| `MARKET_CLOSED` | 200 | นอกเวลาเทรด / วันหยุด / ไม่มี slot |
| `SLOT_CONSUMED` | 200 | slot นี้ commit ไปแล้ว |
| `STALE_ANCHOR` | 409 | anchor ไม่ตรง state |
| `CALENDAR_DRIFT` | 409 | ปฏิทิน/slot config เปลี่ยนหลัง commit แรก |
| `ORDINAL_REGRESSION` | 409 | slot ใหม่ให้ ordinal ที่ไม่เดินหน้า — DNA เดินถอยไม่ได้ |
| `HOLDINGS_ANOMALY` | 409 | chain เคยเห็นของ แต่ snapshot อ่านได้ 0 — ไม่ commit ไม่ยิง order |
| `DNA_DRIFT` | 409 | `dna_code` เดิมแต่ decode ได้ gate array คนละชุด (มักคือ numpy เปลี่ยนเวอร์ชัน) |
| `DNA_EXHAUSTED` | 200 | DNA เดินจนหมด array (`bypass:100` ที่ slot 30m ≈ 8 วันทำการ) — เป็นจุดจบที่คาดไว้ ไม่ใช่ระบบพัง ต้องต่อ DNA ใหม่หรือหยุด scheduler |
| `CONFIG_ERROR` | 500 | config ไม่ปลอดภัย/ไม่รองรับ เช่น slot, `WEBULL_ENV`, account หรือ runtime identity |
| `SNAPSHOT_OR_ENGINE_ERROR` | 500/503 | 503 เมื่อเป็น transient |

field เตือนที่จะโผล่ใน response ของแถวที่ commit สำเร็จ (ไม่มี = ไม่มีอะไรต้องดู):

| field | แปลว่า |
|---|---|
| `outbox_skipped` | แถวนี้เป็น `READY_*` และ `AUTO_SUBMIT=true` แต่ **ไม่มี order intent ถูกสร้าง** เพราะ clock degraded (ไม่มี slot จึงคำนวณ `expires_at` ไม่ได้) — นับสะสมที่ `webull_lego_warnings/degraded_clock_no_order` |
| `outbox_blocked` | แถวนี้เป็น `READY_*` และ `AUTO_SUBMIT=true` แต่ **preflight ไม่ผ่าน** จึงไม่สร้าง intent — `outbox_blocked_checks` บอกว่าติดข้อไหน นับสะสมที่ `webull_lego_warnings/auto_submit_blocked` |
| `outbox_error` | materialize intent ไม่สำเร็จ (แถว commit แล้ว ไม่ rollback; state เก็บ recovery marker และ worker จะลองซ้ำ) |
| `clock_warning` | market clock resolve ไม่ได้ จึงเดินด้วย legacy step |
| `dna_steps_remaining` | DNA เหลือน้อยกว่า `LEGO_DNA_LOW_WATERMARK` (default 10) แล้ว |

## ✅ ตรวจ invariant หลัง deploy
1. ทุกแถวผ่าน `validate_row_columns` (17 คอลัมน์)
2. ยิงซ้ำ snapshot เดิม → no-op (idempotent), anchor เก่า → StaleAnchorError
3. `version` เดินหน้า monotonic +1 ทุก commit และ `market_ordinal` เดินหน้าเสมอ
4. หนึ่ง slot หนึ่งแถว — retry ใน slot เดิมได้ `SLOT_CONSUMED` ไม่ใช่แถวใหม่
5. order ส่งได้เฉพาะ UAT + READY_* + row committed แล้ว + ผ่าน submit gate; Production read-only
6. FILLED ยืนยันจาก order detail ของ broker เท่านั้น — ไม่โม้จาก SUBMITTED
7. commit แถวก่อน แล้วค่อยเขียน outbox — order พังต้องไม่ rollback แถวและไม่ขวาง slot ถัดไป
8. order ใบเดียวที่ค้างต้องไม่ขวางใบถัดไป — `PLACING_UNKNOWN` มีเพดาน
   (`LEGO_RECONCILE_MAX_ATTEMPTS`) แล้วจบเป็น `RECONCILE_ABANDONED` + `needs_manual_check`
9. จำนวนที่ส่ง broker ต้องเท่าจำนวนที่ตัดสินใจเสมอ ทุกค่า `LEGO_DECIMAL_PRECISION` รวม `0`
10. snapshot ที่ทำให้ของหายไปทั้งก้อนต้องไม่กลายเป็น order — `gap = FIX_C` คือ order ใหญ่สุด
    ที่กลยุทธ์สร้างได้ และการ rebalance ปกติทำให้ holdings เป็น 0 ไม่ได้
11. `dna_code` เดิมต้อง decode ได้ gate array เดิมตลอดอายุ chain — `dna_fingerprint` ใน state
    บังคับข้อนี้ และ `numpy` ถูก pin ตายตัวเพราะ `Generator` ไม่รับประกัน bit stream ข้ามเวอร์ชัน
12. การตัดสินใจ "ไม่ส่ง order" ต้องมีที่ให้เห็นเสมอ — เงียบไม่ได้ ถ้า `AUTO_SUBMIT=true` แล้วแถวเป็น
    `READY_*` ต้องได้อย่างใดอย่างหนึ่ง: intent ใน outbox, `outbox_error`, `outbox_skipped`
    หรือ `outbox_blocked`
16. `AUTO_SUBMIT=true` เป็นแค่ 1 ใน 8 เงื่อนไขของ `auto_submit_preflight` ไม่ใช่สวิตช์เดียว —
    token ไม่พร้อม / clock degraded / step ไม่ตรง market ordinal / DNA ใกล้หมด ต้อง block
    การสร้าง intent เสมอ และ preflight ที่ throw ต้องนับเป็น "ไม่ผ่าน" ไม่ใช่ปล่อยผ่าน
13. ราคาที่เข้าสมการต้องเป็นราคาของ `LEGO_SYMBOL` เท่านั้น — snapshot ที่ตอบมาเป็น symbol อื่น
    ต้อง fail closed ไม่ใช่เอามาคิด `gap`
14. order ที่ fill แล้วต้องไม่ถูกส่งซ้ำ แม้ realized ledger จะคำนวณต่อไม่ได้ — จบเป็น
    `REALIZED_MATH_ERROR` + `needs_manual_check` (คนละเรื่องกับ "ไม่รู้ว่า order มีจริงไหม")
15. `webull-openapi-python-sdk` ต้อง pin exact version — SDK คุม signing/auth/order payload
    โดยตรง การอัปเดตต้องผ่าน UAT ก่อนเสมอ (เหตุผลเดียวกับ numpy)
17. worker ต้อง claim intent ด้วย RTDB transaction ก่อนทำงาน และผ่าน generation fence
    ก่อน `place_order`; lease หมดอายุไม่ทำให้ worker เก่าวิ่งข้าม fence
18. open-order pagination ต้องพิสูจน์ว่าครบทุกหน้า; cursor หาย/ไม่เดิน/ชนเพดาน =
    คง `PENDING_DISPATCH` และไม่ส่ง order
19. fill ที่ยืนยันแล้วแต่ไม่มี quantity/price ต้องจบ `REALIZED_MATH_ERROR` +
    `needs_manual_check`; ห้ามปิดเป็น `FILLED` แบบ ledger ไม่ครบ
20. audit ที่เขียนไม่สำเร็จต้องมี `audit_pending` ใน private outbox และถูกซ่อมก่อน archive
21. `ΔAₙ`/`Aₙ`/`Eₙ` ขยับได้จาก fill ที่ broker ยืนยันเท่านั้น — `READY_*`, `PENDING_DISPATCH`
    และ `SUBMITTED` ห้ามเพิ่ม `Aₙ`; PASS ให้ `ΔAₙ = 0` และ `Aₙ` คงเดิม
22. finalize ต้อง idempotent ที่ `run_id` — partial fill ใช้ cumulative quantity และ finalize
    ครั้งเดียว, worker หลาย instance ต้องได้ผลเดียวกัน, retry ต้องไม่คำนวณซ้ำ
23. finalize ห้ามแตะ decision pointer (`version`, `dna_step`, `p0`, `slot_id`, `market_ordinal`)
    — cashflow อยู่ใต้ `execution_cashflow` และ commit รอบถัดไปต้องพา state นั้นไปต่อ ไม่ทับ
