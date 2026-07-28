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

rules ตัวจริงถูก track ที่ `database.rules.json` และชี้จาก `firebase.json`
ให้ deploy ไฟล์นี้แทนการ copy หลายชุดจากเอกสาร:

```bash
firebase deploy --only database --project="$PROJECT"
```

public read มีเฉพาะ rows, state, order audit, audit archive และ warnings;
outbox, outbox archive, realized, errors, root และ path อื่น deny read.
client write ถูกปิดทั้งหมด ส่วน Cloud Function ใช้ Admin SDK จึงเขียนผ่าน service account ได้

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
  --set-env-vars="FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=AAPL,LEGO_FIX_C=3000,LEGO_DIFF=5,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=3,LEGO_SLOT_SECONDS=900,LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z,LEGO_DNA_CLOCK_MODE=market,AUTO_SUBMIT=true" \
  --set-secrets="WEBULL_APP_KEY=webull-app-key:latest,WEBULL_APP_SECRET=webull-app-secret:latest,WEBULL_ACCOUNT_ID=webull-account-id:latest"
```

🐣 **ตัวกันพลาดตัวจริงคือ `WEBULL_ENV=UAT`** — สนามซ้อม ไม่ยิงเงินจริง
ตราบใดที่ยังเป็น `UAT` การเปิด `AUTO_SUBMIT=true` มาตั้งแต่ deploy แรกจึงปลอดภัย
และเป็นวิธีเดียวที่จะเห็นวงจร order ครบจริง (`preflight → outbox → worker → fill →
finalize ΔAₙ/Aₙ/Eₙ`) ก่อนแตะเงินจริง

- `WEBULL_ENV=UAT` — สนามซ้อม · `Production` = read-only ส่ง order ไม่ได้เลย
- `AUTO_SUBMIT=true` — **ขอ**สร้าง order intent เมื่อแถวเป็น `READY_*` เท่านั้น
  ยังต้องผ่าน `auto_submit_preflight` ครบ 8 ข้อ (หัวข้อ 7.6) ไม่ผ่าน = แถว commit ปกติ
  แต่ไม่มี intent เกิดขึ้น และ response บอกเหตุผลทุกครั้ง

ถ้ายังไม่อยากให้มี order intent เลยแม้แต่ใบเดียวระหว่างทดสอบ ตั้ง `AUTO_SUBMIT=false`
แทนได้ (ค่า default ของโค้ดคือ `false` อยู่แล้ว) แล้วค่อยเปิดทีหลัง
ส่วนการย้ายไป production เป็นคนละเรื่อง — เปลี่ยน `WEBULL_ENV` เมื่อมั่นใจแล้วเท่านั้น

### ตาราง env ทั้งหมดที่โค้ดอ่านจริง

**บังคับ** (ไม่มี = ระบบไม่ทำงาน):

| env | ตัวอย่าง | ความหมาย |
|---|---|---|
| `FIREBASE_DB_URL` | `https://...firebasedatabase.app` | RTDB ที่จะเขียนแถว |
| `LEGO_SYMBOL` | `AAPL` | สินทรัพย์ที่เทรด |
| `LEGO_FIX_C` | `3000` | มูลค่าพอร์ตเป้าหมาย (ต้อง > 0) |
| `LEGO_SLOT_SECONDS` | `900` | ขนาด slot ต้องตรง timeframe ที่เทรน DNA — รับเฉพาะ `900` (15m), `1800` (30m), `3600` (1h), `14400` (4h), `86400` (1d) · ค่าอื่น = `CONFIG_ERROR` 500 |
| `WEBULL_APP_KEY` / `WEBULL_APP_SECRET` / `WEBULL_ACCOUNT_ID` | (secret) | credential ของ Webull OpenAPI |

**ค่าเริ่มต้นมีให้แล้ว** (ตั้งเมื่ออยากเปลี่ยน):

> คอลัมน์ `default` คือค่าที่ **โค้ด** ใช้เมื่อไม่ตั้ง env ไม่ใช่ค่าในคำสั่ง deploy ข้างบน
> — ตัวอย่างในคู่มือนี้ตั้งทับ 3 ตัว: `LEGO_DIFF=5`, `LEGO_DECIMAL_PRECISION=3`,
> `AUTO_SUBMIT=true`

