# Signature Algorithm (HMAC-SHA256) — รายละเอียดฉบับเต็ม

> ปกติ SDK ทางการทำ signature ให้อัตโนมัติ ทำเองเฉพาะกรณีไม่ใช้ SDK

## สิ่งที่ถูกนำมา sign (What Gets Signed)

signature คำนวณจาก 4 ส่วนของ HTTP request:
1. Request path
2. Query parameters
3. Request body
4. **Signing headers** เหล่านี้เท่านั้น:
   - `x-app-key`
   - `x-signature-algorithm`
   - `x-signature-version`
   - `x-signature-nonce`
   - `x-timestamp`
   - `host`

**ไม่รวมในการ sign:** `x-signature` (คือผลลัพธ์เอง) และ `x-version` (จำเป็นเป็น header แต่ไม่เข้าสูตร)
สำหรับ POST: `Content-Type` ต้องเป็น `application/json`

## อัลกอริทึม 3 ขั้น

### Step 1 — สร้าง Signature String
1. รวม query params + signing headers เป็นลิสต์เดียว
2. เรียงชื่อพารามิเตอร์ตามตัวอักษร A→Z (ascending)
3. ต่อเป็น `name1=value1&name2=value2&...` → ได้ **str1**
4. ถ้ามี body: `str2 = toUpper(SHA256(body))`
5. ต่อกัน: `str3 = path + "&" + str1 + "&" + str2`  (ถ้า body ว่าง: `str3 = path + "&" + str1` และตัด str2 ทิ้ง)
6. URL-encode `str3` → ได้ **encoded_string**

> ⚠️ ห้ามมีช่องว่างเกินระหว่าง key กับ value ใน body — เนื้อหาที่ sign **ยังไม่ต้อง** URL-encode ในขั้นก่อนหน้า (encode เฉพาะ str3 ตอน step 1.6)

### Step 2 — สร้าง Key
ต่อ `&` ท้าย App Secret:
```
key = "<your_app_secret>&"
```

### Step 3 — สร้าง Signature
```
signature = base64(HMAC-SHA256(key, encoded_string))
```

## Worked Example (ใช้ตรวจสอบโค้ดของคุณ)

**Request:**
- Path: `/trade/place_order`
- Query: `a1=webull`, `a2=123`, `a3=xxx`, `q1=yyy`
- Headers:
  - `x-app-key = 776da210ab4a452795d74e726ebd74b6`
  - `x-timestamp = 2022-01-04T03:55:31Z`
  - `x-signature-version = 1.0`
  - `x-signature-algorithm = HMAC-SHA256`
  - `x-signature-nonce = 48ef5afed43d4d91ae514aaeafbc29ba`
  - `host = api.webull.com.sg`
- Body:
  ```json
  {"k1":123,"k2":"this is the api request body","k3":true,"k4":{"foo":[1,2]}}
  ```
- App Secret: `0f50a2e853334a9aae1a783bee120c1f`

**str1** (เรียง A→Z แล้ว):
```
a1=webull&a2=123&a3=xxx&host=api.webull.com.sg&q1=yyy&x-app-key=776da210ab4a452795d74e726ebd74b6&x-signature-algorithm=HMAC-SHA256&x-signature-nonce=48ef5afed43d4d91ae514aaeafbc29ba&x-signature-version=1.0&x-timestamp=2022-01-04T03:55:31Z
```

**str2** = `E296C96787E1A309691CEF3692F5EEDD`

**str3** = `/trade/place_order&` + str1 + `&E296C96787E1A309691CEF3692F5EEDD`

**key** = `0f50a2e853334a9aae1a783bee120c1f&`

**✅ ผลลัพธ์ที่ถูกต้อง:** `kvlS6opdZDhEBo5jq40nHYXaLvM=`
ถ้าโค้ดคุณให้ค่านี้ = implement ถูกต้อง

## โครงโค้ด Python (ไม่ใช้ SDK)

```python
import hashlib, hmac, base64, json, uuid, urllib.parse
from datetime import datetime, timezone
import requests

APP_KEY = "<app_key>"
APP_SECRET = "<app_secret>"
HOST = "<api_endpoint>"

def generate_signature(path, query_params, body_string, app_key, app_secret, host, timestamp, nonce):
    signing_headers = {
        "x-app-key": app_key,
        "x-timestamp": timestamp,
        "x-signature-algorithm": "HMAC-SHA256",
        "x-signature-version": "1.0",
        "x-signature-nonce": nonce,
        "host": host,
    }
    # 1. รวม query + signing headers แล้วเรียง A→Z
    all_params = {**query_params, **signing_headers}
    str1 = "&".join(f"{k}={all_params[k]}" for k in sorted(all_params))
    # 2. body -> SHA256 uppercase  (body_string ต้องเป็น string เดียวกับที่ส่งจริง)
    if body_string:
        str2 = hashlib.sha256(body_string.encode()).hexdigest().upper()
        str3 = f"{path}&{str1}&{str2}"
    else:
        str3 = f"{path}&{str1}"
    # 3. URL-encode
    encoded = urllib.parse.quote(str3, safe="")
    # 4. HMAC-SHA256 ด้วย key = app_secret + "&"
    key = (app_secret + "&").encode()
    sig = hmac.new(key, encoded.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()

# ใช้งาน: GET /openapi/account/list
timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
nonce = uuid.uuid4().hex
sig = generate_signature("/openapi/account/list", {}, "", APP_KEY, APP_SECRET, HOST, timestamp, nonce)
headers = {
    "x-app-key": APP_KEY, "x-timestamp": timestamp,
    "x-signature-algorithm": "HMAC-SHA256", "x-signature-version": "1.0",
    "x-signature-nonce": nonce, "x-version": "v2", "x-signature": sig,
}
# requests.get(f"https://{HOST}/openapi/account/list", headers=headers)
```

## ⭐ Edge Cases ที่ทำให้ signature พัง (สาเหตุ INVALID_TOKEN อันดับ 1)

### 1. JSON Body Serialization (สำคัญที่สุดสำหรับ POST เช่น place_order)
- string ที่ใช้คำนวณ SHA256 **ต้องเป็น string เดียวกันเป๊ะ** กับที่ส่งใน HTTP body
- ใช้ compact serialization ไม่มีช่องว่าง: `json.dumps(body, separators=(',', ':'))`
- **ห้ามใช้ `json=body`** ใน `requests.post()` เพราะ library จะ serialize เองได้ string คนละแบบกับที่เราคำนวณ SHA256 → signature ไม่ตรง → เจอ `INVALID_TOKEN`
- วิธีถูก:
  ```python
  body_string = json.dumps(body, separators=(',', ':'))
  sig = generate_signature(path, {}, body_string, ...)
  requests.post(url, data=body_string,
                headers={..., "Content-Type": "application/json"})
  ```

### 2. Language-Specific HTML Escaping
บางภาษา escape `<`, `>`, `&` ใน JSON output อัตโนมัติ ต้อง unescape ก่อนคำนวณ SHA256
เช่น Go (`json.Marshal` ตั้ง `escapeHtml=true` โดยดีฟอลต์) ต้องแทน `&`→`&`, `<`→`<`, `>`→`>`

### 3. Duplicate Parameter Names
ถ้ามีชื่อพารามิเตอร์ซ้ำ ต้องจัดการตามกฎ merge/sort ให้ตรงกับที่ server คาดหวัง

> 💡 ถ้าใช้ SDK ทางการ ปัญหาทั้ง 3 ข้อนี้ไม่เกิด — SDK จัดการ serialization + signature ให้ครบ
