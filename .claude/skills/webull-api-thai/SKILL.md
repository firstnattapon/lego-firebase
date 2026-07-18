---
name: webull-api-thai
description: ผู้เชี่ยวชาญ Webull OpenAPI (ภาษาไทย) สำหรับเทรดหุ้น/ETF ตลาดสหรัฐฯ แบบ programmatic ครอบคลุม Authentication + Signature (HMAC-SHA256), การตั้งค่า SDK (Python/Java), Market Data (HTTP + MQTT streaming), Trading (place/replace/cancel order), Accounts (balance/positions) และการแก้ปัญหาที่พบบ่อย รวมถึงกลยุทธ์ Shannon Demon LEGO (one-new-row pipeline, DNA gate, recurrence Rₙ/ΔAₙ/Aₙ/Eₙ, flowchart) ใน references/lego.md ใช้ skill นี้ทุกครั้งที่ผู้ใช้พูดถึง Webull API, webull-openapi-sdk, x-signature, place_order, get_history_bar, MQTT quote, 403 market data, Shannon Demon, LEGO flowchart/diagram, DNA step/signal, PASS/BUY/SELL หรือถามวิธีเชื่อมต่อ/เขียนโค้ดเทรดกับ Webull ทั้งภาษาไทยและอังกฤษ
---

# Webull OpenAPI — คู่มือผู้เชี่ยวชาญ (ภาษาไทย)

ตอบเป็นภาษาไทยเสมอ (ยกเว้นผู้ใช้ขอภาษาอื่น) โค้ดและชื่อพารามิเตอร์ให้คงภาษาอังกฤษตามจริง

## ภาพรวมระบบ

Webull OpenAPI ให้บริการเทรดเชิงปริมาณ (quantitative trading) สำหรับ **ตลาดสหรัฐฯ (NYSE, NASDAQ) — หุ้นและ ETF เท่านั้น**

โปรโตคอลที่ใช้ 3 แบบ:
| โปรโตคอล | ใช้ทำอะไร |
|---|---|
| HTTP | เทรด, จัดการบัญชี, ดึงข้อมูลตลาดย้อนหลัง/snapshot |
| MQTT (ผ่าน WebSocket/TCP) | สตรีมข้อมูลตลาดแบบเรียลไทม์ |
| gRPC | รับ event push เช่น สถานะออเดอร์เปลี่ยน |

**ทุก request ต้องเป็น HTTPS เท่านั้น** — HTTP ธรรมดาจะ fail

## หลักการทำงาน 3 ข้อที่ต้องจำ

1. **ใช้ SDK ทางการเสมอถ้าทำได้** — SDK จัดการ signature + token 2FA + โปรโตคอลให้อัตโนมัติ เขียน signature เองเมื่อจำเป็นจริงๆ เท่านั้น (ดู `references/signature.md`)
2. **Authentication มี 2 ชั้น**: Signature (ทุก request) + Token (2FA จำเป็นครั้งแรก แล้ว reuse ได้)
3. **ราคาและตัวเลขใน response เป็น string** เพื่อรักษาความแม่นยำ — timestamp เป็น Unix millis

## การติดตั้ง SDK

**Python (3.8–3.13):**
```bash
pip3 install --upgrade webull-openapi-python-sdk
```

**Java (JDK 8+, Maven):**
```xml
<dependency>
  <groupId>com.webull.openapi</groupId>
  <artifactId>webull-openapi-java-sdk</artifactId>
  <version>1.0.3</version>
</dependency>
```

## Environments (ไทย — regionId `"th"`)

| Service | Production | Test (UAT) |
|---|---|---|
| Trading API | `api.webull.co.th` | `th-api.uat.webullbroker.com` |
| Market Data API | `api.webull.co.th` | `th-api.uat.webullbroker.com` |
| Trading Events (gRPC) | `events-api.webull.co.th` | `th-events-api.uat.webullbroker.com` |
| Market Data Streaming (MQTT) | `data-api.webull.co.th` | `data-api.uat.webullbroker.com` |

เปลี่ยน environment แค่เปลี่ยน endpoint ตอน init client — ไม่ต้องแก้โค้ดอื่น
Test environment มี test account แชร์สาธารณะ (ดู `references/endpoints.md`) — ใช้เริ่มเขียนโค้ดได้ทันทีไม่ต้องสมัคร แต่ orders/positions เปลี่ยนได้ตลอดเพราะแชร์กัน

## Authentication headers (กรณีเรียกเองไม่ผ่าน SDK)

| Header | จำเป็น | ค่า |
|---|---|---|
| `x-app-key` | ✓ | App Key |
| `x-timestamp` | ✓ | ISO 8601 UTC: `YYYY-MM-DDThh:mm:ssZ` |
| `x-signature` | ✓ | ลายเซ็น HMAC-SHA256 |
| `x-signature-algorithm` | ✓ | `HMAC-SHA256` |
| `x-signature-version` | ✓ | `1.0` |
| `x-signature-nonce` | ✓ | random string ใหม่ทุก request |
| `x-version` | ✓ | `v2` |
| `x-access-token` | ✓ (หลัง 2FA) | token ที่ผ่านการยืนยันแล้ว |