| env | default | ความหมาย |
|---|---|---|
| `LEGO_DIFF` | `0` | ครึ่งความกว้างแถบ no-trade · `|gap| ≤ DIFF` → `PASS_THRESHOLD` · ตัวอย่างใช้ `5` = ห่างเป้าไม่ถึง $5 ให้ `PASS_THRESHOLD` |
| `LEGO_DNA_CODE` | `bypass:100` | โค้ด DNA (`bypass:N` / `[1, N]` / stream ตัวเลขล้วน) · ⚠️ ความยาว DNA คือ**จำนวน slot ที่ chain นี้มีชีวิตอยู่ได้** — ที่ `LEGO_SLOT_SECONDS=900` มี 26 slot ต่อวันทำการ (early close 14) ดังนั้น `bypass:100` หมดใน **~4 วันทำการ** แล้วตอบ `DNA_EXHAUSTED` · ตั้ง `LEGO_DNA_LOW_WATERMARK` ให้เห็นล่วงหน้าและเตรียม DNA ที่ยาวพอ |
| `LEGO_DECIMAL_PRECISION` | `5` | ทศนิยมของจำนวนสั่ง (0–5) · `0` = สั่งเป็นจำนวนเต็มหุ้น · ตัวอย่างใช้ `3` · ต้องเท่ากันทั้ง 2 ฟังก์ชัน (อยู่ใน `config_hash`) |
| `LEGO_STRATEGY_ID` | `shannon_demon_lego` | ป้ายกำกับกลยุทธ์ (อยู่ใน `config_hash`) |
| `WEBULL_ENV` | `UAT` | รับเฉพาะ `UAT`, `PROD`, `PRODUCTION` (case-insensitive) · ค่าอื่น = `CONFIG_ERROR` และไม่ไหลไป Production |
| `AUTO_SUBMIT` | `false` | `true` = **ขอ**สร้าง order intent อัตโนมัติเมื่อแถวเป็น `READY_*` · ตัวอย่างใช้ `true` (ปลอดภัยเพราะอยู่บน `WEBULL_ENV=UAT`) · ไม่ใช่สวิตช์เดียว — ต้องผ่าน preflight ครบ 8 ข้อก่อน (ดูหัวข้อ 7.6) |
| `LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING` | `1` | preflight บล็อกการสร้าง intent ใหม่เมื่อ DNA เหลือน้อยกว่านี้ |
| `WEBULL_TOKEN_DIR` | `/tmp/webull_token` | ที่เก็บ token ของ SDK · ⚠️ `/tmp` หายทุกครั้งที่ instance ถูกรีไซเคิล → SDK จะสร้าง token ใหม่และรอคนกด 2FA ในแอป 300 วิ ถ้าไม่มีคนกด = `ERROR_INIT_TOKEN` · ชี้ไป volume ที่คงอยู่ (เช่น GCS FUSE mount) จะเห็นคำเตือนที่ `webull_lego_warnings/webull_token` จนกว่าจะย้าย |
| `LEGO_ALLOW_EPHEMERAL_TOKEN_DIR` | `false` | `true` = ยอมรับว่า token dir อยู่บน `/tmp` แล้วให้ preflight `token_ready` ผ่านได้ · จำเป็นเมื่อยัง mount volume ไม่ได้ เพราะ Cloud Functions เขียนได้แค่ `/tmp` → ไม่ตั้งก็ **ไม่มี order intent เกิดขึ้นเลย** · ให้อภัยเฉพาะข้อ "dir ไม่คงอยู่" ข้อเดียว (ไม่พบ token / status ≠ `NORMAL` / ใกล้หมดอายุ ยังบล็อกเหมือนเดิม) และคำเตือน `token_warning` ยังขึ้นทุก slot |
| `LEGO_TOKEN_REFRESH_MARGIN_DAYS` | `3` | token อายุ 15 วันและ SDK **ไม่ต่ออายุให้** — เหลือน้อยกว่านี้จะเรียก `token/refresh` เองตอนสร้าง client · refresh ล้มเหลวไม่หยุด slot (token เดิมยังใช้ได้) แค่แจ้งเตือน |
| `LEGO_MARKET_CATEGORY` | `US_STOCK` | Category ที่ใช้ขอ snapshot · ตั้ง `US_ETF` เมื่อ `LEGO_SYMBOL` เป็น ETF · ค่านอก enum ของ SDK = fail closed |
| `LEGO_CLIENT_CACHE_TTL_SECONDS` | `3600` | instance ที่ยังอุ่นใช้ client คู่เดิม (สร้างใหม่ 1 ครั้ง = 4 auth request และ token create จำกัด 10/30s) · ครบเวลาแล้วสร้างใหม่เพื่อตรวจ token อีกรอบ |
| `LEGO_WEBULL_LOG_LEVEL` | `INFO` | ระดับ log ของ SDK ที่ส่งลง stdout · `DEBUG` จะพิมพ์ response body ทุกครั้ง (มีข้อมูลบัญชี) |
| `LEGO_ALLOW_ZERO_HOLDINGS` | `false` | `true` = ยอมรับว่า "ถือ 0 จริง" ทั้งที่ chain เคยเห็นของ · ใช้เฉพาะตอนขายทิ้งเอง/ย้าย position นอกระบบ **แล้วเอาออกทันที** (ดู `HOLDINGS_ANOMALY`) |

**นาฬิกา DNA** (ทุกตัวมีผลต่อ phase ของ gate array — ดูหัวข้อ 7.5):

| env | default | ความหมาย |
|---|---|---|
| `LEGO_DNA_CLOCK_MODE` | `shadow` | `shadow` = เดิน step ตาม anchor+1 แล้วรายงานส่วนต่างเฉย ๆ · `market` = ใช้ market ordinal เป็นตัวจริง · `legacy` = ของเดิมไว้ rollback |
| `LEGO_DNA_ORIGIN_UTC` | — | เวลาเริ่มนับ ordinal · หาได้จาก `find_origin.py` · ไม่ตั้ง = โหมด degraded (mode `market` จะ error) · ⚠️ **degraded + `AUTO_SUBMIT=true` = แถว commit ปกติ แต่ไม่มี order intent ถูกสร้างเลย** — response จะมี `outbox_skipped` และนับสะสมที่ `webull_lego_warnings/degraded_clock_no_order` |
| `LEGO_DNA_LOW_WATERMARK` | `10` | DNA เหลือน้อยกว่าค่านี้ → response แนบ `dna_steps_remaining` มาเตือนก่อนจะเจอ `DNA_EXHAUSTED` |
| `LEGO_MARKET_HOLIDAYS` | — | CSV วันที่ ISO เพิ่มเข้าปฏิทินวันหยุด เช่น `2026-01-02` |
| `LEGO_MARKET_EARLY_CLOSES` | — | CSV วันที่ ISO ที่ปิด 13:00 ET |

