# Quick Start Guide — LEGO Firebase Dashboard

คู่มือนี้สรุปการ deploy แบบง่ายสำหรับ stack นี้:

```text
Google Cloud Scheduler (Triggering time)
        ↓ เรียกตามเวลา
Google Cloud Functions (run code)
        ↓ เขียนผลลัพธ์
Firebase Realtime Database (data)
        ↓ อ่านข้อมูล
Streamlit Community Cloud / streamlit.app (Dashboard)
```

> เป้าหมาย: ใช้ **Cloud Shell ตั้งแต่ต้นจนจบ**, เชื่อม **GitHub repository** เพื่อให้ deploy อัตโนมัติเมื่อ push code และใช้ Firebase เป็น data store กลาง

---

## 0) สิ่งที่ต้องมีก่อนเริ่ม

1. Google Cloud project ที่เปิด Billing แล้ว
2. Firebase Realtime Database ใน project เดียวกัน
3. GitHub repository ที่เก็บ code นี้
4. Account สำหรับ Streamlit Community Cloud ที่ connect GitHub ได้
5. ค่า credential ของ Webull สำหรับเก็บใน Secret Manager

กำหนดตัวแปรใน Cloud Shell:

```bash
export PROJECT="your-gcp-project"
export REGION="asia-southeast1"
export DB_URL="https://your-project-default-rtdb.asia-southeast1.firebasedatabase.app"
export REPO_URL="https://github.com/<org-or-user>/<repo>.git"
```

ตั้งค่า project:

```bash
gcloud config set project "$PROJECT"
```

---

## 1) เปิด API ที่ต้องใช้ใน Google Cloud

รันใน Cloud Shell:

```bash
gcloud services enable \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  firebase.googleapis.com \
  firebasedatabase.googleapis.com \
  artifactregistry.googleapis.com \
  iamcredentials.googleapis.com
```

---

## 2) Clone repository จาก GitHub ใน Cloud Shell

```bash
git clone "$REPO_URL"
cd "$(basename "$REPO_URL" .git)"
```

ตรวจ branch ที่จะใช้ deploy เช่น `main`:

```bash
git branch --show-current
git status
```

---

## 3) เตรียมไฟล์ requirements สำหรับ Cloud Functions

Cloud Functions for Python จะหาไฟล์ชื่อ `requirements.txt` ที่ root ของ source ที่ deploy ดังนั้นถ้า deploy จาก root repository ให้ copy dependency ของ function เป็นชื่อนี้:

```bash
cp functions_requirements.txt requirements.txt
```

> ถ้าในอนาคตแยก folder `functions/` ให้ย้าย `main.py`, module ที่เกี่ยวข้อง และ `requirements.txt` เข้า folder นั้น แล้วปรับ `--source` ให้ตรง

---

## 4) เก็บ secret ใน Secret Manager

ห้าม hardcode key ลงใน code หรือ commit ลง GitHub ให้ใส่ค่า secret ผ่าน Cloud Shell:

```bash
printf "%s" "<WEBULL_APP_KEY>" | gcloud secrets create webull-app-key --data-file=-
printf "%s" "<WEBULL_APP_SECRET>" | gcloud secrets create webull-app-secret --data-file=-
printf "%s" "<WEBULL_ACCOUNT_ID>" | gcloud secrets create webull-account-id --data-file=-
```

ถ้า secret มีอยู่แล้วและต้องการ update:

```bash
printf "%s" "<NEW_VALUE>" | gcloud secrets versions add webull-app-key --data-file=-
printf "%s" "<NEW_VALUE>" | gcloud secrets versions add webull-app-secret --data-file=-
printf "%s" "<NEW_VALUE>" | gcloud secrets versions add webull-account-id --data-file=-
```

---

## 5) ตั้งค่า Firebase Realtime Database Rules

ใน Firebase Console ไปที่ **Realtime Database → Rules** แล้วใส่ rules แบบ read-only สำหรับ dashboard:

```json
{
  "rules": {
    "webull_lego_rows": { ".read": true, ".write": false },
    "webull_lego_state": { ".read": true, ".write": false },
    "webull_lego_order_audit": { ".read": true, ".write": false },
    "webull_lego_errors": { ".read": false, ".write": false }
  }
}
```

Cloud Function ใช้ Firebase Admin SDK จึงเขียนข้อมูลได้ผ่าน service account ถึงแม้ client ทั่วไปจะเขียนไม่ได้

