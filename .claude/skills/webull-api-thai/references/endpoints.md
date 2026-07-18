# Endpoints, Test Accounts และ Enums อ้างอิง

## Hosts ตาม Environment (regionId = `"th"`)

### Production
| Service | Host |
|---|---|
| Trading API | `api.webull.co.th` |
| Market Data API | `api.webull.co.th` |
| Trading Events (gRPC) | `events-api.webull.co.th` |
| Market Data Streaming (MQTT) | `data-api.webull.co.th` |

### Test (UAT)
| Service | Host |
|---|---|
| Trading API | `th-api.uat.webullbroker.com` |
| Market Data API | `th-api.uat.webullbroker.com` |
| Trading Events (gRPC) | `th-events-api.uat.webullbroker.com` |
| Market Data Streaming (MQTT) | `data-api.uat.webullbroker.com` |

## Test Accounts (แชร์สาธารณะ — ใช้กับ Test env เท่านั้น)

> ⚠️ แชร์กันหลายคน orders/positions เปลี่ยนได้ตลอด อย่าใช้กับ production หรือข้อมูลจริง

| No. | Account ID | App Key | App Secret |
|---|---|---|---|
| 1 | 1249711087713001472 | 86d0dac12b1b28de7539f087b2c1dca7 | 28dfb45a1be192a04242efd1aeba20f6 |
| 2 | 1251161604462252032 | 0e3bd1b4a9d94857aee5410f9b769121 | e5e0f0046235380d5f6d3cebfe6d9696 |
| 3 | 1251161573554425856 | c12c25c93f98169ad56f5148e4edfd16 | f3ac0da97d2085ad4ce14b961cbd8824 |

## Market Data — HTTP endpoints (Data API)

| Endpoint | คำอธิบาย |
|---|---|
| Tick | รายการซื้อขาย tick-by-tick ในช่วงเวลาที่ระบุ |
| Snapshot | snapshot ราคาล่าสุด, การเปลี่ยนแปลง, volume |
| Quotes | order book ตาม depth (ราคา/จำนวน/orders) |
| Footprint | order flow และ volume profile |
| Historical Bars (single) | OHLCV แท่งเทียนหลาย granularity |
| Historical Bars (batch) | OHLCV หลาย symbol พร้อมกัน |

ตัวอย่าง raw HTTP request:
```
GET /openapi/market-data/stock/snapshot?symbols=AAPL&category=US_STOCK&extend_hour_required=false&overnight_required=false
```

Response ตัวอย่าง (ราคาเป็น string, time เป็น Unix millis):
```json
[{"symbol":"AAPL","instrument_id":"913256135","price":"185.50","open":"184.00",
  "high":"186.20","low":"183.80","volume":"52340000","change":"1.50",
  "change_ratio":"0.0082","pre_close":"184.00","last_trade_time":1710849600000}]
```

## Enums ที่ใช้บ่อย

**Category:** `US_STOCK`, `US_ETF`

**Timespan (bars):** `M1`, `M5`, `D` … (นาที/วัน)

**SubscribeType (MQTT):** `QUOTE`, `SNAPSHOT`, `TICK`

**Order:**
- `order_type`: `MARKET`, `LIMIT`, `STOP_LOSS`, `STOP_LOSS_LIMIT`
- `side`: `BUY`, `SELL`
- `time_in_force`: `DAY`, `GTC`
- `entrust_type`: `QTY`
- `combo_type`: `NORMAL`
- `instrument_type`: `EQUITY`
- `support_trading_session`: `CORE`
- `market`: `US`

## Trading API — endpoints

| กลุ่ม | Endpoint | Rate Limit |
|---|---|---|
| Instruments | Stock Instruments (ดึงรายละเอียด symbol) | 60/60s |
| Account | Account List / Balance / Positions | 10/30s (ต่ออัน) |
| Orders | Preview / Place / Replace / Cancel Order | 15/s (ต่ออัน) |
| Orders | Order History / Open Orders / Order Detail | 40/2s (ต่ออัน) |
| Events | Trade Event Subscription (gRPC) | — (streaming) |

**Feature Matrix (US Stocks):** Market ✓ · Limit ✓ · Stop Loss ✓ · Stop Loss Limit ✓ (รองรับหุ้น US เท่านั้น)

## Rate Limits (สรุป)

| API | Limit |
|---|---|
| Data API (HTTP) | 300 requests / 60 วินาที |
| Streaming Subscribe/Unsubscribe (MQTT) | ไม่จำกัด |
| Trading — ดูรายละเอียดรายendpoint ตารางด้านบน | แตกต่างกันต่อ endpoint |

## Requirements SDK

| ภาษา | เวอร์ชัน |
|---|---|
| Python | 3.8 – 3.13 · `pip3 install --upgrade webull-openapi-python-sdk` |
| Java | JDK 8+ · Maven `com.webull.openapi:webull-openapi-java-sdk:1.0.3` |