**order worker**:

| env | default | ความหมาย |
|---|---|---|
| `LEGO_INLINE_ORDER_WORKER` | `false` | `true` = ส่ง order ต่อท้ายการ commit เลย (เพิ่ม latency ให้ฟังก์ชัน DNA — ไม่แนะนำ) |
| `LEGO_ORDER_WORKER_LIMIT` | `3` | จำนวน intent สูงสุดต่อการเรียก 1 ครั้ง |
| `LEGO_ORDER_CLAIM_LEASE_SECONDS` | `120` | lease ของ transactional worker claim; generation fence ก่อน `place_order` กัน worker เก่าหลัง lease หมด |
| `LEGO_ORDER_EXPIRY_MARGIN_SECONDS` | `15` | กันส่ง order คาบเกี่ยว slot ถัดไป |
| `LEGO_HOLDINGS_DRIFT_TOLERANCE` | `0.000001` | holdings เปลี่ยนเกินนี้ระหว่างรอส่ง = `SUPPRESSED_STATE_CHANGED` |
| `LEGO_RECONCILE_MAX_ATTEMPTS` | `20` | ถาม broker ซ้ำได้กี่ครั้งก่อนยอมแพ้เป็น `RECONCILE_ABANDONED` (ที่ `*/5` = ~100 นาที) — กัน order ที่ broker ไม่เคยรับ วนถามไม่รู้จบจนเบียด intent ใหม่ทั้งหมด |
| `LEGO_FILL_CONFIRM_MAX_ATTEMPTS` | `5` | broker บอก fill แล้วแต่ position ยังไม่ขยับ = `AWAITING_FILL_CONFIRMATION` แล้วถามใหม่ได้กี่ครั้งก่อนปล่อยออกจากคิวพร้อม `needs_manual_check` · ระหว่างนี้ `ΔAₙ`/`Aₙ`/`Eₙ` ยังไม่ถูกบันทึก (ห้าม book cashflow จากคำพูด broker อย่างเดียว) |
| `LEGO_OPEN_ORDER_PAGE_SIZE` | `50` | `get_order_open` ตอบเป็น "หน้า" (default ของ broker = 10) · การกันส่งซ้ำอ่านจากรายการนี้ ถ้าหน้าเดียวไม่ครบจะมองไม่เห็น order ของเราเอง |
| `LEGO_OPEN_ORDER_MAX_PAGES` | `5` | เพดานจำนวนหน้าที่ไล่ต่อการตรวจ 1 ครั้ง · ชนเพดาน/cursor ไม่ครบ = fail closed คง intent รอและไม่ส่ง order |

> 🔐 state บันทึกเฉพาะ fingerprint ของ account/environment ไม่บันทึก account ID.
> chain เดิมที่ยังไม่มี fingerprint จะถูก **adopt อัตโนมัติ** ใน commit แรกหลัง deploy
> (DNA ไม่หยุด ไม่ต้องตั้ง env) แล้วนับที่ `webull_lego_warnings/runtime_identity_adopted`
> — operator เปิดดู warning นั้นหนึ่งครั้งเพื่อยืนยันว่า account/`WEBULL_ENV` ถูกชุด.
> ส่วน fingerprint ที่มีแล้วแต่ไม่ตรง = `CONFIG_ERROR` และไม่มีสวิตช์ให้ข้าม

**archive worker** (`lego_archive_worker` — งานบ้าน ยิงวันละครั้งพอ):

| env | default | ความหมาย |
|---|---|---|
| `LEGO_ARCHIVE_RETENTION_DAYS` | `30` | intent/audit ที่ terminal แล้วและเก่ากว่านี้ ถูกย้ายไป `*_archive` (ย้าย ไม่ลบ) |
| `LEGO_ARCHIVE_LIMIT` | `500` | ย้ายได้สูงสุดกี่ record ต่อการเรียก 1 ครั้ง |

> ไม่ย้าย 2 อย่างเสมอ: record ที่ `needs_manual_check` (ยังรอคนตอบ) และ record ที่ไม่มี timestamp
> (บอกอายุไม่ได้ = ยังไม่เก่าพอ)

---

## 6.0) ⏱️ ตั้ง `LEGO_DNA_ORIGIN_UTC` ตั้งแต่ deploy แรก

คำสั่ง deploy ข้างบนใส่ `LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z` +
`LEGO_DNA_CLOCK_MODE=market` มาให้แล้ว (13:30Z = เวลาเปิดตลาด 09:30 ET ของวันจันทร์ที่
2026-07-27 → `slot_id 2026-07-27:0`, `market_ordinal 0` บนกริด 15 นาที) —
**ค่า origin ในตัวอย่างเป็นแค่ตัวอย่าง ต้องหาของตัวเองก่อน** ด้วย
`python find_origin.py <dna_step ถัดไป>` (อธิบายเต็มที่ข้อ 7.5)

ถ้ายังไม่พร้อมเปิด mode `market` ให้เอา 2 ตัวนี้ออกได้ แต่ต้องรู้ว่าเกิดอะไรขึ้น:
ไม่มี origin = market clock resolve ไม่ได้ = โหมด **degraded** ซึ่งยัง commit แถวได้ปกติ
แต่ **สร้าง order intent ไม่ได้เลยสักใบ** (ไม่มี slot จึงคำนวณ `expires_at` ไม่ได้)
ระบบจะบอกทุกครั้งด้วย `outbox_skipped` ใน response และนับสะสมไว้ที่
`webull_lego_warnings/degraded_clock_no_order` — ตั้ง alert ที่ node นี้ได้เลย

