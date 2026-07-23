# 🚀 Quick Start Guide — LEGO Firebase Dashboard

สวัสดีมือใหม่! 👋 คู่มือนี้จะพาคุณ deploy ระบบทีละต่อจนจบ **แบบไม่ต้องเก่งมาก่อน** ทำตามทีละขั้นได้เลย

คิดง่าย ๆ ว่าเราต่อ "เลโก้" 4 ก้อนให้ทำงานร่วมกัน:

```text
⏰ Google Cloud Scheduler   (นาฬิกาปลุก — คอยเรียกตามเวลา)
        ↓ เรียกตามเวลา
⚙️  Google Cloud Functions   (สมอง — รัน code คำนวณ)
        ↓ เขียนผลลัพธ์
🔥 Firebase Realtime Database (ตู้เก็บของ — เก็บข้อมูล)
        ↓ อ่านข้อมูล
📊 Streamlit (streamlit.app)  (หน้าจอ — โชว์ dashboard สวย ๆ)
```

> 🎯 **เป้าหมาย:** ทำทุกอย่างจาก **Cloud Shell ตั้งแต่ต้นจนจบ** โดยใช้ Firebase เป็นตู้เก็บข้อมูลกลาง แล้วให้ Cloud Scheduler คอยกดปุ่มให้อัตโนมัติ

> 🗺️ **แผนที่การเดินทาง:** ข้อ 0 เตรียมของ → ข้อ 1–5 ตั้งค่า → ข้อ 6–7 ทำให้มันวิ่งเอง → ข้อ 8 ทำหน้าจอ → ข้อ 9–10 เช็คให้ชัวร์ → ข้อ 11 วิธีแก้ code ทีหลัง

---

## 0) 🧳 สิ่งที่ต้องมีก่อนเริ่ม

เช็คลิสต์ของที่ต้องเตรียม (มีครบแล้วค่อยไปต่อ):

1. ✅ Google Cloud project ที่เปิด Billing แล้ว
2. ✅ Firebase Realtime Database ใน project เดียวกัน
3. ✅ GitHub repository ที่เก็บ code นี้
4. ✅ Account สำหรับ Streamlit Community Cloud ที่ connect GitHub ได้
5. ✅ ค่า credential ของ Webull สำหรับเก็บใน Secret Manager

เปิด **Cloud Shell** (ปุ่ม `>_` มุมขวาบนของ Google Cloud Console) แล้วกำหนดตัวแปรไว้ใช้ซ้ำ ๆ:

```bash
export PROJECT="lego-firebase"
export REGION="asia-southeast1"
export DB_URL="https://lego-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
export REPO_URL="https://github.com/firstnattapon/lego-firebase.git"
```

ตั้งค่า project ให้ Cloud Shell รู้ว่าเราจะทำงานกับ project ไหน:

```bash
gcloud config set project "$PROJECT"
```

> 💡 **เคล็ดลับ:** ถ้าปิด Cloud Shell แล้วเปิดใหม่ ตัวแปร `export` พวกนี้จะหายไป ให้รันบล็อกด้านบนซ้ำอีกครั้งก่อนทำงานต่อ

---

## 1) 🔌 เปิด API ที่ต้องใช้ใน Google Cloud

เหมือนเปิดสวิตช์ไฟให้บริการต่าง ๆ พร้อมใช้ — รันครั้งเดียวจบ:

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

> ⏳ อาจใช้เวลาสักครู่ ถ้าขึ้นว่า enabled เรียบร้อยก็ไปต่อได้เลย

---

## 2) 📥 ดึง code จาก GitHub เข้ามาใน Cloud Shell

### กรณีที่ 1: เพิ่งเริ่ม — ยังไม่เคย clone (ทำครั้งแรกครั้งเดียว)

```bash
git clone https://github.com/firstnattapon/lego-firebase.git
cd lego-firebase
```

> 💡 พิมพ์ URL เต็ม ๆ ไปเลยจะชัวร์กว่า ถ้าใช้ `git clone "$REPO_URL"` แต่ยังไม่ได้ตั้งตัวแปร `REPO_URL` จะเจอ error ว่า repository ว่างหรือไม่มีอยู่

### กรณีที่ 2: เคย clone ไปแล้ว — แค่อยากอัปเดต code จาก GitHub เฉย ๆ 🔄