**สำคัญ:** `app_secret` ใช้คำนวณ signature ฝั่ง client เท่านั้น — **ห้ามส่งเป็น header เด็ดขาด** และห้าม commit App Key/Secret ขึ้น GitHub
ขั้นตอนสร้าง signature แบบละเอียดพร้อมตัวอย่างที่ตรวจสอบได้ → อ่าน `references/signature.md`

## Market Data

- **Data API (HTTP)** — ข้อมูลย้อนหลัง + snapshot: Tick, Snapshot, Quotes (order book), Footprint, Historical Bars (single/batch, OHLCV: M1/M5/D ฯลฯ)
- **Data Streaming (MQTT)** — เรียลไทม์: Subscribe/Unsubscribe
- **Category:** `US_STOCK` (หุ้น), `US_ETF` (ETF)
- **Rate limit:** Data API = **300 req/60 วินาที**; Subscribe/Unsubscribe = ไม่จำกัด
- **ต้องซื้อ subscription แยกสำหรับ OpenAPI** (LV1/LV2) — subscription ในแอปมือถือ/QT ใช้กับ OpenAPI ไม่ได้ ถ้าได้ **403 = ยังไม่ได้ subscribe**

ดึง candlestick ย้อนหลัง (Python):
```python
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.core.client import ApiClient
from webull.data.data_client import DataClient

api_client = ApiClient("<app_key>", "<app_secret>", "th")
api_client.add_endpoint("th", "<api_endpoint>")
data_client = DataClient(api_client)

res = data_client.market_data.get_history_bar("AAPL", Category.US_STOCK.name, Timespan.M1.name)
# batch: get_batch_history_bar(["AAPL","TSLA"], Category.US_STOCK.name, Timespan.M1.name, 1)
```

Subscribe เรียลไทม์ (MQTT):
```python
from webull.data.common.category import Category
from webull.data.common.subscribe_type import SubscribeType
from webull.data.data_streaming_client import DataStreamingClient

c = DataStreamingClient("<app_key>", "<app_secret>", "th", "session_1",
                        http_host="<api_endpoint>", mqtt_host="<data_api_endpoint>")
def on_connect(client, api_client, sid):
    client.subscribe(["AAPL"], Category.US_STOCK.name,
                     [SubscribeType.QUOTE.name, SubscribeType.SNAPSHOT.name, SubscribeType.TICK.name])
c.on_connect_success = on_connect
c.on_quotes_message = lambda client, topic, q: print(topic, q)
c.connect_and_loop_forever()
```

## Accounts

ลำดับงานมาตรฐาน: `get_account_list()` → เก็บ `account_id` → query `balance` / `positions`
```python
trade_client.account_v2.get_account_list()        # ทุกบัญชีภายใต้ credential
trade_client.account_v2.get_account_balance(account_id)   # buying power, cash
trade_client.account_v2.get_account_position(account_id)  # holdings
```

## Trading — วงจรออเดอร์

Lifecycle: **Preview → Place → Replace → Cancel** (ใช้ `trade_client.order_v3.*`)

```python
import uuid
client_order_id = uuid.uuid4().hex
new_orders = [{
    "combo_type": "NORMAL", "client_order_id": client_order_id,
    "symbol": "AAPL", "instrument_type": "EQUITY", "market": "US",
    "order_type": "LIMIT", "limit_price": "180", "quantity": "1",
    "side": "BUY", "time_in_force": "DAY", "entrust_type": "QTY",
    "support_trading_session": "CORE"
}]
trade_client.order_v3.preview_order(account_id, new_orders)   # ประเมินค่าธรรมเนียมก่อน
trade_client.order_v3.place_order(account_id, new_orders)
trade_client.order_v3.replace_order(account_id, [{"client_order_id": client_order_id, "quantity": "2", "limit_price": "179"}])
trade_client.order_v3.cancel_order(account_id, client_order_id)
trade_client.order_v3.get_order_detail(account_id, client_order_id)
```

### รับสถานะออเดอร์เรียลไทม์ (gRPC — Trade Events)

หลัง place order ให้ subscribe เพื่อรู้ทันทีเมื่อ filled/cancelled/failed แทนการ poll `get_order_detail` ซ้ำๆ:
```python
from webull.trade.events.types import ORDER_STATUS_CHANGED, EVENT_TYPE_ORDER
from webull.trade.trade_events_client import TradeEventsClient

def on_event(event_type, subscribe_type, payload, raw_message):
    if event_type == EVENT_TYPE_ORDER and subscribe_type == ORDER_STATUS_CHANGED:
        print("Order update:", payload)

events_client = TradeEventsClient("<app_key>", "<app_secret>", "th")
events_client.on_events_message = on_event
events_client.do_subscribe([account_id])
```