---

## 6.1) 📮 Deploy function ตัวที่สอง — `lego-order-worker`

> ⚠️ **ข้ามข้อนี้ไม่ได้** — ตัวอย่างในคู่มือนี้ตั้ง `AUTO_SUBMIT=true` มาแล้ว
> `lego_one_row` แค่ **จด order intent** ลง outbox แล้วจบ (ตั้งใจ: ถ้าไปรอ broker
> ฟังก์ชันจะช้าจน scheduler timeout แล้ว retry จนกิน DNA step) ตัวที่ส่ง order จริงคือ
> `lego_order_worker` — **ไม่ deploy = intent ทุกใบหมดอายุเป็น `EXPIRED_UNSENT` ไม่มี order
> ออกสักใบ**

ใช้ env และ secret ชุดเดียวกับ `lego-one-row` เป๊ะ ๆ (คนละ entry point เท่านั้น):

```bash
gcloud functions deploy lego-order-worker \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source=. \
  --entry-point=lego_order_worker \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=512Mi \
  --timeout=300s \
  --set-env-vars="FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=AAPL,LEGO_FIX_C=3000,LEGO_DIFF=5,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=3,LEGO_SLOT_SECONDS=900,LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z,LEGO_DNA_CLOCK_MODE=market,AUTO_SUBMIT=true" \
  --set-secrets="WEBULL_APP_KEY=webull-app-key:latest,WEBULL_APP_SECRET=webull-app-secret:latest,WEBULL_ACCOUNT_ID=webull-account-id:latest"
```

📌 **สำคัญ:** `LEGO_SYMBOL`, `LEGO_FIX_C`, `LEGO_DIFF`, `LEGO_DNA_CODE`,
`LEGO_DECIMAL_PRECISION`, `LEGO_STRATEGY_ID` ต้องเท่ากันทั้งสองฟังก์ชัน เพราะค่าพวกนี้
ประกอบเป็น `chain_key` — ถ้าไม่ตรง worker จะมองไม่เห็น outbox ของ chain ที่ engine เขียน

worker จะทำตามลำดับนี้ทุกครั้ง แล้วหยุดทันทีที่ข้อไหนไม่ผ่าน (fail closed):
แถว committed แล้วหรือยัง → ตามผล order ที่ค้างอยู่ → หมดอายุ slot แล้วหรือยัง →
มี order เปิดค้างไหม → holdings เปลี่ยนไปหรือยัง → เป็น UAT ไหม → preview + submit gate →
place → poll สถานะ → มี fill จริงจึงบันทึก realized

---

## 6.2) 🗄️ Deploy function ตัวที่สาม — `lego-archive-worker` (งานบ้าน)

ไม่ deploy ก็เทรดได้ แต่ `webull_lego_order_outbox` และ `webull_lego_order_audit`
จะโตขึ้นทุก slot ตลอดไป และทั้ง order worker กับ dashboard ต้อง **โหลดทั้ง path**
ทุกครั้งเพื่อหา record ที่ยังมีชีวิตไม่กี่ใบ ตัวนี้ย้าย record ที่จบแล้วและเก่ากว่า
`LEGO_ARCHIVE_RETENTION_DAYS` ไปไว้ที่ `*_archive` (ย้าย ไม่ลบ — ยังสอบย้อนหลังได้)

```bash
gcloud functions deploy lego-archive-worker \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source=. \
  --entry-point=lego_archive_worker \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=512Mi \
  --timeout=300s \
  --set-env-vars="FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=AAPL,LEGO_FIX_C=3000,LEGO_DIFF=5,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=3,LEGO_SLOT_SECONDS=900,LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z,LEGO_DNA_CLOCK_MODE=market,AUTO_SUBMIT=true" \
  --set-secrets="WEBULL_APP_KEY=webull-app-key:latest,WEBULL_APP_SECRET=webull-app-secret:latest,WEBULL_ACCOUNT_ID=webull-account-id:latest"
```

ตั้งนาฬิกาวันละครั้งหลังตลาดปิดก็พอ:

```bash
export ARCHIVE_URL="$(gcloud functions describe lego-archive-worker --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"

gcloud scheduler jobs create http lego-archive-tick \
  --location="$REGION" \
  --schedule="30 22 * * *" \
  --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$ARCHIVE_URL" \
  --http-method=POST \
  --oidc-service-account-email="$FUNCTION_SA" \
  --oidc-token-audience="$ARCHIVE_URL"
```

---

## 7) ⏰ Deploy Google Cloud Scheduler — นาฬิกาปลุกของระบบ

ก่อนอื่นดึง URL และ service account ของ Cloud Function มาเก็บไว้:

```bash
export FUNCTION_URL="$(gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"
export FUNCTION_SA="$(gcloud functions describe lego-one-row --gen2 --region="$REGION" --format='value(serviceConfig.serviceAccountEmail)')"

echo "$FUNCTION_URL"
echo "$FUNCTION_SA"
```

สร้าง scheduler ให้เรียก function **ทุก 5 นาที จันทร์–ศุกร์** ในช่วงเวลา UTC ที่ครอบคลุมตลาดสหรัฐฯ:

```bash
gcloud scheduler jobs create http lego-tick \
  --location="$REGION" \
  --schedule="*/5 13-20 * * 1-5" \
  --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --oidc-service-account-email="$FUNCTION_SA" \
  --oidc-token-audience="$FUNCTION_URL"
```