**ไม่ต้อง clone ใหม่!** แค่เข้าไปใน folder เดิมแล้วดึงของใหม่ล่าสุดมา:

```bash
cd lego-firebase
git pull origin main
```

> ⚠️ ถ้า `git pull` ฟ้องว่ามีไฟล์ค้างแก้อยู่ (local changes) ให้เช็คด้วย `git status` ก่อน ถ้าเป็นของที่ไม่ได้ตั้งใจแก้ ค่อยเก็บ (`git stash`) หรือทิ้ง แล้วค่อย pull ใหม่

### เช็คว่าอยู่ branch ไหน (ทั้งสองกรณี)

```bash
git branch --show-current
git status
```

> โดยปกติเรา deploy จาก branch `main`

---

## 3) 📦 เตรียมไฟล์ requirements สำหรับ Cloud Functions

ข่าวดี: **ไม่ต้องทำอะไรเลย!** 🎉 Cloud Functions for Python จะมองหาไฟล์ชื่อ `requirements.txt` ที่ root ของ source — repo นี้มีให้อยู่แล้ว deploy จาก root ได้เลย

> ถ้าในอนาคตแยก folder `functions/` ให้ย้าย `main.py`, module ที่เกี่ยวข้อง และ `requirements.txt` เข้า folder นั้น แล้วปรับ `--source` ให้ตรง

---

## 4) 🔐 เก็บ secret ใน Secret Manager

**กฎเหล็ก:** ห้าม hardcode key ลงใน code หรือ commit ลง GitHub เด็ดขาด! ให้เก็บไว้ในตู้เซฟ (Secret Manager) แทน:

```bash
printf "%s" "<WEBULL_APP_KEY>" | gcloud secrets create webull-app-key --data-file=-
printf "%s" "<WEBULL_APP_SECRET>" | gcloud secrets create webull-app-secret --data-file=-
printf "%s" "<WEBULL_ACCOUNT_ID>" | gcloud secrets create webull-account-id --data-file=-
```

ถ้า secret มีอยู่แล้วและต้องการเปลี่ยนค่าใหม่ (update):

```bash
printf "%s" "<NEW_VALUE>" | gcloud secrets versions add webull-app-key --data-file=-
printf "%s" "<NEW_VALUE>" | gcloud secrets versions add webull-app-secret --data-file=-
printf "%s" "<NEW_VALUE>" | gcloud secrets versions add webull-account-id --data-file=-
```

> 💡 แทนที่ `<...>` ด้วยค่าจริงของคุณ (ไม่ต้องเก็บเครื่องหมาย `<` `>` ไว้)

---

## 5) 🔥 ตั้งค่า Firebase Realtime Database Rules

ใน Firebase Console ไปที่ **Realtime Database → Rules** แล้ววาง rules แบบ read-only สำหรับ dashboard:

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

> 🛡️ ไม่ต้องห่วงว่า Function จะเขียนข้อมูลไม่ได้ — Cloud Function ใช้ Firebase Admin SDK เขียนผ่าน service account ได้อยู่แล้ว rules นี้แค่กันไม่ให้คนนอกมาแก้ข้อมูลของเรา

---

## 6) ⚙️ Deploy Google Cloud Functions — สมองของระบบ

Deploy function แบบ Gen2 HTTP จาก root repository (คำสั่งยาวหน่อยแต่ก๊อปวางได้เลย):

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

🐣 **มือใหม่เริ่มแบบปลอดภัยไว้ก่อน** ด้วยค่า 2 ตัวนี้:

- `WEBULL_ENV=UAT` — ใช้สนามซ้อม ไม่ยิงเงินจริง
- `AUTO_SUBMIT=false` — ยังไม่ส่ง order จริงอัตโนมัติ

เมื่อระบบนิ่งและมั่นใจแล้ว ค่อยพิจารณาเปิด production / auto submit ทีหลัง

---

## 7) ⏰ Deploy Google Cloud Scheduler — นาฬิกาปลุกของระบบ

ก่อนอื่นดึง URL และ service account ของ Cloud Function มาเก็บไว้:

```bash
export FUNCTION_URL="$(gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"
export FUNCTION_SA="$(gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.serviceAccountEmail)')"

echo "$FUNCTION_URL"
echo "$FUNCTION_SA"
```

สร้าง scheduler ให้เรียก function **ทุก 10 นาที จันทร์–ศุกร์** ในช่วงเวลา UTC ที่ครอบคลุมตลาดสหรัฐฯ:

```bash
gcloud scheduler jobs create http lego-tick \
  --location="$REGION" \
  --schedule="*/10 13-20 * * 1-5" \
  --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --oidc-service-account-email="$FUNCTION_SA" \
  --oidc-token-audience="$FUNCTION_URL"
```

> 📖 **อ่าน schedule ยังไง?** `*/10 13-20 * * 1-5` = ทุก ๆ 10 นาที ในชั่วโมง 13–20 UTC วันจันทร์ถึงศุกร์ (ครอบคลุมเวลาเปิด–ปิดตลาดหุ้นสหรัฐฯ)

ลองทดสอบยิง scheduler ด้วยมือ (ไม่ต้องรอถึงเวลา):

```bash
gcloud scheduler jobs run lego-tick --location="$REGION"
```

แล้วดู log ของ function ว่าทำงานไหม:

```bash
gcloud functions logs read lego-one-row --gen2 --region="$REGION" --limit=50
```

---

## 8) 📊 Deploy Streamlit Dashboard — หน้าจอสวย ๆ

1. Push repository ขึ้น GitHub (ถ้ายังไม่ได้ push)
2. เข้า <https://streamlit.io/cloud>
3. กด **New app**
4. เลือก repository และ branch
5. Main file path: `streamlit_app.py`
6. กด **Advanced settings → Secrets** แล้ววาง:

```toml
FIREBASE_DB_URL = "https://lego-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
FIREBASE_SA_JSON = '{"type":"service_account", "project_id":"lego-firebase", "private_key_id":"...", "private_key":"-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n", "client_email":"...", "client_id":"...", "auth_uri":"https://accounts.google.com/o/oauth2/auth", "token_uri":"https://oauth2.googleapis.com/token", "auth_provider_x509_cert_url":"https://www.googleapis.com/oauth2/v1/certs", "client_x509_cert_url":"..."}'
```

7. กด **Deploy** แล้วรอสักครู่ 🎉

> 🛡️ ควรใช้ service account สำหรับ dashboard ที่มีสิทธิ์อ่าน Firebase เท่าที่จำเป็น และห้าม commit JSON key ลง repository เด็ดขาด

---

## 9) 🔄 Flow หลัง deploy สำเร็จ

พอทุกอย่างต่อกันครบ ระบบจะวิ่งเองแบบนี้:

1. ⏰ Cloud Scheduler ยิงตามเวลา (ทุก 10 นาที)
2. ⚙️ Cloud Function รัน `lego_one_row`
3. 🔥 Function อ่าน snapshot / คำนวณ row / commit ลง Firebase RTDB
4. 📊 Streamlit dashboard อ่าน path ต่อไปนี้จาก Firebase:
   - `webull_lego_rows`
   - `webull_lego_state`
   - `webull_lego_order_audit`
5. ✨ พอมีข้อมูล committed แล้ว dashboard จะโชว์ metric, chart และตารางให้เห็น

---

## 10) ✅ Checklist ตรวจหลัง deploy

รันชุดคำสั่งนี้เพื่อเช็คว่าทุกอย่างโอเค:

```bash
# 1) Scheduler ยิง function ได้
gcloud scheduler jobs run lego-tick --location="$REGION"

# 2) Function มี log ล่าสุด
gcloud functions logs read lego-one-row --gen2 --region="$REGION" --limit=50

# 3) Function URL ถูกต้อง
gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.uri)'
```

ตรวจใน **Firebase Console:**

- ✅ มีข้อมูลใหม่ใน `webull_lego_rows`
- ✅ `webull_lego_state` มี version ล่าสุด
- ✅ ไม่มี error ผิดปกติใน `webull_lego_errors`

ตรวจใน **Streamlit:**

- ✅ App เปิดได้
- ✅ ไม่ฟ้อง missing `FIREBASE_DB_URL` หรือ `FIREBASE_SA_JSON`
- ✅ Dashboard แสดงข้อมูล committed ล่าสุด

---

## 🆘 Troubleshooting แบบเร็ว

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

### Function deploy ไม่เจอ dependency

ตรวจว่ามี `requirements.txt` ที่ root ของ source ที่ deploy (repo นี้มีที่ root อยู่แล้ว) และ `--source` ชี้ตำแหน่งถูก

