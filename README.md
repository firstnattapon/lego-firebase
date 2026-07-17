# Deploy — LEGO × Firebase RTDB × Cloud Functions × Scheduler × Streamlit

Stack: **Scheduler (เวลา) → Cloud Function (engine) → RTDB (data) → Streamlit (dashboard)**

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
    "webull_lego_errors":{ ".read": false, ".write": false }
  }
}
```
> service account ของ Cloud Function (Admin SDK) ข้าม rules อยู่แล้ว จึงเขียนได้; client อื่นอ่านได้อย่างเดียว

## 3. Deploy Cloud Function (Gen2, HTTP)

```bash
gcloud functions deploy lego-one-row \
  --gen2 --runtime=python312 --region=$REGION \
  --source=./functions --entry-point=lego_one_row \
  --trigger-http --no-allow-unauthenticated \
  --memory=512Mi --timeout=120s \
  --set-env-vars=FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=APLS,LEGO_FIX_C=1500,LEGO_DIFF=60,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=5,LEGO_SLOT_SECONDS=1800,AUTO_SUBMIT=false \
  --set-secrets=WEBULL_APP_KEY=webull-app-key:latest,WEBULL_APP_SECRET=webull-app-secret:latest,WEBULL_ACCOUNT_ID=webull-account-id:latest \
  --project=$PROJECT
```
> UAT ก่อนเสมอ (`WEBULL_ENV=UAT`), `AUTO_SUBMIT=false` จน pipeline นิ่ง แล้วค่อยเปิด

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
```
> cron ยิงเผื่อไว้ 13:00–20:xx UTC; `is_us_market_open()` ตัดนอกเวลา/สุดสัปดาห์ (PASS_MARKET_CLOSED)
> `--max-retry-attempts=0` + `LEGO_SLOT_SECONDS=1800` = สองชั้นกัน retry สร้าง 2 แถวใน slot เดียว (กิน DNA step ซ้ำ)

## 5. Streamlit (streamlit.app)

- ชี้ที่ `dashboard/streamlit_app.py`
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

## ✅ ตรวจ invariant หลัง deploy
1. ทุกแถวผ่าน `validate_row_columns` (17 คอลัมน์)
2. ยิงซ้ำ snapshot เดิม → no-op (idempotent), anchor เก่า → StaleAnchorError
3. `version` เดินหน้า monotonic +1 ทุก commit
4. order ส่งได้เฉพาะ UAT + READY_* + ผ่าน submit gate; Production read-only
5. FILLED ยืนยันจาก Trade Events (gRPC) เท่านั้น — ไม่โม้จาก SUBMITTED