> 📖 **อ่าน schedule ยังไง?** `*/5 13-20 * * 1-5` = ทุก ๆ 5 นาที ในชั่วโมง 13–20 UTC วันจันทร์ถึงศุกร์ (ครอบคลุมเวลาเปิด–ปิดตลาดหุ้นสหรัฐฯ)

ลองทดสอบยิง scheduler ด้วยมือ (ไม่ต้องรอถึงเวลา):

```bash
gcloud scheduler jobs run lego-tick --location="$REGION"
```

แล้วดู log ของ function ว่าทำงานไหม:

```bash
gcloud functions logs read lego-one-row --gen2 --region="$REGION" --limit=50
```

> 🧭 **scheduler ยิงถี่กว่า slot ได้ ไม่เสียหาย** — `*/5` กับ `LEGO_SLOT_SECONDS=900`
> แปลว่า 3 tick ต่อ 1 slot: tick แรก commit แถว อีก 2 tick ได้ `SLOT_CONSUMED` (200)
> เพราะ slot guard ตีตกให้ **ห้ามยิงห่างกว่า slot** เด็ดขาด เพราะ slot ที่พลาดไปจะถูกข้าม
> ถาวร (DNA เดินตามเวลาตลาด ไม่ย้อนกลับไปใช้ signal เก่า)
>
> ⚠️ กติกานี้ผูกกับ `LEGO_SLOT_SECONDS` โดยตรง: เปลี่ยนขนาด slot เมื่อไร ต้องแก้ cron
> ให้ถี่กว่าเสมอ — กริด 15 นาทีกับ `*/20` จะพลาด slot ทิ้งทุกวัน

### 7.1) scheduler ของ order worker

ถ้า deploy `lego-order-worker` ตามหัวข้อ 6.1 ให้มันมีนาฬิกาของตัวเองด้วย (ยิงถี่กว่าได้
เพราะไม่แตะ DNA — มันแค่ไล่ intent ที่ค้างอยู่ในหน้าต่างของ slot ปัจจุบัน):

```bash
export WORKER_URL="$(gcloud functions describe lego-order-worker --gen2 --region="$REGION" --format='value(serviceConfig.uri)')"

gcloud scheduler jobs create http lego-order-tick \
  --location="$REGION" \
  --schedule="*/5 13-20 * * 1-5" \
  --time-zone="UTC" \
  --max-retry-attempts=0 \
  --uri="$WORKER_URL" \
  --http-method=POST \
  --oidc-service-account-email="$FUNCTION_SA" \
  --oidc-token-audience="$WORKER_URL"
```

---

## 7.5) 🧬 เปิด market mode — ให้ DNA เดินตามเวลาตลาดจริง

ค่าเริ่มต้น `LEGO_DNA_CLOCK_MODE=shadow` คือ **ยังไม่เปลี่ยนพฤติกรรม**: step เดินแบบเดิม
(`anchor + 1`) แต่ระบบจะรายงาน `alignment_error` ให้เห็นว่าห่างจากเวลาตลาดเท่าไร
ใช้ดูสัก 1–2 วันก่อนได้

**ทำไมต้องมี origin?** DNA ถูกเทรนจากลำดับแท่งเทียน (bar index) ไม่ใช่ timestamp
production จึงต้องรักษาสมการนี้ตลอดอายุ chain:

```
market_ordinal(t)  ==  bar index ที่ DNA เทรนมา
```

`market_ordinal` นับ slot เดินหน้าจากจุดเริ่ม (`LEGO_DNA_ORIGIN_UTC`) ดังนั้น "จุดเริ่ม" คือ
slot ที่อยู่ก่อนหน้า slot ปัจจุบันเท่ากับ step ที่เราอยากได้ — คำนวณด้วย `find_origin.py`
(นับเฉพาะเวลาทำการจริง: ข้ามกลางคืน เสาร์อาทิตย์ วันหยุด และวันปิดครึ่งวันให้อัตโนมัติ):

```bash
# รันในเวลาตลาดเปิด · <N> = DNA step ที่อยากให้ "แถวถัดไป" เป็น
# chain ใหม่ = 0 · chain เดิมที่ anchor.dna_step = 41 ให้ใส่ 42
export LEGO_SLOT_SECONDS=900
python find_origin.py 0
```

ผลลัพธ์จะบอกค่าที่ต้อง set ตรง ๆ เช่น:

```
LEGO_SLOT_SECONDS = 900
slot ปัจจุบัน      = 2026-07-27:0 (เริ่ม 2026-07-27T13:30:00Z)
ตั้งค่าเป็น:
  LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z
  LEGO_DNA_CLOCK_MODE=market
```

> กริด 15 นาทีมี **26 slot ต่อวันทำการปกติ** (early close 14) — `2026-07-27:0` คือ slot แรก
> ของวัน (`market_ordinal 0`), `2026-07-27:25` คือ slot สุดท้าย และ `2026-07-28:0`
> เดินต่อเป็น `market_ordinal 26` ทันที ไม่นับกลางคืน/เสาร์อาทิตย์/วันหยุด

เอาไป update ทั้งสองฟังก์ชัน (ค่าต้องตรงกัน):

```bash
gcloud functions deploy lego-one-row --gen2 --region="$REGION" \
  --source=. --entry-point=lego_one_row \
  --update-env-vars="LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z,LEGO_DNA_CLOCK_MODE=market"

gcloud functions deploy lego-order-worker --gen2 --region="$REGION" \
  --source=. --entry-point=lego_order_worker \
  --update-env-vars="LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z,LEGO_DNA_CLOCK_MODE=market"
```