พารามิเตอร์สำคัญ:
| Parameter | จำเป็น | หมายเหตุ |
|---|---|---|
| `client_order_id` | ✓ | unique ต่อบัญชี, max 32 chars |
| `combo_type` | ✓ | `NORMAL` = ออเดอร์เดี่ยวปกติ |
| `instrument_type` | ✓ | `EQUITY` สำหรับหุ้น/ETF |
| `order_type` | ✓ | `MARKET` / `LIMIT` / `STOP_LOSS` / `STOP_LOSS_LIMIT` |
| `side` | ✓ | `BUY` / `SELL` |
| `entrust_type` | ✓ | `QTY` (ตามจำนวนหุ้น) |
| `time_in_force` | ✓ | `DAY` / `GTC` |
| `limit_price` | เงื่อนไข | ต้องมีเมื่อ `LIMIT`, `STOP_LOSS_LIMIT` |
| `stop_price` | เงื่อนไข | ต้องมีเมื่อ `STOP_LOSS`, `STOP_LOSS_LIMIT` |
| `support_trading_session` | — | `CORE` = ชั่วโมงเทรดปกติ |

## การแก้ปัญหาที่พบบ่อย

| อาการ | สาเหตุ / วิธีแก้ |
|---|---|
| **`INVALID_TOKEN` ทั้งที่ token ถูก** | ⭐ ส่วนใหญ่คือ **signature mismatch ไม่ใช่ปัญหา token** — สาเหตุยอดฮิต: ใช้ `json=body` ใน `requests.post()` ทำให้ string ที่ส่งต่างจากที่คำนวณ SHA256, มีช่องว่างเกินใน JSON, หรือ URL-encode ผิด → ต้อง serialize เอง `json.dumps(body, separators=(',',':'))` แล้วส่งด้วย `data=body_string` (ดู `references/signature.md`) |
| `403 Forbidden` (เทรด) | ขาด auth headers, credential ไม่ถูก, หรือ**สิทธิ์บัญชีไม่พอ** (ยังไม่ได้เซ็น trading agreement) |
| `403` เวลาดึง market data | ยังไม่ได้ซื้อ OpenAPI market data subscription (LV1/LV2) — คนละอันกับแอป |
| ออเดอร์ถูก reject | buying power/margin ไม่พอ, ตลาดปิด, พารามิเตอร์ผิด (ราคานอกช่วง/order type ไม่รองรับ), หรือขาด trading agreement — อ่าน error message ใน response |
| Signature ไม่ผ่าน | ตรวจ: เรียง param A→Z, ต่อ `&` ท้าย app_secret, SHA256 ของ body เป็นตัวพิมพ์ใหญ่, ไม่มีช่องว่างเกินใน body, URL-encode `str3` ก่อน HMAC, ภาษาที่ escape `<>&` อัตโนมัติ (เช่น Go) ต้อง unescape ก่อน → ดู `references/signature.md` |
| ต้องยืนยัน 2FA | Token ครั้งแรกต้องยืนยันในแอป Webull หนึ่งครั้ง จากนั้น reuse ได้ |
| MQTT LV1/LV2 ต่อไม่ได้ | อนุญาตให้ **1 device** เข้าถึงข้อมูล LV1/LV2 พร้อมกันเท่านั้น |
| โดน rate limit | ดูลิมิตรายendpoint ใน `references/endpoints.md` (เช่น place/cancel order = 15/s, account = 10/30s, Data API = 300/60s) — throttle หรือ batch |
| สมัคร API รออนุมัตินานไหม | ปกติ 1–2 วันทำการ · ระหว่างรอใช้ test environment ได้เลย · **ไม่มีค่าธรรมเนียมเพิ่ม**สำหรับเทรดผ่าน OpenAPI (ค่าธรรมเนียมเท่าในแอป) |

## ข้อมูลอ้างอิงเพิ่มเติม

- `references/signature.md` — อัลกอริทึม signature 3 ขั้น พร้อม worked example ที่ verify ได้ (ผลลัพธ์ = `kvlS6opdZDhEBo5jq40nHYXaLvM=`)
- `references/endpoints.md` — hosts ทุก environment, test accounts, category/timespan enums
- `references/lego.md` — กลยุทธ์ **Shannon Demon LEGO**: flowchart (แผนภาพ A/B), DNA gate, decision PASS/BUY/SELL, recurrence Rₙ/ΔAₙ/Aₙ/Eₙ, 17 คอลัมน์, Step 18 persistence **+ "🎯 ขั้นตอนการเทรดจริง 7 ขั้น (LEGO × Webull OpenAPI, region th)"** ที่เชื่อม decision เข้ากับ API call จริง (get position/snapshot → decision → place MARKET order) พร้อม checklist ให้เทรดสำเร็จ — ใช้เมื่อผู้ใช้ขอ diagram หรือถามวิธีรันกลยุทธ์นี้จริงบน Webull (ยึดโค้ดจริงเสมอถ้ามี)

เอกสารต้นฉบับ (HTML) อยู่ในโฟลเดอร์โปรเจกต์นี้ — อ้างอิงได้ถ้าต้องการรายละเอียด endpoint ที่ลึกกว่านี้