---

## 6) Deploy Google Cloud Functions — run code

Deploy function แบบ Gen2 HTTP จาก root repository:

```bash
gcloud functions deploy lego-one-row \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source=. \
  --entry-point=lego_one_row \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=512Mi \
  --timeout=120s \
  --set-env-vars="FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=APLS,LEGO_FIX_C=1500,LEGO_DIFF=60,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=5,LEGO_SLOT_SECONDS=1800,AUTO_SUBMIT=false" \
  --set-secrets="WEBULL_APP_KEY=webull-app-key:latest,WEBULL_APP_SECRET=webull-app-secret:latest,WEBULL_ACCOUNT_ID=webull-account-id:latest"
```

แนะนำให้เริ่มด้วย:

- `WEBULL_ENV=UAT`
- `AUTO_SUBMIT=false`

เมื่อระบบนิ่งแล้วค่อยพิจารณาเปิด production / auto submit

---

## 7) Deploy Google Cloud Scheduler — Triggering time

ดึง URL และ service account ของ Cloud Function:

```bash
export FUNCTION_URL="$(gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"
export FUNCTION_SA="$(gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.serviceAccountEmail)')"

echo "$FUNCTION_URL"
echo "$FUNCTION_SA"
```

สร้าง scheduler ให้เรียก function ทุก 30 นาที จันทร์–ศุกร์ ช่วงเวลา UTC ที่ครอบคลุมตลาดสหรัฐฯ:

```bash
gcloud scheduler jobs create http lego-tick \
  --location="$REGION" \
  --schedule="*/30 13-20 * * 1-5" \
  --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --oidc-service-account-email="$FUNCTION_SA" \
  --oidc-token-audience="$FUNCTION_URL"
```

ทดสอบยิง scheduler ด้วยมือ:

```bash
gcloud scheduler jobs run lego-tick --location="$REGION"
```

ดู log function:

```bash
gcloud functions logs read lego-one-row --gen2 --region="$REGION" --limit=50
```

---

## 8) ตั้งค่า GitHub auto deploy สำหรับ Cloud Functions

วิธีง่ายสุดคือใช้ **Cloud Build Trigger** ผูกกับ GitHub repository แล้วให้ deploy ทุกครั้งที่ push เข้า branch หลัก

### 8.1 สร้างไฟล์ `cloudbuild.yaml`

ถ้ายังไม่มีไฟล์ ให้สร้างที่ root repository:

```yaml
steps:
  - name: gcr.io/google.com/cloudsdktool/cloud-sdk:slim
    entrypoint: gcloud
    args:
      - functions
      - deploy
      - lego-one-row
      - --gen2
      - --runtime=python312
      - --region=asia-southeast1
      - --source=.
      - --entry-point=lego_one_row
      - --trigger-http
      - --no-allow-unauthenticated
      - --memory=512Mi
      - --timeout=120s
      - --set-env-vars=FIREBASE_DB_URL=${_DB_URL},WEBULL_ENV=UAT,LEGO_SYMBOL=APLS,LEGO_FIX_C=1500,LEGO_DIFF=60,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=5,LEGO_SLOT_SECONDS=1800,AUTO_SUBMIT=false
      - --set-secrets=WEBULL_APP_KEY=webull-app-key:latest,WEBULL_APP_SECRET=webull-app-secret:latest,WEBULL_ACCOUNT_ID=webull-account-id:latest
substitutions:
  _DB_URL: https://your-project-default-rtdb.asia-southeast1.firebasedatabase.app
options:
  logging: CLOUD_LOGGING_ONLY
```

> ถ้าใช้ region หรือ DB URL อื่น ให้แก้ใน `cloudbuild.yaml` ให้ตรงกับ environment จริง

### 8.2 Connect repository ใน Google Cloud Console

1. ไปที่ **Google Cloud Console → Cloud Build → Triggers**
2. กด **Connect repository**
3. เลือก **GitHub** และ authorize Google Cloud Build
4. เลือก repository ของ project นี้
5. สร้าง trigger ใหม่:
   - Event: **Push to a branch**
   - Branch: `^main$` หรือ branch ที่ใช้จริง
   - Configuration: **Cloud Build configuration file**
   - Location: `/cloudbuild.yaml`
6. Save

### 8.3 ให้สิทธิ์ service account ของ Cloud Build