> เขียนแยกสองคำสั่งเพราะ `--entry-point` ของสองฟังก์ชันไม่เหมือนกัน — ระบุให้ชัดทุกครั้ง
> อย่าพึ่งให้ gcloud จำค่าเดิม และรันจาก root ของ repo เสมอเพราะ `--source=.`

> 🚨 **แก้ได้ครั้งเดียวก่อน commit แถวแรกเท่านั้น**
> `LEGO_DNA_ORIGIN_UTC`, `LEGO_SLOT_SECONDS`, `LEGO_MARKET_HOLIDAYS`,
> `LEGO_MARKET_EARLY_CLOSES` ทุกตัวถูกผูกเป็น `calendar_fingerprint` ไว้กับ chain
> เปลี่ยนทีหลัง = ระบบตอบ `CALENDAR_DRIFT` (409) และหยุดนิ่ง **โดยตั้งใจ** เพราะถ้าเดินต่อ
> gate array จะเลื่อน phase ถาวร (บอทจะเทรดคนละ slot กับที่ backtest มา)
> จะเปลี่ยนจริง ๆ ต้อง **เริ่ม chain ใหม่** หรือคืนค่าเดิม
>
> ส่วน `LEGO_SYMBOL`, `LEGO_FIX_C`, `LEGO_DIFF`, `LEGO_DECIMAL_PRECISION`,
> `LEGO_DNA_CODE`, `LEGO_STRATEGY_ID` เปลี่ยนแล้ว `config_hash` เปลี่ยน = **chain ใหม่**
> (ของเก่ายังอยู่ครบใน RTDB ไม่ถูกลบ)

เช็คว่าเข้าโหมดแล้วจริงจาก response ของ function: `clock_mode` ต้องเป็น `market`
และ `step` ต้องเท่ากับ `market_step`

---

## 7.6) 🚦 Checklist ก่อนเปิด `AUTO_SUBMIT=true` (บังคับด้วยโค้ด)

`AUTO_SUBMIT=true` **ไม่ใช่** สวิตช์เดียวที่ตัดสินว่าจะส่ง order ทุกแถวที่เป็น `READY_*`
ต้องผ่าน `auto_submit_preflight` (`lego_preflight.py`) ครบทุกข้อก่อน ไม่ผ่านแม้ข้อเดียว =
**แถวยัง commit ปกติ แต่ไม่มี order intent ถูกสร้าง** และ response จะบอกเหตุผลเสมอ

| # | check | ผ่านเมื่อ | ไม่ผ่านแปลว่า |
|---|---|---|---|
| 1 | `auto_submit_enabled` | `AUTO_SUBMIT=true` | ยังไม่เปิด |
| 2 | `environment_uat` | `WEBULL_ENV=UAT` | Production = read-only |
| 3 | `row_durable` | แถว commit หรือ idempotent แล้ว | ห้ามสั่งจากแถวที่ยังไม่ persist |
| 4 | `row_actionable` | สถานะ `READY_BUY`/`READY_SELL` และ quantity > 0 | ไม่ใช่ decision ที่ส่งได้ |
| 5 | `clock_not_degraded` | resolve slot ได้ (ตั้ง `LEGO_DNA_ORIGIN_UTC` แล้ว) | ไม่มี slot window → คำนวณ `expires_at` ไม่ได้ |
| 6 | `step_matches_market_ordinal` | `DNA step` = `market_ordinal` ของ slot | order จะตกคนละ slot กับที่ DNA เทรนมา (เกิดใน mode `shadow` เมื่อ scheduler พลาด slot) |
| 7 | `token_ready` | `token_health()["ready"]` | ไม่พบ token file / status ≠ `NORMAL` / ใกล้หมดอายุ · **token dir ไม่คงอยู่** ก็บล็อกเช่นกัน เว้นแต่ตั้ง `LEGO_ALLOW_EPHEMERAL_TOKEN_DIR=true` |
| 8 | `dna_headroom` | เหลือ ≥ `LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING` | chain ใกล้ `DNA_EXHAUSTED` |

ลำดับที่แนะนำให้ไล่ปิดก่อนเปิดจริง:

1. ย้าย `WEBULL_TOKEN_DIR` ออกจาก `/tmp` ไป volume ที่คงอยู่ (ตาราง env หัวข้อ 6 → check 7)
   ⚠️ **Cloud Functions มี `/tmp` เป็น path เดียวที่เขียนได้** ถ้าไม่ mount volume ให้ check 7 จะไม่มีวันผ่าน
   → ทุกแถว `READY_BUY`/`READY_SELL` จะ commit แต่ไม่มี order intent เลยสักใบ และจำนวนถือครองจะไม่ขยับ
   ถ้ายังไม่พร้อม mount แต่ต้องการเดินต่อ ให้ตั้ง `LEGO_ALLOW_EPHEMERAL_TOKEN_DIR=true`
   (ยอมรับความเสี่ยงว่า token หายเมื่อ instance ถูกรีไซเคิลและต้องกด 2FA ใหม่ — คำเตือน
   `webull_lego_warnings/webull_token` ยังขึ้นทุก slot เหมือนเดิม)
2. `python find_origin.py <dna_step+1>` → ตั้ง `LEGO_DNA_ORIGIN_UTC` → `LEGO_DNA_CLOCK_MODE=market`
   (ข้อ 7.5 → check 5 และ 6) · เปลี่ยนหลัง commit แรก = `CalendarDriftError` ต้องเริ่ม chain ใหม่
