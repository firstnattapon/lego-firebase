# Deploy — LEGO × Firebase RTDB × Cloud Functions × Scheduler × Streamlit

Stack: **Scheduler (เวลา) → Cloud Function (engine) → RTDB (data) → Streamlit (dashboard)**

> ต้องการคู่มือเริ่มต้นแบบ Cloud Shell + GitHub auto deploy + Streamlit? ดู [QUICKSTART_TH.md](QUICKSTART_TH.md)

สองเครื่องจักรอิสระ ห้ามเอา state ฝั่งหนึ่งไปคุมอีกฝั่ง:

| module | หน้าที่ |
|---|---|
| `main.py` | Cloud Function 2 ตัว: `lego_one_row` (เดิน DNA) และ `lego_order_worker` (ส่ง/ตาม order) |
| `market_clock.py` | นาฬิกาตลาด: slot, market ordinal, ปฏิทิน NYSE, calendar fingerprint |
| `lego_one_row.py` | สมการ 17 คอลัมน์: DNA step/signal, decision, recurrence Rₙ/ΔAₙ/Aₙ/Eₙ |
| `dna_engine.py` | ถอด DNA code เป็น gate array 0/1 |
| `lego_state.py` | Step 18 persistence: transaction, idempotency, guard ทุกตัว, realized ledger |
| `lego_outbox.py` | order outbox (1 decision = 1 intent) แยกจาก DNA pointer |
| `lego_orders.py` | submit gate, normalize ผล broker, จับคู่ fill เป็น realized |
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

```json
{
  "rules": {
    "webull_lego_rows":  { ".read": true, ".write": false },
    "webull_lego_state": { ".read": true, ".write": false },
    "webull_lego_order_audit": { ".read": true, ".write": false },
    "webull_lego_order_outbox": { ".read": false, ".write": false },
    "webull_lego_realized": { ".read": false, ".write": false },
    "webull_lego_errors":{ ".read": false, ".write": false }
  }
}
```
> service account ของ Cloud Function (Admin SDK) ข้าม rules อยู่แล้ว จึงเขียนได้; client อื่นอ่านได้อย่างเดียว

## 3. Deploy Cloud Functions (Gen2, HTTP) — ต้อง deploy ทั้ง 2 ตัว

```bash
ENVS=FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=APLS,LEGO_FIX_C=1500,LEGO_DIFF=60,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=5,LEGO_SLOT_SECONDS=1800,AUTO_SUBMIT=false
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
```
> UAT ก่อนเสมอ (`WEBULL_ENV=UAT`), `AUTO_SUBMIT=false` จน pipeline นิ่ง แล้วค่อยเปิด
> **ไม่ deploy `lego-order-worker` = intent ทุกใบจะหมดอายุเป็น `EXPIRED_UNSENT`** เพราะ
> `LEGO_INLINE_ORDER_WORKER` default `false` (ตั้งใจ: broker latency ห้ามถ่วงเวลา DNA)

ตาราง env ครบทุกตัวอยู่ใน [QUICKSTART_TH.md](QUICKSTART_TH.md) หัวข้อ 6

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
| `CONFIG_ERROR` | 500 | `LEGO_SLOT_SECONDS` ไม่ตั้ง/ไม่รองรับ |
| `SNAPSHOT_OR_ENGINE_ERROR` | 500/503 | 503 เมื่อเป็น transient |

## ✅ ตรวจ invariant หลัง deploy
1. ทุกแถวผ่าน `validate_row_columns` (17 คอลัมน์)
2. ยิงซ้ำ snapshot เดิม → no-op (idempotent), anchor เก่า → StaleAnchorError
3. `version` เดินหน้า monotonic +1 ทุก commit และ `market_ordinal` เดินหน้าเสมอ
4. หนึ่ง slot หนึ่งแถว — retry ใน slot เดิมได้ `SLOT_CONSUMED` ไม่ใช่แถวใหม่
5. order ส่งได้เฉพาะ UAT + READY_* + row committed แล้ว + ผ่าน submit gate; Production read-only
6. FILLED ยืนยันจาก order detail ของ broker เท่านั้น — ไม่โม้จาก SUBMITTED
7. commit แถวก่อน แล้วค่อยเขียน outbox — order พังต้องไม่ rollback แถวและไม่ขวาง slot ถัดไป