Cloud Build service account ต้อง deploy function และอ่าน secret ได้ ตัวอย่างคำสั่ง:

```bash
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
export CLOUDBUILD_SA="$PROJECT_NUMBER@cloudbuild.gserviceaccount.com"

for ROLE in \
  roles/cloudfunctions.developer \
  roles/run.admin \
  roles/iam.serviceAccountUser \
  roles/artifactregistry.writer \
  roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$CLOUDBUILD_SA" \
    --role="$ROLE"
done
```

หลังจากนี้ เมื่อ push code เข้า branch ที่ตั้ง trigger ไว้ Cloud Build จะ deploy Cloud Function ให้อัตโนมัติ

---

## 9) Deploy Streamlit Dashboard — streamlit.app

1. Push repository ขึ้น GitHub
2. เข้า <https://streamlit.io/cloud>
3. กด **New app**
4. เลือก repository และ branch
5. Main file path: `streamlit_app.py`
6. กด **Advanced settings → Secrets** แล้วใส่:

```toml
FIREBASE_DB_URL = "https://your-project-default-rtdb.asia-southeast1.firebasedatabase.app"
FIREBASE_SA_JSON = '{"type":"service_account", "project_id":"your-gcp-project", "private_key_id":"...", "private_key":"-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n", "client_email":"...", "client_id":"...", "auth_uri":"https://accounts.google.com/o/oauth2/auth", "token_uri":"https://oauth2.googleapis.com/token", "auth_provider_x509_cert_url":"https://www.googleapis.com/oauth2/v1/certs", "client_x509_cert_url":"..."}'
```

7. กด **Deploy**

> ควรใช้ service account สำหรับ dashboard ที่มีสิทธิ์อ่าน Firebase เท่าที่จำเป็น และไม่ควร commit JSON key ลง repository

---

## 10) Flow หลัง deploy สำเร็จ

1. Cloud Scheduler ยิงตามเวลา
2. Cloud Function รัน `lego_one_row`
3. Function อ่าน snapshot / คำนวณ row / commit ลง Firebase RTDB
4. Streamlit dashboard อ่าน path ต่อไปนี้จาก Firebase:
   - `webull_lego_rows`
   - `webull_lego_state`
   - `webull_lego_order_audit`
5. ถ้ามีข้อมูล committed แล้ว dashboard จะแสดง metric, chart และตาราง

---

## 11) Checklist ตรวจหลัง deploy

```bash
# 1) Scheduler ยิง function ได้
gcloud scheduler jobs run lego-tick --location="$REGION"

# 2) Function มี log ล่าสุด
gcloud functions logs read lego-one-row --gen2 --region="$REGION" --limit=50

# 3) Function URL ถูกต้อง
gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.uri)'
```

ตรวจใน Firebase Console:

- มีข้อมูลใหม่ใน `webull_lego_rows`
- `webull_lego_state` มี version ล่าสุด
- ไม่มี error ผิดปกติใน `webull_lego_errors`

ตรวจใน Streamlit:

- App เปิดได้
- ไม่ฟ้อง missing `FIREBASE_DB_URL` หรือ `FIREBASE_SA_JSON`
- Dashboard แสดงข้อมูล committed ล่าสุด

---

## Troubleshooting แบบเร็ว

### Streamlit ขึ้น error เรื่อง Firebase secret

ตรวจว่าใส่ secrets ใน streamlit.app ครบ:

- `FIREBASE_DB_URL`
- `FIREBASE_SA_JSON`

โดย `FIREBASE_SA_JSON` ต้องเป็น JSON string บรรทัดเดียว และ `private_key` ต้องใช้ `\\n`

### Cloud Scheduler ได้ 401 / 403

ตรวจว่า scheduler ใช้ OIDC service account และ audience ตรงกับ function URL:

```bash
gcloud scheduler jobs describe lego-tick --location="$REGION"
```

### Cloud Build deploy ไม่ผ่านเพราะ permission

ให้สิทธิ์ Cloud Build service account ตามขั้นตอน 8.3 แล้วกด run trigger ใหม่

### Function deploy ไม่เจอ dependency

ตรวจว่ามี `requirements.txt` ที่ root source ที่ deploy หรือ copy จาก `functions_requirements.txt` อีกครั้ง:

```bash
cp functions_requirements.txt requirements.txt
```