3. ยิง 1 slot แล้วยืนยันว่า response มี `market_slot_id`, `market_step` และ `clock_mode` ไม่มีคำว่า `degraded`
4. ตัดสินใจเรื่อง DNA (ต่ออายุ / ใส่ champion จริง) ให้เหลือ headroom พอ
5. ตั้ง alert บน `webull_lego_warnings/auto_submit_blocked` และ `.../webull_token`
6. เฝ้า slot แรกที่ได้ `READY_*` ด้วยตา — ตัวอย่างในคู่มือนี้ตั้ง `AUTO_SUBMIT=true`
   มาตั้งแต่ deploy แรกแล้ว ดังนั้นข้อ 1–5 คือสิ่งที่ตัดสินว่ามี intent ออกจริงหรือไม่
   ไม่ใช่ตัวสวิตช์ · ถ้าอยากปิดสนิทระหว่างไล่ข้อ 1–5 ให้ตั้ง `AUTO_SUBMIT=false` ก่อน
   แล้วเปลี่ยนกลับเป็น `true` เมื่อพร้อม (ไม่กระทบ `config_hash` — ไม่ใช่ chain ใหม่)

> [!IMPORTANT]
> preflight เป็น fail-closed: อ่านค่าไหนไม่ได้ หรือตัว preflight เองพัง ก็นับเป็น "ไม่ผ่าน"
> ไม่มีทางที่ intent จะถูกสร้างโดยข้าม checklist

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

1. ⏰ Cloud Scheduler ยิงตามเวลา (ทุก 5 นาที)
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
- ✅ `webull_lego_state` มี version ล่าสุด และ `market_ordinal` เดินหน้าเสมอ (ห้ามเท่าเดิม/ถอย)
- ✅ ไม่มี error ผิดปกติใน `webull_lego_errors`
- ✅ ถ้าเปิด `AUTO_SUBMIT=true`: `webull_lego_order_outbox` ต้องไม่ค้างเป็น `PENDING_DISPATCH`
  ข้าม slot — ถ้าเห็น `EXPIRED_UNSENT` ทุกใบ แปลว่ายังไม่ได้ deploy `lego-order-worker` (ข้อ 6.1)
- ⚠️ ถ้าเห็น `RECONCILE_ABANDONED` ใน outbox (หรือ `needs_manual_check: true` ใน
  `webull_lego_order_audit`) = **ต้องเข้าไปเช็คที่ broker เองว่า order ใบนั้นมีจริงไหม**
  ระบบถาม broker จนครบ `LEGO_RECONCILE_MAX_ATTEMPTS` แล้วไม่ได้คำตอบ จึงเลิกถามเพื่อไม่ให้
  ไปเบียด order ใหม่ · อ่าน `first_error` เพื่อรู้สาเหตุตั้งต้น (`last_error` คือครั้งล่าสุด)

ค่า `pipeline_status` ที่ต้องอ่านให้ออกจาก log:

| เห็นแบบนี้ | แปลว่า | ต้องทำอะไร |
|---|---|---|
| `ROW_COMMITTED` | ปกติ | ไม่ต้องทำอะไร |
| `MARKET_CLOSED` | นอกเวลา/วันหยุด | ปกติ ไม่ใช่ error |
| `SLOT_CONSUMED` | slot นี้ commit ไปแล้ว | ปกติเมื่อ scheduler ยิงถี่กว่า slot |
| `CONFIG_ERROR` | `LEGO_SLOT_SECONDS` ไม่ตั้ง/ไม่รองรับ | แก้ env แล้ว deploy ใหม่ |
| `STALE_ANCHOR` | มี 2 instance เขียนชนกัน | ตั้ง `--max-retry-attempts=0` และอย่ายิงซ้อน |
| `CALENDAR_DRIFT` | ปฏิทิน/slot/origin เปลี่ยนหลัง commit แรก | คืนค่าเดิม หรือเริ่ม chain ใหม่ (ข้อ 7.5) |
| `ORDINAL_REGRESSION` | slot ให้ ordinal ที่ไม่เดินหน้า | ตรวจ origin/เวลาเครื่อง — DNA เดินถอยไม่ได้ |
| `HOLDINGS_ANOMALY` | chain เคยเห็นของ แต่ snapshot อ่านได้ 0 | เช็คที่ broker ว่ายังถืออยู่ไหม · ถ้าถืออยู่จริง = positions response ไม่ครบ รอรอบหน้า · ถ้าขายทิ้งไปจริง ตั้ง `LEGO_ALLOW_ZERO_HOLDINGS=true` 1 รอบแล้วเอาออก |
| `DNA_DRIFT` | `dna_code` เดิม แต่ decode ได้ gate array คนละชุด | เกือบทั้งหมดคือ **numpy เปลี่ยนเวอร์ชัน** — คืน numpy ตัวที่ pin ไว้ใน `requirements.txt` หรือเริ่ม chain ใหม่ ห้ามปล่อยผ่าน (= เทรดคนละกลยุทธ์ใต้ชื่อเดิม) |
| `DNA_EXHAUSTED` | DNA เดินจนหมด array แล้ว (HTTP 200 ไม่ใช่ error) | ต่อ DNA ที่ยาวกว่าเดิม (= chain ใหม่ เพราะ `config_hash` เปลี่ยน) หรือหยุด scheduler ของ chain นี้ · ป้องกันล่วงหน้าด้วย `dna_steps_remaining` ที่ response แนบมาเมื่อใกล้หมด |

field เตือนใน response ของแถวที่ commit สำเร็จ (ไม่มี field = ไม่มีอะไรต้องดู):