### Function deploy ช้าหรือ error ระหว่าง build

Cloud Functions Gen2 จะ build ผ่าน Cloud Build ให้อัตโนมัติ ถ้า build ล้มเหลว ลองดู log:

```bash
gcloud functions logs read lego-one-row --gen2 --region="$REGION" --limit=50
gcloud builds list --limit=5
```

---

## 11) 📚 คู่มือการเรียนรู้: ถ้าจะแก้ / อัปเดต code ต้องทำไง

ส่วนนี้เป็น workflow สำหรับมือใหม่ที่อยากแก้ code, ทดสอบ, push ขึ้น GitHub แล้ว deploy ใหม่อย่างปลอดภัย ทำตามทีละขั้นได้เลย 🙂

### 11.1 เข้าใจ flow ก่อนแก้ code

```text
✏️  แก้ code ในเครื่องหรือ Cloud Shell
        ↓
🧪 ทดสอบว่า function / dashboard ยังรันได้
        ↓
💾 commit ด้วย Git
        ↓
⬆️  push ไป GitHub
        ↓
   ┌──────────────────────────────┬──────────────────────────────┐
⚙️  Cloud Function                  📊 Streamlit
   deploy ด้วยมืออีกครั้ง            ดึง code ล่าสุดจาก GitHub
   (gcloud functions deploy)        แล้ว redeploy ให้เอง
```

> 💡 **จำง่าย ๆ:** push ขึ้น GitHub อย่างเดียว **ยังไม่พอ** สำหรับ Cloud Function — ต้องสั่ง `gcloud functions deploy` เองอีกทีเพื่อเอา code ใหม่ขึ้นไปวิ่ง ส่วน Streamlit จะดึงของใหม่ให้เองอัตโนมัติ

### 11.2 ก่อนเริ่มแก้ code ทุกครั้ง

เช็คก่อนว่าอยู่ branch ไหน และมีไฟล์ค้างอยู่หรือไม่:

```bash
git branch --show-current
git status
```

ถ้าทำงานบน Cloud Shell และอยากดึง code ล่าสุดจาก GitHub ก่อนแก้:

```bash
git pull origin main
```

> ถ้า project ใช้ branch อื่นแทน `main` ให้เปลี่ยนชื่อ branch ให้ตรงกับของจริง เช่น `dev` หรือ `production`

### 11.3 ควรแก้ไฟล์ไหน

| อยากแก้อะไร | ไฟล์ที่มักเกี่ยวข้อง |
| --- | --- |
| Logic หลักของ Cloud Function | `main.py`, `lego_one_row.py`, `lego_orders.py`, `lego_state.py`, `dna_engine.py` |
| การเชื่อมต่อ Webull / external API | `webull_io.py` |
| Dependency Python | `requirements.txt` |
| เอกสารวิธีใช้งาน | `README.md`, `QUICKSTART_TH.md` |
| ค่า config ตอน deploy | คำสั่ง `gcloud functions deploy` (ข้อ 6) |

> 🔐 ห้ามใส่ secret, API key, private key, service account JSON หรือรหัสผ่านลงใน code ให้ใช้ Secret Manager หรือ Streamlit Secrets เท่านั้น

### 11.4 วิธีแก้ code แบบปลอดภัย

1. แก้ทีละเรื่องเล็ก ๆ เช่น แก้ bug หนึ่งจุด หรือเพิ่ม config หนึ่งตัว
2. อย่าเปลี่ยนหลายส่วนพร้อมกันถ้าไม่จำเป็น เพราะจะ debug ยาก
3. ถ้าแก้ logic ที่เกี่ยวกับ order ให้เริ่มที่ `WEBULL_ENV=UAT` และ `AUTO_SUBMIT=false` ก่อนเสมอ
4. ถ้าแก้ scheduler หรือ retry logic ให้ตรวจว่าไม่ทำให้เกิดการสร้าง row ซ้ำใน slot เดียว
5. ถ้าแก้ schema ของ Firebase RTDB ให้ตรวจว่า dashboard ยังอ่าน path เดิมได้ หรือ update dashboard ให้ตรงกัน

### 11.5 ทดสอบในเครื่องก่อน commit

ติดตั้ง dependency ถ้ายังไม่เคยติดตั้ง:

```bash
python -m pip install -r requirements.txt
```

ตรวจ syntax ของ Python ทุกไฟล์:

```bash
python -m compileall .
```

ถ้ามี test ในอนาคต ให้รัน:

```bash
python -m pytest
```

### 11.6 ตรวจ diff ก่อน commit

ดูไฟล์ที่เปลี่ยน:

```bash
git status
```

ดูรายละเอียดที่แก้:

```bash
git diff
```

> ⚠️ ถ้าเห็น secret หรือข้อมูลส่วนตัวใน diff **ให้หยุดทันที** และลบออกก่อน commit

### 11.7 Commit code

เพิ่มไฟล์ที่ต้องการ commit:

```bash
git add README.md QUICKSTART_TH.md main.py lego_one_row.py
```

หรือถ้ามั่นใจว่าทุกไฟล์ที่เปลี่ยนควรถูก commit:

```bash
git add .
```

commit พร้อมข้อความสั้น ๆ ที่บอกว่าแก้อะไร:

```bash
git commit -m "docs: add code update workflow"
```

ตัวอย่าง prefix ที่แนะนำ:

- `feat:` เพิ่ม feature
- `fix:` แก้ bug
- `docs:` แก้เอกสาร
- `refactor:` ปรับโครงสร้าง code โดย behavior ไม่เปลี่ยน
- `chore:` งานดูแลทั่วไป เช่น dependency / config

### 11.8 Push ขึ้น GitHub แล้ว deploy ใหม่

push code ขึ้น GitHub ก่อน:

```bash
git push origin main
```

จากนั้น **deploy Cloud Function ใหม่ด้วยมือ** เพื่อเอา code ล่าสุดขึ้นไปวิ่ง (ใช้คำสั่งเดียวกับข้อ 6):

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

> 📊 ส่วน Streamlit ไม่ต้องทำอะไรเพิ่ม — มันจะเห็นว่า GitHub มี code ใหม่แล้ว redeploy ให้เองอัตโนมัติ

### 11.9 ตรวจหลัง deploy

ตรวจ Cloud Function:

```bash
gcloud functions describe lego-one-row --gen2 --region="$REGION"
gcloud functions logs read lego-one-row --gen2 --region="$REGION" --limit=50
```

ทดสอบยิง scheduler:

```bash
gcloud scheduler jobs run lego-tick --location="$REGION"
```

ตรวจใน **Firebase Console** ว่า:

- `webull_lego_rows` มี row ใหม่ตามที่คาด
- `webull_lego_state` มี version เดินหน้าถูกต้อง
- `webull_lego_errors` ไม่มี error ใหม่ผิดปกติ

ตรวจใน **Streamlit** ว่า:

- dashboard เปิดได้
- chart / table ยังแสดงข้อมูล
- ไม่มี error เรื่อง Firebase secrets หรือ schema ไม่ตรง

### 11.10 ถ้า update แล้วพัง ต้อง rollback ยังไง

ดูประวัติ commit:

```bash
git log --oneline -5
```

ถ้าต้องการย้อน commit ล่าสุดด้วย commit ใหม่ที่ปลอดภัยต่อทีม:

```bash
git revert HEAD
git push origin main
```

จากนั้น deploy Cloud Function ใหม่อีกครั้ง (คำสั่งเดียวกับข้อ 6 / 11.8) เพื่อให้ code ที่ย้อนแล้วขึ้นไปวิ่งจริง

### 11.11 Checklist สั้น ๆ ก่อน push ทุกครั้ง

- [ ] `git status` ไม่มีไฟล์แปลก ๆ ที่ไม่ตั้งใจ commit
- [ ] `git diff` ไม่มี secret หรือ private key
- [ ] `python -m compileall .` ผ่าน
- [ ] ถ้าแก้ order logic ต้องทดสอบด้วย `WEBULL_ENV=UAT` และ `AUTO_SUBMIT=false`
- [ ] commit message อ่านแล้วรู้ว่าเปลี่ยนอะไร
- [ ] push แล้ว **อย่าลืม** `gcloud functions deploy` ใหม่ให้ Cloud Function
- [ ] หลัง deploy ตรวจ log ของ Cloud Function แล้วไม่มี error ใหม่

---

🎉 **จบแล้ว!** ถ้าทำครบทุกข้อ ระบบจะวิ่งเอง เก็บข้อมูลเอง และโชว์ dashboard ให้เอง ขอให้สนุกกับการต่อเลโก้! 🧱