| field | แปลว่า | ต้องทำอะไร |
|---|---|---|
| `outbox_skipped` | แถวเป็น `READY_*` และเปิด `AUTO_SUBMIT` แล้ว แต่ **ไม่มี order intent ถูกสร้าง** เพราะ clock degraded | ตั้ง `LEGO_DNA_ORIGIN_UTC` (ข้อ 7.5) — ระหว่างนี้ DNA เดินต่อแต่ไม่มีคำสั่งซื้อขายออกเลย |
| `outbox_blocked` + `outbox_blocked_checks` | เปิด `AUTO_SUBMIT` แล้วแต่ **preflight ไม่ผ่าน** จึงไม่สร้าง intent | ดูว่า `outbox_blocked_checks` ติดข้อไหน แล้วแก้ตามตารางหัวข้อ 7.6 · นับสะสมที่ `webull_lego_warnings/auto_submit_blocked` |
| `outbox_error` | สร้าง intent ไม่สำเร็จ (แถว commit แล้ว ไม่ rollback) | ดู error แล้วเช็ค RTDB rules/quota · slot ถัดไปยังทำงานปกติ |
| `clock_warning` | resolve slot ไม่ได้ จึงเดินด้วย legacy step | เหมือน `outbox_skipped` — ต้นเหตุเดียวกัน |
| `dna_steps_remaining` | DNA เหลือน้อยกว่า `LEGO_DNA_LOW_WATERMARK` | เตรียม DNA ชุดใหม่ก่อนถึง `DNA_EXHAUSTED` |
| `token_warning` | token ของ Webull ใกล้หมดอายุ / ไม่พบ / เก็บใน dir ที่ไม่คงอยู่ | ดูหัวข้อ `WEBULL_TOKEN_DIR` · นับสะสมที่ `webull_lego_warnings/webull_token` · แถวยัง commit ปกติ ไม่หยุด DNA · เห็นคู่กับ `outbox_blocked_checks: ["token_ready"]` = ยังไม่ได้ตั้ง `LEGO_ALLOW_EPHEMERAL_TOKEN_DIR` และยังไม่ mount volume |

> [!WARNING]
> **อาการ "จำนวนถือครอง (หุ้น) ไม่เปลี่ยนเลย" ทั้งที่แถวเป็น `READY_SELL`/`READY_BUY` ทุก slot**
> คอลัมน์ที่ 7 อ่าน position สดจาก broker ทุกแถว — ค่าที่ไม่ขยับหมายความว่า **ไม่มี order ไปถึง broker**
> ไม่ใช่ว่าคอลัมน์ค้าง ไล่ตามลำดับนี้:
> 1. response ของ `lego-one-row` มี `outbox_blocked_checks` ไหม → preflight บล็อก (ดูตารางหัวข้อ 7.6)
>    ข้อที่เจอบ่อยที่สุดคือ `token_ready` เพราะ `WEBULL_TOKEN_DIR` default เป็น `/tmp`
> 2. `webull_lego_order_outbox/{chain_key}` ว่างเปล่า = ไม่เคยมี intent → ปัญหาอยู่ที่ข้อ 1
> 3. มี intent แต่ status เป็น `EXPIRED_UNSENT` / `SUPPRESSED_*` / `NOT_PLACED` → ปัญหาอยู่ที่ order worker
> 4. เวลาตอบของ `lego-order-worker` ~0.2 วิ ทุกรอบ = ไม่มีอะไรใน queue ให้ทำ (ยืนยันข้อ 2)

สถานะ outbox ที่ต้องมีคนเข้าไปดู (นอกจาก `RECONCILE_ABANDONED`):

| status | แปลว่า | ต้องทำอะไร |
|---|---|---|
| `REALIZED_MATH_ERROR` | **order fill สำเร็จแล้วที่ broker** แต่คำนวณ realized ต่อไม่ได้ (ตัวเลข cumulative fill ที่ได้มาทำให้ราคาต่อหน่วยของส่วนเพิ่ม ≤ 0) | ห้ามส่ง order ซ้ำ — order มีจริงและ fill แล้ว · เข้าไปกระทบยอด `webull_lego_realized` เอง โดยดู `filled_quantity`/`filled_price` ที่เก็บไว้ใน audit |

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
| เวลาตลาด / slot / ปฏิทิน / วันหยุด | `market_clock.py` (แหล่งเดียว — ห้ามเขียนกฎเวลาตลาดที่อื่น) |
| คิว order ที่รอส่ง | `lego_outbox.py` |
| การเชื่อมต่อ Webull / external API | `webull_io.py` |
| หา `LEGO_DNA_ORIGIN_UTC` | `find_origin.py` (เครื่องมือ CLI ไม่ใช่ส่วนของ runtime) |
| Dependency Python | `requirements.txt` |
| เอกสารวิธีใช้งาน | `README.md`, `QUICKSTART_TH.md` |
| ค่า config ตอน deploy | คำสั่ง `gcloud functions deploy` (ข้อ 6 และ 6.1) |

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
  --set-env-vars="FIREBASE_DB_URL=$DB_URL,WEBULL_ENV=UAT,LEGO_SYMBOL=AAPL,LEGO_FIX_C=3000,LEGO_DIFF=5,LEGO_DNA_CODE=bypass:100,LEGO_DECIMAL_PRECISION=3,LEGO_SLOT_SECONDS=900,LEGO_DNA_ORIGIN_UTC=2026-07-27T13:30:00Z,LEGO_DNA_CLOCK_MODE=market,AUTO_SUBMIT=true" \
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
